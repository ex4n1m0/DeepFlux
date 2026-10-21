"""Hardware HUD + monitor tests (6.1).

infra/hardware_monitor.py is Qt-free and pure-parse-testable; the HUD
widget gets an offscreen construction + direct _apply_sample pass (the
sampler thread is never started in tests — set_active stays False and we
feed samples by hand). Dialog logic tests (auto-switch gate, fit badges,
endpoint-aware rows) live here too, next to the widget they drive.
"""
from __future__ import annotations

import os
import sys

import pytest

from infra import hardware_monitor as hm


# ---------------------------------------------------------------------------
# Parser / pure logic
# ---------------------------------------------------------------------------

def test_parse_smi_csv_single_and_multi_gpu():
    text = ("NVIDIA GeForce RTX 5080 Laptop GPU, 37, 8123, 16303\n"
            "NVIDIA GeForce RTX 3050, 12, 500, 4096\n")
    gpus = hm.parse_smi_csv(text)
    assert [g["name"] for g in gpus] == ["NVIDIA GeForce RTX 5080 Laptop GPU",
                                         "NVIDIA GeForce RTX 3050"]
    assert gpus[0]["util"] == 37.0
    assert gpus[0]["mem_used_mb"] == 8123
    assert gpus[0]["mem_total_mb"] == 16303


def test_parse_smi_csv_tolerates_unsupported_util_and_garbage():
    gpus = hm.parse_smi_csv(
        "Some GPU, [Not Supported], 100, 512\n"
        "garbage line without commas\n"
        "Bad, x, y, z\n")
    assert len(gpus) == 1
    assert gpus[0]["util"] is None
    assert gpus[0]["mem_used_mb"] == 100


def test_parse_smi_csv_empty_and_none():
    assert hm.parse_smi_csv("") == []
    assert hm.parse_smi_csv(None) == []


def test_sample_percent_properties():
    s = hm.Sample(ram_used_mb=1000, ram_total_mb=4000,
                  vram_used_mb=9000, vram_total_mb=10000)
    assert s.ram_percent == pytest.approx(25.0)
    assert s.vram_percent == pytest.approx(90.0)
    assert hm.Sample().ram_percent is None
    capped = hm.Sample(ram_used_mb=5000, ram_total_mb=4000)
    assert capped.ram_percent == 100.0  # clamped, never >100


def test_cpu_percent_needs_two_readings(monkeypatch):
    monkeypatch.setattr(hm, "_cpu_times", lambda: None)
    hm._cpu_last = None
    assert hm.cpu_percent() == 0.0
    readings = iter([(100, 100), (200, 300)])
    monkeypatch.setattr(hm, "_cpu_times", lambda: next(readings))
    hm._cpu_last = None
    assert hm.cpu_percent() == 0.0          # baseline reading only
    assert hm.cpu_percent() == pytest.approx(66.666, abs=0.1)


def test_smi_failure_backoff(monkeypatch):
    """A failed query must turn the sampler off for the retry window — a
    missing binary must not spawn a doomed process every poll."""
    monkeypatch.setattr(hm, "_find_smi", lambda: "C:/definitely/missing/smi.exe")
    hm._smi_path = None
    hm._smi_dead_until = 0.0
    calls = []
    import subprocess as sp

    def fake_run(*a, **k):
        calls.append(a)
        raise sp.TimeoutExpired(a[0], 1)
    monkeypatch.setattr(sp, "run", fake_run)
    try:
        assert hm.nvidia_smi_query() == []
        assert calls, "the first query should have been attempted"
        assert hm.nvidia_smi_query() == []
        assert len(calls) == 1, "backoff must suppress the second spawn"
    finally:
        hm._smi_path = None
        hm._smi_dead_until = 0.0


def test_dxgi_posix_returns_empty(monkeypatch):
    monkeypatch.setattr(hm.os, "name", "posix")
    assert hm.dxgi_adapters() == []


def test_sample_uses_static_gpu_when_smi_absent(monkeypatch):
    monkeypatch.setattr(hm, "nvidia_smi_query", lambda: [])
    monkeypatch.setattr(hm, "cpu_percent", lambda: 11.0)
    s = hm.sample(static_gpu=("AMD Radeon RX", 8192))
    assert s.gpu_name == "AMD Radeon RX"
    assert s.vram_total_mb == 8192
    assert s.gpu_live is False
    assert s.gpu_percent is None
    assert s.cpu_percent == 11.0


def test_sample_prefers_live_smi(monkeypatch):
    monkeypatch.setattr(hm, "nvidia_smi_query", lambda: [
        {"name": "NVIDIA GPU", "util": 42.0, "mem_used_mb": 1000,
         "mem_total_mb": 8000}])
    s = hm.sample(static_gpu=("whatever", 1))
    assert s.gpu_live is True
    assert s.gpu_name == "NVIDIA GPU"
    assert s.gpu_percent == 42.0


