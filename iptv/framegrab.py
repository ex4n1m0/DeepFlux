"""Poster fallback: pull a frame out of the stream itself with FFmpeg.

No metadata provider covers everything — measured on a real playlist, TMDb
misses most VOD and TPDB reaches only about half of adult entries. A frame
taken from the actual stream has 100% coverage by construction, so it is the
last resort for any entry that would otherwise keep a blank tile.

How it plugs in
---------------
The grabbed frame is written straight into :class:`iptv.artwork.ArtworkCache`
under a synthetic ``framegrab:<sha1>`` URL. Nothing downstream needs to know:
``ArtworkCache.fetch_async`` finds it via ``get_cached``, builds the thumbnail
and hands back a path exactly as it would for a downloaded poster.

Politeness
----------
Every grab opens a connection to the user's IPTV provider and pulls part of a
video file, so this is deliberately conservative:

* **Visible tiles only.** The grid's background sweep must never trigger it —
  a 48k-entry adult section would otherwise mean 48k stream opens.
* At most :data:`_MAX_WORKERS` grabs run at once, on a dedicated pool so they
  cannot starve ordinary artwork downloads.
* Failures are remembered so a dead URL is attempted once per session.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
import time
from typing import Optional

from .artwork import _BoundedExecutor

logger = logging.getLogger(__name__)

# Hide the console window FFmpeg would pop up in the windowed build.
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Concurrent grabs. Low on purpose: each one is a video connection to the
# user's provider, not a cheap image GET.
_MAX_WORKERS = 2

# Where to seek before grabbing. The first seconds are usually studio idents
# or black, so prefer a frame from inside the content and fall back for
# anything shorter than the seek point.
_SEEK_SECONDS = (120, 8)

# Hard ceiling per FFmpeg invocation.
_TIMEOUT_SECONDS = 25
# FFmpeg's own I/O timeout (microseconds) so it can't hang before ours fires.
_RW_TIMEOUT_US = 15_000_000

# Stream CDNs reset connections under bursts.  Keep those retryable, but do
# not classify generic EOF/I/O failures as transient: a dead stream commonly
# exits with only "End of file" and should be eligible for negative caching.
_TRANSIENT_MARKERS = (
    "handshake", "10054", "connection reset", "connection aborted",
    "timed out", "timeout", "connection refused", "temporarily unavailable",
    "resource temporarily unavailable", "http error 429", "server returned 5",
)
_PERMANENT_MARKERS = (
    "end of file", "invalid data found", "404 not found", "http error 404",
    "server returned 4", "no such file", "moov atom not found",
    "could not find codec parameters", "does not contain any stream",
)


def _classify_ffmpeg_error(stderr: str) -> str:
    """Classify FFmpeg diagnostics, preferring explicit transient signals."""
    low = (stderr or "").lower()
    if any(marker in low for marker in _TRANSIENT_MARKERS):
        return "transient"
    if any(marker in low for marker in _PERMANENT_MARKERS):
        return "permanent"
    # Unknown failures are deterministic until evidence says otherwise and can
    # be negative-cached; this prevents repeatedly opening permanently dead
    # provider streams while tiles remain visible.
    return "permanent"
_RETRY_PASSES = 2
_RETRY_DELAY = 1.5

URL_PREFIX = "framegrab:"


def synthetic_url(stream_url: str) -> str:
    """Stable cache key for a stream's grabbed frame."""
    digest = hashlib.sha1((stream_url or "").encode("utf-8", "replace")).hexdigest()
    return f"{URL_PREFIX}{digest}"


def is_framegrab_url(url: str) -> bool:
    return (url or "").startswith(URL_PREFIX)


