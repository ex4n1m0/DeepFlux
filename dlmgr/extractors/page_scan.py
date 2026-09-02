"""Generic stream discovery inside a web page's HTML.

Video pages rarely link their stream directly: the manifest URL usually
sits inside a player configuration script, frequently obfuscated with the
ubiquitous "p,a,c,k,e,d" JavaScript packer (Dean Edwards' packer). This
module finds stream URLs the generic way — no per-site knowledge:

1. scan the raw HTML (JSON-escaped ``\\/`` and ``\\u002F`` normalised),
2. unpack every ``eval(function(p,a,c,k,e,d){...})`` block (recursively —
   packers are sometimes nested) and scan the unpacked source too,
3. pick up ``<video>``/``<source>``/``<audio>`` ``src`` attributes,
4. rank: HLS master playlists first, then any HLS, DASH, direct files;
   previews/trailers/samples are demoted.

Everything here is pure string processing; callers do the HTTP.
"""
from __future__ import annotations

import html as html_mod
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

# --- p,a,c,k,e,d ----------------------------------------------------------

_PACKED_RE = re.compile(
    r"eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*[dr]\s*\)"
    r".*?\}\s*\(\s*'(?P<p>(?:[^'\\]|\\.)*)'\s*,\s*(?P<a>\d+)\s*,\s*(?P<c>\d+)\s*,\s*"
    r"'(?P<k>(?:[^'\\]|\\.)*)'\s*\.split\s*\(\s*'\|'\s*\)",
    re.S,
)
_JS_ESCAPE_RE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
_WORD_RE = re.compile(r"\b\w+\b")
_MAX_UNPACK_DEPTH = 4
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _js_unescape(text: str) -> str:
    def repl(match: "re.Match[str]") -> str:
        esc = match.group(1)
        if esc[0] == "u" and len(esc) == 5:
            return chr(int(esc[1:], 16))
        if esc[0] == "x" and len(esc) == 3:
            return chr(int(esc[1:], 16))
        return _SIMPLE_ESCAPES.get(esc, esc)

    return _JS_ESCAPE_RE.sub(repl, text)


def _decode_word(word: str, radix: int) -> Optional[int]:
    """Packer keys: 0-9, a-z (10-35) and, above radix 36, A-Z (36-61)."""
    value = 0
    for ch in word:
        if ch.isdigit():
            digit = ord(ch) - 48
        elif "a" <= ch <= "z":
            digit = ord(ch) - 87
        elif "A" <= ch <= "Z":
            digit = ord(ch) - 29
        else:
            return None
        if digit >= radix:
            return None
        value = value * radix + digit
    return value


def unpack_packed_js(source: str, depth: int = 0) -> List[str]:
    """Return the unpacked source of every p,a,c,k,e,d block in ``source``.

    Nested packers are unpacked recursively; blocks that fail to decode are
    skipped so one odd script never hides another."""
    results: List[str] = []
    if depth >= _MAX_UNPACK_DEPTH:
        return results
    for match in _PACKED_RE.finditer(source):
        try:
            payload = _js_unescape(match.group("p"))
            radix = int(match.group("a"))
            keywords = _js_unescape(match.group("k")).split("|")
        except (ValueError, IndexError):
            continue
        if not 2 <= radix <= 62:
            continue

        def repl(word_match: "re.Match[str]") -> str:
            word = word_match.group(0)
            index = _decode_word(word, radix)
            if index is None or index >= len(keywords) or not keywords[index]:
                return word
            return keywords[index]

        unpacked = _WORD_RE.sub(repl, payload)
        results.append(unpacked)
        results.extend(unpack_packed_js(unpacked, depth + 1))
    return results


# --- stream discovery ------------------------------------------------------

_MEDIA_EXTS = ("m3u8", "mpd", "mp4", "webm", "mkv", "mov", "m4v")
_STREAM_URL_RE = re.compile(
    r"""https?://[^\s"'<>\\()\[\]{}]+?\.(?:%s)(?:\?[^\s"'<>\\()\[\]{}]*)?""" % "|".join(_MEDIA_EXTS),
    re.I,
)
_MEDIA_SRC_RE = re.compile(
    r"""<(?:video|source|audio)\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I | re.S)
_OG_TITLE_RE = re.compile(
    r"""<meta\s+[^>]*property\s*=\s*["']og:title["'][^>]*content\s*=\s*["']([^"']+)["']""", re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SITE_SUFFIX_RE = re.compile(r"\s+[-|–—]\s+(?=[^-|–—]{2,40}$)")
_MASTER_HINTS = ("master", "playlist", "index")
_DEMOTE_HINTS = ("preview", "trailer", "thumb", "sample", "teaser")


def _normalise(text: str) -> str:
    return text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")


def _clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    return html_mod.unescape(re.sub(r"\s+", " ", text)).strip()


def _stream_type(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    return "file"


def _rank(url: str) -> tuple:
    kind = _stream_type(url)
    path = urlparse(url).path.lower()
    name = path.rsplit("/", 1)[-1]
    demoted = 1 if any(hint in path for hint in _DEMOTE_HINTS) else 0
    if kind == "hls":
        # Master playlists beat variant playlists; among equals, the shorter
        # path is usually the one closer to the "root" of the stream.
        return (demoted, 0, 0 if any(hint in name for hint in _MASTER_HINTS) else 1, len(path))
    if kind == "dash":
        return (demoted, 1, 0, len(path))
    return (demoted, 2, 0, len(path))


def find_stream_urls(html: str, page_url: str = "") -> List[str]:
    """Every stream-looking URL on the page, best candidate first, deduped."""
    found: List[str] = []
    seen = set()

    def add(candidate: str) -> None:
        candidate = html_mod.unescape(candidate.strip())
        if not candidate or candidate.startswith(("data:", "blob:", "javascript:")):
            return
        if not candidate.lower().startswith(("http://", "https://")):
            if not page_url:
                return
            candidate = urljoin(page_url, candidate)
        if candidate not in seen:
            seen.add(candidate)
            found.append(candidate)

    flat = _normalise(html or "")
    for url in _STREAM_URL_RE.findall(flat):
        add(url)
    for unpacked in unpack_packed_js(flat):
        for url in _STREAM_URL_RE.findall(_normalise(unpacked)):
            add(url)
    for src in _MEDIA_SRC_RE.findall(flat):
        add(src)
    found.sort(key=_rank)
    return found


def page_title(html: str, page_url: str = "") -> str:
    """Best-effort human title: og:title, then <h1>, then <title> (with a
    trailing " - Site Name" dropped), then the URL slug."""
    for pattern in (_OG_TITLE_RE, _H1_RE):
        match = pattern.search(html or "")
        if match:
            title = _clean_text(match.group(1))
            if title:
                return title
    match = _TITLE_RE.search(html or "")
    if match:
        title = _SITE_SUFFIX_RE.split(_clean_text(match.group(1)), maxsplit=1)[0].strip()
        if title:
            return title
    slug = urlparse(page_url).path.rstrip("/").rsplit("/", 1)[-1]
    return slug or "video"


def find_stream(html: str, page_url: str = "") -> Optional[Dict[str, str]]:
    """The best stream on the page, or None.

    Returns ``{"manifest_url", "type", "title"}``; the caller adds request
    headers (a Referer of the page is what most CDNs want)."""
    urls = find_stream_urls(html, page_url)
    if not urls:
        return None
    best = urls[0]
    return {"manifest_url": best, "type": _stream_type(best), "title": page_title(html, page_url)}
