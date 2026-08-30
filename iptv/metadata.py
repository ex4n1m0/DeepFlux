"""Metadata + artwork enrichment pipeline.

Providers:
- **TMDb** (movies & series) — requires a user-supplied API key.
- **TVmaze** (series) — keyless automatic fallback when TMDb is unconfigured
  or returns no match.
- **iptv-org logos** (live channels) — keyless fallback for channel logos.

All providers implement :class:`MetadataProvider`. To add a new provider,
subclass it and register an instance in :class:`MetadataPipeline`.

Lookups are rate-limited and queued via :class:`_RateLimiter` so a 50k-entry
playlist never fires thousands of simultaneous API calls. Metadata is resolved
on demand (visible items first), not for the whole playlist upfront, and
cached in SQLite via :class:`iptv.cache.IPTVCache`.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from .cache import IPTVCache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Title cleaning
# ---------------------------------------------------------------------------

# Quality / codec / source tags to strip before querying metadata APIs.
_QUALITY_TAGS = [
    r"\b(480|720|1080|2160)[pi]?\b",
    r"\b(x264|x265|h264|h265|hevc|av1|vp9|divx|xvid)\b",
    r"\b(hdtv|web-dl|webrip|bluray|blu-ray|bdrip|brrip|dvdrip|cam|ts|tc)\b",
    r"\b(remux|atmos|truehd|dts|ddp|dd|aac\d?|mp3|flac)\b",
    r"\b(multi|dual|subbed|subs|internal|proper|repack)\b",
    r"\b(10bit|8bit|sdr|hdr|hdr10|dolby)\b",
    r"\b(nordic|vostfr|dubbed|amzn|dsnp|hmax|atvp)\b",
]
# Audio channel markers ("5.1", "DDP5.1", "2 0"). Stripped before dots become
# spaces, otherwise a bare "5 1" survives into the API query.
_AUDIO_CH_RE = re.compile(r"\b(?:ddp?|dts|aac|eac3|ac3)?\s?[2578][.\s][01]\b", re.IGNORECASE)
# Scene release-group suffix: " -LAMA", " -WORLD", " -MeGusta". Requires the
# space-then-hyphen-then-word shape so "Mission Impossible - Fallout" and
# "Spider-Man" are left alone.
_GROUP_SUFFIX_RE = re.compile(r"\s[-–][A-Za-z0-9]{2,15}\s*$")
# Country / region prefixes like "US:", "|US|", "[US]".
_COUNTRY_PREFIX = re.compile(r"^(\[|\(|\|)?[A-Z]{2}(\]|\)|\|)?[:\s\-]+")
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_SE_EP_RE = re.compile(r"[Ss]\d{1,2}\s?[Ee]\d{1,3}.*$")
_SEASON_RE = re.compile(r"[Ss]\d{1,2}\b.*$")

_QUALITY_RE = re.compile("|".join(_QUALITY_TAGS), re.IGNORECASE)

# Bracketed release tags like "[1080p] [WEBRip] [WEB]" — inside brackets we
# can afford a broader tag set (bare "web"/"dvd"/"hd" would be too aggressive
# on free text, but are safe inside release-tag brackets).
_BRACKET_TAGS = _QUALITY_TAGS + [
    r"\b(web|dvd|bd|uhd|4k|fhd|hd|sd|hdrip|hdts|x265|10-bit|5\.1)\b",
]
_BRACKET_TAG_RE = re.compile("|".join(_BRACKET_TAGS), re.IGNORECASE)
_BRACKET_GROUP_RE = re.compile(r"\[([^\]]*)\]")


def _strip_tag_brackets(t: str) -> str:
    """Drop […] groups that contain only release tags; strip stray brackets."""
    def _drop(m: "re.Match") -> str:
        inner = _BRACKET_TAG_RE.sub("", m.group(1)).strip(" -_.,")
        return "" if not inner else m.group(0)

    prev = None
    while prev != t:
        prev = t
        t = _BRACKET_GROUP_RE.sub(_drop, t)
    # Remove leftover unmatched bracket characters (e.g. a trailing "[").
    return re.sub(r"[\[\]]", " ", t)


def clean_title(name: str) -> str:
    """Strip quality/codec/country tags and season/episode markers."""
    if not name:
        return ""
    t = name
    # Drop file extension.
    if "." in t and t.rsplit(".", 1)[-1].lower() in {
        "mkv", "mp4", "avi", "mov", "webm", "m4v", "ts", "m3u8", "mpg", "mpeg", "flv",
    }:
        t = t.rsplit(".", 1)[0]
    # Audio layouts first — "5.1" must go before dots turn into spaces.
    t = _AUDIO_CH_RE.sub(" ", t)
    # Replace separators with spaces.
    t = t.replace(".", " ").replace("_", " ").replace("  ", " ")
    # Strip country prefix.
    t = _COUNTRY_PREFIX.sub("", t)
    # Strip SxxExx / Sxx and everything after.
    t = _SE_EP_RE.sub("", t)
    t = _SEASON_RE.sub("", t)
    # Strip quality/codec tags.
    t = _QUALITY_RE.sub("", t)
    # Strip bracketed release-tag groups ("[1080p] [WEBRip] [WEB]" -> gone).
    t = _strip_tag_brackets(t)
    # Strip a parenthesized year — the year is passed to the API separately,
    # and "(2006)" in the query string makes TMDb return no results.
    t = re.sub(r"\((?:19|20)\d{2}\)", " ", t)
    # Collapse whitespace.
    t = re.sub(r"\s+", " ", t).strip()
    # Trailing release-group tag(s), now that the tags around them are gone.
    prev = None
    while prev != t:
        prev = t
        t = _GROUP_SUFFIX_RE.sub("", t).strip()
    return t


def extract_year(name: str) -> str:
    m = _YEAR_RE.search(name or "")
    return m.group(1) if m else ""


def metadata_key(section: str, name: str, year: str = "") -> str:
    return f"{section}:{clean_title(name).lower()}:{year or ''}"


# ---------------------------------------------------------------------------
# Shared retry layer
# ---------------------------------------------------------------------------
#
# Pulling artwork/metadata from the internet very often needs more than one
# try: CDNs reset connections under bursts, providers rate-limit (429), and
# transient 5xx blips are common. Every provider therefore goes through this
# single helper, which mirrors the retry style proven in ``iptv.artwork`` and
# ``iptv.framegrab``:
#
#   * 3-4 attempts with jittered exponential backoff (a CDN that reset a burst
#     would just get the same burst back in lockstep otherwise).
#   * Transient vs permanent classification — retry on 408/425/429/5xx and
#     connection resets/timeouts; bail immediately on 404/401/403.
#   * Never negative-cache a transient failure (a blip must not blank a tile
#     forever — the framegrab principle).
#   * Per-thread keep-alive ``requests.Session`` (fresh TLS handshakes per
#     request lose ~45% of calls to logo CDNs).

# HTTP status codes worth retrying (transient). 4xx other than these are
# permanent client errors (404 not found, 401 bad key, 403 forbidden).
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Error-string markers classifying a failed request as transient (worth
# retrying) vs permanent. Reuses the framegrab set — stream CDNs and metadata
# APIs share the same reset/timeout failure modes.
_TRANSIENT_MARKERS = (
    "handshake", "10054", "connection reset", "timed out", "timeout",
    "end of file", "i/o error", "connection refused", "temporarily",
    "connection aborted", "read timed out", "chunked encoding",
)

# Default retry policy. Providers can override per-call when an API documents
# a tighter limit (e.g. OMDb's free tier is 1000 req/day — no point hammering).
_DEFAULT_MAX_ATTEMPTS = 4
_DEFAULT_BACKOFF_BASE = 0.3  # seconds; ~0.3, 0.6, 1.2, 2.4 with jitter


def _is_transient_exc(exc: Exception) -> bool:
    """True when a requests exception looks like a transient network blip."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        msg = str(exc).lower()
        # A 401/403/404 raised via raise_for_status lands as HTTPError, not
        # here — ConnectionError/Timeout are always network-level and thus
        # transient by definition.
        return True
    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code in _RETRY_STATUS:
            return True
        return False
    # Unknown exception — err on the side of retrying once.
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _retry_request(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    backoff_base: float = _DEFAULT_BACKOFF_BASE,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """GET ``url`` with retries, returning ``(json_or_None, status_class)``.

    ``status_class`` is one of:
      * ``"ok"``        — 2xx, JSON parsed (may still be an empty result set).
      * ``"not_found"`` — 404, a permanent "no match" (not an error).
      * ``"auth"``      — 401/403, bad key/forbidden (permanent, caller logs).
      * ``"transient"`` — exhausted retries on a retryable status/network error.
      * ``"permanent"`` — other non-retryable failure (4xx, bad payload).

    The caller decides whether a ``"transient"`` exhaustion is worth a
    negative-cache entry — by the framegrab principle it usually is NOT.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            r = session.get(url, params=params, headers=headers, timeout=timeout)
            status = r.status_code
            if status == 200:
                try:
                    return r.json(), "ok"
                except ValueError:
                    r.close()
                    logger.debug("retry_request: bad JSON from %s", url)
                    return None, "permanent"
            r.close()
            if status == 404:
                return None, "not_found"
            if status in (401, 403):
                return None, "auth"
            if status in _RETRY_STATUS:
                last_exc = requests.HTTPError(response=r)
                if attempt < max_attempts - 1:
                    wait = backoff_base * (2 ** attempt) * (0.5 + random.random())
                    time.sleep(wait)
                    continue
                return None, "transient"
            logger.debug("retry_request: HTTP %d for %s", status, url)
            return None, "permanent"
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                wait = backoff_base * (2 ** attempt) * (0.5 + random.random())
                logger.debug("retry_request: transient %s (attempt %d/%d) — retrying in %.1fs",
                             type(exc).__name__, attempt + 1, max_attempts, wait)
                time.sleep(wait)
                continue
            logger.debug("retry_request: exhausted retries for %s: %s", url, exc)
            return None, "transient"
        except Exception as exc:
            if _is_transient_exc(exc) and attempt < max_attempts - 1:
                wait = backoff_base * (2 ** attempt) * (0.5 + random.random())
                time.sleep(wait)
                continue
            logger.debug("retry_request: permanent failure for %s: %s", url, exc)
            return None, "permanent"
    return None, "transient"


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class MetadataProvider:
    """Base class. Subclasses implement :meth:`fetch`."""

    name = "base"

    def fetch(self, title: str, year: str, section: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


class TMDBProvider(MetadataProvider):
    name = "tmdb"
    BASE = "https://api.themoviedb.org/3"
    IMG = "https://image.tmdb.org/t/p"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        # Keep-alive session: TMDb's CDN resets connections under bursts just
        # like the logo CDNs — a fresh TLS handshake per request is wasteful
        # and loses calls. Reused across all fetch() calls on this provider.
        self._session = requests.Session()

    def fetch(self, title: str, year: str, section: str) -> Optional[Dict[str, Any]]:
        if not self.api_key or not title:
            return None
        kind = "tv" if section == "series" else "movie"
        try:
            params = {"api_key": self.api_key, "query": title, "language": "en-US"}
            if year:
                params["year" if kind == "movie" else "first_air_date_year"] = year
            data, status = _retry_request(
                self._session, f"{self.BASE}/search/{kind}",
                params=params, timeout=15,
            )
            if status != "ok" or not data:
                if status == "auth":
                    logger.warning("TMDb rejected the API key (401)")
                return None
            results = data.get("results", [])
            if not results:
                return None
            # Prefer a result whose year matches the one we asked for: TMDb
            # ranks by popularity, so a remake or same-name title can sit
            # above the right one even with year= in the query.
            if year:
                matching = [r for r in results
                            if (r.get("release_date") or r.get("first_air_date") or "")[:4] == year]
                if matching:
                    results = matching
            res = results[0]
            meta = {
                "title": res.get("title") or res.get("name", title),
                "year": (res.get("release_date") or res.get("first_air_date") or "")[:4],
                "rating": float(res.get("vote_average", 0) or 0),
                "synopsis": res.get("overview", ""),
                "genres": [],
                "poster": self._img(res.get("poster_path"), "w780"),
                "backdrop": self._img(res.get("backdrop_path"), "w1280"),
                "provider": self.name,
                "_tmdb_id": str(res.get("id", "")),  # for Fanart.tv enrichment
                "_is_series": kind == "tv",
            }
            # Fetch genres via detail endpoint (also retried — a 5xx blip on
            # the detail call must not discard an otherwise-good search hit).
            detail_id = res.get("id")
            if detail_id:
                dj, dstatus = _retry_request(
                    self._session, f"{self.BASE}/{kind}/{detail_id}",
                    params={"api_key": self.api_key, "language": "en-US"},
                    timeout=15,
                )
                if dstatus == "ok" and dj:
                    meta["genres"] = [g["name"] for g in dj.get("genres", [])]
                    if not meta["year"]:
                        meta["year"] = (dj.get("release_date") or dj.get("first_air_date") or "")[:4]
            return meta
        except Exception as exc:
            logger.debug("TMDb fetch failed for %r: %s", title, exc)
            return None

    def _img(self, path: Optional[str], size: str = "w500") -> str:
        return f"{self.IMG}/{size}{path}" if path else ""


class TVMazeProvider(MetadataProvider):
    name = "tvmaze"
    BASE = "https://api.tvmaze.com"

    def __init__(self) -> None:
        self._session = requests.Session()

    def fetch(self, title: str, year: str, section: str) -> Optional[Dict[str, Any]]:
        if not title or section != "series":
            return None
        try:
            # TVMaze returns 404 for no-match — that's a clean "not found",
            # not an error, and _retry_request classifies it as such (no
            # wasted retries on a permanent miss).
            data, status = _retry_request(
                self._session, f"{self.BASE}/singlesearch/shows",
                params={"q": title}, timeout=15,
            )
            if status != "ok" or not data:
                return None
            show = data
            return {
                "title": show.get("name", title),
                "year": (show.get("premiered") or "")[:4],
                "rating": float((show.get("rating") or {}).get("average") or 0),
                "synopsis": re.sub(r"<[^>]+>", "", show.get("summary") or ""),
                "genres": show.get("genres", []),
                "poster": (show.get("image") or {}).get("original", "") or (show.get("image") or {}).get("medium", ""),
                "backdrop": "",
                "provider": self.name,
            }
        except Exception as exc:
            logger.debug("TVmaze fetch failed for %r: %s", title, exc)
            return None


class OMDbProvider(MetadataProvider):
    """OMDb API — keyless-ish movie/series fallback (free key from omdbapi.com).

    OMDb is a community-driven movie database with a simple JSON API. It
    requires a free API key (user registers at omdbapi.com/apikey.aspx). It's
    the fallback when TMDb has no key or misses — particularly useful for
    older/obscure titles and non-English cinema that TMDb's search ranks poorly.

    The API is simple: ``?t=<title>&y=<year>&type=movie|series`` returns a
    single best-match record (no list). Response includes poster URL, year,
    genre, rating, and plot. The ``Response: "False"`` field signals a miss
    (not an HTTP error) — classified as a permanent miss (no retry).
    """

    name = "omdb"
    BASE = "https://www.omdbapi.com"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._session = requests.Session()

    def fetch(self, title: str, year: str, section: str,
              raw_name: str = "") -> Optional[Dict[str, Any]]:
        if not self.api_key or not title:
            return None
        kind = "series" if section == "series" else "movie"
        params = {"apikey": self.api_key, "t": title, "type": kind}
        if year:
            params["y"] = year
        data, status = _retry_request(
            self._session, self.BASE, params=params, timeout=15,
        )
        if status != "ok" or not data:
            if status == "auth":
                logger.warning("OMDb rejected the API key (401)")
            return None
        # OMDb signals a miss with Response: "False", not an HTTP error.
        if str(data.get("Response", "True")).lower() == "false":
            return None
        poster = data.get("Poster", "")
        if poster and poster.upper() == "N/A":
            poster = ""
        return {
            "title": data.get("Title", title),
            "year": (data.get("Year") or "")[:4],
            "rating": float(data.get("imdbRating", 0) or 0),
            "synopsis": data.get("Plot", ""),
            "genres": [g.strip() for g in (data.get("Genre") or "").split(",") if g.strip()],
            "poster": poster,
            "backdrop": "",
            "provider": self.name,
        }


class WikipediaProvider(MetadataProvider):
    """Wikipedia — keyless last-resort fallback for movie/series art.

    When all keyed providers miss (or no keys are configured), Wikipedia's
    REST API can still provide a poster image for well-known titles. The
    flow is:

    1. Search Wikipedia's REST API for the title (``/w/rest.php/v1/search/page``).
    2. Fetch the page summary (``/api/rest_v1/page/summary/{key}``) — a
       structured JSON payload whose ``originalimage``/``thumbnail`` IS the
       infobox poster, chosen by Wikipedia itself. (The old flow regexed the
       first ``<img>`` out of the raw page HTML, which could land on navbox
       icons; the summary endpoint can't.)

    The summary's ``extract`` doubles as a synopsis. Disambiguation pages are
    rejected — a wrong poster is worse than no poster.
    """

    name = "wikipedia"
    REST = "https://en.wikipedia.org/w/rest.php/v1"
    SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary"
    # Wikipedia rate limit is generous (10 req/s for anonymous), but we
    # still use the shared retry layer for transient failures.

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _JAV_BROWSER_UA,  # reuse the browser UA
            "Accept": "application/json",
        })

    # Wikipedia titles media pages as "<Title> (<year> film)" / "(TV series)".
    # Ranking those first is what stops "Dune" resolving to the landform.
    _MEDIA_TITLE_RE = re.compile(
        r"\((?:\d{4}\s+)?(?:film|(?:tv|television)\s+(?:series|programme|show)|miniseries)\)",
        re.IGNORECASE,
    )

    def fetch(self, title: str, year: str, section: str,
              raw_name: str = "") -> Optional[Dict[str, Any]]:
        if not title:
            return None
        # Step 1: search for the page. The known year goes IN the query here
        # (unlike TMDb, where it must be a parameter): "Dune 1984" surfaces
        # "Dune (1984 film)" first, while a bare "Dune" tops out at the
        # landform and the 2021 remake — the 1984 page isn't even in the top 5.
        search_data, sstatus = _retry_request(
            self._session, f"{self.REST}/search/page",
            params={"q": f"{title} {year}".strip(), "limit": 5}, timeout=15,
        )
        if sstatus != "ok" or not search_data:
            return None
        pages = search_data.get("pages") or []
        if not pages:
            return None
        # Rank candidates: media-suffixed titles first (year match inside the
        # suffix beats without), then title-contains-query, then relevance
        # order. Wikipedia's primary topic for a title is often NOT the film
        # ("Dune" -> the landform), so plain first-hit is a wrong poster.
        title_lower = title.lower()

        def _rank(p: Dict[str, Any]) -> Tuple[bool, bool, bool]:
            t = (p.get("title") or "").lower()
            media = bool(self._MEDIA_TITLE_RE.search(t))
            year_in = bool(year and year in t)
            contains = title_lower in t
            # (media, year, contains) — True sorts after False, so negate.
            return (not media, not year_in, not contains)

        pages = sorted(pages, key=_rank)
        # Step 2: the page summary carries the infobox image as structured
        # data — no HTML scraping needed. Try candidates in rank order,
        # skipping disambiguation pages (a wrong poster is worse than none).
        from urllib.parse import quote
        for page in pages[:3]:
            page_key = page.get("key") or page.get("title")
            if not page_key:
                continue
            summary, st = _retry_request(
                self._session, f"{self.SUMMARY}/{quote(str(page_key), safe='')}",
                timeout=15,
            )
            if st != "ok" or not summary:
                continue
            if summary.get("type") == "disambiguation":
                continue
            poster = ((summary.get("originalimage") or {}).get("source")
                      or (summary.get("thumbnail") or {}).get("source") or "")
            if not poster:
                continue
            return {
                "title": summary.get("title") or page.get("title", title),
                "year": year,
                "rating": 0,
                "synopsis": summary.get("extract") or "",
                "genres": [],
                "poster": poster,
                "backdrop": "",
                "provider": self.name,
            }
        return None


class FanartTvProvider(MetadataProvider):
    """Fanart.tv — backdrop/fanart supplement for movies and series.

    Fanart.tv hosts high-quality fan-made backdrops, logos, and art for movies
    and TV series. It complements TMDb's backdrops with community art that's
    often higher quality or more stylistically varied. Needs a free personal
    API key from fanart.tv/get-an-api-key.

    This provider is a **backdrop supplement**: it's tried after TMDb/TVMaze
    have already resolved a poster, to enrich the backdrop. If the primary
    provider already returned a backdrop, this provider is skipped. The
    provider returns only a backdrop (poster is empty) — the pipeline merges
    it with the existing poster.
    """

    name = "fanarttv"
    BASE = "https://webservice.fanart.tv/v3"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._session = requests.Session()

    def fetch(self, title: str, year: str, section: str,
              raw_name: str = "") -> Optional[Dict[str, Any]]:
        # Fanart.tv needs a TMDb or TVDb ID — we look it up via TMDb search
        # first (if a TMDb key is available). Without an ID, we can't query.
        # This provider is designed to be called by the pipeline with the
        # TMDb ID already resolved, via the backdrop_enrich path.
        # As a standalone fetch, it returns None (no ID to query with).
        return None

    def fetch_backdrop(self, tmdb_id: str, is_series: bool = False) -> str:
        """Fetch the best backdrop URL for a TMDb ID.

        Returns an empty string if no backdrop is available. Called by the
        pipeline after a primary provider hit to enrich the backdrop.
        """
        if not self.api_key or not tmdb_id:
            return ""
        kind = "tv" if is_series else "movies"
        data, status = _retry_request(
            self._session, f"{self.BASE}/{kind}/{tmdb_id}",
            params={"api_key": self.api_key}, timeout=15,
        )
        if status != "ok" or not data:
            return ""
        # Fanart.tv returns moviebackground/tvbackground arrays for backdrops.
        backdrops = data.get("moviebackground") or data.get("showbackground") or []
        if not backdrops:
            return ""
        # Pick the highest-rated backdrop (likes field).
        best = max(backdrops, key=lambda b: int(b.get("likes", 0) or 0))
        url = best.get("url", "")
        # Fanart.tv URLs are sometimes // (protocol-relative) — normalize.
        if url.startswith("//"):
            url = "https:" + url
        return url


class TPDBProvider(MetadataProvider):
    """ThePornDB — metadata + posters for adult VOD.

    Needs a user-supplied bearer token (theporndb.net); every endpoint 401s
    without one. ``/movies``, ``/scenes`` and ``/jav`` all return the same
    shape — ``{"data": [row, ...]}`` — where a row carries ``title``,
    ``date`` (YYYY-MM-DD), ``description``, ``tags[].name`` and the
    ``posters`` / ``background`` size dicts.

    The API resets connections on rapid sequential requests, so calls go
    through a single keep-alive :class:`requests.Session` and the shared
    :func:`_retry_request` layer (4 attempts, jittered backoff) — the same
    lesson the channel-logo fetcher learned in :mod:`iptv.artwork`, now
    applied uniformly across every metadata provider.
    """

    name = "tpdb"
    BASE = "https://api.theporndb.net"
    # Below these scores a hit is treated as noise. A wrong poster is worse
    # than no poster, and TPDB's search returns loose keyword candidates.
    MIN_SIMILARITY = 0.5
    MIN_CONTAINMENT = 0.7

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })

    def fetch(self, title: str, year: str, section: str,
              raw_name: str = "") -> Optional[Dict[str, Any]]:
        if not self.api_key or not title:
            return None
        # A JAV code is an exact catalogue key — trusted without a title check.
        code = jav_code(raw_name or "")
        if code:
            row = self._search("/jav", code, year)
            if row:
                return self._to_meta(row)

        # Adult playlist names are studio-led ("<Studio> <scene title> …"),
        # which poisons a plain title query — measured 4% on a real playlist.
        # TPDB's parse mode is built for exactly this shape (9x better).
        row = self._parse_lookup(raw_name or title, year)
        if row:
            return self._to_meta(row)

        # Fall back to plain keyword search for non-studio-led names.
        for path in ("/movies", "/scenes"):
            row = self._search(path, title, year, expect=title)
            if row:
                return self._to_meta(row)
        return None

    def _parse_lookup(self, name: str, year: str = "") -> Optional[Dict[str, Any]]:
        """TPDB filename-parsing mode, with the match verified locally.

        Parse returns loose candidates (rows came back for 80% of entries but
        only ~30% were real matches), so every row is checked before use.
        """
        if not name:
            return None
        rows = [row for row in self._request("/scenes", {"parse": name})
                if self._verify(name, row)]
        return self._best(rows, year)

    def _verify(self, name: str, row: Dict[str, Any]) -> bool:
        """Accept a row only if its title really belongs to this entry.

        Two independent checks, either of which is sufficient; both measured
        0% false positives against a shuffled control.
        """
        return (title_containment(name, row.get("title", "")) >= self.MIN_CONTAINMENT
                or site_stripped_similarity(name, row) >= self.MIN_SIMILARITY)

    def _request(self, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        data, status = _retry_request(
            self._session, f"{self.BASE}{path}",
            params=params, timeout=20,
        )
        if status == "auth":
            logger.warning("TPDB rejected the API token (401)")
        if status != "ok" or not data:
            return []
        return data.get("data") or []

    def _search(self, path: str, query: str, year: str,
                expect: str = "") -> Optional[Dict[str, Any]]:
        rows = self._request(path, {"q": query})
        if expect:
            rows = [row for row in rows
                    if title_similarity(expect, row.get("title", "")) >= self.MIN_SIMILARITY]
        return self._best(rows, year)

    @staticmethod
    def _best(rows: List[Dict[str, Any]], year: str) -> Optional[Dict[str, Any]]:
        """Prefer an exact year match, then any row that actually has art."""
        if not rows:
            return None
        if year:
            for row in rows:
                if (row.get("date") or "")[:4] == year:
                    return row
        for row in rows:
            if row.get("posters") or row.get("poster"):
                return row
        return rows[0]

    @staticmethod
    def _pick(images: Any, *sizes: str) -> str:
        """First non-null URL from a TPDB size dict (values may be null)."""
        if not isinstance(images, dict):
            return ""
        for size in sizes:
            url = images.get(size)
            if url:
                return url
        return ""

    def _to_meta(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "title": row.get("title") or "",
            "year": (row.get("date") or "")[:4],
            "rating": float(row.get("rating") or 0),
            "synopsis": row.get("description") or "",
            "genres": [t.get("name", "") for t in (row.get("tags") or [])
                       if t.get("name")][:8],
            "poster": (self._pick(row.get("posters"), "large", "full", "medium", "small")
                       or row.get("poster") or ""),
            "backdrop": self._pick(row.get("background"), "full", "large", "medium"),
            "provider": self.name,
        }


class StashDBProvider(MetadataProvider):
    """StashDB — community-driven adult metadata database (GraphQL API).

    Open-source, community-maintained, with a GraphQL API at
    ``https://stashdb.org/graphql``. Needs a free API key (user registers at
    stashdb.org, enters it in settings). Strong on western web scenes —
    complements TPDB (which has thinner scene coverage) and is the second
    source in the adult chain.

    The ``searchScenes`` query takes a free-text ``term`` and returns scenes
    with ``images`` (cover + backdrops), ``studio``, ``performers``,
    ``release_date``, ``title``, and ``code``. Results are verified locally
    via the combined scoring function — StashDB's search is keyword-based and
    returns loose candidates, same as TPDB.
    """

    name = "stashdb"
    ENDPOINT = "https://stashdb.org/graphql"
    MIN_SCORE = 0.5  # combined score floor (see _combined_score)

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._session = requests.Session()
        self._session.headers.update({
            "ApiKey": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def fetch(self, title: str, year: str, section: str,
              raw_name: str = "") -> Optional[Dict[str, Any]]:
        if not self.api_key or not title:
            return None
        query = title
        # Use the raw name for scoring — it carries the studio prefix that
        # helps disambiguate adult scenes.
        raw = raw_name or title
        rows = self._search_scenes(query)
        if not rows:
            return None
        # Rank by combined score; reject everything below the floor.
        scored = [(self._combined_score(raw, year, row), row) for row in rows]
        scored = [(s, r) for s, r in scored if s >= self.MIN_SCORE]
        if not scored:
            return None
        scored.sort(key=lambda sr: sr[0], reverse=True)
        return self._to_meta(scored[0][1])

    def _search_scenes(self, term: str) -> List[Dict[str, Any]]:
        """Query the StashDB searchScenes GraphQL endpoint via POST."""
        gql = {
            "operationName": "SearchScenes",
            "variables": {"term": term, "page": 1, "per_page": 10},
            "query": (
                "query SearchScenes($term: String!, $page: Int, $per_page: Int) {"
                "  searchScenes(term: $term, page: $page, per_page: $per_page) {"
                "    count"
                "    scenes { id title code release_date duration"
                "      images { id url width height }"
                "      studio { id name }"
                "      performers { as performer { id name } }"
                "    }"
                "  }"
                "}"
            ),
        }
        return self._post_gql(gql)

    def _post_gql(self, gql: Dict[str, Any]) -> List[Dict[str, Any]]:
        """POST a GraphQL query with the shared retry pattern."""
        for attempt in range(_DEFAULT_MAX_ATTEMPTS):
            try:
                r = self._session.post(
                    self.ENDPOINT, json=gql, timeout=20,
                    headers={"ApiKey": self.api_key, "Content-Type": "application/json"},
                )
                if r.status_code == 200:
                    body = r.json()
                    if body.get("errors"):
                        logger.debug("StashDB GraphQL errors: %s", body["errors"][:200])
                        return []
                    search = (body.get("data") or {}).get("searchScenes") or {}
                    return search.get("scenes") or []
                if r.status_code in (401, 403):
                    logger.warning("StashDB rejected the API key (%d)", r.status_code)
                    return []
                if r.status_code == 404:
                    return []
                r.close()
                if r.status_code not in _RETRY_STATUS:
                    return []
            except (requests.ConnectionError, requests.Timeout):
                pass
            if attempt < _DEFAULT_MAX_ATTEMPTS - 1:
                time.sleep(_DEFAULT_BACKOFF_BASE * (2 ** attempt) * (0.5 + random.random()))
        return []

    @staticmethod
    def _combined_score(name: str, year: str, row: Dict[str, Any]) -> float:
        """Weighted 0..1 score: 0.5*title + 0.3*year + 0.2*studio.

        Replaces the independent MIN_SIMILARITY/MIN_CONTAINMENT gates with a
        single ranked score. A strong title match with a year mismatch still
        scores high; a weak title with a perfect year + studio match can
        clear the floor. This catches matches the old gates rejected.
        """
        title = row.get("title") or ""
        t_score = title_similarity(name, title)
        # Containment is the better test for studio-led adult names (the
        # returned title is shorter than the playlist name).
        c_score = title_containment(name, title)
        title_score = max(t_score, c_score)
        year_score = 1.0 if year and (row.get("release_date") or "")[:4] == year else 0.0
        studio = ((row.get("studio") or {}) or {}).get("name") or ""
        studio_score = site_stripped_similarity(name, {"site": {"name": studio}, "title": title})
        return 0.5 * title_score + 0.3 * year_score + 0.2 * studio_score

    def _to_meta(self, row: Dict[str, Any]) -> Dict[str, Any]:
        # StashDB images: pick the first (usually the cover). Landscape
        # images are backdrops; portrait are posters.
        poster = ""
        backdrop = ""
        for img in (row.get("images") or []):
            url = img.get("url") or ""
            w = img.get("width") or 0
            h = img.get("height") or 0
            if not url:
                continue
            if w and h and w > h:
                if not backdrop:
                    backdrop = url
            else:
                if not poster:
                    poster = url
        if not poster:
            poster = backdrop  # any image is better than none
        performers = [p.get("performer", {}).get("name", "")
                      for p in (row.get("performers") or [])
                      if p.get("performer", {}).get("name")]
        return {
            "title": row.get("title") or "",
            "year": (row.get("release_date") or "")[:4],
            "rating": 0,
            "synopsis": "",
            "genres": [p for p in performers if p][:5],
            "poster": poster,
            "backdrop": backdrop,
            "provider": self.name,
        }


# ---------------------------------------------------------------------------
# JAV providers
# ---------------------------------------------------------------------------
#
# JAV (Japanese Adult Video) has the richest cover-art ecosystem of any adult
# category: front + back covers, actress galleries, studio branding. But it's
# also the most fragmented — no single source covers everything, and the
# sources are split by censored (mosaic) vs uncensored.
#
# The chain (built in MetadataPipeline._build_chain) tries providers in order:
#   1. JavBus  — keyless HTML scrape, handles both censored + uncensored,
#                has mirror domains for region-block resilience.
#   2. JavLibrary — keyless HTML scrape, highest coverage, but region-locked
#                   (uses /en/ then /cn/ mirrors).
#   3. TPDB /jav — API-based, lowest JAV coverage but already wired.
#
# All scrapers share the retry layer (_retry_request) and a per-domain rate
# limiter (1 req / 2s) — the "standard" politeness policy. Each uses a
# keep-alive requests.Session with a browser UA, the same pattern the artwork
# fetcher and TPDB client use.

# Browser UA — JAV sites serve different content (or block) to non-browser UAs.
_JAV_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class _DomainRateLimiter:
    """Per-domain rate limiter: at most 1 request per ``min_interval`` seconds.

    JAV sites (JavBus, JavLibrary) are aggressive about IP bans on burst
    scraping — a single keep-alive session with 1 req / 2s spacing is the
    politeness policy the user chose ("standard", matching the artwork fetcher).
    """

    def __init__(self, min_interval: float = 2.0) -> None:
        self._min_interval = min_interval
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(self, domain: str) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                last = self._last.get(domain, 0.0)
                wait = self._min_interval - (now - last)
                if wait <= 0:
                    self._last[domain] = now
                    return
            time.sleep(min(wait, 0.5))


# Shared limiter instance for all JAV scrapers — one per domain, so JavBus and
# JavLibrary don't interfere with each other but each stays within its own
# politeness budget.
_jav_rate_limiter = _DomainRateLimiter(min_interval=2.0)


def _jav_session() -> requests.Session:
    """A keep-alive session with a browser UA for JAV scrapers."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _JAV_BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ja;q=0.6,zh-TW;q=0.5",
    })
    return s


