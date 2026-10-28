"""Browser auto-hide chrome + activity rail + link context-menu order.

The real MainWindow hover methods are bound to a lightweight QWidget host
(no engine/agent/tray launch), mirroring test_browser_zoom's approach.
underMouse() is always False offscreen, so the hide paths run unguarded —
deterministic. Visibility is asserted via the _chrome_hidden /
_rail_hidden_for_browser flags (the source of truth the production code
uses) plus isHidden() on the widgets.

The context-menu tests exercise promote_open_link_action() on plain
QMenus — QtWebEngine's offscreen pipeline is too unstable for synthetic
context-menu events (see test_browser_zoom's module docstring).
"""
from __future__ import annotations

import os
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QTabWidget, QWidget

from gui.main_window import MainWindow, promote_open_link_action


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Host(QWidget):
    def eventFilter(self, obj, event) -> bool:
        return False


def _make_host(current_browser: bool = True):
    """Real MainWindow hover methods on a bare QWidget host."""
    host = _Host()
    host.main_tabs = QTabWidget(host)
    host.main_tabs.setFixedSize(1200, 800)
    agent_page = QWidget()
    browser_page = QWidget()
    browser_page.setFixedSize(1200, 800)
    host.main_tabs.addTab(agent_page, "Agent")
    host.main_tabs.addTab(browser_page, "Browse")
    host.main_tabs.setCurrentWidget(browser_page if current_browser else agent_page)
    host._browser_tab = browser_page
    host._browser_fs_state = None
    host._browser_chrome = QWidget(host)
    host._browser_chrome.setFixedSize(1200, 72)
    host._browser_chrome.hide()
    host.activity_rail = QWidget(host)
    host.activity_rail.setFixedSize(84, 600)
    host._chrome_hidden = True
    host._rail_hidden_for_browser = False
    host._chrome_hide_timer = QTimer(host)
    host._chrome_hide_timer.setSingleShot(True)
    host._rail_hide_timer = QTimer(host)
    host._rail_hide_timer.setSingleShot(True)
    host._EDGE_REVEAL_PX = MainWindow._EDGE_REVEAL_PX
    for name in (
        "_poll_browser_hover",
        "_check_chrome_hover",
        "_show_browser_chrome",
        "_hide_browser_chrome",
        "_check_rail_hover",
        "_hide_browser_rail",
    ):
        setattr(host, name, types.MethodType(getattr(MainWindow, name), host))
    return host


# -- chrome (address bar + tab strip) ----------------------------------------

def test_chrome_reveals_at_top_edge(app):
    host = _make_host()
    host._check_chrome_hover(QPoint(600, 5))
    assert host._chrome_hidden is False
    assert not host._browser_chrome.isHidden()
    assert not host._chrome_hide_timer.isActive()


def test_chrome_stays_hidden_away_from_edge(app):
    host = _make_host()
    # 40px down is well past the 10px reveal edge and inside what used to
    # be the always-visible nav row — no reveal, no timer.
    host._check_chrome_hover(QPoint(600, 40))
    assert host._chrome_hidden is True
    assert host._browser_chrome.isHidden()
    assert not host._chrome_hide_timer.isActive()


def test_chrome_zone_widens_while_visible(app):
    host = _make_host()
    host._show_browser_chrome()
    # A few px below the 72px chrome: still pinned (hysteresis zone).
    host._check_chrome_hover(QPoint(600, 76))
    assert host._chrome_hidden is False
    assert not host._chrome_hide_timer.isActive()
    # Far below: hide timer arms, chrome stays until it fires.
    host._check_chrome_hover(QPoint(600, 300))
    assert host._chrome_hide_timer.isActive()
    assert host._chrome_hidden is False
    host._hide_browser_chrome()
    assert host._chrome_hidden is True
    assert host._browser_chrome.isHidden()


def test_chrome_hide_is_noop_while_revealed(app):
    host = _make_host()
    host._show_browser_chrome()
    host._show_browser_chrome()  # idempotent
    assert host._chrome_hidden is False


# -- activity rail ------------------------------------------------------------

def test_rail_reveals_at_left_edge(app):
    host = _make_host()
    host._rail_hidden_for_browser = True
    host.activity_rail.hide()
    host._check_rail_hover(QPoint(4, 300))
    assert host._rail_hidden_for_browser is False
    assert not host.activity_rail.isHidden()


def test_rail_hidden_when_cursor_moves_away(app):
    host = _make_host()
    host._check_rail_hover(QPoint(300, 300))
    assert host._rail_hide_timer.isActive()
    assert host._rail_hidden_for_browser is False  # grace period
    host._hide_browser_rail()
    assert host._rail_hidden_for_browser is True
    assert host.activity_rail.isHidden()


