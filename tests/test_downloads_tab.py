from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QTableView

from dlmgr.job import DownloadJob, JobStatus, SegmentState, SegmentStatus
from gui.downloads_tab import DownloadsTab, DownloadsTableModel


class FakeEngine:
    def __init__(self, jobs=()):
        self.jobs = {job.id: job for job in jobs}
        self.calls = []

    def list_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def _record(self, action, job_id):
        self.calls.append((action, job_id))
        return True

    def pause_job(self, job_id):
        return self._record("pause", job_id)

    def resume_job(self, job_id):
        return self._record("resume", job_id)

    def cancel_job(self, job_id):
        return self._record("cancel", job_id)

    def retry_job(self, job_id):
        return self._record("retry", job_id)

    def remove_job(self, job_id):
        return self._record("remove", job_id)


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


def select_ids(tab, *job_ids):
    selection = tab.table.selectionModel()
    selection.clearSelection()
    first = None
    wanted = set(job_ids)
    for row in range(tab._proxy.rowCount()):
        index = tab._proxy.index(row, 0)
        if index.data(DownloadsTableModel.JOB_ID_ROLE) in wanted:
            selection.select(
                index,
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
            first = first or index
    if first is not None:
        selection.setCurrentIndex(first, QItemSelectionModel.SelectionFlag.NoUpdate)
    tab._on_selection_changed()


def source_index_for(tab, job_id, column):
    for row in range(tab._model.rowCount()):
        index = tab._model.index(row, column)
        if index.data(DownloadsTableModel.JOB_ID_ROLE) == job_id:
            return index
    raise AssertionError(f"job {job_id!r} not found")


def test_model_filtering_summary_error_and_url_redaction(make_tab):
    downloading = make_job(
        "download", JobStatus.DOWNLOADING, filename="Movie Night.mkv", speed_bps=2048
    )
    queued = make_job("queued", JobStatus.QUEUED, filename="Linux ISO.iso")
    failed = make_job(
        "failed",
        JobStatus.ERROR,
        filename="private.zip",
        error_message="server rejected request",
        url="https://alice:hunter2@example.test/private.zip?token=abc&part=2",
    )
    tab = make_tab(FakeEngine([downloading, queued, failed]))

    assert isinstance(tab.table, QTableView)
    assert tab.table.selectionMode() == QTableView.ExtendedSelection
    assert "Active: 1" in tab.summary_label.text()
    assert "Queued: 1" in tab.summary_label.text()
    assert "2.0 KB/s" in tab.summary_label.text()

    error_index = source_index_for(tab, "failed", 4)
    assert error_index.data(Qt.DisplayRole) == "Error: server rejected request"
    assert error_index.data(Qt.ToolTipRole) == "server rejected request"

    url_index = source_index_for(tab, "failed", 6)
    displayed = url_index.data(Qt.DisplayRole)
    tooltip = url_index.data(Qt.ToolTipRole)
    assert "hunter2" not in displayed + tooltip
    assert "abc" not in displayed + tooltip
    assert "credentials-redacted" in tooltip
    assert "REDACTED" in tooltip

    tab.search_edit.setText("linux")
    assert tab._proxy.rowCount() == 1
    assert tab._proxy.index(0, 0).data(DownloadsTableModel.JOB_ID_ROLE) == "queued"
    tab.search_edit.clear()
    tab.status_filter.setCurrentIndex(tab.status_filter.findData(JobStatus.ERROR.value))
    assert tab._proxy.rowCount() == 1
    assert tab._proxy.index(0, 0).data(DownloadsTableModel.JOB_ID_ROLE) == "failed"
    assert "Showing: 1/3" in tab.summary_label.text()


def test_selection_is_stable_across_sort_refresh_and_filters(make_tab):
    alpha = make_job("alpha", JobStatus.DOWNLOADING, speed_bps=100)
    beta = make_job("beta", JobStatus.DOWNLOADING, speed_bps=200)
    engine = FakeEngine([alpha, beta])
    tab = make_tab(engine)
    tab.table.sortByColumn(2, Qt.DescendingOrder)
    select_ids(tab, "beta")

    alpha.speed_bps = 500
    beta.speed_bps = 1
    tab._refresh()
    assert tab._get_selected_job_id() == "beta"

    tab.search_edit.setText("alpha")
    assert not tab.table.selectionModel().selectedRows()
    tab.search_edit.clear()
    assert tab._get_selected_job_id() == "beta"
    assert [i.data(DownloadsTableModel.JOB_ID_ROLE)
            for i in tab.table.selectionModel().selectedRows(0)] == ["beta"]


def test_bulk_actions_are_state_aware_and_cancel_is_safe(make_tab, monkeypatch):
    jobs = [
        make_job("queued", JobStatus.QUEUED),
        make_job("downloading", JobStatus.DOWNLOADING),
        make_job("processing", JobStatus.PROCESSING),
        make_job("paused", JobStatus.PAUSED),
        make_job("completed", JobStatus.COMPLETED),
        make_job("failed", JobStatus.ERROR, error_message="boom"),
    ]
    engine = FakeEngine(jobs)
    tab = make_tab(engine)
    select_ids(tab, *(job.id for job in jobs))

    tab._pause_selected()
    assert {job_id for action, job_id in engine.calls if action == "pause"} == {
        "queued", "downloading"
    }
    tab._resume_selected()
    assert {job_id for action, job_id in engine.calls if action == "resume"} == {
        "paused", "failed"
    }
    tab._retry_selected()
    assert [(action, job_id) for action, job_id in engine.calls if action == "retry"] == [
        ("retry", "failed")
    ]
    tab._remove_selected()
    assert {job_id for action, job_id in engine.calls if action == "remove"} == {
        "completed", "failed"
    }

    prompts = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: prompts.append(args) or QMessageBox.Yes,
    )
    tab._cancel_selected()
    assert len(prompts) == 1
    assert {job_id for action, job_id in engine.calls if action == "cancel"} == {
        "queued", "downloading", "processing", "paused", "failed"
    }

    engine.calls.clear()
    select_ids(tab, "completed")
    tab._cancel_selected()
    assert prompts and len(prompts) == 1
    assert engine.calls == []
    assert not tab.cancel_btn.isEnabled()
    assert tab.remove_btn.isEnabled()

    tab.table.clearSelection()
    tab._on_selection_changed()
    tab._pause_selected()
    tab._resume_selected()
    tab._remove_selected()
    assert engine.calls == []


