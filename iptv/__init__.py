"""IPTV subsystem for DeepFlux.

A self-contained IPTV client: M3U/Xtream playlist loading, categorization,
internet-enriched artwork/metadata, EPG, and an embedded media player
(libmpv, with a libVLC fallback) behind a small backend abstraction.

Public entry points:
    - :class:`iptv.manager.IPTVManager` — orchestrates sources, parsing,
      favorites/recent, and background work.
    - :class:`iptv.player.PlayerBackend` — playback engine abstraction.
"""
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
