"""Segment worker — downloads a byte-range chunk and writes it at the correct
offset in the pre-allocated destination file.

Each segment runs in its own thread. The engine monitors throughput and can
rebalance (split/donate ranges) based on the progress reported here."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Callable, Optional

from . import http_client
from .job import SegmentState, SegmentStatus

logger = logging.getLogger(__name__)

# How long (seconds) with zero throughput before a segment is considered stalled.
STALL_TIMEOUT = 10.0
# Chunk size for streaming reads (64 KB).
CHUNK_SIZE = 65536


class SegmentWorker(threading.Thread):
    """Downloads a single byte-range segment via HTTP Range requests.

    Writes data directly into the destination file at ``start_byte + offset``
    so no merge step is needed after all segments complete.

    The ``on_progress`` callback is invoked periodically with the number of
    bytes downloaded since the last callback, allowing the engine to track
    throughput and apply bandwidth throttling."""

    def __init__(
        self,
        url: str,
        segment: SegmentState,
        file_path: str,
        headers: Optional[dict] = None,
        cookies: str = "",
        referrer: str = "",
        on_progress: Optional[Callable[[int], None]] = None,
        on_done: Optional[Callable[[bool, str], None]] = None,
        throttle_event: Optional[threading.Event] = None,
        use_range: bool = True,
        if_range: str = "",
        expected_file_size: int = 0,
    ) -> None:
        super().__init__(daemon=True, name=f"seg-{segment.index}")
        self._url = url
        self._segment = segment
        self._file_path = file_path
        self._headers = dict(headers or {})
        self._cookies = cookies
        self._referrer = referrer
        self._on_progress = on_progress
        self._on_done = on_done
        self._throttle_event = throttle_event  # set = paused
        self._use_range = use_range
        self._if_range = if_range
        self._expected_file_size = expected_file_size
        self._stop_flag = threading.Event()
        self._last_data_time = time.time()

    @property
    def segment(self) -> SegmentState:
        return self._segment

    @property
    def is_stalled(self) -> bool:
        """True if no data has been received for STALL_TIMEOUT seconds."""
        if self._segment.status != SegmentStatus.ACTIVE:
            return False
        return (time.time() - self._last_data_time) > STALL_TIMEOUT

    def stop(self) -> None:
        """Signal the worker to stop at the next chunk boundary."""
        self._stop_flag.set()

    def run(self) -> None:
        self._segment.status = SegmentStatus.ACTIVE
        self._last_data_time = time.time()

        try:
            req_headers = dict(self._headers)
            if not self._use_range:
                self._segment.completed_bytes = 0
            request_start = self._segment.start_byte + self._segment.completed_bytes
            # end_byte < 0 means unknown total size: plain GET, stream to EOF
            # (no Range header — a range request would fetch a single byte on
            # range-capable servers).
            if self._use_range and self._segment.end_byte >= 0:
                req_headers["Range"] = f"bytes={request_start}-{self._segment.end_byte}"
                if self._if_range and self._segment.completed_bytes:
                    req_headers["If-Range"] = self._if_range
            if self._referrer:
                req_headers["Referer"] = self._referrer
            if self._cookies:
                req_headers["Cookie"] = self._cookies

            # No context manager here: curl_cffi's Response doesn't support
            # the protocol — close explicitly instead (requests' does both).
            resp = http_client.get(self._url, headers=req_headers, stream=True, timeout=(10, 20))
            try:
                resp.raise_for_status()
                ranged = self._use_range and self._segment.end_byte >= 0
                if ranged:
                    status_code = getattr(resp, "status_code", 0)
                    content_range = resp.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range.strip(), re.IGNORECASE)
                    if status_code != 206 or match is None:
                        raise RuntimeError("Range request was not honored by the server")
                    actual_start, actual_end = int(match.group(1)), int(match.group(2))
                    if actual_start != request_start or actual_end != self._segment.end_byte:
                        raise RuntimeError("Range response did not match the requested byte window")
                    if self._expected_file_size and match.group(3) != "*" and int(match.group(3)) != self._expected_file_size:
                        raise RuntimeError("Remote file size changed during download")
                # The file may not exist yet (unknown-size jobs aren't
                # pre-allocated) — create it in that case.
                mode = "r+b" if ranged and os.path.isfile(self._file_path) else "w+b"
                with open(self._file_path, mode) as f:
                    write_pos = request_start if ranged else 0
                    f.seek(write_pos)
                    response_bytes = 0
                    expected_bytes = self._segment.end_byte - request_start + 1 if self._segment.end_byte >= 0 else -1

                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if self._stop_flag.is_set():
                            self._segment.status = SegmentStatus.PENDING
                            return

                        # Respect pause/throttle. (set = paused, so poll until
                        # cleared — Event.wait() would return immediately here.)
                        if self._throttle_event is not None and self._throttle_event.is_set():
                            self._segment.status = SegmentStatus.STALLED
                            while self._throttle_event.is_set() and not self._stop_flag.is_set():
                                time.sleep(0.1)
                            if self._stop_flag.is_set():
                                self._segment.status = SegmentStatus.PENDING
                                return
                            self._segment.status = SegmentStatus.ACTIVE
                            # Re-seek in case other segments wrote to the file.
                            f.seek(self._segment.start_byte + self._segment.completed_bytes)

                        if not chunk:
                            continue

                        chunk_len = len(chunk)
                        response_bytes += chunk_len
                        if expected_bytes >= 0 and response_bytes > expected_bytes:
                            raise RuntimeError("Response body exceeded the requested byte window")
                        f.write(chunk)
                        self._segment.completed_bytes += chunk_len
                        self._last_data_time = time.time()

                        if self._on_progress:
                            self._on_progress(chunk_len)
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()

            # Unknown-size segment: whatever we got to EOF is the whole file.
            if self._segment.end_byte < 0 or self._segment.completed_bytes >= self._segment.total_bytes:
                self._segment.status = SegmentStatus.DONE
                self._segment.speed_bps = 0
                if self._on_done:
                    self._on_done(True, "")
            else:
                # Stopped before completion.
                self._segment.status = SegmentStatus.PENDING
                if self._on_done:
                    self._on_done(False, "Incomplete")

        except Exception as exc:
            logger.warning("Segment %d error: %s", self._segment.index, exc)
            self._segment.status = SegmentStatus.ERROR
            self._segment.error = str(exc)
            self._segment.speed_bps = 0
            if self._on_done:
                self._on_done(False, str(exc))
