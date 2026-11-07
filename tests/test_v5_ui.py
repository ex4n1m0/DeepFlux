"""v5/v7.6 UI surface: menu-bar tab icons, agent quick-ask panel, taskbar
progress.

The v5 left activity rail was REMOVED in 7.6 (owner decision 2026-11-07) —
its tab icons live in the menu bar left of File. All offscreen; the icon
helpers are MainWindow methods bound onto a bare host (the real
MainWindow is too heavy to build here). Qt is bootstrapped the same way
the other GUI tests do it (a module-scoped QApplication — no pytest-qt).
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QTabWidget, QWidget  # noqa: E402

from gui.main_window import MainWindow, _TAB_ICONS  # noqa: E402
from gui.agent_panel import AgentPanel  # noqa: E402
from gui.win_taskbar import TaskbarProgress  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _tabs(parent) -> QTabWidget:
    tabs = QTabWidget(parent)
    tabs.addTab(QWidget(), "Agent")
    tabs.addTab(QWidget(), "Browse")
    tabs.addTab(QWidget(), "Download")
    tabs.addTab(QWidget(), "Play")
    tabs.addTab(QWidget(), "Command")
    tabs.addTab(QWidget(), "Room")
    return tabs


def _icon_host(qapp):
    """Bare host carrying the two v7.6 icon helpers (bound from the real
    MainWindow) + a QTabWidget shaped like production."""
    import types
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QToolButton
    host = QWidget()
    host.main_tabs = _tabs(host)
    host._tab_icon_actions = []
    host._download_tab_action = None
    for i in range(host.main_tabs.count()):
        act = QToolButton(host)
        act.setCheckable(True)
        act.setToolButtonStyle(Qt.ToolButtonIconOnly)
        host._tab_icon_actions.append(act)
        if host.main_tabs.tabText(i) == "Download":
            host._download_tab_action = act
    for name in ("_sync_tab_icon_actions", "_set_download_badge"):
        setattr(host, name, types.MethodType(getattr(MainWindow, name), host))
    host._sync_tab_icon_actions()
    return host


def test_tab_icons_map_covers_all_pages(qapp):
    assert set(_TAB_ICONS) == {"Agent", "Browse", "Download", "Play",
                               "Command", "Room"}


def test_tab_icon_sync_follows_current_tab(qapp):
    host = _icon_host(qapp)
    checked = [a.isChecked() for a in host._tab_icon_actions]
    assert checked[0] is True and sum(checked) == 1
    host.main_tabs.setCurrentIndex(3)  # Play
    host._sync_tab_icon_actions()
    checked = [a.isChecked() for a in host._tab_icon_actions]
    assert checked[3] is True and sum(checked) == 1


def test_tab_icon_sync_tolerates_empty_host(qapp):
    # Fires during _build_ui's insertTab calls before the icons exist.
    import types
    bare = QWidget()
    bare._sync_tab_icon_actions = types.MethodType(
        MainWindow._sync_tab_icon_actions, bare)
    bare._sync_tab_icon_actions()  # must not raise


def test_download_badge_sets_and_clears(qapp):
    from PySide6.QtCore import Qt
    host = _icon_host(qapp)
    host._set_download_badge(3)
    assert host._download_tab_action.text() == "3"
    assert (host._download_tab_action.toolButtonStyle()
            == Qt.ToolButtonTextBesideIcon)
    host._set_download_badge(0)
    assert host._download_tab_action.text() == ""
    assert (host._download_tab_action.toolButtonStyle()
            == Qt.ToolButtonIconOnly)


def test_tab_icon_pixmap_paints_all_kinds(qapp):
    from gui.main_window import _tab_icon_pixmap
    for kind in list(_TAB_ICONS.values()) + ["generic", "unknown-page"]:
        pm = _tab_icon_pixmap(kind)
        assert not pm.isNull() and pm.width() == 32  # 16px @ 2x DPR


def test_agent_panel_submit_signal(qapp):
    panel = AgentPanel()
    received = []
    panel.submit_requested.connect(received.append)
    panel.input.setText("what is downloading?")
    panel._submit()
    assert received == ["what is downloading?"]
    assert panel.input.text() == ""  # cleared after submit


def test_agent_panel_mirrors_and_trims(qapp):
    from gui.agent_panel import _MAX_BLOCKS
    panel = AgentPanel()
    for i in range(80):
        panel.append_exchange("agent", f"block {i}")
    # Hard cap keeps the tail bounded, not the full history.
    assert panel._blocks <= _MAX_BLOCKS
    assert "block 79" in panel._view.toPlainText()
    # The oldest blocks were trimmed away.
    assert "block 0\n" not in panel._view.toPlainText()
    panel.set_busy(True)
    assert panel._busy_lbl.text() != ""
    panel.set_busy(False)
    assert panel._busy_lbl.text() == ""


def test_taskbar_progress_inert_offscreen():
    # Offscreen has no HWND; the helper must construct + no-op without raising.
    tb = TaskbarProgress(0)
    tb.set_progress(10, 100)
    tb.set_indeterminate()
    tb.clear()


def _hub_entries():
    calls = []
    return [
        {"category": "AI & API Keys", "title": "AI Agent Key",
         "description": "LLM provider key", "open": lambda: calls.append("ai")},
        {"category": "Downloads & Sources", "title": "Jackett Indexer Settings",
         "description": "Service URL and key", "open": lambda: calls.append("jkt")},
        {"category": "Play", "title": "IPTV Playlist Sources",
         "description": "M3U/Xtream playlists", "open": lambda: calls.append("iptv")},
    ], calls


def test_settings_hub_groups_and_opens(qapp):
    from gui.settings_hub import SettingsHub
    entries, calls = _hub_entries()
    hub = SettingsHub(entries)
    # 3 entry rows + 3 category headers
    assert hub._list.count() == 6
    # First selectable row preselected; Open fires the right callback.
    hub._open_selected()
    assert calls == ["ai"]


def test_settings_hub_search_filters(qapp):
    from gui.settings_hub import SettingsHub
    entries, calls = _hub_entries()
    hub = SettingsHub(entries)
    hub._populate("jackett")
    texts = [hub._list.item(i).text() for i in range(hub._list.count())]
    assert texts == ["Downloads & Sources", "Jackett Indexer Settings"]
    hub._open_selected()
    assert calls == ["jkt"]
    hub._populate("")
    assert hub._list.count() == 6
