"""Browser-impersonating HTTP client for the download manager.

Cloudflare-fronted CDNs (e.g. surrit.com) reject plain ``requests`` traffic
by TLS fingerprint (403 or connection reset) even with correct headers.
``curl_cffi`` impersonates Chrome's TLS/HTTP2 fingerprint, which these CDNs
accept. Falls back to plain ``requests`` if curl_cffi is unavailable.

Both wrappers return a ``requests``-compatible Response object.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Dict, Optional

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


def get(url: str, headers: Optional[Dict[str, str]] = None, timeout=15, stream: bool = False):
    """GET with a Chrome TLS fingerprint when curl_cffi is available."""
    headers = _sanitize_headers(headers)
    if _HAS_CURL_CFFI:
        try:
            return _session().get(url, headers=headers, timeout=timeout, stream=stream)
        except Exception as exc:
            # Fingerprint failures (unsupported target, etc.) fall back to
            # plain requests; HTTP errors (403/404) are returned as-is.
            if getattr(exc, "response", None) is not None:
                raise
            logger.debug("curl_cffi GET failed (%s) — retrying with requests", exc)
    return requests.get(url, headers=headers, timeout=timeout, stream=stream)


def head(url: str, headers: Optional[Dict[str, str]] = None, timeout=15, allow_redirects: bool = True):
    """HEAD with a Chrome TLS fingerprint when curl_cffi is available."""
    headers = _sanitize_headers(headers)
    if _HAS_CURL_CFFI:
        try:
            return _curl_requests.head(
                url, headers=headers, timeout=timeout,
                allow_redirects=allow_redirects, impersonate=_IMPERSONATE,
            )
        except Exception as exc:
            if getattr(exc, "response", None) is not None:
                raise
            logger.debug("curl_cffi HEAD failed (%s) — retrying with requests", exc)
    return requests.head(url, headers=headers, timeout=timeout, allow_redirects=allow_redirects)
