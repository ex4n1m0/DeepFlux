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


def test_auto_heal_does_not_bypass_chat_confirmation(mock_engine):
    from agent.llm import LLMMessage

    add_call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "add_magnet",
            "arguments": json.dumps({"uri": "magnet:?xt=urn:btih:" + "d" * 40, "save_path": "/tmp"}),
        },
    }
    config = DeeptorrentConfig()
    config.watchdog.auto_heal = True
    loop = AgentLoop(
        mock_engine, config, tools=ToolRegistry(mock_engine, config),
        llm=MockLLM([LLMMessage(role="assistant", tool_calls=[add_call])]),
    )

    result = loop.chat("add it")

    assert result["pending_confirmation"] is True
    mock_engine.add_magnet.assert_not_called()


def test_mixed_batch_is_preserved_until_confirmation(mock_engine):
    from agent.llm import LLMMessage

    list_call = {"id": "call_1", "type": "function",
                 "function": {"name": "list_torrents", "arguments": "{}"}}
    add_call = {
        "id": "call_2",
        "type": "function",
        "function": {
            "name": "add_magnet",
            "arguments": json.dumps({"uri": "magnet:?xt=urn:btih:" + "d" * 40, "save_path": "/tmp"}),
        },
    }
    llm = MockLLM([
        LLMMessage(role="assistant", tool_calls=[list_call, add_call]),
        LLMMessage(role="assistant", content="Done."),
    ])
    config = DeeptorrentConfig()
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=llm)

    result = loop.chat("list and add")
    assert result["pending_confirmation"] is True
    assert [p.tool_name for p in loop.pending] == ["list_torrents", "add_magnet"]

    loop.chat("yes")
    mock_engine.list_torrents.assert_called()
    mock_engine.add_magnet.assert_called_once()


def test_reject_clears_pending_action(mock_engine):
    from agent.llm import LLMMessage

    call = {
        "id": "call_1", "type": "function",
        "function": {"name": "delete_path", "arguments": json.dumps({"path": "/tmp/file"})},
    }
    config = DeeptorrentConfig()
    loop = AgentLoop(
        mock_engine, config, tools=ToolRegistry(mock_engine, config),
        llm=MockLLM([LLMMessage(role="assistant", tool_calls=[call])]),
    )

    loop.chat("delete it")
    result = loop.chat("no")

    assert result["content"] == "Canceled the pending action(s)."
    assert loop.pending == []


def test_followup_destructive_call_requires_new_confirmation(mock_engine):
    from agent.llm import LLMMessage

    first = {
        "id": "call_1", "type": "function",
        "function": {"name": "add_tracker", "arguments": json.dumps({
            "info_hash": "a" * 40, "url": "udp://tracker.example:80/announce",
        })},
    }
    second = {
        "id": "call_2", "type": "function",
        "function": {"name": "remove_torrent", "arguments": json.dumps({
            "info_hash": "a" * 40, "delete_files": False,
        })},
    }
    mock_engine.add_tracker.return_value = True
    llm = MockLLM([
        LLMMessage(role="assistant", tool_calls=[first]),
        LLMMessage(role="assistant", tool_calls=[second]),
    ])
    config = DeeptorrentConfig()
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=llm)

    loop.chat("repair it")
    result = loop.chat("yes")

    assert result["pending_confirmation"] is True
    assert [p.tool_name for p in loop.pending] == ["remove_torrent"]
    mock_engine.remove.assert_not_called()


def test_confirmation_preview_redacts_sensitive_arguments(mock_engine):
    from agent.llm import LLMMessage

    call = {
        "id": "call_1", "type": "function",
        "function": {"name": "browser_fill", "arguments": json.dumps({
            "selector": "#password", "value": "secret-value", "submit": True,
        })},
    }
    config = DeeptorrentConfig()
    loop = AgentLoop(
        mock_engine, config, tools=ToolRegistry(mock_engine, config),
        llm=MockLLM([LLMMessage(role="assistant", tool_calls=[call])]),
    )

    result = loop.chat("log in")

    assert "secret-value" not in result["content"]
    assert "<redacted>" in result["content"]


