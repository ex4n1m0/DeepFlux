"""Agent task sessions (agent/sessions.py, owner 2026-11-22).

Contract under test: one JSON file per auto-saved conversation under a
local-only directory, atomic upserts (tmp + os.replace), newest-first
listing that SKIPS corrupt files, rename/delete, and the
``sanitize_messages`` invariant — a restored history must never carry a
dangling tool-call exchange (assistant ``tool_calls`` without every
reply), or reopening a crash-interrupted task would hand the LLM an
API-breaking message list.

This file deliberately stays Qt-free (the a–c chunk heap-shape rule).
"""
from __future__ import annotations

import json
import os
import time

from agent.sessions import (
    SessionStore,
    auto_name,
    default_sessions_dir,
    new_session_id,
    sanitize_messages,
)


def _mk_store(tmp_path) -> SessionStore:
    return SessionStore(str(tmp_path / "sessions"))


def _sample_messages():
    return [
        {"role": "user", "content": "find a torrent"},
        {"role": "assistant", "content": "on it"},
    ]


def _sample_transcript():
    return [
        {"k": "user", "t": "find a torrent"},
        {"k": "event", "t": "searching…"},
        {"k": "agent", "t": "on it"},
    ]


# -- save / load round-trip -------------------------------------------------


def test_save_then_load_round_trips_payloads(tmp_path):
    store = _mk_store(tmp_path)
    sid = new_session_id()
    assert store.save(sid, "My task", _sample_messages(), _sample_transcript())
    data = store.load(sid)
    assert data is not None
    assert data.id == sid
    assert data.name == "My task"
    assert data.messages == _sample_messages()
    assert data.transcript == _sample_transcript()


def test_resave_preserves_created_at_and_bumps_updated(tmp_path):
    store = _mk_store(tmp_path)
    sid = new_session_id()
    store.save(sid, "t1", _sample_messages(), [])
    first = store.load(sid)
    time.sleep(1.1)  # isoformat(timespec="seconds") — force a new second
    store.save(sid, "t2", _sample_messages(), [])
    second = store.load(sid)
    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at


def test_save_is_atomic_no_tmp_leftover(tmp_path):
    store = _mk_store(tmp_path)
    sid = new_session_id()
    store.save(sid, "t", _sample_messages(), [])
    files = os.listdir(store._dir)
    assert files == [f"{sid}.json"]
    # The saved file is complete, valid JSON with the schema envelope.
    with open(os.path.join(store._dir, f"{sid}.json"), encoding="utf-8") as fh:
        envelope = json.load(fh)
    assert envelope["schema"] == 1
    assert envelope["id"] == sid


def test_empty_name_falls_back_to_auto_name_from_transcript(tmp_path):
    store = _mk_store(tmp_path)
    sid = new_session_id()
    store.save(sid, "", [], [{"k": "user", "t": "grab ubuntu iso please"}])
    assert store.load(sid).name == "grab ubuntu iso please"


# -- listing -----------------------------------------------------------------


def test_list_orders_newest_first(tmp_path):
    store = _mk_store(tmp_path)
    ids = []
    for i in range(3):
        sid = new_session_id()
        ids.append(sid)
        store.save(sid, f"task {i}", [], [])
        time.sleep(1.1)  # distinct second-precision timestamps
    metas = store.list_sessions()
    assert [m.id for m in metas] == list(reversed(ids))
    assert metas[0].name == "task 2"


def test_list_skips_corrupt_and_foreign_files(tmp_path):
    store = _mk_store(tmp_path)
    good = new_session_id()
    store.save(good, "good", [], [])
    # Corrupt JSON, wrong schema, and a non-id filename: all ignored.
    with open(os.path.join(store._dir, f"{new_session_id()}.json"), "w") as fh:
        fh.write("{not json")
    with open(os.path.join(store._dir, f"{new_session_id()}.json"), "w") as fh:
        json.dump({"schema": 99, "id": new_session_id()}, fh)
    with open(os.path.join(store._dir, "readme.json"), "w") as fh:
        fh.write("[]")
    metas = store.list_sessions()
    assert [m.id for m in metas] == [good]


def test_list_preview_prefers_first_user_line(tmp_path):
    store = _mk_store(tmp_path)
    sid = new_session_id()
    store.save(sid, "n", [], [
        {"k": "event", "t": "noise"},
        {"k": "user", "t": "download the file"},
    ])
    assert store.list_sessions()[0].preview == "download the file"


