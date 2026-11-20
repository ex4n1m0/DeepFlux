"""System Health tab — live hardware readout with STRICT visibility gating.

The successor to the removed 7.6 hardware HUD, under its documented
contract (owner 2026-11-18: "only does readings when that tab is selected,
not always"): the sampling QTimer starts when MainWindow reports this tab
became current and stops on every switch away, on hide and on shutdown.
There is NO always-on thread; each tick spawns one short-lived collector
thread (skipped if the previous one is still running) that gathers the
Qt-free snapshot in infra/system_health.py and hands it back through a
queued signal — the exact marshal pattern the mini player uses for mpv
events.

Visuals follow the app ladder (#05070c base, #080b12 panels, #0b1120
cards, #1a2a4a borders, #2a7abf accent), painted text follows the theme
via fonts.text_color(), every stylesheet goes through scale_qss.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from config import DeeptorrentConfig
from gui.fonts import scale_qss, text_color
from gui.i18n import tr
from gui.responsive import shrink_label
from infra import hardware_monitor, system_health

logger = logging.getLogger(__name__)

# Meter fill thresholds (percent) — same breakpoints the old HUD used.
_WARN_AT = 75.0
_DANGER_AT = 90.0
_WARN_HEX = "#e67e22"     # the app's warning orange
_DANGER_HEX = "#ff3366"   # the app's error red (downloads _STATUS_COLORS)
_OK_HEX = "#2a7abf"       # accent blue
_TRACK_HEX = "#1a2a4a"    # empty bar / borders

# Step-by-step diagnosis (owner idea 2026-11-19): one reading at a time,
# each attempt logged crash-safe BEFORE it runs — if the app dies, the
# last BEGIN line in ~/.deeptorrent/logs/health_steps.log names the
# culprit reading. Dwell between steps makes the sequence watchable.
_STEP_DWELL = 2.0
_STEP_READING_HEX = _OK_HEX
_STEP_ERROR_HEX = _DANGER_HEX

_STEP_LABELS = {
    "cpu_total": ("CPU total"),
    "cpu_per_core": ("CPU per-core"),
    "ram": ("Memory"),
    "gpu": ("GPU (nvidia-smi / adapter list)"),
    "disks": ("Disks (space + speeds)"),
    "network": ("Network adapters"),
    "drives": ("Drive health (NVMe / SMART)"),
    "cpu_temp": ("CPU temperature (ACPI)"),
    "battery": ("Battery"),
}


def _elide(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_bps(bps: Optional[float]) -> str:
    if bps is None:
        return "—"
    if bps >= 1024 ** 3:
        return f"{bps / 1024 ** 3:.1f} GB/s"
    if bps >= 1024 ** 2:
        return f"{bps / 1024 ** 2:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{max(0.0, bps):.0f} B/s"


def _fmt_uptime(seconds: Optional[int]) -> str:
    if not seconds or seconds < 0:
        return "—"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


class HealthMeter(QWidget):
    """Caption + value + thin threshold-colored bar, painted — the removed
    HUD's meter design, reimplemented for this tab. ``neutral`` meters
    (disk usage) always fill with the accent color: a nearly-full data
    disk is normal life, not a warning."""

    def __init__(self, caption: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._caption = caption
        self._value_text = "—"
        self._sub = ""
        self._frac: Optional[float] = None
        self._neutral = False
        self.setFixedHeight(58)
        self.setMinimumWidth(110)

    def set_reading(self, value_text: str, frac: Optional[float],
                    sub: str = "", neutral: bool = False) -> None:
        if (value_text, sub, frac, neutral) != \
                (self._value_text, self._sub, self._frac, self._neutral):
            self._value_text, self._sub = value_text, sub
            self._frac, self._neutral = frac, neutral
            self.update()

    def _fill_color(self) -> QColor:
        if self._neutral:
            return QColor(_OK_HEX)
        return QColor(_threshold_hex(self._frac))

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        try:
            rect = self.rect()
            base = self.font()
            cap_font = QFont(base)
            cap_font.setPointSizeF(max(6.5, base.pointSizeF() - 1.5))
            val_font = QFont(base)
            val_font.setBold(True)

            painter.setPen(text_color())
            painter.setFont(cap_font)
            painter.drawText(rect.adjusted(6, 4, -6, -14),
                             Qt.AlignLeft | Qt.AlignTop, self._caption)
            painter.setFont(val_font)
            painter.drawText(rect.adjusted(6, 4, -6, -14),
                             Qt.AlignRight | Qt.AlignTop, self._value_text)
            if self._sub:
                painter.setFont(cap_font)
                painter.drawText(rect.adjusted(6, 4, -6, -14),
                                 Qt.AlignLeft | Qt.AlignBottom,
                                 _elide(self._sub, 30))

            # The bar: full-width track + proportional fill, 4 px tall.
            track = QRectF(rect.adjusted(6, 0, -6, -5))
            track.setHeight(4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(_TRACK_HEX))
            painter.drawRoundedRect(track, 2, 2)
            if self._frac is not None and self._frac > 0.5:
                fill = QRectF(track)
                fill.setWidth(track.width() * min(100.0, self._frac) / 100.0)
                painter.setBrush(self._fill_color())
                painter.drawRoundedRect(fill, 2, 2)
        finally:
            painter.end()


def _threshold_hex(frac: Optional[float]) -> str:
    """Meter fill color for a 0–100 fraction — one place for the
    warn/danger breakpoints (HealthMeter and the per-core strip share)."""
    if frac is None:
        return _OK_HEX
    if frac >= _DANGER_AT:
        return _DANGER_HEX
    if frac >= _WARN_AT:
        return _WARN_HEX
    return _OK_HEX


class CoreStrip(QWidget):
    """Thin per-core activity strip under the meters — painted, so any
    core count (capped at 32 bars) renders without layout churn. Hidden
    entirely when the per-core source is unavailable (first tick of a
    cold process, or the NtQuery probe refused: absence, not fake 0%)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._values: List[float] = []
        self.setFixedHeight(26)
        self.setVisible(False)

    def set_values(self, values: List[Any]) -> None:
        vals = [min(100.0, max(0.0, float(v)))
                for v in values or [] if isinstance(v, (int, float))][:32]
        if vals != self._values:
            self._values = vals
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._values:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        try:
            rect = self.rect()
            n = len(self._values)
            gap = 2.0
            bar_w = max(3.0, (rect.width() - 12.0 - gap * (n - 1)) / n)
            painter.setPen(Qt.NoPen)
            for i, value in enumerate(self._values):
                frac = value / 100.0
                x = 6.0 + i * (bar_w + gap)
                track = QRectF(x, 8.0, bar_w, rect.height() - 10.0)
                painter.setBrush(QColor(_TRACK_HEX))
                painter.drawRoundedRect(track, 1, 1)
                if frac > 0.02:
                    fill = QRectF(track)
                    fill.setTop(track.bottom() - track.height() * frac)
                    painter.setBrush(QColor(_threshold_hex(value)))
                    painter.drawRoundedRect(fill, 1, 1)
        finally:
            painter.end()


