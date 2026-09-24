"""Settings Hub — one searchable entry point for every settings surface (v5.1).

The v5 Settings menu already gathers the old File-menu zones; the hub adds the
two things a flat menu cannot do: search ("where do I change the jackett key?"
→ type "jackett") and a stable, documented list of every surface with a
one-line description. Selecting a row and pressing Open (or double-click /
Enter) launches the SAME focused dialogs the menu uses — MainWindow owns the
callbacks, so nothing about the dialogs themselves changes here.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)

from gui.window_sizing import roomy
from gui.i18n import tr


class SettingsHub(QDialog):
    """Searchable launcher for every settings dialog, grouped by category."""

    def __init__(self, entries: List[Dict[str, object]], parent=None) -> None:
        # entries: [{"category", "title", "description", "open": callable}]
        super().__init__(parent)
        self.setWindowTitle(tr('Settings — DeepFlux'))
        self.setModal(True)
        self.setMinimumSize(640, 480)
        roomy(self)

        self._entries = list(entries)
        self._callbacks: Dict[int, Callable[[], None]] = {}

        vbox = QVBoxLayout(self)
        vbox.setSpacing(8)

        self._search = QLineEdit()
        self._search.setObjectName("big_input")
        self._search.setPlaceholderText(tr('Search settings — try “jackett”, “api key”, “subtitles”…'))
        self._search.textChanged.connect(self._refilter)
        vbox.addWidget(self._search)

        self._list = QListWidget()
        self._list.itemActivated.connect(self._open_selected)
        self._list.itemSelectionChanged.connect(self._show_description)
        vbox.addWidget(self._list, 1)

        bottom = QHBoxLayout()
        self._desc = QLabel("")
        self._desc.setWordWrap(True)
        self._desc.setStyleSheet("color: #8a9ab0; font-size: 15px;")
        bottom.addWidget(self._desc, 1)
        open_btn = QPushButton(tr('Open…'))
        open_btn.setObjectName("btn_accent")
        open_btn.clicked.connect(self._open_selected)
        bottom.addWidget(open_btn)
        close_btn = QPushButton(tr('Close'))
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        vbox.addLayout(bottom)

        self._populate("")
        self._search.setFocus()

    # -- internals -----------------------------------------------------------
    def _populate(self, query: str) -> None:
        self._list.clear()
        self._callbacks.clear()
        q = query.strip().lower()
        current_category: Optional[str] = None
        row = 0
        for entry in self._entries:
            category = str(entry.get("category", ""))
            title = str(entry.get("title", ""))
            description = str(entry.get("description", ""))
            hay = f"{category} {title} {description}".lower()
            if q and q not in hay:
                continue
            if category != current_category:
                current_category = category
                header = QListWidgetItem(category)
                header.setFlags(Qt.NoItemFlags)  # disabled group label
                self._list.addItem(header)
                row += 1
            item = QListWidgetItem(title)
            item.setToolTip(description)
            self._list.addItem(item)
            self._callbacks[self._list.row(item)] = entry.get("open")  # type: ignore[assignment]
            row += 1
        if self._list.count():
            # First selectable row preselected so Enter opens it.
            for i in range(self._list.count()):
                if self._list.item(i).flags() != Qt.NoItemFlags:
                    self._list.setCurrentRow(i)
                    break

    def _refilter(self, text: str) -> None:
        self._populate(text)

    def _show_description(self) -> None:
        item = self._list.currentItem()
        if item is not None and item.flags() != Qt.NoItemFlags:
            self._desc.setText(item.toolTip())
        else:
            self._desc.setText("")

    def _open_selected(self) -> None:
        item = self._list.currentItem()
        if item is None or item.flags() == Qt.NoItemFlags:
            return
        cb = self._callbacks.get(self._list.row(item))
        if callable(cb):
            cb()
