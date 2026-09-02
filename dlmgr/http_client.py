"""Browser-impersonating HTTP client for the download manager.

Cloudflare-fronted CDNs (e.g. surrit.com) reject plain ``requests`` traffic
by TLS fingerprint (403 or connection reset) even with correct headers.
``curl_cffi`` impersonates Chrome's TLS/HTTP2 fingerprint, which these CDNs
accept. Falls back to plain ``requests`` if curl_cffi is unavailable.

Both wrappers return a ``requests``-compatible Response object.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
from typing import Dict, Optional
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

try:
    from curl_cffi import requests as _curl_requests
    _HAS_CURL_CFFI = True
except ImportError:
    _curl_requests = None
    _HAS_CURL_CFFI = False
    logger.info("curl_cffi not installed — downloads use plain requests TLS fingerprint")

# Chrome impersonation profile. "chrome" tracks the latest known-good alias.
_IMPERSONATE = "chrome"

# Request headers that match the impersonated browser — for fetching web
# pages (search results, video pages) rather than media segments.
BROWSER_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Thread-local sessions for connection reuse (keep-alive). Reusing the same
# TLS connection for consecutive segment downloads avoids fresh handshakes —
# each handshake is a chance for middleboxes to inject a TCP reset.
_thread_local = threading.local()


def _session():
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = _curl_requests.Session(impersonate=_IMPERSONATE)
        _thread_local.session = s
    return s


def _sanitize_headers(headers: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """Align the User-Agent with the impersonated Chrome fingerprint.

    The browser extension forwards the built-in browser's UA, which contains
    a "QtWebEngine/x.y.z" token. Cloudflare compares the UA against the TLS
    fingerprint and rejects the mismatch with 403 — strip the token so the UA
    is indistinguishable from real Chrome."""
    if not headers:
        return headers
    h = dict(headers)
    for key in ("User-Agent", "user-agent"):
        ua = h.get(key)
        if ua and "QtWebEngine" in ua:
            h[key] = re.sub(r"\s*QtWebEngine/[\d.]+", "", ua)
    return h


def validate_public_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise requests.exceptions.InvalidURL("Only public HTTP(S) URLs are accepted")
    if parsed.username or parsed.password:
        raise requests.exceptions.InvalidURL("URLs containing credentials are not accepted")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise requests.exceptions.InvalidURL("Local/private URLs are not accepted")
    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0].split("%", 1)[0])
                for item in socket.getaddrinfo(
                    hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except (OSError, ValueError) as exc:
            raise requests.exceptions.InvalidURL(f"Could not resolve URL host: {hostname}") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise requests.exceptions.InvalidURL("Local/private URLs are not accepted")
    return parsed.geturl()


def _request_once(method: str, url: str, headers, timeout, stream: bool):
    if _HAS_CURL_CFFI:
        try:
            request = getattr(_session(), method.lower())
            return request(url, headers=headers, timeout=timeout, stream=stream, allow_redirects=False)
        except Exception as exc:
            # Fingerprint failures (unsupported target, etc.) fall back to
            # plain requests; HTTP errors (403/404) are returned as-is.
            if getattr(exc, "response", None) is not None:
                raise
            logger.debug("curl_cffi %s failed (%s) — retrying with requests", method, exc)
    return requests.request(
        method, url, headers=headers, timeout=timeout, stream=stream, allow_redirects=False)


def _request(method: str, url: str, headers, timeout, stream: bool, allow_redirects: bool):
    current = url
    for _ in range(7):
        current = validate_public_url(current)
        response = _request_once(method, current, headers, timeout, stream)
        if not allow_redirects or response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location", "")
        response.close()
        if not location:
            raise requests.exceptions.InvalidURL("Redirect response did not include a destination")
        current = urljoin(current, location)
    raise requests.exceptions.TooManyRedirects("Too many redirects")


def get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout=15,
    stream: bool = False,
    allow_redirects: bool = True,
):
    """GET with a Chrome TLS fingerprint when curl_cffi is available."""
    return _request("GET", url, _sanitize_headers(headers), timeout, stream, allow_redirects)


def head(url: str, headers: Optional[Dict[str, str]] = None, timeout=15, allow_redirects: bool = True):
    """HEAD with a Chrome TLS fingerprint when curl_cffi is available."""
    return _request("HEAD", url, _sanitize_headers(headers), timeout, False, allow_redirects)
