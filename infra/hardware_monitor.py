"""Live hardware sampling for the Agent-tab HUD (CPU / RAM / GPU / VRAM).

ModelSwarm-style resource strip: the user watches real utilization while a
local model runs, which is exactly the evidence needed to pick a quant the
machine can handle (a GPU pegged at 100% with VRAM swap says "smaller
model"). Everything here is Qt-free and safe from background threads.

Sampling is dependency-free on purpose (no psutil — it is not in
requirements.txt, so frozen builds cannot rely on it):
- CPU%: GetSystemTimes deltas on Windows, /proc/stat deltas on POSIX.
- RAM: GlobalMemoryStatusEx on Windows, /proc/meminfo on POSIX.
- GPU/VRAM: ``nvidia-smi --query-gpu`` (ships with every NVIDIA driver at
  %SystemRoot%\\System32, ~80 ms per call). Non-NVIDIA GPUs report NAME +
  VRAM total through the static probes in local_model/dxgi but no live
  utilization — the HUD degrades to "GPU: name, no live stats".
- dxgi_adapters(): Direct3D adapter enumeration (name + DEDICATED VRAM,
  correct above 4 GB unlike WMI's truncated AdapterRAM) — the static
  fallback that makes "No GPU detected" on a working RTX 5080 Mobile
  practically impossible even when the Vulkan probe fails.

Failure containment: a failed nvidia-smi query turns the sampler off for
SMI_RETRY_SECONDS (a missing binary or a broken driver must not spawn a
doomed process every 2 s); the resolved path and the dead-until stamp are
process-global so several samplers share one verdict.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SMI_TIMEOUT = 4.0
SMI_RETRY_SECONDS = 60.0

# Colors/thresholds live in the GUI; this module only produces numbers.


@dataclass
class Sample:
    """One hardware snapshot (any field may be unknown → zero/None)."""
    cpu_percent: float = 0.0
    ram_used_mb: int = 0
    ram_total_mb: int = 0
    gpu_name: str = ""
    gpu_percent: Optional[float] = None
    vram_used_mb: int = 0
    vram_total_mb: int = 0
    gpu_live: bool = False          # True when utilization numbers are live

    @property
    def ram_percent(self) -> Optional[float]:
        if self.ram_total_mb <= 0:
            return None
        return min(100.0, self.ram_used_mb * 100.0 / self.ram_total_mb)

    @property
    def vram_percent(self) -> Optional[float]:
        if self.vram_total_mb <= 0:
            return None
        return min(100.0, self.vram_used_mb * 100.0 / self.vram_total_mb)


# ---------------------------------------------------------------------------
# CPU — system-wide busy fraction between two calls
# ---------------------------------------------------------------------------

_cpu_lock = threading.Lock()
_cpu_last: Optional[Tuple[int, int, int]] = None  # (idle, busy, monotonic)


def _cpu_times_windows() -> Optional[Tuple[int, int]]:
    """(idle, busy) in 100 ns units via GetSystemTimes (kernel time INCLUDES
    idle, so busy = kernel - idle + user)."""
    try:
        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_ulong),
                        ("dwHighDateTime", ctypes.c_ulong)]

        idle, kern, user = FILETIME(), FILETIME(), FILETIME()

        def ft_value(ft: "FILETIME") -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        if ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            return ft_value(idle), ft_value(kern) - ft_value(idle) + ft_value(user)
    except Exception:
        pass
    return None


def _cpu_times_posix() -> Optional[Tuple[int, int]]:
    """(idle, busy) in jiffies from /proc/stat's aggregate cpu line."""
    try:
        with open("/proc/stat", "r", encoding="ascii", errors="ignore") as fh:
            parts = fh.readline().split()
        vals = [int(v) for v in parts[1:]]
        if len(vals) < 5:
            return None
        idle = vals[3] + vals[4]  # idle + iowait
        return idle, sum(vals) - idle
    except (OSError, ValueError):
        return None


def _cpu_times() -> Optional[Tuple[int, int]]:
    if os.name == "nt":
        return _cpu_times_windows()
    return _cpu_times_posix()


def seed_cpu_baseline() -> None:
    """Take the first reading now so the first cpu_percent() is meaningful."""
    with _cpu_lock:
        global _cpu_last
        t = _cpu_times()
        if t is not None:
            _cpu_last = (t[0], t[1], time.monotonic())


