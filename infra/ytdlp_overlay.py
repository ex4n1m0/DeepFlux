"""In-app update of the bundled yt-dlp — Windows and Linux frozen builds.

THE PROBLEM. All 1,044 yt_dlp modules of a frozen build are compiled into
DeepFlux.exe's embedded Python archive; nothing on disk can be swapped.
YouTube breaks old yt-dlp releases every few weeks, so between DeepFlux
releases the YouTube lane rots (dlmgr/ytdlp_update.py could only say
"update DeepFlux").

THE MECHANISM (proven 2026-11-25 in a throwaway PyInstaller build whose
import code is identical to the shipped 9.7 exe). A newer yt_dlp tree
written to the user's data dir takes precedence at import time through ONE
scoped sys.meta_path finder — it answers only ``yt_dlp`` and
``yt_dlp.*``, adds nothing to sys.path, and sits ahead of PyInstaller's
own finder. Measured: every module loads from the override, a real
YouTube download came out byte-identical to the bundled copy's, and the
import takes as long as the bundled one when the tree is pre-compiled
(the frozen app never writes bytecode caches itself — hence the
pre-compile step below).

THE RULES — do not regress:
* ``import_yt_dlp()`` is the ONLY way app code imports yt_dlp (pinned by
  tests/test_ytdlp_overlay.py). It decides ONCE per process, and an
  override is used only when it is STRICTLY newer than the bundled copy —
  an app update that ships an equal or newer yt-dlp retires it by itself.
* A broken override can never take yt-dlp down: the first import falls
  back to the bundle, the override is switched off and recorded in
  ``skipped.json``, and automatic mode never retries that release.
* Nothing is switched on before it passed a start-up check in a CHILD
  process of this same app — a crash there cannot touch the running app.
* Trust root = the owner-pinned feed on deepflux.space (HTTPS): size AND
  sha256 must match before a byte is extracted, the same posture as
  infra/updater.py. The wheel is still read defensively (zip-slip,
  symlinks, size and member caps, per-file RECORD digests).
* Never an agent tool, and the automatic-mode switch is denied to the
  agent settings tool (agent/tools.py) — a prompt injection must never
  switch on code installation.

Qt-free and dependency-free at import (stdlib only; the HTTP client is
imported inside the download function, like infra/updater.py), because
the start-up check runs this module in a bare child process.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import importlib
import importlib.util
import io
import json
import logging
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

FEED_URL = "https://deepflux.space/deepflux/latest-ytdlp.json"
FEED_HOST = "deepflux.space"
FEED_BODY_LIMIT = 16 * 1024             # the feed is a few hundred bytes
DOWNLOAD_HOST = "files.pythonhosted.org"
DOWNLOAD_CHUNK = 256 * 1024
MIN_WHEEL_BYTES = 64 * 1024             # sanity floor; real wheels are ~3 MB
MAX_WHEEL_BYTES = 64 * 1024 * 1024      # sanity ceiling
MAX_UNPACKED_BYTES = 192 * 1024 * 1024  # the unpacked yt_dlp tree is ~40 MB
MAX_FILE_BYTES = 32 * 1024 * 1024       # the largest file in the package is ~220 KB
MAX_MEMBERS = 5000                      # the wheel carries ~1,060 entries
SELFCHECK_TIMEOUT = 90.0                # a cold start under AV scanning is slow
STALE_STAGING_SECONDS = 24 * 3600
TRANSIENT_LIMIT = 3                     # local transient failures before a release is given up

POINTER_NAME = "current.json"
SKIP_NAME = "skipped.json"
TRANSIENT_NAME = "transient.json"
SKIP_STAGES = ("install", "selfcheck", "load", "user")

# Modules the start-up check imports on top of the package itself: the
# code paths a DeepFlux download goes through (they load lazily otherwise).
_DEEP_CHECK_MODULES = (
    "yt_dlp.extractor.youtube", "yt_dlp.extractor.generic",
    "yt_dlp.networking._curlcffi", "yt_dlp.downloader.http",
    "yt_dlp.postprocessor.ffmpeg")

_VERSION_RE = re.compile(r"^\d{4}(?:\.\d{1,2})+$")  # CalVer: 2026.11.23
_VERSION_LINE_RE = re.compile(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.M)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# An installed release directory: <version>-<first 8 hex of the wheel digest>.
_RELEASE_DIR_RE = re.compile(r"^\d{4}(?:\.\d{1,2})+-[0-9a-f]{8}$")

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Windows cannot create these as ordinary file names; an archive that
# carries one is not an archive we want to unpack.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)})


class OverlayError(RuntimeError):
    """A permanent, release-specific install failure. The release is recorded
    in ``skipped.json`` so automatic mode does not retry it forever."""


class TransientUpdateError(RuntimeError):
    """A failure that proves nothing against the release: being offline,
    or (see _LocalTransientError) this machine could not run the checks.
    Nothing is recorded by the failure alone — a later probe may retry."""


class _LocalTransientError(TransientUpdateError):
    """The start-up check could not start, timed out or died without a
    result, or the release folder could not be moved into place. Counted
    per release (TRANSIENT_LIMIT) so a really broken release is still
    given up on."""


class _SelfcheckError(OverlayError):
    """The release failed its start-up check in the child process."""


class _Cancelled(Exception):
    """Raised inside the install pipeline when the caller cancels."""


# --------------------------------------------------------------------------
# Versions and locations
# --------------------------------------------------------------------------

def version_tuple(version: str) -> Optional[tuple[int, ...]]:
    """Parse a CalVer yt-dlp version ("2026.11.23") to a comparable tuple,
    or None when unparseable."""
    try:
        return tuple(int(part) for part in version.strip().split("."))
    except ValueError:
        return None


def is_newer(candidate: str, current: str) -> bool:
    """True when `candidate` sorts after `current` (padded to equal length).
    Fails closed: anything unparseable is never "newer"."""
    a, b = version_tuple(candidate), version_tuple(current)
    if not a or not b:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def _read_version_file(path: str) -> str:
    """``__version__`` out of a yt_dlp/version.py, read as TEXT (never
    imported). "" when the file is missing or holds no valid version."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read(4096)
    except OSError:
        return ""
    match = _VERSION_LINE_RE.search(text)
    if not match or not _VERSION_RE.match(match.group(1)):
        return ""
    return match.group(1)