def _extract_domain(url: str) -> str:
    """Extract the registrable domain from a URL for rate limiting."""
    from urllib.parse import urlparse
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


class JavBusProvider(MetadataProvider):
    """JavBus — keyless HTML scrape for JAV covers + metadata.

    Handles both censored and uncensored titles (the URL path differs but the
    page structure is the same). Has mirror domains (buscdn.fun, busdmm.fun,
    etc.) for when the primary javbus.com domain is region-blocked — the
    scraper tries the primary, then falls back to a random mirror on failure.

    Page structure (stable across years):
      * Cover: ``<a class="bigImage" href="...">`` — may be an absolute DMM
        URL (``https://pics.dmm.co.jp/...``) or a relative javbus path.
      * Title: ``<title>`` tag or the ``<img>`` inside ``.bigImage``.
      * Studio: ``<span>製作商:</span>`` or ``<span>メーカー:</span>`` sibling.
      * Release date: ``<p>`` in the info container.
      * Actresses: ``<div class="star-name"><a>...</a></div>``.
    """

    name = "javbus"
    PRIMARY = "https://www.javbus.com"
    MIRRORS = [
        "https://www.buscdn.fun",
        "https://www.busdmm.fun",
        "https://www.busfan.fun",
        "https://www.busjav.fun",
        "https://www.cdnbus.fun",
        "https://www.dmmbus.fun",
        "https://www.dmmsee.fun",
        "https://www.seedmm.fun",
    ]

    def __init__(self) -> None:
        self._session = _jav_session()

    def fetch(self, title: str, year: str, section: str,
              raw_name: str = "") -> Optional[Dict[str, Any]]:
        code_info = jav_code_info(raw_name or title)
        if not code_info:
            return None
        code, is_censored = code_info
        # Censored codes use the primary path; uncensored uses /uncensored/.
        # JavBus auto-redirects, but being explicit avoids a redirect round-trip.
        path = f"/{code}" if is_censored else f"/uncensored/{code}"
        for base in [self.PRIMARY] + self.MIRRORS:
            url = base + path
            html_text = self._fetch_html(url)
            if html_text:
                meta = self._parse(html_text, code)
                if meta:
                    return meta
            # 404 on the primary -> try mirrors; don't retry the same domain.
        return None

    def _fetch_html(self, url: str) -> str:
        """Fetch raw HTML with the shared retry pattern (3 attempts, jittered)."""
        domain = _extract_domain(url)
        for attempt in range(3):
            try:
                r = self._session.get(url, timeout=20)
                if r.status_code == 200:
                    return r.text
                if r.status_code == 404:
                    return ""  # permanent miss for this domain
                r.close()
                if r.status_code not in _RETRY_STATUS:
                    return ""
                # transient — fall through to backoff
            except (requests.ConnectionError, requests.Timeout):
                pass
            if attempt < 2:
                time.sleep(_DEFAULT_BACKOFF_BASE * (2 ** attempt) * (0.5 + random.random()))
        return ""

    def _parse(self, html: str, code: str) -> Optional[Dict[str, Any]]:
        """Extract metadata from a JavBus detail page via regex."""
        # Cover: <a class="bigImage" href="...">
        cover_m = re.search(r'class="bigImage"\s+href="([^"]+)"', html)
        poster = cover_m.group(1) if cover_m else ""
        if poster and poster.startswith("/"):
            poster = self.PRIMARY + poster
        if not poster:
            return None  # no cover = not a real match

        # Title: <title>JAVBuster - ABP-123 - Title Here - JavBus</title>
        # or the alt attribute on the cover image.
        title_m = re.search(r'<title>([^<]+)</title>', html)
        title = ""
        if title_m:
            raw_title = title_m.group(1).strip()
            # Strip " - JavBus" suffix and the code prefix.
            title = re.sub(r'\s*-\s*JavBus\s*$', '', raw_title, flags=re.IGNORECASE)
            title = re.sub(r'^[A-Za-z]{2,6}-\d{2,5}\s*[-–:\s]\s*', '', title)
        if not title:
            alt_m = re.search(r'class="bigImage"[^>]*>\s*<img[^>]*alt="([^"]+)"', html)
            title = alt_m.group(1).strip() if alt_m else code

        # Studio: <span>製作商:</span> ... <a href="...">Studio</a>
        #         or <span>メーカー:</span> ...
        studio = ""
        for label in ("製作商", "メーカー", "Studio"):
            m = re.search(
                rf'<span[^>]*>{label}\s*:</span>\s*</p>\s*<p[^>]*>\s*<a[^>]*>([^<]+)</a>',
                html,
            )
            if m:
                studio = m.group(1).strip()
                break

        # Release date: <p>2023-05-14</p> in the info container.
        date_m = re.search(r'(\d{4}-\d{2}-\d{2})', html)
        release_date = date_m.group(1) if date_m else ""

        # Actresses: <div class="star-name"><a href="...">Name</a></div>
        actresses = re.findall(r'class="star-name"[^>]*>\s*<a[^>]*>([^<]+)</a>', html)
        genres = [a.strip() for a in actresses if a.strip()][:5]

        # Back cover: JavBus sometimes has a second image in the sample waterfall.
        # The first sample image is often the back cover.
        backdrop = ""
        sample_m = re.search(r'id="sample-waterfall"[^>]*>\s*<a[^>]*href="([^"]+)"', html)
        if sample_m:
            backdrop = sample_m.group(1)
            if backdrop.startswith("/"):
                backdrop = self.PRIMARY + backdrop

        return {
            "title": title,
            "year": release_date[:4] if release_date else "",
            "rating": 0,
            "synopsis": "",
            "genres": genres,
            "poster": poster,
            "backdrop": backdrop,
            "provider": self.name,
        }


