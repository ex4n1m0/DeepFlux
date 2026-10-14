"""YOLO mode tests (config.llm.yolo_mode, owner request 2026-10-13).

When on, the agent NEVER asks for tool confirmation — even confirmation-
gated tools (add_magnet, run_shell, deletions) execute immediately. The
flag must also be unreachable from the agent's own set_settings tool: a
prompt injection must not be able to unlock its own gates.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agent.llm import LLMMessage
from agent.loop import AgentLoop
from agent.tools import ToolRegistry
from config import DeeptorrentConfig


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.list_torrents.return_value = []
    engine.add_magnet.return_value = {"success": True, "name": "test"}
    return engine


def _add_magnet_call():
    return {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "add_magnet",
            "arguments": json.dumps(
                {"uri": "magnet:?xt=urn:btih:" + "d" * 40, "save_path": "/tmp", "category": "Movies"}),
        },
    }


def test_yolo_mode_executes_confirmation_tools_immediately(mock_engine):
    """The exact scenario of test_add_magnet_confirmation, with yolo on: no
    pending_confirmation — the tool runs on the first chat()."""
    add_call = _add_magnet_call()
    done = LLMMessage(role="assistant", content="The torrent has been added.", tool_calls=[])
    llm = _MockLLM([
        LLMMessage(role="assistant", content=None, tool_calls=[add_call]),
        done,
    ])
    config = DeeptorrentConfig()
    config.llm.yolo_mode = True
    tools = ToolRegistry(mock_engine, config)
    loop = AgentLoop(mock_engine, config, tools=tools, llm=llm)

    r1 = loop.chat("add magnet:?xt=urn:btih:" + "d" * 40 + " to Movies")
    # No confirmation round-trip: the first chat returns the model's FINAL
    # message (the normal ReAct completion) with the tool already executed.
    assert not r1.get("pending_confirmation")
    assert r1["content"] == "The torrent has been added."
    mock_engine.add_magnet.assert_called_once()
    assert not loop.pending  # nothing queued awaiting a "yes"


def test_yolo_off_still_confirms(mock_engine):
    """Default off: the pre-existing behavior is untouched."""
    add_call = _add_magnet_call()
    llm = _MockLLM([
        LLMMessage(role="assistant", content=None, tool_calls=[add_call]),
    ])
    config = DeeptorrentConfig()
    assert config.llm.yolo_mode is False
    tools = ToolRegistry(mock_engine, config)
    loop = AgentLoop(mock_engine, config, tools=tools, llm=llm)
    r1 = loop.chat("add magnet:?xt=urn:btih:" + "d" * 40 + " to Movies")
    assert r1.get("pending_confirmation") is True
    mock_engine.add_magnet.assert_not_called()


def test_yolo_system_prompt_states_unattended_mode(mock_engine):
    config = DeeptorrentConfig()
    tools = ToolRegistry(mock_engine, config)
    loop = AgentLoop(mock_engine, config, tools=tools, llm=_MockLLM([]))
    base = loop._render_system_prompt()
    assert "UNATTENDED MODE" not in base
    config.llm.yolo_mode = True
    yolo = loop._render_system_prompt()
    assert "UNATTENDED MODE (YOLO) IS ON" in yolo
    # The base confirmation rule is still present (we append, not replace).
    assert "approved by the user" in yolo


def test_yolo_mode_denied_from_agent_settings(mock_engine):
    """The agent can never flip its own permission gate: llm.yolo_mode is
    absent from list_settings and rejected by set_settings."""
    config = DeeptorrentConfig()
    tools = ToolRegistry(mock_engine, config)

    listed = tools.call("list_settings", {})
    flat = json.dumps(listed)
    assert "yolo_mode" not in flat

    from agent.tools import ToolError
    with pytest.raises(ToolError):
        tools.call("set_settings", {"path": "llm.yolo_mode", "value": True})
    assert config.llm.yolo_mode is False  # gate stays closed


def test_yolo_off_mid_conversation_on_next_chat(mock_engine):
    """Toggling between chats: pendings from before still resolve the old way,
    and the next chat honors the new mode."""
    config = DeeptorrentConfig()
    tools = ToolRegistry(mock_engine, config)
    add_call = _add_magnet_call()
    llm = _MockLLM([
        LLMMessage(role="assistant", content=None, tool_calls=[add_call]),
        LLMMessage(role="assistant", content="added", tool_calls=[]),
        LLMMessage(role="assistant", content=None, tool_calls=[_add_magnet_call()]),
        LLMMessage(role="assistant", content="added again", tool_calls=[]),
    ])
    loop = AgentLoop(mock_engine, config, tools=tools, llm=llm)
    assert loop.chat("add it").get("pending_confirmation") is True
    # User flips YOLO on (the GUI toggles the shared config object) and
    # approves the OLD pending via the same "yes" as before.
    config.llm.yolo_mode = True
    r2 = loop.chat("yes")
    assert r2["content"].startswith("- Executed")
    # The next tool call runs without any confirmation round-trip.
    r3 = loop.chat("add the same one again")
    assert not r3.get("pending_confirmation")
    assert r3["content"] == "added again"  # straight to the final message
    assert mock_engine.add_magnet.call_count == 2


class _MockLLM:
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
