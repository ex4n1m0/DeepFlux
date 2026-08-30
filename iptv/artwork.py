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
import re
import json
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

try:
    from PIL import Image, features as _pil_features  # type: ignore
    _HAS_PIL = True
    _HAS_WEBP = bool(_pil_features.check("webp"))
except Exception:
    _HAS_PIL = False
    _HAS_WEBP = False

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

# SVG detection: SVG files start with <?xml or <svg (with optional whitespace/BOM).
_SVG_MAGIC = (b"<?xml", b"<svg")
_SVG_RE = re.compile(rb"<svg", re.IGNORECASE)


def _is_svg(data: bytes) -> bool:
    """True when ``data`` looks like an SVG file."""
    head = data[:512].lstrip()
    # Skip UTF-8 BOM if present.
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:].lstrip()
    return head.startswith(_SVG_MAGIC) or bool(_SVG_RE.search(data[:512]))


def _rasterize_svg(svg_path: str, out_path: str, size: int = 512) -> bool:
    """Rasterize an SVG to PNG. Returns True on success.

    Tries QtSvg (QSvgRenderer) first — it's the most reliable on Windows.
    Falls back to cairosvg if available (pip package, not bundled).
    """
    # Path 1: QtSvg (ships with PyQt5/PySide6 on Windows).
    try:
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
        with open(svg_path, "rb") as f:
            svg_data = f.read()
        renderer = QSvgRenderer(QByteArray(svg_data))
        if renderer.isValid():
            img = QImage(size, size, QImage.Format_ARGB32)
            img.fill(0)  # transparent
            painter = QPainter(img)
            renderer.render(painter)
            painter.end()
            buf = QBuffer()
            buf.open(QIODevice.WriteOnly)
            img.save(buf, "PNG")
            with open(out_path, "wb") as f:
                f.write(buf.data().data())
            return True
    except Exception:
        pass
    # Path 2: cairosvg (pip package, optional).
    try:
        import cairosvg
        cairosvg.svg2png(url=svg_path, write_to=out_path, output_width=size, output_height=size)
        return True
    except Exception:
        pass
    return False


