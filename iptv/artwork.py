"""Disk-backed artwork cache and async downloader.

Artwork (channel logos, movie/series posters & backdrops) is downloaded in
background threads, stored on disk under hashed filenames, and downscaled for
grid thumbnails. The UI never blocks on these: it asks for a pixmap and gets
either a cached file path or ``None`` (with the fetch kicked off in the
background).

Uses Pillow when available for downscaling; falls back to raw bytes if
Pillow is absent (the full image is still cached, just not thumbnailed).
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

try:
    from PIL import Image  # type: ignore
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

# Logo CDNs used by IPTV playlists (logo.m3uassets.com & friends) reset
# connections when hit with a burst of unconnected requests — a fresh TLS
# handshake per image at 6 workers loses ~45% of them. A per-thread keep-alive
# session plus a couple of retries takes that to ~1%.
_SESSIONS = threading.local()
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_DOWNLOAD_ATTEMPTS = 4
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
# Measured throughput on real playlists (posters + channel logos): 4 workers
# ~5-9 img/s, 8 ~14-18, 12 ~16-25, 16+ barely better. 12 is the knee; the
# handful of load-induced resets it costs are absorbed by the retry loop.
_MAX_WORKERS = 12

# Fetch priorities (lower runs first).
PRIORITY_VISIBLE = 0   # on screen right now
PRIORITY_NORMAL = 1
PRIORITY_PREFETCH = 2  # ahead of/behind the viewport
_PRIORITY_SHUTDOWN = -1


def _session() -> requests.Session:
    s = getattr(_SESSIONS, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": _BROWSER_UA, "Accept": "image/*,*/*;q=0.8"})
        _SESSIONS.session = s
    return s


_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",  # png
    b"\xff\xd8\xff",       # jpeg
    b"GIF87a", b"GIF89a",  # gif
    b"BM",                 # bmp
    b"II*\x00", b"MM\x00*",  # tiff
)


def _is_image_file(path: str) -> bool:
    """True when ``path`` holds a decodable raster image.

    Some hosts answer with an HTML error page (or an empty body) and a 200 —
    caching that would leave a permanently broken tile, so reject it here."""
    try:
        if os.path.getsize(path) < 64:
            return False
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    if not (head.startswith(_IMAGE_MAGIC) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")):
        return False
    if not _HAS_PIL:
        return True
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


class ArtworkCache:
    """On-disk image cache with background fetching + thumbnailing."""

    def __init__(self, root: str, thumb_size: Tuple[int, int] = (300, 450)) -> None:
        self.root = root
        self.full_dir = os.path.join(root, "full")
        self.thumb_dir = os.path.join(root, "thumb")
        os.makedirs(self.full_dir, exist_ok=True)
        os.makedirs(self.thumb_dir, exist_ok=True)
        self.thumb_size = thumb_size
        self._executor = _BoundedExecutor(max_workers=_MAX_WORKERS)
        self._inflight: Dict[str, threading.Event] = {}
        self._inflight_lock = threading.Lock()

    # -- paths ---------------------------------------------------------------
    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()

    def full_path(self, url: str) -> str:
        ext = os.path.splitext(urlparse(url).path)[1] or ".img"
        if len(ext) > 5:
            ext = ".img"
        return os.path.join(self.full_dir, self._key(url) + ext)

    def thumb_path(self, url: str) -> str:
        # Include the thumb size in the filename so a display-size bump
        # automatically regenerates existing thumbnails from the cached fulls.
        w, h = self.thumb_size
        return os.path.join(self.thumb_dir, f"{self._key(url)}_{w}x{h}.png")

    # -- synchronous helpers -------------------------------------------------
    def get_cached(self, url: str) -> Optional[str]:
        """Return the cached full-image path if present, else None."""
        if not url:
            return None
        p = self.full_path(url)
        return p if os.path.isfile(p) and os.path.getsize(p) > 0 else None

    def get_thumb(self, url: str) -> Optional[str]:
        if not url:
            return None
        p = self.thumb_path(url)
        return p if os.path.isfile(p) and os.path.getsize(p) > 0 else None

    # -- async fetch ---------------------------------------------------------
    def fetch_async(
        self,
        url: str,
        on_done: Optional[Callable[[str, Optional[str]], None]] = None,
        headers: Optional[dict] = None,
        priority: int = PRIORITY_NORMAL,
    ) -> None:
        """Fetch ``url`` in the background; call ``on_done(url, path_or_None)``.

        If the image is already cached, ``on_done`` is called immediately from
        this thread (so the UI can paint right away). Otherwise the fetch is
        queued and ``on_done`` is invoked from a worker thread — the caller is
        responsible for marshalling back to the GUI thread.
        """
        if not url:
            if on_done:
                on_done(url, None)
            return
        cached = self.get_cached(url)
        if cached:
            self._ensure_thumb(url)
            if on_done:
                on_done(url, self.get_thumb(url) or cached)
            return

        # Dedupe concurrent fetches of the same URL.
        with self._inflight_lock:
            ev = self._inflight.get(url)
            if ev is None:
                ev = threading.Event()
                self._inflight[url] = ev
                self._executor.submit(self._fetch_worker, url, on_done, headers, ev,
                                      priority=priority)
            else:
                # Already fetching; piggyback a second callback.
                self._executor.submit(self._wait_and_callback, url, on_done, ev,
                                      priority=priority)

    def _wait_and_callback(self, url: str, on_done, ev: threading.Event) -> None:
        ev.wait(timeout=30)
        if on_done:
            on_done(url, self.get_thumb(url) or self.get_cached(url))

    def _fetch_worker(self, url: str, on_done, headers, ev: threading.Event) -> None:
        path: Optional[str] = None
        try:
            path = self._download(url, headers)
            if path:
                self._ensure_thumb(url)
                path = self.get_thumb(url) or path
        except Exception as exc:
            logger.debug("Artwork fetch failed for %s: %s", url, exc)
        finally:
            ev.set()
            with self._inflight_lock:
                self._inflight.pop(url, None)
            if on_done:
                try:
                    on_done(url, path)
                except Exception:
                    logger.exception("artwork on_done callback raised")

    def _download(self, url: str, headers: Optional[dict]) -> Optional[str]:
        """Download ``url`` into the cache, retrying transient failures.

        Returns the cached path, or None when the image is permanently
        unavailable (404, non-image payload, size limit)."""
        dest = self.full_path(url)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return dest
        for attempt in range(_DOWNLOAD_ATTEMPTS):
            try:
                path, retry = self._download_once(url, headers, dest)
                if path or not retry:
                    return path
            except requests.RequestException as exc:
                if attempt == _DOWNLOAD_ATTEMPTS - 1:
                    logger.debug("Artwork download failed for %s: %s", url, exc)
                    raise
            if attempt < _DOWNLOAD_ATTEMPTS - 1:
                # Jittered backoff: a CDN that reset a burst of connections
                # would just get the same burst back in lockstep otherwise.
                time.sleep(0.3 * (2 ** attempt) * (0.5 + random.random()))
        return None

    def _download_once(
        self, url: str, headers: Optional[dict], dest: str
    ) -> Tuple[Optional[str], bool]:
        """One download attempt. Returns (path_or_None, worth_retrying)."""
        hdrs = dict(headers) if headers else None
        resp = _session().get(url, headers=hdrs, timeout=20, stream=True)
        if resp.status_code != 200:
            resp.close()
            if resp.status_code in _RETRY_STATUS:
                return None, True
            logger.debug("Artwork HTTP %d for %s", resp.status_code, url)
            return None, False
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and not (ctype.startswith("image/") or ctype == "application/octet-stream"):
            resp.close()
            logger.debug("Artwork is not an image (%s): %s", ctype, url)
            return None, False
        # Guard against oversized/bogus responses filling the disk.
        max_bytes = 20 * 1024 * 1024
        declared = int(resp.headers.get("Content-Length") or 0)
        if declared > max_bytes:
            logger.debug("Artwork too large (%d bytes): %s", declared, url)
            return None, False
        tmp = dest + ".part"
        downloaded = 0
        try:
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(8192):
                    if chunk:
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            logger.debug("Artwork exceeded size limit: %s", url)
                            return None, False
                        f.write(chunk)
            if not _is_image_file(tmp):
                logger.debug("Artwork payload is not a decodable image: %s", url)
                return None, False
            os.replace(tmp, dest)
        finally:
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return dest, False

    def _ensure_thumb(self, url: str) -> None:
        thumb = self.thumb_path(url)
        if os.path.isfile(thumb) and os.path.getsize(thumb) > 0:
            return
        full = self.full_path(url)
        if not os.path.isfile(full):
            return
        if not _HAS_PIL:
            return
        try:
            with Image.open(full) as im:
                im = im.convert("RGBA")
                im.thumbnail(self.thumb_size)
                im.save(thumb, "PNG")
        except Exception as exc:
            logger.debug("Thumbnail generation failed for %s: %s", url, exc)

    def submit_task(self, fn) -> None:
        """Run an arbitrary small task on the bounded pool (off the caller's thread)."""
        self._executor.submit(fn)

    # -- maintenance ---------------------------------------------------------
    def prune(self, max_age_days: int = 30) -> int:
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        for d in (self.full_dir, self.thumb_dir):
            for name in os.listdir(d):
                p = os.path.join(d, name)
                try:
                    if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                        os.remove(p)
                        removed += 1
                except OSError:
                    pass
        return removed

    def shutdown(self) -> None:
        self._executor.shutdown()


class _BoundedExecutor:
    """A fixed-size worker pool fed by a priority queue (stdlib only).

    Unlike a semaphore + thread-per-task approach, submitting 50k tasks only
    queues 50k work items — the worker thread count stays at ``max_workers``.

    Order is (priority, newest first). Scrolling a big grid keeps queueing
    prefetches; without this the tiles the user is looking at right now would
    sit behind a minute's worth of stale requests for rows they scrolled past.
    """

    def __init__(self, max_workers: int = 6) -> None:
        import itertools
        import queue
        self._queue: "queue.PriorityQueue" = queue.PriorityQueue()
        self._counter = itertools.count()
        self._counter_lock = threading.Lock()
        self._max_workers = max_workers
        self._shutdown = False
        for _ in range(max_workers):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()

    def _next_seq(self) -> int:
        with self._counter_lock:
            return next(self._counter)

    def _worker(self) -> None:
        while True:
            _prio, _seq, fn, args, kwargs = self._queue.get()
            if fn is None:  # shutdown sentinel
                self._queue.task_done()
                return
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception("background task failed")
            finally:
                self._queue.task_done()

    def submit(self, fn, *args, priority: int = PRIORITY_NORMAL, **kwargs) -> None:
        if not self._shutdown:
            # Negative sequence => most recently queued task runs first.
            self._queue.put((priority, -self._next_seq(), fn, args, kwargs))

    def shutdown(self) -> None:
        # Stop accepting work and let in-flight/queued tasks drain.
        self._shutdown = True
        # Wake each worker with a sentinel so blocked queue.get() calls return.
        # Highest priority so exit isn't stuck behind a full prefetch queue.
        for _ in range(self._max_workers):
            self._queue.put((_PRIORITY_SHUTDOWN, -self._next_seq(), None, (), {}))