# -- rename / delete ----------------------------------------------------------


def test_rename_persists_and_missing_returns_false(tmp_path):
    store = _mk_store(tmp_path)
    sid = new_session_id()
    store.save(sid, "old", [], [])
    assert store.rename(sid, "renamed task")
    assert store.load(sid).name == "renamed task"
    assert store.rename(new_session_id(), "x") is False
    # Blank rename is refused.
    assert store.rename(sid, "   ") is False


def test_delete_removes_file_and_tolerates_missing(tmp_path):
    store = _mk_store(tmp_path)
    sid = new_session_id()
    store.save(sid, "t", [], [])
    assert store.delete(sid)
    assert store.load(sid) is None
    assert store.delete(sid) is False
    assert store.list_sessions() == []


# -- id hygiene ----------------------------------------------------------------


def test_bad_ids_are_rejected_not_path_traversal(tmp_path):
    store = _mk_store(tmp_path)
    for bad in ("", "abc", "../escape", "A" * 32, "0" * 31):
        assert store.save(bad, "x", [], []) is False
        assert store.load(bad) is None
        assert store.delete(bad) is False


def test_env_override_selects_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("DF_AGENT_SESSIONS", str(tmp_path / "envdir"))
    assert default_sessions_dir() == str(tmp_path / "envdir")
    store = SessionStore()
    sid = new_session_id()
    store.save(sid, "t", [], [])
    assert os.path.exists(tmp_path / "envdir" / f"{sid}.json")


# -- sanitize_messages ----------------------------------------------------------


def test_sanitize_drops_dangling_assistant_tool_calls():
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "f"}}],
        },
        # call_2 never answered → the assistant exchange is incomplete.
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "f"}},
                {"id": "call_2", "type": "function", "function": {"name": "f"}},
            ],
        },
    ]
    out = sanitize_messages(msgs)
    assert out == [{"role": "user", "content": "hi"}]


def test_sanitize_drops_orphan_trailing_tool_message():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done"},
        {"role": "tool", "tool_call_id": "call_ghost", "content": "{}"},
    ]
    out = sanitize_messages(msgs)
    assert out[-1]["role"] == "assistant"


def test_sanitize_keeps_complete_exchange_untouched():
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {"role": "assistant", "content": "all done"},
    ]
    assert sanitize_messages(msgs) == msgs


def test_sanitize_ignores_non_dicts():
    assert sanitize_messages(["junk", {"role": "user", "content": "hi"}, 3]) == [
        {"role": "user", "content": "hi"}
    ]


def test_save_sanitizes_before_writing(tmp_path):
    store = _mk_store(tmp_path)
    sid = new_session_id()
    dangling = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}],
        },
    ]
    store.save(sid, "t", dangling, [])
    assert store.load(sid).messages == [{"role": "user", "content": "hi"}]


# -- auto_name -------------------------------------------------------------------


def test_auto_name_collapses_and_truncates():
    assert auto_name("  find   me\nan iso  ") == "find me an iso"
    long = "x" * 80
    assert auto_name(long) == "x" * 47 + "…"
    assert len(auto_name(long)) == 48
    assert auto_name("") == ""


def test_sanitize_drops_partially_answered_multi_tool_call_tail():
    """Review HIGH 2026-11-22: agent/loop.py appends the assistant with
    ALL tool_calls before replies land one-by-one — a save in that window
    (closeEvent / sweep path) used to persist assistant(c1,c2)+tool(c1),
    which breaks the LLM API on restore AND on every later turn."""
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f"}},
                {"id": "c2", "type": "function", "function": {"name": "f"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    out = sanitize_messages(msgs)
    assert out == [{"role": "user", "content": "hi"}]


def test_sanitize_keeps_complete_trailing_tool_exchange():
    """[user, assistant(c1), tool(c1)] is API-valid — must survive."""
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    assert sanitize_messages(msgs) == msgs


def test_sanitize_strips_partial_exchange_from_a_later_owner():
    """A tool reply whose id belongs to a LATER assistant with a second,
    unanswered call must not legitimize that exchange."""
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f"}},
                {"id": "c9", "type": "function", "function": {"name": "f"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    out = sanitize_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "tool"]
    assert out[1]["tool_calls"][0]["id"] == "c1"