def _bundled_package_dir() -> str:
    """Directory of the yt_dlp package the app SHIPPED with."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        # The frozen bundle ships the package's sources as inert data next
        # to the compiled modules (collect_all datas) — read, never import.
        candidate = os.path.join(meipass, "yt_dlp")
        return candidate if os.path.isfile(os.path.join(candidate, "version.py")) else ""
    try:
        spec = importlib.util.find_spec("yt_dlp")
    except (ImportError, AttributeError, ValueError):
        return ""
    for location in getattr(spec, "submodule_search_locations", None) or []:
        if os.path.isfile(os.path.join(location, "version.py")):
            return location
    return ""


def bundled_version() -> str:
    """Version of the yt-dlp the app shipped with ("" when it cannot be
    read — the override is then disabled and the bundle alone is used)."""
    package_dir = _bundled_package_dir()
    if not package_dir:
        return ""
    return _read_version_file(os.path.join(package_dir, "version.py"))


def supported() -> bool:
    """The override exists for the frozen Windows and Linux builds only —
    source checkouts use their venv's package (pip is the updater there)."""
    return sys.platform in ("win32", "linux") and bool(getattr(sys, "frozen", False))


def can_override() -> bool:
    """True when a downloaded release could actually take effect on this
    build (supported platform AND the bundled version is readable)."""
    return supported() and bool(bundled_version())


def overlay_dir() -> Path:
    """Where downloaded releases live: ~/.deeptorrent/ytdlp — user data
    that the uninstaller removes with the rest of ~/.deeptorrent. Tests
    redirect it with DF_YTDLP_OVERLAY_DIR."""
    override = os.environ.get("DF_YTDLP_OVERLAY_DIR")
    if override:
        return Path(override)
    return Path.home() / ".deeptorrent" / "ytdlp"


