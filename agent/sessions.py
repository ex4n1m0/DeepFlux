"""Persistent agent conversation sessions ("tasks") — local-only.

The Agent tab auto-saves every conversation (owner decision 2026-11-22,
ZCode-style): each turn atomically writes ONE JSON file per session into
``~/.deeptorrent/agent_sessions/``, the tasks sidebar lists/renames/
deletes them, and nothing ever leaves the machine (no network, no
telemetry — the same locality contract as every other ~/.deeptorrent
file).

Two payloads are kept per session because they answer different questions:
- ``messages``  — the AgentLoop.history OpenAI-style dicts, so a reopened
  task resumes with full LLM context.
- ``transcript`` — the rendered GUI lines ({"k": kind, "t": text}) so a
  reopened task looks like it did, including web-sweep results and
  progress events that never enter agent history. Deliberately NOT
  captured (v1 scope, documented 2026-11-22): live-streamed token spans
  that a tool call seals mid-turn, and debug-mode dumps — the final
  agent bubble and the full LLM history always are.

Save points are turn-safe (user send / turn finished / error / close),
but ``sanitize_messages`` belt-and-braces strips a dangling trailing
tool-call exchange anyway: a task saved mid-crash must never restore an
API-breaking history (assistant tool_calls without their tool replies).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA = 1
MAX_NAME_LEN = 48
MAX_PREVIEW_LEN = 60
# Session ids are uuid4().hex — pinning the shape also makes the store
# path-traversal-proof for every entry point (list, load, rename, delete).
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_VALID_KINDS = ("user", "search_user", "agent", "event", "error", "pending", "search_html")
_TAG_RE = re.compile(r"<[^>]+>")


def default_sessions_dir() -> str:
    override = os.environ.get("DF_AGENT_SESSIONS")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".deeptorrent", "agent_sessions")


def new_session_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def auto_name(text: str) -> str:
    """Derive a task name from the first user message: collapse
    whitespace, cap at MAX_NAME_LEN chars with an ellipsis."""
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    if len(flat) <= MAX_NAME_LEN:
        return flat
    return flat[: MAX_NAME_LEN - 1].rstrip() + "…"


def sanitize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a copy of an LLM history that is safe to restore: any
    trailing incomplete tool-call exchange (assistant ``tool_calls``
    without every reply — including a PARTIAL reply set, e.g.
    ``assistant(c1,c2), tool(c1)`` — or an orphan trailing ``tool``
    message) is stripped from the end. Mid-history damage is left alone —
    saves only ever happen at turn boundaries, so incompleteness can only
    be at the tail (a turn killed by a crash)."""
    msgs = [m for m in messages if isinstance(m, dict)]
    changed = True
    while changed and msgs:
        changed = False
        last = msgs[-1]
        if last.get("role") == "tool":
            cid = str(last.get("tool_call_id") or "")
            # The owning assistant is the LAST one requesting this id.
            owner_idx = None
            for i in range(len(msgs) - 1, -1, -1):
                a = msgs[i]
                if a.get("role") == "assistant" and any(
                    isinstance(c, dict) and str(c.get("id") or "") == cid
                    for c in (a.get("tool_calls") or [])
                ):
                    owner_idx = i
                    break
            if owner_idx is None:
                msgs.pop()  # orphan: nobody ever asked for this reply
                changed = True
                continue
            owner = msgs[owner_idx]
            needed = {
                str(c.get("id") or "")
                for c in (owner.get("tool_calls") or [])
                if isinstance(c, dict)
            }
            answered = {
                str(m.get("tool_call_id") or "")
                for m in msgs[owner_idx + 1:]
                if m.get("role") == "tool"
            }
            if not needed <= answered:
                # Partial reply set (agent/loop.py appends the assistant
                # with ALL tool_calls before the replies land one-by-one):
                # drop this reply; the loop then re-examines the assistant.
                msgs.pop()
                changed = True
                continue
        if last.get("role") == "assistant":
            calls = [c for c in (last.get("tool_calls") or []) if isinstance(c, dict)]
            if calls:
                answered = {
                    str(m.get("tool_call_id") or "")
                    for m in msgs
                    if m.get("role") == "tool"
                }
                if any(str(c.get("id") or "") not in answered for c in calls):
                    msgs.pop()
                    changed = True
                    continue
    return msgs


@dataclass
class SessionMeta:
    """List-row summary of one saved task (no payloads)."""
    id: str
    name: str
    created_at: str
    updated_at: str
    message_count: int = 0
    preview: str = ""


@dataclass
class SessionData:
    """Full saved task as handed to the GUI on open."""
    id: str
    name: str
    created_at: str
    updated_at: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
    transcript: List[Dict[str, str]] = field(default_factory=list)


