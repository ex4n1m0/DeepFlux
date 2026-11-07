"""Browser auto-hide chrome + link context-menu order (the activity rail
and its left-edge auto-hide were REMOVED in 7.6 — the poll is chrome-only).

The real MainWindow hover methods are bound to a lightweight QWidget host
(no engine/agent/tray launch), mirroring test_browser_zoom's approach.
underMouse() is always False offscreen, so the hide paths run unguarded —
deterministic. Visibility is asserted via the _chrome_hidden flag (the
source of truth the production code uses) plus isHidden() on the widget.

Since 2026-11-07 the chrome SLIDES over _EdgeSlide.DURATION_MS instead
of popping: the flags flip when a slide COMPLETES, so hide-path tests
fast-forward the animation (setCurrentTime(duration) finishes it
synchronously — no half-second wall waits). The hosts below build real
_EdgeSlide instances around their fake widgets, so the animation engine
itself is under test too.

The context-menu tests exercise promote_open_link_action() on plain
QMenus — QtWebEngine's offscreen pipeline is too unstable for synthetic
context-menu events (see test_browser_zoom's module docstring).
"""
from __future__ import annotations

import os
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QAction, QMouseEvent
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMenu, QTabWidget, QWidget)

from gui.main_window import (_BROWSER_TAB_MAX_WIDTH, _BookmarksPopup,
                             _EdgeSlide, _ElideLabel,
                             MainWindow, _MenuUrlHost,
                             promote_open_link_action)
from gui.responsive import ResponsiveRow


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Host(QWidget):
    def eventFilter(self, obj, event) -> bool:
        return False


def _finish(slide) -> None:
    """Fast-forward a running slide to its end — finished() fires
    synchronously, no wall-clock wait."""
    anim = slide._anim
    assert anim.state() == QAbstractAnimation.State.Running
    anim.setCurrentTime(anim.duration())


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
    host._chrome_hidden = True
    host._chrome_hide_timer = QTimer(host)
    host._chrome_hide_timer.setSingleShot(True)
    host._EDGE_REVEAL_PX = MainWindow._EDGE_REVEAL_PX
    host._CHROME_HIDE_GRACE_MS = MainWindow._CHROME_HIDE_GRACE_MS
    for name in (
        "_poll_browser_hover",
        "_check_chrome_hover",
        "_show_browser_chrome",
        "_hide_browser_chrome",
        "_on_chrome_slide_hidden",
    ):
        setattr(host, name, types.MethodType(getattr(MainWindow, name), host))
    # Real slide engine around the fake chrome. natural() mirrors its
    # fixed extent (the production chrome uses its layout sizeHint; this
    # bare QWidget has none).
    host._chrome_slide = _EdgeSlide(
        host._browser_chrome, lambda: 72, host._on_chrome_slide_hidden,
        vertical=True, parent=host)
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
    _finish(host._chrome_slide)  # let the slide-in finish
    app.processEvents()  # layout applies the restored natural height
    # A few px below the 72px chrome: still pinned (hysteresis zone).
    host._check_chrome_hover(QPoint(600, 76))
    assert host._chrome_hidden is False
    assert not host._chrome_hide_timer.isActive()
    # Far below: hide timer arms, chrome stays until it fires.
    host._check_chrome_hover(QPoint(600, 300))
    assert host._chrome_hide_timer.isActive()
    assert host._chrome_hidden is False
    # The hide now SLIDES: mid-flight the bar is still visible and the
    # flag still down; both flip only when the slide completes.
    host._hide_browser_chrome()
    assert not host._browser_chrome.isHidden()
    assert host._chrome_hidden is False
    _finish(host._chrome_slide)
    assert host._chrome_hidden is True
    assert host._browser_chrome.isHidden()


def test_chrome_hide_grace_is_two_seconds(app):
    """Owner 2026-11-07: the banner lingers 2s after the mouse leaves it
    (was 400ms — it vanished while you were still aiming around it)."""
    assert MainWindow._CHROME_HIDE_GRACE_MS == 2000
    host = _make_host()
    host._show_browser_chrome()
    _finish(host._chrome_slide)
    app.processEvents()
    host._check_chrome_hover(QPoint(600, 300))  # mouse leaves the banner
    assert host._chrome_hide_timer.isActive()
    assert host._chrome_hide_timer.interval() == 2000
    # Nothing hides while the grace runs...
    assert host._chrome_hidden is False
    assert not host._browser_chrome.isHidden()
    # ...and coming back cancels the pending hide entirely.
    host._check_chrome_hover(QPoint(600, 5))
    assert not host._chrome_hide_timer.isActive()
    assert host._chrome_hidden is False


