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
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

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

    def fetch(self, title: str, year: str, section: str) -> Optional[Dict[str, Any]]:
        if not self.api_key or not title:
            return None
        kind = "tv" if section == "series" else "movie"
        try:
            params = {"api_key": self.api_key, "query": title, "language": "en-US"}
            if year:
                params["year" if kind == "movie" else "first_air_date_year"] = year
            r = requests.get(f"{self.BASE}/search/{kind}", params=params, timeout=15)
            r.raise_for_status()
            results = r.json().get("results", [])
            if not results:
                return None
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
            }
            # Fetch genres via detail endpoint.
            detail_id = res.get("id")
            if detail_id:
                d = requests.get(
                    f"{self.BASE}/{kind}/{detail_id}",
                    params={"api_key": self.api_key, "language": "en-US"},
                    timeout=15,
                )
                if d.ok:
                    dj = d.json()
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

    def fetch(self, title: str, year: str, section: str) -> Optional[Dict[str, Any]]:
        if not title or section != "series":
            return None
        try:
            r = requests.get(f"{self.BASE}/singlesearch/shows", params={"q": title}, timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            show = r.json()
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


class TPDBProvider(MetadataProvider):
    """ThePornDB — metadata + posters for adult VOD.

    Needs a user-supplied bearer token (theporndb.net); every endpoint 401s
    without one. ``/movies``, ``/scenes`` and ``/jav`` all return the same
    shape — ``{"data": [row, ...]}`` — where a row carries ``title``,
    ``date`` (YYYY-MM-DD), ``description``, ``tags[].name`` and the
    ``posters`` / ``background`` size dicts.

    The API resets connections on rapid sequential requests, so calls go
    through a single keep-alive :class:`requests.Session` with one retry —
    the same lesson the channel-logo fetcher learned in :mod:`iptv.artwork`.
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
        for attempt in range(2):
            try:
                r = self._session.get(f"{self.BASE}{path}", params=params, timeout=20)
                if r.status_code == 401:
                    logger.warning("TPDB rejected the API token (401)")
                    return []
                r.raise_for_status()
                return r.json().get("data") or []
            except requests.ConnectionError:
                if attempt == 0:
                    time.sleep(1.0)  # reset under rapid-fire requests
                    continue
                logger.debug("TPDB connection reset for %r", params)
            except Exception as exc:
                logger.debug("TPDB %s failed for %r: %s", path, params, exc)
                return []
        return []

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
    and get routed to the /jav endpoint.
    """
    for m in _JAV_CODE_RE.finditer(name or ""):
        letters, sep, digits = m.group(1), m.group(2), m.group(3)
        if _YEAR_ONLY_RE.fullmatch(digits):
            continue  # a release year, not a disc number
        if sep.isspace() and not letters.isupper():
            continue  # "Some 123" only counts in the uppercase form codes use
        return f"{letters.upper()}-{digits.lstrip('0') or '0'}"
    return ""


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
    """

    def __init__(
        self,
        cache: IPTVCache,
        tmdb_api_key: str = "",
        rate_per_second: float = 5.0,
        tpdb_api_key: str = "",
    ) -> None:
        self.cache = cache
        self.tmdb = TMDBProvider(tmdb_api_key) if tmdb_api_key else None
        self.tvmaze = TVMazeProvider()
        self.tpdb = TPDBProvider(tpdb_api_key) if tpdb_api_key else None
        self.logos = IptvOrgLogos(cache)
        self._limiter = _RateLimiter(rate=rate_per_second, per=1.0)
        self._executor = _BoundedExecutor(max_workers=4)
        self._inflight: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def set_tmdb_key(self, key: str) -> None:
        self.tmdb = TMDBProvider(key) if key else None

    def set_tpdb_key(self, key: str) -> None:
        self.tpdb = TPDBProvider(key) if key else None

    def channel_logo_fallback(self, name: str) -> str:
        """Synchronous iptv-org logo lookup (used when tvg-logo is missing)."""
        return self.logos.lookup(name)

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
                # Negative results are cached briefly so titles with no match
                # aren't re-queried on every visit.
                if time.time() - cached.get("updated_at", 0) < 6 * 3600:
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
            # Adult VOD goes to TPDB first: TMDb filters adult titles out of
            # its search results entirely, so it can only ever miss on these.
            if self.tpdb and looks_adult(group, name):
                res = self.tpdb.fetch(adult_clean_title(name), year, section, raw_name=name)
                if res:
                    result = res
            # Try TMDb (if configured).
            if not result and self.tmdb:
                res = self.tmdb.fetch(query, year, section)
                if res:
                    result = res
            # TVmaze fallback for series.
            if not result and section == "series":
                res = self.tvmaze.fetch(query, year, section)
                if res:
                    result = res
            if result:
                self.cache.save_metadata(key, section, title, year, result.get("provider", ""), result)
            else:
                # Cache the negative result (with a timestamp for TTL) so
                # unmatched titles don't trigger repeated API calls.
                self.cache.save_metadata(key, section, title, year, "none",
                                         {"negative": True, "updated_at": time.time()})
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
