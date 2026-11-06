"""infra/gpu_preference.py — per-exe Windows graphics preference that pins
the built-in Chromium browser to the dedicated GPU on hybrid machines
(owner decision 2026-11-06: browser flashes when Windows switches
adapters)."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest import mock

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


def test_apply_force_writes_high_performance_preference(monkeypatch):
    fake, calls = _fake_winreg()
    monkeypatch.setattr(gpu_preference.os, "name", "nt")
    monkeypatch.setattr(gpu_preference, "_exe_path",
                        lambda: "C:\\Programs\\DeepFlux\\DeepFlux.exe")
    monkeypatch.setattr(gpu_preference, "has_discrete_gpu", lambda: True)
    monkeypatch.setitem(sys.modules, "winreg", fake)

    result = gpu_preference.apply_force_dedicated_gpu(True)
    assert result == "C:\\Programs\\DeepFlux\\DeepFlux.exe"
    assert calls["set"] == [(
        r"Software\Microsoft\DirectX\UserGpuPreferences",
        "C:\\Programs\\DeepFlux\\DeepFlux.exe", "GpuPreference=2;")]


def test_apply_force_removes_preference_when_disabled(monkeypatch):
    fake, calls = _fake_winreg()
    monkeypatch.setattr(gpu_preference.os, "name", "nt")
    monkeypatch.setattr(gpu_preference, "_exe_path",
                        lambda: "C:\\Programs\\DeepFlux\\DeepFlux.exe")
    monkeypatch.setitem(sys.modules, "winreg", fake)

    assert gpu_preference.apply_force_dedicated_gpu(False) == "removed"
    assert calls["delete"] == [(
        r"Software\Microsoft\DirectX\UserGpuPreferences",
        "C:\\Programs\\DeepFlux\\DeepFlux.exe")]


def test_apply_force_noops_without_discrete_gpu(monkeypatch):
    fake, calls = _fake_winreg()
    monkeypatch.setattr(gpu_preference.os, "name", "nt")
    monkeypatch.setattr(gpu_preference, "_exe_path",
                        lambda: "C:\\Programs\\DeepFlux\\DeepFlux.exe")
    monkeypatch.setattr(gpu_preference, "has_discrete_gpu", lambda: False)
    monkeypatch.setitem(sys.modules, "winreg", fake)

    assert gpu_preference.apply_force_dedicated_gpu(True) == ""
    assert calls["set"] == []


def test_apply_force_noops_when_not_frozen_or_posix(monkeypatch):
    monkeypatch.setattr(gpu_preference.os, "name", "nt")
    monkeypatch.setattr(gpu_preference, "_exe_path", lambda: "")  # source run
    assert gpu_preference.apply_force_dedicated_gpu(True) == ""
    monkeypatch.setattr(gpu_preference.os, "name", "posix")
    assert gpu_preference.apply_force_dedicated_gpu(True) == ""
