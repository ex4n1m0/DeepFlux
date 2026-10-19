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
