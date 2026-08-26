"""RSS feed management dialog for Deeptorrent."""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import DeeptorrentConfig, RSSFeed

logger = logging.getLogger(__name__)


class RSSDialog(QDialog):
    """Dialog for adding, editing, and removing RSS feed subscriptions."""

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        self._check_requested = False
        self.setWindowTitle("RSS Feeds")
        self.setMinimumSize(700, 400)
        self.setStyleSheet("""
            QDialog { background-color: #0a0a0f; color: #c8d3e0; }
            QLabel { color: #c8d3e0; }
            QTableWidget { background-color: #0d1117; color: #c8d3e0; gridline-color: #1a2a4a; border: 1px solid #1a2a4a; border-radius: 6px; selection-background-color: #1a2a4a; selection-color: #2a7abf; alternate-background-color: #0f1520; }
            QHeaderView::section { background-color: #111827; color: #2a7abf; border: none; border-bottom: 1px solid #1a2a4a; padding: 2px 4px; font-weight: 600; font-size: 11px; text-transform: uppercase; }
            QLineEdit, QComboBox { background-color: #0d1117; color: #c8d3e0; border: 1px solid #1a2a4a; padding: 2px 8px; border-radius: 3px; }
            QLineEdit:focus, QComboBox:focus { border: 1px solid #2a7abf; }
            QPushButton { background-color: #111827; color: #c8d3e0; border: 1px solid #1a2a4a; padding: 2px 10px; border-radius: 3px; }
            QPushButton:hover { background-color: #1a2a4a; border: 1px solid #2a7abf; color: #2a7abf; }
        """)
        self._build_ui()
        self._load_feeds()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("RSS Feed Subscriptions"))

        # Feed table
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Name", "URL", "Mode", "Category"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)

        # Buttons
        btn_layout = QHBoxLayout()
        add_btn = QPushButton("+ Add Feed")
        add_btn.clicked.connect(self._add_feed)
        btn_layout.addWidget(add_btn)

        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._edit_feed)
        btn_layout.addWidget(edit_btn)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._remove_feed)
        btn_layout.addWidget(remove_btn)

        btn_layout.addStretch()

        check_now_btn = QPushButton("Check Now")
        check_now_btn.clicked.connect(self._check_now)
        btn_layout.addWidget(check_now_btn)

        layout.addLayout(btn_layout)

        # Dialog buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _load_feeds(self) -> None:
        feeds = self.config.rss.feeds
        self.table.setRowCount(len(feeds))
        for i, f in enumerate(feeds):
            self.table.setItem(i, 0, QTableWidgetItem(f.name or f.url))
            self.table.setItem(i, 1, QTableWidgetItem(f.url))
            mode_label = "Auto-download" if f.mode == "auto_download" else "Monitor only"
            self.table.setItem(i, 2, QTableWidgetItem(mode_label))
            self.table.setItem(i, 3, QTableWidgetItem(f.category))
            self.table.item(i, 0).setData(Qt.UserRole, i)

    def _add_feed(self) -> None:
        """Open a sub-dialog to add a new feed."""
        dialog = _FeedEditDialog(self)
        if dialog.exec() == QDialog.Accepted:
            feed = dialog.get_feed()
            if feed and feed.url:
                self.config.rss.feeds.append(feed)
                self._load_feeds()

    def _edit_feed(self) -> None:
        """Edit the selected feed."""
        row = self.table.currentRow()
        if row < 0 or row >= len(self.config.rss.feeds):
            return
        feed = self.config.rss.feeds[row]
        dialog = _FeedEditDialog(self, feed)
        if dialog.exec() == QDialog.Accepted:
            updated = dialog.get_feed()
            if updated:
                self.config.rss.feeds[row] = updated
                self._load_feeds()

    def _remove_feed(self) -> None:
        """Remove the selected feed."""
        row = self.table.currentRow()
        if row < 0 or row >= len(self.config.rss.feeds):
            return
        name = self.config.rss.feeds[row].name or self.config.rss.feeds[row].url
        reply = QMessageBox.question(
            self, "Remove Feed",
            f"Remove feed '{name}'?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            del self.config.rss.feeds[row]
            self._load_feeds()

    def _check_now(self) -> None:
        """Trigger a manual check of all feeds (will be handled by the main window)."""
        self._check_requested = True
        self.accept()


class _FeedEditDialog(QDialog):
    """Sub-dialog for adding or editing a single feed."""

    def __init__(self, parent: QWidget, feed: Optional[RSSFeed] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Feed" if feed else "Add Feed")
        self.setMinimumWidth(500)
        self.setStyleSheet(parent.styleSheet())
        self._feed = feed
        self._build_ui()
        if feed:
            self._fill_form(feed)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Name (optional):"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. My favorite show RSS")
        layout.addWidget(self.name_input)

        layout.addWidget(QLabel("Feed URL:"))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://example.com/feed.rss")
        layout.addWidget(self.url_input)

        layout.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Monitor only (load feed, don't auto-download)", "monitor")
        self.mode_combo.addItem("Auto-download (download all new items)", "auto_download")
        layout.addWidget(self.mode_combo)

        layout.addWidget(QLabel("Category:"))
        self.category_input = QLineEdit()
        self.category_input.setPlaceholderText("Other")
        self.category_input.setText("Other")
        layout.addWidget(self.category_input)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _fill_form(self, feed: RSSFeed) -> None:
        self.name_input.setText(feed.name)
        self.url_input.setText(feed.url)
        if feed.mode == "auto_download":
            self.mode_combo.setCurrentIndex(1)
        self.category_input.setText(feed.category or "Other")

    def _validate_and_accept(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Validation", "Feed URL is required.")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            QMessageBox.warning(self, "Validation", "Feed URL must start with http:// or https://")
            return
        self.accept()

    def get_feed(self) -> Optional[RSSFeed]:
        """Return a RSSFeed from the dialog inputs."""
        if self._feed:
            # Preserve seen_items when editing.
            seen = self._feed.seen_items
        else:
            seen = []
        return RSSFeed(
            name=self.name_input.text().strip(),
            url=self.url_input.text().strip(),
            mode=self.mode_combo.currentData(),
            category=self.category_input.text().strip() or "Other",
            seen_items=seen,
        )
