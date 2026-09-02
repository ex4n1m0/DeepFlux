from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class BrowserHistory:
    def __init__(self, path: str, retention_days: int = 90) -> None:
        self.path = path
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS visits ("
                "url TEXT PRIMARY KEY, title TEXT NOT NULL, visited_at REAL NOT NULL, visit_count INTEGER NOT NULL)"
            )

    @staticmethod
    def _sanitize_url(url: str) -> str:
        parsed = urlsplit(url)
        query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if any(part in key.lower() for part in ("token", "secret", "key", "auth", "signature", "password")):
                value = "redacted"
            query.append((key, value))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))

    @staticmethod
    def _allowed(url: str) -> bool:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)

    def record(self, url: str, title: str = "") -> None:
        if not self._allowed(url):
            return
        url = self._sanitize_url(url)
        now = time.time()
        cutoff = now - self.retention_days * 86400
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO visits(url, title, visited_at, visit_count) VALUES(?, ?, ?, 1) "
                "ON CONFLICT(url) DO UPDATE SET title=excluded.title, visited_at=excluded.visited_at, "
                "visit_count=visits.visit_count+1",
                (url, title or url, now),
            )
            connection.execute("DELETE FROM visits WHERE visited_at < ?", (cutoff,))

    def suggestions(self, query: str = "", limit: int = 20) -> List[Dict[str, object]]:
        query = (query or "").strip()
        pattern = f"%{query}%"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT url, title, visited_at, visit_count FROM visits "
                "WHERE ? = '' OR url LIKE ? OR title LIKE ? "
                "ORDER BY visited_at DESC LIMIT ?",
                (query, pattern, pattern, max(1, min(int(limit), 100))),
            ).fetchall()
        return [
            {"url": row[0], "title": row[1], "visited_at": row[2], "visit_count": row[3]}
            for row in rows
        ]

    def clear(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM visits")
