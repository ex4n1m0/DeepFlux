"""IPTV tab widget for DeepFlux.

Layout:
    +----------------------------------------------------------+
    | toolbar: [source▾] [Refresh] [search.....] [grid|list] ⚙ |
    +----------+-----------------------------+-----------------+
    | sidebar  | content (grid/list)         | player pane     |
    | Live TV  |  artwork grid / list view   |  embedded video |
    |  > News  |                             |  controls       |
    | Movies   |                             |                 |
    | Series   |                             |                 |
    | Favorites|                             |                 |
    | Recent   |                             |                 |
    +----------+-----------------------------+-----------------+
    | status bar: progress / messages                           |
    +----------------------------------------------------------+

All network/parse work runs through :class:`iptv.manager.IPTVManager` on
background threads; results are marshalled back to the GUI thread via Qt
signals. Lists/grids are virtualized (QListWidget in icon mode / QTableWidget)
and artwork is lazy-loaded so 50k+ entries stay responsive.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import zlib
from collections import OrderedDict, deque
from typing import Any, Dict, List, Optional

from PySide6.QtCore import (Qt, QPoint, QTimer, QUrl, Signal, QObject, QSize,
                            QRect, QEvent)
from PySide6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QFontMetrics,
                           QPainter, QPen, QPixmap, QIcon)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QDialog,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import DeeptorrentConfig, IPTVSourceConfig
from iptv.artwork import PRIORITY_PREFETCH, PRIORITY_VISIBLE
from iptv.manager import YEAR_OTHERS, IPTVManager
from iptv.metadata import clean_title, extract_year, metadata_key
from iptv.models import (
    SECTION_FAVORITES,
    SECTION_LIVE,
    SECTION_MOVIES,
    SECTION_RECENT,
    SECTION_SERIES,
    Channel,
    Movie,
    Series,
)
from iptv.player import PlayerBackend, create_backend
from dlmgr.ffmpeg import StreamRecorder, find_ffmpeg, is_network_stream_url

logger = logging.getLogger(__name__)

# Maximum items shown per sidebar leaf node. When a category or year bucket
# has more than this, the sidebar adds numbered bulk children (1–500, 501–1000,
# …) so the metadata scraper never processes tens of thousands of entries at
# once. Parent nodes with bulk children expand/collapse instead of showing all
# items directly.
BULK_SIZE = 500


def _source_empty_message(source: Any, playlist: Any) -> str:
    """Describe an empty source without guessing for non-Xtream loaders."""
    name = getattr(source, "name", "") or getattr(playlist, "source_id", "source")
    if getattr(source, "kind", "") != "xtream":
        return ("No entries loaded. Check the source URL/credentials in "
                "Settings → IPTV.")
    error = getattr(playlist, "error", None)
    kind = getattr(getattr(error, "kind", None), "value", "")
    if kind == "auth_rejected":
        return (f"{name}: Xtream authentication was rejected. Check the "
                "username, password, and subscription status.")
    if kind == "unreachable":
        return (f"{name}: Xtream provider is unreachable. Check the server "
                "URL, network connection, and provider status.")
    if kind == "invalid_response":
        return (f"{name}: Xtream provider returned an invalid response. "
                "Verify that the URL points to an Xtream Codes API server.")
    return (f"{name}: Xtream connection succeeded, but the source is "
            "genuinely empty (no channels, movies, or series were returned).")


# ---------------------------------------------------------------------------
# Qt signal bridge: worker threads -> GUI thread
# ---------------------------------------------------------------------------

class _IPTVSignals(QObject):
    progress = Signal(int, object)        # (count, total_or_None)
    load_done = Signal(bool, object)      # (ok, Playlist)
    artwork_ready = Signal(str, str)      # (url, local_path)
    metadata_ready = Signal(str, dict)    # (key, metadata)
    status = Signal(str)
    player_state = Signal(str)
    player_position = Signal(float, float)
    player_error = Signal(str)


# ---------------------------------------------------------------------------
# Player widget — embeds a backend (mpv/vlc) and draws auto-hiding controls
# ---------------------------------------------------------------------------

class PlayerWidget(QWidget):
    """Embedded video area with a control bar that auto-hides in fullscreen.

    The embedded mpv/VLC native window covers the video surface and swallows
    mouse events, so hover-based auto-hide is unreliable — once hidden there
    is no hover event to bring the controls back. Instead, a timer polls the
    global cursor position (QCursor.pos() reads it at OS level, unaffected
    by the native window) and reveals the bar on any movement."""

    # Backend callbacks fire on libmpv/libVLC event threads — touching Qt
    # widgets from there is undefined behavior, so they only emit these
    # signals; Qt queues delivery to the GUI thread automatically.
    sig_state = Signal(str)
    sig_position = Signal(float, float)
    sig_tracks = Signal()
    sig_error = Signal(str)
    # Internal error delivery carries the playback generation/attempt active
    # when the backend reported it.  The public sig_error stays string-only for
    # existing IPTVTab/agent consumers.
    sig_attempt_error = Signal(str, int, int)
    sig_retry = Signal(int, int)  # (attempt, max_retries) — auto-retry fired
    sig_playback_started = Signal()  # real playback detected (not just loadfile)
    sig_recording_status = Signal(str, str)  # worker-safe recorder status
    sig_compact = Signal(bool)  # compact always-on-top host; never reparents surface

    # Seconds the rewind/forward buttons (and ←/→ keys) jump.
    SKIP_SECONDS = 10
    # True on entering fullscreen — the host tab hides its chrome so only the
    # player is visible. (The video widget can't be reparented to its own
    # window: that would recreate the native winId mpv/VLC embed into.)
    sig_fullscreen = Signal(bool)

    def __init__(self, manager: IPTVManager, config: DeeptorrentConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.sig_state.connect(self._on_state)
        self.sig_position.connect(self._on_position)
        self.sig_attempt_error.connect(self._on_error)
        self.sig_tracks.connect(self._apply_preferred_languages)
        self.sig_recording_status.connect(self._on_recording_status)
        self._langs_applied_url = ""
        self._manager = manager
        self._config = config
        self._engine: Any = None          # TorrentEngine, set via set_engine()
        self._throttled = False
        self._saved_limits = (0, 0)
        self._backend: Optional[PlayerBackend] = None   # currently active
        self._media_backend: Optional[PlayerBackend] = None  # mpv/VLC
        self._media_backend_svp = False   # iptv.svp_enabled value at backend creation
        self._backend_recreate_on_play = False  # SVP toggle pending (see apply_config)
        self._milkdrop: Optional[Any] = None            # Butterchurn (audio)
        self._current_item: Any = None
        # A generation identifies a user play/stop lifecycle; attempt_id
        # identifies each backend.play within it.  At most one retry may claim
        # an attempt, whether the claimant is the loading watchdog or the
        # backend error callback.
        self._playback_generation = 0
        self._attempt_id = 0
        self._retry_count = 0
        self._retry_scheduled_for: Optional[tuple[int, int]] = None
        self._error_retry_pending = False  # compatibility/readability mirror
        self._last_position = 0.0
        self._last_duration = 0.0
        self._pending_resume = 0.0
        self._resume_applied = False
        self._watched_marked = False
        self._playback_active = False
        self._recorder: Optional[StreamRecorder] = None
        self._sleep_deadline = 0.0
        self._compact = False
        self._compact_geometry = None
        self._compact_minimum_size: Optional[QSize] = None
        self._compact_control_visibility: Dict[QWidget, bool] = {}
        self._build_ui()
        self._checkpoint_timer = QTimer(self)
        self._checkpoint_timer.setInterval(15_000)
        self._checkpoint_timer.timeout.connect(self._checkpoint_current)
        self._checkpoint_timer.start()
        self._sleep_timer = QTimer(self)
        self._sleep_timer.setSingleShot(True)
        self._sleep_timer.timeout.connect(self._on_sleep_timeout)
        self._sleep_tick = QTimer(self)
        self._sleep_tick.setInterval(1_000)
        self._sleep_tick.timeout.connect(self._update_sleep_label)
        self._aspect_modes = ["auto", "16:9", "4:3", "2.35:1"]
        self._aspect_idx = 0
        # Fullscreen auto-hide state (see class docstring for why polling).
        self._cursor_poll = QTimer(self)
        self._cursor_poll.setInterval(150)
        self._cursor_poll.timeout.connect(self._poll_cursor)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(2500)
        self._hide_timer.timeout.connect(self._hide_controls)
        self._last_cursor = None
        self._controls_hidden = False

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Video surface (the backend embeds into this widget via winId()).
        self.surface = QFrame()
        self.surface.setStyleSheet("background-color: #000000;")
        self.surface.setMinimumHeight(220)
        self.surface.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # The MilkDrop web view becomes a second page here rather than a child
        # of the surface: a native child would make Qt re-create the surface's
        # window handle and kill the embedded mpv instance.
        self.video_stack = QStackedWidget()
        self.video_stack.addWidget(self.surface)
        layout.addWidget(self.video_stack, 1)

        # Control bar (auto-hiding).
        self.controls = QWidget()
        self.controls.setStyleSheet("background-color: rgba(10,10,15,0.85);")
        controls_layout = QVBoxLayout(self.controls)
        controls_layout.setContentsMargins(8, 4, 8, 4)
        controls_layout.setSpacing(3)
        ctrl = QHBoxLayout()
        tools = QHBoxLayout()
        controls_layout.addLayout(ctrl)

        # Rewind/forward start disabled: meaningless for live streams and
        # before anything is loaded — _on_position enables them for VOD.
        self.rw_btn = QPushButton("⏪")
        self.rw_btn.setToolTip(f"Back {self.SKIP_SECONDS}s (←)")
        self.rw_btn.setEnabled(False)
        self.rw_btn.clicked.connect(lambda: self._skip(-self.SKIP_SECONDS))
        ctrl.addWidget(self.rw_btn)

        self.play_btn = QPushButton("⏸")
        self.play_btn.setToolTip("Play/Pause (Space)")
        self.play_btn.clicked.connect(self._toggle_pause)
        ctrl.addWidget(self.play_btn)

        self.stop_btn = QPushButton("⏹")
        self.stop_btn.setToolTip("Stop")
        self.stop_btn.clicked.connect(self.stop)
        ctrl.addWidget(self.stop_btn)

        self.ff_btn = QPushButton("⏩")
        self.ff_btn.setToolTip(f"Forward {self.SKIP_SECONDS}s (→)")
        self.ff_btn.setEnabled(False)
        self.ff_btn.clicked.connect(lambda: self._skip(self.SKIP_SECONDS))
        ctrl.addWidget(self.ff_btn)

        self.seek = QSlider(Qt.Horizontal)
        self.seek.setToolTip("Seek")
        self.seek.sliderReleased.connect(self._on_seek)
        ctrl.addWidget(self.seek, 1)

        self.time_lbl = QLabel("00:00 / 00:00")
        self.time_lbl.setStyleSheet("color: #c8d3e0;")
        ctrl.addWidget(self.time_lbl)

        self.mute_btn = QPushButton("�" if self._config.iptv.muted else "�🔊")
        self.mute_btn.setToolTip("Mute (M)")
        self.mute_btn.clicked.connect(self._toggle_mute)
        ctrl.addWidget(self.mute_btn)

        self.vol = QSlider(Qt.Horizontal)
        self.vol.setMaximumWidth(90)
        self.vol.setMaximum(100)
        self.vol.setValue(max(0, min(100, self._config.iptv.volume)))
        self.vol.valueChanged.connect(self._on_volume)
        ctrl.addWidget(self.vol)

        self.aspect_btn = QPushButton("⛶")
        self.aspect_btn.setToolTip("Cycle aspect ratio (A)")
        self.aspect_btn.clicked.connect(self._cycle_aspect)
        ctrl.addWidget(self.aspect_btn)

        self.audio_btn = QPushButton("🎧")
        self.audio_btn.setToolTip("Audio track (# cycles)")
        self.audio_btn.clicked.connect(lambda: self._show_track_menu("audio"))
        ctrl.addWidget(self.audio_btn)

        self.subs_btn = QPushButton("CC")
        self.subs_btn.setToolTip("Subtitles (J cycles)")
        self.subs_btn.clicked.connect(lambda: self._show_track_menu("sub"))
        ctrl.addWidget(self.subs_btn)

        # Only meaningful while the MilkDrop visualizer is on screen.
        self.preset_btn = QPushButton("🌀")
        self.preset_btn.setToolTip("MilkDrop preset")
        self.preset_btn.clicked.connect(self._show_preset_menu)
        self.preset_btn.hide()
        ctrl.addWidget(self.preset_btn)

        self.record_btn = QPushButton("Start Recording")
        self.record_btn.setToolTip(
            "Record the currently playing network stream with FFmpeg")
        self.record_btn.clicked.connect(self._toggle_recording)
        tools.addWidget(self.record_btn)

        self.record_status_lbl = QLabel("")
        self.record_status_lbl.setStyleSheet("color: #e67e22; font-size: 11px;")
        self.record_status_lbl.setMaximumWidth(180)
        self.record_status_lbl.hide()
        tools.addWidget(self.record_status_lbl, 1)

        self.sleep_btn = QPushButton("Sleep: Off")
        self.sleep_btn.setToolTip("Stop playback after a chosen time")
        self.sleep_btn.clicked.connect(self._show_sleep_menu)
        tools.addWidget(self.sleep_btn)

        self.compact_btn = QPushButton("Compact")
        self.compact_btn.setToolTip(
            "Compact always-on-top host mode (keeps the native player surface in place)")
        self.compact_btn.clicked.connect(self._toggle_compact)
        tools.addWidget(self.compact_btn)

        self.fs_btn = QPushButton("⛶ Full")
        self.fs_btn.setToolTip("Fullscreen (F or double-click)")
        self.fs_btn.clicked.connect(self._toggle_fullscreen)
        ctrl.addWidget(self.fs_btn)
        controls_layout.addLayout(tools)

        layout.addWidget(self.controls)

        # Error overlay (hidden by default).
        self.error_overlay = QWidget(self.surface)
        self.error_overlay.setStyleSheet("background-color: rgba(10,10,15,0.95);")
        el = QVBoxLayout(self.error_overlay)
        el.setSpacing(12)
        self.error_icon = QLabel("⚠")
        self.error_icon.setStyleSheet("color: #e67e22; font-size: 48px; font-weight: bold;")
        self.error_icon.setAlignment(Qt.AlignCenter)
        el.addWidget(self.error_icon)
        self.error_title = QLabel("Stream Unavailable")
        self.error_title.setStyleSheet("color: #ff6b6b; font-size: 18px; font-weight: bold;")
        self.error_title.setAlignment(Qt.AlignCenter)
        el.addWidget(self.error_title)
        self.error_lbl = QLabel("")
        self.error_lbl.setStyleSheet("color: #c8d3e0; font-size: 13px;")
        self.error_lbl.setAlignment(Qt.AlignCenter)
        self.error_lbl.setWordWrap(True)
        el.addWidget(self.error_lbl)
        self.error_hint = QLabel("")
        self.error_hint.setStyleSheet("color: #8a9ab0; font-size: 12px;")
        self.error_hint.setAlignment(Qt.AlignCenter)
        self.error_hint.setWordWrap(True)
        el.addWidget(self.error_hint)
        self.retry_btn = QPushButton("↻ Retry")
        self.retry_btn.setStyleSheet(
            "QPushButton { background-color: #2a7abf; color: white; "
            "border: none; border-radius: 6px; padding: 8px 24px; "
            "font-size: 14px; font-weight: bold; }\n"
            "QPushButton:hover { background-color: #e67e22; }"
        )
        self.retry_btn.clicked.connect(self._retry)
        el.addWidget(self.retry_btn, 0, Qt.AlignCenter)
        self.error_overlay.hide()

        # Loading / buffering overlay (shown until the first frame is ready).
        # raise_() is called on show so it sits above the native mpv/VLC HWND.
        self.loading_overlay = QWidget(self.surface)
        self.loading_overlay.setStyleSheet("background-color: rgba(10,10,15,0.94);")
        ll = QVBoxLayout(self.loading_overlay)
        self.loading_lbl = QLabel("Opening stream…")
        self.loading_lbl.setStyleSheet(
            "color: #2a7abf; font-size: 20px; font-weight: bold;")
        self.loading_lbl.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.loading_lbl)
        self.loading_progress = QProgressBar()
        self.loading_progress.setMaximumWidth(320)
        self.loading_progress.setMinimumHeight(22)
        self.loading_progress.setTextVisible(True)
        self.loading_progress.setAlignment(Qt.AlignCenter)
        self.loading_progress.setStyleSheet(
            "QProgressBar { color: #c8d3e0; background-color: #1a2a4a; "
            "border: 1px solid #2a7abf; border-radius: 6px; text-align: center; "
            "font-size: 12px; }\n"
            "QProgressBar::chunk { background-color: #2a7abf; border-radius: 5px; }"
        )
        ll.addWidget(self.loading_progress, 0, Qt.AlignCenter)
        self.loading_detail = QLabel("")
        self.loading_detail.setStyleSheet("color: #8a9ab0; font-size: 13px;")
        self.loading_detail.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.loading_detail)
        self.loading_overlay.hide()

        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(250)
        self._loading_timer.timeout.connect(self._update_loading)
        self._loading_started = 0.0

        # Mid-playback stall badge: once the startup overlay is gone, a
        # cache starvation pause still needs a visible cue (and feeds the
        # adaptive cache ramp below).
        self.buffer_badge = QLabel("⏳ Buffering…", self.surface)
        self.buffer_badge.setAlignment(Qt.AlignCenter)
        self.buffer_badge.setStyleSheet(
            "background-color: rgba(10,10,15,0.82); color: #e67e22; "
            "border-radius: 12px; padding: 6px 18px; font-size: 13px; "
            "font-weight: bold;")
        self.buffer_badge.hide()
        self._stall_count = 0
        self._adaptive_cache_secs = 0

    # -- backend lifecycle ---------------------------------------------------
    def _ensure_backend(self) -> bool:
        """Create the media backend (mpv/VLC). MilkDrop is a second, lazily
        created backend that takes over for audio files — see
        :meth:`_play_with_milkdrop`."""
        if self._media_backend is None:
            svp_on = self._prepare_svp()
            mb = create_backend(self.surface, preferred=self._config.iptv.preferred_player,
                                svp=svp_on)
            if mb is None:
                self._show_error("No playback backend available. See Settings → IPTV.")
                return False
            # Snapshot the SETTING (not whether the backend honors it) so a
            # VLC backend doesn't retrigger the recreate flag every time
            # apply_config runs while SVP is enabled.
            self._media_backend_svp = bool(self._config.iptv.svp_enabled)
            self._wire_backend(mb)
            mb.set_hwdec(self._config.iptv.hwdec)
            mb.set_cache(self._config.iptv.cache_seconds)
            mb.set_overscan(self._config.iptv.overscan_pct)
            mb.set_interpolation(self._config.iptv.interpolation)
            mb.set_audio_delay(self._config.iptv.audio_delay)
            self._media_backend = mb
        if self._backend is None:
            self._backend = self._media_backend
        return True

    def _prepare_svp(self) -> bool:
        """Arm SVP 4 motion interpolation for the next mpv backend creation.

        Returns False (playback simply goes on without SVP) when the setting
        is off, no SVP 4 install is found, or the Manager won't start. SVP's
        mpv options are baked in at creation, so this only runs from
        :meth:`_ensure_backend`."""
        if not self._config.iptv.svp_enabled:
            return False
        from iptv import svp
        inst = svp.find_install()
        if inst is None:
            logger.warning("SVP enabled in settings but no SVP 4 install found")
            return False
        svp.prepare_environment(inst)
        # The manager (re)start polls (kill wait + boot wait) — run it off
        # the GUI thread; SVP attaches mid-playback once it's up, which is
        # its normal late-discovery behavior.
        threading.Thread(target=svp.ensure_manager, args=(inst,), daemon=True).start()
        return True

    def _teardown_media_backend(self) -> None:
        """Destroy the mpv/VLC backend so the next play() re-creates it.

        Needed for settings that are baked into mpv at creation (SVP mode);
        deferred to the next play() so a toggle never kills active playback."""
        mb, self._media_backend = self._media_backend, None
        if self._backend is mb:
            self._backend = None
        if mb is not None:
            try:
                mb.stop()
                mb.destroy()
            except Exception:
                logger.debug("media backend teardown failed", exc_info=True)

    def _wire_backend(self, backend: PlayerBackend) -> None:
        backend.on_state = self.sig_state.emit
        backend.on_position = self.sig_position.emit
        backend.on_error = self._emit_backend_error
        backend.on_tracks = lambda _t: self.sig_tracks.emit()
        # Apply the persisted audio state to the fresh backend.
        backend.set_volume(self.vol.value())
        if self._config.iptv.muted:
            backend.set_mute(True)

    def _ensure_milkdrop(self) -> Optional[Any]:
        """The Butterchurn backend, created on first use (it costs a
        QWebEngineView, so audio-less sessions never pay for it)."""
        if self._milkdrop is None:
            from gui.milkdrop import MilkdropBackend
            md = MilkdropBackend(self)
            if not md.create():
                return None
            self._wire_backend(md)
            self.video_stack.addWidget(md.view)  # sibling, never a child
            self._milkdrop = md
        return self._milkdrop

    def _use_backend(self, backend: Any) -> None:
        """Switch the active backend, showing its page and stopping the other."""
        if backend is None:
            # Nothing to switch to — still leave the video surface up rather
            # than a stale visualizer.
            self.video_stack.setCurrentWidget(self.surface)
            self.preset_btn.setVisible(False)
            return
        if self._backend is backend:
            return
        other = self._backend
        if other is not None:
            try:
                other.stop()
            except Exception:
                logger.debug("stopping previous backend failed", exc_info=True)
        on_milkdrop = self._milkdrop is not None and backend is self._milkdrop
        if on_milkdrop and self._milkdrop.view is not None:
            self.video_stack.setCurrentWidget(self._milkdrop.view)
        else:
            self.video_stack.setCurrentWidget(self.surface)
        self.preset_btn.setVisible(on_milkdrop)
        self._backend = backend

    def _play_with_milkdrop(self, item: Any) -> bool:
        """Route audio files to the Butterchurn visualizer.

        Returns False for anything it can't take (video, streams, formats
        Chromium can't decode) so the caller falls back to mpv."""
        from gui import milkdrop as md
        if not self._config.iptv.milkdrop_enabled:
            return False
        url = getattr(item, "url", "")
        if not md.is_audio_url(url):
            return False
        backend = self._ensure_milkdrop()
        if backend is None:
            return False
        # Chromium refuses some files only once it tries to decode them;
        # when that happens, replay through mpv instead.
        backend.on_unsupported = lambda: self.play(item, allow_milkdrop=False)
        self._use_backend(backend)
        backend.set_preset(self._selected_preset(), blend=0.0)
        backend.play(url)
        self._manager.record_recent(item)
        self.play_btn.setText("⏸")
        self._hide_loading()
        return True

    def _show_preset_menu(self) -> None:
        """Switch MilkDrop preset on the fly (blended, like MilkDrop does)."""
        from gui import milkdrop as md
        if self._milkdrop is None:
            return
        presets = md.list_presets()
        menu = QMenu(self)
        current = self._milkdrop.current_preset()
        for path in presets:
            act = menu.addAction(md.preset_name(path))
            act.setCheckable(True)
            act.setChecked(path == current)
            act.triggered.connect(lambda _c=False, p=path: self._pick_preset(p))
        if not presets:
            menu.addAction("No .milk presets found").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Open presets folder…").triggered.connect(self._open_preset_folder)
        menu.exec(self.preset_btn.mapToGlobal(self.preset_btn.rect().bottomLeft()))

    def _pick_preset(self, path: str) -> None:
        if self._milkdrop is not None:
            self._milkdrop.set_preset(path, blend=2.0)
        self._config.iptv.milkdrop_preset = os.path.basename(path)

    def _open_preset_folder(self) -> None:
        """Reveal the user preset folder (created on demand) in Explorer."""
        from gui import milkdrop as md
        target = md.user_preset_dir()
        os.makedirs(target, exist_ok=True)
        shipped = md.shipped_preset_dir()
        readme = os.path.join(target, "README.txt")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as fh:
                fh.write("Drop MilkDrop .milk preset files here — they show up in the\n"
                         "player's preset menu (the spiral button) after a restart.\n"
                         "Presets shipped with DeepFlux live in:\n  " + shipped + "\n"
                         "More presets: https://milkdrop.org\n")
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _selected_preset(self) -> str:
        """Path of the configured .milk preset (or the first available one)."""
        from gui import milkdrop as md
        presets = md.list_presets()
        if not presets:
            return ""
        want = self._config.iptv.milkdrop_preset
        for p in presets:
            if os.path.basename(p) == want:
                return p
        return presets[0]

    # -- torrent throttling while streaming -----------------------------------
    def set_engine(self, engine: Any) -> None:
        """Give the player access to the torrent engine for QoS throttling."""
        self._engine = engine

    @staticmethod
    def _effective_limit(current_kb: int, cap_kb: int) -> int:
        """Only ever LOWER a limit; 0 (unlimited) is treated as infinite."""
        if cap_kb <= 0:
            return current_kb
        if current_kb <= 0:
            return cap_kb
        return min(current_kb, cap_kb)

    def _throttle_torrents(self) -> None:
        """Cap torrent rates while streaming so the video doesn't starve."""
        if self._throttled or self._engine is None or not self._config.iptv.throttle_torrents:
            return
        self._saved_limits = (
            self._config.torrents.download_rate_limit_kb,
            self._config.torrents.upload_rate_limit_kb,
        )
        dl = self._effective_limit(self._saved_limits[0], self._config.iptv.throttle_download_kb)
        ul = self._effective_limit(self._saved_limits[1], self._config.iptv.throttle_upload_kb)
        if (dl, ul) == self._saved_limits:
            return  # already at or below the caps — nothing to do
        self._throttled = True
        logger.info("IPTV playing — throttling torrents to %d/%d KB/s", dl, ul)

        def _work() -> None:
            try:
                self._engine.set_rate_limits(dl, ul)
            except Exception:
                logger.debug("throttle failed", exc_info=True)

        threading.Thread(target=_work, daemon=True).start()

    def _restore_torrent_rates(self) -> None:
        """Restore the user's torrent rate limits after playback stops."""
        if not self._throttled or self._engine is None:
            return
        self._throttled = False
        dl, ul = self._saved_limits
        logger.info("IPTV stopped — restoring torrent limits to %d/%d KB/s", dl, ul)

        def _work() -> None:
            try:
                self._engine.set_rate_limits(dl, ul)
            except Exception:
                logger.debug("restore limits failed", exc_info=True)

        threading.Thread(target=_work, daemon=True).start()

    # -- playback ------------------------------------------------------------
    # Auto-retry: re-hit the server every _RETRY_INTERVAL seconds if the stream
    # hasn't started, up to _MAX_RETRIES times. IPTV servers often need several
    # connection attempts (load balancing, connection limits, transient drops)
    # — this automates the "click Play a few times until it works" pattern.
    _RETRY_INTERVAL = 10  # seconds between stuck-loading retry attempts
    _ERROR_RETRY_INTERVAL = 1.0  # seconds between error retry attempts
    _MAX_RETRIES = 5      # total attempts before giving up

    def play(self, item: Any, allow_milkdrop: bool = True) -> None:
        # Persist the outgoing VOD/episode before any backend stop callback can
        # race with the item switch. Live channels are ignored by the manager.
        if self._current_item is not None:
            self._checkpoint_current()
            if item is not self._current_item:
                self._stop_recording()
        if self._backend_recreate_on_play:
            self._backend_recreate_on_play = False
            self._teardown_media_backend()
        if not self._ensure_backend():
            return
        self._throttle_torrents()
        # Invalidate every callback/timer belonging to the previous item before
        # touching its backend connection.
        self._playback_generation += 1
        self._attempt_id = 0
        self._retry_count = 0
        self._retry_scheduled_for = None
        self._error_retry_pending = False
        # Explicitly stop the previous stream before opening a new one.
        # Without this, mpv/VLC replaces the file internally but the old
        # HTTP/TCP connection to the IPTV provider lingers — providers
        # count these as concurrent connections and reject new ones with
        # "connection limit reached" after a few rapid channel switches.
        # The stop must happen BEFORE the new URL is loaded so the old
        # socket is torn down first.
        if self._current_item is not None and self._backend is not None:
            try:
                self._backend.stop()
            except Exception:
                logger.debug("stop before new play failed", exc_info=True)
        self._current_item = item
        self._last_position = 0.0
        self._last_duration = 0.0
        self._pending_resume = self._manager.resume_position(item)
        self._resume_applied = False
        self._watched_marked = False
        self._playback_active = True
        self._langs_applied_url = ""  # new file — re-apply preferred languages
        self._reset_stall_state()
        self.error_overlay.hide()
        if allow_milkdrop and self._play_with_milkdrop(item):
            return
        self._use_backend(self._media_backend)
        self._show_loading()
        # Live TV runs on mpv's default audio-clock sync: display-resample
        # (+interpolation) assumes a seekable, steadily-timestamped source,
        # and on live streams it makes playback stall and restart.
        is_live = self._manager.is_live_item(item)
        self._backend.set_smooth_video(not is_live)
        # Re-apply the ordinary startup target first, then opt live playback
        # into mpv's bounded volatile back-buffer. Other backends no-op safely.
        self._backend.set_cache(self._config.iptv.cache_seconds)
        self._backend.set_live_pause_buffer(
            self._config.iptv.live_pause_buffer_seconds if is_live else 0)
        self._start_playback()
        self._manager.record_recent(item)
        self.play_btn.setText("⏸")
        # VLC has no track-list observer — poll once shortly after load.
        QTimer.singleShot(2500, self._apply_preferred_languages)

    def _playback_headers(self, item: Any) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        src = self._manager.active_source()
        if src and src.user_agent:
            headers["User-Agent"] = src.user_agent
        if src and src.referer:
            headers["Referer"] = src.referer
        # Per-entry #EXTVLCOPT headers from the playlist override source-level
        # ones (some streams require a specific UA/Referer).
        opts = (getattr(item, "extra", None) or {}).get("extvlcopt", [])
        if isinstance(opts, str):
            opts = [opts]
        for opt in opts:
            k, _, v = str(opt).partition("=")
            k = k.strip().lower()
            if k == "http-user-agent" and v.strip():
                headers["User-Agent"] = v.strip()
            elif k == "http-referrer" and v.strip():
                headers["Referer"] = v.strip()
        # Arbitrary headers passed by web-stream playback (cookies, origin...).
        extra_headers = (getattr(item, "extra", None) or {}).get("headers")
        if isinstance(extra_headers, dict):
            headers.update({str(k): str(v) for k, v in extra_headers.items() if v})
        return headers

    def _start_playback(self, generation: Optional[int] = None) -> None:
        """Start one uniquely numbered backend attempt for this generation."""
        if generation is not None and generation != self._playback_generation:
            return
        item = self._current_item
        if item is None or self._backend is None:
            return
        self._attempt_id += 1
        self._retry_scheduled_for = None
        self._error_retry_pending = False
        self._loading_started = time.monotonic()
        self._backend.play(
            getattr(item, "url", ""), headers=self._playback_headers(item))

    def _retry(self) -> None:
        if self._current_item:
            self.play(self._current_item)

    def _toggle_pause(self) -> None:
        if self._backend is None:
            return
        if self._backend.is_playing:
            self._backend.pause()
            self.play_btn.setText("▶")
        else:
            self._backend.resume()
            self.play_btn.setText("⏸")

    def stop(self) -> None:
        self._checkpoint_current()
        self._stop_recording()
        self._playback_active = False
        self._cancel_sleep_timer()
        self._playback_generation += 1  # invalidate queued errors/retries
        self._retry_scheduled_for = None
        self._error_retry_pending = False
        self._retry_count = 0
        if self._backend is not None:
            self._backend.stop()
        self._hide_loading()
        self._restore_torrent_rates()
        self.play_btn.setText("▶")
        self.seek.setValue(0)
        self.rw_btn.setEnabled(False)
        self.ff_btn.setEnabled(False)
        self.time_lbl.setText("00:00 / 00:00")

    def _checkpoint_current(self) -> None:
        item = self._current_item
        if item is None:
            return
        self._manager.update_watch_progress(
            item, self._last_position, self._last_duration)

    # -- explicit network-stream recording ---------------------------------
    def _toggle_recording(self) -> None:
        if self._recorder is not None and self._recorder.is_recording:
            self._stop_recording()
            return
        item = self._current_item
        url = getattr(item, "url", "") if item is not None else ""
        if not self._playback_active or not is_network_stream_url(url):
            self._on_recording_status(
                "error", "Play a network stream before starting a recording.")
            return
        ffmpeg = find_ffmpeg(self._config.download.ffmpeg_path)
        self._recorder = StreamRecorder(
            ffmpeg,
            self._config.iptv.recording_dir,
            on_status=self.sig_recording_status.emit,
        )
        name = getattr(item, "name", "") or getattr(item, "display_name", "")
        ok, detail = self._recorder.start(
            url, name or "IPTV Recording", self._playback_headers(item))
        if not ok:
            self._on_recording_status("error", detail)

    def _stop_recording(self) -> None:
        if self._recorder is not None and self._recorder.stop():
            self.record_btn.setEnabled(False)
            self.record_status_lbl.setText("Stopping recording…")
            self.record_status_lbl.show()

    def _on_recording_status(self, state: str, message: str) -> None:
        if state == "recording":
            self.record_btn.setText("Stop Recording")
            self.record_btn.setEnabled(True)
            colour = "#ff6b6b"
        elif state == "error":
            self.record_btn.setText("Start Recording")
            self.record_btn.setEnabled(True)
            colour = "#ff6b6b"
        else:
            self.record_btn.setText("Start Recording")
            self.record_btn.setEnabled(True)
            colour = "#7fd1b9"
        self.record_status_lbl.setStyleSheet(f"color: {colour}; font-size: 11px;")
        self.record_status_lbl.setText(message)
        self.record_status_lbl.setToolTip(message)
        self.record_status_lbl.setVisible(bool(message))

    # -- sleep timer ---------------------------------------------------------
    def _show_sleep_menu(self) -> None:
        menu = QMenu(self)
        for minutes in (15, 30, 45, 60, 90, 120):
            menu.addAction(f"Stop in {minutes} minutes").triggered.connect(
                lambda _checked=False, m=minutes: self._set_sleep_minutes(m))
        menu.addSeparator()
        cancel = menu.addAction("Cancel sleep timer")
        cancel.setEnabled(self._sleep_timer.isActive())
        cancel.triggered.connect(self._cancel_sleep_timer)
        menu.exec(self.sleep_btn.mapToGlobal(self.sleep_btn.rect().bottomLeft()))

    def _set_sleep_minutes(self, minutes: int) -> None:
        seconds = max(1, int(minutes) * 60)
        self._sleep_deadline = time.monotonic() + seconds
        self._sleep_timer.start(seconds * 1000)
        self._sleep_tick.start()
        self._update_sleep_label()

    def _update_sleep_label(self) -> None:
        if not self._sleep_timer.isActive() or not self._sleep_deadline:
            return
        remaining = max(0, int(self._sleep_deadline - time.monotonic() + 0.999))
        self.sleep_btn.setText(
            f"Sleep: {remaining // 60:02d}:{remaining % 60:02d}")

    def _cancel_sleep_timer(self) -> None:
        self._sleep_timer.stop()
        self._sleep_tick.stop()
        self._sleep_deadline = 0.0
        self.sleep_btn.setText("Sleep: Off")

    def _on_sleep_timeout(self) -> None:
        self._sleep_tick.stop()
        self._sleep_deadline = 0.0
        self.stop()
        self.sleep_btn.setText("Sleep: Stopped")
        self.sleep_btn.setToolTip("Sleep timer expired and stopped playback")

    # -- safe compact host (PiP alternative) --------------------------------
    @staticmethod
    def compact_mode_supported() -> bool:
        """Only Windows offers a no-reparent topmost API used by this mode.

        Qt window-flag changes may recreate native child surfaces on other
        platforms, so true PiP is deliberately deferred there rather than
        risking the mpv/VLC HWND/XID.
        """
        return sys.platform == "win32"

    @staticmethod
    def _set_native_topmost(widget: QWidget, on: bool) -> bool:
        if not PlayerWidget.compact_mode_supported():
            return False
        try:
            import ctypes
            from ctypes import wintypes
            set_window_pos = ctypes.WinDLL("user32", use_last_error=True).SetWindowPos
            set_window_pos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT,
            ]
            set_window_pos.restype = wintypes.BOOL
            hwnd = wintypes.HWND(int(widget.winId()))
            insert_after = wintypes.HWND(-1 if on else -2)  # HWND_TOPMOST / HWND_NOTOPMOST
            ok = bool(set_window_pos(
                hwnd, insert_after, 0, 0, 0, 0, 0x0001 | 0x0002))
            if not ok:
                logger.debug("compact topmost mode failed: winerror=%s", ctypes.get_last_error())
            return ok
        except Exception:
            logger.debug("compact topmost mode unavailable", exc_info=True)
            return False

    def _set_compact_controls(self, on: bool) -> None:
        secondary = (
            self.rw_btn, self.ff_btn, self.vol, self.aspect_btn,
            self.audio_btn, self.subs_btn, self.preset_btn, self.record_btn,
            self.record_status_lbl, self.sleep_btn, self.fs_btn,
        )
        if on:
            self._compact_control_visibility = {
                widget: not widget.isHidden() for widget in secondary
            }
            for widget in secondary:
                widget.hide()
        else:
            for widget, visible in self._compact_control_visibility.items():
                widget.setVisible(visible)
            self._compact_control_visibility.clear()

    def _toggle_compact(self) -> None:
        on = not self._compact
        if on and not self.compact_mode_supported():
            self.compact_btn.setToolTip(
                "Compact mode is deferred on this platform because changing Qt\n"
                "window flags can recreate the native mpv/VLC surface.")
            QToolTip.showText(
                self.compact_btn.mapToGlobal(self.compact_btn.rect().topLeft()),
                self.compact_btn.toolTip(), self.compact_btn)
            return
        win = self.window()
        if on:
            self._compact_geometry = win.saveGeometry()
            self._compact_minimum_size = win.minimumSize()
            if not self._set_native_topmost(win, True):
                return
            self._compact = True
            self._set_compact_controls(True)
            self.sig_compact.emit(True)
            win.setMinimumSize(480, 320)
            win.resize(640, 420)
            self.compact_btn.setText("Exit Compact")
        else:
            self._set_native_topmost(win, False)
            self._compact = False
            self._set_compact_controls(False)
            self.sig_compact.emit(False)
            if self._compact_minimum_size is not None:
                win.setMinimumSize(self._compact_minimum_size)
            if self._compact_geometry is not None:
                win.restoreGeometry(self._compact_geometry)
            self.compact_btn.setText("Compact")

    def _on_seek(self) -> None:
        if self._backend is not None:
            self._backend.seek(self.seek.value())

    def _skip(self, delta: float) -> None:
        if self._backend is not None:
            self._backend.seek_by(delta)

    def _on_volume(self, v: int) -> None:
        self._config.iptv.volume = v  # persisted with the config on close
        if self._backend is not None:
            self._backend.set_volume(v)

    def _toggle_mute(self) -> None:
        if self._backend is not None:
            mute = self.mute_btn.text() == "🔊"  # showing 🔊 = currently unmuted
            self._backend.set_mute(mute)
            self.mute_btn.setText("🔇" if mute else "🔊")
            self._config.iptv.muted = mute  # persisted with the config on close

    def _cycle_aspect(self) -> None:
        if self._backend is None:
            return
        self._aspect_idx = (self._aspect_idx + 1) % len(self._aspect_modes)
        self._backend.set_aspect(self._aspect_modes[self._aspect_idx])

    # -- audio / subtitle tracks ---------------------------------------------
    @staticmethod
    def _track_label(t: dict) -> str:
        bits = [b for b in (t.get("title") or "", t.get("lang") or "") if b]
        return f"Track {t['id']}" + (f" — {' · '.join(bits)}" if bits else "")

    def _show_track_menu(self, kind: str) -> None:
        """Popup listing audio/subtitle tracks with the active one checked."""
        if self._backend is None:
            return
        if kind == "audio":
            tracks = self._backend.audio_tracks()
            current = self._backend.current_audio_track()
            btn = self.audio_btn
            entries = [("Auto", "auto")] + [(self._track_label(t), t["id"]) for t in tracks]
        else:
            tracks = self._backend.subtitle_tracks()
            current = self._backend.current_subtitle_track()
            btn = self.subs_btn
            entries = [("Off", "no")] + [(self._track_label(t), t["id"]) for t in tracks]
        menu = QMenu(self)
        if not tracks:
            menu.addAction("No tracks available").setEnabled(False)
        for label, tid in entries:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(str(current) == str(tid))
            act.triggered.connect(lambda _checked=False, t=tid, k=kind: self._set_track(k, t))
        if kind == "audio":
            menu.addSeparator()
            self._add_audio_sync_submenu(menu)
        if kind == "sub":
            menu.addSeparator()
            find_act = menu.addAction("Find subtitles online…")
            find_act.setEnabled(self._current_item is not None)
            find_act.triggered.connect(self._find_subtitles_online)
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    # -- audio sync (offset) -------------------------------------------------
    # SVP 4 motion interpolation adds latency to the video path (frames are
    # synthesized behind the audio clock), so the audio runs ahead of the
    # picture. A positive audio-delay shifts the audio later to realign it.
    _AUDIO_DELAY_STEP = 0.05   # seconds per +/- key nudge
    _AUDIO_DELAY_RANGE = 1.0   # clamp to +/- 1s
    _AUDIO_DELAY_PRESETS = (-0.500, -0.300, -0.200, -0.100, 0.0,
                            0.100, 0.200, 0.300, 0.500)

    @staticmethod
    def _fmt_delay(seconds: float) -> str:
        return f"{seconds:+.2f}s"

    def _current_audio_delay(self) -> float:
        """The effective audio delay: the live backend value if a backend
        exists, otherwise the persisted config default."""
        if self._backend is not None:
            try:
                return self._backend.audio_delay()
            except Exception:
                pass
        return float(self._config.iptv.audio_delay)

    def _add_audio_sync_submenu(self, parent_menu: QMenu) -> None:
        sub = QMenu("Audio sync", parent_menu)
        sub.setToolTip(
            "Shift audio timing to compensate for video-path latency (e.g.\n"
            "SVP 4 motion interpolation, which renders frames behind the\n"
            "audio clock). Positive = audio plays later. Also: +/- keys."
        )
        cur = self._current_audio_delay()
        for secs in self._AUDIO_DELAY_PRESETS:
            act = sub.addAction(self._fmt_delay(secs))
            act.setCheckable(True)
            act.setChecked(abs(cur - secs) < 0.005)
            act.triggered.connect(lambda _c=False, s=secs: self._apply_audio_delay(s))
        sub.addSeparator()
        back_act = sub.addAction("− 0.05s")
        back_act.triggered.connect(lambda: self._nudge_audio_delay(-self._AUDIO_DELAY_STEP))
        fwd_act = sub.addAction("+ 0.05s")
        fwd_act.triggered.connect(lambda: self._nudge_audio_delay(self._AUDIO_DELAY_STEP))
        sub.addSeparator()
        reset_act = sub.addAction("Reset (0.00s)")
        reset_act.triggered.connect(lambda: self._apply_audio_delay(0.0))
        parent_menu.addMenu(sub)

    def _nudge_audio_delay(self, delta: float) -> None:
        if self._backend is None:
            return
        new = max(-self._AUDIO_DELAY_RANGE, min(self._AUDIO_DELAY_RANGE,
                self._current_audio_delay() + delta))
        self._apply_audio_delay(new)

    def _apply_audio_delay(self, seconds: float) -> None:
        if self._backend is None:
            return
        seconds = max(-self._AUDIO_DELAY_RANGE, min(self._AUDIO_DELAY_RANGE, float(seconds)))
        self._backend.set_audio_delay(seconds)
        # Persist so the offset survives restarts and applies to the next file.
        self._config.iptv.audio_delay = seconds
        self._show_audio_delay_osd(seconds)

    def _show_audio_delay_osd(self, seconds: float) -> None:
        QToolTip.showText(
            self.audio_btn.mapToGlobal(self.audio_btn.rect().topLeft()),
            f"Audio delay: {self._fmt_delay(seconds)}",
            self.audio_btn,
        )

    def _find_subtitles_online(self) -> None:
        """Open the OpenSubtitles search dialog for the current video."""
        if self._backend is None or self._current_item is None:
            return
        url = getattr(self._current_item, "url", "")
        name = getattr(self._current_item, "name", "") or os.path.basename(url)
        file_path = url if os.path.isfile(url) else ""
        from iptv.opensubtitles import clean_media_query
        dlg = _SubtitleSearchDialog(
            self._config, file_path, clean_media_query(file_path or name),
            on_loaded=self._backend.add_subtitle_file, parent=self)
        dlg.exec()

    def _set_track(self, kind: str, track_id: Any) -> None:
        if self._backend is None:
            return
        if kind == "audio":
            self._backend.set_audio_track(track_id)
        else:
            self._backend.set_subtitle_track(track_id)

    # -- preferred languages ---------------------------------------------------
    # ISO 639-1 ↔ 639-2 aliases for the common cases mpv/VLC report.
    _LANG_ALIASES = {
        "en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita",
        "pt": "por", "ru": "rus", "ar": "ara", "zh": "zho", "ja": "jpn",
        "ko": "kor", "nl": "nld", "pl": "pol", "sv": "swe", "tr": "tur",
        "ro": "ron", "cs": "ces", "el": "ell", "he": "heb", "hi": "hin",
    }

    @classmethod
    def _lang_matches(cls, track_lang: str, pref: str) -> bool:
        t, p = (track_lang or "").strip().lower(), (pref or "").strip().lower()
        if not t or not p:
            return False
        variants = {p, cls._LANG_ALIASES.get(p, "")}
        variants |= {k for k, v in cls._LANG_ALIASES.items() if v == p}
        return any(t == v or t.startswith(v) for v in variants if v)

    def _apply_preferred_languages(self) -> None:
        """Auto-select the configured audio/subtitle languages once per loaded
        file — tracks appear asynchronously after playback starts (mpv's
        track-list observer / a one-shot timer on VLC)."""
        if self._backend is None or self._current_item is None:
            return
        url = getattr(self._current_item, "url", "")
        if self._langs_applied_url == url:
            return
        audio = self._backend.audio_tracks()
        subs = self._backend.subtitle_tracks()
        if not audio and not subs:
            return  # tracks not loaded yet — observer/timer will retry
        self._langs_applied_url = url
        cfg = self._config.iptv
        if cfg.preferred_audio_lang:
            for t in audio:
                if self._lang_matches(t.get("lang", ""), cfg.preferred_audio_lang):
                    self._backend.set_audio_track(t["id"])
                    break
        if cfg.preferred_sub_lang:
            for t in subs:
                if self._lang_matches(t.get("lang", ""), cfg.preferred_sub_lang):
                    self._backend.set_subtitle_track(t["id"])
                    break

    def _toggle_fullscreen(self) -> None:
        self._set_fullscreen(not self.window().isFullScreen())

    def _set_fullscreen(self, on: bool) -> None:
        win = self.window()
        if on == win.isFullScreen():
            return
        if on:
            win.showFullScreen()
        else:
            win.showNormal()
        self.sig_fullscreen.emit(on)
        self._set_controls_autohide(on)

    # -- fullscreen control auto-hide -----------------------------------------
    def _set_controls_autohide(self, on: bool) -> None:
        if on:
            self._last_cursor = QCursor.pos()
            self._cursor_poll.start()
            self._hide_timer.start()  # hide after the initial idle grace
        else:
            self._cursor_poll.stop()
            self._hide_timer.stop()
            self._show_controls()

    def _poll_cursor(self) -> None:
        pos = QCursor.pos()
        if pos != self._last_cursor:
            self._last_cursor = pos
            self._show_controls()
            self._hide_timer.start()  # restart the countdown after movement

    def _show_controls(self) -> None:
        if self._controls_hidden:
            self._controls_hidden = False
            self.controls.show()
            self.window().unsetCursor()

    def _hide_controls(self) -> None:
        # Only in fullscreen; never mid-drag on the seek slider or while the
        # pointer sits on the bar itself.
        if not self.window().isFullScreen() or self.seek.isSliderDown() or self.controls.underMouse():
            return
        self._controls_hidden = True
        self.controls.hide()
        self.window().setCursor(Qt.BlankCursor)

    # -- backend callbacks (delivered on the GUI thread via the signals) ----
    def _emit_backend_error(self, msg: str) -> None:
        """Tag an off-thread backend error before queueing it to Qt."""
        generation = self._playback_generation
        attempt = self._attempt_id
        self.sig_attempt_error.emit(msg, generation, attempt)
        self.sig_error.emit(msg)

    def _on_state(self, state: str) -> None:
        if state == "stopped":
            # Skip teardown if an error-retry is pending — the failed attempt
            # emits "stopped", but _error_retry will re-show loading and
            # re-hit the server in a moment.
            if not self._error_retry_pending:
                self._checkpoint_current()
                self._playback_active = False
                self._stop_recording()
                self._hide_loading()
                self.buffer_badge.hide()
                self.play_btn.setText("▶")
                self._restore_torrent_rates()
        elif state == "playing":
            # NOTE: mpv emits "playing" on loadfile, before the stream is
            # actually open. Do NOT reset _retry_count here — that would kill
            # the auto-retry loop. The counter is reset in _update_loading
            # when real playback (time-pos advancing) is confirmed.
            self._playback_active = True
            self.play_btn.setText("⏸")
        elif state == "buffering":
            # The overlay is already shown by play(); keep the pause icon so
            # the user can hit space to pause once playback starts.
            self.play_btn.setText("⏸")
            # A stall after the startup overlay is gone is a mid-playback
            # rebuffer — surface it with the badge (never a full overlay,
            # which would blank the frozen last frame).
            if self._playback_active and not self.loading_overlay.isVisible():
                self._on_midplayback_stall()
        elif state == "playing":
            self.buffer_badge.hide()

    def _on_position(self, pos: float, dur: float) -> None:
        self._last_position = max(0.0, float(pos or 0.0))
        self._last_duration = max(0.0, float(dur or 0.0))
        is_live = self._manager.is_live_item(self._current_item)

        # Apply a persisted resume once the backend reports a real duration.
        # Live channels never produce a pending resume in the manager.
        if (not is_live and not self._resume_applied and dur > 0
                and self._pending_resume > 0):
            self._resume_applied = True
            if self._manager.is_meaningful_resume(self._pending_resume, dur):
                if self._backend is not None:
                    self._backend.seek(self._pending_resume)
                QToolTip.showText(
                    self.time_lbl.mapToGlobal(self.time_lbl.rect().topLeft()),
                    f"Resumed at {_fmt_time(self._pending_resume)}", self.time_lbl)

        # Mark watched as soon as the near-completion rule is crossed, rather
        # than waiting for a clean EOF that a flaky network stream may omit.
        if (not is_live and not self._watched_marked
                and self._manager.is_near_completion(pos, dur)):
            self._manager.update_watch_progress(self._current_item, pos, dur)
            self._watched_marked = True

        # Cap slider range to duration for VOD; live streams report 0 duration
        # (seeking is meaningless there, so the slider is disabled).
        if dur > 0 and not is_live:
            self.seek.setEnabled(True)
            self.rw_btn.setEnabled(True)
            self.ff_btn.setEnabled(True)
            self.seek.setMaximum(int(dur))
            # Don't fight the user while they're dragging the slider.
            if not self.seek.isSliderDown():
                self.seek.blockSignals(True)
                self.seek.setValue(int(pos))
                self.seek.blockSignals(False)
        else:
            self.seek.setEnabled(False)
            self.rw_btn.setEnabled(False)
            self.ff_btn.setEnabled(False)
        self.time_lbl.setText(f"{_fmt_time(pos)} / {_fmt_time(dur)}")

    def _on_error(self, msg: str, generation: Optional[int] = None,
                  attempt: Optional[int] = None) -> None:
        """Retry a backend failure once for the attempt that emitted it.

        ``generation``/``attempt`` are attached on the backend thread.  Direct
        calls (including compatibility tests) default to the current attempt.
        """
        generation = self._playback_generation if generation is None else generation
        attempt = self._attempt_id if attempt is None else attempt
        if generation != self._playback_generation or attempt != self._attempt_id:
            logger.debug("Ignoring stale playback error for generation %s attempt %s",
                         generation, attempt)
            return
        self._schedule_retry(
            generation, attempt, "backend error", self._ERROR_RETRY_INTERVAL,
            msg or "The stream could not be opened.")

    def _schedule_retry(self, generation: int, attempt: int, reason: str,
                        delay: float, final_error: str) -> bool:
        """Atomically claim one attempt for retry.

        Both the watchdog and backend errors pass through here, so whichever
        arrives first consumes the retry and the other becomes a no-op.
        """
        if (generation != self._playback_generation
                or attempt != self._attempt_id):
            return False
        token = (generation, attempt)
        if self._retry_scheduled_for == token:
            return False
        if self._current_item is None or self._backend is None:
            self._show_error(final_error)
            return False
        if self._retry_count >= self._MAX_RETRIES:
            self._retry_scheduled_for = None
            self._error_retry_pending = False
            self._show_error(final_error)
            return False

        self._retry_scheduled_for = token
        self._error_retry_pending = True
        self._retry_count += 1
        logger.info("Auto-retry on %s %d/%d for item %s", reason,
                    self._retry_count, self._MAX_RETRIES,
                    getattr(self._current_item, "name", "") or "<unnamed>")
        self.sig_retry.emit(self._retry_count, self._MAX_RETRIES)
        if delay <= 0:
            self._run_scheduled_retry(generation, attempt)
        else:
            QTimer.singleShot(
                int(delay * 1000),
                lambda g=generation, a=attempt: self._run_scheduled_retry(g, a),
            )
        return True

    def _run_scheduled_retry(self, generation: int, attempt: int) -> None:
        """Run a retry only if its generation and failed attempt are current."""
        token = (generation, attempt)
        if (self._retry_scheduled_for != token
                or generation != self._playback_generation
                or attempt != self._attempt_id):
            return
        self._retry_scheduled_for = None
        self._error_retry_pending = False
        if self._current_item is None or self._backend is None:
            return
        self._show_loading()
        self._start_playback(generation)

    def _error_retry(self) -> None:
        """Compatibility entry point for an already-scheduled current retry."""
        token = self._retry_scheduled_for
        if token is not None:
            self._run_scheduled_retry(*token)

    def _show_error(self, msg: str) -> None:
        self._hide_loading()
        # Classify the error so the overlay can show a helpful hint.
        msg_lower = (msg or "").lower()
        if any(k in msg_lower for k in ("timeout", "timed out", "not responding")):
            title, hint = "Server Not Responding", \
                "The stream didn't respond in time. The server may be overloaded or down. Try again in a moment."
        elif any(k in msg_lower for k in ("404", "not found", "no such")):
            title, hint = "Stream Not Found", \
                "This channel or movie may have been removed from the provider. Try refreshing the playlist."
        elif any(k in msg_lower for k in ("403", "forbidden", "unauthorized", "auth", "denied")):
            title, hint = "Access Denied", \
                "The server refused the connection. Your subscription may have expired or the source needs new credentials."
        elif any(k in msg_lower for k in ("dns", "resolve", "host")):
            title, hint = "Connection Failed", \
                "Couldn't reach the server. Check your internet connection or the source URL in Settings."
        elif any(k in msg_lower for k in ("network", "connection", "reset", "refused", "unreachable")):
            title, hint = "Network Error", \
                "The connection was interrupted. This is usually temporary — retry, or try another source."
        else:
            title, hint = "Playback Error", \
                msg or "The stream could not be opened. Try again or pick another item."
        self.error_title.setText(title)
        self.error_lbl.setText(hint)
        item_name = getattr(self._current_item, "name", "") or \
            getattr(self._current_item, "display_name", "") if self._current_item else ""
        self.error_hint.setText(f"Item: {item_name}" if item_name else "")
        self.error_overlay.resize(self.surface.size())
        self.error_overlay.show()
        self.error_overlay.raise_()  # above the native mpv/VLC window

    # -- loading / buffering overlay ----------------------------------------
    def _show_loading(self) -> None:
        """Show the loading overlay and start polling the backend for status."""
        self.loading_lbl.setText("Opening stream…")
        self.loading_detail.setText("")
        self.loading_progress.setRange(0, 0)
        self.loading_progress.setTextVisible(False)
        self.loading_overlay.resize(self.surface.size())
        self.loading_overlay.show()
        self.loading_overlay.raise_()  # above the native mpv/VLC window
        self._loading_started = time.monotonic()
        self._loading_timer.start()

    def _hide_loading(self) -> None:
        """Hide the overlay and stop polling."""
        self._loading_timer.stop()
        self.loading_overlay.hide()

    # -- mid-playback stall badge / adaptive cache ----------------------------
    def _on_midplayback_stall(self) -> None:
        """A cache-starvation pause hit after playback had started.

        Shows the badge, and from the second stall in the same playback
        ramps mpv's forward cache (cache-secs) up so bursts of repeated
        stalls grow the buffer instead of pausing the video each time.
        Live channels with a pause/rewind buffer are skipped: their
        cache-secs already tracks the (larger) live-pause setting, and a
        small ramp value would shrink it.
        """
        self._stall_count += 1
        detail = "waiting for data"
        base = int(self._config.iptv.cache_seconds)
        is_live = self._manager.is_live_item(self._current_item)
        has_live_pause = (is_live
                          and int(self._config.iptv.live_pause_buffer_seconds) > 0)
        if self._stall_count >= 2 and base >= 2 and not has_live_pause:
            target = min(base * min(self._stall_count, 4), 240)
            if target > self._adaptive_cache_secs:
                self._adaptive_cache_secs = target
                if self._backend is not None:
                    # 3 MiB/s covers ~24 Mbit/s streams without being byte-
                    # capped below the requested seconds.
                    self._backend.set_cache(target, max_bytes=target * 3 * 1024 * 1024)
                detail = f"raised buffer to {target}s"
        self.buffer_badge.setText(f"⏳ Buffering… {detail}")
        self._layout_buffer_badge()
        self.buffer_badge.show()
        self.buffer_badge.raise_()  # above the native mpv/VLC window

    def _layout_buffer_badge(self) -> None:
        """Center the stall badge near the top of the video surface."""
        self.buffer_badge.adjustSize()
        w = self.surface.width()
        self.buffer_badge.move(max(0, (w - self.buffer_badge.width()) // 2), 12)

    def _reset_stall_state(self) -> None:
        """Forget stall history / adaptive cache for a new playback."""
        self._stall_count = 0
        self._adaptive_cache_secs = 0
        self.buffer_badge.hide()

    def _update_loading(self) -> None:
        """Poll the backend and update the overlay text/progress.

        Tries to show realistic stages: stream open / network handshake /
        cache fill, then hides once playback has actually started. The label
        colour shifts blue → yellow → red as time passes with no progress so
        the user can tell a stuck stream from a slow one.

        Auto-retry: if no cache progress has been made after _RETRY_INTERVAL
        seconds, re-hit the server (call backend.play again). This repeats up
        to _MAX_RETRIES times — IPTV servers often need several attempts.
        """
        if self._backend is None:
            self._hide_loading()
            return
        elapsed = time.monotonic() - self._loading_started
        status = self._backend.buffer_status()
        state = status.get("state") or ""
        pct = status.get("percent", -1)
        pfc = bool(status.get("paused_for_cache"))
        tpos = float(status.get("time_pos") or 0.0)
        dcd = float(status.get("demuxer_cache_duration") or 0.0)
        core_idle = bool(status.get("core_idle"))

        # Once we have actual playback time and the player isn't stalled for
        # cache, the stream is really playing. This is the reliable check —
        # the backend's "playing" state fires on loadfile, before the stream
        # is actually open, so we can't use sig_state for this.
        if (tpos > 0.0 or state == "playing") and not pfc and not core_idle:
            self._retry_count = 0  # real playback — reset for later failures
            self._retry_scheduled_for = None
            self._error_retry_pending = False
            self._hide_loading()
            self.sig_playback_started.emit()
            return

        # Live streams sometimes report time-pos == 0 for a few moments even
        # though the cache is full and playing. Give up after a short grace.
        if (pct == 100 and not pfc and not core_idle and dcd > 0.0
                and elapsed > 5.0):
            self._retry_count = 0
            self._retry_scheduled_for = None
            self._error_retry_pending = False
            self._hide_loading()
            self.sig_playback_started.emit()
            return

        no_progress = (pct == 0 or pct < 0) and not dcd

        # Auto-retry: re-hit the server every _RETRY_INTERVAL seconds if there
        # has been zero cache progress. This is the automated equivalent of
        # the user clicking Play again — many IPTV servers need 2-3 attempts.
        if no_progress and elapsed > self._RETRY_INTERVAL:
            generation, attempt = self._playback_generation, self._attempt_id
            handled = self._schedule_retry(
                generation, attempt, "loading watchdog", 0,
                "timeout: stream not responding after "
                f"{self._MAX_RETRIES} retries",
            )
            if handled:
                # _start_playback reset the attempt clock.  Return so this tick
                # cannot continue rendering status from the failed attempt.
                return
            if not self.loading_overlay.isVisible():
                return

        if pct >= 0:
            self.loading_progress.setRange(0, 100)
            self.loading_progress.setValue(pct)
            self.loading_progress.setTextVisible(True)
        else:
            # No per-cache percentage yet: keep the bar as a busy indicator.
            self.loading_progress.setRange(0, 0)
            self.loading_progress.setTextVisible(False)

        # Colour cue: blue (normal) → yellow (slow, >8s) → red (stuck, >20s).
        if elapsed > 20 and no_progress:
            colour = "#ff6b6b"  # red — likely stuck
        elif elapsed > 8 and no_progress:
            colour = "#e67e22"  # orange — slow
        else:
            colour = "#2a7abf"  # blue — normal

        # Choose a stage label that matches what's actually happening.
        if self._retry_count > 0 and no_progress:
            label = f"Retrying… attempt {self._retry_count}/{self._MAX_RETRIES}"
            detail = "reconnecting to server"
        elif state == "stopped" or core_idle:
            label = "Opening stream…"
            detail = ""
        elif pct == 0 or (pct < 0 and not dcd):
            label = "Handshaking…"
            detail = "negotiating stream"
        elif pfc or (0 < pct < 100):
            if pfc:
                label = "Buffering…"
                detail = f"{dcd:.1f}s buffered" if dcd else ""
            else:
                label = "Buffering…"
                detail = f"{pct}%"
        else:
            # mpv reports full cache but hasn't produced a frame yet.
            label = "Starting playback…"
            detail = ""

        self.loading_lbl.setStyleSheet(
            f"color: {colour}; font-size: 20px; font-weight: bold;")
        self.loading_lbl.setText(f"{label}  {int(elapsed)}s")
        self.loading_detail.setText(detail)

    # -- settings live-apply -------------------------------------------------
    def apply_config(self) -> None:
        """Re-apply player settings (buffer, hwdec, overscan, interpolation) to a live backend."""
        if bool(self._config.iptv.svp_enabled) != self._media_backend_svp:
            # SVP mode is baked into mpv at creation (IPC pipe + copy-back
            # hwdec) — rebuild the backend when the next file plays.
            self._backend_recreate_on_play = True
        if self._media_backend is not None:
            # Re-applying the base cache drops any adaptive ramp from
            # mid-playback stalls — restart it from the configured value.
            self._adaptive_cache_secs = 0
            self._media_backend.set_cache(self._config.iptv.cache_seconds)
            self._media_backend.set_live_pause_buffer(
                self._config.iptv.live_pause_buffer_seconds
                if self._manager.is_live_item(self._current_item) else 0)
            self._media_backend.set_hwdec(self._config.iptv.hwdec)
            self._media_backend.set_overscan(self._config.iptv.overscan_pct)
            self._media_backend.set_interpolation(self._config.iptv.interpolation)
            self._media_backend.set_audio_delay(self._config.iptv.audio_delay)
        if self._milkdrop is not None:
            self._milkdrop.set_preset(self._selected_preset())
        if self._backend is not None:
            # Preferred languages changed? Re-run selection on the live file.
            self._langs_applied_url = ""
            self._apply_preferred_languages()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._toggle_fullscreen()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Keep the overlays covering the video surface after resizes, and on
        # top of the native mpv/VLC window (raise_ — native HWND z-order).
        if self.error_overlay.isVisible():
            self.error_overlay.resize(self.surface.size())
            self.error_overlay.raise_()
        if self.loading_overlay.isVisible():
            self.loading_overlay.resize(self.surface.size())
            self.loading_overlay.raise_()
        if self.buffer_badge.isVisible():
            self._layout_buffer_badge()
            self.buffer_badge.raise_()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Any key press in fullscreen reveals the controls briefly.
        if self.window().isFullScreen():
            self._show_controls()
            self._hide_timer.start()
        k = event.key()
        if k == Qt.Key_Space:
            self._toggle_pause()
        elif k == Qt.Key_Left:
            self._skip(-self.SKIP_SECONDS)
        elif k == Qt.Key_Right:
            self._skip(self.SKIP_SECONDS)
        elif k == Qt.Key_F:
            self._toggle_fullscreen()
        elif k == Qt.Key_M:
            self._toggle_mute()
        elif k == Qt.Key_A:
            self._cycle_aspect()
        elif k == Qt.Key_J:
            if self._backend is not None:
                self._backend.cycle_subtitle_track()
        elif k == Qt.Key_NumberSign:
            if self._backend is not None:
                self._backend.cycle_audio_track()
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            # "+" nudges audio delay later; "=" shares the physical key on
            # US layouts (no shift) so it works without Shift too.
            self._nudge_audio_delay(self._AUDIO_DELAY_STEP)
        elif k == Qt.Key_Minus:
            self._nudge_audio_delay(-self._AUDIO_DELAY_STEP)
        elif k == Qt.Key_Escape and self.window().isFullScreen():
            self._set_fullscreen(False)
        else:
            super().keyPressEvent(event)

    def shutdown(self) -> None:
        self._checkpoint_timer.stop()
        self._checkpoint_current()
        self._cancel_sleep_timer()
        if self._recorder is not None:
            self._recorder.shutdown()
        if self._compact:
            self._toggle_compact()
        if self._backend is not None:
            self._backend.stop()
            self._backend.destroy()
            self._backend = None
        self._playback_active = False


# ---------------------------------------------------------------------------
# OpenSubtitles search dialog
# ---------------------------------------------------------------------------

class _SubtitleSearchDialog(QDialog):
    """Search OpenSubtitles for the current video and load the chosen file.

    Local files are matched by movie hash first (exact), with the editable
    title query as fallback; network streams search by title only. Network
    I/O runs on worker threads; results arrive via Qt signals."""

    results_ready = Signal(object)        # list[dict] or Exception
    download_done = Signal(str, str, str) # (saved path, error, login warning)

    _LANGS = ["en", "es", "fr", "de", "it", "pt", "ro", "ru", "ar",
              "zh", "ja", "ko", "nl", "pl", "sv", "tr", "all"]

    def __init__(self, config: DeeptorrentConfig, file_path: str, query_hint: str,
                 on_loaded: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._file_path = file_path
        self._on_loaded = on_loaded
        self._results: List[dict] = []
        self.setWindowTitle("Find Subtitles — OpenSubtitles")
        self.setMinimumSize(520, 340)
        self.resize(640, 420)
        self.setStyleSheet(
            "QDialog { background-color: #0a0a0f; color: #c8d3e0; }"
            "QLabel { color: #c8d3e0; }"
            "QLineEdit, QComboBox { background-color: #0d1117; color: #c8d3e0; border: 1px solid #1a2a4a; padding: 3px 8px; border-radius: 3px; }"
            "QTableWidget { background-color: #0d1117; color: #c8d3e0; gridline-color: #1a2a4a; border: 1px solid #1a2a4a; border-radius: 6px; }"
            "QPushButton { background-color: #111827; color: #c8d3e0; border: 1px solid #1a2a4a; padding: 3px 12px; border-radius: 3px; }"
            "QPushButton:hover { border-color: #2a7abf; color: #2a7abf; }"
            "QLabel#hint { color: #4a6a8a; font-size: 11px; }"
        )
        self._build_ui(query_hint)
        self.results_ready.connect(self._on_results)
        self.download_done.connect(self._on_downloaded)

    def _build_ui(self, query_hint: str) -> None:
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.query = QLineEdit(query_hint)
        top.addWidget(self.query, 1)
        self.lang = QComboBox()
        self.lang.addItems(self._LANGS)
        # Default to the user's preferred subtitle language when configured.
        pref = (self._config.iptv.preferred_sub_lang or "").strip().lower()[:2]
        idx = self._LANGS.index(pref) if pref in self._LANGS else 0
        self.lang.setCurrentIndex(idx)
        top.addWidget(self.lang)
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._search)
        top.addWidget(self.search_btn)
        layout.addLayout(top)

        if self._file_path:
            hint = QLabel("Local file — matched by hash first, title query as fallback.")
        else:
            hint = QLabel("Network stream — title search only.")
        hint.setObjectName("hint")
        layout.addWidget(hint)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Release", "Lang", "Downloads", "Rating"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.doubleClicked.connect(lambda _i: self._download_selected())
        layout.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setObjectName("hint")
        layout.addWidget(self.status)

        bottom = QHBoxLayout()
        self.dl_btn = QPushButton("Download && Load")
        self.dl_btn.clicked.connect(self._download_selected)
        bottom.addWidget(self.dl_btn)
        bottom.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)

        if not self._config.iptv.opensubtitles_api_key:
            self.status.setText(
                "No OpenSubtitles API key — add one in Config → IPTV Settings "
                "(free consumer key at opensubtitles.com).")
            self.search_btn.setEnabled(False)
            self.dl_btn.setEnabled(False)

    # -- search ---------------------------------------------------------------
    def _search(self) -> None:
        from iptv.opensubtitles import OpenSubtitlesClient
        cfg = self._config.iptv
        client = OpenSubtitlesClient(cfg.opensubtitles_api_key,
                                     cfg.opensubtitles_username,
                                     cfg.opensubtitles_password)
        query = self.query.text().strip()
        languages = self.lang.currentText()
        file_path = self._file_path
        self.status.setText("Searching…")
        self.search_btn.setEnabled(False)

        def _work() -> None:
            try:
                self.results_ready.emit(client.search(
                    query=query, file_path=file_path, languages=languages))
            except Exception as exc:
                self.results_ready.emit(exc)

        threading.Thread(target=_work, daemon=True).start()

    def _on_results(self, results: object) -> None:
        self.search_btn.setEnabled(True)
        if isinstance(results, Exception):
            self.status.setText(str(results))
            return
        self._results = list(results)
        self.table.setRowCount(len(self._results))
        for r, e in enumerate(self._results):
            hi = " (HI)" if e.get("hearing_impaired") else ""
            self.table.setItem(r, 0, QTableWidgetItem(str(e.get("release", "")) + hi))
            self.table.setItem(r, 1, QTableWidgetItem(str(e.get("language", ""))))
            self.table.setItem(r, 2, QTableWidgetItem(str(e.get("downloads", 0))))
            self.table.setItem(r, 3, QTableWidgetItem(f"{e.get('rating', 0.0):.1f}"))
        self.status.setText(f"{len(self._results)} result(s) — double-click or Download & Load.")

    # -- download -------------------------------------------------------------
    def _download_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._results):
            self.status.setText("Select a subtitle first.")
            return
        entry = self._results[row]
        file_id = entry.get("file_id")
        if not file_id:
            return
        dest = self._dest_path(entry)
        from iptv.opensubtitles import OpenSubtitlesClient
        cfg = self._config.iptv
        client = OpenSubtitlesClient(cfg.opensubtitles_api_key,
                                     cfg.opensubtitles_username,
                                     cfg.opensubtitles_password)
        self.status.setText("Downloading…")
        self.dl_btn.setEnabled(False)

        def _work() -> None:
            try:
                path = client.download(int(file_id), dest)
                warning = client.login_warning.message if client.login_warning else ""
                self.download_done.emit(path, "", warning)
            except Exception as exc:
                warning = client.login_warning.message if client.login_warning else ""
                self.download_done.emit("", str(exc), warning)

        threading.Thread(target=_work, daemon=True).start()

    def _dest_path(self, entry: dict) -> str:
        from iptv.opensubtitles import subtitle_dest_path
        return subtitle_dest_path(self._file_path, self.query.text(),
                                  entry.get("language", ""))

    def _on_downloaded(self, path: str, error: str, login_warning: str = "") -> None:
        self.dl_btn.setEnabled(True)
        if error:
            self.status.setText(
                f"{login_warning} {error}".strip() if login_warning else error)
            return
        if self._on_loaded is not None:
            self._on_loaded(path)
        loaded = f"Loaded: {os.path.basename(path)}"
        if login_warning:
            # Keep the dialog open so the non-fatal account problem is visible;
            # the subtitle itself has still loaded using key-only mode.
            self.status.setText(f"{loaded}. Warning: {login_warning}")
            return
        self.status.setText(loaded)
        self.accept()


