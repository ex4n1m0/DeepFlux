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
    empty and land in the sidebar's "Others" bucket."""
    from .metadata import extract_year  # local import to avoid cycle

    for it in playlist.movies + playlist.series:
        if not it.year:
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
