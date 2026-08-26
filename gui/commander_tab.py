"""Commander tab — a dual-pane orthodox file manager (Double Commander style).

Two QFileSystemModel-backed panes sit side by side; file operations run from
the *active* pane into the *other* pane's current directory:

- F5 Copy, F6 Move, F7 New Folder, F8 Delete, F2 Rename, Backspace Up
- Drag & drop between panes (QFileSystemModel handles the copy)
- Long operations run on a worker thread with a cancellable progress dialog;
  copies are chunked so even multi-GB files keep the UI alive.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QDir, QEvent, QModelIndex, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QFileSystemModel,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

_COPY_CHUNK = 4 * 1024 * 1024

_MENU_STYLE = (
    "QMenu { background-color: #111827; color: #c8d3e0; border: 1px solid #1a2a4a;"
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


class FilePane(QWidget):
    """One side of the commander: drive bar + path edit + file tree."""

    activated_pane = Signal(object)    # emitted with self when the pane gains focus
    op_requested = Signal(str)         # "copy" | "move" | "delete" (needs the other pane)
    status_message = Signal(str)

    def __init__(self, start_path: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
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
            for fi in QDir.drives():
                self._drives.addItem(fi.absoluteFilePath())
            self._drives.setFixedWidth(70)
            self._drives.activated.connect(
                lambda _i: self.navigate(self._drives.currentText()))
            bar.addWidget(self._drives)
        up_btn = QPushButton("⬆")
        up_btn.setToolTip("Up one level (Backspace)")
        up_btn.setFixedWidth(30)
        up_btn.clicked.connect(self.go_up)
        bar.addWidget(up_btn)
        self._path_edit = QLineEdit()
        completer = QCompleter(self)
        comp_model = QFileSystemModel(completer)
        comp_model.setRootPath("")
        completer.setModel(comp_model)
        self._path_edit.setCompleter(completer)
        self._path_edit.returnPressed.connect(
            lambda: self.navigate(self._path_edit.text()))
        bar.addWidget(self._path_edit, 1)
        refresh_btn = QPushButton("⟳")
        refresh_btn.setToolTip("Refresh")
        refresh_btn.setFixedWidth(30)
        refresh_btn.clicked.connect(lambda: self.navigate(self.current_path()))
        bar.addWidget(refresh_btn)
        layout.addLayout(bar)

        self._view = QTreeView(self)
        self._view.setModel(self._model)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self._view.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self._view.setDefaultDropAction(Qt.DropAction.CopyAction)
        self._view.setUniformRowHeights(True)
        self._view.setAnimated(False)
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self._view.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._view.activated.connect(self._on_activated)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self._view, 1)

        self._view.installEventFilter(self)
        self._path_edit.installEventFilter(self)
        self._view.selectionModel().selectionChanged.connect(self._report_selection)

        start = start_path if os.path.isdir(start_path) else str(Path.home())
        self.navigate(start)

    # -- navigation ----------------------------------------------------------
    def navigate(self, path: str) -> None:
        path = os.path.abspath(path.strip().strip('"'))
        if not os.path.isdir(path):
            self.status_message.emit(f"Path not found: {path}")
            return
        self._view.setRootIndex(self._model.index(path))
        self._path_edit.setText(QDir.toNativeSeparators(path))
        if sys.platform == "win32":
            drive = os.path.splitdrive(path)[0] + "\\"
            i = self._drives.findText(drive, Qt.MatchFlag.MatchStartsWith)
            if i >= 0:
                self._drives.setCurrentIndex(i)
        self.status_message.emit(path)

    def go_up(self) -> None:
        parent = os.path.dirname(self.current_path())
        if parent and parent != self.current_path():
            self.navigate(parent)

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
        self._view.setStyleSheet(f"QTreeView {{ border: 1px solid {color}; }}")

    # -- internals -------------------------------------------------------------
    def _apply_filter(self) -> None:
        filt = QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot
        self._model.setFilter(filt)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.FocusIn:
            self.activated_pane.emit(self)
        return super().eventFilter(obj, event)

    def _on_activated(self, index: QModelIndex) -> None:
        if self._model.isDir(index):
            self.navigate(self._model.filePath(index))
        else:
            _open_native(self._model.filePath(index))

    def _report_selection(self) -> None:
        n = len(self.selected_paths())
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
            menu.addAction("Rename…\tF2", self.rename_selected)
            menu.addAction("Delete…\tF8", lambda: self.op_requested.emit("delete"))
            menu.addSeparator()
        menu.addAction("New Folder…\tF7", self.new_folder)
        menu.addAction("Refresh", lambda: self.navigate(self.current_path()))
        menu.exec(self._view.viewport().mapToGlobal(pos))


class CommanderTab(QWidget):
    """Dual-pane file manager tab (Double Commander style)."""

    op_progress = Signal(int, int, str)   # done KB, total KB, current item
    op_finished = Signal(bool, str)       # ok, message

    def __init__(self, config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._cancel = threading.Event()
        self._progress_dlg: Optional[QProgressDialog] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        left_start = config.default_save_path if os.path.isdir(config.default_save_path) else str(Path.home())
        self.left_pane = FilePane(left_start)
        self.right_pane = FilePane(str(Path.home()))
        self.splitter.addWidget(self.left_pane)
        self.splitter.addWidget(self.right_pane)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([1000, 1000])
        layout.addWidget(self.splitter, 1)

        # Function-key bar (Double Commander style).
        keys = QHBoxLayout()
        keys.setSpacing(6)
        for text, slot in (
            ("F5 Copy", lambda: self._start_transfer("copy")),
            ("F6 Move", lambda: self._start_transfer("move")),
            ("F7 New Folder", self._new_folder),
            ("F8 Delete", self._start_delete),
            ("F2 Rename", lambda: self._active.rename_selected()),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            keys.addWidget(btn)
        keys.addStretch()
        self._status = QLabel("")
        self._status.setStyleSheet("color: #8a9ab0; font-size: 11px;")
        keys.addWidget(self._status)
        layout.addLayout(keys)

        self._active: FilePane = self.left_pane
        for pane in (self.left_pane, self.right_pane):
            pane.activated_pane.connect(self._set_active)
            pane.op_requested.connect(self._on_op_requested)
            pane.status_message.connect(self._status.setText)
        self.left_pane.set_active(True)
        self.right_pane.set_active(False)

        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for key, slot in (
            ("F5", lambda: self._start_transfer("copy")),
            ("F6", lambda: self._start_transfer("move")),
            ("F7", self._new_folder),
            ("F8", self._start_delete),
            ("F2", lambda: self._active.rename_selected()),
            ("Backspace", lambda: self._active.go_up()),
        ):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(ctx)
            sc.activated.connect(slot)

        self.op_progress.connect(self._on_op_progress)
        self.op_finished.connect(self._on_op_finished)

    # -- pane coordination -----------------------------------------------------
    def _set_active(self, pane: FilePane) -> None:
        self._active = pane
        self.left_pane.set_active(pane is self.left_pane)
        self.right_pane.set_active(pane is self.right_pane)

    def _other(self, pane: FilePane) -> FilePane:
        return self.right_pane if pane is self.left_pane else self.left_pane

    def _on_op_requested(self, mode: str) -> None:
        if mode == "delete":
            self._start_delete()
        else:
            self._start_transfer(mode)

    def _new_folder(self) -> None:
        self._active.new_folder()

    # -- file operations ---------------------------------------------------------
    def _start_transfer(self, mode: str) -> None:
        paths = self._active.selected_paths()
        if not paths:
            self._status.setText("Nothing selected")
            return
        dst_dir = self._other(self._active).current_path()
        if mode == "move" and os.path.abspath(dst_dir) == os.path.abspath(self._active.current_path()):
            self._status.setText("Source and destination are the same")
            return
        conflicts = [p for p in paths
                     if os.path.exists(os.path.join(dst_dir, os.path.basename(p)))]
        overwrite = False
        if conflicts:
            r = QMessageBox.question(
                self, "Overwrite?",
                f"{len(conflicts)} item(s) already exist in the destination.\n"
                "Yes = overwrite them, No = skip them.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel:
                return
            overwrite = r == QMessageBox.StandardButton.Yes
        self._launch_worker(mode, paths, dst_dir, overwrite)

    def _start_delete(self) -> None:
        paths = self._active.selected_paths()
        if not paths:
            self._status.setText("Nothing selected")
            return
        r = QMessageBox.warning(
            self, "Delete",
            f"Permanently delete {len(paths)} item(s)?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if r != QMessageBox.StandardButton.Yes:
            return
        self._launch_worker("delete", paths, "", False)

    def _launch_worker(self, mode: str, paths: List[str], dst_dir: str, overwrite: bool) -> None:
        self._cancel.clear()
        self._progress_dlg = QProgressDialog("Preparing…", "Cancel", 0, 100, self)
        self._progress_dlg.setWindowTitle({"copy": "Copying", "move": "Moving",
                                           "delete": "Deleting"}[mode])
        self._progress_dlg.setMinimumDuration(400)
        self._progress_dlg.setAutoClose(True)
        self._progress_dlg.canceled.connect(self._cancel.set)
        threading.Thread(target=self._worker, args=(mode, paths, dst_dir, overwrite),
                         daemon=True).start()

    # -- worker thread ----------------------------------------------------------
    def _worker(self, mode: str, paths: List[str], dst_dir: str, overwrite: bool) -> None:
        try:
            total_kb = max(1, sum(self._size_of(p) for p in paths) // 1024)
            done_kb = 0
            for src in paths:
                if self._cancel.is_set():
                    raise _Cancelled
                if mode == "delete":
                    self.op_progress.emit(done_kb, total_kb, src)
                    size_kb = self._size_of(src) // 1024
                    self._remove(src)
                    done_kb += max(1, size_kb)
                    continue
                dst = os.path.join(dst_dir, os.path.basename(src))
                if os.path.abspath(src) == os.path.abspath(dst):
                    continue
                if os.path.isdir(src) and self._is_inside(dst, src):
                    raise OSError(f"Cannot copy '{src}' into itself")
                if os.path.exists(dst):
                    if not overwrite:
                        continue
                    self._remove(dst)
                done_kb = self._copy_any(src, dst, done_kb, total_kb)
                if mode == "move":
                    self._remove(src)
            self.op_finished.emit(True, {"copy": "Copy complete", "move": "Move complete",
                                         "delete": "Delete complete"}[mode])
        except _Cancelled:
            self.op_finished.emit(False, "Cancelled")
        except Exception as exc:
            logger.warning("commander %s failed: %s", mode, exc)
            self.op_finished.emit(False, str(exc))

    def _copy_any(self, src: str, dst: str, done_kb: int, total_kb: int) -> int:
        """Chunked copy with progress + cancel checks. Returns updated done_kb."""
        if os.path.isdir(src):
            os.makedirs(dst, exist_ok=True)
            for entry in os.scandir(src):
                done_kb = self._copy_any(entry.path, os.path.join(dst, entry.name),
                                         done_kb, total_kb)
            shutil.copystat(src, dst)
            return done_kb
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            while True:
                if self._cancel.is_set():
                    fout.close()
                    self._remove(dst)  # drop the partial file
                    raise _Cancelled
                chunk = fin.read(_COPY_CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                done_kb += len(chunk) // 1024
                self.op_progress.emit(done_kb, total_kb, src)
        shutil.copystat(src, dst)
        return done_kb

    @staticmethod
    def _remove(path: str) -> None:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

    @staticmethod
    def _size_of(path: str) -> int:
        try:
            if os.path.isdir(path):
                return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
            return os.path.getsize(path)
        except OSError:
            return 0

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

    def _on_op_finished(self, ok: bool, message: str) -> None:
        if self._progress_dlg is not None:
            self._progress_dlg.reset()
            self._progress_dlg = None
        self._status.setText(message)
        if not ok and message != "Cancelled":
            QMessageBox.warning(self, "File Operation", message)
