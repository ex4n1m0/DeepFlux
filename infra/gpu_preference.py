"""Per-exe Windows graphics preference for the built-in browser.

Windows picks the GPU for an exe through the per-app "Graphics
performance preference" stored under
``HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences`` (value name =
full exe path, data = ``GpuPreference=<k>;`` — 1 = power saving / iGPU,
2 = high performance / dGPU). This module writes that preference for OUR
exe according to ``config.browser.gpu_preference``:

* ``"integrated"`` (DEFAULT since 7.6, owner decision 2026-11-07) —
  power-saving iGPU. Needs NO adapter detection: valid on every machine
  and never touches the DXGI stack (its enumeration disturbed the heap
  on at least one machine — the 2026-11-06/07 forensics; the old
  always-on dGPU pin also cost laptop battery).
* ``"dedicated"`` — high-performance dGPU, kept for machines where the
  browser flashes when Windows switches adapters (the original 7.4
  symptom); writes only when a discrete adapter actually exists.
* anything else (``"auto"``) — preference removed, Windows decides.

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
_PREF_DATA = {"integrated": "GpuPreference=1;", "dedicated": "GpuPreference=2;"}


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
    NOT write a preference for python.exe (it would pin every Python
    process to one GPU)."""
    if not getattr(sys, "frozen", False) or not sys.executable:
        return ""
    return sys.executable


def apply_gpu_preference(preference: str) -> str:
    """Write (or remove) the per-exe GPU preference.

    "integrated"  -> GpuPreference=1 (power saving / iGPU) — no adapter
                     detection, safe on every machine.
    "dedicated"   -> GpuPreference=2 (high performance / dGPU), only when
                     a discrete adapter exists.
    anything else -> preference removed; Windows decides.

    Returns the exe path written, "removed" after a removal,
    "already-absent", "no-dgpu" when a dGPU pin was requested but none
    exists, or "" for no-ops (POSIX / source runs / registry failure)."""
    if os.name != "nt":
        return ""
    exe = _exe_path()
    if not exe:
        return ""
    data = _PREF_DATA.get(preference)
    if data is None:
        try:
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _PREF_KEY) as key:
                try:
                    winreg.DeleteValue(key, exe)
                    logger.info("removed the GPU preference for %s", exe)
                    return "removed"
                except FileNotFoundError:
                    return "already-absent"
        except OSError as exc:
            logger.warning("GPU preference removal failed: %s", exc)
            return ""
    if preference == "dedicated" and not has_discrete_gpu():
        logger.info("no dedicated GPU found — leaving Windows GPU defaults")
        return "no-dgpu"
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _PREF_KEY) as key:
            winreg.SetValueEx(key, exe, 0, winreg.REG_SZ, data)
            logger.info("set the %s GPU preference for %s", preference, exe)
            return exe
    except OSError as exc:
        logger.warning("GPU preference write failed (continuing with defaults): %s", exc)
        return ""
