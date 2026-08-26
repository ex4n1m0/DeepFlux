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
    runtime_dir: str   # mpv64 — portable VapourSynth+Python, plus mpv.exe

    @property
    def usable(self) -> bool:
        return os.path.isfile(self.manager_exe) and os.path.isdir(self.runtime_dir)

    @property
    def mpv_exe(self) -> str:
        """SVP's own mpv.exe (installed with the "mpv video player" package).

        The out-of-process backend runs THIS binary: it sits next to SVP's
        portable VapourSynth, so the frame server resolves without any
        environment gymnastics, and it's a C host so VSScript initializes
        normally. Empty string when the user didn't install the component."""
        p = os.path.join(self.runtime_dir, "mpv.exe")
        return p if os.path.isfile(p) else ""


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


def prepare_environment(inst: SvpInstall) -> None:
    """Put SVP's runtime dir on PATH for CHILD processes.

    SVP Manager resolves the frame server (``mpv64\\vapoursynth.dll``)
    through PATH; when we start it ourselves it inherits this. The player
    (SVP's own ``mpv.exe``) lives in that same folder, so it finds
    VapourSynth next to itself.

    Deliberately narrow — two things this must NEVER do, both verified the
    hard way on this machine:

    - No PYTHONPATH: mpv64 ships a complete Python 3.12 stdlib whose .pyd
      files shadow our interpreter's own modules ("Module use of
      python312.dll conflicts with this version of Python").
    - No ctypes preload of vapoursynth.dll/VSScript.dll into THIS process:
      that pulls a second CPython (3.12) into our 3.11 runtime and hard-
      crashes the app (exit 0xCFFFFFFF). Loading VapourSynth in-process is
      exactly the thing the out-of-process backend exists to avoid.

    Process-local and idempotent."""
    global _prepared
    if _prepared:
        return
    d = inst.runtime_dir
    path = os.environ.get("PATH", "")
    if d.lower() not in path.lower():
        os.environ["PATH"] = path + os.pathsep + d if path else d
    _prepared = True
    logger.info("SVP runtime added to PATH for child processes: %s", d)


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