def test_details_and_play_notify_torrent_callbacks(make_tab, tmp_path):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")
    torrent_path = tmp_path / "payload.torrent"
    torrent_path.write_bytes(b"d4:infod4:name4:testee")

    video = make_job(
        "video", JobStatus.COMPLETED, filename="clip.mp4", save_path=str(video_path)
    )
    torrent = make_job(
        "torrent", JobStatus.COMPLETED, filename="payload.torrent", save_path=str(torrent_path)
    )
    segmented = make_job(
        "segment",
        JobStatus.DOWNLOADING,
        supports_ranges=True,
        segments=[SegmentState(0, 0, 999, 500, SegmentStatus.ACTIVE)],
    )
    notifying = make_job("notify", JobStatus.DOWNLOADING)
    engine = FakeEngine([video, torrent, segmented, notifying])
    tab = make_tab(engine)

    played = []
    torrents = []
    notified = []
    tab.set_play_callback(played.append)
    tab.set_torrent_callback(lambda path, job_id: torrents.append((path, job_id)))
    tab.set_notify_callback(notified.append)

    tab._refresh()
    tab._refresh()
    assert torrents == [(str(torrent_path), "torrent")]
    assert notified == []

    notifying.status = JobStatus.COMPLETED
    tab._refresh()
    assert notified == [notifying]

    select_ids(tab, "video")
    tab._play_selected()
    tab._on_double_click()
    assert played == [str(video_path), str(video_path)]

    select_ids(tab, "segment")
    assert tab.details_group.isVisibleTo(tab)
    assert tab.segment_table.rowCount() == 1
    assert "1 segments" in tab.details_label.text()
