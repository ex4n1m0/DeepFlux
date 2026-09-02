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
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

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

# Never log credentials — mask them in every debug URL.
_TIMEOUT = 30
_DEFAULT_SERIES_CONCURRENCY = 2
_MAX_SERIES_CONCURRENCY = 6
_SERIES_INFO_ATTEMPTS = 3
_SERIES_INFO_CACHE_TTL = 30 * 60
_SERIES_INFO_CACHE_MAX = 2048
_SERIES_INFO_CACHE: "OrderedDict[Tuple[str, str, str], Tuple[float, Mapping[str, Any]]]" = OrderedDict()
_SERIES_INFO_CACHE_LOCK = threading.Lock()


class XtreamErrorKind(str, Enum):
    """Failure classes that callers can present differently to users."""

    AUTH_REJECTED = "auth_rejected"
    UNREACHABLE = "unreachable"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True)
class XtreamLoadError:
    """A credential-safe, user-facing Xtream playlist load failure."""

    kind: XtreamErrorKind
    message: str


@dataclass
class XtreamPlaylist(Playlist):
    """Playlist result carrying a typed load error when loading failed."""

    error: Optional[XtreamLoadError] = None


_AUTH_QUERY_KEYS = {"username", "password"}


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


def _safe_api_url(url: str) -> str:
    """Return an API URL whose credential query values are fully redacted."""
    parts = urlsplit(url)
    query = urlencode(
        [
            (key, "***" if key.lower() in _AUTH_QUERY_KEYS else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ],
        safe="*",
    )
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _request(src: PlaylistSource, url: str) -> Any:
    headers = {}
    if src.user_agent:
        headers["User-Agent"] = src.user_agent
    if src.referer:
        headers["Referer"] = src.referer
    logger.debug("Xtream request: %s", _safe_api_url(url))
    resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _get(src: PlaylistSource, action: str, **extra: str) -> Any:
    return _request(src, _api(src, action=action, **extra))


def _series_cache_key(src: PlaylistSource, series_id: str) -> Tuple[str, str, str]:
    # Password deliberately omitted: cached metadata is not credential data.
    return (_base_url(src), src.username, series_id)


def _series_cache_get(src: PlaylistSource, series_id: str) -> Optional[Mapping[str, Any]]:
    key = _series_cache_key(src, series_id)
    now = time.monotonic()
    with _SERIES_INFO_CACHE_LOCK:
        entry = _SERIES_INFO_CACHE.get(key)
        if entry is None:
            return None
        created, data = entry
        if now - created > _SERIES_INFO_CACHE_TTL:
            _SERIES_INFO_CACHE.pop(key, None)
            return None
        _SERIES_INFO_CACHE.move_to_end(key)
        return data


def _series_cache_put(src: PlaylistSource, series_id: str,
                      data: Mapping[str, Any]) -> None:
    key = _series_cache_key(src, series_id)
    with _SERIES_INFO_CACHE_LOCK:
        _SERIES_INFO_CACHE[key] = (time.monotonic(), dict(data))
        _SERIES_INFO_CACHE.move_to_end(key)
        while len(_SERIES_INFO_CACHE) > _SERIES_INFO_CACHE_MAX:
            _SERIES_INFO_CACHE.popitem(last=False)


def _clear_series_info_cache() -> None:
    """Test/cache-maintenance hook."""
    with _SERIES_INFO_CACHE_LOCK:
        _SERIES_INFO_CACHE.clear()


def _is_transient_series_error(exc: Exception) -> bool:
    if not isinstance(exc, requests.RequestException):
        return False
    response = getattr(exc, "response", None)
    if response is None:
        return True
    status = int(getattr(response, "status_code", 0) or 0)
    return status == 429 or status >= 500


def _series_retry_delay(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    if response is not None:
        raw = getattr(response, "headers", {}).get("Retry-After", "")
        try:
            return max(0.0, min(30.0, float(raw)))
        except (TypeError, ValueError):
            pass
    return (0.5, 1.5)[min(attempt, 1)]


def _sleep_unless_cancelled(delay: float,
                            is_cancelled: Optional[Callable[[], bool]]) -> bool:
    deadline = time.monotonic() + delay
    while True:
        if is_cancelled and is_cancelled():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.1, remaining))


def _get_series_info(src: PlaylistSource, series_id: str,
                     is_cancelled: Optional[Callable[[], bool]]) -> Mapping[str, Any]:
    cached = _series_cache_get(src, series_id)
    if cached is not None:
        return cached
    last_error: Optional[Exception] = None
    for attempt in range(_SERIES_INFO_ATTEMPTS):
        if is_cancelled and is_cancelled():
            return {}
        try:
            data = _get(src, action="get_series_info", series_id=series_id) or {}
            if not isinstance(data, Mapping):
                raise ValueError("Xtream series info was not an object")
            _series_cache_put(src, series_id, data)
            return data
        except Exception as exc:
            last_error = exc
            if (not _is_transient_series_error(exc)
                    or attempt + 1 >= _SERIES_INFO_ATTEMPTS):
                raise
            delay = _series_retry_delay(exc, attempt)
            logger.info("Xtream series detail throttled/transient; retrying in %.1fs", delay)
            if not _sleep_unless_cancelled(delay, is_cancelled):
                return {}
    if last_error is not None:
        raise last_error
    return {}


