"""Jackett service management + source-list synchronization.

DeepFlux searches Jackett per-indexer (private tier first, then public
batches) using the sources list — see agent/tools.py. That list is a snapshot
of Jackett's configured indexers; this module keeps it fresh (startup + daily
while the app runs) and can start Jackett when it's installed but not running
(Windows service first, then the tray/console executable).

All functions are safe to call from a background thread.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
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
# getattr() keeps these 0 on POSIX, where Popen() rejects nonzero flags.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)

_IS_WINDOWS = os.name == "nt"

# Jackett Windows install locations + executables. Console first: it serves
# the API in-process even when the (unstartable-without-elevation) service
# is installed but stopped; the tray app can sit idle without serving in
# that state. ProgramData is a real install location on some machines.
_EXE_NAMES = ("JackettConsole.exe", "JackettTray.exe")

# macOS: the official Jackett .app bundle's console host (serves in-process).
_MACOS_EXE_CANDIDATES = (
    "/Applications/Jackett.app/Contents/MacOS/jackett",
    os.path.expanduser("~/Applications/Jackett.app/Contents/MacOS/jackett"),
)

# Linux: the official installer script puts the console binary at /opt/jackett
# (root/systemd install) or ~/.config/Jackett (user install); it serves the
# API in-process just like the Windows console host. Starting the systemd
# service needs elevation, so DeepFlux only ever spawns the binary directly.
# The ~/.local/share path is our own user-mode install from the one-click
# setup (infra/jackett_setup.py::_install_linux_jackett).
_LINUX_EXE_CANDIDATES = (
    "/opt/jackett/jackett",
    os.path.expanduser("~/.local/share/Jackett/Jackett/jackett"),
    os.path.expanduser("~/.config/Jackett/jackett"),
)


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
    if _IS_WINDOWS:
        for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramData"):
            base = os.environ.get(env_var)
            if base:
                candidates.extend(os.path.join(base, "Jackett", exe) for exe in _EXE_NAMES)
    elif sys.platform == "darwin":
        candidates.extend(_MACOS_EXE_CANDIDATES)
    else:
        candidates.extend(_LINUX_EXE_CANDIDATES)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def start(config: DeeptorrentConfig) -> bool:
    """Start Jackett: the Windows service first, then the tray/console exe.

    Returns True when something was launched (service start issued or process
    spawned) — readiness still needs wait_until_ready().
    """
    if _IS_WINDOWS:
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


def fetch_indexers_detailed(config: DeeptorrentConfig, timeout: float = 15
                            ) -> tuple[List[SourceConfig], set]:
    """Fetch Jackett's configured indexers PLUS every definition id.

    Returns (sources, definition_ids). Each source is marked
    ``origin="jackett"`` and carries its torznab ``<caps>`` category ids
    (free from the same t=indexers response — used to skip e.g. anime-only
    indexers for movie queries). definition_ids covers ALL of Jackett's
    known indexers including unconfigured ones (~650): an id that is in
    our snapshot but MISSING from that set was removed from Jackett itself
    — a zombie that errors on every search forever.

    Raises on network/parse errors — callers decide how to report it."""
    resp = requests.get(
        _torznab_url(config),
        params={"apikey": config.indexer.api_key, "t": "indexers"},
        timeout=timeout,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    sources: List[SourceConfig] = []
    definition_ids: set = set()
    for idx in root.findall("indexer"):
        idx_id = idx.get("id", "")
        if not idx_id:
            continue
        definition_ids.add(idx_id)
        if idx.get("configured", "false") != "true":
            continue
        title_el = idx.find("title")
        link_el = idx.find("link")
        type_el = idx.find("type")
        caps: List[int] = []
        caps_el = idx.find("caps")
        if caps_el is not None:
            for cat in caps_el.iter("category"):
                try:
                    caps.append(int(cat.get("id", "")))
                except ValueError:
                    continue
        sources.append(SourceConfig(
            id=idx_id,
            name=title_el.text if title_el is not None and title_el.text else idx_id,
            url=link_el.text if link_el is not None and link_el.text else "",
            type=type_el.text if type_el is not None and type_el.text else "public",
            origin="jackett",
            torznab_caps=caps,
        ))
    return sources, definition_ids


def fetch_indexers(config: DeeptorrentConfig, timeout: float = 15) -> List[SourceConfig]:
    """Fetch Jackett's *configured* indexers as SourceConfig entries."""
    return fetch_indexers_detailed(config, timeout)[0]


