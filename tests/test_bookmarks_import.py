"""Tests for dlmgr.bookmarks_import: Chromium/Firefox parsing + merge logic."""
import json
import sqlite3

from dlmgr.bookmarks_import import (
    BrowserSource,
    merge_bookmarks,
    read_bookmarks,
    read_chromium_bookmarks,
    read_firefox_bookmarks,
)


def _chromium_json() -> dict:
    return {
        "roots": {
            "bookmark_bar": {
                "type": "folder",
                "name": "Bookmarks bar",
                "children": [
                    {"type": "url", "name": "Example", "url": "https://example.com/"},
                    {
                        "type": "folder",
                        "name": "News",
                        "children": [
                            {"type": "url", "name": "CNN", "url": "https://cnn.com/"},
                            {
                                "type": "folder",
                                "name": "Tech",
                                "children": [
                                    {"type": "url", "name": "Ars", "url": "https://arstechnica.com/"},
                                ],
                            },
                        ],
                    },
                ],
            },
            "other": {
                "type": "folder",
                "name": "Other bookmarks",
                "children": [
                    {"type": "url", "name": "FTP ignored", "url": "ftp://files.example.com/"},
                    {"type": "url", "name": "Other", "url": "https://other.example.com/"},
                ],
            },
        }
    }


def test_chromium_parse_preserves_folder_paths(tmp_path):
    f = tmp_path / "Bookmarks"
    f.write_text(json.dumps(_chromium_json()), encoding="utf-8")

    result = read_chromium_bookmarks(str(f))

    assert ("Example", "https://example.com/", "") in result
    assert ("CNN", "https://cnn.com/", "News") in result
    assert ("Ars", "https://arstechnica.com/", "News/Tech") in result
    assert ("Other", "https://other.example.com/", "") in result
    # Non-http(s) schemes are skipped.
    assert all(not url.startswith("ftp:") for _t, url, _f in result)


def test_read_bookmarks_dispatches_chromium(tmp_path):
    f = tmp_path / "Bookmarks"
    f.write_text(json.dumps(_chromium_json()), encoding="utf-8")
    src = BrowserSource(browser="Chrome", kind="chromium", path=str(f))
    assert len(read_bookmarks(src)) == 4


def test_firefox_parse_preserves_folder_paths(tmp_path):
    profile = tmp_path / "ffprofile"
    profile.mkdir()
    db = profile / "places.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT)")
    conn.execute(
        "CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, parent INTEGER, type INTEGER, title TEXT, fk INTEGER, position INTEGER)"
    )
    # Roots: 1 = places root, 2 = bookmarks menu folder.
    conn.execute("INSERT INTO moz_bookmarks VALUES (1, 0, 2, '', NULL, 0)")
    conn.execute("INSERT INTO moz_bookmarks VALUES (2, 1, 2, 'menu', NULL, 0)")
    conn.execute("INSERT INTO moz_bookmarks VALUES (3, 2, 2, 'Videos', NULL, 0)")
    conn.execute("INSERT INTO moz_places VALUES (10, 'https://video.example.com/')")
    conn.execute("INSERT INTO moz_places VALUES (11, 'https://top.example.com/')")
    conn.execute("INSERT INTO moz_bookmarks VALUES (4, 3, 1, 'Video Site', 10, 0)")
    conn.execute("INSERT INTO moz_bookmarks VALUES (5, 2, 1, 'Top Site', 11, 1)")
    conn.commit()
    conn.close()

    result = read_firefox_bookmarks(str(profile))

    assert ("Video Site", "https://video.example.com/", "Videos") in result
    assert ("Top Site", "https://top.example.com/", "") in result
    assert len(result) == 2


def test_merge_adds_new_and_adopts_folders():
    from config import Bookmark

    existing = [
        Bookmark(title="CNN", url="https://cnn.com/"),  # flat from a previous import
        Bookmark(title="Mine", url="https://mine.example.com/", folder="Keep"),
    ]
    imported = [
        ("CNN", "https://cnn.com/", "News"),           # existing, gains folder
        ("Mine", "https://mine.example.com/", "Other"),  # existing folder wins
        ("New", "https://new.example.com/", "News"),    # brand new
    ]

    added, updated = merge_bookmarks(existing, imported)

    assert (added, updated) == (1, 1)
    assert existing[0].folder == "News"
    assert existing[1].folder == "Keep"
    assert existing[2].url == "https://new.example.com/"
    assert existing[2].folder == "News"
    assert len(existing) == 3
