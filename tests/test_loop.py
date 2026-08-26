"""Tests for the ReAct agent loop."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent.loop import AgentLoop
from agent.tools import ToolRegistry
from config import DeeptorrentConfig


class MockLLM:
    def __init__(self, responses):
        self.responses = responses
        self.i = 0
        self.calls = []

    def chat(self, messages, tools=None, effort=None, model=None, on_delta=None):
        self.calls.append({"effort": effort, "model": model})
        resp = self.responses[self.i]
        self.i += 1
        return resp

    def supports_tools(self):
        return True


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.list_torrents.return_value = [
        {
            "info_hash": "a" * 40,
            "name": "Big Torrent",
            "category": "Movies",
            "state": "downloading",
            "progress": 0.0,
            "download_rate": 0,
            "upload_rate": 0,
            "num_seeds": 0,
            "num_peers": 0,
            "total_size": 60_000_000_000,
        }
    ]
    engine.add_magnet.return_value = "b" * 40
    engine.get_swarm_stats.return_value = {
        "info_hash": "a" * 40,
        "name": "Big Torrent",
        "state": "downloading",
        "progress": 0.0,
        "download_rate": 0,
        "upload_rate": 0,
        "num_seeds": 0,
        "num_peers": 0,
        "num_complete": 0,
        "num_incomplete": 0,
        "list_peers": 0,
        "list_seeds": 0,
        "connect_candidates": 0,
        "dht_nodes": 0,
        "trackers": [],
        "peers": [],
        "piece_availability_histogram": {"bins": [0] * 10, "max": 0, "mean": 0.0, "zeros": 0},
        "error": None,
    }
    engine.pause.return_value = True
    engine.resume.return_value = True
    return engine


def test_add_magnet_confirmation(mock_engine):
    from agent.llm import LLMMessage

    add_call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "add_magnet",
            "arguments": json.dumps({"uri": "magnet:?xt=urn:btih:" + "d" * 40, "save_path": "/tmp", "category": "Movies"}),
        },
    }
    done = LLMMessage(role="assistant", content="The torrent has been added.", tool_calls=[])
    llm = MockLLM([
        LLMMessage(role="assistant", content=None, tool_calls=[add_call]),
        done,
    ])
    config = DeeptorrentConfig()
    tools = ToolRegistry(mock_engine, config)
    loop = AgentLoop(mock_engine, config, tools=tools, llm=llm)

    r1 = loop.chat("add magnet:?xt=urn:btih:" + "d" * 40 + " to Movies")
    assert r1.get("pending_confirmation") is True

    r2 = loop.chat("yes")
    assert r2["content"].startswith("- Executed")
    mock_engine.add_magnet.assert_called_once()
    # The post-execution summary runs on the fast model with low effort.
    assert any(c["effort"] == "low" and c["model"] == "deepseek-v4-flash" for c in llm.calls)


def test_pause_over_50gb(mock_engine):
    from agent.llm import LLMMessage

    list_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "list_torrents", "arguments": json.dumps({})},
    }
    pause_call = {
        "id": "call_2",
        "type": "function",
        "function": {"name": "pause_torrent", "arguments": json.dumps({"info_hash": "a" * 40})},
    }
    done = LLMMessage(role="assistant", content="Paused.", tool_calls=[])
    llm = MockLLM([
        LLMMessage(role="assistant", content=None, tool_calls=[list_call]),
        LLMMessage(role="assistant", content=None, tool_calls=[pause_call]),
        done,
    ])
    config = DeeptorrentConfig()
    tools = ToolRegistry(mock_engine, config)
    loop = AgentLoop(mock_engine, config, tools=tools, llm=llm)

    result = loop.chat("pause everything over 50GB")
    assert result["content"] == "Paused."
    mock_engine.pause.assert_called_once_with("a" * 40)


def test_watchdog_escalation_logs_and_requires_confirmation(mock_engine, tmp_path):
    from agent.llm import LLMMessage

    diagnose_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "diagnose_swarm", "arguments": json.dumps({"info_hash": "a" * 40})},
    }
    refresh_call = {
        "id": "call_2",
        "type": "function",
        "function": {"name": "refresh_tracker_list", "arguments": json.dumps({})},
    }
    find_alt_call = {
        "id": "call_3",
        "type": "function",
        "function": {"name": "find_alt_trackers", "arguments": json.dumps({"torrent_name": "Big Torrent"})},
    }
    search_call = {
        "id": "call_4",
        "type": "function",
        "function": {"name": "search_indexers", "arguments": json.dumps({"query": "Big Torrent"})},
    }
    done = LLMMessage(role="assistant", content="Escalation sequence completed.", tool_calls=[])

    llm = MockLLM([
        LLMMessage(role="assistant", content=None, tool_calls=[diagnose_call]),
        LLMMessage(role="assistant", content=None, tool_calls=[refresh_call]),
        LLMMessage(role="assistant", content=None, tool_calls=[find_alt_call]),
        LLMMessage(role="assistant", content=None, tool_calls=[search_call]),
        done,
    ])

    config = DeeptorrentConfig()
    config.watchdog.enabled = True
    config.watchdog.stall_threshold_seconds = 0  # always stalled
    config.watchdog.auto_heal = False
    tools = ToolRegistry(mock_engine, config)

    # Patch web/indexer clients to avoid network calls.
    tools._web_search.fetch_tracker_lists = MagicMock(return_value={"success": True, "trackers": ["udp://tracker1"], "errors": []})
    tools._web_search.search = MagicMock(return_value={"provider": "duckduckgo", "query": "x", "results": []})
    tools._indexer.search = MagicMock(return_value={"success": True, "results": []})

    loop = AgentLoop(mock_engine, config, tools=tools, llm=llm)
    loop._last_progress["a" * 40] = (0.0, 0.0)
    loop._watchdog_check()

    # Without auto-heal, add_tracker/add_magnet are not executed, but reasoning is logged.
    assert llm.i >= 2  # at least diagnosis + refresh attempt
    mock_engine.add_tracker.assert_not_called()


def _tc(call_id):
    return {"id": call_id, "type": "function",
            "function": {"name": "list_torrents", "arguments": "{}"}}


def test_history_budget_keeps_tail(mock_engine):
    """History older than the budget is dropped; full history stays for display."""
    config = DeeptorrentConfig()
    config.llm.history_budget = 4
    tools = ToolRegistry(mock_engine, config)
    loop = AgentLoop(mock_engine, config, tools=tools, llm=MockLLM([]))
    loop.history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("c1")]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]
    pruned = loop._pruned_history()
    assert [m["content"] for m in pruned] == ["a1", "u2", "a2", "u3"]
    assert len(loop.history) == 7  # untouched


def test_history_budget_never_starts_with_orphan_tool(mock_engine):
    """A budget cut landing mid tool-exchange drops the orphaned tool message."""
    config = DeeptorrentConfig()
    config.llm.history_budget = 2
    tools = ToolRegistry(mock_engine, config)
    loop = AgentLoop(mock_engine, config, tools=tools, llm=MockLLM([]))
    loop.history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("c1")]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("c2")]},
        {"role": "tool", "tool_call_id": "c2", "content": "{}"},
        {"role": "user", "content": "u2"},
    ]
    pruned = loop._pruned_history()
    # Window [tool c2, u2] would start with an orphaned tool message — skip it.
    assert pruned == [{"role": "user", "content": "u2"}]


def test_history_budget_keeps_complete_tool_exchange(mock_engine):
    """An assistant tool_calls message is kept when all its responses fit."""
    config = DeeptorrentConfig()
    config.llm.history_budget = 3
    tools = ToolRegistry(mock_engine, config)
    loop = AgentLoop(mock_engine, config, tools=tools, llm=MockLLM([]))
    loop.history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("c1")]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("c2")]},
        {"role": "tool", "tool_call_id": "c2", "content": "{}"},
        {"role": "user", "content": "u2"},
    ]
    pruned = loop._pruned_history()
    assert len(pruned) == 3
    assert pruned[0]["tool_calls"][0]["id"] == "c2"


def test_history_budget_zero_unlimited(mock_engine):
    config = DeeptorrentConfig()
    config.llm.history_budget = 0
    tools = ToolRegistry(mock_engine, config)
    loop = AgentLoop(mock_engine, config, tools=tools, llm=MockLLM([]))
    loop.history = [{"role": "user", "content": f"u{i}"} for i in range(100)]
    assert len(loop._pruned_history()) == 100


def _watchdog_loop(mock_engine, debug, done_content="Nothing more to do."):
    from agent.llm import LLMMessage

    config = DeeptorrentConfig()
    config.watchdog.enabled = True
    config.watchdog.stall_threshold_seconds = 0  # always stalled
    config.watchdog.auto_heal = False
    config.ui_agent_debug = debug
    tools = ToolRegistry(mock_engine, config)
    tools._indexer.search = MagicMock(return_value={"success": True, "results": []})
    llm = MockLLM([LLMMessage(role="assistant", content=done_content, tool_calls=[])])
    events = []
    loop = AgentLoop(mock_engine, config, tools=tools, llm=llm, on_event=events.append)
    loop._last_progress["a" * 40] = (0.0, 0.0)
    loop._watchdog_check()
    return events


def test_watchdog_emits_events_in_debug_mode(mock_engine):
    """Watchdog stall + conclusion are surfaced to the chat when debug is on (default)."""
    events = _watchdog_loop(mock_engine, debug=True)
    wd = [e for e in events if e.get("type") == "watchdog"]
    assert any("Stalled torrent detected" in e["message"] for e in wd)
    assert any("Watchdog conclusion" in e["message"] for e in wd)


def test_watchdog_silent_when_debug_off(mock_engine):
    events = _watchdog_loop(mock_engine, debug=False)
    assert not [e for e in events if e.get("type") == "watchdog"]


def test_streamed_content_not_duplicated_as_reasoning_event(mock_engine):
    """Narration that streamed live must not be re-shown as a 💭 event."""
    from agent.llm import LLMMessage

    list_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "list_torrents", "arguments": json.dumps({})},
    }

    class StreamMockLLM(MockLLM):
        def chat(self, messages, tools=None, effort=None, model=None, on_delta=None):
            if on_delta:
                on_delta("content", "Let me check.")
            return super().chat(messages, tools=tools, effort=effort, model=model, on_delta=on_delta)

    llm = StreamMockLLM([
        LLMMessage(role="assistant", content="Let me check.", tool_calls=[list_call]),
        LLMMessage(role="assistant", content="Here are your torrents.", tool_calls=[]),
    ])
    config = DeeptorrentConfig()  # stream=True by default
    tools = ToolRegistry(mock_engine, config)
    events = []
    loop = AgentLoop(mock_engine, config, tools=tools, llm=llm, on_event=events.append)
    loop.chat("list my torrents")

    assert not [e for e in events if e.get("type") == "reasoning"]
    assert [e for e in events if e.get("type") == "stream_delta"]
    # tool_end events carry the raw result payload for debug rendering.
    te = [e for e in events if e.get("type") == "tool_end"]
    assert te and isinstance(te[0].get("result"), dict)
