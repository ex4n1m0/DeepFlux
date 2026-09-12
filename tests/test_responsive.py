"""Responsive window sizing (gui/responsive.py) + dynamic poster columns.

The window's minimum width is the widest page's layout minimum. Before
these helpers, one long toolbar or a dynamic status label locked the
whole window (measured 2026-09-12: IPTV toolbar 1138px, player controls
961px, Commander 1236px, IRC 1232px, Downloads 1034px).
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

import pytest  # noqa: E402


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _row_with_buttons(count):
    """The responsive row inside a plain container — a bare top-level row
    can never shrink below its own minimum, so nothing would ever hide."""
    from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget
    from gui.responsive import OverflowRow, ResponsiveRow
    host = QWidget()
    row = ResponsiveRow()
    QVBoxLayout(host).addWidget(row)
    lay = QHBoxLayout(row)
    buttons = [QPushButton(f"Btn {i}") for i in range(count)]
    for b in buttons:
        lay.addWidget(b)
    return host, row, buttons


def test_overflow_row_hides_and_restores():
    app = _app()
    from gui.responsive import OverflowRow
    host, row, buttons = _row_with_buttons(6)
    overflow = OverflowRow(row, buttons[:5])
    host.resize(1400, 40)
    host.show()
    app.processEvents()
    app.processEvents()
    # Wide: everything on the row, no menu button.
    assert overflow.hidden_widgets() == []
    assert overflow.button.isHidden()

    # Narrow: candidates disappear in order until the row fits, and the
    # "..." button takes their place.
    host.resize(300, 40)
    app.processEvents()
    hidden = overflow.hidden_widgets()
    assert 0 < len(hidden) < 6, hidden
    assert hidden == buttons[:len(hidden)]
    assert not overflow.button.isHidden()
    assert row.layout().minimumSize().width() <= row.width() + 1

    # Wide again: everything returns.
    host.resize(1400, 40)
    app.processEvents()
    assert overflow.hidden_widgets() == []
    assert overflow.button.isHidden()
    for b in buttons:
        assert not b.isHidden()
    host.deleteLater()
    app.processEvents()


def test_overflow_menu_still_triggers_hidden_button():
    app = _app()
    from gui.responsive import OverflowRow
    host, row, buttons = _row_with_buttons(6)
    overflow = OverflowRow(row, buttons[:5])
    host.resize(300, 40)
    host.show()
    app.processEvents()
    target = overflow.hidden_widgets()[0]
    assert target in buttons

    clicked = []
    target.clicked.connect(lambda: clicked.append(True))
    # The menu is rebuilt right before it opens; simulate that and click.
    overflow._rebuild_menu()
    actions = overflow._menu.actions()
    assert actions
    actions[0].trigger()
    assert clicked == [True]
    host.deleteLater()
    app.processEvents()


def test_responsive_row_caps_reported_minimum():
    app = _app()
    from gui.responsive import ResponsiveRow
    host, row, _buttons = _row_with_buttons(6)
    row.set_row_min_width(300)
    assert row.minimumSizeHint().width() <= 300
    host.deleteLater()
    app.processEvents()


def test_shrink_label_stops_status_text_growing_layout():
    app = _app()
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget
    from gui.responsive import shrink_label
    text = "15 selected | 245.3 GB free | 4 dirs, 11 files (2.4 GiB) | filtered: 3"

    def row_min(label):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.addWidget(QPushButton("B"))
        lay.addWidget(label, 1)
        m = lay.minimumSize().width()
        w.deleteLater()
        return m

    plain = QLabel(text)
    shrunk = QLabel(text)
    shrink_label(shrunk)
    assert row_min(shrunk) < row_min(plain) // 2
    app.processEvents()


def test_grid_columns_dynamic_3_to_9():
    """Poster columns adapt to the grid width: never fewer than 3, never
    more than 9, tiles fill the width exactly."""
    app = _app()
    from iptv.manager import IPTVManager
    from iptv.models import SECTION_LIVE, Channel, make_id
    from gui.iptv_tab import ContentGrid

    mgr = IPTVManager(sources=[], data_dir=None)
    grid = ContentGrid(mgr)
    grid.set_items([
        Channel(id=make_id("a", str(i)), name=f"Chan {i}", url=f"http://x/{i}",
                group="g", section=SECTION_LIVE)
        for i in range(60)
    ])
    grid.show()
    app.processEvents()

    try:
        for width in (500, 900, 1400, 2000, 2600, 3400):
            grid.resize(width, 800)
            app.processEvents()
            app.processEvents()
            rects = [grid.visualItemRect(grid.item(i)) for i in range(60)]
            top = min(r.top() for r in rects)
            cols = len({r.left() for r in rects if r.top() == top})
            icon_w = grid.iconSize().width()
            if width <= 900:
                assert cols == 3, (width, cols)
            elif width >= 2600:
                assert cols == 9, (width, cols)
            else:
                assert 3 < cols < 9, (width, cols)
            # Tiles fill the width: each column is ~viewport/cols wide.
            assert 120 <= icon_w <= 420, (width, icon_w)
    finally:
        mgr.shutdown()
        grid.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("tab_name", ["iptv", "commander", "irc", "downloads"])
def test_tabs_stay_shrinkable(tmp_path, tab_name):
    """Regression cap: no tab page may demand a wide window again — the
    window's minimum is the widest page's minimum (1138-1236px before)."""
    app = _app()

    def build():
        if tab_name == "iptv":
            from config import DeeptorrentConfig, IPTVSourceConfig
            from gui import iptv_tab as tab_mod
            cfg = DeeptorrentConfig()
            cfg.iptv.cache_dir = str(tmp_path)
            cfg.iptv.sources = [IPTVSourceConfig(id="a", name="A", kind="m3u_url",
                                                 url="http://a/p.m3u")]
            with mock.patch.object(tab_mod.IPTVTab, "_refresh", lambda self: None):
                return tab_mod.IPTVTab(cfg), lambda t: t.shutdown()
        if tab_name == "commander":
            from gui.commander_tab import CommanderTab
            return CommanderTab(SimpleNamespace(default_save_path=str(tmp_path))), None
        if tab_name == "irc":
            from config import DeeptorrentConfig, IRCNetworkConfig
            from ircmgr.client import IRCClientCore
            from gui.irc_tab import IRCTab
            cfg = DeeptorrentConfig()
            cfg.irc.networks = [IRCNetworkConfig(id="a", host="irc.a.net", nick="u")]
            client = IRCClientCore(cfg.irc)
            return IRCTab(cfg, client), lambda t: (t.shutdown(), client.shutdown())
        from PySide6.QtCore import QObject, Signal
        from config import DeeptorrentConfig
        from gui.downloads_tab import DownloadsTab

        class FakeEngine(QObject):
            job_updated = Signal(str)
            jobs_changed = Signal()

            def list_jobs(self):
                return []

            def get_job(self, _jid):
                return None

        cfg = DeeptorrentConfig()
        cfg.download.default_folder = str(tmp_path)
        return DownloadsTab(FakeEngine(), cfg), None

    tab, teardown = build()
    tab.resize(1400, 900)
    tab.show()
    app.processEvents()
    try:
        msh = tab.minimumSizeHint()
        assert msh.width() <= 700, f"{tab_name} tab demands {msh.width()}px width"
        assert msh.height() <= 460, f"{tab_name} tab demands {msh.height()}px height"
        # Long live status text must not grow the minimum ("sometimes I
        # can't make the window smaller"). Stress the labels the app
        # actually mutates at runtime.
        long_text = "0123456789 " * 12
        for lbl_name in ("_status_lbl", "_art_lbl",           # IPTV status row
                         "summary_label",                     # Downloads
                         "status_label", "topic_label",       # IRC
                         "_status", "_outcome_status"):       # Commander
            lbl = getattr(tab, lbl_name, None)
            if lbl is not None:
                lbl.setText(long_text)
        from gui.commander_tab import FilePane
        for pane in tab.findChildren(FilePane):
            pane._pane_status.setText(long_text)
        app.processEvents()
        assert tab.minimumSizeHint().width() <= 700, (
            f"{tab_name} tab minimum grows with status text")
    finally:
        if teardown is not None:
            teardown(tab)
        tab.deleteLater()
        app.processEvents()
