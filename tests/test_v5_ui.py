"""v5 UI surface: activity rail, agent quick-ask panel, taskbar progress.

All offscreen; no MainWindow needed for the rail/panel (they take any parent),
and TaskbarProgress must be an inert no-op wherever COM is unavailable. Qt is
bootstrapped the same way the other GUI tests do it (a module-scoped
QApplication — no pytest-qt in this repo).
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QTabWidget, QWidget  # noqa: E402

from gui.activity_rail import ActivityRail  # noqa: E402
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


def test_rail_builds_one_button_per_tab(qapp):
    parent = QWidget()
    tabs = _tabs(parent)
    rail = ActivityRail(tabs, parent)
    assert list(rail._buttons) == ["Agent", "Browse", "Download", "Play", "Command", "Room"]


def test_rail_syncs_active_with_tabs(qapp):
    parent = QWidget()
    tabs = _tabs(parent)
    rail = ActivityRail(tabs, parent)
    tabs.setCurrentIndex(3)
    assert rail._buttons["Play"].property("active") is True
    assert rail._buttons["Agent"].property("active") is False
    rail.set_active(0)
    assert rail._buttons["Agent"].property("active") is True
    assert rail._buttons["Play"].property("active") is False


def test_rail_page_requested_signal(qapp):
    parent = QWidget()
    rail = ActivityRail(_tabs(parent), parent)
    received = []
    rail.page_requested.connect(received.append)
    rail._buttons["Download"].clicked.emit()
    rail._settings_btn.clicked.emit()  # settings pin, not a page
    assert received == [2]


def test_rail_settings_pin_signal(qapp):
    parent = QWidget()
    rail = ActivityRail(_tabs(parent), parent)
    received = []
    rail.settings_requested.connect(lambda: received.append(True))
    rail._settings_btn.clicked.emit()
    assert received == [True]


def test_rail_badge_show_hide(qapp):
    parent = QWidget()
    rail = ActivityRail(_tabs(parent), parent)
    rail.set_badge("Download", "3")
    assert rail._buttons["Download"]._badge.text() == "3"
    rail.set_badge("Download", "")
    assert rail._buttons["Download"]._badge.isHidden()
    # Unknown page title is a no-op, not a crash.
    rail.set_badge("Nope", "9")


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
