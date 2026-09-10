"""Content classification: Live TV / Movies / Series.

Rules are deliberately data-driven and tunable via :data:`CLASSIFICATION_RULES`
so they can be adjusted without touching parsing logic. An entry is classified
by the first matching rule (checked in order: series, movies, then live as the
fallback).

Classification signals:
- ``group_keywords`` — substrings matched (case-insensitive) against group-title.
- ``url_patterns``  — substrings matched against the stream URL (Xtream-style
  ``/movie/``, ``/series/`` paths, or file extensions).
- ``extensions``     — file extensions implying VOD (movie/series) vs. live.
"""
from __future__ import annotations

import logging
import os
import re
from typing import List

from .m3u_parser import parse_season_episode
from .models import (
    SECTION_LIVE,
    SECTION_MOVIES,
    SECTION_SERIES,
    Channel,
    Episode,
    Movie,
    Playlist,
    Series,
    make_id,
)

logger = logging.getLogger(__name__)


# A rule maps a section to the signals that imply it.
CLASSIFICATION_RULES = {
    SECTION_SERIES: {
        "group_keywords": [
            "series", "tv show", "tv shows", "shows", "seriya", "dizi",
            "serial", "serials", "soap",
        ],
        "url_patterns": ["/series/", "/tv/", "series/"],
    },
    SECTION_MOVIES: {
        "group_keywords": [
            "movie", "movies", "film", "films", "cinema", "vod", "vodu",
            "pelicula", "peliculas",
        ],
        "url_patterns": ["/movie/", "movie/", ".mkv", ".mp4", ".avi", ".mov"],
        "extensions": [".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v"],
    },
}

# Extensions that strongly imply a *live* stream (no file extension, or .ts/.m3u8).
LIVE_EXTENSIONS = {".ts", ".m3u8", ".m3u"}

# Live-channel naming patterns: "UK: Sky Cinema FHD", "US|ESPN HD" — a country
# prefix and no release year. Providers often file 24/7 movie *channels* under
# groups like "UK Movies", so group keywords alone misclassify them as VOD.
_LIVE_NAME_PREFIX = re.compile(r"^[A-Z]{2,4}[:|]\s*")
_NAME_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

# Adult VOD date pattern: "<Studio> YY MM DD <Title>" — providers like
# soursignal use 2-digit years in adult VOD names (e.g. "Tushy 26 08 09
# Kubera Fortuna" = Aug 9, 2026). The 4-digit _YEAR_RE above misses these,
# so every adult VOD entry lands in the "Others" year bucket. This pattern
# is only applied to entries in adult groups (XXX/XXX VOD) to avoid
# misinterpreting 2-digit numbers in regular movie names.
_ADULT_VOD_DATE_RE = re.compile(r"\b(\d{2})\s+(\d{2})\s+(\d{2})\b")

# Group-title keywords that mark adult content (used to gate the 2-digit
# year extraction — we don't want to misinterpret "Movie 24 01 15" as a
# date in a non-adult group).
_ADULT_GROUP_KEYWORDS = ("xxx", "adult", "porn", "jav", "18+", "erotic")


def _is_adult_group(group: str) -> bool:
    g = (group or "").lower()
    return any(k in g for k in _ADULT_GROUP_KEYWORDS)


def _extract_adult_vod_year(name: str) -> str:
    """Extract a 4-digit year from an adult VOD name with YY MM DD dates.

    "Tushy 26 08 09 Kubera Fortuna" -> "2026"
    "SexMex 22 09 25 Camila" -> "2022"
    Returns "" when no date pattern is found.
    """
    m = _ADULT_VOD_DATE_RE.search(name or "")
    if not m:
        return ""
    yy = int(m.group(1))
    mm = int(m.group(2))
    dd = int(m.group(3))
    # Validate it's a real date (not just 3 random 2-digit numbers).
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return ""
    # 2-digit year to 4-digit: 00-49 -> 2000s, 50-99 -> 1900s.
    full_year = 2000 + yy if yy < 50 else 1900 + yy
    return str(full_year)


def _looks_like_live_channel(name: str) -> bool:
    """Live-channel naming: country-prefixed and no release year."""
    n = name or ""
    return bool(_LIVE_NAME_PREFIX.match(n)) and not _NAME_YEAR_RE.search(n)


def _lower(s: str) -> str:
    return (s or "").lower()


def classify_entry(group: str, url: str, name: str) -> str:
    """Return the section for a single entry."""
    g = _lower(group)
    u = _lower(url)
    ext = os.path.splitext(u)[1]

    # Series first: a name with SxxExx is a strong series signal regardless of group.
    if re.search(r"[Ss]\d{1,2}\s?[Ee]\d{1,3}", name or ""):
        if any(p in u for p in CLASSIFICATION_RULES[SECTION_SERIES]["url_patterns"]) or \
           any(k in g for k in CLASSIFICATION_RULES[SECTION_SERIES]["group_keywords"]) or \
           ext not in LIVE_EXTENSIONS:
            return SECTION_SERIES

    # Live-channel naming ("UK: Sky Cinema FHD") beats group keywords — e.g.
    # groups like "UK Movies" contain 24/7 channels, not VOD.
    if _looks_like_live_channel(name):
        return SECTION_LIVE

    for section in (SECTION_SERIES, SECTION_MOVIES):
        rules = CLASSIFICATION_RULES[section]
        if any(k in g for k in rules.get("group_keywords", [])):
            return section
        if any(p in u for p in rules.get("url_patterns", [])):
            return section
        if ext and ext in rules.get("extensions", []):
            return section

    return SECTION_LIVE


