"""Bundled UI fonts — registered at startup so layout metrics are identical
on every machine.

Without this, widgets fall back to the OS default GUI font (whose family,
size, and DPI behavior vary across machines), which is why dialog buttons and
chat bubbles occasionally render text that doesn't fit its allotted space.

Fonts live in ``packaging/fonts/`` (bundled into the frozen app via
``app.spec``) and are both SIL OFL licensed (see the *-OFL.txt files):

  - Inter            — UI sans-serif (replaces the Segoe UI fallback chain)
  - JetBrains Mono   — monospace for code/hashes/magnet links
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

logger = logging.getLogger(__name__)

# Family names — used in QSS / HTML stacks ahead of the old fallbacks.
UI_FONT = "Inter"
MONO_FONT = "JetBrains Mono"

# CSS stacks to reference from stylesheets (bundled name first, then the
# previous platform fallbacks so a missing font file degrades gracefully).
UI_STACK = f"'{UI_FONT}', 'Segoe UI', 'Helvetica Neue', sans-serif"
MONO_STACK = f"'{MONO_FONT}', 'Cascadia Code', 'Cascadia Mono', Consolas, monospace"

_FONT_FILES = ("Inter.ttf", "JetBrainsMono.ttf")

# UI point size chosen to match Segoe UI 9pt (the classic Windows default)
# in x-height and average advance width.
_UI_POINT_SIZE = 10


def _fonts_dir() -> Path:
    """packaging/fonts — works frozen (PyInstaller onedir) and from source."""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "packaging" / "fonts"
    return Path(__file__).resolve().parent.parent / "packaging" / "fonts"


def load_app_fonts(app) -> bool:
    """Register the bundled fonts and set the application-wide default font.

    Returns True when the bundled UI font became the app font. On any failure
    the previous platform default is kept, so the app still works.
    """
    fonts_dir = _fonts_dir()
    loaded = []
    for fname in _FONT_FILES:
        path = fonts_dir / fname
        if not path.is_file():
            logger.warning("Bundled font missing: %s", path)
            continue
        fid = QFontDatabase.addApplicationFont(str(path))
        if fid < 0:
            logger.warning("Failed to register font: %s", path)
            continue
        loaded.extend(QFontDatabase.applicationFontFamilies(fid))

    if UI_FONT not in loaded:
        logger.warning("Bundled UI font %r unavailable — keeping system default font", UI_FONT)
        return False

    app.setFont(QFont(UI_FONT, _UI_POINT_SIZE))
    logger.info("UI font: %s %dpt (registered families: %s)", UI_FONT, _UI_POINT_SIZE, ", ".join(loaded))
    return True
