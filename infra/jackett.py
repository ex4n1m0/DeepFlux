"""Jackett service management + source-list synchronization.

DeepFlux searches Jackett per-indexer (private tier first, then public
batches) using the sources list — see agent/tools.py. That list is a snapshot
of Jackett's configured indexers; this module keeps it fresh (startup + daily
while the app runs) and can start Jackett when it's installed but not running
(Windows service first, then the tray/console executable).

All functions are safe to call from a background thread.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests

from config import DeeptorrentConfig, SourceConfig

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 24 * 3600  # daily source-list refresh
START_TIMEOUT = 30                 # Jackett cold start can take a while
PING_TIMEOUT = 3.0                 # cheap t=caps health check

# Per AGENTS.md: Windows subprocess spawns must not flash a console window.
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008

# Jackett Windows install locations + executables. Console first: it serves
# the API in-process even when the (unstartable-without-elevation) service
# is installed but stopped; the tray app can sit idle without serving in
# that state. ProgramData is a real install location on some machines.
_EXE_NAMES = ("JackettConsole.exe", "JackettTray.exe")


def _torznab_url(config: DeeptorrentConfig) -> str:
    path = config.indexer.torznab_path or "/api/v2.0/indexers/all/results/torznab"
    return f"{config.indexer.url.rstrip('/')}{path}"


def is_running(config: DeeptorrentConfig, timeout: float = PING_TIMEOUT) -> bool:
    """True when Jackett answers a cheap t=caps call (validates URL+path+key)."""
    if not config.indexer.api_key:
        return False
    try:
        resp = requests.get(
            _torznab_url(config),
            params={"apikey": config.indexer.api_key, "t": "caps"},
            timeout=timeout,
        )
        return resp.status_code == 200
    except Exception:
        return False


def find_executable(configured_path: str = "") -> Optional[str]:
    """Locate a Jackett executable: explicit config path first, then the
    standard install locations."""
    candidates: List[str] = []
    if configured_path:
        candidates.append(configured_path)
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramData"):
        base = os.environ.get(env_var)
        if base:
            candidates.extend(os.path.join(base, "Jackett", exe) for exe in _EXE_NAMES)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def start(config: DeeptorrentConfig) -> bool:
    """Start Jackett: the Windows service first, then the tray/console exe.

    Returns True when something was launched (service start issued or process
    spawned) — readiness still needs wait_until_ready().
    """
    try:
        proc = subprocess.run(
            ["sc.exe", "start", "Jackett"],
            capture_output=True, text=True, timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0 or "1056" in output:  # 1056 = already running
            logger.info("Jackett service start issued (rc=%d)", proc.returncode)
            return True
        # 1060 = service not installed; 5 = access denied — fall through to exe.
        logger.debug("sc start Jackett failed (rc=%d): %s", proc.returncode, output.strip())
    except Exception as exc:
        logger.debug("sc start Jackett failed: %s", exc)

    exe = find_executable(config.indexer.jackett_path)
    if not exe:
        logger.info("No Jackett executable found (service also unavailable)")
        return False
    try:
        subprocess.Popen(
            [exe],
            creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
            close_fds=True,
        )
        logger.info("Started Jackett executable: %s", exe)
        return True
    except Exception as exc:
        logger.warning("Failed to start Jackett executable %s: %s", exe, exc)
        return False


def wait_until_ready(config: DeeptorrentConfig, timeout: float = START_TIMEOUT) -> bool:
    """Poll is_running until Jackett answers (or the timeout expires)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running(config):
            return True
        time.sleep(1.0)
    return is_running(config)


