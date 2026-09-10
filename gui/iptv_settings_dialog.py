"""IPTV Settings — split into small, focused pages (the old single dialog
overflowed small windows):

- :class:`IPTVSourcesDialog`   — playlist sources table (add/edit/remove)
- :class:`IPTVMetadataDialog`  — TMDb key, artwork cache, EPG
- :class:`IPTVSubtitlesDialog` — OpenSubtitles account + preferred languages
- :class:`IPTVPlaybackDialog`  — backend, hwdec, buffer, overscan, throttling

Every page lives on a scrollable canvas so nothing clips off-screen on small
windows. Dark cyberpunk theme via ``_SHARED_STYLE``.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Optional
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import DeeptorrentConfig, IPTVSourceConfig
from gui.milkdrop import list_presets, preset_name
from gui.window_sizing import roomy

logger = logging.getLogger(__name__)

# Preferred-language dropdowns: the most common track languages, with a free-
# text editable combo for anything not listed. Stored value is the ISO code.
_PREF_LANGS = [
    ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"),
    ("it", "Italian"), ("pt", "Portuguese"), ("ru", "Russian"), ("ar", "Arabic"),
    ("zh", "Chinese"), ("ja", "Japanese"), ("ko", "Korean"), ("hi", "Hindi"),
]


def _epg_url_error(value: str) -> str:
    """Return a user-facing validation error, or empty for a valid/blank URL."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        # Accessing port also catches malformed values such as host:abc.
        _port = parsed.port
    except ValueError:
        return "EPG URL is malformed."
    if parsed.scheme.lower() not in ("http", "https"):
        return "EPG URL must start with http:// or https://."
    if not parsed.hostname:
        return "EPG URL must include a valid host name."
    if any(ch.isspace() for ch in value):
        return "EPG URL cannot contain spaces."
    return ""


def _parse_lang_text(text: str) -> str:
    """'English (en)' / 'en' / 'eng' → normalized ISO code ('' = default)."""
    text = text.strip()
    if not text or text.startswith("—"):
        return ""
    import re
    m = re.search(r"\(([a-zA-Z]{2,3})\)\s*$", text)
    return (m.group(1) if m else text).lower()


def _lang_combo() -> QComboBox:
    """Editable combo: popular languages in the dropdown, type your own."""
    combo = QComboBox()
    combo.setEditable(True)
    combo.addItem("— Player default —", "")
    for code, name in _PREF_LANGS:
        combo.addItem(f"{name} ({code})", code)
    return combo


def _set_lang_combo(combo: QComboBox, code: str) -> None:
    idx = combo.findData(code)
    if idx >= 0:
        combo.setCurrentIndex(idx)
    else:
        combo.setCurrentText(code)  # custom code typed previously


_SHARED_STYLE = """
    QDialog { background-color: #0a0a0f; color: #ffffff; }
    QLabel { color: #ffffff; }
    QGroupBox { color: #ffffff; border: 3px solid #1a2a4a; border-radius: 6px; margin-top: 12px; padding-top: 12px; }
    QGroupBox::title { color: #2a7abf; subcontrol-origin: margin; left: 10px; padding: 0 5px; font-weight: 600; }
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background-color: #0d1117; color: #ffffff; border: 3px solid #1a2a4a; padding: 2px 8px; border-radius: 3px; }
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border: 3px solid #2a7abf; }
    QCheckBox { color: #ffffff; }
    QCheckBox::indicator { border: 3px solid #1a2a4a; border-radius: 3px; width: 16px; height: 16px; }
    QCheckBox::indicator:checked { background-color: #2a7abf; border-color: #2a7abf; }
    QPushButton { background-color: #111827; color: #ffffff; border: 3px solid #1a2a4a; padding: 2px 10px; border-radius: 3px; }
    QPushButton:hover { background-color: #1a2a4a; border: 3px solid #2a7abf; color: #2a7abf; }
    QTableWidget { background-color: #0d1117; color: #ffffff; gridline-color: #1a2a4a; border: 3px solid #1a2a4a; border-radius: 6px; }
    QScrollArea { background-color: transparent; border: none; }
    QLabel#hint { color: #4a6a8a; font-size: 17px; }
"""


