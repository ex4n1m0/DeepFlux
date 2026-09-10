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

from PySide6.QtCore import QDir, QEvent, QModelIndex, QTimer, Qt, Signal
from PySide6.QtGui import QImage, QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCompleter,
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
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from gui.column_sizing import AutoColumnSizer

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


class FilePane(QWidget):
    """One side of the commander: drive bar + path edit + file tree."""

    activated_pane = Signal(object)    # emitted with self when the pane gains focus
    op_requested = Signal(str)         # commander-routed filesystem mutation
    selection_changed = Signal(object) # selected paths, for asynchronous preview
    status_message = Signal(str)

    def __init__(self, start_path: str, parent: Optional[QWidget] = None,
                 pane_name: str = "File pane") -> None:
        super().__init__(parent)
        self._pane_name = pane_name
        self.setAccessibleName(pane_name)
        self._back_history: List[str] = []
        self._forward_history: List[str] = []
        self._show_hidden_system = False
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
            self._drives.setToolTip("Choose a drive")
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
            "⬆", "Up", "Up one level (Backspace)", self.go_up)
        bar.addWidget(self._up_btn)
        self._path_edit = QLineEdit()
        self._path_edit.setAccessibleName(f"{pane_name} path")
        self._path_edit.setToolTip("Current folder path; press Enter to navigate")
        completer = QCompleter(self)
        comp_model = QFileSystemModel(completer)
        comp_model.setRootPath("")
        completer.setModel(comp_model)
        self._path_edit.setCompleter(completer)
        self._path_edit.returnPressed.connect(
            lambda: self.navigate(self._path_edit.text()))
        bar.addWidget(self._path_edit, 1)
        self._refresh_btn = self._nav_button(
            "⟳", "Refresh", "Refresh current folder",
            lambda: self.navigate(self.current_path(), record_history=False))
        bar.addWidget(self._refresh_btn)
        layout.addLayout(bar)

        tools = QHBoxLayout()
        tools.setSpacing(4)
        filter_label = QLabel("Filter:")
        filter_label.setAccessibleName(f"{pane_name} filter label")
        tools.addWidget(filter_label)
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("filename contains…")
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setAccessibleName(f"{pane_name} filename filter")
        self._filter_edit.setToolTip("Quickly show names containing this text")
        self._filter_edit.textChanged.connect(self.set_filename_filter)
        tools.addWidget(self._filter_edit, 1)
        self._hidden_btn = QPushButton("Hidden/System")
        self._hidden_btn.setCheckable(True)
        self._hidden_btn.setAccessibleName(f"{pane_name} hidden and system files toggle")
        self._hidden_btn.setToolTip("Show or hide hidden and system items")
        self._hidden_btn.toggled.connect(self.set_show_hidden_system)
        tools.addWidget(self._hidden_btn)
        layout.addLayout(tools)

        self._view = QTreeView(self)
        self._view.setAccessibleName(f"{pane_name} files")
        self._view.setToolTip("Files and folders; native drag and drop is disabled for safety")
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
        self._pane_status.setToolTip("Selection count and free disk space")
        self._pane_status.setStyleSheet("color: #8a9ab0; font-size: 17px;")
        layout.addWidget(self._pane_status)

        self._view.installEventFilter(self)
        self._view.viewport().installEventFilter(self)
        self._path_edit.installEventFilter(self)
        self._filter_edit.installEventFilter(self)
        self._view.selectionModel().selectionChanged.connect(self._report_selection)

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
        button.setFixedWidth(30)
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
        self._view.setRootIndex(self._model.index(path))
        # Columns sized for the new folder right away (header hints), then
        # again as the async listing lands rows via _fit_timer.
        self._column_sizer.auto_fit()
        self._fit_timer.start()
        self._path_edit.setText(QDir.toNativeSeparators(path))
        if sys.platform == "win32":
            drive = os.path.splitdrive(path)[0] + "\\"
            i = self._drives.findText(drive, Qt.MatchFlag.MatchStartsWith)
            if i >= 0:
                self._drives.setCurrentIndex(i)
        self._update_navigation_buttons()
        self._update_pane_status()
        self.status_message.emit(path)

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
        return self._model.filePath(self._view.rootIndex())

    def selected_paths(self) -> List[str]:
        return [self._model.filePath(i)
                for i in self._view.selectionModel().selectedRows(0)]

    def rename_selected(self) -> None:
        rows = self._view.selectionModel().selectedRows(0)
        if rows:
            self._view.edit(rows[0])

    def new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        name = name.strip()
        if not ok or not name:
            return
        if not QDir(self.current_path()).mkdir(name):
            QMessageBox.warning(self, "New Folder", f"Could not create '{name}'.")

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
        self._apply_filter()
        self._update_pane_status()

    def shows_hidden_system(self) -> bool:
        return self._show_hidden_system

    def _apply_filter(self) -> None:
        filt = QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot
        if self._show_hidden_system:
            filt |= QDir.Filter.Hidden | QDir.Filter.System
        self._model.setFilter(filt)

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
        self._pane_status.setText(f"{count} selected | {space}{filtered}")

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
            menu.addAction("Open", lambda: self._on_activated(
                self._view.selectionModel().selectedRows(0)[0]))
            folder = paths[0] if os.path.isdir(paths[0]) else os.path.dirname(paths[0])
            label = "Show in Explorer" if sys.platform == "win32" else "Show in File Manager"
            menu.addAction(label, lambda: _open_native(folder))
            menu.addSeparator()
            menu.addAction("Copy to other pane\tF5", lambda: self.op_requested.emit("copy"))
            menu.addAction("Move to other pane\tF6", lambda: self.op_requested.emit("move"))
            menu.addSeparator()
            if len(paths) == 1 and os.path.isfile(paths[0]):
                menu.addAction("Preview", lambda: self.op_requested.emit("preview"))
                menu.addAction("Calculate SHA-256", lambda: self.op_requested.emit("checksum"))
                menu.addAction("Verify SHA-256…", lambda: self.op_requested.emit("verify_checksum"))
                if paths[0].lower().endswith(_ARCHIVE_EXTENSIONS):
                    menu.addAction("Extract safely to other pane", lambda: self.op_requested.emit("extract"))
            menu.addAction("Create archive…", lambda: self.op_requested.emit("archive"))
            menu.addSeparator()
            menu.addAction("Rename…\tF2", lambda: self.op_requested.emit("rename"))
            menu.addAction("Move to Recycle Bin\tF8", lambda: self.op_requested.emit("recycle"))
            menu.addAction("Permanently Delete…\tShift+Delete",
                           lambda: self.op_requested.emit("permanent_delete"))
            menu.addSeparator()
        menu.addAction("New Folder…\tF7", lambda: self.op_requested.emit("mkdir"))
        menu.addAction("Refresh", lambda: self.navigate(
            self.current_path(), record_history=False))
        menu.exec(self._view.viewport().mapToGlobal(pos))