def test_chrome_hide_is_noop_while_revealed(app):
    host = _make_host()
    host._show_browser_chrome()
    host._show_browser_chrome()  # idempotent (also mid-slide)
    assert host._chrome_hidden is False
    _finish(host._chrome_slide)
    host._show_browser_chrome()  # and once idle-shown
    assert host._chrome_hidden is False
    assert host._chrome_slide._anim.state() == QAbstractAnimation.State.Stopped


# -- poll: tab-switch + DOM-fullscreen behavior -------------------------------

def test_poll_hides_chrome_when_leaving_browser_tab(app):
    host = _make_host(current_browser=True)
    host._show_browser_chrome()
    host.main_tabs.setCurrentIndex(0)  # Agent tab
    host._poll_browser_hover()
    assert host._chrome_hidden is True
    assert host._browser_chrome.isHidden()


def test_poll_dom_fullscreen_hides_chrome(app):
    host = _make_host(current_browser=True)
    host._show_browser_chrome()
    host._browser_fs_state = ("view", 0, "title", "tip")
    host._poll_browser_hover()
    assert host._chrome_hidden is True
    assert host._browser_chrome.isHidden()
    # Leaving DOM fullscreen returns to normal auto-hide semantics: the
    # chrome reveals again on top-edge hover.
    host._browser_fs_state = None
    host._check_chrome_hover(QPoint(600, 5))
    assert host._chrome_hidden is False


# -- slide animation (2026-11-07: banners glide, half a second) ----------------

def test_slide_duration_is_half_second(app):
    # Owner-requested duration, pinned so a refactor can't silently
    # regress the glide.
    assert _EdgeSlide.DURATION_MS == 500


def test_chrome_hide_slides_then_hides(app):
    host = _make_host()
    host._show_browser_chrome()
    _finish(host._chrome_slide)
    host._hide_browser_chrome()
    anim = host._chrome_slide._anim
    assert anim.state() == QAbstractAnimation.State.Running
    assert anim.duration() == _EdgeSlide.DURATION_MS  # full travel
    assert not host._browser_chrome.isHidden()  # still visible mid-slide
    assert host._chrome_hidden is False
    _finish(host._chrome_slide)
    assert host._chrome_hidden is True
    assert host._browser_chrome.isHidden()
    # Constraints restored after the slide: the chrome sizes naturally
    # again (the host pins it at 72).
    assert host._browser_chrome.minimumHeight() == 72
    assert host._browser_chrome.maximumHeight() == 72


def test_chrome_midflight_hide_reverses_and_rescales(app):
    host = _make_host()
    host._show_browser_chrome()
    _finish(host._chrome_slide)
    host._hide_browser_chrome()
    host._chrome_slide._anim.setCurrentTime(250)  # ~half out (OutCubic)
    cur = host._chrome_slide._cur
    assert 0 < cur < 72
    host._show_browser_chrome()  # re-hover while it slides away
    anim = host._chrome_slide._anim
    assert anim.state() == QAbstractAnimation.State.Running
    # Partial distance, proportionally shorter than the full slide.
    assert anim.duration() == round(_EdgeSlide.DURATION_MS * (72 - cur) / 72)
    assert host._chrome_hidden is False
    _finish(host._chrome_slide)
    assert not host._browser_chrome.isHidden()
    assert host._chrome_hidden is False
    assert host._chrome_slide._anim.state() == QAbstractAnimation.State.Stopped


def test_chrome_force_hide_is_instant(app):
    host = _make_host()
    host._show_browser_chrome()  # reveal slide still running
    host._hide_browser_chrome(force=True)
    assert host._browser_chrome.isHidden()
    assert host._chrome_hidden is True
    assert host._chrome_slide._anim.state() == QAbstractAnimation.State.Stopped
    assert host._browser_chrome.minimumHeight() == 72  # constraints restored
    # A later reveal still starts from zero extent.
    host._show_browser_chrome()
    assert not host._browser_chrome.isHidden()


