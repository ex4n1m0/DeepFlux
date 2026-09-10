"""4×4 multiview grid for the Play tab.

Sixteen tiles, each playing either a local file or (at most ONE tile at a
time) an IPTV stream. Deliberately minimal playback plumbing — the full
PlayerWidget control set (tracks, subtitles, recording, retry watchdog)
stays on the main single player; a tile is just surface + backend + audio.

Design constraints (do not "fix" these):
- Tiles always use the in-process MpvBackend path with SVP disabled — the
  out-of-process SVP backend's fixed ``mpvpipe`` IPC name can host only one
  instance, and grid tiles want no post-processing anyway.
- No post-processing: smooth video/interpolation are forced off and the
  per-tile cache is tiny so sixteen instances don't each hold a 150 MiB
  demuxer cache.
- All tiles start muted; audio is opt-in per tile.
- IPTV providers cap concurrent connections (one extra player already
  triggers WSAECONNRESET resets), so at most ONE tile may carry a network
  stream. The other 15 are local files only.
- Each tile's surface widget is created up-front and NEVER reparented —
  reparenting recreates the native winId the backend embeds into and kills
  it (same rule as the MilkDrop view in PlayerWidget).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMenu, QMessageBox, QPushButton, QSizePolicy,
    QSlider, QVBoxLayout, QWidget,
)

from config import DeeptorrentConfig
from iptv.local_folder import VIDEO_EXTS
from iptv.models import Channel, SECTION_LIVE, SECTION_MOVIES
from iptv.player import create_backend

logger = logging.getLogger(__name__)

# Per-tile playback tuning: keep sixteen concurrent decodes light.
TILE_CACHE_SECS = 2
TILE_CACHE_BYTES = 8 * 1024 * 1024

# Grid teardown waits at most this long for all sixteen mpv cores to
# terminate (in parallel) before the app exits anyway — a stuck core must
# never hold window close hostage.
DESTROY_DEADLINE_SECS = 4.0

# Shared file-picker filter (single pick + bulk fill).
MEDIA_FILTER = (
    "Media Files (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v "
    "*.mpg *.mpeg *.ts *.m2ts *.vob *.3gp *.ogv *.mp3 *.flac *.m4a "
    "*.aac *.ogg *.opus *.wav);;All Files (*)"
)


def build_playback_headers(item: Any, source: Any = None) -> Dict[str, str]:
    """Playback HTTP headers for an item (source UA/Referer → #EXTVLCOPT →
    ``extra["headers"]``).

    Shared by PlayerWidget._playback_headers and the multiview tiles so the
    precedence rules live in one place."""
    headers: Dict[str, str] = {}
    if source is not None:
        if source.user_agent:
            headers["User-Agent"] = source.user_agent
        if source.referer:
            headers["Referer"] = source.referer
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


class GridTile(QWidget):
    """One grid square: a stable video surface plus a control strip.

    The strip lives BELOW the surface (a layout sibling, not an overlay) —
    the embedded native mpv window swallows mouse events and wins z-order,
    so floating controls over the video would be unreachable."""

    sig_promote = Signal(object)  # this tile — grid routes its channel out
    # Backend state changes (fires on the mpv event thread; Qt queues the
    # delivery onto the GUI thread). Used by folder mode to detect EOF.
    sig_state = Signal(str)

    def __init__(self, config: DeeptorrentConfig,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._backend: Any = None
        self._channel: Optional[Channel] = None
        self._is_stream = False
        self._muted = True
        self._request_stream_cb: Any = None  # set by MultiViewGrid
        self._pick_file_cb: Any = None
        self._pick_files_cb: Any = None
        self._pick_folder_cb: Any = None
        self._stopping = False  # user-initiated stop — don't emit its event
        self._paused = False    # tile play/pause state (button + rotation)
        self._full_name = "—"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.surface = QFrame()
        self.surface.setStyleSheet("background-color: #000000;")
        self.surface.setMinimumHeight(60)
        layout.addWidget(self.surface, 1)

        self._empty_lbl = QLabel("＋")
        self._empty_lbl.setAlignment(Qt.AlignCenter)
        self._empty_lbl.setStyleSheet(
            "color: #3a465a; font-size: 42px; background-color: #0b0e14;")
        self._empty_lbl.setParent(self.surface)

        strip = QWidget()
        strip.setStyleSheet("background-color: rgba(10,10,15,0.9);")
        row = QHBoxLayout(strip)
        row.setContentsMargins(4, 1, 4, 1)
        row.setSpacing(4)
        self.name_lbl = QLabel("—")
        self.name_lbl.setStyleSheet("color: #ffffff; font-size: 17px;")
        # A QLabel's minimumSizeHint is its full text width, so a long scene
        # filename here would inflate the tile's (and thus the whole 4-column
        # grid's) minimum size — the grid blew up past the viewport the first
        # time real files were loaded. Ignored lets the label shrink freely;
        # the text is elided on resize instead of forcing the layout wide.
        self.name_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.name_lbl.setMinimumWidth(0)
        row.addWidget(self.name_lbl, 1)
        self.mute_btn = QPushButton("🔇")
        self.mute_btn.setFixedWidth(26)
        self.mute_btn.setToolTip("Unmute/mute this tile")
        self.mute_btn.clicked.connect(self._toggle_mute)
        row.addWidget(self.mute_btn)
        self.play_btn = QPushButton("⏸")
        self.play_btn.setFixedWidth(26)
        self.play_btn.setToolTip("Play/pause this tile")
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self._toggle_pause)
        row.addWidget(self.play_btn)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setMaximum(100)
        self.vol.setValue(100)
        self.vol.setMaximumWidth(70)
        self.vol.setToolTip("Tile volume")
        self.vol.valueChanged.connect(self._on_volume)
        row.addWidget(self.vol)
        self.promote_btn = QPushButton("⛶")
        self.promote_btn.setFixedWidth(26)
        self.promote_btn.setToolTip("Move to the main player (full controls)")
        self.promote_btn.clicked.connect(lambda: self.sig_promote.emit(self))
        row.addWidget(self.promote_btn)
        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedWidth(26)
        self.close_btn.setToolTip("Stop and clear this tile")
        self.close_btn.clicked.connect(self.clear)
        row.addWidget(self.close_btn)
        layout.addWidget(strip)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda _pos: self._show_menu())
        # Backend "paused"/"playing" events (queued onto the GUI thread) keep
        # the play/pause button honest — including mpv's own keep-open pause.
        self.sig_state.connect(self._on_own_state)

    # -- geometry ------------------------------------------------------------
    def _set_tile_name(self, text: str) -> None:
        self._full_name = text or "—"
        self._elide_name()

    def _elide_name(self) -> None:
        fm = self.name_lbl.fontMetrics()
        self.name_lbl.setText(fm.elidedText(
            self._full_name, Qt.ElideMiddle, max(10, self.name_lbl.width())))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._channel is None:
            self._empty_lbl.setGeometry(0, 0, self.surface.width(),
                                        self.surface.height())
        self._elide_name()

    # -- assignment ----------------------------------------------------------
    @property
    def channel(self) -> Optional[Channel]:
        return self._channel

    @property
    def is_stream(self) -> bool:
        return self._is_stream and self._channel is not None

    @property
    def backend(self) -> Any:
        return self._backend

    def _ensure_backend(self) -> Any:
        if self._backend is None:
            # svp=False always: the SVP backend's fixed IPC pipe name hosts
            # one instance only, and grid tiles want no post-processing.
            backend = create_backend(self.surface, preferred="mpv", svp=False)
            if backend is None:
                return None
            backend.set_hwdec(self._config.iptv.hwdec)
            backend.set_smooth_video(False)
            backend.set_interpolation(False)
            backend.set_cache(TILE_CACHE_SECS, TILE_CACHE_BYTES)
            backend.set_mute(True)
            backend.set_volume(self.vol.value())
            backend.on_state = self._emit_state
            self._backend = backend
        return self._backend

    def _emit_state(self, state: str) -> None:
        # MpvBackend.stop() reports "stopped" inline (same thread), while an
        # EOF arrives asynchronously from the mpv event thread — the flag
        # distinguishes the user-initiated stop (suppress) from a natural end.
        if self._stopping:
            return
        self.sig_state.emit(state)

    def assign(self, channel: Channel, is_stream: bool,
               headers: Optional[Dict[str, str]] = None) -> bool:
        """Load an item into this tile. Returns False when no backend."""
        backend = self._ensure_backend()
        if backend is None:
            return False
        self.clear(clear_backend=False)
        self._channel = channel
        self._is_stream = bool(is_stream)
        self._muted = True
        self.mute_btn.setText("🔇")
        self._set_tile_name(channel.name or channel.url)
        self._empty_lbl.hide()
        self.play_btn.setEnabled(True)
        backend.set_mute(True)
        backend.play(channel.url, headers=headers or {})
        # mpv's pause property SURVIVES loadfile: a tile that just ended
        # under keep-open sits paused, so without this the folder rotation
        # (and any re-assign) would load the next video paused.
        backend.resume()
        self._paused = False
        self._update_play_btn()
        return True

    def clear(self, clear_backend: bool = True) -> None:
        """Stop playback and reset the tile to empty."""
        self._channel = None
        self._is_stream = False
        if self._backend is not None:
            self._stopping = True
            try:
                self._backend.stop()
            except Exception:
                logger.debug("tile stop failed", exc_info=True)
            finally:
                self._stopping = False
        if clear_backend and self._backend is not None:
            try:
                self._backend.destroy()
            except Exception:
                logger.debug("tile backend destroy failed", exc_info=True)
            self._backend = None
        self._paused = False
        self._update_play_btn()
        self.play_btn.setEnabled(False)
        self._set_tile_name("—")
        self._empty_lbl.show()

    def shutdown(self) -> None:
        self.clear(clear_backend=True)

    # -- transport ------------------------------------------------------------
    def _toggle_pause(self) -> None:
        if self._backend is None or self._channel is None:
            return
        self._paused = not self._paused
        self._update_play_btn()
        if self._paused:
            self._backend.pause()
        else:
            self._backend.resume()

    def _update_play_btn(self) -> None:
        self.play_btn.setText("▶" if self._paused else "⏸")

    def _on_own_state(self, state: str) -> None:
        """Track the backend's own pause transitions (keep-open EOF pause,
        user toggle) so the button never lies."""
        if state == "paused":
            self._paused = True
        elif state in ("playing", "buffering"):
            self._paused = False
        self._update_play_btn()

    # -- audio ---------------------------------------------------------------
    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self.mute_btn.setText("🔊" if not self._muted else "🔇")
        if self._backend is not None:
            self._backend.set_mute(self._muted)

    def _on_volume(self, value: int) -> None:
        if self._backend is not None:
            self._backend.set_volume(value)

    # -- interactions --------------------------------------------------------
    def _show_menu(self) -> None:
        # Assign actions live on the grid — it owns the single-stream rule.
        menu = QMenu(self)
        if self._channel is None:
            act_file = menu.addAction("Assign local file…")
            act_file.triggered.connect(
                lambda: self._pick_file_cb and self._pick_file_cb(self))
            act_bulk = menu.addAction("Fill empty tiles with files…")
            act_bulk.triggered.connect(
                lambda: self._pick_files_cb and self._pick_files_cb())
            act_folder = menu.addAction("Play folder (auto-rotate)…")
            act_folder.triggered.connect(
                lambda: self._pick_folder_cb and self._pick_folder_cb())
            act_stream = menu.addAction("Assign IPTV channel…")
            act_stream.triggered.connect(
                lambda: self._request_stream_cb and self._request_stream_cb(self))
        else:
            menu.addAction("Clear tile").triggered.connect(self.clear)
            menu.addSeparator()
            menu.addAction("Play folder (auto-rotate)…").triggered.connect(
                lambda: self._pick_folder_cb and self._pick_folder_cb())
        menu.exec(self.mapToGlobal(self.rect().center()))


class ChannelPickDialog(QDialog):
    """Search the playlist and pick one channel (live or VOD, not series)."""

    def __init__(self, manager: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pick a channel for the tile")
        self.resize(480, 420)
        self._manager = manager
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search channels, movies…")
        self.search.textChanged.connect(self._run)
        layout.addWidget(self.search)
        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(lambda _i: self.accept())
        layout.addWidget(self.results)
        btns = QHBoxLayout()
        ok = QPushButton("Assign")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        layout.addLayout(btns)
        self._items: List[Any] = []
        self._run("")

    def _run(self, text: str) -> None:
        text = (text or "").strip()
        self.results.clear()
        self._items = []
        if not text:
            self.results.addItem("Type to search…")
            return
        found = self._manager.search(text)
        for section in (SECTION_LIVE, SECTION_MOVIES):
            for item in found.get(section, [])[:60]:
                self._items.append(item)
                self.results.addItem(
                    getattr(item, "display_name", None) or item.name)

    def selected(self) -> Optional[Any]:
        idx = self.results.currentRow()
        if 0 <= idx < len(self._items):
            return self._items[idx]
        return None


class MultiViewGrid(QWidget):
    """The 4×4 tile grid — one optional IPTV stream, the rest local files."""

    GRID = 4
    sig_promote_requested = Signal(object)  # Channel → main player

    def __init__(self, manager: Any, config: DeeptorrentConfig,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._config = config
        # Folder auto-rotate mode: files waiting for a freed tile, in order.
        self._folder_pending: List[str] = []
        self._folder_mode = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        grid_host = QWidget()
        layout = QGridLayout(grid_host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.tiles: List[GridTile] = []
        for r in range(self.GRID):
            for c in range(self.GRID):
                tile = GridTile(config)
                tile._request_stream_cb = self._pick_stream_for
                tile._pick_file_cb = self.pick_file_for
                tile._pick_files_cb = self.pick_files_bulk
                tile._pick_folder_cb = self.pick_folder
                tile.sig_promote.connect(self._promote)
                tile.sig_state.connect(
                    lambda s, t=tile: self._on_tile_state(t, s))
                layout.addWidget(tile, r, c)
                self.tiles.append(tile)
        for i in range(self.GRID):
            layout.setRowStretch(i, 1)
            layout.setColumnStretch(i, 1)
        outer.addWidget(grid_host, 1)

        # Slim control bar under the tiles (grid-wide actions).
        bar = QWidget()
        bar.setStyleSheet("background-color: rgba(10,10,15,0.9);")
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 2, 6, 2)
        self.skip_buttons: List[QPushButton] = []
        for delta, label in ((-60, "⏪ −60s"), (-10, "⏪ −10s"),
                             (10, "⏩ +10s"), (60, "⏩ +60s")):
            btn = QPushButton(f"{label} (all tiles)")
            btn.setToolTip(
                f"Jump every loaded clip "
                f"{'back' if delta < 0 else 'forward'} {abs(delta)} seconds "
                f"(repeat presses keep going)")
            btn.clicked.connect(lambda _c=False, d=delta: self.skip_all(d))
            row.addWidget(btn)
            self.skip_buttons.append(btn)
            if delta == -10:
                self.rew_btn = btn
            elif delta == 10:
                self.skip_btn = btn
        row.addStretch(1)
        outer.addWidget(bar)

    # -- grid-wide transport ----------------------------------------------------
    def skip_all(self, seconds: float) -> None:
        """Seek every LOADED tile forward by ``seconds``; empty tiles are
        untouched and each backend clamps to its own duration (live-stream
        tiles simply ignore relative seeks)."""
        for t in self.tiles:
            if t._backend is None or t.channel is None:
                continue
            try:
                t._backend.seek_by(seconds)
            except Exception:
                logger.debug("tile seek failed", exc_info=True)

    # -- single-stream rule --------------------------------------------------
    @property
    def stream_tile(self) -> Optional[GridTile]:
        for t in self.tiles:
            if t.is_stream:
                return t
        return None

    def assign_stream(self, tile: GridTile, channel: Channel) -> bool:
        """Assign an IPTV stream tile. Refuses while another tile streams —
        providers count concurrent connections and reset them."""
        current = self.stream_tile
        if current is not None and current is not tile:
            return False
        headers = build_playback_headers(channel,
                                         self._manager.active_source())
        if not tile.assign(channel, is_stream=True, headers=headers):
            return False
        return True

    def assign_file(self, tile: GridTile, path: str) -> bool:
        if not path or not os.path.isfile(path):
            return False
        ch = Channel(id=path, name=os.path.basename(path), url=path,
                     section=SECTION_MOVIES)
        return tile.assign(ch, is_stream=False)

    # -- interactions --------------------------------------------------------
    def _pick_stream_for(self, tile: GridTile) -> None:
        if self.stream_tile not in (None, tile):
            QMessageBox.information(
                self, "One stream per grid",
                "Another tile is already playing an IPTV stream.\n"
                "Providers reject multiple simultaneous connections — "
                "clear that tile first.")
            return
        dlg = ChannelPickDialog(self._manager, self)
        if dlg.exec() == QDialog.Accepted:
            item = dlg.selected()
            if item is not None and getattr(item, "url", ""):
                self.assign_stream(tile, item)

    def pick_file_for(self, tile: GridTile) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Media File", "", MEDIA_FILTER)
        if path:
            self.assign_file(tile, path)

    # -- bulk fill -------------------------------------------------------------
    def pick_files_bulk(self) -> None:
        """Multi-select files, then deal them out to the empty tiles."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open Media Files (fill empty tiles)", "", MEDIA_FILTER)
        if paths:
            self.fill_from_files(paths)

    def fill_from_files(self, paths: List[str]) -> int:
        """Assign the given files to EMPTY tiles in reading order.

        Sorted alphabetically so the fill is predictable (episodes numbered
        S01E01… land in order); already-filled tiles — including the single
        IPTV stream slot — are skipped, and extra files beyond the last
        empty tile are ignored. Returns how many tiles were filled."""
        valid = sorted(p for p in paths if os.path.isfile(p))
        empty = [t for t in self.tiles if t.channel is None]
        added = 0
        for tile, path in zip(empty, valid):
            if self.assign_file(tile, path):
                added += 1
        return added

    # -- folder auto-rotate ------------------------------------------------------
    @staticmethod
    def scan_folder_videos(folder: str) -> List[str]:
        """Every playable video under ``folder`` (recursive), sorted by
        relative path — the same extension set the local-media scanner uses,
        with sample/preview junk skipped."""
        found: List[str] = []
        for root, _dirs, names in os.walk(folder):
            for name in names:
                ext = os.path.splitext(name)[1].lower()
                if ext not in VIDEO_EXTS:
                    continue
                if "sample" in name.lower():
                    continue
                found.append(os.path.join(root, name))
        found.sort()
        return found

    def pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder of videos")
        if folder:
            self.start_folder(folder)

    def start_folder(self, folder: str) -> int:
        """Fill the grid with the folder's first 16 videos and auto-rotate:
        whenever a tile's video ends naturally, that tile plays the next
        unplayed file from the folder, until every video has played once.

        Folder mode replaces the grid's content (the stream tile included).
        Returns how many tiles were filled; 0 when the folder has no videos
        (caller may want to tell the user)."""
        files = self.scan_folder_videos(folder)
        if not files:
            return 0
        self._folder_mode = False  # inert while we clear (stop events suppressed anyway)
        for t in self.tiles:
            t.clear()
        self._folder_pending = files[self.GRID * self.GRID:]
        self._folder_mode = True
        filled = 0
        for tile, path in zip(self.tiles, files[:self.GRID * self.GRID]):
            if self.assign_file(tile, path):
                filled += 1
        return filled

    def _on_tile_state(self, tile: GridTile, state: str) -> None:
        """Auto-rotate: a tile's file ended -> hand it the next unplayed one.
        User-cleared tiles arrive here with no channel and are left empty."""
        if not self._folder_mode or state != "stopped":
            return
        if tile.is_stream or tile.channel is None:
            return
        while self._folder_pending:
            nxt = self._folder_pending.pop(0)
            if self.assign_file(tile, nxt):
                return  # file vanished since the scan -> try the next one
        # Every video in the folder has now played once — free the tile.
        tile.clear()

    def _promote(self, tile: GridTile) -> None:
        ch = tile.channel
        if ch is None:
            return
        # Stop the tile first (frees the provider connection / decoder) and
        # hand the item to the main player, which owns the full control set.
        tile.clear()
        self.sig_promote_requested.emit(ch)

    def shutdown(self) -> None:
        """Stop every tile, then destroy the backends IN PARALLEL on daemon
        threads under a hard deadline.

        Sequential destroy made app close take forever: each mpv terminate()
        blocks until its core tears down the decoder and stream, and sixteen
        of those on the GUI thread compound to many seconds. Daemon threads
        mean even a core that refuses to die cannot block process exit."""
        self._folder_mode = False
        self._folder_pending = []
        backends = [t._backend for t in self.tiles if t._backend is not None]
        for t in self.tiles:
            t.clear(clear_backend=False)
            t._backend = None

        def _destroy(b: Any) -> None:
            try:
                b.destroy()
            except Exception:
                logger.debug("tile backend destroy failed", exc_info=True)

        threads = [threading.Thread(target=_destroy, args=(b,), daemon=True)
                   for b in backends]
        for th in threads:
            th.start()
        deadline = time.monotonic() + DESTROY_DEADLINE_SECS
        for th in threads:
            th.join(max(0.0, deadline - time.monotonic()))
