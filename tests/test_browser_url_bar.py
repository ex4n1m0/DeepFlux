"""Browser address bar: typed text must survive the history completer's
popup closing and page URL changes in the current tab.

The real MainWindow address-bar methods are bound to a lightweight host
(mirroring test_browser_zoom). The completer popup is a real Qt::Popup, so
these tests pump the (offscreen) event loop with QTest — the bug being
pinned is Qt sending FocusIn(PopupFocusReason) when that popup closes.
"""
from __future__ import annotations

import os
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QObject, QStringListModel, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCompleter, QHBoxLayout, QLabel, QLineEdit, QTabWidget, QWidget
from PySide6.QtWebEngineWidgets import QWebEngineView

from config import DeeptorrentConfig
from gui.main_window import MainWindow

_BOUND = (
    "_url_bar_event",
    "_url_bar_editing",
    "_browser_reset_url_bar",
    "_browser_url_changed",
    "_browser_navigate",
    "_browser_load_finished",
    "_browser_set_tab_loading",
    "_browser_update_history_suggestions",
    "_current_browser_view",
)


class _Host(QObject):
    """Bare attribute host; its eventFilter delegates to the real policy."""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.browser_url_bar:
            return bool(self._url_bar_event(event))
        return False


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def host(app):
    window = QWidget()
    layout = QHBoxLayout(window)
    tabs = QTabWidget()
    view = QWebEngineView()
    tabs.addTab(view, "tab")
    bar = QLineEdit()
    layout.addWidget(bar)
    layout.addWidget(tabs)

    h = _Host()
    h.browser_tabs = tabs
    h.browser_url_bar = bar
    h.browser_zoom_label = QLabel("100%")
    h.browser_progress = QLabel()
    h.config = DeeptorrentConfig()
    h._browser_history_model = QStringListModel(
        ["https://youtube.com/", "https://young.example/"], h)
    completer = QCompleter(h._browser_history_model, h)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    completer.setFilterMode(Qt.MatchContains)
    bar.setCompleter(completer)
    h._browser_history_completer = completer
    # Recorded visits + status messages are irrelevant here; keep the
    # suggestion model static so the popup behaviour is deterministic.
    h._browser_history = types.SimpleNamespace(
        record=lambda *a, **k: None,
        suggestions=lambda text, limit: [{"url": u} for u in h._browser_history_model.stringList()])
    h._schedule_browser_session_save = lambda: None
    h._browser_update_nav_buttons = lambda: None
    h._rebuild_browser_tab_strip = lambda: None
    h._browser_status_message = lambda msg: None
    h._browser_origin = MainWindow._browser_origin
    h._display_url = MainWindow._display_url
    h.sender = lambda: view
    for name in _BOUND:
        setattr(h, name, types.MethodType(getattr(MainWindow, name), h))
    bar.returnPressed.connect(h._browser_navigate)
    bar.installEventFilter(h)
    h.window, h.view = window, view
    window.show()
    bar.setFocus()
    QTest.qWait(20)
    yield h
    # Tear down in order: detach the filter/completer before the widgets go,
    # or Qt keeps calling the filter on a host whose Python attrs are gone.
    bar.removeEventFilter(h)
    bar.setCompleter(None)
    window.close()
    window.deleteLater()
    QTest.qWait(10)


def _type(bar: QLineEdit, text: str) -> None:
    for ch in text:
        QTest.keyClick(bar, ch)
        QTest.qWait(15)


def test_completer_popup_closing_keeps_typed_text(host):
    bar, popup = host.browser_url_bar, host._browser_history_completer.popup()
    _type(bar, "you")            # matches history → popup opens
    assert popup.isVisible()
    assert host._url_bar_editing()   # the popup doesn't steal focus from the bar
    _type(bar, "x")              # no match → popup closes → FocusIn(PopupFocusReason)
    QTest.qWait(30)
    assert not popup.isVisible()
    # Regression: the select-all-on-focus filter used to fire here, so the
    # next keystrokes replaced everything typed so far ("yz" instead of "youxyz").
    assert bar.selectedText() == ""
    _type(bar, "yz")
    assert bar.text() == "youxyz"


def test_focus_by_mouse_or_tab_still_selects_all(host):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QFocusEvent
    bar = host.browser_url_bar
    bar.setText("https://example.invalid/page")
    assert bar.selectedText() == ""
    host._url_bar_event(QFocusEvent(QEvent.FocusIn, Qt.MouseFocusReason))
    QTest.qWait(10)
    assert bar.selectedText() == bar.text()
    bar.deselect()
    host._url_bar_event(QFocusEvent(QEvent.FocusIn, Qt.ActiveWindowFocusReason))
    QTest.qWait(10)
    assert bar.selectedText() == ""


def test_page_url_change_does_not_clobber_text_being_typed(host):
    bar = host.browser_url_bar
    bar.setText("https://old.example/")
    _type(bar, "abc")            # user is editing: focused + modified
    assert host._url_bar_editing()
    host._browser_url_changed(QUrl("https://redirect.example/landing"))
    assert bar.text().endswith("abc")
    # ...but once the edit is dropped (Esc) or submitted, the bar follows again.
    QTest.keyClick(bar, Qt.Key_Escape)
    assert not bar.isModified()
    host._browser_url_changed(QUrl("https://redirect.example/landing"))
    assert bar.text() == "https://redirect.example/landing"


def test_submitting_lets_the_bar_follow_the_loaded_url(host):
    bar = host.browser_url_bar
    bar.clear()
    _type(bar, "example.invalid")
    assert bar.isModified()
    QTest.keyClick(bar, Qt.Key_Return)   # _browser_navigate
    assert not bar.isModified()
    assert not host._url_bar_editing()
    host._browser_url_changed(QUrl("https://example.invalid/"))
    assert bar.text() == "https://example.invalid/"


def test_unfocused_bar_follows_the_page(host):
    bar = host.browser_url_bar
    _type(bar, "draft")
    host.window.setFocus()       # user clicked into the page
    QTest.qWait(10)
    assert not host._url_bar_editing()
    host._browser_url_changed(QUrl("https://clicked.example/"))
    assert bar.text() == "https://clicked.example/"


def test_load_finished_does_not_reset_suggestions_mid_typing(host):
    bar = host.browser_url_bar
    calls = []
    host._browser_update_history_suggestions = lambda text: calls.append(text)
    _type(bar, "yo")
    host._browser_load_finished(host.view, True)
    assert calls == []                      # editing → left alone
    QTest.keyClick(bar, Qt.Key_Escape)
    host._browser_load_finished(host.view, True)
    assert calls == [""]                    # idle → refreshed as before
