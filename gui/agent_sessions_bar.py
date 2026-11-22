"""Agent tasks sidebar — saved conversation list on the Agent tab.

ZCode-style session list (owner request 2026-11-22): every conversation
is auto-saved as a local "task" under ~/.deeptorrent/agent_sessions and
surfaced here — click to reopen (transcript + full LLM context), ✎ to
rename, 🗑 to delete. The widget is store-agnostic: MainWindow feeds it
SessionMeta rows and owns all persistence; this class only renders and
emits intent signals.

Painted second lines (the dim preview under each task name) are a dim
painted secondary — they keep their own color like the EPG/tile second
lines, while the name follows the text theme via fonts.text_color().
"""
from __future__ import annotations

from gui.i18n import tr
from gui.fonts import scale_qss, text_color

from PySide6.QtCore import Qt, QRect, QSize, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMenu, QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QVBoxLayout, QWidget,
)

_ID_ROLE = Qt.UserRole          # session id (str)
_PREVIEW_ROLE = Qt.UserRole + 1  # dim second line (str)
_DIM_COLOR = QColor(0x8b, 0x98, 0xab)  # dim painted secondary (own color by rule)


def _font_up(font: QFont, steps: int) -> QFont:
    """A copy of ``font`` scaled up by ``steps`` (works in point OR pixel
    size — QSS sets px, app defaults set points)."""
    scaled = QFont(font)
    if scaled.pointSizeF() > 0:
        scaled.setPointSizeF(scaled.pointSizeF() + steps)
    elif scaled.pixelSize() > 0:
        scaled.setPixelSize(scaled.pixelSize() + steps)
    return scaled


class _TaskDelegate(QStyledItemDelegate):
    """Two-line row: task name (theme color, DemiBold) over a dim one-line
    preview, laid out by real font METRICS (owner screenshot 2026-11-23
    caught two defects: drawControl(CE_ItemViewItem) also renders the
    item's own text under ours — double print — and a fixed half/half
    rect split collides at larger font scales)."""

    # Owner 2026-11-23: text was too small — the name paints two font steps
    # up, the preview one step up (dim).
    _NAME_STEPS = 2
    _PREVIEW_STEPS = 1
    _PAD = 6          # outer padding inside the row rect
    _GAP = 2          # gap between the two lines

    def _row_fonts(self, option):
        name_font = _font_up(option.font, self._NAME_STEPS)
        name_font.setWeight(QFont.Weight.DemiBold)
        preview_font = _font_up(option.font, self._PREVIEW_STEPS)
        preview_font.setWeight(QFont.Weight.Normal)
        return name_font, preview_font

    def paint(self, painter: QPainter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # Selection/hover background ONLY: blank the text first, or
        # CE_ItemViewItem draws the item's DisplayRole text UNDER our
        # custom two-line paint (the live double-print bug).
        opt.text = ""
        style = opt.widget.style() if opt.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        name = index.data(Qt.DisplayRole) or ""
        preview = index.data(_PREVIEW_ROLE) or ""
        name_font, preview_font = self._row_fonts(opt)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(opt.rect)
        rect = opt.rect.adjusted(self._PAD, self._PAD, -self._PAD, -self._PAD)
        painter.setFont(name_font)
        painter.setPen(text_color())
        painter.drawText(rect, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                         painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, rect.width()))
        name_h = painter.fontMetrics().height()
        preview_rect = QRect(rect.x(), rect.y() + name_h + self._GAP,
                             rect.width(), max(rect.height() - name_h - self._GAP, 0))
        painter.setFont(preview_font)
        painter.setPen(_DIM_COLOR)
        painter.drawText(preview_rect, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                         painter.fontMetrics().elidedText(preview, Qt.TextElideMode.ElideRight, preview_rect.width()))
        painter.restore()

    def sizeHint(self, option, index):
        # Build the QSize EXPLICITLY: QSize.expandedTo has no (int, int)
        # overload, and a TypeError escaping sizeHint into the C++ layout
        # call segfaults natively (no traceback — found 2026-11-22 via
        # bare-list bisect; pinned by
        # tests/test_gui_render.py::test_task_delegate_sizehint_is_native_safe).
        # Height follows the two REAL font lines so no scale ever collides.
        from PySide6.QtGui import QFontMetrics
        name_font, preview_font = self._row_fonts(option)
        line_h = (QFontMetrics(name_font).height() + self._GAP
                  + QFontMetrics(preview_font).height() + 2 * self._PAD)
        base = super().sizeHint(option, index)
        return QSize(max(base.width(), 1), max(base.height(), line_h, 40))


