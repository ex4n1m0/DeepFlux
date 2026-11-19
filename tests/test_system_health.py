"""System Health engine + tab tests (infra/system_health.py,
gui/system_health_tab.py, hardware_monitor's extended smi columns).

The autouse fixture fakes EVERY external sampler (ctypes, nvidia-smi,
PowerShell, disk IOCTLs) and resets the module caches — a test must never
spawn a real process or touch the host's hardware state.
"""
import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import infra.hardware_monitor as hm
import infra.system_health as sh

# Captured at import time, BEFORE the autouse fixture swaps cpu_percent_quick
# for its fake — the two quick-helper tests below exercise the REAL one
# (which itself calls the REAL cpu_percent at the end).
_REAL_QUICK = hm.cpu_percent_quick
_REAL_CPU_PCT = hm.cpu_percent
# Same for the study-P0/P1 sources: the fixture fakes them for snapshot
# isolation, the dedicated engine tests re-attach the REAL ones.
_REAL_PERCPU = hm.per_core_percent
_REAL_NET = sh.network_snapshot
_REAL_DRV = sh.drive_health_snapshot
_REAL_BAT = sh.battery_status
_REAL_DXGI = hm.dxgi_adapters

_FAKE_IO = {}          # volume → (read_bytes, write_bytes) | None


def _fake_counters(volume):
    return _FAKE_IO.get(volume)


def _no_powershell(script, timeout=25.0):
    return None


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(sh.hardware_monitor, "cpu_percent", lambda: 12.5)
    monkeypatch.setattr(sh.hardware_monitor, "cpu_percent_quick",
                        lambda settle_seconds=0.2: 12.5)
    monkeypatch.setattr(sh.hardware_monitor, "per_core_percent",
                        lambda: [])
    monkeypatch.setattr(sh.hardware_monitor, "ram_stats",
                        lambda: (8000, 32000))
    monkeypatch.setattr(sh.hardware_monitor, "pagefile_stats",
                        lambda: (1000, 40000))
    monkeypatch.setattr(sh.hardware_monitor, "nvidia_smi_query", lambda: [])
    monkeypatch.setattr(sh.hardware_monitor, "dxgi_adapters", lambda: [])
    monkeypatch.setattr(sh, "_fixed_volumes", lambda: ["C:", "D:"])
    monkeypatch.setattr(sh, "read_volume_counters", _fake_counters)
    monkeypatch.setattr(sh, "_run_powershell", _no_powershell)
    monkeypatch.setattr(sh, "network_snapshot", lambda: [])
    monkeypatch.setattr(sh, "drive_health_snapshot", lambda: [])
    monkeypatch.setattr(sh, "battery_status", lambda: None)
    _FAKE_IO.clear()
    sh._disk_last.clear()
    sh._disk_dead.clear()
    sh._net_last.clear()
    sh._drv_cache.clear()
    saved = (sh._thermal_value, sh._thermal_stamp,
             sh._inv_cache, sh._inv_stamp, sh._inv_failed,
             sh._net_dead_until,
             hm._cpu_last, hm._percpu_last, hm._percpu_stride,
             hm._dxgi_cache, hm._dxgi_stamp)
    sh._thermal_value, sh._thermal_stamp = None, 0.0
    sh._inv_cache, sh._inv_stamp, sh._inv_failed = None, 0.0, False
    sh._net_dead_until = 0.0
    hm._cpu_last, hm._percpu_last, hm._percpu_stride = None, None, None
    hm._dxgi_cache, hm._dxgi_stamp = None, 0.0
    yield
    _FAKE_IO.clear()
    sh._disk_last.clear()
    sh._disk_dead.clear()
    sh._net_last.clear()
    sh._drv_cache.clear()
    (sh._thermal_value, sh._thermal_stamp,
     sh._inv_cache, sh._inv_stamp, sh._inv_failed,
     sh._net_dead_until,
     hm._cpu_last, hm._percpu_last, hm._percpu_stride,
     hm._dxgi_cache, hm._dxgi_stamp) = saved


# ------------------------------------------------------------- smi parsing

def test_smi_parse_extended_columns():
    gpus = hm.parse_smi_csv(
        "NVIDIA GeForce RTX 5080, 45, 8192, 16384, 71, 40, 220.5, 5, 16")
    assert len(gpus) == 1
    g = gpus[0]
    # Legacy keys unchanged (local_model depends on them).
    assert g["name"] == "NVIDIA GeForce RTX 5080"
    assert g["util"] == 45 and g["mem_used_mb"] == 8192
    # The new health columns.
    assert g["temp_c"] == 71 and g["fan_percent"] == 40
    assert g["power_w"] == 220.5
    assert g["pcie_gen"] == 5 and g["pcie_width"] == 16


def test_smi_parse_unsupported_columns_are_none():
    g = hm.parse_smi_csv(
        "GPU0, [Not Supported], 100, 200, [Not Supported], , 5.2, "
        "[Not Supported], 16")[0]
    assert g["util"] is None and g["temp_c"] is None
    assert g["fan_percent"] is None and g["pcie_gen"] is None
    assert g["power_w"] == 5.2 and g["pcie_width"] == 16


def test_smi_parse_legacy_four_column_line_still_works():
    g = hm.parse_smi_csv("GPU0, 30, 100, 200")[0]
    assert g["util"] == 30 and g["temp_c"] is None


def test_pagefile_stats_returns_nonnegative():
    used, total = hm.pagefile_stats()
    assert used >= 0 and total >= 0


def test_cpu_percent_quick_seeds_once_when_cold(monkeypatch):
    monkeypatch.setattr(hm, "cpu_percent_quick", _REAL_QUICK)
    monkeypatch.setattr(hm, "cpu_percent", _REAL_CPU_PCT)
    sleeps = []
    monkeypatch.setattr(hm.time, "sleep", lambda s: sleeps.append(s))
    # Baseline, then two readings whose deltas are an even busy/idle split.
    readings = iter([(100, 100), (150, 150), (175, 175)])
    monkeypatch.setattr(hm, "_cpu_times", lambda: next(readings))
    saved = hm._cpu_last
    hm._cpu_last = None
    try:
        first = hm.cpu_percent_quick()
        second = hm.cpu_percent_quick()
    finally:
        hm._cpu_last = saved
    # Cold: one short settle sleep, then a real delta reading; warm: no
    # more sleeps (cadence callers keep using seed + cpu_percent).
    assert sleeps == [0.2]
    assert first == 50.0
    assert second == 50.0


def test_cpu_percent_quick_unavailable_source_is_zero(monkeypatch):
    monkeypatch.setattr(hm, "cpu_percent_quick", _REAL_QUICK)
    monkeypatch.setattr(hm, "seed_cpu_baseline", lambda: None)
    saved = hm._cpu_last
    hm._cpu_last = None
    try:
        monkeypatch.setattr(hm, "_cpu_times", lambda: None)
        assert hm.cpu_percent_quick() == 0.0
    finally:
        hm._cpu_last = saved