# ---------------------------------------------------------------------------
# Widget + dialog (offscreen)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_hud_meters_apply_sample(qapp):
    from config import DeeptorrentConfig
    from gui.hardware_hud import HardwareHud

    hud = HardwareHud(DeeptorrentConfig())
    try:
        hud._apply_sample(hm.Sample(
            cpu_percent=42.0, ram_used_mb=16000, ram_total_mb=32000,
            gpu_name="NVIDIA GeForce RTX 5080 Laptop GPU",
            gpu_percent=77.0, vram_used_mb=8000, vram_total_mb=16000,
            gpu_live=True))
        assert hud.cpu_meter._value == "42%"
        assert hud.ram_meter._value == "15.6/31.2 GB"
        assert hud.gpu_meter._value == "77%"
        assert hud.vram_meter._value == "7.8/15.6 GB"
        assert not hud.gpu_meter.isHidden()

        # No GPU at all -> both GPU meters hide, others keep working.
        hud._apply_sample(hm.Sample(cpu_percent=1.0, ram_used_mb=1,
                                    ram_total_mb=2))
        assert hud.gpu_meter.isHidden()
        assert hud.vram_meter.isHidden()

        # Static-only GPU (no nvidia-smi) -> visible, no utilization.
        hud._apply_sample(hm.Sample(
            cpu_percent=1.0, ram_used_mb=1, ram_total_mb=2,
            gpu_name="AMD Radeon RX", vram_total_mb=8192))
        assert not hud.gpu_meter.isHidden()
        assert hud.gpu_meter._percent is None
        assert hud.vram_meter._value == "0/8.0 GB"
    finally:
        hud.shutdown()


def test_hud_chip_states(qapp):
    from config import DeeptorrentConfig
    from gui.hardware_hud import HardwareHud
    import infra.local_model as lm

    cfg = DeeptorrentConfig()
    cfg.llm.provider = "deepseek"
    hud = HardwareHud(cfg)
    try:
        hud._apply_sample(hm.Sample())
        assert "Cloud AI" in hud.ai_chip.text()

        cfg.llm.provider = "local"
        cfg.llm.model = "qwen35-9b-uncensored-q4km"
        hud._server_ok = None
        lm._write_state({})           # no pid -> down state
        hud._refresh_ai_chip()
        assert "○" in hud.ai_chip.text()

        lm._write_state({"pid": 123, "model_id": "x"})
        hud._refresh_ai_chip()
        assert "◐" in hud.ai_chip.text()   # starting

        hud._server_ok = True
        hud._refresh_ai_chip()
        assert "●" in hud.ai_chip.text()   # running
        assert "Qwen3.5 9B Uncensored" in hud.ai_chip.text()
    finally:
        hud.shutdown()
        try:
            os.remove(lm._state_path())
        except OSError:
            pass


def test_hud_open_keys_signal(qapp):
    from config import DeeptorrentConfig
    from gui.hardware_hud import HardwareHud
    hud = HardwareHud(DeeptorrentConfig())
    try:
        seen = []
        hud.open_keys_requested.connect(lambda: seen.append(1))
        hud.ai_chip.click()
        assert seen == [1]
    finally:
        hud.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="local models are win32")
