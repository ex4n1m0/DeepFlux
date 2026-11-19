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
    monkeypatch.setattr(sh.hardware_monitor, "ram_stats",
                        lambda: (8000, 32000))
    monkeypatch.setattr(sh.hardware_monitor, "pagefile_stats",
                        lambda: (1000, 40000))
    monkeypatch.setattr(sh.hardware_monitor, "nvidia_smi_query", lambda: [])
    monkeypatch.setattr(sh.hardware_monitor, "dxgi_adapters", lambda: [])
    monkeypatch.setattr(sh, "_fixed_volumes", lambda: ["C:", "D:"])
    monkeypatch.setattr(sh, "read_volume_counters", _fake_counters)
    monkeypatch.setattr(sh, "_run_powershell", _no_powershell)
    _FAKE_IO.clear()
    sh._disk_last.clear()
    sh._disk_dead.clear()
    saved = (sh._thermal_value, sh._thermal_stamp,
             sh._inv_cache, sh._inv_stamp, sh._inv_failed)
    sh._thermal_value, sh._thermal_stamp = None, 0.0
    sh._inv_cache, sh._inv_stamp, sh._inv_failed = None, 0.0, False
    yield
    _FAKE_IO.clear()
    sh._disk_last.clear()
    sh._disk_dead.clear()
    (sh._thermal_value, sh._thermal_stamp,
     sh._inv_cache, sh._inv_stamp, sh._inv_failed) = saved


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
                            lambda: [("Intel Arc A770", 16384)])
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
        assert not isinstance(node, (ast.Import, ast.ImportFrom)),             f"lazy import found in warnings_from: line {node.lineno}"
