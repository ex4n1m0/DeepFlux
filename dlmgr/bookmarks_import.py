"""Import bookmarks from other browsers.

Chrome/Edge/Brave store bookmarks as a JSON file; Firefox stores them in
a SQLite database (places.sqlite). Both formats are read-only here — the
source browser's data is never modified. Firefox's database is copied to
a temp file first because SQLite locks it while Firefox is running.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)


@dataclass
class BrowserSource:
    """A detected browser bookmark source."""

    browser: str  # display name, e.g. "Chrome"
    kind: str  # "chromium" or "firefox"
    path: str  # Bookmarks JSON file or Firefox profile dir


def detect_browser_sources() -> List[BrowserSource]:
    """Find bookmark stores of installed browsers (best effort, read-only)."""
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    sources: List[BrowserSource] = []

    # Chromium-family: <User Data>/<profile>/Bookmarks (JSON).
    for name, root in (
        ("Chrome", Path(local) / "Google" / "Chrome" / "User Data"),
        ("Edge", Path(local) / "Microsoft" / "Edge" / "User Data"),
        ("Brave", Path(local) / "BraveSoftware" / "Brave-Browser" / "User Data"),
    ):
        if not root.is_dir():
            continue
        profiles = [p for p in root.iterdir() if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile "))]
        for prof in sorted(profiles):
            bookmarks_file = prof / "Bookmarks"
            if bookmarks_file.is_file():
                label = name if prof.name == "Default" else f"{name} ({prof.name})"
                sources.append(BrowserSource(browser=label, kind="chromium", path=str(bookmarks_file)))

    # Firefox: places.sqlite inside each profile.
    ff_profiles = Path(roaming) / "Mozilla" / "Firefox" / "Profiles"
    if ff_profiles.is_dir():
        for prof in sorted(p for p in ff_profiles.iterdir() if p.is_dir()):
            if (prof / "places.sqlite").is_file():
                sources.append(BrowserSource(browser=f"Firefox ({prof.name})", kind="firefox", path=str(prof)))

    return sources


def read_chromium_bookmarks(json_path: str) -> List[Tuple[str, str, str]]:
    """Parse a Chromium-format Bookmarks JSON file into (title, url, folder) triples.

    `folder` is the "/" -separated path of bookmark-folder names (""
    for items at the root of the bookmarks bar / other-bookmarks root).
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out: List[Tuple[str, str, str]] = []

    def walk(node: dict, path: Tuple[str, ...]) -> None:
        if node.get("type") == "url":
            url = node.get("url", "")
            if url.startswith(("http://", "https://")):
                out.append((node.get("name", "") or url, url, "/".join(path)))
            return
        for child in node.get("children", []) or []:
            # Only named folders extend the path; the roots themselves don't.
            child_path = path
            if node.get("type") == "folder" and node.get("name"):
                child_path = path + (node["name"],)
            walk(child, child_path)

    for root in (data.get("roots") or {}).values():
        if isinstance(root, dict):
            for child in root.get("children", []) or []:
                walk(child, ())
    return out


def read_firefox_bookmarks(profile_dir: str) -> List[Tuple[str, str, str]]:
    """Read bookmarks from a Firefox profile's places.sqlite (via a temp copy).

    Folder paths are reconstructed by walking moz_bookmarks parents.
    """
    src = Path(profile_dir) / "places.sqlite"
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        conn = sqlite3.connect(tmp)
        try:
            rows = conn.execute(
                """
                SELECT b.id, b.parent, b.type, b.title, p.url, b.position
                FROM moz_bookmarks b
                LEFT JOIN moz_places p ON p.id = b.fk
                """
            ).fetchall()
        finally:
            conn.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    by_id = {row[0]: row for row in rows}

    def folder_path(parent_id: int) -> str:
        parts = []
        seen = set()
        node = by_id.get(parent_id)
        while node is not None and node[0] not in seen:
            seen.add(node[0])
            # type 2 = folder; parent 0/1 = library roots (not real folders)
            if node[2] == 2 and node[1] > 1 and node[3]:
                parts.append(node[3])
            node = by_id.get(node[1])
        return "/".join(reversed(parts))

    out: List[Tuple[str, str, str]] = []
    for _id, parent, btype, title, url, _pos in rows:
        if btype == 1 and url and url.startswith(("http://", "https://")) and title:
            out.append((title, url, folder_path(parent)))
    return out


def read_bookmarks(source: BrowserSource) -> List[Tuple[str, str, str]]:
    """Read (title, url, folder) triples from the given source."""
    if source.kind == "chromium":
        return read_chromium_bookmarks(source.path)
    return read_firefox_bookmarks(source.path)


def merge_bookmarks(existing: list, imported: List[Tuple[str, str, str]]) -> Tuple[int, int]:
    """Merge imported (title, url, folder) triples into the existing Bookmark list.

    - New URLs are appended (added).
    - Existing bookmarks without a folder adopt the imported folder (updated),
      so re-importing after a flat import organizes them in place.
    - Existing bookmarks that already have a folder are left untouched.

    Returns (added, updated) counts.
    """
    from config import Bookmark

    by_url = {b.url: b for b in existing}
    added = updated = 0
    for title, url, folder in imported:
        bm = by_url.get(url)
        if bm is None:
            existing.append(Bookmark(title=title, url=url, folder=folder))
            by_url[url] = existing[-1]
            added += 1
        elif folder and not bm.folder:
            bm.folder = folder
            updated += 1
    return added, updated
