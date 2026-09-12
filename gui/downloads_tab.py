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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PySide6.QtCore import (
    QAbstractTableModel,
    QItemSelectionModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dlmgr.engine import DownloadEngine
from dlmgr.job import JobStatus, SegmentStatus
from gui.column_sizing import AutoColumnSizer
from gui.responsive import OverflowRow, ResponsiveRow, shrink_label

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


def _redact_url(url: str) -> str:
    """Return a useful URL for display without credentials or secret query values."""
    if not url:
        return ""
    sensitive = {
        "access_token", "apikey", "api_key", "auth", "authorization",
        "credential", "key", "passwd", "password", "sig", "signature", "token",
    }
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = f":{parts.port}" if parts.port is not None else ""
        except ValueError:
            port = ""
        credentials = "[credentials-redacted]@" if parts.username or parts.password else ""
        netloc = credentials + host + port if parts.netloc else ""
        query = urlencode([
            (key, "REDACTED" if key.lower() in sensitive else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ])
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except (TypeError, ValueError):
        return "[invalid URL]"


_STATUS_COLORS = {
    JobStatus.QUEUED: "#ffcc00",
    JobStatus.DOWNLOADING: "#a8edff",
    JobStatus.PROCESSING: "#c084fc",
    JobStatus.PAUSED: "#8a9ab0",
    JobStatus.COMPLETED: "#2a7abf",
    JobStatus.ERROR: "#ff3366",
}


class DownloadsTableModel(QAbstractTableModel):
    """Read-only job model that updates in place on the one-second refresh."""

    COLUMNS = ("Filename", "Progress", "Speed", "ETA", "Status", "Size", "Source URL", "Category", "Priority")
    JOB_ID_ROLE = int(Qt.UserRole) + 1
    SORT_ROLE = int(Qt.UserRole) + 2
    UNKNOWN_PROGRESS_ROLE = int(Qt.UserRole) + 3

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._jobs = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._jobs)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and 0 <= section < len(self.COLUMNS):
            return self.COLUMNS[section]
        return super().headerData(section, orientation, role)

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def job_at(self, row: int):
        return self._jobs[row] if 0 <= row < len(self._jobs) else None

    def update_jobs(self, jobs) -> bool:
        """Swap in the latest job list; returns True when the membership
        changed (model reset) — the caller then re-fits column widths."""
        jobs = list(jobs)
        old_ids = [job.id for job in self._jobs]
        new_ids = [job.id for job in jobs]
        if old_ids != new_ids:
            self.beginResetModel()
            self._jobs = jobs
            self.endResetModel()
            return True
        self._jobs = jobs
        if jobs:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(jobs) - 1, len(self.COLUMNS) - 1), []
            )
        return False

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._jobs):
            return None
        job = self._jobs[index.row()]
        column = index.column()
        source_url = job.source_url or job.url
        redacted_url = _redact_url(source_url)
        unknown_size = job.file_size <= 0 and job.job_type not in ("hls", "dash")

        if role == self.JOB_ID_ROLE:
            return job.id
        if role == self.UNKNOWN_PROGRESS_ROLE:
            return unknown_size and job.status == JobStatus.DOWNLOADING
        if role == self.SORT_ROLE:
            return (
                job.filename.casefold(),
                job.progress,
                job.speed_bps,
                job.eta_seconds,
                job.status.value,
                job.file_size if job.file_size > 0 else job.downloaded,
                redacted_url.casefold(),
                job.category.casefold(),
                job.priority,
            )[column]
        if role == Qt.ForegroundRole and column == 4:
            return QColor(_STATUS_COLORS.get(job.status, "#ffffff"))
        if role == Qt.ToolTipRole:
            if column == 0:
                return job.filename
            if column == 4 and job.error_message:
                return job.error_message
            if column == 6:
                return redacted_url
            return None
        if role == Qt.TextAlignmentRole and column in (1, 2, 3, 5):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role != Qt.DisplayRole:
            return None

        if column == 0:
            play_mark = "▶ " if _is_playable_job(job) else ""
            return play_mark + job.filename
        if column == 1:
            if unknown_size and job.status == JobStatus.DOWNLOADING:
                return f"{_format_size(job.downloaded)} so far" if job.downloaded else "Downloading…"
            return f"{job.progress * 100:.1f}%"
        if column == 2:
            return _format_speed(job.speed_bps)
        if column == 3:
            return _format_eta(job.eta_seconds)
        if column == 4:
            if job.status == JobStatus.ERROR and job.error_message:
                return f"Error: {job.error_message}"
            return job.status.value.capitalize()
        if column == 5:
            if job.job_type in ("hls", "dash"):
                return f"{job.file_size} segs" if job.file_size > 0 else "—"
            if job.file_size > 0:
                return _format_size(job.file_size)
            if job.downloaded > 0:
                return f"{_format_size(job.downloaded)} / ?"
            return "—"
        if column == 6:
            return redacted_url if len(redacted_url) <= 72 else redacted_url[:69] + "..."
        if column == 7:
            return job.category or "—"
        if column == 8:
            return str(job.priority)
        return None


