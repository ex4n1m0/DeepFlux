"""System-health snapshot engine — the System Health tab + agent tool.

Successor to the removed 7.6 hardware HUD (owner decision 2026-11-18:
"only does readings when that tab is selected, not always"). NOTHING in
this module runs on a cadence by itself: the tab drives sampling while it
is the SELECTED tab, the agent's ``system_health`` tool samples per call,
and the slow probes (ACPI temperature, PowerShell inventory) sit behind
TTL/backoff guards so they can never spawn more than roughly one process
per half-minute while the tab is open. The old HUD's boot-crash lesson
(AGENTS.md, 2026-11-07) is the contract here: on demand, gated, best-effort.

Everything is Qt-free, psutil-free and thread-safe (the tab's collector
thread and the agent tool may sample concurrently). Windows provides the
full picture; POSIX degrades honestly (live CPU/RAM via /proc, /sys
temperatures, no disk perf counters — the tab shows "—").

Data sources, all dependency-free:
- CPU%: hardware_monitor.cpu_percent (GetSystemTimes / /proc/stat deltas).
- RAM + commit charge: GlobalMemoryStatusEx / /proc/meminfo.
- GPU live: nvidia-smi via hardware_monitor.nvidia_smi_query (utilization,
  VRAM, temperature, fan, power, PCIe link) with its built-in 60 s failure
  backoff; static fallback via dxgi_adapters.
- Disks: shutil.disk_usage per fixed volume for space; live read/write
  throughput via IOCTL_DISK_PERFORMANCE deltas (no admin needed — a volume
  that refuses the IOCTL is marked cold and never asked again).
- CPU/board temperature: ACPI thermal zone (PowerShell CIM one-shot on
  Windows; /sys/class/thermal on Linux). Many boards expose nothing —
  None is an honest answer, never an error.
- Inventory: one PowerShell CIM query (CPU, RAM sticks incl. speed in
  MT/s, board, BIOS, GPUs + drivers, physical disks with bus/media type,
  enabled NICs, OS) cached ~10 min; /proc digest on POSIX.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import SystemHealthConfig
from infra import hardware_monitor

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PS_TIMEOUT = 25.0


# ---------------------------------------------------------------------------
# Disk volumes — enumeration, space, live throughput
# ---------------------------------------------------------------------------

# CTL_CODE(IOCTL_DISK_BASE=7, 0x0008, METHOD_BUFFERED, FILE_ANY_ACCESS) =
# (7 << 16) | (0x8 << 2) = 0x70020. 0x70000 is IOCTL_DISK_GET_DRIVE_GEOMETRY
# (function 0x0000) — the shipped 8.5 used it by mistake, so the driver
# answered with a 24-byte DISK_GEOMETRY that we misread as perf counters:
# constant "deltas" and 0 B/s disk speeds everywhere (measured 2026-11-19:
# 0x70000 → bytes_in_out=24, 0x70020 → the real 88-byte live counters).
IOCTL_DISK_PERFORMANCE = 0x00070020


class _DISK_PERFORMANCE(ctypes.Structure):
    """winioctl.h DISK_PERFORMANCE. No pack pragma in the SDK header, so
    ctypes' default (natural per-field) alignment matches the driver's
    layout on every supported architecture."""
    _fields_ = [
        ("BytesRead", ctypes.c_longlong),
        ("BytesWritten", ctypes.c_longlong),
        ("ReadTime", ctypes.c_longlong),
        ("WriteTime", ctypes.c_longlong),
        ("IdleTime", ctypes.c_longlong),
        ("ReadCount", ctypes.c_ulong),
        ("WriteCount", ctypes.c_ulong),
        ("QueueDepth", ctypes.c_ulong),
        ("SplitCount", ctypes.c_ulong),
        ("QueryTime", ctypes.c_longlong),
        ("StorageDeviceNumber", ctypes.c_ulong),
        ("StorageManagerName", ctypes.c_wchar * 8),
    ]


_DRIVE_FIXED = 3


def _fixed_volumes() -> List[str]:
    """Fixed volume ids: drive letters ("C:") on Windows, ["/"] on POSIX
    (space only — no perf counters there)."""
    if os.name != "nt":
        return ["/"]
    try:
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        out: List[str] = []
        for i in range(26):
            if mask & (1 << i):
                letter = chr(ord("A") + i)
                root = f"{letter}:\\"
                if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == _DRIVE_FIXED:
                    out.append(f"{letter}:")
        return out
    except Exception:
        return []


def read_volume_counters(volume: str) -> Optional[Tuple[int, int]]:
    """Cumulative (bytes_read, bytes_written) for one fixed volume via
    IOCTL_DISK_PERFORMANCE, or None when the handle/IOCTL is refused (some
    machines deny perf counters to standard users — the caller then marks
    the volume cold and stops asking). Windows only; module-level so tests
    can monkeypatch it."""
    if os.name != "nt":
        return None
    handle = None
    try:
        k32 = ctypes.windll.kernel32
        # Pointer-size restype/argtypes are MANDATORY for CreateFileW — a
        # WinDLL call without them converts as C ints and TRUNCATES 64-bit
        # handles (the compact-mode ctypes lesson).
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint,
                                    ctypes.c_uint, ctypes.c_void_p,
                                    ctypes.c_uint, ctypes.c_uint,
                                    ctypes.c_void_p]
        # dwDesiredAccess 0 (device query only) + full sharing: unelevated.
        handle = k32.CreateFileW(f"\\\\.\\{volume}", 0, 3, None, 3, 0, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        buf = _DISK_PERFORMANCE()
        returned = ctypes.c_ulong()
        k32.DeviceIoControl.restype = ctypes.c_int
        k32.DeviceIoControl.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
        if not k32.DeviceIoControl(handle, IOCTL_DISK_PERFORMANCE,
                                   None, 0, ctypes.byref(buf),
                                   ctypes.sizeof(buf),
                                   ctypes.byref(returned), None):
            return None
        return int(buf.BytesRead), int(buf.BytesWritten)
    except Exception:
        return None
    finally:
        if handle:
            try:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
            except Exception:
                pass


_disk_lock = threading.Lock()
_disk_last: Dict[str, Tuple[int, int, float]] = {}   # volume → (rd, wr, mono)
_disk_dead: set = set()                              # volumes refusing IOCTL


def disk_snapshot() -> List[dict]:
    """Per fixed volume: space (total/free GB, used %) + live read/write
    B/s from consecutive IOCTL_DISK_PERFORMANCE deltas. Speeds are None on
    the first reading (no baseline yet) and stay None on POSIX/refusing
    volumes — never a crash, never a division by zero."""
    now = time.monotonic()
    out: List[dict] = []
    with _disk_lock:
        for volume in _fixed_volumes():
            root = f"{volume}\\" if os.name == "nt" else volume
            entry: Dict[str, Any] = {
                "volume": volume, "total_gb": None, "free_gb": None,
                "used_percent": None, "free_percent": None,
                "read_bps": None, "write_bps": None,
            }
            try:
                du = shutil.disk_usage(root)
                if du.total > 0:
                    entry["total_gb"] = round(du.total / 2 ** 30, 1)
                    entry["free_gb"] = round(du.free / 2 ** 30, 1)
                    entry["used_percent"] = round(
                        (du.total - du.free) * 100.0 / du.total, 1)
                    entry["free_percent"] = round(
                        du.free * 100.0 / du.total, 1)
            except OSError:
                pass
            if volume not in _disk_dead:
                counters = read_volume_counters(volume)
                if counters is None:
                    _disk_dead.add(volume)   # refused once → never re-asked
                else:
                    prev = _disk_last.get(volume)
                    _disk_last[volume] = (counters[0], counters[1], now)
                    if prev is not None and now - prev[2] > 0.2:
                        dt = now - prev[2]
                        entry["read_bps"] = max(
                            0.0, (counters[0] - prev[0]) / dt)
                        entry["write_bps"] = max(
                            0.0, (counters[1] - prev[1]) / dt)
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# CPU/board temperature — ACPI thermal zone, slow lane
# ---------------------------------------------------------------------------

_thermal_lock = threading.Lock()
_thermal_value: Optional[float] = None    # last successful reading (°C)
_thermal_stamp = 0.0                      # monotonic of the last PROBE
_THERMAL_TTL = 30.0                       # good value served this long
_THERMAL_FAIL_BACKOFF = 300.0             # a failed probe waits this long


def cpu_temperature(probe: bool = False) -> Optional[float]:
    """Best-effort CPU/board temperature (°C). Windows: ACPI thermal zone
    via a one-shot PowerShell CIM query — many boards expose none, None is
    an honest answer. Linux: /sys/class/thermal zones (millidegrees).
    Cached: a good value is re-served for _THERMAL_TTL, a failed probe
    backs off _THERMAL_FAIL_BACKOFF. ``probe=True`` refreshes when the
    guard allows — the caller owns the cadence (tab slow lane / tool call).
    """
    global _thermal_value, _thermal_stamp
    with _thermal_lock:
        limit = (_THERMAL_TTL if _thermal_value is not None
                 else _THERMAL_FAIL_BACKOFF)
        if not probe or (time.monotonic() - _thermal_stamp) < limit:
            return _thermal_value
    value = _probe_temperature()   # the slow part runs OUTSIDE the lock
    with _thermal_lock:
        _thermal_value, _thermal_stamp = value, time.monotonic()
    return value


def _probe_temperature() -> Optional[float]:
    if os.name == "nt":
        text = _run_powershell(
            "(Get-CimInstance -Namespace root/wmi -ClassName "
            "MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue "
            "| Measure-Object -Maximum CurrentKelvin).Maximum")
        if not text or not text.strip():
            return None
        try:
            # CurrentKelvin is in TENTHS of a kelvin.
            celsius = float(text.strip()) / 10.0 - 273.15
        except ValueError:
            return None
        return round(celsius, 1) if -20.0 <= celsius <= 120.0 else None
    try:
        best: Optional[float] = None
        for zone in sorted(os.listdir("/sys/class/thermal")):
            path = f"/sys/class/thermal/{zone}/temp"
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="ascii", errors="ignore") as fh:
                    celsius = int(fh.read().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            if -20.0 <= celsius <= 120.0 and (best is None or celsius > best):
                best = celsius
        return round(best, 1) if best is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Hardware inventory — one PowerShell CIM query, cached
# ---------------------------------------------------------------------------

_inv_lock = threading.Lock()
_inv_cache: Optional[dict] = None
_inv_stamp = 0.0
_inv_failed = False
_INV_TTL = 600.0          # good inventory served for 10 min
_INV_FAIL_BACKOFF = 60.0  # a failed fetch retries at most once a minute

# One process, one JSON document. SilentlyContinue everywhere: a missing
# class (Get-PhysicalDisk needs the Storage module) must only blank its own
# section, never the whole query.
_INV_PS_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$o=[ordered]@{};"
    "$o.cpu=@(Get-CimInstance Win32_Processor | Select-Object -First 1 "
    "Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed);"
    "$o.ram=@(Get-CimInstance Win32_PhysicalMemory | Select-Object "
    "DeviceLocator,Manufacturer,PartNumber,Capacity,Speed,ConfiguredClockSpeed);"
    "$o.board=@(Get-CimInstance Win32_BaseBoard | Select-Object -First 1 "
    "Manufacturer,Product,Version);"
    "$o.bios=@(Get-CimInstance Win32_BIOS | Select-Object -First 1 "
    "SMBIOSBIOSVersion,ReleaseDate);"
    "$o.gpus=@(Get-CimInstance Win32_VideoController | Select-Object "
    "Name,DriverVersion,AdapterRAM);"
    "$o.disks=@(Get-PhysicalDisk | Select-Object "
    "FriendlyName,BusType,MediaType,Size,HealthStatus);"
    "$o.nics=@(Get-CimInstance Win32_NetworkAdapter -Filter "
    "'NetEnabled=true' | Select-Object Name,Speed);"
    "$o.os=@(Get-CimInstance Win32_OperatingSystem | Select-Object -First 1 "
    "Caption,Version,BuildNumber);"
    "$o | ConvertTo-Json -Depth 3 -Compress"
)


def _powershell_path() -> Optional[str]:
    exe = shutil.which("powershell")
    if exe:
        return exe
    # GUI processes inherit a minimal environment — resolve the canonical
    # location directly (same lesson as nvidia-smi's fallback).
    cand = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32",
        "WindowsPowerShell", "v1.0", "powershell.exe")
    return cand if os.path.isfile(cand) else None


def _run_powershell(script: str, timeout: float = _PS_TIMEOUT) -> Optional[str]:
    """One-shot PowerShell query → stdout, or None on any failure. Callers
    degrade, never raise. Windows only."""
    if os.name != "nt":
        return None
    exe = _powershell_path()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-Command", script],
            capture_output=True, timeout=timeout, encoding="utf-8",
            errors="replace", creationflags=_CREATE_NO_WINDOW)
        if proc.returncode != 0:
            return None
        return proc.stdout or ""
    except Exception:
        return None


def _inv_one(x: Any) -> dict:
    """PowerShell unwraps single-element arrays to bare objects — accept
    both shapes and always hand back a dict."""
    if isinstance(x, list):
        x = x[0] if x else None
    return x if isinstance(x, dict) else {}


def _inv_many(x: Any) -> List[dict]:
    if isinstance(x, dict):
        x = [x]
    if not isinstance(x, list):
        return []
    return [e for e in x if isinstance(e, dict)]


def _s(value: Any, cap: int = 120) -> str:
    text = str(value if value is not None else "").strip()
    return text[:cap]


def _parse_inventory_win(text: str) -> Optional[dict]:
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None

    cpu = _inv_one(raw.get("cpu"))
    board = _inv_one(raw.get("board"))
    bios = _inv_one(raw.get("bios"))
    osinfo = _inv_one(raw.get("os"))
    return {
        "available": True,
        "cpu": {
            "name": _s(cpu.get("Name")),
            "cores": cpu.get("NumberOfCores"),
            "logical": cpu.get("NumberOfLogicalProcessors"),
            "base_clock_mhz": cpu.get("MaxClockSpeed"),
        },
        "ram": [{
            "slot": _s(m.get("DeviceLocator"), 40),
            "manufacturer": _s(m.get("Manufacturer"), 40),
            "part": _s(m.get("PartNumber"), 40),
            "capacity_gb": (round(m["Capacity"] / 2 ** 30, 1)
                            if m.get("Capacity") else None),
            "speed_mts": m.get("Speed"),           # max supported MT/s
            "configured_mts": m.get("ConfiguredClockSpeed"),  # running MT/s
        } for m in _inv_many(raw.get("ram"))],
        "board": {"manufacturer": _s(board.get("Manufacturer")),
                  "model": _s(board.get("Product")),
                  "version": _s(board.get("Version"), 40)},
        "bios": {"version": _s(bios.get("SMBIOSBIOSVersion"), 40),
                 "date": _s(bios.get("ReleaseDate"), 32)},
        "gpus": [{
            "name": _s(g.get("Name")),
            "driver": _s(g.get("DriverVersion"), 40),
            # WMI AdapterRAM is uint32 and wraps above 4 GB — a coarse hint
            # only; exact VRAM comes from dxgi in the live snapshot.
            "vram_mb": g.get("AdapterRAM"),
        } for g in _inv_many(raw.get("gpus"))],
        "disks": [{
            "model": _s(d.get("FriendlyName")),
            "bus": _s(d.get("BusType"), 16),       # NVMe / SATA / USB / RAID
            "media": _s(d.get("MediaType"), 16),   # SSD / HDD / Unspecified
            "size_gb": (round(d["Size"] / 2 ** 30, 1)
                        if d.get("Size") else None),
            "health": _s(d.get("HealthStatus"), 24),
        } for d in _inv_many(raw.get("disks"))],
        "nics": [{
            "name": _s(n.get("Name")),
            "link_bps": n.get("Speed"),
        } for n in _inv_many(raw.get("nics"))],
        "os": {"name": _s(osinfo.get("Caption")),
               "version": _s(osinfo.get("Version"), 40),
               "build": osinfo.get("BuildNumber")},
    }


def _inventory_posix() -> dict:
    out: dict = {"available": True, "cpu": {"name": "", "cores":
                 os.cpu_count(), "logical": os.cpu_count(),
                 "base_clock_mhz": None},
                 "ram": [], "board": {}, "bios": {},
                 "gpus": [], "disks": [], "nics": [], "os": {}}
    try:
        with open("/proc/cpuinfo", "r", encoding="ascii",
                  errors="ignore") as fh:
            for line in fh:
                if line.startswith("model name"):
                    out["cpu"]["name"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        out["os"] = {"name": " ".join(os.uname().sysname.split()),
                     "version": os.uname().release, "build": None}
    except Exception:
        pass
    return out


def inventory(refresh: bool = False) -> dict:
    """Hardware inventory (device names, like a hardware inventory list),
    cached: a good fetch serves for _INV_TTL, a failed one retries at most
    every _INV_FAIL_BACKOFF. Never raises; on total failure returns
    {"available": False}."""
    global _inv_cache, _inv_stamp, _inv_failed
    with _inv_lock:
        ttl = _INV_FAIL_BACKOFF if _inv_failed else _INV_TTL
        if not refresh and _inv_cache is not None \
                and (time.monotonic() - _inv_stamp) < ttl:
            return _inv_cache
    fetched = _fetch_inventory()
    with _inv_lock:
        _inv_cache = fetched
        _inv_stamp = time.monotonic()
        _inv_failed = not fetched.get("available", False)
        return _inv_cache


def _fetch_inventory() -> dict:
    if os.name == "nt":
        text = _run_powershell(_INV_PS_SCRIPT)
        parsed = _parse_inventory_win(text) if text else None
        return parsed if parsed else {"available": False}
    return _inventory_posix()


def inventory_digest(inv: dict) -> List[str]:
    """Compact inventory lines for the agent tool (kept short: tool results
    ride the 8 K context cap in the loop's _fit_context)."""
    if not inv or not inv.get("available"):
        return ["Inventory unavailable (system query failed or was blocked)"]
    lines: List[str] = []
    cpu = inv.get("cpu") or {}
    if cpu.get("name"):
        bits = cpu["name"]
        if cpu.get("cores"):
            bits += f" — {cpu['cores']} cores / {cpu.get('logical', '?')} threads"
        if cpu.get("base_clock_mhz"):
            bits += f", base {cpu['base_clock_mhz']} MHz"
        lines.append(f"CPU: {bits}")
    sticks = inv.get("ram") or []
    if sticks:
        total = sum(s.get("capacity_gb") or 0 for s in sticks)
        speeds = sorted({int(s["configured_mts"] or s["speed_mts"] or 0)
                         for s in sticks
                         if s.get("configured_mts") or s.get("speed_mts")})
        speed_txt = (f" @ {speeds[0]} MT/s" if len(speeds) == 1
                     else f" @ {'/'.join(str(x) for x in speeds)} MT/s")
        maker = sticks[0].get("manufacturer") or ""
        lines.append(f"RAM: {len(sticks)} × "
                     f"{total / len(sticks):.0f} GB{speed_txt}"
                     + (f" ({maker})" if maker else ""))
    board = inv.get("board") or {}
    if board.get("model"):
        lines.append("Board: " + " ".join(
            p for p in (board.get("manufacturer"), board["model"]) if p))
    bios = inv.get("bios") or {}
    if bios.get("version"):
        lines.append(f"BIOS: {bios['version']}"
                     + (f" ({bios['date']})" if bios.get("date") else ""))
    for g in inv.get("gpus") or []:
        if g.get("name"):
            lines.append(f"GPU: {g['name']}"
                         + (f", driver {g['driver']}" if g.get("driver")
                            else ""))
    for d in inv.get("disks") or []:
        if d.get("model"):
            size = f" {d['size_gb']:.0f} GB" if d.get("size_gb") else ""
            bus = f" {d['bus']}" if d.get("bus") else ""
            lines.append(f"Disk: {d['model']}{size}{bus}")
    for n in inv.get("nics") or []:
        if n.get("name"):
            link = (f" ({n['link_bps'] / 1e6:.0f} Mb/s)"
                    if n.get("link_bps") else "")
            lines.append(f"NIC: {n['name']}{link}")
    osinfo = inv.get("os") or {}
    if osinfo.get("name"):
        lines.append("OS: " + " ".join(
            p for p in (osinfo["name"], osinfo.get("version"),
                        f"build {osinfo['build']}"
                        if osinfo.get("build") else None) if p))
    return lines or ["Inventory query returned no devices"]


# ---------------------------------------------------------------------------
# The snapshot + warnings
# ---------------------------------------------------------------------------

def _gpu_block() -> dict:
    gpu: Dict[str, Any] = {
        "name": "", "live": False, "percent": None,
        "vram_used_mb": 0, "vram_total_mb": 0,
        "temp_c": None, "fan_percent": None, "power_w": None,
        "pcie_gen": None, "pcie_width": None,
    }
    gpus = hardware_monitor.nvidia_smi_query()
    if gpus:
        g = gpus[0]
        gpu.update({
            "name": g.get("name") or "",
            "live": True,
            "percent": g.get("util"),
            "vram_used_mb": g.get("mem_used_mb") or 0,
            "vram_total_mb": g.get("mem_total_mb") or 0,
            "temp_c": g.get("temp_c"),
            "fan_percent": g.get("fan_percent"),
            "power_w": g.get("power_w"),
            "pcie_gen": g.get("pcie_gen"),
            "pcie_width": g.get("pcie_width"),
        })
        return gpu
    static = hardware_monitor.dxgi_adapters()
    if static:
        gpu["name"], gpu["vram_total_mb"] = static[0][0], static[0][1]
    return gpu


def _uptime_seconds() -> Optional[int]:
    if os.name == "nt":
        try:
            k32 = ctypes.windll.kernel32
            k32.GetTickCount64.restype = ctypes.c_ulonglong  # truncates as c_int
            return int(k32.GetTickCount64() // 1000)
        except Exception:
            return None
    try:
        with open("/proc/uptime", "r", encoding="ascii",
                  errors="ignore") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _pct(used: float, total: float) -> Optional[float]:
    if total <= 0:
        return None
    return round(min(100.0, used * 100.0 / total), 1)


def warnings_from(snap: dict, thresholds: Any = None) -> List[dict]:
    """Threshold warnings for a snapshot: GPU/CPU temperature, memory
    pressure, per-volume free space. ``thresholds`` is a SystemHealthConfig
    (or anything with the same field names); the defaults apply when None.
    Readings that are None (unknown) never warn."""
    if thresholds is None:
        # Imported at module top: a function-level import compiles config.py
        # on whatever thread first lands here (the collector thread) —
        # demonstrated as a native-AV dice trigger 2026-11-19.
        thresholds = SystemHealthConfig()
    out: List[dict] = []

    gpu = snap.get("gpu") or {}
    gpu_t = gpu.get("temp_c")
    if gpu_t is not None and gpu_t >= thresholds.temp_warn_gpu_c:
        out.append({
            "kind": "gpu_temp",
            "severity": ("danger" if gpu_t >= thresholds.temp_warn_gpu_c + 10
                         else "warn"),
            "message": (f"GPU temperature is {gpu_t:.0f} °C "
                        f"(threshold {thresholds.temp_warn_gpu_c} °C)"),
        })

    cpu_t = (snap.get("temperatures") or {}).get("cpu_c")
    if cpu_t is not None and cpu_t >= thresholds.temp_warn_cpu_c:
        out.append({
            "kind": "cpu_temp",
            "severity": ("danger" if cpu_t >= thresholds.temp_warn_cpu_c + 10
                         else "warn"),
            "message": (f"CPU/board temperature is {cpu_t:.0f} °C "
                        f"(threshold {thresholds.temp_warn_cpu_c} °C)"),
        })

    ram = snap.get("ram") or {}
    ram_pct = ram.get("percent")
    if ram_pct is not None and ram_pct >= 95.0:
        out.append({
            "kind": "ram",
            "severity": "danger" if ram_pct >= 98.0 else "warn",
            "message": f"Memory usage is at {ram_pct:.0f}%",
        })

    for d in snap.get("disks") or []:
        free_gb = d.get("free_gb")
        if free_gb is None:
            continue
        free_pct = d.get("free_percent")
        if (free_gb < thresholds.disk_min_free_gb
                or (free_pct is not None
                    and free_pct < thresholds.disk_min_free_percent)):
            danger = free_gb < max(1.0, thresholds.disk_min_free_gb / 2.0)
            out.append({
                "kind": "disk_free",
                "severity": "danger" if danger else "warn",
                "message": (f"{d.get('volume', '?')} has only "
                            f"{free_gb:.0f} GB free"),
            })
    return out


def snapshot(include_inventory: bool = False,
             thresholds: Any = None) -> dict:
    """One on-demand system-health reading. Never raises. ``thresholds``
    is a SystemHealthConfig; defaults apply when None."""
    try:
        ram_used_mb, ram_total_mb = hardware_monitor.ram_stats()
    except Exception:
        ram_used_mb, ram_total_mb = 0, 0
    try:
        commit_used_mb, commit_total_mb = hardware_monitor.pagefile_stats()
    except Exception:
        commit_used_mb, commit_total_mb = 0, 0
    try:
        # One-shot semantics: a cold process gets a real reading over a
        # short settle window instead of the 0.0 a first delta would give.
        cpu_pct = hardware_monitor.cpu_percent_quick()
    except Exception:
        cpu_pct = 0.0
    try:
        gpu = _gpu_block()
    except Exception:
        gpu = {"name": "", "live": False, "percent": None,
               "vram_used_mb": 0, "vram_total_mb": 0, "temp_c": None,
               "fan_percent": None, "power_w": None,
               "pcie_gen": None, "pcie_width": None}
    try:
        disks = disk_snapshot()
    except Exception:
        disks = []
    try:
        cpu_c = cpu_temperature(probe=True)
    except Exception:
        cpu_c = None

    snap: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "uptime_seconds": _uptime_seconds(),
        "cpu": {"percent": round(cpu_pct, 1),
                "cores": os.cpu_count() or 0},
        "ram": {"used_mb": ram_used_mb, "total_mb": ram_total_mb,
                "percent": _pct(ram_used_mb, ram_total_mb),
                "commit_used_mb": commit_used_mb,
                "commit_total_mb": commit_total_mb},
        "gpu": gpu,
        "temperatures": {"cpu_c": cpu_c, "gpu_c": gpu.get("temp_c")},
        "disks": disks,
    }
    snap["warnings"] = warnings_from(snap, thresholds)
    if include_inventory:
        snap["inventory"] = inventory_digest(inventory())
    return snap


def summary_line(snap: dict) -> str:
    """One-line human summary (agent tool UI line + quick reads)."""
    parts: List[str] = []
    cpu = (snap.get("cpu") or {}).get("percent")
    parts.append(f"CPU {cpu:.0f}%" if cpu is not None else "CPU ?")
    ram = snap.get("ram") or {}
    ram_txt = (f"RAM {ram.get('used_mb', 0) / 1024:.1f}/"
               f"{ram.get('total_mb', 0) / 1024:.1f} GB")
    if ram.get("percent") is not None:
        ram_txt += f" ({ram['percent']:.0f}%)"
    parts.append(ram_txt)
    gpu = snap.get("gpu") or {}
    if gpu.get("name"):
        if gpu.get("percent") is not None:
            gtxt = f"GPU {gpu['percent']:.0f}%"
            if gpu.get("temp_c") is not None:
                gtxt += f" {gpu['temp_c']:.0f}°C"
        else:
            gtxt = f"GPU {gpu['name']}"
        parts.append(gtxt)
    for d in snap.get("disks") or []:
        if d.get("volume") and d.get("free_gb") is not None:
            parts.append(f"{d['volume']} {d['free_gb']:.0f} GB free")
    warnings_ = snap.get("warnings") or []
    if warnings_:
        parts.append(f"{len(warnings_)} warning(s)")
    return " · ".join(parts)