def test_edge_slide_caches_full_extent_for_reversals(app):
    # _EdgeSlide must never re-read natural() mid-flight: this slide's
    # natural() reads the LIVE constraint (the exact pattern that caused
    # the rail snap), yet a reversal started from SHOWN still targets the
    # cached full extent. (From-hidden reveals with such a natural are
    # unsupported by contract — see the _EdgeSlide docstring.)
    w = QWidget()
    w.setFixedSize(84, 600)
    slide = _EdgeSlide(w, lambda: w.minimumWidth(), vertical=False, parent=w)
    w.show()
    slide.collapse()
    slide._anim.setCurrentTime(250)
    assert 0 < slide._cur < 84
    slide.reveal()  # reversal mid-collapse
    anim = slide._anim
    assert anim.state() == QAbstractAnimation.State.Running
    assert anim.endValue() == 84
    _finish(slide)
    assert w.minimumWidth() == 84 and w.maximumWidth() == 84
    # And the reverse direction: a collapse started mid-reverse targets 0.
    slide.collapse()
    slide._anim.setCurrentTime(150)
    slide.reveal()
    slide.collapse()  # reverse again, mid-reveal
    assert slide._anim.endValue() == 0
    _finish(slide)
    assert w.isHidden()
    assert w.minimumWidth() == 84 and w.maximumWidth() == 84


def test_slide_show_now_discards_animation(app):
    host = _make_host()
    host._show_browser_chrome()
    host._chrome_slide._anim.setCurrentTime(200)  # partial reveal
    host._chrome_slide.show_now()
    assert not host._browser_chrome.isHidden()
    assert host._chrome_slide._anim.state() == QAbstractAnimation.State.Stopped
    assert host._browser_chrome.minimumHeight() == 72
    assert host._browser_chrome.maximumHeight() == 72


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


# -- menu-bar address bar (moved out of the chrome, 2026-11-04) ---------------

def _make_tab_host(app):
    """QMainWindow host mirroring production: a menu bar (File/Help), the
    address-bar corner-widget container, and the REAL tab-change + width-fit
    methods bound on. The bar lives right of Help while Browse is active and
    is swapped out on every other tab (hiding alone does NOT shrink the bar
    — a hidden corner widget keeps the menu row tall)."""
    host = QMainWindow()
    host.main_tabs = QTabWidget()
    host.setCentralWidget(host.main_tabs)
    agent_page = QWidget()
    browser_page = QWidget()
    host.main_tabs.addTab(agent_page, "Agent")
    host.main_tabs.addTab(browser_page, "Browse")
    host._browser_tab = browser_page
    host._torrents_tab = None
    for name in ("_on_main_tab_changed", "_fit_menubar_url_host"):
        setattr(host, name, types.MethodType(getattr(MainWindow, name), host))
    host.main_tabs.currentChanged.connect(host._on_main_tab_changed)

    mb = host.menuBar()
    mb.addMenu("File")
    mb.addMenu("Help")
    host._menubar = mb
    host._menubar_url_host = _MenuUrlHost()
    lay = QHBoxLayout(host._menubar_url_host)
    lay.setContentsMargins(10, 2, 10, 2)
    lay.addWidget(QLineEdit())
    mb.setCornerWidget(host._menubar_url_host, Qt.TopRightCorner)
    # Same initial condition as the build: the app starts on a non-Browse
    # tab, so the corner is swapped out until Browse becomes active.
    mb.setCornerWidget(None, Qt.TopRightCorner)
    host._menubar_url_host.hide()
    host.resize(1200, 800)
    host.show()
    app.processEvents()
    return host


def test_menubar_url_bar_installs_only_on_browse_tab(app):
    host = _make_tab_host(app)
    mb = host.menuBar()
    # Starts on the Agent tab: no corner widget, bar hidden.
    assert mb.cornerWidget(Qt.TopRightCorner) is None
    assert host._menubar_url_host.isHidden()

    host.main_tabs.setCurrentWidget(host._browser_tab)
    app.processEvents()  # the deferred fit runs on the next loop pass
    assert mb.cornerWidget(Qt.TopRightCorner) is host._menubar_url_host
    assert not host._menubar_url_host.isHidden()
    # Corner widgets only ever get their sizeHint from QMenuBar — the fit
    # must set the width explicitly so the bar fills the space right of the
    # menus instead of huddling at the window's right edge (and must not
    # overhang the right edge from a stale pre-fit anchor).
    menus_right = max(mb.actionGeometry(a).right() for a in mb.actions())
    geo = host._menubar_url_host.geometry()
    assert geo.width() >= mb.width() - menus_right - 8
    assert geo.right() <= mb.width() - 1
    assert geo.x() >= menus_right - 8

    # Leaving Browse swaps the corner out again (and hides the container).
    host.main_tabs.setCurrentIndex(0)
    assert mb.cornerWidget(Qt.TopRightCorner) is None
    assert host._menubar_url_host.isHidden()
    # ...and coming back reinstalls it.
    host.main_tabs.setCurrentWidget(host._browser_tab)
    app.processEvents()
    assert mb.cornerWidget(Qt.TopRightCorner) is host._menubar_url_host
    assert not host._menubar_url_host.isHidden()