def _is_under(path: str, root: str) -> bool:
    if not path or not root:
        return False
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r + os.sep)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _read_json(path: Path, limit: int = 16 * 1024) -> Optional[dict]:
    try:
        if path.stat().st_size > limit:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, data: dict) -> None:
    """temp + os.replace in the target's own directory — the same atomic
    write rule as config.to_file (a truncated pointer must never load)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")     # match the tracked feed files' style
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        _silent_unlink(Path(tmp))
        raise
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# The scoped import hook
# --------------------------------------------------------------------------

class _YtdlpOnlyFinder:
    """``sys.meta_path`` finder that resolves ONLY ``yt_dlp`` and
    ``yt_dlp.*`` from one directory tree. Nothing is added to ``sys.path``,
    so no other module name can be shadowed through the override, and every
    name part must be a plain ASCII identifier — a dotted name can never be
    mapped onto a path separator."""

    def __init__(self, package_root: str) -> None:
        self._root = package_root

    def find_spec(self, fullname: str, path=None, target=None):
        if fullname != "yt_dlp" and not fullname.startswith("yt_dlp."):
            return None
        if not os.path.isdir(self._root):
            # The release directory vanished under a running app (deleted by
            # hand or quarantined): step aside and let the normal search run.
            # Returning None here is safe — nothing is removed from
            # sys.meta_path while the import machinery is iterating it.
            return None
        parts = fullname.split(".")
        if not all(part.isascii() and part.isidentifier() for part in parts):
            return None
        base = os.path.join(self._root, *parts[1:])
        init = os.path.join(base, "__init__.py")
        if os.path.isfile(init):
            return importlib.util.spec_from_file_location(
                fullname, init, submodule_search_locations=[base])
        if len(parts) == 1:
            # The package itself is gone or damaged since it was chosen: fail
            # LOUDLY, so the first-import safety net falls back to the bundle
            # instead of letting the search continue to another copy.
            raise ModuleNotFoundError(f"No module named 'yt_dlp' in {self._root!r}")
        if os.path.isfile(base + ".py"):
            return importlib.util.spec_from_file_location(fullname, base + ".py")
        # A submodule missing from the override is resolved nowhere else: the
        # parent package's __path__ holds this tree only, so Python itself
        # raises ModuleNotFoundError (pinned by test).
        return None


_import_lock = threading.RLock()
_decided = False                                  # the one-time decision was made
_finder: Optional[_YtdlpOnlyFinder] = None        # installed while the override is in use


def _pointer() -> dict:
    data = _read_json(overlay_dir() / POINTER_NAME)
    if not data:
        return {}
    version = str(data.get("version") or "")
    sha = str(data.get("sha256") or "")
    name = str(data.get("dir") or "")
    if (not _VERSION_RE.match(version) or not _SHA256_RE.match(sha)
            or name != f"{version}-{sha[:8]}"):
        return {}
    return {"version": version, "dir": name, "sha256": sha}


def _active_dir() -> str:
    """Directory of the override THIS process should load, or "" (then the
    bundle is used). A pointer only counts when it names a complete
    installed release that is STRICTLY newer than the bundled copy."""
    if not supported():
        return ""
    pointer = _pointer()
    if not pointer:
        return ""
    candidate = overlay_dir() / "versions" / pointer["dir"]
    manifest = _read_json(candidate / "manifest.json") or {}
    if manifest.get("version") != pointer["version"]:
        return ""
    if not os.path.isfile(candidate / "yt_dlp" / "__init__.py"):
        return ""
    if _read_version_file(str(candidate / "yt_dlp" / "version.py")) != pointer["version"]:
        return ""
    bundled = bundled_version()
    if not bundled or not is_newer(pointer["version"], bundled):
        return ""
    return str(candidate)


def active_version() -> str:
    """Version of the override in use in this process, or "" (the bundle)."""
    active = _active_dir()
    return _read_version_file(os.path.join(active, "yt_dlp", "version.py")) if active else ""


def import_yt_dlp():
    """THE way app code imports yt-dlp.

    Decides once per process whether a downloaded release (strictly newer
    than the bundled copy) is loaded instead of the bundle, with the safety
    net: if that release fails on its very first import, the hook is
    removed, every module it had already loaded is forgotten, the release
    is switched off and recorded, and the bundled copy is imported instead.
    """
    global _decided, _finder
    with _import_lock:
        if not _decided:
            _decided = True
            # A yt_dlp already imported by someone else is left alone: this
            # process then runs entirely on that copy.
            if "yt_dlp" not in sys.modules:
                try:
                    active = _active_dir()
                except Exception:
                    logger.debug("yt-dlp overlay check failed", exc_info=True)
                    active = ""
                if active and not _verify_release_integrity(active):
                    logger.warning("the downloaded yt-dlp failed its integrity check; "
                                   "the bundled copy is used instead")
                    active = ""
                if supported():
                    # The one safe moment to drop retired releases: nothing
                    # is loaded in THIS process yet. install()/revert() never
                    # delete — a running DeepFlux keeps importing extractors
                    # lazily from the release it started with.
                    _drop_other_releases(overlay_dir() / "versions",
                                         keep=Path(active).name if active else "")
                if active:
                    _finder = _YtdlpOnlyFinder(os.path.join(active, "yt_dlp"))
                    sys.meta_path.insert(0, _finder)
                    try:
                        module = importlib.import_module("yt_dlp")
                    except Exception as exc:
                        _recover_from_broken_overlay(exc)
                    else:
                        logger.info("yt-dlp %s loaded from an in-app update",
                                    getattr(getattr(module, "version", None),
                                            "__version__", "?"))
                        return module
        return importlib.import_module("yt_dlp")


def _recover_from_broken_overlay(exc: BaseException) -> None:
    global _finder
    try:
        sys.meta_path.remove(_finder)
    except ValueError:
        pass
    _finder = None
    for name in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.")]:
        sys.modules.pop(name, None)
    try:
        pointer = _pointer()
        _mark_skipped("load", f"{type(exc).__name__}: {exc}",
                      pointer.get("version", ""), pointer.get("sha256", ""))
    except Exception:
        logger.debug("could not record the broken yt-dlp release", exc_info=True)
    logger.warning("The downloaded yt-dlp failed to load and was switched off; "
                   "using the bundled copy (%s)", exc)


def _mark_skipped(stage: str, error: str, version: str = "", sha256: str = "") -> None:
    """Record a release automatic mode must not retry (``stage`` says why).

    Single slot by design: a later record replaces an earlier one (so a
    forced install of Y that fails overwrites the user's "stay on bundled"
    choice for X — that only matters if the feed ever re-serves X, and the
    user can choose again).

    The active pointer is dropped ONLY when it points at this very release —
    the "load" case, where the pointer IS the broken release. A failed
    install of some OTHER release must leave a healthy installed release
    switched on (security review 2026-11-26, M1)."""
    write_json_atomic(overlay_dir() / SKIP_NAME, {
        "version": version, "sha256": sha256, "stage": stage,
        "error": error[:300], "at": _utc_now()})
    pointer = _pointer()
    if pointer and pointer.get("version") == version and pointer.get("sha256") == sha256:
        _silent_unlink(overlay_dir() / POINTER_NAME)


def skipped() -> Optional[dict]:
    """The recorded skipped release, or None: ``{"version", "sha256",
    "stage", "error"}`` with stage in SKIP_STAGES."""
    data = _read_json(overlay_dir() / SKIP_NAME)
    if not data:
        return None
    version = str(data.get("version") or "")
    sha = str(data.get("sha256") or "")
    stage = str(data.get("stage") or "")
    if not _VERSION_RE.match(version) or not _SHA256_RE.match(sha) or stage not in SKIP_STAGES:
        return None
    return {"version": version, "sha256": sha, "stage": stage,
            "error": str(data.get("error") or "")[:300]}


def is_skipped(info: "YtdlpUpdateInfo") -> bool:
    """True when this exact release (version AND digest) was recorded as
    skipped — a failed start-up check, a load failure, or the user's own
    "Use bundled version"."""
    data = skipped()
    return bool(data and data.get("version") == info.version
                and data.get("sha256") == info.sha256)


def status() -> dict:
    """What the UI shows: ``{"supported", "bundled", "active", "skipped"}``."""
    return {"supported": supported(),
            "bundled": bundled_version(),
            "active": active_version(),
            "skipped": skipped()}


# --------------------------------------------------------------------------
# Feed
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class YtdlpUpdateInfo:
    version: str
    file: str
    size: int
    sha256: str
    url: str
    released: str = ""

    @property
    def dir_name(self) -> str:
        """Directory name of an installed release: version + digest prefix."""
        return f"{self.version}-{self.sha256[:8]}"


def _is_pypi_wheel_url(url: str, file_name: str) -> bool:
    """Only PyPI's own file host, over HTTPS, on the canonical content path
    that ends in the pinned file name — no credentials, port, query or
    fragment."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.hostname != DOWNLOAD_HOST:
        return False
    if parts.username or parts.password or port not in (None, 443):
        return False
    if parts.query or parts.fragment:
        return False
    # PyPI's content path: /packages/<2 hex>/<2 hex>/<content hash>/<file>.
    # The hash segment measured 60 hex characters in 2026-11; its length is
    # PyPI's business (the integrity comes from the pinned sha256), so only
    # its shape is pinned here.
    return bool(re.fullmatch(
        r"/packages/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{32,128}/" + re.escape(file_name),
        parts.path))


