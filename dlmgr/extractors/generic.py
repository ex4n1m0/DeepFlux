"""Generic extractor — the default (and, for most sites, the only) extractor.

Handles, in order:

1. URLs that directly point to a ``.m3u8``/``.mpd`` manifest or a media
   file (by extension),
2. URLs whose Content-Type says manifest/media even without an extension,
3. **web pages**: the HTML is fetched and scanned for the stream it embeds
   (player configs, ``<video>``/``<source>`` tags, and scripts obfuscated
   with the common p,a,c,k,e,d packer — see ``page_scan``). The page URL
   is returned as the Referer for the stream, which is what most CDNs
   require.

No site names or CDN hostnames live here: everything is pattern-based, so
the same code resolves a video page on any site that embeds its stream in
the page.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .. import http_client
from ..http_client import BROWSER_HEADERS

from . import ExtractorBase
from .page_scan import find_stream

logger = logging.getLogger(__name__)

VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".avi", ".mov", ".flv", ".ts", ".m4v")
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac")


class NoStreamFound(ValueError):
    """The URL is a web page, and no stream could be found in it."""


class GenericExtractor(ExtractorBase):
    """Pattern-based extractor: direct media URLs, sniffed manifests, and
    streams embedded in web pages."""

    name = "generic"

    def can_handle(self, url: str) -> bool:
        """Always returns True — the generic extractor is the fallback."""
        return True

    def extract(self, url: str, headers: Optional[Dict[str, str]] = None, cookies: str = "") -> Dict[str, Any]:
        """Determine the stream for ``url``.

        Returns a dict with ``manifest_url``, ``type`` ("hls" | "dash" |
        "file"), ``title`` and — for streams found inside a page —
        ``headers`` (Referer + User-Agent) plus ``page_url``."""
        parsed = urlparse(url)
        path = parsed.path.lower()

        # Direct manifest URLs.
        if path.endswith(".m3u8"):
            return {"manifest_url": url, "type": "hls", "title": self._title_from_url(url)}
        if path.endswith(".mpd"):
            return {"manifest_url": url, "type": "dash", "title": self._title_from_url(url)}

        # Direct video/audio file URLs.
        if path.endswith(VIDEO_EXTS) or path.endswith(AUDIO_EXTS):
            return {"manifest_url": url, "type": "file", "title": self._title_from_url(url)}

        # Sniff Content-Type via a HEAD request.
        req_headers = dict(BROWSER_HEADERS)
        req_headers.update(headers or {})
        if cookies:
            req_headers["Cookie"] = cookies
        content_type = ""
        try:
            resp = http_client.head(url, headers=req_headers, allow_redirects=True, timeout=10)
            content_type = resp.headers.get("Content-Type", "").lower()
            if "mpegurl" in content_type:
                return {"manifest_url": url, "type": "hls", "title": self._title_from_url(url)}
            if "dash+xml" in content_type or "application/dash" in content_type:
                return {"manifest_url": url, "type": "dash", "title": self._title_from_url(url)}
            if content_type.startswith(("video/", "audio/")):
                return {"manifest_url": url, "type": "file", "title": self._title_from_url(url)}
        except Exception:
            pass  # some hosts reject HEAD — fall through to the page scan

        # A web page: look for the stream it embeds.
        if not content_type or "html" in content_type or "text/" in content_type:
            return self.extract_from_page(url, req_headers)

        # Unknown binary — treat as a regular file download.
        return {"manifest_url": url, "type": "file", "title": self._title_from_url(url)}

    def extract_from_page(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Fetch ``url`` as a web page and return the best stream it embeds.

        Raises ``NoStreamFound`` when the page has no recognisable stream."""
        req_headers = dict(BROWSER_HEADERS)
        req_headers.update(headers or {})
        resp = http_client.get(url, headers=req_headers, timeout=25)
        resp.raise_for_status()
        page_url = getattr(resp, "url", None) or url
        found = find_stream(resp.text, page_url)
        if not found:
            raise NoStreamFound(f"No video stream found on {url}")
        found["page_url"] = page_url
        found["headers"] = {
            "Referer": page_url,
            "User-Agent": req_headers.get("User-Agent", BROWSER_HEADERS["User-Agent"]),
        }
        logger.info("Page scan: %s -> %s (%s)", url, found["manifest_url"], found["type"])
        return found

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
