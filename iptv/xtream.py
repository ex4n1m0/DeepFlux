"""Xtream Codes API client.

Talks to a provider's ``player_api.php`` endpoint to fetch Live, VOD, and
Series categories + content. Credentials are never logged.

The Xtream API shape (abbreviated):

    GET player_api.php?username=..&password=..
        -> user_info, server_info, plus a flat list of categories for live/vod/series

    GET player_api.php?...&action=get_live_categories
    GET player_api.php?...&action=get_vod_categories
    GET player_api.php?...&action=get_series_categories
    GET player_api.php?...&action=get_live_streams&category_id=..
    GET player_api.php?...&action=get_vod_streams&category_id=..
    GET player_api.php?...&action=get_series&category_id=..
    GET player_api.php?...&action=get_series_info&series_id=..
    GET player_api.php?...&action=get_vod_info&vod_id=..

Stream URLs are constructed from server_info + stream_type:
    Live:  http://host:port/live/<user>/<pass>/<stream_id>.m3u8 (or .ts)
    VOD:   http://host:port/movie/<user>/<pass>/<stream_id>.<container_extension>
    Series:http://host:port/series/<user>/<pass>/<episode_id>.<container_extension>
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin

import requests

from .models import (
    SECTION_LIVE,
    SECTION_MOVIES,
    SECTION_SERIES,
    Category,
    Channel,
    Episode,
    Movie,
    Playlist,
    PlaylistSource,
    Series,
    make_id,
)

logger = logging.getLogger(__name__)

# Never log credentials — mask the password in any debug URL.
_TIMEOUT = 30


def _base_url(src: PlaylistSource) -> str:
    url = (src.url or "").rstrip("/")
    if not url:
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    return url


def _api(src: PlaylistSource, **params: str) -> str:
    base = _base_url(src)
    q = urlencode(
        {
            "username": src.username,
            "password": src.password,
            **params,
        }
    )
    return f"{base}/player_api.php?{q}"


def _get(src: PlaylistSource, action: str, **extra: str) -> Any:
    headers = {}
    if src.user_agent:
        headers["User-Agent"] = src.user_agent
    if src.referer:
        headers["Referer"] = src.referer
    url = _api(src, action=action, **extra)
    # Log without credentials.
    safe = url.replace(src.password, "***") if src.password else url
    logger.debug("Xtream request: %s", safe)
    resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data


def authenticate(src: PlaylistSource) -> Optional[Dict[str, Any]]:
    """Validate credentials; returns user_info/server_info or None on failure."""
    try:
        data = _get_raw(src)
    except Exception as exc:
        logger.warning("Xtream auth failed for %s: %s", src.name, exc)
        return None
    if not data or data.get("user_info", {}).get("auth", 0) != 1:
        logger.warning("Xtream auth rejected for %s", src.name)
        return None
    return data


def _get_raw(src: PlaylistSource) -> Any:
    """The bare auth call (no action) returns user/server info + categories."""
    headers = {}
    if src.user_agent:
        headers["User-Agent"] = src.user_agent
    if src.referer:
        headers["Referer"] = src.referer
    url = _api(src)
    resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def load_playlist(
    src: PlaylistSource,
    on_progress=None,
    is_cancelled=None,
) -> Playlist:
    """Fetch and assemble a full :class:`Playlist` from an Xtream source."""
    playlist = Playlist(source_id=src.id)
    info = authenticate(src)
    if info is None:
        return playlist

    server = info.get("server_info", {})
    host = server.get("url", _base_url(src))
    # server_info.url often omits the port while server_info.port has it —
    # append the port when the URL doesn't already carry one.
    port = str(server.get("port", "") or "")
    if port and port not in ("80", "443"):
        from urllib.parse import urlparse
        netloc = urlparse(host).netloc
        if ":" not in netloc:
            host = f"{host}:{port}"
    user = src.username
    pwd = src.password

    # --- Live ---
    try:
        live_cats = _get(src, action="get_live_categories") or []
        for cat in live_cats:
            cat_id = str(cat.get("category_id", ""))
            cat_name = cat.get("category_name", "Live")
            streams = _get(src, action="get_live_streams", category_id=cat_id) or []
            for s in streams:
                if is_cancelled and is_cancelled():
                    return playlist
                sid = str(s.get("stream_id", ""))
                ext = s.get("direct_source", "") and "ts" or "m3u8"
                url = f"{host}/live/{user}/{pwd}/{sid}.{ext}"
                playlist.channels.append(
                    Channel(
                        id=make_id(src.id, "live", sid),
                        name=s.get("name", ""),
                        url=url,
                        logo=s.get("stream_icon", ""),
                        tvg_id=str(s.get("epg_channel_id", "")),
                        group=cat_name,
                        section=SECTION_LIVE,
                        extra={"stream_id": sid, "category_id": cat_id},
                    )
                )
            playlist.categories.append(Category(name=cat_name, section=SECTION_LIVE, count=len(streams)))
            if on_progress:
                on_progress(len(playlist.channels), None)
    except Exception as exc:
        logger.warning("Xtream live fetch failed: %s", exc)

    # --- VOD (Movies) ---
    try:
        vod_cats = _get(src, action="get_vod_categories") or []
        for cat in vod_cats:
            cat_id = str(cat.get("category_id", ""))
            cat_name = cat.get("category_name", "Movies")
            streams = _get(src, action="get_vod_streams", category_id=cat_id) or []
            for s in streams:
                if is_cancelled and is_cancelled():
                    return playlist
                sid = str(s.get("stream_id", ""))
                ext = s.get("container_extension", "mp4") or "mp4"
                url = f"{host}/movie/{user}/{pwd}/{sid}.{ext}"
                playlist.movies.append(
                    Movie(
                        id=make_id(src.id, "vod", sid),
                        name=s.get("name", ""),
                        url=url,
                        logo=s.get("stream_icon", ""),
                        group=cat_name,
                        section=SECTION_MOVIES,
                        extra={"stream_id": sid, "category_id": cat_id},
                    )
                )
            playlist.categories.append(Category(name=cat_name, section=SECTION_MOVIES, count=len(streams)))
            if on_progress:
                on_progress(len(playlist.movies), None)
    except Exception as exc:
        logger.warning("Xtream VOD fetch failed: %s", exc)

    # --- Series ---
    try:
        series_cats = _get(src, action="get_series_categories") or []
        # First pass: collect all series stubs across categories.
        stubs: List[Series] = []
        cat_counts: List[Tuple[str, int]] = []
        for cat in series_cats:
            if is_cancelled and is_cancelled():
                return playlist
            cat_id = str(cat.get("category_id", ""))
            cat_name = cat.get("category_name", "Series")
            items = _get(src, action="get_series", category_id=cat_id) or []
            for s in items:
                sid = str(s.get("series_id", ""))
                stubs.append(Series(
                    id=make_id(src.id, "series", sid),
                    name=s.get("name", ""),
                    logo=s.get("cover", "") or s.get("stream_icon", ""),
                    group=cat_name,
                    section=SECTION_SERIES,
                    # get_series carries releaseDate ("2019-05-01") in the list
                    # payload — free year data, unlike the VOD list call.
                    year=str(s.get("releaseDate") or "")[:4],
                    extra={"series_id": sid, "category_id": cat_id},
                ))
            cat_counts.append((cat_name, len(items)))

        # Second pass: fetch episode lists concurrently — one API call per
        # series is an N+1 pattern that takes hours on large providers when
        # done sequentially.
        def _fill_episodes(series: Series) -> Series:
            try:
                info_data = _get(src, action="get_series_info", series_id=series.extra["series_id"]) or {}
                for season_num, eps in (info_data.get("episodes", {}) or {}).items():
                    for ep in eps:
                        eid = str(ep.get("id", ""))
                        ext = ep.get("container_extension", "mp4") or "mp4"
                        series.episodes.append(
                            Episode(
                                season=int(season_num) if str(season_num).isdigit() else 1,
                                episode=int(ep.get("episode_num", 0) or 0),
                                name=ep.get("title", "") or ep.get("name", ""),
                                url=f"{host}/series/{user}/{pwd}/{eid}.{ext}",
                                title=ep.get("title", "") or ep.get("name", ""),
                                extra={"episode_id": eid},
                            )
                        )
            except Exception as exc:
                logger.debug("Series info fetch failed for %s: %s", series.extra.get("series_id"), exc)
            return series

        from concurrent.futures import ThreadPoolExecutor
        done = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_fill_episodes, s) for s in stubs]
            for fut in futures:
                if is_cancelled and is_cancelled():
                    break
                playlist.series.append(fut.result())
                done += 1
                if on_progress and done % 20 == 0:
                    on_progress(done, len(stubs))
        for cat_name, count in cat_counts:
            playlist.categories.append(Category(name=cat_name, section=SECTION_SERIES, count=count))
        if on_progress:
            on_progress(len(playlist.series), len(stubs))
    except Exception as exc:
        logger.warning("Xtream series fetch failed: %s", exc)

    return playlist
