"""Force the built-in Chromium browser onto the dedicated GPU (Windows).

Hybrid-GPU machines (Intel/AMD iGPU + NVIDIA/AMD dGPU) make Chromium's
GPU process flip between adapters or lose its context, which shows as
one-frame white/black flashes — worst during video playback (hardware
decode adds GPU work). Windows picks the GPU for an exe through the
per-app "Graphics performance preference" stored under
``HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences`` (value name =
full exe path, data = ``GpuPreference=2;`` = high performance / dGPU).
This module writes that preference for OUR exe when a discrete adapter
exists, so the browser renders on the dGPU instead of the iGPU path.

Qt-free and safe to call from startup. Every failure is a silent no-op
(a machine without the registry key just keeps Windows defaults).
"""
from __future__ import annotations

import logging
import os
import sys

from infra import hardware_monitor

logger = logging.getLogger(__name__)

_PREF_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
# DXGI_GPU_PREFERENCE values: 0 = unspecified, 1 = power saving (iGPU),
# 2 = high performance (dGPU). Windows writes exactly this "<k>=<v>;"
# string format; keep it byte-identical to what the Settings UI writes.
_PREF_DATA = "GpuPreference=2;"


def _is_integrated(name: str) -> bool:
    """Mirror of local_model._is_integrated_gpu (kept local so this module
    never imports the heavy local-AI stack)."""
    n = name.lower()
    if "intel" in n:
        return any(h in n for h in ("iris", "uhd", "hd graphics")) or n.rstrip(")").endswith("graphics")
    if "radeon" in n:
        return "vega" in n or n.rstrip(")").endswith("graphics")
    return False


def has_discrete_gpu() -> bool:
    """True when a non-integrated adapter with dedicated VRAM exists."""
    return any(
        not _is_integrated(name) and vram_mb > 0
        for name, vram_mb in hardware_monitor.dxgi_adapters()
    )


def _exe_path() -> str:
    """The installed exe path the preference is keyed by. Source runs must
    NOT write a preference for python.exe (it would force every Python
    process onto the dGPU)."""
    if not getattr(sys, "frozen", False) or not sys.executable:
        return ""
    return sys.executable


def apply_force_dedicated_gpu(enable: bool = True) -> str:
    """Write (or remove) the per-exe high-performance GPU preference.

    Returns the exe path the preference was set for, "removed" after a
    removal, or "" when it was a no-op (POSIX / not frozen / no dGPU)."""
    if os.name != "nt":
        return ""
    exe = _exe_path()
    if not exe:
        return ""
    if enable and not has_discrete_gpu():
        logger.info("no dedicated GPU found — leaving Windows GPU defaults")
        return ""
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _PREF_KEY) as key:
            if enable:
                winreg.SetValueEx(key, exe, 0, winreg.REG_SZ, _PREF_DATA)
                logger.info("forced the dedicated GPU for %s", exe)
                return exe
            try:
                winreg.DeleteValue(key, exe)
                logger.info("removed the forced-GPU preference for %s", exe)
                return "removed"
            except FileNotFoundError:
                return "already-absent"
    except OSError as exc:
        logger.warning("GPU preference write failed (continuing with defaults): %s", exc)
        return ""