def fetch_indexers(config: DeeptorrentConfig, timeout: float = 15) -> List[SourceConfig]:
    """Fetch Jackett's *configured* indexers as SourceConfig entries.

    Raises on network/parse errors — callers decide how to report it."""
    resp = requests.get(
        _torznab_url(config),
        params={"apikey": config.indexer.api_key, "t": "indexers"},
        timeout=timeout,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    sources: List[SourceConfig] = []
    for idx in root.findall("indexer"):
        if idx.get("configured", "false") != "true":
            continue
        idx_id = idx.get("id", "")
        title_el = idx.find("title")
        link_el = idx.find("link")
        type_el = idx.find("type")
        sources.append(SourceConfig(
            id=idx_id,
            name=title_el.text if title_el is not None and title_el.text else idx_id,
            url=link_el.text if link_el is not None and link_el.text else "",
            type=type_el.text if type_el is not None and type_el.text else "public",
        ))
    return sources


def merge_sources(existing: List[SourceConfig], fetched: List[SourceConfig],
                  enable_new: bool = True) -> List[SourceConfig]:
    """Merge a fetched snapshot into the source list.

    Existing ids keep their enabled state (a source the user disabled stays
    disabled across re-fetches); brand-new indexers get `enable_new` —
    default True: if it came from Jackett, the user wants it searched.
    """
    existing_enabled = {s.id: s.enabled for s in existing}
    return [
        SourceConfig(id=f.id, name=f.name, url=f.url, type=f.type,
                     enabled=existing_enabled.get(f.id, enable_new))
        for f in fetched
    ]


def sync_sources(config: DeeptorrentConfig, config_path: Optional[str] = None,
                 enable_new: bool = True) -> Optional[Dict[str, Any]]:
    """Fetch + merge + persist the source list. None when nothing was applied
    (fetch failed or Jackett has no configured indexers)."""
    try:
        fetched = fetch_indexers(config)
    except Exception as exc:
        logger.warning("Jackett source fetch failed: %s", exc)
        return None
    if not fetched:
        logger.info("Jackett reports no configured indexers — keeping existing source list")
        return None
    bootstrap = not config.sources.sources
    config.sources.sources = merge_sources(
        config.sources.sources, fetched, enable_new=enable_new or bootstrap)
    config.sources.last_jackett_fetch = time.time()
    enabled = sum(1 for s in config.sources.sources if s.enabled)
    if config_path:
        try:
            config.to_file(config_path)
        except Exception as exc:
            logger.warning("Failed to persist sources after Jackett sync: %s", exc)
    logger.info("Jackett sync: %d indexers (%d enabled, bootstrap=%s)",
                len(fetched), enabled, bootstrap)
    return {"total": len(fetched), "enabled": enabled, "bootstrap": bootstrap}


def maybe_auto_sync(config: DeeptorrentConfig, config_path: Optional[str] = None,
                    force: bool = False) -> Optional[Dict[str, Any]]:
    """Daily-gated auto-sync, safe to call at startup and periodically.

    Sequence: skip when Jackett isn't configured → ping → if down and
    `indexer.auto_start` is on, start it (service/exe) and wait → when the
    source list is due for refresh (daily) or empty (bootstrap), fetch and
    merge it. `force=True` skips the daily gate — used when the user just
    saved/tested Jackett settings and expects an immediate sync; a forced run
    that reaches Jackett but fails to fetch reports sync_failed=True so the
    caller can tell the user. Returns None when there is nothing to report,
    otherwise a result dict with: reachable, started, changed, sync_failed,
    bootstrap, total, enabled.
    """
    if not config.sources.use_jackett or not config.indexer.api_key:
        return None

    result: Dict[str, Any] = {"reachable": False, "started": False, "changed": False,
                              "bootstrap": False, "total": 0, "enabled": 0}
    if not is_running(config):
        if not config.indexer.auto_start:
            return None
        logger.info("Jackett not reachable — attempting to start it")
        if not start(config) or not wait_until_ready(config):
            logger.warning("Jackett could not be started / did not answer in time")
            return result  # reachable=False — caller may notify once
        result["started"] = True
    result["reachable"] = True

    due = force or time.time() - (config.sources.last_jackett_fetch or 0.0) >= SYNC_INTERVAL_SECONDS
    bootstrap = not config.sources.sources
    if not due and not bootstrap:
        return result if result["started"] else None
    sync = sync_sources(config, config_path)
    if sync:
        result.update(sync)
        result["changed"] = True
    elif force:
        # Reachable but nothing was applied (bad key, no configured indexers)
        # — a user-triggered sync should say so instead of staying silent.
        result["sync_failed"] = True
    return result if (result["started"] or result["changed"] or result.get("sync_failed")) else None