def test_menubar_url_bar_fit_tracks_width(app):
    host = _make_tab_host(app)
    mb = host.menuBar()
    host.main_tabs.setCurrentWidget(host._browser_tab)
    app.processEvents()  # the deferred fit runs on the next loop pass
    wide = host._menubar_url_host.width()
    assert wide > 400  # 1200px window: the bar really does expand

    host.resize(700, 600)
    app.processEvents()
    host._fit_menubar_url_host()  # the Resize eventFilter path calls exactly this
    narrow = host._menubar_url_host.width()
    assert narrow < wide
    menus_right = max(mb.actionGeometry(a).right() for a in mb.actions())
    assert narrow >= mb.width() - menus_right - 8


# -- tab strip auto-fit --------------------------------------------------------

def _make_strip_host(n_tabs: int = 12) -> "_Host":
    """Bare host carrying exactly what _rebuild_browser_tab_strip touches:
    a real QTabWidget with long-titled tabs plus the production strip
    container (ResponsiveRow floor) and layout."""
    host = _Host()
    host.browser_tabs = QTabWidget(host)
    for i in range(n_tabs):
        title = f"A Reasonably Long Page Title {i}"
        host.browser_tabs.addTab(QWidget(), title)
        host.browser_tabs.setTabToolTip(i, title + " — full")
    host._tab_strip_container = ResponsiveRow(min_width=240)
    host._tab_strip_layout = QHBoxLayout(host._tab_strip_container)
    host._tab_strip_layout.setContentsMargins(4, 0, 0, 2)
    host._tab_strip_layout.setSpacing(2)
    host._tab_strip_layout.addStretch()
    host._browser_close_tab = lambda idx: None
    host._rebuild_browser_tab_strip = types.MethodType(
        MainWindow._rebuild_browser_tab_strip, host)
    return host


def test_tab_strip_never_grows_the_window_minimum(app):
    """Owner report 2026-11-07: many browser tabs ballooned the window to a
    huge minimum width (plain QLabel minimums = full title text). The strip
    container's reported minimum is capped (ResponsiveRow) and every title
    label is an _ElideLabel, so any number of tabs fits the space the strip
    already has."""
    host = _make_strip_host(n_tabs=15)
    host._rebuild_browser_tab_strip()
    app.processEvents()
    assert host._tab_strip_container.minimumSizeHint().width() <= 700

    widgets = [host._tab_strip_layout.itemAt(i).widget()
               for i in range(host._tab_strip_layout.count())]
    chips = [w for w in widgets if w is not None]
    assert len(chips) == 15  # the trailing stretch item is not a widget
    for chip in chips:
        assert chip.maximumWidth() == _BROWSER_TAB_MAX_WIDTH
    labels = [lbl for chip in chips for lbl in chip.findChildren(QLabel)]
    assert len(labels) == 15
    assert all(isinstance(lbl, _ElideLabel) for lbl in labels)
    # Zero-width minimum is the fix itself: titles shrink (elide) instead of
    # demanding their full text width.
    assert all(lbl.minimumSizeHint().width() == 0 for lbl in labels)
    # The full title rides the tooltip so a narrowed tab stays identifiable.
    assert labels[0].toolTip().endswith("— full")


