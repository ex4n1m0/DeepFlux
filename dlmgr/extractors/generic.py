"""Generic manifest sniffer — the default extractor.

Handles any URL that directly points to a .m3u8 or .mpd file, and
also sniffs Content-Type from HTTP responses to detect HLS/DASH
manifests that don't have a recognizable file extension.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .. import http_client

from . import ExtractorBase

logger = logging.getLogger(__name__)


class GenericExtractor(ExtractorBase):
    """Generic extractor that handles direct manifest URLs."""

    name = "generic"

    def can_handle(self, url: str) -> bool:
        """Always returns True — the generic extractor is the fallback."""
        return True

    def extract(self, url: str, headers: Optional[Dict[str, str]] = None, cookies: str = "") -> Dict[str, Any]:
        """Determine the stream type from the URL or Content-Type.

        Returns a dict with manifest_url, type, and title."""
        parsed = urlparse(url)
        path = parsed.path.lower()

        # Direct manifest URLs.
        if path.endswith(".m3u8"):
            return {
                "manifest_url": url,
                "type": "hls",
                "title": self._title_from_url(url),
            }
        if path.endswith(".mpd"):
            return {
                "manifest_url": url,
                "type": "dash",
                "title": self._title_from_url(url),
            }

        # Direct video/audio file URLs.
        video_exts = (".mp4", ".webm", ".mkv", ".avi", ".mov", ".flv", ".ts", ".m4v")
        audio_exts = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac")
        if path.endswith(video_exts) or path.endswith(audio_exts):
            return {
                "manifest_url": url,
                "type": "file",
                "title": self._title_from_url(url),
            }

        # Sniff Content-Type via a HEAD request.
        req_headers = dict(headers or {})
        if cookies:
            req_headers["Cookie"] = cookies
        try:
            resp = http_client.head(url, headers=req_headers, allow_redirects=True, timeout=10)
            content_type = resp.headers.get("Content-Type", "").lower()
            if "mpegurl" in content_type or "vnd.apple.mpegurl" in content_type:
                return {"manifest_url": url, "type": "hls", "title": self._title_from_url(url)}
            if "dash+xml" in content_type or "application/dash" in content_type:
                return {"manifest_url": url, "type": "dash", "title": self._title_from_url(url)}
            if content_type.startswith("video/") or content_type.startswith("audio/"):
                return {"manifest_url": url, "type": "file", "title": self._title_from_url(url)}
        except Exception:
            pass

        # Unknown — return as a regular file download.
        return {"manifest_url": url, "type": "file", "title": self._title_from_url(url)}

    @staticmethod
    def _title_from_url(url: str) -> str:
        """Extract a clean title from a URL."""
        parsed = urlparse(url)
        name = os.path.basename(parsed.path)
        # Remove extension.
        name = os.path.splitext(name)[0]
        # Clean up common patterns.
        name = name.replace("_", " ").replace("-", " ").strip()
        return name or "download"