def classify(playlist: Playlist) -> Playlist:
    """Reorganize a parsed playlist into channels/movies/series in place.

    The parser stores every entry as a :class:`Channel`; this pass promotes
    movies and series into their own collections and groups series episodes.
    Returns the same playlist for convenience.
    """
    if not playlist.channels:
        return playlist

    series_index: dict[str, Series] = {}
    leftover_channels: List[Channel] = []

    for ch in playlist.channels:
        # local_folder sources pre-classify: the URL/extension heuristics
        # misfire on filesystem paths (a local .m2ts is a movie, not live).
        forced = (ch.extra or {}).get("local_section")
        if forced in (SECTION_MOVIES, SECTION_SERIES):
            section = forced
        else:
            section = classify_entry(ch.group, ch.url, ch.name)
        if section == SECTION_MOVIES:
            playlist.movies.append(_channel_to_movie(ch))
        elif section == SECTION_SERIES:
            _add_series_episode(series_index, ch)
        else:
            ch.section = SECTION_LIVE
            leftover_channels.append(ch)

    playlist.channels = leftover_channels
    playlist.series = list(series_index.values())
    return playlist


def _channel_to_movie(ch: Channel) -> Movie:
    return Movie(
        id=ch.id,
        name=ch.name,
        url=ch.url,
        logo=ch.logo,
        group=ch.group,
        section=SECTION_MOVIES,
        extra=ch.extra,
    )


def populate_years(playlist: Playlist) -> Playlist:
    """Fill in Movie/Series ``year`` from entry names when still empty.

    A cheap regex pass over the classified collections: scene-style VOD names
    carry the release year ("Title.2023.1080p", "Title (2023)") — measured
    ~100% of movies on real playlists, a third or so of series. The year
    powers the sidebar's group-by-year view. Xtream series arrive with
    ``releaseDate`` already mapped and keep it; names without a year stay
    empty and land in the sidebar's "Others" bucket.

    Adult VOD entries use a different naming convention: "<Studio> YY MM DD
    <Title>" (e.g. "Tushy 26 08 09 Kubera Fortuna"). The standard 4-digit
    year regex misses these, so a dedicated 2-digit date extractor is used
    for entries in adult groups — without it, all adult VOD lands in
    "Others" regardless of release date.

    All adult content shares ONE folder structure (user decision
    2026-09-10): JAV/Asian entries are NOT re-grouped into a separate
    sub-category any more — the old "<group> JAV" synthetic split was
    removed because it scattered the adult library."""
    from .metadata import extract_year  # local import to avoid cycle

    for it in playlist.movies + playlist.series:
        if it.year:
            continue
        # Adult VOD entries use YY MM DD dates — try the adult extractor
        # first for adult groups, then fall back to the standard 4-digit
        # extractor for any adult entries that happen to use 4-digit years.
        if _is_adult_group(it.group or ""):
            it.year = _extract_adult_vod_year(it.name)
            if not it.year:
                it.year = extract_year(it.name)
        else:
            it.year = extract_year(it.name)

    return playlist



def _series_key(name: str) -> str:
    """Normalize a title into a series grouping key.

    Strips season/episode markers and quality tags so all episodes of one show
    collapse into a single :class:`Series`.
    """
    from .metadata import clean_title  # local import to avoid cycle

    base = clean_title(name)
    base = re.sub(r"[Ss]\d{1,2}\s?[Ee]\d{1,3}", "", base)
    base = re.sub(r"[Ss]\d{1,2}\b", "", base)
    base = re.sub(r"\bseason\s*\d+\b", "", base, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", base).strip().lower() or name.strip().lower()


def _add_series_episode(series_index: dict[str, Series], ch: Channel) -> None:
    key = _series_key(ch.name)
    series = series_index.get(key)
    if series is None:
        sid = make_id(ch.id.rsplit("::", 1)[0] if "::" in ch.id else ch.id, "series", key)
        series = Series(
            id=sid,
            name=_series_display_name(ch.name),
            url=ch.url,
            logo=ch.logo,
            group=ch.group,
            section=SECTION_SERIES,
            extra=ch.extra,
        )
        series_index[key] = series
    season, episode = parse_season_episode(ch.name)
    series.episodes.append(
        Episode(
            season=season or 1,
            episode=episode,
            name=ch.name,
            url=ch.url,
            title=ch.name,
            extra=ch.extra,
        )
    )


def _series_display_name(name: str) -> str:
    """Best-effort clean series title for display."""
    from .metadata import clean_title

    base = clean_title(name)
    base = re.sub(r"[Ss]\d{1,2}\s?[Ee]\d{1,3}.*$", "", base)
    base = re.sub(r"[Ss]\d{1,2}\b.*$", "", base)
    base = re.sub(r"\bseason\s*\d+.*$", "", base, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", base).strip() or name.strip()
