"""Settings dialogs for DeepFlux — split into focused pages.

Each dialog handles one area of configuration:
  - IndexerSettingsDialog  — Jackett URL, torznab path, auto-start (key + test live in APIKeysDialog)
  - DownloadsSettingsDialog — Torrent save path + Download Manager (IDM-style)
  - BrowserSettingsDialog  — Homepage, ad blocking, bookmark import
  - WebSearchSettingsDialog — Brave/Perplexity keys (DuckDuckGo is keyless)
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from typing import List, Optional, Tuple

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
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config import DeeptorrentConfig, IndexerConfig, LLM_PROVIDER_PRESETS

logger = logging.getLogger(__name__)

_SHARED_STYLE = """
    QDialog { background-color: #0a0a0f; color: #c8d3e0; }
    QLabel { color: #c8d3e0; }
    QGroupBox { color: #c8d3e0; border: 1px solid #1a2a4a; border-radius: 4px; margin-top: 10px; padding-top: 8px; }
    QGroupBox::title { color: #2a7abf; subcontrol-origin: margin; left: 10px; padding: 0 5px; font-weight: 600; }
    QLineEdit, QSpinBox, QComboBox { background-color: #0d1117; color: #c8d3e0; border: 1px solid #1a2a4a; padding: 2px 8px; border-radius: 3px; }
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid #2a7abf; }
    QComboBox QAbstractItemView { background-color: #0d1117; color: #c8d3e0; selection-background-color: #1a2a4a; }
    QCheckBox { color: #c8d3e0; }
    QCheckBox::indicator { border: 1px solid #1a2a4a; border-radius: 3px; width: 16px; height: 16px; }
    QCheckBox::indicator:checked { background-color: #2a7abf; border-color: #2a7abf; }
    QPushButton { background-color: #111827; color: #c8d3e0; border: 1px solid #1a2a4a; padding: 2px 10px; border-radius: 3px; }
    QPushButton:hover { background-color: #1a2a4a; border: 1px solid #2a7abf; color: #2a7abf; }
    QLabel#hint { color: #4a6a8a; font-size: 11px; }
"""


# ---------------------------------------------------------------------------
# Indexer Settings — Jackett
# ---------------------------------------------------------------------------

class IndexerSettingsDialog(QDialog):
    """Settings for the Jackett indexer integration.

    The API key and the Test Connection button live in the API Keys window
    (File → API Keys) — this page keeps the connection/tuning fields."""

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Jackett Settings")
        self.setMinimumWidth(500)
        self.setStyleSheet(_SHARED_STYLE)
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        idx_group = QGroupBox("Jackett")
        idx_layout = QFormLayout(idx_group)

        self.idx_url = QLineEdit()
        self.idx_url.setPlaceholderText("http://localhost:9117")
        idx_layout.addRow("URL:", self.idx_url)

        key_note = QLabel("API key is managed in File → API Keys.")
        key_note.setObjectName("hint")
        idx_layout.addRow("", key_note)

        self.idx_torznab_path = QLineEdit()
        self.idx_torznab_path.setPlaceholderText("/api/v2.0/indexers/all/results/torznab")
        idx_layout.addRow("Torznab Path:", self.idx_torznab_path)

        self.idx_auto_start = QCheckBox("Start Jackett automatically when it's not running")
        self.idx_auto_start.setToolTip(
            "On startup (and hourly while running), DeepFlux starts Jackett if it's\n"
            "configured but unreachable — Windows service first, then the executable.\n"
            "The sources list is also synced from Jackett at startup and once per day."
        )
        idx_layout.addRow("", self.idx_auto_start)

        self.idx_jackett_path = QLineEdit()
        self.idx_jackett_path.setPlaceholderText("Optional — path to JackettTray.exe (auto-detect when blank)")
        idx_layout.addRow("Executable:", self.idx_jackett_path)

        idx_hint = QLabel(
            "Jackett runs on port 9117 — download from https://github.com/Jackett/Jackett\n"
            "Add your torrent sites in the Jackett web UI, then set the API key in File → API Keys\n"
            "(the Test Connection button lives there too). Leave the URL blank to search your\n"
            "enabled Sources directly instead. To turn Jackett off entirely, use the\n"
            "'Use Jackett' toggle in Download → Sources."
        )
        idx_hint.setObjectName("hint")
        idx_hint.setWordWrap(True)
        idx_layout.addRow("", idx_hint)

        layout.addWidget(idx_group)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_values(self) -> None:
        self.idx_url.setText(self.config.indexer.url)
        self.idx_torznab_path.setText(self.config.indexer.torznab_path)
        self.idx_auto_start.setChecked(self.config.indexer.auto_start)
        self.idx_jackett_path.setText(self.config.indexer.jackett_path)

    def _save_and_accept(self) -> None:
        self.config.indexer.url = self.idx_url.text().strip() or "http://localhost:9117"
        self.config.indexer.torznab_path = self.idx_torznab_path.text().strip() or "/api/v2.0/indexers/all/results/torznab"
        self.config.indexer.auto_start = self.idx_auto_start.isChecked()
        self.config.indexer.jackett_path = self.idx_jackett_path.text().strip()
        self.accept()


# ---------------------------------------------------------------------------
# Downloads Settings — Torrent save path + Download Manager
# ---------------------------------------------------------------------------

class DownloadsSettingsDialog(QDialog):
    """Settings for torrent save path and the IDM-style download manager."""

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Downloads Settings")
        self.setMinimumWidth(550)
        self.setStyleSheet(_SHARED_STYLE)
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Torrent save path
        dl_group = QGroupBox("Torrent Downloads")
        dl_layout = QFormLayout(dl_group)

        self.save_path = QLineEdit()
        self.save_path.setPlaceholderText("C:\\Users\\...\\Downloads\\DeepFlux")
        dl_layout.addRow("Default Save Path:", self.save_path)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_save_path)
        dl_layout.addRow("", browse_btn)

        self.tor_download_limit = QSpinBox()
        self.tor_download_limit.setRange(0, 999999)
        self.tor_download_limit.setSuffix(" KB/s")
        self.tor_download_limit.setSpecialValueText("Unlimited")
        dl_layout.addRow("Download Rate Limit:", self.tor_download_limit)

        self.tor_upload_limit = QSpinBox()
        self.tor_upload_limit.setRange(0, 999999)
        self.tor_upload_limit.setSuffix(" KB/s")
        self.tor_upload_limit.setSpecialValueText("Unlimited")
        dl_layout.addRow("Upload Rate Limit:", self.tor_upload_limit)

        self.tor_listen_port = QSpinBox()
        self.tor_listen_port.setRange(0, 65535)
        self.tor_listen_port.setSpecialValueText("Random")
        dl_layout.addRow("Listen Port (requires restart):", self.tor_listen_port)

        self.tor_max_connections = QSpinBox()
        self.tor_max_connections.setRange(0, 10000)
        self.tor_max_connections.setSpecialValueText("Default")
        dl_layout.addRow("Max Connections (requires restart):", self.tor_max_connections)

        self.tor_restore_completed = QCheckBox("Keep completed torrents in the list across restarts")
        dl_layout.addRow("", self.tor_restore_completed)

        # --- Queue / concurrency ---
        queue_group = QGroupBox("Torrent Queue")
        queue_layout = QFormLayout(queue_group)

        self.tor_max_downloading = QSpinBox()
        self.tor_max_downloading.setRange(0, 1000)
        self.tor_max_downloading.setSpecialValueText("Unlimited")
        self.tor_max_downloading.setValue(5)
        queue_layout.addRow("Max downloading at once:", self.tor_max_downloading)

        self.tor_max_seeding = QSpinBox()
        self.tor_max_seeding.setRange(0, 1000)
        self.tor_max_seeding.setSpecialValueText("Unlimited")
        self.tor_max_seeding.setValue(5)
        queue_layout.addRow("Max seeding at once:", self.tor_max_seeding)

        self.tor_max_active = QSpinBox()
        self.tor_max_active.setRange(0, 1000)
        self.tor_max_active.setSpecialValueText("Unlimited")
        self.tor_max_active.setValue(10)
        queue_layout.addRow("Max active torrents:", self.tor_max_active)

        self.tor_max_queued = QSpinBox()
        self.tor_max_queued.setRange(0, 10000)
        self.tor_max_queued.setSpecialValueText("Unlimited")
        self.tor_max_queued.setValue(0)
        queue_layout.addRow("Max queued torrents:", self.tor_max_queued)

        self.tor_auto_manage_interval = QSpinBox()
        self.tor_auto_manage_interval.setRange(5, 3600)
        self.tor_auto_manage_interval.setSuffix(" s")
        self.tor_auto_manage_interval.setValue(30)
        queue_layout.addRow("Auto-manage interval:", self.tor_auto_manage_interval)

        self.tor_seed_ratio = QDoubleSpinBox()
        self.tor_seed_ratio.setRange(0.0, 100.0)
        self.tor_seed_ratio.setSingleStep(0.1)
        self.tor_seed_ratio.setSpecialValueText("Unlimited")
        self.tor_seed_ratio.setSuffix(" x")
        queue_layout.addRow("Stop seeding at ratio:", self.tor_seed_ratio)

        self.tor_seed_time = QSpinBox()
        self.tor_seed_time.setRange(0, 10080)
        self.tor_seed_time.setSingleStep(60)
        self.tor_seed_time.setSpecialValueText("Unlimited")
        self.tor_seed_time.setSuffix(" min")
        queue_layout.addRow("Stop seeding after:", self.tor_seed_time)

        self.tor_auto_save = QSpinBox()
        self.tor_auto_save.setRange(10, 3600)
        self.tor_auto_save.setSuffix(" s")
        self.tor_auto_save.setValue(60)
        queue_layout.addRow("Auto-save state interval:", self.tor_auto_save)

        layout.addWidget(dl_group)
        layout.addWidget(queue_group)

        # Download Manager (IDM-style)
        dm_group = QGroupBox("Download Manager")
        dm_layout = QFormLayout(dm_group)

        self.dm_max_concurrent = QSpinBox()
        self.dm_max_concurrent.setRange(1, 20)
        self.dm_max_concurrent.setValue(3)
        dm_layout.addRow("Max Concurrent Downloads:", self.dm_max_concurrent)

        self.dm_max_connections = QSpinBox()
        self.dm_max_connections.setRange(1, 32)
        self.dm_max_connections.setValue(8)
        dm_layout.addRow("Max Connections per Download:", self.dm_max_connections)

        self.dm_default_folder = QLineEdit()
        self.dm_default_folder.setPlaceholderText("C:\\Users\\...\\Downloads\\DeepFlux")
        dm_layout.addRow("Default Download Folder:", self.dm_default_folder)

        dm_browse_btn = QPushButton("Browse...")
        dm_browse_btn.clicked.connect(self._browse_dm_folder)
        dm_layout.addRow("", dm_browse_btn)

        self.dm_bandwidth = QSpinBox()
        self.dm_bandwidth.setRange(0, 999999)
        self.dm_bandwidth.setSuffix(" KB/s")
        self.dm_bandwidth.setSpecialValueText("Unlimited")
        self.dm_bandwidth.setValue(0)
        dm_layout.addRow("Bandwidth Limit:", self.dm_bandwidth)

        self.dm_auto_start = QCheckBox("Automatically start downloads when added")
        dm_layout.addRow("", self.dm_auto_start)

        self.dm_segment_threshold = QSpinBox()
        self.dm_segment_threshold.setRange(1, 1000)
        self.dm_segment_threshold.setSuffix(" MB")
        self.dm_segment_threshold.setValue(1)
        dm_layout.addRow("Segment Threshold (files smaller than this use single-stream):", self.dm_segment_threshold)

        dm_hint = QLabel("The download manager uses segmented downloading with dynamic rebalancing\n"
                         "for maximum speed, similar to Internet Download Manager.")
        dm_hint.setObjectName("hint")
        dm_hint.setWordWrap(True)
        dm_layout.addRow("", dm_hint)

        layout.addWidget(dm_group)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_values(self) -> None:
        self.save_path.setText(self.config.default_save_path)
        self.tor_download_limit.setValue(self.config.torrents.download_rate_limit_kb)
        self.tor_upload_limit.setValue(self.config.torrents.upload_rate_limit_kb)
        self.tor_listen_port.setValue(self.config.torrents.listen_port)
        self.tor_max_connections.setValue(self.config.torrents.max_connections)
        self.tor_restore_completed.setChecked(self.config.torrents.restore_completed)
        self.tor_max_downloading.setValue(self.config.torrents.max_downloading_torrents)
        self.tor_max_seeding.setValue(self.config.torrents.max_seeding_torrents)
        self.tor_max_active.setValue(self.config.torrents.max_active_torrents)
        self.tor_max_queued.setValue(self.config.torrents.max_queued_torrents)
        self.tor_auto_manage_interval.setValue(self.config.torrents.auto_manage_interval_seconds)
        self.tor_seed_ratio.setValue(float(self.config.torrents.seed_ratio_limit))
        self.tor_seed_time.setValue(self.config.torrents.seed_time_limit_minutes)
        self.tor_auto_save.setValue(self.config.torrents.auto_save_state_seconds)
        self.dm_max_concurrent.setValue(self.config.download.max_concurrent)
        self.dm_max_connections.setValue(self.config.download.max_connections_per_download)
        self.dm_default_folder.setText(self.config.download.default_folder)
        self.dm_bandwidth.setValue(self.config.download.bandwidth_limit_bps // 1024)
        self.dm_auto_start.setChecked(self.config.download.auto_start)
        self.dm_segment_threshold.setValue(self.config.download.segment_threshold_mb)

    def _browse_save_path(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Default Save Path")
        if path:
            self.save_path.setText(path)

    def _browse_dm_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Default Download Folder")
        if path:
            self.dm_default_folder.setText(path)

    def _save_and_accept(self) -> None:
        self.config.default_save_path = self.save_path.text().strip() or self.config.default_save_path
        self.config.torrents.download_rate_limit_kb = self.tor_download_limit.value()
        self.config.torrents.upload_rate_limit_kb = self.tor_upload_limit.value()
        self.config.torrents.listen_port = self.tor_listen_port.value()
        self.config.torrents.max_connections = self.tor_max_connections.value()
        self.config.torrents.restore_completed = self.tor_restore_completed.isChecked()
        self.config.torrents.max_downloading_torrents = self.tor_max_downloading.value()
        self.config.torrents.max_seeding_torrents = self.tor_max_seeding.value()
        self.config.torrents.max_active_torrents = self.tor_max_active.value()
        self.config.torrents.max_queued_torrents = self.tor_max_queued.value()
        self.config.torrents.auto_manage_interval_seconds = self.tor_auto_manage_interval.value()
        self.config.torrents.seed_ratio_limit = self.tor_seed_ratio.value()
        self.config.torrents.seed_time_limit_minutes = self.tor_seed_time.value()
        self.config.torrents.auto_save_state_seconds = self.tor_auto_save.value()
        self.config.download.max_concurrent = self.dm_max_concurrent.value()
        self.config.download.max_connections_per_download = self.dm_max_connections.value()
        self.config.download.default_folder = self.dm_default_folder.text().strip() or self.config.download.default_folder
        self.config.download.bandwidth_limit_bps = self.dm_bandwidth.value() * 1024
        self.config.download.auto_start = self.dm_auto_start.isChecked()
        self.config.download.segment_threshold_mb = self.dm_segment_threshold.value()
        self.accept()


# ---------------------------------------------------------------------------
# Browser Settings — Homepage
# ---------------------------------------------------------------------------

class BrowserSettingsDialog(QDialog):
    """Settings for the built-in browser."""

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Browser Settings")
        self.setMinimumWidth(450)
        self.setStyleSheet(_SHARED_STYLE)
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        browser_group = QGroupBox("Browser")
        browser_layout = QFormLayout(browser_group)

        self.browser_homepage = QLineEdit()
        self.browser_homepage.setPlaceholderText("Default: https://deepflux.space/")
        browser_layout.addRow("Homepage:", self.browser_homepage)

        hint = QLabel("The page loaded when the Browse tab opens or the Home button is clicked. Default: https://deepflux.space/ — leave empty to reset to the default.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        browser_layout.addRow("", hint)

        from PySide6.QtWidgets import QCheckBox
        self.browser_adblock_check = QCheckBox("Enable ad blocking (blocks ads, trackers, and analytics)")
        browser_layout.addRow("", self.browser_adblock_check)

        adblock_hint = QLabel("When enabled, requests to known ad/tracker domains are blocked before they load. Off by default. You can also toggle this from the AdBlock button in the browser toolbar.")
        adblock_hint.setObjectName("hint")
        adblock_hint.setWordWrap(True)
        browser_layout.addRow("", adblock_hint)

        self.import_bookmarks_btn = QPushButton("Import Bookmarks from Other Browsers...")
        self.import_bookmarks_btn.setObjectName("btn_secondary")
        self.import_bookmarks_btn.clicked.connect(self._on_import_bookmarks)
        browser_layout.addRow("", self.import_bookmarks_btn)

        layout.addWidget(browser_group)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_values(self) -> None:
        self.browser_homepage.setText(self.config.browser.homepage)
        self.browser_adblock_check.setChecked(self.config.browser.adblock_enabled)

    def _save_and_accept(self) -> None:
        self.config.browser.homepage = self.browser_homepage.text().strip()
        self.config.browser.adblock_enabled = self.browser_adblock_check.isChecked()
        self.accept()

    def _on_import_bookmarks(self) -> None:
        """Delegate to the main window's import flow (single code path)."""
        parent = self.parent()
        if parent is not None and hasattr(parent, "_import_bookmarks"):
            parent._import_bookmarks()


# ---------------------------------------------------------------------------
# Web Search Settings — Brave / Perplexity keys (DuckDuckGo is keyless)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# API Keys — one window for every API key the app can use
# ---------------------------------------------------------------------------

def _api_hint(text: str) -> QLabel:
    hint = QLabel(text)
    hint.setObjectName("hint")
    hint.setWordWrap(True)
    return hint


class APIKeysDialog(QDialog):
    """Unified API keys window — every external service key in one place."""

    # Worker-thread → GUI-thread bridge for the post-test Jackett sources
    # fetch. Carries List[SourceConfig] on success, the Exception on failure.
    jkt_fetch_done = Signal(object)

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("API Keys")
        self.setMinimumWidth(560)
        self.setStyleSheet(_SHARED_STYLE)
        self.jkt_fetch_done.connect(self._on_jkt_fetch_done)
        self._build_ui()
        self._load_values()

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(body)
        layout.setSpacing(8)

        # --- AI Agent (LLM) ---
        llm_group = QGroupBox("AI Agent (LLM)")
        fl = QFormLayout(llm_group)
        self.llm_endpoint = QComboBox()
        for pid, preset in LLM_PROVIDER_PRESETS.items():
            self.llm_endpoint.addItem(preset["label"], pid)
        self.llm_endpoint.currentIndexChanged.connect(self._on_endpoint_changed)
        fl.addRow("API endpoint:", self.llm_endpoint)
        self.llm_base_url = QLineEdit()
        fl.addRow("Base URL:", self.llm_base_url)
        self.llm_key = QLineEdit()
        self.llm_key.setEchoMode(QLineEdit.Password)
        self.llm_key.setPlaceholderText("sk-...")
        fl.addRow("API Key:", self.llm_key)
        fl.addRow("", _api_hint(
            "DeepSeek or OpenRouter key — powers the conversational agent. "
            "Get one at https://platform.deepseek.com/api_keys (DeepSeek) or "
            "https://openrouter.ai/keys (OpenRouter). Without a key the agent "
            "runs in demo mode."))
        layout.addWidget(llm_group)

        # --- Jackett ---
        jkt_group = QGroupBox("Jackett (Torrent Search)")
        fl = QFormLayout(jkt_group)
        self.jkt_key = QLineEdit()
        self.jkt_key.setEchoMode(QLineEdit.Password)
        self.jkt_key.setPlaceholderText("API key from Jackett dashboard")
        fl.addRow("API Key:", self.jkt_key)
        self.jkt_test_btn = QPushButton("Test Connection")
        self.jkt_test_btn.clicked.connect(self._test_jackett)
        fl.addRow("", self.jkt_test_btn)
        self.jkt_test_result = QLabel("")
        self.jkt_test_result.setObjectName("hint")
        self.jkt_test_result.setWordWrap(True)
        fl.addRow("", self.jkt_test_result)
        fl.addRow("", _api_hint(
            "Copy from the Jackett web UI (http://localhost:9117). "
            "Jackett aggregates torrent indexer results for the agent. The URL "
            "and Torznab path are configured in Download → Jackett Settings."))
        layout.addWidget(jkt_group)

        # --- Web Search ---
        ws_group = QGroupBox("Web Search")
        fl = QFormLayout(ws_group)
        self.brave_key = QLineEdit()
        self.brave_key.setEchoMode(QLineEdit.Password)
        self.brave_key.setPlaceholderText("BSA...")
        fl.addRow("Brave API Key:", self.brave_key)
        self.pplx_key = QLineEdit()
        self.pplx_key.setEchoMode(QLineEdit.Password)
        self.pplx_key.setPlaceholderText("pplx-...")
        fl.addRow("Perplexity API Key:", self.pplx_key)
        fl.addRow("", _api_hint(
            "DuckDuckGo always works without a key. Brave adds an independent "
            "index (free tier: 1 query/sec). Perplexity adds synthesized answers. "
            "Both are optional — each is only used when its key is set."))
        layout.addWidget(ws_group)

        # --- TMDb ---
        tmdb_group = QGroupBox("TMDb (Metadata & Artwork)")
        fl = QFormLayout(tmdb_group)
        self.tmdb_key = QLineEdit()
        self.tmdb_key.setEchoMode(QLineEdit.Password)
        self.tmdb_key.setPlaceholderText("TMDb API key (optional)")
        fl.addRow("API Key:", self.tmdb_key)
        fl.addRow("", _api_hint(
            "Optional — enter your own key from themoviedb.org/settings/api "
            "for TMDb posters/metadata. Left blank, TVmaze remains the keyless "
            "fallback for series."))
        layout.addWidget(tmdb_group)

        # --- ThePornDB ---
        tpdb_group = QGroupBox("ThePornDB (Adult VOD Metadata & Artwork)")
        fl = QFormLayout(tpdb_group)
        self.tpdb_key = QLineEdit()
        self.tpdb_key.setEchoMode(QLineEdit.Password)
        self.tpdb_key.setPlaceholderText("API token from theporndb.net (optional)")
        fl.addRow("API Token:", self.tpdb_key)
        fl.addRow("", _api_hint(
            "Optional — TMDb filters adult titles out of its search results, "
            "so adult VOD entries stay artwork-less without this. Only used "
            "for entries whose group-title marks them as adult."))
        layout.addWidget(tpdb_group)

        # --- OpenSubtitles ---
        ost_group = QGroupBox("OpenSubtitles (Subtitle Downloads)")
        fl = QFormLayout(ost_group)
        self.ost_key = QLineEdit()
        self.ost_key.setEchoMode(QLineEdit.Password)
        self.ost_key.setPlaceholderText("API key from opensubtitles.com")
        fl.addRow("API Key:", self.ost_key)
        self.ost_user = QLineEdit()
        fl.addRow("Username (optional):", self.ost_user)
        self.ost_pass = QLineEdit()
        self.ost_pass.setEchoMode(QLineEdit.Password)
        fl.addRow("Password (optional):", self.ost_pass)
        fl.addRow("", _api_hint(
            "Used by the player's CC menu → \"Find subtitles online…\". "
            "Account credentials are optional and raise the daily download quota."))
        layout.addWidget(ost_group)

        layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _selected_endpoint(self) -> str:
        pid = self.llm_endpoint.currentData()
        return pid if pid in LLM_PROVIDER_PRESETS else "deepseek"

    def _on_endpoint_changed(self, _index: int) -> None:
        """Endpoint switch: swap the base URL unless the user typed a custom one."""
        preset = LLM_PROVIDER_PRESETS[self._selected_endpoint()]
        self.llm_base_url.setPlaceholderText(preset["base_url"])
        preset_urls = {p["base_url"] for p in LLM_PROVIDER_PRESETS.values()}
        if self.llm_base_url.text().strip() in preset_urls:
            self.llm_base_url.setText(preset["base_url"])

    # -- Jackett test (moved from Jackett Settings) --------------------------

    def _test_jackett(self) -> None:
        import requests
        import xml.etree.ElementTree as ET

        # The URL/path are configured in Download → Jackett Settings; the key
        # being tested is the TYPED (not yet saved) one.
        url = self.config.indexer.url.strip()
        api_key = self.jkt_key.text().strip()
        torznab_path = self.config.indexer.torznab_path.strip() or "/api/v2.0/indexers/all/results/torznab"

        if not url or not api_key:
            self.jkt_test_result.setText(
                "Enter the Jackett API key above first (the URL is set in Download → Jackett Settings).")
            self.jkt_test_result.setStyleSheet("color: #ffcc00;")
            return

        self.jkt_test_result.setText("Testing connection...")
        self.jkt_test_result.setStyleSheet("color: #2a7abf;")

        try:
            full_url = f"{url.rstrip('/')}{torznab_path}"
            # t=caps validates URL + path + API key in ~2s. A real search
            # (t=search) against the aggregate 'all' endpoint waits for EVERY
            # configured indexer, so slow/dead indexers blow the dialog timeout
            # even though the connection itself is fine.
            resp = requests.get(full_url, params={"apikey": api_key, "t": "caps"}, timeout=10)
        except Exception as exc:
            self.jkt_test_result.setText(f"Connection failed: {exc}")
            self.jkt_test_result.setStyleSheet("color: #ff3366;")
            return

        # Jackett reports API errors as HTTP 200 with an <error> XML body
        # (e.g. code 100 "Invalid API Key") — check the body, not the status.
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            self.jkt_test_result.setText(f"HTTP {resp.status_code}: response is not Torznab XML — check the URL/path.")
            self.jkt_test_result.setStyleSheet("color: #ffcc00;")
            return
        err = root if root.tag == "error" else root.find(".//error")
        if err is not None:
            desc = err.get("description") or (err.text or "").strip() or "unknown error"
            self.jkt_test_result.setText(f"Jackett rejected the request: {desc}")
            self.jkt_test_result.setStyleSheet("color: #ff3366;")
        elif root.tag == "caps":
            self.jkt_test_result.setText("Connection successful! Fetching indexer list…")
            self.jkt_test_result.setStyleSheet("color: #00ff9d;")
            # Fetch with the TYPED (not yet saved) key via a probe config, so
            # cancelling the dialog leaves the saved key untouched. The fetched
            # sources land in the live config either way — same as the Fetch
            # button in Download → Sources.
            probe = copy.copy(self.config)
            probe.indexer = IndexerConfig(
                url=url, api_key=api_key, torznab_path=torznab_path,
                timeout=self.config.indexer.timeout,
                auto_start=self.config.indexer.auto_start,
                jackett_path=self.config.indexer.jackett_path,
            )
            threading.Thread(target=self._jkt_fetch_worker, args=(probe,), daemon=True).start()
        else:
            self.jkt_test_result.setText("Got a response but it doesn't look like Torznab XML. Check the URL/path.")
            self.jkt_test_result.setStyleSheet("color: #ffcc00;")

    def _jkt_fetch_worker(self, probe: DeeptorrentConfig) -> None:
        from infra import jackett
        try:
            sources = jackett.fetch_indexers(probe)
        except Exception as exc:
            sources = exc
        try:
            self.jkt_fetch_done.emit(sources)
        except RuntimeError:
            pass  # dialog closed mid-fetch

    def _on_jkt_fetch_done(self, outcome) -> None:
        from infra import jackett
        if isinstance(outcome, Exception):
            self.jkt_test_result.setText(f"Connection OK, but the indexer fetch failed: {outcome}")
            self.jkt_test_result.setStyleSheet("color: #ffcc00;")
            return
        if not outcome:
            self.jkt_test_result.setText("Connection OK, but Jackett has no configured indexers to import.")
            self.jkt_test_result.setStyleSheet("color: #ffcc00;")
            return
        self.config.sources.sources = jackett.merge_sources(self.config.sources.sources, outcome)
        self.config.sources.last_jackett_fetch = time.time()
        enabled = sum(1 for s in self.config.sources.sources if s.enabled)
        self.jkt_test_result.setText(
            f"Connection successful! Synced {len(outcome)} indexer(s) from Jackett ({enabled} enabled)."
        )
        self.jkt_test_result.setStyleSheet("color: #00ff9d;")

    def _load_values(self) -> None:
        provider = self.config.llm.provider
        idx = self.llm_endpoint.findData(provider if provider in LLM_PROVIDER_PRESETS else "deepseek")
        self.llm_endpoint.blockSignals(True)
        self.llm_endpoint.setCurrentIndex(max(idx, 0))
        self.llm_endpoint.blockSignals(False)
        self.llm_base_url.setText(self.config.llm.base_url)
        self.llm_base_url.setPlaceholderText(
            LLM_PROVIDER_PRESETS[self._selected_endpoint()]["base_url"])
        self.llm_key.setText(self.config.llm.api_key)
        self.jkt_key.setText(self.config.indexer.api_key)
        self.brave_key.setText(self.config.web_search.brave_api_key)
        self.pplx_key.setText(self.config.web_search.api_key)
        self.tmdb_key.setText(self.config.iptv.tmdb_api_key)
        self.tpdb_key.setText(self.config.iptv.tpdb_api_key)
        self.ost_key.setText(self.config.iptv.opensubtitles_api_key)
        self.ost_user.setText(self.config.iptv.opensubtitles_username)
        self.ost_pass.setText(self.config.iptv.opensubtitles_password)

    def _save_and_accept(self) -> None:
        self.config.llm.api_key = self.llm_key.text().strip()
        self.config.llm.provider = self._selected_endpoint() if self.config.llm.api_key else "dummy"
        self.config.llm.base_url = self.llm_base_url.text().strip()
        self.config.indexer.api_key = self.jkt_key.text().strip()
        self.config.web_search.brave_api_key = self.brave_key.text().strip()
        self.config.web_search.api_key = self.pplx_key.text().strip()
        self.config.iptv.tmdb_api_key = self.tmdb_key.text().strip()
        self.config.iptv.tpdb_api_key = self.tpdb_key.text().strip()
        self.config.iptv.opensubtitles_api_key = self.ost_key.text().strip()
        self.config.iptv.opensubtitles_username = self.ost_user.text().strip()
        self.config.iptv.opensubtitles_password = self.ost_pass.text()
        self.accept()


# ---------------------------------------------------------------------------
# Bookmark Import — pick a detected browser, import its bookmarks
# ---------------------------------------------------------------------------

class BookmarkImportDialog(QDialog):
    """Lets the user pick a detected browser (or a bookmarks file) to import from."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Bookmarks")
        self.setMinimumWidth(420)
        self.setStyleSheet(_SHARED_STYLE)
        self.imported: List[Tuple[str, str, str]] = []  # (title, url, folder)
        self._build_ui()

    def _build_ui(self) -> None:
        from dlmgr.bookmarks_import import detect_browser_sources

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Import bookmarks from:"))

        self.list = QListWidget()
        self._sources = detect_browser_sources()
        for src in self._sources:
            QListWidgetItem(src.browser, self.list)
        if self._sources:
            self.list.setCurrentRow(0)
        layout.addWidget(self.list)

        if not self._sources:
            note = QLabel("No Chrome/Edge/Brave/Firefox bookmarks found automatically.")
            note.setObjectName("hint")
            note.setWordWrap(True)
            layout.addWidget(note)

        browse_btn = QPushButton("Choose Bookmarks file manually...")
        browse_btn.setObjectName("btn_secondary")
        browse_btn.clicked.connect(self._import_from_file)
        layout.addWidget(browse_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._import_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _import_selected(self) -> None:
        from dlmgr.bookmarks_import import read_bookmarks

        row = self.list.currentRow()
        if row < 0 or row >= len(self._sources):
            QMessageBox.information(self, "Import Bookmarks", "Select a browser first.")
            return
        try:
            pairs = read_bookmarks(self._sources[row])
        except Exception as exc:
            QMessageBox.warning(self, "Import Bookmarks", f"Failed to read bookmarks: {exc}")
            return
        if not pairs:
            QMessageBox.information(self, "Import Bookmarks", "No bookmarks found in that browser.")
            return
        self.imported = pairs
        self.accept()

    def _import_from_file(self) -> None:
        from dlmgr.bookmarks_import import read_chromium_bookmarks

        path, _ = QFileDialog.getOpenFileName(
            self, "Select a Chrome/Edge Bookmarks file", "", "Bookmarks file (Bookmarks);;All files (*)"
        )
        if not path:
            return
        try:
            pairs = read_chromium_bookmarks(path)
        except Exception as exc:
            QMessageBox.warning(self, "Import Bookmarks", f"Failed to read bookmarks: {exc}")
            return
        if not pairs:
            QMessageBox.information(self, "Import Bookmarks", "No bookmarks found in that file.")
            return
        self.imported = pairs
        self.accept()