class JavLibraryProvider(MetadataProvider):
    """JavLibrary — keyless HTML scrape, highest JAV coverage.

    Community-maintained database with very high coverage for both censored
    and uncensored JAV. Region-locked in some countries, so the scraper tries
    the English site first, then the Chinese mirror, then a known proxy.

    Page structure:
      * Cover: ``<img id="video_jacket_img" src="...">``
      * Title: ``<title>ABP-123 Title - JAVLibrary</title>``
      * Actresses: ``<a href="...">Name</a>`` in the cast section.
      * Studio: ``<a href="...">Studio</a>`` in the metadata table.
      * Release date: text in the metadata table.
    """

    name = "javlibrary"
    BASES = [
        "http://www.javlibrary.com/en",
        "http://www.javlibrary.com/cn",
        "http://www.javlibrary.com/ja",
    ]
    # Known mirrors/proxies — tried in order if the primary domains fail.
    MIRRORS = [
        "https://www.javlibrary.com/en",
    ]

    def __init__(self) -> None:
        self._session = _jav_session()

    def fetch(self, title: str, year: str, section: str,
              raw_name: str = "") -> Optional[Dict[str, Any]]:
        code_info = jav_code_info(raw_name or title)
        if not code_info:
            return None
        code = code_info[0]
        # JavLibrary uses ?v=<code> for the detail page.
        for base in self.BASES + self.MIRRORS:
            url = f"{base}/?v={code}"
            html_text = self._fetch_html(url)
            if html_text:
                meta = self._parse(html_text, code)
                if meta:
                    return meta
        return None

    def _fetch_html(self, url: str) -> str:
        """Fetch raw HTML with the shared retry pattern."""
        domain = _extract_domain(url)
        _jav_rate_limiter.acquire(domain)
        for attempt in range(3):
            try:
                r = self._session.get(url, timeout=20, allow_redirects=True)
                if r.status_code == 200:
                    # JavLibrary serves a "region locked" page with 200 — check
                    # for the lock marker before accepting.
                    if "region" in r.text.lower() and "blocked" in r.text.lower():
                        return ""  # try next mirror
                    return r.text
                if r.status_code == 404:
                    return ""
                r.close()
                if r.status_code not in _RETRY_STATUS:
                    return ""
            except (requests.ConnectionError, requests.Timeout):
                pass
            if attempt < 2:
                time.sleep(_DEFAULT_BACKOFF_BASE * (2 ** attempt) * (0.5 + random.random()))
        return ""

    def _parse(self, html: str, code: str) -> Optional[Dict[str, Any]]:
        """Extract metadata from a JavLibrary detail page via regex."""
        # Cover: <img id="video_jacket_img" src="...">
        cover_m = re.search(r'id="video_jacket_img"\s+src="([^"]+)"', html)
        poster = cover_m.group(1) if cover_m else ""
        if not poster:
            return None

        # Title: <title>ABP-123 Title Here - JAVLibrary</title>
        title_m = re.search(r'<title>([^<]+)</title>', html)
        title = ""
        if title_m:
            raw = title_m.group(1).strip()
            # Strip " - JAVLibrary" and the code prefix.
            title = re.sub(r'\s*-\s*JAVLibrary\s*$', '', raw, flags=re.IGNORECASE)
            title = re.sub(r'^[A-Za-z]{2,6}-\d{2,5}\s*[-–:\s]\s*', '', title)
        if not title:
            title = code

        # Actresses: links in the cast section.
        actresses = re.findall(r'<a href="vl_star\.php[^"]*"[^>]*>([^<]+)</a>', html)
        genres = [a.strip() for a in actresses if a.strip()][:5]

        # Studio: link in the metadata table.
        studio_m = re.search(r'<a href="vl_maker\.php[^"]*"[^>]*>([^<]+)</a>', html)
        studio = studio_m.group(1).strip() if studio_m else ""

        # Release date.
        date_m = re.search(r'(\d{4}-\d{2}-\d{2})', html)
        release_date = date_m.group(1) if date_m else ""

        return {
            "title": title,
            "year": release_date[:4] if release_date else "",
            "rating": 0,
            "synopsis": "",
            "genres": genres,
            "poster": poster,
            "backdrop": "",
            "provider": self.name,
        }


