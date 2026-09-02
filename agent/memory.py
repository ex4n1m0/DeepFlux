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
import hashlib
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

SCOPES = ("user", "fact", "note")

# Prompt-injection budgets (chars). Files on disk grow unbounded; only the
# injected copy is capped so old context can't crowd out the conversation.
USER_BUDGET = 2000
MEMORY_BUDGET = 3000


def _contains_secret(content: str) -> bool:
    patterns = (
        r"(?i)(api[_ -]?key|password|passphrase|secret|access[_ -]?token)\s*[:=]\s*\S+",
        r"(?i)\b(?:sk|pplx|ghp|github_pat|xox[baprs])[-_][a-z0-9_-]{12,}\b",
        r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )
    return any(re.search(pattern, content) for pattern in patterns)


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

    @staticmethod
    def _entry_id(scope: str, line: str) -> str:
        match = re.match(r"^- \[id:([a-z0-9-]+)\] ", line, re.IGNORECASE)
        if match:
            return match.group(1)
        digest = hashlib.sha256(f"{scope}:{line}".encode("utf-8")).hexdigest()[:12]
        return f"legacy-{digest}"

    @classmethod
    def _entry_content(cls, line: str) -> str:
        return re.sub(r"^- \[id:[a-z0-9-]+\] ", "", line, flags=re.IGNORECASE).strip()

    @classmethod
    def _clean_prompt_text(cls, text: str) -> str:
        return "\n".join(
            f"- {cls._entry_content(line)}" if line.strip().startswith("- ") else line
            for line in text.splitlines()
        )

    def _memory_files(self, scope: str = "") -> List[tuple[str, str]]:
        files: List[tuple[str, str]] = []
        if scope in ("", "user"):
            files.append(("user", self.user_file))
        if scope in ("", "fact"):
            files.append(("fact", self.memory_file))
        if scope in ("", "note"):
            daily_dir = os.path.join(self.root, "daily")
            try:
                files.extend(
                    ("note", os.path.join(daily_dir, name))
                    for name in sorted(os.listdir(daily_dir), reverse=True)
                    if name.endswith(".md")
                )
            except OSError:
                pass
        return files

    @staticmethod
    def _write_lines(path: str, lines: List[str]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        os.replace(temp_path, path)

    def load_core(self) -> Dict[str, str]:
        """Budgeted contents of the curated files, for prompt injection."""
        with self._lock:
            user = self._clean_prompt_text(self._read(self.user_file)).strip()
            memory = self._clean_prompt_text(self._read(self.memory_file)).strip()
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
        if _contains_secret(content):
            return {"success": False, "error": "Memory cannot store credentials or secrets"}
        if scope not in SCOPES:
            scope = "fact"
        path = self._file_for_scope(scope)
        today = datetime.date.today().isoformat()
        entry_id = uuid.uuid4().hex[:12]
        value = content if scope != "note" else f"{today}: {content}"
        line = f"- [id:{entry_id}] {value}"

        with self._lock:
            existing = self._read(path)
            existing_values = {
                self._entry_content(existing_line).lower()
                for existing_line in existing.splitlines()
                if existing_line.strip().startswith("- ")
            }
            if value.lower() in existing_values:
                duplicate_id = next(
                    self._entry_id(scope, existing_line)
                    for existing_line in existing.splitlines()
                    if existing_line.strip().startswith("- ")
                    and self._entry_content(existing_line).lower() == value.lower()
                )
                return {"success": True, "id": duplicate_id, "scope": scope, "path": path, "duplicate": True}
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
        return {"success": True, "id": entry_id, "scope": scope, "path": path, "duplicate": False}

    def list_entries(self, scope: str = "", max_results: int = 200) -> Dict[str, Any]:
        if scope and scope not in SCOPES:
            return {"success": False, "error": f"Unknown scope: {scope}", "results": []}
        results = []
        with self._lock:
            for entry_scope, path in self._memory_files(scope):
                for line in self._read(path).splitlines():
                    line = line.strip()
                    if not line.startswith("- "):
                        continue
                    value = self._entry_content(line)
                    created = ""
                    content = value
                    if entry_scope == "note":
                        match = re.match(r"^(\d{4}-\d{2}-\d{2}):\s*(.*)$", value)
                        if match:
                            created, content = match.groups()
                    results.append({
                        "id": self._entry_id(entry_scope, line),
                        "scope": entry_scope,
                        "source": os.path.basename(path),
                        "created": created,
                        "content": content,
                    })
                    if len(results) >= max_results:
                        return {"success": True, "count": len(results), "results": results, "truncated": True}
        return {"success": True, "count": len(results), "results": results, "truncated": False}

    def edit(self, memory_id: str, content: str) -> Dict[str, Any]:
        content = " ".join((content or "").split())
        if not content:
            return {"success": False, "error": "empty content"}
        if _contains_secret(content):
            return {"success": False, "error": "Memory cannot store credentials or secrets"}
        with self._lock:
            for scope, path in self._memory_files():
                lines = self._read(path).splitlines()
                for index, line in enumerate(lines):
                    if not line.strip().startswith("- ") or self._entry_id(scope, line.strip()) != memory_id:
                        continue
                    value = content
                    if scope == "note":
                        old_value = self._entry_content(line.strip())
                        match = re.match(r"^(\d{4}-\d{2}-\d{2}):", old_value)
                        day = match.group(1) if match else datetime.date.today().isoformat()
                        value = f"{day}: {content}"
                    lines[index] = f"- [id:{memory_id}] {value}"
                    try:
                        self._write_lines(path, lines)
                    except OSError as exc:
                        return {"success": False, "error": str(exc)}
                    return {"success": True, "id": memory_id, "scope": scope, "content": content}
        return {"success": False, "error": f"Memory not found: {memory_id}"}

    def forget(self, memory_id: str) -> Dict[str, Any]:
        with self._lock:
            for scope, path in self._memory_files():
                lines = self._read(path).splitlines()
                for index, line in enumerate(lines):
                    if not line.strip().startswith("- ") or self._entry_id(scope, line.strip()) != memory_id:
                        continue
                    content = self._entry_content(line.strip())
                    del lines[index]
                    try:
                        self._write_lines(path, lines)
                    except OSError as exc:
                        return {"success": False, "error": str(exc)}
                    return {"success": True, "id": memory_id, "scope": scope, "content": content}
        return {"success": False, "error": f"Memory not found: {memory_id}"}

    # -- search --------------------------------------------------------------

    def search(self, query: str, max_results: int = 8) -> Dict[str, Any]:
        """Keyword search over every memory file (token-overlap scoring)."""
        tokens = [t for t in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(t) > 2]
        if not tokens:
            return {"success": False, "error": "query too short", "results": []}

        hits: List[Dict[str, Any]] = []
        with self._lock:
            for scope, path in self._memory_files():
                text = self._read(path)
                if not text:
                    continue
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    content = self._entry_content(line)
                    low = content.lower()
                    score = sum(1 for t in tokens if t in low)
                    if score:
                        hits.append({
                            "id": self._entry_id(scope, line),
                            "scope": scope,
                            "score": score,
                            "source": os.path.basename(path),
                            "line": content[:500],
                        })
        hits.sort(key=lambda h: h["score"], reverse=True)
        hits = hits[:max_results]
        return {"success": True, "count": len(hits), "results": hits}
