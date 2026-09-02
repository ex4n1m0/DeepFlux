"""Segmented download engine with dynamic rebalancing.

This is the core of the IDM-style download manager. It:
1. Issues HEAD requests to check Accept-Ranges and Content-Length.
2. Pre-allocates the destination file.
3. Splits the file into N segments and downloads them concurrently.
4. Monitors per-segment throughput and rebalances stalled/slow segments.
5. Persists resume state so interrupted downloads can resume.
6. Falls back to single-stream for servers without range support.

The engine runs in its own thread and exposes a simple command interface
via a queue, similar to TorrentEngine."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from . import http_client
from .job import DownloadJob, JobStatus, SegmentState, SegmentStatus
from .segment import SegmentWorker
from . import resume_state

logger = logging.getLogger(__name__)

# How often (seconds) the monitor loop checks segment throughput and rebalances.
REBALANCE_INTERVAL = 2.0
# How often (seconds) the engine saves resume state.
SAVE_INTERVAL = 5.0
# Max automatic retries for a failed segment before the job errors out.
MAX_SEGMENT_RETRIES = 3


class _YTAbort(Exception):
    """Raised inside the yt-dlp progress hook to abort a download on pause."""


class _BandwidthLimiter:
    """Global token bucket: at most ``limit`` bytes/sec across all workers.

    Workers block in acquire() before writing, which throttles the actual
    download rate. Limit 0 disables throttling."""

    def __init__(self, limit_bps: int = 0) -> None:
        self._limit = max(0, limit_bps)
        self._allowance = float(self._limit)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def set_limit(self, limit_bps: int) -> None:
        with self._lock:
            self._limit = max(0, limit_bps)
            self._allowance = float(self._limit)
            self._last = time.monotonic()

    def acquire(self, nbytes: int) -> None:
        while True:
            with self._lock:
                if self._limit <= 0:
                    return
                now = time.monotonic()
                self._allowance = min(float(self._limit), self._allowance + (now - self._last) * self._limit)
                self._last = now
                if self._allowance >= nbytes:
                    self._allowance -= nbytes
                    return
                need = (nbytes - self._allowance) / self._limit
            time.sleep(min(need, 0.05))


class DownloadEngine:
    """Manages a pool of segmented download jobs.

    The engine is started once and runs a background monitor thread that:
    - Starts queued jobs up to the concurrency limit.
    - Monitors active segment throughput.
    - Rebalances stalled segments (donates remaining range to new workers).
    - Saves resume state periodically.
    - Updates job speed/ETA estimates.
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config
        self._max_concurrent = getattr(config, "max_concurrent", 3) if config else 3
        self._max_connections = getattr(config, "max_connections_per_download", 8) if config else 8
        # `or` (not getattr default) so an empty configured folder can't reach
        # os.makedirs("") — it always resolves to the real default.
        _fallback_folder = os.path.join(os.path.expanduser("~"), "Downloads", "DeepFlux")
        self._default_folder = (getattr(config, "default_folder", "") if config else "") or _fallback_folder
        self._segment_threshold = getattr(config, "segment_threshold_mb", 1) if config else 1
        self._auto_start = getattr(config, "auto_start", True) if config else True
        self._bandwidth_limit = getattr(config, "bandwidth_limit_bps", 0) if config else 0

        self._jobs: Dict[str, DownloadJob] = {}
        self._workers: Dict[str, List[SegmentWorker]] = {}
        self._stream_threads: Dict[str, threading.Thread] = {}
        self._probe_threads: Dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._last_save_time = 0.0
        self._global_throttle = threading.Event()
        # Per-job pause events.
        self._job_throttles: Dict[str, threading.Event] = {}
        # HLS/YouTube jobs don't use segment workers — track their controllers
        # so pause/cancel/resume work for them too.
        self._stream_controllers: Dict[str, Any] = {}
        # Speed sampling: job_id -> (timestamp, downloaded_bytes)
        self._speed_samples: Dict[str, Any] = {}
        # Global bandwidth limiter (bytes/s; 0 = unlimited).
        self._limiter = _BandwidthLimiter(self._bandwidth_limit)
        # Registry of folders containing downloads, for resume scanning.
        self._registry_path = os.path.join(os.path.expanduser("~"), ".deeptorrent", "dlmgr_folders.json")
        self._known_folders: set = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the engine and monitor thread. Also recovers incomplete
        downloads from the default folder."""
        if self._running:
            return
        self._running = True
        # Recover incomplete downloads from every folder we've ever saved to.
        self._load_folder_registry()
        try:
            for folder in sorted(self._known_folders):
                recovered = resume_state.scan_for_incomplete(folder)
                for job in recovered:
                    self._jobs[job.id] = job
                    self._job_throttles.setdefault(job.id, threading.Event())
                    logger.info("Recovered job: %s (%s)", job.id, job.filename)
        except Exception as exc:
            logger.warning("Failed to scan for incomplete downloads: %s", exc)

        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True, name="dl-engine")
        self._monitor_thread.start()
        logger.info("DownloadEngine started (max_concurrent=%d, max_connections=%d)", self._max_concurrent, self._max_connections)

    def stop(self) -> None:
        """Stop all workers and the monitor thread. Saves resume state."""
        self._running = False
        with self._lock:
            workers = [worker for group in self._workers.values() for worker in group]
            stream_threads = list(self._stream_threads.values())
            probe_threads = list(self._probe_threads.values())
            for throttle in self._job_throttles.values():
                throttle.set()
            for worker in workers:
                worker.stop()
            for controller in self._stream_controllers.values():
                try:
                    controller.stop()
                except Exception:
                    pass
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=REBALANCE_INTERVAL + 1.0)
        # Wait briefly for workers to stop.
        for thread in workers + stream_threads + probe_threads:
            if thread is not threading.current_thread():
                thread.join(timeout=5.0)
        with self._lock:
            # Save resume state for all incomplete jobs.
            for job in self._jobs.values():
                if not job.is_complete:
                    if job.job_type == "file":
                        self._update_job_downloaded(job)
                    resume_state.save_resume_state(job)
            self._workers.clear()
            self._stream_threads.clear()
            self._probe_threads.clear()
            self._stream_controllers.clear()
        logger.info("DownloadEngine stopped")

    def set_bandwidth_limit(self, limit_bps: int) -> None:
        """Update the global bandwidth limit at runtime (bytes/s; 0 = unlimited)."""
        self._bandwidth_limit = limit_bps
        self._limiter.set_limit(limit_bps)

    def update_settings(self, config: Any) -> None:
        with self._lock:
            self._config = config
            self._max_concurrent = max(1, int(config.max_concurrent))
            self._max_connections = max(1, int(config.max_connections_per_download))
            self._default_folder = config.default_folder or os.path.join(
                os.path.expanduser("~"), "Downloads", "DeepFlux")
            self._segment_threshold = max(1, int(config.segment_threshold_mb))
            self._auto_start = bool(config.auto_start)
            self.set_bandwidth_limit(max(0, int(config.bandwidth_limit_bps)))
            self._known_folders.add(self._default_folder)
        if self._auto_start:
            self._try_start_jobs()

    # -- download-folder registry (so resume scans cover custom save paths) --

    def _load_folder_registry(self) -> None:
        self._known_folders = {self._default_folder}
        try:
            if os.path.isfile(self._registry_path):
                import json
                with open(self._registry_path, "r", encoding="utf-8") as f:
                    for d in json.load(f):
                        if isinstance(d, str) and os.path.isdir(d):
                            self._known_folders.add(d)
        except Exception:
            pass

    def _register_folder(self, save_path: str) -> None:
        folder = os.path.dirname(os.path.abspath(save_path)) if save_path else ""
        if not folder or folder in self._known_folders:
            return
        self._known_folders.add(folder)
        try:
            import json
            os.makedirs(os.path.dirname(self._registry_path), exist_ok=True)
            with open(self._registry_path, "w", encoding="utf-8") as f:
                json.dump(sorted(self._known_folders), f)
        except Exception:
            pass

    def _unique_save_path(self, folder: str, filename: str) -> str:
        """Return a save path that doesn't collide with an existing file or job.

        Appends ' (1)', ' (2)', … to the stem when needed."""
        taken = {os.path.normcase(j.save_path) for j in self._jobs.values()}
        candidate = os.path.join(folder, filename)
        stem, ext = os.path.splitext(filename)
        n = 1
        while os.path.normcase(candidate) in taken or os.path.exists(candidate):
            candidate = os.path.join(folder, f"{stem} ({n}){ext}")
            n += 1
        return candidate

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = unquote(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
        stem, ext = os.path.splitext(name)
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if stem.upper() in reserved:
            stem = f"_{stem}"
        if not stem:
            stem = "download"
        limit = max(1, 240 - len(ext))
        return stem[:limit].rstrip(" .") + ext[:20]

    def _resolve_category(self, filename: str, requested: str = "") -> tuple[str, str]:
        extension = os.path.splitext(filename)[1].lower().lstrip(".")
        for category in getattr(self._config, "categories", []) or []:
            extensions = {str(value).lower().lstrip(".") for value in category.extensions}
            if (requested and category.name.casefold() == requested.casefold()) or (not requested and extension in extensions):
                if category.folder:
                    return category.name, category.folder
        return requested, self._default_folder

    def _prepare_save_path(self, filename: str, save_path: str, category: str = "") -> tuple[str, str]:
        filename = self._safe_filename(filename)
        resolved_category = category
        if save_path:
            expanded = os.path.abspath(os.path.expanduser(save_path))
            if os.path.isdir(expanded) or save_path.endswith((os.sep, "/", "\\")):
                folder = expanded
            else:
                folder = os.path.dirname(expanded) or "."
                filename = self._safe_filename(os.path.basename(expanded))
        else:
            resolved_category, folder = self._resolve_category(filename, category)
        os.makedirs(folder, exist_ok=True)
        with self._lock:
            return self._unique_save_path(folder, filename), resolved_category

    # ------------------------------------------------------------------
    # Job management
    # ------------------------------------------------------------------

    def add_job(
        self,
        url: str,
        filename: str = "",
        save_path: str = "",
        headers: Optional[Dict[str, str]] = None,
        cookies: str = "",
        referrer: str = "",
        job_type: str = "file",
        source_url: str = "",
        category: str = "",
    ) -> DownloadJob:
        """Create a new download job and queue it.

        The HEAD probe (file size + range support) runs in a background thread
        so the caller (GUI / control API) never blocks on network I/O. If the
        server doesn't support ranges, a single-segment job is created."""
        # Determine filename from URL if not provided.
        if not filename:
            parsed = urlparse(url)
            filename = os.path.basename(parsed.path) or "download"
        elif not os.path.splitext(filename)[1]:
            # Title-based filename (e.g. from the browser extension) without
            # an extension — borrow it from the URL when possible.
            url_ext = os.path.splitext(urlparse(url).path)[1]
            if url_ext and len(url_ext) <= 6:
                filename += url_ext

        # Determine save path, deduplicated against existing files and jobs.
        save_path, category = self._prepare_save_path(filename, save_path, category)
        filename = os.path.basename(save_path)
        self._register_folder(save_path)

        job = DownloadJob(
            url=url,
            filename=filename,
            save_path=save_path,
            status=JobStatus.QUEUED,
            source_url=source_url,
            job_type=job_type,
            headers=headers or {},
            cookies=cookies,
            referrer=referrer,
            category=category,
        )

        with self._lock:
            self._jobs[job.id] = job
            self._job_throttles[job.id] = threading.Event()

        def _run_probe() -> None:
            try:
                self._probe_and_setup(job)
            finally:
                with self._lock:
                    self._probe_threads.pop(job.id, None)

        thread = threading.Thread(target=_run_probe, daemon=True, name=f"probe-{job.id}")
        with self._lock:
            self._probe_threads[job.id] = thread
        thread.start()
        logger.info("Added download job %s: %s (probing…)", job.id, filename)
        return job

    def _probe_and_setup(self, job: DownloadJob) -> None:
        """HEAD-probe the server, set up segments, and start the job."""
        probe_headers = dict(job.headers)
        if job.referrer:
            probe_headers["Referer"] = job.referrer
        if job.cookies:
            probe_headers["Cookie"] = job.cookies

        file_size = 0
        supports_ranges = False
        etag = ""
        last_modified = ""
        try:
            resp = http_client.head(job.url, headers=probe_headers, allow_redirects=True, timeout=15)
            resp.raise_for_status()
            file_size = int(resp.headers.get("Content-Length", 0))
            supports_ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
            etag = resp.headers.get("ETag", "")
            last_modified = resp.headers.get("Last-Modified", "")
        except Exception as exc:
            logger.warning("HEAD request failed for %s: %s — will try GET", job.url, exc)

        threshold_bytes = self._segment_threshold * 1024 * 1024
        segments: List[SegmentState] = []
        if supports_ranges and file_size > threshold_bytes:
            num_segments = max(1, min(file_size // (512 * 1024), self._max_connections))
            seg_size = file_size // num_segments
            for i in range(num_segments):
                start = i * seg_size
                end = (i + 1) * seg_size - 1 if i < num_segments - 1 else file_size - 1
                segments.append(SegmentState(index=i, start_byte=start, end_byte=end))
        elif file_size > 0:
            segments.append(SegmentState(index=0, start_byte=0, end_byte=file_size - 1))
            supports_ranges = False
        else:
            # Unknown size — single streaming segment; end_byte=-1 sentinel
            # tells the worker to do a plain GET and read to EOF.
            segments.append(SegmentState(index=0, start_byte=0, end_byte=-1))
            supports_ranges = False

        with self._lock:
            if job.id not in self._jobs or not self._running:
                return  # cancelled while probing
            job.file_size = file_size
            job.supports_ranges = supports_ranges
            job.etag = etag
            job.last_modified = last_modified
            job.segments = segments
            if not self._auto_start:
                job.status = JobStatus.PAUSED

        # Pre-allocate the file if we know the size.
        if file_size > 0:
            try:
                with open(job.save_path, "wb") as f:
                    f.truncate(file_size)
            except OSError as exc:
                logger.warning("Failed to pre-allocate %s: %s", job.save_path, exc)

        logger.info("Probed job %s: %d bytes, %d segments, ranges=%s",
                    job.id, file_size, len(segments), supports_ranges)
        if self._auto_start:
            self._try_start_jobs()

    def _select_stream_rendition(self, renditions):
        max_height = max(0, int(getattr(self._config, "stream_max_height", 0) or 0))
        eligible = [item for item in renditions if not max_height or not item.height or item.height <= max_height]
        return max(eligible or renditions, key=lambda item: item.bandwidth)

    def add_stream_job(
        self,
        url: str,
        filename: str = "",
        save_path: str = "",
        headers: Optional[Dict[str, str]] = None,
        cookies: str = "",
        referrer: str = "",
        source_url: str = "",
        category: str = "",
    ) -> DownloadJob:
        """Create a new HLS/DASH stream capture job.

        Parses the manifest, downloads segments, and remuxes via FFmpeg.
        The job's file_size is set to the number of segments (for progress
        tracking), and the actual output file is produced after remuxing."""
        from .hls_dash import parse_manifest, HLSDownloader, DRMError
        from .ffmpeg import FFmpegWrapper, find_ffmpeg, KNOWN_CONTAINER_EXTS

        # Parse the manifest.
        info = parse_manifest(
            url, headers=headers, cookies=cookies, referrer=referrer,
            rendition_selector=self._select_stream_rendition,
        )
        if info.error:
            # Create an errored job so the UI can show it.
            job = DownloadJob(
                url=url,
                filename=filename or "stream",
                save_path=save_path,
                status=JobStatus.ERROR,
                error_message=info.error,
                job_type="hls",
                source_url=source_url,
            )
            with self._lock:
                self._jobs[job.id] = job
            return job

        if not filename:
            filename = os.path.splitext(os.path.basename(urlparse(url).path))[0] or "stream"
            filename += ".mp4"
        elif os.path.splitext(filename)[1].lower() not in KNOWN_CONTAINER_EXTS:
            # Title-based filename from the extension — HLS output is mp4.
            # (Titles contain stray dots, so a bare splitext check misfires.)
            filename += ".mp4"

        save_path, category = self._prepare_save_path(filename, save_path, category)
        filename = os.path.basename(save_path)
        self._register_folder(save_path)

        job = DownloadJob(
            url=url,
            filename=filename,
            save_path=save_path,
            file_size=len(info.segment_urls),
            status=JobStatus.QUEUED if self._auto_start else JobStatus.PAUSED,
            source_url=source_url,
            job_type=info.stream_type,
            headers=headers or {},
            cookies=cookies,
            referrer=referrer,
            category=category,
        )

        with self._lock:
            self._jobs[job.id] = job
            self._job_throttles[job.id] = threading.Event()

        if self._auto_start:
            self._try_start_jobs()
        logger.info("Added stream job %s: %s (%d segments)", job.id, filename, len(info.segment_urls))
        return job

    def _start_stream_job(self, job: DownloadJob) -> None:
        """Start (or restart) an HLS/DASH capture in a background thread.

        The downloader is tracked in _stream_controllers so pause/cancel can
        actually signal it. On restart the manifest is re-parsed and already
        downloaded segments on disk are skipped."""
        from .hls_dash import parse_manifest, HLSDownloader, DRMError
        from .ffmpeg import FFmpegWrapper, find_ffmpeg, KNOWN_CONTAINER_EXTS

        with self._lock:
            if job.id not in self._jobs or job.status != JobStatus.QUEUED:
                return
            job.status = JobStatus.DOWNLOADING

        def _stream_worker():
            try:
                info = parse_manifest(
                    job.url, headers=job.headers, cookies=job.cookies, referrer=job.referrer,
                    rendition_selector=self._select_stream_rendition,
                )
                if info.error:
                    job.status = JobStatus.ERROR
                    job.error_message = info.error
                    return
                throttle = self._job_throttles.get(job.id)
                if not self._running or (throttle is not None and throttle.is_set()):
                    job.status = JobStatus.PAUSED
                    return
                if not job.file_size:
                    job.file_size = len(info.segment_urls)

                temp_dir = job.save_path + ".segments"
                speed_state = {"bytes": 0, "t": time.monotonic()}

                def _on_stream_progress(n: int) -> None:
                    job.downloaded = n
                    now = time.monotonic()
                    dt = now - speed_state["t"]
                    if dt >= 1.0:
                        dbytes = downloader.bytes_downloaded - speed_state["bytes"]
                        job.speed_bps = int(dbytes / dt)
                        speed_state["bytes"] = downloader.bytes_downloaded
                        speed_state["t"] = now
                        if job.speed_bps > 0 and info.segment_urls:
                            # Rough byte/segment average to project ETA.
                            avg_seg = downloader.bytes_downloaded / max(1, n)
                            remaining = (len(info.segment_urls) - n) * avg_seg
                            job.eta_seconds = int(remaining / job.speed_bps)

                downloader = HLSDownloader(
                    stream_info=info,
                    temp_dir=temp_dir,
                    headers=job.headers,
                    cookies=job.cookies,
                    referrer=job.referrer,
                    max_workers=self._max_connections,
                    on_progress=_on_stream_progress,
                )
                with self._lock:
                    self._stream_controllers[job.id] = downloader
                try:
                    segment_files = downloader.download()
                finally:
                    with self._lock:
                        self._stream_controllers.pop(job.id, None)
                job.downloaded = len(segment_files)

                if downloader._stop_flag.is_set():
                    job.status = JobStatus.PAUSED
                    return

                audio_files: List[str] = []
                audio_temp_dir = temp_dir + ".audio"
                if info.audio_segment_urls:
                    audio_info = replace(
                        info,
                        segment_urls=list(info.audio_segment_urls),
                        init_segment_url=info.audio_init_segment_url,
                        segment_metadata=[],
                        segment_key_urls=[],
                        segment_ivs=[],
                        segment_init_urls=[],
                        discontinuity_indices=[],
                    )
                    audio_downloader = HLSDownloader(
                        stream_info=audio_info,
                        temp_dir=audio_temp_dir,
                        headers=job.headers,
                        cookies=job.cookies,
                        referrer=job.referrer,
                        max_workers=self._max_connections,
                    )
                    with self._lock:
                        self._stream_controllers[job.id] = audio_downloader
                    try:
                        audio_files = audio_downloader.download()
                    finally:
                        with self._lock:
                            self._stream_controllers.pop(job.id, None)
                    if audio_downloader._stop_flag.is_set():
                        job.status = JobStatus.PAUSED
                        return

                # Remux with FFmpeg. Distinct status so the UI shows the job
                # is merging/processing rather than stuck at "downloading".
                job.status = JobStatus.PROCESSING
                job.speed_bps = 0
                job.eta_seconds = 0
                if os.path.splitext(job.save_path)[1].lower() not in KNOWN_CONTAINER_EXTS:
                    job.save_path += ".mp4"
                ffmpeg_path = find_ffmpeg(getattr(self._config, "ffmpeg_path", ""))
                ff = FFmpegWrapper(ffmpeg_path)
                if ff.available:
                    if audio_files:
                        video_track = job.save_path + ".video.mp4"
                        audio_track = job.save_path + ".audio.m4a"
                        success = ff.remux(
                            segment_files,
                            video_track,
                            init_segment=info.init_segment_url and os.path.join(temp_dir, "init.mp4"),
                        )
                        success = success and ff.remux(
                            audio_files,
                            audio_track,
                            init_segment=info.audio_init_segment_url and os.path.join(audio_temp_dir, "init.mp4"),
                        )
                        success = success and ff.merge_av(video_track, audio_track, job.save_path)
                        for track in (video_track, audio_track):
                            try:
                                if os.path.isfile(track):
                                    os.remove(track)
                            except OSError:
                                pass
                    else:
                        success = ff.remux(
                            segment_files,
                            job.save_path,
                            init_segment=info.init_segment_url and os.path.join(temp_dir, "init.mp4"),
                            on_progress=lambda p: setattr(job, "downloaded", int(p * len(segment_files))),
                        )
                    if success:
                        job.status = JobStatus.COMPLETED
                        job.downloaded = len(segment_files)
                        logger.info("Stream job %s completed: %s", job.id, job.filename)
                    else:
                        job.status = JobStatus.ERROR
                        job.error_message = "FFmpeg remux failed"
                else:
                    # No FFmpeg — just concatenate raw segments.
                    if audio_files:
                        job.status = JobStatus.ERROR
                        job.error_message = "FFmpeg is required to merge separate DASH video and audio streams"
                    else:
                        with open(job.save_path, "wb") as out:
                            for seg in segment_files:
                                with open(seg, "rb") as f:
                                    out.write(f.read())
                        job.status = JobStatus.COMPLETED
                        job.downloaded = len(segment_files)
                        logger.info("Stream job %s completed (no remux): %s", job.id, job.filename)

                # Clean up temp segments — only on success, so a failed remux
                # can be retried without re-downloading thousands of segments.
                if job.status == JobStatus.COMPLETED:
                    import shutil
                    try:
                        for completed_temp_dir in (temp_dir, audio_temp_dir):
                            if os.path.isdir(completed_temp_dir):
                                shutil.rmtree(completed_temp_dir)
                    except OSError:
                        pass

            except DRMError as exc:
                job.status = JobStatus.ERROR
                job.error_message = str(exc)
                logger.warning("Stream job %s DRM error: %s", job.id, exc)
            except Exception as exc:
                if job.status == JobStatus.PAUSED:
                    return  # aborted via pause, not an error
                job.status = JobStatus.ERROR
                job.error_message = str(exc)
                logger.warning("Stream job %s error: %s", job.id, exc)

        def _run_stream_worker() -> None:
            try:
                _stream_worker()
            finally:
                with self._lock:
                    self._stream_threads.pop(job.id, None)

        thread = threading.Thread(target=_run_stream_worker, daemon=True, name=f"stream-{job.id}")
        with self._lock:
            self._stream_threads[job.id] = thread
        thread.start()
        return job

    def add_youtube_job(
        self,
        url: str,
        filename: str = "",
        save_path: str = "",
        source_url: str = "",
        category: str = "",
    ) -> DownloadJob:
        """Create a YouTube download job using yt-dlp.

        YouTube no longer embeds direct media URLs in the page HTML — the
        signed googlevideo.com URLs are generated at runtime by YouTube's JS
        player. yt-dlp handles the signature cipher, adaptive format merging,
        and other complexities. Progress is reported via yt-dlp's progress hook."""
        try:
            from yt_dlp import YoutubeDL
        except ImportError:
            job = DownloadJob(
                url=url,
                filename=filename or "youtube_video",
                save_path=save_path or "",
                status=JobStatus.ERROR,
                error_message="yt-dlp is not installed. Run: pip install yt-dlp",
                job_type="youtube",
                source_url=source_url,
            )
            with self._lock:
                self._jobs[job.id] = job
            return job

        base = filename or "youtube_video"
        save_path, category = self._prepare_save_path(base, save_path, category)
        self._register_folder(save_path)
        filename = os.path.basename(save_path)

        job = DownloadJob(
            url=url,
            filename=filename,
            save_path=save_path,
            status=JobStatus.QUEUED if self._auto_start else JobStatus.PAUSED,
            source_url=source_url,
            job_type="youtube",
            category=category,
        )

        with self._lock:
            self._jobs[job.id] = job
            self._job_throttles[job.id] = threading.Event()

        if self._auto_start:
            self._try_start_jobs()
        logger.info("Added YouTube job %s: %s", job.id, url)
        return job

    def _start_youtube_worker(self, job: DownloadJob) -> None:
        """Start (or restart) the yt-dlp worker for a YouTube job.

        Pause works by raising _YTAbort from the progress hook; yt-dlp keeps
        its .part files, so a resume simply re-runs the worker and continues
        where it left off."""
        from yt_dlp import YoutubeDL

        with self._lock:
            if job.id not in self._jobs or job.status != JobStatus.QUEUED:
                return
            job.status = JobStatus.DOWNLOADING

        def _yt_dlp_worker():
            try:
                # Build the output template. yt-dlp needs %(ext)s to append
                # the correct extension based on the downloaded format. Without
                # it, prepare_filename returns a path with no extension and the
                # final-file detection below fails.
                filename = job.filename
                if job.filename and job.filename != "youtube_video" and os.path.splitext(job.filename)[1]:
                    # User gave a filename with extension — use it as-is.
                    outtmpl = os.path.join(os.path.dirname(job.save_path), job.filename)
                elif job.filename and job.filename != "youtube_video":
                    # No extension — let yt-dlp fill it in.
                    outtmpl = os.path.join(os.path.dirname(job.save_path), job.filename + ".%(ext)s")
                else:
                    # No filename given — name the file after the video title.
                    outtmpl = os.path.join(os.path.dirname(job.save_path), "%(title)s.%(ext)s")

                # Locate FFmpeg — yt-dlp needs it to merge separate video/audio
                # streams (bestvideo+bestaudio) into a single mp4. In the
                # bundled PyInstaller app, ffmpeg.exe is in _internal/ffmpeg/
                # and won't be on PATH, so we must pass its location explicitly.
                from .ffmpeg import find_ffmpeg
                ffmpeg_path = find_ffmpeg(getattr(self._config, "ffmpeg_path", ""))

                max_height = max(144, int(getattr(self._config, "youtube_max_height", 1080) or 1080))
                ydl_opts: dict = {
                    "outtmpl": outtmpl,
                    "format": f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best",
                    "merge_output_format": "mp4",
                    "noplaylist": not bool(getattr(self._config, "youtube_playlists", False)),
                    "quiet": True,
                    "no_warnings": True,
                    "progress_hooks": [lambda d: _on_progress(d)],
                }
                if bool(getattr(self._config, "youtube_subtitles", False)):
                    ydl_opts.update({"writesubtitles": True, "writeautomaticsub": True, "embedsubtitles": True})
                if ffmpeg_path:
                    ydl_opts["ffmpeg_location"] = ffmpeg_path

                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(job.url, download=True)
                    final_path = ydl.prepare_filename(info)
                    # After merging, the actual file has the merge_output_format
                    # extension (.mp4). prepare_filename may return a path
                    # without the extension when outtmpl lacks %(ext)s, so
                    # check for the merged file explicitly.
                    if not os.path.exists(final_path):
                        merged = os.path.splitext(final_path)[0] + ".mp4"
                        if os.path.exists(merged):
                            final_path = merged
                        else:
                            # Try common extensions yt-dlp might produce.
                            stem = os.path.splitext(final_path)[0]
                            for ext in (".mp4", ".mkv", ".webm", ".m4a"):
                                candidate = stem + ext
                                if os.path.exists(candidate):
                                    final_path = candidate
                                    break
                    # Update job with actual filename and size.
                    job.filename = os.path.basename(final_path)
                    job.save_path = final_path
                    if os.path.exists(final_path):
                        job.file_size = os.path.getsize(final_path)
                        job.downloaded = job.file_size
                    job.status = JobStatus.COMPLETED
                    logger.info("YouTube job %s completed: %s", job.id, job.filename)

            except _YTAbort:
                # Pause requested — status was already set by pause_job.
                logger.info("YouTube job %s paused", job.id)
            except Exception as exc:
                if job.status == JobStatus.PAUSED:
                    return
                job.status = JobStatus.ERROR
                job.error_message = str(exc)
                logger.warning("YouTube job %s error: %s", job.id, exc)

        def _on_progress(d):
            # Pause: abort the yt-dlp run from inside the progress hook.
            throttle = self._job_throttles.get(job.id)
            if throttle is not None and throttle.is_set():
                raise _YTAbort()
            # Replace the placeholder name with the real output filename as
            # soon as yt-dlp has resolved the video. During download the path
            # may carry a ".part" suffix and an adaptive-format tag like
            # ".f137" (stripped again at merge time), so remove both.
            if job.filename == "youtube_video":
                src = d.get("filename")
                if src:
                    base = os.path.basename(src)
                    if base.endswith(".part"):
                        base = base[:-5]
                    job.filename = re.sub(r"\.f\d+(?=\.[^.]+$)", "", base)
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                if total:
                    job.file_size = total
                    job.downloaded = downloaded
            elif d["status"] == "finished":
                if d.get("total_bytes"):
                    job.file_size = d["total_bytes"]
                    job.downloaded = job.file_size

        def _run_youtube_worker() -> None:
            try:
                _yt_dlp_worker()
            finally:
                with self._lock:
                    self._stream_threads.pop(job.id, None)

        thread = threading.Thread(target=_run_youtube_worker, daemon=True, name=f"youtube-{job.id}")
        with self._lock:
            self._stream_threads[job.id] = thread
        thread.start()

    def pause_job(self, job_id: str) -> bool:
        """Pause a download job. Workers finish their current chunk then stop."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status not in (JobStatus.DOWNLOADING, JobStatus.QUEUED):
                return False
            job.status = JobStatus.PAUSED
            throttle = self._job_throttles.get(job_id)
            if throttle:
                throttle.set()
            # Stop segment workers — they'll be restarted on resume.
            for w in self._workers.get(job_id, []):
                w.stop()
            # HLS: signal the downloader to stop. YouTube: the throttle event
            # is checked in the yt-dlp progress hook, which aborts the run.
            controller = self._stream_controllers.get(job_id)
            if controller is not None:
                try:
                    controller.stop()
                except Exception:
                    pass
            logger.info("Paused job %s", job_id)
        return True

    def resume_job(self, job_id: str) -> bool:
        """Resume a paused or errored job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status not in (JobStatus.PAUSED, JobStatus.ERROR):
                return False
            job.status = JobStatus.QUEUED
            job.error_message = ""
            throttle = self._job_throttles.get(job_id)
            if throttle:
                throttle.clear()
            if job.job_type == "file" and not job.supports_ranges:
                for seg in job.segments:
                    seg.completed_bytes = 0
                job.downloaded = 0
            # Reset incomplete segments to pending and clear retry counters
            # so auto-retry gets a fresh budget.
            for seg in job.segments:
                if seg.status not in (SegmentStatus.DONE,):
                    seg.status = SegmentStatus.PENDING
                    seg.retries = 0
            logger.info("Resumed job %s", job_id)
        self._try_start_jobs()
        return True

    def cancel_job(self, job_id: str, delete_file: bool = True) -> bool:
        """Cancel a job and optionally delete the partial file."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            # Stop workers (segments) and stream controllers (HLS/YouTube).
            for w in self._workers.get(job_id, []):
                w.stop()
            controller = self._stream_controllers.get(job_id)
            if controller is not None:
                try:
                    controller.stop()
                except Exception:
                    pass
            # Remove from active tracking.
            self._workers.pop(job_id, None)
            self._job_throttles.pop(job_id, None)
            # Delete partial file, HLS temp segments, and resume state.
            if delete_file:
                try:
                    if os.path.exists(job.save_path):
                        os.remove(job.save_path)
                except OSError:
                    pass
                import shutil
                for temp_dir in (job.save_path + ".segments", job.save_path + ".segments.audio"):
                    if os.path.isdir(temp_dir):
                        try:
                            shutil.rmtree(temp_dir)
                        except OSError:
                            pass
            resume_state.delete_resume_state(job)
            # Remove from job list.
            del self._jobs[job_id]
            logger.info("Cancelled job %s", job_id)
        return True

    def remove_job(self, job_id: str) -> bool:
        """Remove a completed/errored job from the list (keeps the file)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED):
                return False
            self._workers.pop(job_id, None)
            self._job_throttles.pop(job_id, None)
            del self._jobs[job_id]
            resume_state.delete_resume_state(job)
            return True

    def retry_job(self, job_id: str) -> Optional[DownloadJob]:
        """Retry an errored job by re-queuing it."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
        if self.resume_job(job_id):
            return job
        return None

    def get_job(self, job_id: str) -> Optional[DownloadJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> List[DownloadJob]:
        with self._lock:
            return list(self._jobs.values())

    def set_job_priority(self, job_id: str, priority: int) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.priority = max(-10, min(10, int(priority)))
            if not job.is_complete:
                resume_state.save_resume_state(job)
            return True

    # ------------------------------------------------------------------
    # Internal: job scheduling and segment management
    # ------------------------------------------------------------------

    def _try_start_jobs(self) -> None:
        """Start queued jobs up to the concurrency limit."""
        with self._lock:
            active = sum(1 for j in self._jobs.values() if j.status in (JobStatus.DOWNLOADING, JobStatus.PROCESSING))
            queued = sorted(
                (job for job in self._jobs.values() if job.status == JobStatus.QUEUED),
                key=lambda job: (-job.priority, job.created_at),
            )
            for job in queued:
                if active >= self._max_concurrent:
                    break
                if job.job_type == "file" and not job.segments:
                    continue  # HEAD probe still in flight
                if job.job_type in ("hls", "dash"):
                    self._start_stream_job(job)
                elif job.job_type == "youtube":
                    self._start_youtube_worker(job)
                else:
                    self._start_job(job)
                active += 1

    def _start_job(self, job: DownloadJob) -> None:
        """Start (or restart) segment workers for a job."""
        job.status = JobStatus.DOWNLOADING
        throttle = self._job_throttles.get(job.id)
        workers: List[SegmentWorker] = []

        for seg in job.segments:
            if seg.status == SegmentStatus.DONE:
                continue
            if seg.end_byte >= 0 and seg.remaining_bytes <= 0:
                seg.status = SegmentStatus.DONE
                continue

            worker = SegmentWorker(
                url=job.url,
                segment=seg,
                file_path=job.save_path,
                headers=job.headers,
                cookies=job.cookies,
                referrer=job.referrer,
                on_progress=lambda n, jid=job.id: self._on_segment_progress(jid, n),
                on_done=lambda ok, err, jid=job.id: self._on_segment_done(jid, ok, err),
                throttle_event=throttle,
                use_range=job.supports_ranges,
                if_range=job.etag or job.last_modified,
                expected_file_size=job.file_size,
            )
            workers.append(worker)
            worker.start()

        self._workers[job.id] = workers
        logger.info("Started job %s with %d workers", job.id, len(workers))

    def _on_segment_progress(self, job_id: str, bytes_downloaded: int) -> None:
        """Called by segment workers as data arrives."""
        # Block here to enforce the global bandwidth limit — the worker thread
        # waits for tokens, which throttles its write rate.
        self._limiter.acquire(bytes_downloaded)
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.downloaded += bytes_downloaded

    def _on_segment_done(self, job_id: str, success: bool, error: str) -> None:
        """Called when a segment worker finishes (success or failure)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            # Unknown-size job: adopt the actual byte count once done.
            if job.file_size <= 0 and job.segments:
                job.file_size = sum(s.completed_bytes for s in job.segments)
            # Check if all segments are done.
            all_done = all(s.status == SegmentStatus.DONE for s in job.segments)
            if all_done:
                job.status = JobStatus.COMPLETED
                job.speed_bps = 0
                job.eta_seconds = 0
                self._workers.pop(job_id, None)  # release dead workers
                resume_state.delete_resume_state(job)
                logger.info("Job %s completed: %s", job.id, job.filename)
                return
            # Errors: segments with retries left are restarted by the monitor
            # loop; only fail the job when nothing is running and every errored
            # segment has exhausted its retries.
            any_error = any(s.status == SegmentStatus.ERROR for s in job.segments)
            any_active = any(s.status in (SegmentStatus.ACTIVE, SegmentStatus.PENDING) for s in job.segments)
            retryable = any(s.status == SegmentStatus.ERROR and s.retries < MAX_SEGMENT_RETRIES for s in job.segments)
            if any_error and not any_active and not retryable:
                job.status = JobStatus.ERROR
                job.error_message = error
                logger.warning("Job %s errored: %s", job.id, error)

    def _update_job_downloaded(self, job: DownloadJob) -> None:
        """Recalculate downloaded bytes from segment states."""
        job.downloaded = sum(s.completed_bytes for s in job.segments)

    # ------------------------------------------------------------------
    # Monitor loop
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        """Background loop: start queued jobs, rebalance segments, save state."""
        while self._running:
            try:
                self._try_start_jobs()
                self._auto_retry_errors()
                self._rebalance_segments()
                self._update_speeds_and_eta()
                self._maybe_save_state()
            except Exception as exc:
                logger.warning("Monitor loop error: %s", exc)
            time.sleep(REBALANCE_INTERVAL)

    def _auto_retry_errors(self) -> None:
        """Restart segments that failed transiently, up to MAX_SEGMENT_RETRIES."""
        with self._lock:
            for job in self._jobs.values():
                if job.status != JobStatus.DOWNLOADING or job.job_type != "file":
                    continue
                for seg in job.segments:
                    if seg.status != SegmentStatus.ERROR or seg.retries >= MAX_SEGMENT_RETRIES:
                        continue
                    seg.retries += 1
                    seg.status = SegmentStatus.PENDING
                    seg.error = ""
                    throttle = self._job_throttles.get(job.id)
                    worker = SegmentWorker(
                        url=job.url,
                        segment=seg,
                        file_path=job.save_path,
                        headers=job.headers,
                        cookies=job.cookies,
                        referrer=job.referrer,
                        on_progress=lambda n, jid=job.id: self._on_segment_progress(jid, n),
                        on_done=lambda ok, err, jid=job.id: self._on_segment_done(jid, ok, err),
                        throttle_event=throttle,
                        use_range=job.supports_ranges,
                        if_range=job.etag or job.last_modified,
                        expected_file_size=job.file_size,
                    )
                    self._workers.setdefault(job.id, []).append(worker)
                    worker.start()
                    logger.info("Retrying segment %d of job %s (attempt %d)", seg.index, job.id, seg.retries)

    def _rebalance_segments(self) -> None:
        """Check for stalled segments and rebalance their remaining range.

        If a segment has been stalled (no data for STALL_TIMEOUT seconds),
        we split its remaining range and assign the tail to a new worker.
        Finished segments that completed early also donate range to stalled
        siblings."""
        with self._lock:
            for job_id, workers in list(self._workers.items()):
                job = self._jobs.get(job_id)
                if not job or job.status != JobStatus.DOWNLOADING:
                    continue
                if not job.supports_ranges:
                    continue  # Can't rebalance single-stream downloads.

                # Find stalled segments with remaining bytes.
                stalled = [w for w in workers if w.is_stalled and w.segment.remaining_bytes > 0]
                # Find done workers that could take over.
                done = [w for w in workers if w.segment.status == SegmentStatus.DONE]

                for stalled_w in stalled:
                    if not done:
                        # No donor available — restart the stalled segment
                        # outright with a fresh connection instead of leaving
                        # it stuck until the read timeout fires.
                        stalled_w.stop()
                        stalled_seg = stalled_w.segment
                        stalled_seg.status = SegmentStatus.PENDING
                        stalled_seg.retries += 1
                        if stalled_seg.retries > MAX_SEGMENT_RETRIES:
                            continue
                        throttle = self._job_throttles.get(job_id)
                        new_worker = SegmentWorker(
                            url=job.url,
                            segment=stalled_seg,
                            file_path=job.save_path,
                            headers=job.headers,
                            cookies=job.cookies,
                            referrer=job.referrer,
                            on_progress=lambda n, jid=job_id: self._on_segment_progress(jid, n),
                            on_done=lambda ok, err, jid=job_id: self._on_segment_done(jid, ok, err),
                            throttle_event=throttle,
                            use_range=job.supports_ranges,
                            if_range=job.etag or job.last_modified,
                            expected_file_size=job.file_size,
                        )
                        workers.append(new_worker)
                        new_worker.start()
                        logger.info("Restarted stalled segment %d of job %s", stalled_seg.index, job_id)
                        continue
                    # Take the first done worker and re-purpose it.
                    donor = done.pop(0)
                    stalled_seg = stalled_w.segment
                    # Split the stalled segment's remaining range in half.
                    remaining = stalled_seg.remaining_bytes
                    if remaining < 256 * 1024:
                        continue  # Too small to bother rebalancing.
                    split_point = stalled_seg.start_byte + stalled_seg.completed_bytes + remaining // 2

                    # Stop the stalled worker.
                    stalled_w.stop()

                    # Create a new segment for the tail.
                    new_seg = SegmentState(
                        index=len(job.segments),
                        start_byte=split_point,
                        end_byte=stalled_seg.end_byte,
                    )
                    job.segments.append(new_seg)
                    # Truncate the stalled segment's end to the split point.
                    stalled_seg.end_byte = split_point - 1

                    # Start a new worker for the tail.
                    throttle = self._job_throttles.get(job_id)
                    new_worker = SegmentWorker(
                        url=job.url,
                        segment=new_seg,
                        file_path=job.save_path,
                        headers=job.headers,
                        cookies=job.cookies,
                        referrer=job.referrer,
                        on_progress=lambda n, jid=job_id: self._on_segment_progress(jid, n),
                        on_done=lambda ok, err, jid=job_id: self._on_segment_done(jid, ok, err),
                        throttle_event=throttle,
                        use_range=job.supports_ranges,
                        if_range=job.etag or job.last_modified,
                        expected_file_size=job.file_size,
                    )
                    workers.append(new_worker)
                    new_worker.start()
                    logger.info("Rebalanced job %s: split segment %d at byte %d", job_id, stalled_seg.index, split_point)

    def _update_speeds_and_eta(self) -> None:
        """Update per-job speed and ETA from downloaded-byte deltas between ticks.

        (Segment workers can't measure their own rate meaningfully — speed is
        computed here as bytes downloaded since the previous monitor tick.)"""
        now = time.time()
        with self._lock:
            for job in self._jobs.values():
                if job.status != JobStatus.DOWNLOADING:
                    job.speed_bps = 0
                    job.eta_seconds = 0
                    self._speed_samples.pop(job.id, None)
                    continue
                last = self._speed_samples.get(job.id)
                self._speed_samples[job.id] = (now, job.downloaded)
                if not last:
                    continue  # need two samples; first tick shows 0
                dt = now - last[0]
                if dt <= 0:
                    continue
                job.speed_bps = max(0, int((job.downloaded - last[1]) / dt))
                remaining = job.file_size - job.downloaded if job.file_size > 0 else 0
                if job.speed_bps > 0 and remaining > 0:
                    job.eta_seconds = int(remaining / job.speed_bps)
                else:
                    job.eta_seconds = 0

    def _maybe_save_state(self) -> None:
        """Periodically save resume state for active jobs."""
        now = time.time()
        if now - self._last_save_time < SAVE_INTERVAL:
            return
        self._last_save_time = now
        with self._lock:
            for job in self._jobs.values():
                if job.status in (JobStatus.DOWNLOADING, JobStatus.PAUSED):
                    if job.job_type == "file":
                        self._update_job_downloaded(job)
                    resume_state.save_resume_state(job)
