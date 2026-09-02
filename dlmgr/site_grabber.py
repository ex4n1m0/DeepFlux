"""Website grabber: keyword search on any video site → video list → downloads.

Fully generic — no per-site code. Given a site (any URL on it) and some
keywords, the grabber:

1. **Discovers the site's search URL.** A GET search form on the site's
   page (``<form action><input name="q|s|search|query|keyword">``) wins;
   otherwise common patterns are probed (``/search?q=``, ``/search/{q}``,
   ``/?s=``, ... — with the site's language prefix such as ``/en/`` when
   its URLs carry one). A pattern counts as working when the results page
   contains video cards. The working pattern is remembered per host in
   config (``browser.grabber_search_templates``) so discovery runs once.
   Users can also type a pattern themselves (``{query}`` placeholder,
   optional ``{page}``).
2. **Parses video cards** heuristically: anchors on the same site whose
   content includes a thumbnail ``<img>``; anchors sharing an ``href`` are
   merged into one card (sites often wrap thumbnail, duration badge and
   title in separate links). Title from the image ``alt`` / link text,
   thumbnail from ``src``/``data-src``, duration from a ``h:mm:ss`` badge.
   Links to taxonomy pages (tags, categories, performers, ...) are skipped.
3. **Resolves each video page** with the generic extractor: the page is
   fetched and scanned for the stream it embeds (see
   ``extractors/page_scan.py``), and the page becomes the Referer.

Pure Python (no Qt) so it is usable from the GUI, the CLI, or tests.
"""
from __future__ import annotations

import html as html_mod
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urljoin, urlparse, urlsplit, urlunsplit

from . import http_client
from .extractors.generic import GenericExtractor, NoStreamFound
from .http_client import BROWSER_HEADERS

logger = logging.getLogger(__name__)


class GrabberError(RuntimeError):
    """Search/resolve failure with a user-presentable message."""


@dataclass
class GrabberVideo:
    """One search-result video, ready to list and resolve."""

    title: str
    url: str  # video page URL
    code: str  # slug used as the filename base, e.g. "abc-123"
    thumbnail: str = ""
    duration: str = ""
    site: str = ""  # host the video was found on


# Search-URL patterns tried when the site has no discoverable search form.
# ``{lang}`` is filled from the site's URL language prefix (e.g. "/en/");
# patterns with it are skipped when the site has none.
SEARCH_PATTERNS: Tuple[str, ...] = (
    "{origin}/{lang}/search/{query}",
    "{origin}/{lang}/search?q={query}",
    "{origin}/search?q={query}",
    "{origin}/search/{query}",
    "{origin}/?s={query}",
    "{origin}/search?query={query}",
    "{origin}/search?search={query}",
    "{origin}/videos?search={query}",
    "{origin}/{lang}/?s={query}",
)

# Path words that mark taxonomy/navigation pages rather than videos.
_SKIP_PATH_WORDS = {
    "search", "tag", "tags", "category", "categories", "genre", "genres",
    "actress", "actresses", "actor", "actors", "model", "models", "star",
    "stars", "pornstar", "pornstars", "maker", "makers", "label", "labels",
    "studio", "studios", "channel", "channels", "playlist", "playlists",
    "user", "users", "profile", "login", "register", "signup", "page",
    "pages", "upload", "uploads", "about", "contact", "terms", "privacy",
    "dmca", "faq", "help", "language", "languages", "new", "trending",
    "popular", "top", "random", "live", "premium", "vip", "series",
    "collection", "collections", "sitemap", "feed", "rss",
}
_LANG_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.I)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.S)
_IMG_RE = re.compile(r"<img\b([^>]*)>", re.I | re.S)
_DURATION_RE = re.compile(r"(?<![\d:])(\d{1,2}:)?\d{1,2}:\d{2}(?![\d:])")
_TAG_RE = re.compile(r"<[^>]+>")
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
_INPUT_RE = re.compile(r"<input\b([^>]*)>", re.I | re.S)
_SEARCH_INPUT_NAMES = ("q", "s", "search", "query", "keyword", "keywords", "term", "k")
_THUMB_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-lazy", "data-thumb", "src")


def _attrs(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw or ""):
        key = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else (
            match.group(3) if match.group(3) is not None else match.group(4))
        out.setdefault(key, html_mod.unescape(value or ""))
    return out