def cpu_percent() -> float:
    """Busy fraction since the previous call (0.0 on the first call or when
    the platform source is unavailable)."""
    global _cpu_last
    with _cpu_lock:
        t = _cpu_times()
        if t is None:
            return 0.0
        prev = _cpu_last
        _cpu_last = (t[0], t[1], time.monotonic())
        if prev is None:
            return 0.0
        d_idle = t[0] - prev[0]
        d_busy = t[1] - prev[1]
        total = d_idle + d_busy
        if total <= 0:
            return 0.0
        return min(100.0, d_busy * 100.0 / total)


# ---------------------------------------------------------------------------
# RAM — used/total in MiB
# ---------------------------------------------------------------------------

def ram_stats() -> Tuple[int, int]:
    """(used_mb, total_mb); (0, 0) when nothing can be read."""
    if os.name == "nt":
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = MEMORYSTATUSEX(dwLength=ctypes.sizeof(MEMORYSTATUSEX))
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total = int(stat.ullTotalPhys // (1024 * 1024))
                used = int((stat.ullTotalPhys - stat.ullAvailPhys) // (1024 * 1024))
                return used, total
        except Exception:
            pass
        return 0, 0
    total = avail = 0
    try:
        with open("/proc/meminfo", "r", encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return max(0, total - avail), total


# ---------------------------------------------------------------------------
# GPU — live stats via nvidia-smi, static inventory via DXGI
# ---------------------------------------------------------------------------

_smi_lock = threading.Lock()
_smi_path: Optional[str] = None           # resolved once ("" = known missing)
_smi_dead_until: float = 0.0              # failure backoff stamp


def _find_smi() -> Optional[str]:
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    # The driver always drops it here even when PATH is mangled (GUI apps
    # launched from Explorer/Finder inherit a minimal environment).
    sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "nvidia-smi.exe")
    return sysdir if os.path.isfile(sysdir) else None


def parse_smi_csv(text: str) -> List[dict]:
    """Parse ``--query-gpu=name,utilization.gpu,memory.used,memory.total
    --format=csv,noheader,nounits`` output. One dict per GPU:
    {name, util (float|None), mem_used_mb, mem_total_mb}. Unparsable lines
    are skipped, never raised — a driver hiccup must not blank the HUD."""
    gpus: List[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 4:
            continue
        try:
            util: Optional[float] = None
            if cols[1] and not cols[1].startswith("["):  # "[Not Supported]"
                util = float(cols[1])
            gpus.append({"name": cols[0], "util": util,
                         "mem_used_mb": int(float(cols[2])),
                         "mem_total_mb": int(float(cols[3]))})
        except ValueError:
            continue
    return gpus


def nvidia_smi_query() -> List[dict]:
    """Live per-GPU stats ([] when unavailable — see module docstring for
    the backoff contract)."""
    global _smi_path, _smi_dead_until
    with _smi_lock:
        if _smi_path is None:
            _smi_path = _find_smi() or ""
        if not _smi_path or time.monotonic() < _smi_dead_until:
            return []
        try:
            proc = subprocess.run(
                [_smi_path, "--query-gpu",
                 "name,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, timeout=SMI_TIMEOUT,
                encoding="utf-8", errors="replace",
                creationflags=_CREATE_NO_WINDOW)
            gpus = parse_smi_csv(proc.stdout or "")
            if not gpus and proc.returncode != 0:
                raise RuntimeError(f"nvidia-smi exited {proc.returncode}")
        except Exception as exc:
            _smi_dead_until = time.monotonic() + SMI_RETRY_SECONDS
            logger.debug("nvidia-smi query failed (%s); backing off", exc)
            return []
        return gpus


# --- static inventory: DXGI adapters (Windows) ------------------------------

_DXGI_ADAPTER_FILTERS = ("microsoft basic render driver", "software")


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


# IID_IDXGIFactory1 {770aae78-f26f-4dba-a829-253c83d1b387}, field order per
# the GUID wire layout (verified against Windows Kits dxgi.h — a mis-encoded
# IID answers E_NOINTERFACE and the probe silently dies).
_IID_IDXGIFactory1 = _GUID(0x770AAE78, 0xF26F, 0x4DBA,
                           (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C,
                                                0x83, 0xD1, 0xB3, 0x87))


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    # Field order per dxgi.h (Description first, then the four UINTs, then
    # the SIZE_T trio VIDEO/SYSTEM/SHARED — swapping them reads 0 VRAM).
    _fields_ = [("Description", ctypes.c_wchar * 128),
                ("VendorId", ctypes.c_ulong),
                ("DeviceId", ctypes.c_ulong),
                ("SubSysId", ctypes.c_ulong),
                ("Revision", ctypes.c_ulong),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuidLow", ctypes.c_ulong),
                ("AdapterLuidHigh", ctypes.c_long)]


def dxgi_adapters() -> List[Tuple[str, int]]:
    """(name, dedicated-VRAM MiB) for every hardware adapter, via D3D's own
    enumeration. DedicatedVideoMemory is a SIZE_T — unlike WMI's uint32
    AdapterRAM it does NOT wrap above 4 GB, so a 16 GB card reports 16 GB.
    Software adapters (Basic Render Driver) are filtered. POSIX → []."""
    if os.name != "nt":
        return []
    try:
        dxgi = ctypes.WinDLL("dxgi")
        factory = ctypes.c_void_p()
        hr = dxgi.CreateDXGIFactory1(
            ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory))
        if hr != 0 or not factory:
            return []

        # COM vtables, indexes after IUnknown{0 QueryInterface, 1 AddRef,
        # 2 Release}: IDXGIObject{3..6}; IDXGIFactory{7 EnumAdapters,
        # 8 MakeWindowAssociation, 9 GetWindowAssociation, 10 CreateSwapChain,
        # 11 CreateSoftwareAdapter}; IDXGIFactory1{12 EnumAdapters1, 13
        # IsCurrent}. On the ADAPTER side: IDXGIAdapter{7 EnumOutputs,
        # 8 GetDesc, 9 CheckInterfaceSupport}, IDXGIAdapter1{10 GetDesc1}.
        def vtbl_slot(com_obj: "ctypes.c_void_p", index: int):
            # COM objects start with an lpVtbl pointer; the slot array lives
            # behind it (two dereferences — casting the object itself only
            # reaches its own fields).
            obj = ctypes.c_void_p(com_obj.value if isinstance(
                com_obj, ctypes.c_void_p) else com_obj)
            lpVtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
            vtbl = ctypes.cast(ctypes.c_void_p(lpVtbl),
                               ctypes.POINTER(ctypes.c_void_p))
            return vtbl[index]

        release_factory = ctypes.WINFUNCTYPE(
            ctypes.c_ulong, ctypes.c_void_p)(vtbl_slot(factory, 2))
        enum_adapters1 = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p))(vtbl_slot(factory, 12))
        try:
            out: List[Tuple[str, int]] = []
            for index in range(8):  # no machine has more hardware adapters
                adapter = ctypes.c_void_p()
                if enum_adapters1(factory, index, ctypes.byref(adapter)) != 0:
                    break  # DXGI_ERROR_NOT_FOUND = end of the list
                try:
                    release_adapter = ctypes.WINFUNCTYPE(
                        ctypes.c_ulong, ctypes.c_void_p)(vtbl_slot(adapter, 2))
                    get_desc1 = ctypes.WINFUNCTYPE(
                        ctypes.c_long, ctypes.c_void_p,
                        ctypes.POINTER(_DXGI_ADAPTER_DESC1))(
                            vtbl_slot(adapter, 10))
                    desc = _DXGI_ADAPTER_DESC1()
                    if get_desc1(adapter, ctypes.byref(desc)) == 0:
                        name = desc.Description or ""
                        if name.lower() not in _DXGI_ADAPTER_FILTERS:
                            out.append((name, int(desc.DedicatedVideoMemory
                                                  // (1024 * 1024))))
                finally:
                    release_adapter(adapter)
            return out
        finally:
            release_factory(factory)
    except Exception as exc:
        logger.debug("DXGI adapter enumeration failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# The aggregate snapshot
# ---------------------------------------------------------------------------

def sample(static_gpu: Optional[Tuple[str, int]] = None) -> Sample:
    """One HUD snapshot. ``static_gpu`` = (name, vram_mb) from
    local_model.detect_hardware() — used when nvidia-smi is unavailable so
    the GPU chip still names the card and its VRAM budget."""
    s = Sample(cpu_percent=cpu_percent())
    s.ram_used_mb, s.ram_total_mb = ram_stats()
    gpus = nvidia_smi_query()
    if gpus:
        g = gpus[0]
        s.gpu_name = g["name"]
        s.gpu_percent = g["util"]
        s.vram_used_mb = g["mem_used_mb"]
        s.vram_total_mb = g["mem_total_mb"]
        s.gpu_live = True
    elif static_gpu:
        s.gpu_name, s.vram_total_mb = static_gpu[0], static_gpu[1]
    return s