class FanzaProvider(MetadataProvider):
    """FANZA (DMM) — cover URL prediction + HTML scrape for censored JAV.

    FANZA is the primary distribution platform for censored JAV. It doesn't
    have a public search API (the DMM affiliate API needs an API ID + affiliate
    ID), so this provider uses two strategies:

    1. **Cover URL prediction**: FANZA cover images follow a predictable URL
       pattern: ``https://pics.dmm.co.jp/digital/video/<cid>/<cid>pl.jpg``
       where ``cid`` is the lowercase, zero-padded code (``ABP-123`` ->
       ``abp00123``). This gives a poster with zero HTML parsing.
    2. **HTML scrape**: the detail page at
       ``https://www.dmm.co.jp/digital/videoa/-/detail/=/cid=<cid>/``
       carries the title, actresses, studio, and release date. An age-check
       cookie is required.

    Only censored JAV is on FANZA — uncensored codes (Caribbean, Heyzo, etc.)
    return ``None`` immediately so the chain falls through to JavBus/JavLibrary.
    """

    name = "fanza"
    BASE = "https://www.dmm.co.jp"
    IMG_BASE = "https://pics.dmm.co.jp/digital/video"

    def __init__(self) -> None:
        self._session = _jav_session()
        self._session.cookies.set("age_check", "1", domain=".dmm.co.jp")

    def fetch(self, title: str, year: str, section: str,
              raw_name: str = "") -> Optional[Dict[str, Any]]:
        code_info = jav_code_info(raw_name or title)
        if not code_info:
            return None
        code, is_censored = code_info
        if not is_censored:
            return None  # FANZA only carries censored JAV

        # Build the FANZA content ID: lowercase, zero-padded to 5+ digits.
        # "ABP-123" -> "abp00123"; "SSNI-456" -> "ssni00456".
        prefix, num = code.split("-", 1)
        cid = f"{prefix.lower()}{num.zfill(5)}"

        # Strategy 1: predict the cover URL (zero HTML parsing needed).
        poster = f"{self.IMG_BASE}/{cid}/{cid}pl.jpg"

        # Strategy 2: scrape the detail page for full metadata.
        url = f"{self.BASE}/digital/videoa/-/detail/=/cid={cid}/"
        html_text = self._fetch_html(url)

        if html_text:
            meta = self._parse(html_text, code, poster)
            if meta:
                return meta

        # Even if the detail page fails, the predicted cover URL is often
        # correct — return a minimal record so the tile gets a poster.
        # The artwork fetcher will validate the URL (404 -> no tile).
        if poster:
            return {
                "title": code,
                "year": "",
                "rating": 0,
                "synopsis": "",
                "genres": [],
                "poster": poster,
                "backdrop": "",
                "provider": self.name,
            }
        return None

    def _fetch_html(self, url: str) -> str:
        """Fetch raw HTML with the shared retry pattern."""
        domain = _extract_domain(url)
        _jav_rate_limiter.acquire(domain)
        for attempt in range(3):
            try:
                r = self._session.get(url, timeout=20, allow_redirects=True)
                if r.status_code == 200:
                    return r.text
                if r.status_code == 404:
                    return ""
                r.close()
                if r.status_code not in _RETRY_STATUS:
                    return ""
            except (requests.ConnectionError, requests.Timeout):
                pass
            if attempt < 2:
                time.sleep(_DEFAULT_BACKOFF_BASE * (2 ** attempt) * (0.5 + random.random()))
        return ""

    def _parse(self, html: str, code: str, predicted_poster: str) -> Optional[Dict[str, Any]]:
        """Extract metadata from a FANZA detail page via regex."""
        # Title: <title>...</title> — FANZA titles include the code + actress.
        title_m = re.search(r'<title>([^<]+)</title>', html)
        title = ""
        if title_m:
            raw = title_m.group(1).strip()
            # Strip common FANZA title suffixes.
            title = re.sub(r'\s*[-–]\s*FANZA.*$', '', raw, flags=re.IGNORECASE)
            title = re.sub(r'\s*[-–]\s*DMM.*$', '', title, flags=re.IGNORECASE)
        if not title:
            title = code

        # Cover: try the page's own cover image first, fall back to predicted.
        cover_m = re.search(r'<a[^>]*class="[^"]*floatleft[^"]*"[^>]*href="([^"]+)"', html)
        if not cover_m:
            cover_m = re.search(r'src="(https://pics\.dmm\.co\.jp/[^"]+pl\.jpg)"', html)
        poster = cover_m.group(1) if cover_m else predicted_poster

        # Actresses: <a href=".../article=actress/id=.../">Name</a>
        actresses = re.findall(
            r'<a[^>]*href="[^"]*article=actress[^"]*"[^>]*>([^<]+)</a>', html,
        )
        genres = [a.strip() for a in actresses if a.strip()][:5]

        # Studio: <a href=".../article=maker/id=.../">Studio</a>
        studio_m = re.search(
            r'<a[^>]*href="[^"]*article=maker[^"]*"[^>]*>([^<]+)</a>', html,
        )
        studio = studio_m.group(1).strip() if studio_m else ""

        # Release date: <td>2023/05/14</td> or 2023-05-14.
        date_m = re.search(r'(\d{4}[-/]\d{2}[-/]\d{2})', html)
        release_date = date_m.group(1).replace("/", "-") if date_m else ""

        return {
            "title": title,
            "year": release_date[:4] if release_date else "",
            "rating": 0,
            "synopsis": "",
            "genres": genres,
            "poster": poster,
            "backdrop": "",
            "provider": self.name,
        }


