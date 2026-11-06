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
import re
import sys
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase

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
# big/small on another; small/large are ±~15% and micro one step further
# down at −30%. "medium" is scale 1.0 — byte-identical to the pre-setting
# look. Default is "small" (owner decision 2026-11-04; existing 7.0/7.1
# configs are migrated once — see config.from_file's ui_defaults_gen).
FONT_SCALES = {"micro": 0.70, "small": 0.85, "medium": 1.0, "large": 1.15}
_font_scale = 1.0

_FONT_SIZE_RE = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)\s*(px|pt)")

# UI text color theme (Settings → Text Color): every QSS/HTML text literal in
# the app is `color: #ffffff` at the source level (owner decision 2026-11-03)
# and scale_qss rewrites it to the ACTIVE theme hex at apply time — so a
# theme rides the same baked-at-setStyleSheet restart-to-apply convention as
# the font scale. "white" is byte-identical to the pre-setting look.
# light_blue reuses the app's established #a8edff cyan; dark_blue is a royal
# blue kept just readable on the near-black background ladder (~3.7:1).
TEXT_THEMES = {
    "white": "#ffffff",
    "light_blue": "#a8edff",
    "blue": "#4da3ff",
    "dark_blue": "#2a63d6",
}
_text_theme_hex = "#ffffff"

# Bare `color:` only — the (?<![-\w]) lookbehind rejects selection-color /,
# background-color / border-color / gridline-color, which all keep their own
# values: selected text stays WHITE in every theme (dark-blue text on the
# #1a2a4a selection background would be unreadable), and backgrounds are
# never recolored by the text theme. Semantic status colors (reds/oranges/
# yellows/greens) aren't #ffffff and can't match.
_TEXT_COLOR_RE = re.compile(r"(?<![-\w])color:\s*#ffffff\b", re.IGNORECASE)

# Declaration blocks whose BACKGROUND is one of the bright fills (hover /
# selected / active / selection + the accent blue) keep their white text in
# every theme — same reasoning as selection-color: blue text on the accent
# blue would vanish. Matched against the block's background declarations
# only, so gridline-color/border-color uses of the same hexes don't protect
# a block, and selection-background-color in a base-widget block doesn't
# freeze the widget's body text. A block may also carry an explicit
# `/* keep-white */` marker for text that INHERITS a bright background from
# a parent rule (the active rail button's label).
_BRIGHT_BG_RE = re.compile(
    r"(?:^|[;{])\s*(?:alternate-)?background(?:-color)?\s*:[^;}]*"
    r"(?:#1a2a4a|#2a7abf|rgba\(\s*42\s*,\s*122\s*,\s*191)",
    re.IGNORECASE,
)


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
    """The application font's point size at the current scale (9/11/13/15pt)."""
    return max(9, round(_UI_POINT_SIZE * _font_scale))


def set_text_theme(mode: str) -> None:
    """Set the process-wide text color theme from the config's ui_text_theme.

    Called ONCE at boot next to set_font_scale — QSS/HTML text colors are
    baked at setStyleSheet time, so a later change can only take effect next
    launch. Unknown values fail closed to white.
    """
    global _text_theme_hex
    _text_theme_hex = TEXT_THEMES.get((mode or "").strip().lower(), "#ffffff")


def text_theme_hex() -> str:
    """The active text theme as a #rrggbb literal."""
    return _text_theme_hex


def text_color() -> QColor:
    """The active text theme as a QColor — for Python-PAINTED text (HUD
    captions/values, tile titles, progress-delegate digits): painted text
    must follow the same theme as QSS text, so never hardcode white there."""
    return QColor(_text_theme_hex)


def scale_qss(css: str) -> str:
    """Scale every `font-size: Npx`/`Npt` to the current font scale AND
    rewrite every bare `color: #ffffff` to the active text theme.

    Stylesheets pin absolute sizes and colors that would otherwise NOT
    follow the app font / text theme — every setStyleSheet/setHtml string
    that sets font-size or text color must go through this. Border widths,
    paddings and radii are untouched; selection-color/background-color keep
    their own values (see _TEXT_COLOR_RE).
    """
    if _font_scale == 1.0 and _text_theme_hex == "#ffffff":
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

    out = _FONT_SIZE_RE.sub(_scaled, css) if _font_scale != 1.0 else css
    if _text_theme_hex != "#ffffff":
        out = _apply_text_theme(out)
    return out


def _apply_text_theme(css: str) -> str:
    """Rewrite themeable `color: #ffffff` declarations to the active theme,
    QSS-block aware: a declaration block that sets a bright background (see
    _BRIGHT_BG_RE) or whose selector side / body carries a `keep-white`
    comment marker keeps its white text; everything else (top-level HTML
    fragments included) is themed."""
    out: list = []
    depth = 0
    keep_white = False
    prev = ""
    for part in re.split(r"([{}])", css):
        if part == "{":
            depth += 1
            keep_white = depth == 1 and "keep-white" in prev
            out.append(part)
        elif part == "}":
            depth -= 1
            out.append(part)
        elif depth > 0 and (keep_white or "keep-white" in part
                            or _BRIGHT_BG_RE.search(part)):
            out.append(part)
        else:
            out.append(_TEXT_COLOR_RE.sub(f"color: {_text_theme_hex}", part))
        prev = part
    return "".join(out)


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