def parse_feed(body: bytes) -> Optional[YtdlpUpdateInfo]:
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
    url = str(data.get("url") or "")
    if not _VERSION_RE.match(version):
        return None
    # The file name is derived from the version and compared verbatim, so
    # it can never carry a separator or a traversal fragment.
    if file_name != f"yt_dlp-{version}-py3-none-any.whl":
        return None
    if not _SHA256_RE.match(sha):
        return None
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        return None
    if size < MIN_WHEEL_BYTES or size > MAX_WHEEL_BYTES:
        return None
    if not _is_pypi_wheel_url(url, file_name):
        return None
    return YtdlpUpdateInfo(version=version, file=file_name, size=size, sha256=sha,
                           url=url, released=str(data.get("released") or "")[:32])


def fetch_feed(timeout: float = 10.0) -> YtdlpUpdateInfo:
    """Fetch + validate the owner-pinned feed. Raises OverlayError on a
    network or format failure."""
    from dlmgr.http_client import get

    response = get(FEED_URL, timeout=timeout, stream=True)
    try:
        response.raise_for_status()
        # Pin the ORIGIN we ended up on: get() follows redirects (each hop
        # re-validated as public, but not as ours), and the feed is the one
        # payload here with no digest of its own.
        final = urlsplit(str(getattr(response, "url", "") or ""))
        if final.scheme != "https" or final.hostname != FEED_HOST:
            raise OverlayError("yt-dlp feed came from an unexpected origin")
        body = b""
        for chunk in response.iter_content(4096):
            if not chunk:
                continue
            body += chunk
            if len(body) > FEED_BODY_LIMIT:
                raise OverlayError("yt-dlp feed larger than expected")
    finally:
        try:
            response.close()
        except Exception:
            pass
    info = parse_feed(body)
    if info is None:
        raise OverlayError("yt-dlp feed is malformed")
    return info


# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------

def _download(info: YtdlpUpdateInfo, dest: Path,
              progress: Optional[Callable[[int, int], None]],
              cancel: Optional[threading.Event]) -> None:
    """Stream the wheel into dest (through a .part file), hashing on the
    fly; the size and the sha256 must match the feed before dest exists."""
    from dlmgr.http_client import get

    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    written = 0

    def _chunks(response):
        # A failure INSIDE the network stream means "retry later"; one in
        # the loop body below (disk full, cancel, size) keeps its meaning.
        try:
            for chunk in response.iter_content(DOWNLOAD_CHUNK):
                yield chunk
        except Exception as exc:
            raise TransientUpdateError(f"download failed: {exc}") from exc

    response = None
    try:
        try:
            response = get(info.url, timeout=60, stream=True)
            response.raise_for_status()
        except Exception as exc:
            raise TransientUpdateError(f"download failed: {exc}") from exc
        if response.status_code != 200:
            raise TransientUpdateError(f"unexpected response {response.status_code}")
        _silent_unlink(part)
        with open(part, "wb") as fh:
            for chunk in _chunks(response):
                if cancel is not None and cancel.is_set():
                    raise _Cancelled()
                if not chunk:
                    continue
                written += len(chunk)
                if written > info.size:
                    # A middlebox or proxy can pad a response just as easily
                    # as a bad pin can: counted, not an instant verdict.
                    raise _LocalTransientError("download is larger than the pinned size")
                digest.update(chunk)
                fh.write(chunk)
                if progress:
                    progress(written, info.size)
    except BaseException:
        _silent_unlink(part)
        raise
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
    if written != info.size:
        raise TransientUpdateError(f"download incomplete ({written} of {info.size} bytes)")
    if digest.hexdigest() != info.sha256:
        # Never activated either way; classified as local because a
        # TLS-intercepting proxy produces this on every attempt.
        raise _LocalTransientError("download failed the integrity check (sha256 mismatch)")
    os.replace(part, dest)


