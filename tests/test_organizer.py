"""Tests for the local metadata organizer."""
from __future__ import annotations

from agent.organizer import Organizer


def test_detect_category_movies():
    o = Organizer("/tmp")
    assert o.detect_category("Inception 2010 1080p", [{"path": "Inception.mkv", "size": 0}]) == "Movies"


def test_detect_category_tv():
    o = Organizer("/tmp")
    assert o.detect_category("Show S01E02", [{"path": "episode.mkv", "size": 0}]) == "TV"


def test_detect_category_software():
    o = Organizer("/tmp")
    assert o.detect_category("MyApp", [{"path": "setup.exe", "size": 0}]) == "Software"


def test_gather_metadata():
    o = Organizer("/tmp")
    status = {"name": "Ubuntu", "info_hash": "a" * 40, "category": "Software", "save_path": "/tmp", "files": [{"file_id": 0, "path": "ubuntu.iso", "size": 1024, "priority": 4}]}
    meta = o.gather_metadata(status)
    assert meta["name"] == "Ubuntu"
    assert meta["total_size"] == 1024


def test_build_proposal_uses_allowed_category(tmp_path):
    organizer = Organizer(str(tmp_path))
    status = {
        "name": "Show S01E02", "info_hash": "a" * 40, "category": "Other",
        "save_path": str(tmp_path), "progress": 1.0,
        "files": [{"file_id": 0, "path": "episode.mkv", "size": 1024, "priority": 4}],
    }

    proposal = organizer.build_proposal(status, ["TV", "Other"])

    assert proposal["suggested_category"] == "TV"
    assert proposal["destination"] == str((tmp_path / "TV").resolve())
    assert proposal["file_renames"] == []
