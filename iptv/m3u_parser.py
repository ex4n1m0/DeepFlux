"""Robust M3U / M3U8 playlist parser.

Design goals:
- Tolerate malformed lines, missing attributes, BOM, CRLF, blank lines.
- Stream line-by-line so 50k+ entry playlists parse without huge memory spikes.
- Callable from a worker thread; emits progress via a callback so the UI can
  show a progress bar and the work is cancellable.
- Extract tvg-id, tvg-name, tvg-logo, group-title, url-tvg, and arbitrary
  extra attributes from the ``#EXTINF`` line.

The parser returns a :class:`iptv.models.Playlist` whose items are *not yet*
classified into Live/Movies/Series — classification is performed separately by
:mod:`iptv.classify` so the rules stay tunable and independent of parsing.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Callable, Iterable, List, Optional, Tuple

from .models import (
    SECTION_LIVE,
    Channel,
    Episode,
    Movie,
    Playlist,
    Series,
    make_id,
)

logger = logging.getLogger(__name__)

# Progress callback: (parsed_count, total_lines_or_None) -> None
ProgressFn = Callable[[int, Optional[int]], None]


# Matches key="value" pairs inside an #EXTINF (and #EXTGRP) line.
_ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
# The trailing display name is the text after the last comma on an #EXTINF line.
_EXTINF_NAME_RE = re.compile(r"^#EXTINF:[^,]*,(.*)$", re.DOTALL)


def _parse_attrs(line: str) -> Tuple[dict, str]:
    """Split an #EXTINF line into (attributes, display_name).

    Quoted attributes are removed before looking for the name separator so a
    comma *inside* a quoted value (e.g. tvg-name="Foo, Bar") isn't mistaken
    for the start of the display name."""
    attrs = dict(_ATTR_RE.findall(line))
    stripped = _ATTR_RE.sub("", line.strip())
    m = _EXTINF_NAME_RE.match(stripped)
    name = m.group(1).strip() if m else ""
    return attrs, name


def _iter_lines(path: str, encoding: str = "utf-8") -> Iterable[str]:
    """Yield stripped lines from a file, tolerating BOM and bad bytes."""
    with open(path, "r", encoding=encoding, errors="replace") as f:
        for raw in f:
            yield raw.rstrip("\r\n")


def _iter_lines_text(text: str) -> Iterable[str]:
    for raw in text.splitlines():
        yield raw


def looks_like_m3u(text: str) -> bool:
    """Cheap payload sanity check for downloaded playlists.

    M3U content starts with ``#EXTM3U`` or a stream URL — never with ``<``,
    which is what HTML error pages and XMLTV EPG endpoints return (a common
    mixup: providers hand out the EPG link right next to the playlist link).
    Parsing such a payload line-by-line would create thousands of garbage
    entries and poison the on-disk cache.
    """
    head = text.lstrip("\ufeff \t\r\n")[:1]
    return bool(head) and head != "<"


def parse_m3u(
    source_id: str,
    text: Optional[str] = None,
    path: Optional[str] = None,
    encoding: str = "utf-8",
    on_progress: Optional[ProgressFn] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> Playlist:
    """Parse an M3U/M3U8 playlist.

    Pass exactly one of ``text`` or ``path``. ``on_progress`` is called
    periodically with the number of entries parsed so far (and the total line
    count when known). ``is_cancelled`` returning True aborts early.
    """
    if text is None and path is None:
        raise ValueError("parse_m3u requires either text or path")

    lines_iter: Iterable[str]
    total: Optional[int] = None
    if path is not None:
        # Two passes: first count lines for progress, then parse. Counting a
        # 50k-line file is cheap and lets us show a real percentage.
        try:
            with open(path, "rb") as fb:
                total = sum(1 for _ in fb)
        except OSError:
            total = None
        lines_iter = _iter_lines(path, encoding)
    else:
        assert text is not None
        lines_iter = _iter_lines_text(text)

    playlist = Playlist(source_id=source_id)
    current_attrs: Optional[dict] = None
    current_name: str = ""
    pending_group: str = ""
    count = 0

    for line in lines_iter:
        if is_cancelled and is_cancelled():
            logger.info("M3U parse cancelled for source %s", source_id)
            break

        stripped = line.strip()
        if not stripped:
            continue

        # #EXTM3U header — may declare an EPG url.
        if stripped.upper().startswith("#EXTM3U"):
            attrs, _ = _parse_attrs(stripped)
            for k in ("url-tvg", "tvg-url", "x-tvg-url"):
                if attrs.get(k):
                    playlist.url_tvg = attrs[k]
                    break
            continue

        if stripped.upper().startswith("#EXTINF"):
            current_attrs, current_name = _parse_attrs(stripped)
            # A pending #EXTGRP (seen before this #EXTINF) applies here too.
            if pending_group and current_attrs is not None and not current_attrs.get("group-title"):
                current_attrs["group-title"] = pending_group
                pending_group = ""
            continue

        if stripped.upper().startswith("#EXTGRP"):
            # #EXTGRP:<group> is an alternate way to declare the group. It can
            # appear before an #EXTINF or between #EXTINF and the URL.
            grp = stripped.split(":", 1)[-1].strip()
            if current_attrs is not None and not current_attrs.get("group-title"):
                current_attrs["group-title"] = grp
            else:
                pending_group = grp
            continue

        # Skip other directives (#EXTVLCOPT, #EXT-X-..., comments).
        if stripped.startswith("#"):
            # EXTVLCOPT can carry http headers; keep them in extras.
            if current_attrs is not None and stripped.upper().startswith("#EXTVLCOPT"):
                opt = stripped.split(":", 1)[-1].strip()
                if "=" in opt:
                    k, v = opt.split("=", 1)
                    current_attrs.setdefault("extvlcopt", []).append(f"{k.strip()}={v.strip()}")
            continue

        # A non-comment line following an #EXTINF is the stream URL.
        url = stripped
        if current_attrs is None:
            # URL with no preceding #EXTINF — synthesize a minimal entry.
            current_attrs = {}
            current_name = os.path.basename(url) or url
            if pending_group:
                current_attrs["group-title"] = pending_group
                pending_group = ""

        _build_item(playlist, source_id, current_attrs, current_name, url)
        count += 1
        current_attrs = None
        current_name = ""
        pending_group = ""

        if on_progress and count % 500 == 0:
            on_progress(count, total)

    if on_progress:
        on_progress(count, total)

    return playlist


def _build_item(
    playlist: Playlist,
    source_id: str,
    attrs: dict,
    name: str,
    url: str,
) -> None:
    """Append a parsed entry to the playlist.

    Items are stored as :class:`Channel` by default; the classifier in
    :mod:`iptv.classify` later promotes movies/series and reorganizes them.
    Keeping everything as channels first means a single, simple parse pass.
    """
    tvg_id = attrs.get("tvg-id", "")
    tvg_name = attrs.get("tvg-name", "")
    logo = attrs.get("tvg-logo", "")
    group = attrs.get("group-title", "")
    extra = {k: v for k, v in attrs.items() if k not in ("tvg-id", "tvg-name", "tvg-logo", "group-title")}

    item_id = make_id(source_id, tvg_id or name, url)
    channel = Channel(
        id=item_id,
        name=name,
        url=url,
        logo=logo,
        tvg_id=tvg_id,
        tvg_name=tvg_name,
        group=group,
        section=SECTION_LIVE,
        extra=extra,
    )
    playlist.channels.append(channel)


# ---------------------------------------------------------------------------
# Series season/episode parsing from names (e.g. "Show Name S01E02 ...")
# ---------------------------------------------------------------------------

_SEASON_EP_RE = re.compile(r"[Ss](\d{1,2})\s?[Ee](\d{1,3})")
_SEASON_RE = re.compile(r"[Ss](\d{1,2})\b")


def parse_season_episode(name: str) -> Tuple[int, int]:
    """Return (season, episode) parsed from a filename/title, (0,0) if unknown."""
    m = _SEASON_EP_RE.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _SEASON_RE.search(name)
    if m:
        return int(m.group(1)), 0
    return 0, 0