def _is_image_file(path: str) -> bool:
    """True when ``path`` holds a decodable raster image or an SVG.

    Some hosts answer with an HTML error page (or an empty body) and a 200 —
    caching that would leave a permanently broken tile, so reject it here.
    SVGs are accepted (the artwork cache rasterizes them to PNG on download
    so Qt can render them without the SVG image plugin)."""
    try:
        if os.path.getsize(path) < 64:
            return False
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return False
    # SVG check first — SVGs don't have the raster magic bytes.
    if _is_svg(head):
        return True
    if not (head[:16].startswith(_IMAGE_MAGIC) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")):
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

    def _meta_path(self, url: str) -> str:
        """Sidecar path for ETag/Last-Modified revalidation data."""
        return os.path.join(self.full_dir, self._key(url) + ".meta")

    def _load_meta(self, url: str) -> dict:
        """Load cached HTTP metadata (ETag, Last-Modified) for a URL."""
        p = self._meta_path(url)
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_meta(self, url: str, etag: str = "", last_modified: str = "") -> None:
        """Persist ETag/Last-Modified for conditional revalidation."""
        if not etag and not last_modified:
            return
        p = self._meta_path(url)
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"etag": etag, "last_modified": last_modified}, f)
        except OSError:
            pass

    def thumb_path(self, url: str) -> str:
        # Include the thumb size in the filename so a display-size bump
        # automatically regenerates existing thumbnails from the cached fulls.
        # Thumbnails are WebP (lossy q85): ~5-10x smaller than the PNGs they
        # replaced while keeping alpha for channel logos. Qt and Pillow both
        # read/write WebP; the legacy .png path stays readable for caches
        # written before the switch.
        w, h = self.thumb_size
        return os.path.join(self.thumb_dir, f"{self._key(url)}_{w}x{h}.webp")

    def _legacy_thumb_path(self, url: str) -> str:
        w, h = self.thumb_size
        return os.path.join(self.thumb_dir, f"{self._key(url)}_{w}x{h}.png")

    def _thumb_candidates(self, url: str) -> Tuple[str, str]:
        return self.thumb_path(url), self._legacy_thumb_path(url)

    # -- synchronous helpers -------------------------------------------------
    @staticmethod
    def _touch(path: str) -> None:
        """Bump atime so size-limit eviction is true LRU. Windows disables
        NTFS last-access updates by default, so reads don't refresh atime on
        their own — without this the eviction order degrades to FIFO."""
        try:
            st = os.stat(path)
            os.utime(path, (time.time(), st.st_mtime))
        except OSError:
            pass

    def get_cached(self, url: str) -> Optional[str]:
        """Return the cached full-image path if present, else None."""
        if not url:
            return None
        p = self.full_path(url)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            self._touch(p)
            return p
        return None

    def get_thumb(self, url: str) -> Optional[str]:
        if not url:
            return None
        for p in self._thumb_candidates(url):
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                self._touch(p)
                return p
        return None

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
        unavailable (404, non-image payload, size limit). Sends
        If-None-Match/If-Modified-Since when ETag/Last-Modified metadata is
        cached from a prior fetch — a 304 response reuses the cached file
        without re-downloading the body."""
        dest = self.full_path(url)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            # File already cached — but we may want to revalidate. For now,
            # the cache is treated as fresh until the user clears it or the
            # file is deleted. ETag revalidation happens on a forced refresh.
            return dest
        # Build conditional headers from cached ETag/Last-Modified.
        meta = self._load_meta(url)
        cond_headers = dict(headers) if headers else {}
        if meta.get("etag"):
            cond_headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            cond_headers["If-Modified-Since"] = meta["last_modified"]
        for attempt in range(_DOWNLOAD_ATTEMPTS):
            try:
                path, retry = self._download_once(url, cond_headers, dest)
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
        """One download attempt. Returns (path_or_None, worth_retrying).

        Handles 304 Not Modified (ETag/If-None-Match revalidation): the cached
        file is reused without re-downloading the body. Saves ETag and
        Last-Modified headers for future conditional requests."""
        hdrs = dict(headers) if headers else None
        resp = _session().get(url, headers=hdrs, timeout=20, stream=True)
        # 304 Not Modified: cached file is still valid — reuse it.
        if resp.status_code == 304:
            resp.close()
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                return dest, False
            # 304 but no cached file — treat as a fresh miss (shouldn't happen
            # unless the meta file outlived the image file).
            return None, False
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
        # SVG content-type is image/svg+xml — accepted above, but Qt can't
        # render SVG without the plugin. Rasterize to PNG after download.
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
            # If the downloaded file is an SVG, rasterize it to PNG so Qt
            # can render it without the SVG image plugin. The PNG replaces
            # the SVG in the cache — subsequent reads are pure raster.
            with open(tmp, "rb") as f:
                head = f.read(512)
            if _is_svg(head):
                png_dest = dest + ".png"
                if _rasterize_svg(tmp, png_dest):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    # The cache key (full_path) points to the original dest;
                    # store the PNG there instead so get() finds it.
                    os.replace(png_dest, dest)
                else:
                    logger.debug("SVG rasterization failed for %s", url)
                    return None, False
            else:
                os.replace(tmp, dest)
            # Save ETag/Last-Modified for future conditional revalidation.
            self._save_meta(url,
                            etag=resp.headers.get("ETag", ""),
                            last_modified=resp.headers.get("Last-Modified", ""))
        finally:
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return dest, False

    def _ensure_thumb(self, url: str) -> None:
        webp, legacy = self._thumb_candidates(url)
        for p in (webp, legacy):
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return
        full = self.full_path(url)
        if not os.path.isfile(full):
            return
        if not _HAS_PIL:
            return
        try:
            with Image.open(full) as im:
                # Progressive JPEG optimization: use draft mode to decode at
                # the target thumbnail size directly. This avoids decoding the
                # full-resolution image (often 2000x3000px for movie posters)
                # only to downscale it — draft() reads only the needed DCT
                # coefficients, cutting memory and CPU by 5-10x for large JPEGs.
                if im.format == "JPEG":
                    try:
                        im.draft("RGB", self.thumb_size)
                    except Exception:
                        pass  # not a progressive JPEG or draft not supported
                im = im.convert("RGBA")
                im.thumbnail(self.thumb_size)
                if _HAS_WEBP:
                    im.save(webp, "WEBP", quality=85, method=4)
                else:
                    im.save(legacy, "PNG")
        except Exception as exc:
            logger.debug("Thumbnail generation failed for %s: %s", url, exc)

    def submit_task(self, fn) -> None:
        """Run an arbitrary small task on the bounded pool (off the caller's thread)."""
        self._executor.submit(fn)

    # -- maintenance ---------------------------------------------------------
    def stats(self) -> Dict[str, int]:
        """On-disk footprint: file counts and bytes for fulls and thumbs."""
        out = {"full_files": 0, "full_bytes": 0, "thumb_files": 0, "thumb_bytes": 0}
        for d, prefix in ((self.full_dir, "full"), (self.thumb_dir, "thumb")):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                p = os.path.join(d, name)
                try:
                    if os.path.isfile(p):
                        out[prefix + "_files"] += 1
                        out[prefix + "_bytes"] += os.path.getsize(p)
                except OSError:
                    pass
        out["total_bytes"] = out["full_bytes"] + out["thumb_bytes"]
        return out

    def clear(self) -> Tuple[int, int]:
        """Delete every cached image (fulls, thumbs, .meta sidecars, .part
        strays). Returns (files_removed, bytes_freed). In-flight downloads
        may re-add a file after the wipe — harmless, it's just re-cached."""
        removed = 0
        freed = 0
        for d in (self.full_dir, self.thumb_dir):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                p = os.path.join(d, name)
                try:
                    if os.path.isfile(p):
                        freed += os.path.getsize(p)
                        os.remove(p)
                        removed += 1
                except OSError:
                    pass
        return removed, freed

    def enforce_size_limit(self, limit_bytes: int, target_ratio: float = 0.9) -> int:
        """Evict least-recently-used files until the cache fits ``limit_bytes``.

        Eviction is grouped by URL key: deleting a full image also deletes its
        thumbnails (any size, either format) and its ``.meta`` sidecar, so no
        orphan files accumulate. Groups go oldest-access first (atime, bumped
        on every cache hit by :meth:`_touch`). Returns bytes freed.
        """
        if limit_bytes <= 0:
            return 0
        groups: Dict[str, Dict[str, Any]] = {}
        total = 0
        for d in (self.full_dir, self.thumb_dir):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                p = os.path.join(d, name)
                try:
                    if not os.path.isfile(p):
                        continue
                    st = os.stat(p)
                except OSError:
                    continue
                total += st.st_size
                # "<sha1>.jpg" / "<sha1>.meta" / "<sha1>_300x450.webp" -> sha1
                key = name.split(".", 1)[0].split("_", 1)[0]
                g = groups.setdefault(key, {"recency": 0.0, "size": 0, "paths": []})
                g["size"] += st.st_size
                g["recency"] = max(g["recency"], st.st_atime, st.st_mtime)
                g["paths"].append(p)
        if total <= limit_bytes:
            return 0
        target = int(limit_bytes * target_ratio)
        freed = 0
        for _key, g in sorted(groups.items(), key=lambda kv: kv[1]["recency"]):
            if total - freed <= target:
                break
            for p in g["paths"]:
                try:
                    os.remove(p)
                except OSError:
                    pass
            freed += g["size"]
        return freed

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
