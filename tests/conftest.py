from __future__ import annotations

import os

# Qt-before-libtorrent guard (2026-11-21): on some boots Windows assigns
# libtorrent.pyd and Qt6Core.dll ASLR bases that cannot coexist when
# libtorrent maps FIRST — the subsequent first PySide6 import AVs (proven
# deterministically: `import libtorrent; from PySide6.QtCore import Qt`
# segfaulted 5/5 while the reverse order passed 3/3, same wheels that were
# gate-green on 2026-11-19; bases re-roll per boot). The app itself always
# imports gui (Qt) before the agent/engine chain, but pytest collection
# imports test modules alphabetically — a chunk collecting agent/engine
# importers (llm, jackett, loop...) before a Qt import died. conftest loads
# before any test module, so importing Qt HERE pins the safe order for
# every process. Same philosophy as test_local_model.py's agent.llm-early
# guard; delete neither.
import PySide6.QtCore  # noqa: F401  (must precede any libtorrent import)

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


@pytest.fixture(autouse=True)
def isolate_indexer_health(monkeypatch, tmp_path):
    """Never read/write the real ~/.deeptorrent indexer-state files.

    Same isolation rule as the config redirect above: the search tier
    records per-indexer failures through a process-wide singleton and reads
    the Jackett definitions sidecar, so tests must point both at per-test
    temp files and drop the singleton before and after (a stale singleton
    would carry failures between tests; a real sidecar would turn unrelated
    ""-origin test sources into zombie suspects)."""
    from infra import indexer_health
    monkeypatch.setenv("DF_INDEXER_HEALTH", str(tmp_path / "indexer_health.json"))
    monkeypatch.setenv("DF_JACKETT_DEFINITIONS", str(tmp_path / "jackett_definitions.json"))
    indexer_health.reset_health_store_singleton()
    yield
    indexer_health.reset_health_store_singleton()


@pytest.fixture(autouse=True)
def isolate_room_portal(monkeypatch, tmp_path):
    """Never open the user's real ~/.deeptorrent/room-portal profile. The
    one test that builds a real RoomTab view (test_oh_room delenvs
    DF_NO_ROOM) constructs a QtWebEngine profile with persistent leveldb/
    cookie storage — without this redirect it opened the SAME store the
    installed app uses (2026-11-09 review, M8)."""
    monkeypatch.setenv("DF_ROOM_DIR", str(tmp_path / "room-portal"))


@pytest.fixture(autouse=True)
def isolate_agent_sessions(monkeypatch, tmp_path):
    """Never read/write the real ~/.deeptorrent/agent_sessions (the Agent
    tab's saved tasks). Same isolation rule as the config redirect: a GUI
    test that sends one chat message auto-saves a session file."""
    monkeypatch.setenv("DF_AGENT_SESSIONS", str(tmp_path / "agent_sessions"))


@pytest.fixture(autouse=True)
def isolate_ytdlp_overlay(monkeypatch, tmp_path):
    """Never read/write the real ~/.deeptorrent/ytdlp — the downloaded
    yt-dlp releases of the in-app updater (infra/ytdlp_overlay.py). Without
    the redirect a Settings-dialog test's status line would read the user's
    real pointer file, and an unpatched revert() could act on real folders."""
    monkeypatch.setenv("DF_YTDLP_OVERLAY_DIR", str(tmp_path / "ytdlp"))


@pytest.fixture(autouse=True)
def isolate_local_model(monkeypatch, tmp_path):
    """Never touch the real ~/.deeptorrent/models (whisper weights + real
    GGUFs, multi-GB) or ~/.deeptorrent/llama (extracted runtimes), and drop
    the cached hardware probe so a test can't leak the host's GPU into the
    next one (or spawn a real llama-server device probe)."""
    monkeypatch.setenv("DF_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("DF_LLAMA_DIR", str(tmp_path / "llama"))
    from infra import local_model
    monkeypatch.setattr(local_model, "_hw_cache", None)
    yield
    monkeypatch.setattr(local_model, "_hw_cache", None)
