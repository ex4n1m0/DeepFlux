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
from dlmgr import http_client, resume_state


class _FakeResp:
    """Minimal streaming-response stub for requests.get."""

    def __init__(self, data: bytes, status_code: int = 200, headers=None):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}

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


def test_stop_is_immediate_mid_monitor_sleep(tmp_path):
    """2026-11-17 boot/shutdown study: the monitor loop's inter-pass wait
    MUST be interruptible. It used to be a bare time.sleep(2.0), so
    DownloadEngine.stop() — which runs on every app close — blocked for the
    REMAINDER of the interval (measured median ~1 s, max ~1.85 s): the
    whole 'app hangs for a second or two on close' report."""
    import time as _time

    eng = DownloadEngine(None)
    # Never read/scan the real machine's download folders or registry.
    eng._registry_path = str(tmp_path / "folders.json")
    eng._default_folder = str(tmp_path / "dl")
    eng._auto_start = False
    eng.start()
    _time.sleep(0.3)   # the monitor is now ~0.3 s into its 2 s wait
    t0 = _time.perf_counter()
    eng.stop()
    elapsed = _time.perf_counter() - t0
    assert eng._monitor_thread is not None
    assert not eng._monitor_thread.is_alive()
    # Broken shape blocked ~1.7 s from this offset; 0.6 s leaves CI headroom
    # while still failing loudly on a regression to the bare sleep.
    assert elapsed < 0.6, f"stop() took {elapsed:.2f}s — monitor wait is not interruptible"

    # A stopped engine can start again (the wake event must reset).
    eng.start()
    _time.sleep(0.1)
    t0 = _time.perf_counter()
    eng.stop()
    assert _time.perf_counter() - t0 < 0.6


def test_http_client_revalidates_redirect_destinations():
    response = mock.MagicMock(status_code=302)
    response.headers = {"Location": "http://127.0.0.1/private"}

    def validate(url):
        if "127.0.0.1" in url:
            raise ValueError("private")
        return url

    with mock.patch.object(http_client, "validate_public_url", side_effect=validate), \
            mock.patch.object(http_client, "_request_once", return_value=response) as request:
        with pytest.raises(ValueError, match="private"):
            http_client.get("https://example.com/start")

    request.assert_called_once()
    response.close.assert_called_once()


def test_http_client_rejects_private_and_credential_urls():
    with pytest.raises(Exception, match="Local/private"):
        http_client.validate_public_url("http://127.0.0.1/private")
    with pytest.raises(Exception, match="credentials"):
        http_client.validate_public_url("https://user:pass@example.com/file")


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
    response = _FakeResp(data, status_code=206, headers={"Content-Range": "bytes 0-999/1000"})
    with mock.patch("dlmgr.segment.http_client.get", return_value=response) as g:
        w.run()
    assert seg.status == SegmentStatus.DONE
    assert g.call_args.kwargs["headers"]["Range"] == "bytes=0-999"
    with open(dest, "rb") as f:
        assert f.read() == data


def test_range_response_must_be_partial_and_match_requested_window(tmp_path):
    dest = str(tmp_path / "file.bin")
    with open(dest, "wb") as f:
        f.truncate(1000)
    seg = SegmentState(index=0, start_byte=500, end_byte=999)
    response = _FakeResp(b"x" * 1000, status_code=200)
    done = []
    w = SegmentWorker("http://x/f", seg, dest, on_done=lambda ok, err: done.append((ok, err)))

    with mock.patch("dlmgr.segment.http_client.get", return_value=response):
        w.run()

    assert seg.status == SegmentStatus.ERROR
    assert done and done[0][0] is False
    assert "range" in done[0][1].lower()
    assert os.path.getsize(dest) == 1000
    assert seg.completed_bytes == 0


def test_range_response_rejects_wrong_content_range(tmp_path):
    dest = str(tmp_path / "file.bin")
    with open(dest, "wb") as f:
        f.truncate(1000)
    seg = SegmentState(index=0, start_byte=500, end_byte=999)
    response = _FakeResp(
        b"x" * 500,
        status_code=206,
        headers={"Content-Range": "bytes 0-499/1000"},
    )
    w = SegmentWorker("http://x/f", seg, dest)

    with mock.patch("dlmgr.segment.http_client.get", return_value=response):
        w.run()

    assert seg.status == SegmentStatus.ERROR
    assert seg.completed_bytes == 0


