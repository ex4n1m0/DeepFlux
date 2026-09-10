"""RSS feed viewer window — shows all items from feeds in a browsable table."""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import requests

from agent.rss import RSSMonitor
from config import DeeptorrentConfig

logger = logging.getLogger(__name__)


class _ViewerSignals(QObject):
    """Signals for cross-thread communication during feed fetching."""
    loaded = Signal(dict)  # feed_url -> {items, feed_name, mode}


class RSSViewer(QDialog):
    """Full-screen RSS viewer showing all feed items in a browsable table.

    Features:
    - One tab/section per feed
    - Full table: # | Title | Download type | Published
    - Search/filter box
    - Select items and download with one click
    - Download confirmation shown in the window, not the chat
    """

    def __init__(
        self,
        config: DeeptorrentConfig,
        tools,  # ToolRegistry
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.tools = tools
        self._rss_monitor = RSSMonitor(config.rss)
        self._all_items: Dict[str, List[Dict[str, Any]]] = {}  # feed_url -> items
        self._feed_names: Dict[str, str] = {}  # feed_url -> name
        self._feed_modes: Dict[str, str] = {}  # feed_url -> mode
        self._signals = _ViewerSignals()
        self._signals.loaded.connect(self._on_feeds_loaded)

        self.setWindowTitle("RSS Feed Viewer")
        self.setMinimumSize(1000, 600)
        self.setStyleSheet("""
            QDialog { background-color: #0a0a0f; color: #ffffff; }
            QLabel { color: #ffffff; }
            QTableWidget { background-color: #0d1117; color: #ffffff; gridline-color: #1a2a4a; border: 3px solid #1a2a4a; border-radius: 6px; selection-background-color: #1a2a4a; selection-color: #2a7abf; alternate-background-color: #0f1520; }
            QHeaderView::section { background-color: #111827; color: #2a7abf; border: none; border-bottom: 3px solid #1a2a4a; padding: 2px 4px; font-weight: 600; font-size: 17px; text-transform: uppercase; }
            QLineEdit { background-color: #0d1117; color: #ffffff; border: 3px solid #1a2a4a; padding: 2px 8px; border-radius: 3px; }
            QLineEdit:focus { border: 3px solid #2a7abf; }
            QPushButton { background-color: #111827; color: #ffffff; border: 3px solid #1a2a4a; padding: 2px 10px; border-radius: 3px; }
            QPushButton:hover { background-color: #1a2a4a; border: 3px solid #2a7abf; color: #2a7abf; }
            QPushButton:pressed { background-color: #0a2a1a; }
            QPushButton:disabled { background-color: #0d1117; color: #3a4a5a; }
            QCheckBox { color: #ffffff; }
            QLabel#status { color: #2a7abf; font-size: 20px; }
        """)

        self._build_ui()

        # Auto-load feeds on open.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(100, self.load_feeds)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Header
        header = QHBoxLayout()
        header.addWidget(QLabel("RSS Feed Items"))
        header.addStretch()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.load_feeds)
        header.addWidget(self.refresh_btn)
        layout.addLayout(header)

        # Search/filter bar
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Filter:"))
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Type to filter by title...")
        self.filter_input.textChanged.connect(self._apply_filter)
        filter_layout.addWidget(self.filter_input)

        self.auto_dl_check = QCheckBox("Show auto-download items only")
        self.auto_dl_check.toggled.connect(self._apply_filter)
        filter_layout.addWidget(self.auto_dl_check)

        layout.addLayout(filter_layout)

        # Main table
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["#", "Feed", "Title", "Type", "Published"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet("QTableWidget { alternate-background-color: #0f1520; }")
        layout.addWidget(self.table)

        # Status label
        self.status_label = QLabel("Loading feeds...")
        self.status_label.setObjectName("status")
        layout.addWidget(self.status_label)

        # Bottom buttons
        btn_layout = QHBoxLayout()
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self._select_all)
        btn_layout.addWidget(self.select_all_btn)

        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.clicked.connect(self._deselect_all)
        btn_layout.addWidget(self.deselect_all_btn)

        btn_layout.addStretch()

        self.download_btn = QPushButton("Download Selected")
        self.download_btn.clicked.connect(self._download_selected)
        btn_layout.addWidget(self.download_btn)

        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

    def load_feeds(self) -> None:
        """Fetch all feeds in a background thread and populate the table."""
        if not self.config.rss.feeds:
            self.status_label.setText("No RSS feeds configured. Add feeds via File → RSS Feeds...")
            return

        self.status_label.setText("Loading feeds...")
        self.refresh_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.table.setRowCount(0)

        def _worker():
            results = {}
            for feed in self.config.rss.feeds:
                try:
                    result = self._rss_monitor.check_feed(feed)
                    items = result.get("all_items", [])
                    results[feed.url] = {
                        "items": items,
                        "feed_name": feed.name or feed.url,
                        "mode": feed.mode,
                        "category": feed.category,
                        "error": result.get("error"),
                    }
                except Exception as exc:
                    results[feed.url] = {
                        "items": [],
                        "feed_name": feed.name or feed.url,
                        "mode": feed.mode,
                        "category": feed.category,
                        "error": str(exc),
                    }
            self._signals.loaded.emit(results)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def _on_feeds_loaded(self, results: dict) -> None:
        """Populate the table with all feed items (called on GUI thread)."""
        self._all_items = {}
        self._feed_names = {}
        self._feed_modes = {}
        total = 0
        errors = 0

        for feed_url, data in results.items():
            if data.get("error"):
                errors += 1
                continue
            items = data.get("items", [])
            self._all_items[feed_url] = items
            self._feed_names[feed_url] = data.get("feed_name", feed_url)
            self._feed_modes[feed_url] = data.get("mode", "monitor")
            total += len(items)

        self._populate_table()
        self.status_label.setText(
            f"Loaded {total} items from {len(self._all_items)} feed(s)" +
            (f"  ({errors} feed(s) failed)" if errors else "")
        )
        self.refresh_btn.setEnabled(True)
        self.download_btn.setEnabled(total > 0)

    def _populate_table(self) -> None:
        """Fill the table from _all_items, applying current filter."""
        filter_text = self.filter_input.text().lower().strip()
        auto_only = self.auto_dl_check.isChecked()

        self.table.setRowCount(0)
        row = 0
        for feed_url, items in self._all_items.items():
            feed_name = self._feed_names.get(feed_url, feed_url)
            feed_mode = self._feed_modes.get(feed_url, "monitor")

            for i, item in enumerate(items):
                title = item.get("title", "Unknown")
                # Apply filter.
                if filter_text and filter_text not in title.lower():
                    continue
                if auto_only and not (item.get("magnet_uri") or item.get("torrent_url")):
                    continue

                dl_type = "—"
                if item.get("magnet_uri"):
                    dl_type = "magnet"
                elif item.get("torrent_url"):
                    dl_type = ".torrent"

                published = item.get("published", "")

                self.table.insertRow(row)
                # Global row number for display (the per-feed index is kept
                # in the UserRole data for download lookups).
                self.table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
                self.table.setItem(row, 1, QTableWidgetItem(feed_name))
                self.table.setItem(row, 2, QTableWidgetItem(title))
                self.table.setItem(row, 3, QTableWidgetItem(dl_type))
                self.table.setItem(row, 4, QTableWidgetItem(published))

                # Store feed_url and item_index in the title item's UserRole.
                self.table.item(row, 2).setData(Qt.UserRole, {"feed_url": feed_url, "index": i})
                row += 1

        if row == 0:
            self.status_label.setText("No items match your filter.")
        else:
            self.status_label.setText(f"Showing {row} item(s).")

    def _apply_filter(self) -> None:
        """Re-populate the table with the current filter text."""
        self._populate_table()

    def _select_all(self) -> None:
        self.table.selectAll()

    def _deselect_all(self) -> None:
        self.table.clearSelection()

    def _download_selected(self) -> None:
        """Download all selected items from the table."""
        selected_rows = set(idx.row() for idx in self.table.selectedIndexes())
        if not selected_rows:
            return

        # Group by feed_url.
        to_download: Dict[str, List[int]] = {}
        for row in selected_rows:
            item = self.table.item(row, 2)
            if not item:
                continue
            data = item.data(Qt.UserRole)
            if not data:
                continue
            feed_url = data["feed_url"]
            idx = data["index"]
            to_download.setdefault(feed_url, []).append(idx)

        total = sum(len(v) for v in to_download.values())
        self.status_label.setText(f"Downloading {total} item(s)...")
        self.download_btn.setEnabled(False)

        def _worker():
            downloaded = 0
            failed = 0
            for feed_url, indices in to_download.items():
                items = self._all_items.get(feed_url, [])
                feed_name = self._feed_names.get(feed_url, feed_url)
                # Find the feed config to get category.
                category = "Other"
                for f in self.config.rss.feeds:
                    if f.url == feed_url:
                        category = f.category or "Other"
                        break

                for idx in indices:
                    if idx < 0 or idx >= len(items):
                        failed += 1
                        continue
                    item = items[idx]
                    magnet = item.get("magnet_uri", "")
                    torrent_url = item.get("torrent_url", "")
                    title = item.get("title", f"item_{idx}")
                    try:
                        if magnet:
                            self.tools.call("add_magnet", {
                                "uri": magnet,
                                "save_path": self.config.default_save_path,
                                "category": category,
                            })
                            downloaded += 1
                        elif torrent_url:
                            import tempfile, os
                            resp = requests.get(torrent_url, timeout=30, headers={"User-Agent": "Deeptorrent/0.1"})
                            resp.raise_for_status()
                            tmp_path = None
                            try:
                                with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as tmp:
                                    tmp.write(resp.content)
                                    tmp_path = tmp.name
                                result = self.tools.call("add_torrent_file", {
                                    "path": tmp_path,
                                    "save_path": self.config.default_save_path,
                                    "category": category,
                                })
                                # Keep a persistent copy so the torrent can be
                                # restored after restart, then drop the temp file.
                                state_mgr = getattr(self.parent(), "_state_manager", None)
                                info_hash = result.get("info_hash", "") if isinstance(result, dict) else ""
                                if state_mgr and info_hash:
                                    state_mgr.store_torrent_file(tmp_path, info_hash)
                                downloaded += 1
                            finally:
                                if tmp_path and os.path.exists(tmp_path):
                                    try:
                                        os.unlink(tmp_path)
                                    except OSError:
                                        pass
                        else:
                            failed += 1
                    except Exception as exc:
                        logger.warning("Download failed for %s: %s", title, exc)
                        failed += 1

            # Update status on GUI thread.
            result = {"downloaded": downloaded, "failed": failed}
            # Use a signal-like approach via QTimer.singleShot.
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._on_download_done(result))

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def _on_download_done(self, result: dict) -> None:
        """Called when downloads finish (on GUI thread)."""
        downloaded = result["downloaded"]
        failed = result["failed"]
        if failed == 0:
            self.status_label.setText(f"✅ Downloaded {downloaded} item(s) successfully.")
        else:
            self.status_label.setText(f"✅ Downloaded {downloaded}, ❌ failed {failed}.")
        self.download_btn.setEnabled(True)