def _member_parts(name: str) -> list:
    """Split an archive member name into safe path components, or raise."""
    if (not name or name.startswith("/") or "\\" in name or ":" in name
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        raise OverlayError("the downloaded package contains an unsafe path")
    for part in name.split("/"):
        # Empty / dot components and trailing dots or spaces: Windows drops
        # the latter, which would let two names land on one file.
        if part in ("", ".", "..") or part != part.rstrip(" ."):
            raise OverlayError("the downloaded package contains an unsafe path")
        if part.split(".")[0].upper() in _RESERVED_NAMES:
            raise OverlayError("the downloaded package contains an unsafe path")
    return name.split("/")


def _record_digests(zf: zipfile.ZipFile) -> dict:
    """path -> (sha256 hex, size) from the wheel's RECORD file."""
    records = [i.filename for i in zf.infolist() if i.filename.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        raise OverlayError("the wheel has no single RECORD file")
    digests = {}
    try:
        for row in csv.reader(io.StringIO(zf.read(records[0]).decode("utf-8"))):
            if not row:
                continue
            if len(row) != 3:
                raise OverlayError("malformed RECORD entry")
            path, digest, size = row[0], row[1].strip(), row[2].strip()
            if not digest:
                continue  # RECORD's own row carries no hash; any package file without one is rejected below
            algorithm, _, encoded = digest.partition("=")
            if algorithm != "sha256" or not encoded:
                raise OverlayError("unsupported RECORD digest")
            hexdigest = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).hex()
            digests[path] = (hexdigest, int(size))
    except OverlayError:
        raise
    except Exception as exc:
        raise OverlayError(f"malformed RECORD file: {exc}") from exc
    return digests


def _extract_wheel(wheel: Path, candidate: Path) -> int:
    """Unpack ONLY the ``yt_dlp/`` tree into candidate/, verifying every
    file against the wheel's RECORD (part of the digest-pinned wheel).
    Returns the number of files written."""
    try:
        zf = zipfile.ZipFile(wheel)
    except (zipfile.BadZipFile, OSError) as exc:
        raise OverlayError(f"the downloaded file is not a valid wheel: {exc}") from exc
    written = 0
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise OverlayError("the wheel holds too many entries")
        if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
            raise OverlayError("the wheel unpacks larger than expected")
        digests = _record_digests(zf)
        for member in infos:
            name = member.filename
            _member_parts(name)  # every member is name-checked, kept or not
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise OverlayError("the wheel contains a symbolic link")
            if not name.startswith("yt_dlp/") or name.endswith("/"):
                continue
            expected = digests.get(name)
            if expected is None:
                raise OverlayError(f"{name} is not listed in the wheel's RECORD")
            if not 0 <= expected[1] <= MAX_FILE_BYTES:
                raise OverlayError(f"{name} claims an implausible size")
            # Bounded read: at most one byte over the RECORD size, so a
            # lying header cannot stream an unbounded body to disk.
            with zf.open(member) as stream:
                data = stream.read(expected[1] + 1)
            if (len(data) != expected[1]
                    or hashlib.sha256(data).hexdigest() != expected[0]):
                raise OverlayError(f"{name} does not match its RECORD digest")
            target = candidate.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)
            written += 1
    if not os.path.isfile(candidate / "yt_dlp" / "__init__.py"):
        raise OverlayError("the wheel does not contain the yt_dlp package")
    if not _read_version_file(str(candidate / "yt_dlp" / "version.py")):
        raise OverlayError("the package carries no valid version")
    return written