class CommanderTab(QWidget):
    """Dual-pane file manager tab (Double Commander style)."""

    op_progress = Signal(int, int, str)   # done KiB/items, total, current item
    op_finished = Signal(object, bool, str)  # operation, ok, message
    preview_ready = Signal(str, object)    # request token, bounded preview result

    def __init__(self, config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._operation_guard = threading.Lock()
        self._active_operation: Optional[_OperationState] = None
        self._progress_dlg: Optional[QProgressDialog] = None
        self._operation_outcomes: List[Dict[str, object]] = []
        self._preview_token = ""
        self._preview_path = ""

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
        self.left_pane = FilePane(left_start, pane_name="Left pane")
        self.right_pane = FilePane(right_start, pane_name="Right pane")
        self.splitter.addWidget(self.left_pane)
        self.splitter.addWidget(self.right_pane)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([1000, 1000])
        layout.addWidget(self.splitter, 1)

        self._preview_stack = QStackedWidget(self)
        self._preview_stack.setAccessibleName("Selected file preview")
        self._preview_stack.setToolTip("Bounded preview of the selected file; media is never played automatically")
        self._preview_stack.setMaximumHeight(220)
        self._preview_text = QPlainTextEdit(self)
        self._preview_text.setReadOnly(True)
        self._preview_text.setAccessibleName("Selected file text and metadata preview")
        self._preview_text.setPlaceholderText("Select one file to preview")
        self._preview_image = QLabel(self)
        self._preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_image.setAccessibleName("Selected image preview")
        self._preview_image.setToolTip("Image preview constrained by byte and pixel limits")
        self._preview_stack.addWidget(self._preview_text)
        self._preview_stack.addWidget(self._preview_image)
        layout.addWidget(self._preview_stack)

        checksum_bar = QHBoxLayout()
        checksum_label = QLabel("SHA-256:")
        checksum_label.setAccessibleName("SHA-256 result label")
        checksum_bar.addWidget(checksum_label)
        self._checksum_result = QLineEdit()
        self._checksum_result.setReadOnly(True)
        self._checksum_result.setAccessibleName("SHA-256 checksum result")
        self._checksum_result.setToolTip("Calculated SHA-256 checksum")
        checksum_bar.addWidget(self._checksum_result, 1)
        self._checksum_copy_btn = QPushButton("Copy")
        self._checksum_copy_btn.setAccessibleName("Copy SHA-256 checksum")
        self._checksum_copy_btn.setToolTip("Copy the checksum to the clipboard")
        self._checksum_copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self._checksum_result.text()))
        checksum_bar.addWidget(self._checksum_copy_btn)
        layout.addLayout(checksum_bar)

        # Function-key bar (Double Commander style).
        keys = QHBoxLayout()
        keys.setSpacing(6)
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
        keys.addStretch()
        self._status = QLabel("")
        self._status.setAccessibleName("Commander status")
        self._status.setToolTip("Current Commander status")
        self._status.setStyleSheet("color: #8a9ab0; font-size: 17px;")
        keys.addWidget(self._status)
        layout.addLayout(keys)

        self._outcome_status = QLabel("No file operations yet")
        self._outcome_status.setAccessibleName("File operation history summary")
        self._outcome_status.setToolTip("Recent file operation outcomes")
        self._outcome_status.setStyleSheet("color: #8a9ab0; font-size: 17px;")
        layout.addWidget(self._outcome_status)

        self._active: FilePane = self.left_pane
        for pane in (self.left_pane, self.right_pane):
            pane.activated_pane.connect(self._set_active)
            pane.op_requested.connect(self._on_op_requested)
            pane.selection_changed.connect(
                lambda paths, source=pane: self._on_selection_changed(source, paths))
            pane.status_message.connect(self._status.setText)
        self.left_pane.set_active(True)
        self.right_pane.set_active(False)

        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for key, slot in (
            ("F5", lambda: self._start_transfer("copy")),
            ("F6", lambda: self._start_transfer("move")),
            ("F7", self._new_folder),
            ("F8", self._start_delete),
            ("Delete", self._start_delete),
            ("Shift+Delete", self._start_permanent_delete),
            ("F2", self._rename),
            ("Backspace", lambda: self._active.go_up()),
            ("Alt+Left", lambda: self._active.go_back()),
            ("Alt+Right", lambda: self._active.go_forward()),
            ("Ctrl+F", lambda: self._active._filter_edit.setFocus()),
        ):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(ctx)
            sc.activated.connect(slot)

        self.op_progress.connect(self._on_op_progress)
        self.op_finished.connect(self._on_op_finished)
        self.preview_ready.connect(self._apply_preview)

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
        """Copy validated pane locations into their dedicated config field."""
        left, right = self.pane_paths()
        paths = {
            "left": self._restorable_directory(left, self._fs_root()),
            "right": self._restorable_directory(right, self._fs_root()),
        }
        self._config.ui_commander_paths = paths
        return dict(paths)

    def _set_active(self, pane: FilePane) -> None:
        self._active = pane
        self.left_pane.set_active(pane is self.left_pane)
        self.right_pane.set_active(pane is self.right_pane)
        self._request_preview(pane.selected_paths())

    def _other(self, pane: FilePane) -> FilePane:
        return self.right_pane if pane is self.left_pane else self.left_pane

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
        elif mode == "rename":
            self._rename()
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
            self._status.setText("Another file operation is already running")
            return
        self._active.new_folder()

    def _rename(self) -> None:
        if self._operation_running():
            self._status.setText("Another file operation is already running")
            return
        self._active.rename_selected()

    # -- file operations ---------------------------------------------------------
    def _operation_running(self) -> bool:
        with self._operation_guard:
            return self._active_operation is not None

    def _start_transfer(self, mode: str) -> None:
        if self._operation_running():
            self._status.setText("Another file operation is already running")
            return
        paths = self._active.selected_paths()
        if not paths:
            self._status.setText("Nothing selected")
            return
        dst_dir = self._other(self._active).current_path()
        if mode == "move" and self._same_path(dst_dir, self._active.current_path()):
            self._status.setText("Source and destination are the same")
            return

        target_plan: Dict[str, Tuple[str, bool]] = {}
        work_paths: List[str] = []
        reserved: Set[str] = set()
        for src in paths:
            dst = os.path.join(dst_dir, os.path.basename(src))
            overwrite = False
            if os.path.lexists(dst):
                choice = self._prompt_conflict(src, dst)
                if choice == "cancel":
                    return
                if choice == "skip":
                    continue
                if choice == "keep_both":
                    dst = self._keep_both_path(dst, reserved)
                else:
                    overwrite = True
            target_plan[src] = (dst, overwrite)
            work_paths.append(src)
            reserved.add(os.path.normcase(os.path.abspath(dst)))
        if not work_paths:
            self._status.setText("All conflicting items were skipped")
            return
        self._launch_worker(mode, work_paths, dst_dir, False, target_plan)

    def _prompt_conflict(self, src: str, dst: str) -> str:
        """Ask for a safe decision for one conflict, never a whole batch."""
        box = QMessageBox(self)
        box.setWindowTitle("Destination item exists")
        box.setAccessibleName("File conflict options")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"Choose what to do with:\n{QDir.toNativeSeparators(os.path.abspath(src))}\n\n"
            f"Existing destination:\n{QDir.toNativeSeparators(os.path.abspath(dst))}")
        overwrite_btn = box.addButton("Overwrite", QMessageBox.ButtonRole.DestructiveRole)
        keep_btn = box.addButton("Keep Both", QMessageBox.ButtonRole.AcceptRole)
        skip_btn = box.addButton("Skip", QMessageBox.ButtonRole.NoRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        overwrite_btn.setToolTip("Replace this destination item transactionally")
        keep_btn.setToolTip("Copy or move this item under a unique name")
        skip_btn.setToolTip("Leave both source and destination unchanged")
        cancel_btn.setToolTip("Cancel the entire operation")
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
            self._status.setText("Another file operation is already running")
            return None
        paths = self._active.selected_paths()
        if not paths:
            self._status.setText("Nothing selected")
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
            self, "Move to Recycle Bin",
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
            self, "Permanently Delete",
            "This cannot be undone. Type every exact path below, one per line, "
            f"to permanently delete:\n\n{display_paths}\n\nExact paths:", "")
        if not ok:
            return
        if typed.strip() != display_paths:
            QMessageBox.warning(
                self, "Permanent Delete Not Confirmed",
                "The entered paths did not exactly match. Nothing was deleted.")
            return
        self._launch_worker("delete", paths, "", False)

    def _start_checksum(self, verify: bool = False) -> None:
        if self._operation_running():
            self._status.setText("Another file operation is already running")
            return
        paths = self._active.selected_paths()
        if len(paths) != 1 or not os.path.isfile(paths[0]):
            self._status.setText("Select exactly one regular file")
            return
        expected = ""
        if verify:
            expected, ok = QInputDialog.getText(
                self, "Verify SHA-256", "Expected 64-character SHA-256 checksum:")
            expected = expected.strip().lower()
            if not ok:
                return
            if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
                QMessageBox.warning(self, "Verify SHA-256", "Enter exactly 64 hexadecimal characters.")
                return
        self._launch_worker("verify_checksum" if verify else "checksum", paths, "", False,
                            options={"expected": expected})

    def _start_archive_create(self) -> None:
        if self._operation_running():
            self._status.setText("Another file operation is already running")
            return
        paths = self._active.selected_paths()
        if not paths:
            self._status.setText("Nothing selected")
            return
        suggested = os.path.join(self._other(self._active).current_path(),
                                 os.path.basename(paths[0]) + ".zip")
        target, _selected = QFileDialog.getSaveFileName(
            self, "Create Archive", suggested,
            "ZIP archive (*.zip);;Tar gzip archive (*.tar.gz);;Tar archive (*.tar);;Tar bzip2 archive (*.tar.bz2);;Tar xz archive (*.tar.xz)")
        if not target:
            return
        if os.path.lexists(target):
            QMessageBox.warning(self, "Create Archive", "For safety, existing archives are never overwritten.")
            return
        if not self._archive_format(target):
            QMessageBox.warning(self, "Create Archive", "Use .zip, .tar, .tar.gz, .tar.bz2, or .tar.xz.")
            return
        self._launch_worker("archive_create", paths, target, False)

    def _start_archive_extract(self) -> None:
        if self._operation_running():
            self._status.setText("Another file operation is already running")
            return
        paths = self._active.selected_paths()
        if len(paths) != 1 or not os.path.isfile(paths[0]):
            self._status.setText("Select exactly one archive")
            return
        archive = paths[0]
        if not self._archive_format(archive):
            self._status.setText("Unsupported archive format")
            return
        destination = os.path.join(
            self._other(self._active).current_path(), self._archive_stem(archive))
        if os.path.lexists(destination):
            QMessageBox.warning(
                self, "Extract Archive",
                f"Destination already exists and will not be overwritten:\n{destination}")
            return
        self._launch_worker("archive_extract", [archive], destination, False)

    def _start_compare(self) -> None:
        if self._operation_running():
            self._status.setText("Another file operation is already running")
            return
        self._launch_worker("compare", list(self.pane_paths()), "", False)

    def _launch_worker(self, mode: str, paths: List[str], dst_dir: str,
                       overwrite: bool,
                       target_plan: Optional[Dict[str, Tuple[str, bool]]] = None,
                       options: Optional[Dict[str, object]] = None) -> bool:
        operation = _OperationState(mode, item_count=len(paths), options=dict(options or {}))
        with self._operation_guard:
            if self._active_operation is not None:
                self._status.setText("Another file operation is already running")
                return False
            self._active_operation = operation
        self._progress_dlg = QProgressDialog("Preparing…", "Cancel", 0, 100, self)
        self._progress_dlg.setAccessibleName("File operation progress")
        self._progress_dlg.setWindowTitle({
            "copy": "Copying", "move": "Moving", "recycle": "Recycling",
            "delete": "Permanently Deleting", "checksum": "Calculating SHA-256",
            "verify_checksum": "Verifying SHA-256", "archive_create": "Creating Archive",
            "archive_extract": "Extracting Archive", "compare": "Comparing Folders",
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
        if self._progress_dlg is not None:
            self._progress_dlg.setMaximum(total_kb)
            self._progress_dlg.setValue(min(done_kb, total_kb))
            self._progress_dlg.setLabelText(os.path.basename(name))

    def _on_op_finished(self, operation: _OperationState,
                        ok: bool, message: str) -> None:
        with self._operation_guard:
            if operation is not self._active_operation:
                return
            self._active_operation = None
        if self._progress_dlg is not None:
            self._progress_dlg.reset()
            self._progress_dlg = None
        self._status.setText(message)
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
                self, "Folder Comparison",
                str(operation.result.get("summary", message)) +
                (("\n\n" + "\n\n".join(details)) if details else ""))
        if ok and operation.mode in ("archive_create", "archive_extract"):
            self.left_pane.navigate(self.left_pane.current_path(), record_history=False)
            self.right_pane.navigate(self.right_pane.current_path(), record_history=False)
        outcome = {
            "mode": operation.mode,
            "ok": ok,
            "message": message,
            "items": operation.item_count,
        }
        self._operation_outcomes.append(outcome)
        del self._operation_outcomes[:-20]
        label = {
            "copy": "Copy", "move": "Move", "recycle": "Recycle",
            "delete": "Permanent delete", "checksum": "SHA-256",
            "verify_checksum": "SHA-256 verification", "archive_create": "Archive creation",
            "archive_extract": "Archive extraction", "compare": "Folder comparison",
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
        if not ok and message != "Cancelled":
            QMessageBox.warning(self, "File Operation", message)
