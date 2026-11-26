"""yt-dlp freshness check.

YouTube changes its player/signing machinery every few weeks; an outdated
yt-dlp starts downloads fine and then gets cut off mid-transfer with HTTP
403 from the googlevideo CDN (verified live 2026-09: 2026.07.04 died at 8%,
2026.08.19 downloaded the same video cleanly).

In the frozen Windows and Linux builds the app can now fix this itself
(infra/ytdlp_overlay.py, 2026-11-25). The probe reads the owner-pinned
feed (deepflux.space/deepflux/latest-ytdlp.json) and reports a newer
pinned release as updatable: ask mode (the default) shows an "Update now"
link, automatic mode installs it right in the probe thread.

When the feed cannot be read, or on other builds, the probe keeps its
first behaviour: ask PyPI which release is newest, tell frozen users to
update DeepFlux and source users to `pip install --upgrade yt-dlp`.

Qt-free — same shape as `infra.jackett.maybe_auto_sync`: a daily-gated
check safe to call at startup from a background thread. The VERSION in use
is read from disk, never imported.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict, Optional, Tuple

from config import DeeptorrentConfig
from infra import ytdlp_overlay

logger = logging.getLogger(__name__)

# PyPI JSON API — the fallback "latest yt-dlp release" source (and the only
# one on builds where the app cannot update yt-dlp itself).
PYPI_JSON_URL = "https://pypi.org/pypi/yt-dlp/json"

CHECK_INTERVAL_SECONDS = 24 * 3600  # at most one probe per day


def installed_version() -> str:
    """Version of the yt-dlp actually used by the app — a downloaded release
    when one is active, else the bundled copy. Read from disk, so calling it
    imports nothing; yt-dlp is imported only when neither version file can
    be read."""
    active = ytdlp_overlay.active_version()
    if active:
        return active
    bundled = ytdlp_overlay.bundled_version()
    if bundled:
        return bundled
    try:
        return str(ytdlp_overlay.import_yt_dlp().version.__version__)
    except Exception:
        return ""


def _version_tuple(version: str) -> Optional[Tuple[int, ...]]:
    """Parse a CalVer yt-dlp version ("2026.8.19") to a comparable tuple.

    Returns None for anything unparseable — callers treat that as "not
    outdated" so a malformed version string never produces a false alarm.
    """
    return ytdlp_overlay.version_tuple(version)


def is_outdated(current: str, latest: str) -> bool:
    """True when `current` sorts before `latest` (padded to equal length)."""
    return ytdlp_overlay.is_newer(latest, current)


def fetch_latest_version(timeout: float = 10.0) -> str:
    """Latest yt-dlp version from PyPI. Raises on network/API failure."""
    from .http_client import get, read_capped

    response = get(PYPI_JSON_URL, timeout=timeout, stream=True)
    response.raise_for_status()
    # Cap the read anyway — no unbounded buffering (review 2026-11-06).
    # The yt-dlp PyPI JSON lists every release+wheel and measures 2-5 MiB,
    # so the cap must stay well above that or the probe always fails
    # (measured 2026-11-06 boot: >1 MiB).
    data = read_capped(response, 8 * 1024 * 1024)
    version = json.loads(data)["info"]["version"]
    return str(version)


def is_frozen_build() -> bool:
    """True in the PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def _persist_gate(config: DeeptorrentConfig, config_path: Optional[str]) -> None:
    if not config_path:
        return
    try:
        config.to_file(config_path)
    except Exception as exc:
        logger.warning("Could not persist yt-dlp check timestamp: %s", exc)


