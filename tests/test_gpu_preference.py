"""infra/gpu_preference.py — per-exe Windows graphics preference.

Three-state (7.6, owner decision 2026-11-07): "integrated" (power saving,
DEFAULT — writes GpuPreference=1 with NO adapter detection), "dedicated"
(writes 2 only when a discrete adapter exists), anything else ("auto" =
remove the value so Windows decides). Legacy migration of the 7.4–7.5
force_dedicated_gpu boolean lives in config.py (_gpu_pref_of) and is
pinned in test_config_migrations_gpu_preference below."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from infra import gpu_preference


def test_is_integrated_matches_known_shapes():
    assert gpu_preference._is_integrated("Intel(R) UHD Graphics")
    assert gpu_preference._is_integrated("AMD Radeon(TM) Graphics")
    assert gpu_preference._is_integrated("AMD Radeon Vega 8 Graphics")
    assert not gpu_preference._is_integrated("NVIDIA GeForce RTX 5080 Laptop GPU")
    assert not gpu_preference._is_integrated("AMD Radeon RX 7900 XTX")


def test_has_discrete_gpu_true_and_false(monkeypatch):
    monkeypatch.setattr(
        gpu_preference.hardware_monitor, "dxgi_adapters",
        lambda: [("Intel(R) UHD Graphics", 512),
                 ("NVIDIA GeForce RTX 5080 Laptop GPU", 16384)])
    assert gpu_preference.has_discrete_gpu() is True
    monkeypatch.setattr(
        gpu_preference.hardware_monitor, "dxgi_adapters",
        lambda: [("Intel(R) UHD Graphics", 512)])
    assert gpu_preference.has_discrete_gpu() is False
    monkeypatch.setattr(gpu_preference.hardware_monitor, "dxgi_adapters", lambda: [])
    assert gpu_preference.has_discrete_gpu() is False


def _fake_winreg():
    calls = {"set": [], "delete": []}

    class _Key:
        def __init__(self, hkey, path):
            self._hkey, self._path = hkey, path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def create_key(hkey, path):
        return _Key(hkey, path)

    def set_value_ex(key, name, _reserved, _type, data):
        calls["set"].append((key._path, name, data))

    def delete_value(key, name):
        calls["delete"].append((key._path, name))

    fake = SimpleNamespace(
        HKEY_CURRENT_USER=object(), REG_SZ=1,
        CreateKey=create_key, SetValueEx=set_value_ex,
        DeleteValue=delete_value, FileNotFoundError=FileNotFoundError,
    )
    return fake, calls


def _armed(monkeypatch, exe="C:\\Programs\\DeepFlux\\DeepFlux.exe"):
    fake, calls = _fake_winreg()
    monkeypatch.setattr(gpu_preference.os, "name", "nt")
    monkeypatch.setattr(gpu_preference, "_exe_path", lambda: exe)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return calls


def test_integrated_writes_power_saving_without_detection(monkeypatch):
    # The integrated path must NEVER enumerate adapters (DXGI disturbed
    # the heap on the dev machine — 2026-11 forensics): has_discrete_gpu
    # is poisoned and the write still happens.
    calls = _armed(monkeypatch)

    def _boom():
        raise AssertionError("integrated preference must not detect GPUs")

    monkeypatch.setattr(gpu_preference, "has_discrete_gpu", _boom)
    assert gpu_preference.apply_gpu_preference("integrated") == \
        "C:\\Programs\\DeepFlux\\DeepFlux.exe"
    assert calls["set"] == [(
        r"Software\Microsoft\DirectX\UserGpuPreferences",
        "C:\\Programs\\DeepFlux\\DeepFlux.exe", "GpuPreference=1;")]


def test_dedicated_writes_high_performance_when_dgpu_exists(monkeypatch):
    calls = _armed(monkeypatch)
    monkeypatch.setattr(gpu_preference, "has_discrete_gpu", lambda: True)
    assert gpu_preference.apply_gpu_preference("dedicated") == \
        "C:\\Programs\\DeepFlux\\DeepFlux.exe"
    assert calls["set"] == [(
        r"Software\Microsoft\DirectX\UserGpuPreferences",
        "C:\\Programs\\DeepFlux\\DeepFlux.exe", "GpuPreference=2;")]


def test_dedicated_noops_without_discrete_gpu(monkeypatch):
    calls = _armed(monkeypatch)
    monkeypatch.setattr(gpu_preference, "has_discrete_gpu", lambda: False)
    assert gpu_preference.apply_gpu_preference("dedicated") == "no-dgpu"
    assert calls["set"] == []


def test_auto_removes_preference(monkeypatch):
    calls = _armed(monkeypatch)
    assert gpu_preference.apply_gpu_preference("auto") == "removed"
    assert calls["delete"] == [(
        r"Software\Microsoft\DirectX\UserGpuPreferences",
        "C:\\Programs\\DeepFlux\\DeepFlux.exe")]


def test_noops_when_not_frozen_or_posix(monkeypatch):
    monkeypatch.setattr(gpu_preference.os, "name", "nt")
    monkeypatch.setattr(gpu_preference, "_exe_path", lambda: "")  # source run
    assert gpu_preference.apply_gpu_preference("integrated") == ""
    monkeypatch.setattr(gpu_preference.os, "name", "posix")
    assert gpu_preference.apply_gpu_preference("integrated") == ""


def test_config_migrations_gpu_preference(tmp_path):
    # New key wins; the legacy boolean migrates True→"dedicated",
    # False→"auto"; a fresh config gets the 7.6 default "integrated".
    import json
    from config import DeeptorrentConfig

    def _cfg(browser: dict):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"browser": browser}), encoding="utf-8")
        return DeeptorrentConfig.from_file(str(p))

    assert _cfg({"gpu_preference": "dedicated"}).browser.gpu_preference == "dedicated"
    assert _cfg({"force_dedicated_gpu": True}).browser.gpu_preference == "dedicated"
    assert _cfg({"force_dedicated_gpu": False}).browser.gpu_preference == "auto"
    assert _cfg({}).browser.gpu_preference == "integrated"  # fresh default
    assert _cfg({"gpu_preference": "garbage"}).browser.gpu_preference == "integrated"
    # Default on the dataclass itself:
    assert DeeptorrentConfig().browser.gpu_preference == "integrated"