# ------------------------------------------------------------- disk speeds

def test_disk_speeds_from_deltas():
    # Pre-seed a baseline 1 s in the past; 1000/2000 bytes since then.
    sh._disk_last["C:"] = (400, 800, time.monotonic() - 1.0)
    _FAKE_IO["C:"] = (1400, 2800)
    snap = sh.disk_snapshot()
    c = next(d for d in snap if d["volume"] == "C:")
    assert c["read_bps"] == pytest.approx(1000, abs=60)
    assert c["write_bps"] == pytest.approx(2000, abs=60)
    assert c["total_gb"] is not None and c["free_gb"] is not None


def test_disk_first_reading_has_no_speeds_and_never_crashes():
    _FAKE_IO["C:"] = (100, 200)
    snap = sh.disk_snapshot()
    c = next(d for d in snap if d["volume"] == "C:")
    assert c["read_bps"] is None and c["write_bps"] is None


def test_disk_refusing_ioctl_is_marked_cold():
    _FAKE_IO["C:"] = None            # CreateFile/DeviceIoControl refused
    sh.disk_snapshot()
    assert "C:" in sh._disk_dead
    # The volume is never asked again — no repeated failed IOCTLs per tick.
    _FAKE_IO["C:"] = (10, 10)
    snap = sh.disk_snapshot()
    c = next(d for d in snap if d["volume"] == "C:")
    assert c["read_bps"] is None


# -------------------------------------------------------------- temperature

def test_thermal_probe_kelvin_conversion(monkeypatch):
    # MSAcpi CurrentKelvin is in TENTHS of a kelvin: 3450 → 71.85 °C.
    monkeypatch.setattr(sh, "_run_powershell",
                        lambda s, timeout=25.0: "3450\n")
    value = sh.cpu_temperature(probe=True)
    assert value == pytest.approx(71.85, abs=0.15)


def test_thermal_probe_garbage_is_none(monkeypatch):
    monkeypatch.setattr(sh, "_run_powershell",
                        lambda s, timeout=25.0: "not a number")
    assert sh.cpu_temperature(probe=True) is None


def test_thermal_served_from_cache_within_ttl(monkeypatch):
    calls = []

    def fake(script, timeout=25.0):
        calls.append(1)
        return "3450"

    monkeypatch.setattr(sh, "_run_powershell", fake)
    sh.cpu_temperature(probe=True)
    sh.cpu_temperature(probe=True)
    assert len(calls) == 1, "a fresh value must be served from cache"


# ---------------------------------------------------------------- inventory

_CANNED_PS = json.dumps({
    "cpu": [{"Name": "AMD Ryzen 9", "NumberOfCores": 12,
             "NumberOfLogicalProcessors": 24, "MaxClockSpeed": 3400}],
    "ram": [{"DeviceLocator": "DIMM_A1", "Manufacturer": "Corsair",
             "PartNumber": "CMK", "Capacity": 17179869184, "Speed": 5600,
             "ConfiguredClockSpeed": 5200}],
    "board": [{"Manufacturer": "ASUS", "Product": "PROART X670E",
               "Version": "Rev 1.02"}],
    "bios": [{"SMBIOSBIOSVersion": "F22", "ReleaseDate": "2026-01-02"}],
    "gpus": [{"Name": "NVIDIA GeForce RTX 5080",
              "DriverVersion": "32.0.15.6099", "AdapterRAM": 429391872}],
    "disks": [{"FriendlyName": "Samsung 990 Pro", "BusType": "NVMe",
               "MediaType": "SSD", "Size": 2048366386176,
               "HealthStatus": "Healthy"}],
    "nics": [{"Name": "Intel(R) Ethernet Controller I226-V",
              "Speed": 1000000000}],
    "os": [{"Caption": "Microsoft Windows 11 Pro", "Version": "10.0.28000",
            "BuildNumber": 28000}],
})


def test_inventory_parse_and_digest(monkeypatch):
    monkeypatch.setattr(sh, "_run_powershell",
                        lambda s, timeout=25.0: _CANNED_PS)
    inv = sh.inventory(refresh=True)
    assert inv["available"] is True
    assert inv["cpu"]["name"] == "AMD Ryzen 9"
    assert inv["cpu"]["cores"] == 12
    assert inv["ram"][0]["capacity_gb"] == 16.0
    assert inv["ram"][0]["speed_mts"] == 5600
    assert inv["ram"][0]["configured_mts"] == 5200
    assert inv["disks"][0]["bus"] == "NVMe"
    digest = sh.inventory_digest(inv)
    joined = "\n".join(digest)
    assert "Ryzen" in joined and "5200 MT/s" in joined
    assert "990 Pro" in joined and "NVMe" in joined


def test_inventory_served_from_cache(monkeypatch):
    calls = []

    def fake(script, timeout=25.0):
        calls.append(1)
        return _CANNED_PS

    monkeypatch.setattr(sh, "_run_powershell", fake)
    sh.inventory(refresh=True)
    sh.inventory()
    assert len(calls) == 1


def test_inventory_failure_degrades(monkeypatch):
    monkeypatch.setattr(sh, "_run_powershell", _no_powershell)
    inv = sh.inventory(refresh=True)
    assert inv["available"] is False
    # Honest unavailable line, never an exception.
    assert sh.inventory_digest(inv)


def test_inventory_tolerates_scalar_unwrapping():
    # PowerShell unwraps single-element arrays: {"ram": {...}} not [{...}].
    scalar = json.dumps({"cpu": {"Name": "Solo CPU", "NumberOfCores": 8,
                                 "NumberOfLogicalProcessors": 8,
                                 "MaxClockSpeed": 3000}})
    parsed = sh._parse_inventory_win(scalar)
    assert parsed["cpu"]["name"] == "Solo CPU"
    assert parsed["ram"] == [] and parsed["gpus"] == []


# ---------------------------------------------------------------- snapshot

def _snap(thresholds=None):
    return sh.snapshot(thresholds=thresholds)


def test_snapshot_shape_and_summary():
    sh._disk_last["C:"] = (0, 0, time.monotonic() - 1.0)
    _FAKE_IO["C:"] = (100, 200)
    snap = _snap()
    assert snap["cpu"]["percent"] == 12.5
    assert snap["ram"]["percent"] == 25.0
    assert snap["ram"]["commit_total_mb"] == 40000
    assert snap["disks"][0]["volume"] == "C:"
    assert snap["disks"][0]["read_bps"] is not None
    assert snap["warnings"] == []
    assert snap["uptime_seconds"] is None or snap["uptime_seconds"] >= 0
    line = sh.summary_line(snap)
    assert "CPU" in line and "RAM" in line and "C:" in line


