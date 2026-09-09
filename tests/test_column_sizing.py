"""Auto-fitting, user-draggable column widths for the download tables.

Regression tests for gui/column_sizing.AutoColumnSizer and its wiring into
the Downloads tab: file names must always be fully visible (columns fit
their content), every column must stay user-resizable (Interactive mode),
and a column the user dragged must never be stomped by a later auto-fit
(double-clicking its header separator hands it back)."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

from dlmgr.job import DownloadJob, JobStatus, SegmentState, SegmentStatus
from gui.column_sizing import AutoColumnSizer
from gui.downloads_tab import DownloadsTab, DownloadsTableModel


class FakeEngine:
    def __init__(self, jobs=()):
        self.jobs = {job.id: job for job in jobs}

    def list_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs.get(job_id)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def make_tab(qapp):
    tabs = []

    def make(engine):
        tab = DownloadsTab(engine, SimpleNamespace())
        tab._timer.stop()
        tab._refresh()
        tabs.append(tab)
        return tab

    yield make
    for tab in tabs:
        tab.close()
        tab.deleteLater()
    qapp.processEvents()


def make_job(job_id, status, **kwargs):
    defaults = {
        "filename": f"{job_id}.bin",
        "url": f"https://example.test/{job_id}.bin",
        "save_path": f"C:/downloads/{job_id}.bin",
        "file_size": 1000,
        "downloaded": 250,
        "created_at": float(ord(job_id[0])),
    }
    defaults.update(kwargs)
    return DownloadJob(id=job_id, status=status, **defaults)


def make_widget_table(rows):
    table = QTableWidget(len(rows), 3)
    table.setHorizontalHeaderLabels(["Name", "A", "B"])
    for row, values in enumerate(rows):
        for column, text in enumerate(values):
            table.setItem(row, column, QTableWidgetItem(text))
    return table


# ----------------------------------------------------------------------
# AutoColumnSizer unit behaviour (shared by the torrent + download tables)
# ----------------------------------------------------------------------

def test_all_columns_interactive_and_names_fully_visible(qapp):
    table = make_widget_table([["short.txt", "x", "y"],
                               ["a-very-long-release-name.mkv", "z", "w"]])
    AutoColumnSizer(table, fill_column=0).auto_fit()

    header = table.horizontalHeader()
    for column in range(3):
        assert header.sectionResizeMode(column) == QHeaderView.Interactive
    # The name column is at least as wide as its longest content.
    metrics = table.fontMetrics()
    assert header.sectionSize(0) >= metrics.horizontalAdvance("a-very-long-release-name.mkv") + 10


def test_fill_column_absorbs_leftover_viewport(qapp):
    table = make_widget_table([["n", "1", "2"]])
    sizer = AutoColumnSizer(table, fill_column=0)
    table.resize(800, 140)
    qapp.processEvents()
    sizer.auto_fit()

    header = table.horizontalHeader()
    others = header.sectionSize(1) + header.sectionSize(2)
    assert header.sectionSize(0) == table.viewport().width() - others


def test_user_dragged_column_is_never_stomped_and_double_click_restores(qapp):
    table = make_widget_table([["name.mkv", "1", "2"]])
    sizer = AutoColumnSizer(table, fill_column=0)
    sizer.auto_fit()

    # A resize outside auto_fit is a user drag: the column goes manual.
    table.horizontalHeader().resizeSection(1, 300)
    table.setItem(0, 1, QTableWidgetItem("cell text much longer than before"))
    sizer.auto_fit()
    assert table.horizontalHeader().sectionSize(1) == 300

    # Double-clicking the header separator hands the column back to auto-fit.
    table.horizontalHeader().sectionHandleDoubleClicked.emit(1)
    metrics = table.fontMetrics()
    assert table.horizontalHeader().sectionSize(1) >= metrics.horizontalAdvance(
        "cell text much longer than before") + 10


def test_max_width_clamps_runaway_columns(qapp):
    table = make_widget_table([["n", "https://example.test/" + "x" * 120, "2"]])
    AutoColumnSizer(table, fill_column=0, max_widths={1: 200}).auto_fit()
    assert table.horizontalHeader().sectionSize(1) <= 200


# ----------------------------------------------------------------------
# Downloads tab integration
# ----------------------------------------------------------------------

def test_filename_column_fits_and_tooltip_carries_full_name(make_tab):
    long_name = "A.Really.Long.Release.Name.2026.1080p.BLU-RAY.x264-SOMEGROUP.mkv"
    tab = make_tab(FakeEngine([make_job("dl", JobStatus.DOWNLOADING, filename=long_name)]))

    header = tab.table.horizontalHeader()
    assert header.sectionResizeMode(0) == QHeaderView.Interactive
    assert header.sectionSize(0) >= tab.table.fontMetrics().horizontalAdvance(long_name) + 10
    assert tab._proxy.index(0, 0).data(Qt.ToolTipRole) == long_name


def test_user_column_resize_survives_refresh(make_tab):
    engine = FakeEngine([make_job("a", JobStatus.DOWNLOADING, created_at=2.0)])
    tab = make_tab(engine)
    header = tab.table.horizontalHeader()
    header.resizeSection(2, 260)  # user drags the Speed column wide

    new_name = "newly.added.download.with.a.much.longer.name.mkv"
    engine.jobs["b"] = make_job("b", JobStatus.DOWNLOADING, filename=new_name, created_at=3.0)
    tab._refresh()

    assert header.sectionSize(2) == 260  # manual width untouched
    assert header.sectionSize(0) >= tab.table.fontMetrics().horizontalAdvance(new_name) + 10


def test_search_filter_refits_visible_names(make_tab):
    long_name = "x" * 90 + ".bin"
    engine = FakeEngine([
        make_job("long", JobStatus.DOWNLOADING, filename=long_name, created_at=2.0),
        make_job("short", JobStatus.DOWNLOADING, filename="shortfile.bin", created_at=1.0),
    ])
    tab = make_tab(engine)
    header = tab.table.horizontalHeader()
    wide = header.sectionSize(0)

    tab.search_edit.setText("shortfile")  # only the short name stays visible
    narrow = header.sectionSize(0)
    assert narrow < wide

    tab.search_edit.clear()
    assert header.sectionSize(0) == wide


def test_segment_table_columns_fit_content(make_tab):
    job = make_job(
        "seg",
        JobStatus.DOWNLOADING,
        supports_ranges=True,
        segments=[SegmentState(0, 0, 999, 500, SegmentStatus.ACTIVE)],
    )
    tab = make_tab(FakeEngine([job]))

    index = tab._proxy.index(0, 0)
    selection = tab.table.selectionModel()
    selection.select(
        index,
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
    tab._on_selection_changed()

    assert not tab.details_group.isHidden()
    header = tab.segment_table.horizontalHeader()
    metrics = tab.segment_table.fontMetrics()
    for column in range(tab.segment_table.columnCount()):
        assert header.sectionResizeMode(column) == QHeaderView.Interactive
        text = tab.segment_table.item(0, column).text()
        assert header.sectionSize(column) >= metrics.horizontalAdvance(text) + 10


def test_update_jobs_reports_membership_change(qapp):
    from gui.downloads_tab import DownloadsTableModel

    model = DownloadsTableModel()
    job = make_job("a", JobStatus.DOWNLOADING)
    assert model.update_jobs([job]) is True
    assert model.update_jobs([job]) is False