# ---------------------------------------------------------------------------
# Adult VOD
# ---------------------------------------------------------------------------

# Unambiguous adult markers — safe to match against an entry *name* as well as
# its group-title.
_ADULT_STRONG_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:xxx+|porn\w*|hentai|jav|18\s*\+|\+\s*18)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
# Ambiguous markers — only trusted on a group-title. A film called "Adult
# World" or "Erotica" must not be routed away from TMDb on its name alone.
_ADULT_WEAK_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:adults?|erotic\w*|for\s+adults)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)

# JAV disc codes: "ABP-123", "SSNI 456", "abp00123".
_JAV_CODE_RE = re.compile(r"\b([A-Za-z]{2,6})([-\s_]?)(\d{2,5})\b")
_YEAR_ONLY_RE = re.compile(r"(?:19|20)\d{2}")

# Suffixes bolted onto JAV codes in playlist filenames that must be stripped
# before the code is used for lookup. Order matters: longer suffixes first so
# "-leak" is tried before "-l". All are case-insensitive.
_JAV_SUFFIX_RE = re.compile(
    r"[-_.\s]+(?:"
    r"leak|uncensored|uncut|uc|c|ch|cntv|hd|fhd|uhd|4k|"
    r"part\s*\d+|p\d+|cd\d+|disc\d+|"
    r"\d{1,2}"  # a trailing part number ("abp00123_5")
    r")\s*$",
    re.IGNORECASE,
)

# Censored JAV code prefixes (major studios). Codes starting with these are
# censored (mosaic) releases — the vast majority of JAV. Used to route to the
# right metadata source: FANZA/R18/JavBus cover censored; JavLibrary covers
# both but separates them by URL path.
_JAV_CENSORED_PREFIXES = frozenset({
    "abp", "abwin", "adtn", "aka", "apak", "apns", "atid", "bda", "blk", "bgn",
    "cawd", "cjod", "cls", "dass", "dfeb", "dldss", "dmat", "dpmi", "dvaj",
    "ebod", "evo", "fcdss", "fsdss", "gdgd", "gfdh", "gnab", "hbad", "hnd",
    "hnds", "hodv", "hunt", "ipx", "ipzz", "ipx", "jufd", "jul", "jux", "kawd",
    "kire", "kmdr", "knmb", "kwbd", "lafbd", "lulu", "mdbk", "mdtm", "mdte",
    "mim", "mimk", "mist", "mcsr", "midv", "mide", "miad", "mxgs", "nacr",
    "nacr", "nagi", "nanf", "ndra", "nice", "nima", "nttr", "oka", "okax",
    "orec", "orn", "pkpd", "pred", "prbr", "rbd", "rbf", "rebd", "roeb", "sdab",
    "sdde", "sdjs", "sdnm", "siro", "sivr", "skmj", "skyo", "sma", "snis",
    "sod", "ssis", "ssni", "star", "stars", "stcv", "sw", "saba", "smdh",
    "t28", "tcd", "tdbr", "tfd", "tmvi", "tpn", "tre", "tui", "umso", "vagu",
    "venx", "waap", "wanz", "xcute", "zuko",
})