def test_elide_label_elides_instead_of_demanding_width(app):
    """A plain QLabel's minimumSizeHint is its FULL text width (the
    documented window-growth gotcha); _ElideLabel's is zero, its sizeHint
    still wants the whole title, and its overridden paintEvent renders."""
    text = "A Very Long Browser Tab Title That Would Be Huge"
    plain = QLabel(text)
    elide = _ElideLabel(text)
    assert plain.minimumSizeHint().width() > 200
    assert elide.minimumSizeHint().width() == 0
    assert elide.sizeHint().width() > 200  # full width when space exists
    # The paint path (overridden paintEvent) must render without crashing.
    elide.resize(80, 24)
    pix = elide.grab()
    assert not pix.isNull()
    # And the text it draws at that width is the elided form.
    fm = elide.fontMetrics()
    assert fm.elidedText(text, Qt.TextElideMode.ElideRight, 60).endswith("…")


# -- bookmarks popup: right-click to remove (2026-11-07) ----------------------
#
# QMenu swallows right-clicks on plain actions, so the popup is a QMenu
# subclass (_BookmarksPopup) that maps a right press on a registered
# top-level bookmark action to MainWindow._bookmark_context_menu, while
# folder rows (QWidgetAction rows) use CustomContextMenu on the row.
# Synthetic mouse presses to plain QWidgets are reliable offscreen (the
# module-doc caveat is about QtWebEngine's context-menu events).

class _BookmarkHost(QMainWindow):
    """Lightweight host carrying just the attributes the bookmark methods
    touch (same bind-real-methods pattern as the URL-bar host above)."""

    def __init__(self):
        super().__init__()
        self._bookmarks_menu = _BookmarksPopup("Bookmarks", self)
        self._bookmark_context_menu = MagicMock()
        self._bookmark_folder_context_menu = MagicMock()
        self._browser_remove_bookmark = MagicMock()
        self._import_bookmarks = MagicMock()
        self._export_bookmarks = MagicMock()
        self._fill_bookmark_menu = MagicMock()
        self._add_bookmark_menu_item = types.MethodType(
            MainWindow._add_bookmark_menu_item, self)


def _right_press(local_and_global: QPoint) -> QMouseEvent:
    return QMouseEvent(QEvent.Type.MouseButtonPress,
                       QPointF(local_and_global), QPointF(local_and_global),
                       Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
                       Qt.KeyboardModifier.NoModifier)


def test_popup_right_click_on_bookmark_opens_remove_menu(app):
    host = _BookmarkHost()
    menu = host._bookmarks_menu
    menu.addAction("Import Bookmarks...")  # unregistered filler row
    act = menu.addAction("Example")
    menu.bookmark_urls[act] = "https://example.com"
    menu.show()
    app.processEvents()
    center = menu.actionGeometry(act).center()
    QtWidgets.QApplication.sendEvent(menu, _right_press(center))
    host._bookmark_context_menu.assert_called_once()
    url, pos = host._bookmark_context_menu.call_args[0]
    assert url == "https://example.com"
    assert pos == center  # the context menu opens at the cursor
    # The popup STAYS OPEN under the context menu (owner 2026-11-07: it
    # vanishing the moment the remove menu appeared felt like a crash).
    assert not menu.isHidden()


def test_popup_right_click_elsewhere_is_swallowed(app):
    host = _BookmarkHost()
    menu = host._bookmarks_menu
    import_act = menu.addAction("Import Bookmarks...")
    act = menu.addAction("Example")
    menu.bookmark_urls[act] = "https://example.com"
    menu.show()
    app.processEvents()
    QtWidgets.QApplication.sendEvent(
        menu, _right_press(menu.actionGeometry(import_act).center()))
    host._bookmark_context_menu.assert_not_called()
    assert not menu.isHidden()  # stays open — QMenu ignores stray right-clicks


def test_folder_row_right_click_wiring(app):
    host = _BookmarkHost()
    menu = QMenu()
    b = SimpleNamespace(title="Foldered", url="https://foldered.example",
                        folder="News")
    host._add_bookmark_menu_item(menu, b)
    row = menu.actions()[0].defaultWidget()
    assert row.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    row.customContextMenuRequested.emit(QPoint(3, 3))
    host._bookmark_context_menu.assert_called_once()
    url, _pos = host._bookmark_context_menu.call_args[0]
    assert url == "https://foldered.example"


def _popup_context_menu(app, host) -> QMenu:
    """Run the REAL _bookmark_context_menu and return its (non-blocking
    popup) menu — found by its Remove action, no isVisible requirement
    (offscreen popups report visibility unreliably)."""
    MainWindow._bookmark_context_menu(host, "https://example.com", QPoint(10, 10))
    app.processEvents()
    for w in QtWidgets.QApplication.topLevelWidgets():
        if isinstance(w, QMenu) and any(a.text() == "Remove bookmark"
                                        for a in w.actions()):
            return w
    pytest.fail("context menu never appeared")


