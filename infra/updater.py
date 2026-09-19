"""In-app auto-update for the Windows build (frozen installs only).

Flow: a daily-gated feed check (``deepflux.space/deepflux/latest.json``)
returns the newest version + its setup exe's size and sha256 → the GUI
offers "Update now / Later / Skip this version" → the setup exe is
downloaded with a streamed, Range-resumable transfer and verified → a
detached PowerShell helper waits for this process to exit, runs the
installer silently and relaunches the app.

Why a full setup exe instead of a patch framework: the Inno installer IS
the supported upgrade path (Jackett payload, file associations, Chrome
native host, config seed) and the install is per-user
(``PrivilegesRequired=lowest`` → ``%LOCALAPPDATA%\\Programs\\DeepFlux``), so
the silent reinstall needs no elevation. ``/VERYSILENT`` skips the finished
page and both its postinstall entries (``skipifsilent``: the Jackett setup
step and the launch entry — Jackett's key already lives in the PRESERVED
config.json, which the installer no longer deletes).

Security posture: the feed is fetched over HTTPS from our own origin and
the download is rejected unless size AND sha256 match the feed — the same
trust root as a manual website download. Files written by the app carry no
Mark-of-the-Web, so the silent installer run does not hit SmartScreen
(residual: Win11 Smart App Control, when enabled, still evaluates unsigned
exes — same as a manual install). The updater is deliberately NOT exposed
as an agent tool: a page-borne prompt injection must never be able to
trigger an installer run.

Qt-free — same shape as ``dlmgr/ytdlp_update.py``: a background thread
calls :func:`maybe_check_update` at startup; the GUI layer (gui/
update_dialog.py + main_window) owns dialogs and queued signals.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from config import APP_VERSION, DeeptorrentConfig

logger = logging.getLogger(__name__)

FEED_URL = "https://deepflux.space/deepflux/latest.json"
FEED_BODY_LIMIT = 64 * 1024          # the feed is a few hundred bytes
CHECK_INTERVAL_SECONDS = 24 * 3600   # at most one probe per day
DOWNLOAD_CHUNK = 256 * 1024
DOWNLOAD_RETRIES = 4
_MAX_SETUP_BYTES = 2 * 1024 * 1024 * 1024  # sanity ceiling; real builds ~370 MB
_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class UpdateError(RuntimeError):
    """Any update failure the GUI should surface to the user."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    file: str      # setup exe filename as served in the deploy dir
    size: int
    sha256: str
    notes: str = ""
    released: str = ""

    @property
    def url(self) -> str:
        return f"https://deepflux.space/deepflux/{self.file}"

    @property
    def size_mb(self) -> int:
        return max(1, self.size // (1024 * 1024))


def updates_supported() -> bool:
    """Auto-update exists only in the frozen Windows build — source runs and
    the macOS/Linux builds (whose binaries the feed never carries) skip it."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def is_newer(latest: str, current: str) -> bool:
    """True when `latest` sorts after `current` — reuses the yt-dlp tuple
    compare, which handles 4.10 > 4.9 correctly."""
    from dlmgr.ytdlp_update import is_outdated
    return is_outdated(current, latest)


def parse_feed(body: bytes) -> Optional[UpdateInfo]:
    """Strictly validate the feed document. Returns None on anything
    malformed — a broken feed must degrade to "no update", never crash or
    half-apply."""
    if not body or len(body) > FEED_BODY_LIMIT:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("version") or "")
    file_name = str(data.get("file") or "")
    sha = str(data.get("sha256") or "").lower()
    if not _VERSION_RE.match(version):
        return None
    # The filename becomes a path segment below — a basename only, never
    # separators or traversal fragments.
    if not file_name or "/" in file_name or "\\" in file_name or file_name.startswith("."):
        return None
    if not _SHA256_RE.match(sha):
        return None
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0 or size > _MAX_SETUP_BYTES:
        return None
    notes = str(data.get("notes") or "")[:500]
    released = str(data.get("released") or "")[:32]
    return UpdateInfo(version=version, file=file_name, size=size,
                      sha256=sha, notes=notes, released=released)


def fetch_feed(timeout: float = 10.0) -> UpdateInfo:
    """Fetch + validate the feed. Raises on network/API failure."""
    from dlmgr.http_client import get

    response = get(FEED_URL, timeout=timeout, stream=True)
    response.raise_for_status()
    body = b""
    for chunk in response.iter_content(8 * 1024):
        body += chunk
        if len(body) > FEED_BODY_LIMIT:
            raise UpdateError("update feed larger than expected")
    info = parse_feed(body)
    if info is None:
        raise UpdateError("update feed is malformed")
    return info


def maybe_check_update(
    config: DeeptorrentConfig,
    config_path: Optional[str] = None,
    force: bool = False,
) -> Optional[UpdateInfo]:
    """Daily-gated update probe, safe to call at startup from a background
    thread. Returns the UpdateInfo when a NEWER, not-skipped version is out,
    else None (unsupported build, disabled, not due, network failure, feed
    error, or up to date). The gate timestamp is only persisted after a
    successful probe, so a flaky network retries next launch."""
    if not updates_supported():
        return None
    if not config.updater.check_enabled:
        return None
    if not force and time.time() - (config.updater.last_check or 0.0) < CHECK_INTERVAL_SECONDS:
        return None
    try:
        info = fetch_feed()
    except Exception as exc:
        logger.debug("update check failed: %s", exc)
        return None
    config.updater.last_check = time.time()
    if config_path:
        try:
            config.to_file(config_path)
        except Exception as exc:
            logger.warning("Could not persist update check timestamp: %s", exc)
    if not is_newer(info.version, APP_VERSION):
        return None
    # A manual check (force) re-offers a skipped version — the user asked.
    if not force and info.version == (config.updater.skip_version or ""):
        return None
    logger.info("update available: %s (installed %s)", info.version, APP_VERSION)
    return info


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

def updates_dir() -> Path:
    """Staging dir for downloaded setup exes. %LOCALAPPDATA%\DeepFlux\
updates — user-writable, stable across reinstalls, and deliberately NOT
inside ~/.deeptorrent (which the uninstaller wipes)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "DeepFlux" / "updates"


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, info: UpdateInfo) -> bool:
    try:
        if path.stat().st_size != info.size:
            return False
        return _sha256_of(path).lower() == info.sha256.lower()
    except OSError:
        return False