# Uncensored JAV code prefixes. These are smaller studios that release without
# mosaic. FANZA does NOT carry these — they need a different source path.
_JAV_UNCENSORED_PREFIXES = frozenset({
    "carib", "caribpr", "caribbean", "heyzo", "heyhd", "1pon", "pacopaco",
    "paco", "tokyohot", "muramura", "1000giri", "gachi", "h4610", "c0930",
    "h0930", "aki", "kin8", "mesubuta", "sperm", "tale", "shiro", "tohjiro",
    "natspo", "skyo", "zenra", "fc2", "kdv", "rbd", "tktk", "kin8tengoku",
})

# Studio/site prefix adult playlists prepend: "Brazzers - Scene Title".
_STUDIO_PREFIX_RE = re.compile(r"^\s*[A-Za-z0-9'&.\s]{2,25}\s+-\s+")
# Trailing scene date: "... 2023-05-14" / "... 14.05.2023".
_SCENE_DATE_RE = re.compile(
    r"[\s\-_]+(?:\d{4}[-.]\d{2}[-.]\d{2}|\d{2}[-.]\d{2}[-.]\d{4})\s*$"
)


def looks_adult(group: str, name: str = "") -> bool:
    """True when an entry is adult VOD.

    Drives *provider routing only* — adult entries stay in the Movies section
    and are not gated in the UI. Group-titles are the reliable signal; names
    are only consulted for the unambiguous markers.
    """
    g = group or ""
    return bool(
        _ADULT_STRONG_RE.search(g)
        or _ADULT_WEAK_RE.search(g)
        or _ADULT_STRONG_RE.search(name or "")
    )


def jav_code(name: str) -> str:
    """Extract a normalized JAV code ("abp00123" -> "ABP-123"), else ''.

    Deliberately strict: "Regular Movie 2023" must not read as "MOVIE-2023"
    and get routed to the /jav endpoint. Suffixes like "-c", "-leak", "_5"
    (part 5) are stripped before normalization so the code matches the
    catalogue key the metadata sources expect.
    """
    info = jav_code_info(name)
    return info[0] if info else ""


def jav_code_info(name: str) -> Optional[Tuple[str, bool]]:
    """Extract ``(normalized_code, is_censored)`` from a name, else ``None``.

    ``is_censored`` is ``True`` for major-studio censored (mosaic) releases,
    ``False`` for uncensored studios (Caribbean, Heyzo, etc.). Unknown prefixes
    default to censored (the common case). This drives source routing: FANZA
    and R18 carry censored titles; uncensored needs JavLibrary/JavBus.
    """
    text = name or ""
    # Strip common suffixes before matching so "ABP-123-c" -> "ABP-123".
    cleaned = _JAV_SUFFIX_RE.sub("", text)
    for m in _JAV_CODE_RE.finditer(cleaned):
        letters, sep, digits = m.group(1), m.group(2), m.group(3)
        if _YEAR_ONLY_RE.fullmatch(digits):
            continue  # a release year, not a disc number
        if sep.isspace() and not letters.isupper():
            continue  # "Some 123" only counts in the uppercase form codes use
        prefix = letters.lower()
        is_censored = prefix not in _JAV_UNCENSORED_PREFIXES
        code = f"{letters.upper()}-{digits.lstrip('0') or '0'}"
        return (code, is_censored)
    return None


def title_similarity(a: str, b: str) -> float:
    """0..1 similarity between two titles (sequence ratio or token overlap).

    TPDB's ``q`` is a loose keyword search: nonsense returns nothing, but a
    short query of ordinary words matches arbitrary scenes. Every title-based
    match must therefore be verified before its poster is trusted.
    """
    import difflib

    na = re.sub(r"[^a-z0-9 ]", " ", (a or "").lower())
    nb = re.sub(r"[^a-z0-9 ]", " ", (b or "").lower())
    na = re.sub(r"\s+", " ", na).strip()
    nb = re.sub(r"\s+", " ", nb).strip()
    if not na or not nb:
        return 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb) / max(len(ta), 1)
    return max(ratio, overlap)


# Filler words that must not carry a title match on their own.
_MATCH_STOPWORDS = {
    "the", "a", "an", "and", "of", "in", "my", "your", "with", "for", "on",
}


def _match_tokens(s: str) -> List[str]:
    return [t for t in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()
            if t not in _MATCH_STOPWORDS]


def title_containment(name: str, row_title: str) -> float:
    """Fraction of ``row_title``'s tokens that appear in ``name``.

    Adult playlist names are studio-led and much longer than the scene title
    they contain, so plain similarity scores them near zero. Asking whether
    the *returned* title is contained in the playlist name is the right test.
    """
    rt = _match_tokens(row_title)
    if not rt:
        return 0.0
    nm = set(_match_tokens(name))
    return sum(1 for t in rt if t in nm) / len(rt)


def site_stripped_similarity(name: str, row: Dict[str, Any]) -> float:
    """Similarity after removing the row's OWN studio name from ``name``.

    The studio is what breaks the comparison, and each row already carries
    ``site.name`` — so it costs no extra request to strip it.
    """
    site = ((row.get("site") or {}) or {}).get("name") or ""
    nm = _match_tokens(adult_clean_title(name))
    for tok in _match_tokens(site):
        if tok in nm:
            nm.remove(tok)
    remainder = " ".join(nm)
    rt = " ".join(_match_tokens(row.get("title", "")))
    if not remainder or not rt:
        return 0.0
    return title_similarity(remainder, rt)


def adult_clean_title(name: str) -> str:
    """Title cleanup tuned for adult VOD.

    :func:`clean_title` is built for scene-release movie names and mangles
    adult entries: the country-prefix and release-group rules eat studio
    names. Here the studio prefix and trailing scene date are dropped
    explicitly instead.
    """
    t = clean_title(name)
    t = _SCENE_DATE_RE.sub("", t)
    stripped = _STUDIO_PREFIX_RE.sub("", t)
    # Only accept the strip if something substantial survives.
    if len(stripped) >= 4:
        t = stripped
    return re.sub(r"\s+", " ", t).strip()


# Decorations playlists bolt onto channel names; they must not take part in
# matching ("BBC One HD" and "BBC One FHD" are the same channel).
_LOGO_NOISE_TOKENS = {
    "hd", "hdtv", "fhd", "uhd", "sd", "4k", "8k", "hq", "lq", "raw",
    "hevc", "h264", "h265", "1080p", "1080i", "1080", "720p", "720",
    "480p", "2160p", "2160", "vip", "backup", "alt", "multi",
}

# Country prefixes playlists put in front of the real name ("UK: Sky News",
# "Portugal  RTP1"). Values are the iptv-org country code used to disambiguate.
_COUNTRY_WORDS = {
    "uk": "uk", "gb": "uk", "england": "uk", "unitedkingdom": "uk",
    "us": "us", "usa": "us", "unitedstates": "us", "ca": "ca", "canada": "ca",
    "pt": "pt", "portugal": "pt", "fr": "fr", "france": "fr",
    "es": "es", "spain": "es", "espana": "es", "de": "de", "germany": "de",
    "deutschland": "de", "it": "it", "italy": "it", "italia": "it",
    "nl": "nl", "netherlands": "nl", "be": "be", "belgium": "be",
    "br": "br", "brazil": "br", "brasil": "br", "tr": "tr", "turkey": "tr",
    "pl": "pl", "poland": "pl", "ro": "ro", "romania": "ro",
    "ru": "ru", "russia": "ru", "in": "in", "india": "in",
    "id": "id", "indonesia": "id", "au": "au", "australia": "au",
    "ie": "ie", "ireland": "ie", "ch": "ch", "at": "at", "se": "se",
    "no": "no", "dk": "dk", "fi": "fi", "gr": "gr", "greece": "gr",
    "mx": "mx", "mexico": "mx", "ar": "ar", "argentina": "ar",
}