def _fmt_time(s: float) -> str:
    if s <= 0:
        return "00:00"
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Content grid (icon mode) + list view, stacked
# ---------------------------------------------------------------------------

_PLACEHOLDER_CACHE: "OrderedDict[str, QPixmap]" = OrderedDict()
_TILE_SIZE = QSize(240, 320)
# A 240x320 pixmap costs ~308 KB of RAM, and every decorated tile holds one:
# per-name initials for a 16k-entry section would be ~335 MB. So icons only
# exist for tiles near the viewport (see ContentGrid._release_far_icons) and
# both caches are bounded.
_MAX_PLACEHOLDERS = 400
_MAX_CACHED_PIXMAPS = 800
_NEUTRAL: Optional[QPixmap] = None
_ROW_ROLE = Qt.UserRole + 1
_EPG_ROLE = Qt.UserRole + 2  # per-tile "now playing" text (live channels)

# Tiles fetched ahead of/behind the viewport so scrolling rarely shows a
# placeholder, and the ceiling on outstanding prefetch downloads that keeps a
# fast scroll from starving the tiles actually on screen.
_PREFETCH_TILES = 250
_MAX_PENDING_ARTWORK = 800
# Icons are dropped this far outside the prefetch window (hysteresis: tiles
# aren't cleared the moment they leave it, so a small scroll doesn't churn).
_RELEASE_MARGIN = 120
# Pixmap decodes per event-loop turn (~1.5 ms each).
_DECODE_BATCH = 12
# Background artwork sweep pacing (metadata lookups are limited to ~5/s).
_SWEEP_BATCH = 5
_SWEEP_INTERVAL_MS = 1000