class FrameGrabber:
    """Extracts a poster frame from a stream URL into the artwork cache."""

    def __init__(self, artwork_cache, ffmpeg_path: str = "") -> None:
        self.cache = artwork_cache
        self._ffmpeg_path = ffmpeg_path
        self._executor = _BoundedExecutor(max_workers=_MAX_WORKERS)
        self._failed: set = set()
        self._inflight: set = set()
        self._lock = threading.Lock()

    # -- ffmpeg ---------------------------------------------------------------
    def _ffmpeg(self) -> str:
        if not self._ffmpeg_path:
            try:
                from dlmgr.ffmpeg import find_ffmpeg
                self._ffmpeg_path = find_ffmpeg()
            except Exception:
                self._ffmpeg_path = ""
        return self._ffmpeg_path

    @property
    def available(self) -> bool:
        return bool(self._ffmpeg())

    def _run(self, stream_url: str, dest: str, seek: int) -> str:
        """One FFmpeg invocation.

        Returns ``"ok"``, ``"transient"`` (worth retrying) or ``"permanent"``.
        """
        ffmpeg = self._ffmpeg()
        if not ffmpeg:
            return "permanent"
        base_cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-rw_timeout", str(_RW_TIMEOUT_US),
        ]
        reconnect_args = [
            # Ride out mid-transfer drops instead of failing the whole grab.
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ]
        input_cmd = [
            # -ss BEFORE -i is an input seek: FFmpeg range-requests straight to
            # the offset instead of decoding the whole file up to it.
            "-ss", str(seek),
            "-i", stream_url,
            "-frames:v", "1", "-an", "-sn",
            "-vf", "scale=720:-2",
            "-q:v", "3",
            "-f", "image2", dest,
        ]

        def _invoke(args):
            return subprocess.run(args, capture_output=True,
                                  timeout=_TIMEOUT_SECONDS,
                                  creationflags=_CREATION_FLAGS)

        try:
            proc = _invoke(base_cmd + reconnect_args + input_cmd)
            err = proc.stderr.decode(errors="replace")
            if proc.returncode and "option reconnect not found" in err.lower():
                proc = _invoke(base_cmd + input_cmd)
                err = proc.stderr.decode(errors="replace")
        except subprocess.TimeoutExpired:
            logger.debug("framegrab timed out at %ss", seek)
            return "transient"
        except OSError as exc:
            logger.debug("framegrab could not start ffmpeg: %s", exc)
            return "permanent"
        if proc.returncode == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return "ok"
        logger.debug("framegrab ffmpeg rc=%s: %s", proc.returncode, err[:200])
        return _classify_ffmpeg_error(err)

    # -- public API -----------------------------------------------------------
    def grab(self, stream_url: str) -> str:
        """Grab a frame synchronously. Returns the synthetic URL, or ''.

        Safe to call repeatedly: an already-cached frame short-circuits and a
        previous failure is not retried.
        """
        if not stream_url or not self.available:
            return ""
        url = synthetic_url(stream_url)
        if self.cache.get_cached(url):
            return url
        with self._lock:
            if url in self._failed:
                return ""

        dest = self.cache.full_path(url)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # Write to a temp file so a half-written frame is never treated as a
        # cache hit by get_cached().
        tmp = dest + ".part"
        ok = False
        transient = False
        try:
            for attempt in range(_RETRY_PASSES):
                # Recompute per pass.  A first-pass timeout followed by a
                # definitive EOF should end as permanent and be cached.
                transient = False
                for seek in _SEEK_SECONDS:
                    status = self._run(stream_url, tmp, seek)
                    if status == "ok":
                        ok = True
                        break
                    transient = transient or status == "transient"
                if ok or not transient:
                    break
                if attempt + 1 < _RETRY_PASSES:
                    time.sleep(_RETRY_DELAY)
            if ok:
                os.replace(tmp, dest)
        except OSError as exc:
            logger.debug("framegrab write failed: %s", exc)
            ok = False
        finally:
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if not ok:
            # Only a definitive failure is remembered. A CDN reset must stay
            # retryable or the tile is blank forever over a blip.
            if not transient:
                with self._lock:
                    self._failed.add(url)
            return ""
        return url

    def grab_async(self, stream_url: str, on_done) -> None:
        """Queue a grab; ``on_done(url_or_empty)`` runs on a worker thread."""
        if not stream_url or not self.available:
            on_done("")
            return
        url = synthetic_url(stream_url)
        cached = self.cache.get_cached(url)
        if cached:
            on_done(url)
            return
        with self._lock:
            if url in self._failed:
                on_done("")
                return
            if url in self._inflight:
                # Another tile is already grabbing this exact stream.
                on_done("")
                return
            self._inflight.add(url)

        def _work() -> None:
            result = ""
            try:
                result = self.grab(stream_url)
            except Exception:
                logger.exception("framegrab failed for a stream")
            finally:
                with self._lock:
                    self._inflight.discard(url)
                try:
                    on_done(result)
                except Exception:
                    logger.exception("framegrab on_done raised")

        self._executor.submit(_work)

    def reset(self) -> None:
        """Forget recorded failures — called when the artwork cache is cleared
        so previously-failing streams get one fresh attempt."""
        with self._lock:
            self._failed.clear()

    def shutdown(self) -> None:
        self._executor.shutdown()
