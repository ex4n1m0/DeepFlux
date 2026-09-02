"""SQLite cache for the IPTV subsystem.

Stores parsed playlists, metadata, EPG, favorites, and recently-watched so
restarts are instant and network failures degrade gracefully. Reuses the
app's data directory (``~/.deeptorrent``).

The cache is thread-safe via a per-thread connection backed by a single file.
Write-heavy operations use a single transaction to keep things fast.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    source_id TEXT PRIMARY KEY,
    updated_at REAL NOT NULL,
    url_tvg TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,           -- "<section>:<clean_title>:<year>"
    section TEXT,
    title TEXT,
    year TEXT,
    provider TEXT,
    updated_at REAL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS epg (
    channel_id TEXT,                -- tvg-id
    start INTEGER,
    "end" INTEGER,
    title TEXT,
    "desc" TEXT,
    url TEXT DEFAULT '',            -- which XMLTV source this row came from
    PRIMARY KEY (channel_id, start)
);

CREATE TABLE IF NOT EXISTS epg_meta (
    url TEXT PRIMARY KEY,
    updated_at REAL,
    channel_count INTEGER
);

-- The guide's own <channel> directory (id + display name). Needed for
-- name-based matching: playlists without matching tvg-ids still get EPG by
-- normalizing their channel names against these display names.
CREATE TABLE IF NOT EXISTS epg_channels (
    url TEXT,
    channel_id TEXT,
    name TEXT,
    PRIMARY KEY (url, channel_id)
);

CREATE TABLE IF NOT EXISTS favorites (
    source_id TEXT,
    item_id TEXT,
    section TEXT,
    PRIMARY KEY (source_id, item_id)
);

CREATE TABLE IF NOT EXISTS recent (
    source_id TEXT,
    item_id TEXT,
    section TEXT,
    name TEXT,
    url TEXT,
    watched_at REAL,
    PRIMARY KEY (source_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_recent_time ON recent(watched_at DESC);

-- Durable VOD/episode playback state. Kept separate from ``recent`` because
-- opening an item and actually watching it are different events.
CREATE TABLE IF NOT EXISTS watch_progress (
    source_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    position_seconds REAL NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    watched INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (source_id, item_id)
);
"""