def _make_hint(text: str) -> QLabel:
    """Word-wrapped hint label — long single-line hints used to stretch the
    dialog far wider than the screen-friendly size."""
    hint = QLabel(text)
    hint.setObjectName("hint")
    hint.setWordWrap(True)
    return hint


class _SettingsPage(QDialog):
    """One small settings page on a scrollable canvas — content never clips
    off-screen no matter how small the window gets."""

    def __init__(self, config: DeeptorrentConfig, title: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        roomy(self)
        self.setStyleSheet(_SHARED_STYLE)

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll, 1)
        body = QWidget()
        self.body = QVBoxLayout(body)
        scroll.setWidget(body)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)


class _SourceEditDialog(QDialog):
    """Add/edit a single IPTV source."""

    def __init__(self, source: Optional[IPTVSourceConfig] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit IPTV Source" if source else "Add IPTV Source")
        self.setMinimumWidth(520)
        self.setStyleSheet(_SHARED_STYLE)
        self._build_ui()
        if source:
            self._load(source)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        form = QFormLayout()

        self.name = QLineEdit()
        form.addRow("Display name:", self.name)

        self.kind = QComboBox()
        self.kind.addItems(["M3U URL", "Local M3U file", "Xtream Codes API",
                            "Media folder (local)"])
        self.kind.setItemData(0, "m3u_url")
        self.kind.setItemData(1, "m3u_file")
        self.kind.setItemData(2, "xtream")
        self.kind.setItemData(3, "local_folder")
        self.kind.currentIndexChanged.connect(self._on_kind_changed)
        form.addRow("Type:", self.kind)

        self.url = QLineEdit()
        self.url.setPlaceholderText("http://provider/playlist.m3u8")
        form.addRow("URL / file path:", self.url)

        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._browse_file)
        form.addRow("", self.browse_btn)

        self.user_agent = QLineEdit()
        form.addRow("User-Agent:", self.user_agent)

        self.referer = QLineEdit()
        form.addRow("Referer:", self.referer)

        self.username = QLineEdit()
        form.addRow("Xtream username:", self.username)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        form.addRow("Xtream password:", self.password)

        self.epg_url = QLineEdit()
        self.epg_url.setPlaceholderText("http://provider/epg.xml (optional)")
        form.addRow("EPG URL:", self.epg_url)

        self.auto_refresh = QSpinBox()
        self.auto_refresh.setRange(0, 10080)
        self.auto_refresh.setSuffix(" min")
        form.addRow("Auto-refresh:", self.auto_refresh)

        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(True)
        form.addRow("", self.enabled)

        layout.addLayout(form)

        layout.addWidget(_make_hint("Xtream credentials are stored locally and never logged."))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._on_kind_changed(0)

    def _on_kind_changed(self, _idx: int) -> None:
        kind = self.kind.currentData()
        is_file = kind == "m3u_file"
        is_folder = kind == "local_folder"
        is_xtream = kind == "xtream"
        self.browse_btn.setVisible(is_file or is_folder)
        self.url.setPlaceholderText(
            "Server URL (http://host:port)" if is_xtream
            else ("Path to .m3u file" if is_file
                  else ("Path to media folder (e.g. C:\\Media)" if is_folder
                        else "http://provider/playlist.m3u8"))
        )
        # Xtream-only fields.
        self.username.setVisible(is_xtream)
        self.password.setVisible(is_xtream)
        # Headers most relevant for URL sources; EPG is meaningless for a
        # folder of local files.
        for w in (self.user_agent, self.referer):
            w.setVisible(not is_file and not is_folder)
        self.epg_url.setVisible(not is_folder)

    def _browse_file(self) -> None:
        if self.kind.currentData() == "local_folder":
            path = QFileDialog.getExistingDirectory(self, "Select media folder")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Select M3U file", "", "M3U playlists (*.m3u *.m3u8);;All files (*)")
        if path:
            self.url.setText(path)

    def _load(self, s: IPTVSourceConfig) -> None:
        self.name.setText(s.name)
        kind_map = {"m3u_url": 0, "m3u_file": 1, "xtream": 2, "local_folder": 3}
        self.kind.setCurrentIndex(kind_map.get(s.kind, 0))
        self.url.setText(s.url)
        self.user_agent.setText(s.user_agent)
        self.referer.setText(s.referer)
        self.username.setText(s.username)
        self.password.setText(s.password)
        self.auto_refresh.setValue(s.auto_refresh_minutes)
        self.epg_url.setText(getattr(s, "epg_url", ""))
        self.enabled.setChecked(s.enabled)

    def accept(self) -> None:
        if self.kind.currentData() != "local_folder":
            error = _epg_url_error(self.epg_url.text())
            if error:
                QMessageBox.warning(self, "Invalid EPG URL", error)
                self.epg_url.setFocus()
                return
        super().accept()

    def to_source(self, existing: Optional[IPTVSourceConfig] = None) -> IPTVSourceConfig:
        sid = existing.id if existing else str(uuid.uuid4())
        return IPTVSourceConfig(
            id=sid,
            name=self.name.text().strip() or "Untitled",
            kind=self.kind.currentData(),
            url=self.url.text().strip(),
            user_agent=self.user_agent.text().strip(),
            referer=self.referer.text().strip(),
            username=self.username.text().strip(),
            password=self.password.text(),
            auto_refresh_minutes=self.auto_refresh.value(),
            epg_url=self.epg_url.text().strip(),
            enabled=self.enabled.isChecked(),
        )


