"""Agent-tab hardware HUD — live CPU / RAM / GPU / VRAM strip (6.1).

ModelSwarm-style resource strip, always visible at the top of the Agent
page: watching real utilization while a local model runs is exactly the
evidence needed to pick a quant the machine can handle (a GPU pegged at
100% with VRAM exhausted says "smaller model", spiky CPU-only decode says
"use the GPU tier"). Sampling lives in Qt-free ``infra/hardware_monitor.py``
(GetSystemTimes / GlobalMemoryStatusEx / nvidia-smi — no new dependency);
this widget only renders and owns ONE persistent sampler thread that
sleeps while the Agent tab is not active.

The rightmost chip is the local-AI status: provider → model → live server
state, and clicking it opens File → API Keys → AI (the model picker).
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton

from gui.i18n import tr

from infra import hardware_monitor
from infra import local_model

POLL_SECONDS = 2.0
SERVER_CHECK_EVERY = 3          # health probes go out every Nth tick
HEALTH_TIMEOUT = 0.8            # sub-second so one slow probe can't stack

_ACCENT = QColor("#2a7abf")
_WARN = QColor("#d29922")
_DANGER = QColor("#f85149")
_TEXT_DIM = QColor("#8a9ab0")
_TEXT = QColor("#e8eefc")
_OK = QColor("#3fb950")
_BAR_BACK = QColor("#111827")
_BAR_EDGE = QColor("#1a2a4a")


def _level_color(percent):
    if percent is None:
        return _ACCENT
    if percent >= 90.0:
        return _DANGER
    if percent >= 75.0:
        return _WARN
    return _ACCENT


class HudMeter(QFrame):
    """One metric: caption + value on the first line, a 3px bar under it.

    Custom paint (not QProgressBar) so the strip stays visually identical
    under every global QSS the app has shipped — the meters must look like
    the console UI, not like a form widget.
    """

    def __init__(self, caption: str, parent=None) -> None:
        super().__init__(parent)
        self._caption = caption
        self._value = "—"
        self._percent = None          # None = bar hidden (unknown)
        self._tooltip = ""
        self.setFixedSize(112, 30)
        self.setToolTip(caption)

    def set_value(self, value: str, percent, tooltip: str = "") -> None:
        self._value = value
        self._percent = percent
        self._tooltip = tooltip
        tip = tooltip or self._caption
        if tip != self.toolTip():
            self.setToolTip(tip)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.width()

        font = QFont(self.font())
        font.setPointSizeF(8.5)
        painter.setFont(font)
        metrics = painter.fontMetrics()

        painter.setPen(QPen(_TEXT_DIM))
        painter.drawText(2, 2, w - 4, metrics.height(), Qt.AlignLeft, self._caption)

        color = _level_color(self._percent)
        painter.setPen(QPen(color))
        painter.drawText(2, 2, w - 4, metrics.height(), Qt.AlignRight, self._value)

        if self._percent is None:
            return
        bar_y = self.height() - 6
        painter.setPen(QPen(_BAR_EDGE))
        painter.setBrush(_BAR_BACK)
        painter.drawRoundedRect(2, bar_y, w - 4, 4, 2, 2)
        fill_w = max(3, int((w - 4) * min(1.0, max(0.0, self._percent / 100.0))))
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(2, bar_y, fill_w, 4, 2, 2)


def _fmt_gb(mb: int) -> str:
    return f"{mb / 1024:.1f}" if mb >= 1024 else f"{mb}"


class HardwareHud(QFrame):
    """The strip: CPU / RAM / GPU / VRAM meters + local-AI status chip."""

    updated = Signal(object)          # hardware_monitor.Sample
    open_keys_requested = Signal()    # click on the AI chip → API Keys → AI

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("hw_hud")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "#hw_hud { background-color: #0a0a0f; border: none;"
            " border-bottom: 2px solid #1a2a4a; }")

        self._config = config
        # Sampling is OFF until MainWindow activates the HUD after the UI is
        # fully built — the first tick spawns subprocesses (nvidia-smi, the
        # device probe) that must not race the construction of the window.
        self._active = False
        self._stop = threading.Event()
        self._tick = 0
        self._server_ok = None         # None = not checked yet
        self._labels = {e["id"]: e.get("label", e["id"])
                        for e in local_model.load_manifest()}

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 3, 10, 2)
        row.setSpacing(14)

        self.cpu_meter = HudMeter(tr("CPU"))
        self.ram_meter = HudMeter(tr("RAM"))
        self.gpu_meter = HudMeter(tr("GPU"))
        self.vram_meter = HudMeter(tr("VRAM"))
        for meter in (self.cpu_meter, self.ram_meter, self.gpu_meter,
                      self.vram_meter):
            row.addWidget(meter)

        row.addStretch(1)

        self.ai_chip = QPushButton("…")
        self.ai_chip.setObjectName("status_chip")
        self.ai_chip.setCursor(Qt.PointingHandCursor)
        self.ai_chip.setToolTip(tr(
            "Local AI status — click to open the model manager"))
        self.ai_chip.clicked.connect(self.open_keys_requested.emit)
        row.addWidget(self.ai_chip)

        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="hardware-hud")
        self._thread.start()
        self.updated.connect(self._apply_sample)

    # -- lifecycle -----------------------------------------------------------

    def set_active(self, active: bool) -> None:
        """Sampling only runs while the Agent page is visible (MainWindow
        toggles this on tab changes)."""
        self._active = bool(active)

    def shutdown(self) -> None:
        self._stop.set()

    # -- sampler thread (Qt-free) --------------------------------------------

    def _loop(self) -> None:
        hardware_monitor.seed_cpu_baseline()
        static_gpu = None
        while not self._stop.is_set():
            try:
                if self._active:
                    if static_gpu is None:
                        hw = local_model.detect_hardware()
                        static_gpu = (hw.gpu_name, hw.vram_mb) if hw.gpu_name else False
                    sample = hardware_monitor.sample(
                        static_gpu=static_gpu or None)
                    if self._tick % SERVER_CHECK_EVERY == 0:
                        self._server_ok = local_model.is_running(
                            timeout=HEALTH_TIMEOUT)
                    self._tick += 1
                    self.updated.emit(sample)
            except Exception:
                pass  # a HUD must never take the app down with it
            self._stop.wait(POLL_SECONDS)

    # -- rendering -------------------------------------------------------------

    def _apply_sample(self, sample) -> None:
        self.cpu_meter.set_value(
            f"{sample.cpu_percent:.0f}%", sample.cpu_percent,
            tr("Processor load (all cores)"))

        if sample.ram_total_mb:
            self.ram_meter.set_value(
                f"{_fmt_gb(sample.ram_used_mb)}/{_fmt_gb(sample.ram_total_mb)} GB",
                sample.ram_percent,
                tr("System memory"))
            self.ram_meter.setFixedWidth(126)
        else:
            self.ram_meter.set_value("—", None)

        if sample.gpu_name:
            # Re-show in case a previous sample had no GPU (hotplug /
            # probe timing) — hide() must always be paired with show().
            self.gpu_meter.show()
            self.vram_meter.show()
            if sample.gpu_live:
                self.gpu_meter.set_value(
                    "—" if sample.gpu_percent is None
                    else f"{sample.gpu_percent:.0f}%",
                    sample.gpu_percent,
                    sample.gpu_name)
            else:
                self.gpu_meter.set_value(
                    tr("n/a"), None,
                    sample.gpu_name + "\n" + tr(
                        "Live GPU stats need the NVIDIA driver (nvidia-smi)."))
            if sample.vram_total_mb:
                self.vram_meter.set_value(
                    f"{_fmt_gb(sample.vram_used_mb)}/{_fmt_gb(sample.vram_total_mb)} GB",
                    sample.vram_percent,
                    tr("GPU memory") + " — " + sample.gpu_name)
                self.vram_meter.setFixedWidth(126)
            else:
                self.vram_meter.set_value("—", None)
        else:
            self.gpu_meter.hide()
            self.vram_meter.hide()

        self._refresh_ai_chip()

    def _refresh_ai_chip(self) -> None:
        try:
            provider = getattr(self._config.llm, "provider", "")
        except Exception:
            provider = ""
        if provider != "local":
            self.ai_chip.setText(tr("☁ Cloud AI"))
            self.ai_chip.setToolTip(tr(
                "The agent runs on a cloud provider — click to open the AI "
                "settings (local models run on this PC instead)"))
            return
        model_id = getattr(self._config.llm, "model", "")
        label = self._labels.get(model_id) or model_id or "local model"
        short = label.split(" — ")[0]      # "Qwen3.5 9B Uncensored — Q4_K_M
        short = short.split(" (")[0]       #  (5.2 GiB)" → "Qwen3.5 9B Uncensored"
        state = local_model._state()
        if self._server_ok:
            self.ai_chip.setText(f"🧠 {short} · ●")
            self.ai_chip.setToolTip(tr("Local AI running") + f"\n{label}")
        elif state.get("pid"):
            self.ai_chip.setText(f"🧠 {short} · ◐")
            self.ai_chip.setToolTip(
                tr("Local AI starting (a GPU model load can take up to a "
                   "minute)") + f"\n{label}")
        else:
            self.ai_chip.setText(f"🧠 {short} · ○")
            self.ai_chip.setToolTip(
                tr("Local AI server is down — it starts automatically on "
                   "the next message") + f"\n{label}")
