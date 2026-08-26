"""Tests for the segmented download engine (dlmgr): unknown-size downloads,
range headers, save-path dedup, speed sampling, auto-retry, bandwidth limit,
and HLS cancel cleanup.
"""
from __future__ import annotations

import os
import time
from unittest import mock

import pytest

from dlmgr.engine import DownloadEngine, _BandwidthLimiter, MAX_SEGMENT_RETRIES
from dlmgr.job import DownloadJob, JobStatus, SegmentState, SegmentStatus
from dlmgr.segment import SegmentWorker


class _FakeResp:
    """Minimal streaming-response stub for requests.get."""

    def __init__(self, data: bytes):
        self._data = data
        self.headers = {}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def engine():
    eng = DownloadEngine(None)
    yield eng
    eng._running = False


# ---------------------------------------------------------------------------
# SegmentWorker
# ---------------------------------------------------------------------------

def test_unknown_size_download_writes_new_file(tmp_path):
    """Unknown-size jobs (no pre-allocation) must not crash and must not send
    a Range header — the worker streams to EOF and marks the segment done."""
    dest = str(tmp_path / "file.bin")
    data = b"x" * 150_000
    seg = SegmentState(index=0, start_byte=0, end_byte=-1)  # unknown size
    done = []
    w = SegmentWorker("http://x/f", seg, dest, on_done=lambda ok, err: done.append(ok))
    with mock.patch("dlmgr.segment.http_client.get", return_value=_FakeResp(data)) as g:
        w.run()
    assert seg.status == SegmentStatus.DONE
    assert done == [True]
    assert os.path.getsize(dest) == len(data)
    assert "Range" not in g.call_args.kwargs["headers"]


def test_known_size_sends_range_header(tmp_path):
    dest = str(tmp_path / "file.bin")
    open(dest, "wb").close()  # pre-allocated (empty)
    data = b"y" * 1000
    seg = SegmentState(index=0, start_byte=0, end_byte=999)
    w = SegmentWorker("http://x/f", seg, dest)
    with mock.patch("dlmgr.segment.http_client.get", return_value=_FakeResp(data)) as g:
        w.run()
    assert seg.status == SegmentStatus.DONE
    assert g.call_args.kwargs["headers"]["Range"] == "bytes=0-999"
    with open(dest, "rb") as f:
        assert f.read() == data


# ---------------------------------------------------------------------------
# Engine: save-path dedup, speed sampling, auto-retry, cancel cleanup
# ---------------------------------------------------------------------------

def test_unique_save_path_dedupes(engine, tmp_path):
    existing = tmp_path / "movie.mp4"
    existing.write_bytes(b"old")
    p1 = engine._unique_save_path(str(tmp_path), "movie.mp4")
    assert p1.endswith("movie (1).mp4")
    # A second call with a job holding p1 yields (2).
    job = DownloadJob(filename="movie (1).mp4", save_path=p1)
    engine._jobs[job.id] = job
    p2 = engine._unique_save_path(str(tmp_path), "movie.mp4")
    assert p2.endswith("movie (2).mp4")


def test_speed_and_eta_from_deltas(engine):
    job = DownloadJob(filename="f", save_path="f", file_size=100_000_000, downloaded=0)
    job.status = JobStatus.DOWNLOADING
    engine._jobs[job.id] = job
    engine._update_speeds_and_eta()
    assert job.speed_bps == 0  # first sample
    time.sleep(0.05)
    job.downloaded = 1_000_000
    engine._update_speeds_and_eta()
    assert job.speed_bps > 0
    assert job.eta_seconds > 0
    # Inactive jobs report zero and drop their sample.
    job.status = JobStatus.COMPLETED
    engine._update_speeds_and_eta()
    assert job.speed_bps == 0 and job.id not in engine._speed_samples


def test_unknown_size_job_adopts_file_size_on_done(engine):
    job = DownloadJob(filename="f", save_path="f", file_size=0)
    job.status = JobStatus.DOWNLOADING
    job.segments = [SegmentState(index=0, start_byte=0, end_byte=-1, completed_bytes=1234)]
    job.segments[0].status = SegmentStatus.DONE
    engine._jobs[job.id] = job
    engine._on_segment_done(job.id, True, "")
    assert job.file_size == 1234
    assert job.status == JobStatus.COMPLETED