def test_non_range_worker_restarts_from_zero_without_range_header(tmp_path):
    dest = str(tmp_path / "file.bin")
    with open(dest, "wb") as f:
        f.write(b"old-prefix")
    data = b"replacement"
    seg = SegmentState(index=0, start_byte=0, end_byte=len(data) - 1, completed_bytes=4)
    response = _FakeResp(data)
    w = SegmentWorker("http://x/f", seg, dest, use_range=False)

    with mock.patch("dlmgr.segment.http_client.get", return_value=response) as get:
        w.run()

    assert "Range" not in get.call_args.kwargs["headers"]
    assert seg.status == SegmentStatus.DONE
    assert seg.completed_bytes == len(data)
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


def test_prepare_save_path_sanitizes_names_and_uses_destination_folder(engine, tmp_path):
    destination = os.path.join(str(tmp_path), "")
    path, category = engine._prepare_save_path("../../CON?.zip", destination)

    assert category == ""
    assert os.path.dirname(path) == str(tmp_path)
    assert "?" not in os.path.basename(path)
    assert ".." not in os.path.basename(path)


def test_update_settings_applies_live(engine, tmp_path):
    config = mock.MagicMock(
        max_concurrent=7,
        max_connections_per_download=12,
        default_folder=str(tmp_path),
        segment_threshold_mb=5,
        auto_start=False,
        bandwidth_limit_bps=12345,
    )

    engine.update_settings(config)

    assert engine._max_concurrent == 7
    assert engine._max_connections == 12
    assert engine._default_folder == str(tmp_path)
    assert engine._segment_threshold == 5
    assert engine._auto_start is False
    assert engine._bandwidth_limit == 12345


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


def test_resume_non_range_job_restarts_from_zero(engine):
    job = DownloadJob(filename="f", save_path="f", file_size=100, downloaded=40)
    job.status = JobStatus.PAUSED
    job.supports_ranges = False
    job.segments = [SegmentState(index=0, start_byte=0, end_byte=99, completed_bytes=40)]
    engine._jobs[job.id] = job
    engine._job_throttles[job.id] = __import__("threading").Event()

    with mock.patch.object(engine, "_try_start_jobs"):
        assert engine.resume_job(job.id)

    assert job.downloaded == 0
    assert job.segments[0].completed_bytes == 0
    assert job.segments[0].status == SegmentStatus.PENDING


def test_auto_start_false_holds_stream_job(engine, tmp_path):
    engine._auto_start = False
    info = mock.MagicMock()
    info.error = ""
    info.segment_urls = ["https://example.com/1.ts"]
    info.stream_type = "hls"

    with mock.patch("dlmgr.hls_dash.parse_manifest", return_value=info), \
            mock.patch.object(engine, "_start_stream_job") as start:
        job = engine.add_stream_job("https://example.com/master.m3u8", save_path=str(tmp_path / "v.mp4"))

    assert job.status == JobStatus.PAUSED
    start.assert_not_called()


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


def test_remove_errored_stream_job_cleans_segment_dirs(engine, tmp_path):
    # Regression: remove_job used shutil.rmtree without importing shutil in
    # this module (NameError) — removing an incomplete stream job crashed and
    # re-orphaned the multi-GB .segments dirs (review 2026-11-06).
    save = str(tmp_path / "stream.mp4")
    seg_dir = save + ".segments"
    audio_dir = save + ".segments.audio"
    os.makedirs(seg_dir)
    os.makedirs(audio_dir)
    job = DownloadJob(filename="stream.mp4", save_path=save, job_type="hls")
    job.status = JobStatus.ERROR
    engine._jobs[job.id] = job
    with mock.patch.object(resume_state, "delete_resume_state"):
        assert engine.remove_job(job.id) is True
    assert job.id not in engine._jobs
    assert not os.path.isdir(seg_dir)
    assert not os.path.isdir(audio_dir)