class IPTVSourcesDialog(_SettingsPage):
    """Playlist sources table (add/edit/remove). Mutations apply to
    config.iptv.sources immediately; OK/Cancel only decides whether the
    caller persists to disk."""

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(config, "IPTV — Playlist Sources", parent)

        src_group = QGroupBox("Playlist Sources")
        sl = QVBoxLayout(src_group)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "URL / Server", "Auto-refresh", "Enabled"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        sl.addWidget(self.table)

        btns = QHBoxLayout()
        add_btn = QPushButton("+ Add")
        add_btn.setObjectName("btn_accent")
        add_btn.clicked.connect(self._add_source)
        btns.addWidget(add_btn)
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._edit_source)
        btns.addWidget(edit_btn)
        del_btn = QPushButton("Remove")
        del_btn.clicked.connect(self._remove_source)
        btns.addWidget(del_btn)
        btns.addStretch()
        sl.addLayout(btns)
        self.body.addWidget(src_group)
        self._load_sources()
        # The base page sized itself before this table existed — refit now.
        roomy(self)

    def _load_sources(self) -> None:
        self.table.setRowCount(len(self.config.iptv.sources))
        for r, s in enumerate(self.config.iptv.sources):
            self.table.setItem(r, 0, QTableWidgetItem(s.name))
            self.table.setItem(r, 1, QTableWidgetItem(s.kind))
            self.table.setItem(r, 2, QTableWidgetItem(s.url))
            self.table.setItem(r, 3, QTableWidgetItem(f"{s.auto_refresh_minutes} min"))
            self.table.setItem(r, 4, QTableWidgetItem("Yes" if s.enabled else "No"))

    def _selected_source(self) -> Optional[IPTVSourceConfig]:
        r = self.table.currentRow()
        if r < 0 or r >= len(self.config.iptv.sources):
            return None
        return self.config.iptv.sources[r]

    def _add_source(self) -> None:
        dlg = _SourceEditDialog(parent=self)
        if dlg.exec() == QDialog.Accepted:
            self.config.iptv.sources.append(dlg.to_source())
            self._load_sources()

    def _edit_source(self) -> None:
        s = self._selected_source()
        if s is None:
            return
        dlg = _SourceEditDialog(source=s, parent=self)
        if dlg.exec() == QDialog.Accepted:
            idx = self.table.currentRow()
            self.config.iptv.sources[idx] = dlg.to_source(existing=s)
            self._load_sources()

    def _remove_source(self) -> None:
        s = self._selected_source()
        if s is None:
            return
        if QMessageBox.question(self, "Remove source", f"Remove '{s.name}'?") == QMessageBox.Yes:
            del self.config.iptv.sources[self.table.currentRow()]
            self._load_sources()


