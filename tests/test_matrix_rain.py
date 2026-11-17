"""Matrix digital rain (gui/matrix_rain.py) — the catch22.net-effect port
that paints behind the Agent tab's idle banner (owner request 2026-11-17)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtGui = pytest.importorskip("PySide6.QtGui")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from gui.matrix_rain import _BRIGHTNESS_LEVELS, _GLYPHS, MatrixRain  # noqa: E402


def test_matrix_rain_paints_visible_trails():
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from PySide6.QtCore import QRect

    rain = MatrixRain(seed=1)
    surface = QtGui.QPixmap(480, 360)
    painter = QtGui.QPainter(surface)
    painter.fillRect(surface.rect(), QtGui.QColor(0, 0, 0))
    rain.paint(painter, QRect(0, 0, 480, 360))  # builds the column grid
    for _ in range(40):
        rain.tick()
    painter.fillRect(surface.rect(), QtGui.QColor(0, 0, 0))
    rain.paint(painter, QRect(0, 0, 480, 360))
    painter.end()

    img = surface.toImage()
    green = sum(QtGui.qGreen(img.pixel(x, y))
                for y in range(0, 360, 4) for x in range(0, 480, 4))
    assert green > 2000, "expected visible green glyph pixels"


def test_matrix_rain_columns_stable_across_respawns():
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from PySide6.QtCore import QRect

    rain = MatrixRain(seed=2)
    surface = QtGui.QPixmap(320, 240)
    painter = QtGui.QPainter(surface)
    rain.paint(painter, QRect(0, 0, 320, 240))
    painter.end()
    count = rain.columns
    assert count == 320 // 16
    for _ in range(2000):
        rain.tick()
    assert rain.columns == count  # respawns keep the grid stable


def test_matrix_rain_glyph_cache_is_bounded():
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from PySide6.QtCore import QRect

    rain = MatrixRain(seed=3)
    surface = QtGui.QPixmap(160, 120)
    painter = QtGui.QPainter(surface)
    for _ in range(60):
        rain.tick()
        rain.paint(painter, QRect(0, 0, 160, 120))
    painter.end()
    assert len(rain._cache) <= len(_GLYPHS) * (_BRIGHTNESS_LEVELS + 1)


def test_chat_history_rain_runs_only_while_idle_and_shown():
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from gui.main_window import _ChatHistoryEdit

    view = _ChatHistoryEdit("")
    view.resize(600, 400)
    view.show()
    assert view._rain_timer.isActive()  # idle + shown -> running

    view.set_logo_visible(False)  # conversation starts -> stop
    assert not view._rain_timer.isActive()

    view.set_logo_visible(True)  # back to idle -> running again
    assert view._rain_timer.isActive()

    view.hide()  # tab switched away -> stop
    assert not view._rain_timer.isActive()