def merge_sources(existing: List[SourceConfig], fetched: List[SourceConfig],
                  enable_new: bool = True) -> List[SourceConfig]:
    """Merge a fetched snapshot into the source list.

    Existing ids keep their enabled state (a source the user disabled stays
    disabled across re-fetches) plus their per-source popularity override;
    matched ids are Jackett-managed by definition, so their origin becomes
    "jackett" and caps refresh from the snapshot. Brand-new indexers get
    `enable_new` — default True: if it came from Jackett, the user wants it
    searched. Manually added sources (origin "manual") are NOT in the
    Jackett snapshot and must survive the merge with all their fields —
    returning only the fetched entries silently deleted them on every sync
    (review 2026-09-15)."""
    existing_state = {s.id: s for s in existing}
    merged = []
    for f in fetched:
        prior = existing_state.get(f.id)
        merged.append(SourceConfig(
            id=f.id, name=f.name, url=f.url, type=f.type,
            enabled=prior.enabled if prior is not None else enable_new,
            popularity=prior.popularity if prior is not None else 0,
            categories=list(prior.categories) if prior is not None else [],
            origin=f.origin,
            torznab_caps=list(f.torznab_caps),
        ))
    fetched_ids = {f.id for f in fetched}
    for s in existing:
        if s.id in fetched_ids:
            continue
        merged.append(SourceConfig(
            id=s.id, name=s.name, url=s.url, type=s.type, enabled=s.enabled,
            popularity=s.popularity, categories=list(s.categories),
            origin=s.origin, torznab_caps=list(s.torznab_caps),
        ))
    return merged


def default_definitions_path() -> str:
    override = os.environ.get("DF_JACKETT_DEFINITIONS")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".deeptorrent", "jackett_definitions.json")


def save_definition_ids(definition_ids: set, path: str = "") -> None:
    """Persist Jackett's full definition-id list (configured + unconfigured,
    ~650 ids) as a sidecar. Searches use it to fast-path zombie sources:
    a legacy (""-origin) source whose id is absent from this set no longer
    exists in Jackett — probing it once per search lets the health store's
    3-strike auto-disable reach it without waiting for a tail-reaching sweep."""
    path = path or default_definitions_path()
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"ids": sorted(definition_ids), "fetched": time.time()}, fh)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception as exc:
        logger.debug("Jackett definitions save failed: %s", exc)


def load_definition_ids(path: str = "") -> set:
    """Read the saved definition-id set; empty set = unknown (never filter)."""
    path = path or default_definitions_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        ids = data.get("ids")
        return set(ids) if isinstance(ids, list) else set()
    except FileNotFoundError:
        return set()
    except Exception as exc:
        logger.debug("Jackett definitions load failed: %s", exc)
        return set()


