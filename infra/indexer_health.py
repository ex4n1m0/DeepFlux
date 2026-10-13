"""Persistent per-indexer health tracking + Jackett error classification.

Live study 2026-10-13 (this machine, 96 configured Jackett indexers): 22 of
96 indexers failed every search — 19 Cloudflare-challenged (no FlareSolverr
configured, including 1337x, the most popular public indexer), 2 sites down
(Error 522, ~20s to give up), 1 moved domain — and 4 sources in DeepFlux's
snapshot no longer existed in Jackett at all. Before this module the
per-search ``dead`` dict was the ONLY failure memory, so every search
re-queried all of them, re-paid their latency and re-listed their raw
.NET stack traces in the result.

Design:
- A tiny JSON store in ``~/.deeptorrent/indexer_health.json`` (override with
  env ``DF_INDEXER_HEALTH``) records per indexer id: consecutive failures,
  last error kind, cooldown deadline. Atomic writes, thread-safe.
- Cooldown policy: an indexer enters cooldown after COOLDOWN_THRESHOLD
  consecutive failures; the cooldown doubles per further failure up to
  COOLDOWN_MAX (never a permanent disable — Cloudflare walls are
  intermittent, so a cooling indexer is re-probed automatically once its
  cooldown expires).
- "Global" failures (Jackett itself unreachable / bad API key) are NOT
  recorded against individual indexers — a Jackett outage must not
  mass-cooldown the whole list.
- ``classify_indexer_error`` maps a Jackett/torznab error string to a
  stable kind used for the compact agent-facing failure summary.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# Failure kinds (stable identifiers — agent-facing summaries + tests pin them).
KIND_FLARE = "flare"                  # Cloudflare challenge, needs FlareSolverr
KIND_DOWN = "down"                    # tracker site down / bad gateway
KIND_MOVED = "moved"                  # tracker moved domains
KIND_UNKNOWN = "unknown_indexer"      # id no longer exists in Jackett
KIND_AUTH = "auth"                    # login/cookies/403 — private trackers
KIND_TIMEOUT = "timeout"
KIND_RATE = "rate_limit"
KIND_JACKETT = "jackett"              # Jackett itself unreachable/bad key — GLOBAL
KIND_PARSE = "parse"
KIND_OTHER = "other"

# Kinds that are the indexer's own fault and count toward cooldown. Global
# failures (Jackett down) and parse hiccups don't — they say nothing about
# the individual indexer.
INDEXER_FAULT_KINDS = {
    KIND_FLARE, KIND_DOWN, KIND_MOVED, KIND_UNKNOWN, KIND_AUTH,
    KIND_TIMEOUT, KIND_RATE, KIND_OTHER,
}

COOLDOWN_THRESHOLD = 2        # consecutive failures before cooldown starts
COOLDOWN_BASE_SECONDS = 30 * 60   # 30 min after the threshold, then doubling
COOLDOWN_MAX_SECONDS = 8 * 3600   # cap: 8 hours
# Repeated "Unknown indexer" → the source is a zombie (removed from Jackett);
# the caller auto-disables it (never origin=="manual" sources).
ZOMBIE_FAILURES = 3

_KIND_LABELS = {
    KIND_FLARE: "Cloudflare challenge (needs FlareSolverr)",
    KIND_DOWN: "tracker down",
    KIND_MOVED: "moved domains",
    KIND_UNKNOWN: "no longer exists in Jackett",
    KIND_AUTH: "login/auth failed",
    KIND_TIMEOUT: "timed out",
    KIND_RATE: "rate limited",
    KIND_JACKETT: "Jackett unreachable",
    KIND_PARSE: "response parse error",
    KIND_OTHER: "error",
}

_KIND_HINTS = {
    KIND_FLARE: "Configure FlareSolverr (Jackett Settings → FlareSolverr API URL) "
                "to unlock these indexers.",
    KIND_DOWN: "These trackers are unreachable right now.",
    KIND_MOVED: "These trackers moved domains — update Jackett or re-sync later.",
    KIND_UNKNOWN: "These sources no longer exist in Jackett and were disabled.",
    KIND_AUTH: "Private tracker credentials need refreshing in Jackett.",
    KIND_TIMEOUT: "These trackers are responding too slowly.",
    KIND_RATE: "These trackers are rate-limiting searches.",
}


def kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, _KIND_LABELS[KIND_OTHER])


def kind_hint(kind: str) -> str:
    return _KIND_HINTS.get(kind, "")


# Ordered (pattern, kind) — first match wins. Patterns run lowercased.
# Jackett-level connection failures MUST be classified before KIND_TIMEOUT:
# a down Jackett raises "...ConnectionPool... Max retries exceeded...
# (Caused by ConnectTimeoutError...)" whose text contains "timeout" — without
# the ordering every indexer would mass-cooldown on a Jackett outage.
# No bare "429"/"403" substrings: .NET stack traces carry line numbers and
# status codes in unrelated text.
_CLASSIFIERS = (
    (("challenge detected", "flaresolverr"), KIND_FLARE),
    (("seems to be down", "error 522", "error 521", "error 502", "error 503",
      "bad gateway", "service unavailable"), KIND_DOWN),
    (("redirected to another domain", "changing the indexer url"), KIND_MOVED),
    (("unknown indexer", "is not configured"), KIND_UNKNOWN),
    (("error 429", "http 429", "too many requests", "rate limit", "rate-limit"), KIND_RATE),
    # Jackett-level: connection to the Jackett host itself / auth to Jackett.
    # NOTE: no "connectionpool" pattern — a SLOW indexer also surfaces as
    # "HTTPSConnectionPool... Read timed out", which is per-indexer. Only
    # establishment failures ("Max retries exceeded"/"refused") are global.
    (("max retries exceeded", "connection refused", "connectionerror",
      "connection aborted", "invalid api key", "unauthorized api access"), KIND_JACKETT),
    (("timed out", "timeout"), KIND_TIMEOUT),
    (("error 403", "http 403", "unauthorized", "login", "credentials", "cookie"), KIND_AUTH),
    (("parse error", "not well-formed", "no element found"), KIND_PARSE),
)


def classify_indexer_error(text: str) -> str:
    """Map a Jackett/torznab error string to a stable failure kind."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return KIND_OTHER
    for patterns, kind in _CLASSIFIERS:
        if any(p in lowered for p in patterns):
            return kind
    return KIND_OTHER