def _chip(text: str = "—") -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(scale_qss(
        "QLabel { background-color: #0b1120; border: 1px solid #1a2a4a;"
        " border-radius: 4px; padding: 4px 12px; color: #ffffff;"
        " font-size: 14px; }"))
    return lbl


def _section(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(scale_qss(
        "QLabel { color: #ffffff; font-size: 15px; font-weight: 600;"
        " border-bottom: 1px solid #1a2a4a; padding-bottom: 2px; }"))
    return lbl


class SystemHealthTab(QWidget):
    """Hardware health surface: live meters (CPU/RAM/GPU/VRAM), GPU detail
    chips, per-volume disk rows with live throughput, temperatures and a
    hardware inventory tree. Sampling runs ONLY while the tab is the
    current tab (set_active) — see the module docstring."""

    # Emitted by the collector thread, queued onto the GUI thread.
    sampled = Signal(object)   # (snapshot dict, inventory dict | None)
    stepUpdate = Signal(object)  # (key, state, detail, cycle) — step mode

    def __init__(self, config: DeeptorrentConfig,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._active = False
        self._closing = False
        self._collector_busy = False
        self._want_inventory = True      # first activation fetches it
        self._force_inventory = False
        self._last_severity = ""         # banner restyle cache
        self._disk_rows: Dict[str, Tuple[HealthMeter, QLabel, QWidget]] = {}
        self._net_rows: Dict[str, Tuple[QLabel, QLabel, QWidget]] = {}
        self._drv_rows: Dict[int, QLabel] = {}

        self._timer = QTimer(self)
        self._timer.setInterval(max(
            1000, int(config.system_health.refresh_seconds * 1000)))
        self._timer.timeout.connect(self._on_tick)
        self.sampled.connect(self._apply_sample)
        self.stepUpdate.connect(self._on_step_update)

        self._step_mode = False
        self._step_cycle = 0

        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(10, 10, 10, 10)
        col.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(tr("System Health"))
        title.setStyleSheet(scale_qss(
            "QLabel { color: #ffffff; font-size: 20px; font-weight: 600; }"))
        head.addWidget(title)
        head.addStretch(1)
        self._updated_lbl = QLabel("")
        shrink_label(self._updated_lbl)
        self._updated_lbl.setStyleSheet(scale_qss(
            "QLabel { color: #ffffff; font-size: 13px; }"))
        head.addWidget(self._updated_lbl)
        self._refresh_btn = QPushButton(tr("↻ Refresh now"))
        self._refresh_btn.setObjectName("btn_accent")
        self._refresh_btn.clicked.connect(self._manual_refresh)
        head.addWidget(self._refresh_btn)
        self._step_btn = QPushButton(tr("Step-by-step"))
        self._step_btn.setCheckable(True)
        self._step_btn.clicked.connect(self._toggle_step_mode)
        head.addWidget(self._step_btn)
        col.addLayout(head)

        note = QLabel(tr("Live readings update only while this tab is open"))
        shrink_label(note)
        note.setStyleSheet(scale_qss(
            "QLabel { color: #ffffff; font-size: 12px; }"))
        col.addWidget(note)

        # Step-by-step diagnosis panel (hidden unless the mode is on).
        self._step_host = QWidget()
        self._step_lay = QVBoxLayout(self._step_host)
        self._step_lay.setContentsMargins(0, 0, 0, 0)
        self._step_lay.setSpacing(2)
        self._step_head_lbl = QLabel("")
        shrink_label(self._step_head_lbl)
        self._step_head_lbl.setStyleSheet(scale_qss(
            "QLabel { color: #ffffff; font-size: 12px; }"))
        self._step_lay.addWidget(self._step_head_lbl)
        self._step_rows: Dict[str, QLabel] = {}
        self._step_host.hide()
        col.addWidget(self._step_host)

        self._warn_lbl = QLabel("")
        self._warn_lbl.setWordWrap(True)
        shrink_label(self._warn_lbl)
        self._warn_lbl.hide()
        col.addWidget(self._warn_lbl)

        meters = QHBoxLayout()
        meters.setSpacing(6)
        self._cpu_m = HealthMeter(tr("CPU"))
        self._ram_m = HealthMeter(tr("Memory"))
        self._gpu_m = HealthMeter(tr("GPU"))
        self._vram_m = HealthMeter(tr("VRAM"))
        for meter in (self._cpu_m, self._ram_m, self._gpu_m, self._vram_m):
            meters.addWidget(meter, 1)
        col.addLayout(meters)

        self._core_strip = CoreStrip()
        col.addWidget(self._core_strip)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self._gpu_temp_chip = _chip()
        self._fan_chip = _chip()
        self._power_chip = _chip()
        self._pcie_chip = _chip()
        self._cpu_temp_chip = _chip()
        self._uptime_chip = _chip()
        self._battery_chip = _chip()
        for chip in (self._gpu_temp_chip, self._fan_chip, self._power_chip,
                     self._pcie_chip, self._cpu_temp_chip, self._uptime_chip,
                     self._battery_chip):
            chips.addWidget(chip)
        self._battery_chip.hide()
        chips.addStretch(1)
        col.addLayout(chips)

        col.addWidget(_section(tr("Disks")))
        self._disk_host = QWidget()
        self._disk_lay = QVBoxLayout(self._disk_host)
        self._disk_lay.setContentsMargins(0, 0, 0, 0)
        self._disk_lay.setSpacing(4)
        col.addWidget(self._disk_host)

        col.addWidget(_section(tr("Network")))
        self._net_host = QWidget()
        self._net_lay = QVBoxLayout(self._net_host)
        self._net_lay.setContentsMargins(0, 0, 0, 0)
        self._net_lay.setSpacing(4)
        col.addWidget(self._net_host)

        col.addWidget(_section(tr("Drive health")))
        self._drv_host = QWidget()
        self._drv_lay = QVBoxLayout(self._drv_host)
        self._drv_lay.setContentsMargins(0, 0, 0, 0)
        self._drv_lay.setSpacing(4)
        col.addWidget(self._drv_host)

        inv_head = QHBoxLayout()
        self._inv_toggle = QPushButton(tr("▸ Hardware inventory"))
        self._inv_toggle.clicked.connect(self._toggle_inventory)
        inv_head.addWidget(self._inv_toggle)
        inv_head.addStretch(1)
        col.addLayout(inv_head)

        self._inv_tree = QTreeWidget()
        self._inv_tree.setHeaderLabels([tr("Device"), tr("Details")])
        self._inv_tree.setColumnWidth(0, 230)
        self._inv_tree.header().setStretchLastSection(True)
        self._inv_tree.setStyleSheet(scale_qss(
            "QTreeWidget { background-color: #080b12;"
            " border: 1px solid #1a2a4a; color: #ffffff; font-size: 14px; }"
            "QHeaderView::section { background-color: #0b1120;"
            " color: #ffffff; border: 1px solid #1a2a4a; padding: 3px; }"))
        self._inv_tree.hide()
        col.addWidget(self._inv_tree)

        col.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)

    # ----------------------------------------------------------- sampling
    def set_active(self, active: bool) -> None:
        """MainWindow._on_main_tab_changed calls this on EVERY switch:
        sampling runs only while this tab is the current one (the owner
        contract — nothing samples at boot, in the background, or on any
        other tab)."""
        self._active = bool(active)
        if self._active and not self._closing:
            hardware_monitor.seed_cpu_baseline()
            if not self._timer.isActive():
                self._timer.start()
            self._kick_collector()
        else:
            self._timer.stop()

    def _on_tick(self) -> None:
        if not self.isVisible() or self._closing:
            return
        self._kick_collector()

    def _manual_refresh(self) -> None:
        """The on-demand path (button): also forces a fresh inventory."""
        self._force_inventory = True
        self._want_inventory = True
        self._kick_collector()

    def _kick_collector(self) -> None:
        """One short-lived collector thread; skipped while the previous
        one is still running (no pile-up — the HUD lesson). In step mode
        the thread walks ONE reading at a time with a dwell between
        steps (a full cycle takes ~20 s; ticks arriving meanwhile are
        skipped by the busy guard)."""
        if self._collector_busy or self._closing:
            return
        self._collector_busy = True
        want_inv, force_inv = self._want_inventory, self._force_inventory
        step_mode = self._step_mode
        self._force_inventory = False

        def work() -> None:
            snap: Optional[Dict[str, Any]] = None
            inv: Optional[Dict[str, Any]] = None
            try:
                if step_mode:
                    snap = self._run_steps_sequentially()
                else:
                    snap = system_health.snapshot(
                        thresholds=self._config.system_health)
                if want_inv:
                    inv = system_health.inventory(refresh=force_inv)
            except Exception:
                logger.debug("system-health collection failed", exc_info=True)
            finally:
                self._collector_busy = False
            if not self._closing:
                self.sampled.emit((snap, inv))

        threading.Thread(target=work, daemon=True,
                         name="system-health-sample").start()

    # ------------------------------------------------- step-by-step mode

    def _toggle_step_mode(self, on: bool) -> None:
        """Owner-facing diagnosis switch (session-only, default off): run
        each reading sequentially, visibly, one cycle after another."""
        self._step_mode = bool(on)
        if self._step_mode:
            self._step_cycle = 0
            self._ensure_step_rows()
            self._step_host.show()
            self._step_head_lbl.setText(tr(
                "Every attempt is logged to "
                "~/.deeptorrent/logs/health_steps.log — if the app "
                "closes, the last line names the reading that caused it"))
        else:
            self._step_host.hide()
        if self._active and not self._closing:
            self._kick_collector()

    def _ensure_step_rows(self) -> None:
        if self._step_rows:
            return
        for key, _fn in system_health.health_steps():
            lbl = QLabel("")
            shrink_label(lbl)
            lbl.setStyleSheet(scale_qss(
                "QLabel { color: #ffffff; font-size: 13px; }"))
            self._step_lay.addWidget(lbl)
            self._step_rows[key] = lbl
            self._set_step_row(key, "queued", "")

    def _set_step_row(self, key: str, state: str, detail: str) -> None:
        lbl = self._step_rows.get(key)
        if lbl is None:
            return
        label = tr(_STEP_LABELS.get(key, key))
        glyph = {"queued": "○", "reading": "◍", "ok": "✓",
                 "error": "✗"}.get(state, "○")
        color = (_STEP_READING_HEX if state == "reading"
                 else _STEP_ERROR_HEX if state == "error" else "#ffffff")
        text = f"{glyph}  {label}"
        if detail:
            text += f"   {detail}"
        lbl.setText(text)
        lbl.setStyleSheet(scale_qss(
            f"QLabel {{ color: {color}; font-size: 13px; }}"))

    def _on_step_update(self, payload) -> None:
        if self._closing or not isinstance(payload, tuple):
            return
        key, state, detail, cycle = payload
        if cycle:
            self._step_cycle = cycle
        self._set_step_row(key, state, detail)

    def _run_steps_sequentially(self) -> Dict[str, Any]:
        """Collector-thread body of the diagnosis mode: walk every reading
        one at a time, log BEGIN before each attempt (crash-safe — the
        log outlives the process) and OK/FAIL after, update the panel via
        the queued signal, dwell between steps. force=True on the readers
        re-drives the raw dxgi path (the 8.8 cache is bypassed here on
        purpose)."""
        snap = system_health.snapshot_base()
        self._step_cycle += 1
        cycle = self._step_cycle
        system_health.step_log(f"=== cycle {cycle} begin ===")
        for key, step_fn in system_health.health_steps():
            if self._closing or not self._step_mode:
                break
            system_health.step_log(f"cycle {cycle} BEGIN {key}")
            self.stepUpdate.emit((key, "reading", "", cycle))
            started = time.monotonic()
            try:
                step_fn(snap, True)
                state, note = "ok", ""
            except Exception as exc:   # readers never raise — belt anyway
                state, note = "error", str(exc)[:60]
            part = snap.get(key)
            if state == "ok" and isinstance(part, dict) and part.get("skipped"):
                # e.g. the GPU reading on AMD machines (owner 2026-11-20):
                # the step succeeded by NOT running its dangerous part.
                note = f"skipped ({part['skipped']})"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            detail = f"{elapsed_ms} ms" + (f"  {note}" if note else "")
            system_health.step_log(
                f"cycle {cycle} {'OK' if state == 'ok' else 'FAIL'} "
                f"{key} {elapsed_ms} ms")
            self.stepUpdate.emit((key, state, detail, cycle))
            time.sleep(_STEP_DWELL)
        system_health.step_log(f"=== cycle {cycle} end ===")
        system_health.snapshot_finalize(snap, self._config.system_health)
        return snap

    # ------------------------------------------------------------ updates
    def _apply_sample(self, payload) -> None:
        if self._closing or not payload:
            return
        snap, inv = payload
        if not isinstance(snap, dict):
            return
        try:
            self._apply_live(snap)
        except Exception:
            logger.debug("health sample apply failed", exc_info=True)
        if isinstance(inv, dict):
            if inv.get("available"):
                self._want_inventory = False
            self._fill_inventory(inv)

    def _apply_live(self, snap: Dict[str, Any]) -> None:
        cpu = snap.get("cpu") or {}
        cpu_pct = cpu.get("percent")
        self._cpu_m.set_reading(
            f"{cpu_pct:.0f}%" if cpu_pct is not None else "—",
            cpu_pct, sub=tr("{n} threads").format(n=cpu.get("cores") or "?"))

        ram = snap.get("ram") or {}
        ram_pct = ram.get("percent")
        self._ram_m.set_reading(
            f"{ram_pct:.0f}%" if ram_pct is not None else "—",
            ram_pct,
            sub=(f"{ram.get('used_mb', 0) / 1024:.1f}/"
                 f"{ram.get('total_mb', 0) / 1024:.1f} GB"))

        gpu = snap.get("gpu") or {}
        gpu_pct = gpu.get("percent") if gpu.get("live") else None
        if gpu.get("skipped") == "amd":
            # Owner directive 2026-11-20: the dxgi-based GPU reading is OFF
            # on AMD machines (stability) — say so instead of "not detected".
            gpu_sub = tr("skipped on AMD (stability)")
        else:
            gpu_sub = _elide(gpu.get("name") or tr("not detected"), 30)
        self._gpu_m.set_reading(
            f"{gpu_pct:.0f}%" if gpu_pct is not None else "—",
            gpu_pct, sub=gpu_sub)
        vram_pct = None
        if gpu.get("live") and gpu.get("vram_total_mb"):
            vram_pct = min(100.0, gpu["vram_used_mb"] * 100.0
                           / gpu["vram_total_mb"])
        vram_sub = (f"{gpu.get('vram_used_mb', 0) / 1024:.1f}/"
                    f"{gpu.get('vram_total_mb', 0) / 1024:.1f} GB")
        if gpu.get("skipped") == "amd":
            vram_sub = tr("skipped on AMD (stability)")
        self._vram_m.set_reading(
            f"{vram_pct:.0f}%" if vram_pct is not None else "—", vram_pct,
            sub=vram_sub)

        temps = snap.get("temperatures") or {}
        gpu_t = temps.get("gpu_c")
        self._gpu_temp_chip.setText(
            tr("GPU {t} °C").format(t=f"{gpu_t:.0f}") if gpu_t is not None
            else tr("GPU temp —"))
        fan = gpu.get("fan_percent")
        self._fan_chip.setText(
            tr("Fan {n}%").format(n=f"{fan:.0f}") if fan is not None
            else tr("Fan —"))
        power = gpu.get("power_w")
        self._power_chip.setText(
            tr("Power {n} W").format(n=f"{power:.0f}") if power is not None
            else tr("Power —"))
        gen, width = gpu.get("pcie_gen"), gpu.get("pcie_width")
        if gen is not None and width is not None:
            self._pcie_chip.setText(
                tr("PCIe {g} × {w}").format(g=f"{gen:.0f}", w=f"{width:.0f}"))
        else:
            self._pcie_chip.setText(tr("PCIe —"))
        cpu_t = temps.get("cpu_c")
        self._cpu_temp_chip.setText(
            tr("CPU {t} °C").format(t=f"{cpu_t:.0f}") if cpu_t is not None
            else tr("CPU temp —"))
        self._uptime_chip.setText(
            tr("Up {n}").format(n=_fmt_uptime(snap.get("uptime_seconds"))))

        battery = snap.get("battery")
        if battery and battery.get("percent") is not None:
            self._battery_chip.show()
            text = tr("Battery {n}%").format(n=battery["percent"])
            if battery.get("charging"):
                text += " ⚡"
            self._battery_chip.setText(text)
        else:
            self._battery_chip.hide()

        per_core = cpu.get("per_core") or []
        self._core_strip.set_values(per_core)
        self._core_strip.setVisible(bool(per_core))

        self._sync_disk_rows(snap.get("disks") or [])
        self._sync_net_rows(snap.get("network") or [])
        self._sync_drive_rows(snap.get("drives") or [])
        self._sync_warnings(snap.get("warnings") or [])
        self._updated_lbl.setText(
            tr("Updated {t}").format(t=time.strftime("%H:%M:%S")))

    def _sync_disk_rows(self, disks: List[Dict[str, Any]]) -> None:
        seen = set()
        for d in disks:
            vol = str(d.get("volume") or "?")
            seen.add(vol)
            row = self._disk_rows.get(vol)
            if row is None:
                meter = HealthMeter(vol)
                speed = QLabel("")
                shrink_label(speed)
                speed.setStyleSheet(scale_qss(
                    "QLabel { color: #ffffff; font-size: 14px; }"))
                wrap = QWidget()
                lay = QHBoxLayout(wrap)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.setSpacing(8)
                lay.addWidget(meter, 1)
                lay.addWidget(speed)
                self._disk_lay.addWidget(wrap)
                row = self._disk_rows[vol] = (meter, speed, wrap)
            meter, speed, _ = row
            free_gb = d.get("free_gb")
            meter.set_reading(
                tr("{n} GB free").format(n=f"{free_gb:.0f}")
                if free_gb is not None else "—",
                d.get("used_percent"), neutral=True,
                sub=(tr("of {n} GB").format(n=f"{d.get('total_gb') or 0:.0f}")))
            speed.setText(
                f"↓ {_fmt_bps(d.get('read_bps'))}"
                f"   ↑ {_fmt_bps(d.get('write_bps'))}")
        for vol in list(self._disk_rows):
            if vol not in seen:
                _, _, wrap = self._disk_rows.pop(vol)
                wrap.setParent(None)

    def _sync_net_rows(self, net: List[Dict[str, Any]]) -> None:
        seen = set()
        for entry in net:
            name = str(entry.get("name") or "?")
            # Key by interface INDEX when present: two same-model NICs
            # (dual-port / twin USB adapters) must not collapse into one
            # row just because their display names match.
            key = entry.get("index", name)
            seen.add(key)
            row = self._net_rows.get(key)
            if row is None:
                lbl = QLabel("")
                shrink_label(lbl)
                lbl.setStyleSheet(scale_qss(
                    "QLabel { color: #ffffff; font-size: 13px; }"))
                speed = QLabel("")
                shrink_label(speed)
                speed.setStyleSheet(scale_qss(
                    "QLabel { color: #ffffff; font-size: 13px; }"))
                wrap = QWidget()
                lay = QHBoxLayout(wrap)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.setSpacing(8)
                lay.addWidget(lbl, 1)
                lay.addWidget(speed)
                self._net_lay.addWidget(wrap)
                row = self._net_rows[key] = (lbl, speed, wrap)
            lbl, speed, _ = row
            lbl.setText(_elide(name, 40))
            speed.setText(
                f"↓ {_fmt_bps(entry.get('rx_bps'))}"
                f"   ↑ {_fmt_bps(entry.get('tx_bps'))}")
        for key in list(self._net_rows):
            if key not in seen:
                _, _, wrap = self._net_rows.pop(key)
                wrap.setParent(None)

    def _sync_drive_rows(self, drives: List[Dict[str, Any]]) -> None:
        seen = set()
        for drv in drives:
            index = drv.get("index")
            if not isinstance(index, int):
                continue
            seen.add(index)
            label = self._drv_rows.get(index)
            if label is None:
                label = QLabel("")
                shrink_label(label)
                self._drv_lay.addWidget(label)
                self._drv_rows[index] = label
            parts = [str(drv.get("name") or f"Drive {index}")]
            kind = drv.get("kind")
            if kind:
                parts.append(str(kind).upper())
            temp = drv.get("temp_c")
            if temp is not None:
                parts.append(f"{temp:.0f} °C")
            if kind == "nvme" and drv.get("used_percent") is not None:
                parts.append(tr("{n}% used").format(n=drv["used_percent"]))
            health = drv.get("health")
            if health == "fail":
                parts.append(tr("failing — back up this drive"))
            elif health == "warn":
                parts.append(tr("wear high"))
            label.setText(_elide(" · ".join(parts), 90))
            color = (_DANGER_HEX if health == "fail"
                     else _WARN_HEX if health == "warn" else "#ffffff")
            label.setStyleSheet(scale_qss(
                f"QLabel {{ color: {color}; font-size: 13px; }}"))
        for index in list(self._drv_rows):
            if index not in seen:
                label = self._drv_rows.pop(index)
                label.setParent(None)

    def _sync_warnings(self, warnings: List[Dict[str, Any]]) -> None:
        if not warnings:
            self._warn_lbl.hide()
            self._last_severity = ""
            return
        severity = ("danger" if any(w.get("severity") == "danger"
                                     for w in warnings) else "warn")
        border = _DANGER_HEX if severity == "danger" else _WARN_HEX
        if severity != self._last_severity:
            self._last_severity = severity
            self._warn_lbl.setStyleSheet(scale_qss(
                "QLabel { background-color: #0b1120; border: 1px solid "
                f"{border}; border-radius: 4px; padding: 6px 10px;"
                " color: #ffffff; font-size: 14px; }"))
        self._warn_lbl.setText("⚠  " + "\n".join(
            str(w.get("message") or "") for w in warnings))
        self._warn_lbl.show()

    # ----------------------------------------------------------- inventory
    def _toggle_inventory(self) -> None:
        visible = not self._inv_tree.isVisible()
        self._inv_tree.setVisible(visible)
        self._inv_toggle.setText(
            tr("▾ Hardware inventory") if visible
            else tr("▸ Hardware inventory"))

    def _fill_inventory(self, inv: Dict[str, Any]) -> None:
        self._inv_tree.clear()
        if not inv.get("available"):
            self._inv_tree.addTopLevelItem(QTreeWidgetItem(
                [tr("Inventory"), tr("unavailable — the system query "
                                     "failed or was blocked")]))
            return
        rows: List[Tuple[str, str]] = []
        cpu = inv.get("cpu") or {}
        if cpu.get("name"):
            rows.append(("CPU", " ".join(filter(None, (
                cpu["name"],
                f"{cpu.get('cores') or '?'} cores / "
                f"{cpu.get('logical') or '?'} threads"
                if cpu.get("cores") or cpu.get("logical") else "",
                f"{cpu['base_clock_mhz']} MHz"
                if cpu.get("base_clock_mhz") else "")))))
        for stick in inv.get("ram") or []:
            speed = stick.get("configured_mts") or stick.get("speed_mts")
            rows.append((
                f"RAM · {stick.get('slot') or '?'}",
                " ".join(filter(None, (
                    f"{stick.get('capacity_gb', 0):.0f} GB"
                    if stick.get("capacity_gb") else "",
                    stick.get("manufacturer") or "",
                    stick.get("part") or "",
                    f"{speed} MT/s" if speed else "")))))
        board = inv.get("board") or {}
        if board.get("model") or board.get("manufacturer"):
            rows.append((tr("Motherboard"), " ".join(filter(None, (
                board.get("manufacturer") or "", board.get("model") or "",
                board.get("version") or "")))))
        bios = inv.get("bios") or {}
        if bios.get("version"):
            rows.append((tr("BIOS"), " ".join(filter(None, (
                bios["version"], bios.get("date") or "")))))
        for g in inv.get("gpus") or []:
            if g.get("name"):
                rows.append((tr("GPU"), " ".join(filter(None, (
                    g["name"],
                    f"driver {g['driver']}" if g.get("driver") else "")))))
        for d in inv.get("disks") or []:
            if d.get("model"):
                rows.append((tr("Disk"), " ".join(filter(None, (
                    d["model"],
                    f"{d['size_gb']:.0f} GB" if d.get("size_gb") else "",
                    d.get("bus") or "", d.get("media") or "",
                    d.get("health") or "")))))
        for n in inv.get("nics") or []:
            if n.get("name"):
                rows.append((tr("Network"), " ".join(filter(None, (
                    n["name"],
                    f"{n['link_bps'] / 1e6:.0f} Mb/s"
                    if n.get("link_bps") else "")))))
        osinfo = inv.get("os") or {}
        if osinfo.get("name"):
            rows.append((tr("Operating system"), " ".join(filter(None, (
                osinfo["name"], osinfo.get("version") or "",
                f"build {osinfo['build']}"
                if osinfo.get("build") else "")))))
        for name, details in rows:
            self._inv_tree.addTopLevelItem(QTreeWidgetItem([name, details]))

    # ----------------------------------------------------------- lifecycle
    def hideEvent(self, event) -> None:  # noqa: N802
        # Minimized/covered while still the current tab: stop wasting
        # spawns; showEvent resumes when visible again.
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        if self._active and not self._closing \
                and not self._timer.isActive():
            hardware_monitor.seed_cpu_baseline()
            self._timer.start()
        super().showEvent(event)

    def shutdown(self) -> None:
        """MainWindow.closeEvent hook — stops all sampling for good."""
        self._closing = True
        self._active = False
        self._timer.stop()
