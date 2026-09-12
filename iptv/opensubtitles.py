"""OpenSubtitles.com REST API v1 client for the media player.

Flow: search (GET /subtitles — by file hash for exact matches, falling back
to a title query) → download (POST /download for a short-lived link, then a
plain GET of that link). Every request carries the consumer ``Api-Key`` and
a descriptive User-Agent; providing account credentials adds a Bearer token
from /login, which raises the daily download quota.

No key ships with the app — the user registers a consumer at
opensubtitles.com and enters the key in IPTV Settings (or sets the
OPENSUBTITLES_API_KEY env var).
"""
from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "DeepFlux v3.8"
_TIMEOUT = 20


def opensubtitles_hash(path: str) -> str:
    """The classic OpenSubtitles movie hash: file size + the 64-bit LE sums
    of the first and last 64 KiB of the file. Requires ≥ 64 KiB of data."""
    filesize = os.path.getsize(path)
    if filesize < 64 * 1024:
        raise ValueError("File too small to hash (< 64 KiB)")

    def _sum64(f) -> int:
        total = 0
        for _ in range(64 * 1024 // 8):
            (chunk,) = struct.unpack("<Q", f.read(8))
            total = (total + chunk) & 0xFFFFFFFFFFFFFFFF
        return total

    with open(path, "rb") as f:
        h = filesize + _sum64(f)
        f.seek(max(0, filesize - 64 * 1024))
        h = (h + _sum64(f)) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def clean_media_query(name: str) -> str:
    """'Some.Movie.2024.1080p.mkv' → 'Some Movie 2024 1080p'."""
    base = os.path.splitext(os.path.basename(name or ""))[0]
    for sep in "._":
        base = base.replace(sep, " ")
    return " ".join(base.split())


def subtitle_dest_path(video_path: str, fallback_name: str, language: str) -> str:
    """Where to save a downloaded subtitle: next to the video (auto-loads on
    next play) for local files, else the subtitles store. Never overwrites."""
    lang = language or "und"
    if video_path and os.path.isfile(video_path):
        folder, stem = os.path.split(video_path)
        base = os.path.splitext(stem)[0]
    else:
        folder = os.path.join(os.path.expanduser("~"), ".deeptorrent", "subtitles")
        base = " ".join((fallback_name or "subtitle").split()) or "subtitle"
    candidate = os.path.join(folder, f"{base}.{lang}.srt")
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base}.{lang}.{n}.srt")
        n += 1
    return candidate


def pick_best(results: List[Dict[str, Any]], preferred_lang: str = "") -> Optional[Dict[str, Any]]:
    """Rank candidates for auto-loading: exact hash match > preferred
    language > popularity (downloads), rating as tiebreak."""
    if not results:
        return None
    pref = preferred_lang.strip().lower()

    def _score(r: Dict[str, Any]) -> float:
        s = 0.0
        if r.get("hash_match"):
            s += 1_000_000
        if pref and str(r.get("language", "")).lower().startswith(pref[:2]):
            s += 100_000
        s += min(int(r.get("downloads", 0)), 10_000)
        s += float(r.get("rating", 0.0)) * 10
        return s

    return max(results, key=_score)


class OpenSubtitlesError(Exception):
    """User-facing failure (quota exhausted, bad key, network, …)."""


@dataclass(frozen=True)
class OpenSubtitlesLoginWarning:
    """Non-fatal account-login failure; API-key-only mode may continue."""

    message: str


