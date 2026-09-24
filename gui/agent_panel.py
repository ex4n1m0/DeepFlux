"""Agent side panel — DeepFlux 5.0 "agent everywhere" dock.

A slim ask-anywhere panel on the right edge of the window (Ctrl+K toggles it).
It shares the ONE agent conversation with the full Agent page: sends are
routed through MainWindow's existing chat pipeline (``_on_send``), and the
panel mirrors the tail of the exchange — user messages, final answers,
errors — so the user can ask from any page without hunting for the Agent tab.
Long transcripts, memory tools and diagnostics stay on the full page; this is
the quick-ask surface.
"""
from __future__ import annotations
from gui.i18n import tr

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextBrowser,
    QVBoxLayout,
)

_PANEL_CSS = (
    "body { color: #e8eefc; font-family: 'Inter','Segoe UI',sans-serif;"
    " font-size: 13px; }"
    ".u { color: #a8edff; font-weight: 600; }"
    ".a { color: #ffffff; }"
    ".e { color: #f0b490; }"
    ".w { color: #8a9ab0; font-size: 12px; }"
)

_MAX_BLOCKS = 60          # hard cap on mirrored blocks (the panel is a tail view)
_MAX_BLOCK_CHARS = 1200   # per-block clip — full text always on the Agent page


class AgentPanel(QFrame):
    """Right-edge quick-ask dock mirroring the agent conversation."""

    submit_requested = Signal(str)
    open_chat_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("agent_panel")
        self.setFixedWidth(340)
        self._blocks = 0

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(8, 8, 8, 8)
        vbox.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel(tr('🧠 Agent'))
        title.setObjectName("panel_title")
        head.addWidget(title)
        self._busy_lbl = QLabel("")
        self._busy_lbl.setProperty("railText", True)
        head.addWidget(self._busy_lbl)
        head.addStretch(1)
        expand = QPushButton(tr('Chat ↗'))
        expand.setObjectName("btn_secondary")
        expand.setToolTip(tr('Open the full Agent page (transcript, memory, diagnostics)'))
        expand.clicked.connect(self.open_chat_requested.emit)
        head.addWidget(expand)
        hide = QPushButton(tr('×'))
        hide.setObjectName("btn_secondary")
        hide.setFixedWidth(34)
        hide.setToolTip(tr('Hide the panel (Ctrl+K brings it back)'))
        hide.clicked.connect(self.close_requested.emit)
        head.addWidget(hide)
        vbox.addLayout(head)

        self._view = QTextBrowser()
        self._view.setOpenLinks(False)
        self._view.document().setDefaultStyleSheet(_PANEL_CSS)
        self._view.setStyleSheet(
            "QTextBrowser { background-color: #0a0a0f; color: #ffffff;"
            " border: 2px solid #1a2a4a; border-radius: 4px; padding: 4px;"
            " font-size: 14px; }"
        )
        vbox.addWidget(self._view, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.input = QLineEdit()
        self.input.setPlaceholderText(tr('Ask anything — Enter to send'))
        self.input.returnPressed.connect(self._submit)
        row.addWidget(self.input, 1)
        send = QPushButton(tr('Send'))
        send.setObjectName("btn_accent")
        send.clicked.connect(self._submit)
        row.addWidget(send)
        vbox.addLayout(row)

    # -- public API ---------------------------------------------------------
    def append_exchange(self, kind: str, text: str) -> None:
        """Mirror one block. kind: 'user' | 'agent' | 'error' | 'note'."""
        cls = {"user": "u", "agent": "a", "error": "e"}.get(kind, "w")
        clipped = text if len(text) <= _MAX_BLOCK_CHARS else text[:_MAX_BLOCK_CHARS] + " …"
        self._view.append(f'<span class="{cls}">{html.escape(clipped)}</span>')
        self._blocks += 1
        while self._blocks > _MAX_BLOCKS:
            doc = self._view.document()
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.Start)
            # Select the first block INCLUDING its separator, then drop it.
            if not cursor.movePosition(QTextCursor.NextBlock, QTextCursor.KeepAnchor):
                break
            cursor.removeSelectedText()
            self._blocks -= 1
        bar = self._view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_busy(self, busy: bool) -> None:
        self._busy_lbl.setText(tr("· thinking") if busy else "")
        self.input.setEnabled(True)  # typing ahead is fine; send is guarded upstream

    def focus_input(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    # -- internals ----------------------------------------------------------
    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.submit_requested.emit(text)
