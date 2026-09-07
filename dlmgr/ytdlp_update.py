"""yt-dlp freshness check.

YouTube changes its player/signing machinery every few weeks; an outdated
yt-dlp starts downloads fine and then gets cut off mid-transfer with HTTP
403 from the googlevideo CDN (verified live 2026-09: 2026.07.04 died at 8%,
2026.08.19 downloaded the same video cleanly). The frozen app bundles
whatever yt-dlp was present at build time, so this can ONLY be reported,
not fixed in place: the notice tells frozen users to update DeepFlux and
source users to `pip install --upgrade yt-dlp`.

Qt-free — same shape as `infra/jackett.maybe_auto_sync`: a daily-gated
check safe to call at startup from a background thread.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Dict, Optional, Tuple

from config import DeeptorrentConfig

logger = logging.getLogger(__name__)

# PyPI JSON API — the canonical "latest yt-dlp release" source.
PYPI_JSON_URL = "https://pypi.org/pypi/yt-dlp/json"

CHECK_INTERVAL_SECONDS = 24 * 3600  # at most one probe per day


def installed_version() -> str:
    """Version of the yt-dlp package actually imported by the app."""
    try:
        from yt_dlp.version import __version__ as version
        return str(version)
    except Exception:
        return ""


def _version_tuple(version: str) -> Optional[Tuple[int, ...]]:
    """Parse a CalVer yt-dlp version ("2026.8.19") to a comparable tuple.

    Returns None for anything unparseable — callers treat that as "not
    outdated" so a malformed version string never produces a false alarm.
    """
    parts = version.strip().split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def is_outdated(current: str, latest: str) -> bool:
    """True when `current` sorts before `latest` (padded to equal length)."""
    a, b = _version_tuple(current), _version_tuple(latest)
    if not a or not b:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) < b + (0,) * (width - len(b))


def fetch_latest_version(timeout: float = 10.0) -> str:
    """Latest yt-dlp version from PyPI. Raises on network/API failure."""
    from .http_client import get

    response = get(PYPI_JSON_URL, timeout=timeout)
    response.raise_for_status()
    version = response.json()["info"]["version"]
    return str(version)


def is_frozen_build() -> bool:
    """True in the PyInstaller bundle (updating yt-dlp means a new app)."""
    return bool(getattr(sys, "frozen", False))


def maybe_check_update(
    config: DeeptorrentConfig,
    config_path: Optional[str] = None,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """Daily-gated freshness probe, safe to call at startup.

    Returns None when there is nothing to report (disabled, not due, yt-dlp
    missing, network failure, or already current) and otherwise
    {"installed", "latest", "outdated", "frozen"} — always with outdated=True;
    an up-to-date result stays silent. The daily gate is only persisted
    after a successful probe, so a flaky network retries next launch.
    """
    if not config.download.youtube_update_check:
        return None
    if not force and time.time() - (config.download.ytdlp_last_check or 0.0) < CHECK_INTERVAL_SECONDS:
        return None
    current = installed_version()
    if not current:
        return None
    try:
        latest = fetch_latest_version()
    except Exception as exc:
        logger.debug("yt-dlp update check failed: %s", exc)
        return None
    config.download.ytdlp_last_check = time.time()
    if config_path:
        try:
            config.to_file(config_path)
        except Exception as exc:
            logger.warning("Could not persist yt-dlp check timestamp: %s", exc)
    if not is_outdated(current, latest):
        return None
    logger.info("yt-dlp %s is outdated (latest %s)", current, latest)
    return {"installed": current, "latest": latest, "outdated": True,
            "frozen": is_frozen_build()}