def test_auto_retry_restarts_errored_segments(engine):
    job = DownloadJob(filename="f", save_path="f", file_size=100, job_type="file")
    job.status = JobStatus.DOWNLOADING
    seg = SegmentState(index=0, start_byte=0, end_byte=99, status=SegmentStatus.ERROR)
    job.segments = [seg]
    engine._jobs[job.id] = job
    engine._job_throttles[job.id] = __import__("threading").Event()
    with mock.patch("dlmgr.engine.SegmentWorker") as SW:
        engine._auto_retry_errors()
        assert SW.called  # a replacement worker was started
    assert seg.status == SegmentStatus.PENDING
    assert seg.retries == 1
    # Exhausted retries are not restarted.
    seg.status = SegmentStatus.ERROR
    seg.retries = MAX_SEGMENT_RETRIES
    with mock.patch("dlmgr.engine.SegmentWorker") as SW:
        engine._auto_retry_errors()
        assert not SW.called


def test_cancel_hls_job_removes_temp_segments(engine, tmp_path):
    save = str(tmp_path / "stream.mp4")
    temp_dir = save + ".segments"
    os.makedirs(temp_dir)
    open(os.path.join(temp_dir, "seg_00000.ts"), "wb").write(b"x")
    job = DownloadJob(filename="stream.mp4", save_path=save, job_type="hls")
    job.status = JobStatus.DOWNLOADING
    engine._jobs[job.id] = job
    engine.cancel_job(job.id)
    assert job.id not in engine._jobs
    assert not os.path.isdir(temp_dir)


# ---------------------------------------------------------------------------
# HLS manifest parsing
# ---------------------------------------------------------------------------

_MASTER_M3U8 = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=246440,RESOLUTION=320x184
ld/video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6221600,RESOLUTION=1920x1080
fhd/video.m3u8
"""

_MEDIA_M3U8 = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:10.0,
seg0.ts
#EXTINF:10.0,
seg1.ts
#EXT-X-ENDLIST
"""


class _FakeTextResp:
    def __init__(self, text: str):
        self.text = text
        self.content = text.encode()

    def raise_for_status(self):
        pass


def test_hls_master_playlist_tuple_resolution():
    """m3u8 returns stream_info.resolution as a (w, h) tuple — parsing a
    master playlist with RESOLUTION attributes must not crash."""
    from dlmgr.hls_dash import parse_manifest

    def fake_get(url, **kwargs):
        if url.endswith("video.m3u8"):
            return _FakeTextResp(_MEDIA_M3U8)
        return _FakeTextResp(_MASTER_M3U8)

    with mock.patch("dlmgr.hls_dash.http_client.get", side_effect=fake_get):
        info = parse_manifest("https://cdn.example.com/abc/playlist.m3u8")
    assert info.error == ""
    assert len(info.renditions) == 2
    by_bw = {r.bandwidth: r for r in info.renditions}
    assert by_bw[6221600].resolution == "1920x1080"
    assert by_bw[6221600].width == 1920 and by_bw[6221600].height == 1080
    # Best rendition's media playlist was followed and segments resolved
    # against the sub-playlist URL.
    assert info.manifest_url == "https://cdn.example.com/abc/fhd/video.m3u8"
    assert info.segment_urls == [
        "https://cdn.example.com/abc/fhd/seg0.ts",
        "https://cdn.example.com/abc/fhd/seg1.ts",
    ]
    # A browser-like User-Agent is applied by default.
    from dlmgr.hls_dash import DEFAULT_USER_AGENT
    with mock.patch("dlmgr.hls_dash.http_client.get", side_effect=fake_get) as g:
        parse_manifest("https://cdn.example.com/abc/playlist.m3u8")
    assert g.call_args.kwargs["headers"]["User-Agent"] == DEFAULT_USER_AGENT


# ---------------------------------------------------------------------------
# Bandwidth limiter
# ---------------------------------------------------------------------------

def test_bandwidth_limiter_throttles():
    limiter = _BandwidthLimiter(100_000)  # 100 KB/s
    limiter.acquire(100_000)  # first burst is instant (full bucket)
    t0 = time.monotonic()
    limiter.acquire(100_000)
    assert time.monotonic() - t0 >= 0.8


def test_bandwidth_limiter_disabled():
    limiter = _BandwidthLimiter(0)
    t0 = time.monotonic()
    limiter.acquire(10_000_000)
    assert time.monotonic() - t0 < 0.2

