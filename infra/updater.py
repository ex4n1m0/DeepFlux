"""In-app auto-update for the frozen Windows and Linux builds.

Windows flow: a daily-gated feed check (``deepflux.space/deepflux/
latest.json``) returns the newest version + its setup exe's size and
sha256 → the GUI offers "Update now / Later / Skip this version" → the
setup exe is downloaded with a SEGMENTED parallel transfer (the download
engine's own ``SegmentWorker`` byte-range workers; single-stream fallback
when the server ignores Range) and verified → a detached PowerShell
helper waits for this process to exit, runs the installer silently and
relaunches the app.

Linux flow (AppImage, since the 2026-10-11 full port): same feed ritual
against ``latest-linux.json``; the verified AppImage is applied by a
detached /bin/sh helper that waits for our PID to exit, ``chmod +x`` the
new file and ``mv``s it over the running AppImage's path (POSIX rename
swaps the directory entry while the running process keeps its old inode —
no root needed as long as the AppImage lives in a user-writable spot),
then relaunches it. Non-AppImage installs (tar.gz) degrade to the
"downloaded, run it manually" path.

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
exes — same as a manual install). AppImages carry no MOTW analog at all.
The updater is deliberately NOT exposed as an agent tool: a page-borne
prompt injection must never be able to trigger an installer run.

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
FEED_URL_LINUX = "https://deepflux.space/deepflux/latest-linux.json"
FEED_BODY_LIMIT = 64 * 1024          # the feed is a few hundred bytes
CHECK_INTERVAL_SECONDS = 24 * 3600   # at most one probe per day
DOWNLOAD_CHUNK = 256 * 1024
DOWNLOAD_RETRIES = 4
_MAX_SETUP_BYTES = 2 * 1024 * 1024 * 1024  # sanity ceiling; real builds ~750 MB

# Segmented transfer (5.9): update payloads are hundreds of MB, so the
# download reuses the engine's SegmentWorker — parallel byte-range
# connections writing into one preallocated file. Sizes below the
# threshold stay on the single-stream path (probe overhead isn't worth
# it). Resume state lives in a small sidecar next to the .part file.
SEGMENT_CONNECTIONS = 6                 # parallel range connections
SEGMENT_MIN_BYTES = 4 * 1024 * 1024     # smaller payloads: single stream
SEGMENT_MIN_SPAN = 512 * 1024           # segment size floor (engine parity)
SEGMENT_RESPAWNS = 6                    # per-segment restarts per attempt
SEGMENT_POLL = 0.05                     # monitor cadence (s)
SEGMENT_FLUSH_EVERY = 2.0               # sidecar flush cadence (s)
SEGMENT_RESTART_DELAY = 1.0             # cooldown before a failed segment restarts
PROGRESS_STEP = 256 * 1024              # progress callback granularity (bytes)
DOWNLOAD_BACKOFFS = (0, 2, 5, 15)       # whole-transfer retry delays
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
    """Auto-update exists in the frozen Windows and Linux builds — source
    runs and macOS (whose binaries the feed never carries) skip it."""
    return sys.platform in ("win32", "linux") and bool(getattr(sys, "frozen", False))


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
    """Fetch + validate the platform's feed. Raises on network/API failure."""
    from dlmgr.http_client import get

    response = get(FEED_URL_LINUX if sys.platform == "linux" else FEED_URL,
                   timeout=timeout, stream=True)
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
    """Staging dir for downloaded update payloads. Windows:
    %LOCALAPPDATA%\\DeepFlux\\updates — user-writable, stable across
    reinstalls, and deliberately NOT inside ~/.deeptorrent (which the
    uninstaller wipes). Linux: the XDG state dir (~/.local/state)."""
    if sys.platform == "linux":
        return Path.home() / ".local" / "state" / "DeepFlux" / "updates"
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
    """Download the setup exe to the staging dir with a segmented parallel
    transfer (Range-resumable both within and across attempts).

    Returns the VERIFIED final path. Raises UpdateError on failure — a
    failed transfer keeps its .part file + segment-state sidecar so the
    next attempt resumes instead of restarting the ~750 MB download.
    """
    dest_dir = updates_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / info.file
    if final.exists() and verify_file(final, info):
        return final  # already downloaded (retry after a failed apply)

    part = dest_dir / (info.file + ".part")
    last_error: Optional[Exception] = None
    for attempt, backoff in enumerate(DOWNLOAD_BACKOFFS):
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
    if info.size >= SEGMENT_MIN_BYTES:
        result = _download_segmented(info, final, part, progress, cancel)
        if result is not None:
            return result
        # Server does not honor Range requests — one connection it is.
    return _download_stream(info, final, part, progress, cancel)


