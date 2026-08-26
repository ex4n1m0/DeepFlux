"""Local media folder sources — scan a directory tree into a Playlist.

A ``local_folder`` source (``url`` = directory path, e.g. a Plex library
root) turns the video files on disk into first-class Movies/Series entries:
same sidebar tree, same poster pipeline (TMDb/TVmaze via
:class:`iptv.metadata.MetadataPipeline` with the frame-grab fallback), same
favorites/recent handling as M3U/Xtream sources. Playback is just the file
path through mpv/VLC, and scanned playlists are cached in SQLite like any
other source, so startup stays instant and a refresh is a cheap re-scan.

Layout handling (measured against a real Plex-style library):
- loose files (``MOVIES/Dunki.2023.../x.mkv``) -> category = relative subdir;
- movie-per-folder (``MOVIES/Title (2020)/Title (2020).mkv``) -> the folder
  named after its own file is collapsed so the category stays ``MOVIES``;
- season folders (``TV SERIES/Show.S01.720p-GRP/`` or ``Show/Season 01/``)
  -> category = the show/library dir above them, episodes grouped by the
  SxxEyy marker in the filename;
- nameless episode files (``S01E01.mkv``) borrow the show's folder name so
  all episodes still collapse into one Series;
- non-video sidecars (.srt/.nfo/.jpg, ``Screens/`` dirs) never match the
  video extension filter, and ``sample`` files are skipped.

The scanner emits Channels carrying ``extra["local_section"]``; classify()
honours that flag because the usual URL/extension heuristics misfire on
local paths (a local ``.m2ts`` is a movie, not a live stream).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Callable, Optional

from .models import (
    SECTION_MOVIES,
    SECTION_SERIES,
    Channel,
    Playlist,
    PlaylistSource,
    make_id,
)

logger = logging.getLogger(__name__)

# Containers mpv/VLC decode. Only these are picked up — sidecars (.srt/.nfo/
# .jpg) and everything else are ignored by construction.
VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".3gp", ".ogv",
}

# Episode marker in a file name ("Show.S01E02...", "show s1e2").
_SE_MARKER_RE = re.compile(r"[Ss]\d{1,2}\s?[Ee]\d{1,3}")

# A directory that IS a season: "Season 01", "Show.S01.720p-GRP [NO RAR]",
# "Show.S01E01...". The marker must be separator-delimited so "S.W.A.T" or
# "Toy Story 5" never match.
_SEASON_DIR_RE = re.compile(
    r"(?:^|[\s._\-])(?:season\s*\d{1,2}|s\d{1,2}(?:\s?e\d{1,3})?)(?:$|[\s._\-])",
    re.IGNORECASE,
)

# Release samples carry no content worth a tile.
_SAMPLE_RE = re.compile(r"(?:^|[\s._\-])sample(?:$|[\s._\-])", re.IGNORECASE)

_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _alnum(s: str) -> str:
    return _ALNUM_RE.sub("", (s or "").lower())


_YEAR_CORE_RE = re.compile(r"(?:19|20)\d{2}")


def _same_named_dir(dir_name: str, stem: str) -> bool:
    """True when a folder is named after the file it holds.

    Exact match covers "Title (2020)/Title (2020).mkv". The core check covers
    release-renames like "Toy Story 5 (2026) 2160p 4K WEB 5.1-LAMA/" holding
    "Toy.Story.5.2026.2160p.4K.WEB.x265.10bit.AAC5.1-LAMA.mkv": the cleaned
    (year-less) folder name prefixes the cleaned file name and both end in
    the same release group. The last-token rule keeps legit subcategories
    ("Comedy/" holding "Comedy Central Roast 2024.mkv") from collapsing."""
    if _alnum(dir_name) == _alnum(stem):
        return True
    from .metadata import clean_title  # local import to avoid cycle

    def _core(s: str) -> str:
        return _alnum(_YEAR_CORE_RE.sub("", clean_title(s)))

    core_dir = _core(dir_name)
    if len(core_dir) < 4 or not _core(stem).startswith(core_dir):
        return False
    d_toks, s_toks = _TOKEN_RE.findall(dir_name.lower()), _TOKEN_RE.findall(stem.lower())
    return bool(d_toks and s_toks) and d_toks[-1] == s_toks[-1]


def _is_season_dir(name: str) -> bool:
    return bool(_SEASON_DIR_RE.search(name or ""))


def scan_folder(
    source: PlaylistSource,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> Playlist:
    """Walk ``source.url`` and build a playlist of its video files.

    Blocking; call from a worker thread. A missing/unreadable folder is not
    an error — it yields an empty playlist so the GUI shows "0 entries"
    instead of a failure state."""
    pl = Playlist(source_id=source.id)
    root = (source.url or "").strip()
    if not root or not os.path.isdir(root):
        logger.warning("local folder source %r: not a directory: %r",
                       source.name, root)
        return pl

    found = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if is_cancelled and is_cancelled():
            break
        dirnames.sort()
        for fname in sorted(filenames):
            stem, ext = os.path.splitext(fname)
            if ext.lower() not in VIDEO_EXTS:
                continue
            if _SAMPLE_RE.search(stem):
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(dirpath, root)
            if rel == ".":
                rel = ""
            is_episode = bool(_SE_MARKER_RE.search(stem)) or _is_season_dir(
                os.path.basename(dirpath))
            group = _group_for(rel, dirpath, stem, is_episode)
            name = _display_name(stem, dirpath, root)
            pl.channels.append(Channel(
                id=make_id(source.id, os.path.relpath(path, root)),
                name=name,
                url=path,
                group=group,
                extra={
                    "local_file": "1",
                    "local_section": SECTION_SERIES if is_episode else SECTION_MOVIES,
                },
            ))
            found += 1
            if on_progress and found % 200 == 0:
                on_progress(found, None)
    if on_progress:
        on_progress(found, None)
    logger.info("local folder %r: %d video files", source.name, found)
    return pl


def _group_for(rel: str, dirpath: str, stem: str, is_episode: bool) -> str:
    """Pick the tree category (Channel.group) for one file.

    Season dirs collapse to their parent (one category per show, not per
    season); a movie's own folder (dir named like the file) collapses the
    same way so flat libraries keep a single "MOVIES"-style category."""
    if not rel:
        return ""
    parts = rel.split(os.sep)
    if is_episode and _is_season_dir(parts[-1]):
        parts = parts[:-1]
    elif not is_episode and _same_named_dir(parts[-1], stem):
        parts = parts[:-1]
    return os.sep.join(parts)


def _display_name(stem: str, dirpath: str, root: str) -> str:
    """File stem, or "<show folder> <SxxEyy>" for nameless episode files.

    "S01E01.mkv" alone would give every episode its own series key, so the
    title is borrowed from the nearest ancestor dir that isn't a season
    folder (and isn't the source root itself)."""
    m = _SE_MARKER_RE.search(stem)
    if not m or len(_alnum(stem[:m.start()] + stem[m.end():])) >= 3:
        return stem
    show_dir = dirpath
    while show_dir != root and _is_season_dir(os.path.basename(show_dir)):
        show_dir = os.path.dirname(show_dir)
    if show_dir == root:
        return stem
    show = os.path.basename(show_dir)
    return f"{show} {m.group(0)}" if show else stem
