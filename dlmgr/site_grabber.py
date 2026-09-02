"""Website grabber: keyword search → video list → download queue.

Site adapters turn search keywords into a list of videos (title, watch
URL, thumbnail, duration) and resolve a watch URL into a direct stream
manifest for the download engine. Adapters are pure Python (no Qt), so
they can be used from the GUI, the CLI, or tests alike.

MissAV notes (verified 2026-09):

- Search: ``GET {base}/en/search/{quoted words}[?page=N]`` — the results
  are server-rendered cards (Alpine.js + lozad lazy thumbnails), so plain
  HTTP gets the full list; no JavaScript is required.
- Card markup order: ``@mouseenter="setPreview('<uuid>')"`` → first
  ``<a href>`` (watch URL, ``/en/{code}``) → ``data-src`` (hover-preview
  mp4) → ``data-src`` (cover jpg) → ``alt`` (title) → duration ``<span>``.
  Actress links (``/en/actresses/...``) are search hits, not videos, and
  are filtered out.
- The card's ``setPreview`` UUID is NOT the stream UUID — resolving a
  video needs one extra watch-page request (see
  ``dlmgr/extractors/missav.py``).
"""
from __future__ import annotations

import html as html_mod
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.parse import quote, urlparse

from . import http_client
from .extractors.missav import BROWSER_HEADERS, MissAVExtractor

logger = logging.getLogger(__name__)


class GrabberError(RuntimeError):
    """Search/resolve failure with a user-presentable message."""


@dataclass
class GrabberVideo:
    """One search-result video, ready to list and resolve."""

    title: str
    url: str  # watch-page URL
    code: str  # site video id, e.g. "goju-322" — used as the filename base
    thumbnail: str = ""
    duration: str = ""
    site: str = "missav"


class MissAvSite:
    """MissAV search + stream resolution (surrit.com HLS)."""

    key = "missav"
    label = "MissAV"
    base_url = "https://missav.ws"

    # One result card — see the module docstring for the field order.
    _CARD_RE = re.compile(
        r"""setPreview\('(?P<prev_uuid>[^']+)'\)
            .*?<a\s+href="(?P<url>[^"]*)"[^>]*>
            .*?data-src="(?P<preview>[^"]*)"
            .*?data-src="(?P<cover>[^"]*)"
            .*?alt="(?P<title>[^"]*)"
            .*?<span[^>]*>\s*(?P<duration>[\d:]+)\s*</span>
        """,
        re.S | re.X,
    )

    def __init__(self) -> None:
        self._extractor = MissAVExtractor()

    def build_search_url(self, query: str, page: int = 1) -> str:
        url = f"{self.base_url}/en/search/{quote((query or '').strip())}"
        if int(page or 1) > 1:
            url += f"?page={int(page)}"
        return url

    def search(self, query: str, page: int = 1) -> List[GrabberVideo]:
        """Search the site; returns videos or raises GrabberError."""
        url = self.build_search_url(query, page)
        try:
            resp = http_client.get(url, headers=dict(BROWSER_HEADERS), timeout=25)
            resp.raise_for_status()
        except Exception as exc:
            raise GrabberError(f"Search request failed: {exc}") from exc
        videos = self.parse_search(resp.text)
        if not videos:
            raise GrabberError(
                "No results (or the site layout changed — check the logs).")
        return videos

    def parse_search(self, html: str) -> List[GrabberVideo]:
        """Extract video cards from a search page's HTML."""
        videos: List[GrabberVideo] = []
        for match in self._CARD_RE.finditer(html or ""):
            url = html_mod.unescape(match.group("url") or "")
            path = urlparse(url).path
            if not path.startswith("/en/") or "/actresses/" in path:
                continue
            if path.rstrip("/").rsplit("/", 1)[-1] in ("", "en", "search"):
                continue
            code = path.rsplit("/", 1)[-1]
            title = html_mod.unescape(match.group("title") or "").strip()
            videos.append(GrabberVideo(
                title=title or code.upper(),
                url=url if url.startswith("http") else self.base_url + url,
                code=code,
                thumbnail=html_mod.unescape(match.group("cover") or ""),
                duration=(match.group("duration") or "").strip(),
                site=self.key,
            ))
        return videos

    def resolve(self, url: str) -> Dict[str, Any]:
        """Watch-page URL → {manifest_url, type, title, headers}."""
        try:
            return self._extractor.extract(url)
        except GrabberError:
            raise
        except Exception as exc:
            raise GrabberError(f"Could not resolve {url}: {exc}") from exc


# Registry of grabber sites — future adapters plug in here.
SITES: Dict[str, MissAvSite] = {"missav": MissAvSite()}