def _neutral_pixmap() -> QPixmap:
    """The shared 'nothing loaded here' tile — one instance for the whole app."""
    global _NEUTRAL
    if _NEUTRAL is None:
        pm = QPixmap(_TILE_SIZE)
        pm.fill(QColor(24, 26, 34))
        _NEUTRAL = pm
    return _NEUTRAL


def _initials(name: str) -> str:
    """Up to 3 characters standing in for a channel name ("BBC News HD" -> BBC)."""
    words = [w for w in re.findall(r"[0-9A-Za-z]+", name or "")
             if w.upper() not in ("HD", "FHD", "UHD", "SD", "4K", "TV", "VIP")]
    if not words:
        words = re.findall(r"[0-9A-Za-z]+", name or "")
    if not words:
        return ""
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(w[0] for w in words[:3]).upper()


def _placeholder_pixmap(name: str = "") -> QPixmap:
    """Tile used until (or instead of) a real logo.

    A flat grey box reads as "broken", so channels without artwork get their
    initials on a colour derived from the name — distinct per channel and
    stable across restarts."""
    text = _initials(name)
    pm = _PLACEHOLDER_CACHE.get(text)
    if pm is not None:
        _PLACEHOLDER_CACHE.move_to_end(text)
        return pm
    pm = QPixmap(_TILE_SIZE)
    hue = (zlib.crc32(text.encode("utf-8")) % 360) if text else 210
    pm.fill(QColor.fromHsv(hue, 70, 95))
    if text:
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        font = painter.font()
        font.setBold(True)
        font.setPointSize(60 if len(text) < 3 else 44)
        painter.setFont(font)
        painter.setPen(QColor(232, 236, 244))
        painter.drawText(pm.rect(), Qt.AlignCenter, text)
        painter.end()
    _PLACEHOLDER_CACHE[text] = pm
    while len(_PLACEHOLDER_CACHE) > _MAX_PLACEHOLDERS:
        _PLACEHOLDER_CACHE.popitem(last=False)
    return pm


