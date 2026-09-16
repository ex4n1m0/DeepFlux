"""One-shot Jackett bootstrap — the DeepFlux installer's final step.

`DeepFlux --setup-jackett` runs :func:`run_setup` (GUI wrapper in
gui/jackett_setup_dialog.py). The installer seeds an empty config.json at
ssPostInstall and then runs this step, so a fresh machine ends with the
Jackett service installed, its API key linked, every public indexer
configured in Jackett, and the source list already snapshotted — torrent
search works on first launch with zero user typing.

Pipeline (every stage is idempotent — upgrades re-run the whole step and
just re-link, because the installer wipes config.json on EVERY install):

1. discover — read Jackett's own ``ServerConfig.json`` (ProgramData or
   LOCALAPPDATA) for its API key / port / base path.
2. install — when Jackett is neither answering nor startable: run the
   BUNDLED official installer elevated (one UAC prompt) with silent flags.
   Verified against Jackett's own Installer.iss: the ``windowsService``
   task is CHECKED BY DEFAULT and its non-postinstall [Run] entries
   (``JackettConsole --Install`` / ``--Start``) execute even under
   /VERYSILENT, so a silent install leaves the service installed,
   started, and listening. Everything lands in ``C:\\ProgramData\\Jackett``
   with ``everyone-modify`` permissions, so an unelevated DeepFlux can
   read ServerConfig.json.
3. link — write ``indexer.url`` + ``indexer.api_key`` into config.json
   (atomic to_file; same fields the user would paste into API Keys).
4. add — log into Jackett's web UI session (``GET /UI/Login?cookiesChecked=1``;
   with no admin password this issues the auth cookie — the API key alone
   is NOT accepted on the admin routes, verified live: plain apikey gets
   a 302 to the login page), then for every UNCONFIGURED public indexer
   GET ``/api/v2.0/indexers/{id}/Config`` and POST the array back
   unchanged — exactly the web UI's OK button (verified live 2026-09-16:
   204, ``Indexers/{id}.json`` written, configured flag flips). Jackett
   only saves an indexer when its connection test passes, so dead /
   Cloudflare-challenged / i2p sites fail with a 500 and are counted and
   SKIPPED — that is correct, not an error to fix.
5. verify — jackett.is_running + jackett.sync_sources (the same bootstrap
   the app itself runs at startup when the source list is empty).

This module NEVER uninstalls or reconfigures Jackett beyond adding public
indexers — the service is shared infrastructure (Sonarr & co. use it too).
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from config import DeeptorrentConfig
from infra import jackett

logger = logging.getLogger(__name__)

JACKETT_INSTALLER_NAME = "Jackett.Installer.Windows.exe"
INSTALLER_SILENT_FLAGS = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART"
INSTALLER_WAIT_SECONDS = 900.0        # .NET service install on a slow machine
SERVICE_ONLINE_TIMEOUT = 120.0        # service start + first config write
SERVER_CONFIG_TIMEOUT = 90.0          # ServerConfig.json to appear post-install
FANOUT_WORKERS = 5
FANOUT_BUDGET_SECONDS = 480.0         # hard wall-clock cap on the add sweep
UAC_CANCELLED = 1223                  # ERROR_CANCELLED from ShellExecuteEx

# Per AGENTS.md: Windows subprocess spawns must not flash a console window.
_CREATE_NO_WINDOW = 0x08000000


# ----------------------------------------------------------------------
# ServerConfig discovery
# ----------------------------------------------------------------------

def server_config_candidates() -> List[str]:
    """Jackett's ServerConfig.json locations, most likely first.

    The service install writes C:\\ProgramData\\Jackett; a user-mode
    (tray/console) install keeps its config under LOCALAPPDATA.
    """
    paths: List[str] = []
    for env in ("ProgramData", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            paths.append(os.path.join(base, "Jackett", "ServerConfig.json"))
    return paths


def read_jackett_server_config() -> Optional[Dict[str, Any]]:
    """Read Jackett's own runtime config; None when there is no install.

    Returns the fields we care about: api_key, port, base_path and whether
    an admin password is set (a password blocks the web-UI session the
    indexer-add step needs — see open_admin_session).
    """
    for path in server_config_candidates():
        try:
            if not os.path.isfile(path):
                continue
            import json
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            api_key = (data.get("APIKey") or "").strip()
            if not api_key:
                continue
            return {
                "api_key": api_key,
                "port": int(data.get("Port") or 9117),
                "base_path": (data.get("BasePathOverride") or "").strip(),
                "admin_password_set": bool((data.get("AdminPassword") or "").strip()),
            }
        except Exception as exc:
            logger.debug("Failed to read Jackett ServerConfig at %s: %s", path, exc)
    return None


def base_url_from(server_cfg: Dict[str, Any]) -> str:
    return f"http://127.0.0.1:{server_cfg['port']}{server_cfg['base_path'] or ''}"


def _wait_for_server_config(timeout: float) -> Optional[Dict[str, Any]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        cfg = read_jackett_server_config()
        if cfg:
            return cfg
        time.sleep(1.0)
    return read_jackett_server_config()


# ----------------------------------------------------------------------
# Bundled installer
# ----------------------------------------------------------------------

def find_bundled_installer() -> Optional[str]:
    """Locate the Jackett installer shipped next to the app, if any."""
    env = os.environ.get("DEEPFLUX_JACKETT_INSTALLER")
    if env and os.path.isfile(env):
        return env
    candidates: List[str] = []
    # Frozen onedir build: <app>/DeepFlux.exe + _internal/jackett/…
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates.append(os.path.join(exe_dir, "_internal", "jackett", JACKETT_INSTALLER_NAME))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "jackett", JACKETT_INSTALLER_NAME))
    # Source tree: walk up from infra/ to the repo root.
    root = os.path.dirname(os.path.abspath(__file__))
    for _ in range(3):
        root = os.path.dirname(root)
        candidates.append(os.path.join(root, "packaging", "jackett", JACKETT_INSTALLER_NAME))
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return None


# ----------------------------------------------------------------------
# Elevated execution (ctypes ShellExecuteExW)
# ----------------------------------------------------------------------

_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SW_SHOWNORMAL = 1


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkey", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIconUnion", ctypes.c_void_p),   # union { HANDLE hIcon; HMONITOR hMonitor; }
        ("hProcess", ctypes.c_void_p),     # HANDLE — set with SEE_MASK_NOCLOSEPROCESS
    ]


def _shell_execute_runas(file_path: str, params: str,
                         timeout: float = INSTALLER_WAIT_SECONDS) -> Tuple[bool, int]:
    """Run an executable elevated (one UAC prompt) and wait for it.

    Returns (ok, code). ok is True only when the process ran to completion
    and exited 0. code is the installer exit code, the GetLastError() value
    when the launch itself failed (1223 = UAC declined), or -1 on wait
    timeout. All handles/argtypes are declared explicitly — untyped
    ctypes.windll truncates pointer-sized args on 64-bit Windows (see the
    PlayerWidget compact-mode lesson in AGENTS.md).
    """
    if os.name != "nt":
        return False, -1
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = os.path.abspath(file_path)
    info.lpParameters = params
    info.lpDirectory = os.path.dirname(info.lpFile)
    info.nShow = _SW_SHOWNORMAL  # VERYSILENT shows no window anyway

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return False, ctypes.get_last_error()
    if not info.hProcess:
        # Launched but no handle to wait on (process reused) — assume ok.
        return True, 0
    try:
        wait_rc = kernel32.WaitForSingleObject(info.hProcess, int(timeout * 1000))
        if wait_rc != 0:  # WAIT_OBJECT_0 == 0; anything else = timeout/abandoned
            return False, -1
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            return False, -1
        return code.value == 0, int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


# ----------------------------------------------------------------------
# Admin API (web-UI session + indexer fan-out)
# ----------------------------------------------------------------------

def open_admin_session(base_url: str, timeout: float = 10.0) -> Optional[requests.Session]:
    """Log into Jackett's web UI and return an authenticated session.

    With no admin password, ``GET /UI/Login?cookiesChecked=1`` signs us in
    and redirects to the dashboard — the resulting cookie is what the
    admin API routes require (a bare apikey gets a 302 to the login page).
    Returns None when a password is set or the login round-trip fails.
    """
    session = requests.Session()
    try:
        resp = session.get(
            f"{base_url.rstrip('/')}/UI/Login",
            params={"cookiesChecked": "1"},
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception as exc:
        logger.debug("Jackett UI login failed: %s", exc)
        session.close()
        return None
    if "/ui/dashboard" in (resp.url or "").lower():
        return session
    session.close()
    return None


def _list_indexers(config: DeeptorrentConfig, timeout: float = 20.0) -> List[Dict[str, Any]]:
    """All indexers Jackett knows about, with their configured flags.

    Uses the Torznab ``t=indexers`` caps call — the same endpoint
    infra.jackett.fetch_indexers already relies on — because it is stable
    across Jackett versions, unlike the admin list route (which moved
    between v2.2 and v2.0 and rejects some parameter spellings).
    """
    resp = requests.get(
        jackett._torznab_url(config),
        params={"apikey": config.indexer.api_key, "t": "indexers"},
        timeout=timeout,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    out: List[Dict[str, Any]] = []
    for idx in root.findall("indexer"):
        idx_id = idx.get("id", "")
        if not idx_id:
            continue
        out.append({
            "id": idx_id,
            "name": idx.findtext("title") or idx_id,
            "type": (idx.findtext("type") or "public").lower(),
            "configured": idx.get("configured", "false") == "true",
        })
    return out


def add_public_indexers(config: DeeptorrentConfig, session: requests.Session,
                        on_progress: Optional[Callable[[str], None]] = None,
                        max_workers: int = FANOUT_WORKERS,
                        budget_seconds: float = FANOUT_BUDGET_SECONDS) -> Dict[str, int]:
    """Configure every unconfigured PUBLIC indexer via the admin API.

    GET ``/api/v2.0/indexers/{id}/Config`` then POST the array back
    unchanged — the web UI's OK button. Jackett saves the indexer only if
    its connection test passes; failures (dead site, Cloudflare challenge
    needing FlareSolverr, i2p-only host) are counted and skipped. Sessions
    are per-thread (requests.Session is not thread-safe; each worker logs
    in once — a single localhost GET).
    """
    try:
        indexers = _list_indexers(config)
    except Exception as exc:
        logger.warning("Could not list Jackett indexers: %s", exc)
        return {"total": 0, "added": 0, "failed": 0, "listed": False}
    targets = [i for i in indexers if not i["configured"] and i["type"] == "public"]
    total = len(targets)
    base = config.indexer.url.rstrip("/")
    api_key = config.indexer.api_key
    local = threading.local()
    lock = threading.Lock()
    state = {"added": 0, "failed": 0, "done": 0}

    def _worker_session() -> Optional[requests.Session]:
        if getattr(local, "session", None) is None:
            local.session = open_admin_session(base)
        return local.session

    def _add_one(target: Dict[str, Any]) -> None:
        ok = False
        worker_session = _worker_session()
        if worker_session is not None:
            url = f"{base}/api/v2.0/indexers/{target['id']}/Config"
            try:
                resp = worker_session.get(url, params={"apikey": api_key},
                                          timeout=(5.0, 20.0))
                resp.raise_for_status()
                post = worker_session.post(url, params={"apikey": api_key},
                                           json=resp.json(), timeout=(5.0, 90.0))
                ok = post.status_code in (200, 204)
            except Exception:
                ok = False
        with lock:
            state["done"] += 1
            state["added" if ok else "failed"] += 1
            if on_progress:
                on_progress(f"Adding public indexers… {state['done']}/{total} "
                            f"({state['added']} added)")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_add_one, t) for t in targets]
        wait(futures, timeout=budget_seconds)
        for future in futures:               # never let a sweep error escape
            try:
                future.result()
            except Exception:
                pass
        for future in futures:               # cancel what the budget didn't reach
            future.cancel()
    return {"total": total, "added": state["added"],
            "failed": state["failed"], "listed": True}


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

def run_setup(config_path: Optional[str] = None,
              installer_path: Optional[str] = None,
              on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """The whole bootstrap. Safe to run repeatedly; see the module docstring.

    Returns a result dict for presentation:
    ok, error, installed, api_key_linked, added, failed_add,
    public_total, sources_enabled, admin_password.
    """
    def say(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass
        logger.info("jackett-setup: %s", msg)

    config_path = config_path or DeeptorrentConfig.default_config_path()
    config = DeeptorrentConfig.from_file(config_path)
    result: Dict[str, Any] = {
        "ok": False, "error": None, "installed": False, "api_key_linked": False,
        "added": 0, "failed_add": 0, "public_total": 0,
        "sources_enabled": 0, "admin_password": False,
    }

    # --- Stage 1+2: get a reachable Jackett with a known key --------------
    server_cfg = read_jackett_server_config()
    if server_cfg:
        config.indexer.url = base_url_from(server_cfg)
        config.indexer.api_key = server_cfg["api_key"]

    if jackett.is_running(config):
        say("Jackett service found — already running.")
    else:
        started = False
        if server_cfg:
            say("Jackett is installed but not answering — starting it…")
            started = bool(jackett.start(config) and jackett.wait_until_ready(config))
        if not started:
            installer = installer_path or find_bundled_installer()
            if not installer:
                result["error"] = "no-installer"
                say("Jackett is not installed and this build ships no installer for it.")
                say("Install Jackett from https://github.com/Jackett/Jackett — "
                    "DeepFlux will pick it up automatically on the next start.")
                return result
            say("Installing the Jackett service — accept the Windows prompt…")
            ok, code = _shell_execute_runas(installer, INSTALLER_SILENT_FLAGS)
            if code == UAC_CANCELLED:
                result["error"] = "uac-declined"
                say("The Windows prompt was declined — Jackett was not installed.")
                return result
            if not ok:
                result["error"] = f"installer-failed:{code}"
                say(f"The Jackett installer did not complete (exit code {code}).")
                return result
            result["installed"] = True
            say("Jackett installed — waiting for the service to come online…")
            server_cfg = _wait_for_server_config(SERVER_CONFIG_TIMEOUT)
            if not server_cfg:
                result["error"] = "no-server-config"
                say("Jackett installed but its configuration file never appeared; "
                    "try starting it from the Start menu once.")
                return result
            config.indexer.url = base_url_from(server_cfg)
            config.indexer.api_key = server_cfg["api_key"]
            if not jackett.wait_until_ready(config, timeout=SERVICE_ONLINE_TIMEOUT):
                result["error"] = "not-running"
                say("Jackett installed but is not answering yet — DeepFlux will "
                    "keep trying at startup.")
                return result

    result["admin_password"] = bool(server_cfg and server_cfg.get("admin_password_set"))

    # --- Stage 3: link the key into config.json ---------------------------
    try:
        config.to_file(config_path)
        result["api_key_linked"] = True
        say("Jackett linked — API key saved to DeepFlux settings.")
    except Exception as exc:
        result["error"] = f"config-write-failed:{exc}"
        say(f"Could not save the DeepFlux settings file: {exc}")
        return result

    # --- Stage 4: add every public indexer to Jackett ---------------------
    session = open_admin_session(config.indexer.url)
    if session is None:
        if result["admin_password"]:
            say("Jackett has an admin password set — add indexers in its web UI "
                f"({config.indexer.url}) if you want more sources.")
        else:
            say("Could not open a Jackett session — indexers were not auto-added.")
    else:
        try:
            say("Adding all public indexers to Jackett (dead sites are skipped)…")
            counts = add_public_indexers(config, session, on_progress=say)
            result["added"] = counts["added"]
            result["failed_add"] = counts["failed"]
            result["public_total"] = counts["total"]
            if counts["listed"] and counts["total"] == 0:
                say("All public indexers were already configured.")
            elif counts["failed"]:
                say(f"{counts['failed']} of {counts['total']} indexers could not be "
                    "verified (site down or requires extra setup) and were skipped.")
        finally:
            session.close()

    # --- Stage 5: test connection + snapshot the source list --------------
    if not jackett.is_running(config):
        result["error"] = "not-running"
        say("Jackett stopped answering — run this setup step again or start it "
            "from the Start menu.")
        return result
    say("Testing the connection…")
    sync = jackett.sync_sources(config, config_path)
    enabled = sum(1 for s in config.sources.sources if s.enabled)
    result["sources_enabled"] = enabled
    if sync:
        result["ok"] = True
        say(f"Connection OK — torrent search ready with {enabled} sources.")
    else:
        # Reachable but nothing fetched: the app retries at every startup.
        result["ok"] = True
        say("Connection OK. Sources will sync the first time DeepFlux starts.")
    return result