class DownloadsFilterProxyModel(QSortFilterProxyModel):
    """Combined filename/URL search and exact status filter."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._search = ""
        self._status = ""
        self.setDynamicSortFilter(True)
        self.setSortRole(DownloadsTableModel.SORT_ROLE)
        self.setSortCaseSensitivity(Qt.CaseInsensitive)

    def _set_filter_value(self, attribute: str, value: str) -> None:
        if value == getattr(self, attribute):
            return
        if hasattr(self, "beginFilterChange"):
            self.beginFilterChange()
            setattr(self, attribute, value)
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            setattr(self, attribute, value)
            self.invalidateFilter()

    def set_search(self, text: str) -> None:
        self._set_filter_value("_search", text.strip().casefold())

    def set_status(self, status: str) -> None:
        self._set_filter_value("_status", status)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        job = model.job_at(source_row) if isinstance(model, DownloadsTableModel) else None
        if job is None:
            return False
        if self._status and job.status.value != self._status:
            return False
        if self._search:
            haystack = "\n".join((job.filename, job.category, _redact_url(job.source_url or job.url))).casefold()
            if self._search not in haystack:
                return False
        return True


class ProgressDelegate(QStyledItemDelegate):
    """Paint progress without allocating a widget for every queue row."""

    def paint(self, painter, option, index) -> None:
        progress = QStyleOptionProgressBar()
        progress.rect = option.rect.adjusted(4, 3, -4, -3)
        progress.state = option.state
        progress.direction = option.direction
        progress.fontMetrics = option.fontMetrics
        progress.textAlignment = Qt.AlignCenter
        progress.textVisible = True
        progress.text = str(index.data(Qt.DisplayRole) or "")
        if index.data(DownloadsTableModel.UNKNOWN_PROGRESS_ROLE):
            progress.minimum = 0
            progress.maximum = 0
        else:
            progress.minimum = 0
            progress.maximum = 1000
            progress.progress = int(float(index.data(DownloadsTableModel.SORT_ROLE) or 0) * 1000)
        QApplication.style().drawControl(QStyle.CE_ProgressBar, progress, painter)


_VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
               ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".3gp", ".ogv")


def _is_playable_job(job) -> bool:
    return (job.status == JobStatus.COMPLETED
            and job.save_path.lower().endswith(_VIDEO_EXTS)
            and os.path.isfile(job.save_path))


class DownloadsTab(QWidget):
    """The Downloads tab widget — queue list + toolbar + details panel."""

    VIDEO_EXTS = _VIDEO_EXTS
    PAUSABLE = (JobStatus.DOWNLOADING, JobStatus.QUEUED)
    RESUMABLE = (JobStatus.PAUSED, JobStatus.ERROR)
    CANCELLABLE = (
        JobStatus.QUEUED, JobStatus.DOWNLOADING, JobStatus.PROCESSING,
        JobStatus.PAUSED, JobStatus.ERROR,
    )
    REMOVABLE = (JobStatus.COMPLETED, JobStatus.ERROR)

    def __init__(self, engine: DownloadEngine, config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._config = config
        self._play_callback = None  # set via set_play_callback()
        self._selection_job_ids: set[str] = set()
        self._current_job_id: Optional[str] = None
        self._restoring_selection = False
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

    def _is_playable(self, job) -> bool:
        """Completed video file that exists on disk."""
        return _is_playable_job(job)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # --- Toolbar. ResponsiveRow: the nine buttons sum to ~1030px of
        # minimums — optional ones overflow into a "⋯" menu on narrow
        # windows instead of locking the window wide. Every action also
        # lives in the table's context menu. ---
        toolbar_row = ResponsiveRow()
        toolbar = QHBoxLayout(toolbar_row)
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

        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._remove_selected)
        toolbar.addWidget(self.remove_btn)

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

        layout.addWidget(toolbar_row)
        # Hide-first order: bulk/file conveniences go first, the queue
        # controls (Pause/Resume) stay on the row as long as possible.
        self._toolbar_overflow = OverflowRow(toolbar_row, (
            self.clear_completed_btn, self.open_folder_btn,
            self.open_file_btn, self.retry_btn, self.remove_btn,
            self.cancel_btn, self.resume_btn, self.pause_btn,
        ))

        filters = QHBoxLayout()
        filters.setSpacing(6)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("downloads_search")
        self.search_edit.setPlaceholderText("Search filename or source URL…")
        self.search_edit.setClearButtonEnabled(True)
        filters.addWidget(self.search_edit, 1)
        self.status_filter = QComboBox()
        self.status_filter.setObjectName("downloads_status_filter")
        self.status_filter.addItem("All statuses", "")
        for status in JobStatus:
            self.status_filter.addItem(status.value.capitalize(), status.value)
        filters.addWidget(self.status_filter)
        self.summary_label = QLabel("Active: 0  |  Queued: 0  |  Speed: —")
        self.summary_label.setObjectName("downloads_summary")
        shrink_label(self.summary_label)  # live summary must not grow the window min
        filters.addWidget(self.summary_label)
        layout.addLayout(filters)

        # --- Downloads table ---
        self._model = DownloadsTableModel(self)
        self._proxy = DownloadsFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self.table = QTableView()
        self.table.setModel(self._proxy)
        self.table.setItemDelegateForColumn(1, ProgressDelegate(self.table))
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(lambda _index: self._on_double_click())
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.verticalHeader().setDefaultSectionSize(20)
        # Columns auto-fit to content (filename column always fully shows and
        # absorbs leftover width) yet stay user-draggable; error/URL columns
        # are capped because their full text lives in tooltips.
        self._column_sizer = AutoColumnSizer(
            self.table, fill_column=0, max_widths={4: 260, 6: 380})
        self.search_edit.textChanged.connect(self._set_search_filter)
        self.status_filter.currentIndexChanged.connect(self._set_status_filter)
        layout.addWidget(self.table)

        # --- Details panel (segmented file downloads only; hidden for streams) ---
        self.details_group = QGroupBox("Details")
        details_layout = QVBoxLayout(self.details_group)
        details_layout.setContentsMargins(6, 2, 6, 4)
        details_layout.setSpacing(2)
        self.details_label = QLabel("Select a download to see segment details.")
        self.details_label.setStyleSheet("color: #8a9ab0; font-size: 17px;")
        details_layout.addWidget(self.details_label)

        self.segment_table = QTableWidget()
        self.segment_table.setColumnCount(5)
        self.segment_table.setHorizontalHeaderLabels(["#", "Range", "Downloaded", "Progress", "Status"])
        self.segment_table.setMaximumHeight(110)
        self.segment_table.setAlternatingRowColors(True)
        self.segment_table.verticalHeader().setDefaultSectionSize(18)
        self._segment_sizer = AutoColumnSizer(self.segment_table, fill_column=1)
        details_layout.addWidget(self.segment_table)
        self.details_group.setVisible(False)

        layout.addWidget(self.details_group)
        self._update_action_states()

    def _start_refresh_timer(self) -> None:
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Update the model from the engine's current job list."""
        jobs = self._engine.list_jobs()
        # Sort by created_at descending (newest first).
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        self._check_torrent_jobs(jobs)
        self._check_completed_notifications(jobs)

        known_ids = {job.id for job in jobs}
        selected_ids = self._selection_job_ids & known_ids
        current_id = self._current_job_id if self._current_job_id in known_ids else None
        membership_changed = False
        self._restoring_selection = True
        try:
            membership_changed = self._model.update_jobs(jobs)
            self._restore_selection(selected_ids, current_id)
        finally:
            self._restoring_selection = False
        if membership_changed:
            self._column_sizer.auto_fit()
        self._selection_job_ids = selected_ids
        self._current_job_id = current_id if current_id in selected_ids else next(iter(selected_ids), None)

        self._update_summary(jobs)
        self._update_details()
        self._update_action_states()

    def _update_summary(self, jobs) -> None:
        active = sum(job.status in (JobStatus.DOWNLOADING, JobStatus.PROCESSING) for job in jobs)
        queued = sum(job.status == JobStatus.QUEUED for job in jobs)
        speed = sum(max(0, job.speed_bps) for job in jobs
                    if job.status in (JobStatus.DOWNLOADING, JobStatus.PROCESSING))
        showing = self._proxy.rowCount()
        suffix = f"  |  Showing: {showing}/{len(jobs)}" if showing != len(jobs) else ""
        self.summary_label.setText(
            f"Active: {active}  |  Queued: {queued}  |  Speed: {_format_speed(speed)}{suffix}"
        )

    def _set_search_filter(self, text: str) -> None:
        self._apply_filter(lambda: self._proxy.set_search(text))

    def _set_status_filter(self, _index: int) -> None:
        self._apply_filter(lambda: self._proxy.set_status(self.status_filter.currentData() or ""))

    def _apply_filter(self, change_filter) -> None:
        selected_ids = set(self._selection_job_ids)
        current_id = self._current_job_id
        self._restoring_selection = True
        try:
            change_filter()
            self._restore_selection(selected_ids, current_id)
        finally:
            self._restoring_selection = False
        # The visible row set changed — re-fit widths to what's on screen.
        self._column_sizer.auto_fit()
        self._update_summary(self._engine.list_jobs())
        self._update_details()
        self._update_action_states()

    def _restore_selection(self, job_ids: set[str], current_id: Optional[str]) -> None:
        selection_model = self.table.selectionModel()
        selection_model.clearSelection()
        current_index = QModelIndex()
        for row in range(self._proxy.rowCount()):
            index = self._proxy.index(row, 0)
            job_id = index.data(DownloadsTableModel.JOB_ID_ROLE)
            if job_id in job_ids:
                selection_model.select(
                    index,
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )
                if job_id == current_id or not current_index.isValid():
                    current_index = index
        if current_index.isValid():
            selection_model.setCurrentIndex(
                current_index, QItemSelectionModel.SelectionFlag.NoUpdate
            )
        else:
            selection_model.setCurrentIndex(
                QModelIndex(), QItemSelectionModel.SelectionFlag.NoUpdate
            )

    def _on_selection_changed(self, _selected=None, _deselected=None) -> None:
        if self._restoring_selection:
            return
        self._selection_job_ids = set(self._selected_job_ids_from_view())
        current = self.table.currentIndex()
        self._current_job_id = (
            self._proxy.index(current.row(), 0).data(DownloadsTableModel.JOB_ID_ROLE)
            if current.isValid() else next(iter(self._selection_job_ids), None)
        )
        self._update_details()
        self._update_action_states()

    def _selected_job_ids_from_view(self) -> list[str]:
        indexes = self.table.selectionModel().selectedRows(0)
        indexes.sort(key=lambda index: index.row())
        return [index.data(DownloadsTableModel.JOB_ID_ROLE) for index in indexes if index.isValid()]

    def _selected_jobs(self):
        jobs = []
        for job_id in self._selected_job_ids_from_view():
            job = self._engine.get_job(job_id)
            if job is not None:
                jobs.append(job)
        return jobs

    def _update_action_states(self) -> None:
        jobs = self._selected_jobs()
        statuses = {job.status for job in jobs}
        self.pause_btn.setEnabled(bool(statuses.intersection(self.PAUSABLE)))
        self.resume_btn.setEnabled(bool(statuses.intersection(self.RESUMABLE)))
        self.cancel_btn.setEnabled(bool(statuses.intersection(self.CANCELLABLE)))
        self.retry_btn.setEnabled(JobStatus.ERROR in statuses)
        self.remove_btn.setEnabled(bool(statuses.intersection(self.REMOVABLE)))
        one_job = jobs[0] if len(jobs) == 1 else None
        self.open_file_btn.setEnabled(bool(
            one_job and one_job.status == JobStatus.COMPLETED and os.path.isfile(one_job.save_path)
        ))
        self.open_folder_btn.setEnabled(bool(
            one_job and os.path.isdir(os.path.dirname(one_job.save_path))
        ))
        self.clear_completed_btn.setEnabled(any(
            job.status == JobStatus.COMPLETED for job in self._engine.list_jobs()
        ))

    def _update_details(self) -> None:
        """Update the segment details panel for the selected job.

        The panel only exists for segmented file downloads — stream jobs
        (HLS/DASH/YouTube) don't have byte-range segments, so the panel
        hides itself for them."""
        job_id = self._get_selected_job_id()
        job = self._engine.get_job(job_id) if job_id else None

        if job is None or job.job_type in ("hls", "dash", "youtube"):
            self.details_group.setVisible(False)
            self._segment_fit_job_id = None
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
        # Per-second values ("3.2%" → "100.0%") would jitter the widths if
        # fitted on every tick — fit once per selected job instead.
        if job_id != getattr(self, "_segment_fit_job_id", None):
            self._segment_fit_job_id = job_id
            self._segment_sizer.auto_fit()

    # ------------------------------------------------------------------
    # Toolbar actions
    # ------------------------------------------------------------------

    def _get_selected_job_id(self) -> Optional[str]:
        current = self.table.currentIndex()
        if current.isValid():
            return self._proxy.index(current.row(), 0).data(DownloadsTableModel.JOB_ID_ROLE)
        selected = self._selected_job_ids_from_view()
        return selected[0] if selected else None

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

    def _run_bulk_action(self, method_name: str, allowed_statuses) -> None:
        for job in self._selected_jobs():
            current = self._engine.get_job(job.id)
            if current is None or current.status not in allowed_statuses:
                continue
            try:
                getattr(self._engine, method_name)(job.id)
            except Exception:
                logger.exception("Download action %s failed for %s", method_name, job.id)

    def _pause_selected(self) -> None:
        self._run_bulk_action("pause_job", self.PAUSABLE)

    def _resume_selected(self) -> None:
        self._run_bulk_action("resume_job", self.RESUMABLE)

    def _cancel_selected(self) -> None:
        jobs = [job for job in self._selected_jobs() if job.status in self.CANCELLABLE]
        if not jobs:
            return
        noun = "this download" if len(jobs) == 1 else f"these {len(jobs)} downloads"
        reply = QMessageBox.question(
            self, "Cancel Download" if len(jobs) == 1 else "Cancel Downloads",
            f"Cancel {noun} and delete the partial file{'s' if len(jobs) != 1 else ''}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._run_bulk_action("cancel_job", self.CANCELLABLE)

    def _retry_selected(self) -> None:
        self._run_bulk_action("retry_job", (JobStatus.ERROR,))

    def _remove_selected(self) -> None:
        self._run_bulk_action("remove_job", self.REMOVABLE)

    def _set_selected_priority(self, priority: int) -> None:
        for job in self._selected_jobs():
            self._engine.set_job_priority(job.id, priority)
        self._refresh()

    def _open_selected_file(self) -> None:
        jobs = self._selected_jobs()
        if len(jobs) != 1:
            return
        job = jobs[0]
        if job.status == JobStatus.COMPLETED and os.path.isfile(job.save_path):
            if sys.platform == "win32":
                os.startfile(job.save_path)
            else:
                subprocess.Popen(["xdg-open", job.save_path])

    def _open_selected_folder(self) -> None:
        jobs = self._selected_jobs()
        if len(jobs) != 1:
            return
        folder = os.path.dirname(jobs[0].save_path)
        if os.path.exists(folder):
            if sys.platform == "win32":
                os.startfile(folder)
            else:
                subprocess.Popen(["xdg-open", folder])

    def _clear_completed(self) -> None:
        """Remove all completed jobs from the list."""
        for job in self._engine.list_jobs():
            if job.status == JobStatus.COMPLETED:
                try:
                    self._engine.remove_job(job.id)
                except Exception:
                    logger.exception("Failed to remove completed job %s", job.id)

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------

    def _context_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        row_index = self._proxy.index(index.row(), 0)
        clicked_id = row_index.data(DownloadsTableModel.JOB_ID_ROLE)
        if clicked_id not in self._selected_job_ids_from_view():
            self.table.selectionModel().select(
                row_index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
            self.table.setCurrentIndex(row_index)
        jobs = self._selected_jobs()
        if not jobs:
            return
        clicked_job = self._engine.get_job(clicked_id)
        if clicked_job is None:
            return

        statuses = {job.status for job in jobs}
        menu = QMenu(self)
        if statuses.intersection(self.PAUSABLE):
            menu.addAction("Pause", self._pause_selected)
        if statuses.intersection(self.RESUMABLE):
            menu.addAction("Resume", self._resume_selected)
        if JobStatus.ERROR in statuses:
            menu.addAction("Retry", self._retry_selected)
        if statuses.intersection(self.CANCELLABLE):
            menu.addAction("Cancel", self._cancel_selected)
        if statuses.intersection(self.REMOVABLE):
            menu.addAction("Remove from List", self._remove_selected)
        priority_menu = menu.addMenu("Priority")
        for label, value in (("Highest", 10), ("High", 5), ("Normal", 0), ("Low", -5), ("Lowest", -10)):
            priority_menu.addAction(label, lambda checked=False, priority=value: self._set_selected_priority(priority))
        menu.addSeparator()
        if len(jobs) == 1 and self._play_callback is not None and self._is_playable(clicked_job):
            menu.addAction("Play in Player", self._play_selected)
        if (len(jobs) == 1 and clicked_job.status == JobStatus.COMPLETED
                and os.path.isfile(clicked_job.save_path)):
            menu.addAction("Open File", self._open_selected_file)
        if len(jobs) == 1 and os.path.isdir(os.path.dirname(clicked_job.save_path)):
            menu.addAction("Open Folder", self._open_selected_folder)
        menu.addSeparator()
        copy_url_action = menu.addAction("Copy URL")
        copy_url_action.triggered.connect(lambda: QApplication.clipboard().setText(clicked_job.url))
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
        if job and self._is_playable(job):
            self._play_callback(job.save_path)
