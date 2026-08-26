"""Sources dialog — manage search sources for the agent.

Displays all configured torrent indexer sources (from Jackett) and allows
the user to add, remove, enable/disable, and edit them. Also supports
fetching the live list from a running Jackett instance.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import DeeptorrentConfig, SourceConfig

logger = logging.getLogger(__name__)


class SourcesDialog(QDialog):
    """Dialog for managing agent search sources."""

    def __init__(self, config: DeeptorrentConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Sources")
        self.setMinimumSize(700, 500)
        self.setStyleSheet("""
            QDialog { background-color: #0a0a0f; color: #c8d3e0; }
            QLabel { color: #c8d3e0; }
            QTableWidget { background-color: #0d1117; color: #c8d3e0; border: 1px solid #1a2a4a; border-radius: 6px; gridline-color: #1a2a4a; }
            QTableWidget::item { padding: 1px 4px; }
            QTableWidget::item:selected { background-color: #1a2a4a; color: #2a7abf; }
            QHeaderView::section { background-color: #0a0a0f; color: #2a7abf; border: none; border-bottom: 1px solid #1a2a4a; padding: 2px 4px; font-weight: 600; }
            QLineEdit { background-color: #0d1117; color: #c8d3e0; border: 1px solid #1a2a4a; padding: 2px 8px; border-radius: 3px; }
            QLineEdit:focus { border: 1px solid #2a7abf; }
            QCheckBox { color: #c8d3e0; }
            QCheckBox::indicator { border: 1px solid #1a2a4a; border-radius: 3px; width: 16px; height: 16px; }
            QCheckBox::indicator:checked { background-color: #2a7abf; border-color: #2a7abf; }
            QPushButton { background-color: #111827; color: #c8d3e0; border: 1px solid #1a2a4a; padding: 2px 10px; border-radius: 3px; }
            QPushButton:hover { background-color: #1a2a4a; border: 1px solid #2a7abf; color: #2a7abf; }
            QPushButton:disabled { color: #4a6a8a; border-color: #1a2a4a; }
            QLabel#hint { color: #4a6a8a; font-size: 11px; }
        """)

        self._build_ui()
        self._load_sources()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Header
        header = QLabel("Search Sources")
        header.setStyleSheet("color: #2a7abf; font-size: 16px; font-weight: 600;")
        layout.addWidget(header)

        hint = QLabel(
            "These sources are used by the agent when searching for torrents. "
            "Sources marked as configured in Jackett are searched via the Torznab API. "
            "You can add custom sources or remove ones you don't want the agent to use."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Jackett integration row
        jackett_row = QHBoxLayout()
        self.jackett_check = QCheckBox("Use Jackett for searches (instead of web search fallback)")
        self.jackett_check.setChecked(self.config.sources.use_jackett)
        jackett_row.addWidget(self.jackett_check)

        fetch_btn = QPushButton("Fetch from Jackett")
        fetch_btn.clicked.connect(self._fetch_from_jackett)
        jackett_row.addWidget(fetch_btn)

        jackett_row.addStretch()
        layout.addLayout(jackett_row)

        # Sources table
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Enabled", "Name", "ID", "Type", "URL"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.horizontalHeader().resizeSection(0, 70)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        # Add/remove buttons
        btn_row = QHBoxLayout()

        add_btn = QPushButton("+ Add Source")
        add_btn.clicked.connect(self._add_source)
        btn_row.addWidget(add_btn)

        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._remove_selected)
        btn_row.addWidget(remove_btn)

        toggle_btn = QPushButton("Toggle Selected")
        toggle_btn.clicked.connect(self._toggle_selected)
        btn_row.addWidget(toggle_btn)

        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.setToolTip("Replace the list with the built-in default sources")
        reset_btn.clicked.connect(self._reset_to_defaults)
        btn_row.addWidget(reset_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Status label
        self.status_label = QLabel("")
        self.status_label.setObjectName("hint")
        layout.addWidget(self.status_label)

        # Dialog buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_sources(self) -> None:
        """Load sources from config into the table."""
        sources = self.config.sources.sources
        self.table.setRowCount(len(sources))
        for i, src in enumerate(sources):
            # Enabled checkbox (non-editable, use toggle button)
            enabled_item = QTableWidgetItem()
            enabled_item.setFlags(enabled_item.flags() & ~Qt.ItemIsEditable)
            enabled_item.setCheckState(Qt.Checked if src.enabled else Qt.Unchecked)
            self.table.setItem(i, 0, enabled_item)

            self.table.setItem(i, 1, QTableWidgetItem(src.name))
            self.table.setItem(i, 2, QTableWidgetItem(src.id))
            self.table.setItem(i, 3, QTableWidgetItem(src.type))
            self.table.setItem(i, 4, QTableWidgetItem(src.url))

        self.status_label.setText(f"{len(sources)} sources loaded.")

    def _add_source(self) -> None:
        """Add a new blank source row."""
        row = self.table.rowCount()
        self.table.insertRow(row)

        enabled_item = QTableWidgetItem()
        enabled_item.setFlags(enabled_item.flags() & ~Qt.ItemIsEditable)
        enabled_item.setCheckState(Qt.Checked)
        self.table.setItem(row, 0, enabled_item)

        self.table.setItem(row, 1, QTableWidgetItem("New Source"))
        self.table.setItem(row, 2, QTableWidgetItem(""))
        self.table.setItem(row, 3, QTableWidgetItem("public"))
        self.table.setItem(row, 4, QTableWidgetItem("https://"))

        self.table.editItem(self.table.item(row, 1))
        self.status_label.setText(f"Added new source (row {row + 1}).")

    def _remove_selected(self) -> None:
        """Remove the selected source rows."""
        rows = sorted(set(idx.row() for idx in self.table.selectedIndexes()), reverse=True)
        if not rows:
            QMessageBox.information(self, "Remove", "Select a source to remove first.")
            return
        for row in rows:
            self.table.removeRow(row)
        self.status_label.setText(f"Removed {len(rows)} source(s).")

    def _toggle_selected(self) -> None:
        """Toggle the enabled state of selected sources."""
        rows = set(idx.row() for idx in self.table.selectedIndexes())
        if not rows:
            QMessageBox.information(self, "Toggle", "Select a source to toggle first.")
            return
        for row in rows:
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)

    def _fetch_from_jackett(self) -> None:
        """Fetch the list of configured indexers from the running Jackett instance."""
        from infra import jackett

        if not self.config.indexer.api_key:
            QMessageBox.warning(self, "Jackett", "No Jackett API key configured. Set it in Settings first.")
            return

        self.status_label.setText("Fetching from Jackett...")

        try:
            fetched = jackett.fetch_indexers(self.config)
        except Exception as exc:
            self.status_label.setText(f"Fetch failed: {exc}")
            QMessageBox.warning(self, "Jackett", f"Failed to fetch from Jackett:\n{exc}")
            return

        if not fetched:
            self.status_label.setText("No configured indexers found in Jackett.")
            return

        # Build the new source list: existing entries keep their enabled
        # state (a source the user disabled stays disabled); newly fetched
        # indexers start ENABLED — if it's configured in Jackett, the user
        # wants it searched.
        self.config.sources.sources = jackett.merge_sources(
            self.config.sources.sources, fetched, enable_new=True)
        self.config.sources.last_jackett_fetch = time.time()
        self._load_sources()
        n_enabled = sum(1 for s in self.config.sources.sources if s.enabled)
        self.status_label.setText(
            f"Fetched {len(fetched)} configured indexers from Jackett ({n_enabled} enabled)."
        )

    def _reset_to_defaults(self) -> None:
        """Replace the source list with the built-in defaults. No sources ship
        with DeepFlux anymore, so this clears the list."""
        from config import DEFAULT_SOURCES
        if DEFAULT_SOURCES:
            msg = f"Replace the current list with the {len(DEFAULT_SOURCES)} built-in default sources?"
        else:
            msg = "No built-in sources ship with DeepFlux — clear the current list?"
        reply = QMessageBox.question(
            self, "Reset Sources", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.config.sources.sources = [replace(s) for s in DEFAULT_SOURCES]
        self._load_sources()
        self.status_label.setText(
            f"Reset to {len(DEFAULT_SOURCES)} default sources." if DEFAULT_SOURCES
            else "Source list cleared."
        )

    def _save_and_accept(self) -> None:
        """Save the table contents back to config and accept."""
        sources = []
        for row in range(self.table.rowCount()):
            enabled_item = self.table.item(row, 0)
            name_item = self.table.item(row, 1)
            id_item = self.table.item(row, 2)
            type_item = self.table.item(row, 3)
            url_item = self.table.item(row, 4)

            name = name_item.text().strip() if name_item else ""
            if not name:
                continue

            sources.append(SourceConfig(
                id=(id_item.text().strip() if id_item else "") or name.lower().replace(" ", "-"),
                name=name,
                url=(url_item.text().strip() if url_item else ""),
                type=(type_item.text().strip() if type_item else "public"),
                enabled=bool(enabled_item and enabled_item.checkState() == Qt.Checked),
            ))

        self.config.sources.sources = sources
        self.config.sources.use_jackett = self.jackett_check.isChecked()
        self.accept()