def _precompile(package_dir: Path, dfile_root: Optional[str] = None) -> int:
    """Compile every source file to bytecode. The frozen app never writes
    caches (sys.dont_write_bytecode), so an uncompiled tree would be
    re-compiled from source on every start (measured 0.38 s vs 0.20 s) —
    and a file that does not compile is caught HERE, before activation.
    ``dfile_root`` is where the tree will live once it is moved into place,
    so tracebacks do not point at a staging folder that no longer exists."""
    count = 0
    try:
        for base, _dirs, files in os.walk(package_dir):
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                source = os.path.join(base, name)
                cfile = importlib.util.cache_from_source(source)
                os.makedirs(os.path.dirname(cfile), exist_ok=True)
                dfile = (os.path.join(dfile_root, os.path.relpath(source, package_dir.parent))
                         if dfile_root else source)
                py_compile.compile(source, cfile=cfile, dfile=dfile, doraise=True)
                count += 1
    except py_compile.PyCompileError as exc:
        raise OverlayError(f"a file in the new downloader does not compile: {exc}") from exc
    return count


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(package_dir) -> str:
    """sha256 over an unpacked release: every file's relative path and
    content digest, in a deterministic order, ``__pycache__`` excluded (it is
    derived, and Python revalidates it against its source anyway).

    Measured on the real 2026.11.23 tree (1,046 files / 10 MB): ~150-180 ms,
    stable across runs — cheap enough to pay once per start, and it is what
    makes a release that was damaged AFTER activation detectable before any
    of its code runs."""
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(package_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            path = os.path.join(base, name)
            rel = os.path.relpath(path, str(package_dir)).replace(os.sep, "/")
            digest.update(rel.encode("utf-8") + b"\0" + _sha256_file(path).encode() + b"\n")
    return digest.hexdigest()


def _verify_release_integrity(release: str) -> bool:
    """Re-hash an installed release before ANY of its code runs.

    The first-import safety net only covers the package's own import: yt-dlp
    loads most extractors lazily, so a release damaged after activation (a
    disk error, an antivirus quarantine, a partial cleanup) would otherwise
    surface much later as a failed download with no fallback. A missing or
    mismatched tree digest fails CLOSED — the bundled copy is used and the
    release is recorded as skipped."""
    manifest = _read_json(Path(release) / "manifest.json", limit=1024 * 1024) or {}
    expected = str(manifest.get("tree_sha256") or "")
    pointer = _pointer()
    try:
        actual = tree_digest(Path(release) / "yt_dlp")
    except OSError as exc:
        _record_broken("load", f"integrity check could not read the release: {exc}", pointer)
        return False
    if not _SHA256_RE.match(expected) or actual != expected:
        _record_broken("load", "integrity check failed: the downloaded files no longer match",
                       pointer)
        return False
    return True


def _record_broken(stage: str, error: str, pointer: dict) -> None:
    try:
        _mark_skipped(stage, error, pointer.get("version", ""), pointer.get("sha256", ""))
    except Exception:
        logger.debug("could not record the unusable yt-dlp release", exc_info=True)


def _selfcheck_command(candidate: Path) -> list:
    if getattr(sys, "frozen", False):
        # The running app itself. Inside an AppImage sys.executable is the
        # ephemeral mount's binary — fine for a child that only lives while
        # this process does; $APPIMAGE is the documented handle.
        exe = os.environ.get("APPIMAGE") or sys.executable
        return [exe, "--ytdlp-selfcheck", str(candidate)]
    # Source checkout: only tests and the pin script get here.
    return [sys.executable, "-m", "infra.ytdlp_overlay", "--selfcheck", str(candidate)]


def _child_env() -> dict:
    """Environment for the start-up-check child.

    A frozen child must inherit ours (its bootloader needs the surrounding
    environment — APPIMAGE on Linux, SystemRoot/TEMP on Windows — and
    PyInstaller sets sys.path itself, so PYTHONPATH cannot redirect it).
    A source-run child is a plain interpreter, so the PYTHON* variables that
    could redirect its imports are dropped there."""
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            env.pop(name, None)
    return env


def _run_selfcheck(candidate: Path) -> dict:
    """Start-up check of a downloaded release in a child process of this
    app.

    _SelfcheckError (recorded, permanent) only when the child itself
    reports what went wrong INSIDE the candidate. A check that could not
    start, timed out, or died without a result proves nothing against the
    release — an antivirus hold or a busy machine can cause all three — so
    those raise _LocalTransientError (counted, see _note_transient_failure)."""
    result_path = Path(str(candidate) + ".selfcheck.json")
    _silent_unlink(result_path)  # never read a previous attempt's result
    options = {"env": _child_env()}
    if not getattr(sys, "frozen", False):
        options["cwd"] = _REPO_ROOT
    try:
        proc = subprocess.run(_selfcheck_command(candidate), capture_output=True,
                              timeout=SELFCHECK_TIMEOUT,
                              creationflags=_CREATE_NO_WINDOW, **options)
    except subprocess.TimeoutExpired as exc:
        raise _LocalTransientError(
            "the new downloader did not finish its start-up check in time") from exc
    except OSError as exc:
        raise _LocalTransientError(f"could not start the start-up check: {exc}") from exc
    result = _read_json(result_path)
    _silent_unlink(result_path)
    if isinstance(result, dict) and result.get("ok") and proc.returncode == 0:
        return result
    if isinstance(result, dict) and result.get("error"):
        raise _SelfcheckError(
            f"the new downloader failed its start-up check ({str(result['error'])[:300]})")
    raise _LocalTransientError(
        f"the start-up check ended without a result (exit code {proc.returncode})")


def prepare_release(wheel: Path, info: YtdlpUpdateInfo, work: Path,
                    dfile_root: Optional[str] = None) -> dict:
    """Everything between "the verified wheel is on disk" and "ready to
    switch on": unpack it (every file checked against the wheel's RECORD),
    pre-compile it, and run the start-up check in a child process. `work`
    is the caller's private directory; the candidate tree lands in
    work/release. Returns the check's result. install() and
    packaging/pin_ytdlp.py share this, so a pinned release is proven with
    exactly the checks the app applies later."""
    candidate = work / "release"
    _extract_wheel(wheel, candidate)
    if _read_version_file(str(candidate / "yt_dlp" / "version.py")) != info.version:
        raise OverlayError("the downloaded release reports a different version than the feed")
    _precompile(candidate / "yt_dlp", dfile_root)
    check = _run_selfcheck(candidate)
    if check.get("version") != info.version:
        raise OverlayError("the downloaded release reports a different version than the feed")
    return check


def _drop_stale_staging(staging: Path) -> None:
    """Work dirs of attempts that crashed long ago (a concurrent install's
    fresh dir is never touched)."""
    cutoff = time.time() - STALE_STAGING_SECONDS
    try:
        entries = list(staging.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def _publish(candidate: Path, final: Path, info: YtdlpUpdateInfo) -> Path:
    """Move the checked release into versions/ under its final name.
    Another DeepFlux window may have installed the very same release
    meanwhile — then ours is dropped and that one is used."""
    def _same() -> bool:
        manifest = _read_json(final / "manifest.json") or {}
        return (manifest.get("version") == info.version
                and manifest.get("sha256") == info.sha256)

    if final.exists() and not _same():
        shutil.rmtree(final, ignore_errors=True)
    try:
        os.replace(candidate, final)
    except OSError as exc:
        if not _same():
            # A sharing violation or an antivirus hold — this machine's
            # state, not the release's: transient.
            raise _LocalTransientError(
                f"could not store the new downloader ({exc})") from exc
        shutil.rmtree(candidate, ignore_errors=True)
    return final


def _drop_other_releases(versions: Path, keep: str) -> None:
    """Remove every release directory except `keep`. Called ONLY from the
    one-time import decision, before this process has loaded anything — a
    running DeepFlux keeps importing extractors lazily from the release it
    started with, so install() and revert() never delete.

    Another window may still be running on a retired release, so the sweep is
    skipped while another instance is alive (and when that cannot be
    determined). The probe only runs when there is actually something to
    collect, so the usual one-release case never spawns a process."""
    try:
        entries = [entry for entry in versions.iterdir()
                   if entry.name != keep and _RELEASE_DIR_RE.match(entry.name)]
    except OSError:
        return
    if not entries:
        return
    try:
        from infra.updater import other_instances_running
        if other_instances_running() > 0:
            logger.debug("yt-dlp: keeping retired releases — another DeepFlux window is open")
            return
    except Exception:
        logger.debug("yt-dlp: instance probe failed; keeping retired releases", exc_info=True)
        return
    for entry in entries:
        shutil.rmtree(entry, ignore_errors=True)


def _note_transient_failure(info: YtdlpUpdateInfo, error: str) -> None:
    """Count the LOCAL transient failures (start-up check could not run,
    folder could not be renamed) of one release; after TRANSIENT_LIMIT the
    release is recorded as skipped so automatic mode stops retrying it.
    Network failures never get here — being offline must not block a
    release."""
    try:
        data = _read_json(overlay_dir() / TRANSIENT_NAME) or {}
        same = data.get("version") == info.version and data.get("sha256") == info.sha256
        try:
            count = int(data.get("count") or 0) + 1 if same else 1
        except (TypeError, ValueError):
            count = 1
        if count >= TRANSIENT_LIMIT:
            _silent_unlink(overlay_dir() / TRANSIENT_NAME)
            _mark_skipped("install", f"failed {count} times: {error}",
                          info.version, info.sha256)
            return
        write_json_atomic(overlay_dir() / TRANSIENT_NAME, {
            "version": info.version, "sha256": info.sha256, "count": count,
            "error": error[:300], "at": _utc_now()})
    except Exception:
        logger.debug("could not record the transient yt-dlp failure", exc_info=True)


_install_lock = threading.Lock()


def install(info: YtdlpUpdateInfo,
            progress: Optional[Callable[[int, int], None]] = None,
            cancel: Optional[threading.Event] = None,
            force: bool = False) -> dict:
    """Download, verify, unpack, compile, check and switch on `info`.

    Raises TransientUpdateError for failures that prove nothing against the
    release (offline; the start-up check could not run; another update is
    already running here) and OverlayError for release defects (recorded in
    skipped.json). On any failure nothing is switched on and the work dir is
    removed. The previous release stays on disk — this process may still be
    importing from it; the next start removes it.

    ``force`` is the user's own click: it retries a release this machine
    already recorded as skipped. The automatic path never forces, so a
    rejected release is not downloaded again behind the user's back.
    """
    if not supported():
        raise OverlayError("the YouTube downloader can only update itself in the installed app")
    if not force and is_skipped(info):
        raise OverlayError("this release is set aside on this machine; "
                           "choose Update now to try it again")
    if not bundled_version():
        raise OverlayError("this build cannot read its bundled yt-dlp version; update DeepFlux instead")
    if not _install_lock.acquire(blocking=False):
        raise TransientUpdateError("another YouTube downloader update is already running")
    try:
        return _install_locked(info, progress, cancel)
    finally:
        _install_lock.release()


def _install_locked(info: YtdlpUpdateInfo,
                    progress: Optional[Callable[[int, int], None]],
                    cancel: Optional[threading.Event]) -> dict:
    """The pipeline itself; always runs under ``_install_lock``."""
    bundled = bundled_version()
    root = overlay_dir()
    versions = root / "versions"
    staging = root / "staging"
    versions.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    _drop_stale_staging(staging)
    work = Path(tempfile.mkdtemp(prefix="work-", dir=staging))
    try:
        wheel = work / info.file
        _download(info, wheel, progress, cancel)
        final = versions / info.dir_name
        check = prepare_release(wheel, info, work, dfile_root=str(final))
        candidate = work / "release"
        try:
            from config import APP_VERSION as app_version
        except Exception:
            app_version = ""
        write_json_atomic(candidate / "manifest.json", {
            "version": info.version, "sha256": info.sha256, "file": info.file,
            "size": info.size, "extractors": check.get("extractors"),
            "tree_sha256": tree_digest(candidate / "yt_dlp"),
            "installed_at": _utc_now(), "app_version": app_version})
        final = _publish(candidate, final, info)
        write_json_atomic(root / POINTER_NAME, {
            "version": info.version, "sha256": info.sha256, "dir": info.dir_name,
            "installed_at": _utc_now(), "app_version": app_version})
        _silent_unlink(root / SKIP_NAME)
        _silent_unlink(root / TRANSIENT_NAME)
        logger.info("yt-dlp %s installed; it replaces the bundled %s on the next start",
                    info.version, bundled)
        return {"version": info.version, "dir": str(final)}
    except _Cancelled:
        raise
    except TransientUpdateError as exc:
        if isinstance(exc, _LocalTransientError):
            _note_transient_failure(info, str(exc))
        raise
    except OverlayError as exc:
        try:
            _mark_skipped("selfcheck" if isinstance(exc, _SelfcheckError) else "install",
                          str(exc), info.version, info.sha256)
        except OSError:
            logger.debug("could not record the skipped yt-dlp release", exc_info=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def revert() -> bool:
    """Go back to the bundled copy: switch the override off and remember the
    release that was in use, so automatic mode does not immediately
    download it again. The release's files stay where they are — this
    process may still be importing from them; the next start removes them.
    Takes effect the next time DeepFlux starts. Returns True when an
    override was switched off."""
    if not supported():
        return False
    root = overlay_dir()
    was_active = bool(_active_dir())
    pointer = _pointer()
    if pointer.get("version"):
        try:
            write_json_atomic(root / SKIP_NAME, {
                "version": pointer["version"], "sha256": pointer["sha256"],
                "stage": "user", "error": "", "at": _utc_now()})
        except OSError as exc:
            logger.warning("could not record the yt-dlp choice: %s", exc)
    _silent_unlink(root / POINTER_NAME)
    _silent_unlink(root / TRANSIENT_NAME)
    logger.info("yt-dlp override switched off; the bundled copy is used from the next start")
    return was_active


# --------------------------------------------------------------------------
# The child process
# --------------------------------------------------------------------------

def selfcheck_run(candidate: str) -> dict:
    """Runs IN THE CHILD PROCESS: load the candidate through the same hook
    the app would use and exercise the public surface DeepFlux calls — no
    network. Returns ``{"ok": True, "version", "extractors"}``; raises
    OverlayError on any problem."""
    root = os.path.join(candidate, "yt_dlp")
    if not os.path.isfile(os.path.join(root, "__init__.py")):
        raise OverlayError("package files are missing")
    expected = _read_version_file(os.path.join(root, "version.py"))
    if not expected:
        raise OverlayError("the package carries no valid version")
    sys.meta_path.insert(0, _YtdlpOnlyFinder(root))
    module = importlib.import_module("yt_dlp")

    def _stray() -> list:
        # Nothing may resolve back to the bundle (or the venv's copy): a
        # single stray module means the tree is incomplete.
        return sorted(
            name for name, loaded in sys.modules.items()
            if (name == "yt_dlp" or name.startswith("yt_dlp."))
            and not _is_under(getattr(loaded, "__file__", None), root))

    if _stray():
        raise OverlayError(f"incomplete package: {_stray()[0]} is missing")
    version = str(getattr(getattr(module, "version", None), "__version__", ""))
    if version != expected:
        raise OverlayError(f"version mismatch (found {version!r})")
    # The option names dlmgr/engine.py passes.
    options = {"outtmpl": "%(title)s.%(ext)s", "format": "bestvideo+bestaudio/best",
               "merge_output_format": "mp4", "noplaylist": True, "quiet": True,
               "no_warnings": True, "progress_hooks": []}
    with module.YoutubeDL(options):
        count = len(list(module.list_extractor_classes()))
        if count < 100:
            raise OverlayError("no video extractors were registered")
    # The paths DeepFlux itself exercises. Extractors load lazily in normal
    # use, so import the YouTube extractor, the generic fallback, the curl
    # handler and the ffmpeg post-processor explicitly, then look for
    # strays again. A future release may rename these internal modules: a
    # MISSING one is not a failure; anything they raise is.
    for name in _DEEP_CHECK_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:
                continue
            raise
    if _stray():
        raise OverlayError(f"incomplete package: {_stray()[0]} is missing")
    return {"ok": True, "version": version, "extractors": count}


def selfcheck_main(argv: list) -> int:
    """Entry of the child process: ``DeepFlux --ytdlp-selfcheck DIR``
    (main.py) or ``python -m infra.ytdlp_overlay --selfcheck DIR``. Writes
    ``DIR.selfcheck.json`` — the windowed build has no stdout — and returns
    the process exit code. Never raises: an unhandled exception in the
    windowed child would open a modal error box and stall the parent's
    wait until its timeout."""
    result = {"ok": False, "error": ""}
    out_path = None
    try:
        candidate = os.path.abspath(argv[0])
        out_path = Path(candidate + ".selfcheck.json")
        result = selfcheck_run(candidate)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    try:
        if out_path is not None:
            write_json_atomic(out_path, result)
    except Exception:
        return 2
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    # `python -m infra.ytdlp_overlay --selfcheck DIR` (tests, pin script).
    sys.exit(selfcheck_main(sys.argv[2:]) if sys.argv[1:2] == ["--selfcheck"] else 2)
