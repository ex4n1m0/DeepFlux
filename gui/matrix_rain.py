"""Matrix digital rain — a Qt port of the classic catch22.net effect.

The analyzed ``matrix.scr`` (static analysis only, never executed) is J
Brown's 2003 "Matrix Screensaver" rebuilt on the Henry++ framework: GDI
glyph drawing into a ``CreateDIBSection`` framebuffer that DECAYS every
``SetTimer`` tick, katakana glyph streams advancing down each column, head
brighter than the trail, with config for hue / glyph speed / trail length
("Number of glyphs in each level") / color randomization.

This port keeps the look and drops the per-pixel decay buffer (the classic
CPU cost): each column redraws its trail per frame with an alpha ramp, and
glyphs are pre-rendered into small pixmaps (char × brightness level), so a
frame is pure ``drawPixmap`` blits. Pure state + paint — the HOST owns the
timer and calls :meth:`tick` + :meth:`paint` (no widget, no event loop:
offscreen-testable, and it only runs while the host is idle and visible).
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap

# The authentic glyph diet: katakana + digits + a few symbols (the original
# alternates half-width katakana and latin digits).
_GLYPHS = (
    "アイウエオカキクケコサシスセソタチツテトナニヌネノ"
    "ハヒフヘホマミムメモヤユヨラリルレロワン"
    "0123456789:･.=*+-|"
)

# BLUE rain (owner 2026-11-17: "make sure the rain is blue — normally I
# select 30 for the hue rotation" in the .scr's config). The screensaver's
# hue knob rotates its default green; we go straight to the app's accent-
# blue family (#2a7abf ≈ hue 207) so the effect matches DeepFlux's chrome.
HUE = 210
SATURATION = 230

# Brightness ramp: index 0 = head (near-white blue), higher = dimmer trail.
_BRIGHTNESS_LEVELS = 10
_HEAD_COLOR = QColor(185, 225, 255)

FPS = 18
CELL_W = 16
CELL_H = 22
MAX_TRAIL = 24
# Per-tick advance range, in cells (owner 2026-11-18: "reduce the speed").
# Halved from the original port's 0.25–1.1 — the same 18 fps cadence, each
# stream just falls slower. Fractional speeds keep streams out of lockstep.
SPEED_MIN = 0.12
SPEED_MAX = 0.55


def _level_color(level: int, levels: int = _BRIGHTNESS_LEVELS) -> QColor:
    """Trail color for a brightness level (0 = brightest after the head)."""
    value = 235 - int(200 * level / max(1, levels - 1))
    color = QColor.fromHsv(HUE, SATURATION, max(40, value))
    return color


class MatrixRain:
    """Glyph-stream state for one surface; tick() then paint()."""

    def __init__(self, seed: Optional[int] = None) -> None:
        self._rng = random.Random(seed)
        self._columns: List[dict] = []
        self._size = (0, 0)
        # (char, level) -> small rendered pixmap; bounded by construction:
        # len(_GLYPHS) * _BRIGHTNESS_LEVELS entries, built lazily.
        self._cache: Dict[Tuple[str, int], QPixmap] = {}
        self._font = QFont("Yu Gothic UI")
        self._font.setPixelSize(CELL_H - 6)
        self._font.setStyleHint(QFont.StyleHint.Monospace)

    # ------------------------------------------------------------ geometry
    def _build_columns(self, w: int, h: int) -> None:
        rows = max(1, h // CELL_H)
        cols = max(1, w // CELL_W)
        self._columns = []
        for _ in range(cols):
            self._columns.append(self._spawn_column(rows, at_top=False))
        self._size = (w, h)

    def _spawn_column(self, rows: int, at_top: bool) -> dict:
        return {
            # cells per tick — fractional speeds keep streams from marching
            # in lockstep (the .scr's "Glyphs speed" jitter).
            "speed": self._rng.uniform(SPEED_MIN, SPEED_MAX),
            "head": float(self._rng.uniform(0, rows) if not at_top else -self._rng.uniform(0, rows)),
            "trail": self._rng.randint(8, MAX_TRAIL),
            # Glyphs persist within a stream and re-roll occasionally —
            # the flicker of the original (full re-roll each frame is noise).
            "glyphs": [self._rng.choice(_GLYPHS) for _ in range(MAX_TRAIL)],
        }

    # ---------------------------------------------------------------- tick
    def tick(self) -> None:
        """Advance every stream one frame."""
        w, h = self._size
        if not self._columns:
            return
        rows = max(1, h // CELL_H)
        for col in self._columns:
            col["head"] += col["speed"]
            # Re-roll ~15% of the trail glyphs per tick (flicker).
            glyphs = col["glyphs"]
            for _ in range(3):
                glyphs[self._rng.randrange(len(glyphs))] = self._rng.choice(_GLYPHS)
            if col["head"] - col["trail"] > rows:
                col.update(self._spawn_column(rows, at_top=True))

    # --------------------------------------------------------------- paint
    def _pixmap(self, char: str, level: int) -> QPixmap:
        key = (char, level)
        pm = self._cache.get(key)
        if pm is None:
            pm = QPixmap(CELL_W, CELL_H)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setFont(self._font)
            color = _HEAD_COLOR if level < 0 else _level_color(level)
            p.setPen(color)
            p.drawText(QRect(0, 0, CELL_W, CELL_H), 0x84, char)  # AlignCenter
            p.end()
            self._cache[key] = pm
        return pm

    def paint(self, painter: QPainter, rect: QRect) -> None:
        """Draw the current frame into ``rect`` (top-left anchored grid)."""
        if (rect.width(), rect.height()) != self._size or not self._columns:
            self._build_columns(rect.width(), rect.height())
        painter.save()
        # Overall dimness: the effect sits behind the idle banner's margins —
        # atmospheric, not a screensaver takeover.
        painter.setOpacity(0.55)
        rows = max(1, rect.height() // CELL_H)
        for idx, col in enumerate(self._columns):
            x = rect.x() + idx * CELL_W
            head = col["head"]
            trail = col["trail"]
            for step in range(trail):
                row = int(head) - step
                if row < 0 or row >= rows:
                    continue
                level = min(_BRIGHTNESS_LEVELS - 1,
                            int(_BRIGHTNESS_LEVELS * step / trail))
                painter.drawPixmap(x, rect.y() + row * CELL_H,
                                   self._pixmap(col["glyphs"][step], level))
        painter.restore()

    # ------------------------------------------------------------- helpers
    @property
    def columns(self) -> int:
        return len(self._columns)