class OpenSubtitlesClient:
    """Search and download subtitles from OpenSubtitles.com."""

    def __init__(self, api_key: str, username: str = "", password: str = "") -> None:
        self.api_key = api_key.strip()
        self._username = username.strip()
        self._password = password
        self._token: Optional[str] = None
        self._login_attempted = False
        self._login_warning: Optional[OpenSubtitlesLoginWarning] = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def login_warning(self) -> Optional[OpenSubtitlesLoginWarning]:
        return self._login_warning

    # -- HTTP plumbing --------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        h = {"Api-Key": self.api_key, "User-Agent": USER_AGENT,
             "Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _login(self) -> Optional[OpenSubtitlesLoginWarning]:
        """Try account login, returning a typed non-fatal warning on failure."""
        if self._token or not (self._username and self._password):
            return self._login_warning
        if self._login_attempted:
            return self._login_warning
        self._login_attempted = True
        try:
            resp = requests.post(
                f"{API_BASE}/login",
                json={"username": self._username, "password": self._password},
                headers={"Api-Key": self.api_key, "User-Agent": USER_AGENT,
                         "Content-Type": "application/json"},
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                try:
                    self._token = resp.json().get("token")
                except (AttributeError, TypeError, ValueError):
                    self._token = None
                if self._token:
                    return None
                message = ("OpenSubtitles account login returned no token; "
                           "continuing with API-key-only mode.")
            elif resp.status_code in (401, 403):
                message = ("OpenSubtitles account login was rejected; check the "
                           "account username/password. Continuing with API-key-only mode.")
            else:
                message = (f"OpenSubtitles account login failed (HTTP {resp.status_code}); "
                           "continuing with API-key-only mode.")
            logger.warning("OpenSubtitles account login unavailable (HTTP %s)",
                           resp.status_code)
        except requests.RequestException:
            message = ("OpenSubtitles account login could not reach the service; "
                       "continuing with API-key-only mode.")
            # Deliberately omit exception/request details: login requests carry
            # account credentials in their body.
            logger.warning("OpenSubtitles account login request failed")
        self._login_warning = OpenSubtitlesLoginWarning(message)
        return self._login_warning

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = requests.get(f"{API_BASE}{path}", params=params,
                                headers=self._headers(), timeout=_TIMEOUT)
        except requests.RequestException as exc:
            raise OpenSubtitlesError(f"OpenSubtitles unreachable: {exc}") from exc
        if resp.status_code == 401:
            raise OpenSubtitlesError("OpenSubtitles rejected the API key — check IPTV Settings.")
        if resp.status_code == 429:
            raise OpenSubtitlesError("OpenSubtitles rate limit hit — try again later.")
        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise OpenSubtitlesError(f"OpenSubtitles error: HTTP {resp.status_code}") from exc
        return resp.json()

    # -- search ---------------------------------------------------------------
    def search(self, query: str = "", file_path: str = "",
               languages: str = "en", limit: int = 20) -> List[Dict[str, Any]]:
        """Search subtitles. A local file is matched by hash first (exact),
        falling back to the title query when the hash has no hits."""
        if not self.available:
            raise OpenSubtitlesError(
                "No OpenSubtitles API key — add one in Config → IPTV Settings.")

        results: List[Dict[str, Any]] = []
        if file_path and os.path.isfile(file_path):
            try:
                results = self._search({
                    "moviehash": opensubtitles_hash(file_path),
                    "moviebytesize": os.path.getsize(file_path),
                    "languages": languages,
                }, limit)
                for r in results:
                    r["hash_match"] = True  # exact file match — sync guaranteed
            except (ValueError, OSError) as exc:
                logger.debug("hash search skipped: %s", exc)
        if not results and query:
            results = self._search({"query": query, "languages": languages}, limit)
        return results

    def _search(self, params: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
        data = self._get("/subtitles", params).get("data", []) or []
        out: List[Dict[str, Any]] = []
        for item in data[:limit]:
            attr = item.get("attributes", {}) or {}
            files = attr.get("files") or []
            if not files:
                continue
            feature = attr.get("feature_details", {}) or {}
            out.append({
                "file_id": files[0].get("file_id"),
                "release": attr.get("release", "") or files[0].get("file_name", ""),
                "language": attr.get("language", ""),
                "downloads": attr.get("download_count", 0),
                "rating": attr.get("ratings", 0.0),
                "title": feature.get("title", ""),
                "year": feature.get("year", ""),
                "hearing_impaired": bool(attr.get("hearing_impaired")),
            })
        return out

    # -- download -------------------------------------------------------------
    def download(self, file_id: int, dest_path: str) -> str:
        """Fetch subtitle file_id into dest_path; returns dest_path."""
        self._login()  # optional — raises the daily quota when credentials exist
        try:
            resp = requests.post(f"{API_BASE}/download", json={"file_id": file_id},
                                 headers={**self._headers(), "Content-Type": "application/json"},
                                 timeout=_TIMEOUT)
        except requests.RequestException as exc:
            raise OpenSubtitlesError(f"OpenSubtitles unreachable: {exc}") from exc
        if resp.status_code == 406:
            raise OpenSubtitlesError(
                "Download quota exhausted — add your OpenSubtitles account "
                "credentials in IPTV Settings for a higher daily limit, or try tomorrow.")
        if resp.status_code != 200:
            raise OpenSubtitlesError(f"Download request failed: HTTP {resp.status_code}")
        link = resp.json().get("link")
        if not link:
            raise OpenSubtitlesError("Download request returned no link.")
        try:
            file_resp = requests.get(link, headers={"User-Agent": USER_AGENT},
                                     timeout=_TIMEOUT)
            file_resp.raise_for_status()
        except requests.RequestException as exc:
            raise OpenSubtitlesError(f"Subtitle fetch failed: {exc}") from exc
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(file_resp.content)
        logger.info("OpenSubtitles: saved %s (%d bytes)", dest_path, len(file_resp.content))
        return dest_path
