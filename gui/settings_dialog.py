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
import sys
import threading
import time
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, Signal
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
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config import BROWSER_SEARCH_ENGINES, DeeptorrentConfig, DownloadCategory, IndexerConfig, LLM_PROVIDER_PRESETS
from gui.window_sizing import roomy
from gui.i18n import tr

logger = logging.getLogger(__name__)

_SHARED_STYLE = """
    QDialog { background-color: #0a0a0f; color: #ffffff; }
    QLabel { color: #ffffff; }
    QGroupBox { color: #ffffff; border: 3px solid #1a2a4a; border-radius: 4px; margin-top: 10px; padding-top: 8px; }
    QGroupBox::title { color: #2a7abf; subcontrol-origin: margin; left: 10px; padding: 0 5px; font-weight: 600; }
    QLineEdit, QSpinBox, QComboBox { background-color: #0d1117; color: #ffffff; border: 3px solid #1a2a4a; padding: 2px 8px; border-radius: 3px; }
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 3px solid #2a7abf; }
    QComboBox QAbstractItemView { background-color: #0d1117; color: #ffffff; selection-background-color: #1a2a4a; }
    QCheckBox { color: #ffffff; }
    QCheckBox::indicator { border: 3px solid #1a2a4a; border-radius: 3px; width: 16px; height: 16px; }
    QCheckBox::indicator:checked { background-color: #2a7abf; border-color: #2a7abf; }
    QPushButton { background-color: #111827; color: #ffffff; border: 3px solid #1a2a4a; padding: 2px 10px; border-radius: 3px; }
    QPushButton:hover { background-color: #1a2a4a; border: 3px solid #2a7abf; color: #2a7abf; }
    QLabel#hint { color: #4a6a8a; font-size: 17px; }
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
        self.setWindowTitle(tr('Jackett Settings'))
        self.setMinimumWidth(500)
        self.setStyleSheet(_SHARED_STYLE)
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        idx_group = QGroupBox(tr('Jackett'))
        idx_layout = QFormLayout(idx_group)

        self.idx_url = QLineEdit()
        self.idx_url.setPlaceholderText("http://localhost:9117")
        idx_layout.addRow(tr('URL:'), self.idx_url)

        key_note = QLabel(tr('API key is managed in File → API Keys.'))
        key_note.setObjectName("hint")
        idx_layout.addRow("", key_note)

        self.idx_torznab_path = QLineEdit()
        self.idx_torznab_path.setPlaceholderText(tr('/api/v2.0/indexers/all/results/torznab'))
        idx_layout.addRow(tr('Torznab Path:'), self.idx_torznab_path)

        self.idx_auto_start = QCheckBox(tr("Start Jackett automatically when it's not running"))
        self.idx_auto_start.setToolTip(
            "On startup (and hourly while running), DeepFlux starts Jackett if it's\n"
            "configured but unreachable — Windows service first, then the executable.\n"
            "The sources list is also synced from Jackett at startup and once per day."
        )
        idx_layout.addRow("", self.idx_auto_start)

        self.idx_jackett_path = QLineEdit()
        self.idx_jackett_path.setPlaceholderText(tr('Optional — path to JackettTray.exe (auto-detect when blank)'))
        idx_layout.addRow(tr('Executable:'), self.idx_jackett_path)

        self.idx_flaresolverr = QLineEdit()
        self.idx_flaresolverr.setPlaceholderText("http://127.0.0.1:8191/")
        self.idx_flaresolverr.setToolTip(
            "FlareSolverr solves Cloudflare's anti-bot challenge for indexers\n"
            "behind it (in a 2026-10-13 measurement, 19 of 22 failing indexers\n"
            "were Cloudflare-walled — including 1337x, eztv and kickasstorrents).\n"
            "Saving a non-empty URL writes it into Jackett's server config.\n"
            "Clearing the field here never removes it from Jackett."
        )
        idx_layout.addRow(tr('FlareSolverr API URL:'), self.idx_flaresolverr)

        flare_hint = QLabel(
            "Optional. FlareSolverr (github.com/FlareSolverr/FlareSolverr) unlocks\n"
            "Cloudflare-protected indexers in Jackett — run it, then paste its URL\n"
            "here (default port 8191). Saving pushes the URL into Jackett for you."
        )
        flare_hint.setObjectName("hint")
        flare_hint.setWordWrap(True)
        idx_layout.addRow("", flare_hint)

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
        self.idx_flaresolverr.setText(self.config.indexer.flare_solverr_url)

    def _save_and_accept(self) -> None:
        self.config.indexer.url = self.idx_url.text().strip() or "http://localhost:9117"
        self.config.indexer.torznab_path = self.idx_torznab_path.text().strip() or "/api/v2.0/indexers/all/results/torznab"
        self.config.indexer.auto_start = self.idx_auto_start.isChecked()
        self.config.indexer.jackett_path = self.idx_jackett_path.text().strip()
        self.config.indexer.flare_solverr_url = self.idx_flaresolverr.text().strip()
        self.accept()


# ---------------------------------------------------------------------------
# Downloads Settings — Torrent save path + Download Manager
# ---------------------------------------------------------------------------

DOWNLOAD_SETTINGS_PAGES = [
    ("Torrent Download Settings…", "torrent"),
    ("Torrent Queue Settings…", "queue"),
    ("Download Manager Settings…", "manager"),
]


class DownloadsSettingsDialog(QDialog):
    """Settings for torrent save path and the IDM-style download manager."""

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None,
                 page: str = "all") -> None:
        super().__init__(parent)
        self.config = config
        self._page = page
        titles = {
            "torrent": "Torrent Downloads Settings",
            "queue": "Torrent Queue Settings",
            "manager": "Download Manager Settings",
        }
        self.setWindowTitle(titles.get(page, "Downloads Settings"))
        self.setMinimumWidth(480)
        roomy(self)
        self.setStyleSheet(_SHARED_STYLE)
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(body)

        # Torrent save path
        dl_group = QGroupBox(tr('Torrent Downloads'))
        dl_layout = QFormLayout(dl_group)

        self.save_path = QLineEdit()
        self.save_path.setPlaceholderText(tr('C:\\Users\\...\\Downloads\\DeepFlux'))
        dl_layout.addRow(tr('Default Save Path:'), self.save_path)

        browse_btn = QPushButton(tr('Browse...'))
        browse_btn.clicked.connect(self._browse_save_path)
        dl_layout.addRow("", browse_btn)

        self.tor_download_limit = QSpinBox()
        self.tor_download_limit.setRange(0, 999999)
        self.tor_download_limit.setSuffix(" KB/s")
        self.tor_download_limit.setSpecialValueText("Unlimited")
        dl_layout.addRow(tr('Download Rate Limit:'), self.tor_download_limit)

        self.tor_upload_limit = QSpinBox()
        self.tor_upload_limit.setRange(0, 999999)
        self.tor_upload_limit.setSuffix(" KB/s")
        self.tor_upload_limit.setSpecialValueText("Unlimited")
        dl_layout.addRow(tr('Upload Rate Limit:'), self.tor_upload_limit)

        self.tor_listen_port = QSpinBox()
        self.tor_listen_port.setRange(0, 65535)
        self.tor_listen_port.setSpecialValueText("Random")
        dl_layout.addRow(tr('Listen Port (requires restart):'), self.tor_listen_port)

        self.tor_max_connections = QSpinBox()
        self.tor_max_connections.setRange(0, 10000)
        self.tor_max_connections.setSpecialValueText("Default")
        dl_layout.addRow(tr('Max Connections (requires restart):'), self.tor_max_connections)

        self.tor_restore_completed = QCheckBox(tr('Keep completed torrents in the list across restarts'))
        dl_layout.addRow("", self.tor_restore_completed)

        # --- Queue / concurrency ---
        queue_group = QGroupBox(tr('Torrent Queue'))
        queue_layout = QFormLayout(queue_group)

        self.tor_max_downloading = QSpinBox()
        self.tor_max_downloading.setRange(0, 1000)
        self.tor_max_downloading.setSpecialValueText("Unlimited")
        self.tor_max_downloading.setValue(5)
        queue_layout.addRow(tr('Max downloading at once:'), self.tor_max_downloading)

        self.tor_max_seeding = QSpinBox()
        self.tor_max_seeding.setRange(0, 1000)
        self.tor_max_seeding.setSpecialValueText("Unlimited")
        self.tor_max_seeding.setValue(5)
        queue_layout.addRow(tr('Max seeding at once:'), self.tor_max_seeding)

        self.tor_max_active = QSpinBox()
        self.tor_max_active.setRange(0, 1000)
        self.tor_max_active.setSpecialValueText("Unlimited")
        self.tor_max_active.setValue(10)
        queue_layout.addRow(tr('Max active torrents:'), self.tor_max_active)

        self.tor_max_queued = QSpinBox()
        self.tor_max_queued.setRange(0, 10000)
        self.tor_max_queued.setSpecialValueText("Unlimited")
        self.tor_max_queued.setValue(0)
        queue_layout.addRow(tr('Max queued torrents:'), self.tor_max_queued)

        self.tor_auto_manage_interval = QSpinBox()
        self.tor_auto_manage_interval.setRange(5, 3600)
        self.tor_auto_manage_interval.setSuffix(" s")
        self.tor_auto_manage_interval.setValue(30)
        queue_layout.addRow(tr('Auto-manage interval:'), self.tor_auto_manage_interval)

        self.tor_seed_ratio = QDoubleSpinBox()
        self.tor_seed_ratio.setRange(0.0, 100.0)
        self.tor_seed_ratio.setSingleStep(0.1)
        self.tor_seed_ratio.setSpecialValueText("Unlimited")
        self.tor_seed_ratio.setSuffix(" x")
        queue_layout.addRow(tr('Stop seeding at ratio:'), self.tor_seed_ratio)

        self.tor_seed_time = QSpinBox()
        self.tor_seed_time.setRange(0, 10080)
        self.tor_seed_time.setSingleStep(60)
        self.tor_seed_time.setSpecialValueText("Unlimited")
        self.tor_seed_time.setSuffix(" min")
        queue_layout.addRow(tr('Stop seeding after:'), self.tor_seed_time)

        self.tor_auto_save = QSpinBox()
        self.tor_auto_save.setRange(10, 3600)
        self.tor_auto_save.setSuffix(" s")
        self.tor_auto_save.setValue(60)
        queue_layout.addRow(tr('Auto-save state interval:'), self.tor_auto_save)

        layout.addWidget(dl_group)
        layout.addWidget(queue_group)
        dl_group.setVisible(self._page in ("all", "torrent"))
        queue_group.setVisible(self._page in ("all", "queue"))

        # Download Manager (IDM-style)
        dm_group = QGroupBox(tr('Download Manager'))
        dm_layout = QFormLayout(dm_group)

        self.dm_max_concurrent = QSpinBox()
        self.dm_max_concurrent.setRange(1, 20)
        self.dm_max_concurrent.setValue(3)
        dm_layout.addRow(tr('Max Concurrent Downloads:'), self.dm_max_concurrent)

        self.dm_max_connections = QSpinBox()
        self.dm_max_connections.setRange(1, 32)
        self.dm_max_connections.setValue(8)
        dm_layout.addRow(tr('Max Connections per Download:'), self.dm_max_connections)

        self.dm_default_folder = QLineEdit()
        self.dm_default_folder.setPlaceholderText(tr('C:\\Users\\...\\Downloads\\DeepFlux'))
        dm_layout.addRow(tr('Default Download Folder:'), self.dm_default_folder)

        dm_browse_btn = QPushButton(tr('Browse...'))
        dm_browse_btn.clicked.connect(self._browse_dm_folder)
        dm_layout.addRow("", dm_browse_btn)

        self.dm_bandwidth = QSpinBox()
        self.dm_bandwidth.setRange(0, 999999)
        self.dm_bandwidth.setSuffix(" KB/s")
        self.dm_bandwidth.setSpecialValueText("Unlimited")
        self.dm_bandwidth.setValue(0)
        dm_layout.addRow(tr('Bandwidth Limit:'), self.dm_bandwidth)

        self.dm_auto_start = QCheckBox(tr('Automatically start downloads when added'))
        dm_layout.addRow("", self.dm_auto_start)

        self.dm_segment_threshold = QSpinBox()
        self.dm_segment_threshold.setRange(1, 1000)
        self.dm_segment_threshold.setSuffix(" MB")
        self.dm_segment_threshold.setValue(1)
        dm_layout.addRow(tr('Segment Threshold (files smaller than this use single-stream):'), self.dm_segment_threshold)

        self.dm_stream_height = QSpinBox()
        self.dm_stream_height.setRange(0, 4320)
        self.dm_stream_height.setSpecialValueText("Highest available")
        self.dm_stream_height.setSuffix("p")
        dm_layout.addRow(tr('HLS Maximum Height:'), self.dm_stream_height)

        self.dm_youtube_height = QSpinBox()
        self.dm_youtube_height.setRange(144, 4320)
        self.dm_youtube_height.setSuffix("p")
        dm_layout.addRow(tr('YouTube Maximum Height:'), self.dm_youtube_height)

        self.dm_youtube_subtitles = QCheckBox(tr('Download and embed available subtitles'))
        dm_layout.addRow("", self.dm_youtube_subtitles)

        self.dm_youtube_playlists = QCheckBox(tr('Allow complete YouTube playlists'))
        dm_layout.addRow("", self.dm_youtube_playlists)

        self.dm_youtube_update_check = QCheckBox(tr('Warn when the YouTube downloader (yt-dlp) is outdated'))
        dm_layout.addRow("", self.dm_youtube_update_check)

        self.dm_update_check = QCheckBox(
            tr('Check for DeepFlux updates automatically (Windows installs)'))
        dm_layout.addRow("", self.dm_update_check)

        self.dm_update_hint = QLabel(
            "When a new version is out the app offers to download and install it "
            "itself — nothing is ever installed without your click, and your "
            "sources, API keys and settings are kept.")
        self.dm_update_hint.setObjectName("hint")
        self.dm_update_hint.setWordWrap(True)
        dm_layout.addRow("", self.dm_update_hint)

        self.dm_usage_ping = QCheckBox(
            tr('Send an anonymous usage ping (powers the live user count on deepflux.space)'))
        dm_layout.addRow("", self.dm_usage_ping)

        dm_ping_hint = QLabel(
            "The ping carries a random install id, the app version and the OS — nothing else.\n"
            "It is sent every few minutes while the app runs; untick to disable it entirely.")
        dm_ping_hint.setObjectName("hint")
        dm_ping_hint.setWordWrap(True)
        dm_layout.addRow("", dm_ping_hint)

        self.dm_categories = QPlainTextEdit()
        self.dm_categories.setMaximumHeight(90)
        self.dm_categories.setPlaceholderText(tr('Videos|D:\\Media\\Videos|mp4,mkv,webm'))
        dm_layout.addRow(tr('Categories (Name|Folder|extensions):'), self.dm_categories)

        dm_hint = QLabel(tr("The download manager uses segmented downloading with dynamic rebalancing\n"
                            "for maximum speed, similar to Internet Download Manager."))
        dm_hint.setObjectName("hint")
        dm_hint.setWordWrap(True)
        dm_layout.addRow("", dm_hint)

        layout.addWidget(dm_group)
        dm_group.setVisible(self._page in ("all", "manager"))
        layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

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
        self.dm_stream_height.setValue(self.config.download.stream_max_height)
        self.dm_youtube_height.setValue(self.config.download.youtube_max_height)
        self.dm_youtube_subtitles.setChecked(self.config.download.youtube_subtitles)
        self.dm_youtube_playlists.setChecked(self.config.download.youtube_playlists)
        self.dm_youtube_update_check.setChecked(self.config.download.youtube_update_check)
        self.dm_update_check.setChecked(self.config.updater.check_enabled)
        self.dm_usage_ping.setChecked(self.config.stats.ping_enabled)
        self.dm_categories.setPlainText("\n".join(
            f"{category.name}|{category.folder}|{','.join(category.extensions)}"
            for category in self.config.download.categories
        ))

    def _browse_save_path(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Default Save Path")
        if path:
            self.save_path.setText(path)

    def _browse_dm_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Default Download Folder")
        if path:
            self.dm_default_folder.setText(path)

    def _save_and_accept(self) -> None:
        if self._page in ("all", "manager"):
            categories = []
            for line in self.dm_categories.toPlainText().splitlines():
                if not line.strip():
                    continue
                parts = [part.strip() for part in line.split("|", 2)]
                if len(parts) != 3 or not parts[0] or not parts[1]:
                    QMessageBox.warning(self, tr('Download Categories'), f"Invalid category line: {line}")
                    return
                extensions = [value.strip().lower().lstrip(".") for value in parts[2].split(",") if value.strip()]
                categories.append(DownloadCategory(name=parts[0], folder=parts[1], extensions=extensions))
        if self._page in ("all", "torrent"):
            self.config.default_save_path = self.save_path.text().strip() or self.config.default_save_path
            self.config.torrents.download_rate_limit_kb = self.tor_download_limit.value()
            self.config.torrents.upload_rate_limit_kb = self.tor_upload_limit.value()
            self.config.torrents.listen_port = self.tor_listen_port.value()
            self.config.torrents.max_connections = self.tor_max_connections.value()
            self.config.torrents.restore_completed = self.tor_restore_completed.isChecked()
        if self._page in ("all", "queue"):
            self.config.torrents.max_downloading_torrents = self.tor_max_downloading.value()
            self.config.torrents.max_seeding_torrents = self.tor_max_seeding.value()
            self.config.torrents.max_active_torrents = self.tor_max_active.value()
            self.config.torrents.max_queued_torrents = self.tor_max_queued.value()
            self.config.torrents.auto_manage_interval_seconds = self.tor_auto_manage_interval.value()
            self.config.torrents.seed_ratio_limit = self.tor_seed_ratio.value()
            self.config.torrents.seed_time_limit_minutes = self.tor_seed_time.value()
            self.config.torrents.auto_save_state_seconds = self.tor_auto_save.value()
        if self._page in ("all", "manager"):
            self.config.download.max_concurrent = self.dm_max_concurrent.value()
            self.config.download.max_connections_per_download = self.dm_max_connections.value()
            self.config.download.default_folder = self.dm_default_folder.text().strip() or self.config.download.default_folder
            self.config.download.bandwidth_limit_bps = self.dm_bandwidth.value() * 1024
            self.config.download.auto_start = self.dm_auto_start.isChecked()
            self.config.download.segment_threshold_mb = self.dm_segment_threshold.value()
            self.config.download.stream_max_height = self.dm_stream_height.value()
            self.config.download.youtube_max_height = self.dm_youtube_height.value()
            self.config.download.youtube_subtitles = self.dm_youtube_subtitles.isChecked()
            self.config.download.youtube_playlists = self.dm_youtube_playlists.isChecked()
            self.config.download.youtube_update_check = self.dm_youtube_update_check.isChecked()
            self.config.download.categories = categories
            # Applies on the next launch — the heartbeat thread reads the
            # flag once at startup (stopping mid-session would flicker the
            # online count for a setting the user rarely touches).
            self.config.stats.ping_enabled = self.dm_usage_ping.isChecked()
            # Same for the update probe: it reads the flag at startup.
            self.config.updater.check_enabled = self.dm_update_check.isChecked()
        self.accept()


# ---------------------------------------------------------------------------
# Browser Settings — Homepage
# ---------------------------------------------------------------------------

BROWSER_SETTINGS_PAGES = [
    ("Browser General Settings…", "general"),
    ("Browser Privacy && Data…", "privacy"),
]


class BrowserSettingsDialog(QDialog):
    """Settings for the built-in browser."""

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None,
                 page: str = "all") -> None:
        super().__init__(parent)
        self.config = config
        self._page = page
        titles = {
            "general": "Browser General Settings",
            "privacy": "Browser Privacy & Data",
        }
        self.setWindowTitle(titles.get(page, "Browser Settings"))
        self.setMinimumWidth(440)
        roomy(self)
        self.setStyleSheet(_SHARED_STYLE)
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(body)

        general_group = QGroupBox(tr('General'))
        general_layout = QFormLayout(general_group)

        self.browser_homepage = QLineEdit()
        self.browser_homepage.setPlaceholderText("Default: https://deepflux.space/")
        general_layout.addRow(tr('Homepage:'), self.browser_homepage)

        self.browser_search_engine = QComboBox()
        for engine in BROWSER_SEARCH_ENGINES:
            self.browser_search_engine.addItem(engine.title(), engine)
        general_layout.addRow(tr('Search engine:'), self.browser_search_engine)

        hint = QLabel("The page loaded when the Browse tab opens or the Home button is clicked. Default: https://deepflux.space/ — leave empty to reset to the default.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        general_layout.addRow("", hint)

        self.browser_adblock_check = QCheckBox(tr('Enable ad blocking (blocks ads, trackers, and analytics)'))
        general_layout.addRow("", self.browser_adblock_check)

        adblock_hint = QLabel(tr('Requests to known ad/tracker domains are blocked before they load. Enabled by default; right-click the toolbar button to disable it for the current site.'))
        adblock_hint.setObjectName("hint")
        adblock_hint.setWordWrap(True)
        general_layout.addRow("", adblock_hint)

        self.browser_extension_check = QCheckBox(tr('Enable video grabber (detects downloadable videos on web pages)'))
        general_layout.addRow("", self.browser_extension_check)

        extension_hint = QLabel(tr('When enabled, a script scans every page for downloadable videos (<video>, HLS, DASH, YouTube, etc.) and shows a status badge. Its monitoring can cause flashing during video playback — turn off if you experience that. Off by default. You can also toggle this from the toolbar button next to AdBlock.'))
        extension_hint.setObjectName("hint")
        extension_hint.setWordWrap(True)
        general_layout.addRow("", extension_hint)

        self.browser_restore_tabs = QCheckBox(tr('Restore open tabs on startup'))
        general_layout.addRow("", self.browser_restore_tabs)

        self.import_bookmarks_btn = QPushButton(tr('Import Bookmarks from Other Browsers...'))
        self.import_bookmarks_btn.setObjectName("btn_secondary")
        self.import_bookmarks_btn.clicked.connect(self._on_import_bookmarks)
        general_layout.addRow("", self.import_bookmarks_btn)

        privacy_group = QGroupBox(tr('Privacy & Data'))
        privacy_layout = QFormLayout(privacy_group)
        self.browser_history_enabled = QCheckBox(tr('Keep local browsing history for address suggestions'))
        privacy_layout.addRow("", self.browser_history_enabled)
        self.browser_history_days = QSpinBox()
        self.browser_history_days.setRange(1, 3650)
        self.browser_history_days.setSuffix(" days")
        privacy_layout.addRow(tr('History retention:'), self.browser_history_days)

        self.clear_history_btn = QPushButton(tr('Clear Browsing History'))
        self.clear_history_btn.setObjectName("btn_secondary")
        self.clear_history_btn.clicked.connect(self._clear_history)
        privacy_layout.addRow("", self.clear_history_btn)
        self.clear_browser_data_btn = QPushButton(tr('Clear Cookies and Cache'))
        self.clear_browser_data_btn.setObjectName("btn_secondary")
        self.clear_browser_data_btn.clicked.connect(self._clear_browser_data)
        privacy_layout.addRow("", self.clear_browser_data_btn)
        self.clear_agent_permissions_btn = QPushButton(tr('Clear Agent Page Permissions'))
        self.clear_agent_permissions_btn.setObjectName("btn_secondary")
        self.clear_agent_permissions_btn.clicked.connect(self._clear_agent_permissions)
        privacy_layout.addRow("", self.clear_agent_permissions_btn)

        layout.addWidget(general_group)
        layout.addWidget(privacy_group)
        general_group.setVisible(self._page in ("all", "general"))
        privacy_group.setVisible(self._page in ("all", "privacy"))
        layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _load_values(self) -> None:
        self.browser_homepage.setText(self.config.browser.homepage)
        index = self.browser_search_engine.findData(self.config.browser.search_engine)
        self.browser_search_engine.setCurrentIndex(max(index, 0))
        self.browser_adblock_check.setChecked(self.config.browser.adblock_enabled)
        self.browser_extension_check.setChecked(self.config.browser.extension_enabled)
        self.browser_restore_tabs.setChecked(self.config.browser.restore_tabs)
        self.browser_history_enabled.setChecked(self.config.browser.history_enabled)
        self.browser_history_days.setValue(self.config.browser.history_retention_days)

    def _save_and_accept(self) -> None:
        if self._page in ("all", "general"):
            homepage = self.browser_homepage.text().strip()
            if homepage:
                from urllib.parse import urlsplit
                try:
                    parsed = urlsplit(homepage)
                    valid_homepage = parsed.scheme in ("http", "https") and bool(parsed.hostname)
                except ValueError:
                    valid_homepage = False
                if not valid_homepage:
                    QMessageBox.warning(self, tr('Browser Settings'), "Homepage must be an http:// or https:// URL.")
                    return
            self.config.browser.homepage = homepage
            self.config.browser.search_engine = self.browser_search_engine.currentData() or "google"
            self.config.browser.adblock_enabled = self.browser_adblock_check.isChecked()
            self.config.browser.extension_enabled = self.browser_extension_check.isChecked()
            self.config.browser.restore_tabs = self.browser_restore_tabs.isChecked()
        if self._page in ("all", "privacy"):
            self.config.browser.history_enabled = self.browser_history_enabled.isChecked()
            self.config.browser.history_retention_days = self.browser_history_days.value()
        self.accept()

    def _clear_history(self) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "_clear_browser_history"):
            parent._clear_browser_history()

    def _clear_browser_data(self) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "_clear_browser_data"):
            parent._clear_browser_data()

    def _clear_agent_permissions(self) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "_clear_browser_agent_permissions"):
            parent._clear_browser_agent_permissions()

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


API_KEY_PAGES = [
    ("AI Agent Key…", "ai"),
    ("Search Keys (Jackett, Brave, Perplexity)…", "search"),
    ("Movie && TV Metadata Keys (TMDb, OMDb)…", "media"),
    ("Adult Metadata Keys (TPDB, StashDB)…", "adult"),
    ("OpenSubtitles Key…", "subtitles"),
]


class APIKeysDialog(QDialog):
    """Unified API keys window — every external service key in one place."""

    # Worker-thread → GUI-thread bridge for the post-test Jackett sources
    # fetch. Carries List[SourceConfig] on success, the Exception on failure.
    jkt_fetch_done = Signal(object)

    # Local-model bridges: hardware probe result, download progress
    # (model_id, done_bytes, total_bytes), download completion
    # (model_id, result-dict-or-Exception), and off-thread UI actions
    # (action name, payload) for Remove/Test.
    local_hw_done = Signal(object)
    local_dl_progress = Signal(str, int, int)
    local_dl_done = Signal(str, object)
    local_action_done = Signal(str, object)
    hf_search_done = Signal(str, object)
    hf_files_done = Signal(str, object)
    server_state_changed = Signal(object)

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None,
                 page: str = "all") -> None:
        super().__init__(parent)
        self.config = config
        self._page = page
        titles = {
            "ai": "AI Agent API",
            "search": "Torrent & Web Search APIs",
            "media": "Movie & TV Metadata APIs",
            "adult": "Adult Metadata APIs",
            "subtitles": "Subtitle API",
        }
        self.setWindowTitle(titles.get(page, "API Keys"))
        self.setMinimumWidth(480)
        roomy(self)
        self.setStyleSheet(_SHARED_STYLE)
        self.jkt_fetch_done.connect(self._on_jkt_fetch_done)
        self.local_hw_done.connect(self._on_local_hw)
        self.local_dl_progress.connect(self._on_local_dl_progress)
        self.local_dl_done.connect(self._on_local_dl_done)
        self.local_action_done.connect(self._on_local_action_done)
        self.hf_search_done.connect(self._on_hf_search_done)
        self.hf_files_done.connect(self._on_hf_files_done)
        self.server_state_changed.connect(self._on_server_state)
        self._local_cancel = threading.Event()
        self._local_hw: Optional[object] = None
        # Staged local-provider switch (applied only on OK — Cancel must not
        # leave the live config pointing at a provider the user rejected).
        self._pending_local_entry: Optional[dict] = None
        self._build_ui()
        self._load_values()
        if self._page in ("all", "ai") and sys.platform == "win32":
            self._start_local_hw_probe()
            self._refresh_server_state()
            # Periodic refresh keeps the on/off control honest while the
            # dialog stays open (the server may come up or go down behind
            # the user's back via the app's lazy auto-start).
            self._server_poll = QTimer(self)
            self._server_poll.setInterval(5000)
            self._server_poll.timeout.connect(self._refresh_server_state)
            self._server_poll.start()

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
        llm_group = QGroupBox(tr('AI Agent (LLM)'))
        fl = QFormLayout(llm_group)
        self.llm_endpoint = QComboBox()
        for pid, preset in LLM_PROVIDER_PRESETS.items():
            # The bundled llama.cpp runtime ships in the Windows setup only;
            # off-win32 the local entry must not be selectable (the preset
            # stays in config.py so saved configs from Windows installs —
            # and the shared tests — keep loading unchanged).
            if pid == "local" and sys.platform != "win32":
                continue
            self.llm_endpoint.addItem(preset["label"], pid)
        self.llm_endpoint.currentIndexChanged.connect(self._on_endpoint_changed)
        fl.addRow(tr('API endpoint:'), self.llm_endpoint)
        self.llm_base_url = QLineEdit()
        self.llm_base_url.setToolTip(tr(
            'Fixed for preset providers — choose "Custom" to edit'))
        fl.addRow(tr('Base URL:'), self.llm_base_url)
        self.llm_model = QLineEdit()
        fl.addRow(tr('Model:'), self.llm_model)
        self.llm_reasoning_effort = QCheckBox(tr('Send reasoning_effort to custom endpoint'))
        fl.addRow("", self.llm_reasoning_effort)
        self.llm_key = QLineEdit()
        self.llm_key.setEchoMode(QLineEdit.Password)
        self.llm_key.setPlaceholderText(tr('sk-...'))
        # Reveal toggle — masked by default (key hygiene), one click to read
        # back what was typed. Only THIS dialog's AI key gets it: it is the
        # one users paste long tokens into and then ask "did it save?".
        from PySide6.QtGui import QAction
        self._key_visible = False
        self._key_reveal_action = QAction(tr('👁 Show'))
        self._key_reveal_action.setToolTip(tr('Show or hide the key'))
        def _toggle_reveal():
            self._key_visible = not self._key_visible
            self.llm_key.setEchoMode(
                QLineEdit.Normal if self._key_visible else QLineEdit.Password)
            self._key_reveal_action.setText(
                tr('👁 Hide') if self._key_visible else tr('👁 Show'))
        self._key_reveal_action.triggered.connect(_toggle_reveal)
        self.llm_key.addAction(self._key_reveal_action, QLineEdit.TrailingPosition)
        # The pre-filled field often carries the injected SHARED key; only a
        # real textEdited event means the user typed one (drives the
        # auto-switch gate in _local_auto_switch_ok).
        self._key_typed = False
        self.llm_key.textEdited.connect(lambda _t: setattr(self, "_key_typed", True))
        self._llm_key_hint = _api_hint(
            "DeepSeek or OpenRouter key — powers the conversational agent. "
            "Get one at https://platform.deepseek.com/api_keys (DeepSeek) or "
            "https://openrouter.ai/keys (OpenRouter). Custom OpenAI-compatible "
            "endpoints may leave the key blank when their server allows it.")
        fl.addRow(tr('API Key:'), self.llm_key)
        fl.addRow("", self._llm_key_hint)
        self._llm_key_label = fl.labelForField(self.llm_key)
        self._llm_base_url_label = fl.labelForField(self.llm_base_url)
        layout.addWidget(llm_group)

        # --- Local models (bundled llama.cpp, no key) — Windows only -------
        # The uncensored GGUF ladder managed by infra/local_model.py. Downloads
        # are resumable and sha256-verified; each row carries a FIT BADGE
        # computed from this machine's GPU/RAM (fit matrix ported from the
        # ModelSwarm project) so the user can see what their hardware will
        # actually do with the model BEFORE downloading gigabytes. On top of
        # the ladder, users can download ANY GGUF from Hugging Face (search
        # section below) — those land in the same rows, backed by the custom
        # sidecar (models/custom_models.json).
        self._local_rows: dict = {}
        self._local_entries: dict = {}        # model_id -> entry (ladder + custom)
        self._custom_rows: set = set()
        self._custom_dl_entries: dict = {}    # in-flight custom downloads
        self._hf_mode = "search"
        self._hf_repo = ""
        self._hf_search_snapshot: list = []
        self._server_state: dict = {}
        self._server_transitioning = False
        from infra import local_model
        self._local_manifest = [] if sys.platform != "win32" else local_model.load_manifest()
        self._local_tiers: dict = {}          # model_id -> fit tier (or None)
        local_group = None
        if sys.platform == "win32":
            local_group = QGroupBox(tr('Local Models (built-in AI — uncensored, offline)'))
            vl = QVBoxLayout(local_group)
            vl.setSpacing(6)
            self.local_hw_label = QLabel(tr('Detecting hardware…'))
            self.local_hw_label.setObjectName("hint")
            self.local_hw_label.setWordWrap(True)
            self.local_reco_label = QLabel("")
            self.local_reco_label.setWordWrap(True)
            vl.addWidget(self.local_hw_label)
            vl.addWidget(self.local_reco_label)
            # Model rows live in their own container so user-downloaded
            # custom rows can be inserted among them at runtime.
            self._local_rows_box = QWidget()
            self._local_rows_layout = QVBoxLayout(self._local_rows_box)
            self._local_rows_layout.setContentsMargins(0, 0, 0, 0)
            self._local_rows_layout.setSpacing(4)
            vl.addWidget(self._local_rows_box)
            for entry in self._local_manifest:
                self._local_entries[entry["id"]] = entry
                self._add_local_row(entry)
            self._sync_local_rows()  # rows for previously downloaded custom models
            self.local_progress = QProgressBar()
            self.local_progress.setTextVisible(False)
            self.local_progress.setVisible(False)
            self.local_progress_lbl = QLabel("")
            self.local_progress_lbl.setObjectName("hint")
            self.local_cancel_btn = QPushButton(tr('Cancel download'))
            self.local_cancel_btn.setVisible(False)
            self.local_cancel_btn.clicked.connect(self._local_cancel_download)
            prow = QHBoxLayout()
            prow.addWidget(self.local_progress, 1)
            prow.addWidget(self.local_progress_lbl)
            prow.addWidget(self.local_cancel_btn)
            pwrap = QWidget()
            pwrap.setLayout(prow)
            vl.addWidget(pwrap)
            test_row = QHBoxLayout()
            self.local_test_btn = QPushButton(tr('Test local server'))
            self.local_test_btn.clicked.connect(self._local_test)
            self.local_test_result = QLabel("")
            self.local_test_result.setObjectName("hint")
            self.local_test_result.setWordWrap(True)
            test_row.addWidget(self.local_test_btn)
            test_row.addWidget(self.local_test_result, 1)
            twrap = QWidget()
            twrap.setLayout(test_row)
            vl.addWidget(twrap)
            # --- Local server on/off + loading progress ----------------------
            srv_row = QHBoxLayout()
            self.server_toggle_btn = QPushButton(tr('Start server'))
            self.server_toggle_btn.clicked.connect(self._local_server_toggle)
            self.server_state_lbl = QLabel(tr('Checking server…'))
            self.server_state_lbl.setObjectName("hint")
            self.server_state_lbl.setWordWrap(True)
            srv_row.addWidget(self.server_toggle_btn)
            srv_row.addWidget(self.server_state_lbl, 1)
            srv_wrap = QWidget()
            srv_wrap.setLayout(srv_row)
            vl.addWidget(srv_wrap)
            # Indeterminate: b11727's /health reports no load percentage
            # (VERIFIED live — 503 "Loading model" only), so the bar animates
            # and the label carries the phase + elapsed seconds.
            self.server_progress = QProgressBar()
            self.server_progress.setTextVisible(False)
            self.server_progress.setRange(0, 0)
            self.server_progress.setVisible(False)
            vl.addWidget(self.server_progress)
            # --- Custom model from Hugging Face -----------------------------
            hf_box = QWidget()
            hf_vl = QVBoxLayout(hf_box)
            hf_vl.setContentsMargins(0, 0, 0, 0)
            hf_vl.setSpacing(4)
            hf_title = QLabel(tr('Custom model from Hugging Face'))
            hf_title.setStyleSheet("font-weight: 600;")
            hf_vl.addWidget(hf_title)
            hf_top = QHBoxLayout()
            self.hf_search_edit = QLineEdit()
            self.hf_search_edit.setPlaceholderText(
                tr('Search any GGUF model, e.g. "llama 3 8b uncensored"'))
            self.hf_search_edit.returnPressed.connect(self._hf_search)
            self.hf_search_btn = QPushButton(tr('Search'))
            self.hf_search_btn.clicked.connect(self._hf_search)
            hf_top.addWidget(self.hf_search_edit, 1)
            hf_top.addWidget(self.hf_search_btn)
            hf_vl.addLayout(hf_top)
            hf_nav = QHBoxLayout()
            self.hf_back_btn = QPushButton(tr('← Back to results'))
            self.hf_back_btn.setVisible(False)
            self.hf_back_btn.clicked.connect(self._hf_back)
            self.hf_download_btn = QPushButton(tr('Download selected model'))
            self.hf_download_btn.setEnabled(False)
            self.hf_download_btn.clicked.connect(self._hf_download)
            hf_nav.addWidget(self.hf_back_btn)
            hf_nav.addStretch(1)
            hf_nav.addWidget(self.hf_download_btn)
            hf_vl.addLayout(hf_nav)
            self.hf_list = QListWidget()
            self.hf_list.setMaximumHeight(150)
            self.hf_list.itemDoubleClicked.connect(self._hf_open_selected)
            self.hf_list.currentItemChanged.connect(self._hf_on_selection)
            hf_vl.addWidget(self.hf_list)
            self.hf_status = QLabel(tr('Search a repository, double-click it to '
                                       'pick a model file, then download and use it.'))
            self.hf_status.setObjectName("hint")
            self.hf_status.setWordWrap(True)
            hf_vl.addWidget(self.hf_status)
            vl.addWidget(hf_box)
            vl.addWidget(_api_hint(
                "Local models run entirely on this PC through the bundled llama.cpp "
                "server (no API key, nothing sent to the cloud). The badge on each "
                "row says how this machine would run it: GPU = full video-memory "
                "offload (fastest), GPU+CPU = split, CPU = processor only. "
                "Downloading installs the model only — the built-in DeepSeek key "
                "stays the default agent until you switch here explicitly. "
                "Interrupted downloads resume where they stopped. The Hugging Face "
                "search below installs ANY public GGUF model the same way."))
            self._refresh_local_rows()
            layout.addWidget(local_group)

        # --- Jackett ---
        jkt_group = QGroupBox(tr('Jackett (Torrent Search)'))
        fl = QFormLayout(jkt_group)
        self.jkt_key = QLineEdit()
        self.jkt_key.setEchoMode(QLineEdit.Password)
        self.jkt_key.setPlaceholderText(tr('API key from Jackett dashboard'))
        fl.addRow(tr('API Key:'), self.jkt_key)
        self.jkt_test_btn = QPushButton(tr('Test Connection'))
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
        ws_group = QGroupBox(tr('Web Search'))
        fl = QFormLayout(ws_group)
        self.brave_key = QLineEdit()
        self.brave_key.setEchoMode(QLineEdit.Password)
        self.brave_key.setPlaceholderText(tr('BSA...'))
        fl.addRow(tr('Brave API Key:'), self.brave_key)
        self.pplx_key = QLineEdit()
        self.pplx_key.setEchoMode(QLineEdit.Password)
        self.pplx_key.setPlaceholderText(tr('pplx-...'))
        fl.addRow(tr('Perplexity API Key:'), self.pplx_key)
        fl.addRow("", _api_hint(
            "DuckDuckGo always works without a key. Brave adds an independent "
            "index (free tier: 1 query/sec). Perplexity adds synthesized answers. "
            "Both are optional — each is only used when its key is set."))
        layout.addWidget(ws_group)

        # --- TMDb ---
        tmdb_group = QGroupBox(tr('TMDb (Metadata & Artwork)'))
        fl = QFormLayout(tmdb_group)
        self.tmdb_key = QLineEdit()
        self.tmdb_key.setEchoMode(QLineEdit.Password)
        self.tmdb_key.setPlaceholderText(tr('TMDb API key (optional)'))
        fl.addRow(tr('API Key:'), self.tmdb_key)
        fl.addRow("", _api_hint(
            "Optional — enter your own key from themoviedb.org/settings/api "
            "for TMDb posters/metadata. Left blank, TVmaze remains the keyless "
            "fallback for series."))
        layout.addWidget(tmdb_group)

        # --- OMDb ---
        omdb_group = QGroupBox(tr('OMDb (Movie/Series Fallback)'))
        fl = QFormLayout(omdb_group)
        self.omdb_key = QLineEdit()
        self.omdb_key.setEchoMode(QLineEdit.Password)
        self.omdb_key.setPlaceholderText(tr('Free key from omdbapi.com/apikey.aspx (optional)'))
        fl.addRow(tr('API Key:'), self.omdb_key)
        fl.addRow("", _api_hint(
            "Optional — free key from omdbapi.com. Fallback when TMDb "
            "misses or has no key; covers older/obscure titles."))
        layout.addWidget(omdb_group)

        # --- Fanart.tv ---
        fanart_group = QGroupBox(tr('Fanart.tv (Backdrop Supplement)'))
        fl = QFormLayout(fanart_group)
        self.fanarttv_key = QLineEdit()
        self.fanarttv_key.setEchoMode(QLineEdit.Password)
        self.fanarttv_key.setPlaceholderText(tr('Free key from fanart.tv (optional)'))
        fl.addRow(tr('API Key:'), self.fanarttv_key)
        fl.addRow("", _api_hint(
            "Optional — free personal key from fanart.tv/get-an-api-key. "
            "Enriches backdrops with community fan art after a poster is found."))
        layout.addWidget(fanart_group)

        # --- ThePornDB ---
        tpdb_group = QGroupBox(tr('ThePornDB (Adult VOD Metadata & Artwork)'))
        fl = QFormLayout(tpdb_group)
        self.tpdb_key = QLineEdit()
        self.tpdb_key.setEchoMode(QLineEdit.Password)
        self.tpdb_key.setPlaceholderText(tr('API token from theporndb.net (optional)'))
        fl.addRow(tr('API Token:'), self.tpdb_key)
        fl.addRow("", _api_hint(
            "Optional — TMDb filters adult titles out of its search results, "
            "so adult VOD entries stay artwork-less without this. Only used "
            "for entries whose group-title marks them as adult."))
        layout.addWidget(tpdb_group)

        # --- StashDB ---
        stash_group = QGroupBox(tr('StashDB (Adult VOD — Second Source)'))
        fl = QFormLayout(stash_group)
        self.stashdb_key = QLineEdit()
        self.stashdb_key.setEchoMode(QLineEdit.Password)
        self.stashdb_key.setPlaceholderText(tr('API key from stashdb.org (optional)'))
        fl.addRow(tr('API Key:'), self.stashdb_key)
        fl.addRow("", _api_hint(
            "Optional — community-driven adult metadata DB. Complements "
            "ThePornDB for western web scenes; tried when TPDB misses. "
            "Register at stashdb.org for a free key."))
        layout.addWidget(stash_group)

        # --- OpenSubtitles ---
        ost_group = QGroupBox(tr('OpenSubtitles (Subtitle Downloads)'))
        fl = QFormLayout(ost_group)
        self.ost_key = QLineEdit()
        self.ost_key.setEchoMode(QLineEdit.Password)
        self.ost_key.setPlaceholderText(tr('API key from opensubtitles.com'))
        fl.addRow(tr('API Key:'), self.ost_key)
        self.ost_user = QLineEdit()
        fl.addRow(tr('Username (optional):'), self.ost_user)
        self.ost_pass = QLineEdit()
        self.ost_pass.setEchoMode(QLineEdit.Password)
        fl.addRow(tr('Password (optional):'), self.ost_pass)
        fl.addRow("", _api_hint(
            "Used by the player's CC menu → \"Find subtitles online…\". "
            "Account credentials are optional and raise the daily download quota."))
        layout.addWidget(ost_group)

        sections = {
            "ai": (llm_group, local_group),
            "search": (jkt_group, ws_group),
            "media": (tmdb_group, omdb_group, fanart_group),
            "adult": (tpdb_group, stash_group),
            "subtitles": (ost_group,),
        }
        visible = sections.get(self._page)
        if visible is not None:
            for group in (llm_group, local_group, jkt_group, ws_group, tmdb_group,
                          omdb_group, fanart_group, tpdb_group, stash_group,
                          ost_group):
                if group is not None:
                    group.setVisible(group in visible)

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
        provider = self._selected_endpoint()
        preset = LLM_PROVIDER_PRESETS[provider]
        self.llm_base_url.setPlaceholderText(preset["base_url"] or "https://provider.example/v1")
        preset_urls = {p["base_url"] for p in LLM_PROVIDER_PRESETS.values()}
        if self.llm_base_url.text().strip() in preset_urls:
            self.llm_base_url.setText(preset["base_url"])
        custom = provider == "custom"
        # Base URL is only meaningful for custom endpoints — presets (and
        # above all the local loopback server) have a fixed one. Read-only
        # instead of disabled so it stays selectable/copyable.
        self.llm_base_url.setReadOnly(not custom)
        self.llm_model.setEnabled(custom)
        self.llm_reasoning_effort.setEnabled(custom)
        models = preset.get("models") or []
        self.llm_model.setPlaceholderText(models[0] if models else "provider/model-name")
        # Local AI is KEYLESS: the key row (label + field + hint) hides so
        # the section stops asking for an sk-... that must stay empty.
        local = provider == "local"
        for widget in (self.llm_key, self._llm_key_label, self._llm_key_hint):
            widget.setVisible(not local)

    def _local_fit_badge(self, tier) -> tuple:
        """(text, color) for a fit tier — the at-a-glance 'how will my PC
        run this' marker on each model row."""
        if tier == "gpu_full":
            return tr("GPU"), "#3fb950"
        if tier == "gpu_partial":
            return tr("GPU+CPU"), "#2a7abf"
        if tier == "ram_only":
            return tr("CPU"), "#8a9ab0"
        if tier is None:
            return tr("won't fit"), "#f85149"
        return "", ""

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
                tr('Enter the Jackett API key above first (the URL is set in Download → Jackett Settings).'))
            self.jkt_test_result.setStyleSheet("color: #ffcc00;")
            return

        self.jkt_test_result.setText(tr('Testing connection...'))
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
            self.jkt_test_result.setText(tr('Connection successful! Fetching indexer list…'))
            self.jkt_test_result.setStyleSheet("color: #a8edff;")
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
            self.jkt_test_result.setText(tr("Got a response but it doesn't look like Torznab XML. Check the URL/path."))
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
            self.jkt_test_result.setText(tr('Connection OK, but Jackett has no configured indexers to import.'))
            self.jkt_test_result.setStyleSheet("color: #ffcc00;")
            return
        self.config.sources.sources = jackett.merge_sources(self.config.sources.sources, outcome)
        self.config.sources.last_jackett_fetch = time.time()
        enabled = sum(1 for s in self.config.sources.sources if s.enabled)
        self.jkt_test_result.setText(
            f"Connection successful! Synced {len(outcome)} indexer(s) from Jackett ({enabled} enabled)."
        )
        self.jkt_test_result.setStyleSheet("color: #a8edff;")

    # -- Local models (bundled llama.cpp) ------------------------------------

    def _add_local_row(self, entry: dict) -> None:
        """One model row (label / fit badge / status / Use / Download /
        Remove) inside the rows container."""
        row = QHBoxLayout()
        row.setSpacing(6)
        name = QLabel(entry["label"])
        name.setToolTip(entry.get("repo", entry["id"]))
        fit = QLabel("")
        fit.setToolTip(tr('How this PC would run it (from the GPU/RAM scan)'))
        status = QLabel("")
        status.setObjectName("hint")
        use_btn = QPushButton(tr('Use'))
        use_btn.setToolTip(tr('Make this the agent\'s model (applies on OK)'))
        use_btn.clicked.connect(
            lambda _=False, e=entry: self._local_use(e))
        dl_btn = QPushButton(tr('Download'))
        rm_btn = QPushButton(tr('Remove'))
        dl_btn.clicked.connect(lambda _=False, e=entry: self._local_download(e))
        rm_btn.clicked.connect(lambda _=False, e=entry: self._local_remove(e))
        row.addWidget(name, 1)
        row.addWidget(fit)
        row.addWidget(status)
        row.addWidget(use_btn)
        row.addWidget(dl_btn)
        row.addWidget(rm_btn)
        wrap = QWidget()
        wrap.setLayout(row)
        self._local_rows_layout.addWidget(wrap)
        self._local_rows[entry["id"]] = (wrap, name, fit, status, use_btn,
                                         dl_btn, rm_btn)

    def _sync_local_rows(self) -> None:
        """Rows for user-downloaded custom models (sidecar beside the curated
        ladder) — call after the sidecar changes so new models appear."""
        from infra import local_model
        if sys.platform != "win32":
            return
        hw = getattr(self, "_local_hw", None)
        for entry in local_model.custom_models():
            self._local_entries[entry["id"]] = entry
            if hw is not None and entry["id"] not in self._local_tiers:
                self._local_tiers[entry["id"]] = local_model.fit_tier(hw, entry)
            if entry["id"] not in self._local_rows:
                self._add_local_row(entry)
                self._custom_rows.add(entry["id"])

    def _remove_local_row(self, model_id: str) -> None:
        widgets = self._local_rows.pop(model_id, None)
        self._custom_rows.discard(model_id)
        self._local_entries.pop(model_id, None)
        if widgets:
            widgets[0].deleteLater()

    def _start_local_hw_probe(self) -> None:
        """Hardware probe runs llama-server --list-devices (a subprocess) —
        keep it off the GUI thread; the label fills in when it lands."""
        from infra import local_model

        def work() -> None:
            try:
                hw = local_model.detect_hardware(refresh=True)
            except Exception:
                hw = None
            try:
                self.local_hw_done.emit(hw)
            except RuntimeError:
                pass  # dialog closed mid-probe

        threading.Thread(target=work, daemon=True, name="local-hw-probe").start()

    def _on_local_hw(self, hw) -> None:
        from infra import local_model
        if hw is None:
            self.local_hw_label.setText(tr('Hardware detection unavailable.'))
            return
        self._local_hw = hw
        self.local_hw_label.setText(f"{tr('This PC')}: {hw.summary()}")
        entry, reason = local_model.recommend(hw, self._local_manifest)
        # Fit tiers per row drive the badges; the recommended row gets a
        # star so the choice is visible before anything is downloaded.
        self._local_tiers = {eid: local_model.fit_tier(hw, e)
                             for eid, e in self._local_entries.items()}
        if entry is None:
            self.local_reco_label.setText(f"{tr('Recommended')}: {reason}")
        else:
            self.local_reco_label.setText(f"{tr('Recommended for this PC')}: {reason}")
            self._local_reco_id = entry["id"]
        self._refresh_local_rows()

    def _refresh_local_rows(self) -> None:
        from infra import local_model
        reco_id = getattr(self, "_local_reco_id", None)
        active_id = None
        if self.config.llm.provider == "local":
            active_id = self.config.llm.model
        staged = getattr(self, "_pending_local_entry", None)
        if staged is not None:
            active_id = staged["id"]
        for entry_id, widgets in list(self._local_rows.items()):
            entry = self._local_entries.get(entry_id)
            if entry is None:
                continue
            wrap, name, fit, status, use_btn, dl_btn, rm_btn = widgets
            installed = local_model.is_installed(entry)
            size_gb = int(entry["size"]) / 1024 ** 3
            tier = self._local_tiers.get(entry["id"], "")
            badge, color = self._local_fit_badge(tier)
            fit.setText(badge)
            fit.setStyleSheet(f"color: {color}; font-weight: 600;" if color else "")
            if installed:
                ctx = local_model.entry_ctx(entry, getattr(self, "_local_hw", None))
                status.setText("✓ " + tr('installed') + f" · {size_gb:.1f} GiB · {ctx // 1024}K ctx")
            else:
                status.setText(f"{size_gb:.1f} GiB")
            dl_btn.setEnabled(not installed)
            rm_btn.setEnabled(installed)
            use_btn.setEnabled(installed)
            use_btn.setVisible(installed)
            # Active = what OK would persist; recommended = best fit.
            font_bold = (entry["id"] == active_id or entry["id"] == reco_id)
            name.setStyleSheet("font-weight: 700;" if font_bold else "")
            prefix = "★ " if entry["id"] == reco_id else ""
            suffix = "  ✓ " + tr("active") if entry["id"] == active_id else ""
            name.setText(f"{prefix}{entry['label']}{suffix}")

    def _local_use(self, entry: dict) -> None:
        """Stage this installed model as THE local model (visuals now,
        config only on OK — same contract as _apply_local_provider)."""
        self._apply_local_provider(entry)
        self._refresh_local_rows()

    def _local_download(self, entry: dict) -> None:
        from infra import local_model
        widgets = self._local_rows.get(entry["id"])
        if not widgets or not widgets[5].isEnabled():
            return
        self._local_cancel.clear()
        for _wrap, _name, _fit, _status, _use_btn, dl_btn, rm_btn in self._local_rows.values():
            dl_btn.setEnabled(False)
            rm_btn.setEnabled(False)
        if hasattr(self, "hf_download_btn"):
            self.hf_download_btn.setEnabled(False)
        self.local_progress.setVisible(True)
        self.local_progress.setRange(0, 100)
        self.local_progress.setValue(0)
        self.local_progress_lbl.setText(
            f"0 / {int(entry['size']) / 1024**3:.1f} GiB")
        self.local_cancel_btn.setVisible(True)

        def work() -> None:
            try:
                path = local_model.download_model(
                    entry,
                    on_progress=lambda done, total: (
                        self.local_dl_progress.emit(entry["id"], done, total)),
                    cancel=self._local_cancel)
                outcome = {"ok": True, "path": path}
            except Exception as exc:  # RuntimeError cancel/network or real error
                outcome = exc
            try:
                self.local_dl_done.emit(entry["id"], outcome)
            except RuntimeError:
                pass  # dialog closed mid-download; the .part resumes later

        threading.Thread(target=work, daemon=True,
                         name=f"local-dl-{entry['id']}").start()

    def _local_cancel_download(self) -> None:
        self._local_cancel.set()

    def _on_local_dl_progress(self, model_id: str, done: int, total: int) -> None:
        if total > 0:
            self.local_progress.setValue(int(done * 100 / total))
            self.local_progress_lbl.setText(f"{done / 1024**3:.1f} / {total / 1024**3:.1f} GiB")

    def _on_local_dl_done(self, model_id: str, outcome) -> None:
        from infra import local_model
        self.local_cancel_btn.setVisible(False)
        self.local_progress.setVisible(False)
        if hasattr(self, "hf_download_btn"):
            self.hf_download_btn.setEnabled(
                self._hf_mode == "files" and self.hf_list.currentItem() is not None)
        if isinstance(outcome, Exception):
            self._refresh_local_rows()
            self.local_test_result.setText(f"Download stopped: {outcome}")
            self.local_test_result.setStyleSheet("color: #ffcc00;")
            return
        entry = local_model.entry_by_id(model_id)
        custom_entry = self._custom_dl_entries.pop(model_id, None)
        if custom_entry is not None:
            # A custom (Hugging Face) model: persist its sidecar record so
            # the row, the restart logic and the server alias all survive.
            local_model.save_custom_entry(custom_entry)
            self._sync_local_rows()
            entry = entry or custom_entry
            if hasattr(self, "hf_status"):
                self.hf_status.setText(
                    f"{custom_entry['label']} " + tr('installed — it appears in '
                                                     'the model list above.'))
                self.hf_status.setStyleSheet("color: #a8edff;")
        self._refresh_local_rows()
        if entry is None:
            return
        # Downloading NEVER changes the provider (owner decision 6.4.1):
        # the built-in DeepSeek key stays the default regardless — the model
        # is installed and selectable; the user switches explicitly via the
        # endpoint combo or the Use button.
        self.local_test_result.setText(
            f"{entry['label']} installed — the built-in DeepSeek key stays "
            "the default agent. Choose 'Local AI (built-in, uncensored)' in "
            "the endpoint box above (or press Use) to run it.")
        self.local_test_result.setStyleSheet("color: #a8edff;")

    def _apply_local_provider(self, entry: dict) -> None:
        """STAGE the switch to the local provider (visuals only). The live
        config is mutated exclusively by _save_and_accept — Cancel must not
        leave it pointing at a provider the user walked away from."""
        self._pending_local_entry = entry
        idx = self.llm_endpoint.findData("local")
        if idx >= 0:
            self.llm_endpoint.setCurrentIndex(idx)
        self.llm_base_url.setText(LLM_PROVIDER_PRESETS["local"]["base_url"])
        self.local_test_result.setText(
            f"{entry['label']} ready — it becomes the agent when you press OK.")
        self.local_test_result.setStyleSheet("color: #a8edff;")
        self._refresh_local_rows()

    def _local_remove(self, entry: dict) -> None:
        confirm = QMessageBox.question(
            self, tr('Remove local model'),
            tr('Delete') + f" {entry['label']} " + tr('from this PC?') +
            " (" + tr('about') + f" {int(entry['size']) / 1024**3:.1f} GiB)")
        if confirm != QMessageBox.Yes:
            return

        def work() -> None:
            from infra import local_model
            try:
                local_model.remove_model(entry)
                outcome = {"ok": True, "id": entry["id"]}
            except Exception as exc:
                outcome = exc
            try:
                self.local_action_done.emit("remove", outcome)
            except RuntimeError:
                pass  # dialog closed

        threading.Thread(target=work, daemon=True,
                         name="local-model-remove").start()

    def _local_test(self) -> None:
        self.local_test_result.setText(tr('Testing…'))
        self.local_test_result.setStyleSheet("color: #2a7abf;")

        def work() -> None:
            from infra import local_model
            try:
                ok, detail = local_model.test_endpoint()
                outcome = {"ok": ok, "detail": detail}
            except Exception as exc:
                outcome = {"ok": False, "detail": str(exc)}
            try:
                self.local_action_done.emit("test", outcome)
            except RuntimeError:
                pass

        threading.Thread(target=work, daemon=True,
                         name="local-model-test").start()

    def _on_local_action_done(self, action: str, outcome) -> None:
        from infra import local_model
        if action == "remove":
            if isinstance(outcome, Exception):
                self._refresh_local_rows()
                self.local_test_result.setText(f"Remove failed: {outcome}")
                self.local_test_result.setStyleSheet("color: #ff3366;")
                return
            # A removed CUSTOM model's row disappears with its sidecar record
            # (ladder rows stay — they just show "not installed" again).
            if outcome.get("id") in self._custom_rows:
                self._remove_local_row(outcome.get("id"))
            self._refresh_local_rows()
            # A staged Use/download switch pointing at the removed model
            # must not resurrect it on OK.
            staged = getattr(self, "_pending_local_entry", None)
            if staged is not None and staged.get("id") == outcome.get("id"):
                self._pending_local_entry = None
            # Removing the ACTIVE model leaves config pointing at an absent
            # id — reconcile to another installed model (or the preset
            # default) so the request/alias pair stays honest.
            if (self.config.llm.provider == "local"
                    and self.config.llm.model == outcome.get("id")):
                installed = local_model.installed_models()
                self.config.llm.model = (installed[0]["id"] if installed
                                         else LLM_PROVIDER_PRESETS["local"]["models"][0])
                self.config.llm.fast_model = self.config.llm.model
            self.local_test_result.setText(tr('Model removed.'))
            self.local_test_result.setStyleSheet("color: #a8edff;")
            return
        # action == "test"
        if outcome.get("ok"):
            self.local_test_result.setText(
                f"✓ {tr('Local server running')} — {outcome.get('detail', '')}")
            self.local_test_result.setStyleSheet("color: #a8edff;")
        else:
            self.local_test_result.setText(
                tr('Local server not running. It starts automatically with '
                   'DeepFlux once a model is installed and the endpoint is '
                   'set to Local AI.'))
            self.local_test_result.setStyleSheet("color: #ffcc00;")

    # -- Local server on/off + loading progress ------------------------------

    def _refresh_server_state(self) -> None:
        """Lightweight server probe (background — /health has a 2.5 s cap)."""
        from infra import local_model

        def work() -> None:
            try:
                st = local_model.server_status()
            except Exception:
                st = {"state": "off"}
            try:
                self.server_state_changed.emit(st)
            except RuntimeError:
                pass  # dialog closed

        threading.Thread(target=work, daemon=True,
                         name="local-server-probe").start()

    def _local_server_toggle(self) -> None:
        if not self.server_toggle_btn.isEnabled():
            return
        state = self._server_state.get("state", "off")
        if state in ("running", "loading"):
            self._local_server_stop()
        else:
            self._local_server_start()

    def _local_server_stop(self) -> None:
        self._server_transitioning = True
        self.server_toggle_btn.setEnabled(False)
        self._apply_server_state_ui({"state": "loading", "stopping": True})

        def work() -> None:
            from infra import local_model
            try:
                local_model.stop_server_manual()
                time.sleep(0.5)
                st = local_model.server_status()
            except Exception:
                st = {"state": "off"}
            self._server_transitioning = False
            try:
                self.server_state_changed.emit(st)
            except RuntimeError:
                pass  # dialog closed

        threading.Thread(target=work, daemon=True,
                         name="local-server-stop").start()

    def _local_server_start(self) -> None:
        self._server_transitioning = True
        self.server_toggle_btn.setEnabled(False)
        self.server_progress.setVisible(True)

        def work() -> None:
            from infra import local_model

            def emit(st: dict) -> None:
                try:
                    self.server_state_changed.emit(st)
                except RuntimeError:
                    pass

            try:
                hw = local_model.detect_hardware()
                entry = local_model.entry_by_id(self.config.llm.model)
                if entry is None or not local_model.is_installed(entry):
                    installed = local_model.installed_models()
                    if not installed:
                        emit({"state": "off",
                              "error": tr('No model installed — download one first.')})
                        return
                    entry = installed[0]
                if not local_model.ensure_runtime():
                    emit({"state": "off",
                          "error": tr('The llama.cpp runtime is missing in this build.')})
                    return
                if not local_model.start_server(entry, hw):
                    emit({"state": "off", "error": tr('Could not start the server.')})
                    return
                started_at = time.monotonic()
                deadline = started_at + (600 if hw.vram_mb else 240)
                while time.monotonic() < deadline:
                    st = local_model.server_status()
                    st["elapsed"] = time.monotonic() - started_at
                    emit(st)
                    if st["state"] == "running":
                        return
                    time.sleep(1.0)
                emit({"state": "off",
                      "error": tr('Timed out waiting for the model to load. '
                                  'Stop and try again, or pick a smaller model.')})
            finally:
                self._server_transitioning = False

        threading.Thread(target=work, daemon=True,
                         name="local-server-start").start()

    def _on_server_state(self, st) -> None:
        self._server_state = st
        self._apply_server_state_ui(st)

    def _apply_server_state_ui(self, st: dict) -> None:
        from infra import local_model
        state = st.get("state", "off")
        # Terminal states always re-enable the toggle (a terminal emit can
        # be processed before the worker clears its transition flag);
        # mid-transition loading states keep it disabled.
        if state == "loading" and getattr(self, "_server_transitioning", False):
            self.server_toggle_btn.setEnabled(False)
        elif state in ("running", "off") \
                or not getattr(self, "_server_transitioning", False):
            self.server_toggle_btn.setEnabled(True)
        if state == "running":
            self.server_progress.setVisible(False)
            self.server_toggle_btn.setText(tr('Stop server'))
            name = ""
            model_id = st.get("model_id")
            if model_id:
                entry = local_model.entry_by_id(model_id)
                name = (entry or {}).get("label") or model_id
            self.server_state_lbl.setText(
                "● " + tr('Running') + (f" — {name}" if name else ""))
            self.server_state_lbl.setStyleSheet("color: #a8edff;")
        elif state == "loading":
            self.server_progress.setVisible(True)
            if st.get("stopping"):
                self.server_state_lbl.setText(tr('Stopping server…'))
            else:
                text = tr('Loading model…')
                elapsed = st.get("elapsed")
                if isinstance(elapsed, (int, float)) and elapsed > 0:
                    text += f" {elapsed:.0f}s"
                text += " (" + tr('GPU loads can take up to a minute') + ")"
                self.server_state_lbl.setText("◐ " + text)
            self.server_state_lbl.setStyleSheet("color: #ffcc00;")
            self.server_toggle_btn.setText(tr('Cancel / stop'))
        else:
            self.server_progress.setVisible(False)
            self.server_toggle_btn.setText(tr('Start server'))
            err = st.get("error")
            self.server_state_lbl.setText(
                "○ " + (err or tr('Off — the local AI uses no GPU/RAM until you start it.')))
            self.server_state_lbl.setStyleSheet("color: #888888;")

    # -- Custom Hugging Face models (search -> pick file -> download) --------

    def _hf_search(self) -> None:
        from infra import local_model
        query = self.hf_search_edit.text().strip()
        if not query or not self.hf_search_btn.isEnabled():
            return
        self.hf_search_btn.setEnabled(False)
        self.hf_list.setEnabled(False)
        self.hf_status.setText(tr('Searching Hugging Face…'))
        self.hf_status.setStyleSheet("color: #2a7abf;")

        def work() -> None:
            try:
                results = local_model.hf_search(query)
                outcome = {"ok": True, "results": results}
            except Exception as exc:
                outcome = exc
            try:
                self.hf_search_done.emit(query, outcome)
            except RuntimeError:
                pass  # dialog closed mid-search

        threading.Thread(target=work, daemon=True, name="hf-search").start()

    def _on_hf_search_done(self, query: str, outcome) -> None:
        self.hf_search_btn.setEnabled(True)
        self.hf_list.setEnabled(True)
        self._hf_mode = "search"
        self._hf_repo = ""
        self.hf_back_btn.setVisible(False)
        self.hf_download_btn.setEnabled(False)
        self.hf_list.clear()
        if isinstance(outcome, Exception):
            self.hf_status.setText(f"{tr('Search failed')}: {outcome}")
            self.hf_status.setStyleSheet("color: #ff3366;")
            return
        results = outcome.get("results") or []
        if not results:
            self.hf_status.setText(tr("No GGUF models found for") + f" '{query}'.")
            self.hf_status.setStyleSheet("color: #a8edff;")
            return
        for r in results:
            item = QListWidgetItem(f"{r['id']}   ·   ⬇ {r['downloads']:,}")
            item.setData(Qt.UserRole, r)
            self.hf_list.addItem(item)
        self.hf_status.setText(
            f"{len(results)} " + tr('models — double-click one to pick a file '
                                    '(most downloaded first).'))
        self.hf_status.setStyleSheet("color: #a8edff;")

    def _hf_on_selection(self, current, _previous) -> None:
        self.hf_download_btn.setEnabled(
            bool(current) and self._hf_mode == "files")

    def _hf_open_selected(self, item) -> None:
        if self._hf_mode != "search":
            return
        data = item.data(Qt.UserRole) or {}
        self._hf_open_repo(data.get("id") or "")

    def _hf_open_repo(self, repo_id: str) -> None:
        from infra import local_model
        if not repo_id:
            return
        self._hf_repo = repo_id
        # Snapshot the search list so Back restores it verbatim.
        self._hf_search_snapshot = [
            (self.hf_list.item(i).text(), self.hf_list.item(i).data(Qt.UserRole))
            for i in range(self.hf_list.count())]
        self.hf_list.clear()
        self.hf_list.setEnabled(False)
        self.hf_search_btn.setEnabled(False)
        self.hf_download_btn.setEnabled(False)
        self.hf_status.setText(f"{tr('Listing')} {repo_id} {tr('GGUF files…')}")
        self.hf_status.setStyleSheet("color: #2a7abf;")

        def work() -> None:
            try:
                files = local_model.hf_gguf_files(repo_id)
                outcome = {"ok": True, "files": files}
            except Exception as exc:
                outcome = exc
            try:
                self.hf_files_done.emit(repo_id, outcome)
            except RuntimeError:
                pass  # dialog closed

        threading.Thread(target=work, daemon=True, name="hf-files").start()

    def _hf_restore_search_list(self) -> None:
        self._hf_mode = "search"
        self.hf_back_btn.setVisible(False)
        self.hf_download_btn.setEnabled(False)
        self.hf_list.clear()
        for text, data in self._hf_search_snapshot:
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, data)
            self.hf_list.addItem(item)

    def _hf_back(self) -> None:
        self._hf_restore_search_list()

    def _on_hf_files_done(self, repo_id: str, outcome) -> None:
        self.hf_list.setEnabled(True)
        self.hf_search_btn.setEnabled(True)
        if isinstance(outcome, Exception):
            self.hf_status.setText(f"{tr('Could not list')} {repo_id}: {outcome}")
            self.hf_status.setStyleSheet("color: #ff3366;")
            self._hf_restore_search_list()
            return
        files = outcome.get("files") or []
        if not files:
            self.hf_status.setText(f"{repo_id}: " + tr('no .gguf files found.'))
            self.hf_status.setStyleSheet("color: #ffcc00;")
            self._hf_restore_search_list()
            return
        self._hf_mode = "files"
        self.hf_back_btn.setVisible(True)
        for f in files:
            item = QListWidgetItem(
                f"{f['file'].rsplit('/', 1)[-1]}   ·   {f['size'] / 1024**3:.2f} GiB")
            item.setData(Qt.UserRole, {"repo": repo_id, **f})
            self.hf_list.addItem(item)
        self.hf_status.setText(
            f"{len(files)} " + tr('files — select one and press Download.'))
        self.hf_status.setStyleSheet("color: #a8edff;")

    def _hf_download(self) -> None:
        from infra import local_model
        item = self.hf_list.currentItem()
        if not item or self._hf_mode != "files":
            return
        data = item.data(Qt.UserRole) or {}
        try:
            entry = local_model.custom_entry(
                data.get("repo") or self._hf_repo, data.get("file") or "",
                int(data.get("size") or 0), data.get("sha256") or "")
        except ValueError as exc:
            self.hf_status.setText(str(exc))
            self.hf_status.setStyleSheet("color: #ff3366;")
            return
        # The id derives from repo+file, so re-downloading reuses the row.
        self._local_entries[entry["id"]] = entry
        if entry["id"] not in self._local_rows:
            self._add_local_row(entry)
            self._custom_rows.add(entry["id"])
        if getattr(self, "_local_hw", None) is not None:
            self._local_tiers[entry["id"]] = local_model.fit_tier(self._local_hw, entry)
        self._custom_dl_entries[entry["id"]] = entry
        self.hf_status.setText(
            f"{tr('Downloading')} {entry['label']} ({entry['size'] / 1024**3:.1f} GiB)…")
        self.hf_status.setStyleSheet("color: #a8edff;")
        self._local_download(entry)

    def _load_values(self) -> None:
        provider = self.config.llm.provider
        idx = self.llm_endpoint.findData(provider if provider in LLM_PROVIDER_PRESETS else "deepseek")
        self.llm_endpoint.blockSignals(True)
        self.llm_endpoint.setCurrentIndex(max(idx, 0))
        self.llm_endpoint.blockSignals(False)
        self.llm_base_url.setText(self.config.llm.base_url)
        self.llm_model.setText(self.config.llm.model)
        self.llm_reasoning_effort.setChecked(self.config.llm.custom_reasoning_effort)
        self.llm_key.setText(self.config.llm.api_key)
        self._on_endpoint_changed(self.llm_endpoint.currentIndex())
        self.jkt_key.setText(self.config.indexer.api_key)
        self.brave_key.setText(self.config.web_search.brave_api_key)
        self.pplx_key.setText(self.config.web_search.api_key)
        self.tmdb_key.setText(self.config.iptv.tmdb_api_key)
        self.omdb_key.setText(self.config.iptv.omdb_api_key)
        self.fanarttv_key.setText(self.config.iptv.fanarttv_api_key)
        self.tpdb_key.setText(self.config.iptv.tpdb_api_key)
        self.stashdb_key.setText(self.config.iptv.stashdb_api_key)
        self.ost_key.setText(self.config.iptv.opensubtitles_api_key)
        self.ost_user.setText(self.config.iptv.opensubtitles_username)
        self.ost_pass.setText(self.config.iptv.opensubtitles_password)

    def _save_and_accept(self) -> None:
        if self._page in ("all", "ai"):
            provider = self._selected_endpoint()
            base_url = self.llm_base_url.text().strip()
            model = self.llm_model.text().strip()
            if provider == "custom" and (not base_url or not model):
                QMessageBox.warning(self, tr('Custom LLM'), tr('Enter both a base URL and model name for the custom endpoint.'))
                return
            self.config.llm.api_key = self.llm_key.text().strip()
            # "local" (bundled llama.cpp server) is keyless by design, like
            # "custom" — it must not be downgraded to offline demo mode.
            self.config.llm.provider = (
                provider
                if self.config.llm.api_key or provider in ("custom", "local")
                else "dummy")
            self.config.llm.base_url = base_url
            if provider == "custom":
                self.config.llm.model = model
                self.config.llm.fast_model = ""
            elif provider == "local":
                # The model is managed by the Local Models section. A staged
                # download switch wins; otherwise keep the current id when
                # it's a manifest id AND still installed, else the first
                # installed model, else the preset default. The base URL is
                # fixed (our loopback server), never user-typed.
                from infra import local_model
                staged = self._pending_local_entry
                if staged is not None:
                    self.config.llm.model = staged["id"]
                else:
                    installed = local_model.installed_models()
                    ids = {e["id"] for e in installed}
                    if self.config.llm.model not in ids:
                        self.config.llm.model = (installed[0]["id"] if installed
                                                 else LLM_PROVIDER_PRESETS["local"]["models"][0])
                self.config.llm.fast_model = self.config.llm.model
                self.config.llm.base_url = LLM_PROVIDER_PRESETS["local"]["base_url"]
            else:
                models = LLM_PROVIDER_PRESETS[provider].get("models") or []
                if models:
                    self.config.llm.model = models[0]
                    # Fast model = the cheap flash tier when the preset has
                    # one, not merely the second list entry (v4-pro would be
                    # 4x the price for summaries).
                    flash = next((m for m in models[1:] if "flash" in m.lower()), None)
                    self.config.llm.fast_model = flash or (models[1] if len(models) > 1 else "")
            self.config.llm.custom_reasoning_effort = self.llm_reasoning_effort.isChecked()
        if self._page in ("all", "search"):
            self.config.indexer.api_key = self.jkt_key.text().strip()
            self.config.web_search.brave_api_key = self.brave_key.text().strip()
            self.config.web_search.api_key = self.pplx_key.text().strip()
        if self._page in ("all", "media"):
            self.config.iptv.tmdb_api_key = self.tmdb_key.text().strip()
            self.config.iptv.omdb_api_key = self.omdb_key.text().strip()
            self.config.iptv.fanarttv_api_key = self.fanarttv_key.text().strip()
        if self._page in ("all", "adult"):
            self.config.iptv.tpdb_api_key = self.tpdb_key.text().strip()
            self.config.iptv.stashdb_api_key = self.stashdb_key.text().strip()
        if self._page in ("all", "subtitles"):
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
        self.setWindowTitle(tr('Import Bookmarks'))
        self.setMinimumWidth(420)
        self.setStyleSheet(_SHARED_STYLE)
        self.imported: List[Tuple[str, str, str]] = []  # (title, url, folder)
        self._build_ui()

    def _build_ui(self) -> None:
        from dlmgr.bookmarks_import import detect_browser_sources

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr('Import bookmarks from:')))

        self.list = QListWidget()
        self._sources = detect_browser_sources()
        for src in self._sources:
            QListWidgetItem(src.browser, self.list)
        if self._sources:
            self.list.setCurrentRow(0)
        layout.addWidget(self.list)

        if not self._sources:
            note = QLabel(tr('No Chrome/Edge/Brave/Firefox bookmarks found automatically.'))
            note.setObjectName("hint")
            note.setWordWrap(True)
            layout.addWidget(note)

        browse_btn = QPushButton(tr('Choose Bookmarks file manually...'))
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
            QMessageBox.information(self, tr('Import Bookmarks'), tr('Select a browser first.'))
            return
        try:
            pairs = read_bookmarks(self._sources[row])
        except Exception as exc:
            QMessageBox.warning(self, tr('Import Bookmarks'), f"Failed to read bookmarks: {exc}")
            return
        if not pairs:
            QMessageBox.information(self, tr('Import Bookmarks'), tr('No bookmarks found in that browser.'))
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
            QMessageBox.warning(self, tr('Import Bookmarks'), f"Failed to read bookmarks: {exc}")
            return
        if not pairs:
            QMessageBox.information(self, tr('Import Bookmarks'), tr('No bookmarks found in that file.'))
            return
        self.imported = pairs
        self.accept()
