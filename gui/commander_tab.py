"""Commander tab — a dual-pane orthodox file manager (Double Commander style).

Two QFileSystemModel-backed panes sit side by side; file operations run from
the *active* pane into the *other* pane's current directory:

- F5 Copy, F6 Move, F7 New Folder, F8 Delete, F2 Rename, Backspace Up
- Native QFileSystemModel drag/drop is disabled so every mutation goes through
  the command safety controller.
- Long operations run on one serialized worker with per-operation cancellation;
  copies are chunked and transactionally committed from temporary siblings.
"""
from __future__ import annotations

import errno
import fnmatch
import hashlib
import logging
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import (
    QDir, QEvent, QModelIndex, QPoint, QTimer, Qt,
    QItemSelectionModel, Signal,
)
from PySide6.QtGui import QImage, QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCompleter,
    QDialog,
    QFileDialog,
    QFileSystemModel,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from gui.column_sizing import AutoColumnSizer
from gui.responsive import OverflowRow, ResponsiveRow, shrink_label
from gui.i18n import tr

logger = logging.getLogger(__name__)

_COPY_CHUNK = 4 * 1024 * 1024
_CHECKSUM_CHUNK = 1024 * 1024
_PREVIEW_TEXT_BYTES = 256 * 1024
_PREVIEW_IMAGE_BYTES = 20 * 1024 * 1024
_PREVIEW_IMAGE_PIXELS = 16_000_000
_ARCHIVE_MAX_ENTRIES = 10_000
_ARCHIVE_MAX_BYTES = 4 * 1024 * 1024 * 1024
_ARCHIVE_EXTENSIONS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".xml",
    ".yaml", ".yml", ".ini", ".cfg", ".toml", ".py", ".js", ".ts",
    ".css", ".html", ".htm", ".sql", ".sh", ".ps1", ".bat", ".torrent",
}
_MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".m2ts",
    ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav",
}

_MENU_STYLE = (
    "QMenu { background-color: #111827; color: #ffffff; border: 3px solid #1a2a4a;"
    " border-radius: 6px; padding: 4px; }"
    " QMenu::item { padding: 3px 20px; border-radius: 4px; }"
    " QMenu::item:selected { background-color: #1a2a4a; color: #2a7abf; }"
)


