"""DeepFlux mini player — the default lightweight boot surface (7.8).

Fast video open (FAST_VIDEO_OPEN_STUDY.md): association launches and plain
shortcut boots land here FIRST. The window is deliberately Qt-light — it
imports nothing from ``gui.main_window`` / the engine / the agent stack, so
a double-clicked video starts in well under a second (measured 215 ms dev
warm to ``play()``; the full app took 3–6 s). The full app is one morph
button away (two-way, owner decision 2026-11-10):

* ⤢ "Open full app" — stops playback, then builds the real MainWindow
  behind a staged progress bar (honest, measured stage weights) and shows
  it. The heavy imports run on a worker thread while the bar animates.
* ◱ "Mini player" (Play-tab toolbar in the full app) — stops playback,
  hides the MainWindow (engines/agent/downloads keep running) and shows
  this window again instantly.

The morph uses the stopped-playback contract on purpose: keeping video
alive across the transformation would require re-parenting the native mpv
surface (feasible per the 2026-11-10 spikes, but deliberately not built —
see the study's appendix §8).

The idle state paints the same owner banner the Agent tab uses
(gui/brand_assets.resolve_banner_path — single source of truth).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Optional

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QKeyEvent, QLinearGradient, \
    QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel,
                               QMessageBox, QProgressBar, QPushButton,
                               QSlider, QStackedLayout, QVBoxLayout, QWidget)

from config import APP_VERSION, DeeptorrentConfig
from gui.brand_assets import resolve_banner_path, resolve_logo_path
from gui.fonts import scale_qss
from gui.i18n import tr
from infra.file_associations import AUDIO_EXTS, VIDEO_EXTS

logger = logging.getLogger(__name__)

MEDIA_EXTS = tuple(sorted(set(VIDEO_EXTS + AUDIO_EXTS)))

# Morph progress weights — coarse but REAL: they mirror the measured
# construction costs (FAST_VIDEO_OPEN_STUDY.md §3). The import stage runs
# on a worker thread; the build stages run on the GUI thread with
# processEvents() between them so the bar visibly ticks.
_MORPH_STAGES = [
    ("loading", 30.0),      # heavy imports (worker thread)
    ("engines", 35.0),      # torrent+download engines, control API, tools
    ("services", 38.0),     # background workers, branding, theme
    ("agent", 48.0),        # Agent tab (chat + input row)
    ("downloads", 50.0),    # Download tab panels
    ("browser", 82.0),      # Browser tab (QtWebEngine profile — the big one)
    ("play", 90.0),         # IPTV tab
    ("chrome", 96.0),       # Commander + Room + menus + side panel
    ("done", 100.0),
]

_STAGE_LABELS = {
    "loading": "Loading modules…",
    "engines": "Starting engines…",
    "services": "Preparing services…",
    "agent": "Building the Agent…",
    "downloads": "Building downloads…",
    "browser": "Starting the browser…",
    "play": "Building the player…",
    "chrome": "Finishing up…",
    "done": "Done",
}


def is_media_file(path: str) -> bool:
    """True for a local file DeepFlux's mini player can open directly."""
    return bool(path) and os.path.isfile(path) and \
        path.lower().endswith(MEDIA_EXTS)


class _MorphSignals(QWidget):
    """Cross-thread marshal for the morph: the heavy import runs on a worker
    thread, but everything touching QWidget/MainWindow MUST run on the GUI
    thread — a signal emission from the worker arrives as a queued
    connection. (QTimer.singleShot from the worker would fire in a thread
    with no event loop and never run.)"""
    imported = Signal(object)   # Exception | None