class IPTVMetadataDialog(_SettingsPage):
    """Disk cache, EPG toggle. TMDb key lives in File → API Keys.

    When the running IPTVManager is passed in, the page also shows the live
    disk usage and offers a "delete all" for the derived caches (artwork
    files + metadata lookups). Playlists, EPG, favorites and watch history
    are NOT touched — only data that re-downloads on demand."""

    cache_cleared = Signal()        # a clear finished — grids must reset tiles
    _stats_ready = Signal(dict)     # worker thread -> GUI
    _clear_done = Signal(dict)      # worker thread -> GUI

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None,
                 manager=None) -> None:
        super().__init__(config, "IPTV — Metadata & Cache", parent)
        self._manager = manager

        cache_group = QGroupBox("Cache")
        cl = QFormLayout(cache_group)
        self.cache_dir = QLineEdit()
        self.cache_dir.setPlaceholderText("Default: ~/.deeptorrent/iptv")
        cl.addRow("Cache location:", self.cache_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_cache_dir)
        cl.addRow("", browse)
        self.cache_limit = QSpinBox()
        self.cache_limit.setRange(50, 51200)
        self.cache_limit.setSuffix(" MB")
        self.cache_limit.setToolTip(
            "Disk cap for cached covers/artwork. When the cache grows past\n"
            "this, the least-recently-viewed images are evicted first.")
        cl.addRow("Cache size limit:", self.cache_limit)
        self.framegrab = QCheckBox("Frame-grab poster fallback")
        self.framegrab.setToolTip(
            "When no metadata provider finds a poster for a movie/series,\n"
            "grab a frame from the stream itself with FFmpeg (100% coverage).\n"
            "Only ever runs for tiles currently on screen — each grab opens\n"
            "a video connection to your provider, so off-screen entries are\n"
            "never grabbed.")
        cl.addRow("", self.framegrab)
        self.enable_epg = QCheckBox("Enable EPG (XMLTV) when available")
        cl.addRow("", self.enable_epg)
        self.epg_url = QLineEdit()
        self.epg_url.setPlaceholderText("https://provider/epg.xml")
        self.epg_url.setToolTip(
            "XMLTV guide URL applied to every source that doesn't have its\n"
            "own per-source EPG URL. Precedence: per-source > this > the\n"
            "playlist's own url-tvg. Programme times are stored with their\n"
            "UTC offsets, so the guide always shows correctly in your\n"
            "system's timezone.")
        cl.addRow("EPG URL (all sources):", self.epg_url)
        self.xtream_series_concurrency = QSpinBox()
        self.xtream_series_concurrency.setRange(1, 6)
        self.xtream_series_concurrency.setToolTip(
            "Maximum simultaneous Xtream get_series_info requests. A low value\n"
            "reduces provider rate limiting; changes apply on the next refresh.")
        cl.addRow("Xtream series requests:", self.xtream_series_concurrency)
        self.body.addWidget(cache_group)

        if manager is not None:
            usage_group = QGroupBox("Disk Usage")
            ul = QVBoxLayout(usage_group)
            self.usage_label = _make_hint("Calculating…")
            ul.addWidget(self.usage_label)
            self.clear_btn = QPushButton("Delete All Cached Artwork && Metadata…")
            self.clear_btn.setToolTip(
                "Deletes every cached cover, logo and metadata lookup.\n"
                "Playlists, favorites and watch history are kept; artwork\n"
                "and metadata simply re-download as you browse.")
            self.clear_btn.clicked.connect(self._clear_caches)
            ul.addWidget(self.clear_btn)
            self.body.addWidget(usage_group)
            self._stats_ready.connect(self._show_stats)
            self._clear_done.connect(self._on_clear_done)
            self._refresh_stats()

        self.cache_dir.setText(self.config.iptv.cache_dir)
        self.cache_limit.setValue(self.config.iptv.cache_limit_mb)
        self.framegrab.setChecked(self.config.iptv.framegrab_posters)
        self.enable_epg.setChecked(self.config.iptv.enable_epg)
        self.epg_url.setText(self.config.iptv.epg_url)
        self.xtream_series_concurrency.setValue(
            self.config.iptv.xtream_series_concurrency)

    @staticmethod
    def _fmt_mb(n: float) -> str:
        return f"{n / (1024 * 1024):,.1f} MB" if n < 1024 ** 3 else f"{n / 1024 ** 3:,.2f} GB"

    def _refresh_stats(self) -> None:
        def _work() -> None:
            try:
                self._stats_ready.emit(self._manager.cache_stats())
            except Exception:
                logger.exception("cache stats failed")
        threading.Thread(target=_work, daemon=True).start()

    def _show_stats(self, s: dict) -> None:
        self._last_stats = s
        self.usage_label.setText(
            f"Artwork: {self._fmt_mb(s.get('full_bytes', 0) + s.get('thumb_bytes', 0))} "
            f"({s.get('full_files', 0):,} images)\n"
            f"Metadata: {s.get('metadata_rows', 0):,} lookups "
            f"(database {self._fmt_mb(s.get('db_bytes', 0))} incl. playlists/EPG)\n"
            f"Total: {self._fmt_mb(s.get('total_bytes', 0))} "
            f"(limit {self._fmt_mb(self.config.iptv.cache_limit_mb * 1024 * 1024)})")

    def _clear_caches(self) -> None:
        s = getattr(self, "_last_stats", None) or {}
        total = self._fmt_mb(s.get("total_bytes", 0)) if s else "the cached data"
        if QMessageBox.question(
                self, "Delete caches",
                f"Delete all cached artwork and metadata ({total})?\n\n"
                "Covers, logos and metadata re-download as you browse.\n"
                "Playlists, EPG, favorites and watch history are kept.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.clear_btn.setEnabled(False)
        self.clear_btn.setText("Deleting…")
        self._manager.clear_caches_async(lambda summary: self._clear_done.emit(summary))

    def _on_clear_done(self, summary: dict) -> None:
        self.clear_btn.setEnabled(True)
        self.clear_btn.setText("Delete All Cached Artwork && Metadata…")
        freed = summary.get("artwork_bytes", 0)
        QMessageBox.information(
            self, "Caches deleted",
            f"Freed {self._fmt_mb(freed)} of artwork "
            f"({summary.get('artwork_files', 0):,} files) and "
            f"{summary.get('metadata_rows', 0):,} metadata lookups.")
        self.cache_cleared.emit()
        self._refresh_stats()

    def _browse_cache_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose cache directory")
        if d:
            self.cache_dir.setText(d)

    def accept(self) -> None:
        epg_error = _epg_url_error(self.epg_url.text())
        if epg_error:
            QMessageBox.warning(self, "Invalid EPG URL", epg_error)
            self.epg_url.setFocus()
            return
        # Validate the cache directory before saving: it must be creatable
        # and writable, or the IPTV cache will fail at runtime.
        cache_dir = self.cache_dir.text().strip()
        if cache_dir:
            try:
                os.makedirs(cache_dir, exist_ok=True)
                probe = os.path.join(cache_dir, ".deeptorrent_write_test")
                with open(probe, "w") as f:
                    f.write("ok")
                os.unlink(probe)
            except OSError as exc:
                QMessageBox.warning(self, "Invalid cache location",
                                    f"Cannot use this cache directory:\n{exc}")
                return
        self.config.iptv.cache_dir = cache_dir
        self.config.iptv.cache_limit_mb = self.cache_limit.value()
        self.config.iptv.framegrab_posters = self.framegrab.isChecked()
        self.config.iptv.enable_epg = self.enable_epg.isChecked()
        self.config.iptv.epg_url = self.epg_url.text().strip()
        self.config.iptv.xtream_series_concurrency = \
            self.xtream_series_concurrency.value()
        if self._manager is not None:
            # Apply the (possibly lowered) cap to the running app right away.
            self._manager.cache_limit_mb = self.cache_limit.value()
            self._manager.enforce_cache_limit_async()
            self._manager.set_framegrab_enabled(self.framegrab.isChecked())
            self._manager.xtream_series_concurrency = \
                self.xtream_series_concurrency.value()
            # Fetches the guide immediately when the URL changed.
            self._manager.set_epg(self.config.iptv.epg_url, self.enable_epg.isChecked())
        super().accept()


class IPTVSubtitlesDialog(_SettingsPage):
    """Preferred audio/subtitle languages. OpenSubtitles keys live in File → API Keys."""

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(config, "IPTV — Subtitles & Languages", parent)

        lang_group = QGroupBox("Preferred Languages")
        ll = QFormLayout(lang_group)
        self.pref_audio_lang = _lang_combo()
        ll.addRow("Audio language:", self.pref_audio_lang)
        self.pref_sub_lang = _lang_combo()
        ll.addRow("Subtitle language:", self.pref_sub_lang)
        ll.addRow("", _make_hint(
            "When a file has multiple tracks, the one in this language is selected "
            "automatically on playback. Also the default language for subtitle searches "
            "(CC menu and the agent). Pick from the list or type any ISO code (e.g. uk, vie)."))
        self.body.addWidget(lang_group)

        _set_lang_combo(self.pref_audio_lang, self.config.iptv.preferred_audio_lang)
        _set_lang_combo(self.pref_sub_lang, self.config.iptv.preferred_sub_lang)

    def accept(self) -> None:
        self.config.iptv.preferred_audio_lang = _parse_lang_text(self.pref_audio_lang.currentText())
        self.config.iptv.preferred_sub_lang = _parse_lang_text(self.pref_sub_lang.currentText())
        super().accept()


class IPTVPlaybackDialog(_SettingsPage):
    """Player backend, decoding, buffering, torrent throttling."""

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(config, "IPTV — Playback", parent)

        play_group = QGroupBox("Playback")
        pl = QFormLayout(play_group)
        self.player = QComboBox()
        self.player.addItems(["mpv (libmpv)", "libVLC"])
        self.player.setItemData(0, "mpv")
        self.player.setItemData(1, "vlc")
        pl.addRow("Preferred player:", self.player)

        self.hwdec = QComboBox()
        self.hwdec.addItems(["auto-safe (recommended)", "auto", "no (software)"])
        self.hwdec.setItemData(0, "auto-safe")
        self.hwdec.setItemData(1, "auto")
        self.hwdec.setItemData(2, "no")
        pl.addRow("Hardware decoding:", self.hwdec)

        self.cache = QSpinBox()
        self.cache.setRange(1, 120)
        self.cache.setSuffix(" s")
        pl.addRow("Startup buffer:", self.cache)

        self.live_pause_buffer = QSpinBox()
        self.live_pause_buffer.setRange(0, 3600)
        self.live_pause_buffer.setSingleStep(30)
        self.live_pause_buffer.setSuffix(" s")
        self.live_pause_buffer.setSpecialValueText("Disabled")
        self.live_pause_buffer.setToolTip(
            "mpv only: keeps a bounded volatile cache so a live channel can be\n"
            "paused and briefly rewound. This is not durable timeshift; the\n"
            "buffer is lost on stop and high-bitrate channels retain less time."
        )
        pl.addRow("Live bounded pause buffer:", self.live_pause_buffer)

        self.recording_dir = QLineEdit()
        self.recording_dir.setPlaceholderText("Default: ~/Videos/DeepFlux Recordings")
        self.recording_dir.setToolTip(
            "Folder for explicit live/network stream recordings. Recordings\n"
            "use FFmpeg stream copy and unique, sanitized .mkv filenames."
        )
        pl.addRow("Recording folder:", self.recording_dir)
        recording_browse = QPushButton("Browse…")
        recording_browse.clicked.connect(self._browse_recording_dir)
        pl.addRow("", recording_browse)

        self.overscan = QDoubleSpinBox()
        self.overscan.setRange(0.0, 5.0)
        self.overscan.setSingleStep(0.1)
        self.overscan.setSuffix(" %")
        self.overscan.setToolTip(
            "Zooms the video slightly so dirty edge rows in broadcast streams are\n"
            "pushed off-screen instead of showing as a faint bright line at the\n"
            "frame edge (0.5% ≈ 5 px per side at 1080p). mpv backend only."
        )
        pl.addRow("Video overscan:", self.overscan)

        self.audio_delay = QDoubleSpinBox()
        self.audio_delay.setRange(-1.0, 1.0)
        self.audio_delay.setSingleStep(0.05)
        self.audio_delay.setDecimals(2)
        self.audio_delay.setSuffix(" s")
        self.audio_delay.setToolTip(
            "Audio sync offset — shifts the audio track relative to the\n"
            "video. Positive values delay the audio (play it later) to\n"
            "compensate for video-path latency such as SVP 4 motion\n"
            "interpolation, whose frame synthesis renders behind the audio\n"
            "clock. Adjusted live from the player's audio menu or the +/-\n"
            "keys; this is the default applied on every playback."
        )
        pl.addRow("Audio sync offset:", self.audio_delay)

        self.interpolation = QCheckBox("Smooth motion (frame interpolation)")
        self.interpolation.setToolTip(
            "Blends frames to smooth out fps/refresh-rate mismatch judder.\n"
            "Small GPU cost; mpv backend only.\n"
            "Note: this is NOT TV-style motion smoothing — it never creates\n"
            "new frames, and has no visible effect when the video fps divides\n"
            "the refresh rate (e.g. 24 fps movies on a 120 Hz display)."
        )
        pl.addRow("", self.interpolation)

        self.svp = QCheckBox("SVP motion interpolation (soap-opera effect)")
        self.svp.setToolTip(
            "True motion interpolation — synthesizes intermediate frames, so\n"
            "24 fps movies move like high-frame-rate video (verified here:\n"
            "23.976 -> 119.88 fps).\n\n"
            "Requires your own SVP 4 install (svp-team.com, 30-day trial);\n"
            "nothing is bundled. Playback then runs in SVP's mpv player,\n"
            "embedded in this window. Costs GPU; takes effect on the next\n"
            "file you play. Not used for live TV."
        )
        from iptv import svp as _svp
        _inst = _svp.find_install()
        if _inst is None or not _inst.mpv_exe:
            self.svp.setEnabled(False)
            if _inst is None:
                self.svp.setText("SVP motion interpolation (SVP 4 not installed)")
                self.svp.setToolTip(
                    "No SVP 4 installation was found on this machine.\n"
                    "Install SVP 4 (30-day trial at svp-team.com) to enable\n"
                    "true motion interpolation, then reopen this dialog."
                )
            else:
                # SVP is there but the user skipped the mpv component, which
                # is the player we embed.
                self.svp.setText("SVP motion interpolation (SVP mpv component missing)")
                self.svp.setToolTip(
                    "SVP 4 is installed but its mpv player component is not.\n"
                    "Re-run the SVP installer and include the mpv package\n"
                    "(\"[VPS_64] mpv video player\"), then reopen this dialog."
                )
        pl.addRow("", self.svp)

        self.milkdrop = QCheckBox("MilkDrop visualizer for audio files")
        self.milkdrop.setToolTip(
            "Plays audio files with a MilkDrop (Butterchurn) visualization\n"
            "instead of a black screen. Presets are .milk files — drop more\n"
            "into the MilkDrop folder or %USERPROFILE%\\.deeptorrent\\presets."
        )
        pl.addRow("", self.milkdrop)

        self.preset = QComboBox()
        for path in list_presets():
            self.preset.addItem(preset_name(path), os.path.basename(path))
        if self.preset.count() == 0:
            self.preset.addItem("(no .milk presets found)", "")
        self.preset.setToolTip("Which MilkDrop preset to start with.")
        pl.addRow("MilkDrop preset:", self.preset)

        self.auto_next = QCheckBox("Auto-try next source on dead stream")
        pl.addRow("", self.auto_next)
        self.body.addWidget(play_group)

        throttle_group = QGroupBox("Torrent Throttling While Playing")
        tl = QFormLayout(throttle_group)
        self.throttle = QCheckBox("Limit torrent speed while playing")
        self.throttle.setToolTip("Caps torrent download/upload while a stream plays so the video doesn't starve.")
        tl.addRow("", self.throttle)
        self.throttle_dl = QSpinBox()
        self.throttle_dl.setRange(0, 1000000)
        self.throttle_dl.setSuffix(" KB/s")
        tl.addRow("Torrent download cap:", self.throttle_dl)
        self.throttle_ul = QSpinBox()
        self.throttle_ul.setRange(0, 1000000)
        self.throttle_ul.setSuffix(" KB/s")
        tl.addRow("Torrent upload cap:", self.throttle_ul)
        self.body.addWidget(throttle_group)

        idx = max(0, self.player.findData(self.config.iptv.preferred_player))
        self.player.setCurrentIndex(idx)
        hidx = max(0, self.hwdec.findData(self.config.iptv.hwdec))
        self.hwdec.setCurrentIndex(hidx if hidx >= 0 else 0)
        self.cache.setValue(self.config.iptv.cache_seconds)
        self.live_pause_buffer.setValue(self.config.iptv.live_pause_buffer_seconds)
        self.recording_dir.setText(self.config.iptv.recording_dir)
        self.overscan.setValue(self.config.iptv.overscan_pct)
        self.audio_delay.setValue(self.config.iptv.audio_delay)
        self.interpolation.setChecked(self.config.iptv.interpolation)
        self.svp.setChecked(self.config.iptv.svp_enabled and self.svp.isEnabled())
        self.milkdrop.setChecked(self.config.iptv.milkdrop_enabled)
        pidx = self.preset.findData(self.config.iptv.milkdrop_preset)
        self.preset.setCurrentIndex(pidx if pidx >= 0 else 0)
        self.auto_next.setChecked(self.config.iptv.auto_try_next_source)
        self.throttle.setChecked(self.config.iptv.throttle_torrents)
        self.throttle_dl.setValue(self.config.iptv.throttle_download_kb)
        self.throttle_ul.setValue(self.config.iptv.throttle_upload_kb)

    def _browse_recording_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose recording folder")
        if directory:
            self.recording_dir.setText(directory)

    def accept(self) -> None:
        recording_dir = self.recording_dir.text().strip()
        if recording_dir:
            try:
                os.makedirs(recording_dir, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(
                    self, "Invalid recording folder",
                    f"Cannot use this recording folder:\n{exc}")
                return
        self.config.iptv.preferred_player = self.player.currentData()
        self.config.iptv.hwdec = self.hwdec.currentData()
        self.config.iptv.cache_seconds = self.cache.value()
        self.config.iptv.live_pause_buffer_seconds = self.live_pause_buffer.value()
        self.config.iptv.recording_dir = recording_dir
        self.config.iptv.overscan_pct = self.overscan.value()
        self.config.iptv.audio_delay = self.audio_delay.value()
        self.config.iptv.interpolation = self.interpolation.isChecked()
        # A greyed-out (SVP missing) box can only stay off; a disabled box
        # still reports its checked state, so gate it explicitly.
        self.config.iptv.svp_enabled = self.svp.isEnabled() and self.svp.isChecked()
        self.config.iptv.milkdrop_enabled = self.milkdrop.isChecked()
        self.config.iptv.milkdrop_preset = self.preset.currentData() or ""
        self.config.iptv.auto_try_next_source = self.auto_next.isChecked()
        self.config.iptv.throttle_torrents = self.throttle.isChecked()
        self.config.iptv.throttle_download_kb = self.throttle_dl.value()
        self.config.iptv.throttle_upload_kb = self.throttle_ul.value()
        super().accept()


# (menu label, dialog class) — Config → IPTV submenu and the Play tab's gear
# picker are both built from this list.
IPTV_SETTINGS_PAGES = [
    ("IPTV Playlist Sources…", IPTVSourcesDialog),
    ("Artwork, Metadata && Cache…", IPTVMetadataDialog),
    ("Subtitles && Languages…", IPTVSubtitlesDialog),
    ("Playback && Video Settings…", IPTVPlaybackDialog),
]