def test_resume_state_encrypts_request_credentials(tmp_path):
    job = DownloadJob(
        url="https://example.com/file?token=signed-secret",
        filename="file.bin",
        save_path=str(tmp_path / "file.bin"),
        headers={"Authorization": "Bearer header-secret"},
        cookies="session=cookie-secret",
        referrer="https://example.com/private",
        source_url="https://example.com/page?key=source-secret",
        status=JobStatus.PAUSED,
        segments=[SegmentState(index=0, start_byte=0, end_byte=9, completed_bytes=4)],
    )

    with mock.patch.object(resume_state, "_credential_key_path", return_value=str(tmp_path / "dlmgr.key")):
        resume_state.save_resume_state(job)
        raw = open(job.resume_file_path, encoding="utf-8").read()
        restored = resume_state.load_resume_state(job.resume_file_path)

    for secret in ("signed-secret", "header-secret", "cookie-secret", "source-secret"):
        assert secret not in raw
    assert restored is not None
    assert restored.url == job.url
    assert restored.headers == job.headers
    assert restored.cookies == job.cookies
    assert restored.referrer == job.referrer
    assert restored.source_url == job.source_url


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


def test_hls_media_sequence_and_duration_are_preserved():
    from dlmgr.hls_dash import parse_manifest

    playlist = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:42
#EXT-X-TARGETDURATION:6
#EXTINF:6.0,
a.ts
#EXTINF:4.5,
b.ts
#EXT-X-ENDLIST
"""
    with mock.patch("dlmgr.hls_dash.http_client.get", return_value=_FakeTextResp(playlist)):
        info = parse_manifest("https://cdn.example.com/media.m3u8")

    assert info.media_sequence == 42
    assert info.duration_seconds == 10.5


def test_dash_duration_bounds_numbered_segment_generation():
    from dlmgr.hls_dash import parse_manifest

    manifest = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
  <Period><AdaptationSet contentType="video">
    <SegmentTemplate timescale="1" duration="2" startNumber="1" media="v-$Number$.m4s" initialization="init.mp4"/>
    <Representation id="v1" bandwidth="1000" width="640" height="360"/>
  </AdaptationSet></Period>
</MPD>"""
    with mock.patch("dlmgr.hls_dash.http_client.get", return_value=_FakeTextResp(manifest)):
        info = parse_manifest("https://cdn.example.com/video.mpd")

    assert info.error == ""
    assert info.duration_seconds == 10
    assert len(info.segment_urls) == 5
    assert info.segment_urls[-1].endswith("v-5.m4s")


# ---------------------------------------------------------------------------
# Bandwidth limiter
# ---------------------------------------------------------------------------

def test_bandwidth_limiter_throttles():
    limiter = _BandwidthLimiter(100_000)  # 100 KB/s
    limiter.acquire(100_000)  # first burst is instant (full bucket)
    t0 = time.monotonic()
    limiter.acquire(100_000)
    assert time.monotonic() - t0 >= 0.8


def test_bandwidth_limiter_oversized_request_does_not_hang():
    """Regression (2026-11-09 review, H2): a chunk larger than the configured
    limit (engine chunk = 64 KiB, GUI accepts 1–63 KB/s) can never accumulate
    enough allowance — acquire() used to spin forever, silently freezing every
    file download. The oversized request must be granted once the bucket is
    full; the thread+event shape makes a regression FAIL instead of hanging
    the suite."""
    import threading
    limiter = _BandwidthLimiter(32_768)  # 32 KB/s < one 64 KiB chunk
    limiter._allowance = float(limiter._limit)  # bucket full
    done = threading.Event()

    def _acquire():
        limiter.acquire(65_536)
        done.set()

    t = threading.Thread(target=_acquire, daemon=True)
    t.start()
    assert done.wait(timeout=5.0), "acquire(65536) hung with a 32768 bps limit"
    assert limiter._allowance == 0.0  # the full bucket was drained by the grant


def test_bandwidth_limiter_disabled():
    limiter = _BandwidthLimiter(0)
    t0 = time.monotonic()
    limiter.acquire(10_000_000)
    assert time.monotonic() - t0 < 0.2


