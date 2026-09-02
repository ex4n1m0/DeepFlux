"""MissAV watch page → surrit.com HLS manifest extractor.

MissAV serves videos as HLS from the surrit.com CDN:

    https://surrit.com/{uuid}/playlist.m3u8         (master playlist)
    https://surrit.com/{uuid}/{quality}/video.m3u8  (per-quality sub-playlist)

The stream UUID is embedded in an obfuscated ``<script>`` tag on the watch
page, 36 characters ending two characters before the word ``seek`` — the
same pattern the in-browser video grabber uses
(``dlmgr/browser_extension.py::extractMissAVUuid``).

Verified 2026-09: surrit.com is Cloudflare-fronted and rejects non-browser
TLS fingerprints (handled by ``dlmgr.http_client``'s curl_cffi Chrome
impersonation), but playlists and segments need nothing beyond a
browser-like Referer — no cookies, no ``#EXT-X-TOKEN`` forwarding.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .. import http_client
from . import ExtractorBase

logger = logging.getLogger(__name__)

MISSAV_DOMAINS = (
    "missav.ws", "missav.ai", "missav.live", "missav.fans",
    "missav.media", "missav123.com", "missav01.com",
)

SURRIT_CDN = "https://surrit.com"

# Browser-like headers (curl_cffi already impersonates Chrome's TLS stack).
BROWSER_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_UUID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.I)
_TITLE_SUFFIX = " - missav"


class MissAVExtractor(ExtractorBase):
    """Resolves a MissAV watch-page URL to its surrit.com master playlist."""

    name = "missav"

    def can_handle(self, url: str) -> bool:
        host = (urlparse(url or "").hostname or "").lower().rstrip(".")
        return any(host == domain or host.endswith("." + domain)
                   for domain in MISSAV_DOMAINS)

    def extract(self, url: str, headers: Optional[Dict[str, str]] = None,
                cookies: str = "") -> Dict[str, Any]:
        merged = dict(BROWSER_HEADERS)
        merged.update(headers or {})
        if cookies:
            merged["Cookie"] = cookies
        resp = http_client.get(url, headers=merged, timeout=25)
        resp.raise_for_status()
        uuid = self.find_stream_uuid(resp.text)
        if not uuid:
            raise ValueError(
                "No stream UUID found on the page — the site layout may have changed")
        return {
            "manifest_url": f"{SURRIT_CDN}/{uuid}/playlist.m3u8",
            "type": "hls",
            "title": self._page_title(resp.text, url),
            # surrit requires a browser-like Referer on playlist/segment hits.
            "headers": {
                "Referer": url,
                "User-Agent": BROWSER_HEADERS["User-Agent"],
            },
        }

    @staticmethod
    def find_stream_uuid(html: str) -> str:
        """First UUID ending two characters before ``seek`` in the page.

        The watch page embeds the UUID in an obfuscated player script;
        scanning every ``seek`` occurrence mirrors the browser grabber's
        ``extractMissAVUuid`` strategy."""
        for match in re.finditer(r"seek", html or ""):
            start = match.start() - 38
            if start < 0:
                continue
            candidate = html[start:match.start() - 2]
            if _UUID_RE.match(candidate):
                return candidate.lower()
        return ""

    @staticmethod
    def _page_title(html: str, url: str) -> str:
        for pattern in (r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"):
            match = re.search(pattern, html or "", re.S | re.I)
            if not match:
                continue
            title = re.sub(r"<[^>]+>", "", match.group(1))
            title = re.sub(r"\s+", " ", title).strip()
            if title.lower().endswith(_TITLE_SUFFIX):
                title = title[: -len(_TITLE_SUFFIX)].rstrip()
            if title:
                return title
        code = urlparse(url).path.rsplit("/", 1)[-1]
        return code or "missav video"
