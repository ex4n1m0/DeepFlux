from __future__ import annotations

import pytest

from config import DeeptorrentConfig


@pytest.fixture(autouse=True)
def isolate_default_config_path(monkeypatch, tmp_path):
    path = tmp_path / ".deeptorrent" / "config.json"
    monkeypatch.setattr(DeeptorrentConfig, "default_config_path", lambda: str(path))
    return path
