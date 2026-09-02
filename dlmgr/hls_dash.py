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
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
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
class HLSSegmentMetadata:
    """Encryption and boundary metadata for one HLS media segment."""
    url: str = ""
    media_sequence: int = 0
    duration: float = 0.0
    encryption_method: str = "NONE"
    encryption_key_url: str = ""
    encryption_iv: str = ""
    discontinuity: bool = False
    init_segment_url: str = ""


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
    media_sequence: int = 0
    duration_seconds: float = 0.0
    error: str = ""
    selected_rendition: Optional[StreamRendition] = None
    segment_metadata: List[HLSSegmentMetadata] = field(default_factory=list)
    segment_key_urls: List[str] = field(default_factory=list)
    segment_ivs: List[str] = field(default_factory=list)
    segment_init_urls: List[str] = field(default_factory=list)
    discontinuity_indices: List[int] = field(default_factory=list)
    audio_rendition: Optional[StreamRendition] = None
    audio_segment_urls: List[str] = field(default_factory=list)
    audio_init_segment_url: str = ""


class DRMError(Exception):
    """Raised when a stream is DRM-protected and cannot be downloaded."""
    pass


class ManifestParser:
    """Parses HLS and DASH manifests into a StreamInfo object."""

    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        cookies: str = "",
        referrer: str = "",
        rendition_selector: Optional[
            Callable[[List[StreamRendition]], Union[StreamRendition, int, str, None]]
        ] = None,
    ) -> None:
        self._headers = _apply_default_headers(headers, cookies, referrer)
        self._rendition_selector = rendition_selector

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

        # If this is a master playlist (has variants), select a rendition.
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

            try:
                selected = self.select_rendition(info.renditions)
            except Exception as exc:
                info.error = f"Failed to select HLS rendition: {exc}"
                return info
            info.selected_rendition = selected
            logger.info("HLS master playlist: %d renditions, picked %dbps %s",
                        len(info.renditions), selected.bandwidth, selected.resolution)

            try:
                sub_content = self._fetch_text(selected.url)
                playlist = m3u8.loads(sub_content, uri=selected.url)
                info.manifest_url = selected.url
            except Exception as exc:
                info.error = f"Failed to fetch rendition playlist: {exc}"
                return info

        media_url = info.manifest_url
        info.is_live = not playlist.is_endlist
        info.media_sequence = int(playlist.media_sequence or 0)
        info.duration_seconds = sum(float(segment.duration or 0) for segment in playlist.segments)

        for index, segment in enumerate(playlist.segments):
            seg_url = segment.absolute_uri or urljoin(media_url, segment.uri)
            key = getattr(segment, "key", None)
            method = str(getattr(key, "method", "NONE") or "NONE").upper()
            if method == "SAMPLE-AES":
                info.is_drm_protected = True
                info.error = "DRM-protected stream (SAMPLE-AES). Cannot download."
                return info
            if method not in ("NONE", "AES-128"):
                info.is_drm_protected = True
                info.error = f"Unsupported encryption: {method}. Cannot download."
                return info

            key_url = ""
            iv = ""
            if method == "AES-128":
                key_uri = getattr(key, "uri", "") or ""
                key_url = (
                    getattr(key, "absolute_uri", None) or urljoin(media_url, key_uri)
                    if key_uri else ""
                )
                iv = getattr(key, "iv", None) or ""
                if not key_url:
                    info.error = f"HLS segment {index} uses AES-128 without a key URI."
                    return info
                info.is_encrypted = True
                if not info.encryption_key_url:
                    info.encryption_key_url = key_url
                    info.encryption_iv = iv
                    info.encryption_method = method

            init_section = getattr(segment, "init_section", None)
            init_url = ""
            if init_section is not None and getattr(init_section, "uri", None):
                init_url = getattr(init_section, "absolute_uri", None) or urljoin(
                    media_url, init_section.uri
                )
            metadata = HLSSegmentMetadata(
                url=seg_url,
                media_sequence=info.media_sequence + index,
                duration=float(segment.duration or 0),
                encryption_method=method,
                encryption_key_url=key_url,
                encryption_iv=iv,
                discontinuity=bool(getattr(segment, "discontinuity", False)),
                init_segment_url=init_url,
            )
            info.segment_urls.append(seg_url)
            info.segment_metadata.append(metadata)
            info.segment_key_urls.append(key_url)
            info.segment_ivs.append(iv)
            info.segment_init_urls.append(init_url)
            if metadata.discontinuity:
                info.discontinuity_indices.append(index)
            if init_url and not info.init_segment_url:
                info.init_segment_url = init_url

        # Older m3u8 releases expose only the playlist-level map.
        segment_map = getattr(playlist, "segment_map", None)
        if not info.init_segment_url and segment_map is not None:
            if isinstance(segment_map, (list, tuple)):
                segment_map = segment_map[0] if segment_map else None
            if segment_map is not None and getattr(segment_map, "uri", None):
                info.init_segment_url = getattr(segment_map, "absolute_uri", None) or urljoin(
                    media_url, segment_map.uri
                )

        if not info.encryption_method:
            info.encryption_method = "NONE"
        info.total_segments = len(info.segment_urls)
        if info.is_live:
            info.error = "Live HLS capture is not supported; an EXT-X-ENDLIST VOD playlist is required."
        logger.info("HLS playlist: %d segments, live=%s, encrypted=%s",
                     info.total_segments, info.is_live, info.is_encrypted)
        return info

    def select_rendition(self, renditions: List[StreamRendition]) -> StreamRendition:
        """Select an HLS master rendition, allowing callers or subclasses to override it."""
        if not renditions:
            raise ValueError("master playlist contains no renditions")
        if self._rendition_selector is None:
            return max(renditions, key=lambda rendition: rendition.bandwidth)

        selected = self._rendition_selector(list(renditions))
        if isinstance(selected, int):
            try:
                return renditions[selected]
            except IndexError as exc:
                raise ValueError(f"rendition index {selected} is out of range") from exc
        if isinstance(selected, str):
            for rendition in renditions:
                if rendition.url == selected:
                    return rendition
            raise ValueError("rendition selector returned an unknown URL")
        if isinstance(selected, StreamRendition) and selected in renditions:
            return selected
        raise ValueError("rendition selector did not return a listed rendition, index, or URL")

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
        info.duration_seconds = self._parse_iso_duration(root.get("mediaPresentationDuration", ""))

        # Check if live (dynamic MPD).
        mpd_type = root.get("type", "static").lower()
        info.is_live = mpd_type == "dynamic"
        if info.is_live:
            info.error = "Dynamic/live DASH capture is not supported; a static MPD is required."
            return info

        # Check for ContentProtection (DRM).
        drm_markers = (
            "widevine", "fairplay", "playready",
            "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
            "9a04f079-9840-4286-ab92-e65be0885f95",
        )
        for cp in root.iter():
            if self._local_name(cp.tag) != "ContentProtection":
                continue
            scheme = cp.get("schemeIdUri", "").lower()
            if any(marker in scheme for marker in drm_markers):
                info.is_drm_protected = True
                info.error = f"DRM-protected stream ({scheme}). Cannot download."
                return info

        root_base = self._dash_element_base(manifest_url, root)
        video_candidates = []
        audio_candidates = []
        for period in self._children(root, "Period"):
            period_base = self._dash_element_base(root_base, period)
            for aset in self._children(period, "AdaptationSet"):
                aset_base = self._dash_element_base(period_base, aset)
                for rep in self._children(aset, "Representation"):
                    rep_base = self._dash_element_base(aset_base, rep)
                    kind = self._dash_content_type(aset, rep)
                    try:
                        bandwidth = int(rep.get("bandwidth", "0") or 0)
                    except ValueError:
                        bandwidth = 0
                    candidate = (bandwidth, period, aset, rep, rep_base)
                    if kind == "audio":
                        audio_candidates.append(candidate)
                    elif kind == "video":
                        video_candidates.append(candidate)

        if not video_candidates:
            info.error = "No video representation found in MPD"
            return info

        best_bandwidth, period, best_aset, best_rep, best_base = max(
            video_candidates, key=lambda candidate: candidate[0]
        )
        rend = self._dash_rendition(best_rep, best_bandwidth, best_base)
        info.renditions.append(rend)
        info.selected_rendition = rend

        period_duration = self._parse_iso_duration(period.get("duration", "")) or info.duration_seconds
        try:
            info.segment_urls, info.init_segment_url = self._build_dash_segments(
                best_base, best_aset, best_rep, period_duration, period=period
            )
            if audio_candidates:
                audio_bw, audio_period, audio_aset, audio_rep, audio_base = max(
                    audio_candidates, key=lambda candidate: candidate[0]
                )
                info.audio_rendition = self._dash_rendition(audio_rep, audio_bw, audio_base)
                audio_duration = (
                    self._parse_iso_duration(audio_period.get("duration", ""))
                    or info.duration_seconds
                )
                info.audio_segment_urls, info.audio_init_segment_url = self._build_dash_segments(
                    audio_base, audio_aset, audio_rep, audio_duration, period=audio_period
                )
        except ValueError as exc:
            info.error = f"Unsupported DASH segment layout: {exc}"
            return info

        if not info.segment_urls:
            info.error = "No finite video segments found in static MPD"
            return info
        info.total_segments = len(info.segment_urls)
        logger.info("DASH manifest: %d video segments, %d audio segments, init=%s",
                    info.total_segments, len(info.audio_segment_urls), bool(info.init_segment_url))
        return info

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    @classmethod
    def _children(cls, element, name: str) -> List[Any]:
        return [child for child in list(element) if cls._local_name(child.tag) == name]

    @classmethod
    def _first_child(cls, element, name: str):
        for child in list(element):
            if cls._local_name(child.tag) == name:
                return child
        return None

    @staticmethod
    def _parse_iso_duration(value: str) -> float:
        match = re.fullmatch(
            r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?",
            value or "",
        )
        if not match:
            return 0.0
        return (
            float(match.group(1) or 0) * 3600
            + float(match.group(2) or 0) * 60
            + float(match.group(3) or 0)
        )

    @classmethod
    def _dash_element_base(cls, parent_base: str, element) -> str:
        base = cls._first_child(element, "BaseURL")
        if base is None or not (base.text or "").strip():
            return parent_base
        return urljoin(parent_base, (base.text or "").strip())

    @staticmethod
    def _dash_content_type(aset, rep) -> str:
        content_type = (rep.get("contentType") or aset.get("contentType") or "").lower()
        mime_type = (rep.get("mimeType") or aset.get("mimeType") or "").lower()
        if content_type in ("video", "audio"):
            return content_type
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("audio/"):
            return "audio"
        if rep.get("height") or rep.get("width"):
            return "video"
        codecs = (rep.get("codecs") or aset.get("codecs") or "").lower()
        if codecs.startswith(("mp4a", "ac-3", "ec-3", "opus", "vorbis")):
            return "audio"
        return "video"

    @staticmethod
    def _dash_rendition(rep, bandwidth: int, url: str) -> StreamRendition:
        def _integer(name: str) -> int:
            try:
                return int(rep.get(name, "0") or 0)
            except ValueError:
                return 0

        width = _integer("width")
        height = _integer("height")
        return StreamRendition(
            bandwidth=bandwidth,
            codecs=rep.get("codecs", ""),
            url=url,
            height=height,
            width=width,
            resolution=f"{width}x{height}" if width and height else "",
        )

    @staticmethod
    def _dash_substitute(template: str, rep_id: str, bandwidth: str,
                         number: Optional[int] = None, segment_time: Optional[int] = None) -> str:
        sentinel = "\0DOLLAR\0"
        value = template.replace("$$", sentinel)
        values = {
            "RepresentationID": rep_id,
            "Bandwidth": bandwidth,
            "Number": number,
            "Time": segment_time,
        }
        token_re = re.compile(r"\$(RepresentationID|Bandwidth|Number|Time)(%0(\d+)d)?\$")

        def _replace(match: re.Match[str]) -> str:
            token = values[match.group(1)]
            if token is None:
                return match.group(0)
            width = match.group(3)
            if width and isinstance(token, int):
                return f"{token:0{int(width)}d}"
            return str(token)

        return token_re.sub(_replace, value).replace(sentinel, "$")

    def _build_dash_segments(self, base_url: str, aset, rep,
                             duration_seconds: float = 0.0, period=None) -> Tuple[List[str], str]:
        """Build a finite list of segment URLs from DASH SegmentTemplate or SegmentList."""
        segments: List[str] = []
        init_url = ""
        period_template = self._first_child(period, "SegmentTemplate") if period is not None else None
        aset_template = self._first_child(aset, "SegmentTemplate")
        rep_template = self._first_child(rep, "SegmentTemplate")
        template = rep_template if rep_template is not None else aset_template
        if template is None:
            template = period_template

        if template is not None:
            attributes: Dict[str, str] = {}
            if period_template is not None:
                attributes.update(period_template.attrib)
            if aset_template is not None:
                attributes.update(aset_template.attrib)
            if rep_template is not None:
                attributes.update(rep_template.attrib)
            media_template = attributes.get("media", "")
            init_template = attributes.get("initialization", "")
            if not media_template:
                raise ValueError("SegmentTemplate has no media template")
            rep_id = rep.get("id", "")
            bandwidth = rep.get("bandwidth", "")
            if init_template:
                init_url = urljoin(
                    base_url, self._dash_substitute(init_template, rep_id, bandwidth)
                )

            timeline = self._first_child(rep_template, "SegmentTimeline") if rep_template is not None else None
            if timeline is None and aset_template is not None:
                timeline = self._first_child(aset_template, "SegmentTimeline")
            if timeline is None and period_template is not None:
                timeline = self._first_child(period_template, "SegmentTimeline")
            start_number = int(attributes.get("startNumber", "1") or 1)
            timescale = int(attributes.get("timescale", "1") or 1)
            if timescale <= 0:
                raise ValueError("SegmentTemplate timescale must be positive")

            if timeline is not None:
                entries = self._children(timeline, "S")
                seg_num = start_number
                next_time: Optional[int] = None
                presentation_offset = int(attributes.get("presentationTimeOffset", "0") or 0)
                end_time = presentation_offset + int(duration_seconds * timescale)
                for entry_index, entry in enumerate(entries):
                    duration = int(entry.get("d", "0") or 0)
                    if duration <= 0:
                        raise ValueError("SegmentTimeline entry has no positive duration")
                    if entry.get("t") is not None:
                        next_time = int(entry.get("t", "0"))
                    elif next_time is None:
                        next_time = 0
                    repeat = int(entry.get("r", "0") or 0)
                    if repeat < -1:
                        raise ValueError("SegmentTimeline repeat is less than -1")
                    if repeat == -1:
                        following_t = None
                        for following in entries[entry_index + 1:]:
                            if following.get("t") is not None:
                                following_t = int(following.get("t", "0"))
                                break
                        repeat_end = following_t if following_t is not None else end_time
                        if repeat_end <= next_time:
                            raise ValueError("open-ended SegmentTimeline repeat has no finite duration")
                        count = (repeat_end - next_time + duration - 1) // duration
                    else:
                        count = repeat + 1
                    if count > 1_000_000:
                        raise ValueError("SegmentTimeline expands to too many segments")
                    for _ in range(count):
                        if not media_template:
                            raise ValueError("SegmentTemplate has no media template")
                        media = self._dash_substitute(
                            media_template, rep_id, bandwidth, seg_num, next_time
                        )
                        segments.append(urljoin(base_url, media))
                        seg_num += 1
                        next_time += duration
            else:
                duration = int(attributes.get("duration", "0") or 0)
                if duration > 0 and duration_seconds > 0:
                    segment_count = max(
                        1, int((duration_seconds * timescale + duration - 1) // duration)
                    )
                    if segment_count > 1_000_000:
                        raise ValueError("SegmentTemplate expands to too many segments")
                    for offset in range(segment_count):
                        number = start_number + offset
                        media = self._dash_substitute(
                            media_template, rep_id, bandwidth, number, offset * duration
                        )
                        segments.append(urljoin(base_url, media))
                elif media_template:
                    raise ValueError("duration-bounded SegmentTemplate is required")

        # Fallback: SegmentList.
        if not segments:
            seg_list = self._first_child(rep, "SegmentList")
            if seg_list is None:
                seg_list = self._first_child(aset, "SegmentList")
            if seg_list is None and period is not None:
                seg_list = self._first_child(period, "SegmentList")
            if seg_list is not None:
                initialization = self._first_child(seg_list, "Initialization")
                if initialization is not None and initialization.get("sourceURL"):
                    init_url = urljoin(base_url, initialization.get("sourceURL", ""))
                for surl in self._children(seg_list, "SegmentURL"):
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
        self._key_cache: Dict[str, bytes] = {}
        self._key_lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._accounting_lock = threading.Lock()
        self._progress = 0
        self._bytes_downloaded = 0  # real byte total, for speed reporting
        self._total = max(1, len(stream_info.segment_urls))

    @property
    def progress(self) -> float:
        with self._accounting_lock:
            return self._progress / self._total

    @property
    def bytes_downloaded(self) -> int:
        with self._accounting_lock:
            return self._bytes_downloaded

    @bytes_downloaded.setter
    def bytes_downloaded(self, value: int) -> None:
        with self._accounting_lock:
            self._bytes_downloaded = int(value)

    def stop(self) -> None:
        self._stop_flag.set()

    def download(self) -> List[str]:
        """Download all segments. Returns a list of segment file paths in order.

        Raises DRMError if the stream is DRM-protected."""
        if self._info.is_drm_protected:
            raise DRMError(self._info.error or "DRM-protected stream")
        if self._info.is_live:
            raise RuntimeError("Live stream capture is not supported")
        if self._info.error:
            raise RuntimeError(self._info.error)

        os.makedirs(self._temp_dir, exist_ok=True)

        # Download init segment if present (fMP4/DASH).
        if self._info.init_segment_url:
            init_path = os.path.join(self._temp_dir, "init.mp4")
            init_index = next(
                (
                    index for index, metadata in enumerate(
                        getattr(self._info, "segment_metadata", [])
                    )
                    if metadata.init_segment_url == self._info.init_segment_url
                ),
                0,
            )
            self._download_segment(self._info.init_segment_url, init_path, init_index)

        # Download all segments in parallel.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        segment_paths: Dict[int, str] = {}

        def _download_one(idx: int, url: str) -> Tuple[int, str]:
            ext = os.path.splitext(urlparse(url).path)[1] or ".ts"
            path = os.path.join(self._temp_dir, f"seg_{idx:05d}{ext}")
            # Restart support: segments from a previous (paused) run are
            # complete files — skip them instead of re-downloading.
            if os.path.isfile(path) and os.path.getsize(path) > 0:
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
                    with self._accounting_lock:
                        self._progress += 1
                        completed = self._progress
                    if self._on_progress:
                        try:
                            self._on_progress(completed)
                        except Exception:
                            logger.exception("HLS progress callback failed")
                except Exception as exc:
                    logger.warning("Segment download failed: %s", exc)
                    failed.append((futures[future], str(exc)[:120]))

        if failed:
            sample = "; ".join(f"seg {i}: {e}" for i, e in failed[:3])
            raise RuntimeError(
                f"{len(failed)}/{len(self._info.segment_urls)} segments failed "
                f"after {MAX_RETRIES} retries ({sample})"
            )

        # Return segments in order.
        ordered = [segment_paths[i] for i in range(len(self._info.segment_urls)) if i in segment_paths]
        return ordered

    def _download_segment(self, url: str, path: str, index: int,
                          decrypt: bool = True) -> None:
        """Download a single segment with retries. Decrypts if needed."""
        for attempt in range(MAX_RETRIES):
            if self._stop_flag.is_set():
                return
            try:
                resp = http_client.get(url, headers=self._headers, timeout=SEGMENT_TIMEOUT)
                resp.raise_for_status()
                data = resp.content
                downloaded_size = len(data)

                if decrypt and self._segment_encryption_method(index) == "AES-128":
                    data = self._decrypt_aes128(data, index)

                with open(path, "wb") as f:
                    f.write(data)
                with self._accounting_lock:
                    self._bytes_downloaded += downloaded_size
                return
            except Exception as exc:
                if attempt < MAX_RETRIES - 1:
                    logger.debug("Segment %d retry %d: %s", index, attempt + 1, exc)
                    time.sleep(RETRY_BACKOFF * (attempt + 1) * 0.5)
                else:
                    raise

    def _segment_metadata(self, segment_index: int) -> Optional[HLSSegmentMetadata]:
        metadata = getattr(self._info, "segment_metadata", [])
        if 0 <= segment_index < len(metadata):
            return metadata[segment_index]
        return None

    def _segment_encryption_method(self, segment_index: int) -> str:
        metadata = self._segment_metadata(segment_index)
        if metadata is not None:
            return (metadata.encryption_method or "NONE").upper()
        return (self._info.encryption_method or "NONE").upper()

    def _fetch_encryption_key(self, key_url: str) -> bytes:
        with self._key_lock:
            cached = self._key_cache.get(key_url)
            if cached is not None:
                return cached
            last_exc: Optional[Exception] = None
            for attempt in range(MANIFEST_RETRIES):
                try:
                    resp = http_client.get(key_url, headers=self._headers, timeout=15)
                    resp.raise_for_status()
                    key = resp.content
                    if len(key) != 16:
                        raise ValueError(f"AES-128 key must be 16 bytes, got {len(key)}")
                    self._key_cache[key_url] = key
                    if self._encryption_key is None:
                        self._encryption_key = key
                    logger.info("Fetched HLS encryption key from %s", key_url)
                    return key
                except Exception as exc:
                    last_exc = exc
                    if attempt < MANIFEST_RETRIES - 1:
                        time.sleep(RETRY_BACKOFF * (attempt + 1) * 0.5)
            raise RuntimeError(f"Failed to fetch encryption key {key_url}: {last_exc}")

    def _decrypt_aes128(self, data: bytes, segment_index: int) -> bytes:
        """Decrypt an AES-128-CBC HLS segment with its active key and IV."""
        from Crypto.Cipher import AES

        metadata = self._segment_metadata(segment_index)
        key_url = (
            metadata.encryption_key_url if metadata is not None
            else self._info.encryption_key_url
        )
        key = self._fetch_encryption_key(key_url) if key_url else self._encryption_key
        if key is None:
            raise RuntimeError(f"No AES-128 key is available for segment {segment_index}")

        iv_text = metadata.encryption_iv if metadata is not None else self._info.encryption_iv
        if iv_text:
            normalized_iv = iv_text[2:] if iv_text.lower().startswith("0x") else iv_text
            try:
                iv = bytes.fromhex(normalized_iv)
            except ValueError as exc:
                raise ValueError(f"Invalid AES-128 IV for segment {segment_index}") from exc
            if len(iv) != 16:
                raise ValueError(f"AES-128 IV must be 16 bytes, got {len(iv)}")
        else:
            sequence = (
                metadata.media_sequence if metadata is not None
                else self._info.media_sequence + segment_index
            )
            iv = sequence.to_bytes(16, byteorder="big")

        if len(data) % AES.block_size:
            raise ValueError("AES-128 encrypted segment length is not a multiple of 16 bytes")
        plaintext = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
        padding = plaintext[-1] if plaintext else 0
        if 0 < padding <= AES.block_size and plaintext.endswith(bytes([padding]) * padding):
            plaintext = plaintext[:-padding]
        return plaintext


def parse_manifest(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    cookies: str = "",
    referrer: str = "",
    rendition_selector: Optional[
        Callable[[List[StreamRendition]], Union[StreamRendition, int, str, None]]
    ] = None,
) -> StreamInfo:
    """Convenience function to parse a manifest URL."""
    parser = ManifestParser(
        headers=headers,
        cookies=cookies,
        referrer=referrer,
        rendition_selector=rendition_selector,
    )
    return parser.parse(url)
