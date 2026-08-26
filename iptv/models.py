"""Data models for the IPTV subsystem.

These are plain dataclasses (no Qt dependency) so they can be used from
worker threads and unit tests without a running GUI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# Content sections (top-level grouping).
SECTION_LIVE = "live"
SECTION_MOVIES = "movies"
SECTION_SERIES = "series"
SECTION_FAVORITES = "favorites"
SECTION_RECENT = "recent"

ALL_SECTIONS = (SECTION_LIVE, SECTION_MOVIES, SECTION_SERIES)


@dataclass
class Channel:
    """A live TV channel."""
    id: str  # stable id (source_id + "::" + tvg-id or name+url hash)
    name: str
    url: str
    logo: str = ""
    tvg_id: str = ""
    tvg_name: str = ""
    group: str = ""
    section: str = SECTION_LIVE
    extra: Dict[str, str] = field(default_factory=dict)
    # Resolved at runtime:
    epg_now: str = ""
    epg_next: str = ""
    favorite: bool = False

    @property
    def display_name(self) -> str:
        return self.tvg_name or self.name


@dataclass
class Episode:
    season: int = 0
    episode: int = 0
    name: str = ""
    url: str = ""
    title: str = ""  # cleaned episode title
    extra: Dict[str, str] = field(default_factory=dict)


@dataclass
class Series:
    id: str
    name: str
    url: str = ""  # series root url (Xtream) or first episode url
    logo: str = ""
    group: str = ""
    section: str = SECTION_SERIES
    extra: Dict[str, str] = field(default_factory=dict)
    episodes: List[Episode] = field(default_factory=list)
    # Metadata (filled by metadata pipeline):
    poster: str = ""
    backdrop: str = ""
    synopsis: str = ""
    year: str = ""
    rating: float = 0.0
    genres: List[str] = field(default_factory=list)
    favorite: bool = False

    @property
    def season_count(self) -> int:
        return len({e.season for e in self.episodes if e.season > 0}) or 1

    def episodes_for(self, season: int) -> List[Episode]:
        return sorted(
            [e for e in self.episodes if e.season == season],
            key=lambda e: e.episode,
        )


@dataclass
class Movie:
    id: str
    name: str
    url: str
    logo: str = ""
    group: str = ""
    section: str = SECTION_MOVIES
    extra: Dict[str, str] = field(default_factory=dict)
    # Metadata:
    poster: str = ""
    backdrop: str = ""
    synopsis: str = ""
    year: str = ""
    rating: float = 0.0
    genres: List[str] = field(default_factory=list)
    favorite: bool = False


@dataclass
class Category:
    """A group-title bucket within a section."""
    name: str
    section: str
    count: int = 0


@dataclass
class PlaylistSource:
    """A saved IPTV source (URL, local file, Xtream login, or media folder)."""
    id: str
    name: str
    kind: str = "m3u_url"  # "m3u_url" | "m3u_file" | "xtream" | "local_folder"
    url: str = ""
    # M3U URL custom headers:
    user_agent: str = ""
    referer: str = ""
    # Xtream:
    username: str = ""
    password: str = ""
    # Management:
    enabled: bool = True
    auto_refresh_minutes: int = 0  # 0 = manual only
    # Optional XMLTV EPG URL — used when the playlist itself doesn't declare
    # one (providers often hand out the EPG link separately).
    epg_url: str = ""
    # Populated after a successful load:
    url_tvg: str = ""  # XMLTV EPG url declared by the playlist

    def has_credentials(self) -> bool:
        return self.kind == "xtream" and bool(self.username and self.password and self.url)


@dataclass
class Playlist:
    """The parsed result of a single source."""
    source_id: str
    channels: List[Channel] = field(default_factory=list)
    movies: List[Movie] = field(default_factory=list)
    series: List[Series] = field(default_factory=list)
    url_tvg: str = ""
    categories: List[Category] = field(default_factory=list)
    # True when the latest refresh failed and this is the previous (cached)
    # copy being kept alive — the GUI shows "refresh failed, cached data"
    # instead of blanking the source. Never serialized to the cache.
    stale: bool = False

    @property
    def total(self) -> int:
        return len(self.channels) + len(self.movies) + len(self.series)


def make_id(source_id: str, *parts: str) -> str:
    """Build a stable, deterministic item id from source + distinguishing parts."""
    import hashlib

    raw = source_id + "|" + "|".join(p for p in parts if p)
    return source_id + "::" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]