# ---------------------------------------------------------------------------
# Poster delegate — paints the artwork, title, and Select/Play buttons that
# overlay the top of every tile. Select highlights when the tile is selected;
# Play triggers activation. Both are hit-tested in ContentGrid.mousePressEvent.
# ---------------------------------------------------------------------------

_BTN_MARGIN = 6
_BTN_GAP = 6
_TEXT_LINES = 2


class PosterDelegate(QStyledItemDelegate):
    """Custom icon-mode painter with overlaid Select/Play buttons."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

    # -- geometry helpers (shared with ContentGrid for hit-testing) ----------
    @staticmethod
    def tile_layout(rect: QRect, icon_sz: QSize) -> tuple:
        """(icon_rect, text_rect) within an item ``rect`` for icon ``icon_sz``."""
        icon_rect = QRect(rect.left(), rect.top(),
                          min(icon_sz.width(), rect.width()), icon_sz.height())
        text_rect = QRect(rect.left(), icon_rect.bottom() + 2,
                          rect.width(), rect.height() - icon_rect.height() - 2)
        return icon_rect, text_rect

    @staticmethod
    def button_rects(icon_rect: QRect) -> tuple:
        """(select_rect, play_rect) overlaid at the top of the poster."""
        btn_h = max(20, min(30, icon_rect.height() // 6))
        w = (icon_rect.width() - 2 * _BTN_MARGIN - _BTN_GAP) // 2
        if w < 24:
            # Very narrow tiles: stack the two buttons vertically instead.
            w = icon_rect.width() - 2 * _BTN_MARGIN
            y = icon_rect.top() + _BTN_MARGIN
            select = QRect(icon_rect.left() + _BTN_MARGIN, y, w, btn_h)
            play = QRect(icon_rect.left() + _BTN_MARGIN,
                         select.bottom() + _BTN_GAP, w, btn_h)
            return select, play
        y = icon_rect.top() + _BTN_MARGIN
        select = QRect(icon_rect.left() + _BTN_MARGIN, y, w, btn_h)
        play = QRect(select.right() + _BTN_GAP, y, w, btn_h)
        return select, play

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        icon_sz = option.decorationSize
        fm = QFontMetrics(option.font)
        text_h = fm.height() * _TEXT_LINES + 4
        return QSize(icon_sz.width(), icon_sz.height() + text_h)

    # -- painting -----------------------------------------------------------
    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = option.rect
        icon_sz = option.decorationSize
        icon_rect, text_rect = self.tile_layout(rect, icon_sz)

        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        # Selection background behind the whole tile.
        if selected:
            painter.fillRect(rect, QColor(42, 122, 191, 60))
        elif hovered:
            painter.fillRect(rect, QColor(255, 255, 255, 18))

        # Poster / logo artwork, aspect-kept and centred in the icon rect.
        icon = index.data(Qt.DecorationRole)
        if icon is not None and not icon.isNull():
            mode = QIcon.Selected if selected else QIcon.Normal
            icon.paint(painter, icon_rect, Qt.AlignCenter, mode, QIcon.Off)
        else:
            painter.fillRect(icon_rect, QColor(24, 26, 34))
            painter.setPen(QColor(120, 130, 150))
            painter.drawText(icon_rect, Qt.AlignCenter, "—")

        # Title text (wrapped, clipped to the text area). Live tiles with EPG
        # data get a fixed two-line layout instead: name on line 1, current
        # programme (dimmed) on line 2 — one line each, elided.
        text = index.data(Qt.DisplayRole) or ""
        epg = index.data(_EPG_ROLE) or ""
        painter.setPen(QColor(232, 236, 244) if selected else QColor(200, 211, 224))
        if epg:
            fm = QFontMetrics(option.font)
            line_h = fm.height()
            title_rect = QRect(text_rect.left(), text_rect.top(),
                               text_rect.width(), line_h)
            epg_rect = QRect(text_rect.left(), text_rect.top() + line_h + 2,
                             text_rect.width(), line_h)
            painter.drawText(title_rect, Qt.AlignHCenter | Qt.AlignTop,
                             fm.elidedText(text, Qt.ElideRight, text_rect.width()))
            painter.setPen(QColor(138, 154, 176))
            painter.drawText(epg_rect, Qt.AlignHCenter | Qt.AlignTop,
                             fm.elidedText(epg, Qt.ElideRight, text_rect.width()))
        else:
            painter.drawText(text_rect,
                             Qt.AlignTop | Qt.AlignHCenter | Qt.TextWordWrap, text)

        # Overlay buttons — always visible so a single click on Play starts
        # playback without first having to select the tile.
        playing = self._is_playing(index)
        loading = self._is_loading(index)
        self._paint_buttons(painter, icon_rect, selected, hovered, playing,
                            loading)
        painter.restore()

    def _is_playing(self, index) -> bool:
        """True if the item at ``index`` is the one currently playing."""
        grid = self.parent()
        if grid is not None:
            it = index.data(Qt.UserRole)
            return it is not None and id(it) == getattr(grid, "_playing_id", None)
        return False

    def _is_loading(self, index) -> bool:
        """True if the item at ``index`` is currently loading (stream opening)."""
        grid = self.parent()
        if grid is not None:
            it = index.data(Qt.UserRole)
            return it is not None and id(it) == getattr(grid, "_loading_id", None)
        return False

    def _paint_buttons(self, painter: QPainter, icon_rect: QRect,
                       selected: bool, hovered: bool, playing: bool,
                       loading: bool = False) -> None:
        select_rect, play_rect = self.button_rects(icon_rect)
        # Select: filled accent when selected, otherwise a translucent chip that
        # becomes opaque on hover. Hover always brightens it.
        if selected:
            bg = QColor(42, 122, 191, 255 if hovered else 220)
            self._round_rect(painter, select_rect, bg, QColor(255, 255, 255),
                             "Select")
        else:
            bg = QColor(10, 10, 15, 220 if hovered else 160)
            self._round_rect(painter, select_rect, bg, QColor(220, 228, 240),
                             "Select")
        # Play button states (priority: loading > playing > hover > normal):
        #   loading  → pulsing orange/white with "Loading…" or "Retry 2/5" text
        #   playing  → solid orange with "▶ Playing"
        #   hovered  → accent blue with "▶ Play"
        #   normal   → dark translucent chip with "▶ Play"
        if loading:
            grid = self.parent()
            pulse = getattr(grid, "_pulse_on", False) if grid is not None else False
            retries = getattr(grid, "_retry_count", 0) if grid is not None else 0
            max_retries = getattr(grid, "_MAX_RETRIES", 5) if grid is not None else 5
            if pulse:
                bg = QColor(230, 126, 34, 255)
                fg = QColor(255, 255, 255)
            else:
                bg = QColor(230, 126, 34, 140)
                fg = QColor(255, 235, 200)
            if retries > 0:
                label = f"⟳ Retry {retries}/{max_retries}"
            else:
                label = "⟳ Loading…"
            self._round_rect(painter, play_rect, bg, fg, label)
        elif playing:
            bg = QColor(230, 126, 34, 240 if hovered else 210)
            self._round_rect(painter, play_rect, bg, QColor(255, 255, 255),
                             "▶ Playing")
        elif hovered:
            self._round_rect(painter, play_rect, QColor(42, 122, 191, 240),
                             QColor(255, 255, 255), "▶ Play")
        else:
            bg = QColor(10, 10, 15, 200 if selected else 160)
            self._round_rect(painter, play_rect, bg, QColor(220, 228, 240),
                             "▶ Play")

    @staticmethod
    def _round_rect(painter: QPainter, r: QRect, bg: QColor,
                    fg: QColor, label: str) -> None:
        painter.setPen(QPen(QColor(0, 0, 0, 90), 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(r, 5, 5)
        painter.setPen(fg)
        f = painter.font()
        f.setBold(True)
        f.setPointSize(max(8, r.height() // 3))
        painter.setFont(f)
        painter.drawText(r, Qt.AlignCenter, label)


class ContentGrid(QListWidget):
    """Virtualized icon-mode grid with lazy artwork loading.

    Artwork is only fetched for items currently visible in the viewport
    (plus a small prefetch margin), so a 50k-entry section never queues
    50k image downloads. Several items can share one logo URL — the
    pending map is URL -> list of items so all of them get updated."""

    itemActivated = Signal(object)  # double click / context Play -> play it
    itemSelected = Signal(object)   # single click -> show the info panel
    sweep_progress = Signal(int, int)  # (resolved, total) artwork lookups
    sig_artwork = Signal(str, object)  # (url, path) — marshals worker -> GUI
    sig_artwork_failed = Signal(str)   # (url) — fetch gave up, worker -> GUI
    sig_logo = Signal(object, str)     # (item, url) — async channel-logo result

    def __init__(self, manager: IPTVManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self.setViewMode(QListWidget.IconMode)
        # Tile size is recomputed from the viewport (3 rows visible, columns
        # fill the width) — see _recompute_tile_size. Start from a sane default
        # so sizeHint is valid before the first resize.
        self._tile_size = QSize(240, 320)
        self.setIconSize(self._tile_size)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.ElideRight)
        self.setSpacing(8)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Custom delegate paints the poster + overlaid Select/Play buttons.
        self.setItemDelegate(PosterDelegate(self))
        # Hover tracking drives the button overlay (State_MouseOver in paint).
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.itemDoubleClicked.connect(self._on_activate)
        self.itemClicked.connect(self._on_select)
        self.sig_artwork.connect(self._apply_artwork)
        self.sig_artwork_failed.connect(lambda u: self._on_artwork_failed(u))
        self.sig_logo.connect(self._apply_logo)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self._scan_timer.start())
        self._items: List[Any] = []
        self._playing_id: Optional[int] = None  # id(item) currently playing
        self._artwork_requests: Dict[str, List[QListWidgetItem]] = {}
        self._requested_urls: set = set()
        # Loaded artwork, kept per URL: hundreds of channels share a single
        # logo URL (58 for one logo in a typical playlist), and tiles that
        # scroll in after the fetch completed must still get the icon.
        self._pixmaps: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._decorated: set = set()  # rows currently holding a real/initials icon
        self._failed_urls: set = set()       # gave up — don't re-queue
        self._fail_counts: Dict[str, int] = {}  # url -> consecutive failures
        self._logo_pending: Dict[int, List[QListWidgetItem]] = {}  # id(item) -> tiles
        self._logo_tried: set = set()        # id(item) -> artwork lookup attempted
        self._tiles_by_item: Dict[int, QListWidgetItem] = {}
        # Background sweep: resolve artwork for every entry in the section that
        # has none, a few per tick so the metadata rate limiter isn't flooded.
        self._sweep_queue: deque = deque()
        self._sweep_timer = QTimer(self)
        self._sweep_timer.setInterval(_SWEEP_INTERVAL_MS)
        self._sweep_timer.timeout.connect(self._sweep_step)
        # Scrolling fires continuously; coalesce the (wider) prefetch scans.
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.setInterval(60)
        self._scan_timer.timeout.connect(self._load_visible_artwork)
        # Pixmap decoding is spread over event-loop turns (see _apply_artwork).
        self._sweep_total = 0
        self._sweep_done = 0
        self._decode_queue: deque = deque()
        self._decode_timer = QTimer(self)
        self._decode_timer.setInterval(0)
        self._decode_timer.timeout.connect(self._decode_step)
        # Live-tile "now playing" line: refreshed for visible tiles every
        # minute (programmes roll over as time passes even if nothing else
        # changes).
        self._epg_timer = QTimer(self)
        self._epg_timer.setInterval(60_000)
        self._epg_timer.timeout.connect(lambda: self._update_epg_tiles(full=True))
        self._epg_timer.start()
        # Loading pulse: repaints the loading tile's Play button so it blinks
        # orange/white while the stream is being opened.
        self._loading_id: Optional[int] = None
        self._pulse_on = False
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(500)
        self._pulse_timer.timeout.connect(self._pulse_step)
        # Retry count is pushed here by IPTVTab (via sig_retry) so the
        # PosterDelegate can show "Retry 2/5" on the tile's Play button.
        self._retry_count = 0
        self._MAX_RETRIES = 5

    def set_items(self, items: List[Any]) -> None:
        self._items = items
        self._sweep_timer.stop()
        self._sweep_queue.clear()
        self._decode_timer.stop()
        self._decode_queue.clear()
        self._artwork_requests.clear()
        self._requested_urls.clear()
        self._pixmaps.clear()
        self._failed_urls.clear()
        self._fail_counts.clear()
        self._logo_pending.clear()
        self._logo_tried.clear()
        self._tiles_by_item.clear()
        self._decorated.clear()
        self.clear()
        neutral = QIcon(_neutral_pixmap())
        for row, it in enumerate(items):
            name = getattr(it, "name", "") or getattr(it, "display_name", "")
            li = QListWidgetItem(name)
            # Shared placeholder: a per-name one here would cost ~300 MB on a
            # 16k-entry section. Real icons are attached near the viewport.
            li.setIcon(neutral)
            li.setData(Qt.UserRole, it)
            li.setData(_ROW_ROLE, row)  # QListWidget.row() is a linear scan
            self.addItem(li)
            self._tiles_by_item[id(it)] = li
            if not (getattr(it, "logo", "") or getattr(it, "poster", "")):
                self._sweep_queue.append(li)
        self._sweep_total = len(self._sweep_queue)
        self._sweep_done = 0
        self.sweep_progress.emit(0, self._sweep_total)
        # Item geometry isn't valid until the layout pass after the widget is
        # shown — defer slightly so the first visible batch loads immediately.
        QTimer.singleShot(80, self._load_visible_artwork)
        if self._sweep_queue:
            self._sweep_timer.start()

    def reset_artwork_state(self) -> None:
        """Forget every in-memory artwork memo after an external cache clear.

        Without this the grid keeps serving pixmaps decoded from files that no
        longer exist, and its "already tried" sets stop tiles from ever
        re-resolving. Items keep their artwork URLs (still valid — they just
        re-download); entries with none go back on the sweep queue."""
        self._sweep_timer.stop()
        self._decode_timer.stop()
        self._decode_queue.clear()
        self._artwork_requests.clear()
        self._requested_urls.clear()
        self._pixmaps.clear()
        self._failed_urls.clear()
        self._fail_counts.clear()
        self._logo_pending.clear()
        self._logo_tried.clear()
        self._decorated.clear()
        neutral = QIcon(_neutral_pixmap())
        self._sweep_queue.clear()
        for row in range(self.count()):
            li = self.item(row)
            if li is None:
                continue
            li.setIcon(neutral)
            it = li.data(Qt.UserRole)
            if it is not None and not (getattr(it, "logo", "") or getattr(it, "poster", "")):
                self._sweep_queue.append(li)
        self._sweep_total = len(self._sweep_queue)
        self._sweep_done = 0
        if self._sweep_queue:
            self.sweep_progress.emit(0, self._sweep_total)
            self._sweep_timer.start()
        QTimer.singleShot(0, self._load_visible_artwork)

    def set_playing_item(self, item: Any) -> None:
        """Mark ``item`` as the one currently playing so its Play button turns
        orange. Repaints the old + new tiles. Also stops the loading pulse —
        playback has started (or failed), the blink is no longer needed."""
        self.set_loading_item(None)
        old_id = self._playing_id
        new_id = id(item) if item is not None else None
        if old_id == new_id:
            return
        self._playing_id = new_id
        # Repaint the tile that was playing (revert to normal) and the new one.
        for tid in (old_id, new_id):
            if tid is None:
                continue
            li = self._tiles_by_item.get(tid)
            if li is not None:
                self.update(self.indexFromItem(li))

    def set_loading_item(self, item: Any) -> None:
        """Mark ``item`` as loading — its Play button pulses orange/white so
        the user sees the tile is active while the stream opens. Pass None to
        stop the pulse (playback started, errored, or was cancelled)."""
        new_id = id(item) if item is not None else None
        old_id = self._loading_id
        if new_id == old_id:
            return
        self._loading_id = new_id
        if new_id is not None:
            self._pulse_on = True
            self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
            self._pulse_on = False
            self._retry_count = 0  # reset for next item
        # Repaint old + new tiles.
        for tid in (old_id, new_id):
            if tid is None:
                continue
            li = self._tiles_by_item.get(tid)
            if li is not None:
                self.update(self.indexFromItem(li))

    def set_retry_count(self, attempt: int, max_retries: int) -> None:
        """Update the retry count shown on the loading tile's Play button
        (e.g. ``Retry 2/5``). Repaints the loading tile so the new count is
        visible immediately, not on the next pulse tick."""
        self._retry_count = attempt
        self._MAX_RETRIES = max_retries
        if self._loading_id is not None:
            li = self._tiles_by_item.get(self._loading_id)
            if li is not None:
                self.update(self.indexFromItem(li))

    def _pulse_step(self) -> None:
        """Toggle the pulse state and repaint the loading tile."""
        if self._loading_id is None:
            self._pulse_timer.stop()
            return
        self._pulse_on = not self._pulse_on
        li = self._tiles_by_item.get(self._loading_id)
        if li is not None:
            self.update(self.indexFromItem(li))

    # -- background artwork sweep -------------------------------------------
    def _sweep_step(self) -> None:
        """Resolve artwork for the next few entries that have none.

        Visible tiles are handled first by :meth:`_load_visible_artwork`; this
        walks the rest of the section so covers are already in the cache by
        the time the user scrolls down to them."""
        sent = 0
        while self._sweep_queue and sent < _SWEEP_BATCH:
            li = self._sweep_queue.popleft()
            if self._resolve_missing_artwork(li):
                sent += 1
        if not self._sweep_queue:
            self._sweep_timer.stop()

    def _resolve_missing_artwork(self, li: QListWidgetItem,
                                 framegrab: bool = False) -> bool:
        """Kick off the artwork lookup for one artwork-less tile.

        Returns True when a lookup was actually started (callers pace
        themselves against the metadata rate limiter).

        ``framegrab`` enables the FFmpeg last resort and is only ever set for
        tiles on screen: it opens a video connection to the user's provider,
        so the background sweep (tens of thousands of entries) must not."""
        it = li.data(Qt.UserRole)
        if it is None:
            return False
        if getattr(it, "logo", "") or getattr(it, "poster", ""):
            return False  # artwork arrived from somewhere else meanwhile
        if id(it) in self._logo_pending:
            self._logo_pending[id(it)].append(li)
            return False
        if id(it) in self._logo_tried:
            return False
        self._logo_tried.add(id(it))
        self._logo_pending[id(it)] = [li]
        # Channels resolve against the local iptv-org index; movies/series go
        # out to TMDb/TVmaze. Both run off the GUI thread.
        if isinstance(it, Channel):
            self._manager.resolve_channel_logo_async(it, self._on_logo_resolved)
        else:
            self._manager.resolve_poster_async(it, self._on_logo_resolved,
                                               allow_framegrab=framegrab)
        return True

    def apply_external_artwork(self, item: Any, url: str) -> None:
        """Artwork discovered outside the grid (detail-panel metadata).

        Without this the tile keeps its placeholder until the section is
        rebuilt, even though clicking the entry just found its poster."""
        li = self._tiles_by_item.get(id(item))
        if li is None or not url or url in self._failed_urls:
            return
        self._logo_tried.add(id(item))
        self._logo_pending.setdefault(id(item), []).append(li)
        self._apply_logo(item, url)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Geometry is only valid once visible — size the tiles then load artwork.
        # The synchronous pass handles the common case; the deferred pass catches
        # the post-layout geometry (a child widget's viewport can still be empty
        # when showEvent first fires inside a not-yet-laid-out parent).
        self._recompute_tile_size()
        QTimer.singleShot(0, self._recompute_tile_size)
        QTimer.singleShot(0, self._load_visible_artwork)

    # -- dynamic tile sizing -------------------------------------------------
    _TARGET_COLS = 3
    _TARGET_ROWS = 3

    def _recompute_tile_size(self) -> None:
        """Fit posters to a 3×3 grid: 3 columns × 3 rows always visible, tiles
        stretch to fill the available width and height so there's no right-edge
        or bottom gap. Posters shrink when the window shrinks to keep the 3×3
        invariant; a floor keeps them from becoming unusably tiny.

        icon_w is ALWAYS the 3-column width — never shrunk below it — so
        QListWidget packs exactly 3 columns. When the viewport is short, icon_h
        is capped and the portrait poster is centred inside the wider tile
        (the delegate paints it aspect-kept with AlignCenter)."""
        vp = self.viewport().rect()
        if vp.isEmpty():
            return
        spacing = self.spacing()
        fm = self.fontMetrics()
        text_h = fm.height() * _TEXT_LINES + 6
        # 3 columns fill the width exactly — this width is FIXED so QListWidget
        # never packs a 4th column.
        icon_w = (vp.width() - (self._TARGET_COLS + 1) * spacing) // self._TARGET_COLS
        icon_w = max(80, icon_w)
        # Height at perfect portrait aspect (240x320) from this width.
        icon_h_from_w = int(icon_w * 320 / 240)
        # Height available for 3 rows.
        row_h = (vp.height() - (self._TARGET_ROWS + 1) * spacing) // self._TARGET_ROWS
        icon_h_max = max(96, row_h - text_h)
        # Cap the icon height so 3 rows always fit; the poster stays portrait
        # and is centred by the delegate inside the wider tile.
        icon_h = min(icon_h_from_w, icon_h_max)
        new_size = QSize(icon_w, icon_h)
        if new_size != self._tile_size:
            self._tile_size = new_size
            self.setIconSize(new_size)
            # setIconSize + uniform sizes + Adjust resize mode relayout the
            # grid from the delegate sizeHint automatically.

    def _row_at(self, y: int) -> int:
        """Row index at viewport height ``y``, or -1.

        Probes a few x positions: icon mode leaves gaps between tiles, and a
        single probe that lands in one returns an invalid index."""
        vp = self.viewport().rect()
        for frac in (0.5, 0.15, 0.85, 0.32, 0.68):
            idx = self.indexAt(QPoint(int(vp.left() + vp.width() * frac), y))
            if idx.isValid():
                return idx.row()
        return -1

    def _visible_range(self) -> tuple:
        """(first, last) row index in the viewport — O(1), not a full scan."""
        vp = self.viewport().rect()
        first = self._row_at(vp.top() + 2)
        last = self._row_at(vp.bottom() - 2)
        if first < 0 and last < 0:
            # Nothing probed cleanly — approximate from the scroll position so
            # a scrolled-down grid doesn't prefetch from the top of the list.
            sb = self.verticalScrollBar()
            span = sb.maximum() + sb.pageStep()
            first = int(self.count() * sb.value() / span) if span > 0 else 0
        if first < 0:
            first = max(0, last - 60)
        if last < 0:
            last = min(self.count() - 1, first + 60)
        return first, max(first, last)

    def _update_epg_tiles(self, full: bool = False) -> None:
        """Stamp the current programme onto visible live-channel tiles.

        ``full=False`` (scroll path) only fills tiles missing EPG text; the
        minute timer's full pass refreshes everything in view as programmes
        roll over. Channel-id resolution is memoized in the manager; now/next
        is two tiny SQLite reads per tile."""
        if not self.count():
            return
        first, last = self._visible_range()
        lo = max(0, first - 9)
        hi = min(self.count() - 1, last + 9)
        changed = False
        for i in range(lo, hi + 1):
            li = self.item(i)
            if li is None:
                continue
            it = li.data(Qt.UserRole)
            if not isinstance(it, Channel):
                continue
            if not full and li.data(_EPG_ROLE):
                continue
            nn = self._manager.epg_now_next_for(getattr(it, "tvg_id", ""),
                                                getattr(it, "name", ""))
            if not isinstance(nn, dict):
                continue  # defensive: mocked/broken managers return junk
            text = _fmt_now_title(nn)
            if (li.data(_EPG_ROLE) or "") != text:
                li.setData(_EPG_ROLE, text)
                changed = True
        if changed:
            self.viewport().update()

    def _load_visible_artwork(self) -> None:
        if not self.isVisible() or not self.count():
            return
        first, last = self._visible_range()
        lo = max(0, first - _PREFETCH_TILES)
        hi = min(self.count() - 1, last + _PREFETCH_TILES)
        self._release_far_icons(lo, hi)
        for i in range(lo, hi + 1):
            self._request_tile_artwork(self.item(i), prefetch=not (first <= i <= last))
        self._update_epg_tiles(full=False)

    def _release_far_icons(self, lo: int, hi: int) -> None:
        """Drop icons well outside the window so RAM tracks the window size.

        Coming back is cheap: the pixmap is usually still in ``_pixmaps``,
        and otherwise the image is already on disk."""
        lo -= _RELEASE_MARGIN
        hi += _RELEASE_MARGIN
        stale = [r for r in self._decorated if r < lo or r > hi]
        if not stale:
            return
        neutral = QIcon(_neutral_pixmap())
        for row in stale:
            li = self.item(row)
            if li is not None:
                li.setIcon(neutral)
            self._decorated.discard(row)

    def _set_tile_icon(self, li: QListWidgetItem, pm: QPixmap) -> None:
        li.setIcon(QIcon(pm))
        row = li.data(_ROW_ROLE)
        if row is not None:
            self._decorated.add(row)

    def _remember_pixmap(self, url: str, pm: QPixmap) -> None:
        self._pixmaps[url] = pm
        self._pixmaps.move_to_end(url)
        while len(self._pixmaps) > _MAX_CACHED_PIXMAPS:
            old, _ = self._pixmaps.popitem(last=False)
            # Allow a re-fetch (disk cache hit) if it scrolls back into view.
            self._requested_urls.discard(old)

    def _request_tile_artwork(self, li: QListWidgetItem, prefetch: bool = False) -> None:
        it = li.data(Qt.UserRole)
        if it is None:
            return
        logo = getattr(it, "logo", "") or getattr(it, "poster", "")
        if not logo or logo in self._failed_urls:
            # No artwork (yet): show the initials tile, and for visible items
            # kick off the lookup — channels fall back to iptv-org,
            # movies/series to TMDb/TVmaze. The sweep covers the rest.
            row = li.data(_ROW_ROLE)
            if row is not None and row not in self._decorated:
                self._set_tile_icon(li, _placeholder_pixmap(li.text()))
            if not logo and not prefetch:
                # On screen right now — the only place frame-grabbing is allowed.
                self._resolve_missing_artwork(li, framegrab=True)
            return
        pm = self._pixmaps.get(logo)
        if pm is not None:
            self._set_tile_icon(li, pm)  # already downloaded for another tile
            self._pixmaps.move_to_end(logo)
            return
        if logo in self._requested_urls:
            # Fetch in flight — attach this tile so it gets the result too.
            self._artwork_requests.setdefault(logo, []).append(li)
            return
        # Fast scrolling would otherwise pile up thousands of stale prefetches
        # in front of the tiles the user is actually looking at.
        if prefetch and len(self._artwork_requests) >= _MAX_PENDING_ARTWORK:
            return
        self._requested_urls.add(logo)
        self._artwork_requests.setdefault(logo, []).append(li)
        self._manager.fetch_artwork(
            logo, self._on_artwork,
            priority=PRIORITY_PREFETCH if prefetch else PRIORITY_VISIBLE)

    def _on_logo_resolved(self, item: Any, url: str) -> None:
        # Worker thread -> GUI thread.
        self.sig_logo.emit(item, url)

    def _apply_logo(self, item: Any, url: str) -> None:
        tiles = self._logo_pending.pop(id(item), [])
        # Every artwork-less entry produces exactly one result (hit or miss),
        # so this is the honest progress counter for the sweep.
        if self._sweep_total:
            self._sweep_done = min(self._sweep_done + 1, self._sweep_total)
            self.sweep_progress.emit(self._sweep_done, self._sweep_total)
        if not url or url in self._failed_urls:
            return  # nothing matched (or the fallback is dead too) — keep the initials tile
        if isinstance(item, Channel):
            item.logo = url
        else:
            item.poster = url  # VOD keeps artwork in poster; the grid reads either
        pm = self._pixmaps.get(url)
        if pm is not None:
            for li in tiles:
                self._set_tile_icon(li, pm)
            return
        self._artwork_requests.setdefault(url, []).extend(tiles)
        if url in self._requested_urls:
            return  # another tile already queued this URL
        self._requested_urls.add(url)
        self._manager.fetch_artwork(url, self._on_artwork)

    def _on_artwork(self, url: str, path: Optional[str]) -> None:
        # Called from a worker thread — marshal to the GUI thread via signal.
        if path is None:
            self.sig_artwork_failed.emit(url)
            return
        self.sig_artwork.emit(url, path)

    def _apply_artwork(self, url: str, path: str) -> None:
        # Decoding is ~1.5 ms per image and cached tiles all come back at once
        # (a 600-tile scan over a warm cache was a 300 ms freeze), so spread
        # the work across event-loop turns instead of blocking the scroll.
        self._decode_queue.append((url, path))
        if not self._decode_timer.isActive():
            self._decode_timer.start()

    def _decode_step(self) -> None:
        for _ in range(_DECODE_BATCH):
            if not self._decode_queue:
                self._decode_timer.stop()
                return
            url, path = self._decode_queue.popleft()
            self._decode_one(url, path)

    def _decode_one(self, url: str, path: str) -> None:
        items = self._artwork_requests.pop(url, [])
        pm = self._pixmaps.get(url)
        if pm is None:
            pm = QPixmap(path)
        if pm.isNull():
            self._on_artwork_failed(url, items)
            return
        if pm.width() > _TILE_SIZE.width() or pm.height() > _TILE_SIZE.height():
            # Cache at the larger of the fixed tile size and the current
            # dynamic tile so posters stay sharp when the grid shows big tiles,
            # without keeping oversized pixmaps for tiny ones.
            cache_sz = _TILE_SIZE
            if (self._tile_size.width() > _TILE_SIZE.width()
                    or self._tile_size.height() > _TILE_SIZE.height()):
                cache_sz = self._tile_size
            pm = pm.scaled(cache_sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._remember_pixmap(url, pm)
        for li in items:
            self._set_tile_icon(li, pm)

    def _on_artwork_failed(self, url: str, tiles: Optional[List[QListWidgetItem]] = None) -> None:
        """The artwork URL is dead: fall back to iptv-org / TMDb for its tiles.

        Playlists routinely carry stale ``tvg-logo`` URLs; without this the
        tiles would keep the placeholder even though artwork exists upstream."""
        if tiles is None:
            tiles = self._artwork_requests.pop(url, [])
        self._requested_urls.discard(url)
        # One failure isn't proof the URL is dead — under a burst the CDN
        # resets connections. Give it another pass before falling back.
        self._fail_counts[url] = self._fail_counts.get(url, 0) + 1
        if self._fail_counts[url] < 2:
            return
        self._failed_urls.add(url)
        for li in tiles:
            it = li.data(Qt.UserRole)
            if it is None or id(it) in self._logo_tried:
                continue
            self._logo_tried.add(id(it))
            self._logo_pending.setdefault(id(it), []).append(li)
            if isinstance(it, Channel):
                it.logo = ""
                self._manager.resolve_channel_logo_async(it, self._on_logo_resolved)
            else:
                it.logo = ""
                it.poster = ""
                self._manager.resolve_poster_async(it, self._on_logo_resolved)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Re-fit posters to the new viewport, then load any newly visible tiles.
        self._recompute_tile_size()
        QTimer.singleShot(0, self._load_visible_artwork)

    # -- overlaid Select/Play buttons ----------------------------------------
    def _hit_button(self, pos: QPoint):
        """Return (item, 'select'|'play') if ``pos`` lands on an overlay button."""
        idx = self.indexAt(pos)
        if not idx.isValid():
            return None, None
        li = self.item(idx.row())
        if li is None:
            return None, None
        rect = self.visualItemRect(li)
        icon_rect, _ = PosterDelegate.tile_layout(rect, self.iconSize())
        select_rect, play_rect = PosterDelegate.button_rects(icon_rect)
        if select_rect.contains(pos):
            return li, "select"
        if play_rect.contains(pos):
            return li, "play"
        return None, None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            li, which = self._hit_button(event.position().toPoint() if hasattr(event, "position") else event.pos())
            if li is not None and which is not None:
                # Select the tile so the Select button reflects state, then
                # route the action — Play starts playback, Select opens info.
                self.setCurrentItem(li)
                it = li.data(Qt.UserRole)
                if it is not None:
                    if which == "play":
                        self.itemActivated.emit(it)
                    else:
                        self.itemSelected.emit(it)
                return  # swallow — don't fall through to default click handling
        super().mousePressEvent(event)

    def _on_activate(self, li: QListWidgetItem) -> None:
        it = li.data(Qt.UserRole)
        if it is not None:
            self.itemActivated.emit(it)

    def _on_select(self, li: QListWidgetItem) -> None:
        # A single click only opens the info panel — playback needs a double
        # click, so browsing a section doesn't start streams by accident.
        it = li.data(Qt.UserRole)
        if it is not None:
            self.itemSelected.emit(it)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        li = self.itemAt(event.pos())
        if li is None:
            return
        it = li.data(Qt.UserRole)
        menu = QMenu(self)
        act_play = menu.addAction("Play")
        act_fav = menu.addAction("Remove from Favorites" if getattr(it, "favorite", False) else "Add to Favorites")
        chosen = menu.exec(event.globalPos())
        if chosen == act_play:
            self.itemActivated.emit(it)
        elif chosen == act_fav:
            self._manager.toggle_favorite(it)
            self.viewport().update()


def _fmt_prog(title: str, start: int, end: int) -> str:
    """'14:30–16:00 Title' — programme times in the SYSTEM local timezone.

    The XMLTV offsets are baked into the stored epochs at parse time, so
    ``time.localtime`` renders whatever the user's clock says — no timezone
    setting to manage."""
    if not title:
        return ""
    if start and end:
        return (f"{time.strftime('%H:%M', time.localtime(start))}–"
                f"{time.strftime('%H:%M', time.localtime(end))}  {title}")
    return title


def _fmt_now_title(epg: Dict[str, Any]) -> str:
    """Format the "now" programme of an :meth:`IPTVCache.epg_now_next` dict."""
    if not isinstance(epg, dict):
        return ""  # defensive: mocked/broken managers return junk
    return _fmt_prog(epg.get("now") or "",
                     epg.get("now_start") or 0, epg.get("now_end") or 0)


class ContentList(QTableWidget):
    """List view alternative with columns: name, category, EPG now-playing."""

    itemActivated = Signal(object)

    def __init__(self, manager: IPTVManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self.setColumnCount(3)
        self.setHorizontalHeaderLabels(["Name", "Category", "Now Playing"])
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.itemDoubleClicked.connect(self._on_activate)
        self.verticalHeader().setDefaultSectionSize(20)
        self._items: List[Any] = []
        # Refresh the "Now Playing" column for visible rows periodically —
        # EPG data goes stale as programmes change.
        self._epg_timer = QTimer(self)
        self._epg_timer.setInterval(60_000)
        self._epg_timer.timeout.connect(self._refresh_visible_epg)
        self._epg_timer.start()

    def set_items(self, items: List[Any]) -> None:
        self._items = items
        self.setRowCount(len(items))
        for r, it in enumerate(items):
            name = getattr(it, "name", "") or getattr(it, "display_name", "")
            self.setItem(r, 0, QTableWidgetItem(name))
            self.setItem(r, 1, QTableWidgetItem(getattr(it, "group", "")))
            now = ""
            if isinstance(it, Channel) and (it.tvg_id or it.name):
                now = _fmt_now_title(self._manager.epg_now_next_for(it.tvg_id, it.name))
            self.setItem(r, 2, QTableWidgetItem(now))
            self.item(r, 0).setData(Qt.UserRole, it)

    def _refresh_visible_epg(self) -> None:
        """Update the Now Playing column for rows currently in the viewport."""
        if not self.isVisible() or self.rowCount() == 0:
            return
        first = max(0, self.rowAt(0))
        last = self.rowAt(self.viewport().height())
        if last < 0:
            last = self.rowCount() - 1
        for r in range(first, min(last + 1, self.rowCount())):
            name_item = self.item(r, 0)
            now_item = self.item(r, 2)
            if name_item is None or now_item is None:
                continue
            it = name_item.data(Qt.UserRole)
            if isinstance(it, Channel) and (it.tvg_id or it.name):
                now = _fmt_now_title(self._manager.epg_now_next_for(it.tvg_id, it.name))
                if now and now != now_item.text():
                    now_item.setText(now)

    def _on_activate(self, item: QTableWidgetItem) -> None:
        it = self.item(item.row(), 0).data(Qt.UserRole)
        if it is not None:
            self.itemActivated.emit(it)


# ---------------------------------------------------------------------------
# Detail panel for movies/series
# ---------------------------------------------------------------------------

class DetailPanel(QScrollArea):
    """Shows backdrop, synopsis, year, rating, genres, and episode list."""

    play_requested = Signal(object)  # an Episode or the parent item
    artwork_found = Signal(object, str)  # (item, poster url) — feeds the grid
    sig_artwork = Signal(str, object)    # (url, path) — marshals worker -> GUI
    sig_artwork_failed = Signal(str)     # (url) — fetch gave up, worker -> GUI
    sig_metadata = Signal(str, dict)     # (key, metadata) — marshals worker -> GUI
    closed = Signal()                    # user clicked the close button

    def __init__(self, manager: IPTVManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self.sig_artwork.connect(self._apply_artwork)
        self.sig_artwork_failed.connect(self._on_artwork_gone)
        self.sig_metadata.connect(self._apply_metadata)
        self.setWidgetResizable(True)
        self.setStyleSheet(
            "QScrollArea { background-color: rgba(10,10,15,0.96); "
            "border: 1px solid #1a2a4a; border-radius: 6px; }"
        )
        inner = QWidget()
        self._layout = QVBoxLayout(inner)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self.setWidget(inner)

        # Header row: title on the left, close button on the right.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.title = QLabel("")
        self.title.setStyleSheet("color: #2a7abf; font-size: 18px; font-weight: 700;")
        self.title.setWordWrap(True)
        header.addWidget(self.title, 1)
        self.close_btn = QPushButton("✕")
        self.close_btn.setToolTip("Hide details")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setStyleSheet(
            "QPushButton { background-color: rgba(42,122,191,0.25); "
            "border: 1px solid #2a7abf; border-radius: 4px; "
            "color: #c8d3e0; font-size: 14px; font-weight: bold; }\n"
            "QPushButton:hover { background-color: rgba(230,126,34,0.85); "
            "border-color: #e67e22; color: white; }"
        )
        self.close_btn.clicked.connect(self._on_close)
        header.addWidget(self.close_btn, 0, Qt.AlignTop)
        self._layout.addLayout(header)

        self.meta_lbl = QLabel("")
        self.meta_lbl.setStyleSheet("color: #8a9ab0; font-size: 12px;")
        self.meta_lbl.setWordWrap(True)
        self._layout.addWidget(self.meta_lbl)

        self.backdrop = QLabel("")
        self.backdrop.setMinimumHeight(220)
        self.backdrop.setStyleSheet("background-color: #111827; border: 1px solid #1a2a4a; border-radius: 6px;")
        self.backdrop.setAlignment(Qt.AlignCenter)
        self.backdrop.setScaledContents(False)
        # Preferred height (not Fixed) so the label grows to fit the scaled
        # image — a Fixed policy crops portrait posters scaled to full width.
        self.backdrop.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._layout.addWidget(self.backdrop)

        self.synopsis = QLabel("")
        self.synopsis.setStyleSheet("color: #c8d3e0; font-size: 13px;")
        self.synopsis.setWordWrap(True)
        self._layout.addWidget(self.synopsis)

        self.episodes_label = QLabel("Episodes")
        self.episodes_label.setObjectName("section_label")
        self._layout.addWidget(self.episodes_label)
        self.episodes_label.hide()

        self.episodes = QListWidget()
        # Single-click plays an episode — the user is already in the series
        # detail panel, so requiring a double-click is unintuitive. Keep
        # double-click too for habit's sake.
        self.episodes.itemClicked.connect(self._on_episode)
        self.episodes.itemDoubleClicked.connect(self._on_episode)
        self._layout.addWidget(self.episodes)
        self.episodes.hide()

        self._current: Any = None
        self._episodes: List[Any] = []
        self._artwork_pm: Optional[QPixmap] = None
        # Guards against stale async results: fast browsing must not apply the
        # previous item's metadata/artwork to the newly shown one.
        self._expected_meta_key = ""
        self._expected_artwork_url = ""

    def show_item(self, item: Any) -> None:
        self._current = item
        self._artwork_pm = None
        self.show()  # panel starts hidden until something is actually selected
        if isinstance(item, Channel):
            self._show_channel(item)
            return
        self.title.setText(getattr(item, "name", ""))
        year = getattr(item, "year", "") or extract_year(getattr(item, "name", ""))
        rating = getattr(item, "rating", 0.0)
        genres = getattr(item, "genres", []) or []
        self.meta_lbl.setText(
            f"{year}  •  ★ {rating:.1f}  •  {', '.join(genres) if genres else '—'}"
        )
        self.synopsis.setText(getattr(item, "synopsis", "") or "Loading metadata…")

        # Backdrop/poster artwork.
        bd = getattr(item, "backdrop", "") or getattr(item, "poster", "") or getattr(item, "logo", "")
        self._expected_artwork_url = bd
        self.backdrop.setText("Loading artwork…")
        self.backdrop.setPixmap(QPixmap())
        if bd:
            self._manager.fetch_artwork(bd, self._on_artwork)

        # Episodes for series.
        if isinstance(item, Series):
            self.episodes_label.show()
            self.episodes.show()
            self.episodes.clear()
            self._episodes = []
            seasons = sorted({e.season for e in item.episodes} or [1])
            for s in seasons:
                hdr = QListWidgetItem(f"— Season {s} —")
                hdr.setFlags(Qt.NoItemFlags)
                self.episodes.addItem(hdr)
                for ep in item.episodes_for(s):
                    li = QListWidgetItem(f"S{ep.season:02d}E{ep.episode:02d}  {ep.title or ep.name}")
                    li.setData(Qt.UserRole, ep)
                    self.episodes.addItem(li)
                    self._episodes.append(ep)
        else:
            self.episodes_label.hide()
            self.episodes.hide()

        # Resolve metadata on demand.
        section = getattr(item, "section", SECTION_MOVIES)
        self._expected_meta_key = metadata_key(section, getattr(item, "name", ""), year)
        self._manager.resolve_metadata(section, getattr(item, "name", ""), year, self._on_metadata)

    def _show_channel(self, ch: Any) -> None:
        """Detail panel for a live TV channel: logo, group, full EPG guide.

        No TMDb lookup — channels aren't movies. The EPG store is queried
        directly (ch.epg_now/epg_next were designed as item attributes but
        nothing ever populated them — the read was blank forever before the
        fix). Shows Now (with elapsed %), Next, and the next 24 h schedule."""
        self.title.setText(ch.display_name)
        guide = self._manager.epg_guide_for(getattr(ch, "tvg_id", ""),
                                            getattr(ch, "name", ""))
        if not isinstance(guide, dict):
            guide = {"now_next": {}, "programmes": []}  # defensive (mocked mgr)
        nn = guide["now_next"]
        parts = []
        if nn.get("now"):
            line = "Now: " + _fmt_prog(nn["now"],
                                       nn.get("now_start") or 0,
                                       nn.get("now_end") or 0)
            ns, ne = nn.get("now_start") or 0, nn.get("now_end") or 0
            if ns and ne > ns:
                pct = min(100, max(0, int((time.time() - ns) / (ne - ns) * 100)))
                line += f"  ({pct}%)"
            parts.append(line)
        if nn.get("next"):
            parts.append("Next: " + _fmt_prog(nn["next"],
                                              nn.get("next_start") or 0,
                                              nn.get("next_end") or 0))
        self.meta_lbl.setText(ch.group or "Live TV")
        self.synopsis.setText("\n".join(parts))
        rows = guide["programmes"]
        self.episodes.clear()
        self._episodes = []
        if rows:
            self.episodes_label.setText("Upcoming — next 24 h")
            for r in rows:
                li = QListWidgetItem(
                    f"{time.strftime('%H:%M', time.localtime(r['start']))}  {r['title']}")
                # Guide rows are display-only — clicking must not "play" them.
                li.setFlags(li.flags() & ~Qt.ItemIsSelectable)
                li.setData(Qt.UserRole, None)
                self.episodes.addItem(li)
            self.episodes_label.show()
            self.episodes.show()
        else:
            self.episodes_label.hide()
            self.episodes.hide()
        self._expected_meta_key = ""  # drop any in-flight movie/series metadata
        self._expected_artwork_url = ch.logo
        if ch.logo:
            self.backdrop.setText("Loading logo…")
            self.backdrop.setPixmap(QPixmap())
            self._manager.fetch_artwork(ch.logo, self._on_artwork)
        else:
            self.backdrop.setText("")
            self.backdrop.setPixmap(_placeholder_pixmap(ch.display_name))

    def _on_artwork(self, url: str, path: Optional[str]) -> None:
        if not path:
            self.sig_artwork_failed.emit(url)
            return
        self.sig_artwork.emit(url, path)

    def _on_artwork_gone(self, url: str) -> None:
        # Don't leave the panel stuck on "Loading…" when the URL is dead.
        if url != self._expected_artwork_url:
            return
        name = getattr(self._current, "name", "") or getattr(self._current, "display_name", "")
        if isinstance(self._current, Channel):
            self.backdrop.setText("")
            self.backdrop.setPixmap(_placeholder_pixmap(name))
        else:
            self.backdrop.setText("No artwork available")

    def _apply_artwork(self, url: str, path: str) -> None:
        # Drop artwork that belongs to a previously shown item.
        if url != self._expected_artwork_url:
            return
        pm = QPixmap(path)
        if not pm.isNull():
            self._artwork_pm = pm  # keep the full-res for rescaling on resize
            self._scale_backdrop()

    def _scale_backdrop(self) -> None:
        """Rescale the stored artwork to fit the panel, preserving aspect ratio.

        Scales to the viewport width (minus margins) but caps the height at
        60% of the viewport height — portrait posters scaled to full width
        would be taller than the panel and got cropped under the old Fixed
        height policy. When the width-based scale would exceed the height cap,
        the image is scaled to height instead so it always fits."""
        pm = getattr(self, "_artwork_pm", None)
        if pm is None or pm.isNull():
            return
        # viewport width is the reliable inner width (accounts for scrollbar).
        vw = max(200, self.viewport().width() - 24)  # 24 = left+right margins
        vh = self.viewport().height()
        max_h = max(220, int(vh * 0.6))  # cap at 60% of viewport, min 220px
        iw, ih = pm.width(), pm.height()
        if iw <= 0 or ih <= 0:
            return
        # Scale to width first; check if the resulting height fits.
        scaled_h = int(ih * vw / iw)
        if scaled_h <= max_h:
            # Width-driven: fits within the height cap.
            scaled = pm.scaledToWidth(vw, Qt.SmoothTransformation)
        else:
            # Height-driven: would exceed the cap, so scale to height instead.
            scaled = pm.scaledToHeight(max_h, Qt.SmoothTransformation)
        self.backdrop.setPixmap(scaled)
        # Grow the label to match the scaled image so it doesn't crop.
        self.backdrop.setMinimumHeight(scaled.height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._scale_backdrop()

    def _on_metadata(self, key: str, meta: Dict[str, Any]) -> None:
        if not meta or self._current is None:
            return
        self.sig_metadata.emit(key, meta)

    def _apply_metadata(self, key: str, meta: Dict[str, Any]) -> None:
        # Drop metadata that belongs to a previously shown item.
        if self._current is None or key != self._expected_meta_key:
            return
        if meta.get("synopsis"):
            self._current.synopsis = meta["synopsis"]
            self.synopsis.setText(meta["synopsis"])
        if meta.get("year"):
            self._current.year = meta["year"]
        if meta.get("rating"):
            self._current.rating = meta["rating"]
        if meta.get("genres"):
            self._current.genres = meta["genres"]
        if meta.get("poster") and not getattr(self._current, "poster", ""):
            self._current.poster = meta["poster"]
            # The grid tile is still showing a placeholder — hand it the URL
            # instead of making the user rebuild the section to see it.
            self.artwork_found.emit(self._current, meta["poster"])
        if meta.get("backdrop") and not getattr(self._current, "backdrop", ""):
            self._current.backdrop = meta["backdrop"]
        year = getattr(self._current, "year", "")
        rating = getattr(self._current, "rating", 0.0)
        genres = getattr(self._current, "genres", []) or []
        self.meta_lbl.setText(
            f"{year}  •  ★ {rating:.1f}  •  {', '.join(genres) if genres else '—'}"
        )
        bd = getattr(self._current, "backdrop", "") or getattr(self._current, "poster", "")
        if bd and (not self.backdrop.pixmap() or self.backdrop.pixmap().isNull()):
            self._expected_artwork_url = bd
            self._manager.fetch_artwork(bd, self._on_artwork)

    def _on_episode(self, li: QListWidgetItem) -> None:
        ep = li.data(Qt.UserRole)
        if ep is not None:
            self.play_requested.emit(ep)

    def _on_close(self) -> None:
        """Hide the panel — the host tab also clears the grid selection."""
        self.hide()
        self.closed.emit()


# ---------------------------------------------------------------------------
# Main IPTV tab
# ---------------------------------------------------------------------------

class IPTVTab(QWidget):
    """The top-level IPTV tab."""

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._signals = _IPTVSignals()
        self._signals.progress.connect(self._on_progress)
        self._signals.load_done.connect(self._on_load_done)
        self._signals.artwork_ready.connect(self._on_artwork_ready)
        self._signals.metadata_ready.connect(self._on_metadata_ready)
        self._signals.status.connect(self._on_status)
        self._signals.player_state.connect(self._on_player_state)
        self._signals.player_position.connect(self._on_player_position)
        self._signals.player_error.connect(self._on_player_error)

        self._manager = IPTVManager(
            sources=[_source_from_config(s) for s in config.iptv.sources],
            tmdb_api_key=config.iptv.tmdb_api_key,
            data_dir=config.iptv.cache_dir or None,
            cache_seconds=config.iptv.cache_seconds,
            hwdec=config.iptv.hwdec,
            tpdb_api_key=config.iptv.tpdb_api_key,
            framegrab_posters=config.iptv.framegrab_posters,
            stashdb_api_key=config.iptv.stashdb_api_key,
            omdb_api_key=config.iptv.omdb_api_key,
            fanarttv_api_key=config.iptv.fanarttv_api_key,
            enable_javbus=config.iptv.enable_javbus,
            enable_javlibrary=config.iptv.enable_javlibrary,
            enable_fanza=config.iptv.enable_fanza,
            enable_wikipedia=config.iptv.enable_wikipedia,
            cache_limit_mb=config.iptv.cache_limit_mb,
            epg_url=config.iptv.epg_url,
            enable_epg=config.iptv.enable_epg,
            xtream_series_concurrency=config.iptv.xtream_series_concurrency,
        )
        # Guides go stale independently of playlist refreshes — re-fetch any
        # guide older than 6h, checked hourly.
        self._epg_refresh_timer = QTimer(self)
        self._epg_refresh_timer.setInterval(3600_000)
        self._epg_refresh_timer.timeout.connect(
            lambda: self._manager.maybe_refresh_epg())
        self._epg_refresh_timer.start()
        self._current_section = SECTION_LIVE
        self._current_category = ""
        self._current_year = ""  # set when a year node is selected
        # Which source the content pane is showing. Every enabled source is
        # loaded and listed in the tree; this is just the current view.
        self._current_source_id = ""
        self._build_ui()
        self._populate_source_dropdown()
        # Show the source tree straight away (nodes read "loading…" until
        # their playlist lands), then load every enabled source in the
        # background — the first to arrive becomes the visible one.
        self._rebuild_sidebar()
        if any(s.enabled for s in self._manager.sources):
            self._refresh()

    # -- UI construction -----------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # --- Toolbar (in a widget so fullscreen mode can hide it) ---
        self._toolbar_w = QWidget()
        toolbar = QHBoxLayout(self._toolbar_w)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)
        self._source_combo = QComboBox()
        self._source_combo.setMinimumWidth(180)
        self._source_combo.currentTextChanged.connect(self._on_source_changed)
        toolbar.addWidget(QLabel("Source:"))
        toolbar.addWidget(self._source_combo)

        self._refresh_btn = QPushButton("⟳")
        self._refresh_btn.setToolTip("Refresh playlist")
        self._refresh_btn.clicked.connect(self._refresh)
        toolbar.addWidget(self._refresh_btn)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search channels, movies, series…")
        # Debounce: searching 50k entries on every keystroke stutters the UI.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(lambda: self._on_search(self._search.text()))
        self._search.textChanged.connect(lambda _t: self._search_timer.start())
        toolbar.addWidget(self._search, 1)

        self._view_grid_btn = QPushButton("▦ Grid")
        self._view_grid_btn.setObjectName("btn_accent")
        self._view_grid_btn.clicked.connect(lambda: self._set_view("grid"))
        toolbar.addWidget(self._view_grid_btn)

        self._view_list_btn = QPushButton("≡ List")
        self._view_list_btn.clicked.connect(lambda: self._set_view("list"))
        toolbar.addWidget(self._view_list_btn)

        self._open_file_btn = QPushButton("▶ Open File")
        self._open_file_btn.setToolTip("Play a local video file")
        self._open_file_btn.clicked.connect(self._open_local_file)
        toolbar.addWidget(self._open_file_btn)

        self._settings_btn = QPushButton("⚙")
        self._settings_btn.setToolTip("IPTV Settings")
        toolbar.addWidget(self._settings_btn)
        layout.addWidget(self._toolbar_w)

        # --- Body: splitter sidebar | content | player ---
        splitter = QSplitter(Qt.Horizontal)

        # Sidebar (grouping picker + section/category tree).
        sidebar = QWidget()
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(0, 0, 0, 0)
        group_row = QHBoxLayout()
        group_row.setContentsMargins(4, 2, 4, 2)
        group_row.addWidget(QLabel("Group:"))
        self._group_combo = QComboBox()
        self._group_combo.addItem("Categories", "category")
        self._group_combo.addItem("Years", "year")
        self._group_combo.setToolTip(
            "Group Movies/Series by provider category or by release year")
        gidx = self._group_combo.findData(self._group_mode())
        self._group_combo.setCurrentIndex(gidx if gidx >= 0 else 0)
        # Connect after setCurrentIndex so restoring the saved mode doesn't
        # fire a spurious change (which would re-save the config).
        self._group_combo.currentIndexChanged.connect(self._on_group_mode_changed)
        group_row.addWidget(self._group_combo, 1)
        sb_layout.addLayout(group_row)
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setMinimumWidth(160)
        self._tree.itemClicked.connect(self._on_tree_click)
        sb_layout.addWidget(self._tree)
        splitter.addWidget(sidebar)
        self._sidebar = sidebar

        # Content area: just the stacked grid/list. The detail/metadata panel
        # is no longer wedged under the grid (which used to squeeze the other
        # posters) — it now floats as a separate zone over the player pane and
        # hides as soon as playback starts (see _play_item / _layout_detail).
        content = QWidget()
        cl_layout = QVBoxLayout(content)
        cl_layout.setContentsMargins(0, 0, 0, 0)
        self._grid = ContentGrid(self._manager)
        self._list = ContentList(self._manager)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._grid)
        self._stack.addWidget(self._list)
        self._stack.setCurrentWidget(self._grid)
        cl_layout.addWidget(self._stack)
        splitter.addWidget(content)
        self._content = content

        self._grid.sweep_progress.connect(self._on_artwork_progress)

        # Player pane.
        player_col = QWidget()
        pl_layout = QVBoxLayout(player_col)
        pl_layout.setContentsMargins(0, 0, 0, 0)
        self._player = PlayerWidget(self._manager, self._config)
        self._player.sig_fullscreen.connect(self._on_player_fullscreen)
        self._player.sig_compact.connect(self._on_player_compact)
        self._player.sig_retry.connect(self._grid.set_retry_count)
        # Player state → IPTVTab so it can stop the loading pulse / set the
        # playing marker on the grid. (_signals.player_state is the agent
        # bridge path; this is the direct GUI path that actually fires.)
        self._player.sig_state.connect(self._on_player_state)
        self._player.sig_error.connect(self._on_player_error)
        self._player.sig_playback_started.connect(self._on_playback_started)
        pl_layout.addWidget(self._player)
        splitter.addWidget(player_col)

        # Detail/metadata panel — a separate zone overlaid on the player's video
        # area. It shows on selection and disappears when content starts playing.
        self._detail = DetailPanel(self._manager)
        self._detail.play_requested.connect(self._play_item)
        self._detail.artwork_found.connect(self._grid.apply_external_artwork)
        self._detail.closed.connect(self._on_detail_closed)
        self._detail.setParent(self._player)
        self._detail.hide()
        self._player.installEventFilter(self)
        # Content needs enough width for a 3×3 poster grid; the player pane
        # gets the remainder. Sidebar stays narrow (tree only).
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 5)
        splitter.setSizes([160, 560, 720])

        layout.addWidget(splitter, 1)

        # --- Status bar (in a widget so fullscreen mode can hide it) ---
        self._status_w = QWidget()
        status = QHBoxLayout(self._status_w)
        status.setContentsMargins(0, 0, 0, 0)
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet("color: #8a9ab0; font-size: 11px;")
        status.addWidget(self._status_lbl)
        status.addStretch()
        # Artwork sweep indicator — the lookups are deliberately slow (rate
        # limited), so show the progress rather than leaving the user
        # wondering whether the blank tiles are ever going to fill in.
        self._art_lbl = QLabel("")
        self._art_lbl.setStyleSheet("color: #6f7f95; font-size: 11px;")
        self._art_lbl.hide()
        status.addWidget(self._art_lbl)
        self._art_progress = QProgressBar()
        self._art_progress.setMaximumWidth(120)
        self._art_progress.setMaximumHeight(10)
        self._art_progress.setTextVisible(False)
        self._art_progress.hide()
        status.addWidget(self._art_progress)
        self._progress = QProgressBar()
        self._progress.setMaximumWidth(240)
        self._progress.setMaximumHeight(14)
        self._progress.hide()
        status.addWidget(self._progress)
        layout.addWidget(self._status_w)

        # Wire content activation -> play/detail.
        self._grid.itemActivated.connect(self._on_item_activated)
        self._grid.itemSelected.connect(self._on_item_selected)
        self._list.itemActivated.connect(self._on_item_activated)

    # -- sources -------------------------------------------------------------
    def _populate_source_dropdown(self) -> None:
        self._source_combo.blockSignals(True)
        self._source_combo.clear()
        for s in self._manager.sources:
            if s.enabled:
                self._source_combo.addItem(s.name, s.id)
        self._source_combo.blockSignals(False)

    def _sync_source_combo(self, source_id: str) -> None:
        idx = self._source_combo.findData(source_id)
        if idx >= 0 and idx != self._source_combo.currentIndex():
            self._source_combo.blockSignals(True)
            self._source_combo.setCurrentIndex(idx)
            self._source_combo.blockSignals(False)

    def _on_source_changed(self, _name: str) -> None:
        """Dropdown is a jump, not a reload: every source is already loaded."""
        sid = self._source_combo.currentData()
        if not sid:
            return
        self._current_source_id = sid
        self._manager.set_active_source(sid)
        self._current_category = ""
        self._current_year = ""
        self._select_tree_node((sid, self._current_section, ""))
        self._show_section(self._current_section, "", sid)

    def _refresh(self) -> None:
        """Load every enabled source in the background.

        Each one publishes its playlist as it lands, so the first provider is
        usable while the rest are still downloading."""
        sources = [s for s in self._manager.sources if s.enabled]
        if not sources:
            self._set_status("No source configured. Open Settings → IPTV to add one.")
            return
        self._progress.show()
        self._progress.setRange(0, 0)
        self._pending_sources = len(sources)
        self._set_status(f"Loading {len(sources)} source(s)…"
                         if len(sources) > 1 else f"Loading {sources[0].name}…")
        self._manager.load_all_async(
            on_progress=lambda c, t: self._signals.progress.emit(c, t),
            on_done=lambda ok, pl: self._signals.load_done.emit(ok, pl),
        )

    # -- signal handlers (GUI thread) ----------------------------------------
    def _on_progress(self, count: int, total) -> None:
        if total:
            self._progress.setRange(0, total)
            self._progress.setValue(count)
        else:
            self._progress.setRange(0, 0)
        self._set_status(f"Loading… {count} entries")

    def _on_load_done(self, ok: bool, pl: Any) -> None:
        """One source finished. Others may still be loading behind it."""
        loaded = set(self._manager.loaded_source_ids())
        enabled = [s for s in self._manager.sources if s.enabled]
        remaining = [s for s in enabled if s.id not in loaded]
        stale_note = ""
        if getattr(pl, "stale", False):
            # Refresh failed; the manager kept the previous cached copy.
            stale_note = " (refresh failed — showing cached data)"
        if remaining:
            source = next((s for s in enabled if s.id == pl.source_id), None)
            if source is not None and (pl.total == 0 or getattr(pl, "error", None)):
                prefix = _source_empty_message(source, pl)
            else:
                prefix = (f"Loaded {pl.total} entries from "
                          f"{self._source_name(pl.source_id)}{stale_note}")
            self._set_status(f"{prefix} — {len(remaining)} source(s) still loading…")
        else:
            self._progress.hide()
            total = 0
            stale = []
            empty_messages = []
            for source in enabled:
                p = self._manager.playlist_for(source.id)
                if p is None:
                    continue
                total += p.total
                if p.stale:
                    stale.append(self._source_name(source.id))
                if p.total == 0 or getattr(p, "error", None):
                    message = _source_empty_message(source, p)
                    if message not in empty_messages:
                        empty_messages.append(message)
            if total == 0:
                self._set_status("; ".join(empty_messages) if empty_messages else
                                 "No entries loaded. Check the source URL/credentials "
                                 "in Settings → IPTV.")
            else:
                notes = []
                if stale:
                    notes.append("refresh failed for " + ", ".join(stale) +
                                 "; showing cached data")
                if empty_messages:
                    notes.extend(empty_messages)
                suffix = f" — {'; '.join(notes)}" if notes else ""
                self._set_status(f"Loaded {total} entries from "
                                 f"{len(enabled)} source(s){suffix}")
        # First source to arrive becomes the visible one.
        first = not self._current_source_id
        if first and pl.source_id:
            self._current_source_id = pl.source_id
            self._sync_source_combo(pl.source_id)
        self._rebuild_sidebar()
        if first or pl.source_id == self._current_source_id:
            self._show_section(self._current_section, self._current_category,
                               self._current_source_id, year=self._current_year)

    def _source_name(self, source_id: str) -> str:
        for s in self._manager.sources:
            if s.id == source_id:
                return s.name
        return source_id

    def _on_artwork_ready(self, url: str, path: str) -> None:
        # Grid/list apply artwork via their own callbacks; this is a no-op hook.
        pass

    def _on_metadata_ready(self, key: str, meta: dict) -> None:
        pass

    def _on_artwork_progress(self, done: int, total: int) -> None:
        """Show how far the missing-artwork sweep has got for this section."""
        if not total or done >= total:
            self._art_lbl.hide()
            self._art_progress.hide()
            return
        self._art_lbl.setText(f"Finding artwork  {done}/{total}")
        self._art_progress.setRange(0, total)
        self._art_progress.setValue(done)
        self._art_lbl.show()
        self._art_progress.show()

    def _on_status(self, msg: str) -> None:
        self._set_status(msg)

    def _on_player_state(self, state: str) -> None:
        self._set_status(f"Player: {state}")
        # NOTE: the backend emits "playing" immediately on loadfile, before the
        # stream is actually open — so we do NOT clear loading here. Real
        # playback-start is detected by _update_loading (buffer_status poll)
        # and signalled via sig_playback_started → _on_playback_started.
        if state in ("stopped", "ended", "idle"):
            # Clear both the loading pulse and the orange "Playing" marker.
            self._grid.set_loading_item(None)
            self._grid.set_playing_item(None)

    def _on_playback_started(self) -> None:
        """Real playback detected (buffer_status confirms time-pos advancing).
        Stop the loading pulse and mark the tile as playing."""
        self._grid.set_loading_item(None)
        if self._player._current_item is not None:
            self._grid.set_playing_item(self._player._current_item)

    def _on_player_position(self, pos: float, dur: float) -> None:
        pass

    def _on_player_error(self, msg: str) -> None:
        self._set_status(f"Player error: {msg}")
        self._grid.set_loading_item(None)

    def sync_fullscreen_chrome(self, on: bool) -> None:
        """Force the chrome to match the window's real fullscreen state.

        Called by MainWindow.changeEvent: the window can leave fullscreen
        without the player knowing (OS shortcuts, showNormal() from the tray
        / open-target / agent-playback paths), and the menu bar would stay
        hidden forever."""
        self._on_player_fullscreen(on)
        self._player._set_controls_autohide(on)

    def _on_player_compact(self, on: bool) -> None:
        """Collapse chrome around the existing surface for compact host mode.

        Unlike conventional PiP this never reparents or recreates ``surface``;
        the whole top-level host is resized and Windows topmost state is changed
        through SetWindowPos by PlayerWidget.
        """
        self._on_player_fullscreen(on)

    def _on_player_fullscreen(self, on: bool) -> None:
        """Hide/show everything around the player so fullscreen is video-only.

        The video widget itself can't be detached into its own window (the
        mpv/VLC embed is tied to its native winId), so instead the tab and
        main-window chrome collapse around it."""
        for w in (self._toolbar_w, self._sidebar, self._content, self._status_w):
            w.setVisible(not on)
        # The metadata overlay is part of the player pane — collapse it too so
        # fullscreen is video-only, but remember whether it was open so it can
        # come back when fullscreen exits (it hides on play anyway).
        if on:
            self._detail_was_visible = self._detail.isVisible()
            self._detail.hide()
        elif getattr(self, "_detail_was_visible", False):
            self._detail.show()
            self._layout_detail_overlay()
            self._detail_was_visible = False
        # Also hide the main window's menu bar (best-effort). The main tab
        # bar is NOT touched: it is permanently hidden (navigation lives in
        # the menus), and un-hiding it on the way out of fullscreen left an
        # empty strip under the menu bar.
        win = self.window()
        try:
            menubar = win.menuBar() if hasattr(win, "menuBar") else None
            if menubar is not None:
                menubar.setVisible(not on)
            statusbar = win.statusBar() if hasattr(win, "statusBar") else None
            if statusbar is not None:
                statusbar.setVisible(not on)
            chrome = getattr(win, "set_video_fullscreen_chrome", None)
            if chrome is not None:
                chrome(on)
        except Exception:
            pass

    # -- detail/metadata overlay --------------------------------------------
    def eventFilter(self, obj, event):  # noqa: N802
        """Reposition the metadata overlay when the player pane resizes."""
        if obj is self._player and event.type() == QEvent.Resize:
            self._layout_detail_overlay()
        return super().eventFilter(obj, event)

    def _layout_detail_overlay(self) -> None:
        """Float the detail panel over the player's video area (left portion).

        It never covers the control bar, and it hides on play so it never sits
        on top of active video."""
        vs = self._player.video_stack
        if vs is None:
            return
        # Position relative to the PlayerWidget (the detail's parent).
        origin = vs.mapTo(self._player, QPoint(0, 0))
        # Wider panel so the backdrop image fits comfortably with margins.
        w = min(560, max(360, vs.width() * 2 // 5))
        self._detail.setGeometry(origin.x(), origin.y(), w, vs.height())
        self._detail.raise_()

    # -- sidebar -------------------------------------------------------------
    _SECTIONS = [
        (SECTION_LIVE, "Live TV"),
        (SECTION_MOVIES, "Movies"),
        (SECTION_SERIES, "Series"),
        (SECTION_FAVORITES, "Favorites"),
        (SECTION_RECENT, "Recently Watched"),
    ]

    def _group_mode(self) -> str:
        """Sidebar grouping for VOD sections: "category" (default) or "year"."""
        return getattr(self._config.iptv, "vod_group_mode", "category")

    def _on_group_mode_changed(self, _idx: int) -> None:
        mode = self._group_combo.currentData() or "category"
        if mode == self._group_mode():
            return
        self._config.iptv.vod_group_mode = mode
        try:
            self._config.to_file(DeeptorrentConfig.default_config_path())
        except OSError:
            pass
        # A category selection doesn't translate to a year one — drop back to
        # the whole section under the new grouping.
        self._current_category = ""
        self._current_year = ""
        self._rebuild_sidebar()
        self._show_section(self._current_section,
                           source_id=self._current_source_id or None)

    def _tree_state(self) -> tuple:
        """Snapshot which nodes are expanded, keyed by their data tuple.

        The tree is rebuilt every time a source finishes loading, so without
        this the user's expansion and selection would be thrown away several
        times during startup."""
        expanded = set()

        def _walk(node: QTreeWidgetItem) -> None:
            data = node.data(0, Qt.UserRole)
            if data is not None and node.isExpanded():
                expanded.add(data)
            for i in range(node.childCount()):
                _walk(node.child(i))

        for i in range(self._tree.topLevelItemCount()):
            _walk(self._tree.topLevelItem(i))
        current = self._tree.currentItem()
        return expanded, (current.data(0, Qt.UserRole) if current else None)

    def _rebuild_sidebar(self) -> None:
        """Source -> Section -> Category.

        Every enabled source is listed at once (they all stay loaded), so
        moving between providers is just a click instead of a reload."""
        expanded, selected = self._tree_state()
        self._tree.clear()
        sources = [s for s in self._manager.sources if s.enabled]
        loaded = set(self._manager.loaded_source_ids())
        # With a single source the extra level is pure friction — expand it.
        only_one = len(sources) == 1

        for src in sources:
            key = (src.id, "", "")
            top = QTreeWidgetItem([src.name])
            top.setData(0, Qt.UserRole, key)
            self._tree.addTopLevelItem(top)
            if src.id not in loaded:
                top.setText(0, f"{src.name}  (loading…)")
                continue
            pl = self._manager.playlist_for(src.id)
            top.setText(0, f"{src.name} ({pl.total if pl else 0})")
            for sid, label in self._SECTIONS:
                sec_key = (src.id, sid, "")
                sec = QTreeWidgetItem([f"{label} ({self._section_count(sid, src.id)})"])
                sec.setData(0, Qt.UserRole, sec_key)
                top.addChild(sec)
                if sid in (SECTION_LIVE, SECTION_MOVIES, SECTION_SERIES):
                    # Movies/Series sub-group each category by release year in
                    # year mode. Years hang UNDER the category so the
                    # provider's own separation (e.g. "Movie VOD" vs
                    # "XXX VOD") survives; a year node is a 4-tuple
                    # (src, section, category, year).
                    by_year = (sid in (SECTION_MOVIES, SECTION_SERIES)
                               and self._group_mode() == "year")
                    # One pass over the section — NOT years_for per category
                    # (thousands of categories x the full section each time).
                    year_map = (self._manager.years_by_category(sid, src.id)
                                if by_year else {})
                    for cat in self._manager.categories_for(sid, src.id):
                        child = QTreeWidgetItem([f"{cat.name} ({cat.count})"])
                        child.setData(0, Qt.UserRole, (src.id, sid, cat.name))
                        sec.addChild(child)
                        if by_year:
                            for yc in year_map.get(cat.name, []):
                                self._add_year_or_bulk_node(
                                    child, src.id, sid, cat.name, yc, expanded)
                            child.setExpanded((src.id, sid, cat.name) in expanded)
                        else:
                            # Category mode: if the category itself is too
                            # large, add bulk children directly under it.
                            self._add_bulk_nodes_if_needed(
                                child, src.id, sid, cat.name, "",
                                cat.count, expanded)
                sec.setExpanded(sec_key in expanded)
            top.setExpanded(only_one or key in expanded
                            or src.id == self._current_source_id)

        if selected is not None:
            self._select_tree_node(selected)

    def _add_year_or_bulk_node(self, parent: QTreeWidgetItem, src_id: str,
                               sid: str, cat_name: str, yc, expanded: set) -> None:
        """Add a year node under a category. If the year bucket has more than
        BULK_SIZE items, add numbered bulk children instead of making the year
        node a clickable leaf."""
        ynode = QTreeWidgetItem([f"{yc.name} ({yc.count})"])
        ynode.setData(0, Qt.UserRole, (src_id, sid, cat_name, yc.name))
        parent.addChild(ynode)
        self._add_bulk_nodes_if_needed(
            ynode, src_id, sid, cat_name, yc.name, yc.count, expanded)

    def _add_bulk_nodes_if_needed(self, parent: QTreeWidgetItem, src_id: str,
                                  sid: str, cat_name: str, year: str,
                                  count: int, expanded: set) -> None:
        """If ``count`` exceeds BULK_SIZE, add numbered bulk children under
        ``parent``. Each bulk node is a 5-tuple key
        (src, section, category, year, bulk_index) — 1-based."""
        if count <= BULK_SIZE:
            return
        n_bulks = (count + BULK_SIZE - 1) // BULK_SIZE
        for i in range(n_bulks):
            start = i * BULK_SIZE + 1
            end = min((i + 1) * BULK_SIZE, count)
            label = f"{start}\u2013{end}"  # en-dash
            bnode = QTreeWidgetItem([label])
            bnode.setData(0, Qt.UserRole,
                          (src_id, sid, cat_name, year, i + 1))
            parent.addChild(bnode)
        parent.setExpanded(tuple(parent.data(0, Qt.UserRole)) in expanded)

    def _select_tree_node(self, key: tuple) -> None:
        """Restore selection after a rebuild (no-op if the node is gone)."""
        def _walk(node: QTreeWidgetItem):
            if node.data(0, Qt.UserRole) == key:
                return node
            for i in range(node.childCount()):
                hit = _walk(node.child(i))
                if hit is not None:
                    return hit
            return None

        for i in range(self._tree.topLevelItemCount()):
            hit = _walk(self._tree.topLevelItem(i))
            if hit is not None:
                self._tree.setCurrentItem(hit)
                return

    def _section_count(self, sid: str, source_id: Optional[str] = None) -> int:
        if sid == SECTION_FAVORITES:
            return len(self._manager.favorites(source_id))
        if sid == SECTION_RECENT:
            return len(self._manager.recent(source_id))
        return len(self._manager.items_for(sid, source_id=source_id))

    def _on_tree_click(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.UserRole)
        if data is None:
            return
        # Keys are 3-tuples (source/section/category nodes), 4-tuples
        # (year nodes under a category), or 5-tuples (bulk nodes under a
        # year or category). Pad so all unpack the same way.
        key = tuple(data)
        source_id, section, category = (key + ("", "", ""))[:3]
        year = key[3] if len(key) > 3 else ""
        bulk = key[4] if len(key) > 4 else 0
        if source_id and source_id != self._current_source_id:
            self._current_source_id = source_id
            self._manager.set_active_source(source_id)
            self._sync_source_combo(source_id)
        if not section:
            # Clicked the source itself — show its default section.
            item.setExpanded(not item.isExpanded())
            section, category, year = self._current_section, "", ""
            bulk = 0
        elif bulk == 0 and self._has_bulk_children(item):
            # Parent node with bulk children (category or year with >500
            # items) — expand/collapse instead of showing all items, so the
            # metadata scraper never processes tens of thousands of entries.
            item.setExpanded(not item.isExpanded())
            return
        self._current_section = section
        self._current_category = category
        self._current_year = year
        self._show_section(section, category, source_id, year=year, bulk=bulk)

    @staticmethod
    def _has_bulk_children(item: QTreeWidgetItem) -> bool:
        """True if any child of ``item`` is a bulk node (5-tuple key)."""
        for i in range(item.childCount()):
            child_key = tuple(item.child(i).data(0, Qt.UserRole) or ())
            if len(child_key) >= 5:
                return True
        return False

    # -- content display -----------------------------------------------------
    def _show_section(self, section: str, category: str = "",
                      source_id: Optional[str] = None, year: str = "",
                      bulk: int = 0) -> None:
        source_id = source_id or self._current_source_id or None
        if section == SECTION_FAVORITES:
            items = self._manager.favorites(source_id)
        elif section == SECTION_RECENT:
            # Recent is a list of dicts; surface as lightweight playable rows.
            items = []
            for r in self._manager.recent(source_id):
                # Build a transient channel-like object for playback.
                ch = Channel(id=r["item_id"], name=r["name"], url=r["url"], section=r["section"])
                items.append(ch)
        else:
            items = self._manager.items_for(section, category, source_id=source_id,
                                            year=year)
        # Slice to the requested bulk (1-based index) when specified.
        if bulk > 0:
            start = (bulk - 1) * BULK_SIZE
            items = items[start:start + BULK_SIZE]
        self._set_content_items(items)

    def _set_content_items(self, items: List[Any]) -> None:
        """Populate only the visible view; the hidden one is filled lazily on
        view switch (populating both doubles the cost for huge playlists)."""
        self._current_items = items
        if self._stack.currentWidget() is self._grid:
            self._grid.set_items(items)
            self._list_synced = False
        else:
            self._list.set_items(items)
            self._grid_synced = False

    def _set_view(self, view: str) -> None:
        if view == "grid":
            if not getattr(self, "_grid_synced", True):
                self._grid.set_items(getattr(self, "_current_items", []))
            self._grid_synced = True
            self._stack.setCurrentWidget(self._grid)
            self._view_grid_btn.setObjectName("btn_accent")
            self._view_list_btn.setObjectName("")
        else:
            if not getattr(self, "_list_synced", True):
                self._list.set_items(getattr(self, "_current_items", []))
            self._list_synced = True
            self._stack.setCurrentWidget(self._list)
            self._view_grid_btn.setObjectName("")
            self._view_list_btn.setObjectName("btn_accent")
        self._view_grid_btn.style().polish(self._view_grid_btn)
        self._view_list_btn.style().polish(self._view_list_btn)

    def _on_search(self, text: str) -> None:
        text = text.strip()
        if not text:
            self._show_section(self._current_section, self._current_category,
                               year=self._current_year)
            return
        results = self._manager.search(text)
        items: List[Any] = []
        for section in (SECTION_LIVE, SECTION_MOVIES, SECTION_SERIES):
            items.extend(results.get(section, []))
        self._set_content_items(items)

    def _on_item_selected(self, item: Any) -> None:
        """Single click (or Select button): info only, never playback."""
        self._detail.show_item(item)
        self._layout_detail_overlay()

    def _on_detail_closed(self) -> None:
        """User clicked the panel's ✕ — clear the grid selection too so the
        Select button de-highlights and the poster looks unselected again."""
        self._grid.clearSelection()
        self._grid.setCurrentItem(None)

    def _on_item_activated(self, item: Any) -> None:
        # Play button (or double click) plays. Series are the exception: their
        # url is empty (the user picks an episode from the detail panel), so
        # there is nothing to play yet — open the metadata overlay instead.
        if isinstance(item, Series):
            self._detail.show_item(item)
            self._layout_detail_overlay()
        else:
            self._play_item(item)

    def _play_item(self, item: Any) -> None:
        # Metadata overlay is a separate zone over the idle player — once
        # content starts playing it gets out of the way so video is unobstructed.
        self._detail.hide()
        name = getattr(item, "name", "") or getattr(item, "display_name", "")
        status = f"Loading {name}…" if name else "Loading stream…"
        if isinstance(item, Channel):
            # Show what's actually on: "Loading CNN… — 18:30–19:00  News Hour".
            now_line = _fmt_now_title(self._manager.epg_now_next_for(
                getattr(item, "tvg_id", ""), name))
            if now_line:
                status += f"  —  {now_line}"
        self._set_status(status)
        self._grid.set_loading_item(item)  # pulse the Play button while opening
        self._player.play(item)

    # -- local file playback (generic player) --------------------------------
    _VIDEO_EXT = ("*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.mpg "
                  "*.mpeg *.ts *.m2ts *.vob *.3gp *.ogv")
    _AUDIO_EXT = "*.mp3 *.flac *.m4a *.aac *.ogg *.opus *.wav *.wma *.aiff *.alac"
    # Audio first-class: the default filter shows both, so music files aren't
    # hidden behind a dropdown switch.
    VIDEO_FILTER = (
        f"Media Files ({_VIDEO_EXT} {_AUDIO_EXT});;"
        f"Video Files ({_VIDEO_EXT});;"
        f"Audio Files ({_AUDIO_EXT});;All Files (*)"
    )

    def _open_local_file(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, "Open Media File", "", self.VIDEO_FILTER)
        if path:
            self.play_file(path)

    def play_file(self, path: str) -> None:
        """Play a local media file (mpv/VLC decode virtually any codec)."""
        if not os.path.isfile(path):
            self._set_status(f"File not found: {path}")
            return
        ch = Channel(id=path, name=os.path.basename(path), url=path, section=SECTION_MOVIES)
        self._player.play(ch)
        self._set_status(f"Playing {os.path.basename(path)}")

    def play_web_stream(self, url: str, title: str = "", headers: Optional[Dict[str, str]] = None) -> None:
        """Play a remote stream (HLS/DASH/direct file) captured from the browser.

        mpv decodes proprietary codecs (H.264/AAC) that the built-in
        QtWebEngine browser lacks, so this is the in-app playback path for
        HTML5-unplayable videos."""
        if not url:
            return
        name = title or url.rstrip("/").rsplit("/", 1)[-1] or "Web stream"
        ch = Channel(id=url, name=name, url=url, section=SECTION_MOVIES,
                     extra={"headers": dict(headers or {})})
        self._player.play(ch)
        self._set_status(f"Playing {name}")

    # -- settings hook (wired by MainWindow) ---------------------------------
    @property
    def manager(self) -> IPTVManager:
        """The live IPTVManager — settings pages use it for cache stats/clear."""
        return self._manager

    def reset_artwork_state(self) -> None:
        """An external cache clear wiped artwork/metadata: drop in-memory
        memos so tiles re-resolve instead of showing dead pixmaps."""
        self._grid.reset_artwork_state()

    def set_settings_callback(self, cb) -> None:
        self._settings_btn.clicked.connect(cb)

    def set_engine(self, engine: Any) -> None:
        """Give the player access to the torrent engine for QoS throttling."""
        self._player.set_engine(engine)

    def reload_config(self, config: DeeptorrentConfig) -> None:
        """Re-apply config after the user edits IPTV settings."""
        self._config = config
        self._manager.set_sources([_source_from_config(s) for s in config.iptv.sources])
        self._manager.set_tmdb_key(config.iptv.tmdb_api_key)
        self._manager.set_tpdb_key(config.iptv.tpdb_api_key)
        self._manager.set_stashdb_key(config.iptv.stashdb_api_key)
        self._manager.set_omdb_key(config.iptv.omdb_api_key)
        self._manager.set_fanarttv_key(config.iptv.fanarttv_api_key)
        self._manager.cache_seconds = config.iptv.cache_seconds
        self._manager.hwdec = config.iptv.hwdec
        self._manager.cache_limit_mb = config.iptv.cache_limit_mb
        self._manager.set_framegrab_enabled(config.iptv.framegrab_posters)
        self._manager.set_epg(config.iptv.epg_url, config.iptv.enable_epg)
        # Apply buffer/hwdec changes to the running player immediately.
        self._player.apply_config()
        self._populate_source_dropdown()
        # The viewed source may have just been deleted or disabled.
        if self._current_source_id not in {s.id for s in self._manager.sources if s.enabled}:
            self._current_source_id = ""
        self._refresh()

    # -- status helper -------------------------------------------------------
    def _set_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    # -- lifecycle -----------------------------------------------------------
    def shutdown(self) -> None:
        self._player.shutdown()
        self._manager.shutdown()


# ---------------------------------------------------------------------------
# Config -> model adapter
# ---------------------------------------------------------------------------

def _source_from_config(s: IPTVSourceConfig):
    from iptv.models import PlaylistSource

    return PlaylistSource(
        id=s.id,
        name=s.name,
        kind=s.kind,
        url=s.url,
        user_agent=s.user_agent,
        referer=s.referer,
        username=s.username,
        password=s.password,
        enabled=s.enabled,
        auto_refresh_minutes=s.auto_refresh_minutes,
        epg_url=getattr(s, "epg_url", ""),
    )


# ---------------------------------------------------------------------------
# Agent bridge: agent tools (worker threads) -> IPTV tab (GUI thread)
# ---------------------------------------------------------------------------

class _AgentBridgeSignals(QObject):
    play_item = Signal(object)       # Channel/Movie from the playlist
    play_url = Signal(str, str)      # (url, title)
    play_file = Signal(str)
    stop = Signal()
    pause = Signal()
    set_volume = Signal(int)
    add_subs = Signal(str)           # subtitle file path → load into player


class AgentIPTVBridge:
    """Thread-safe bridge between the agent's iptv_* tools and the Play tab.

    Agent tools run on AgentLoop worker threads, so every playback action is
    emitted as a queued Qt signal and executed on the GUI thread. Reads go
    through the IPTVManager (internally locked) and a state snapshot fed by
    the player's signals, so no tool ever touches Qt objects off-thread."""

    def __init__(self, tab: IPTVTab) -> None:
        self._tab = tab
        self.manager: IPTVManager = tab._manager
        self._state_lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "state": "stopped", "position": 0.0, "duration": 0.0, "title": "", "url": "",
        }
        s = self._signals = _AgentBridgeSignals()
        s.play_item.connect(self._do_play_item)
        s.play_url.connect(self._do_play_url)
        s.play_file.connect(self._do_play_file)
        s.stop.connect(tab._player.stop)
        s.pause.connect(tab._player._toggle_pause)
        # Setting the slider value re-triggers valueChanged -> _on_volume,
        # which updates both the backend and the persisted config.
        s.set_volume.connect(tab._player.vol.setValue)
        s.add_subs.connect(self._do_add_subs)
        # Queued delivery to the GUI thread — safe to snapshot player state.
        tab._player.sig_state.connect(self._on_player_state)
        tab._player.sig_position.connect(self._on_player_position)

    # -- called from agent worker threads (queued onto the GUI thread) ------
    def play_item(self, item: Any) -> None:
        self._signals.play_item.emit(item)

    def play_url(self, url: str, title: str = "") -> None:
        self._signals.play_url.emit(url, title or "")

    def play_file(self, path: str) -> None:
        self._signals.play_file.emit(path)

    def stop(self) -> None:
        self._signals.stop.emit()

    def pause(self) -> None:
        self._signals.pause.emit()

    def set_volume(self, level: int) -> None:
        self._signals.set_volume.emit(max(0, min(100, int(level))))

    def add_subtitle_file(self, path: str) -> None:
        """Load a downloaded subtitle file into the player (GUI thread)."""
        self._signals.add_subs.emit(path)

    def status(self) -> Dict[str, Any]:
        """Now-playing snapshot — callable from any thread."""
        with self._state_lock:
            out = dict(self._state)
        cfg = self._tab._config.iptv
        out["volume"] = cfg.volume
        out["muted"] = cfg.muted
        return out

    # -- GUI thread slots ----------------------------------------------------
    def _do_play_item(self, item: Any) -> None:
        self._tab._play_item(item)
        self._snapshot_item()
        self._focus_tab()

    def _do_play_url(self, url: str, title: str) -> None:
        self._tab.play_web_stream(url, title)
        self._snapshot_item()
        self._focus_tab()

    def _do_play_file(self, path: str) -> None:
        self._tab.play_file(path)
        self._snapshot_item()
        self._focus_tab()

    def _focus_tab(self) -> None:
        """Bring the Play tab forward so the user sees what was started."""
        tabs = getattr(self._tab.window(), "main_tabs", None)
        if tabs is not None:
            tabs.setCurrentWidget(self._tab)

    def _do_add_subs(self, path: str) -> None:
        backend = self._tab._player._backend
        if backend is not None and os.path.isfile(path):
            backend.add_subtitle_file(path)

    def _snapshot_item(self) -> None:
        item = self._tab._player._current_item
        with self._state_lock:
            self._state["title"] = getattr(item, "name", "") if item else ""
            self._state["url"] = getattr(item, "url", "") if item else ""

    def _on_player_state(self, state: str) -> None:
        with self._state_lock:
            self._state["state"] = state
        self._snapshot_item()

    def _on_player_position(self, pos: float, dur: float) -> None:
        with self._state_lock:
            self._state["position"] = pos
            self._state["duration"] = dur