def _load_error(exc: Exception) -> XtreamLoadError:
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        if response is not None and response.status_code in (401, 403):
            return XtreamLoadError(
                XtreamErrorKind.AUTH_REJECTED,
                "Authentication was rejected by the Xtream provider.",
            )
    if isinstance(exc, requests.RequestException):
        return XtreamLoadError(
            XtreamErrorKind.UNREACHABLE,
            "The Xtream provider could not be reached.",
        )
    return XtreamLoadError(
        XtreamErrorKind.INVALID_RESPONSE,
        "The Xtream provider returned an invalid response.",
    )


def _authenticate(src: PlaylistSource) -> Tuple[Optional[Dict[str, Any]], Optional[XtreamLoadError]]:
    try:
        data = _get_raw(src)
    except Exception as exc:
        error = _load_error(exc)
        logger.warning("Xtream auth failed for %s (%s)", src.name, error.kind.value)
        return None, error
    if not isinstance(data, Mapping):
        error = XtreamLoadError(
            XtreamErrorKind.INVALID_RESPONSE,
            "The Xtream provider returned an invalid response.",
        )
        logger.warning("Xtream auth failed for %s (%s)", src.name, error.kind.value)
        return None, error
    user_info = data.get("user_info")
    if not isinstance(user_info, Mapping) or "auth" not in user_info:
        error = XtreamLoadError(
            XtreamErrorKind.INVALID_RESPONSE,
            "The Xtream provider returned an invalid response.",
        )
        logger.warning("Xtream auth failed for %s (%s)", src.name, error.kind.value)
        return None, error
    auth = user_info.get("auth")
    if auth != 1 and auth != "1":
        error = XtreamLoadError(
            XtreamErrorKind.AUTH_REJECTED,
            "Authentication was rejected by the Xtream provider.",
        )
        logger.warning("Xtream auth rejected for %s", src.name)
        return None, error
    return dict(data), None


def authenticate(src: PlaylistSource) -> Optional[Dict[str, Any]]:
    """Validate credentials; returns user_info/server_info or None on failure."""
    data, _error = _authenticate(src)
    return data


def _get_raw(src: PlaylistSource) -> Any:
    """The bare auth call (no action) returns user/server info + categories."""
    return _request(src, _api(src))


def load_playlist(
    src: PlaylistSource,
    on_progress=None,
    is_cancelled=None,
    series_info_concurrency: int = _DEFAULT_SERIES_CONCURRENCY,
) -> Playlist:
    """Fetch a playlist, with typed ``error`` details on failed loads."""
    playlist = XtreamPlaylist(source_id=src.id)
    info, playlist.error = _authenticate(src)
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
    user = quote(src.username, safe="")
    pwd = quote(src.password, safe="")

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
        playlist.error = playlist.error or _load_error(exc)
        logger.warning("Xtream live fetch failed (%s)", type(exc).__name__)

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
        playlist.error = playlist.error or _load_error(exc)
        logger.warning("Xtream VOD fetch failed (%s)", type(exc).__name__)

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

        # Second pass: get_series_info is an N+1 API.  Keep only a small
        # bounded window scheduled, cache repeated details, and stop feeding
        # work immediately when the source load is cancelled.
        def _fill_episodes(series: Series) -> Tuple[Series, Optional[Exception]]:
            try:
                info_data = _get_series_info(
                    src, str(series.extra["series_id"]), is_cancelled)
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
                return series, None
            except Exception as exc:
                logger.debug(
                    "Series info fetch failed for %s (%s)",
                    series.extra.get("series_id"), type(exc).__name__,
                )
                return series, exc

        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
        try:
            workers = int(series_info_concurrency)
        except (TypeError, ValueError):
            workers = _DEFAULT_SERIES_CONCURRENCY
        workers = max(1, min(_MAX_SERIES_CONCURRENCY, workers))
        done_count = 0
        next_index = 0
        pending: Dict[Any, int] = {}
        completed: Dict[int, Series] = {}
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            while next_index < len(stubs) and len(pending) < workers:
                future = pool.submit(_fill_episodes, stubs[next_index])
                pending[future] = next_index
                next_index += 1
            while pending:
                ready, _not_done = wait(pending, return_when=FIRST_COMPLETED)
                for future in ready:
                    index = pending.pop(future)
                    series, detail_error = future.result()
                    completed[index] = series
                    if detail_error is not None and playlist.error is None:
                        playlist.error = _load_error(detail_error)
                    done_count += 1
                    if on_progress and done_count % 20 == 0:
                        on_progress(done_count, len(stubs))
                if is_cancelled and is_cancelled():
                    for future in pending:
                        future.cancel()
                    break
                while next_index < len(stubs) and len(pending) < workers:
                    future = pool.submit(_fill_episodes, stubs[next_index])
                    pending[future] = next_index
                    next_index += 1
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        playlist.series.extend(completed[i] for i in sorted(completed))
        for cat_name, count in cat_counts:
            playlist.categories.append(Category(name=cat_name, section=SECTION_SERIES, count=count))
        if on_progress:
            on_progress(len(playlist.series), len(stubs))
    except Exception as exc:
        playlist.error = playlist.error or _load_error(exc)
        logger.warning("Xtream series fetch failed (%s)", type(exc).__name__)

    return playlist