def sync_sources(config: DeeptorrentConfig, config_path: Optional[str] = None,
                 enable_new: bool = True) -> Optional[Dict[str, Any]]:
    """Fetch + merge + persist the source list. None when nothing was applied
    (fetch failed or Jackett has no configured indexers).

    Zombie cleanup (2026-10-13 study): sources with origin "jackett" whose
    id is absent from Jackett's FULL definition list were removed from
    Jackett itself — drop them and report the names. Legacy ""-origin
    sources are never dropped here (they may be manual adds); ""-origin
    ids that ARE in the definition list get upgraded to "jackett"."""
    try:
        fetched, definition_ids = fetch_indexers_detailed(config)
    except Exception as exc:
        logger.warning("Jackett source fetch failed: %s", exc)
        return None
    if not fetched:
        logger.info("Jackett reports no configured indexers — keeping existing source list")
        return None
    bootstrap = not config.sources.sources
    merged = merge_sources(config.sources.sources, fetched, enable_new=enable_new or bootstrap)
    removed_zombies: List[str] = []
    survivors: List[SourceConfig] = []
    for s in merged:
        if s.origin == "":
            if s.id in definition_ids:
                s.origin = "jackett"  # Jackett knows the id — it is one of ours
            survivors.append(s)
        elif s.origin == "jackett" and s.id not in definition_ids:
            removed_zombies.append(s.name or s.id)
        else:
            survivors.append(s)
    config.sources.sources = survivors
    config.sources.last_jackett_fetch = time.time()
    save_definition_ids(definition_ids)
    # Everything Jackett reports CONFIGURED is by definition not a zombie —
    # clear any "Unknown indexer" strike counts so a re-configured indexer
    # resumes being queried (is_dead_zombie keys off the failure count).
    try:
        from infra.indexer_health import health_store
        store = health_store()
        for src in fetched:
            store.record_success(src.id)
    except Exception as exc:
        logger.debug("health-store reset after sync failed: %s", exc)
    enabled = sum(1 for s in config.sources.sources if s.enabled)
    if config_path:
        try:
            config.to_file(config_path)
        except Exception as exc:
            logger.warning("Failed to persist sources after Jackett sync: %s", exc)
    logger.info("Jackett sync: %d indexers (%d enabled, bootstrap=%s, %d zombie(s) removed)",
                len(fetched), enabled, bootstrap, len(removed_zombies))
    result: Dict[str, Any] = {"total": len(fetched), "enabled": enabled,
                              "bootstrap": bootstrap}
    if removed_zombies:
        result["removed_zombies"] = removed_zombies
        result["changed"] = True
    return result


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


def set_flaresolverr_url(config: DeeptorrentConfig, url: str,
                         timeout: float = 10.0) -> Dict[str, Any]:
    """Write the FlareSolverr API URL into Jackett's server config.

    Endpoint shape verified live 2026-10-13 against Jackett v0.24.2793
    (the WebUI's own api.js uses exactly this): an authenticated session
    (``/UI/Login?cookiesChecked=1`` cookie dance — a bare apikey gets a
    302) GETs ``/api/v2.0/server/config`` (lowercase JSON keys,
    ``flaresolverrurl``), we overwrite the one field and POST the whole
    object back, mirroring the UI's save. Returns {"success", "message"}.
    """
    url = (url or "").strip()
    if not url:
        return {"success": False, "message": "No FlareSolverr URL provided"}
    # Local import: jackett_setup imports this module (circular otherwise).
    from infra.jackett_setup import open_admin_session

    base = config.indexer.url.rstrip("/")
    session = open_admin_session(base, timeout=timeout)
    if session is None:
        return {"success": False,
                "message": "Could not open a Jackett admin session (an admin "
                           "password is set, or Jackett is not running)"}
    try:
        resp = session.get(f"{base}/api/v2.0/server/config", timeout=timeout)
        if resp.status_code != 200:
            return {"success": False,
                    "message": f"Jackett config fetch failed (HTTP {resp.status_code})"}
        server_config = resp.json()
        server_config["flaresolverrurl"] = url
        post = session.post(
            f"{base}/api/v2.0/server/config",
            data=json.dumps(server_config),
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        if post.status_code != 200:
            return {"success": False,
                    "message": f"Jackett rejected the config write (HTTP {post.status_code})"}
        # Jackett applies the URL immediately; sanity-check it stuck.
        check = session.get(f"{base}/api/v2.0/server/config", timeout=timeout)
        applied = (check.json().get("flaresolverrurl") or "").strip() if check.ok else url
        if applied.rstrip("/").lower() != url.rstrip("/").lower():
            return {"success": False, "message": "Jackett did not keep the FlareSolverr URL"}
        return {"success": True, "message": f"FlareSolverr URL saved in Jackett ({url})"}
    except Exception as exc:
        return {"success": False, "message": f"FlareSolverr config push failed: {exc}"}
    finally:
        session.close()