class IptvOrgLogos:
    """Keyless fallback for channel logos backed by the iptv-org database.

    Two endpoints are needed: ``logos.json`` is keyed by channel *id*
    (``BBCNews.uk``) and carries no names, so it is joined against
    ``channels.json`` to build a name -> logo index. Raster formats win over
    SVG because Qt ships without the SVG image plugin here.

    The joined index is cached on disk and refreshed weekly.
    """

    LOGOS_URL = "https://iptv-org.github.io/api/logos.json"
    CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
    CACHE_KEY = "iptvorg:logos:index"
    CACHE_VERSION = 2  # bump to invalidate stale on-disk indexes
    REFRESH_SECONDS = 7 * 86400  # weekly
    _RASTER = ("PNG", "WEBP", "JPEG", "JPG", "GIF")

    def __init__(self, cache: IPTVCache) -> None:
        self.cache = cache
        self._index: Optional[Dict[str, str]] = None
        self._index_time = 0.0
        self._lock = threading.Lock()
        self._resolved: Dict[str, str] = {}  # query -> url (incl. misses)

    # -- name normalization --------------------------------------------------
    @staticmethod
    def _normalize(name: str) -> str:
        """'BBC One HD' / 'bbc-one' -> 'bbcone' (decorations dropped)."""
        s = (name or "").lower().replace("&", "and")
        s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
        tokens = [t for t in re.findall(r"[a-z0-9]+", s) if t not in _LOGO_NOISE_TOKENS]
        return "".join(tokens)

    @classmethod
    def _split_country(cls, name: str) -> tuple:
        """Strip playlist decorations: '60 UK: Sky News FHD' -> ('skynews', 'uk').

        Leading channel numbers and country prefixes have to go before
        matching, but only recognised country words are stripped when the
        separator is plain whitespace — otherwise 'Discovery Channel' would
        lose its first word."""
        s = (name or "").lower()
        s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)   # (Meo), [VIP], (D)
        s = re.sub(r"^[\s|:.\-]*\d{1,4}\b", " ", s)   # leading channel number
        country = ""
        for _ in range(2):  # e.g. "10 UK FHD  TNT Sport 1"
            m = re.match(r"\s*([a-z]{2,14})\s*[|:]+\s*(\S.*)", s)
            if m and not (len(m.group(1)) <= 3 or m.group(1) in _COUNTRY_WORDS):
                m = None
            if m is None:
                m = re.match(r"\s*([a-z]{2,14})\s+(\S.*)", s)
                if m and m.group(1) not in _COUNTRY_WORDS:
                    break
            if m is None:
                break
            token, s = m.group(1), m.group(2)
            country = country or _COUNTRY_WORDS.get(token, token if len(token) <= 3 else "")
        return cls._normalize(s), country

    # -- index ---------------------------------------------------------------
    def _logo_score(self, entry: Dict[str, Any]) -> int:
        fmt = (entry.get("format") or "").upper()
        score = 10 if fmt in self._RASTER else 0
        if entry.get("in_use"):
            score += 4
        if not entry.get("feed"):
            score += 2  # main feed, not a regional variant
        return score

    def _build_index(self) -> Dict[str, str]:
        r = requests.get(self.LOGOS_URL, timeout=60)
        r.raise_for_status()
        best: Dict[str, tuple] = {}
        for entry in r.json():
            cid, url = entry.get("channel"), entry.get("url")
            if not cid or not url:
                continue
            score = self._logo_score(entry)
            if cid not in best or score > best[cid][0]:
                best[cid] = (score, url)

        r = requests.get(self.CHANNELS_URL, timeout=60)
        r.raise_for_status()
        idx: Dict[str, str] = {}
        for ch in r.json():
            hit = best.get(ch.get("id", ""))
            if not hit:
                continue
            url = hit[1]
            country = (ch.get("country") or "").lower()
            for nm in [ch.get("name", ""), *(ch.get("alt_names") or [])]:
                key = self._normalize(nm)
                if not key:
                    continue
                idx.setdefault(key, url)
                if country:
                    # Disambiguates 'UK | Sky News' from other Sky News feeds.
                    idx.setdefault(key + country, url)
            idx.setdefault(self._normalize(ch.get("id", "").split(".")[0]), url)
        return idx

    def _load_index(self) -> Dict[str, str]:
        with self._lock:
            if self._index is not None and time.time() - self._index_time < self.REFRESH_SECONDS:
                return self._index
            cached = self.cache.load_metadata(self.CACHE_KEY)
            if cached and cached.get("version") == self.CACHE_VERSION \
                    and time.time() - cached.get("updated", 0) < self.REFRESH_SECONDS:
                self._index = cached["index"]
                self._index_time = cached["updated"]
                return self._index
            try:
                idx = self._build_index()
                if not idx:
                    raise ValueError("iptv-org index came back empty")
                self._index = idx
                self._index_time = time.time()
                self.cache.save_metadata(
                    self.CACHE_KEY, "live", "logos", "", "iptvorg",
                    {"index": idx, "updated": self._index_time, "version": self.CACHE_VERSION},
                )
            except Exception as exc:
                logger.warning("iptv-org logos index fetch failed: %s", exc)
                if cached and cached.get("version") == self.CACHE_VERSION:
                    self._index = cached["index"]
                    self._index_time = cached.get("updated", 0)
                else:
                    self._index = {}
                    self._index_time = 0.0  # retry on the next lookup
            return self._index or {}

    def lookup(self, channel_name: str) -> str:
        if not channel_name:
            return ""
        cached = self._resolved.get(channel_name)
        if cached is not None:
            return cached
        idx = self._load_index()
        if not idx:
            return ""  # index unavailable — not a miss, so don't memoize it
        url = ""
        key, country = self._split_country(channel_name)
        for candidate in ((key + country) if country else "", key):
            if candidate and candidate in idx:
                url = idx[candidate]
                break
        if not url and len(key) >= 5:
            # Prefix match ('bbconenorthwest' -> 'bbcone'); the longest
            # matching key wins so 'skysportsf1' beats 'sky'.
            match = max(
                (k for k in idx if len(k) >= 5 and (key.startswith(k) or k.startswith(key))),
                key=len,
                default="",
            )
            url = idx.get(match, "")
        self._resolved[channel_name] = url
        return url


