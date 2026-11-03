"""Font size setting (Settings → Font Size): config field, the gui/fonts
scale engine, stylesheet wrap coverage, and the 1px-border rule.

The scale engine is process-global module state — every test restores the
default ("medium" / 1.0) via the fixture so nothing leaks into other tests.
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
    yield
    fonts.set_font_scale("medium")


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


# --- stylesheet wrap coverage + 1px border rule -----------------------------

GUI_DIR = pathlib.Path(__file__).resolve().parent.parent / "gui"


def _qss_call_sites():
    """(file, node) for every setStyleSheet/setHtml/setDefaultStyleSheet call
    whose source segment mentions font-size."""
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
            if "font-size" in segment:
                yield path, node, segment


def test_every_font_size_stylesheet_is_scaled():
    """Every stylesheet call that pins font-size must go through scale_qss —
    a bare literal stays frozen when the user picks Small/Large."""
    unwrapped = [
        f"{path.name}:{node.lineno}"
        for path, node, segment in _qss_call_sites()
        if "scale_qss" not in segment
    ]
    assert not unwrapped, f"unwrapped font-size stylesheets: {unwrapped}"


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
    """Every QSS `color:`/`selection-color:` literal is #ffffff or semantic."""
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
