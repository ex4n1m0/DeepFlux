"""SVP 4 (SmoothVideo Project) integration — true motion interpolation.

DeepFlux never bundles SVP (proprietary, per-PC license, ~$25 lifetime with
a 30-day trial). This module only *detects and cooperates with* the user's
own SVP 4 install, using SVP's documented mpv contract:

- mpv is created with ``input-ipc-server=mpvpipe`` (a Windows named pipe).
  SVP Manager discovers the instance through it and injects its VapourSynth
  filter chain (svpflow motion interpolation) over IPC once playback starts.
- mpv's VapourSynth bridge delay-loads ``VSScript.dll``/``vapoursynth.dll``
  at first use; those must resolve to SVP's portable runtime in
  ``<SVP>\\mpv64`` (SVP's own log warns "PATH doesn't contain mpv64 folder,
  libmpv players may not work"). :func:`prepare_environment` puts that
  folder on PATH/PYTHONPATH before mpv init.
- VapourSynth filters take software frames only, so SVP mode also forces
  ``hwdec=auto-copy`` (copy-back) at mpv creation — see player.py.

mpv backend only; the VLC backend ignores SVP mode entirely.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Hide the console window a spawned process would otherwise pop up on
# Windows from a windowed app. 0 on other platforms.
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MANAGER_EXE = "SVPManager.exe"

# SVP 4's default install dirs (it is a 32-bit-built installer even on x64).
_DEFAULT_DIRS = (
    r"C:\Program Files (x86)\SVP 4",
    r"C:\Program Files\SVP 4",
)


@dataclass
class SvpInstall:
    """Paths into a detected SVP 4 installation."""
    install_dir: str   # e.g. C:\Program Files (x86)\SVP 4
    manager_exe: str   # SVPManager.exe
    runtime_dir: str   # mpv64 — portable VapourSynth+Python for libmpv players

    @property
    def usable(self) -> bool:
        return os.path.isfile(self.manager_exe) and os.path.isdir(self.runtime_dir)


def _registry_install_dir() -> str:
    """SVP 4's uninstall entry, if any (both registry views, both hives)."""
    try:
        import winreg
    except ImportError:
        return ""
    views = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in views:
            try:
                with winreg.OpenKey(root, sub) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as entry:
                                name = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                                if "SVP" not in name:
                                    continue
                                loc = str(winreg.QueryValueEx(entry, "InstallLocation")[0])
                                return loc.strip('" ')
                        except OSError:
                            continue
            except OSError:
                continue
    return ""


def _runtime_dir(install_dir: str) -> str:
    """The folder SVP expects on PATH for libmpv-based players.

    ``mpv64`` holds SVP's portable VapourSynth (vapoursynth.dll +
    vsscript.dll + embedded Python); accept the dir only when the DLL is
    really there so a partial/removed install doesn't half-activate."""
    cand = os.path.join(install_dir, "mpv64")
    if os.path.isfile(os.path.join(cand, "vapoursynth.dll")):
        return cand
    return ""


def find_install() -> Optional[SvpInstall]:
    """Locate a usable SVP 4 install, or None. Registry first, then defaults."""
    for d in (_registry_install_dir(), *_DEFAULT_DIRS):
        if not d or not os.path.isdir(d):
            continue
        inst = SvpInstall(
            install_dir=d,
            manager_exe=os.path.join(d, MANAGER_EXE),
            runtime_dir=_runtime_dir(d),
        )
        if inst.usable:
            return inst
    return None


def is_manager_running() -> bool:
    """True when SVPManager.exe is in the process list."""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {MANAGER_EXE}"],
            capture_output=True, text=True, timeout=10,
            creationflags=_CREATION_FLAGS,
        ).stdout
        return MANAGER_EXE.lower() in out.lower()
    except Exception:
        return False


def start_manager(inst: SvpInstall) -> bool:
    """Launch SVP Manager; it then discovers mpv via the mpvpipe named pipe."""
    try:
        subprocess.Popen([inst.manager_exe], cwd=inst.install_dir,
                         creationflags=_CREATION_FLAGS)
        return True
    except OSError as exc:
        logger.warning("could not start SVP Manager: %s", exc)
        return False


_prepared = False
_started_by_us = False
_dll_handles: list = []  # keep preloaded VapourSynth DLLs referenced


def prepare_environment(inst: SvpInstall) -> None:
    """Make SVP's portable VapourSynth runtime loadable in this process.

    Three layers, each needed by a different consumer:

    - PATH append: SVP Manager resolves the frame server (``mpv64\\
      vapoursynth.dll``) through PATH — it only finds it for libmpv players
      when the manager process has mpv64 on its PATH (verified live: an
      installer-autostarted manager logged "Frame server NOT FOUND", one
      started by us found it). Appended, never prepended, so the bundled
      libmpv-2.dll keeps winning its own lookup.
    - ``os.add_dll_directory`` + ctypes PRELOAD by absolute path: this is an
      MSIX-packaged Python (WindowsApps) — bare-name DLL resolution does NOT
      consult PATH (verified: ctypes.CDLL("libmpv-2.dll") fails here even
      with PATH set). mpv delay-loads "VSScript.dll" by bare name when SVP
      injects the filter; preloading puts the module in the process's
      loaded-module list, which every loader search order checks first.

    PATH only, NEVER PYTHONPATH: mpv64 holds a full Python 3.12 stdlib whose
    .pyd files shadow OUR interpreter's modules (_ctypes & co.) — verified:
    PYTHONPATH=mpv64 breaks `import ctypes` in our process. SVP's embedded
    Python doesn't need it (python312._pth makes it self-contained).

    Process-local and idempotent."""
    global _prepared
    if _prepared:
        return
    d = inst.runtime_dir
    path = os.environ.get("PATH", "")
    if d.lower() not in path.lower():
        os.environ["PATH"] = path + os.pathsep + d if path else d
    try:
        os.add_dll_directory(d)
    except (AttributeError, OSError):
        pass  # pre-3.8 / exotic platforms: preload below still helps
    import ctypes
    for dll in ("vapoursynth.dll", "VSScript.dll"):
        try:
            _dll_handles.append(ctypes.CDLL(os.path.join(d, dll)))
        except OSError as exc:
            logger.warning("SVP runtime preload failed for %s: %s", dll, exc)
    _prepared = True
    logger.info("SVP runtime prepared: %s", d)


def ensure_manager(inst: SvpInstall, wait_s: float = 15.0) -> bool:
    """Ensure SVP Manager runs WITH our prepared environment.

    The manager resolves the VapourSynth frame server through its own PATH,
    so a manager autostarted by Windows (no mpv64 on PATH) can't attach to a
    libmpv player — restart it once per session in that case. The manager is
    a stateless tray helper; the bounce takes ~3s. Polls until the process
    is up so playback doesn't race its pipe scan."""
    global _started_by_us
    if is_manager_running() and _started_by_us:
        return True
    if is_manager_running():
        subprocess.run(["taskkill", "/F", "/IM", MANAGER_EXE],
                       capture_output=True, creationflags=_CREATION_FLAGS)
        for _ in range(20):  # wait for the old one to exit
            if not is_manager_running():
                break
            time.sleep(0.25)
    if not start_manager(inst):
        return False
    _started_by_us = True
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if is_manager_running():
            return True
        time.sleep(0.5)
    return False