def test_snapshot_with_inventory_adds_digest(monkeypatch):
    monkeypatch.setattr(sh, "_run_powershell",
                        lambda s, timeout=25.0: _CANNED_PS)
    snap = sh.snapshot(include_inventory=True)
    assert snap["inventory"] and any("Ryzen" in line
                                     for line in snap["inventory"])


def test_snapshot_gpu_static_fallback_names_card():
    # No smi, but DXGI knows the card: name + VRAM, no live numbers.
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(sh.hardware_monitor, "dxgi_adapters",
                            lambda *a, **k: [("Intel Arc A770", 16384)])
        gpu = sh._gpu_block()
        assert gpu["name"] == "Intel Arc A770"
        assert gpu["vram_total_mb"] == 16384 and gpu["live"] is False
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------- warnings

def _warn_snap(**over):
    snap = {"gpu": {"temp_c": 60.0, "name": "X"},
            "temperatures": {"cpu_c": 50.0, "gpu_c": 60.0},
            "ram": {"percent": 40.0},
            "disks": [{"volume": "C:", "free_gb": 400.0,
                       "free_percent": 40.0}]}
    snap.update(over)
    return snap


def test_warnings_healthy_snapshot_is_empty():
    from config import SystemHealthConfig
    assert sh.warnings_from(_warn_snap(), SystemHealthConfig()) == []


def test_warnings_hot_gpu_and_cpu_and_full_disk_and_ram():
    from config import SystemHealthConfig
    snap = _warn_snap(
        gpu={"temp_c": 92.0, "name": "X"},
        temperatures={"cpu_c": 95.0, "gpu_c": 92.0},
        ram={"percent": 97.0},
        disks=[{"volume": "C:", "free_gb": 3.0, "free_percent": 1.0}])
    warns = sh.warnings_from(snap, SystemHealthConfig())
    kinds = {w["kind"] for w in warns}
    assert kinds == {"gpu_temp", "cpu_temp", "ram", "disk_free"}
    assert any(w["severity"] == "danger" for w in warns)
    messages = " ".join(w["message"] for w in warns)
    assert "92" in messages and "C:" in messages


def test_warnings_use_config_thresholds():
    from config import SystemHealthConfig
    cfg = SystemHealthConfig(temp_warn_gpu_c=60)
    warns = sh.warnings_from(_warn_snap(), cfg)   # gpu at 60 == threshold
    assert [w["kind"] for w in warns] == ["gpu_temp"]


def test_warnings_unknown_readings_never_warn():
    snap = {"gpu": {"temp_c": None},
            "temperatures": {"cpu_c": None, "gpu_c": None},
            "ram": {"percent": None},
            "disks": [{"volume": "C:", "free_gb": None}]}
    assert sh.warnings_from(snap) == []


# ------------------------------------------------------------------ the tab

@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _tab(qapp):
    from config import DeeptorrentConfig
    from gui.system_health_tab import SystemHealthTab
    return SystemHealthTab(DeeptorrentConfig())


def _fake_sample(temp=71.0, free_gb=400.0, warnings=None):
    return {
        "generated_at": "2026-11-19T00:00:00+00:00",
        "uptime_seconds": 90061,
        "cpu": {"percent": 33.3, "cores": 24},
        "ram": {"used_mb": 8000, "total_mb": 32000, "percent": 25.0,
                "commit_used_mb": 1000, "commit_total_mb": 40000},
        "gpu": {"name": "RTX Test", "live": True, "percent": 55.0,
                "vram_used_mb": 8192, "vram_total_mb": 16384,
                "temp_c": temp, "fan_percent": 40.0, "power_w": 220.5,
                "pcie_gen": 5.0, "pcie_width": 16.0},
        "temperatures": {"cpu_c": None, "gpu_c": temp},
        "disks": [{"volume": "C:", "total_gb": 931.0, "free_gb": free_gb,
                   "used_percent": 50.0, "free_percent": 50.0,
                   "read_bps": 1048576.0, "write_bps": 2097152.0}],
        "warnings": warnings if warnings is not None else [],
    }


def test_tab_applies_sample_disks_and_warning_banner(qapp):
    tab = _tab(qapp)
    tab._apply_sample((_fake_sample(), None))
    assert tab._updated_lbl.text() != ""
    assert "C:" in tab._disk_rows
    assert "1.0 MB/s" in tab._disk_rows["C:"][1].text()
    assert "2.0 MB/s" in tab._disk_rows["C:"][1].text()
    assert not tab._warn_lbl.isVisibleTo(tab)

    danger = [{"kind": "gpu_temp", "severity": "danger",
               "message": "GPU temperature is 95 °C (threshold 85 °C)"}]
    tab._apply_sample((_fake_sample(temp=95.0, warnings=danger), None))
    assert tab._warn_lbl.isVisibleTo(tab)
    assert "95" in tab._warn_lbl.text()
    # Vanished volume rows are dropped, not zombied.
    tab._apply_sample((_fake_sample(), None))
    assert not tab._warn_lbl.isVisibleTo(tab)


def test_tab_timer_is_gated_by_set_active_and_shutdown(qapp):
    tab = _tab(qapp)
    assert not tab._timer.isActive(), "construction must not sample"
    tab.set_active(True)
    assert tab._timer.isActive()
    tab.set_active(False)
    assert not tab._timer.isActive(), "leaving the tab must stop sampling"
    tab.set_active(True)
    tab.shutdown()
    assert not tab._timer.isActive() and tab._closing
    tab.set_active(True)
    assert not tab._timer.isActive(), "shutdown is final"
    # DRAIN the real collector thread set_active kicked: the autouse
    # fixture tears its fakes down at test end, and a zombie thread that
    # survives into the REAL native readers mid-flight is the heap
    # corruption (0xc0000374) detonation of 2026-11-19's 8.9 test runs.
    deadline = time.monotonic() + 10.0
    while tab._collector_busy and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.05)   # let the queued emit land before teardown


def test_tab_collector_never_piles_up(qapp, monkeypatch):
    import gui.system_health_tab as tab_mod
    created = []

    class _RecordingThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            created.append(1)

    monkeypatch.setattr(tab_mod.threading, "Thread", _RecordingThread)
    tab = _tab(qapp)
    tab._collector_busy = True
    tab._kick_collector()
    assert created == [], "a busy collector must not spawn a second thread"
    tab._collector_busy = False
    tab._kick_collector()
    assert len(created) == 1