class AgentSessionsBar(QWidget):
    """Left sidebar listing saved agent tasks (pure view; no store access)."""

    new_requested = Signal()
    selected = Signal(str)            # session id
    rename_requested = Signal(str)    # session id
    delete_requested = Signal(str)    # session id

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("agent_sessions_bar")
        self.setMinimumWidth(180)
        self.setMaximumWidth(300)
        self._interactive = True

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(4)

        head = QHBoxLayout()
        head.setContentsMargins(6, 4, 6, 2)
        title = QLabel(tr('🗂 Tasks'))
        title.setObjectName("panel_title")
        head.addWidget(title)
        head.addStretch(1)
        new_btn = QPushButton(tr('➕ New task'))
        new_btn.setObjectName("btn_accent")
        new_btn.setToolTip(tr('Start a new task (the current conversation stays saved)'))
        new_btn.clicked.connect(self.new_requested.emit)
        self._new_btn = new_btn
        head.addWidget(new_btn)
        vbox.addLayout(head)

        self._list = QListWidget()
        self._list.setObjectName("tasks_list")
        self._list.setItemDelegate(_TaskDelegate(self._list))
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._context_menu)
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.itemClicked.connect(self._on_clicked)
        # Keyboard/a11y activation: Enter or double-click on a selected row
        # opens it too — mouse-only itemClicked strands keyboard users (and
        # accessibility Invoke, which synthesizes activation rather than a
        # positional click; found live 2026-11-22).
        self._list.itemActivated.connect(self._on_clicked)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        vbox.addWidget(self._list, 1)

        # Empty-state hint (hidden as soon as one task exists).
        self._empty_lbl = QLabel(tr('No saved tasks yet —\nevery conversation is\nsaved automatically.'))
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setStyleSheet(scale_qss(
            "QLabel { color: #ffffff; font-size: 13px; background: transparent; }"))
        vbox.addWidget(self._empty_lbl)

        foot = QHBoxLayout()
        foot.setContentsMargins(6, 2, 6, 4)
        foot.setSpacing(4)
        rename_btn = QPushButton(tr('✎ Rename'))
        rename_btn.setObjectName("btn_secondary")
        rename_btn.setToolTip(tr('Rename the selected task'))
        rename_btn.clicked.connect(self._rename_current)
        self._rename_btn = rename_btn
        foot.addWidget(rename_btn)
        del_btn = QPushButton(tr('🗑 Delete'))
        del_btn.setObjectName("btn_secondary")
        del_btn.setToolTip(tr('Delete the selected task (asks first)'))
        del_btn.clicked.connect(self._delete_current)
        self._del_btn = del_btn
        foot.addWidget(del_btn)
        foot.addStretch(1)
        vbox.addLayout(foot)

        self.setStyleSheet(scale_qss(
            "QWidget#agent_sessions_bar { background-color: #05070c;"
            " border: 1px solid #1a2a4a; border-radius: 4px; }"
            "QListWidget#tasks_list { background-color: #080b12; color: #ffffff;"
            " border: 1px solid #1a2a4a; border-radius: 3px; padding: 2px;"
            " outline: none; font-size: 14px; }"
            "QListWidget#tasks_list::item { background: transparent;"
            " border: none; padding: 1px; }"
            "QListWidget#tasks_list::item:selected { background-color: #1a2a4a;"
            " color: #ffffff; }"
            "QListWidget#tasks_list::item:hover { background-color: #1a2a4a; }"))

    # -- public API ---------------------------------------------------------

    def set_sessions(self, metas) -> None:
        """Rebuild the list (newest first, as given) keeping the current
        selection stable when the row still exists."""
        current = self.current_session_id()
        self._list.blockSignals(True)
        self._list.clear()
        for meta in metas:
            item = QListWidgetItem(meta.name)
            item.setData(_ID_ROLE, meta.id)
            item.setData(_PREVIEW_ROLE, meta.preview)
            self._list.addItem(item)
        self._list.blockSignals(False)
        self._empty_lbl.setVisible(self._list.count() == 0)
        self._list.setVisible(self._list.count() > 0)
        if current:
            self.set_current(current, scroll=False)

    def set_current(self, session_id: str, scroll: bool = True) -> None:
        """Highlight a session WITHOUT emitting selected (programmatic)."""
        self._list.blockSignals(True)
        self._list.clearSelection()
        if session_id:
            for row in range(self._list.count()):
                if self._list.item(row).data(_ID_ROLE) == session_id:
                    self._list.setCurrentRow(row)
                    if scroll:
                        self._list.scrollToItem(self._list.item(row))
                    break
        self._list.blockSignals(False)
        self._sync_footer()

    def current_session_id(self) -> str:
        item = self._list.currentItem()
        return item.data(_ID_ROLE) if item else ""

    def set_interactive(self, enabled: bool) -> None:
        """Busy gate: no switching/deleting/creating while the agent runs."""
        self._interactive = enabled
        self._new_btn.setEnabled(enabled)
        self._list.setEnabled(enabled)
        self._sync_footer()

    # -- internals ----------------------------------------------------------

    def _on_clicked(self, item: QListWidgetItem) -> None:
        session_id = item.data(_ID_ROLE)
        if session_id:
            self.selected.emit(session_id)

    def _on_selection_changed(self) -> None:
        self._sync_footer()

    def _sync_footer(self) -> None:
        has = self._list.currentItem() is not None and self._list.isEnabled()
        self._rename_btn.setEnabled(has and self._interactive)
        self._del_btn.setEnabled(has and self._interactive)

    def _rename_current(self) -> None:
        session_id = self.current_session_id()
        if session_id:
            self.rename_requested.emit(session_id)

    def _delete_current(self) -> None:
        session_id = self.current_session_id()
        if session_id:
            self.delete_requested.emit(session_id)

    def _context_menu(self, pos) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        session_id = item.data(_ID_ROLE)
        menu = QMenu(self)
        act_rename = menu.addAction(tr('✎ Rename'))
        act_delete = menu.addAction(tr('🗑 Delete'))
        action = menu.exec(self._list.mapToGlobal(pos))
        if action == act_rename:
            self.rename_requested.emit(session_id)
        elif action == act_delete:
            self.delete_requested.emit(session_id)

    def keyPressEvent(self, event) -> None:
        """Delete key deletes the selected task (same confirm path)."""
        if event.key() == Qt.Key.Key_Delete:
            self._delete_current()
            return
        super().keyPressEvent(event)