# ---------------------------------------------------------------------------
# Per-call HLS rendition caps (download_video's max_height, 2026-11-17).
# ---------------------------------------------------------------------------

def _renditions():
    from types import SimpleNamespace
    return [SimpleNamespace(height=1080, bandwidth=5_000_000),
            SimpleNamespace(height=720, bandwidth=2_000_000),
            SimpleNamespace(height=0, bandwidth=900_000)]


def test_stream_rendition_cap_per_call_beats_config():
    from types import SimpleNamespace
    stub = SimpleNamespace(_config=SimpleNamespace(stream_max_height=1080))
    # No per-call cap: the engine-wide config cap rules.
    assert DownloadEngine._select_stream_rendition(stub, _renditions()).height == 1080
    # Per-call cap wins over the config.
    assert DownloadEngine._select_stream_rendition(stub, _renditions(), 720).height == 720
    # Unknown-height renditions stay eligible under any cap (existing
    # semantics); a cap nothing else satisfies never returns an empty pick.
    picked = DownloadEngine._select_stream_rendition(stub, _renditions(), 480)
    assert picked.height == 0 and picked.bandwidth == 900_000


def test_add_stream_job_per_call_cap_reaches_selector_and_job(engine):
    info = mock.MagicMock()
    info.error = ""
    info.segment_urls = ["https://example.com/1.ts"]
    info.stream_type = "hls"
    engine._auto_start = False
    with mock.patch("dlmgr.hls_dash.parse_manifest", return_value=info) as pm:
        job = engine.add_stream_job("https://example.com/master.m3u8", max_height=720)
    selector = pm.call_args.kwargs["rendition_selector"]
    assert selector(_renditions()).height == 720
    # The cap is recorded so restarts (manifest re-parse) keep it.
    assert job.max_height == 720


# ---------------------------------------------------------------------------
# Dead-segment tolerance (owner 2026-11-17): a CDN that 403s a handful of
# segments deterministically (measured live: 1 dead object out of 1920 on
# surrit) completes the capture with the gaps skipped instead of failing
# the whole job; beyond the tolerance it still fails loudly.
# ---------------------------------------------------------------------------

def _fake_hls_info(n):
    from dlmgr.hls_dash import StreamInfo
    return StreamInfo(stream_type="hls", manifest_url="https://x.example/m.m3u8",
                      segment_urls=[f"https://x.example/s{i}.ts" for i in range(n)])


def test_hls_dead_segment_within_tolerance_completes(tmp_path, monkeypatch):
    from dlmgr.hls_dash import HLSDownloader

    def fake_dl(self, url, path, index, decrypt=True):
        if index == 3:
            raise RuntimeError("HTTP Error 403")
        with open(path, "wb") as f:
            f.write(b"x" * 8)

    # 300 segments -> tolerance 1 (0.5%): one dead segment completes.
    monkeypatch.setattr(HLSDownloader, "_download_segment", fake_dl)
    dl = HLSDownloader(stream_info=_fake_hls_info(300), temp_dir=str(tmp_path / "segs"))
    paths = dl.download()
    assert len(paths) == 299  # seg 3 skipped, the rest returned in order
    assert len(dl.failed_segments) == 1 and dl.failed_segments[0][0] == 3


def test_hls_dead_segments_beyond_tolerance_still_fail(tmp_path, monkeypatch):
    from dlmgr.hls_dash import HLSDownloader

    def fake_dl(self, url, path, index, decrypt=True):
        if index in (2, 7):
            raise RuntimeError("HTTP Error 403")
        with open(path, "wb") as f:
            f.write(b"x" * 8)

    # 300 segments, 2 dead -> beyond the 0.5% tolerance: still a hard error.
    monkeypatch.setattr(HLSDownloader, "_download_segment", fake_dl)
    dl = HLSDownloader(stream_info=_fake_hls_info(300), temp_dir=str(tmp_path / "segs"))
    with pytest.raises(RuntimeError, match="segments failed"):
        dl.download()