class ChannelLogoChain:
    """Multi-source channel logo fallback chain.

    Tries logo sources in order until one returns a URL:

    1. **IPTV-org** — the existing keyless database (highest quality, curated).
    2. **Google S2 favicon** — ``https://www.google.com/s2/favicons?domain=<d>&sz=128``
       (keyless, works for any domain-like channel name; small but universal).
    3. **DuckDuckGo favicon** — ``https://icons.duckduckgo.com/ip3/<d>.ico``
       (keyless, often higher quality than Google S2 for well-known sites).

    Sources 2-3 need a *domain* derived from the channel name. Channel names
    like "BBC News" or "Sky Sports" don't carry domains, so the domain is
    guessed by joining the normalized name tokens: "BBC News" -> "bbcnews.com".
    This is a heuristic — it won't find logos for every channel, but it
    catches the common case where a channel name matches its website domain.

    The chain is keyless and always available. Results are cached per-channel
    in the IptvOrgLogos resolved dict (shared).
    """

    def __init__(self, iptvorg: IptvOrgLogos) -> None:
        self.iptvorg = iptvorg
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _JAV_BROWSER_UA})

    def lookup(self, channel_name: str) -> str:
        """Return the best logo URL for a channel, trying all sources."""
        if not channel_name:
            return ""
        # Source 1: IPTV-org (highest quality, curated).
        url = self.iptvorg.lookup(channel_name)
        if url:
            return url
        # Sources 2-3: favicon-based fallbacks. Need a domain guess.
        domain = self._guess_domain(channel_name)
        if not domain:
            return ""
        # Source 2: Google S2 favicon (128px, keyless).
        google_url = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
        if self._url_ok(google_url):
            return google_url
        # Source 3: DuckDuckGo favicon (keyless, often higher quality).
        ddg_url = f"https://icons.duckduckgo.com/ip3/{domain}.ico"
        if self._url_ok(ddg_url):
            return ddg_url
        return ""

    @staticmethod
    def _guess_domain(channel_name: str) -> str:
        """Guess a website domain from a channel name.

        "BBC News" -> "bbcnews.com", "Sky Sports F1" -> "skysports.com".
        Country/quality decorations are stripped first. Returns "" if the
        name is too short or has no usable tokens.
        """
        key, _country = IptvOrgLogos._split_country(channel_name)
        if len(key) < 3:
            return ""
        # Common TLD guesses for TV channels.
        return f"{key}.com"

    def _url_ok(self, url: str) -> bool:
        """HEAD-check a favicon URL. Returns True if it likely exists.

        Uses a HEAD request (no body download). A 200 with image content-type
        = exists; 404 = permanent miss; anything else = transient, skip.
        """
        try:
            r = self._session.head(url, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                ct = (r.headers.get("Content-Type") or "").lower()
                # Some favicon services return text/html for 404s with 200
                # status — reject those.
                return "image" in ct or "icon" in ct or "octet-stream" in ct
            return False
        except (requests.ConnectionError, requests.Timeout, requests.RequestException):
            return False


# ---------------------------------------------------------------------------
# Rate limiter + pipeline
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Token-bucket-ish limiter: at most ``rate`` calls per ``per`` seconds."""

    def __init__(self, rate: float = 5.0, per: float = 1.0) -> None:
        self.rate = rate
        self.per = per
        self._lock = threading.Lock()
        self._tokens = rate
        self._last = time.monotonic()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self.rate, self._tokens + elapsed * (self.rate / self.per))
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            time.sleep(0.05)


class MetadataPipeline:
    """Coordinates on-demand metadata lookups with caching + rate limiting.

    Usage from the UI: call :meth:`resolve_async` for a visible item; the
    callback is invoked (from a worker thread) with the merged metadata dict.

    Providers are tried in an ordered chain (first non-empty poster wins,
    backdrops merged from any provider that has one). The chain is built per
    entry from ``(section, is_adult, has_jav_code)`` — see :meth:`_build_chain`.
    Per-provider negative caching skips providers that recently missed for the
    same key, so a TPDB miss doesn't block a later StashDB/JavLibrary attempt
    when those providers are added in later phases.
    """

    # Negative-cache TTL: a provider miss is remembered for this long so an
    # unmatched title isn't re-queried on every visit. Per-provider, so a new
    # provider added to the chain gets tried even when others already missed.
    NEGATIVE_TTL = 6 * 3600

    def __init__(
        self,
        cache: IPTVCache,
        tmdb_api_key: str = "",
        rate_per_second: float = 5.0,
        tpdb_api_key: str = "",
        stashdb_api_key: str = "",
        omdb_api_key: str = "",
        fanarttv_api_key: str = "",
        enable_javbus: bool = True,
        enable_javlibrary: bool = True,
        enable_fanza: bool = True,
        enable_wikipedia: bool = True,
    ) -> None:
        self.cache = cache
        self.tmdb = TMDBProvider(tmdb_api_key) if tmdb_api_key else None
        self.tvmaze = TVMazeProvider()
        self.tpdb = TPDBProvider(tpdb_api_key) if tpdb_api_key else None
        self.stashdb = StashDBProvider(stashdb_api_key) if stashdb_api_key else None
        self.omdb = OMDbProvider(omdb_api_key) if omdb_api_key else None
        self.fanarttv = FanartTvProvider(fanarttv_api_key) if fanarttv_api_key else None
        # Wikipedia is keyless — always available as the last-resort poster
        # fallback for non-adult movies/series. Can be disabled by the user.
        self.wikipedia = WikipediaProvider() if enable_wikipedia else None
        # JAV providers — keyless, always available. On by default per the
        # user's decision (all sources on). The chain tries them in order:
        # FANZA (censored only, cover URL prediction) → JavBus (both) →
        # JavLibrary (both, highest coverage) → TPDB /jav (API, thinnest).
        # Each can be disabled by the user via config flags.
        self.fanza = FanzaProvider() if enable_fanza else None
        self.javbus = JavBusProvider() if enable_javbus else None
        self.javlibrary = JavLibraryProvider() if enable_javlibrary else None
        self.logos = IptvOrgLogos(cache)
        self.logo_chain = ChannelLogoChain(self.logos)
        self._limiter = _RateLimiter(rate=rate_per_second, per=1.0)
        self._executor = _BoundedExecutor(max_workers=4)
        self._inflight: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def set_tmdb_key(self, key: str) -> None:
        self.tmdb = TMDBProvider(key) if key else None

    def set_tpdb_key(self, key: str) -> None:
        self.tpdb = TPDBProvider(key) if key else None

    def set_stashdb_key(self, key: str) -> None:
        self.stashdb = StashDBProvider(key) if key else None

    def set_omdb_key(self, key: str) -> None:
        self.omdb = OMDbProvider(key) if key else None

    def set_fanarttv_key(self, key: str) -> None:
        self.fanarttv = FanartTvProvider(key) if key else None

    def channel_logo_fallback(self, name: str) -> str:
        """Synchronous logo lookup via the multi-source chain (iptv-org →
        favicon fallbacks). Used when tvg-logo is missing."""
        return self.logo_chain.lookup(name)

    # -- provider chain ------------------------------------------------------
    def _build_chain(
        self, section: str, name: str, year: str, group: str,
        query: str, raw_name: str,
    ) -> List[Tuple[str, Callable[[], Optional[Dict[str, Any]]]]]:
        """Build the ordered provider chain for this entry.

        Returns ``[(provider_name, fetch_fn), ...]`` where ``fetch_fn`` takes
        no arguments and returns the provider's metadata dict or ``None``.
        First non-empty poster wins; the chain order encodes the routing logic:

        * **JAV code present** (adult entry with a recognisable disc code):
          FANZA → JavBus → JavLibrary → TPDB /jav. Official APIs/scrapers
          first (richer covers), TPDB last (thinnest JAV coverage).
        * **Adult, no JAV code**: TPDB (western adult) → TMDb (borderline).
        * **Non-adult movies**: TMDb → (Phase 4 will add OMDb/Wikipedia).
        * **Series**: TMDb → TVMaze fallback.

        New providers added in later phases slot in here.
        """
        is_adult = looks_adult(group, name)
        chain: List[Tuple[str, Callable[[], Optional[Dict[str, Any]]]]] = []

        # JAV codes get a dedicated chain: FANZA (censored, cover prediction)
        # → JavBus (both, HTML scrape) → JavLibrary (both, highest coverage)
        # → TPDB /jav (API, thinnest). A JAV code is an exact catalogue key,
        # so these providers don't need a title match — the code alone is
        # sufficient (same principle as TPDB's /jav trust).
        if is_adult:
            code_info = jav_code_info(raw_name or name)
            if code_info:
                if self.fanza:
                    chain.append(("fanza", lambda: self.fanza.fetch(
                        "", year, section, raw_name=raw_name)))
                if self.javbus:
                    chain.append(("javbus", lambda: self.javbus.fetch(
                        "", year, section, raw_name=raw_name)))
                if self.javlibrary:
                    chain.append(("javlibrary", lambda: self.javlibrary.fetch(
                        "", year, section, raw_name=raw_name)))
                if self.tpdb:
                    chain.append(("tpdb", lambda: self.tpdb.fetch(
                        "", year, section, raw_name=raw_name)))
                return chain

        # Adult VOD (no JAV code) goes to TPDB first, then StashDB: TMDb
        # filters adult titles out of its search results entirely, so it can
        # only ever miss on these. StashDB is the second source for western
        # adult scenes — community-driven, complements TPDB's coverage.
        if is_adult:
            adult_title = adult_clean_title(name)
            if self.tpdb:
                chain.append(("tpdb", lambda: self.tpdb.fetch(
                    adult_title, year, section, raw_name=raw_name)))
            if self.stashdb:
                chain.append(("stashdb", lambda: self.stashdb.fetch(
                    adult_title, year, section, raw_name=raw_name)))

        # TMDb (if configured) — the primary source for non-adult movies/series.
        if self.tmdb:
            chain.append(("tmdb", lambda: self.tmdb.fetch(query, year, section)))

        # TVmaze fallback for series (keyless, so always available).
        if section == "series":
            chain.append(("tvmaze", lambda: self.tvmaze.fetch(query, year, section)))

        # OMDb fallback (free key) — covers older/obscure titles TMDb misses.
        if self.omdb:
            chain.append(("omdb", lambda: self.omdb.fetch(
                query, year, section, raw_name=raw_name)))

        # Wikipedia — keyless last-resort poster for well-known titles.
        # Only for non-adult entries (Wikipedia doesn't cover adult VOD).
        # Can be disabled by the user via config.enable_wikipedia.
        if not is_adult and self.wikipedia:
            chain.append(("wikipedia", lambda: self.wikipedia.fetch(
                query, year, section, raw_name=raw_name)))

        return chain

    def resolve_async(
        self,
        section: str,
        name: str,
        year: str,
        on_done: Callable[[str, Dict[str, Any]], None],
        group: str = "",
    ) -> None:
        """Queue a metadata lookup; ``on_done(key, metadata)`` is called off-thread.

        ``group`` is the entry's group-title, used only to route adult VOD to
        TPDB — it is deliberately not part of the cache key.
        """
        key = metadata_key(section, name, year)
        # 1. Cache hit -> immediate callback (caller marshals to GUI thread).
        cached = self.cache.load_metadata(key)
        if cached:
            if cached.get("negative"):
                # Per-provider negative cache: short-circuit only when EVERY
                # provider in the chain has a recent miss. A new provider
                # added to the chain (later phase) still gets tried.
                if self._all_providers_recently_missed(cached, section, name, year, group):
                    on_done(key, {})
                    return
            else:
                on_done(key, cached)
                return

        # 2. Dedupe concurrent lookups for the same key.
        with self._lock:
            ev = self._inflight.get(key)
            if ev is None:
                ev = threading.Event()
                self._inflight[key] = ev
                self._executor.submit(self._worker, key, section, name, year, on_done, ev, group)
            else:
                self._executor.submit(self._wait, key, ev, on_done)

    def _all_providers_recently_missed(
        self, cached: Dict[str, Any], section: str, name: str, year: str, group: str,
    ) -> bool:
        """True when every provider in the chain has a recent miss on record.

        Falls back to the legacy flat negative cache (``updated_at`` only) when
        ``provider_misses`` is absent — older cache entries still work.
        """
        title = clean_title(name)
        if not year:
            year = extract_year(name)
        query = title
        ym = _YEAR_RE.search(title)
        if ym:
            query = title[:ym.start()].strip() or title
        chain = self._build_chain(section, name, year, group, query, name)
        if not chain:
            return True  # no providers configured — nothing to retry

        misses = cached.get("provider_misses") or {}
        now = time.time()
        # Legacy flat negative cache (no per-provider breakdown): treat as a
        # full miss only within the TTL — preserves the old behaviour for
        # cache entries written before this refactor.
        if not misses:
            return now - cached.get("updated_at", 0) < self.NEGATIVE_TTL

        for prov_name, _fn in chain:
            miss_ts = misses.get(prov_name)
            if miss_ts is None or now - miss_ts >= self.NEGATIVE_TTL:
                return False  # at least one provider hasn't missed recently
        return True

    def _wait(self, key: str, ev: threading.Event, on_done) -> None:
        ev.wait(timeout=30)
        try:
            cached = self.cache.load_metadata(key)
        except Exception:
            logger.exception("metadata cache load failed for %s", key)
            cached = None
        # Always fire the callback — a failed/negative lookup must not leave
        # the UI waiting forever.
        if cached and not cached.get("negative"):
            on_done(key, cached)
        else:
            on_done(key, {})

    def _enrich_backdrop(self, result: Dict[str, Any], section: str) -> str:
        """Try to fetch a backdrop from Fanart.tv using the TMDb ID.

        This is a best-effort supplement: if the primary provider returned a
        TMDb ID (in ``_tmdb_id``), Fanart.tv is queried for a community
        backdrop. Returns "" if no backdrop is found or no ID is available.
        """
        if not self.fanarttv:
            return ""
        tmdb_id = result.get("_tmdb_id", "")
        if not tmdb_id:
            return ""
        is_series = result.get("_is_series", section == "series")
        return self.fanarttv.fetch_backdrop(tmdb_id, is_series=is_series)

    def _worker(self, key: str, section: str, name: str, year: str, on_done,
                ev: threading.Event, group: str = "") -> None:
        result: Dict[str, Any] = {}
        try:
            self._limiter.acquire()
            title = clean_title(name)
            if not year:
                year = extract_year(name)
            # TMDb only matches when the year travels in the year PARAMETER —
            # a bare year in the query text ("Dunki 2023") returns ZERO
            # results, verified live. And in scene filenames everything after
            # the year is release junk ("Masters of the Universe 2026
            # TELESYNCx264-DKS"), so the query is the text up to the year.
            query = title
            ym = _YEAR_RE.search(title)
            if ym:
                query = title[:ym.start()].strip() or title

            chain = self._build_chain(section, name, year, group, query, name)

            # Load the existing negative record (if any) to skip providers
            # that recently missed for this key — a TPDB miss must not block a
            # StashDB/JavLibrary attempt when those are added to the chain.
            cached = self.cache.load_metadata(key) or {}
            misses: Dict[str, float] = {}
            if cached.get("negative"):
                misses = dict(cached.get("provider_misses") or {})
            now = time.time()
            # Drop expired misses so a provider gets retried after the TTL.
            misses = {k: v for k, v in misses.items()
                      if now - v < self.NEGATIVE_TTL}

            for prov_name, fetch_fn in chain:
                if prov_name in misses:
                    continue  # this provider already missed recently
                try:
                    res = fetch_fn()
                except Exception:
                    logger.debug("provider %s raised for %r", prov_name, name, exc_info=True)
                    res = None
                if res:
                    result = res
                    break
                # Record the miss so this provider is skipped next time within
                # the TTL window. Other providers in the chain still get tried.
                misses[prov_name] = time.time()

            if result:
                # Fanart.tv backdrop enrichment: if the primary provider
                # returned a poster but no backdrop, try Fanart.tv for a
                # higher-quality community backdrop. This is a supplement,
                # not a replacement — the poster is already saved.
                if (self.fanarttv and result.get("poster")
                        and not result.get("backdrop")):
                    try:
                        backdrop = self._enrich_backdrop(result, section)
                        if backdrop:
                            result["backdrop"] = backdrop
                    except Exception:
                        logger.debug("Fanart.tv backdrop enrich failed for %r", name)
                self.cache.save_metadata(key, section, title, year, result.get("provider", ""), result)
            else:
                # Cache the negative result with per-provider miss timestamps
                # so a later chain expansion (new provider) still gets tried.
                self.cache.save_metadata(
                    key, section, title, year, "none",
                    {"negative": True, "updated_at": time.time(),
                     "provider_misses": misses},
                )
        except Exception:
            logger.exception("metadata resolve failed for %r", name)
        finally:
            ev.set()
            with self._lock:
                self._inflight.pop(key, None)
            try:
                on_done(key, result)
            except Exception:
                logger.exception("metadata on_done raised")

    def shutdown(self) -> None:
        self._executor.shutdown()


# Re-export the small executor used by both artwork and metadata.
from .artwork import _BoundedExecutor  # noqa: E402