def test_context_menu_remove_deletes_bookmark(app):
    host = _BookmarkHost()
    host._bookmarks_menu.show()  # popup is open when the context menu fires
    menu = _popup_context_menu(app, host)
    for a in menu.actions():
        if a.text() == "Remove bookmark":
            a.trigger()  # synchronous direct connection; menu self-deletes
            break
    host._browser_remove_bookmark.assert_called_once_with("https://example.com")
    # The bookmarks popup itself is NOT closed by the removal anymore.
    assert not host._bookmarks_menu.isHidden()


def test_context_menu_cancel_keeps_bookmark(app):
    host = _BookmarkHost()
    menu = _popup_context_menu(app, host)
    menu.close()  # dismiss without picking anything
    host._browser_remove_bookmark.assert_not_called()


def test_rebuild_registers_top_level_bookmarks(app):
    host = _BookmarkHost()
    host.config = SimpleNamespace(browser=SimpleNamespace(bookmarks=[
        SimpleNamespace(title="Example", url="https://example.com", folder=""),
        SimpleNamespace(title="Foldered", url="https://foldered.example",
                        folder="News"),
    ]))
    MainWindow._rebuild_bookmarks_bar(host)
    menu = host._bookmarks_menu
    # Folder items live in QWidgetAction rows (own right-click wiring);
    # only the top-level plain actions are registered on the popup.
    assert set(menu.bookmark_urls.values()) == {"https://example.com"}
    act = next(a for a, u in menu.bookmark_urls.items()
               if u == "https://example.com")
    assert act.text() == "Example"


# -- folder right-click: delete depends on what is under the cursor -----------

def test_popup_right_click_on_folder_title_opens_remove_menu(app):
    host = _BookmarkHost()
    menu = host._bookmarks_menu
    sub = _BookmarksPopup("News", menu)
    menu.addMenu(sub)
    menu.folder_paths[sub.menuAction()] = "News"
    menu.show()
    app.processEvents()
    center = menu.actionGeometry(sub.menuAction()).center()
    QtWidgets.QApplication.sendEvent(menu, _right_press(center))
    host._bookmark_folder_context_menu.assert_called_once()
    folder, pos = host._bookmark_folder_context_menu.call_args[0]
    assert folder == "News"
    assert pos == center
    assert not menu.isHidden()  # stays open under the context menu


def test_nested_submenu_right_click_reaches_main_window(app):
    """A SUBMENU's QObject parent is its parent MENU, not the window —
    dispatch must climb the chain to find the handler owner (regression:
    the first version called parent() once and AttributeError'd, so
    right-click on a nested folder title silently did nothing)."""
    host = _BookmarkHost()
    menu = host._bookmarks_menu          # parent = host (has handlers)
    news = _BookmarksPopup("News", menu)  # parent = menu (NO handlers)
    menu.addMenu(news)
    tech = _BookmarksPopup("Tech", news)  # nested: parent = news menu
    news.addMenu(tech)
    news.folder_paths[tech.menuAction()] = "News/Tech"
    news.show()
    app.processEvents()
    center = news.actionGeometry(tech.menuAction()).center()
    QtWidgets.QApplication.sendEvent(news, _right_press(center))
    host._bookmark_folder_context_menu.assert_called_once()
    folder, _pos = host._bookmark_folder_context_menu.call_args[0]
    assert folder == "News/Tech"


def test_rebuild_registers_folder_paths_nested(app):
    host = _BookmarkHost()
    host._fill_bookmark_menu = types.MethodType(
        MainWindow._fill_bookmark_menu, host)
    host.config = SimpleNamespace(browser=SimpleNamespace(bookmarks=[
        SimpleNamespace(title="Top", url="https://top.example", folder=""),
        SimpleNamespace(title="Deep", url="https://deep.example",
                        folder="News/Tech"),
    ]))
    MainWindow._rebuild_bookmarks_bar(host)
    menu = host._bookmarks_menu
    assert set(menu.folder_paths.values()) == {"News"}
    sub = next(a for a, f in menu.folder_paths.items()
               if f == "News").menu()
    assert isinstance(sub, _BookmarksPopup)
    # Nested folder registers its FULL path on its own submenu.
    assert set(sub.folder_paths.values()) == {"News/Tech"}