def test_hls_tolerance_is_zero_for_short_streams():
    from dlmgr.hls_dash import failed_segment_tolerance
    assert failed_segment_tolerance(2) == 0    # half a short video must fail
    assert failed_segment_tolerance(199) == 0
    assert failed_segment_tolerance(200) == 1
    assert failed_segment_tolerance(1920) == 9



# -- RAM bomb fix (2026-11-20: a 9 GB ISO made the process hold ~10 GB) -------

class _FakeQueue:
    def __init__(self, size):
        self._size = size

    def qsize(self):
        return self._size


class _BloatyResp(_FakeResp):
    """curl_cffi-shaped stub: a real queue attribute whose qsize() reports
    a runaway producer buffer (> segment._MAX_QUEUED_CHUNKS)."""

    def __init__(self, data, buffered_chunks, status_code=206, headers=None):
        super().__init__(data, status_code=status_code, headers=headers)
        self.queue = _FakeQueue(buffered_chunks)
        self.closed = False

    def close(self):
        self.closed = True


def test_segment_worker_sheds_runaway_buffer(tmp_path):
    """The guard: when curl_cffi's unbounded producer queue swells past
    _MAX_QUEUED_CHUNKS because the consumer fell behind, the worker ABORTS
    (status PENDING, response closed) instead of letting the whole
    remaining response park in RAM. Bytes already written survive."""
    dest = str(tmp_path / "file.bin")
    open(dest, "wb").close()
    # ≥ 64 chunks of 64 KiB so the every-64-chunks guard checkpoint fires
    data = b"z" * (70 * 65536)
    seg = SegmentState(index=0, start_byte=0, end_byte=len(data) - 1)
    w = SegmentWorker("http://x/f", seg, dest)
    resp = _BloatyResp(
        data, buffered_chunks=10_000,
        headers={"Content-Range": f"bytes 0-{len(data)-1}/{len(data)}"})
    with mock.patch("dlmgr.segment.http_client.get", return_value=resp):
        w.run()
    assert seg.status == SegmentStatus.PENDING
    assert resp.closed is True                      # transfer aborted
    # The guard trips at the 64-chunk checkpoint: 64 chunks are on disk,
    # the remaining ~6 chunks of buffered data were shed (not written).
    assert seg.completed_bytes == 64 * 65536
    with open(dest, "rb") as f:
        assert f.read() == data[: 64 * 65536]


def test_segment_worker_unbothered_by_small_buffer(tmp_path):
    """A well-behaved queue (or a plain requests response with NO queue
    attribute at all) must complete normally — the guard never trips."""
    dest = str(tmp_path / "file.bin")
    open(dest, "wb").close()
    data = b"y" * (70 * 65536)
    seg = SegmentState(index=0, start_byte=0, end_byte=len(data) - 1)
    w = SegmentWorker("http://x/f", seg, dest)
    resp = _BloatyResp(
        data, buffered_chunks=4,
        headers={"Content-Range": f"bytes 0-{len(data)-1}/{len(data)}"})
    with mock.patch("dlmgr.segment.http_client.get", return_value=resp):
        w.run()
    assert seg.status == SegmentStatus.DONE   # guard never tripped


def test_segment_worker_passes_socket_speed_to_http_client(tmp_path):
    dest = str(tmp_path / "file.bin")
    open(dest, "wb").close()
    data = b"y" * 1000
    seg = SegmentState(index=0, start_byte=0, end_byte=999)
    w = SegmentWorker("http://x/f", seg, dest, max_recv_speed=12345)
    resp = _FakeResp(data, status_code=206,
                     headers={"Content-Range": "bytes 0-999/1000"})
    with mock.patch("dlmgr.segment.http_client.get", return_value=resp) as g:
        w.run()
    assert g.call_args.kwargs["max_recv_speed"] == 12345