def download_update(
    info: UpdateInfo,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> Path:
    """Stream the setup exe to the staging dir with Range resume.

    Returns the VERIFIED final path. Raises UpdateError on failure — a
    failed transfer keeps its .part file so the next attempt resumes
    instead of restarting the ~370 MB download.
    """
    dest_dir = updates_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / info.file
    if final.exists() and verify_file(final, info):
        return final  # already downloaded (retry after a failed apply)

    part = dest_dir / (info.file + ".part")
    if part.exists() and part.stat().st_size == info.size:
        # A complete-but-unverified .part (interrupted between the last
        # write and the hash check). Accept or discard it outright — a
        # Range request for the full size would answer 416.
        if _sha256_of(part).lower() == info.sha256.lower():
            os.replace(part, final)
            return final
        part.unlink()
    backoffs = (0, 2, 5, 15)
    last_error: Optional[Exception] = None
    for attempt, backoff in enumerate(backoffs):
        if backoff:
            time.sleep(backoff)
        if cancel is not None and cancel.is_set():
            raise UpdateError("cancelled")
        try:
            return _download_once(info, final, part, progress, cancel)
        except _Cancelled:
            raise UpdateError("cancelled")
        except Exception as exc:
            last_error = exc
            logger.debug("update download attempt %d failed: %s", attempt + 1, exc)
    raise UpdateError(f"could not download the update ({last_error})")


class _Cancelled(Exception):
    pass


def _download_once(
    info: UpdateInfo,
    final: Path,
    part: Path,
    progress: Optional[Callable[[int, int], None]],
    cancel: Optional[threading.Event],
) -> Path:
    from dlmgr.http_client import get

    done = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={done}-"} if done else None
    response = get(info.url, headers=headers, timeout=30, stream=True)
    response.raise_for_status()
    if response.status_code == 200:
        done = 0  # server ignored the range — restart from scratch
    elif response.status_code != 206:
        raise UpdateError(f"unexpected response {response.status_code}")

    mode = "ab" if done else "wb"
    written = done
    with open(part, mode) as f:
        for chunk in response.iter_content(DOWNLOAD_CHUNK):
            if cancel is not None and cancel.is_set():
                raise _Cancelled()
            if not chunk:
                continue
            written += len(chunk)
            if written > info.size:
                raise UpdateError("download larger than the announced size")
            f.write(chunk)
            if progress:
                progress(written, info.size)
    if written != info.size:
        raise UpdateError(f"download incomplete ({written} of {info.size} bytes)")
    if _sha256_of(part).lower() != info.sha256.lower():
        part.unlink(missing_ok=True)
        raise UpdateError("download failed the integrity check (sha256 mismatch)")
    os.replace(part, final)
    return final


# --------------------------------------------------------------------------
# Apply (the only part that runs anything)
# --------------------------------------------------------------------------

def install_dir() -> Path:
    """Directory of the running DeepFlux.exe — the Inno silent reinstall
    reuses it (recorded in the per-user uninstall key)."""
    return Path(os.path.dirname(os.path.abspath(sys.executable)))


def install_dir_writable() -> bool:
    try:
        path = install_dir()
        return os.access(str(path), os.W_OK) and path.is_dir()
    except OSError:
        return False


def other_instances_running() -> int:
    """DeepFlux.exe processes besides this one. A second instance holds the
    bundle's files open and would break the silent reinstall."""
    if sys.platform != "win32":
        return 0
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq DeepFlux.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return 0  # do not block the update on a failed probe
    return max(0, out.count('"DeepFlux.exe"') - 1)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_helper(setup_path: Path, app_pid: int, app_exe: str, log_path: Path) -> Path:
    """Write the detached apply script. PowerShell (always present on
    Windows) can poll a PID, wait for the installer and relaunch."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    script = "\n".join([
        "$ErrorActionPreference = 'Continue'",
        f"$appPid = {int(app_pid)}",
        f"$setup = {_ps_quote(str(setup_path))}",
        f"$appExe = {_ps_quote(app_exe)}",
        f"$log = {_ps_quote(str(log_path))}",
        "function Log($m) { Add-Content -Path $log -Value \"$(Get-Date -Format s) $m\" }",
        "# Wait for the app to exit — its graceful shutdown saves download",
        "# resume data and the config; the installer must not race it.",
        "$deadline = (Get-Date).AddSeconds(90)",
        "while (Get-Process -Id $appPid -ErrorAction SilentlyContinue) {",
        "  if ((Get-Date) -gt $deadline) { Log \"app $appPid still running after 90s\"; break }",
        "  Start-Sleep -Milliseconds 500",
        "}",
        "Log \"app exited; running installer\"",
        "# Per-user install: /VERYSILENT needs no elevation. Inno reuses the",
        "# previous install dir; skipifsilent skips the finished-page entries.",
        "$p = Start-Process -FilePath $setup -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait -PassThru",
        "Log \"installer exit code: $($p.ExitCode)\"",
        "if (Test-Path $appExe) { Start-Process -FilePath $appExe; Log 'relaunched' }",
        "else { Log 'app exe missing after install' }",
        "# ~370 MB must not linger; the script deletes itself last.",
        "Remove-Item $setup -ErrorAction SilentlyContinue",
        "Remove-Item $MyInvocation.MyCommand.Path -ErrorAction SilentlyContinue",
        "",
    ])
    helper = setup_path.parent / "apply_update.ps1"
    helper.write_text(script, encoding="utf-8", newline="\n")
    return helper


def apply_update(setup_path: Path, app_pid: Optional[int] = None) -> Path:
    """Spawn the detached apply helper and return its script path. Call
    this RIGHT BEFORE triggering the app's normal shutdown — the helper
    waits for our PID to disappear, then installs and relaunches."""
    if not setup_path.exists():
        raise UpdateError("setup file is missing")
    pid = int(app_pid or os.getpid())
    log_path = Path.home() / ".deeptorrent" / "logs" / "update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    helper = build_helper(setup_path, pid, os.path.abspath(sys.executable), log_path)
    # CREATE_NO_WINDOW only — combining it with DETACHED_PROCESS is an
    # INVALID CreateProcess combination on which the child dies instantly
    # with no error (found live in the 4.9 E2E; pinned by
    # test_apply_update_uses_spawnable_flags). CREATE_NO_WINDOW children
    # outlive their parent on Windows — no job object is set.
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(helper)],
        creationflags=_CREATE_NO_WINDOW,
        close_fds=True,
    )
    logger.info("update helper spawned (pid %s, setup %s)", pid, setup_path.name)
    return helper