def default_health_path() -> str:
    override = os.environ.get("DF_INDEXER_HEALTH")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".deeptorrent", "indexer_health.json")


class IndexerHealthStore:
    """Thread-safe per-indexer failure memory persisted to a small JSON file.

    Entries: {id: {"failures": int, "kind": str, "last_error": str,
                   "cooldown_until": epoch, "last_success": epoch}}.
    """

    def __init__(self, path: str = "") -> None:
        self._path = path or default_health_path()
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        self._last_save = 0.0
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                self._entries = {
                    k: v for k, v in raw.items()
                    if isinstance(v, dict) and isinstance(v.get("failures"), int)
                }
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.debug("indexer health load failed (%s) — starting empty", exc)

    def _save(self) -> None:
        # Atomic (temp + replace) like config.to_file: many agent threads
        # may record results concurrently.
        try:
            directory = os.path.dirname(self._path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._entries, fh)
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            self._dirty = False
            self._last_save = time.time()
        except Exception as exc:
            logger.debug("indexer health save failed: %s", exc)

    def flush(self) -> None:
        """Persist pending changes (called when a search completes)."""
        with self._lock:
            if self._dirty:
                self._save()

    def _maybe_save(self) -> None:
        # Records arrive in bursts (one per indexer per search); write at
        # most every few seconds plus the explicit flush().
        if self._dirty and time.time() - self._last_save > 5.0:
            self._save()

    # -- recording --------------------------------------------------------

    def record_failure(self, indexer_id: str, kind: str, error: str = "") -> Dict[str, Any]:
        """Record a failure (skip global kinds — not the indexer's fault).

        Returns the updated entry so callers can react (e.g. zombie count).
        """
        now = time.time()
        with self._lock:
            entry = self._entries.get(indexer_id) or {
                "failures": 0, "kind": "", "last_error": "",
                "cooldown_until": 0.0, "last_success": 0.0,
            }
            if kind not in KIND_JACKETT:
                entry["failures"] = int(entry.get("failures", 0)) + 1
                # "Unknown indexer" is Jackett's own deterministic answer, not
                # an intermittent site problem: NO cooldown, so the failure
                # count keeps climbing every search and the caller's zombie
                # auto-disable actually reaches its threshold (with cooldown
                # the count froze at the threshold and never disabled).
                if (kind != KIND_UNKNOWN
                        and entry["failures"] >= COOLDOWN_THRESHOLD):
                    steps = entry["failures"] - COOLDOWN_THRESHOLD
                    cooldown = min(COOLDOWN_BASE_SECONDS * (2 ** steps),
                                   COOLDOWN_MAX_SECONDS)
                    entry["cooldown_until"] = now + cooldown
            entry["kind"] = kind
            entry["last_error"] = (error or "")[:200]
            self._entries[indexer_id] = entry
            self._dirty = True
            self._maybe_save()
            return dict(entry)

    def record_success(self, indexer_id: str) -> None:
        with self._lock:
            entry = self._entries.get(indexer_id)
            if entry is None:
                # Don't create entries for healthy indexers — the file stays
                # proportional to the problem set, not the source list.
                return
            if entry.get("failures") or entry.get("cooldown_until"):
                entry["failures"] = 0
                entry["cooldown_until"] = 0.0
                entry["last_success"] = time.time()
                self._dirty = True
                self._maybe_save()

    # -- queries ----------------------------------------------------------

    def cooldown_remaining(self, indexer_id: str) -> float:
        with self._lock:
            entry = self._entries.get(indexer_id)
            if not entry:
                return 0.0
            remaining = float(entry.get("cooldown_until", 0.0)) - time.time()
            return max(0.0, remaining)

    def is_cooling(self, indexer_id: str) -> bool:
        return self.cooldown_remaining(indexer_id) > 0.0

    def cooling(self) -> Dict[str, float]:
        """{indexer_id: remaining seconds} for every cooling indexer."""
        out: Dict[str, float] = {}
        with self._lock:
            for indexer_id, entry in self._entries.items():
                remaining = float(entry.get("cooldown_until", 0.0)) - time.time()
                if remaining > 0:
                    out[indexer_id] = remaining
        return out

    def failure_kind(self, indexer_id: str) -> str:
        with self._lock:
            entry = self._entries.get(indexer_id)
            return str(entry.get("kind", "")) if entry else ""

    def consecutive_failures(self, indexer_id: str) -> int:
        with self._lock:
            entry = self._entries.get(indexer_id)
            return int(entry.get("failures", 0)) if entry else 0

    def is_dead_zombie(self, indexer_id: str) -> bool:
        """True once a source has repeatedly drawn 'Unknown indexer' from
        Jackett — it is provably not a Jackett indexer and can never answer
        there again. The search tier uses this to stop querying it through
        Jackett WITHOUT touching the config (the source stays enabled for the
        web-search tier; jackett-origin ids are dropped by the sync instead)."""
        with self._lock:
            entry = self._entries.get(indexer_id)
            return bool(entry
                        and entry.get("kind") == KIND_UNKNOWN
                        and int(entry.get("failures", 0)) >= ZOMBIE_FAILURES)

    def summary(self) -> Dict[str, Any]:
        """Compact snapshot for logs/tests: cooling ids + failure kinds."""
        return {
            "cooling": self.cooling(),
            "kinds": {k: (v.get("kind"), int(v.get("failures", 0)))
                      for k, v in self._entries.items() if v.get("failures")},
        }


_SINGLETON: Optional[IndexerHealthStore] = None
_SINGLETON_LOCK = threading.Lock()


def health_store() -> IndexerHealthStore:
    """Process-wide store (the search path and the GUI share one file)."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = IndexerHealthStore()
        return _SINGLETON


def reset_health_store_singleton() -> None:
    """Test helper — drop the singleton so the next call reloads from disk."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = None
