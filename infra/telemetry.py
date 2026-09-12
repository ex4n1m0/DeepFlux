"""Anonymous usage ping — the app's only "call home".

Powers the live "users online" counter on deepflux.space: while the app runs,
a daemon thread POSTs a tiny heartbeat to the website every few minutes and
one best-effort "leaving" note on shutdown. The server keeps a Redis sorted
set of install ids seen in the last 15 minutes and reports its size.

Privacy contract (also documented in the User Guide):
  * the payload is {id, v, os} — a random id generated once per install,
    the app version, and the OS name. Nothing else: no username, no machine
    name, no paths, no feature usage.
  * it is OFF by a single checkbox (Download settings page,
    ``stats.ping_enabled``) with no dark patterns attached.
  * failures are completely silent — a blocked/unreachable endpoint must
    never log-spam, slow startup or break anything.

Qt-free and stdlib+requests only, same shape as ``infra/jackett`` and
``dlmgr/ytdlp_update``: call :func:`start_heartbeat` once at startup and
:func:`stop_heartbeat` on shutdown.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from typing import Optional

import requests

from config import APP_VERSION, DeeptorrentConfig

logger = logging.getLogger(__name__)

# Where heartbeats go. The DEEPFLUX_TELEMETRY_URL env var exists for
# development against a local `vercel dev` instance — end users never need it.
HEARTBEAT_URL = os.environ.get("DEEPFLUX_TELEMETRY_URL",
                               "https://deepflux.space/api/heartbeat")

# One beat every 5 minutes; the server's presence window is 15 minutes, so a
# single missed beat never flickers a user off the count.
INTERVAL_SECONDS = 5 * 60
REQUEST_TIMEOUT = 5

_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_install_id_cache: Optional[str] = None


def install_id(data_dir: str) -> str:
    """Stable random id for this installation (created on first call).

    Lives in its own file next to config.json — deliberately NOT in the
    config itself, so a Settings Export/Import must not transplant one
    machine's identity onto another (two machines sharing an id would
    under-count). Survives installs/upgrades: the installer only ever
    deletes config.json."""
    global _install_id_cache
    if _install_id_cache:
        return _install_id_cache
    path = os.path.join(data_dir, "install_id")
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = f.read().strip()
        if 8 <= len(value) <= 64:
            _install_id_cache = value
            return value
    except OSError:
        pass
    value = uuid.uuid4().hex
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
    except OSError as exc:
        logger.debug("telemetry: could not persist install id: %s", exc)
    _install_id_cache = value
    return value


def _post(payload: dict) -> bool:
    """One POST, one attempt, fully silent failure."""
    try:
        r = requests.post(HEARTBEAT_URL, json=payload, timeout=REQUEST_TIMEOUT)
        r.close()
        return r.status_code == 200
    except Exception:
        return False


def send_beat(install: str) -> bool:
    """Announce 'this install is running right now'."""
    return _post({"id": install, "v": APP_VERSION, "os": os.name})


def send_leave(install: str) -> bool:
    """Best-effort 'this install is gone' — the 15-min window cleans up
    anything that never lands (crash, kill, offline)."""
    return _post({"id": install, "v": APP_VERSION, "os": os.name, "leave": True})


def _loop(data_dir: str) -> None:
    install = install_id(data_dir)
    while not _stop.is_set():
        send_beat(install)
        _stop.wait(INTERVAL_SECONDS)


def start_heartbeat(config: DeeptorrentConfig, data_dir: Optional[str] = None) -> None:
    """Start the background heartbeat thread (no-op when disabled/running).

    ``data_dir`` defaults to the directory holding config.json (~/.deeptorrent)
    — that is where install_id lives."""
    global _thread
    if not config.stats.ping_enabled:
        return
    if _thread is not None and _thread.is_alive():
        return
    if data_dir is None:
        data_dir = os.path.dirname(DeeptorrentConfig.default_config_path())
    install_id(data_dir)  # create/warm the id now, not on the first beat
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(data_dir,), daemon=True,
                               name="telemetry-heartbeat")
    _thread.start()


def stop_heartbeat() -> None:
    """Stop the loop and fire the best-effort leave note.

    Never blocks: the note runs in its own daemon thread, so if the process
    exits before the POST lands, the server's presence window absorbs it."""
    _stop.set()
    install = _install_id_cache
    if install:
        threading.Thread(target=send_leave, args=(install,), daemon=True,
                         name="telemetry-leave").start()

