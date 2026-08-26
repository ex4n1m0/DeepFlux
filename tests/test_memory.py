"""Tests for the persistent local memory store and its agent tools."""
from __future__ import annotations

from unittest.mock import MagicMock

from agent.memory import MemoryStore
from agent.tools import ToolRegistry
from config import DeeptorrentConfig


def test_save_and_search(tmp_path):
    store = MemoryStore(str(tmp_path))
    r = store.save("Prefers 1080p releases", scope="user")
    assert r["success"] is True
    assert r["duplicate"] is False

    # Same content again -> deduplicated, file not appended.
    r2 = store.save("Prefers 1080p releases", scope="user")
    assert r2["duplicate"] is True
    text = (tmp_path / "USER.md").read_text(encoding="utf-8")
    assert text.count("Prefers 1080p releases") == 1

    store.save("Home connection is 100 Mbit", scope="fact")
    hits = store.search("1080p")
    assert hits["count"] == 1
    assert hits["results"][0]["source"] == "USER.md"

    hits = store.search("connection speed")
    assert hits["count"] == 1
    assert hits["results"][0]["source"] == "MEMORY.md"


def test_note_scope_goes_to_daily_file(tmp_path):
    store = MemoryStore(str(tmp_path))
    r = store.save("Added ubuntu torrent to Movies", scope="note")
    assert r["success"] is True
    daily = list((tmp_path / "daily").glob("*.md"))
    assert len(daily) == 1
    assert "Added ubuntu torrent" in daily[0].read_text(encoding="utf-8")
    # Daily notes are searchable but not injected into the prompt.
    assert store.search("ubuntu")["count"] == 1
    assert "Added ubuntu torrent" not in store.prompt_section()


def test_prompt_section_injection_and_budget(tmp_path):
    store = MemoryStore(str(tmp_path))
    assert store.prompt_section() == ""  # nothing saved yet

    store.save("Always confirm before deleting files", scope="user")
    section = store.prompt_section()
    assert "USER.md" in section
    assert "Always confirm before deleting files" in section

    # Content beyond the budget is truncated in the injected copy only.
    store.save("x" * 5000, scope="fact")
    section = store.prompt_section()
    assert "MEMORY.md" in section
    assert len(section) < 5000
    assert "x" * 5000 in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


def test_search_rejects_tiny_queries(tmp_path):
    store = MemoryStore(str(tmp_path))
    store.save("Prefers 1080p", scope="fact")
    assert store.search("a b")["success"] is False


def test_invalid_scope_falls_back_to_fact(tmp_path):
    store = MemoryStore(str(tmp_path))
    r = store.save("something durable", scope="bogus")
    assert r["success"] is True
    assert r["scope"] == "fact"


def test_memory_tools(tmp_path):
    config = DeeptorrentConfig()
    config.llm.memory_dir = str(tmp_path)
    tools = ToolRegistry(MagicMock(), config)

    r = tools.call("save_memory", {"content": "Prefers x265 encodes", "scope": "user"})
    assert r["success"] is True

    r = tools.call("search_memory", {"query": "x265"})
    assert r["count"] == 1
    assert "x265" in r["results"][0]["line"]


def test_memory_tools_disabled(tmp_path):
    config = DeeptorrentConfig()
    config.llm.memory_enabled = False
    config.llm.memory_dir = str(tmp_path)
    tools = ToolRegistry(MagicMock(), config)
    assert tools.memory is None
    r = tools.call("save_memory", {"content": "anything"})
    assert r["success"] is False
    assert not list(tmp_path.iterdir())  # nothing written
