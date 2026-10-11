"""First-run seeding for installs that have no installer (Linux).

The Windows build seeds a sample IPTV playlist through the Inno installer
(``ssPostInstall`` — see packaging/installer.iss); the seed is MINIMAL by
rule (only real overrides, never a copy of defaults). Linux AppImages have
no installer step, so the same seed is applied on the first boot of a
frozen Linux build when no config file existed at all.

The values mirror the installer's seed EXACTLY (same fixed source id, so a
future Windows→Linux settings import stays idempotent). Dev/source runs
never seed — the flag is ``sys.frozen``.
"""
from __future__ import annotations

import logging
import sys

from config import DeeptorrentConfig, IPTVSourceConfig

logger = logging.getLogger(__name__)

# iptv-org's Macau country playlist: 7 free-to-air channels, #EXTM3U-verified
# (pinned by tests/test_installer_seed.py on the Windows side).
SAMPLE_SOURCE = IPTVSourceConfig(
    id="macau-iptv-org",
    name="Macau",
    kind="m3u_url",
    url="https://iptv-org.github.io/iptv/countries/mo.m3u",
    enabled=True,
)


def maybe_seed_fresh_install(config: DeeptorrentConfig, config_path: str,
                             config_existed: bool) -> bool:
    """Seed the sample playlist on a fresh frozen-Linux install.

    Returns True when the config was written. Gated on: frozen build,
    Linux, the config file did not exist before this launch, and no IPTV
    sources are configured (paranoia — a preserved/back-imported config
    must never be touched)."""
    if not getattr(sys, "frozen", False) or sys.platform != "linux":
        return False
    if config_existed or config.iptv.sources:
        return False
    config.iptv.sources = [SAMPLE_SOURCE]
    try:
        config.to_file(config_path)
    except Exception as exc:  # a failed seed must never block startup
        logger.warning("first-run seed failed: %s", exc)
        return False
    logger.info("seeded sample playlist into fresh install config")
    return True