# --------------------------------------------------------------------------
# Segmented transfer (reuses dlmgr.segment.SegmentWorker)
# --------------------------------------------------------------------------

def _probe_ranges(info: UpdateInfo) -> Optional[tuple]:
    """Ask the server for one byte to learn whether Range requests work.

    Returns (if_range_token, total_size) — the token is the ETag (or
    Last-Modified date) used for If-Range resume validation. Returns None
    when ranges are not supported. Raises UpdateError when the deployed
    file's size disagrees with the feed (a stale feed must fail loudly,
    not download the wrong bytes and lose to the hash check)."""
    from dlmgr.http_client import get

    # 60s: the FIRST ranged GET on a cold edge took ~49s live (751 MB
    # asset, 2026-10-15 measurement) while every following one answered in
    # ~0.1s — the probe warms the URL so the parallel workers start hot.
    response = get(info.url, headers={"Range": "bytes=0-0"}, timeout=60, stream=True)
    try:
        response.raise_for_status()
        if response.status_code != 206:
            return None
        header = (response.headers.get("Content-Range") or "").strip()
        match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", header, re.IGNORECASE)
        if match is None:
            return None
        total = int(match.group(3))
        if total != info.size:
            raise UpdateError(
                f"the deployed file is {total} bytes but the feed announced "
                f"{info.size} — the update feed is stale")
        token = response.headers.get("ETag") or response.headers.get("Last-Modified") or ""
        return (token, total)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _segment_layout(size: int) -> list:
    """Deterministic byte spans — the same formula the engine uses, so a
    resumed attempt maps its saved state onto identical boundaries."""
    count = max(1, min(size // SEGMENT_MIN_SPAN, SEGMENT_CONNECTIONS))
    seg_size = size // count
    return [(i * seg_size, (i + 1) * seg_size - 1 if i < count - 1 else size - 1)
            for i in range(count)]


def _sidecar_path(part: Path) -> Path:
    return Path(str(part) + ".segs.json")


def _load_sidecar(path: Path, size: int, layout: list, if_range: str) -> Optional[list]:
    """Completed-bytes per segment, or None when the state does not apply
    (corrupt, different layout, or the remote file changed — a different
    ETag means the bytes already on disk may belong to the old artifact)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("size") != size:
        return None
    saved = data.get("segments")
    if not isinstance(saved, list) or len(saved) != len(layout):
        return None
    completed = []
    for (start, end), entry in zip(layout, saved):
        try:
            s, e, c = int(entry[0]), int(entry[1]), int(entry[2])
        except Exception:
            return None
        if (s, e) != (start, end):
            return None
        completed.append(max(0, min(c, end - start + 1)))
    if if_range and data.get("etag") and data.get("etag") != if_range:
        return None
    return completed


def _save_sidecar(path: Path, size: int, layout: list, completed: list, if_range: str) -> None:
    payload = {
        "size": size,
        "etag": if_range,
        "segments": [[s, e, c] for (s, e), c in zip(layout, completed)],
    }
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _download_segmented(
    info: UpdateInfo,
    final: Path,
    part: Path,
    progress: Optional[Callable[[int, int], None]],
    cancel: Optional[threading.Event],
) -> Optional[Path]:
    """Parallel byte-range transfer using the engine's SegmentWorker.

    Returns None when the server does not honor Range requests. All exit
    paths (success, failure, cancel) flush the sidecar, so an interrupted
    attempt resumes at range granularity on the next try."""
    from dlmgr.job import SegmentState, SegmentStatus
    from dlmgr.segment import SegmentWorker

    probe = _probe_ranges(info)
    if probe is None:
        return None
    if_range, _total = probe
    layout = _segment_layout(info.size)
    sidecar = _sidecar_path(part)
    completed = _load_sidecar(sidecar, info.size, layout, if_range)
    if completed is None:
        completed = [0] * len(layout)
        # Sparse preallocation: ONE truncation up front, before any worker
        # exists — the workers then open r+b and write at their own offsets
        # (never truncating; see the SegmentWorker no-truncate lesson).
        with open(part, "wb") as f:
            f.truncate(info.size)
        _save_sidecar(sidecar, info.size, layout, completed, if_range)

    states = []
    for i, (start, end) in enumerate(layout):
        state = SegmentState(index=i, start_byte=start, end_byte=end,
                             completed_bytes=completed[i])
        if state.completed_bytes >= state.total_bytes:
            state.status = SegmentStatus.DONE
        states.append(state)

    base = sum(s.completed_bytes for s in states)
    if progress:
        progress(base, info.size)

    lock = threading.Lock()
    delivered = [0]   # bytes fetched this attempt (for progress reporting)
    emitted = [0]

    def _on_chunk(n: int) -> None:
        with lock:
            delivered[0] += n
            if delivered[0] - emitted[0] >= PROGRESS_STEP:
                emitted[0] = delivered[0]
                if progress:
                    progress(base + delivered[0], info.size)

    workers: dict = {}
    attempts = [0] * len(states)
    failures: dict = {}
    respawn_at: dict = {}
    last_flush = time.monotonic()

    def _spawn(i: int) -> None:
        states[i].status = SegmentStatus.PENDING
        states[i].error = ""
        worker = SegmentWorker(
            url=info.url,
            segment=states[i],
            file_path=str(part),
            on_progress=_on_chunk,
            if_range=if_range,
            expected_file_size=info.size,
        )
        workers[i] = worker
        attempts[i] += 1
        worker.start()

    try:
        for i, state in enumerate(states):
            if state.status is not SegmentStatus.DONE:
                _spawn(i)

        while True:
            if cancel is not None and cancel.is_set():
                raise _Cancelled()
            now = time.monotonic()

            # Reap dead workers. A segment that didn't finish is PENDING
            # (stopped/incomplete) or ERROR — both restart from their
            # persisted completed_bytes once the cooldown passes. A worker
            # is only ever respawned after its thread is gone, so two
            # workers can never write the same range concurrently.
            for i, worker in list(workers.items()):
                if worker.is_alive():
                    if worker.is_stalled:
                        worker.stop()  # exits at its next chunk boundary
                    continue
                del workers[i]
                state = states[i]
                if state.status is SegmentStatus.DONE:
                    continue
                failures[i] = state.error or "incomplete"
                respawn_at[i] = now + SEGMENT_RESTART_DELAY

            if all(s.status is SegmentStatus.DONE for s in states) and not workers:
                break

            for i, state in enumerate(states):
                if state.status is SegmentStatus.DONE or i in workers:
                    continue
                if now < respawn_at.get(i, 0.0):
                    continue
                if attempts[i] >= SEGMENT_RESPAWNS:
                    raise UpdateError(
                        f"segment {i} kept failing: {failures.get(i, 'incomplete')}")
                _spawn(i)

            if now - last_flush >= SEGMENT_FLUSH_EVERY:
                _save_sidecar(sidecar, info.size, layout,
                              [s.completed_bytes for s in states], if_range)
                last_flush = now
            time.sleep(SEGMENT_POLL)
    finally:
        # Persist whatever was reached — success deletes the sidecar below,
        # cancel/failure keep it so the next attempt resumes.
        try:
            _save_sidecar(sidecar, info.size, layout,
                          [s.completed_bytes for s in states], if_range)
        except OSError:
            pass
        for worker in workers.values():
            worker.stop()
        for worker in workers.values():
            worker.join(timeout=15)

    if part.stat().st_size != info.size:
        raise UpdateError(f"download incomplete ({part.stat().st_size} of {info.size} bytes)")
    if progress:
        progress(info.size, info.size)
    if _sha256_of(part).lower() != info.sha256.lower():
        part.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise UpdateError("download failed the integrity check (sha256 mismatch)")
    os.replace(part, final)
    sidecar.unlink(missing_ok=True)
    return final


def _download_stream(
    info: UpdateInfo,
    final: Path,
    part: Path,
    progress: Optional[Callable[[int, int], None]],
    cancel: Optional[threading.Event],
) -> Path:
    from dlmgr.http_client import get

    if part.exists() and part.stat().st_size == info.size:
        # A complete-but-unverified .part (interrupted between the last
        # write and the hash check). Accept or discard it outright — a
        # Range request for the full size would answer 416.
        if _sha256_of(part).lower() == info.sha256.lower():
            os.replace(part, final)
            return final
        part.unlink()
    _sidecar_path(part).unlink(missing_ok=True)  # never mix resume bookkeeping

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
    """Whether the silent apply can run. Windows: the install dir (Inno
    reuses it). Linux: the AppImage file itself (the helper mv's over it)."""
    try:
        if sys.platform == "linux":
            target = appimage_path()
            if target is None:
                return False  # tar.gz/source install → manual update path
            parent = target.parent
            return parent.is_dir() and os.access(str(parent), os.W_OK)
        path = install_dir()
        return os.access(str(path), os.W_OK) and path.is_dir()
    except OSError:
        return False


def other_instances_running() -> int:
    """DeepFlux processes besides this one. A second instance holds the
    bundle's files open and would break the silent reinstall / AppImage
    replace."""
    if sys.platform == "linux":
        # The AppImage runtime sets APPIMAGE to the launched file. Only
        # probe when we actually are an AppImage (otherwise the update is a
        # manual download anyway) and match the FULL path so unrelated
        # processes that merely mention "DeepFlux" don't block the update.
        target = os.environ.get("APPIMAGE", "")
        if not target:
            return 0
        try:
            out = subprocess.run(
                ["pgrep", "-f", target], capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception:
            return 0  # do not block the update on a failed probe
        # pgrep never matches itself, but our own mount/launcher lineage
        # might — exclude our PID before counting.
        pids = {int(p) for p in out.split() if p.isdigit()}
        pids.discard(os.getpid())
        return max(0, len(pids))
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


def appimage_path() -> Optional[Path]:
    """Path of the running AppImage, or None (source run / tar.gz install).

    The AppImage runtime exports APPIMAGE; inside the mounted squashfs,
    sys.executable points at the mount dir, which must never be written."""
    raw = os.environ.get("APPIMAGE", "")
    if raw and os.path.isfile(raw):
        return Path(raw)
    return None


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


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
        "# Exit code 5 = a suppressed dialog defaulted to cancel during the",
        "# actual install (live 2026-10-20: a transient file lock on the",
        "# install dir) — retry once after a settle delay; the locker is",
        "# usually gone by then, and a second 5 gives up like before.",
        "$maxAttempts = 2",
        "for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {",
        "  if ($attempt -gt 1) { Log 'installer exit 5 — retrying once after 15s'; Start-Sleep -Seconds 15 }",
        "  $p = Start-Process -FilePath $setup -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait -PassThru",
        "  Log \"installer exit code: $($p.ExitCode)\"",
        "  if ($p.ExitCode -ne 5) { break }",
        "}",
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


def build_posix_helper(new_path: Path, app_pid: int, target: Path,
                       log_path: Path) -> Path:
    """Write the detached apply script for the AppImage flow.

    POSIX sh (always present on Linux) can poll a PID, swap the file and
    relaunch. The replace is an ``mv`` over the AppImage path — the running
    process keeps its old mount (inode), so the swap is safe without
    waiting for the squashfs to unmount."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    script = "\n".join([
        "#!/bin/sh",
        f"APP_PID={int(app_pid)}",
        f"NEW={_sh_quote(str(new_path))}",
        f"TARGET={_sh_quote(str(target))}",
        f"LOG={_sh_quote(str(log_path))}",
        "log() { printf '%s %s\\n' \"$(date -Is)\" \"$1\" >> \"$LOG\"; }",
        "# Wait for the app to exit — its graceful shutdown saves download",
        "# resume data and the config.",
        "deadline=$(( $(date +%s) + 90 ))",
        "while kill -0 \"$APP_PID\" 2>/dev/null; do",
        "  if [ \"$(date +%s)\" -gt \"$deadline\" ]; then log \"app $APP_PID still running after 90s\"; exit 1; fi",
        "  sleep 1",
        "done",
        "log \"app exited; applying update\"",
        "chmod +x \"$NEW\" || { log 'chmod failed'; exit 1; }",
        "mv -f \"$NEW\" \"$TARGET\" || { log 'replace failed'; exit 1; }",
        "log \"replaced $TARGET\"",
        "nohup \"$TARGET\" >/dev/null 2>&1 &",
        "log \"relaunched\"",
        "rm -f \"$0\"",
        "",
    ])
    helper = new_path.parent / "apply_update.sh"
    helper.write_text(script, encoding="utf-8", newline="\n")
    helper.chmod(0o755)
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
    if sys.platform == "linux":
        target = appimage_path()
        if target is None:
            raise UpdateError(
                "not running as an AppImage — apply the downloaded file manually")
        if not setup_path.name.endswith(".AppImage"):
            raise UpdateError(f"unexpected update payload: {setup_path.name}")
        helper = build_posix_helper(setup_path, pid, target, log_path)
        # start_new_session detaches the helper from our process group so it
        # survives our exit (and never receives our signals).
        subprocess.Popen(
            ["/bin/sh", str(helper)],
            start_new_session=True,
            close_fds=True,
        )
        logger.info("update helper spawned (pid %s, target %s)", pid, target)
        return helper
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
