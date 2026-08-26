"""HLS/DASH stream capture pipeline.

Parses HLS (.m3u8) and DASH (.mpd) manifests, downloads segments in
parallel using the segmented engine, decrypts AES-128 encrypted HLS
segments, and prepares them for FFmpeg remuxing.

DRM detection: streams using SAMPLE-AES or containing Widevine/FairPlay/
PlayReady PSSH boxes are detected and refused with a clear message.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from . import http_client

logger = logging.getLogger(__name__)

# Maximum segment download retries before giving up. Some CDNs reset whole
# bursts of connections intermittently — retry generously with backoff.
MAX_RETRIES = 10
# Retries for manifest/key fetches. Connections to some CDNs are reset
# intermittently (middlebox RST injection), so keep retrying with backoff —
# the same stream typically succeeds within a few attempts.
MANIFEST_RETRIES = 8
RETRY_BACKOFF = 1.5  # seconds, multiplied by attempt number
# Timeout for segment downloads (seconds).
SEGMENT_TIMEOUT = 30
# Default User-Agent when the caller (browser extension) doesn't supply one.
# Many CDNs reject the "python-requests/x.y" default outright.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _apply_default_headers(headers: Dict[str, str], cookies: str, referrer: str) -> Dict[str, str]:
    """Merge cookies/referrer into headers and ensure a browser-like UA."""
    merged = dict(headers or {})
    if referrer:
        merged.setdefault("Referer", referrer)
    if cookies:
        merged.setdefault("Cookie", cookies)
    merged.setdefault("User-Agent", DEFAULT_USER_AGENT)
    return merged


@dataclass
class StreamRendition:
    """A single quality rendition from a manifest."""
    bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""
    url: str = ""
    height: int = 0
    width: int = 0


@dataclass
class StreamInfo:
    """Parsed manifest information ready for download."""
    stream_type: str = "hls"  # "hls" or "dash"
    manifest_url: str = ""
    renditions: List[StreamRendition] = field(default_factory=list)
    segment_urls: List[str] = field(default_factory=list)
    init_segment_url: str = ""  # For fMP4/DASH
    is_live: bool = False
    is_encrypted: bool = False
    is_drm_protected: bool = False
    encryption_key_url: str = ""
    encryption_iv: str = ""
    encryption_method: str = ""  # "AES-128", "SAMPLE-AES", "NONE"
    title: str = ""
    total_segments: int = 0
    error: str = ""


class DRMError(Exception):
    """Raised when a stream is DRM-protected and cannot be downloaded."""
    pass


class ManifestParser:
    """Parses HLS and DASH manifests into a StreamInfo object."""

    def __init__(self, headers: Optional[Dict[str, str]] = None, cookies: str = "", referrer: str = "") -> None:
        self._headers = _apply_default_headers(headers, cookies, referrer)

    def parse(self, url: str) -> StreamInfo:
        """Fetch and parse a manifest URL. Auto-detects HLS vs DASH."""
        try:
            content = self._fetch_text(url)
        except Exception as exc:
            return StreamInfo(manifest_url=url, error=f"Failed to fetch manifest: {exc}")

        if url.endswith(".mpd") or "<?xml" in content[:200]:
            return self._parse_dash(url, content)
        else:
            return self._parse_hls(url, content)

    def _fetch_text(self, url: str) -> str:
        return self._fetch_with_retry(url).text

    def _fetch_bytes(self, url: str) -> bytes:
        return self._fetch_with_retry(url).content

    def _fetch_with_retry(self, url: str):
        last_exc: Optional[Exception] = None
        for attempt in range(MANIFEST_RETRIES):
            try:
                resp = http_client.get(url, headers=self._headers, timeout=15)
                resp.raise_for_status()
                return resp
            except Exception as exc:
                last_exc = exc
                if attempt < MANIFEST_RETRIES - 1:
                    logger.debug("fetch %s attempt %d failed: %s", url, attempt + 1, exc)
                    time.sleep(RETRY_BACKOFF * (attempt + 1) * 0.5)
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # HLS parsing
    # ------------------------------------------------------------------

    def _parse_hls(self, manifest_url: str, content: str) -> StreamInfo:
        """Parse an HLS M3U8 manifest."""
        try:
            import m3u8
        except ImportError:
            return StreamInfo(manifest_url=manifest_url, error="m3u8 library not installed")

        try:
            playlist = m3u8.loads(content, uri=manifest_url)
        except Exception as exc:
            return StreamInfo(manifest_url=manifest_url, error=f"Failed to parse M3U8: {exc}")

        info = StreamInfo(stream_type="hls", manifest_url=manifest_url)

        # Check for DRM.
        if playlist.keys:
            for key in playlist.keys:
                if key and key.method:
                    info.encryption_method = key.method
                    if key.method.upper() == "SAMPLE-AES":
                        info.is_drm_protected = True
                        info.error = "DRM-protected stream (SAMPLE-AES). Cannot download."
                        return info
                    if key.method.upper() not in ("NONE", "AES-128"):
                        info.is_drm_protected = True
                        info.error = f"Unsupported encryption: {key.method}. Cannot download."
                        return info
                    if key.method.upper() == "AES-128":
                        info.is_encrypted = True
                        info.encryption_key_url = key.absolute_uri or ""
                        info.encryption_iv = key.iv or ""

        # Check if live.
        info.is_live = not playlist.is_endlist

        # If this is a master playlist (has variants), pick the best rendition.
        if playlist.playlists:
            for variant in playlist.playlists:
                rend = StreamRendition(
                    bandwidth=variant.stream_info.bandwidth or 0,
                    codecs=variant.stream_info.codecs or "",
                    url=variant.absolute_uri or urljoin(manifest_url, variant.uri),
                )
                # m3u8 returns resolution as a (width, height) tuple; some
                # manifests/parsers may yield a "1920x1080" string instead.
                res = getattr(variant.stream_info, "resolution", None)
                if isinstance(res, (tuple, list)) and len(res) == 2:
                    rend.width, rend.height = int(res[0]), int(res[1])
                    rend.resolution = f"{rend.width}x{rend.height}"
                elif isinstance(res, str) and res:
                    rend.resolution = res
                    parts = res.split("x")
                    if len(parts) == 2:
                        try:
                            rend.width = int(parts[0])
                            rend.height = int(parts[1])
                        except ValueError:
                            pass
                info.renditions.append(rend)

            # Pick the highest bandwidth rendition.
            best = max(info.renditions, key=lambda r: r.bandwidth)
            logger.info("HLS master playlist: %d renditions, picked %dbps %s",
                        len(info.renditions), best.bandwidth, best.resolution)

            # Fetch the media playlist for the best rendition.
            try:
                sub_content = self._fetch_text(best.url)
                sub_playlist = m3u8.loads(sub_content, uri=best.url)
                playlist = sub_playlist
                info.manifest_url = best.url
            except Exception as exc:
                return StreamInfo(manifest_url=manifest_url, error=f"Failed to fetch rendition playlist: {exc}")

        # Collect segment URLs.
        for segment in playlist.segments:
            seg_url = segment.absolute_uri or urljoin(manifest_url, segment.uri)
            info.segment_urls.append(seg_url)

        # Check for init segment (fMP4).
        if playlist.segment_map and playlist.segment_map.uri:
            info.init_segment_url = urljoin(manifest_url, playlist.segment_map.uri)

        info.total_segments = len(info.segment_urls)
        logger.info("HLS playlist: %d segments, live=%s, encrypted=%s",
                     info.total_segments, info.is_live, info.is_encrypted)
        return info

    # ------------------------------------------------------------------
    # DASH parsing
    # ------------------------------------------------------------------

    def _parse_dash(self, manifest_url: str, content: str) -> StreamInfo:
        """Parse a DASH MPD manifest."""
        try:
            import xml.etree.ElementTree as ET
        except ImportError:
            return StreamInfo(manifest_url=manifest_url, error="xml.etree not available")

        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            return StreamInfo(manifest_url=manifest_url, error=f"Failed to parse MPD XML: {exc}")

        info = StreamInfo(stream_type="dash", manifest_url=manifest_url)

        # Check if live (dynamic MPD).
        mpd_type = root.get("type", "static").lower()
        info.is_live = mpd_type == "dynamic"

        # Check for ContentProtection (DRM).
        for cp in root.iter("{urn:mpeg:dash:schema:mpd:2011}ContentProtection"):
            scheme = cp.get("schemeIdUri", "").lower()
            if "widevine" in scheme or "fairplay" in scheme or "playready" in scheme:
                info.is_drm_protected = True
                info.error = f"DRM-protected stream ({scheme}). Cannot download."
                return info

        # Find the best video AdaptationSet.
        ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
        best_rep = None
        best_bandwidth = 0

        for aset in root.findall(".//mpd:AdaptationSet", ns):
            content_type = aset.get("contentType", "")
            if content_type and content_type != "video":
                continue
            for rep in aset.findall("mpd:Representation", ns):
                bw = int(rep.get("bandwidth", "0"))
                if bw > best_bandwidth:
                    best_bandwidth = bw
                    best_rep = rep
                    # Get segment template/list from the adaptation set or representation.
                    best_aset = aset

        if best_rep is None:
            # Try without namespace (some MPDs don't use the namespace).
            for aset in root.iter("AdaptationSet"):
                content_type = aset.get("contentType", "")
                if content_type and content_type != "video":
                    continue
                for rep in aset.iter("Representation"):
                    bw = int(rep.get("bandwidth", "0"))
                    if bw > best_bandwidth:
                        best_bandwidth = bw
                        best_rep = rep
                        best_aset = aset
            if best_rep is None:
                return StreamInfo(manifest_url=manifest_url, error="No video representation found in MPD")

        # Build rendition info.
        rend = StreamRendition(
            bandwidth=best_bandwidth,
            codecs=best_rep.get("codecs", ""),
            height=int(best_rep.get("height", "0")),
            width=int(best_rep.get("width", "0")),
        )
        info.renditions.append(rend)

        # Build segment URL list from SegmentTemplate or SegmentList.
        info.segment_urls, info.init_segment_url = self._build_dash_segments(
            manifest_url, best_aset, best_rep
        )
        info.total_segments = len(info.segment_urls)
        logger.info("DASH manifest: %d segments, live=%s, init=%s",
                     info.total_segments, info.is_live, bool(info.init_segment_url))
        return info

    def _build_dash_segments(self, base_url: str, aset, rep) -> Tuple[List[str], str]:
        """Build a list of segment URLs from DASH SegmentTemplate or SegmentList."""
        segments: List[str] = []
        init_url = ""

        # Look for SegmentTemplate on the adaptation set or representation.
        seg_template = aset.find("mpd:SegmentTemplate", {"mpd": "urn:mpeg:dash:schema:mpd:2011"})
        if seg_template is None:
            seg_template = rep.find("mpd:SegmentTemplate", {"mpd": "urn:mpeg:dash:schema:mpd:2011"})
        if seg_template is None:
            seg_template = aset.find("SegmentTemplate")
        if seg_template is None:
            seg_template = rep.find("SegmentTemplate")

        if seg_template is not None:
            media_template = seg_template.get("media", "")
            init_template = seg_template.get("initialization", "")
            rep_id = rep.get("id", "")
            bandwidth = rep.get("bandwidth", "")

            # Init segment.
            if init_template:
                init_url = urljoin(base_url, init_template.replace("$RepresentationID$", rep_id).replace("$Bandwidth$", bandwidth))

            # Segment timeline.
            timeline = seg_template.find("mpd:SegmentTimeline", {"mpd": "urn:mpeg:dash:schema:mpd:2011"})
            if timeline is None:
                timeline = seg_template.find("SegmentTimeline")

            if timeline is not None:
                seg_num = int(seg_template.get("startNumber", "1"))
                for s in timeline.iter("S"):
                    duration = int(s.get("d", "0"))
                    repeat = int(s.get("r", "0"))
                    for _ in range(repeat + 1):
                        url = urljoin(base_url, media_template
                                      .replace("$Number$", str(seg_num))
                                      .replace("$RepresentationID$", rep_id)
                                      .replace("$Bandwidth$", bandwidth))
                        segments.append(url)
                        seg_num += 1
            else:
                # Use startNumber + duration to calculate segment count.
                timescale = int(seg_template.get("timescale", "1"))
                duration = int(seg_template.get("duration", "0"))
                start = int(seg_template.get("startNumber", "1"))
                if duration > 0 and timescale > 0:
                    # Assume a reasonable count (will be refined for live streams).
                    for i in range(start, start + 1000):
                        url = urljoin(base_url, media_template
                                      .replace("$Number$", str(i))
                                      .replace("$RepresentationID$", rep_id)
                                      .replace("$Bandwidth$", bandwidth))
                        segments.append(url)

        # Fallback: SegmentList.
        if not segments:
            seg_list = rep.find("mpd:SegmentList", {"mpd": "urn:mpeg:dash:schema:mpd:2011"})
            if seg_list is None:
                seg_list = rep.find("SegmentList")
            if seg_list is not None:
                for surl in seg_list.iter("SegmentURL"):
                    media = surl.get("media", "")
                    if media:
                        segments.append(urljoin(base_url, media))

        return segments, init_url


class HLSDownloader:
    """Downloads HLS/DASH segments and prepares them for remuxing.

    Segments are downloaded in parallel to a temp directory. AES-128
    encrypted segments are decrypted in-place. The resulting files are
    ready for FFmpeg remuxing via the ffmpeg module."""

    def __init__(
        self,
        stream_info: StreamInfo,
        temp_dir: str,
        headers: Optional[Dict[str, str]] = None,
        cookies: str = "",
        referrer: str = "",
        max_workers: int = 8,
        on_progress: Optional[Any] = None,
    ) -> None:
        self._info = stream_info
        self._temp_dir = temp_dir
        self._headers = _apply_default_headers(headers, cookies, referrer)
        # Cap stream parallelism: connection bursts to some CDNs trigger
        # middlebox RST flooding; 4 concurrent fetches stay under the radar.
        self._max_workers = min(max_workers, 4)
        self._on_progress = on_progress  # called with segments-completed count
        self._encryption_key: Optional[bytes] = None
        self._stop_flag = threading.Event()
        self._progress = 0
        self.bytes_downloaded = 0  # real byte total, for speed reporting
        self._total = max(1, len(stream_info.segment_urls))

    @property
    def progress(self) -> float:
        return self._progress / self._total

    def stop(self) -> None:
        self._stop_flag.set()

    def download(self) -> List[str]:
        """Download all segments. Returns a list of segment file paths in order.

        Raises DRMError if the stream is DRM-protected."""
        if self._info.is_drm_protected:
            raise DRMError(self._info.error or "DRM-protected stream")

        os.makedirs(self._temp_dir, exist_ok=True)

        # Fetch encryption key if needed.
        if self._info.is_encrypted and self._info.encryption_key_url:
            last_exc: Optional[Exception] = None
            for attempt in range(MANIFEST_RETRIES):
                try:
                    resp = http_client.get(self._info.encryption_key_url, headers=self._headers, timeout=15)
                    resp.raise_for_status()
                    self._encryption_key = resp.content
                    logger.info("Fetched HLS encryption key (%d bytes)", len(self._encryption_key))
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < MANIFEST_RETRIES - 1:
                        time.sleep(RETRY_BACKOFF * (attempt + 1) * 0.5)
            else:
                raise RuntimeError(f"Failed to fetch encryption key: {last_exc}")

        # Download init segment if present (fMP4/DASH).
        if self._info.init_segment_url:
            init_path = os.path.join(self._temp_dir, "init.mp4")
            self._download_segment(self._info.init_segment_url, init_path, 0)

        # Download all segments in parallel.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        segment_paths: Dict[int, str] = {}

        def _download_one(idx: int, url: str) -> Tuple[int, str]:
            ext = os.path.splitext(urlparse(url).path)[1] or ".ts"
            path = os.path.join(self._temp_dir, f"seg_{idx:05d}{ext}")
            # Restart support: segments from a previous (paused) run are
            # complete files — skip them instead of re-downloading.
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                self._progress += 1
                return idx, path
            self._download_segment(url, path, idx)
            return idx, path

        failed: List[Tuple[int, str]] = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {}
            for idx, url in enumerate(self._info.segment_urls):
                if self._stop_flag.is_set():
                    break
                futures[pool.submit(_download_one, idx, url)] = idx

            for future in as_completed(futures):
                if self._stop_flag.is_set():
                    break
                try:
                    idx, path = future.result()
                    segment_paths[idx] = path
                except Exception as exc:
                    logger.warning("Segment download failed: %s", exc)
                    failed.append((futures[future], str(exc)[:120]))
                self._progress += 1
                if self._on_progress:
                    self._on_progress(len(segment_paths))

        if failed:
            sample = "; ".join(f"seg {i}: {e}" for i, e in failed[:3])
            raise RuntimeError(
                f"{len(failed)}/{len(self._info.segment_urls)} segments failed "
                f"after {MAX_RETRIES} retries ({sample})"
            )

        # Return segments in order.
        ordered = [segment_paths[i] for i in range(len(self._info.segment_urls)) if i in segment_paths]
        return ordered

    def _download_segment(self, url: str, path: str, index: int) -> None:
        """Download a single segment with retries. Decrypts if needed."""
        for attempt in range(MAX_RETRIES):
            if self._stop_flag.is_set():
                return
            try:
                resp = http_client.get(url, headers=self._headers, timeout=SEGMENT_TIMEOUT)
                resp.raise_for_status()
                data = resp.content

                # Decrypt if AES-128.
                if self._encryption_key and self._info.encryption_method.upper() == "AES-128":
                    data = self._decrypt_aes128(data, index)

                with open(path, "wb") as f:
                    f.write(data)
                self.bytes_downloaded += len(data)
                return
            except Exception as exc:
                if attempt < MAX_RETRIES - 1:
                    logger.debug("Segment %d retry %d: %s", index, attempt + 1, exc)
                    time.sleep(RETRY_BACKOFF * (attempt + 1) * 0.5)
                else:
                    raise

    def _decrypt_aes128(self, data: bytes, segment_index: int) -> bytes:
        """Decrypt AES-128-CBC encrypted HLS segment."""
        from Crypto.Cipher import AES

        # Determine IV.
        if self._info.encryption_iv:
            # IV is specified as hex.
            iv = bytes.fromhex(self._info.encryption_iv.replace("0x", ""))
        else:
            # Default IV is the segment index as a 16-byte big-endian integer.
            iv = segment_index.to_bytes(16, byteorder="big")

        cipher = AES.new(self._encryption_key, AES.MODE_CBC, iv)
        return cipher.decrypt(data)


def parse_manifest(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    cookies: str = "",
    referrer: str = "",
) -> StreamInfo:
    """Convenience function to parse a manifest URL."""
    parser = ManifestParser(headers=headers, cookies=cookies, referrer=referrer)
    return parser.parse(url)