def test_rail_zone_covers_rail_while_visible(app):
    host = _make_host()
    # Cursor over the rail itself (negative x in main_tabs coords): pinned.
    host._check_rail_hover(QPoint(-40, 300))
    assert not host._rail_hide_timer.isActive()
    assert host._rail_hidden_for_browser is False


def test_rail_zones_respect_vertical_bounds(app):
    host = _make_host()
    host._rail_hidden_for_browser = True
    host.activity_rail.hide()
    # Left edge but above the content area (menu bar region): no reveal.
    host._check_rail_hover(QPoint(4, -10))
    assert host._rail_hidden_for_browser is True


# -- poll: tab-switch + DOM-fullscreen behavior -------------------------------

def test_poll_restores_rail_when_leaving_browser_tab(app):
    host = _make_host(current_browser=True)
    host._rail_hidden_for_browser = True
    host.activity_rail.hide()
    host._show_browser_chrome()
    host.main_tabs.setCurrentIndex(0)  # Agent tab
    host._poll_browser_hover()
    assert host._rail_hidden_for_browser is False
    assert not host.activity_rail.isHidden()
    # Chrome is force-hidden off the Browse tab.
    assert host._chrome_hidden is True
    assert host._browser_chrome.isHidden()


def test_poll_never_restores_a_rail_it_did_not_hide(app):
    # Pure video fullscreen hides the rail itself; the flag is False, so
    # the browser poll must not fight it while other tabs are active.
    host = _make_host(current_browser=False)
    host.activity_rail.hide()
    host._poll_browser_hover()
    assert host.activity_rail.isHidden()
    assert host._rail_hidden_for_browser is False


def test_poll_dom_fullscreen_hides_chrome_and_rail(app):
    host = _make_host(current_browser=True)
    host._show_browser_chrome()
    host._browser_fs_state = ("view", 0, "title", "tip")
    host._poll_browser_hover()
    assert host._chrome_hidden is True
    assert host._browser_chrome.isHidden()
    assert host._rail_hidden_for_browser is True
    assert host.activity_rail.isHidden()
    # Leaving DOM fullscreen returns to normal auto-hide semantics: the
    # rail comes back on left-edge hover.
    host._browser_fs_state = None
    host._check_rail_hover(QPoint(4, 300))
    assert host._rail_hidden_for_browser is False


# -- context menu: "Open link in new tab" on top --------------------------------

def _link_menu():
    menu = QMenu()
    back = QAction("Back", menu)
    fwd = QAction("Forward", menu)
    open_new = QAction("Open link in new tab", menu)
    save = QAction("Save link...", menu)
    for a in (back, fwd):
        menu.addAction(a)
    menu.addSeparator()
    for a in (open_new, save):
        menu.addAction(a)
    return menu, open_new


def test_promote_moves_open_in_new_tab_to_top(app):
    menu, act = _link_menu()
    assert promote_open_link_action(menu, act) is True
    assert menu.actions()[0] is act
    # Order of the remaining rows is untouched.
    texts = [a.text() for a in menu.actions()]
    assert texts[0] == "Open link in new tab"
    assert "Back" in texts and "Forward" in texts


def test_promote_drops_leading_separator(app):
    menu = QMenu()
    act = QAction("Open link in new tab", menu)
    menu.addSeparator()
    menu.addAction(act)
    other = QAction("Copy link address", menu)
    menu.addAction(other)
    assert promote_open_link_action(menu, act) is True
    assert not menu.actions()[0].isSeparator()
    assert menu.actions()[0] is act


def test_promote_noop_when_action_missing(app):
    menu, _ = _link_menu()
    stranger = QAction("Not in this menu")
    before = list(menu.actions())
    assert promote_open_link_action(menu, stranger) is False
    assert list(menu.actions()) == before


def test_promote_noop_when_action_invisible(app):
    menu, act = _link_menu()
    act.setVisible(False)
    before = list(menu.actions())
    assert promote_open_link_action(menu, act) is False
    assert list(menu.actions()) == before


def test_promote_noop_on_none(app):
    menu, _ = _link_menu()
    before = list(menu.actions())
    assert promote_open_link_action(menu, None) is False
    assert list(menu.actions()) == before


def test_browser_view_overrides_context_menu(app):
    # _BrowserView must exist and actually override the context menu (the
    # reorder itself is the promoted-action helper tested above).
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from gui import main_window as mw
    assert issubclass(mw._BrowserView, QWebEngineView)
    assert (mw._BrowserView.contextMenuEvent
            is not QWebEngineView.contextMenuEvent)
