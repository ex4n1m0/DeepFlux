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

# JAV / Asian adult studio prefixes. When an adult VOD entry's name starts
# with one of these, it's re-grouped into a separate "JAV" sub-category so
# the user can browse JAV content independently from western adult content.
# Only specific JAV studio names are listed here — generic words like
# "asian", "thai", "japanese", "cosplay", "anime", "hentai" are NOT
# included because western adult content also uses those terms (e.g.
# "Asian Obsession", "Cosplay Babes" are western-produced).
_JAV_STUDIO_PREFIXES = (
    "japanhdv", "littleasians", "manko88", "erito",
    "thaigirlswild", "tripforfuck", "heyzo", "caribbean",
    "tokyohot", "1pondo", "pacopacomama", "heydouga", "10musume",
    "javbus", "javlibrary", "fanza", "dmm", "tokyoface",
    "javsand", "javstream", "uncensoredjav",
    "javidol", "javhub",
    "asianstreetmeat", "creampieasian", "thaiswallow",
)

# JAV catalogue code pattern: 2-6 UPPERCASE letters, dash or no separator,
# 2-5 digits. This is the classic JAV disc code format (ABP-123, SSNI-456,
# abp00123). The uppercase requirement prevents false positives on western
# naming patterns like "ph 245" (PornHub series) or "beauty-angels".
# A space separator is only accepted when the letters are uppercase AND
# both parts are 3+ chars (so "ABP 123" matches but "JAV 26" or "PH 245"
# don't — those are dates/abbreviations, not JAV codes).
_JAV_CODE_DASH_RE = re.compile(r"\b[A-Za-z]{2,6}-(\d{2,5})\b")
_JAV_CODE_COMPACT_RE = re.compile(r"\b[a-zA-Z]{2,6}(\d{4,6})\b")
_JAV_CODE_SPACE_RE = re.compile(r"\b([A-Z]{3,6})\s+(\d{3,5})\b")

# Group-title keywords that mark adult content (used to gate the 2-digit
# year extraction — we don't want to misinterpret "Movie 24 01 15" as a
# date in a non-adult group).
_ADULT_GROUP_KEYWORDS = ("xxx", "adult", "porn", "jav", "18+", "erotic")


def _is_adult_group(group: str) -> bool:
    g = (group or "").lower()
    return any(k in g for k in _ADULT_GROUP_KEYWORDS)


def _is_jav_entry(name: str) -> bool:
    """True when an adult VOD entry name looks like JAV/Asian content.

    Checks the studio prefix (first word) against known JAV studios, and
    also detects JAV catalogue codes (ABP-123, SSNI-456, abp00123) in the
    name. The code matching is strict: it requires uppercase letters or a
    dash separator to avoid false positives on western naming patterns
    like "PH 245" (PornHub series) or "Beauty-Angels".
    """
    n = (name or "").strip()
    if not n:
        return False
    n_lower = n.lower()
    # Studio prefix check: first word must exactly match a known JAV studio.
    first_word = re.match(r"([a-z0-9]+)", n_lower)
    if first_word and first_word.group(1) in _JAV_STUDIO_PREFIXES:
        return True
    # JAV catalogue code check. Three patterns, all strict:
    # 1. Dash separator: "ABP-123", "ssni-456" (case-insensitive)
    # 2. Compact form: "abp00123" (letters + 4-6 digits, no separator)
    # 3. Space separator with UPPERCASE letters only: "ABP 123"
    #    (prevents "some 123" from matching)
    if _JAV_CODE_DASH_RE.search(n):
        # Exclude year-like patterns: "2023-01" is a date, not a code.
        m = _JAV_CODE_DASH_RE.search(n)
        digits = m.group(1)
        if not (19 <= int(digits) <= 49 or int(digits) in (20, 21, 22, 23, 24, 25, 26)):
            return True
        # If the letters part is a real JAV prefix (not a date fragment),
        # it's a JAV code.
        prefix = n[m.start():m.start() + m.end() - m.start() - len(digits) - 1]
        if len(prefix) >= 2 and not prefix.isdigit():
            return True
    if _JAV_CODE_COMPACT_RE.search(n):
        return True
    if _JAV_CODE_SPACE_RE.search(n):
        return True
    return False


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

    JAV/Asian adult entries are also re-grouped into a "JAV" sub-category
    so they can be browsed separately from western adult content."""
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

    # Separate JAV content into its own sub-category. The group field is
    # what the sidebar uses to build categories, so renaming "XXX VOD" to
    # "XXX VOD JAV" for JAV entries makes them appear as a separate folder.
    for it in playlist.movies:
        g = (it.group or "").lower()
        if _is_adult_group(g) and _is_jav_entry(it.name):
            # Preserve the original group prefix (e.g. "XXX VOD") and append
            # "JAV" so the sidebar shows it as a sub-folder of adult content.
            base = it.group or "XXX VOD"
            if "jav" not in base.lower():
                it.group = base + " JAV"

    # Rebuild categories to include the new JAV group.
    _rebuild_categories(playlist)
    return playlist


def _rebuild_categories(playlist: Playlist) -> None:
    """Rebuild the playlist's category list from current item groups.

    Called after populate_years re-groups JAV entries — the original
    categories were built from the raw M3U group-titles, so they don't
    include the synthetic "XXX VOD JAV" group."""
    from .models import Category, SECTION_MOVIES, SECTION_SERIES, SECTION_LIVE

    # Collect unique groups per section.
    seen: dict = {}  # (section, group) -> count
    for it in playlist.movies:
        key = (SECTION_MOVIES, it.group or "")
        seen[key] = seen.get(key, 0) + 1
    for it in playlist.series:
        key = (SECTION_SERIES, it.group or "")
        seen[key] = seen.get(key, 0) + 1
    for ch in playlist.channels:
        key = (SECTION_LIVE, ch.group or "")
        seen[key] = seen.get(key, 0) + 1

    # Preserve existing category order, append new ones at the end.
    existing = {(c.section, c.name) for c in playlist.categories}
    for cat in list(playlist.categories):
        # Update counts for existing categories.
        key = (cat.section, cat.name)
        if key in seen:
            cat.count = seen[key]
    # Add new categories that didn't exist before (e.g. "XXX VOD JAV").
    for (section, group), count in seen.items():
        if (section, group) not in existing and group:
            playlist.categories.append(Category(
                name=group, section=section, count=count,
            ))



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