def maybe_check_update(
    config: DeeptorrentConfig,
    config_path: Optional[str] = None,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """Daily-gated freshness probe, safe to call at startup.

    Returns None when there is nothing to report (disabled, not due, yt-dlp
    missing, network failure, or already current) and otherwise
    {"installed", "latest", "outdated": True, "frozen", "can_update", "notice"}.
    ``can_update`` is True only when a PINNED release newer than the one in
    use can be installed by the app itself; the dict then also carries
    ``size`` and ``info`` (the YtdlpUpdateInfo). ``notice`` is False when
    the user switched the warning off but left automatic updates on — the
    probe then runs for the install only. The daily gate is only persisted
    after a successful probe, so a flaky network retries next launch.

    The probe runs on a background thread and imports yt-dlp there once it
    is past the gate: that keeps the package's first import off the GUI
    thread (the probe used to do so implicitly), and a downloaded release
    that fails to load is switched off at startup instead of at the first
    YouTube job (infra/ytdlp_overlay.py's first-import safety net).
    """
    download = config.download
    if not download.youtube_update_check and not download.ytdlp_auto_update:
        return None
    if not force and time.time() - (download.ytdlp_last_check or 0.0) < CHECK_INTERVAL_SECONDS:
        return None
    current = installed_version()
    if not current:
        return None
    try:
        ytdlp_overlay.import_yt_dlp()
    except Exception:
        logger.debug("yt-dlp import failed in the update probe", exc_info=True)
    if ytdlp_overlay.can_override():
        try:
            info = ytdlp_overlay.fetch_feed()
        except Exception as exc:
            # Feed unreachable or malformed (e.g. before it is published):
            # fall back to the PyPI probe below.
            logger.debug("yt-dlp feed check failed: %s", exc)
        else:
            download.ytdlp_last_check = time.time()
            _persist_gate(config, config_path)
            # Silent only when the release is not newer, or the USER chose
            # the bundled copy for exactly this release. A release that
            # failed on this machine still reports, so the user can see it
            # and retry by hand (security review 2026-11-26, m4).
            skipped = ytdlp_overlay.skipped()
            opted_out = bool(skipped and skipped["stage"] == "user"
                             and skipped["version"] == info.version
                             and skipped["sha256"] == info.sha256)
            if not is_outdated(current, info.version) or opted_out:
                return None
            logger.info("yt-dlp %s can be updated in-app to %s", current, info.version)
            return {"installed": current, "latest": info.version, "outdated": True,
                    "frozen": True, "can_update": True, "size": info.size, "info": info,
                    "notice": download.youtube_update_check}
    try:
        latest = fetch_latest_version()
    except Exception as exc:
        logger.debug("yt-dlp update check failed: %s", exc)
        return None
    download.ytdlp_last_check = time.time()
    _persist_gate(config, config_path)
    if not is_outdated(current, latest):
        return None
    logger.info("yt-dlp %s is outdated (latest %s)", current, latest)
    return {"installed": current, "latest": latest, "outdated": True,
            "frozen": is_frozen_build(), "can_update": False,
            "notice": download.youtube_update_check}


def check_and_maybe_update(
    config: DeeptorrentConfig,
    config_path: Optional[str] = None,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """:func:`maybe_check_update` plus the AUTOMATIC-mode install (the
    owner's opt-in, ``config.download.ytdlp_auto_update``). The outcome of
    an attempted install rides in the same dict as ``updated`` (the
    version) or ``update_error``.

    ``force`` bypasses the daily gate only — with automatic mode on, a
    forced check installs too (that is what "automatic" means); it never
    forces a release this machine recorded as skipped, so a rejected
    release is not retried behind the user's back.
    """
    result = maybe_check_update(config, config_path, force)
    info = (result or {}).get("info")
    if not info or not config.download.ytdlp_auto_update:
        return result
    if ytdlp_overlay.is_skipped(info):
        # Recorded as failed on this machine, or the user chose the bundled
        # copy: automatic mode leaves it alone (a manual "Update now" may
        # still retry it). The ask-mode notice still reports it.
        return result
    try:
        ytdlp_overlay.install(info)
    except Exception as exc:
        result["update_error"] = str(exc)
        logger.warning("automatic yt-dlp update to %s failed: %s", info.version, exc)
    else:
        result["updated"] = info.version
    return result