class IPTVCache:
    """Thin SQLite wrapper used across the IPTV subsystem."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "iptv_cache.sqlite3")
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._init_schema()

    # -- connection handling -------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._init_lock:
            conn = self._conn()
            conn.executescript(_SCHEMA)
            # Migration: older databases lack the epg.url column.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(epg)").fetchall()}
            if "url" not in cols:
                conn.execute("ALTER TABLE epg ADD COLUMN url TEXT DEFAULT ''")

            # ``CREATE TABLE IF NOT EXISTS`` handles databases predating watch
            # progress. These additive checks also make upgrades safe from
            # short-lived development schemas that had only some columns.
            progress_cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(watch_progress)").fetchall()
            }
            progress_migrations = {
                "position_seconds": "REAL NOT NULL DEFAULT 0",
                "duration_seconds": "REAL NOT NULL DEFAULT 0",
                "watched": "INTEGER NOT NULL DEFAULT 0",
                "updated_at": "REAL NOT NULL DEFAULT 0",
            }
            for name, declaration in progress_migrations.items():
                if name not in progress_cols:
                    conn.execute(
                        f"ALTER TABLE watch_progress ADD COLUMN {name} {declaration}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_watch_progress_updated "
                "ON watch_progress(updated_at DESC)"
            )
            conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- playlists -----------------------------------------------------------
    def save_playlist(self, source_id: str, payload: Dict[str, Any], url_tvg: str = "") -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO playlists(source_id, updated_at, url_tvg, payload) VALUES(?,?,?,?)",
            (source_id, time.time(), url_tvg, json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()

    def load_playlist(self, source_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT payload FROM playlists WHERE source_id=?", (source_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except Exception:
            return None

    def playlist_age(self, source_id: str) -> Optional[float]:
        row = self._conn().execute(
            "SELECT updated_at FROM playlists WHERE source_id=?", (source_id,)
        ).fetchone()
        return row["updated_at"] if row else None

    # -- metadata ------------------------------------------------------------
    def save_metadata(self, key: str, section: str, title: str, year: str, provider: str, payload: Dict[str, Any]) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, section, title, year, provider, updated_at, payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (key, section, title, year, provider, time.time(), json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()

    def load_metadata(self, key: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute("SELECT payload FROM metadata WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except Exception:
            return None

    def metadata_count(self) -> int:
        row = self._conn().execute("SELECT COUNT(*) AS n FROM metadata").fetchone()
        return int(row["n"]) if row else 0

    def clear_metadata(self) -> int:
        """Delete every metadata row (lookups AND negative-cache entries).

        Scoped to the metadata table only — playlists, EPG, favorites and
        watch history are user data and survive a cache clear."""
        cur = self._conn().execute("DELETE FROM metadata")
        self._conn().commit()
        return cur.rowcount

    def vacuum(self) -> None:
        """Reclaim freed pages. Big deletes (clear_metadata) otherwise leave
        the .sqlite3 file at its high-water mark forever."""
        try:
            self._conn().execute("VACUUM")
        except sqlite3.Error as exc:
            logger.debug("VACUUM failed: %s", exc)

    # -- EPG -----------------------------------------------------------------
    def save_epg(self, url: str, programs: List[Dict[str, Any]],
                 channels: Optional[List[tuple]] = None) -> None:
        conn = self._conn()
        # Only replace rows belonging to this EPG source — other sources'
        # programs must survive an update.
        conn.execute("DELETE FROM epg WHERE url=?", (url,))
        rows = [
            (p["channel_id"], int(p["start"]), int(p["end"]), p.get("title", ""), p.get("desc", ""), url)
            for p in programs
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO epg(channel_id, start, \"end\", title, \"desc\", url) VALUES(?,?,?,?,?,?)",
            rows,
        )
        if channels is not None:
            conn.execute("DELETE FROM epg_channels WHERE url=?", (url,))
            conn.executemany(
                "INSERT OR REPLACE INTO epg_channels(url, channel_id, name) VALUES(?,?,?)",
                [(url, cid, name) for cid, name in channels],
            )
        conn.execute(
            "INSERT OR REPLACE INTO epg_meta(url, updated_at, channel_count) VALUES(?,?,?)",
            (url, time.time(), len({r[0] for r in rows})),
        )
        conn.commit()

    def epg_has_channel(self, channel_id: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM epg WHERE channel_id=? LIMIT 1", (channel_id,)
        ).fetchone()
        return row is not None

    def epg_channels(self) -> List[tuple]:
        """Every (channel_id, display_name) the guides declare, all sources."""
        return [
            (r["channel_id"], r["name"])
            for r in self._conn().execute(
                "SELECT channel_id, name FROM epg_channels"
            ).fetchall()
        ]

    def epg_programmes(self, channel_id: str, from_ts: float, to_ts: float,
                       limit: int = 30) -> List[Dict[str, Any]]:
        """Programmes overlapping [from_ts, to_ts), start order — the guide view."""
        return [
            {"start": r["start"], "end": r["end"], "title": r["title"],
             "desc": r["desc"]}
            for r in self._conn().execute(
                "SELECT start, \"end\", title, \"desc\" FROM epg "
                "WHERE channel_id=? AND \"end\">? AND start<? "
                "ORDER BY start LIMIT ?",
                (channel_id, from_ts, to_ts, limit),
            ).fetchall()
        ]

    def epg_age(self, url: str) -> Optional[float]:
        """updated_at of the last successful fetch for ``url``, else None."""
        row = self._conn().execute(
            "SELECT updated_at FROM epg_meta WHERE url=?", (url,)
        ).fetchone()
        return row["updated_at"] if row else None

    def epg_now_next(self, channel_id: str, now: Optional[float] = None) -> Dict[str, Any]:
        """Now/next titles plus their epoch times.

        The epoch fields let the UI render programme times in the SYSTEM local
        timezone (``time.localtime``) — the XMLTV offsets are already baked in
        at parse time, so the display always matches the user's clock."""
        now = now or time.time()
        row = self._conn().execute(
            "SELECT title, start, \"end\" FROM epg WHERE channel_id=? AND start<=? AND \"end\">? "
            "ORDER BY start LIMIT 1",
            (channel_id, now, now),
        ).fetchone()
        nxt = self._conn().execute(
            "SELECT title, start, \"end\" FROM epg WHERE channel_id=? AND start>? ORDER BY start LIMIT 1",
            (channel_id, now),
        ).fetchone()
        return {
            "now": row["title"] if row else "",
            "next": nxt["title"] if nxt else "",
            "now_start": row["start"] if row else 0,
            "now_end": row["end"] if row else 0,
            "next_start": nxt["start"] if nxt else 0,
            "next_end": nxt["end"] if nxt else 0,
        }

    # -- favorites -----------------------------------------------------------
    def add_favorite(self, source_id: str, item_id: str, section: str) -> None:
        self._conn().execute(
            "INSERT OR IGNORE INTO favorites(source_id, item_id, section) VALUES(?,?,?)",
            (source_id, item_id, section),
        )
        self._conn().commit()

    def remove_favorite(self, source_id: str, item_id: str) -> None:
        self._conn().execute(
            "DELETE FROM favorites WHERE source_id=? AND item_id=?", (source_id, item_id)
        )
        self._conn().commit()

    def favorites(self, source_id: str) -> List[tuple]:
        return [
            (r["item_id"], r["section"])
            for r in self._conn().execute(
                "SELECT item_id, section FROM favorites WHERE source_id=?", (source_id,)
            ).fetchall()
        ]

    def is_favorite(self, source_id: str, item_id: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM favorites WHERE source_id=? AND item_id=?", (source_id, item_id)
        ).fetchone()
        return row is not None

    # -- recent --------------------------------------------------------------
    def add_recent(self, source_id: str, item_id: str, section: str, name: str, url: str) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO recent(source_id, item_id, section, name, url, watched_at) "
            "VALUES(?,?,?,?,?,?)",
            (source_id, item_id, section, name, url, time.time()),
        )
        conn.commit()

    def recent(self, source_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return [
            {
                "item_id": r["item_id"],
                "section": r["section"],
                "name": r["name"],
                "url": r["url"],
                "watched_at": r["watched_at"],
            }
            for r in self._conn().execute(
                "SELECT item_id, section, name, url, watched_at FROM recent "
                "WHERE source_id=? ORDER BY watched_at DESC LIMIT ?",
                (source_id, limit),
            ).fetchall()
        ]

    # -- watch progress ------------------------------------------------------
    def save_watch_progress(
        self,
        source_id: str,
        item_id: str,
        position_seconds: float,
        duration_seconds: float,
        watched: Optional[bool] = None,
    ) -> None:
        """Upsert a VOD/episode checkpoint.

        ``watched=None`` preserves an existing explicit watched state. This is
        important when somebody replays the first few minutes of an item they
        already completed; periodic checkpoints must not silently unwatch it.
        """
        if not source_id or not item_id:
            return
        conn = self._conn()
        previous = conn.execute(
            "SELECT watched FROM watch_progress WHERE source_id=? AND item_id=?",
            (source_id, item_id),
        ).fetchone()
        watched_value = (
            int(bool(watched)) if watched is not None
            else int(previous["watched"]) if previous is not None else 0
        )
        conn.execute(
            "INSERT OR REPLACE INTO watch_progress("
            "source_id, item_id, position_seconds, duration_seconds, watched, updated_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                source_id,
                item_id,
                max(0.0, float(position_seconds or 0.0)),
                max(0.0, float(duration_seconds or 0.0)),
                watched_value,
                time.time(),
            ),
        )
        conn.commit()

    def watch_progress(self, source_id: str, item_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT position_seconds, duration_seconds, watched, updated_at "
            "FROM watch_progress WHERE source_id=? AND item_id=?",
            (source_id, item_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "position": float(row["position_seconds"] or 0.0),
            "duration": float(row["duration_seconds"] or 0.0),
            "watched": bool(row["watched"]),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    def set_watched(self, source_id: str, item_id: str, watched: bool) -> None:
        existing = self.watch_progress(source_id, item_id) or {}
        self.save_watch_progress(
            source_id,
            item_id,
            existing.get("position", 0.0),
            existing.get("duration", 0.0),
            watched=watched,
        )

    # -- maintenance ---------------------------------------------------------
    def prune_metadata(self, max_age_days: int = 30) -> int:
        cutoff = time.time() - max_age_days * 86400
        cur = self._conn().execute("DELETE FROM metadata WHERE updated_at < ?", (cutoff,))
        self._conn().commit()
        return cur.rowcount

    def clear_all(self) -> None:
        conn = self._conn()
        for t in (
            "playlists", "metadata", "epg", "epg_meta", "favorites", "recent",
            "watch_progress",
        ):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()


def default_data_dir() -> str:
    return str(Path.home() / ".deeptorrent" / "iptv")