def _open_native(path: str) -> None:
    """Open a file/folder with the OS default handler."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        logger.warning("open failed: %s", exc)


class _Cancelled(Exception):
    pass


@dataclass
class _OperationState:
    """Cancellation and identity belonging to exactly one operation."""

    mode: str
    cancel: threading.Event = field(default_factory=threading.Event)
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    item_count: int = 0
    options: Dict[str, object] = field(default_factory=dict)
    result: object = None
    quiet: bool = False  # agent-driven run: no dialogs, failures go to the caller


class _CancellableReader:
    """File wrapper used by tarfile so cancellation/progress stay cooperative."""

    def __init__(self, source, callback) -> None:
        self._source = source
        self._callback = callback

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(size)
        if data:
            self._callback(len(data))
        return data


class _PaneView(QTreeView):
    """File list with orthodox-commander keys and incremental search.

    The selection keys are handled here instead of tab-level QShortcuts on
    purpose: a plain-character shortcut with WidgetWithChildren context
    fires even while a QLineEdit child has focus, so "+" or "-" shortcuts
    would hijack typing in the path/filter boxes. Key handling on the view
    itself only ever runs when the file list has focus.
    """

    group_select_requested = Signal(bool)   # True = select, False = deselect
    invert_selection_requested = Signal()
    clipboard_requested = Signal(str)       # "copy" | "cut" | "paste"
    copy_path_requested = Signal()
    search_echo_changed = Signal(str)

    _SEARCH_TIMEOUT_MS = 1500

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._search = ""
        self._echo_timer = QTimer(self)
        self._echo_timer.setSingleShot(True)
        self._echo_timer.setInterval(self._SEARCH_TIMEOUT_MS)
        self._echo_timer.timeout.connect(self._clear_search)

    # -- incremental search -----------------------------------------------------
    def _clear_search(self) -> None:
        if self._search:
            self._search = ""
            self.search_echo_changed.emit("")

    def _echo(self) -> None:
        self.search_echo_changed.emit(self._search)
        self._echo_timer.start()

    def jump_to_prefix(self, prefix: str) -> bool:
        """Move the cursor (never the selection) to the first matching name."""
        if not prefix:
            return False
        model = self.model()
        root = self.rootIndex()
        rows = model.rowCount(root)
        if rows == 0:
            return False
        start = self.currentIndex().row() + 1 if self.currentIndex().isValid() else 0
        ordered = list(range(start, rows)) + list(range(0, min(start, rows)))
        wildcard = "*" in prefix or "?" in prefix
        lowered = prefix.lower()
        for row in ordered:
            index = model.index(row, 0, root)
            name = os.path.basename(model.filePath(index))
            if fnmatch.fnmatch(name.lower(), lowered) if wildcard else name.lower().startswith(lowered):
                self.selectionModel().setCurrentIndex(
                    index, QItemSelectionModel.SelectionFlag.NoUpdate)
                self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
                return True
        return False

    # -- commander selection keys -------------------------------------------------
    def toggle_current_and_advance(self) -> None:
        """Insert key: flip the row under the cursor, then step down one row."""
        index = self.currentIndex()
        if not index.isValid():
            index = self.model().index(0, 0, self.rootIndex())
            if not index.isValid():
                return
        selection = self.selectionModel()
        if selection.isSelected(index):
            command = QItemSelectionModel.SelectionFlag.Deselect | QItemSelectionModel.SelectionFlag.Rows
        else:
            command = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        selection.select(index, command)
        below = self.model().index(index.row() + 1, 0, self.rootIndex())
        selection.setCurrentIndex(
            below if below.isValid() else index,
            QItemSelectionModel.SelectionFlag.NoUpdate)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self.state() != QAbstractItemView.State.EditingState:
            key = event.key()
            text = event.text()
            modifiers = event.modifiers()
            ctrl_only = modifiers == Qt.KeyboardModifier.ControlModifier
            plain = not modifiers & (Qt.KeyboardModifier.ControlModifier
                                     | Qt.KeyboardModifier.AltModifier
                                     | Qt.KeyboardModifier.MetaModifier)
            if plain and key == Qt.Key.Key_Insert:
                self.toggle_current_and_advance()
                return
            if plain and text == "+":
                self.group_select_requested.emit(True)
                return
            if plain and text in ("-", "_"):
                self.group_select_requested.emit(False)
                return
            if ctrl_only and key == Qt.Key.Key_I:
                self.invert_selection_requested.emit()
                return
            if (modifiers == (Qt.KeyboardModifier.ControlModifier
                              | Qt.KeyboardModifier.ShiftModifier)
                    and key == Qt.Key.Key_C):
                self.copy_path_requested.emit()
                return
            if ctrl_only and key in (Qt.Key.Key_C, Qt.Key.Key_X, Qt.Key.Key_V):
                self.clipboard_requested.emit(
                    {Qt.Key.Key_C: "copy", Qt.Key.Key_X: "cut", Qt.Key.Key_V: "paste"}[key])
                return
            if plain and text and text.isprintable():
                candidate = self._search + text
                if self.jump_to_prefix(candidate):
                    self._search = candidate
                    self._echo()
                return
            if plain and key == Qt.Key.Key_Backspace and self._search:
                self._search = self._search[:-1]
                if self._search:
                    self.jump_to_prefix(self._search)
                self._echo()
                return
            if plain and key == Qt.Key.Key_Escape and self._search:
                self._search = ""
                self.search_echo_changed.emit("")
                return
        super().keyPressEvent(event)


class _PathBar(QWidget):
    """Breadcrumb navigation with the plain path edit one click away.

    Shows one flat QToolButton per path segment; when the row is too
    narrow the LEADING segments collapse into a "⋯" button that jumps to
    the deepest hidden ancestor, so the current folder never falls off
    the bar. The trailing "✎" button (or double-click) swaps in the
    classic QLineEdit; Enter navigates and returns to the crumbs, Esc or
    focus loss goes back without navigating.
    """

    navigate_requested = Signal(str)

    # Deep paths would otherwise set the whole pane's layout minimum via the
    # breadcrumb size hints (the responsive-window cap depends on it).
    _MIN_WIDTH = 40

    def __init__(self, path_edit: QLineEdit, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._path_edit = path_edit
        self._buttons: List[QToolButton] = []
        self._segments: List[str] = []
        self._ellipsis_target = ""
        self.setMinimumWidth(self._MIN_WIDTH)

        self._stack = QStackedWidget(self)
        self._crumbs = QWidget(self)
        row = QHBoxLayout(self._crumbs)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._ellipsis = QToolButton(self._crumbs)
        self._ellipsis.setText("⋯")
        self._ellipsis.setAccessibleName("Hidden parent folders")
        self._ellipsis.setToolTip(tr('Jump to a hidden parent folder'))
        self._ellipsis.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._ellipsis.setMinimumWidth(0)
        self._ellipsis.setVisible(False)
        self._ellipsis.clicked.connect(self._jump_ellipsis)
        row.addWidget(self._ellipsis)
        row.addStretch(1)
        self._edit_btn = QToolButton(self._crumbs)
        self._edit_btn.setText("✎")
        self._edit_btn.setAccessibleName("Edit path")
        self._edit_btn.setToolTip(tr('Type a path instead (press Enter to navigate)'))
        self._edit_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._edit_btn.clicked.connect(self.show_edit)
        row.addWidget(self._edit_btn)
        self._stack.addWidget(self._crumbs)
        self._stack.addWidget(self._path_edit)
        self._stack.setCurrentWidget(self._crumbs)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

        self._path_edit.installEventFilter(self)

    # -- crumbs ---------------------------------------------------------------
    def set_path(self, path: str) -> None:
        for button in self._buttons:
            row = self._crumbs.layout()
            if row is not None:
                row.removeWidget(button)
            button.deleteLater()
        self._buttons = []
        self._segments = []
        if os.name == "nt" and path.startswith("\\\\"):
            # UNC crumbs would rebuild relative paths ("server") that don't
            # navigate back to the share — offer the plain editor instead.
            self._ellipsis.setVisible(False)
            self._ellipsis_target = ""
            self.show_edit()
            return
        parts = [part for part in path.split(os.sep) if part]
        cumulative = os.sep if os.sep == "/" else ""
        for part in parts:
            cumulative = os.path.join(cumulative, part) if cumulative else (
                part + os.sep if os.name == "nt" and part.endswith((":")) else part)
            self._segments.append(cumulative)
        if not self._segments:
            self._segments = [os.sep]
        for index, target in enumerate(self._segments):
            label = os.path.basename(target.rstrip(os.sep)) or target
            button = QToolButton(self._crumbs)
            button.setText(label)
            button.setAccessibleName(f"Path segment {label}")
            button.setToolTip(QDir.toNativeSeparators(target))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            # zero layout minimum: deep paths must not widen the window floor
            button.setMinimumWidth(0)
            button.clicked.connect(lambda _checked=False, p=target: self.navigate_requested.emit(p))
            insert = self._crumbs.layout().count() - 2  # before stretch + edit button
            self._crumbs.layout().insertWidget(insert, button)
            self._buttons.append(button)
        self._relayout()

    def _jump_ellipsis(self) -> None:
        if self._ellipsis_target:
            self.navigate_requested.emit(self._ellipsis_target)

    def _relayout(self) -> None:
        """Hide leading segments while the row cannot fit them at full width."""
        available = max(0, self.width() - self._edit_btn.sizeHint().width() - 24)
        hidden = 0
        while hidden < len(self._buttons) - 1:
            needed = self._ellipsis.sizeHint().width() + sum(
                button.sizeHint().width() for button in self._buttons[hidden:])
            if needed <= available:
                break
            hidden += 1
        for index, button in enumerate(self._buttons):
            button.setVisible(index >= hidden)
        self._ellipsis.setVisible(hidden > 0)
        self._ellipsis_target = self._segments[hidden - 1] if hidden else ""

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    # -- editor ---------------------------------------------------------------
    def show_edit(self) -> None:
        self._stack.setCurrentWidget(self._path_edit)
        self._path_edit.setFocus()
        self._path_edit.selectAll()

    def show_crumbs(self) -> None:
        if self._stack.currentWidget() is not self._crumbs:
            self._stack.setCurrentWidget(self._crumbs)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._path_edit:
            if (event.type() == QEvent.Type.KeyPress
                    and event.key() == Qt.Key.Key_Escape):
                self.show_crumbs()
                return True
            if event.type() == QEvent.Type.FocusOut:
                self.show_crumbs()
        return super().eventFilter(obj, event)

    # -- test/inspection helpers -------------------------------------------------
    def segment_paths(self) -> List[str]:
        return list(self._segments)

    def visible_segment_count(self) -> int:
        # isHidden() (explicit setVisible(False)), not isVisible(): the whole
        # pane may legitimately be unshown (tests, background tab).
        return sum(1 for button in self._buttons if not button.isHidden())


class FilePane(QWidget):
    """One side of the commander: drive bar + path bar + file tree."""

    activated_pane = Signal(object)    # emitted with self when the pane gains focus
    op_requested = Signal(str)         # commander-routed filesystem mutation
    selection_changed = Signal(object) # selected paths, for asynchronous preview
    status_message = Signal(str)
    path_changed = Signal(str)         # final navigation target, for tab titles
    favorite_clicked = Signal()        # star button: the tab owns the favorites list
    file_clipboard_request = Signal(str)  # "copy" | "cut" | "paste" (commander clipboard)

    def __init__(self, start_path: str, parent: Optional[QWidget] = None,
                 pane_name: str = "File pane") -> None:
        super().__init__(parent)
        self._pane_name = pane_name
        self.setAccessibleName(pane_name)
        self._back_history: List[str] = []
        self._forward_history: List[str] = []
        self._show_hidden_system = False
        self._search_echo = ""
        self._last_group_pattern = "*.txt"
        self._favorites_provider = None
        self._path_retries = 0
        self._show_token = 0
        self._shown_path = ""
        self._model = QFileSystemModel(self)
        self._model.setReadOnly(False)
        self._model.setRootPath("")
        self._apply_filter()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        if sys.platform == "win32":
            self._drives = QComboBox()
            self._drives.setAccessibleName(f"{pane_name} drive")
            self._drives.setToolTip(tr('Choose a drive'))
            for fi in QDir.drives():
                self._drives.addItem(fi.absoluteFilePath())
            self._drives.setFixedWidth(70)
            self._drives.activated.connect(
                lambda _i: self.navigate(self._drives.currentText()))
            bar.addWidget(self._drives)
        self._back_btn = self._nav_button(
            "←", "Back", "Go back (Alt+Left)", self.go_back)
        bar.addWidget(self._back_btn)
        self._forward_btn = self._nav_button(
            "→", "Forward", "Go forward (Alt+Right)", self.go_forward)
        bar.addWidget(self._forward_btn)
        self._up_btn = self._nav_button(
            "▲", "Up", "Up one level (Backspace)", self.go_up)
        bar.addWidget(self._up_btn)
        self._path_edit = QLineEdit()
        self._path_edit.setAccessibleName(f"{pane_name} path")
        self._path_edit.setToolTip(tr('Current folder path; press Enter to navigate'))
        completer = QCompleter(self)
        comp_model = QFileSystemModel(completer)
        comp_model.setRootPath("")
        completer.setModel(comp_model)
        self._path_edit.setCompleter(completer)
        self._path_edit.returnPressed.connect(lambda: (
            self.navigate(self._path_edit.text()),
            self._path_bar.show_crumbs(),
        ))
        self._path_bar = _PathBar(self._path_edit)
        self._path_bar.setAccessibleName(f"{pane_name} breadcrumb path")
        self._path_bar.setToolTip(tr('Clickable path segments; press ✎ to type a path'))
        self._path_bar.navigate_requested.connect(self.navigate)
        bar.addWidget(self._path_bar, 1)
        self._refresh_btn = self._nav_button(
            "⟳", "Refresh", "Refresh current folder",
            lambda: self.navigate(self.current_path(), record_history=False))
        bar.addWidget(self._refresh_btn)
        self._fav_btn = self._nav_button(
            "☆", "favorites", tr('Favorites (Ctrl+1..9 jumps to the first nine)'),
            lambda: self.favorite_clicked.emit())
        bar.addWidget(self._fav_btn)
        layout.addLayout(bar)

        tools = QHBoxLayout()
        tools.setSpacing(4)
        filter_label = QLabel(tr('Filter:'))
        filter_label.setAccessibleName(f"{pane_name} filter label")
        tools.addWidget(filter_label)
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(tr('filename contains…'))
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setAccessibleName(f"{pane_name} filename filter")
        self._filter_edit.setToolTip(tr('Quickly show names containing this text'))
        self._filter_edit.textChanged.connect(self.set_filename_filter)
        tools.addWidget(self._filter_edit, 1)
        self._hidden_btn = QPushButton(tr('Hidden/System'))
        self._hidden_btn.setCheckable(True)
        self._hidden_btn.setAccessibleName(f"{pane_name} hidden and system files toggle")
        self._hidden_btn.setToolTip(tr('Show or hide hidden and system items'))
        self._hidden_btn.toggled.connect(self.set_show_hidden_system)
        tools.addWidget(self._hidden_btn)
        layout.addLayout(tools)

        self._view = _PaneView(self)
        self._view.setAccessibleName(f"{pane_name} files")
        self._view.setToolTip(tr('Files and folders; native drag and drop is disabled for safety'))
        self._view.setModel(self._model)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        # QFileSystemModel mutates the filesystem directly for native drops,
        # bypassing overwrite rollback, cancellation, and operation serialization.
        self._view.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self._view.setDragEnabled(False)
        self._view.setAcceptDrops(False)
        self._view.viewport().setAcceptDrops(False)
        self._view.setUniformRowHeights(True)
        self._view.setAnimated(False)
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        # Same column behaviour as the download tables: file names always
        # fully readable (name column fits its content and absorbs leftover
        # viewport width), every column user-draggable, a drag never
        # stomped, double-click a separator to re-enable auto-fit.
        self._column_sizer = AutoColumnSizer(self._view, fill_column=0, padding=24)
        # QFileSystemModel populates directories asynchronously in batches,
        # so refit whenever rows land or details (size/date) fill in.
        self._fit_timer = QTimer(self._view)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(250)
        self._fit_timer.timeout.connect(self._column_sizer.auto_fit)
        self._model.rowsInserted.connect(lambda *_: self._fit_timer.start())
        self._model.dataChanged.connect(lambda *_: self._fit_timer.start())
        self._view.activated.connect(self._on_activated)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self._view, 1)

        self._pane_status = QLabel("")
        self._pane_status.setAccessibleName(f"{pane_name} free space and selection status")
        self._pane_status.setToolTip(tr('Selection count and free disk space'))
        self._pane_status.setStyleSheet("color: #8a9ab0; font-size: 17px;")
        # Live selection/free-space text: word-wrap or a long status sets the
        # whole window's minimum width (measured 612px from one pane).
        shrink_label(self._pane_status)
        layout.addWidget(self._pane_status)

        self._view.installEventFilter(self)
        self._view.viewport().installEventFilter(self)
        self._path_edit.installEventFilter(self)
        self._filter_edit.installEventFilter(self)
        self._view.selectionModel().selectionChanged.connect(self._report_selection)
        # Commander keys live on the view itself (see _PaneView) so typing in
        # the path/filter edits can never trigger them.
        self._view.group_select_requested.connect(self._select_group)
        self._view.invert_selection_requested.connect(self._invert_selection)
        self._view.clipboard_requested.connect(self.file_clipboard_request.emit)
        self._view.copy_path_requested.connect(self._copy_paths_to_clipboard)
        self._view.search_echo_changed.connect(self._set_search_echo)
        # Navigating into a folder whose PARENT listing is still in flight can
        # hand the view a node the model later re-creates while reconciling
        # that listing (rowsRemoved kills the view's root). Re-resolve by path.
        self._model.rowsRemoved.connect(lambda *_: self._heal_root_index())
        self._model.modelReset.connect(lambda *_: self._heal_root_index())

        start = start_path if os.path.isdir(start_path) else str(Path.home())
        self.navigate(start, record_history=False)

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if amount < 1024.0 or unit == "TiB":
                return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
            amount /= 1024.0
        return f"{amount:.1f} TiB"

    def _nav_button(self, text: str, name: str, tooltip: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setAccessibleName(f"{self._pane_name} {name.lower()}")
        button.setToolTip(tooltip)
        # 40px keeps the arrow glyphs readable; at 30px the emoji-presentation
        # forms collapse into blobs (the "unclear buttons" report, 2026-09-13).
        button.setFixedWidth(40)
        button.clicked.connect(slot)
        return button

    # -- navigation ----------------------------------------------------------
    def navigate(self, path: str, record_history: bool = True) -> None:
        path = os.path.abspath(path.strip().strip('"'))
        if not os.path.isdir(path):
            self.status_message.emit(f"Path not found: {path}")
            return
        current = self.current_path()
        if record_history and current and not self._same_location(current, path):
            self._back_history.append(current)
            self._forward_history.clear()
        self._show_path(path)

    def _show_path(self, path: str) -> None:
        self._show_token += 1
        self._show_path_now(path, self._show_token)

    def _show_path_now(self, path: str, token: int) -> None:
        self._shown_path = path
        self.path_changed.emit(path)
        self._path_edit.setText(QDir.toNativeSeparators(path))
        self._path_bar.set_path(path)
        self._update_favorite_star()
        if sys.platform == "win32":
            drive = os.path.splitdrive(path)[0] + "\\"
            i = self._drives.findText(drive, Qt.MatchFlag.MatchStartsWith)
            if i >= 0:
                self._drives.setCurrentIndex(i)
        self._update_navigation_buttons()
        self._update_pane_status()
        self.status_message.emit(path)
        index = self._model.index(path)
        if not index.isValid():
            if self._path_retries < 25:
                # A just-created folder races the model's watcher: index()
                # stays invalid until the queued rowsInserted is processed,
                # and setting an invalid root silently shows the drive list.
                # Retry on later event-loop turns instead. (Do NOT force the
                # model here via setRootPath(path): swapping the model root
                # re-fetches and can prune the node the view already holds.)
                self._path_retries += 1
                QTimer.singleShot(40, lambda: self._retry_show_path(path, token))
                return
            index = QModelIndex()  # gave up: legacy behavior (drive list)
        self._path_retries = 0
        self._view.setRootIndex(index)
        # A QTreeView root change does NOT clear the selection model: an old
        # folder's selection would survive invisibly and the next F5/F6 (or
        # an agent transfer) would act on paths the pane no longer shows.
        if self._view.selectionModel() is not None:
            self._view.selectionModel().clear()
        # QFileSystemModel fetches lazily; a shown view triggers the fetch in
        # its layout pass, but a hidden/background pane (tests, background
        # tab) never lays out — request the listing explicitly so navigation
        # always populates.
        if self._model.canFetchMore(index):
            self._model.fetchMore(index)
        # Columns sized for the new folder right away (header hints), then
        # again as the async listing lands rows via _fit_timer.
        self._column_sizer.auto_fit()
        self._fit_timer.start()

    def _retry_show_path(self, path: str, token: int) -> None:
        if token != self._show_token:
            return  # a newer navigation superseded this retry
        self._show_path_now(path, token)

    def _heal_root_index(self) -> None:
        """Re-resolve the view root by path after model churn killed it."""
        if self._view.rootIndex().isValid() or not self._shown_path:
            return
        index = self._model.index(self._shown_path)
        if index.isValid():
            self._view.setRootIndex(index)
            if self._model.canFetchMore(index):
                self._model.fetchMore(index)
            self._fit_timer.start()

    @staticmethod
    def _same_location(first: str, second: str) -> bool:
        return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
            os.path.abspath(second))

    def go_back(self) -> None:
        if not self._back_history:
            return
        current = self.current_path()
        target = self._back_history.pop()
        if current:
            self._forward_history.append(current)
        self._show_path(target)

    def go_forward(self) -> None:
        if not self._forward_history:
            return
        current = self.current_path()
        target = self._forward_history.pop()
        if current:
            self._back_history.append(current)
        self._show_path(target)

    def go_up(self) -> None:
        parent = os.path.dirname(self.current_path())
        if parent and parent != self.current_path():
            self.navigate(parent)

    def _update_navigation_buttons(self) -> None:
        self._back_btn.setEnabled(bool(self._back_history))
        self._forward_btn.setEnabled(bool(self._forward_history))

    def current_path(self) -> str:
        # The intended folder wins over the model's root index: while a
        # just-created folder is being retried (see _show_path_now), the view
        # still holds the previous root and the model would answer stale.
        return self._shown_path or self._model.filePath(self._view.rootIndex())

    def selected_paths(self) -> List[str]:
        return [self._model.filePath(i)
                for i in self._view.selectionModel().selectedRows(0)]

    def rename_selected(self) -> None:
        rows = self._view.selectionModel().selectedRows(0)
        if rows:
            self._view.edit(rows[0])

    def new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, tr('New Folder'), tr('Folder name:'))
        name = name.strip()
        if not ok or not name:
            return
        if not QDir(self.current_path()).mkdir(name):
            QMessageBox.warning(self, tr('New Folder'), f"Could not create '{name}'.")

    # -- selection ---------------------------------------------------------------
    def select_pattern(self, pattern: str, include: bool = True) -> int:
        """Select (or deselect) top-level rows matching ';' separated wildcards.

        Returns how many rows actually CHANGED — matching an already-selected
        row in select mode is not a change (the count feeds the agent).
        """
        patterns = [part.strip().lower() for part in pattern.split(";") if part.strip()]
        if not patterns:
            return 0
        model = self._model
        root = self._view.rootIndex()
        selection = self._view.selectionModel()
        flags = (QItemSelectionModel.SelectionFlag.Select if include
                 else QItemSelectionModel.SelectionFlag.Deselect) | QItemSelectionModel.SelectionFlag.Rows
        changed = 0
        for row in range(model.rowCount(root)):
            index = model.index(row, 0, root)
            name = os.path.basename(model.filePath(index))
            if any(fnmatch.fnmatch(name.lower(), p) for p in patterns):
                if selection.isSelected(index) != include:
                    selection.select(index, flags)
                    changed += 1
        return changed

    def _select_group(self, include: bool) -> None:
        title = tr('Select Group') if include else tr('Deselect Group')
        pattern, ok = QInputDialog.getText(
            self, title, tr('Pattern (wildcards; ";" separates several):'),
            text=self._last_group_pattern)
        if not ok:
            return
        pattern = pattern.strip()
        self._last_group_pattern = pattern
        if not pattern:
            return
        count = self.select_pattern(pattern, include)
        verb = tr('selected') if include else tr('deselected')
        self.status_message.emit(f"{count} {verb} — {pattern}")

    def _invert_selection(self) -> None:
        model = self._model
        root = self._view.rootIndex()
        selection = self._view.selectionModel()
        for row in range(model.rowCount(root)):
            index = model.index(row, 0, root)
            if selection.isSelected(index):
                command = QItemSelectionModel.SelectionFlag.Deselect | QItemSelectionModel.SelectionFlag.Rows
            else:
                command = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
            selection.select(index, command)

    def select_same_extension(self) -> None:
        """Select every top-level item sharing the first selection's extension."""
        paths = self.selected_paths()
        if not paths:
            self.status_message.emit(tr('Nothing selected'))
            return
        extension = os.path.splitext(paths[0])[1].lower()
        if extension:
            count = self.select_pattern("*" + extension, True)
        else:
            model = self._model
            root = self._view.rootIndex()
            selection = self._view.selectionModel()
            flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
            count = 0
            for row in range(model.rowCount(root)):
                index = model.index(row, 0, root)
                if "." not in os.path.basename(model.filePath(index)):
                    selection.select(index, flags)
                    count += 1
        self.status_message.emit(f"{count} {tr('selected')} — *{extension}")

    # -- favorites + clipboard ------------------------------------------------------
    def set_favorites_provider(self, provider) -> None:
        """Give the pane read access to the tab's favorites for the star state."""
        self._favorites_provider = provider
        self._update_favorite_star()

    def _update_favorite_star(self) -> None:
        favorites = self._favorites_provider() if self._favorites_provider else []
        current = os.path.normcase(os.path.abspath(self.current_path()))
        marked = any(os.path.normcase(os.path.abspath(path)) == current
                     for path in favorites if isinstance(path, str))
        self._fav_btn.setText("★" if marked else "☆")

    def _copy_paths_to_clipboard(self) -> None:
        paths = self.selected_paths()
        if not paths:
            self.status_message.emit(tr('Nothing selected'))
            return
        text = "\n".join(QDir.toNativeSeparators(os.path.abspath(p)) for p in paths)
        QApplication.clipboard().setText(text)
        self.status_message.emit(f"{len(paths)} {tr('path(s) copied')}")

    # -- active-pane visuals ---------------------------------------------------
    def set_active(self, on: bool) -> None:
        color = "#2a7abf" if on else "#1a2a4a"
        self._view.setStyleSheet(f"QTreeView {{ border: 3px solid {color}; }}")

    # -- filtering and status --------------------------------------------------
    def set_filename_filter(self, text: str) -> None:
        """Apply a quick, case-insensitive filename substring filter."""
        text = text.strip()
        if self._filter_edit.text() != text:
            self._filter_edit.setText(text)
            return
        self._model.setNameFilters([f"*{text}*"] if text else ["*"])
        self._model.setNameFilterDisables(False)
        self._update_pane_status()

    def filename_filter(self) -> str:
        return self._filter_edit.text()

    def set_show_hidden_system(self, show: bool) -> None:
        self._show_hidden_system = bool(show)
        self._hidden_btn.setChecked(self._show_hidden_system)
        # Global QSS has no :checked rule — swap to the accent style so the
        # ON state is visible (same pattern as the Play tab view toggles).
        self._hidden_btn.setObjectName("btn_accent" if show else "")
        self._hidden_btn.style().polish(self._hidden_btn)
        self._apply_filter()
        self._update_pane_status()

    def shows_hidden_system(self) -> bool:
        return self._show_hidden_system

    def _apply_filter(self) -> None:
        filt = QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot
        if self._show_hidden_system:
            filt |= QDir.Filter.Hidden | QDir.Filter.System
        self._model.setFilter(filt)

    def _set_search_echo(self, text: str) -> None:
        """Show the live incremental-search prefix in the pane status line."""
        self._search_echo = text
        self._update_pane_status()

    def _update_pane_status(self) -> None:
        path = self.current_path()
        count = len(self.selected_paths()) if self._view.selectionModel() else 0
        try:
            usage = shutil.disk_usage(path)
            space = f"{self._format_bytes(usage.free)} free of {self._format_bytes(usage.total)}"
        except OSError:
            space = "Free space unavailable"
        filter_text = self.filename_filter() if hasattr(self, "_filter_edit") else ""
        filtered = f" | filter: {filter_text}" if filter_text else ""
        search = f"{tr('search')}: {self._search_echo} | " if self._search_echo else ""
        self._pane_status.setText(f"{search}{count} selected | {space}{filtered}")

    # -- internals -------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.FocusIn:
            self.activated_pane.emit(self)
        if obj in (self._view, self._view.viewport()) and event.type() in (
                QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            event.ignore()
            return True
        return super().eventFilter(obj, event)

    def _on_activated(self, index: QModelIndex) -> None:
        if self._model.isDir(index):
            self.navigate(self._model.filePath(index))
        else:
            _open_native(self._model.filePath(index))

    def _report_selection(self) -> None:
        paths = self.selected_paths()
        n = len(paths)
        self._update_pane_status()
        self.selection_changed.emit(paths)
        if n:
            self.status_message.emit(f"{n} selected — {self.current_path()}")

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(_MENU_STYLE)
        paths = self.selected_paths()
        if paths:
            menu.addAction(tr('Open'), lambda: self._on_activated(
                self._view.selectionModel().selectedRows(0)[0]))
            folder = paths[0] if os.path.isdir(paths[0]) else os.path.dirname(paths[0])
            label = "Show in Explorer" if sys.platform == "win32" else "Show in File Manager"
            menu.addAction(label, lambda: _open_native(folder))
            menu.addAction(tr('Copy path\tCtrl+Shift+C'), self._copy_paths_to_clipboard)
            menu.addSeparator()
            menu.addAction(tr('Copy to other pane\tF5'), lambda: self.op_requested.emit("copy"))
            menu.addAction(tr('Move to other pane\tF6'), lambda: self.op_requested.emit("move"))
            menu.addSeparator()
            if len(paths) == 1 and os.path.isfile(paths[0]):
                menu.addAction(tr('Preview'), lambda: self.op_requested.emit("preview"))
                menu.addAction(tr('Calculate SHA-256'), lambda: self.op_requested.emit("checksum"))
                menu.addAction(tr('Verify SHA-256…'), lambda: self.op_requested.emit("verify_checksum"))
                if paths[0].lower().endswith(_ARCHIVE_EXTENSIONS):
                    menu.addAction(tr('Extract safely to other pane'), lambda: self.op_requested.emit("extract"))
            menu.addAction(tr('Create archive…'), lambda: self.op_requested.emit("archive"))
            menu.addSeparator()
            menu.addAction(tr('Rename…\tF2'), lambda: self.op_requested.emit("rename"))
            menu.addAction(tr('Batch Rename…\tCtrl+R'),
                           lambda: self.op_requested.emit("batch_rename"))
            menu.addAction(tr('Move to Recycle Bin\tF8'), lambda: self.op_requested.emit("recycle"))
            menu.addAction(tr('Permanently Delete…\tShift+Delete'),
                           lambda: self.op_requested.emit("permanent_delete"))
            menu.addSeparator()
        menu.addAction(tr('Select Group… (+)'), lambda: self._select_group(True))
        menu.addAction(tr('Deselect Group… (-)'), lambda: self._select_group(False))
        menu.addAction(tr('Invert Selection\tCtrl+I'), self._invert_selection)
        if paths:
            menu.addAction(tr('Select All with Same Extension'), self.select_same_extension)
        menu.addAction(tr('Paste from Commander Clipboard\tCtrl+V'),
                       lambda: self.file_clipboard_request.emit("paste"))
        menu.addSeparator()
        menu.addAction(tr('New Folder…\tF7'), lambda: self.op_requested.emit("mkdir"))
        menu.addAction(tr('New Text File…\tShift+F4'), lambda: self.op_requested.emit("new_file"))
        menu.addAction(tr('Refresh'), lambda: self.navigate(
            self.current_path(), record_history=False))
        menu.exec(self._view.viewport().mapToGlobal(pos))


class CommanderTab(QWidget):
    """Dual-pane file manager tab (Double Commander style)."""

    op_progress = Signal(int, int, str)   # done KiB/items, total, current item
    op_finished = Signal(object, bool, str)  # operation, ok, message
    preview_ready = Signal(str, object)    # request token, bounded preview result
    active_pane_changed = Signal(str)      # "left"/"right" — for the agent bridge
    pane_added = Signal(object, str)       # FilePane, side — for the agent bridge

    def __init__(self, config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._operation_guard = threading.Lock()
        self._active_operation: Optional[_OperationState] = None
        self._progress_dlg: Optional[QProgressDialog] = None
        self._operation_outcomes: List[Dict[str, object]] = []
        self._preview_token = ""
        self._preview_path = ""
        # Commander file clipboard ("copy"/"cut", paths). App-internal on
        # purpose: pasting routes through the same safety controller as
        # F5/F6, which the OS clipboard could not promise.
        self._file_clipboard: Optional[Tuple[str, List[str]]] = None
        raw_favorites = getattr(self._config, "ui_commander_favorites", [])
        self._favorites: List[str] = [
            item for item in raw_favorites
            if isinstance(item, str) and item and "\x00" not in item and len(item) < 400
        ][:100]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        root = self._fs_root()
        left_default = self._restorable_directory(
            getattr(self._config, "default_save_path", ""), root)
        saved_paths = getattr(self._config, "ui_commander_paths", {})
        if not isinstance(saved_paths, dict):
            saved_paths = {}
        # Restore only independently validated directories. Missing drives,
        # malformed values, and stale removable-media paths use safe defaults.
        left_start = self._restorable_directory(saved_paths.get("left"), left_default)
        right_start = self._restorable_directory(saved_paths.get("right"), root)
        # Each SIDE is a closable/movable folder-tab strip; every tab holds a
        # full FilePane (own model so per-tab filter/sort state is kept).
        # Left/right "pane" is the side's CURRENT tab — see the properties.
        self._sides: Dict[str, QTabWidget] = {}
        saved_tabs = getattr(self._config, "ui_commander_tabs", {})
        if not isinstance(saved_tabs, dict):
            saved_tabs = {}
        for side, fallback in (("left", left_start), ("right", right_start)):
            tabs = QTabWidget()
            tabs.setAccessibleName(f"{side.title()} folder tabs")
            tabs.setToolTip(tr('Folder tabs (Ctrl+T new, Ctrl+W close, Ctrl+Tab switch)'))
            tabs.setTabsClosable(True)
            tabs.setMovable(True)
            tabs.setUsesScrollButtons(True)
            add_btn = QPushButton("+")
            add_btn.setAccessibleName(f"{side.title()} new folder tab")
            add_btn.setToolTip(tr('Open a new folder tab (Ctrl+T)'))
            add_btn.setFixedWidth(30)
            add_btn.clicked.connect(lambda _checked=False, s=side: self._new_tab(s))
            tabs.setCornerWidget(add_btn)
            tabs.tabCloseRequested.connect(
                lambda index, s=side: self._close_tab(s, index))
            tabs.currentChanged.connect(
                lambda _index, s=side: self._on_side_tab_changed(s))
            self._sides[side] = tabs
            self.splitter.addWidget(tabs)
            paths = [
                self._restorable_directory(item, fallback)
                for item in (saved_tabs.get(side) or [])[: self._TAB_LIMIT]
                if isinstance(item, str)
            ]
            if not paths:
                paths = [fallback]
            for path in paths:
                self._add_tab(side, path)
            # restore the recorded CURRENT tab of this side
            wanted = saved_paths.get(side)
            if wanted:
                for index in range(tabs.count()):
                    if self._same_path(tabs.widget(index).current_path(), wanted):
                        tabs.setCurrentIndex(index)
                        break
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([1000, 1000])
        layout.addWidget(self.splitter, 1)

        self._preview_stack = QStackedWidget(self)
        self._preview_stack.setAccessibleName("Selected file preview")
        self._preview_stack.setToolTip(tr('Bounded preview of the selected file; media is never played automatically'))
        self._preview_stack.setMaximumHeight(220)
        self._preview_text = QPlainTextEdit(self)
        self._preview_text.setReadOnly(True)
        self._preview_text.setAccessibleName("Selected file text and metadata preview")
        self._preview_text.setPlaceholderText(tr('Select one file to preview'))
        self._preview_image = QLabel(self)
        self._preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_image.setAccessibleName("Selected image preview")
        self._preview_image.setToolTip(tr('Image preview constrained by byte and pixel limits'))
        self._preview_stack.addWidget(self._preview_text)
        self._preview_stack.addWidget(self._preview_image)
        layout.addWidget(self._preview_stack)

        checksum_bar = QHBoxLayout()
        checksum_label = QLabel(tr('SHA-256:'))
        checksum_label.setAccessibleName("SHA-256 result label")
        checksum_bar.addWidget(checksum_label)
        self._checksum_result = QLineEdit()
        self._checksum_result.setReadOnly(True)
        self._checksum_result.setAccessibleName("SHA-256 checksum result")
        self._checksum_result.setToolTip(tr('Calculated SHA-256 checksum'))
        checksum_bar.addWidget(self._checksum_result, 1)
        self._checksum_copy_btn = QPushButton(tr('Copy'))
        self._checksum_copy_btn.setAccessibleName("Copy SHA-256 checksum")
        self._checksum_copy_btn.setToolTip(tr('Copy the checksum to the clipboard'))
        self._checksum_copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self._checksum_result.text()))
        checksum_bar.addWidget(self._checksum_copy_btn)
        layout.addLayout(checksum_bar)

        # Function-key bar (Double Commander style). ResponsiveRow: the six
        # buttons sum to ~770px of minimums — they overflow into a "⋯" menu
        # on narrow windows instead of locking the window wide. The F5–F8
        # shortcuts keep working while a button is in the menu.
        keys_row = ResponsiveRow()
        keys = QHBoxLayout(keys_row)
        keys.setSpacing(6)
        fn_btns = []
        for text, tooltip, slot in (
            ("F5 Copy", "Copy selected items to the other pane",
             lambda: self._start_transfer("copy")),
            ("F6 Move", "Move selected items to the other pane",
             lambda: self._start_transfer("move")),
            ("F7 New Folder", "Create a folder in the active pane", self._new_folder),
            ("F8 Recycle", "Move selected items to the Recycle Bin", self._start_delete),
            ("F2 Rename", "Rename the selected item", self._rename),
            ("Compare", "Compare pane folders by relative name, size, and modification time", self._start_compare),
        ):
            btn = QPushButton(text)
            btn.setAccessibleName(text.replace("F8 Recycle", "Recycle selected items"))
            btn.setToolTip(tooltip)
            btn.clicked.connect(slot)
            keys.addWidget(btn)
            fn_btns.append(btn)
        keys.addStretch()
        self._status = QLabel("")
        self._status.setAccessibleName("Commander status")
        self._status.setToolTip(tr('Current Commander status'))
        self._status.setStyleSheet("color: #8a9ab0; font-size: 17px;")
        shrink_label(self._status)  # live text must not grow the window min
        keys.addWidget(self._status)
        layout.addWidget(keys_row)
        # Hide-first order: Compare/Rename/Recycle go first, Copy/Move last.
        self._fn_overflow = OverflowRow(keys_row, tuple(reversed(fn_btns)))

        self._outcome_status = QLabel(tr('No file operations yet'))
        self._outcome_status.setAccessibleName("File operation history summary")
        self._outcome_status.setToolTip(tr('Recent file operation outcomes'))
        self._outcome_status.setStyleSheet("color: #8a9ab0; font-size: 17px;")
        shrink_label(self._outcome_status)  # live text must not grow the window min
        layout.addWidget(self._outcome_status)

        self._active: FilePane = self.left_pane
        for side in ("left", "right"):
            for index in range(self._sides[side].count()):
                self._wire_pane(self._sides[side].widget(index), side)
        self._apply_active_highlights()

        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for key, slot in (
            ("F5", lambda: self._start_transfer("copy")),
            ("F6", lambda: self._start_transfer("move")),
            ("F7", self._new_folder),
            ("F8", self._start_delete),
            ("Delete", self._start_delete),
            ("Shift+Delete", self._start_permanent_delete),
            ("F2", self._rename),
            ("Shift+F4", self._new_file),
            ("Ctrl+R", self._start_batch_rename_dialog),
            ("Ctrl+T", lambda: self._new_tab(self._side_of(self._active))),
            ("Ctrl+W", self._close_current_tab),
            ("Backspace", lambda: self._active.go_up()),
            ("Alt+Left", lambda: self._active.go_back()),
            ("Alt+Right", lambda: self._active.go_forward()),
            ("Ctrl+F", lambda: self._active._filter_edit.setFocus()),
        ):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(ctx)
            sc.activated.connect(slot)
        # Ctrl+1..9 jump to the first nine favorites in the ACTIVE pane.
        for number in range(1, 10):
            sc = QShortcut(QKeySequence(f"Ctrl+{number}"), self)
            sc.setContext(ctx)
            sc.activated.connect(lambda n=number: self._jump_favorite(n - 1))

        self.op_progress.connect(self._on_op_progress)
        self.op_finished.connect(self._on_op_finished)
        self.preview_ready.connect(self._apply_preview)

    # -- folder tabs ------------------------------------------------------------
    _TAB_LIMIT = 12  # every tab carries its own QFileSystemModel

    @property
    def left_pane(self) -> FilePane:
        pane = self._sides["left"].currentWidget()
        return pane if pane is not None else self._sides["left"].widget(0)

    @property
    def right_pane(self) -> FilePane:
        pane = self._sides["right"].currentWidget()
        return pane if pane is not None else self._sides["right"].widget(0)

    def _wire_pane(self, pane: FilePane, side: str) -> None:
        pane.activated_pane.connect(self._set_active)
        pane.op_requested.connect(self._on_op_requested)
        pane.selection_changed.connect(
            lambda paths, source=pane: self._on_selection_changed(source, paths))
        pane.status_message.connect(self._status.setText)
        pane.favorite_clicked.connect(
            lambda source=pane: self._show_favorites_menu(source))
        pane.file_clipboard_request.connect(
            lambda mode, source=pane: self._on_file_clipboard(source, mode))
        pane.path_changed.connect(
            lambda path, source=pane: self._sync_tab_title(source, path))
        pane.set_favorites_provider(self.commander_favorites)

    def _add_tab(self, side: str, path: str) -> FilePane:
        pane = FilePane(path, pane_name=f"{side.title()} pane")
        tabs = self._sides[side]
        tabs.addTab(pane, os.path.basename(os.path.normpath(path)) or path)
        tabs.setTabToolTip(tabs.indexOf(pane), QDir.toNativeSeparators(path))
        if hasattr(self, "_active"):  # panes created after construction
            self._wire_pane(pane, side)
        self.pane_added.emit(pane, side)
        return pane

    def _new_tab(self, side: str) -> None:
        tabs = self._sides[side]
        if tabs.count() >= self._TAB_LIMIT:
            self._status.setText(tr('Folder tab limit reached'))
            return
        # Classic commander behavior: the new tab duplicates the current one.
        pane = self._add_tab(side, tabs.currentWidget().current_path())
        tabs.setCurrentWidget(pane)
        self._set_active(pane)

    def _close_tab(self, side: str, index: int) -> None:
        tabs = self._sides[side]
        if tabs.count() <= 1:
            self._status.setText(tr('The last folder tab cannot be closed'))
            return
        pane = tabs.widget(index)
        tabs.removeTab(index)
        if pane is not None:
            pane.deleteLater()

    def _close_current_tab(self) -> None:
        side = self._side_of(self._active)
        self._close_tab(side, self._sides[side].currentIndex())

    def _on_side_tab_changed(self, side: str) -> None:
        pane = self._sides[side].currentWidget()
        if pane is None or not hasattr(self, "_active"):
            return
        self._apply_active_highlights()
        # Only a USER-driven tab switch (the strip has focus) moves the
        # active-pane highlight; programmatic tab changes keep the user's
        # active side. _new_tab activates its pane explicitly.
        if self._sides[side].hasFocus():
            self._set_active(pane)

    def _side_of(self, pane: FilePane) -> str:
        for side, tabs in self._sides.items():
            if tabs.indexOf(pane) >= 0:
                return side
        return "left"

    def _sync_tab_title(self, pane: FilePane, path: str) -> None:
        for tabs in self._sides.values():
            index = tabs.indexOf(pane)
            if index >= 0:
                tabs.setTabText(
                    index, os.path.basename(os.path.normpath(path)) or path)
                tabs.setTabToolTip(index, QDir.toNativeSeparators(path))
                break

    def commander_tabs(self) -> Dict[str, List[str]]:
        """Every side's tab folders (current tab first is NOT guaranteed)."""
        return {
            side: [self._sides[side].widget(index).current_path()
                   for index in range(self._sides[side].count())]
            for side in ("left", "right")
        }

    # -- pane coordination -----------------------------------------------------
    @staticmethod
    def _fs_root() -> str:
        """Filesystem root: system drive root on Windows, '/' elsewhere."""
        if sys.platform == "win32":
            drive = os.path.splitdrive(str(Path.home()))[0]
            return (drive + os.sep) if drive else "C:\\"
        return os.sep

    @staticmethod
    def _restorable_directory(value: object, fallback: str) -> str:
        """Return an existing local directory or a known-safe fallback."""
        candidate = value.strip() if isinstance(value, str) else ""
        if not candidate or "\x00" in candidate or len(candidate) > 32767:
            candidate = fallback
        try:
            candidate = os.path.abspath(os.path.expanduser(candidate))
            if os.path.isdir(candidate):
                return candidate
        except (OSError, ValueError):
            pass
        try:
            safe_fallback = os.path.abspath(os.path.expanduser(fallback))
            if os.path.isdir(safe_fallback):
                return safe_fallback
        except (OSError, ValueError):
            pass
        return str(Path.home()) if Path.home().is_dir() else CommanderTab._fs_root()

    def persist_pane_paths(self) -> Dict[str, str]:
        """Copy validated pane locations (and every folder tab) into config."""
        left, right = self.pane_paths()
        paths = {
            "left": self._restorable_directory(left, self._fs_root()),
            "right": self._restorable_directory(right, self._fs_root()),
        }
        self._config.ui_commander_paths = paths
        tabs_state: Dict[str, List[str]] = {}
        for side in ("left", "right"):
            tabs_state[side] = [
                self._restorable_directory(
                    self._sides[side].widget(index).current_path(), self._fs_root())
                for index in range(self._sides[side].count())
            ]
        self._config.ui_commander_tabs = tabs_state
        return dict(paths)

    # -- favorites ---------------------------------------------------------------
    def commander_favorites(self) -> List[str]:
        """Current favorites (paths); the star buttons and jumps read this."""
        return list(self._favorites)

    def _is_favorite(self, path: str) -> bool:
        target = os.path.normcase(os.path.abspath(path))
        return any(os.path.normcase(os.path.abspath(item)) == target
                   for item in self._favorites)

    def _set_favorites(self, favorites: List[str]) -> None:
        self._favorites = favorites[:100]
        self._config.ui_commander_favorites = list(self._favorites)
        for pane in (self.left_pane, self.right_pane):
            pane._update_favorite_star()

    def add_favorite(self, path: str) -> None:
        path = os.path.abspath(path)
        if not self._is_favorite(path):
            self._set_favorites([path] + self._favorites)

    def remove_favorite(self, path: str) -> None:
        target = os.path.normcase(os.path.abspath(path))
        kept = [item for item in self._favorites
                if os.path.normcase(os.path.abspath(item)) != target]
        if len(kept) != len(self._favorites):
            self._set_favorites(kept)

    def _show_favorites_menu(self, pane: FilePane) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(_MENU_STYLE)
        current = pane.current_path()
        if self._is_favorite(current):
            menu.addAction(tr('Remove current folder from favorites'),
                           lambda: self.remove_favorite(current))
        else:
            menu.addAction(tr('Add current folder to favorites'),
                           lambda: self.add_favorite(current))
        menu.addSeparator()
        if not self._favorites:
            menu.addAction(tr('No favorites yet')).setEnabled(False)
        else:
            for favorite in self._favorites[:20]:
                menu.addAction(QDir.toNativeSeparators(favorite),
                               lambda path=favorite: pane.navigate(path))
        menu.exec(pane._fav_btn.mapToGlobal(QPoint(0, pane._fav_btn.height())))

    def _jump_favorite(self, index: int) -> None:
        if 0 <= index < len(self._favorites):
            self._active.navigate(self._favorites[index])
        else:
            self._status.setText(tr('No favorite is stored on this number'))

    # -- commander file clipboard -----------------------------------------------------
    def file_clipboard_state(self) -> Optional[Tuple[str, List[str]]]:
        return (self._file_clipboard[0], list(self._file_clipboard[1])) if self._file_clipboard else None

    def _on_file_clipboard(self, pane: FilePane, mode: str) -> None:
        if mode == "paste":
            self._paste_into(pane)
            return
        paths = pane.selected_paths()
        if not paths:
            self._status.setText(tr('Nothing selected'))
            return
        self._file_clipboard = (mode, paths)
        verb = tr('Cut') if mode == "cut" else tr('Copied')
        action = tr('move') if mode == "cut" else tr('copy')
        self._status.setText(f"{verb} {len(paths)} — {tr('press Ctrl+V in a pane to')} {action}")

    def _paste_into(self, pane: FilePane) -> None:
        if not self._file_clipboard:
            self._status.setText(tr('Commander clipboard is empty'))
            return
        clip_mode, paths = self._file_clipboard
        self._plan_and_launch("move" if clip_mode == "cut" else "copy",
                              list(paths), pane.current_path())

    # -- new file ---------------------------------------------------------------
    def _new_file(self) -> None:
        if self._operation_running():
            self._status.setText(tr('Another file operation is already running'))
            return
        name, ok = QInputDialog.getText(self, tr('New Text File'), tr('File name:'))
        name = name.strip()
        if not ok or not name:
            return
        if (name in (".", "..") or "/" in name or "\\" in name or "\x00" in name):
            QMessageBox.warning(
                self, tr('New Text File'),
                tr('Enter a simple file name without path separators.'))
            return
        target = os.path.join(self._active.current_path(), name)
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(descriptor)
        except OSError as exc:
            QMessageBox.warning(self, tr('New Text File'),
                                tr('Could not create the file:') + f"\n{exc}")
            return
        self._status.setText(f"Created {name}")

    def _set_active(self, pane: FilePane) -> None:
        side = self._side_of(pane)
        if pane is self._active and getattr(self, "_active_side_emitted", "") == side:
            return
        self._active = pane
        self._active_side_emitted = side
        self._apply_active_highlights()
        self._request_preview(pane.selected_paths())
        self.active_pane_changed.emit(side)

    def _apply_active_highlights(self) -> None:
        active_side = self._side_of(self._active)
        for side, tabs in self._sides.items():
            current = tabs.currentWidget()
            if current is not None:
                current.set_active(side == active_side)

    def _other(self, pane: FilePane) -> FilePane:
        side = self._side_of(pane)
        other_tabs = self._sides["right" if side == "left" else "left"]
        pane = other_tabs.currentWidget()
        return pane if pane is not None else other_tabs.widget(0)

    def pane_paths(self) -> Tuple[str, str]:
        """Return pane locations for callers that choose to persist them."""
        return self.left_pane.current_path(), self.right_pane.current_path()

    def set_pane_paths(self, left_path: str, right_path: str) -> None:
        """Restore existing pane locations without coupling this tab to config."""
        self.left_pane.navigate(left_path, record_history=False)
        self.right_pane.navigate(right_path, record_history=False)

    def _on_selection_changed(self, pane: FilePane, paths: List[str]) -> None:
        if pane is self._active:
            self._request_preview(paths)

    def _request_preview(self, paths: List[str]) -> None:
        token = uuid.uuid4().hex
        self._preview_token = token
        self._preview_path = paths[0] if len(paths) == 1 else ""
        self._preview_stack.setCurrentWidget(self._preview_text)
        self._preview_image.clear()
        if len(paths) != 1:
            message = "Select one file to preview" if not paths else "Preview is available for one file at a time"
            self._preview_text.setPlainText(message)
            return
        path = paths[0]
        self._preview_text.setPlainText(f"Loading preview…\n{path}")
        ffmpeg_path = getattr(getattr(self._config, "download", None), "ffmpeg_path", "")

        def worker() -> None:
            result = self._build_preview(path, ffmpeg_path)
            self.preview_ready.emit(token, result)

        threading.Thread(target=worker, daemon=True).start()

    def _preview_selected(self) -> None:
        self._request_preview(self._active.selected_paths())

    @classmethod
    def _build_preview(cls, path: str, ffmpeg_path: str = "") -> Dict[str, object]:
        """Build a bounded, display-neutral preview result off the GUI thread."""
        try:
            if not os.path.isfile(path) or cls._is_boundary(path):
                return {"kind": "state", "text": "Preview unavailable: select a regular file."}
            size = os.path.getsize(path)
            suffix = Path(path).suffix.lower()
            reader = QImageReader(path)
            if reader.canRead():
                if size > _PREVIEW_IMAGE_BYTES:
                    return {"kind": "state", "text": (
                        f"Image is too large to preview ({FilePane._format_bytes(size)}; "
                        f"limit {FilePane._format_bytes(_PREVIEW_IMAGE_BYTES)}).")}
                dimensions = reader.size()
                pixels = max(0, dimensions.width()) * max(0, dimensions.height())
                if not dimensions.isValid() or pixels > _PREVIEW_IMAGE_PIXELS:
                    return {"kind": "state", "text": (
                        f"Image dimensions are unavailable or too large "
                        f"(limit {_PREVIEW_IMAGE_PIXELS:,} pixels).")}
                image = reader.read()
                if image.isNull():
                    return {"kind": "state", "text": "Image preview could not be decoded safely."}
                return {"kind": "image", "image": image,
                        "text": f"{dimensions.width()} × {dimensions.height()} pixels — {FilePane._format_bytes(size)}"}

            if suffix in _MEDIA_EXTENSIONS:
                from dlmgr.ffmpeg import find_ffmpeg, find_ffprobe, probe_duration

                ffmpeg = find_ffmpeg(ffmpeg_path)
                duration = probe_duration(path, find_ffprobe(ffmpeg), timeout=5.0)
                lines = ["Media metadata (not playing)", f"Name: {os.path.basename(path)}",
                         f"Size: {FilePane._format_bytes(size)}", f"Type: {suffix or 'unknown'}"]
                if duration > 0:
                    minutes, seconds = divmod(int(duration), 60)
                    hours, minutes = divmod(minutes, 60)
                    lines.append(f"Duration: {hours:02d}:{minutes:02d}:{seconds:02d}")
                else:
                    lines.append("Duration: unavailable")
                return {"kind": "text", "text": "\n".join(lines)}

            with open(path, "rb") as source:
                data = source.read(_PREVIEW_TEXT_BYTES + 1)
            truncated = len(data) > _PREVIEW_TEXT_BYTES
            data = data[:_PREVIEW_TEXT_BYTES]
            looks_text = suffix in _TEXT_EXTENSIONS or not data or b"\x00" not in data[:8192]
            if not looks_text:
                return {"kind": "state", "text": (
                    f"Unsupported binary file ({FilePane._format_bytes(size)}). "
                    "Use Open to view it with an associated application.")}
            if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                text = data.decode("utf-16", errors="replace")
            else:
                text = data.decode("utf-8-sig", errors="replace")
            if truncated:
                text += (f"\n\n[Preview truncated at "
                         f"{FilePane._format_bytes(_PREVIEW_TEXT_BYTES)} of {FilePane._format_bytes(size)}]")
            return {"kind": "text", "text": text}
        except (OSError, ValueError) as exc:
            return {"kind": "state", "text": f"Preview unavailable: {exc}"}

    def _apply_preview(self, token: str, result: Dict[str, object]) -> None:
        """Apply only the newest asynchronous request; stale workers are ignored."""
        if token != self._preview_token:
            return
        kind = result.get("kind")
        if kind == "image" and isinstance(result.get("image"), QImage):
            image = result["image"]
            pixmap = QPixmap.fromImage(image).scaled(
                800, 180, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self._preview_image.setPixmap(pixmap)
            self._preview_image.setToolTip(str(result.get("text", "Image preview")))
            self._preview_stack.setCurrentWidget(self._preview_image)
        else:
            self._preview_text.setPlainText(str(result.get("text", "Preview unavailable")))
            self._preview_stack.setCurrentWidget(self._preview_text)

    def operation_outcomes(self) -> List[Dict[str, object]]:
        """Return a copy of the bounded operation outcome history."""
        return [dict(outcome) for outcome in self._operation_outcomes]

    def operation_history_summary(self) -> str:
        return self._outcome_status.text()

    def _on_op_requested(self, mode: str) -> None:
        if mode in ("delete", "recycle"):
            self._start_delete()
        elif mode == "permanent_delete":
            self._start_permanent_delete()
        elif mode == "mkdir":
            self._new_folder()
        elif mode == "new_file":
            self._new_file()
        elif mode == "rename":
            self._rename()
        elif mode == "batch_rename":
            self._start_batch_rename_dialog()
        elif mode == "preview":
            self._preview_selected()
        elif mode == "checksum":
            self._start_checksum()
        elif mode == "verify_checksum":
            self._start_checksum(verify=True)
        elif mode == "archive":
            self._start_archive_create()
        elif mode == "extract":
            self._start_archive_extract()
        else:
            self._start_transfer(mode)

    def _new_folder(self) -> None:
        if self._operation_running():
            self._status.setText(tr('Another file operation is already running'))
            return
        self._active.new_folder()

    def _rename(self) -> None:
        if self._operation_running():
            self._status.setText(tr('Another file operation is already running'))
            return
        self._active.rename_selected()

    # -- batch rename --------------------------------------------------------------
    def _start_batch_rename_dialog(self) -> None:
        if self._operation_running():
            self._status.setText(tr('Another file operation is already running'))
            return
        paths = self._active.selected_paths()
        if not paths:
            self._status.setText(tr('Nothing selected'))
            return
        from gui.commander_rename import BatchRenameDialog
        dialog = BatchRenameDialog(self, paths)
        if (dialog.exec() == QDialog.DialogCode.Accepted and dialog.plans):
            self._start_batch_rename(dialog.plans)

    def _start_batch_rename(self, plans) -> None:
        """Launch one serialized, rollback-journaled rename operation."""
        if self._operation_running():
            self._status.setText(tr('Another file operation is already running'))
            return
        pairs = [(item.source, item.target) for item in plans
                 if item.target and os.path.abspath(item.source) != os.path.abspath(item.target)]
        if not pairs:
            self._status.setText(tr('Nothing selected'))
            return
        self._launch_worker("batch_rename", [src for src, _dst in pairs], "", False,
                            options={"plans": pairs})

    # -- file operations ---------------------------------------------------------
    def _operation_running(self) -> bool:
        with self._operation_guard:
            return self._active_operation is not None

    def _start_transfer(self, mode: str) -> None:
        paths = self._active.selected_paths()
        if not paths:
            self._status.setText(tr('Nothing selected'))
            return
        self._plan_and_launch(mode, paths, self._other(self._active).current_path())

    def _plan_and_launch(self, mode: str, paths: List[str], dst_dir: str,
                         conflict_policy: str = "ask") -> None:
        """Resolve per-item conflicts, then launch one serialized operation.

        conflict_policy is "ask" (interactive prompt per item) or a fixed
        answer for headless callers: "skip" | "overwrite" | "keep_both".
        """
        if self._operation_running():
            self._status.setText(tr('Another file operation is already running'))
            return
        if not paths:
            self._status.setText(tr('Nothing selected'))
            return
        if mode == "move":
            same_dir = [path for path in paths
                        if self._same_path(os.path.dirname(path), dst_dir)]
            if len(same_dir) == len(paths):
                self._status.setText(tr('Source and destination are the same'))
                return
            paths = [path for path in paths if path not in same_dir]

        target_plan = self._plan_conflicts(paths, dst_dir, conflict_policy)
        if target_plan is None:
            return  # the user cancelled the batch mid-dialog
        work_paths = list(target_plan)
        if not work_paths:
            self._status.setText(tr('All conflicting items were skipped'))
            return
        self._launch_worker(mode, work_paths, dst_dir, False, target_plan)

    def _plan_conflicts(self, paths: List[str], dst_dir: str,
                        conflict_policy: str) -> Optional[Dict[str, Tuple[str, bool]]]:
        """Resolve per-item conflicts into a target plan.

        conflict_policy is "ask" (interactive prompt per item; None return
        means the user cancelled the whole batch) or a fixed answer used by
        headless callers: "skip" | "overwrite" | "keep_both".
        """
        target_plan: Dict[str, Tuple[str, bool]] = {}
        reserved: Set[str] = set()
        for src in paths:
            dst = os.path.join(dst_dir, os.path.basename(src))
            overwrite = False
            if os.path.lexists(dst):
                choice = (self._prompt_conflict(src, dst) if conflict_policy == "ask"
                          else conflict_policy)
                if choice == "cancel":
                    return None
                if choice == "skip":
                    continue
                if choice == "keep_both":
                    dst = self._keep_both_path(dst, reserved)
                else:
                    overwrite = True
            target_plan[src] = (dst, overwrite)
            reserved.add(os.path.normcase(os.path.abspath(dst)))
        return target_plan

    def run_headless_transfer(self, mode: str, paths: List[str], dst_dir: str,
                              conflict_policy: str) -> Dict[str, object]:
        """Agent-driven copy/move: same serialized controller, no dialogs.

        Runs the operation on the CALLING thread (an agent worker); the guard
        is released synchronously in `finally` so a follow-up call in the
        same approved batch can never see a phantom "already running", and
        the outcome is recorded inline. The queued op_finished signal only
        updates the GUI (status line, progress reset) — by the time the GUI
        processes it the operation is no longer the active one, which
        _on_op_finished tolerates by design.
        """
        if conflict_policy not in ("skip", "overwrite", "keep_both"):
            return {"ok": False, "error": "conflict_policy must be skip, overwrite, or keep_both"}
        if mode == "move" and all(
                self._same_path(os.path.dirname(path), dst_dir) for path in paths):
            return {"ok": False, "error": "source and destination are the same"}
        operation = _OperationState(mode, item_count=len(paths), quiet=True)
        with self._operation_guard:
            if self._active_operation is not None:
                return {"ok": False, "error": "another file operation is already running"}
            self._active_operation = operation
        ok = False
        message = ""
        work_paths: List[str] = []
        try:
            target_plan = self._plan_conflicts(paths, dst_dir, conflict_policy)
            work_paths = list(target_plan or {})
            if not work_paths:
                message = "all conflicting items were skipped"
                ok = True
            else:
                message = self._execute_operation(
                    operation, work_paths, dst_dir, False, target_plan)
                ok = True
        except Exception as exc:  # noqa: BLE001 — reported verbatim to the agent
            logger.warning("headless %s failed: %s", mode, exc)
            message = str(exc)
            ok = False
        finally:
            with self._operation_guard:
                if self._active_operation is operation:
                    self._active_operation = None
            self._append_outcome(operation, ok, message)
            try:
                self.op_finished.emit(operation, ok, message)
            except RuntimeError:
                pass  # tab torn down during shutdown
        return {"ok": ok, "message": message, "items": len(work_paths)}

    def _prompt_conflict(self, src: str, dst: str) -> str:
        """Ask for a safe decision for one conflict, never a whole batch."""
        box = QMessageBox(self)
        box.setWindowTitle(tr('Destination item exists'))
        box.setAccessibleName("File conflict options")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"Choose what to do with:\n{QDir.toNativeSeparators(os.path.abspath(src))}\n\n"
            f"Existing destination:\n{QDir.toNativeSeparators(os.path.abspath(dst))}")
        overwrite_btn = box.addButton(tr('Overwrite'), QMessageBox.ButtonRole.DestructiveRole)
        keep_btn = box.addButton(tr('Keep Both'), QMessageBox.ButtonRole.AcceptRole)
        skip_btn = box.addButton(tr('Skip'), QMessageBox.ButtonRole.NoRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        overwrite_btn.setToolTip(tr('Replace this destination item transactionally'))
        keep_btn.setToolTip(tr('Copy or move this item under a unique name'))
        skip_btn.setToolTip(tr('Leave both source and destination unchanged'))
        cancel_btn.setToolTip(tr('Cancel the entire operation'))
        box.exec()
        clicked = box.clickedButton()
        if clicked is overwrite_btn:
            return "overwrite"
        if clicked is keep_btn:
            return "keep_both"
        if clicked is skip_btn:
            return "skip"
        return "cancel"

    @staticmethod
    def _keep_both_path(path: str, reserved: Optional[Set[str]] = None) -> str:
        """Return a human-readable unique sibling such as ``name (copy 2).ext``."""
        reserved = reserved or set()
        parent = os.path.dirname(path)
        name = os.path.basename(path)
        if os.path.isdir(path):
            stem, suffix = name, ""
        else:
            stem, suffix = os.path.splitext(name)
        number = 1
        while True:
            marker = " (copy)" if number == 1 else f" (copy {number})"
            candidate = os.path.join(parent, f"{stem}{marker}{suffix}")
            normalized = os.path.normcase(os.path.abspath(candidate))
            if not os.path.lexists(candidate) and normalized not in reserved:
                return candidate
            number += 1

    def _selected_for_delete(self) -> Optional[List[str]]:
        if self._operation_running():
            self._status.setText(tr('Another file operation is already running'))
            return None
        paths = self._active.selected_paths()
        if not paths:
            self._status.setText(tr('Nothing selected'))
            return None
        return paths

    @staticmethod
    def _display_paths(paths: List[str]) -> str:
        return "\n".join(
            QDir.toNativeSeparators(os.path.abspath(path)) for path in paths)

    def _start_delete(self) -> None:
        """Default delete: move selected items to the platform trash."""
        paths = self._selected_for_delete()
        if not paths:
            return
        display_paths = self._display_paths(paths)
        r = QMessageBox.question(
            self, tr('Move to Recycle Bin'),
            "Move these paths to the Recycle Bin (Trash on this platform)?\n\n"
            f"{display_paths}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if r != QMessageBox.StandardButton.Yes:
            return
        self._launch_worker("recycle", paths, "", False)

    def _start_permanent_delete(self) -> None:
        """Permanent deletion requires retyping every displayed absolute path."""
        paths = self._selected_for_delete()
        if not paths:
            return
        display_paths = self._display_paths(paths)
        typed, ok = QInputDialog.getMultiLineText(
            self, tr('Permanently Delete'),
            "This cannot be undone. Type every exact path below, one per line, "
            f"to permanently delete:\n\n{display_paths}\n\nExact paths:", "")
        if not ok:
            return
        if typed.strip() != display_paths:
            QMessageBox.warning(
                self, tr('Permanent Delete Not Confirmed'),
                tr('The entered paths did not exactly match. Nothing was deleted.'))
            return
        self._launch_worker("delete", paths, "", False)

    def _start_checksum(self, verify: bool = False) -> None:
        if self._operation_running():
            self._status.setText(tr('Another file operation is already running'))
            return
        paths = self._active.selected_paths()
        if len(paths) != 1 or not os.path.isfile(paths[0]):
            self._status.setText(tr('Select exactly one regular file'))
            return
        expected = ""
        if verify:
            expected, ok = QInputDialog.getText(
                self, tr('Verify SHA-256'), tr('Expected 64-character SHA-256 checksum:'))
            expected = expected.strip().lower()
            if not ok:
                return
            if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
                QMessageBox.warning(self, tr('Verify SHA-256'), tr('Enter exactly 64 hexadecimal characters.'))
                return
        self._launch_worker("verify_checksum" if verify else "checksum", paths, "", False,
                            options={"expected": expected})

    def _start_archive_create(self) -> None:
        if self._operation_running():
            self._status.setText(tr('Another file operation is already running'))
            return
        paths = self._active.selected_paths()
        if not paths:
            self._status.setText(tr('Nothing selected'))
            return
        suggested = os.path.join(self._other(self._active).current_path(),
                                 os.path.basename(paths[0]) + ".zip")
        target, _selected = QFileDialog.getSaveFileName(
            self, "Create Archive", suggested,
            "ZIP archive (*.zip);;Tar gzip archive (*.tar.gz);;Tar archive (*.tar);;Tar bzip2 archive (*.tar.bz2);;Tar xz archive (*.tar.xz)")
        if not target:
            return
        if os.path.lexists(target):
            QMessageBox.warning(self, tr('Create Archive'), tr('For safety, existing archives are never overwritten.'))
            return
        if not self._archive_format(target):
            QMessageBox.warning(self, tr('Create Archive'), tr('Use .zip, .tar, .tar.gz, .tar.bz2, or .tar.xz.'))
            return
        self._launch_worker("archive_create", paths, target, False)

    def _start_archive_extract(self) -> None:
        if self._operation_running():
            self._status.setText(tr('Another file operation is already running'))
            return
        paths = self._active.selected_paths()
        if len(paths) != 1 or not os.path.isfile(paths[0]):
            self._status.setText(tr('Select exactly one archive'))
            return
        archive = paths[0]
        if not self._archive_format(archive):
            self._status.setText(tr('Unsupported archive format'))
            return
        destination = os.path.join(
            self._other(self._active).current_path(), self._archive_stem(archive))
        if os.path.lexists(destination):
            QMessageBox.warning(
                self, tr('Extract Archive'),
                f"Destination already exists and will not be overwritten:\n{destination}")
            return
        self._launch_worker("archive_extract", [archive], destination, False)

    def _start_compare(self) -> None:
        if self._operation_running():
            self._status.setText(tr('Another file operation is already running'))
            return
        self._launch_worker("compare", list(self.pane_paths()), "", False)

    def _launch_worker(self, mode: str, paths: List[str], dst_dir: str,
                       overwrite: bool,
                       target_plan: Optional[Dict[str, Tuple[str, bool]]] = None,
                       options: Optional[Dict[str, object]] = None) -> bool:
        operation = _OperationState(mode, item_count=len(paths), options=dict(options or {}))
        with self._operation_guard:
            if self._active_operation is not None:
                self._status.setText(tr('Another file operation is already running'))
                return False
            self._active_operation = operation
        self._progress_dlg = QProgressDialog("Preparing…", "Cancel", 0, 100, self)
        self._progress_dlg.setAccessibleName("File operation progress")
        self._progress_dlg.setWindowTitle({
            "copy": "Copying", "move": "Moving", "recycle": "Recycling",
            "delete": "Permanently Deleting", "checksum": "Calculating SHA-256",
            "verify_checksum": "Verifying SHA-256", "archive_create": "Creating Archive",
            "archive_extract": "Extracting Archive", "compare": "Comparing Folders",
            "batch_rename": "Renaming Items",
        }.get(mode, "File Operation"))
        self._progress_dlg.setMinimumDuration(400)
        self._progress_dlg.setAutoClose(True)
        self._progress_dlg.canceled.connect(operation.cancel.set)
        threading.Thread(
            target=self._worker,
            args=(operation, list(paths), dst_dir, overwrite, target_plan),
            daemon=True,
        ).start()
        return True

    # -- worker thread ----------------------------------------------------------
    def _worker(self, operation: _OperationState, paths: List[str],
                dst_dir: str, overwrite: bool,
                target_plan: Optional[Dict[str, Tuple[str, bool]]] = None) -> None:
        try:
            message = self._execute_operation(
                operation, paths, dst_dir, overwrite, target_plan)
            self.op_finished.emit(operation, True, message)
        except _Cancelled:
            self.op_finished.emit(operation, False, "Cancelled")
        except Exception as exc:
            logger.warning("commander %s failed: %s", operation.mode, exc)
            self.op_finished.emit(operation, False, str(exc))

    def _execute_operation(self, operation: _OperationState, paths: List[str],
                           dst_dir: str, overwrite: bool,
                           target_plan: Optional[Dict[str, Tuple[str, bool]]] = None) -> str:
        mode = operation.mode
        if mode in ("checksum", "verify_checksum"):
            digest = self._calculate_checksum(paths[0], operation)
            operation.result = digest
            expected = str(operation.options.get("expected", "")).lower()
            if mode == "verify_checksum":
                if digest.lower() != expected:
                    raise OSError(f"SHA-256 mismatch\nExpected: {expected}\nActual:   {digest}")
                return f"SHA-256 verified: {digest}"
            return f"SHA-256 complete: {digest}"
        if mode == "archive_create":
            self._create_archive(paths, dst_dir, operation)
            operation.result = dst_dir
            return f"Archive created: {dst_dir}"
        if mode == "archive_extract":
            self._extract_archive(paths[0], dst_dir, operation)
            operation.result = dst_dir
            return f"Archive extracted: {dst_dir}"
        if mode == "compare":
            summary = self._compare_folders(paths[0], paths[1], operation)
            operation.result = summary
            return str(summary["summary"])
        if mode == "batch_rename":
            plans = [tuple(pair) for pair in operation.options.get("plans", [])]
            renamed = self._batch_rename(plans, operation)
            operation.result = renamed
            return f"Batch rename complete ({renamed} items)"
        if mode in ("delete", "recycle"):
            # Deletion progress is item based. Walking every directory merely to
            # calculate bytes doubles the traversal and delays deletion/cancel.
            total = max(1, len(paths))
            for done, src in enumerate(paths):
                self._check_cancel(operation)
                self.op_progress.emit(done, total, src)
                if mode == "recycle":
                    self._recycle(src)
                else:
                    self._remove(src)
                self.op_progress.emit(done + 1, total, src)
            return "Moved to Recycle Bin" if mode == "recycle" else "Permanent delete complete"

        sizes = [self._size_of(path, operation) for path in paths]
        total_kb = max(1, sum(sizes) // 1024)
        done_kb = 0
        for src, size in zip(paths, sizes):
            self._check_cancel(operation)
            dst, item_overwrite = ((target_plan or {}).get(
                src, (os.path.join(dst_dir, os.path.basename(src)), overwrite)))
            if self._same_path(src, dst):
                continue
            if (not self._is_boundary(src) and os.path.isdir(src)
                    and self._is_inside(dst, src)):
                raise OSError(f"Cannot copy '{src}' into itself")
            if os.path.lexists(dst) and not item_overwrite:
                continue
            previous_kb = done_kb
            if mode == "move":
                done_kb = self._move_transactional(
                    src, dst, done_kb, total_kb, operation, item_overwrite)
            else:
                done_kb, _committed = self._copy_transactional(
                    src, dst, done_kb, total_kb, operation, item_overwrite)
            if done_kb == previous_kb:  # fast rename or a sub-KiB item
                done_kb += max(1, size // 1024)
            done_kb = min(done_kb, total_kb)
            self.op_progress.emit(done_kb, total_kb, src)
        return "Move complete" if mode == "move" else "Copy complete"

    @staticmethod
    def _check_cancel(operation: _OperationState) -> None:
        if operation.cancel.is_set():
            raise _Cancelled

    def _calculate_checksum(self, path: str, operation: _OperationState) -> str:
        if self._is_boundary(path) or not os.path.isfile(path):
            raise OSError("Checksums require a regular file")
        total = max(1, (os.path.getsize(path) + 1023) // 1024)
        done = 0
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            while True:
                self._check_cancel(operation)
                chunk = source.read(_CHECKSUM_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                done += len(chunk)
                self.op_progress.emit(min(total, done // 1024), total, path)
        self._check_cancel(operation)
        self.op_progress.emit(total, total, path)
        return digest.hexdigest()

    @staticmethod
    def _archive_format(path: str) -> str:
        lower = path.lower()
        for suffix, archive_format in (
            (".tar.gz", "tar:gz"), (".tgz", "tar:gz"),
            (".tar.bz2", "tar:bz2"), (".tbz2", "tar:bz2"),
            (".tar.xz", "tar:xz"), (".txz", "tar:xz"),
            (".tar", "tar:"), (".zip", "zip"),
        ):
            if lower.endswith(suffix):
                return archive_format
        return ""

    @staticmethod
    def _archive_stem(path: str) -> str:
        name = os.path.basename(path)
        lower = name.lower()
        for suffix in _ARCHIVE_EXTENSIONS:
            if lower.endswith(suffix):
                return name[:-len(suffix)] or "extracted"
        return os.path.splitext(name)[0] or "extracted"

    def _archive_entries(self, paths: List[str], operation: _OperationState,
                         target: str) -> Tuple[List[Tuple[str, str, bool, int]], int]:
        entries: List[Tuple[str, str, bool, int]] = []
        total = 0
        roots: Set[str] = set()

        def add(source: str, archive_name: str) -> None:
            nonlocal total
            self._check_cancel(operation)
            if self._is_boundary(source):
                raise OSError(f"Refusing to archive symlink or reparse point: {source}")
            info = os.lstat(source)
            is_dir = stat.S_ISDIR(info.st_mode)
            if not is_dir and not stat.S_ISREG(info.st_mode):
                raise OSError(f"Refusing to archive special file: {source}")
            size = 0 if is_dir else info.st_size
            entries.append((source, archive_name.replace(os.sep, "/"), is_dir, size))
            total += size
            if len(entries) > _ARCHIVE_MAX_ENTRIES or total > _ARCHIVE_MAX_BYTES:
                raise OSError("Archive exceeds the safe entry-count or uncompressed-size limit")
            if is_dir:
                with os.scandir(source) as children:
                    for child in sorted(children, key=lambda item: item.name.lower()):
                        add(child.path, f"{archive_name}/{child.name}")

        for source in paths:
            source = os.path.abspath(source)
            if not os.path.lexists(source):
                raise OSError(f"Source no longer exists: {source}")
            if os.path.isdir(source) and self._is_inside(target, source):
                raise OSError("Archive destination cannot be inside a selected source folder")
            root_name = os.path.basename(os.path.normpath(source))
            normalized = os.path.normcase(root_name)
            if not root_name or normalized in roots:
                raise OSError("Selected items must have unique non-empty names")
            roots.add(normalized)
            add(source, root_name)
        return entries, total

    def _create_archive(self, paths: List[str], target: str,
                        operation: _OperationState) -> None:
        archive_format = self._archive_format(target)
        if not archive_format:
            raise OSError("Unsupported archive format")
        target = os.path.abspath(target)
        if os.path.lexists(target):
            raise FileExistsError("Archive destination already exists")
        entries, total_bytes = self._archive_entries(paths, operation, target)
        total_kb = max(1, (total_bytes + 1023) // 1024)
        staged = self._unique_sibling(target, "tmp")
        done = 0

        def progressed(count: int, name: str) -> None:
            nonlocal done
            self._check_cancel(operation)
            done += count
            if done > _ARCHIVE_MAX_BYTES:
                raise OSError("Archive source grew beyond the safety limit")
            self.op_progress.emit(min(total_kb, done // 1024), total_kb, name)

        try:
            if archive_format == "zip":
                with zipfile.ZipFile(staged, "x", compression=zipfile.ZIP_DEFLATED,
                                     allowZip64=True) as archive:
                    for source_path, archive_name, is_dir, _size in entries:
                        self._check_cancel(operation)
                        if is_dir:
                            archive.writestr(archive_name.rstrip("/") + "/", b"")
                            continue
                        with open(source_path, "rb") as source, archive.open(archive_name, "w") as output:
                            while True:
                                self._check_cancel(operation)
                                chunk = source.read(_COPY_CHUNK)
                                if not chunk:
                                    break
                                output.write(chunk)
                                progressed(len(chunk), source_path)
            else:
                mode = "w" + archive_format[3:]
                with tarfile.open(staged, mode, dereference=False) as archive:
                    for source_path, archive_name, is_dir, _size in entries:
                        self._check_cancel(operation)
                        info = archive.gettarinfo(source_path, archive_name)
                        if is_dir:
                            archive.addfile(info)
                            continue
                        with open(source_path, "rb") as source:
                            wrapped = _CancellableReader(
                                source, lambda count, name=source_path: progressed(count, name))
                            archive.addfile(info, wrapped)
            self._check_cancel(operation)
            if os.path.lexists(target):
                raise FileExistsError("Archive destination appeared during creation")
            os.replace(staged, target)
            staged = ""
            self.op_progress.emit(total_kb, total_kb, target)
        finally:
            if staged and os.path.lexists(staged):
                self._cleanup(staged)

    @staticmethod
    def _safe_member_parts(name: str) -> Tuple[str, ...]:
        if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
            raise OSError("Archive contains an invalid member path")
        path = PurePosixPath(name)
        parts = tuple(part for part in path.parts if part not in ("", "."))
        if path.is_absolute() or not parts or ".." in parts or ":" in parts[0]:
            raise OSError(f"Archive path traversal rejected: {name}")
        return parts

    def _extract_archive(self, archive_path: str, destination: str,
                         operation: _OperationState) -> None:
        archive_format = self._archive_format(archive_path)
        if not archive_format:
            raise OSError("Unsupported archive format")
        destination = os.path.abspath(destination)
        if os.path.lexists(destination):
            raise FileExistsError("Extraction destination already exists")
        staged = self._unique_sibling(destination, "tmp")
        os.mkdir(staged)
        done = 0

        def validate_limits(count: int, total: int) -> None:
            if count > _ARCHIVE_MAX_ENTRIES:
                raise OSError("Archive has too many entries")
            if total > _ARCHIVE_MAX_BYTES:
                raise OSError("Archive uncompressed size exceeds the safety limit")

        def target_for(name: str, seen: Set[str]) -> str:
            parts = self._safe_member_parts(name)
            key = os.path.normcase(os.path.join(*parts))
            if key in seen:
                raise OSError(f"Archive contains duplicate output path: {name}")
            seen.add(key)
            target = os.path.abspath(os.path.join(staged, *parts))
            if os.path.commonpath([target, os.path.abspath(staged)]) != os.path.abspath(staged):
                raise OSError(f"Archive path traversal rejected: {name}")
            return target

        try:
            if archive_format == "zip":
                with zipfile.ZipFile(archive_path, "r") as archive:
                    members = archive.infolist()
                    validate_limits(len(members), sum(max(0, item.file_size) for item in members))
                    seen: Set[str] = set()
                    plans = []
                    for item in members:
                        self._check_cancel(operation)
                        mode = (item.external_attr >> 16) & 0xFFFF
                        file_type = stat.S_IFMT(mode)
                        if stat.S_ISLNK(mode) or (file_type and not (
                                stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                            raise OSError(f"Archive link or special entry rejected: {item.filename}")
                        if item.external_attr & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                            raise OSError(f"Archive reparse entry rejected: {item.filename}")
                        plans.append((item, target_for(item.filename, seen)))
                    total_kb = max(1, (sum(item.file_size for item in members) + 1023) // 1024)
                    for item, target in plans:
                        self._check_cancel(operation)
                        if item.is_dir():
                            os.makedirs(target, exist_ok=True)
                            continue
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with archive.open(item, "r") as source, open(target, "xb") as output:
                            while True:
                                self._check_cancel(operation)
                                chunk = source.read(_COPY_CHUNK)
                                if not chunk:
                                    break
                                done += len(chunk)
                                if done > _ARCHIVE_MAX_BYTES:
                                    raise OSError("Archive expanded beyond the safety limit")
                                output.write(chunk)
                                self.op_progress.emit(min(total_kb, done // 1024), total_kb, item.filename)
            else:
                with tarfile.open(archive_path, "r:*") as archive:
                    members = archive.getmembers()
                    validate_limits(len(members), sum(max(0, item.size) for item in members))
                    seen = set()
                    plans = []
                    for item in members:
                        self._check_cancel(operation)
                        if item.issym() or item.islnk() or not (item.isdir() or item.isfile()):
                            raise OSError(f"Archive link or special entry rejected: {item.name}")
                        plans.append((item, target_for(item.name, seen)))
                    total_kb = max(1, (sum(item.size for item in members) + 1023) // 1024)
                    for item, target in plans:
                        self._check_cancel(operation)
                        if item.isdir():
                            os.makedirs(target, exist_ok=True)
                            continue
                        source = archive.extractfile(item)
                        if source is None:
                            raise OSError(f"Could not read archive member: {item.name}")
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with source, open(target, "xb") as output:
                            while True:
                                self._check_cancel(operation)
                                chunk = source.read(_COPY_CHUNK)
                                if not chunk:
                                    break
                                done += len(chunk)
                                if done > _ARCHIVE_MAX_BYTES:
                                    raise OSError("Archive expanded beyond the safety limit")
                                output.write(chunk)
                                self.op_progress.emit(min(total_kb, done // 1024), total_kb, item.name)
                        os.chmod(target, item.mode & 0o777)
            self._check_cancel(operation)
            if os.path.lexists(destination):
                raise FileExistsError("Extraction destination appeared during extraction")
            os.replace(staged, destination)
            staged = ""
        finally:
            if staged and os.path.lexists(staged):
                self._cleanup(staged)

    def _folder_manifest(self, root: str, operation: _OperationState) -> Dict[str, Tuple[str, int, int]]:
        manifest: Dict[str, Tuple[str, int, int]] = {}
        stack = [(os.path.abspath(root), "")]
        while stack:
            folder, relative = stack.pop()
            self._check_cancel(operation)
            with os.scandir(folder) as entries:
                for entry in entries:
                    self._check_cancel(operation)
                    rel = os.path.join(relative, entry.name)
                    info = entry.stat(follow_symlinks=False)
                    boundary = self._is_boundary(entry.path)
                    if entry.is_dir(follow_symlinks=False) and not boundary:
                        kind, size = "directory", 0
                        stack.append((entry.path, rel))
                    elif entry.is_file(follow_symlinks=False) and not boundary:
                        kind, size = "file", info.st_size
                    else:
                        kind, size = "boundary", 0
                    manifest[os.path.normcase(rel)] = (kind, size, info.st_mtime_ns)
                    if len(manifest) > _ARCHIVE_MAX_ENTRIES:
                        raise OSError("Folder comparison exceeds the entry-count limit")
                    self.op_progress.emit(len(manifest), _ARCHIVE_MAX_ENTRIES, rel)
        return manifest

    def _batch_rename(self, plans: List[Tuple[str, str]],
                      operation: _OperationState) -> int:
        """Apply a validated rename plan with a rollback journal.

        Re-validates every target at execution time (the plan was computed
        before the worker started; the disk may have changed since). A
        failure or cancellation rolls every completed rename back in
        reverse order — a half-applied batch is never left behind.
        """
        journal: List[Tuple[str, str]] = []
        total = max(1, len(plans))
        try:
            for index, (src, dst) in enumerate(plans):
                self._check_cancel(operation)
                self.op_progress.emit(index, total, src)
                if not os.path.lexists(src):
                    raise OSError(f"Source disappeared: {src}")
                if os.path.lexists(dst) and not self._same_path(src, dst):
                    raise OSError(f"Destination already exists: {dst}")
                # exact (case-sensitive) comparison: a case-only rename IS a
                # change and must run; normcase would silently skip it
                if os.path.abspath(src) != os.path.abspath(dst):
                    os.rename(src, dst)
                    journal.append((src, dst))
                self.op_progress.emit(index + 1, total, dst)
        except BaseException:
            stuck: List[str] = []
            for prev_src, prev_dst in reversed(journal):
                try:
                    os.rename(prev_dst, prev_src)
                except OSError as exc:
                    logger.warning("rollback stuck for %s: %s", prev_dst, exc)
                    stuck.append(f"'{prev_dst}' (was '{prev_src}')")
            if stuck:
                raise OSError(
                    "Batch rename failed and could not fully roll back; still "
                    "renamed: " + "; ".join(stuck))
            raise
        return len(journal)

    def _compare_folders(self, left: str, right: str,
                         operation: _OperationState) -> Dict[str, object]:
        left_items = self._folder_manifest(left, operation)
        right_items = self._folder_manifest(right, operation)
        left_keys, right_keys = set(left_items), set(right_items)
        left_only = sorted(left_keys - right_keys)
        right_only = sorted(right_keys - left_keys)
        different = sorted(key for key in left_keys & right_keys
                           if left_items[key] != right_items[key])
        same = len(left_keys & right_keys) - len(different)
        summary = (f"Folder comparison: {same} same, {len(different)} different, "
                   f"{len(left_only)} only left, {len(right_only)} only right")
        return {"summary": summary, "same": same, "different": different,
                "left_only": left_only, "right_only": right_only}

    @classmethod
    def _recycle(cls, path: str) -> None:
        """Move one path to a recoverable platform trash location."""
        if not os.path.lexists(path):
            return
        if sys.platform == "win32":
            cls._recycle_windows(path)
        else:
            cls._recycle_fallback(path)

    @staticmethod
    def _recycle_windows(path: str) -> None:
        """Use the dependency-free Windows shell Recycle Bin API."""
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", wintypes.WORD),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        # SHFileOperation requires a double-NUL-terminated source list.
        request = SHFILEOPSTRUCTW()
        request.wFunc = 3  # FO_DELETE
        request.pFrom = os.path.abspath(path) + "\0\0"
        request.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO etc.
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(request))
        if result:
            raise OSError(result, f"Windows Recycle Bin rejected '{path}'")
        if request.fAnyOperationsAborted:
            raise OSError(f"Windows Recycle Bin operation was aborted for '{path}'")

    @classmethod
    def _recycle_fallback(cls, path: str) -> None:
        """Move to the desktop trash; never silently fall back to unlinking."""
        if sys.platform == "darwin":
            files_dir = Path.home() / ".Trash"
            info_dir: Optional[Path] = None
        else:
            data_home = Path(os.environ.get(
                "XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
            trash_root = data_home / "Trash"
            files_dir = trash_root / "files"
            info_dir = trash_root / "info"
        files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if info_dir is not None:
            info_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        destination = cls._keep_both_path(str(files_dir / os.path.basename(path)))
        try:
            os.replace(path, destination)
        except OSError as exc:
            if not cls._is_cross_device_error(exc):
                raise
            # shutil.move copies before removing the source across filesystems;
            # a failure leaves the source in place rather than deleting it.
            shutil.move(path, destination)

        if info_dir is not None:
            from datetime import datetime
            from urllib.parse import quote

            info_path = info_dir / (os.path.basename(destination) + ".trashinfo")
            try:
                info_path.write_text(
                    "[Trash Info]\n"
                    f"Path={quote(os.path.abspath(path))}\n"
                    f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n",
                    encoding="utf-8")
            except OSError as exc:
                # The recoverable payload is already safely in Trash. Missing
                # metadata may prevent one-click restore but must not delete it.
                logger.warning("could not write trash metadata for %s: %s", path, exc)

    def _copy_transactional(self, src: str, dst: str, done_kb: int,
                            total_kb: int, operation: _OperationState,
                            overwrite: bool) -> tuple[int, bool]:
        """Copy to a unique sibling and atomically commit, rolling back dst."""
        staged = self._unique_sibling(dst, "tmp")
        try:
            done_kb = self._copy_any(
                src, staged, done_kb, total_kb, operation)
            self._check_cancel(operation)
            if os.path.lexists(dst) and not overwrite:
                return done_kb, False
            self._commit_staged(staged, dst)
            staged = ""
            return done_kb, True
        finally:
            if staged and os.path.lexists(staged):
                self._cleanup(staged)

    def _move_transactional(self, src: str, dst: str, done_kb: int,
                            total_kb: int, operation: _OperationState,
                            overwrite: bool) -> int:
        """Use a same-volume rename first; copy/remove only across volumes."""
        self._check_cancel(operation)
        if os.path.lexists(dst) and not overwrite:
            return done_kb

        backup = ""
        if os.path.lexists(dst):
            backup = self._unique_sibling(dst, "bak")
            os.replace(dst, backup)
        try:
            os.replace(src, dst)
        except OSError as exc:
            if backup:
                self._restore_backup(backup, dst)
                backup = ""
            if not self._is_cross_device_error(exc):
                raise
            done_kb, committed = self._copy_transactional(
                src, dst, done_kb, total_kb, operation, overwrite)
            if committed:
                # Once the staged destination is committed, finish the move even
                # if Cancel is clicked: leaving both trees would not be a move.
                self._remove(src)
            return done_kb
        except BaseException:
            if backup:
                self._restore_backup(backup, dst)
                backup = ""
            raise
        if backup:
            self._cleanup(backup)
        return done_kb

    def _commit_staged(self, staged: str, dst: str) -> None:
        backup = ""
        if os.path.lexists(dst):
            backup = self._unique_sibling(dst, "bak")
            os.replace(dst, backup)
        try:
            os.replace(staged, dst)
        except BaseException:
            if backup:
                self._restore_backup(backup, dst)
                backup = ""
            raise
        if backup:
            self._cleanup(backup)

    def _restore_backup(self, backup: str, dst: str) -> None:
        """Restore the old destination without deleting an unexpected path."""
        if os.path.lexists(dst):
            raise OSError(
                f"Cannot roll back '{dst}': destination unexpectedly exists; "
                f"original retained at '{backup}'")
        os.replace(backup, dst)

    def _copy_any(self, src: str, dst: str, done_kb: int, total_kb: int,
                  operation: _OperationState) -> int:
        """Chunked boundary-aware copy into a private staging path."""
        self._check_cancel(operation)
        if self._is_boundary(src):
            if not os.path.islink(src):
                raise OSError(f"Refusing to traverse reparse point: {src}")
            os.symlink(os.readlink(src), dst, target_is_directory=os.path.isdir(src))
            return done_kb
        try:
            mode = os.lstat(src).st_mode
        except OSError:
            raise
        if stat.S_ISDIR(mode):
            os.mkdir(dst)
            for entry in os.scandir(src):
                self._check_cancel(operation)
                done_kb = self._copy_any(
                    entry.path, os.path.join(dst, entry.name),
                    done_kb, total_kb, operation)
            shutil.copystat(src, dst, follow_symlinks=False)
            return done_kb
        with open(src, "rb") as fin, open(dst, "xb") as fout:
            while True:
                self._check_cancel(operation)
                chunk = fin.read(_COPY_CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                done_kb += len(chunk) // 1024
                self.op_progress.emit(done_kb, total_kb, src)
        shutil.copystat(src, dst, follow_symlinks=False)
        return done_kb

    @classmethod
    def _remove(cls, path: str) -> None:
        """Remove a tree without following symlinks, junctions, or reparses."""
        if not os.path.lexists(path):
            return
        if cls._is_boundary(path):
            if os.path.islink(path):
                os.unlink(path)
            elif os.path.isdir(path):
                os.rmdir(path)
            else:
                os.unlink(path)
            return
        if stat.S_ISDIR(os.lstat(path).st_mode):
            with os.scandir(path) as entries:
                for entry in entries:
                    cls._remove(entry.path)
            cls._remove_empty_dir(path)
        else:
            cls._unlink_file(path)

    @staticmethod
    def _unlink_file(path: str) -> None:
        try:
            os.unlink(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            os.unlink(path)

    @staticmethod
    def _remove_empty_dir(path: str) -> None:
        try:
            os.rmdir(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            os.rmdir(path)

    @classmethod
    def _cleanup(cls, path: str) -> None:
        try:
            cls._remove(path)
        except OSError as exc:
            logger.warning("could not clean operation artifact %s: %s", path, exc)

    @classmethod
    def _size_of(cls, path: str, operation: Optional[_OperationState] = None) -> int:
        """Return size without crossing boundaries; cancellation is cooperative."""
        if operation is not None:
            cls._check_cancel(operation)
        try:
            if cls._is_boundary(path):
                return 0
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode):
                return info.st_size
            total = 0
            with os.scandir(path) as entries:
                for entry in entries:
                    if operation is not None:
                        cls._check_cancel(operation)
                    total += cls._size_of(entry.path, operation)
            return total
        except _Cancelled:
            raise
        except OSError:
            return 0

    @staticmethod
    def _is_boundary(path: str) -> bool:
        """Detect links plus Windows junction/reparse traversal boundaries."""
        if os.path.islink(path):
            return True
        is_junction = getattr(os.path, "isjunction", None)
        if is_junction is not None:
            try:
                if is_junction(path):
                    return True
            except OSError:
                return True
        try:
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except OSError:
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

    @staticmethod
    def _unique_sibling(path: str, suffix: str) -> str:
        parent = os.path.dirname(path) or os.curdir
        name = os.path.basename(path)
        while True:
            candidate = os.path.join(
                parent, f".{name}.deepflux-{uuid.uuid4().hex}.{suffix}")
            if not os.path.lexists(candidate):
                return candidate

    @staticmethod
    def _is_cross_device_error(exc: OSError) -> bool:
        return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17

    @staticmethod
    def _same_path(first: str, second: str) -> bool:
        return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
            os.path.abspath(second))

    @staticmethod
    def _is_inside(path: str, folder: str) -> bool:
        try:
            return os.path.commonpath([os.path.abspath(path),
                                       os.path.abspath(folder)]) == os.path.abspath(folder)
        except ValueError:  # different drives
            return False

    # -- progress reporting (GUI thread) -----------------------------------------
    def _on_op_progress(self, done_kb: int, total_kb: int, name: str) -> None:
        if self._progress_dlg is None:
            # Headless (agent-driven) operations never pre-create the dialog;
            # spawn it on first progress so they are visible AND cancellable.
            operation = self._active_operation
            if operation is None:
                return
            self._progress_dlg = QProgressDialog("Preparing…", "Cancel", 0, 100, self)
            self._progress_dlg.setAccessibleName("File operation progress")
            self._progress_dlg.setWindowTitle({
                "copy": "Copying", "move": "Moving", "recycle": "Recycling",
                "delete": "Permanently Deleting", "checksum": "Calculating SHA-256",
                "verify_checksum": "Verifying SHA-256", "archive_create": "Creating Archive",
                "archive_extract": "Extracting Archive", "compare": "Comparing Folders",
                "batch_rename": "Renaming Items",
            }.get(operation.mode, "File Operation"))
            self._progress_dlg.setMinimumDuration(400)
            self._progress_dlg.setAutoClose(True)
            self._progress_dlg.canceled.connect(operation.cancel.set)
        self._progress_dlg.setMaximum(total_kb)
        self._progress_dlg.setValue(min(done_kb, total_kb))
        self._progress_dlg.setLabelText(os.path.basename(name))

    def _append_outcome(self, operation: _OperationState, ok: bool, message: str) -> None:
        """Record into the bounded outcome history (safe from worker threads)."""
        self._operation_outcomes.append({
            "mode": operation.mode,
            "ok": ok,
            "message": message,
            "items": operation.item_count,
        })
        del self._operation_outcomes[:-20]

    def _on_op_finished(self, operation: _OperationState,
                        ok: bool, message: str) -> None:
        with self._operation_guard:
            is_current = operation is self._active_operation
            if is_current:
                self._active_operation = None
        if self._progress_dlg is not None:
            self._progress_dlg.reset()
            self._progress_dlg = None
        if not is_current:
            # Headless operations release the guard and record their outcome
            # on their own thread; this signal is GUI cosmetics only.
            return
        self._status.setText(message)
        if (ok and operation.mode == "move" and self._file_clipboard
                and self._file_clipboard[0] == "cut"):
            # A completed cut-paste consumed the clipboard; copies stay pastable.
            self._file_clipboard = None
        if operation.mode in ("checksum", "verify_checksum") and isinstance(operation.result, str):
            self._checksum_result.setText(operation.result)
        if operation.mode == "compare" and ok and isinstance(operation.result, dict):
            details = []
            for key, title in (("different", "Different"), ("left_only", "Only left"),
                               ("right_only", "Only right")):
                values = list(operation.result.get(key, []))
                if values:
                    details.append(f"{title}:\n" + "\n".join(str(item) for item in values[:20]))
            QMessageBox.information(
                self, tr('Folder Comparison'),
                str(operation.result.get("summary", message)) +
                (("\n\n" + "\n\n".join(details)) if details else ""))
        if ok and operation.mode in ("archive_create", "archive_extract"):
            self.left_pane.navigate(self.left_pane.current_path(), record_history=False)
            self.right_pane.navigate(self.right_pane.current_path(), record_history=False)
        self._append_outcome(operation, ok, message)
        label = {
            "copy": "Copy", "move": "Move", "recycle": "Recycle",
            "delete": "Permanent delete", "checksum": "SHA-256",
            "verify_checksum": "SHA-256 verification", "archive_create": "Archive creation",
            "archive_extract": "Archive extraction", "compare": "Folder comparison",
            "batch_rename": "Batch rename",
        }.get(operation.mode, operation.mode.title())
        state = "completed" if ok else ("cancelled" if message == "Cancelled" else "failed")
        summary = f"Last operation: {label} {state} — {message}"
        self._outcome_status.setText(summary)
        recent = []
        for item in self._operation_outcomes[-5:]:
            item_label = str(item["mode"]).replace("_", " ").title()
            item_state = "OK" if item["ok"] else "Failed"
            recent.append(f"{item_label}: {item_state} — {item['message']}")
        self._outcome_status.setToolTip("Recent operations:\n" + "\n".join(recent))
        if not ok and message != "Cancelled" and not operation.quiet:
            QMessageBox.warning(self, tr('File Operation'), message)
