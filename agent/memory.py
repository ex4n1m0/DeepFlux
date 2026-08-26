"""Persistent local memory for the agent — plain Markdown files on disk.

Design borrowed from OpenClaw's memory model, scaled down to a single user:

- ``USER.md``             — stable user preferences/profile, written as
                            imperative directives ("Prefer ...", "Never ...").
- ``MEMORY.md``           — curated long-term facts, decisions, lessons.
- ``daily/YYYY-MM-DD.md`` — episodic working notes (what happened today).

The curated files (USER.md / MEMORY.md) are injected into the system prompt
with a character budget; daily notes are only reachable via search. All state
lives in these files — there is no hidden state.
"""
from __future__ import annotations

import datetime
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

SCOPES = ("user", "fact", "note")

# Prompt-injection budgets (chars). Files on disk grow unbounded; only the
# injected copy is capped so old context can't crowd out the conversation.
USER_BUDGET = 2000
MEMORY_BUDGET = 3000


class MemoryStore:
    """Thread-safe markdown memory store rooted at ``~/.deeptorrent/memory``."""

    def __init__(self, root: str = "") -> None:
        self.root = root or str(Path.home() / ".deeptorrent" / "memory")
        self._lock = threading.Lock()

    # -- paths ---------------------------------------------------------------

    @property
    def user_file(self) -> str:
        return os.path.join(self.root, "USER.md")

    @property
    def memory_file(self) -> str:
        return os.path.join(self.root, "MEMORY.md")

    def _daily_file(self, day: datetime.date | None = None) -> str:
        day = day or datetime.date.today()
        return os.path.join(self.root, "daily", f"{day.isoformat()}.md")

    def _file_for_scope(self, scope: str) -> str:
        if scope == "user":
            return self.user_file
        if scope == "note":
            return self._daily_file()
        return self.memory_file

    # -- read ----------------------------------------------------------------

    @staticmethod
    def _read(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def load_core(self) -> Dict[str, str]:
        """Budgeted contents of the curated files, for prompt injection."""
        with self._lock:
            user = self._read(self.user_file).strip()
            memory = self._read(self.memory_file).strip()
        if len(user) > USER_BUDGET:
            user = user[:USER_BUDGET].rsplit("\n", 1)[0] + "\n… (truncated — see USER.md)"
        if len(memory) > MEMORY_BUDGET:
            memory = memory[:MEMORY_BUDGET].rsplit("\n", 1)[0] + "\n… (truncated — see MEMORY.md)"
        return {"user": user, "memory": memory}

    def prompt_section(self) -> str:
        """Rendered markdown block for the system prompt (empty when no memory)."""
        core = self.load_core()
        if not core["user"] and not core["memory"]:
            return ""
        parts = ["Long-term memory (persistent, stored on the user's drive):"]
        if core["user"]:
            parts.append(f"### About the user (USER.md)\n{core['user']}")
        if core["memory"]:
            parts.append(f"### Remembered facts (MEMORY.md)\n{core['memory']}")
        return "\n\n".join(parts)

    # -- write ---------------------------------------------------------------

    def save(self, content: str, scope: str = "fact") -> Dict[str, Any]:
        """Append a memory entry to the file for ``scope`` (deduped per line).

        User-scope entries are written as imperative directives; callers should
        phrase them as "Always/Never/Prefer ..." where possible.
        """
        content = " ".join((content or "").split())
        if not content:
            return {"success": False, "error": "empty content"}
        if scope not in SCOPES:
            scope = "fact"
        path = self._file_for_scope(scope)
        today = datetime.date.today().isoformat()
        line = f"- {content}" if scope != "note" else f"- {today}: {content}"

        with self._lock:
            existing = self._read(path)
            if content.lower() in existing.lower():
                return {"success": True, "scope": scope, "path": path, "duplicate": True}
            header = ""
            if not existing:
                header = {
                    "user": "# USER.md — stable preferences & profile\n\n",
                    "fact": "# MEMORY.md — durable facts, decisions, lessons\n\n",
                    "note": f"# Notes for {today}\n\n",
                }[scope]
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    if header:
                        f.write(header)
                    elif not existing.endswith("\n"):
                        f.write("\n")
                    f.write(line + "\n")
            except OSError as exc:
                logger.warning("memory save failed: %s", exc)
                return {"success": False, "error": str(exc)}
        return {"success": True, "scope": scope, "path": path, "duplicate": False}

    # -- search --------------------------------------------------------------

    def search(self, query: str, max_results: int = 8) -> Dict[str, Any]:
        """Keyword search over every memory file (token-overlap scoring)."""
        tokens = [t for t in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(t) > 2]
        if not tokens:
            return {"success": False, "error": "query too short", "results": []}

        files = [self.user_file, self.memory_file]
        daily_dir = os.path.join(self.root, "daily")
        try:
            files.extend(
                os.path.join(daily_dir, name)
                for name in sorted(os.listdir(daily_dir), reverse=True)
                if name.endswith(".md")
            )
        except OSError:
            pass

        hits: List[Dict[str, Any]] = []
        with self._lock:
            for path in files:
                text = self._read(path)
                if not text:
                    continue
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    low = line.lower()
                    score = sum(1 for t in tokens if t in low)
                    if score:
                        hits.append({
                            "score": score,
                            "source": os.path.basename(path),
                            "line": line[:500],
                        })
        hits.sort(key=lambda h: h["score"], reverse=True)
        hits = hits[:max_results]
        return {"success": True, "count": len(hits), "results": hits}