def test_tab_inventory_fills_tree(qapp):
    tab = _tab(qapp)
    inv = {"available": True,
           "cpu": {"name": "AMD Ryzen 9", "cores": 12, "logical": 24,
                   "base_clock_mhz": 3400},
           "ram": [{"slot": "DIMM_A1", "manufacturer": "Corsair",
                    "part": "CMK", "capacity_gb": 16.0, "speed_mts": 5600,
                    "configured_mts": 5200}],
           "board": {"manufacturer": "ASUS", "model": "X670E", "version": ""},
           "bios": {"version": "F22", "date": ""},
           "gpus": [{"name": "RTX 5080", "driver": "32.0",
                     "vram_mb": 429391872}],
           "disks": [{"model": "Samsung 990 Pro", "bus": "NVMe",
                      "media": "SSD", "size_gb": 2048.0,
                      "health": "Healthy"}],
           "nics": [{"name": "Intel I226-V", "link_bps": 1000000000}],
           "os": {"name": "Windows 11", "version": "10.0.28000",
                  "build": 28000}}
    tab._apply_sample((_fake_sample(), inv))
    names = [tab._inv_tree.topLevelItem(i).text(0)
             for i in range(tab._inv_tree.topLevelItemCount())]
    assert "CPU" in names and "Motherboard" in names
    assert "BIOS" in names and "Operating system" in names
    details = " ".join(tab._inv_tree.topLevelItem(i).text(1)
                       for i in range(tab._inv_tree.topLevelItemCount()))
    assert "5200 MT/s" in details and "NVMe" in details
    # A failed inventory is honest, not empty.
    tab._fill_inventory({"available": False})
    assert tab._inv_tree.topLevelItem(0).text(1).startswith("unavailable")


def test_tab_minimum_stays_small(qapp):
    tab = _tab(qapp)
    tab.resize(1400, 900)
    tab.show()
    qapp.processEvents()
    msh = tab.minimumSizeHint()
    assert msh.width() <= 700 and msh.height() <= 460
    tab.shutdown()


def test_ioctl_disk_performance_code_is_derived_not_guessed():
    """8.5 shipped 0x70000 — which is IOCTL_DISK_GET_DRIVE_GEOMETRY
    (function 0x0000), so the driver filled a 24-byte DISK_GEOMETRY that
    read_volume_counters misread as counters: constant deltas, disk speeds
    0 B/s everywhere (owner-adjacent finding 2026-11-19). The real value
    is CTL_CODE(IOCTL_DISK_BASE=7, 0x0008, METHOD_BUFFERED, FILE_ANY_ACCESS)
    — derive it here so a typo can never ship silently again."""
    ctl_code = (7 << 16) | (0 << 14) | (0x0008 << 2) | 0
    assert ctl_code == 0x70020
    assert sh.IOCTL_DISK_PERFORMANCE == ctl_code


