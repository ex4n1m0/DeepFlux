"""Font size + text color settings (Settings → Font Size / Text Color):
config fields, the gui/fonts scale/theme engine, stylesheet wrap coverage,
the 1px-border rule, and the pure-white-text-source rule.

The scale and theme engines are process-global module state — every test
restores the defaults ("medium" / 1.0, "white") via the fixture so nothing
leaks into other tests.
"""
import ast
import json
import pathlib
import re

import pytest

from config import DeeptorrentConfig
from gui import fonts


@pytest.fixture(autouse=True)
def _default_scale():
    fonts.set_font_scale("medium")
    fonts.set_text_theme("white")
    yield
    fonts.set_font_scale("medium")
    fonts.set_text_theme("white")


# --- config field -----------------------------------------------------------

def test_config_ui_font_size_roundtrip(tmp_path):
    path = str(tmp_path / "config.json")
    assert DeeptorrentConfig.from_file(path).ui_font_size == "medium"

    for mode in ("small", "large"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ui_font_size": mode}, f)
        assert DeeptorrentConfig.from_file(path).ui_font_size == mode

    # invalid values fail closed to medium, not to a crash
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ui_font_size": "huge"}, f)
    assert DeeptorrentConfig.from_file(path).ui_font_size == "medium"

    # to_file persists the field (asdict round trip)
    DeeptorrentConfig(ui_font_size="small").to_file(path)
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["ui_font_size"] == "small"


def test_font_size_is_not_agent_settable():
    """ui_font_size is top-level (like ui_language) — the agent's settings
    whitelist only iterates section dataclasses plus two named scalars, so
    the LLM must never be able to change the UI font size."""
    from agent.tools import _iter_setting_fields
    paths = {path for path, _ in _iter_setting_fields(DeeptorrentConfig())}
    assert not any("ui_font_size" in p for p in paths)


# --- config field: text theme ----------------------------------------------

def test_config_ui_text_theme_roundtrip(tmp_path):
    path = str(tmp_path / "config.json")
    assert DeeptorrentConfig.from_file(path).ui_text_theme == "white"

    for mode in ("light_blue", "blue", "dark_blue"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ui_text_theme": mode}, f)
        assert DeeptorrentConfig.from_file(path).ui_text_theme == mode

    # invalid values fail closed to white, not to a crash
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ui_text_theme": "red"}, f)
    assert DeeptorrentConfig.from_file(path).ui_text_theme == "white"

    # to_file persists the field (asdict round trip)
    DeeptorrentConfig(ui_text_theme="dark_blue").to_file(path)
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["ui_text_theme"] == "dark_blue"


def test_text_theme_is_not_agent_settable():
    """Same structural protection as ui_font_size: top-level field, never
    reachable through the agent's settings whitelist."""
    from agent.tools import _iter_setting_fields
    paths = {path for path, _ in _iter_setting_fields(DeeptorrentConfig())}
    assert not any("ui_text_theme" in p for p in paths)


# --- scale engine -----------------------------------------------------------

def test_set_font_scale_maps_and_fails_closed():
    assert fonts.FONT_SCALES == {"small": 0.85, "medium": 1.0, "large": 1.15}
    fonts.set_font_scale("small")
    assert fonts.font_scale() == 0.85
    fonts.set_font_scale("large")
    assert fonts.font_scale() == 1.15
    fonts.set_font_scale("  LARGE ")  # tolerant of stray whitespace/casing
    assert fonts.font_scale() == 1.15
    # unknown / empty / None fail closed to medium
    for bad in ("huge", "", None):
        fonts.set_font_scale(bad)
        assert fonts.font_scale() == 1.0


def test_ui_point_size_tiers():
    for mode, expected in (("small", 11), ("medium", 13), ("large", 15)):
        fonts.set_font_scale(mode)
        assert fonts.ui_point_size() == expected


def test_scale_qss_scales_font_sizes_only():
    css = ("QPushButton { font-size: 17px; border: 3px solid #1a2a4a; "
           "border-radius: 3px; padding: 3px; }"
           "QLabel { font-size: 15pt; }")
    fonts.set_font_scale("medium")
    assert fonts.scale_qss(css) == css  # identity at 1.0 — byte-for-byte

    fonts.set_font_scale("large")  # 1.15
    out = fonts.scale_qss(css)
    assert "font-size: 20px" in out      # 17 * 1.15 = 19.55 -> 20
    assert "font-size: 17.2pt" in out   # 15 * 1.15 = 17.25 -> 17.2 (round half even)
    # borders, radii and paddings are NOT font sizes — untouched
    assert "border: 3px solid #1a2a4a" in out
    assert "border-radius: 3px" in out
    assert "padding: 3px" in out

    fonts.set_font_scale("small")  # 0.85
    out = fonts.scale_qss(css)
    assert "font-size: 14px" in out    # 17 * 0.85 = 14.45 -> 14
    assert "font-size: 12.8pt" in out  # 15 * 0.85 = 12.75 -> 12.8
    # the floor keeps tiny sizes from vanishing entirely
    assert fonts.scale_qss("a { font-size: 1px; }") == "a { font-size: 1px; }"


def test_scale_qss_handles_spacing_variants():
    fonts.set_font_scale("small")
    # the value is rewritten in canonical "font-size: Npx" form (CSS-equal)
    assert fonts.scale_qss("a{font-size:18px}") == "a{font-size: 15px}"
    assert fonts.scale_qss("a{font-size:  18 px}") == "a{font-size: 15px}"


# --- text theme engine --------------------------------------------------------

def test_set_text_theme_maps_and_fails_closed():
    assert fonts.TEXT_THEMES == {
        "white": "#ffffff",
        "light_blue": "#a8edff",
        "blue": "#4da3ff",
        "dark_blue": "#2a63d6",
    }
    for mode, hexv in fonts.TEXT_THEMES.items():
        fonts.set_text_theme(mode)
        assert fonts.text_theme_hex() == hexv
    fonts.set_text_theme("  DARK_BLUE ")  # tolerant of stray whitespace/casing
    assert fonts.text_theme_hex() == "#2a63d6"
    # unknown / empty / None fail closed to white
    for bad in ("red", "", None):
        fonts.set_text_theme(bad)
        assert fonts.text_theme_hex() == "#ffffff"


def test_text_color_returns_theme_qcolor():
    from PySide6.QtGui import QColor
    fonts.set_text_theme("blue")
    c = fonts.text_color()
    assert isinstance(c, QColor)
    assert c.name() == "#4da3ff"  # painted text follows the same theme


def test_scale_qss_applies_text_theme():
    fonts.set_text_theme("white")
    css = "QLabel { color: #ffffff; }"
    assert fonts.scale_qss(css) == css  # identity under the default theme

    fonts.set_text_theme("dark_blue")
    out = fonts.scale_qss(
        "QLabel { color: #ffffff; }\n"
        "a { color:#ffffff; font-size: 18px; }")
    # both forms are rewritten canonically (like the font-size pass)
    assert out.count("color: #2a63d6") == 2
    assert "font-size: 18px" in out         # theme pass doesn't break scaling
    # font scale still applies alongside the theme
    fonts.set_font_scale("large")
    out = fonts.scale_qss("a { color: #ffffff; font-size: 18px; }")
    assert "color: #2a63d6" in out and "font-size: 21px" in out


def test_scale_qss_theme_protects_non_text_colors():
    fonts.set_text_theme("dark_blue")
    out = fonts.scale_qss(
        "QTableWidget {"
        " background-color: #080b12; color: #ffffff;"
        " selection-color: #ffffff; selection-background-color: #1a2a4a;"
        " gridline-color: #1a2a4a; border: 1px solid #1a2a4a; }"
        ".msg-error { color: #ff3366; }")
    assert "color: #2a63d6" in out          # the one themeable declaration
    assert out.count("color: #ffffff") == 1  # only selection-color survives
    assert "selection-background-color: #1a2a4a" in out
    assert "gridline-color: #1a2a4a" in out
    assert "border: 1px solid #1a2a4a" in out
    assert "color: #ff3366" in out          # semantic colors are never touched


def test_scale_qss_theme_keeps_text_white_on_bright_backgrounds():
    fonts.set_text_theme("dark_blue")
    css = (
        # selected/active/hover fills + accent bg keep their white text
        'QMenu::item:selected { background-color: #1a2a4a; color: #ffffff; }\n'
        'QLabel[railBadge="true"] { background-color: #2a7abf; color: #ffffff; }\n'
        # ...and a background GRADIENT carrying the accent counts as bright
        'a { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,'
        ' stop:0 #05070c, stop:1 #2a7abf); color: #ffffff; }\n'
        # explicit keep-white marker on the selector side (text inheriting a
        # bright parent fill) and inside the block body both hold
        'b /* keep-white */ { color: #ffffff; }\n'
        'i { color: #ffffff; /* keep-white */ }\n'
        # but a gridline/border use of the same hex must NOT protect a block
        'QTableWidget { gridline-color: #1a2a4a; color: #ffffff; }')
    out = fonts.scale_qss(css)
    assert out.count("color: #2a63d6") == 1   # only the gridline block themes
    assert "gridline-color: #1a2a4a; color: #2a63d6" in out
    assert out.count("color: #ffffff") == 5   # protected whites survive


def test_scale_qss_theme_on_braceless_html():
    """Rich-text fragments (chat spans) carry no braces — still themed."""
    fonts.set_text_theme("light_blue")
    out = fonts.scale_qss('<span style="color:#ffffff; font-size:18px">x</span>')
    assert "color: #a8edff" in out


# --- stylesheet wrap coverage + 1px border rule -----------------------------

GUI_DIR = pathlib.Path(__file__).resolve().parent.parent / "gui"


def _qss_call_sites(mention):
    """(file, node, segment) for every setStyleSheet/setHtml/
    setDefaultStyleSheet call whose source segment mentions ``mention``."""
    for path in sorted(GUI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = path.read_text(encoding="utf-8").splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr in ("setStyleSheet", "setHtml", "setDefaultStyleSheet")):
                continue
            segment = "\n".join(lines[node.lineno - 1:node.end_lineno])
            if mention in segment:
                yield path, node, segment


def test_every_font_size_stylesheet_is_scaled():
    """Every stylesheet call that pins font-size must go through scale_qss —
    a bare literal stays frozen when the user picks Small/Large."""
    unwrapped = [
        f"{path.name}:{node.lineno}"
        for path, node, segment in _qss_call_sites("font-size")
        if "scale_qss" not in segment
    ]
    assert not unwrapped, f"unwrapped font-size stylesheets: {unwrapped}"


_WHITE_TEXT_RE = re.compile(r"color:\s*#ffffff", re.IGNORECASE)


def test_every_white_text_stylesheet_is_themed():
    """Every stylesheet call carrying themeable white text must go through
    scale_qss — a bare literal stays frozen when the user picks a blue
    theme. (Named `color: white` is deliberate pin-white on accent fills
    and is exempt; the pure-white test ignores it too.)"""
    unwrapped = [
        f"{path.name}:{node.lineno}"
        for path, node, segment in _qss_call_sites("color:")
        if _WHITE_TEXT_RE.search(segment) and "scale_qss" not in segment
    ]
    assert not unwrapped, f"unwrapped white-text stylesheets: {unwrapped}"


def test_no_thick_qss_borders():
    """Outlines are 1px (owner decision 2026-11-03). border-radius values
    are corner rounding and are exempt."""
    thick = re.compile(r"border(?:-top|-right|-bottom|-left|-width)?:\s*[2-9]px")
    offenders = []
    for path in sorted(GUI_DIR.glob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in thick.finditer(line):
                offenders.append(f"{path.name}:{i}: {m.group(0)}")
    assert not offenders, f"thick QSS borders found: {offenders}"


# Text is PURE WHITE (owner decision 2026-11-03). Semantic status colors are
# the only exemption — errors/warnings must stay visible. Python-side color
# maps (downloads _STATUS_COLORS, record-state colours) are data, not QSS,
# and keep their own palettes.
SEMANTIC_TEXT_COLORS = {
    "#ffcc00",  # queued / warning-test-result yellow
    "#ff3366", "#ff6b6b", "#ff8080", "#ff5555", "#ff6666",  # error reds
    "#e67e22", "#f0b490",   # warning oranges (record, buffering, agent .e)
    "#5a7a6a",               # quiet-ok green
}


def test_all_text_is_pure_white():
    """Every QSS `color:`/`selection-color:` literal is #ffffff or semantic.

    The literal stays #ffffff at the SOURCE level even though the text is
    themeable — scale_qss rewrites it to the active theme hex at apply time,
    so a theme color must never be hardcoded into a stylesheet string."""
    decl = re.compile(r"(?<![-\w])(?:selection-)?color:\s*(#[0-9a-fA-F]{6})\b")
    dim_rgba = re.compile(r"(?<![-\w])color:\s*rgba\(238,\s*244,\s*255")
    offenders = []
    for path in sorted(GUI_DIR.glob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if dim_rgba.search(line):
                offenders.append(f"{path.name}:{i}: translucent off-white text")
            for m in decl.finditer(line):
                if m.group(1).lower() not in SEMANTIC_TEXT_COLORS | {"#ffffff"}:
                    offenders.append(f"{path.name}:{i}: {m.group(0)}")
    assert not offenders, f"non-white text colors found: {offenders}"


# Background ladder (darkened 2026-11-03 so the darkest blue text theme
# stays readable): the OLD brighter literals must not come back as QSS
# backgrounds. Hover/selection (#1a2a4a), accent (#2a7abf) and pure black
# are NOT in this table — they were kept deliberately.
_OLD_BRIGHT_BGS = re.compile(
    r"background(?:-color)?:\s*#(?:0a0a0f|0d1117|111827|0f1520)\b"
    r"|rgba\(10,\s*10,\s*15,")
_NEW_LADDER = {"#05070c": "window base", "#080b12": "panel",
               "#0b1120": "card/button", "#0a0e16": "zebra"}


def test_background_ladder_is_dark():
    """The near-black navy ladder is in place and the old brighter
    backgrounds are gone from every QSS/painted surface."""
    offenders = []
    seen_new = set()
    for path in sorted(GUI_DIR.glob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = _OLD_BRIGHT_BGS.search(line)
            if m:
                offenders.append(f"{path.name}:{i}: {m.group(0)}")
            seen_new.update(h for h in _NEW_LADDER if h in line)
    assert not offenders, f"old bright backgrounds found: {offenders}"
    assert seen_new == set(_NEW_LADDER), "new ladder literals missing"