def test_api_keys_dialog_local_ux(qapp, monkeypatch, tmp_path):
    """Endpoint-aware rows, fit badges, the Use button, and the
    download-never-switches-provider rule (6.4.1)."""
    from config import DeeptorrentConfig
    from gui.settings_dialog import APIKeysDialog
    from PySide6.QtWidgets import QLineEdit
    import infra.local_model as lm

    monkeypatch.setattr(lm, "detect_hardware", lambda refresh=False: None)

    cfg = DeeptorrentConfig()
    cfg.llm.provider = "deepseek"
    cfg.llm.api_key = "sk-shared-injected"

    dlg = APIKeysDialog(cfg, None, page="ai")
    try:
        # A completed download must NOT stage the local provider (6.4.1):
        # the DeepSeek default stands until the user switches explicitly.
        monkeypatch.setattr(lm, "is_installed", lambda e: True)
        entry = lm.entry_by_id("qwen35-9b-uncensored-q4km")
        dlg._on_local_dl_done(entry["id"], {"ok": True, "path": "x"})
        assert dlg._pending_local_entry is None
        assert "DeepSeek key stays" in dlg.local_test_result.text()
        assert dlg.llm_endpoint.currentData() == "deepseek"

        # Local endpoint: key row hides, base URL locks; the Model field
        # follows the endpoint (owner report 6.4.2 — never a stale id).
        dlg.llm_model.setText("qwen35-9b-uncensored-q4km")  # stale local id
        dlg.llm_endpoint.setCurrentIndex(dlg.llm_endpoint.findData("custom"))
        dlg.llm_endpoint.setCurrentIndex(dlg.llm_endpoint.findData("deepseek"))
        assert dlg.llm_model.text() == "deepseek-flash"
        dlg.llm_endpoint.setCurrentIndex(dlg.llm_endpoint.findData("local"))
        assert dlg.llm_key.isHidden()
        assert dlg.llm_base_url.isReadOnly()
        assert dlg.llm_model.text() == "qwen35-9b-uncensored-q4km"  # local preset default
        # Custom: everything editable again.
        dlg.llm_endpoint.setCurrentIndex(dlg.llm_endpoint.findData("custom"))
        assert not dlg.llm_key.isHidden()
        assert not dlg.llm_base_url.isReadOnly()

        # Reveal toggle round-trip.
        dlg._key_reveal_action.trigger()
        assert dlg.llm_key.echoMode() == QLineEdit.EchoMode.Normal
        dlg._key_reveal_action.trigger()
        assert dlg.llm_key.echoMode() == QLineEdit.EchoMode.Password

        # Fit badges land with the hardware probe result.
        hw = lm.HardwareInfo(cpu_cores=24, ram_mb=32000,
                             gpu_name="NVIDIA GeForce RTX 5080 Laptop GPU",
                             vram_mb=16003)
        monkeypatch.setattr(lm, "is_installed", lambda e: False)
        dlg._on_local_hw(hw)
        _wrap, name, fit, status, use_btn, dl_btn, rm_btn = \
            dlg._local_rows["qwen35-9b-uncensored-q4km"]
        assert fit.text() == "GPU"
        assert "GiB" in status.text()
        assert not use_btn.isVisibleTo(dlg) or not use_btn.isEnabled()

        # Use button stages an installed model (the explicit switch path).
        monkeypatch.setattr(lm, "is_installed", lambda e: True)
        dlg._local_use(entry)
        assert dlg._pending_local_entry["id"] == entry["id"]
        assert dlg.llm_endpoint.currentData() == "local"
    finally:
        dlg.deleteLater()


def test_fit_badge_mapping(qapp):
    from config import DeeptorrentConfig
    from gui.settings_dialog import APIKeysDialog
    dlg = APIKeysDialog(DeeptorrentConfig(), None, page="subtitles")
    dlg.deleteLater()
    assert dlg._local_fit_badge("gpu_full")[0] == "GPU"
    assert dlg._local_fit_badge("gpu_partial")[0] == "GPU+CPU"
    assert dlg._local_fit_badge("ram_only")[0] == "CPU"
    assert "fit" in dlg._local_fit_badge(None)[0].lower()


@pytest.mark.skipif(sys.platform != "win32", reason="local models are win32")
def test_local_server_on_off_control(qapp, monkeypatch, tmp_path):
    """Start/Stop toggle states: off -> loading (button locked) -> running,
    with the progress bar and labels following the server state."""
    from config import DeeptorrentConfig
    from gui.settings_dialog import APIKeysDialog
    from gui.i18n import tr
    import infra.local_model as lm

    monkeypatch.setattr(lm, "detect_hardware", lambda refresh=False: None)
    monkeypatch.setattr(lm, "server_status", lambda: {"state": "off"})
    dlg = APIKeysDialog(DeeptorrentConfig(), None, page="ai")
    try:
        dlg._on_server_state({"state": "off"})
        assert dlg.server_toggle_btn.text() == tr("Start server")
        assert "Off" in dlg.server_state_lbl.text()
        assert dlg.server_progress.isHidden()

        # Mid-transition loading keeps the toggle locked (it is the cancel).
        dlg._server_transitioning = True
        dlg._on_server_state({"state": "loading", "elapsed": 7.0})
        assert dlg.server_toggle_btn.text() == tr("Cancel / stop")
        assert not dlg.server_toggle_btn.isEnabled()
        assert "Loading model" in dlg.server_state_lbl.text()
        assert not dlg.server_progress.isHidden()

        # Terminal state re-enables even while the transition flag is set.
        dlg._on_server_state({"state": "running",
                              "model_id": "qwen35-9b-uncensored-q4km"})
        assert dlg.server_toggle_btn.text() == tr("Stop server")
        assert dlg.server_toggle_btn.isEnabled()
        assert "Running" in dlg.server_state_lbl.text()
        assert dlg.server_progress.isHidden()

        # Error terminal states land back on Start.
        dlg._on_server_state({"state": "off", "error": "No model installed — download one first."})
        assert dlg.server_toggle_btn.text() == tr("Start server")
        assert "No model" in dlg.server_state_lbl.text()
    finally:
        dlg.deleteLater()
