"""Brand-art resolvers shared by the Agent tab and the mini player.

Deliberately Qt-free and dependency-free (stdlib ``os``/``sys`` only) so
the mini player can resolve its background without importing the heavy
``gui.main_window`` chain — same trick as ``iptv/player.py`` staying
Qt-light for the fast boot path (FAST_VIDEO_OPEN_STUDY.md, 7.8).
"""
from __future__ import annotations

import os
import sys


def _candidate_pairs(relative: str) -> list:
    """Dev path + frozen (_MEIPASS) path for a repo-root asset."""
    return [
        os.path.join(os.path.dirname(__file__), "..", relative),
        os.path.join(sys._MEIPASS, relative) if hasattr(sys, "_MEIPASS") else "",
    ]


def resolve_banner_path() -> str:
    """Find the wordmark banner for the Agent tab / mini player background
    (owner art, 2026-11-07: DeepFluxNovBanner.webp — the chrome wordmark with
    lens flare + purple swoosh; succeeded the 2026-10-13 neon banner). The
    website hero banner and a square icon stay as fallbacks so the surfaces
    keep working if the art is ever removed."""
    candidates = [
        *_candidate_pairs("DeepFluxNovBanner.webp"),
        *_candidate_pairs("DeepFluxNeonBanner.webp"),
        *_candidate_pairs(os.path.join("website", "deepflux", "DeepFluxNov2026.webp")),
        *_candidate_pairs("DeepFlux.png"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return ""


def resolve_logo_path() -> str:
    """Find the full-size logo PNG (prefers high-res)."""
    candidates = [
        *_candidate_pairs("DeepFlux.png"),
        *_candidate_pairs(os.path.join("packaging", "logo_48.png")),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return ""
