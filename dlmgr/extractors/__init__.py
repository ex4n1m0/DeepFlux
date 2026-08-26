"""Extractor plugin system for site-specific stream URL extraction.

Extractors are loaded from this package and from the user's
``~/.deeptorrent/extractors/`` directory, allowing hot-swappable
site-specific logic without a full app reinstall.

Each extractor implements:
    name: str
    can_handle(url: str) -> bool
    extract(url, headers, cookies) -> dict  # {manifest_url, type, title}
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ExtractorBase:
    """Base class for site-specific extractors."""

    name: str = "base"

    def can_handle(self, url: str) -> bool:
        """Return True if this extractor can handle the given URL."""
        return False

    def extract(self, url: str, headers: Optional[Dict[str, str]] = None, cookies: str = "") -> Dict[str, Any]:
        """Extract the manifest/video URL from a page URL.

        Returns a dict with keys:
            manifest_url: str — the direct .m3u8 or .mpd URL
            type: str — "hls", "dash", or "file"
            title: str — suggested filename (without extension)
        """
        return {}


class ExtractorRegistry:
    """Manages extractor plugins. Loads built-in and user extractors.

    User extractors are Python files placed in ``~/.deeptorrent/extractors/``.
    They are loaded at startup and can be hot-reloaded."""

    def __init__(self, user_extractors_dir: str = "") -> None:
        self._extractors: List[ExtractorBase] = []
        self._user_dir = user_extractors_dir or os.path.join(os.path.expanduser("~"), ".deeptorrent", "extractors")
        self._load_builtin()
        self._load_user()

    def _load_builtin(self) -> None:
        """Load built-in extractors from this package."""
        try:
            from .generic import GenericExtractor
            self._extractors.append(GenericExtractor())
        except Exception as exc:
            logger.warning("Failed to load generic extractor: %s", exc)

    def _load_user(self) -> None:
        """Load user extractors from the user directory."""
        if not os.path.isdir(self._user_dir):
            return
        sys.path.insert(0, self._user_dir)
        try:
            for filename in os.listdir(self._user_dir):
                if filename.endswith(".py") and not filename.startswith("_"):
                    modname = filename[:-3]
                    try:
                        mod = importlib.import_module(modname)
                        # Look for an Extractor class.
                        for attr in dir(mod):
                            obj = getattr(mod, attr)
                            if (isinstance(obj, type) and issubclass(obj, ExtractorBase)
                                    and obj is not ExtractorBase):
                                instance = obj()
                                self._extractors.append(instance)
                                logger.info("Loaded user extractor: %s", instance.name)
                    except Exception as exc:
                        logger.warning("Failed to load user extractor %s: %s", filename, exc)
        finally:
            # Don't leave the user dir on the global import path.
            try:
                sys.path.remove(self._user_dir)
            except ValueError:
                pass

    def reload(self) -> None:
        """Reload all extractors (hot-reload)."""
        self._extractors.clear()
        self._load_builtin()
        self._load_user()
        logger.info("Extractors reloaded: %d loaded", len(self._extractors))

    def extract(self, url: str, headers: Optional[Dict[str, str]] = None, cookies: str = "") -> Dict[str, Any]:
        """Try each extractor in order. Returns the first match's result.

        Falls back to the generic extractor if no site-specific one matches."""
        for ext in self._extractors:
            try:
                if ext.can_handle(url):
                    result = ext.extract(url, headers=headers, cookies=cookies)
                    if result and result.get("manifest_url"):
                        logger.info("Extractor '%s' matched %s", ext.name, url)
                        return result
            except Exception as exc:
                logger.warning("Extractor %s error: %s", ext.name, exc)

        # Fallback: return the URL as-is (generic extractor handles this).
        return {"manifest_url": url, "type": "file", "title": ""}

    @property
    def count(self) -> int:
        return len(self._extractors)
