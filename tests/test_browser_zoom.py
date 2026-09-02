"""Browser zoom: Ctrl+scroll-wheel zoom and the nav-bar percentage badge.

The real MainWindow zoom/eventFilter methods are bound to a lightweight
attribute host (no engine/agent/tray launch), mirroring test_browser_page's
approach. The filter is invoked directly with constructed QWheelEvents —
no event-loop pumping, because QtWebEngine's offscreen widget pipeline is
unstable under synthetic native event delivery. Wheel events target a CHILD
widget of the view, exactly like Chromium's internal render widget does.
"""
from __future__ import annotations

import os
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt, QPoint, QPointF, QUrl
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QLabel, QTabWidget, QWidget
from PySide6.QtWebEngineWidgets import QWebEngineView

from config import DeeptorrentConfig
from gui.main_window import MainWindow


class _HostBase:
    def eventFilter(self, obj, event) -> bool:
        return False


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _make_host(tabs: QTabWidget):
    """(host, badge) — real MainWindow zoom methods on a bare attribute host."""
    host = _HostBase()
    host.browser_tabs = tabs
    host._browser_fs_state = None
    host._browser_content = None
    host.chat_input = None
    host.browser_url_bar = None
    host.browser_zoom_label = QLabel("100%")
    host.config = DeeptorrentConfig()
    host._schedule_browser_session_save = lambda: None
    for name in (
        "eventFilter",
        "_browser_view_for_widget",
        "_browser_zoom",
        "_browser_set_zoom",
        "_current_browser_view",
    ):
        setattr(host, name, types.MethodType(getattr(MainWindow, name), host))
    host._browser_origin = MainWindow._browser_origin  # staticmethod — no binding
    return host, host.browser_zoom_label


def _ctrl_wheel(dy: int = 120) -> QWheelEvent:
    return QWheelEvent(
        QPointF(50, 50), QPointF(50, 50), QPoint(0, 0), QPoint(0, dy),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase, False)


def _browser_with_child():
    tabs = QTabWidget()
    view = QWebEngineView()
    tabs.addTab(view, "tab")
    # Wheel events target the view's child widget, like Chromium's render
    # widget — the filter must walk up the parent chain to find the view.
    child = QWidget(view)
    return tabs, view, child


def test_ctrl_wheel_zooms_view_and_updates_badge(app):
    tabs, view, child = _browser_with_child()
    host, badge = _make_host(tabs)
    assert host.eventFilter(child, _ctrl_wheel(120)) is True  # consumed
    assert abs(view.zoomFactor() - 1.1) < 1e-6
    assert badge.text() == "110%"
    assert host.eventFilter(child, _ctrl_wheel(-120)) is True
    assert host.eventFilter(child, _ctrl_wheel(-120)) is True
    assert abs(view.zoomFactor() - 0.9) < 1e-6
    assert badge.text() == "90%"
    # Keyboard-shortcut path (no explicit view) drives the same code.
    host._browser_zoom(0.1)
    assert abs(view.zoomFactor() - 1.0) < 1e-6
    assert badge.text() == "100%"


def test_ctrl_wheel_zoom_is_clamped(app):
    tabs, view, child = _browser_with_child()
    host, _ = _make_host(tabs)
    for _ in range(30):
        assert host.eventFilter(child, _ctrl_wheel(-120)) is True
    assert abs(view.zoomFactor() - 0.5) < 1e-6
    for _ in range(60):
        assert host.eventFilter(child, _ctrl_wheel(120)) is True
    assert abs(view.zoomFactor() - 3.0) < 1e-6


def test_non_browser_widgets_are_not_matched(app):
    tabs, view, child = _browser_with_child()
    host, _ = _make_host(tabs)
    # DevTools-style window: a web view that is NOT a browser tab — the
    # filter must not touch it (its ctrl-wheel passes through untouched).
    devtools = QWebEngineView()
    assert host._browser_view_for_widget(QWidget(devtools)) is None
    # No web-view ancestor at all / not a widget.
    assert host._browser_view_for_widget(QWidget()) is None
    assert host._browser_view_for_widget("not-a-widget") is None
    # The actual browser-tab child resolves to its view.
    assert host._browser_view_for_widget(child) is view


def test_fullscreen_view_ctrl_wheel_zooms_detached_view(app):
    tabs, view, child = _browser_with_child()
    host, badge = _make_host(tabs)
    # Simulate DOM fullscreen: the view is detached from the tab widget and
    # tracked in _browser_fs_state.
    tabs.removeTab(0)
    host._browser_fs_state = (view, 0, "tab", "tab")
    assert host.eventFilter(child, _ctrl_wheel(120)) is True
    assert abs(view.zoomFactor() - 1.1) < 1e-6
    # The badge only tracks the ACTIVE tab; a detached view isn't it.
    assert badge.text() == "100%"


def test_zoom_persists_per_http_origin(app):
    tabs, view, child = _browser_with_child()
    host, _ = _make_host(tabs)
    view.setUrl(QUrl("https://example.invalid/"))
    host._browser_set_zoom(1.5, view=view)
    assert host.config.browser.zoom_by_origin.get("https://example.invalid") == 1.5


def test_private_view_zoom_is_not_persisted(app):
    tabs, view, child = _browser_with_child()
    host, _ = _make_host(tabs)
    view.setProperty("deepflux_private", True)
    view.setUrl(QUrl("https://example.invalid/"))
    host._browser_set_zoom(2.0, view=view)
    assert abs(view.zoomFactor() - 2.0) < 1e-6
    assert host.config.browser.zoom_by_origin == {}
