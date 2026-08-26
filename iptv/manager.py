"""IPTVManager — orchestrates sources, parsing, favorites/recent, and threads.

This is the bridge between the GUI and the rest of the ``iptv`` package. It
owns the cache, artwork cache, metadata pipeline, and EPG manager, and runs
all network/parse work on background threads, reporting progress and results
through Qt signals emitted on the GUI thread.

The GUI only ever talks to this manager (and to the player widget directly),
so the rest of the subsystem stays Qt-free and unit-testable.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from . import classify, local_folder, m3u_parser, xtream
from .artwork import ArtworkCache
from .cache import IPTVCache, default_data_dir
from .epg import EPGManager
from .framegrab import FrameGrabber
from .metadata import MetadataPipeline, metadata_key
from .models import (
    ALL_SECTIONS,
    SECTION_FAVORITES,
    SECTION_LIVE,
    SECTION_MOVIES,
    SECTION_RECENT,
    SECTION_SERIES,
    Category,
    Channel,
    Movie,
    Playlist,
    PlaylistSource,
    Series,
)

logger = logging.getLogger(__name__)

# Sidebar bucket for VOD entries whose release year is unknown (no year in
# the name and no metadata lookup yet). Lives here so the GUI and the manager
# share one spelling; a 4-digit year can never collide with it.
YEAR_OTHERS = "Others"


class IPTVManager:
    """Coordinates IPTV data flow. Qt-agnostic; uses callbacks for signals."""

    def __init__(
        self,
        sources: List[PlaylistSource],
        tmdb_api_key: str = "",
        data_dir: Optional[str] = None,
        cache_seconds: int = 8,
        hwdec: str = "auto-safe",
        tpdb_api_key: str = "",
        framegrab_posters: bool = True,
    ) -> None:
        self.sources: List[PlaylistSource] = list(sources)
        self.data_dir = data_dir or default_data_dir()
        self.cache = IPTVCache(self.data_dir)
        self.artwork = ArtworkCache(os.path.join(self.data_dir, "artwork"))
        self.metadata = MetadataPipeline(self.cache, tmdb_api_key=tmdb_api_key,
                                         tpdb_api_key=tpdb_api_key)
        # Last-resort poster source when no provider matches (see framegrab.py).
        self.framegrab = FrameGrabber(self.artwork) if framegrab_posters else None
        self.epg = EPGManager(self.cache)
        self.cache_seconds = cache_seconds
        self.hwdec = hwdec

        # The currently loaded playlist (per source) and the active source id.
        self._playlists: Dict[str, Playlist] = {}
        self._active_source_id: Optional[str] = None
        self._lock = threading.RLock()

        # Background work tracking.
        self._cancel_flags: Dict[str, threading.Event] = {}
        self._refresh_thread: Optional[threading.Thread] = None
        # Set when load_all_async is called while a sweep is already running:
        # the worker re-runs once with the latest source list instead of
        # downloading the same playlists in parallel (providers answer
        # concurrent duplicate downloads with connection resets).
        self._reload_requested = False
        # source_id -> Event, present while that source is being refreshed.
        self._refresh_events: Dict[str, threading.Event] = {}

    # -- source management ---------------------------------------------------
    def set_sources(self, sources: List[PlaylistSource]) -> None:
        with self._lock:
            self.sources = list(sources)

    def set_tmdb_key(self, key: str) -> None:
        self.metadata.set_tmdb_key(key)

    def set_tpdb_key(self, key: str) -> None:
        self.metadata.set_tpdb_key(key)

    def active_source(self) -> Optional[PlaylistSource]:
        with self._lock:
            for s in self.sources:
                if s.id == self._active_source_id:
                    return s
            return self.sources[0] if self.sources else None

    def set_active_source(self, source_id: str) -> None:
        with self._lock:
            self._active_source_id = source_id

    # -- loading -------------------------------------------------------------
    def load_source_async(
        self,
        source: PlaylistSource,
        on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
        on_done: Optional[Callable[[bool, Playlist], None]] = None,
        use_cache: bool = True,
    ) -> threading.Thread:
        """Load a source in a background thread. ``on_done(ok, playlist)`` off-thread."""
        cancel = threading.Event()
        with self._lock:
            self._cancel_flags[source.id] = cancel

        def _worker() -> None:
            ok = False
            playlist = Playlist(source_id=source.id)
            try:
                # Try cached playlist first for instant startup.
                if use_cache:
                    cached = self.cache.load_playlist(source.id)
                    if cached:
                        playlist = _playlist_from_cache(source.id, cached)
                        with self._lock:
                            self._playlists[source.id] = playlist
                            # Only claim "active" if nothing else has: with
                            # every source loading at once, the last one to
                            # finish must not yank the user's selection.
                            self._active_source_id = self._active_source_id or source.id
                        if on_done:
                            on_done(True, playlist)
                        # Then refresh in the background, and notify on_done
                        # again when the fresh data is in so the GUI can
                        # rebuild — otherwise the UI shows the stale cache
                        # until the next manual reload.
                        def _bg_refresh() -> None:
                            try:
                                pl = self._refresh_source(source, on_progress, on_done, True)
                                if on_done:
                                    on_done(pl.total > 0, pl)
                            except Exception:
                                logger.exception("background refresh failed for %s", source.name)

                        threading.Thread(target=_bg_refresh, daemon=True).start()
                        return

                playlist = self._refresh_source(source, on_progress, on_done, False)
                ok = playlist.total > 0
            except Exception:
                logger.exception("load_source_async failed for %s", source.name)
            finally:
                with self._lock:
                    self._cancel_flags.pop(source.id, None)
                if on_done:
                    on_done(ok, playlist)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    def _refresh_source(
        self,
        source: PlaylistSource,
        on_progress,
        on_done,
        is_background_refresh: bool,
    ) -> Playlist:
        """Refresh one source — never twice in parallel.

        A single-source load (agent/CLI) can overlap the GUI's sweep, and two
        concurrent downloads of the same playlist trip provider rate limits
        (connection resets). Followers wait for the in-flight refresh and
        serve whatever it produced."""
        with self._lock:
            done_ev = self._refresh_events.get(source.id)
            if done_ev is None:
                done_ev = threading.Event()
                self._refresh_events[source.id] = done_ev
                leader = True
            else:
                leader = False
        if not leader:
            logger.info(
                "Refresh already in progress for %s — serving its result",
                source.name)
            done_ev.wait(timeout=180)
            with self._lock:
                existing = self._playlists.get(source.id)
            return existing if existing is not None else Playlist(source_id=source.id)
        try:
            return self._refresh_source_inner(
                source, on_progress, on_done, is_background_refresh)
        finally:
            with self._lock:
                self._refresh_events.pop(source.id, None)
            done_ev.set()

    def _refresh_source_inner(
        self,
        source: PlaylistSource,
        on_progress,
        on_done,
        is_background_refresh: bool,
    ) -> Playlist:
        cancel = self._cancel_flags.get(source.id)
        is_cancelled = (lambda: cancel.is_set()) if cancel else None

        if source.kind == "local_folder":
            pl = local_folder.scan_folder(
                source, on_progress=on_progress, is_cancelled=is_cancelled)
        elif source.kind == "xtream":
            if not source.has_credentials():
                pl = Playlist(source_id=source.id)
            else:
                pl = xtream.load_playlist(source, on_progress=on_progress, is_cancelled=is_cancelled)
        else:
            text = None
            path = None
            headers = {}
            if source.user_agent:
                headers["User-Agent"] = source.user_agent
            if source.referer:
                headers["Referer"] = source.referer
            if source.kind == "m3u_file":
                path = source.url
            else:
                # m3u_url — download. IPTV servers are often flaky (TLS resets,
                # dropped connections), so retry a few times with backoff
                # before giving up.
                text = self._download_m3u(source, headers, is_cancelled)
                if text is None:
                    with self._lock:
                        keep = self._playlists.get(source.id)
                        if keep is not None and keep.total > 0:
                            keep.stale = True
                        else:
                            keep = None
                    if keep is not None:
                        # Refresh failed but a previous (cached) copy is in
                        # memory — keep serving it instead of blanking the
                        # source for the rest of the session.
                        logger.warning(
                            "Refresh failed for %s — keeping the cached "
                            "playlist (%d entries)", source.name, keep.total)
                        return keep
                    pl = Playlist(source_id=source.id)
                    with self._lock:
                        self._playlists[source.id] = pl
                        self._active_source_id = self._active_source_id or source.id
                    return pl
            pl = m3u_parser.parse_m3u(
                source.id, text=text, path=path, on_progress=on_progress, is_cancelled=is_cancelled
            )

        classify.classify(pl)
        classify.populate_years(pl)
        _rebuild_categories(pl)

        # A per-source EPG URL (entered in settings) wins over / fills in for
        # the playlist's own url-tvg declaration.
        if source.epg_url and source.epg_url != pl.url_tvg:
            pl.url_tvg = source.epg_url

        # Persist to cache.
        self.cache.save_playlist(source.id, _playlist_to_cache(pl), url_tvg=pl.url_tvg)

        # Mark favorites on the loaded items.
        self._apply_favorites(pl)

        with self._lock:
            self._playlists[source.id] = pl
            self._active_source_id = self._active_source_id or source.id

        # Kick off EPG if the playlist declares an XMLTV url.
        if pl.url_tvg:
            self.epg.update_async(pl.url_tvg, headers=headers)

        return pl

    @staticmethod
    def _download_m3u(
        source: PlaylistSource,
        headers: Dict[str, str],
        is_cancelled: Optional[Callable[[], bool]],
        attempts: int = 4,
    ) -> Optional[str]:
        """Download the M3U text with retries. Returns None on failure.

        Decoding is forced to UTF-8: most providers omit the charset header,
        which makes requests fall back to ISO-8859-1 and garble non-ASCII
        channel names."""
        for attempt in range(attempts):
            if is_cancelled and is_cancelled():
                return None
            try:
                r = requests.get(source.url, headers=headers or None, timeout=30)
                r.raise_for_status()
                text = r.content.decode("utf-8", errors="replace")
                if not m3u_parser.looks_like_m3u(text):
                    # Not a playlist at all (HTML error page or an XMLTV EPG
                    # endpoint pasted as the playlist URL). Retrying won't
                    # help — fail fast so the GUI shows the check-URL hint.
                    logger.warning(
                        "M3U download for %s did not return a playlist "
                        "(payload looks like XML/HTML) — check the source URL",
                        source.name,
                    )
                    return None
                return text
            except Exception as exc:
                logger.warning(
                    "M3U download failed for %s (attempt %d/%d): %s",
                    source.name, attempt + 1, attempts, exc,
                )
                if attempt + 1 < attempts:
                    # 2s / 5s / 15s — ride out provider rate-limit windows
                    # instead of burning every attempt inside a few seconds.
                    time.sleep((2, 5, 15)[min(attempt, 2)])
        return None

    def load_all_async(
        self,
        on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
        on_done: Optional[Callable[[bool, Playlist], None]] = None,
        use_cache: bool = True,
    ) -> threading.Thread:
        """Load every enabled source, one after another, off the caller's thread.

        ``on_done(ok, playlist)`` fires per source as each finishes, so the UI
        can show the first provider immediately and fill the rest in behind
        it. Sources are loaded sequentially rather than in parallel: each one
        is a large download plus a parse of (frequently) >100k entries, and
        running them together just starves whichever the user is looking at.

        Only one sweep runs at a time. A call made while a sweep is still
        running (e.g. the user saves settings mid-load) does NOT start a
        parallel one — it flags a re-run, and the worker loops once more with
        the latest source list after finishing the current pass.
        """
        with self._lock:
            if self._refresh_thread is not None and self._refresh_thread.is_alive():
                self._reload_requested = True
                logger.info("load already in progress — coalesced into a re-run")
                return self._refresh_thread
            self._reload_requested = False

        def _worker() -> None:
            while True:
                with self._lock:
                    sources = [s for s in self.sources if s.enabled]
                    self._reload_requested = False
                for src in sources:
                    try:
                        self._load_one(src, on_progress, on_done, use_cache)
                    except Exception:
                        logger.exception("load failed for source %s", src.name)
                        if on_done:
                            on_done(False, Playlist(source_id=src.id))
                with self._lock:
                    if not self._reload_requested:
                        break

        t = threading.Thread(target=_worker, daemon=True)
        with self._lock:
            self._refresh_thread = t
        t.start()
        return t

    def _load_one(self, source: PlaylistSource, on_progress, on_done, use_cache: bool) -> None:
        """Blocking load of a single source (shared by load_all_async)."""
        if use_cache:
            cached = self.cache.load_playlist(source.id)
            if cached:
                playlist = _playlist_from_cache(source.id, cached)
                with self._lock:
                    self._playlists[source.id] = playlist
                    self._active_source_id = self._active_source_id or source.id
                if on_done:
                    on_done(True, playlist)
                # Cache shown; refresh in place so stale data doesn't stick.
                pl = self._refresh_source(source, on_progress, on_done, True)
                if on_done:
                    on_done(pl.total > 0, pl)
                return
        pl = self._refresh_source(source, on_progress, on_done, False)
        if on_done:
            on_done(pl.total > 0, pl)

    def cancel_load(self, source_id: str) -> None:
        with self._lock:
            ev = self._cancel_flags.get(source_id)
        if ev:
            ev.set()

    # -- accessors -----------------------------------------------------------
    def current_playlist(self) -> Optional[Playlist]:
        with self._lock:
            sid = self._active_source_id
            return self._playlists.get(sid) if sid else None

    def playlist_for(self, source_id: str) -> Optional[Playlist]:
        """The loaded playlist for one source, or None if it isn't loaded yet."""
        with self._lock:
            return self._playlists.get(source_id)

    def loaded_source_ids(self) -> List[str]:
        """Source ids that currently have a playlist in memory."""
        with self._lock:
            return list(self._playlists.keys())

    @staticmethod
    def source_id_of(item: Any) -> str:
        """Recover an item's owning source from its id.

        :func:`iptv.models.make_id` builds ids as ``<source_id>::<hash>``, so
        an item always knows where it came from. That matters once several
        playlists are loaded at once: favorites and history must be filed
        against the item's real source, not whichever one happens to be
        active."""
        iid = getattr(item, "id", "") or ""
        return iid.split("::", 1)[0] if "::" in iid else ""

    def _playlist_of(self, item: Any) -> Optional[Playlist]:
        sid = self.source_id_of(item)
        if sid:
            with self._lock:
                pl = self._playlists.get(sid)
            if pl is not None:
                return pl
        return self.current_playlist()

    def items_for(self, section: str, category: str = "",
                  source_id: Optional[str] = None, year: str = "") -> List[Any]:
        """Return the items for a section, optionally filtered.

        ``source_id`` selects a specific loaded playlist; the active one is
        used when it is None. ``year`` filters Movies/Series by release year;
        pass ``YEAR_OTHERS`` for entries whose year is unknown."""
        pl = self.playlist_for(source_id) if source_id else self.current_playlist()
        if pl is None:
            return []
        if section == SECTION_LIVE:
            items = pl.channels
        elif section == SECTION_MOVIES:
            items = pl.movies
        elif section == SECTION_SERIES:
            items = pl.series
        else:
            items = []
        if category:
            items = [i for i in items if (i.group or "") == category]
        if year:
            items = [i for i in items
                     if (getattr(i, "year", "") or YEAR_OTHERS) == year]
        return items

    def years_for(self, section: str, source_id: Optional[str] = None,
                  category: str = "") -> List[Category]:
        """Release-year buckets for a VOD section, newest first.

        The group-by-year counterpart of :meth:`categories_for`. ``category``
        scopes the buckets to one provider group so year mode keeps the
        provider's own separation (e.g. "Movie VOD" vs "XXX VOD"). Entries
        with no known year land in a trailing "Others" category."""
        if section not in (SECTION_MOVIES, SECTION_SERIES):
            return []
        counts: Dict[str, int] = {}
        for it in self.items_for(section, category, source_id=source_id):
            y = getattr(it, "year", "") or YEAR_OTHERS
            counts[y] = counts.get(y, 0) + 1
        return _year_counts_to_categories(counts, section)

    def years_by_category(self, section: str,
                          source_id: Optional[str] = None
                          ) -> Dict[str, List[Category]]:
        """{category: year buckets} for a whole VOD section in ONE pass.

        The sidebar rebuild needs buckets for every category at once; calling
        :meth:`years_for` per category would re-scan the section each time
        (thousands of categories x tens of thousands of items)."""
        if section not in (SECTION_MOVIES, SECTION_SERIES):
            return {}
        per_cat: Dict[str, Dict[str, int]] = {}
        for it in self.items_for(section, source_id=source_id):
            g = getattr(it, "group", "") or "Uncategorized"
            y = getattr(it, "year", "") or YEAR_OTHERS
            counts = per_cat.setdefault(g, {})
            counts[y] = counts.get(y, 0) + 1
        return {g: _year_counts_to_categories(c, section)
                for g, c in per_cat.items()}

    def search(self, query: str) -> Dict[str, List[Any]]:
        """Global search across names + categories. Returns per-section lists."""
        pl = self.current_playlist()
        if pl is None or not query:
            return {}
        q = query.lower()
        out: Dict[str, List[Any]] = {}
        for section, items in (
            (SECTION_LIVE, pl.channels),
            (SECTION_MOVIES, pl.movies),
            (SECTION_SERIES, pl.series),
        ):
            hits = [
                i for i in items
                if q in (i.name or "").lower() or q in (i.group or "").lower()
            ]
            if hits:
                out[section] = hits
        return out

    def categories_for(self, section: str,
                       source_id: Optional[str] = None) -> List[Category]:
        pl = self.playlist_for(source_id) if source_id else self.current_playlist()
        if pl is None:
            return []
        return [c for c in pl.categories if c.section == section]

    # -- favorites & recent --------------------------------------------------
    def toggle_favorite(self, item: Any) -> bool:
        # File against the item's OWN source: with several playlists loaded,
        # the active one is often not the one this item came from.
        sid = self.source_id_of(item)
        if not sid:
            pl = self.current_playlist()
            sid = pl.source_id if pl else ""
        if not sid:
            return False
        section = getattr(item, "section", SECTION_LIVE)
        fav = not getattr(item, "favorite", False)
        if fav:
            self.cache.add_favorite(sid, item.id, section)
        else:
            self.cache.remove_favorite(sid, item.id)
        item.favorite = fav
        return fav

    def _apply_favorites(self, pl: Playlist) -> None:
        favs = {iid: True for iid, _s in self.cache.favorites(pl.source_id)}
        for i in pl.channels + pl.movies + pl.series:
            i.favorite = i.id in favs

    def favorites(self, source_id: Optional[str] = None) -> List[Any]:
        pl = self.playlist_for(source_id) if source_id else self.current_playlist()
        if pl is None:
            return []
        return [i for i in (pl.channels + pl.movies + pl.series) if i.favorite]

    def record_recent(self, item: Any) -> None:
        sid = self.source_id_of(item)
        if not sid:
            pl = self.current_playlist()
            sid = pl.source_id if pl else ""
        if sid:
            self.cache.add_recent(sid, item.id, getattr(item, "section", SECTION_LIVE), item.name, getattr(item, "url", ""))

    def recent(self, source_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if source_id:
            return self.cache.recent(source_id)
        pl = self.current_playlist()
        if pl is None:
            return []
        return self.cache.recent(pl.source_id)

    # -- metadata / artwork delegation --------------------------------------
    def resolve_metadata(self, section: str, name: str, year: str, on_done,
                         group: str = "") -> None:
        self.metadata.resolve_async(section, name, year, on_done, group=group)

    def fetch_artwork(self, url: str, on_done, headers: Optional[dict] = None,
                      priority: Optional[int] = None) -> None:
        kwargs = {} if priority is None else {"priority": priority}
        self.artwork.fetch_async(url, on_done, headers=headers, **kwargs)

    def channel_logo(self, name: str, tvg_logo: str) -> str:
        """Return the best logo URL for a channel: tvg-logo, else iptv-org."""
        if tvg_logo:
            return tvg_logo
        return self.metadata.channel_logo_fallback(name)

    def resolve_channel_logo_async(self, channel: Any, on_done: Callable[[Any, str], None]) -> None:
        """Resolve a channel logo off the caller's thread.

        The iptv-org fallback may download a multi-MB index on first use —
        that must never run on the GUI thread. ``on_done(channel, url)`` is
        invoked from a worker thread (url may be '' when nothing matches)."""
        if channel.logo:
            on_done(channel, channel.logo)
            return

        def _work() -> None:
            try:
                on_done(channel, self.metadata.channel_logo_fallback(channel.name))
            except Exception:
                logger.debug("logo resolve failed for %s", channel.name, exc_info=True)

        self.artwork.submit_task(_work)

    def resolve_poster_async(self, item: Any, on_done: Callable[[Any, str], None],
                             allow_framegrab: bool = False) -> None:
        """Resolve missing movie/series artwork via TMDb/TVmaze/TPDB.

        The VOD equivalent of :meth:`resolve_channel_logo_async`: playlists
        ship a few percent of entries with no ``tvg-logo``, and those tiles
        would otherwise never get a poster. ``on_done(item, url)`` is invoked
        from a worker thread (url may be '').

        ``allow_framegrab`` opts into the FFmpeg last resort when no provider
        matched. It must only be set for tiles the user is actually looking
        at — each grab opens a video connection to their IPTV provider, so
        letting the background sweep do it would mean tens of thousands of
        stream opens.
        """
        poster = getattr(item, "poster", "") or getattr(item, "logo", "")
        if poster:
            on_done(item, poster)
            return
        section = getattr(item, "section", SECTION_MOVIES)
        name = getattr(item, "name", "")
        year = getattr(item, "year", "")
        group = getattr(item, "group", "")

        def _fallback() -> None:
            """No provider matched — grab a frame from the stream itself."""
            grabber = self.framegrab
            stream_url = getattr(item, "url", "")
            if not (allow_framegrab and grabber and stream_url):
                on_done(item, "")
                return
            grabber.grab_async(stream_url, lambda url: on_done(item, url))

        def _done(_key: str, meta: Dict[str, Any]) -> None:
            meta = meta or {}
            # Enrich the group-by-year view as a side effect: series names
            # often carry no year, so the provider's release/first-air date
            # is the only source. Counts refresh on the next sidebar rebuild.
            if meta.get("year") and not getattr(item, "year", ""):
                item.year = meta["year"]
            url = meta.get("poster", "") or meta.get("backdrop", "")
            if url:
                on_done(item, url)
            else:
                _fallback()

        def _work() -> None:
            try:
                self.metadata.resolve_async(section, name, year, _done, group=group)
            except Exception:
                logger.debug("poster resolve failed for %s", name, exc_info=True)
                _fallback()

        # Kept off the caller's thread: resolve_async hits SQLite (and may call
        # back synchronously) before queueing the network lookup.
        self.artwork.submit_task(_work)

    def epg_now_next(self, tvg_id: str) -> Dict[str, str]:
        return self.epg.now_next(tvg_id)

    # -- lifecycle -----------------------------------------------------------
    def shutdown(self) -> None:
        for ev in list(self._cancel_flags.values()):
            ev.set()
        try:
            self.artwork.shutdown()
        except Exception:
            pass
        try:
            self.metadata.shutdown()
        except Exception:
            pass
        if self.framegrab:
            try:
                self.framegrab.shutdown()
            except Exception:
                pass
        try:
            self.cache.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# (de)serialization helpers for the SQLite playlist cache
# ---------------------------------------------------------------------------


def _playlist_to_cache(pl: Playlist) -> Dict[str, Any]:
    import dataclasses

    def _asdict_list(items):
        return [dataclasses.asdict(i) for i in items]

    return {
        "channels": _asdict_list(pl.channels),
        "movies": _asdict_list(pl.movies),
        "series": _asdict_list(pl.series),
        "categories": _asdict_list(pl.categories),
        "url_tvg": pl.url_tvg,
    }


def _playlist_from_cache(source_id: str, data: Dict[str, Any]) -> Playlist:
    pl = Playlist(source_id=source_id, url_tvg=data.get("url_tvg", ""))
    for c in data.get("channels", []):
        pl.channels.append(Channel(**_filter_fields(Channel, c)))
    for m in data.get("movies", []):
        pl.movies.append(Movie(**_filter_fields(Movie, m)))
    for s in data.get("series", []):
        eps = s.pop("episodes", [])
        series = Series(**_filter_fields(Series, s))
        from .models import Episode
        for e in eps:
            series.episodes.append(Episode(**_filter_fields(Episode, e)))
        pl.series.append(series)
    for cat in data.get("categories", []):
        pl.categories.append(Category(**cat))
    # Playlists cached before years were populated get them backfilled from
    # names here — no cache invalidation needed for the upgrade.
    classify.populate_years(pl)
    return pl


def _filter_fields(cls, data: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only keys that are valid fields of ``cls``."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in fields}


def _year_counts_to_categories(counts: Dict[str, int], section: str) -> List[Category]:
    """Year -> count map as Category rows, newest first, "Others" trailing.

    4-digit years sort lexically, so a plain reverse sort is numeric order."""
    years = sorted((y for y in counts if y != YEAR_OTHERS), reverse=True)
    cats = [Category(name=y, section=section, count=counts[y]) for y in years]
    if YEAR_OTHERS in counts:
        cats.append(Category(name=YEAR_OTHERS, section=section,
                             count=counts[YEAR_OTHERS]))
    return cats


def _rebuild_categories(pl: Playlist) -> None:
    """Recompute the Category list (with counts) from the classified items."""
    cats: Dict[tuple, Category] = {}
    for section, items in (
        (SECTION_LIVE, pl.channels),
        (SECTION_MOVIES, pl.movies),
        (SECTION_SERIES, pl.series),
    ):
        for it in items:
            g = it.group or "Uncategorized"
            key = (section, g)
            c = cats.get(key)
            if c is None:
                c = Category(name=g, section=section)
                cats[key] = c
            c.count += 1
    pl.categories = list(cats.values())