def test_warnings_from_imports_config_at_module_level():
    """The default-thresholds import must NOT be function-level: compiling
    config.py lazily on the collector thread is a native-AV dice trigger
    (demonstrated 2026-11-19 — AV inside _compile_bytecode on the sampler
    thread), and in any fresh process it re-parses a module every call."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(sh.warnings_from))
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.Import, ast.ImportFrom)), \
            f"lazy import found in warnings_from: line {node.lineno}"


# =========================================================== study P0/P1 work
# SYSTEM_HEALTH_STUDY.md (2026-11-19): negative-delta clamps, the IOCTL
# error ladder, per-core CPU, network throughput, drive health, battery.
import ctypes
import inspect
import struct
import unittest.mock


# ------------------------------------------------------- P0-1: delta clamps

def test_cpu_percent_clamps_negative_deltas(monkeypatch):
    """Windows CPU counters can go backwards (psutil #392/#645/#1210 —
    they clamp, htop saturatingSub, btop4win max(0ll)). The MIXED-SIGN
    sequence is the one that distinguishes: idle falls 200→100 while
    busy grows 100→150. Clamped: 100% busy. Unclamped: idle delta -100
    drags total negative → the total<=0 guard would read 0.0. (A plain
    backwards busy step alone is vacuous — it trips the guard either
    way.)"""
    monkeypatch.setattr(hm, "cpu_percent", _REAL_CPU_PCT)
    prev, cur = (200, 100), (100, 150)

    with unittest.mock.patch.object(hm, "_cpu_times", lambda: cur):
        hm._cpu_last = (prev[0], prev[1], 0.0)
        assert hm.cpu_percent() == 100.0

    # Teeth check: without the clamps the same sequence reads 0.0.
    source = inspect.getsource(hm)
    clamped = source.count("max(0, t[0] - prev[0])")
    assert clamped == 1, "negative-delta clamp missing from cpu_percent"


# --------------------------------------------------- P1-2: per-core engine

def test_per_core_first_call_empty_then_percentages(monkeypatch):
    monkeypatch.setattr(hm, "per_core_percent", _REAL_PERCPU)
    rows1 = [(4000, 1000), (2000, 500)]
    rows2 = [(5000, 1500), (3000, 700)]
    state = {"i": 0}

    def fake(count, stride):
        rows = [rows1, rows2][min(state["i"], 1)]
        state["i"] += 1
        return rows, count * stride, 0

    monkeypatch.setattr(hm, "_percpu_read", fake)
    import unittest.mock as mock
    with mock.patch.object(hm, "_cpu_times", lambda: None):
        assert hm.per_core_percent() == []       # baseline only
        hm._percpu_last = (rows1, time.monotonic() - 1.0)
        values = hm.per_core_percent()
    assert len(values) == 2
    assert all(0.0 <= v <= 100.0 for v in values)
    assert values[0] > values[1]                 # core 0 busier by craft


def test_per_core_stride_falls_back_to_40(monkeypatch):
    monkeypatch.setattr(hm, "per_core_percent", _REAL_PERCPU)
    rows = [(4000, 1000)]

    def fake(count, stride):
        if stride == 48:
            return None, 0, 0xC0000004 - (1 << 32)   # length mismatch
        return rows, count * stride, 0

    monkeypatch.setattr(hm, "_percpu_read", fake)
    import unittest.mock as mock
    with mock.patch.object(hm, "_cpu_times", lambda: None):
        hm.per_core_percent()
        assert hm._percpu_stride == 40


def test_per_core_hard_failure_is_sticky(monkeypatch):
    monkeypatch.setattr(hm, "per_core_percent", _REAL_PERCPU)
    calls = {"n": 0}

    def fake(count, stride):
        calls["n"] += 1
        return None, 0, 0xC0000004 - (1 << 32)

    monkeypatch.setattr(hm, "_percpu_read", fake)
    import unittest.mock as mock
    with mock.patch.object(hm, "_cpu_times", lambda: None):
        assert hm.per_core_percent() == []
        assert hm.per_core_percent() == []      # no re-probe
    assert calls["n"] == 2                       # 48 + 40, then never again
    assert hm._percpu_stride == 0


# ------------------------------------------------ P0-2: IOCTL error ladder

def test_disk_perf_ladder_doubles_buffer_on_insufficient(monkeypatch):
    """psutil's disk.c retry: ERROR_INSUFFICIENT_BUFFER → double the
    buffer (drivers may return a bigger struct) instead of giving up."""
    sizes = []

    def fake_deviceio(handle, ctl, inb, inl, outb, outl, ret, ov):
        sizes.append(outl)
        if outl < 200:
            monkeypatch.setattr(ctypes, "get_last_error", lambda: 122)
            return 0
        struct.pack_into("<qq", outb, 0, 111, 222)
        ret._obj.value = ctypes.sizeof(sh._DISK_PERFORMANCE)
        return 1

    assert sh._query_disk_performance(None, fake_deviceio) == (111, 222)
    assert len(sizes) >= 2 and sizes[1] > sizes[0]   # it grew


def test_disk_perf_invalid_function_is_exotic(monkeypatch):
    calls = {"n": 0}

    def fake_deviceio(handle, ctl, inb, inl, outb, outl, ret, ov):
        calls["n"] += 1
        monkeypatch.setattr(ctypes, "get_last_error", lambda: 1)
        return 0

    assert sh._query_disk_performance(None, fake_deviceio) is None
    assert calls["n"] == 1   # INVALID_FUNCTION: no retry loop


def test_disk_perf_short_write_is_distrusted():
    def fake_deviceio(handle, ctl, inb, inl, outb, outl, ret, ov):
        ret._obj.value = 10   # far less than sizeof(DISK_PERFORMANCE)
        return 1

    assert sh._query_disk_performance(None, fake_deviceio) is None


# -------------------------------------------------- P1-1: network snapshot

def _craft_if_row(index, name, hw=False, oper=1, type_=6,
                  rx=0, tx=0, in_oc=0, out_oc=0):
    raw = bytearray(sh._IF_ROW_STRIDE)
    struct.pack_into("<I", raw, sh._IF_OFF_INDEX, index)
    encoded = name.encode("utf-16-le")
    raw[sh._IF_OFF_DESCRIPTION:sh._IF_OFF_DESCRIPTION + len(encoded)] = \
        encoded
    struct.pack_into("<I", raw, sh._IF_OFF_TYPE, type_)
    if hw:
        raw[sh._IF_OFF_FLAGS] = 1
    struct.pack_into("<I", raw, sh._IF_OFF_OPER, oper)
    struct.pack_into("<2Q", raw, sh._IF_OFF_SPEEDS, tx, rx)
    struct.pack_into("<Q", raw, sh._IF_OFF_IN_OCTETS, in_oc)
    struct.pack_into("<Q", raw, sh._IF_OFF_OUT_OCTETS, out_oc)
    return bytes(raw)


def test_parse_if_row_pinned_offsets():
    row = sh._parse_if_row(
        _craft_if_row(7, "Intel(R) Wi-Fi 7 BE200", hw=True,
                      rx=865_000_000, in_oc=1234, out_oc=99))
    assert row["index"] == 7
    assert row["name"] == "Intel(R) Wi-Fi 7 BE200"
    assert row["hw"] is True and row["oper"] == 1
    assert row["rx_speed"] == 865_000_000
    assert row["in"] == 1234 and row["out"] == 99
    assert sh._parse_if_row(b"\x00" * sh._IF_ROW_STRIDE) is None  # idx 0


def test_network_snapshot_filters_mirrors_and_keeps_real(monkeypatch):
    monkeypatch.setattr(sh, "network_snapshot", _REAL_NET)
    """Live-verified shape: WFP/QoS rows duplicate the real adapter's
    counters to the byte — only HardwareInterface rows (and VPNs with
    live traffic) may appear."""
    rows = [
        {"index": 4, "name": "Intel(R) Wi-Fi 7 BE200 320MHz",
         "type": 71, "hw": True, "oper": 1, "rx_speed": 865_000_000,
         "tx_speed": 865_000_000, "in": 1000, "out": 100},
        {"index": 32, "name": "Intel(R) Wi-Fi 7 BE200 320MHz-WFP Nat",
         "type": 71, "hw": False, "oper": 1, "rx_speed": 865_000_000,
         "tx_speed": 865_000_000, "in": 1000, "out": 100},
        {"index": 49, "name": "WAN Miniport (IP)-QoS Packet Scheduler",
         "type": 6, "hw": False, "oper": 1, "rx_speed": 0, "tx_speed": 0,
         "in": 0, "out": 0},
        {"index": 60, "name": "Loopback", "type": 24, "hw": False,
         "oper": 1, "rx_speed": 0, "tx_speed": 0, "in": 5, "out": 5},
    ]
    with unittest.mock.patch.object(sh, "_win_iftable_rows",
                                    lambda: rows):
        first = sh.network_snapshot()
        assert [e["name"] for e in first] == ["Intel(R) Wi-Fi 7 BE200 320MHz"]
        assert first[0]["rx_bps"] is None       # no delta yet
        for key in list(sh._net_last):          # fake a 1 s sample gap
            rx, tx, _ = sh._net_last[key]
            sh._net_last[key] = (rx, tx, time.monotonic() - 1.0)
        for row in rows:                        # advance the counters
            row["in"] += 2000
            row["out"] += 500
        second = sh.network_snapshot()
    assert len(second) == 1
    assert second[0]["rx_bps"] > 0 and second[0]["tx_bps"] > 0
    assert second[0]["link_mbps"] == 865


def test_network_vpn_shown_only_with_traffic(monkeypatch):
    monkeypatch.setattr(sh, "network_snapshot", _REAL_NET)
    rows = [
        {"index": 4, "name": "Ethernet", "type": 6, "hw": True, "oper": 1,
         "rx_speed": 1000, "tx_speed": 1000, "in": 10, "out": 10},
        {"index": 50, "name": "Tailscale", "type": 53, "hw": False,
         "oper": 1, "rx_speed": 0, "tx_speed": 0, "in": 0, "out": 0},
    ]
    with unittest.mock.patch.object(sh, "_win_iftable_rows",
                                    lambda: rows):
        assert len(sh.network_snapshot()) == 1         # idle VPN hidden
        for key in list(sh._net_last):          # fake a 1 s sample gap
            rx, tx, _ = sh._net_last[key]
            sh._net_last[key] = (rx, tx, time.monotonic() - 1.0)
        for row in rows:
            row["in"] += 500
        names = [e["name"] for e in sh.network_snapshot()]
    assert names == ["Ethernet", "Tailscale"]          # VPN now visible


def test_network_counter_reset_distrusts_window(monkeypatch):
    monkeypatch.setattr(sh, "network_snapshot", _REAL_NET)
    rows = [{"index": 4, "name": "Ethernet", "type": 6, "hw": True,
             "oper": 1, "rx_speed": 0, "tx_speed": 0, "in": 9000,
             "out": 9000}]
    with unittest.mock.patch.object(sh, "_win_iftable_rows",
                                    lambda: rows):
        sh.network_snapshot()                          # baseline
        rows[0]["in"] = 5                              # replug: DECREASED
        rows[0]["out"] += 10
        entry = sh.network_snapshot()[0]
    assert entry["rx_bps"] is None and entry["tx_bps"] is None


def test_network_failure_backoff(monkeypatch):
    monkeypatch.setattr(sh, "network_snapshot", _REAL_NET)
    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        return None

    with unittest.mock.patch.object(sh, "_win_iftable_rows", failing):
        assert sh.network_snapshot() == []
        assert sh.network_snapshot() == []
    assert calls["n"] == 1          # dead-until stamp: no second attempt


def test_posix_net_rows_parses_proc_net_dev(monkeypatch):
    import io
    fake_text = ("Inter-|   Receive                            |"
                 "  Transmit\n"
                 " face |bytes packets errs drop fifo frame"
                 " compressed multicast|bytes packets\n"
                 "  eth0: 1000 5 0 0 0 0 0 0 500 4 0 0 0 0 0 0\n"
                 "  lo: 99 1 0 0 0 0 0 0 99 1 0 0 0 0 0 0\n"
                 " badline\n")

    class _FakeFile(io.StringIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(path, *a, **kw):
        assert path == "/proc/net/dev"
        return _FakeFile(fake_text)

    monkeypatch.setattr("builtins.open", fake_open)
    rows = sh._posix_if_rows()
    assert len(rows) == 1 and rows[0]["name"] == "eth0"
    assert rows[0]["in"] == 1000 and rows[0]["out"] == 500


# --------------------------------------------------- P1-3: drive health

def _fake_nvme_log(temp_k=311, used=5, spare=100, crit=0, unsafe=7):
    log = bytearray(512)
    log[0] = crit
    struct.pack_into("<H", log, 1, temp_k)
    log[3] = spare
    log[5] = used
    struct.pack_into("<Q", log, 144, unsafe)
    return bytes(log)


def test_parse_nvme_health():
    d = sh._parse_nvme_health(_fake_nvme_log())
    assert d["kind"] == "nvme"
    assert d["temp_c"] == 37.9 or d["temp_c"] == 37.8   # 311 K − 273.15
    assert d["used_percent"] == 5 and d["spare_percent"] == 100
    assert d["critical_warning"] == 0 and d["unsafe_shutdowns"] == 7
    # Implausible temperature: temp None, everything else intact.
    d2 = sh._parse_nvme_health(_fake_nvme_log(temp_k=100))
    assert d2["temp_c"] is None and d2["used_percent"] == 5
    # 255 = vendor "not reported" sentinel → None, never "255% used".
    assert sh._parse_nvme_health(
        _fake_nvme_log(used=255))["used_percent"] is None
    # All-zero log (non-NVMe answering empty) → None.
    assert sh._parse_nvme_health(bytes(512)) is None
    assert sh._parse_nvme_health(b"\x00" * 8) is None


def test_disk_temp_threshold_survives_config_roundtrip(tmp_path):
    """Review finding (MED): from_file must load temp_warn_disk_c or the
    user-only knob silently resets to the default on every restart."""
    from config import DeeptorrentConfig
    path = str(tmp_path / "cfg.json")
    cfg = DeeptorrentConfig()
    cfg.system_health.temp_warn_disk_c = 55
    cfg.to_file(path)
    loaded = DeeptorrentConfig.from_file(path)
    assert loaded.system_health.temp_warn_disk_c == 55


def test_parse_sata_smart():
    vendor = bytearray(512)
    # attr 194 @ entry 0: id, flags u16, value, worst, raw[6], resv
    vendor[2] = 194
    vendor[5] = 36          # normalized current value @ off+3
    vendor[7] = 41          # raw[0] @ off+5 — the real Celsius reading
    d = sh._parse_sata_smart(bytes(vendor), 0)
    assert d["kind"] == "sata" and d["temp_c"] == 41.0
    assert d["predict_failure"] is False and d["temp_attr"] == 194
    # Predicted failure flag rides along.
    assert sh._parse_sata_smart(bytes(vendor), 1)["predict_failure"] is True
    # No temperature attribute → None, never a fabricated 0.
    vendor[2] = 0
    assert sh._parse_sata_smart(bytes(vendor), 0)["temp_c"] is None


def test_drive_health_snapshot_names_and_cache(monkeypatch):
    monkeypatch.setattr(sh, "drive_health_snapshot", _REAL_DRV)
    opened = {"n": 0}
    queries = {"n": 0}

    class _Handle:
        pass

    def fake_open_drive(n):
        opened["n"] += 1
        return _Handle() if n in (0, 1) else None

    def fake_nvme(handle):
        queries["n"] += 1
        return sh._parse_nvme_health(_fake_nvme_log(temp_k=308))

    with unittest.mock.patch.object(sh, "_open_physical_drive",
                                    fake_open_drive), \
         unittest.mock.patch.object(sh, "_query_nvme_health", fake_nvme), \
         unittest.mock.patch.object(sh, "_query_sata_smart",
                                    lambda h: None), \
         unittest.mock.patch.object(sh, "inventory", lambda **kw: {
             "available": True,
             "disks": [{"id": 0, "model": "Samsung 970", "bus": "NVMe"},
                       {"id": 1, "model": "Samsung 970", "bus": "NVMe"}]}):
        drives = sh.drive_health_snapshot()
        assert [d["name"] for d in drives] == ["Samsung 970", "Samsung 970"]
        assert all(d["temp_c"] is not None and d["health"] == "ok"
                   for d in drives)
        sh.drive_health_snapshot()
        assert queries["n"] == 2          # TTL cache: 2 live queries


def test_drive_health_backoff_serves_last_good(monkeypatch):
    monkeypatch.setattr(sh, "drive_health_snapshot", _REAL_DRV)
    class _Handle:
        pass

    state = {"ok": True}

    def fake_nvme(handle):
        return sh._parse_nvme_health(_fake_nvme_log()) if state["ok"] \
            else None

    with unittest.mock.patch.object(
            sh, "_open_physical_drive",
            lambda n: _Handle() if n == 0 else None), \
         unittest.mock.patch.object(sh, "_query_nvme_health", fake_nvme), \
         unittest.mock.patch.object(sh, "_query_sata_smart",
                                    lambda h: None), \
         unittest.mock.patch.object(sh, "inventory",
                                    lambda **kw: {"available": False}):
        first = sh.drive_health_snapshot()
        assert first and first[0]["temp_c"] is not None
        state["ok"] = False
        sh._drv_cache[0] = (sh._drv_cache[0][0], 0.0)  # force TTL expiry
        second = sh.drive_health_snapshot()
        assert second and second[0]["temp_c"] == first[0]["temp_c"]


def test_warnings_disk_temp_and_health():
    snap = {"drives": [
        {"index": 0, "name": "D0", "temp_c": 65, "health": "warn"},
        {"index": 1, "name": "D1", "temp_c": None, "health": "fail"},
    ]}
    warnings = sh.warnings_from(snap)
    severities = {w["kind"]: w["severity"] for w in warnings}
    assert severities["disk_temp"] == "warn"       # 70 < 60+10
    assert severities["disk_health"] == "danger"


# ------------------------------------------------------- P1-4: battery

def test_battery_windows_parsing(monkeypatch):
    monkeypatch.setattr(sh, "battery_status", _REAL_BAT)
    def fake_status(ref):
        obj = ref._obj
        obj.ACLineStatus = 1
        obj.BatteryFlag = 0x08          # charging
        obj.BatteryLifePercent = 66
        return 1

    monkeypatch.setattr(ctypes.windll.kernel32, "GetSystemPowerStatus",
                        fake_status)
    assert sh.battery_status() == {"percent": 66, "charging": True,
                                   "ac": True}


def test_battery_absent_is_none(monkeypatch):
    monkeypatch.setattr(sh, "battery_status", _REAL_BAT)
    def fake_status(ref):
        obj = ref._obj
        obj.BatteryFlag = 0x80          # BATTERY_FLAG_NO_BATTERY
        obj.BatteryLifePercent = 255
        return 1

    monkeypatch.setattr(ctypes.windll.kernel32, "GetSystemPowerStatus",
                        fake_status)
    assert sh.battery_status() is None


# ------------------------------------------------------ P0-3: gpu clamps

def test_gpu_block_rejects_impossible_readings(monkeypatch):
    """LHM's rule: some cards 'report 54000 °C' — garbage outside the
    plausibility range reads as absent, never displays, never warns."""
    monkeypatch.setattr(sh.hardware_monitor, "nvidia_smi_query", lambda: [{
        "name": "RTX 5080", "util": 54000.0, "mem_used_mb": 999999,
        "mem_total_mb": 16384, "temp_c": 54000.0, "fan_percent": -3,
        "power_w": 99999.0, "pcie_gen": 99, "pcie_width": 250}])
    gpu = sh._gpu_block()
    assert gpu["live"] is True
    assert gpu["percent"] is None and gpu["temp_c"] is None
    assert gpu["fan_percent"] is None and gpu["power_w"] is None
    assert gpu["pcie_gen"] is None and gpu["pcie_width"] is None
    assert gpu["vram_used_mb"] == 16384   # glitch clamped to total


# ------------------------------------------------- snapshot integration

def test_snapshot_carries_new_sections(monkeypatch):
    monkeypatch.setattr(sh.hardware_monitor, "per_core_percent",
                        lambda: [10.0, 20.0])
    monkeypatch.setattr(sh, "network_snapshot",
                        lambda: [{"name": "eth0", "rx_bps": 5.0,
                                  "tx_bps": 1.0, "link_mbps": 1000}])
    monkeypatch.setattr(sh, "drive_health_snapshot",
                        lambda: [{"index": 0, "name": "D0", "kind": "nvme",
                                  "temp_c": 40.0, "health": "ok"}])
    monkeypatch.setattr(sh, "battery_status",
                        lambda: {"percent": 80, "charging": False,
                                 "ac": True})
    snap = sh.snapshot()
    assert snap["cpu"]["per_core"] == [10.0, 20.0]
    assert snap["network"][0]["name"] == "eth0"
    assert snap["drives"][0]["temp_c"] == 40.0
    assert snap["battery"]["percent"] == 80
    assert "battery 80%" in sh.summary_line(snap)


def test_snapshot_never_raises_when_new_sources_fail(monkeypatch):
    def boom():
        raise RuntimeError("source down")

    monkeypatch.setattr(sh, "network_snapshot", boom)
    monkeypatch.setattr(sh, "drive_health_snapshot", boom)
    monkeypatch.setattr(sh, "battery_status", boom)
    monkeypatch.setattr(sh.hardware_monitor, "per_core_percent", boom)
    snap = sh.snapshot()
    assert snap["network"] == [] and snap["drives"] == []
    assert snap["battery"] is None and snap["cpu"]["per_core"] == []


# ----------------------------------------------------- inventory joins

def test_inventory_cached_only_never_fetches(monkeypatch):
    calls = {"n": 0}

    def no_ps(script, timeout=25.0):
        calls["n"] += 1
        return None

    monkeypatch.setattr(sh, "_run_powershell", no_ps)
    assert sh.inventory(cached_only=True) == {"available": False}
    assert calls["n"] == 0


def test_inventory_parse_carries_drive_ids():
    payload = json.dumps({
        "cpu": [{"Name": "CPU", "NumberOfCores": 8,
                 "NumberOfLogicalProcessors": 16, "MaxClockSpeed": 3600}],
        "disks": [{"DeviceId": "3", "FriendlyName": "SSK Portable",
                   "BusType": "USB", "MediaType": "Unspecified",
                   "Size": 512110190592, "HealthStatus": "Healthy"}],
    })
    parsed = sh._parse_inventory_win(payload)
    assert parsed["disks"][0]["id"] == "3"
    assert parsed["disks"][0]["model"] == "SSK Portable"


# ------------------------------------------------------------ tab wiring

def _fake_sample_v2(per_core=None, network=None, drives=None, battery=None):
    return {
        "generated_at": "2026-11-19T00:00:00+00:00",
        "uptime_seconds": 3600,
        "cpu": {"percent": 12.0, "cores": 16, "per_core": per_core or []},
        "ram": {"used_mb": 8000, "total_mb": 32000, "percent": 25.0,
                "commit_used_mb": 1000, "commit_total_mb": 40000},
        "gpu": {"name": "RTX", "live": True, "percent": 5.0,
                "vram_used_mb": 1000, "vram_total_mb": 16000,
                "temp_c": 45.0, "fan_percent": 30.0, "power_w": 100.0,
                "pcie_gen": 5.0, "pcie_width": 16.0},
        "temperatures": {"cpu_c": None, "gpu_c": 45.0},
        "disks": [{"volume": "C:", "total_gb": 900, "free_gb": 400.0,
                   "used_percent": 50.0, "free_percent": 50.0,
                   "read_bps": 1024.0, "write_bps": None}],
        "network": network or [],
        "drives": drives or [],
        "battery": battery,
        "warnings": [],
    }


def test_tab_applies_cores_network_drives_battery(qapp):
    tab = _tab(qapp)
    sample = _fake_sample_v2(
        per_core=[10.0, 90.0, 40.0],
        network=[{"name": "Wi-Fi 7", "rx_bps": 2048.0, "tx_bps": 512.0,
                  "link_mbps": 865}],
        drives=[{"index": 0, "name": "Samsung 970", "kind": "nvme",
                 "temp_c": 41.0, "used_percent": 5, "health": "ok"},
                {"index": 1, "name": "Old SSD", "kind": "sata",
                 "temp_c": None, "health": "fail"}],
        battery={"percent": 42, "charging": True, "ac": True})
    tab._apply_sample((sample, None))
    qapp.processEvents()
    assert tab._core_strip._values == [10.0, 90.0, 40.0]
    assert tab._battery_chip.text().startswith("Battery")
    assert "Wi-Fi 7" in tab._net_rows["Wi-Fi 7"][0].text()
    assert "2 KB/s" in tab._net_rows["Wi-Fi 7"][1].text()
    assert "Samsung 970" in tab._drv_rows[0].text()
    assert "#ff3366" in tab._drv_rows[1].styleSheet()
    assert "#ffffff" in tab._drv_rows[0].styleSheet()
    tab.shutdown()


def test_tab_hides_battery_and_cores_when_absent(qapp):
    tab = _tab(qapp)
    tab._apply_sample((_fake_sample_v2(), None))
    qapp.processEvents()
    assert not tab._battery_chip.isVisibleTo(tab)
    assert not tab._core_strip.isVisibleTo(tab)
    assert tab._net_rows == {}
    assert tab._drv_rows == {}
    tab.shutdown()



# ------------------------------------------- laptop-crash hardening (8.8)

def test_dxgi_adapters_ttl_cached(monkeypatch):
    """Laptop crash investigation 2026-11-19 (AMD 890M): the health tab's
    GPU fallback walked the DXGI factory EVERY tick on machines without
    nvidia-smi — driving the AMD overlay's dxgi hook twice a second, the
    prime suspect in the tab-correlated python311.dll+0x49245 AVs. The
    static adapter list must be walked ONCE per TTL window; empty results
    stay uncached so transient failures still retry."""
    monkeypatch.setattr(hm, "dxgi_adapters", _REAL_DXGI)
    calls = {"n": 0}

    def fake_walk():
        calls["n"] += 1
        return [("AMD Radeon(TM) 890M Graphics", 8192)]

    monkeypatch.setattr(hm, "_dxgi_walk", fake_walk)
    hm._dxgi_cache, hm._dxgi_stamp = None, 0.0
    first = hm.dxgi_adapters()
    assert hm.dxgi_adapters() == first
    hm.dxgi_adapters()
    assert calls["n"] == 1                       # one walk per window
    hm._dxgi_stamp = time.monotonic() - 601.0    # TTL expiry
    assert hm.dxgi_adapters() == first
    assert calls["n"] == 2

    monkeypatch.setattr(hm, "_dxgi_walk", lambda: [])
    hm._dxgi_cache, hm._dxgi_stamp = None, 0.0
    calls["n"] = 0

    def counting_empty():
        calls["n"] += 1
        return []

    monkeypatch.setattr(hm, "_dxgi_walk", counting_empty)
    hm.dxgi_adapters()
    hm.dxgi_adapters()
    assert calls["n"] == 2                       # empty: retried, not cached


# ------------------------------------------------ step-by-step diagnosis

def test_health_steps_merge_is_the_snapshot(monkeypatch):
    """Single source of truth: running every health_steps() reader into a
    base + finalize must produce exactly snapshot()'s key set — the
    diagnostic path can never drift from the real one."""
    snap = sh.snapshot()
    manual = sh.snapshot_base()
    for _key, fn in sh.health_steps():
        fn(manual, False)
    sh.snapshot_finalize(manual)
    assert sorted(manual.keys()) == sorted(snap.keys())
    assert "per_core" in manual["cpu"] and "temperatures" in manual


def test_step_log_crash_safe_lines(tmp_path, monkeypatch):
    log = tmp_path / "steps.log"
    monkeypatch.setattr(sh, "_STEPS_LOG_PATH", str(log))
    sh.step_log("cycle 1 BEGIN gpu")
    sh.step_log("cycle 1 OK gpu 12 ms")
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("cycle 1 BEGIN gpu")
    assert lines[1].endswith("cycle 1 OK gpu 12 ms")


def test_dxgi_force_bypasses_the_cache(monkeypatch):
    monkeypatch.setattr(hm, "dxgi_adapters", _REAL_DXGI)
    calls = {"n": 0}

    def fake_walk():
        calls["n"] += 1
        return [("GPU", 1024)]

    monkeypatch.setattr(hm, "_dxgi_walk", fake_walk)
    hm._dxgi_cache, hm._dxgi_stamp = None, 0.0
    hm.dxgi_adapters()                    # warms the cache
    hm.dxgi_adapters()                    # cached
    assert calls["n"] == 1
    hm.dxgi_adapters(force=True)          # diagnosis mode: raw walk again
    assert calls["n"] == 2


def test_tab_step_mode_runs_reads_and_logs(qapp, monkeypatch, tmp_path):
    """The owner's diagnosis flow: toggle Step-by-step → each reading runs
    alone, in order, with a visible state and a crash-safe log line BEFORE
    every attempt. A raising reader marks ✗ and the cycle continues."""
    import gui.system_health_tab as sht
    log_path = tmp_path / "steps.log"
    monkeypatch.setattr(sh, "_STEPS_LOG_PATH", str(log_path))
    monkeypatch.setattr(sht, "_STEP_DWELL", 0.0)
    order = []

    def fake_steps():
        def ok_reader(snap, force):
            order.append(("ok_reader", force))
            snap["cpu"] = {"percent": 5.0, "cores": 4, "per_core": []}

        def boom(snap, force):
            order.append(("boom", force))
            raise RuntimeError("native death")

        def battery_reader(snap, force):
            order.append(("battery", force))
            snap["battery"] = {"percent": 50, "charging": False, "ac": True}

        return [("cpu_total", ok_reader), ("bad", boom),
                ("battery", battery_reader)]

    monkeypatch.setattr(sh, "health_steps", fake_steps)
    tab = _tab(qapp)
    tab._step_btn.click()                 # toggle ON (not active: no kick)
    assert tab._step_mode is True and tab._step_host.isVisibleTo(tab)
    snap = tab._run_steps_sequentially()
    qapp.processEvents()
    # All three ran in order despite the failure; force=True propagated.
    assert [name for name, _f in order] == ["ok_reader", "boom", "battery"]
    assert all(f is True for _n, f in order)
    # The assembled snapshot still carries the successful fragments.
    assert snap["cpu"]["percent"] == 5.0 and snap["battery"]["percent"] == 50
    assert "warnings" in snap
    # Log lines: BEGIN before every attempt, FAIL for the raiser.
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert any(l.endswith("BEGIN bad") for l in lines)
    assert any(" FAIL bad " in l for l in lines)   # line carries the ms too
    assert any(" OK battery " in l for l in lines)
    # Rows: ✓ / ✗ / ✓ states with the step labels.
    assert tab._step_rows["bad"].text().startswith("\u2717")
    assert tab._step_rows["battery"].text().startswith("\u2713")
    tab.shutdown()


def test_tab_step_mode_off_is_invisible(qapp):
    tab = _tab(qapp)
    qapp.processEvents()
    assert tab._step_mode is False
    assert not tab._step_host.isVisibleTo(tab)
    tab.shutdown()
