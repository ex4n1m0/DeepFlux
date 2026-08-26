"""Downloads tab — IDM-style download manager UI for DeepFlux.

Displays a queue of download jobs with progress bars, speed, ETA, and
status. Includes a toolbar for add/pause/resume/cancel/retry/open and
a details panel showing per-segment progress.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dlmgr.engine import DownloadEngine
from dlmgr.job import JobStatus, SegmentStatus

logger = logging.getLogger(__name__)


def _format_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def _format_speed(bps: int) -> str:
    if bps <= 0:
        return "—"
    return _format_size(bps) + "/s"


def _format_eta(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


class DownloadsTab(QWidget):
    """The Downloads tab widget — queue list + toolbar + details panel."""

    def __init__(self, engine: DownloadEngine, config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._config = config
        self._play_callback = None  # set via set_play_callback()
        self._build_ui()
        self._start_refresh_timer()

    def set_play_callback(self, cb) -> None:
        """Wire the 'Play in Player' action to the main window's player tab."""
        self._play_callback = cb

    def set_torrent_callback(self, cb) -> None:
        """Called with (path, job_id) when a completed job is a .torrent file."""
        self._torrent_callback = cb

    def set_notify_callback(self, cb) -> None:
        """Called with the job when a download completes (once each)."""
        self._notify_callback = cb

    def _check_completed_notifications(self, jobs) -> None:
        """Fire the notify callback for jobs that just reached COMPLETED.

        The first tick only seeds the seen-set so jobs that were already
        finished when the app started don't trigger a notification storm."""
        cb = getattr(self, "_notify_callback", None)
        if cb is None:
            return
        if not hasattr(self, "_notify_seen"):
            self._notify_seen = {j.id for j in jobs if j.status == JobStatus.COMPLETED}
            return
        for job in jobs:
            if job.status == JobStatus.COMPLETED and job.id not in self._notify_seen:
                self._notify_seen.add(job.id)
                cb(job)

    @staticmethod
    def _is_torrent_file(path: str) -> bool:
        """True if the file is a .torrent (by extension or bencode sniff)."""
        if path.lower().endswith(".torrent"):
            return True
        try:
            if os.path.getsize(path) > 20 * 1024 * 1024:
                return False
            with open(path, "rb") as f:
                head = f.read(4096)
            return head.startswith(b"d") and b"4:info" in head
        except OSError:
            return False

    def _check_torrent_jobs(self, jobs) -> None:
        """Hand completed .torrent downloads to the torrent engine (once each)."""
        cb = getattr(self, "_torrent_callback", None)
        if cb is None:
            return
        if not hasattr(self, "_torrent_handled"):
            self._torrent_handled: set = set()
        for job in jobs:
            if (job.status == JobStatus.COMPLETED
                    and job.id not in self._torrent_handled
                    and os.path.isfile(job.save_path)
                    and self._is_torrent_file(job.save_path)):
                self._torrent_handled.add(job.id)
                cb(job.save_path, job.id)

    VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
                  ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".3gp", ".ogv")

    def _is_playable(self, job) -> bool:
        """Completed video file that exists on disk."""
        return (job.status == JobStatus.COMPLETED
                and job.save_path.lower().endswith(self.VIDEO_EXTS)
                and os.path.isfile(job.save_path))

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # --- Toolbar ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self.add_url_btn = QPushButton("+ Add URL")
        self.add_url_btn.setObjectName("btn_accent")
        self.add_url_btn.clicked.connect(self._add_url_dialog)
        toolbar.addWidget(self.add_url_btn)

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self._pause_selected)
        toolbar.addWidget(self.pause_btn)

        self.resume_btn = QPushButton("Resume")
        self.resume_btn.clicked.connect(self._resume_selected)
        toolbar.addWidget(self.resume_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel_selected)
        toolbar.addWidget(self.cancel_btn)

        self.retry_btn = QPushButton("Retry")
        self.retry_btn.clicked.connect(self._retry_selected)
        toolbar.addWidget(self.retry_btn)

        toolbar.addStretch()

        self.open_file_btn = QPushButton("Open File")
        self.open_file_btn.clicked.connect(self._open_selected_file)
        toolbar.addWidget(self.open_file_btn)

        self.open_folder_btn = QPushButton("Open Folder")
        self.open_folder_btn.clicked.connect(self._open_selected_folder)
        toolbar.addWidget(self.open_folder_btn)

        self.clear_completed_btn = QPushButton("Clear Completed")
        self.clear_completed_btn.clicked.connect(self._clear_completed)
        toolbar.addWidget(self.clear_completed_btn)

        layout.addLayout(toolbar)

        # --- Downloads table ---
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["Filename", "Progress", "Speed", "ETA", "Status", "Size", "Source URL"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.itemDoubleClicked.connect(lambda _item: self._on_double_click())
        self.table.verticalHeader().setDefaultSectionSize(20)
        layout.addWidget(self.table)

        # --- Details panel (segmented file downloads only; hidden for streams) ---
        self.details_group = QGroupBox("Details")
        details_layout = QVBoxLayout(self.details_group)
        details_layout.setContentsMargins(6, 2, 6, 4)
        details_layout.setSpacing(2)
        self.details_label = QLabel("Select a download to see segment details.")
        self.details_label.setStyleSheet("color: #8a9ab0; font-size: 11px;")
        details_layout.addWidget(self.details_label)

        self.segment_table = QTableWidget()
        self.segment_table.setColumnCount(5)
        self.segment_table.setHorizontalHeaderLabels(["#", "Range", "Downloaded", "Progress", "Status"])
        self.segment_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.segment_table.setMaximumHeight(110)
        self.segment_table.setAlternatingRowColors(True)
        self.segment_table.verticalHeader().setDefaultSectionSize(18)
        details_layout.addWidget(self.segment_table)
        self.details_group.setVisible(False)

        layout.addWidget(self.details_group)

    def _start_refresh_timer(self) -> None:
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Update the table from the engine's current job list."""
        jobs = self._engine.list_jobs()
        # Sort by created_at descending (newest first).
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        self._check_torrent_jobs(jobs)
        self._check_completed_notifications(jobs)

        selected_row = self.table.currentRow()
        selected_id = ""
        if selected_row >= 0 and selected_row < self.table.rowCount():
            item = self.table.item(selected_row, 0)
            if item:
                selected_id = item.data(Qt.UserRole) or ""

        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            # Filename — completed videos get a play indicator (double-click plays).
            play_mark = "▶ " if self._is_playable(job) else ""
            name_item = QTableWidgetItem(play_mark + job.filename)
            name_item.setData(Qt.UserRole, job.id)
            self.table.setItem(row, 0, name_item)

            # Progress bar — reuse the existing cell widget if present
            # (allocating a fresh widget per row per second is wasteful).
            bar = None
            existing = self.table.cellWidget(row, 1)
            if existing is not None:
                bar = existing.findChild(QProgressBar)
            if bar is None:
                progress_widget = QWidget()
                progress_layout = QHBoxLayout(progress_widget)
                progress_layout.setContentsMargins(4, 2, 4, 2)
                bar = QProgressBar()
                bar.setMinimum(0)
                bar.setMaximum(100)
                bar.setTextVisible(True)
                bar.setFixedHeight(18)
                progress_layout.addWidget(bar)
                self.table.setCellWidget(row, 1, progress_widget)
            # Unknown total size (no Content-Length / HEAD failed): a plain
            # 0% bar looks dead, so animate a busy indicator instead — Qt
            # renders no text on a busy bar, the byte counter lives in the
            # Size column below.
            unknown_size = job.file_size <= 0 and job.job_type not in ("hls", "dash")
            if unknown_size and job.status == JobStatus.DOWNLOADING:
                if bar.maximum() != 0:
                    bar.setRange(0, 0)  # busy indicator
            else:
                if bar.maximum() != 100:
                    bar.setRange(0, 100)
                bar.setValue(int(job.progress * 100))
                if unknown_size and job.status != JobStatus.COMPLETED:
                    bar.setFormat(f"{_format_size(job.downloaded)} so far")
                else:
                    bar.setFormat(f"{job.progress * 100:.1f}%")

            # Speed
            self.table.setItem(row, 2, QTableWidgetItem(_format_speed(job.speed_bps)))

            # ETA
            self.table.setItem(row, 3, QTableWidgetItem(_format_eta(job.eta_seconds)))

            # Status
            status_colors = {
                JobStatus.QUEUED: "#ffcc00",
                JobStatus.DOWNLOADING: "#00ff9d",
                JobStatus.PROCESSING: "#c084fc",
                JobStatus.PAUSED: "#8a9ab0",
                JobStatus.COMPLETED: "#2a7abf",
                JobStatus.ERROR: "#ff3366",
            }
            status_item = QTableWidgetItem(job.status.value.capitalize())
            status_item.setForeground(Qt.GlobalColor.white)
            self.table.setItem(row, 4, status_item)

            # Size — unknown-total jobs show bytes fetched so far instead.
            if job.job_type in ("hls", "dash"):
                size_text = f"{job.file_size} segs" if job.file_size > 0 else "—"
            elif job.file_size > 0:
                size_text = _format_size(job.file_size)
            elif job.downloaded > 0:
                size_text = f"{_format_size(job.downloaded)} / ?"
            else:
                size_text = "—"
            self.table.setItem(row, 5, QTableWidgetItem(size_text))

            # Source URL
            url_text = job.source_url or job.url
            if len(url_text) > 60:
                url_text = url_text[:57] + "..."
            self.table.setItem(row, 6, QTableWidgetItem(url_text))

        # Restore selection.
        if selected_id:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item and item.data(Qt.UserRole) == selected_id:
                    self.table.selectRow(row)
                    break

        # Update details panel.
        self._update_details()

    def _update_details(self) -> None:
        """Update the segment details panel for the selected job.

        The panel only exists for segmented file downloads — stream jobs
        (HLS/DASH/YouTube) don't have byte-range segments, so the panel
        hides itself for them."""
        row = self.table.currentRow()
        job = None
        if 0 <= row < self.table.rowCount():
            item = self.table.item(row, 0)
            if item:
                job = self._engine.get_job(item.data(Qt.UserRole))

        if job is None or job.job_type in ("hls", "dash", "youtube"):
            self.details_group.setVisible(False)
            return
        self.details_group.setVisible(True)

        active_segs = sum(1 for s in job.segments if s.status.value == "active")
        self.details_label.setText(
            f"{job.filename}  |  {len(job.segments)} segments  |  {active_segs} active  |  "
            f"Ranges: {'yes' if job.supports_ranges else 'no'}"
        )

        self.segment_table.setRowCount(len(job.segments))
        for i, seg in enumerate(job.segments):
            # end_byte < 0 = unknown-size streaming segment (plain GET to EOF)
            # — its total is unknown until done, don't show bogus 0/100%.
            streaming = seg.end_byte < 0
            self.segment_table.setItem(i, 0, QTableWidgetItem(str(seg.index)))
            self.segment_table.setItem(i, 1, QTableWidgetItem(
                "stream" if streaming else f"{seg.start_byte:,} - {seg.end_byte:,}"))
            self.segment_table.setItem(i, 2, QTableWidgetItem(
                f"{seg.completed_bytes:,} / ?" if streaming else f"{seg.completed_bytes:,} / {seg.total_bytes:,}"))
            self.segment_table.setItem(i, 3, QTableWidgetItem(
                "—" if streaming and seg.status != SegmentStatus.DONE else f"{seg.progress * 100:.1f}%"))
            self.segment_table.setItem(i, 4, QTableWidgetItem(seg.status.value))

    # ------------------------------------------------------------------
    # Toolbar actions
    # ------------------------------------------------------------------

    def _get_selected_job_id(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0 or row >= self.table.rowCount():
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _add_url_dialog(self) -> None:
        """Open a dialog to add a download URL manually.

        Auto-detects HLS (.m3u8) and DASH (.mpd) URLs and routes them
        to the stream capture pipeline."""
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Add Download URL")
        dialog.setLabelText("Enter the download URL (file, .m3u8, or .mpd):")
        dialog.setTextValue("")
        if dialog.exec() == QInputDialog.Accepted:
            url = dialog.textValue().strip()
            if url:
                try:
                    url_lower = url.lower()
                    if "youtube.com/watch" in url_lower or "youtu.be/" in url_lower:
                        self._engine.add_youtube_job(url=url, source_url=url)
                    elif url_lower.endswith(".m3u8") or url_lower.endswith(".mpd"):
                        self._engine.add_stream_job(url=url)
                    else:
                        self._engine.add_job(url=url)
                except Exception as exc:
                    QMessageBox.warning(self, "Error", f"Failed to add download: {exc}")

    def _pause_selected(self) -> None:
        job_id = self._get_selected_job_id()
        if job_id:
            self._engine.pause_job(job_id)

    def _resume_selected(self) -> None:
        job_id = self._get_selected_job_id()
        if job_id:
            self._engine.resume_job(job_id)

    def _cancel_selected(self) -> None:
        job_id = self._get_selected_job_id()
        if not job_id:
            return
        reply = QMessageBox.question(
            self, "Cancel Download",
            "Cancel this download and delete the partial file?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._engine.cancel_job(job_id)

    def _retry_selected(self) -> None:
        job_id = self._get_selected_job_id()
        if job_id:
            self._engine.retry_job(job_id)

    def _open_selected_file(self) -> None:
        job_id = self._get_selected_job_id()
        if not job_id:
            return
        job = self._engine.get_job(job_id)
        if job and os.path.exists(job.save_path):
            if sys.platform == "win32":
                os.startfile(job.save_path)
            else:
                subprocess.Popen(["xdg-open", job.save_path])

    def _open_selected_folder(self) -> None:
        job_id = self._get_selected_job_id()
        if not job_id:
            return
        job = self._engine.get_job(job_id)
        if job:
            folder = os.path.dirname(job.save_path)
            if os.path.exists(folder):
                if sys.platform == "win32":
                    os.startfile(folder)
                else:
                    subprocess.Popen(["xdg-open", folder])

    def _clear_completed(self) -> None:
        """Remove all completed and errored jobs from the list."""
        for job in self._engine.list_jobs():
            if job.status in (JobStatus.COMPLETED, JobStatus.ERROR):
                self._engine.remove_job(job.id)

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------

    def _context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        self.table.selectRow(row)
        job_id = self._get_selected_job_id()
        if not job_id:
            return
        job = self._engine.get_job(job_id)
        if not job:
            return

        menu = QMenu(self)
        if job.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED):
            menu.addAction("Pause", self._pause_selected)
        if job.status in (JobStatus.PAUSED, JobStatus.ERROR):
            menu.addAction("Resume", self._resume_selected)
        if job.status == JobStatus.ERROR:
            menu.addAction("Retry", self._retry_selected)
        menu.addAction("Cancel", self._cancel_selected)
        menu.addSeparator()
        if self._play_callback is not None and os.path.isfile(job.save_path):
            menu.addAction("Play in Player", self._play_selected)
        menu.addAction("Open File", self._open_selected_file)
        menu.addAction("Open Folder", self._open_selected_folder)
        menu.addSeparator()
        copy_url_action = menu.addAction("Copy URL")
        copy_url_action.triggered.connect(lambda: QApplication.clipboard().setText(job.url))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _on_double_click(self) -> None:
        """Double-click: play completed videos in the Player, else open normally."""
        job_id = self._get_selected_job_id()
        job = self._engine.get_job(job_id) if job_id else None
        if job and self._is_playable(job) and self._play_callback is not None:
            self._play_callback(job.save_path)
        else:
            self._open_selected_file()

    def _play_selected(self) -> None:
        job_id = self._get_selected_job_id()
        if not job_id or self._play_callback is None:
            return
        job = self._engine.get_job(job_id)
        if job and os.path.isfile(job.save_path):
            self._play_callback(job.save_path)
