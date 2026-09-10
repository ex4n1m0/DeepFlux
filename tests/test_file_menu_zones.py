"""File-menu zones (2026-09-10): separator line + bold disabled header per
zone, small indent on zone items. Guards the two invariants that made zones
visible: headers must render (addSection() text does NOT under the app
stylesheet) and items must be indented under their header."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMenu

from gui.main_window import _FILE_ITEM_INDENT, _file_item, _file_zone

app = QApplication.instance() or QApplication([])


def _visible_texts(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator() and a.isVisible()]


def test_zone_header_is_bold_disabled_and_flush_left():
    menu = QMenu()
    _file_zone(menu, "Download", first=True)
    header = menu.actions()[-1]
    assert header.text() == "Download"
    assert not header.isEnabled()
    assert header.font().bold()
    assert not header.text().startswith(" ")


def test_zone_separator_skipped_only_for_first():
    menu = QMenu()
    _file_zone(menu, "API Keys", first=True)
    assert not any(a.isSeparator() for a in menu.actions())
    _file_zone(menu, "Browser Settings")
    assert sum(1 for a in menu.actions() if a.isSeparator()) == 1


def test_zone_items_are_indented_exit_is_not():
    menu = QMenu()
    _file_zone(menu, "Download", first=True)
    _file_item(menu, "Add Magnet Link...")
    _file_item(menu, "Exit", indent=False)
    texts = _visible_texts(menu)
    assert texts[0] == "Download"
    assert texts[1] == _FILE_ITEM_INDENT + "Add Magnet Link..."
    assert texts[-1] == "Exit"