class SessionStore:
    """One-JSON-file-per-task store with atomic writes.

    Thread-safe: GUI-thread saves plus the closeEvent final save never
    interleave (same tmp+os.replace discipline as config.to_file).
    """

    def __init__(self, directory: str = "") -> None:
        self._dir = directory or default_sessions_dir()
        self._lock = threading.Lock()

    # -- paths -------------------------------------------------------------

    def _path(self, session_id: str) -> Optional[str]:
        if not _ID_RE.match(session_id or ""):
            return None
        return os.path.join(self._dir, f"{session_id}.json")

    # -- write side ---------------------------------------------------------

    def save(
        self,
        session_id: str,
        name: str,
        messages: List[Dict[str, Any]],
        transcript: List[Dict[str, str]],
    ) -> bool:
        """Upsert one session file atomically; ``created_at`` of an
        existing file survives re-saves."""
        path = self._path(session_id)
        if path is None:
            return False
        now = _now()
        with self._lock:
            existing = self._read_unlocked(path)
            created_at = existing.get("created_at") if existing else now
            envelope = {
                "schema": SCHEMA,
                "id": session_id,
                "name": (name or "").strip() or auto_name(self._preview(transcript)),
                "created_at": created_at or now,
                "updated_at": now,
                "messages": sanitize_messages(messages),
                "transcript": transcript,
            }
            tmp = path + ".tmp"
            try:
                os.makedirs(self._dir, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(envelope, fh, ensure_ascii=False)
                os.replace(tmp, path)
                return True
            except Exception as exc:
                logger.error("agent session save failed (%s): %s", session_id, exc)
                try:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                except OSError:
                    pass
                return False

    def rename(self, session_id: str, name: str) -> bool:
        path = self._path(session_id)
        if path is None:
            return False
        clean = (name or "").strip()
        if not clean:
            return False
        with self._lock:
            envelope = self._read_unlocked(path)
            if not envelope:
                return False
            envelope["name"] = clean[: MAX_NAME_LEN * 2]  # custom names may run longer
            envelope["updated_at"] = _now()
            return self._write_unlocked(path, envelope)

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if path is None:
            return False
        with self._lock:
            try:
                os.unlink(path)
                return True
            except FileNotFoundError:
                return False
            except OSError as exc:
                logger.error("agent session delete failed (%s): %s", session_id, exc)
                return False

    # -- read side ----------------------------------------------------------

    def load(self, session_id: str) -> Optional[SessionData]:
        path = self._path(session_id)
        if path is None:
            return None
        with self._lock:
            envelope = self._read_unlocked(path)
        if not envelope:
            return None
        return SessionData(
            id=envelope["id"],
            name=envelope.get("name", ""),
            created_at=envelope.get("created_at", ""),
            updated_at=envelope.get("updated_at", ""),
            messages=envelope.get("messages", []),
            transcript=envelope.get("transcript", []),
        )

    def list_sessions(self) -> List[SessionMeta]:
        """All saved tasks, newest first. A corrupt/unreadable file is
        logged and skipped — one bad file must never hide the rest (the
        from_file filtering discipline)."""
        try:
            names = os.listdir(self._dir)
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.error("agent sessions list failed: %s", exc)
            return []
        metas: List[SessionMeta] = []
        for fname in names:
            if not fname.endswith(".json"):
                continue
            stem = fname[: -len(".json")]
            if not _ID_RE.match(stem):
                continue
            envelope = self._read_unlocked(os.path.join(self._dir, fname))
            if not envelope:
                continue
            metas.append(
                SessionMeta(
                    id=envelope["id"],
                    name=envelope.get("name", "") or stem,
                    created_at=envelope.get("created_at", ""),
                    updated_at=envelope.get("updated_at", ""),
                    message_count=len(envelope.get("messages", [])),
                    preview=self._preview(envelope.get("transcript", [])),
                )
            )
        metas.sort(key=lambda m: (m.updated_at, m.id), reverse=True)
        return metas

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _read_unlocked(path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning("agent session file unreadable, skipping: %s (%s)", path, exc)
            return None
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            return None
        if not _ID_RE.match(str(raw.get("id") or "")):
            return None
        messages = [
            m for m in (raw.get("messages") or [])
            if isinstance(m, dict) and isinstance(m.get("role"), str)
        ]
        transcript = [
            {"k": str(e.get("k")), "t": str(e.get("t") or "")}
            for e in (raw.get("transcript") or [])
            if isinstance(e, dict) and e.get("k") in _VALID_KINDS
        ]
        raw["messages"] = messages
        raw["transcript"] = transcript
        return raw

    def _write_unlocked(self, path: str, envelope: Dict[str, Any]) -> bool:
        tmp = path + ".tmp"
        try:
            os.makedirs(self._dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(envelope, fh, ensure_ascii=False)
            os.replace(tmp, path)
            return True
        except Exception as exc:
            logger.error("agent session write failed (%s): %s", path, exc)
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
            return False

    @staticmethod
    def _preview(transcript: List[Dict[str, str]]) -> str:
        """First user line if any, else the first entry — the sidebar's
        dim second line. HTML-carrying entries (events, search blocks)
        are tag-stripped so markup never shows in the list."""
        text = ""
        for entry in transcript:
            if entry.get("k") in ("user", "search_user"):
                text = entry.get("t", "")
                break
        if not text and transcript:
            text = transcript[0].get("t", "")
        flat = " ".join(_TAG_RE.sub(" ", text).split())
        if len(flat) > MAX_PREVIEW_LEN:
            flat = flat[: MAX_PREVIEW_LEN - 1].rstrip() + "…"
        return flat
