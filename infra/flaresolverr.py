"""FlareSolverr service management + the installer's setup step.

FlareSolverr (MIT, github.com/FlareSolverr/FlareSolverr) solves Cloudflare's
anti-bot challenge for Jackett. Measured live 2026-10-13 on this machine:
with it running, 15 of 19 Cloudflare-walled indexers became functional
(incl. eztv and kickasstorrents; 1337x's current challenge is NOT solvable
by it); first solves take 40-90s, then cookies are cached.

The official Windows build (v3.5.2, pinned by packaging/fetch_flaresolverr.py)
is a self-contained PyInstaller onedir that bundles its OWN Chromium — no
system Chrome dependency — and configures itself purely through ENV vars.
DeepFlux treats it exactly like Jackett (infra/jackett.py): the installer
ships the zip, ``--setup-flaresolverr`` extracts it next to the app, starts
it and links the URL into Jackett's own config through the verified admin
API; afterwards the app lazily starts it whenever it is configured but down.

All functions are safe to call from a background thread. POSIX: FlareSolverr
is Windows-setup-only for now — find_executable returns None there and
everything degrades to a clean no-op (the URL field still works for a
manually installed instance on any platform).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import zipfile
from typing import Any, Callable, Dict, List, Optional

import requests

from config import DeeptorrentConfig

logger = logging.getLogger(__name__)

ZIP_NAME = "flaresolverr_windows_x64.zip"
# Local, loopback-only binding (HOST=127.0.0.1): the solver is for OUR
# Jackett, not the LAN.
DEFAULT_URL = "http://127.0.0.1:8191/"
DEFAULT_PORT = 8191
START_TIMEOUT = 90.0     # first boot launches the bundled Chromium — slow
EXTRACT_PROGRESS_EVERY = 50   # log lines between zip-extract progress notes

# Per AGENTS.md: Windows subprocess spawns must not flash a console window.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)


def install_root() -> str:
    """Where the extracted flaresolverr/ tree lives (next to the frozen exe;
    in dev, inside packaging/)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "packaging", "flaresolverr")
    return os.path.join(base, "flaresolverr")


def find_executable() -> Optional[str]:
    """Path to flaresolverr.exe when the setup step has extracted it."""
    if sys.platform != "win32":
        return None
    exe = os.path.join(install_root(), "flaresolverr.exe")
    return exe if os.path.isfile(exe) else None


def _zip_candidates() -> List[str]:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return [os.path.join(sys._MEIPASS, "flaresolverr", ZIP_NAME)]
    return [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "packaging", "flaresolverr", ZIP_NAME)]


