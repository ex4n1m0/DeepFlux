from __future__ import annotations

import os

import pytest

from config import DeeptorrentConfig

# Tests must never load the Room tab's embedded portal page (network +
# hub contact). The Room tab skips creating its web view entirely while
# this is set; the one test that builds the view delenvs it and loads
# about:blank instead of the real site.
os.environ.setdefault("DF_NO_ROOM", "1")


@pytest.fixture(autouse=True)
def isolate_default_config_path(monkeypatch, tmp_path):
    path = tmp_path / ".deeptorrent" / "config.json"
    monkeypatch.setattr(DeeptorrentConfig, "default_config_path", lambda: str(path))
    return path