class _IdleBanner(QWidget):
    """The idle PAGE — paints the owner banner FITTED behind its own layout
    of hint row + buttons. Owner decisions 2026-11-10/11: the page itself
    must be the painter (a sibling "background" child with no layout never
    resizes with the window — the small-top-left-corner bug), and the image
    is FITTED, never cropped: scaled as large as the window allows with the
    aspect ratio preserved, centered, letterboxed with black where the
    window's shape differs (CSS object-fit: contain). Mipmap-style
    downscale like _ChatHistoryEdit."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        banner = resolve_banner_path()
        self._banner = QPixmap(banner) if banner else QPixmap()
        self._scaled = QPixmap()

    def _rescale(self) -> None:
        if self._banner.isNull():
            self._scaled = QPixmap()
            return
        vp = self.size()
        if vp.width() < 2 or vp.height() < 2:
            return
        # Fit: the WHOLE image stays visible — scale to the larger
        # constraint that still fits both axes, aspect preserved.
        target = self._banner.size()
        target.scale(vp.width(), vp.height(), Qt.KeepAspectRatio)
        # Cascade alias-free 2:1 steps before the final smooth scale (same
        # rationale as _ChatHistoryEdit — bilinear aliases on big downscales).
        img = self._banner.toImage()
        while img.width() > 2 * target.width() and img.width() > 1:
            img = img.scaled(max(1, img.width() // 2), max(1, img.height() // 2),
                             Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self._scaled = QPixmap.fromImage(
            img.scaled(target, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._rescale()
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        rect = self.rect()
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, QColor("#000000"))
        gradient.setColorAt(1, QColor("#000000"))
        painter.fillRect(rect, gradient)
        if not self._scaled.isNull():
            # Centered letterbox: exactly one axis matches the viewport,
            # the other centers with black bars on either side.
            x = (rect.width() - self._scaled.width()) // 2
            y = (rect.height() - self._scaled.height()) // 2
            painter.drawPixmap(x, y, self._scaled)
        painter.end()


class MiniPlayerWindow(QWidget):
    """The lightweight player window. Two visual states (idle / playing)
    plus a morph overlay; one optional heavy sibling (the MainWindow)."""

    # Emitted when the staged morph finished and the full app is up.
    morphed_to_full = Signal(object)   # (MainWindow)
    # Control-API opens arrive on the API's HTTP thread — QWidget work must
    # hop to the GUI thread via this signal (same pattern as MainWindow's
    # _OpenSignals).
    openRequested = Signal(str)
    # Backend callbacks fire on mpv's EVENT thread — widget updates must hop
    # to the GUI thread via signals (exactly what PlayerWidget's sig_state /
    # sig_position exist for in the full app).
    backendState = Signal(str)
    backendPosition = Signal(float, float)

    def __init__(self, config: DeeptorrentConfig, config_path: str,
                 backend_factory=None) -> None:
        super().__init__()
        self._config = config
        self._config_path = config_path
        self._backend = None
        self._main = None            # the morphed-to MainWindow (hidden ↔ shown)
        self._morphing = False
        self._duration = 0.0
        self._seeking = False
        self._last_media = ""        # resume offer after the first morph
        self._pending_target = ""    # open target that triggered a morph
        self._queue: list = []       # on-deck media (multi-target opens)
        self._chrome_timer = QTimer(self)
        self._chrome_timer.setInterval(2000)
        self._chrome_timer.timeout.connect(self._hide_chrome_if_fullscreen)
        # Worker→GUI marshal for the morph (see _MorphSignals).
        self._morph_signals = _MorphSignals()
        self._morph_signals.imported.connect(self._after_import)
        # Control-API opens are emitted (thread-safe) and routed on the GUI
        # thread by the slot.
        self.openRequested.connect(self.route_open_target)
        # Backend callbacks are likewise emitted from mpv's event thread and
        # land here queued on the GUI thread.
        self.backendState.connect(self._on_state)
        self.backendPosition.connect(self._on_position)
        # Injectable for tests (default: the real mpv/VLC factory).
        self._backend_factory = backend_factory or self._create_backend

        self.setWindowTitle(f"DeepFlux Mini {APP_VERSION}")
        logo = resolve_logo_path()
        if logo:
            self.setWindowIcon(QIcon(logo))
        self.setMinimumSize(420, 280)
        self.setAcceptDrops(True)
        # Keyboard ownership (the PlayerWidget focus-stranding lesson): the
        # WINDOW holds focus; sliders never take it; every transport click
        # hands it back — so ←/→ (±10 s), Space, F etc. always work no
        # matter what was touched last.
        self.setFocusPolicy(Qt.StrongFocus)
        self._apply_qss()

        # --- stacked pages: idle | video ---
        self._stack = QStackedLayout(self)
        self._build_idle_page()
        self._build_video_page()
        self._build_morph_overlay()

        # Restore last session's geometry (falls back to a compact default).
        if config.mini_player.geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(
                    config.mini_player.geometry.encode("ascii")))
            except Exception:
                pass
        if not config.mini_player.geometry:
            self.resize(680, 440)

    # ------------------------------------------------------------------ UI
    def _apply_qss(self) -> None:
        self.setStyleSheet(scale_qss("""
            QWidget { background-color: #05070c; color: #ffffff; }
            QPushButton {
                background-color: #0b1120; color: #ffffff;
                border: 1px solid #1a2a4a; border-radius: 4px;
                padding: 6px 14px; font-size: 14px;
            }
            QPushButton:hover { background-color: #1a2a4a; }
            QPushButton#morph_btn {
                background-color: #2a7abf; color: #ffffff;
                font-weight: 600;
            }
            QPushButton#morph_btn:hover {
                background-color: #2a7abf; border-color: #a8edff;
            }
            QSlider::groove:horizontal {
                height: 4px; background: #1a2a4a; border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #2a7abf; width: 10px; margin: -4px 0;
                border-radius: 5px;
            }
            QLabel { color: #ffffff; background: transparent; }
            QProgressBar {
                background-color: #080b12; color: #ffffff;
                border: 1px solid #1a2a4a; border-radius: 4px;
                text-align: center; font-size: 13px;
            }
            QProgressBar::chunk { background-color: #2a7abf; border-radius: 3px; }
        """))

    def _build_idle_page(self) -> None:
        # The page IS the banner painter (children draw over its paintEvent)
        # — never a sibling background widget: without a layout it stays
        # tiny in the top-left corner (owner bug report 2026-11-11).
        page = _IdleBanner()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 24)
        lay.addStretch(1)
        hint = QLabel(tr("Drop a video or audio file anywhere — or"))
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._open_btn = QPushButton(tr("Open file…"))
        self._open_btn.setToolTip(tr("Pick a video or audio file to play"))
        self._open_btn.clicked.connect(self._pick_file)
        btn_row.addWidget(self._open_btn)
        self._morph_btn_idle = QPushButton(tr("⤢ Open full app"))
        self._morph_btn_idle.setObjectName("morph_btn")
        self._morph_btn_idle.setToolTip(tr(
            "Build the full DeepFlux app (torrents, browser, agent) — "
            "playback stops during the switch"))
        self._morph_btn_idle.clicked.connect(self.morph_to_full)
        btn_row.addWidget(self._morph_btn_idle)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)
        self._idle_page = page
        self._idle_bg = page
        self._stack.addWidget(page)

    def _build_video_page(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.surface = QWidget()
        self.surface.setStyleSheet("background-color: #000000;")
        lay.addWidget(self.surface, 1)
        self._build_transport(lay)
        self._video_page = page
        self._stack.addWidget(page)
        self._stack.setCurrentWidget(self._idle_page)

    def _build_transport(self, lay: QVBoxLayout) -> None:
        self._transport = QWidget()
        self._transport.setStyleSheet(scale_qss(
            "QWidget { background-color: #080b12; border-top: 1px solid #1a2a4a; }"))
        row = QHBoxLayout(self._transport)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(6)

        self._play_btn = QPushButton("⏸")
        self._play_btn.setFixedWidth(40)
        self._play_btn.setToolTip(tr("Play/Pause (Space)"))
        self._play_btn.clicked.connect(self._toggle_pause)
        row.addWidget(self._play_btn)

        self._rw_btn = QPushButton("⏪")
        self._rw_btn.setFixedWidth(40)
        self._rw_btn.setToolTip(tr("Back 10 seconds (←)"))
        self._rw_btn.clicked.connect(lambda: self._seek_by(-10))
        row.addWidget(self._rw_btn)

        self._ff_btn = QPushButton("⏩")
        self._ff_btn.setFixedWidth(40)
        self._ff_btn.setToolTip(tr("Forward 10 seconds (→)"))
        self._ff_btn.clicked.connect(lambda: self._seek_by(10))
        row.addWidget(self._ff_btn)

        self.seek = QSlider(Qt.Horizontal)
        self.seek.setToolTip(tr("Seek"))
        self.seek.setFocusPolicy(Qt.NoFocus)   # keys belong to the window
        self.seek.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek.sliderReleased.connect(self._on_seek_released)
        self.seek.sliderMoved.connect(self._on_seek_moved)
        row.addWidget(self.seek, 1)

        self._time_lbl = QLabel("0:00 / 0:00")
        row.addWidget(self._time_lbl)

        self._mute_btn = QPushButton("🔊")
        self._mute_btn.setFixedWidth(36)
        self._mute_btn.setToolTip(tr("Mute (M)"))
        self._mute_btn.clicked.connect(self._toggle_mute)
        row.addWidget(self._mute_btn)

        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(self._config.mini_player.volume)
        self.vol.setMaximumWidth(90)
        self.vol.setFocusPolicy(Qt.NoFocus)    # keys belong to the window
        self.vol.setToolTip(tr("Volume"))
        self.vol.valueChanged.connect(self._on_volume)
        row.addWidget(self.vol)

        self._fs_btn = QPushButton("⛶")
        self._fs_btn.setFixedWidth(36)
        self._fs_btn.setToolTip(tr("Fullscreen (F)"))
        self._fs_btn.clicked.connect(self._toggle_fullscreen)
        row.addWidget(self._fs_btn)

        self._morph_btn = QPushButton(tr("⤢ Full app"))
        self._morph_btn.setObjectName("morph_btn")
        self._morph_btn.setToolTip(tr(
            "Switch to the full DeepFlux app — playback stops during the switch"))
        self._morph_btn.clicked.connect(self.morph_to_full)
        row.addWidget(self._morph_btn)

        lay.addWidget(self._transport)

        # A clicked button KEEPS keyboard focus — Space would re-trigger it
        # and ←/→ would never reach the window. Hand focus back after every
        # transport interaction (same lesson as PlayerWidget's re-seating).
        for btn in (self._open_btn, self._play_btn, self._rw_btn,
                    self._ff_btn, self._mute_btn, self._fs_btn):
            btn.clicked.connect(self._reseat_focus)

    def _reseat_focus(self, *args) -> None:
        """Return keyboard focus to the window after button/slider use."""
        self.setFocus()

    def _build_morph_overlay(self) -> None:
        self._morph_overlay = QWidget(self)
        self._morph_overlay.setStyleSheet(scale_qss(
            "QWidget { background-color: rgba(5,7,12,0.96); }"))
        col = QVBoxLayout(self._morph_overlay)
        col.addStretch(1)
        self._morph_lbl = QLabel(tr("Loading modules…"))
        self._morph_lbl.setAlignment(Qt.AlignCenter)
        col.addWidget(self._morph_lbl)
        self._morph_bar = QProgressBar()
        self._morph_bar.setRange(0, 100)
        self._morph_bar.setValue(0)
        col.addWidget(self._morph_bar)
        sub = QLabel(tr("Building DeepFlux — your download engines, browser and agent are on their way"))
        sub.setAlignment(Qt.AlignCenter)
        col.addWidget(sub)
        col.addStretch(1)
        self._morph_overlay.setLayout(col)
        self._morph_overlay.hide()

    # -------------------------------------------------------------- playback
    def _create_backend(self):
        from iptv.player import create_backend
        backend = create_backend(self.surface,
                                 preferred=self._config.iptv.preferred_player,
                                 svp=False)
        if backend is None:
            return None
        backend.on_state = self.backendState.emit
        backend.on_position = self.backendPosition.emit
        backend.on_error = self._on_error
        backend.set_hwdec(self._config.iptv.hwdec)
        backend.set_volume(self._config.mini_player.volume)
        backend.set_mute(self._config.mini_player.muted)
        return backend

    def open_media(self, path: str) -> None:
        """Play a local media file (association open / drag-drop / picker)."""
        if self._morphing:
            return
        path = os.path.abspath(os.path.expanduser(path))
        if not is_media_file(path):
            if path.lower().endswith(MEDIA_EXTS):
                # Named like media but not openable — say so instead of
                # silently showing the idle banner (missing/moved file).
                QMessageBox.warning(
                    self, tr("DeepFlux Mini"),
                    tr("Cannot open this file:") + f"\n{path}")
            else:
                logger.warning("mini player: not a playable media file: %r",
                               path)
            return
        if self._backend is None:
            self._backend = self._backend_factory()
            if self._backend is None:
                QMessageBox.warning(
                    self, tr("DeepFlux Mini"),
                    tr("No playback backend available (mpv/VLC missing).\n"
                       "The full app's Play tab has player diagnostics."))
                return
        self._last_media = path
        self._stack.setCurrentWidget(self._video_page)
        self.setWindowTitle(f"{os.path.basename(path)} — DeepFlux Mini")
        self._duration = 0.0
        self.setFocus()   # keys (←/→ ±10 s, Space, F) belong to the window
        self._backend.play(path)

    def _pick_file(self) -> None:
        start = self._config.mini_player.last_folder or ""
        if start and not os.path.isdir(start):
            start = ""
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Open media file"), start,
            tr("Media files ({exts})").format(
                exts=" ".join("*" + e for e in MEDIA_EXTS)))
        if path:
            self._config.mini_player.last_folder = \
                os.path.dirname(os.path.abspath(path))
            self.open_media(path)

    def _show_morph_error(self, message: str) -> None:
        QMessageBox.warning(self, tr("DeepFlux Mini"), message)

    # backend callbacks ------------------------------------------------
    def _on_state(self, state: str) -> None:
        if state == "playing":
            self._play_btn.setText("⏸")
        elif state == "paused":
            self._play_btn.setText("▶")
        elif state == "stopped":
            self._play_btn.setText("▶")
            # Auto-advance the on-deck queue (multi-select "Open with").
            if self._queue and not self._morphing:
                nxt = self._queue.pop(0)
                QTimer.singleShot(0, lambda: self.open_media(nxt))

    def _on_position(self, pos: float, duration: float) -> None:
        if duration > 0 and abs(duration - self._duration) > 0.5:
            self._duration = duration
            self.seek.setRange(0, int(duration))
        if not self._seeking:
            self.seek.setValue(int(pos))
        self._time_lbl.setText(f"{_fmt(pos)} / {_fmt(duration)}")

    def _on_error(self, message: str) -> None:
        logger.warning("mini player backend error: %s", message)

    # transport ----------------------------------------------------------
    def _toggle_pause(self) -> None:
        if self._backend is None:
            return
        if self._play_btn.text() == "⏸":
            self._backend.pause()
        else:
            self._backend.resume()

    def _seek_by(self, delta: float) -> None:
        if self._backend is not None:
            self._backend.seek_by(delta)

    def _on_seek_moved(self, value: int) -> None:
        self._time_lbl.setText(f"{_fmt(value)} / {_fmt(self._duration)}")

    def _on_seek_released(self) -> None:
        self._seeking = False
        if self._backend is not None:
            self._backend.seek(float(self.seek.value()))

    def _on_volume(self, value: int) -> None:
        self._config.mini_player.volume = value
        if self._backend is not None:
            self._backend.set_volume(value)

    def _toggle_mute(self) -> None:
        mute = self._mute_btn.text() == "🔊"  # showing 🔊 = currently unmuted
        if self._backend is not None:
            self._backend.set_mute(mute)
        self._mute_btn.setText("🔇" if mute else "🔊")
        self._config.mini_player.muted = mute

    # fullscreen -----------------------------------------------------------
    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._chrome_timer.stop()
            self._transport.show()
        else:
            self.showFullScreen()
            self._arm_chrome_timer()

    def _hide_chrome_if_fullscreen(self) -> None:
        if self.isFullScreen():
            self._transport.hide()

    def _arm_chrome_timer(self) -> None:
        self._chrome_timer.start()
        if not self.isFullScreen():
            self._transport.show()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.isFullScreen():
            self._transport.show()
            self._arm_chrome_timer()
        super().mouseMoveEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        # The morph overlay must track resizes while visible (the window can
        # still be resized programmatically during staged construction).
        if self._morph_overlay.isVisible():
            self._morph_overlay.setGeometry(self.rect())
        super().resizeEvent(event)

    # keyboard ---------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key_Space:
            self._toggle_pause()
        elif key == Qt.Key_F:
            self._toggle_fullscreen()
        elif key == Qt.Key_Escape and self.isFullScreen():
            self._toggle_fullscreen()
        elif key == Qt.Key_Left:
            self._seek_by(-10)
        elif key == Qt.Key_Right:
            self._seek_by(10)
        elif key == Qt.Key_Up:
            self.vol.setValue(min(100, self.vol.value() + 5))
        elif key == Qt.Key_Down:
            self.vol.setValue(max(0, self.vol.value() - 5))
        elif key == Qt.Key_M:
            self._toggle_mute()
        else:
            super().keyPressEvent(event)

    # drag & drop ---------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        if urls and is_media_file(urls[0].toLocalFile()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        for url in urls:
            path = url.toLocalFile()
            if is_media_file(path):
                self.open_media(path)
                break

    # -------------------------------------------------------------- morphing
    def _on_stage(self, stage: str, fraction: float) -> None:
        """Progress hook handed to MainWindow's staged constructor."""
        self._morph_lbl.setText(tr(_STAGE_LABELS.get(stage, stage)))
        self._morph_bar.setValue(int(fraction))
        QApplication.processEvents()   # let the bar paint between stages

    def _set_morph_overlay(self, on: bool) -> None:
        self._morphing = on
        self._open_btn.setEnabled(not on)
        self._morph_btn_idle.setEnabled(not on)
        self._morph_btn.setEnabled(not on)
        if on:
            self._morph_bar.setRange(0, 100)
            self._morph_bar.setValue(0)
            self._morph_lbl.setText(tr(_STAGE_LABELS["loading"]))
            self._morph_overlay.setGeometry(self.rect())
            self._morph_overlay.show()
            self._morph_overlay.raise_()
        else:
            self._morph_overlay.hide()

    def morph_to_full(self, after_target: Optional[str] = None) -> None:
        """Stop playback and transform into the full app behind a progress
        bar. If the MainWindow already exists (we morphed before), the
        switch is instant — no reconstruction, no progress bar."""
        if self._morphing:
            return
        if self._main is not None:
            self._stop_playback()
            self.hide()
            self._main._return_from_mini()
            self._main.show()
            self._main.raise_()
            self._main.activateWindow()
            if after_target:
                QTimer.singleShot(0, lambda: self._main.open_target(after_target))
            return
        if after_target:
            self._pending_target = after_target
        self._stop_playback()
        self._set_morph_overlay(True)
        # The heavy import runs on a worker thread so the bar keeps animating.
        threading.Thread(target=self._import_main_window, daemon=True,
                         name="mini-morph-import").start()

    def _import_main_window(self) -> None:
        error = None
        try:
            import gui.main_window  # noqa: F401  (the heavy chain)
        except Exception as exc:   # pragma: no cover - environment failure
            error = exc
        # Signal (not QTimer): the worker thread has no event loop, but a
        # signal emitted here is queued to the GUI thread automatically.
        self._morph_signals.imported.emit(error)

    def _after_import(self, error: Optional[Exception]) -> None:
        if error is not None:
            self._set_morph_overlay(False)
            self._show_morph_error(f"Could not load the full app: {error}")
            return
        try:
            # Port handoff: OUR control API must release 53742 before the
            # MainWindow constructs its own (sequential on the GUI thread).
            self._stop_control_api()
            # deepflux:// must be registered before the first WebEngine
            # profile — MainWindow normally does this in run_gui BEFORE the
            # QApplication exists; here the app already exists, which is
            # fine: the requirement is only "before the first profile".
            from gui.main_window import MainWindow, register_deepflux_scheme
            register_deepflux_scheme()
            self._on_stage("loading", 30.0)
            self._main = MainWindow(self._config_path,
                                    progress=self._on_stage)
            self._on_stage("done", 100.0)
            # Mirror run_gui's startup ritual: collect cyclic garbage FIRST,
            # then freeze the freshly built object graph off the collector
            # (the documented band-aid against the frozen build's
            # native-AV family — see run_gui's comment).
            import gc
            gc.collect()
            gc.freeze()
        except Exception as exc:
            logger.exception("mini→full morph failed")
            self._set_morph_overlay(False)
            self._show_morph_error(f"Could not build the full app: {exc}")
            return
        self._set_morph_overlay(False)
        self.hide()
        self._main._return_from_mini()
        self._main.show()
        self.morphed_to_full.emit(self._main)
        if self._pending_target:
            target = self._pending_target
            self._pending_target = ""
            QTimer.singleShot(0, lambda: self._main.open_target(target))
        elif getattr(self._main, "iptv_tab", None) is not None and \
                self._last_media:
            # Land on the Play tab with the file queued at the last position.
            media, pos = self._last_media, max(0.0, float(self.seek.value()))
            QTimer.singleShot(0, lambda: self._resume_in_play_tab(media, pos))

    def _resume_in_play_tab(self, media: str, pos: float) -> None:
        try:
            main = self._main
            main.main_tabs.setCurrentWidget(main.iptv_tab)
            main.iptv_tab.play_file(media)
            if pos > 1.0:
                player = main.iptv_tab._player
                player.seek.setValue(int(pos))   # visual pre-position
                backend = getattr(player, "_backend", None)
                if backend is not None:
                    backend.seek(pos)
        except Exception:
            logger.debug("resume-in-play-tab failed", exc_info=True)

    def _stop_playback(self) -> None:
        if self._backend is not None:
            try:
                self._backend.stop()
            except Exception:
                logger.debug("mini backend stop failed", exc_info=True)

    # ------------------------------------------------------------ app-level
    def start_control_api(self) -> None:
        """Single-instance listener so a second launch forwards its target
        here instead of opening another player (same contract as the full
        app's /api/open). Cheap: ≈2 ms with a dormant DownloadEngine."""
        if getattr(self, "_control_api", None) is not None:
            return
        try:
            from dlmgr.engine import DownloadEngine
            from dlmgr.control_api import ControlAPI
            self._dl_engine = DownloadEngine(self._config.download)
            self._dl_engine.start()
            self._control_api = ControlAPI(
                self._dl_engine, self._config.download.control_api_port)
            # EMIT the signal, never call the slot: the handler runs on the
            # API's HTTP thread; the slot touches QWidgets.
            self._control_api.set_open_handler(self.openRequested.emit)
            self._control_api.start()
        except Exception:
            logger.debug("mini control API failed to start", exc_info=True)

    def _stop_control_api(self) -> None:
        api = getattr(self, "_control_api", None)
        engine = getattr(self, "_dl_engine", None)
        self._control_api = None
        self._dl_engine = None
        if api is not None:
            try:
                api.stop()
            except Exception:
                pass
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass

    def route_open_target(self, target: str) -> None:
        """Control-API open routing while the mini player is the visible
        surface: media plays here; everything else (magnet/.torrent/html)
        needs the full app — morph if necessary, then dispatch there."""
        target = (target or "").strip().strip('"')
        if not target:
            return
        if is_media_file(target):
            self.show()
            self.raise_()
            self.activateWindow()
            self.open_media(target)
            return
        if self._main is not None:
            self._main.show()
            self._main.raise_()
            self._main.open_target(target)
        else:
            self._pending_target = target
            self.morph_to_full()

    def _route_control_api_to_main(self) -> None:
        """After the first morph the MainWindow owns the control API (it
        built its own; ours was stopped for the port). Nothing to do — kept
        as the explicit seam for the reverse morph's re-routing (see
        MainWindow.morph_to_mini)."""
        return

    def show_from_main(self, main) -> None:
        """Called by MainWindow.morph_to_mini: become the visible surface."""
        self._stop_playback()
        self._main = main
        self._stack.setCurrentWidget(self._idle_page)
        self.setWindowTitle(f"DeepFlux Mini {APP_VERSION}")
        self._play_btn.setText("▶")
        self.seek.setValue(0)
        self._time_lbl.setText("0:00 / 0:00")
        self._last_media = ""
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    # ------------------------------------------------------------- lifecycle
    def _save_prefs(self) -> None:
        try:
            self._config.mini_player.geometry = bytes(
                self.saveGeometry().toBase64()).decode("ascii")
            self._config.to_file(self._config_path)
        except Exception:
            logger.debug("mini prefs save failed", exc_info=True)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._morphing:
            event.ignore()
            return
        if self._main is not None:
            # The full app exists (possibly hidden). Its closeEvent runs the
            # REAL graceful shutdown — fast-resume, config, engines, telemetry
            # — and may ASK about active transfers. Ask it FIRST: if the user
            # declines there, the mini window must stay open too (closing it
            # anyway would leave no visible window and quit the app unclean).
            if not self._main.close():
                event.ignore()
                return
        else:
            # Mini-only session: no MainWindow ran the teardown, so do the
            # two things that matter — destroy the libmpv handle (a live
            # backend at interpreter shutdown is the exit-crash class the
            # IPTV tab fixed) and send the telemetry "leaving" note.
            self._stop_playback()
            if self._backend is not None:
                try:
                    self._backend.destroy()
                except Exception:
                    logger.debug("mini backend destroy failed",
                                 exc_info=True)
                self._backend = None
            try:
                from infra import telemetry
                telemetry.stop_heartbeat()
            except Exception:
                pass
        self._save_prefs()
        self._stop_control_api()
        event.accept()


def _fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Process-level singleton (one mini window per process, even across morphs)
# ---------------------------------------------------------------------------

_MINI_WINDOW: Optional[MiniPlayerWindow] = None


def get_mini_window() -> Optional[MiniPlayerWindow]:
    return _MINI_WINDOW


def show_mini_player(main_window) -> MiniPlayerWindow:
    """MainWindow.morph_to_mini entry point: reuse (or lazily create) the
    process's mini window and hand it the main-window reference."""
    global _MINI_WINDOW
    if _MINI_WINDOW is None:
        _MINI_WINDOW = MiniPlayerWindow(main_window.config,
                                        main_window.config_path)
    _MINI_WINDOW.show_from_main(main_window)
    return _MINI_WINDOW


def run_mini_player(config_path: Optional[str] = None,
                    media: Optional[str] = None,
                    queue: Optional[list] = None) -> int:
    """The fast boot path: mini player first, full app on demand.

    ``media`` plays immediately; ``queue`` (further media targets from a
    multi-select "Open with") auto-plays in order as each item stops.
    Mirrors run_gui's pre-window ritual (fonts/theme/i18n before any
    widget builds, first-run seed) but with the Qt-light window only."""
    global _MINI_WINDOW
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Deeptorrent")

    from config import DeeptorrentConfig as _Cfg
    _cfg_path = config_path or _Cfg.default_config_path()
    _cfg_existed = os.path.isfile(_cfg_path)
    _cfg = _Cfg.from_file(_cfg_path)

    from gui.fonts import load_app_fonts, set_font_scale, set_text_theme
    set_font_scale(_cfg.ui_font_size)
    set_text_theme(_cfg.ui_text_theme)
    load_app_fonts(app)
    try:
        from infra.first_run import maybe_seed_fresh_install
        maybe_seed_fresh_install(_cfg, _cfg_path, _cfg_existed)
    except Exception:
        logging.getLogger(__name__).debug("first-run seed skipped",
                                          exc_info=True)
    from gui.i18n import set_language
    set_language(_cfg.ui_language)

    _MINI_WINDOW = MiniPlayerWindow(_cfg, _cfg_path)
    if queue:
        _MINI_WINDOW._queue = [os.path.abspath(q) for q in queue]
    _MINI_WINDOW.show()
    # Single-instance listener + delayed telemetry (both off the paint path).
    QTimer.singleShot(0, _MINI_WINDOW.start_control_api)
    QTimer.singleShot(2000, _start_telemetry_lazily)
    if media:
        m = os.path.abspath(media)
        QTimer.singleShot(0, lambda: _MINI_WINDOW.open_media(m))
    return app.exec()


def _start_telemetry_lazily() -> None:
    """Heartbeat for mini-only sessions too (idempotent — MainWindow's own
    start call no-ops if we beat it). requests' import cost is paid here,
    off the boot path."""
    try:
        if _MINI_WINDOW is None:
            return
        cfg = _MINI_WINDOW._config
        if not cfg.stats.ping_enabled:
            return
        from infra import telemetry
        telemetry.start_heartbeat(
            cfg, data_dir=os.path.dirname(_MINI_WINDOW._config_path))
    except Exception:
        logging.getLogger(__name__).debug("telemetry start failed",
                                          exc_info=True)
