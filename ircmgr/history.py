"""Encrypted, opt-in persistent IRC transcript storage.

Only routing data needed for bounded lookup (network id, target digest, time and
kind) is stored in plaintext. Targets, nicknames, message bodies, and IRCv3
metadata are encrypted independently with a local Fernet key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken

MAX_QUERY_LIMIT = 1000
MAX_SEARCH_SCAN = 5000
MAX_HISTORY_ROWS = 100000
MAX_NETWORK_LENGTH = 128
MAX_TARGET_LENGTH = 512
MAX_NICK_LENGTH = 256
MAX_TEXT_LENGTH = 8192
MAX_METADATA_LENGTH = 8192


class IRCHistoryStore:
    """Small thread-safe SQLite store with application-level field encryption."""

    def __init__(self, db_path: Optional[str] = None, key_path: Optional[str] = None,
                 retention_days: int = 30) -> None:
        self.db_path = str(db_path or self.default_db_path())
        self.key_path = str(key_path or self.default_key_path())
        self.retention_days = max(1, min(3650, int(retention_days or 30)))
        self._lock = threading.RLock()
        self._writes_since_cleanup = 0
        self._db: Optional[sqlite3.Connection] = None
        key = self._load_or_create_key(Path(self.key_path))
        self._fernet = Fernet(key)
        self._digest_key = hashlib.sha256(base64.urlsafe_b64decode(key)).digest()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.cleanup()

    @staticmethod
    def default_db_path() -> str:
        return str(Path.home() / ".deeptorrent" / "irc_history.sqlite3")

    @staticmethod
    def default_key_path() -> str:
        return str(Path.home() / ".deeptorrent" / "irc_history.key")

    @staticmethod
    def _read_key(path: Path) -> bytes:
        key = path.read_bytes().strip()
        # Validate before returning; malformed/partial files fail closed.
        Fernet(key)
        return key

    @classmethod
    def _load_or_create_key(cls, path: Path) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return cls._read_key(path)

        key = Fernet.generate_key()
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                        dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(key + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            try:
                # A hard-link commit is atomic and never replaces a key won by
                # another process. It is supported by the local Windows/Unix
                # filesystems where the application data directory lives.
                os.link(tmp_name, path)
            except FileExistsError:
                pass
            except OSError:
                # Conservative fallback for filesystems without hard links.
                try:
                    out_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(out_fd, "wb") as handle:
                        handle.write(key + b"\n")
                        handle.flush()
                        os.fsync(handle.fileno())
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

        # Always re-read the committed key. If another process won the race,
        # every process converges on that stable key.
        return cls._read_key(path)

    def _connect(self) -> sqlite3.Connection:
        """One cached connection (WAL + NORMAL) shared under the store lock.

        Opening a fresh connection per message with journal_mode=DELETE and
        synchronous=FULL forced a full fsync commit on the IRC reactor thread
        for every line — busy channels stalled every network's socket. WAL +
        NORMAL keeps the same encrypted-at-rest schema and durability across
        process crashes; only an OS/power crash can lose the last commits.
        """
        if self._db is None:
            conn = sqlite3.connect(self.db_path, timeout=5.0,
                                   check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._db = conn
        return self._db

    def close(self) -> None:
        """Close the cached connection (idempotent). Writes are already committed."""
        with self._lock:
            db, self._db = self._db, None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    event_id TEXT PRIMARY KEY,
                    network TEXT NOT NULL,
                    target_digest TEXT NOT NULL,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    target BLOB NOT NULL,
                    nick BLOB NOT NULL,
                    body BLOB NOT NULL,
                    metadata BLOB NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_irc_history_lookup "
                "ON messages(network, target_digest, ts DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_irc_history_time ON messages(ts DESC)"
            )

    def _encrypt(self, value: str, limit: int) -> bytes:
        return self._fernet.encrypt(value[:limit].encode("utf-8", errors="replace"))

    def _decrypt(self, value: bytes) -> str:
        return self._fernet.decrypt(value).decode("utf-8", errors="replace")

    def _target_digest(self, target: Optional[str]) -> str:
        normalized = (target or "").casefold()[:MAX_TARGET_LENGTH].encode("utf-8")
        return hmac.new(self._digest_key, normalized, hashlib.sha256).hexdigest()

    def add(self, network: str, target: Optional[str], message: Dict[str, Any]) -> None:
        """Persist one normalized event. Duplicate event ids are ignored."""
        event_id = str(message.get("id") or uuid.uuid4().hex)[:64]
        metadata = {
            "account": str(message.get("account") or "")[:MAX_NICK_LENGTH],
            "away": message.get("away") if isinstance(message.get("away"), bool) else None,
            "tags": message.get("tags") if isinstance(message.get("tags"), dict) else {},
        }
        metadata_text = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        if len(metadata_text) > MAX_METADATA_LENGTH:
            # Keep the JSON envelope valid even if a server sends an excessive
            # number of tags; account/away survive while optional tags drop.
            metadata["tags"] = {}
            metadata_text = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        row = (
            event_id,
            str(network)[:MAX_NETWORK_LENGTH],
            self._target_digest(target),
            float(message.get("ts") or time.time()),
            str(message.get("kind") or "")[:32],
            self._encrypt(target or "", MAX_TARGET_LENGTH),
            self._encrypt(str(message.get("nick") or ""), MAX_NICK_LENGTH),
            self._encrypt(str(message.get("text") or ""), MAX_TEXT_LENGTH),
            self._encrypt(metadata_text, MAX_METADATA_LENGTH),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO messages "
                "(event_id, network, target_digest, ts, kind, target, nick, body, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        self._writes_since_cleanup += 1
        if self._writes_since_cleanup >= 100:
            self.cleanup()

    def _decode_row(self, row) -> Optional[Dict[str, Any]]:
        try:
            metadata = json.loads(self._decrypt(row[8]))
            if not isinstance(metadata, dict):
                metadata = {}
            return {
                "id": row[0], "network": row[1], "channel": self._decrypt(row[5]) or None,
                "ts": float(row[3]), "kind": row[4], "nick": self._decrypt(row[6]),
                "text": self._decrypt(row[7]), "account": metadata.get("account", ""),
                "away": metadata.get("away"),
                "tags": metadata.get("tags") if isinstance(metadata.get("tags"), dict) else {},
            }
        except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def get_messages(self, network: str, target: Optional[str], limit: int = 50,
                     since: float = 0.0) -> List[Dict[str, Any]]:
        limit = max(0, min(MAX_QUERY_LIMIT, int(limit)))
        if not limit:
            return []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id, network, target_digest, ts, kind, target, nick, body, metadata "
                "FROM messages WHERE network=? AND target_digest=? AND ts>=? "
                "ORDER BY ts DESC, event_id DESC LIMIT ?",
                (str(network)[:MAX_NETWORK_LENGTH], self._target_digest(target),
                 float(since or 0.0), limit),
            ).fetchall()
        decoded = [item for item in (self._decode_row(row) for row in reversed(rows)) if item]
        # Digest collisions are fantastically unlikely, but decrypt-and-verify
        # avoids ever returning a different target even in that case.
        wanted = (target or "").casefold()
        return [item for item in decoded if (item.get("channel") or "").casefold() == wanted]

    def search(self, query: str, network: Optional[str] = None,
               target: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        query = str(query or "")[:512].casefold()
        limit = max(0, min(MAX_QUERY_LIMIT, int(limit)))
        if not query or not limit:
            return []
        clauses, params = [], []
        if network:
            clauses.append("network=?")
            params.append(str(network)[:MAX_NETWORK_LENGTH])
        if target is not None:
            clauses.append("target_digest=?")
            params.append(self._target_digest(target))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id, network, target_digest, ts, kind, target, nick, body, metadata "
                f"FROM messages{where} ORDER BY ts DESC, event_id DESC LIMIT ?",
                (*params, MAX_SEARCH_SCAN),
            ).fetchall()
        wanted = target.casefold() if target is not None else None
        hits: List[Dict[str, Any]] = []
        for row in rows:
            item = self._decode_row(row)
            if not item or (wanted is not None
                            and (item.get("channel") or "").casefold() != wanted):
                continue
            if query in item["text"].casefold() or query in item["nick"].casefold():
                hits.append(item)
                if len(hits) >= limit:
                    break
        hits.reverse()
        return hits

    def cleanup(self, now: Optional[float] = None) -> None:
        cutoff = float(now if now is not None else time.time()) - self.retention_days * 86400
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
            conn.execute(
                "DELETE FROM messages WHERE event_id NOT IN "
                "(SELECT event_id FROM messages ORDER BY ts DESC, event_id DESC LIMIT ?)",
                (MAX_HISTORY_ROWS,),
            )
        self._writes_since_cleanup = 0

    def clear(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM messages")
            # Keep the bounded file compact after an explicit privacy action.
            with self._connect() as conn:
                conn.execute("VACUUM")