def test_confirmation_preview_includes_destructive_defaults(mock_engine):
    from agent.llm import LLMMessage

    call = {
        "id": "call_1", "type": "function",
        "function": {"name": "cancel_download", "arguments": json.dumps({"job_id": "abc123"})},
    }
    config = DeeptorrentConfig()
    loop = AgentLoop(
        mock_engine, config, tools=ToolRegistry(mock_engine, config),
        llm=MockLLM([LLMMessage(role="assistant", tool_calls=[call])]),
    )

    result = loop.chat("cancel that download")

    assert '"delete_file": true' in result["content"]


def test_watchdog_first_observation_does_not_escalate(mock_engine):
    config = DeeptorrentConfig()
    config.watchdog.stall_threshold_seconds = 0
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=MockLLM([]))
    loop._escalate = MagicMock()

    loop._watchdog_check()

    loop._escalate.assert_not_called()
    assert "a" * 40 in loop._last_progress


def test_watchdog_ignores_paused_torrents(mock_engine):
    config = DeeptorrentConfig()
    config.watchdog.stall_threshold_seconds = 0
    mock_engine.list_torrents.return_value[0]["paused"] = True
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=MockLLM([]))
    loop._last_progress["a" * 40] = (0.0, 0.0)
    loop._escalate = MagicMock()

    loop._watchdog_check()

    loop._escalate.assert_not_called()


def test_watchdog_cooldown_prevents_repeat_escalation(mock_engine):
    import time

    config = DeeptorrentConfig()
    config.watchdog.stall_threshold_seconds = 0
    config.watchdog.cooldown_seconds = 3600
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=MockLLM([]))
    loop._last_progress["a" * 40] = (0.0, 0.0)
    loop._last_escalation["a" * 40] = time.time()
    loop._escalate = MagicMock()

    loop._watchdog_check()

    loop._escalate.assert_not_called()


def test_watchdog_tool_messages_follow_assistant_call(mock_engine):
    from agent.llm import LLMMessage

    call = {
        "id": "call_1", "type": "function",
        "function": {"name": "list_torrents", "arguments": "{}"},
    }

    class CapturingLLM(MockLLM):
        def chat(self, messages, tools=None, effort=None, model=None, on_delta=None):
            self.calls.append(json.loads(json.dumps(messages)))
            response = self.responses[self.i]
            self.i += 1
            return response

    llm = CapturingLLM([
        LLMMessage(role="assistant", tool_calls=[call]),
        LLMMessage(role="assistant", content="Done."),
    ])
    config = DeeptorrentConfig()
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=llm)

    loop._escalate(mock_engine.list_torrents.return_value[0])

    assert [m["role"] for m in llm.calls[1][-2:]] == ["assistant", "tool"]
    assert llm.calls[1][-2]["tool_calls"][0]["id"] == "call_1"
    assert llm.calls[1][-1]["tool_call_id"] == "call_1"


def test_watchdog_rejects_tools_outside_recovery_allowlist(mock_engine):
    from agent.llm import LLMMessage

    call = {
        "id": "call_1", "type": "function",
        "function": {"name": "browser_navigate", "arguments": json.dumps({"url": "example.com"})},
    }
    llm = MockLLM([
        LLMMessage(role="assistant", tool_calls=[call]),
        LLMMessage(role="assistant", content="Done."),
    ])
    config = DeeptorrentConfig()
    config.watchdog.auto_heal = True
    tools = ToolRegistry(mock_engine, config, browser_bridge=MagicMock())
    loop = AgentLoop(mock_engine, config, tools=tools, llm=llm)

    loop._escalate(mock_engine.list_torrents.return_value[0])

    tools._browser_bridge.call.assert_not_called()


def test_malformed_tool_arguments_return_error_without_execution(mock_engine):
    from agent.llm import LLMMessage

    call = {
        "id": "call_1", "type": "function",
        "function": {"name": "list_torrents", "arguments": "{"},
    }
    llm = MockLLM([
        LLMMessage(role="assistant", tool_calls=[call]),
        LLMMessage(role="assistant", content="The tool call was invalid."),
    ])
    config = DeeptorrentConfig()
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=llm)

    result = loop.chat("list")

    assert result["content"] == "The tool call was invalid."
    mock_engine.list_torrents.assert_not_called()
    tool_result = json.loads(next(message["content"] for message in loop.history if message["role"] == "tool"))
    assert "Invalid tool arguments" in tool_result["error"]