def _pinned_release() -> Optional[Dict[str, Any]]:
    """The tracked pin written by packaging/fetch_flaresolverr.py
    (version/asset/size/sha256) — shipped into _internal/flaresolverr/ by
    app.spec so the frozen setup step downloads the exact reviewed bytes."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        path = os.path.join(sys._MEIPASS, "flaresolverr", "PINNED.json")
    else:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "packaging", "flaresolverr", "PINNED.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if data.get("version") and data.get("sha256") else None
    except Exception:
        return None


_DL_CAP = 600 * 1024 * 1024  # sanity cap; the zip is ~378 MB


def _download_pinned_zip(say: Callable[[str], None]) -> Optional[str]:
    """Download the pinned official FlareSolverr zip (stream-hashed against
    the pin) into a temp file next to the extraction target. Returns the
    path, or None on any failure (the caller reports it)."""
    pin = _pinned_release()
    if pin is None:
        say("No FlareSolverr pin available — cannot download.")
        return None
    url = ("https://github.com/FlareSolverr/FlareSolverr/releases/download/"
           f"{pin['version']}/{pin['asset']}")
    dest = os.path.join(os.path.dirname(install_root()), ZIP_NAME + ".download")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    say(f"Downloading FlareSolverr {pin['version']} from the official release "
        f"({pin['size'] / 1e6:.0f} MB)…")
    try:
        sha = hashlib.sha256()
        got = 0
        with requests.get(url, stream=True, timeout=(15, 90)) as dl:
            dl.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in dl.iter_content(chunk_size=1024 * 512):
                    if not chunk:
                        continue
                    got += len(chunk)
                    if got > _DL_CAP:
                        raise RuntimeError("download exceeded the sanity cap")
                    sha.update(chunk)
                    fh.write(chunk)
    except Exception as exc:
        say(f"Download failed: {exc}")
        try:
            os.remove(dest)
        except OSError:
            pass
        return None
    if got != pin.get("size") or sha.hexdigest() != pin["sha256"]:
        say("Downloaded file failed the pinned digest check — aborting.")
        try:
            os.remove(dest)
        except OSError:
            pass
        return None
    return dest


def is_running(url: str = DEFAULT_URL, timeout: float = 3.0) -> bool:
    """True when FlareSolverr answers its /health endpoint (verified live:
    200 {"status": "ok"})."""
    try:
        resp = requests.get(f"{url.rstrip('/')}/health", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def start(url: str = DEFAULT_URL) -> bool:
    """Spawn FlareSolverr detached (loopback-only, headless) — the same
    lazy-start pattern as infra.jackett.start. Readiness needs
    wait_until_ready()."""
    exe = find_executable()
    if not exe:
        logger.info("No FlareSolverr executable found (setup step not run)")
        return False
    env = dict(os.environ,
               PORT=str(DEFAULT_PORT),
               HOST="127.0.0.1",
               LOG_LEVEL="info")
    try:
        subprocess.Popen(
            [exe],
            cwd=os.path.dirname(exe),
            env=env,
            creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Started FlareSolverr: %s", exe)
        return True
    except Exception as exc:
        logger.warning("Failed to start FlareSolverr %s: %s", exe, exc)
        return False


def wait_until_ready(url: str = DEFAULT_URL, timeout: float = START_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running(url):
            return True
        time.sleep(1.0)
    return is_running(url)


def ensure_running(config: DeeptorrentConfig, url: str = DEFAULT_URL) -> bool:
    """Start FlareSolverr when the user's config points at OUR default local
    instance, it is installed, and it is down (a custom/remote URL is never
    ours to start). Cheap when already running."""
    configured = (config.indexer.flare_solverr_url or "").strip().rstrip("/").lower()
    if configured != DEFAULT_URL.rstrip("/").lower():
        return False
    if is_running(url):
        return True
    if not start(url):
        return False
    return wait_until_ready(url)


def cleanup_bundled_zip() -> None:
    """Delete the bundled zip once the extracted install exists.

    Inno re-copies the zip on EVERY install and update — including silent
    self-updates, which skip the postinstall step that would otherwise
    consume it — so without this it lingers as ~378 MB of dead weight in
    _internal after each auto-update. FROZEN ONLY: in a dev tree the
    "bundled zip" is the fetch-script build input in packaging/ and the
    E2E extraction sits next to it — cleaning there would eat the build
    input (bit us 2026-10-13: a dev GUI run deleted the pinned zip)."""
    if not getattr(sys, "frozen", False):
        return
    if find_executable() is None:
        return
    for path in _zip_candidates():
        try:
            if path and os.path.isfile(path):
                os.remove(path)
                logger.info("Removed the bundled FlareSolverr zip (already extracted)")
        except OSError as exc:
            logger.debug("bundled zip cleanup failed: %s", exc)


def notify_flare_failure(config: DeeptorrentConfig) -> None:
    """Called by the search tier when Cloudflare-challenged indexers show up:
    makes sure our local solver is up so the NEXT search (and Jackett's
    retry) benefits. Never blocks the search — worst case it is a no-op."""
    try:
        ensure_running(config)
    except Exception as exc:
        logger.debug("flare auto-start failed: %s", exc)


def run_setup(config_path: Optional[str] = None,
              zip_path: Optional[str] = None,
              on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """The installer's FlareSolverr final step (``DeepFlux --setup-flaresolverr``).

    Idempotent pipeline: extract the bundled zip next to the app, delete the
    zip (the extracted tree replaces it — saves ~378 MB), start the service,
    save ``indexer.flare_solverr_url`` and push it into Jackett's own config
    when Jackett is linked. Never elevates, never touches Jackett beyond the
    one admin-config write."""
    def say(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass
        logger.info("flaresolverr-setup: %s", msg)

    config_path = config_path or DeeptorrentConfig.default_config_path()
    config = DeeptorrentConfig.from_file(config_path)
    result: Dict[str, Any] = {
        "ok": False, "error": None, "extracted": False, "started": False,
        "already_running": False, "jackett_linked": False,
    }

    if sys.platform != "win32":
        result["error"] = "The bundled FlareSolverr setup is Windows-only."
        return result

    # Already set up? Just make sure it runs.
    if find_executable() is not None:
        cleanup_bundled_zip()  # upgrade re-copied it; the extraction exists
        if is_running():
            say("FlareSolverr is already installed and running.")
            result["already_running"] = True
        else:
            say("FlareSolverr is installed — starting it…")
            if not (start() and wait_until_ready()):
                result["error"] = "installed copy did not start"
                return result
            result["started"] = True
    else:
        candidates = [zip_path] if zip_path else _zip_candidates()
        source_zip = next((p for p in candidates if p and os.path.isfile(p)), None)
        if source_zip is None:
            # Since 6.0 the zip is NOT bundled in the setup exe (it pushed
            # the installer past Vercel's per-file serving cap — 5.9 at
            # 751.3 MB served, 6.0 at 802.4 MB got hard 500s). The step
            # downloads the pinned official release instead, same contract
            # as the local-model step and Linux Jackett.
            source_zip = _download_pinned_zip(say)
            if not source_zip:
                result["error"] = ("FlareSolverr could not be downloaded "
                                   "(no network?). Retry from Help → User Guide.")
                say(result["error"])
                return result
        say(f"Extracting FlareSolverr ({os.path.getsize(source_zip) / 1e6:.0f} MB)…")
        try:
            root = os.path.dirname(install_root())
            os.makedirs(root, exist_ok=True)
            with zipfile.ZipFile(source_zip) as zf:
                entries = zf.namelist()
                for i, name in enumerate(entries):
                    zf.extract(name, root)
                    if (i + 1) % EXTRACT_PROGRESS_EVERY == 0:
                        say(f"  {i + 1}/{len(entries)} files…")
        except Exception as exc:
            result["error"] = f"extraction failed: {exc}"
            say(result["error"])
            return result
        result["extracted"] = True
        say("Extraction complete — removing the bundled zip to save disk space…")
        try:
            os.remove(source_zip)
        except OSError as exc:
            logger.debug("zip cleanup failed (harmless): %s", exc)

        if find_executable() is None:
            result["error"] = "zip did not contain flaresolverr/flaresolverr.exe"
            say(result["error"])
            return result
        say("Starting FlareSolverr (first boot opens its bundled browser)…")
        if not (start() and wait_until_ready()):
            result["error"] = "FlareSolverr did not answer on port 8191 in time"
            say(result["error"])
            return result
        result["started"] = True

    # Persist the URL in DeepFlux's config…
    config.indexer.flare_solverr_url = DEFAULT_URL
    try:
        config.to_file(config_path)
    except Exception as exc:
        logger.warning("Failed to persist flare_solverr_url: %s", exc)

    # …and link it into Jackett when the key is there (the step normally runs
    # right after --setup-jackett, so Jackett is up).
    from infra import jackett
    if config.indexer.api_key:
        say("Linking FlareSolverr into Jackett…")
        link = jackett.set_flaresolverr_url(config, DEFAULT_URL)
        result["jackett_linked"] = bool(link.get("success"))
        say(link.get("message") or "")
    else:
        say("Jackett is not linked yet — the URL is saved and will be pushed "
            "when you connect Jackett.")

    result["ok"] = True
    return result