def _same_site(host: str, site_host: str) -> bool:
    host, site_host = host.lower(), site_host.lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    host, site_host = strip(host), strip(site_host)
    return host == site_host or host.endswith("." + site_host) or site_host.endswith("." + host)


def _clean_text(raw: str) -> str:
    return html_mod.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", raw or ""))).strip()


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return slug[:80]


class SiteGrabber:
    """Keyword search + stream resolution for any video site."""

    def __init__(self, templates: Optional[Dict[str, str]] = None) -> None:
        # host -> working search-URL template (persisted by the GUI).
        self.templates: Dict[str, str] = dict(templates or {})
        self._extractor = GenericExtractor()

    # ------------------------------------------------------------ site url
    @staticmethod
    def normalize_site(site: str) -> str:
        """Any URL (or bare host) on the site → its origin, e.g. https://host."""
        value = (site or "").strip()
        if not value:
            raise GrabberError("Enter the site's address (e.g. https://example.com).")
        if "://" not in value:
            value = "https://" + value
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise GrabberError(f"Not a web address: {site}")
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    @staticmethod
    def _lang_prefix(url: str) -> str:
        first = urlparse(url).path.strip("/").split("/", 1)[0]
        return first if first and _LANG_RE.match(first) else ""

    # ------------------------------------------------------------ discovery
    @staticmethod
    def fill_template(template: str, query: str, page: int = 1) -> str:
        url = template.replace("{query}", quote((query or "").strip()))
        page = max(1, int(page or 1))
        if "{page}" in url:
            return url.replace("{page}", str(page))
        if page > 1:
            url += ("&" if "?" in url else "?") + f"page={page}"
        return url

    def _fetch(self, url: str, referer: str = ""):
        headers = dict(BROWSER_HEADERS)
        if referer:
            headers["Referer"] = referer
        return http_client.get(url, headers=headers, timeout=25)

    def _form_templates(self, page_html: str, page_url: str) -> List[str]:
        """Search-URL templates derived from GET search forms on the page."""
        templates: List[str] = []
        for form_match in _FORM_RE.finditer(page_html or ""):
            form = _attrs(form_match.group(1))
            if form.get("method", "get").lower() != "get":
                continue
            for input_match in _INPUT_RE.finditer(form_match.group(2)):
                inp = _attrs(input_match.group(1))
                name = inp.get("name", "")
                kind = inp.get("type", "text").lower()
                if name and (kind == "search" or name.lower() in _SEARCH_INPUT_NAMES):
                    action = urljoin(page_url, form.get("action") or page_url)
                    action = action.split("#", 1)[0]
                    sep = "&" if "?" in action else "?"
                    templates.append(f"{action}{sep}{name}={{query}}")
                    break
        return templates

    def _candidate_templates(self, site_url: str) -> List[str]:
        """Ordered search templates to probe for a site."""
        origin = self.normalize_site(site_url)
        lang = self._lang_prefix(site_url)
        page_url, page_html = site_url, ""
        try:
            resp = self._fetch(site_url)
            page_html = resp.text if resp.status_code < 400 else ""
            page_url = getattr(resp, "url", None) or site_url
            lang = lang or self._lang_prefix(page_url)  # e.g. "/" redirected to "/en"
        except Exception as exc:
            logger.debug("site page fetch failed for %s: %s", site_url, exc)
        candidates = self._form_templates(page_html, page_url)
        for pattern in SEARCH_PATTERNS:
            if "{lang}" in pattern and not lang:
                continue
            candidates.append(pattern.format(origin=origin, lang=lang, query="{query}"))
        seen, ordered = set(), []
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                ordered.append(candidate)
        return ordered

    # --------------------------------------------------------------- search
    def search(self, site: str, query: str, page: int = 1,
               template: str = "") -> Tuple[List[GrabberVideo], str]:
        """Search ``site`` for ``query``.

        Returns ``(videos, template_used)``; raises GrabberError on failure.
        ``template`` forces a search-URL pattern; otherwise the remembered
        pattern for the host is used, and failing that patterns are probed."""
        query = (query or "").strip()
        if not query:
            raise GrabberError("Enter some keywords to search for.")
        origin = self.normalize_site(site)
        host = urlparse(origin).hostname or ""
        forced = (template or "").strip()
        if forced and "{query}" not in forced:
            raise GrabberError("The search pattern must contain {query}.")
        candidates = [forced] if forced else (
            [self.templates[host]] if host in self.templates else self._candidate_templates(site))
        errors: List[str] = []
        for candidate in candidates:
            url = self.fill_template(candidate, query, page)
            try:
                resp = self._fetch(url, referer=origin)
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue
            if resp.status_code >= 400:
                errors.append(f"{url}: HTTP {resp.status_code}")
                continue
            videos = self.parse_cards(resp.text, getattr(resp, "url", None) or url, site_host=host)
            if videos:
                self.templates[host] = candidate
                return videos, candidate
            errors.append(f"{url}: no video cards")
        if page > 1:
            raise GrabberError("No more results.")
        if not forced and host in self.templates and len(candidates) == 1:
            # The remembered pattern stopped working: re-run discovery once
            # before giving up.
            del self.templates[host]
            return self.search(site, query, page)
        detail = errors[-1] if errors else "no search URL worked"
        raise GrabberError(
            f"No results on {host} for “{query}” ({detail}). If the site has a search "
            "page, enter its URL pattern with {query} in the search-pattern box.")

    # ---------------------------------------------------------------- cards
    def parse_cards(self, page_html: str, page_url: str, site_host: str = "") -> List[GrabberVideo]:
        """Heuristic video-card extraction from a results page."""
        site_host = site_host or (urlparse(page_url).hostname or "")
        page_path = urlparse(page_url).path.rstrip("/")
        groups: Dict[str, Dict[str, str]] = {}
        order: List[str] = []
        for match in _ANCHOR_RE.finditer(page_html or ""):
            attrs = _attrs(match.group(1))
            href = attrs.get("href", "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            url = urljoin(page_url, href).split("#", 1)[0]
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                continue
            if not _same_site(parsed.hostname, site_host):
                continue
            path = parsed.path.rstrip("/")
            if not path or path == page_path:
                continue
            segments = [seg.lower() for seg in path.strip("/").split("/")]
            if any(seg in _SKIP_PATH_WORDS for seg in segments):
                continue
            if _LANG_RE.match(segments[-1]) and len(segments) == 1:
                continue  # a bare language root like /en
            inner = match.group(2)
            group = groups.get(url)
            if group is None:
                group = groups[url] = {"thumb": "", "title": "", "duration": "", "text": "", "has_img": ""}
                order.append(url)
            for img_match in _IMG_RE.finditer(inner):
                img = _attrs(img_match.group(1))
                group["has_img"] = "1"
                if not group["title"] and len(img.get("alt", "").strip()) > 3:
                    group["title"] = img["alt"].strip()
                if not group["thumb"]:
                    for attr in _THUMB_ATTRS:
                        value = img.get(attr, "").strip()
                        if value and not value.startswith("data:"):
                            group["thumb"] = urljoin(page_url, value)
                            break
            if not group["duration"]:
                duration = _DURATION_RE.search(_clean_text(inner))
                if duration:
                    group["duration"] = duration.group(0)
            text = _clean_text(inner)
            if text and len(text) > len(group["text"]) and not _DURATION_RE.fullmatch(text):
                group["text"] = text
            if not group["title"] and len(attrs.get("title", "").strip()) > 3:
                group["title"] = attrs["title"].strip()
        videos: List[GrabberVideo] = []
        for url in order:
            group = groups[url]
            if not group["has_img"]:
                continue  # text-only links are navigation, not video cards
            parsed = urlparse(url)
            slug = _slug(parsed.path.rstrip("/").rsplit("/", 1)[-1])
            if parsed.query:
                # /watch?v=abc123 → "watch-abc123": keep per-video filenames distinct.
                pairs = parse_qsl(parsed.query, keep_blank_values=False)
                if pairs and pairs[0][1]:
                    slug = _slug(f"{slug}-{pairs[0][1]}" if slug else pairs[0][1])
            title = group["title"] or group["text"] or slug
            if not slug or (slug.isdigit() and len(slug) < 3):
                slug = _slug(title) or slug
            videos.append(GrabberVideo(
                title=title, url=url, code=slug or "video", thumbnail=group["thumb"],
                duration=group["duration"], site=parsed.hostname or site_host))
        return videos

    # -------------------------------------------------------------- resolve
    def resolve(self, url: str) -> Dict[str, str]:
        """Video page URL → ``{manifest_url, type, title, headers, page_url}``."""
        try:
            return self._extractor.extract(url, headers=dict(BROWSER_HEADERS))
        except NoStreamFound as exc:
            raise GrabberError(str(exc)) from exc
        except Exception as exc:
            raise GrabberError(f"Could not resolve {url}: {exc}") from exc