def test_engine_progress_never_blocks_on_curl_lane(engine, monkeypatch):
    """On the curl lane the limiter.acquire must NOT run — a blocked
    consumer is exactly what buffered a 9 GB response in RAM. The token
    bucket stays for the plain-requests fallback (natural backpressure)."""
    calls = []
    monkeypatch.setattr(engine._limiter, "acquire", lambda n: calls.append(n))
    engine._jobs["j"] = DownloadJob(
        id="j", url="http://x/f", filename="f", save_path="x")
    monkeypatch.setattr(http_client, "USING_CURL", True)
    engine._on_segment_progress("j", 65536)
    assert calls == []
    monkeypatch.setattr(http_client, "USING_CURL", False)
    engine._on_segment_progress("j", 65536)
    assert calls == [65536]


def test_engine_respawns_orphan_pending_segments(engine):
    """A PENDING segment with no live worker (the guard's exit state — and
    the 2026-09-15 orphan class generally) must be restarted WITHOUT
    consuming a retry; a live worker's segment must never double-spawn."""
    job = DownloadJob(id="j", url="http://x/f", filename="f",
                      save_path=str("x"))
    job.status = JobStatus.DOWNLOADING
    seg_a = SegmentState(index=0, start_byte=0, end_byte=99)
    seg_b = SegmentState(index=1, start_byte=100, end_byte=199)
    seg_a.status = SegmentStatus.PENDING
    seg_b.status = SegmentStatus.PENDING
    job.segments = [seg_a, seg_b]
    engine._jobs["j"] = job

    class _AliveWorker:      # between ticks: owns seg_a, must not respawn
        segment = seg_a

        def is_alive(self):
            return True

    class _DeadWorker:       # guard-exited worker: pruned, seg_b orphaned
        segment = seg_b

        def is_alive(self):
            return False

    class _StubWorker:
        def __init__(self, **kwargs):
            self.segment = kwargs["segment"]
            self.started = False

        def start(self):
            self.started = True

        def is_alive(self):
            return True

    engine._workers["j"] = [_AliveWorker(), _DeadWorker()]
    spawned = []
    real_spawn = engine._spawn_segment_worker

    def _record(job_, seg, workers=None):
        w = _StubWorker(url=job_.url, segment=seg, file_path=job_.save_path)
        (workers if workers is not None
         else engine._workers.setdefault(job_.id, [])).append(w)
        spawned.append(seg)
        return w

    with mock.patch.object(engine, "_spawn_segment_worker", _record):
        engine._respawn_orphan_segments()

    assert spawned == [seg_b]                    # only the uncovered one
    workers = engine._workers["j"]
    assert all(w.__class__ is _StubWorker or w.__class__ is _AliveWorker
               for w in workers)                 # dead worker pruned


def test_recv_speed_share_math(engine, monkeypatch):
    monkeypatch.setattr(http_client, "USING_CURL", True)
    engine._bandwidth_limit = 16_000_000
    engine._max_concurrent = 2
    engine._max_connections = 8
    assert engine._recv_speed_per_worker() == 1_000_000   # 16M // 16
    engine._bandwidth_limit = 100_000            # floor protects tiny limits
    assert engine._recv_speed_per_worker() == 64 * 1024
    engine._bandwidth_limit = 0                  # unlimited → 0
    assert engine._recv_speed_per_worker() == 0
    monkeypatch.setattr(http_client, "USING_CURL", False)  # requests lane
    engine._bandwidth_limit = 1_000_000
    assert engine._recv_speed_per_worker() == 0


def test_http_client_forwards_max_recv_speed_to_curl(monkeypatch):
    """The socket cap reaches curl_cffi's request kwargs on the curl lane
    (and is simply absent on the requests fallback)."""
    recorded = {}

    class _FakeSession:
        def get(self, url, **kwargs):
            recorded.update(kwargs)
            return _FakeResp(b"", headers={})

    monkeypatch.setattr(http_client, "_HAS_CURL_CFFI", True)
    monkeypatch.setattr(http_client, "_session", lambda: _FakeSession())
    http_client.get("https://example.com/f", max_recv_speed=4321)
    assert recorded.get("max_recv_speed") == 4321
    monkeypatch.setattr(http_client, "_HAS_CURL_CFFI", False)
    with mock.patch.object(http_client.requests, "request",
                           return_value=_FakeResp(b"")) as g:
        http_client.get("https://example.com/f", max_recv_speed=4321)
    assert "max_recv_speed" not in g.call_args.kwargs