def test_cancel_stops_before_tool_execution(mock_engine):
    from agent.llm import LLMMessage

    call = {
        "id": "call_1", "type": "function",
        "function": {"name": "list_torrents", "arguments": "{}"},
    }

    class CancelingLLM:
        def __init__(self):
            self.loop = None

        def chat(self, messages, tools=None, effort=None, model=None, on_delta=None):
            self.loop.cancel()
            return LLMMessage(role="assistant", tool_calls=[call])

    config = DeeptorrentConfig()
    llm = CancelingLLM()
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=llm)
    llm.loop = loop

    result = loop.chat("list")

    assert result["stopped"] is True
    assert "Canceled" in result["content"]
    mock_engine.list_torrents.assert_not_called()


def test_repeated_identical_tool_calls_stop_loop(mock_engine):
    from agent.llm import LLMMessage

    call = {
        "id": "call_1", "type": "function",
        "function": {"name": "list_torrents", "arguments": "{}"},
    }
    config = DeeptorrentConfig()
    config.llm.repeated_call_limit = 2
    llm = MockLLM([LLMMessage(role="assistant", tool_calls=[call]) for _ in range(3)])
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=llm)

    result = loop.chat("keep listing")

    assert result["stopped"] is True
    assert "identical arguments" in result["content"]
    assert mock_engine.list_torrents.call_count == 2


def test_llm_request_budget_stops_loop(mock_engine):
    from agent.llm import LLMMessage

    calls = [
        {"id": f"call_{index}", "type": "function",
         "function": {"name": "list_torrents", "arguments": json.dumps({"offset": index})}}
        for index in range(2)
    ]
    config = DeeptorrentConfig()
    config.llm.max_llm_calls = 2
    llm = MockLLM([LLMMessage(role="assistant", tool_calls=[call]) for call in calls])
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=llm)

    result = loop.chat("list repeatedly")

    assert result["stopped"] is True
    assert "2-request LLM limit" in result["content"]
    assert llm.i == 2


def test_context_budget_trims_old_messages_without_orphan_tools(mock_engine):
    config = DeeptorrentConfig()
    config.llm.context_budget_tokens = 1000
    config.llm.response_reserve_tokens = 200
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=MockLLM([]))
    messages = [{"role": "system", "content": "system"}]
    for index in range(20):
        messages.extend([
            {"role": "user", "content": f"request {index} " + "x" * 400},
            {"role": "assistant", "content": f"answer {index} " + "y" * 400},
        ])

    fitted = loop._fit_context(messages, [])

    assert len(fitted) < len(messages)
    assert fitted[0]["role"] == "system"
    assert fitted[1]["role"] != "tool"
    assert sum(loop._message_size(message) for message in fitted) <= 3200


def test_context_budget_truncates_large_tool_payload(mock_engine):
    config = DeeptorrentConfig()
    config.llm.context_budget_tokens = 1000
    config.llm.response_reserve_tokens = 200
    loop = AgentLoop(mock_engine, config, tools=ToolRegistry(mock_engine, config), llm=MockLLM([]))
    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("call_1")]},
        {"role": "tool", "tool_call_id": "call_1", "content": "z" * 20000},
    ]

    fitted = loop._fit_context(messages, [])

    assert fitted[-1]["role"] == "tool"
    assert "context-truncated" in fitted[-1]["content"]
    assert len(fitted[-1]["content"]) < 20000


def test_dummy_llm_completes_pause_over_size_flow(mock_engine):
    from agent.llm import DummyLLMClient

    config = DeeptorrentConfig()
    config.llm.provider = "dummy"
    loop = AgentLoop(
        mock_engine, config, tools=ToolRegistry(mock_engine, config),
        llm=DummyLLMClient(config.llm),
    )

    result = loop.chat("pause everything over 50 GB")

    assert "paused" in result["content"].lower()
    mock_engine.pause.assert_called_once_with("a" * 40)
