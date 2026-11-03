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
import re
import sys
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

logger = logging.getLogger(__name__)

# Family names — used in QSS / HTML stacks ahead of the old fallbacks.
UI_FONT = "Inter"
MONO_FONT = "JetBrains Mono"

# CSS stacks to reference from stylesheets (bundled name first, then the
# previous platform fallbacks so a missing font file degrades gracefully).
# The CJK families cover ui_language="zh": Inter/JetBrains Mono carry no
# Chinese glyphs, and naming the fallbacks keeps per-glyph substitution
# deliberate (Microsoft YaHei on Windows, PingFang on macOS) instead of
# relying on whatever Qt's font merge picks.
UI_STACK = (
    f"'{UI_FONT}', 'Segoe UI', 'Helvetica Neue', "
    "'Microsoft YaHei', 'PingFang SC', 'Noto Sans SC', sans-serif"
)
MONO_STACK = f"'{MONO_FONT}', 'Cascadia Code', 'Cascadia Mono', Consolas, monospace"

# Preferred per-glyph fallback order for the application font (see UI_STACK).
UI_FALLBACK_FAMILIES = (UI_FONT, "Microsoft YaHei", "PingFang SC", "Noto Sans SC")

_FONT_FILES = ("Inter.ttf", "JetBrainsMono.ttf")

# UI point size: 13pt is the everywhere base (user request 2026-09-10,
# after 15pt read too big); the stylesheet "big" tiers (20-21px ≈ 15pt)
# carry headings and prominent labels, and anything that inherits this
# font — menus included — renders at 13pt.
_UI_POINT_SIZE = 13

# UI font size setting (Settings → Font Size): one multiplier for the app
# font point size AND every stylesheet font-size (via scale_qss). Display
# scaling differs per machine, so what reads fine on one screen is too
# big/small on another; small/large are ±~15%. "medium" is scale 1.0 —
# byte-identical to the pre-setting look.
FONT_SCALES = {"small": 0.85, "medium": 1.0, "large": 1.15}
_font_scale = 1.0

_FONT_SIZE_RE = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)\s*(px|pt)")


def set_font_scale(mode: str) -> None:
    """Set the process-wide font scale from the config's ui_font_size.

    Called ONCE at boot (gui.main_window.run_gui) BEFORE load_app_fonts —
    QSS font sizes are baked at setStyleSheet time, so a later change can
    only take effect next launch. Unknown values fail closed to medium.
    """
    global _font_scale
    _font_scale = FONT_SCALES.get((mode or "").strip().lower(), 1.0)


def font_scale() -> float:
    return _font_scale


def ui_point_size() -> int:
    """The application font's point size at the current scale (11/13/15pt)."""
    return max(9, round(_UI_POINT_SIZE * _font_scale))


def scale_qss(css: str) -> str:
    """Scale every `font-size: Npx`/`Npt` in a QSS/HTML-CSS string to the
    current font scale. Stylesheets pin absolute sizes that would otherwise
    NOT follow the app font — every setStyleSheet/setHtml string that sets
    font-size must go through this. Border widths, paddings and radii are
    untouched.
    """
    if _font_scale == 1.0:
        return css

    def _scaled(m: "re.Match[str]") -> str:
        value = float(m.group(1)) * _font_scale
        unit = m.group(2)
        if unit == "px":
            value = max(1.0, round(value))
            text = f"{value:.0f}"
        else:
            value = max(1.0, round(value, 1))
            text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"font-size: {text}{unit}"

    return _FONT_SIZE_RE.sub(_scaled, css)


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

    app_font = QFont(UI_FONT, ui_point_size())
    app_font.setFamilies(list(UI_FALLBACK_FAMILIES))
    app.setFont(app_font)
    logger.info("UI font: %s %dpt (registered families: %s)", UI_FONT, ui_point_size(), ", ".join(loaded))
    return True
