"""Tests for the yt-dlp freshness check (dlmgr/ytdlp_update.py) and the
outdated-yt-dlp hint attached to failed YouTube jobs."""
from __future__ import annotations

import time
from unittest import mock

import pytest

from config import DeeptorrentConfig
from dlmgr import ytdlp_update
from dlmgr.engine import DownloadEngine
from dlmgr.job import JobStatus

# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

def test_is_outdated_compares_calver():
    assert ytdlp_update.is_outdated("2026.07.04", "2026.8.19") is True
    assert ytdlp_update.is_outdated("2026.8.19", "2026.8.19") is False
    assert ytdlp_update.is_outdated("2026.9.1", "2026.8.19") is False
    assert ytdlp_update.is_outdated("2027.1.2", "2026.8.19") is False


def test_is_outdated_pads_short_versions():
    assert ytdlp_update.is_outdated("2026.8", "2026.8.19") is True
    assert ytdlp_update.is_outdated("2026.8.19", "2026.8") is False


def test_is_outdated_fails_closed_on_garbage():
    # A malformed version must never produce a false "outdated" alarm.
    assert ytdlp_update.is_outdated("", "2026.8.19") is False
    assert ytdlp_update.is_outdated("garbage", "2026.8.19") is False
    assert ytdlp_update.is_outdated("2026.07.04", "unknown") is False


def test_installed_version_parses():
    version = ytdlp_update.installed_version()
    assert version  # yt-dlp is a hard dependency
    assert ytdlp_update._version_tuple(version) is not None


# ---------------------------------------------------------------------------
# maybe_check_update gating
# ---------------------------------------------------------------------------

def _config(tmp_path, **download_overrides):
    config = DeeptorrentConfig()
    for key, value in download_overrides.items():
        setattr(config.download, key, value)
    return config, str(tmp_path / "config.json")


def test_check_disabled_returns_none(tmp_path):
    config, path = _config(tmp_path, youtube_update_check=False)
    with mock.patch.object(ytdlp_update, "fetch_latest_version") as fetch:
        assert ytdlp_update.maybe_check_update(config, path) is None
    fetch.assert_not_called()


def test_check_daily_gate_skips_recent_probe(tmp_path):
    recent = time.time() - 3600
    config, path = _config(tmp_path, ytdlp_last_check=recent)
    with mock.patch.object(ytdlp_update, "fetch_latest_version") as fetch:
        assert ytdlp_update.maybe_check_update(config, path) is None
    fetch.assert_not_called()
    assert config.download.ytdlp_last_check == recent  # untouched


def test_check_force_bypasses_daily_gate(tmp_path):
    config, path = _config(tmp_path, ytdlp_last_check=time.time() - 60)
    with mock.patch.object(ytdlp_update, "fetch_latest_version",
                           return_value="2030.1.1"), \
         mock.patch.object(ytdlp_update, "installed_version",
                           return_value="2026.07.04"):
        result = ytdlp_update.maybe_check_update(config, path, force=True)
    assert result is not None and result["outdated"] is True


def test_check_reports_outdated_and_persists_gate(tmp_path):
    config, path = _config(tmp_path)
    before = time.time()
    with mock.patch.object(ytdlp_update, "fetch_latest_version",
                           return_value="2026.8.19"), \
         mock.patch.object(ytdlp_update, "installed_version",
                           return_value="2026.07.04"):
        result = ytdlp_update.maybe_check_update(config, path)
    assert result is not None
    assert result["installed"] == "2026.07.04"
    assert result["latest"] == "2026.8.19"
    assert result["outdated"] is True
    assert config.download.ytdlp_last_check >= before
    # The gate was persisted to the explicit path (never the real config).
    reloaded = DeeptorrentConfig.from_file(path)
    assert reloaded.download.ytdlp_last_check >= before


def test_check_current_version_stays_silent_but_gates(tmp_path):
    config, path = _config(tmp_path)
    with mock.patch.object(ytdlp_update, "fetch_latest_version",
                           return_value="2026.07.04"), \
         mock.patch.object(ytdlp_update, "installed_version",
                           return_value="2026.07.04"):
        assert ytdlp_update.maybe_check_update(config, path) is None
    assert config.download.ytdlp_last_check > 0


def test_check_network_failure_returns_none_without_gate(tmp_path):
    config, path = _config(tmp_path)
    with mock.patch.object(ytdlp_update, "fetch_latest_version",
                           side_effect=OSError("network down")):
        assert ytdlp_update.maybe_check_update(config, path) is None
    assert config.download.ytdlp_last_check == 0.0
    assert not (tmp_path / "config.json").exists()


def test_check_without_path_does_not_write(tmp_path):
    config, _ = _config(tmp_path)
    with mock.patch.object(ytdlp_update, "fetch_latest_version",
                           return_value="2026.8.19"), \
         mock.patch.object(ytdlp_update, "installed_version",
                           return_value="2026.07.04"):
        result = ytdlp_update.maybe_check_update(config, None)
    assert result is not None
    assert not (tmp_path / "config.json").exists()


def test_config_round_trips_new_fields(tmp_path):
    config, path = _config(tmp_path, youtube_update_check=False,
                           ytdlp_last_check=123.5)
    config.to_file(path)
    reloaded = DeeptorrentConfig.from_file(path)
    assert reloaded.download.youtube_update_check is False
    assert reloaded.download.ytdlp_last_check == 123.5


def test_config_defaults():
    config = DeeptorrentConfig()
    assert config.download.youtube_update_check is True
    assert config.download.ytdlp_last_check == 0.0


# ---------------------------------------------------------------------------
# YouTube job error hint
# ---------------------------------------------------------------------------

def _wait_status(engine, job_id, status, timeout=10.0):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = engine.get_job(job_id)
        if job and job.status == status:
            return job
        time.sleep(0.05)
    return job


class _FailingYDL:
    """YoutubeDL stand-in whose extract_info raises a DownloadError."""

    message = "ERROR: video unavailable"

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        from yt_dlp.utils import DownloadError
        raise DownloadError(self.message)

    def prepare_filename(self, info):
        raise AssertionError("not reached — extract_info failed")


@pytest.fixture
def engine():
    eng = DownloadEngine(None)
    yield eng
    eng._running = False


def test_youtube_job_403_error_carries_hint(engine, tmp_path):
    """The classic outdated-yt-dlp failure surfaces an actionable hint."""
    _FailingYDL.message = ("ERROR: unable to download video data: "
                           "HTTP Error 403: Forbidden")
    # Patch BEFORE adding: engine(None) auto-starts, and the worker resolves
    # YoutubeDL at call time — the fake must already be in place.
    with mock.patch("yt_dlp.YoutubeDL", _FailingYDL):
        job = engine.add_youtube_job(url="https://www.youtube.com/watch?v=x",
                                     save_path=str(tmp_path / "out.mp4"))
        job = _wait_status(engine, job.id, JobStatus.ERROR)
    assert job is not None and job.status == JobStatus.ERROR
    assert "403" in job.error_message
    assert "outdated" in job.error_message


def test_youtube_job_other_error_has_no_hint(engine, tmp_path):
    _FailingYDL.message = "ERROR: video unavailable"
    with mock.patch("yt_dlp.YoutubeDL", _FailingYDL):
        job = engine.add_youtube_job(url="https://www.youtube.com/watch?v=x",
                                     save_path=str(tmp_path / "out.mp4"))
        job = _wait_status(engine, job.id, JobStatus.ERROR)
    assert job is not None and job.status == JobStatus.ERROR
    assert "outdated" not in job.error_message