def _folder_host() -> _BookmarkHost:
    host = _BookmarkHost()
    host._save_config = MagicMock()
    host._rebuild_bookmarks_bar = MagicMock()
    host.config = SimpleNamespace(browser=SimpleNamespace(bookmarks=[
        SimpleNamespace(title="A", url="https://a.example", folder="News"),
        SimpleNamespace(title="B", url="https://b.example", folder="News/Tech"),
        SimpleNamespace(title="C", url="https://c.example", folder="Other"),
        SimpleNamespace(title="D", url="https://d.example", folder=""),
    ]))
    return host


def _popup_folder_context_menu(app, host, folder) -> QMenu:
    """Run the REAL _bookmark_folder_context_menu and return its popup."""
    MainWindow._bookmark_folder_context_menu(host, folder, QPoint(10, 10))
    app.processEvents()
    for w in QtWidgets.QApplication.topLevelWidgets():
        if isinstance(w, QMenu) and any(a.text().startswith("Remove folder")
                                        for a in w.actions()):
            return w
    pytest.fail("folder context menu never appeared")


def test_folder_context_menu_removes_whole_folder(app):
    host = _folder_host()
    host._bookmarks_menu.show()  # popup is open when the context menu fires
    menu = _popup_folder_context_menu(app, host, "News")
    for a in menu.actions():
        if a.text().startswith("Remove folder"):
            a.trigger()  # synchronous direct connection; menu self-deletes
            break
    # "News" AND its nested "News/Tech" go together; everything else stays.
    urls = {b.url for b in host.config.browser.bookmarks}
    assert urls == {"https://c.example", "https://d.example"}
    host._save_config.assert_called_once()
    host._rebuild_bookmarks_bar.assert_called_once()
    # The bookmarks popup itself is NOT closed by the removal anymore.
    assert not host._bookmarks_menu.isHidden()


def test_remove_updates_open_menu_in_place(app):
    """Full user flow: popup open → right-click a bookmark → Remove → the
    popup STAYS OPEN and the entry vanishes from it (rebuild-in-place;
    owner 2026-11-07: the popup vanishing with the remove menu felt like
    a crash)."""
    host = _BookmarkHost()
    host._save_config = MagicMock()
    host._bookmark_context_menu = types.MethodType(
        MainWindow._bookmark_context_menu, host)
    host._browser_remove_bookmark = types.MethodType(
        MainWindow._browser_remove_bookmark, host)
    host._rebuild_bookmarks_bar = types.MethodType(
        MainWindow._rebuild_bookmarks_bar, host)
    host._fill_bookmark_menu = types.MethodType(
        MainWindow._fill_bookmark_menu, host)
    host.config = SimpleNamespace(browser=SimpleNamespace(bookmarks=[
        SimpleNamespace(title="Example", url="https://example.com", folder=""),
        SimpleNamespace(title="Keep", url="https://keep.example", folder=""),
    ]))
    MainWindow._rebuild_bookmarks_bar(host)
    menu = host._bookmarks_menu
    menu.show()
    app.processEvents()
    act = next(a for a, u in menu.bookmark_urls.items()
               if u == "https://example.com")
    # Snapshot first: only a menu CREATED by this right-press counts (a
    # prior test's WA_DeleteOnClose menu may still be mid-deletion).
    before = set(QtWidgets.QApplication.topLevelWidgets())
    QtWidgets.QApplication.sendEvent(
        menu, _right_press(menu.actionGeometry(act).center()))
    ctx = None
    for w in QtWidgets.QApplication.topLevelWidgets():
        if (w not in before and isinstance(w, QMenu)
                and any(a.text() == "Remove bookmark" for a in w.actions())):
            ctx = w
            break
    assert ctx is not None
    for a in ctx.actions():
        if a.text() == "Remove bookmark":
            a.trigger()
            break
    app.processEvents()
    assert not menu.isHidden()  # still open
    assert set(menu.bookmark_urls.values()) == {"https://keep.example"}
    assert all(a.text() != "Example" for a in menu.actions())


def test_folder_context_menu_cancel_keeps_bookmarks(app):
    host = _folder_host()
    menu = _popup_folder_context_menu(app, host, "News")
    menu.close()
    assert len(host.config.browser.bookmarks) == 4
    host._save_config.assert_not_called()
