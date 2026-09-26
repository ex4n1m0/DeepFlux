"""Left activity rail — DeepFlux 5.0 navigation.

Replaces the 3.5-era six page-title buttons on the menu bar (menu items that
were really tab switches — a platform-contract violation) with an explicit
vertical mode picker beside the content, in the style of VS Code / Discord:

  * icon + small label per page, tooltip carries the Ctrl+N shortcut so the
    shortcuts finally become discoverable;
  * badge slot per page (active download count, unread chat, agent pulse) so
    global state is visible from ANY page, not only inside the tab;
  * Settings pinned at the bottom.

The rail never touches pages itself — it reads the QTabWidget's titles and
emits ``page_requested(index)``; MainWindow wires that to setCurrentIndex,
exactly like the old menu actions did.
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QSizePolicy
from gui.i18n import tr

# Page title (QTabWidget text) -> (icon, rail key). Unknown titles get a
# generic diamond so a future tab still shows up on the rail.
_PAGE_ICONS: Dict[str, str] = {
    "Agent": "🧠",
    "Browse": "🌐",
    "Download": "⬇",
    "Play": "▶",
    "Command": "🗂",
    "Room": "💬",
}


class _RailButton(QFrame):
    """One rail item: icon over label, active/hover via QSS properties."""

    clicked = Signal()

    def __init__(self, icon: str, label: str, shortcut: str, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("railBtn", True)
        self.setProperty("active", False)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"{label}  ({shortcut})" if shortcut else label)
        self._shortcut = shortcut

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(4, 7, 4, 6)
        vbox.setSpacing(2)
        self._icon = QLabel(icon)
        self._icon.setProperty("railIcon", True)
        self._icon.setAlignment(Qt.AlignCenter)
        vbox.addWidget(self._icon)
        self._text = QLabel(label)
        self._text.setProperty("railText", True)
        self._text.setAlignment(Qt.AlignCenter)
        vbox.addWidget(self._text)

        # Corner badge (top-right) — hidden until set_badge puts text in it.
        self._badge = QLabel("", self)
        self._badge.setProperty("railBadge", True)
        self._badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._badge.hide()

    def resizeEvent(self, event) -> None:  # noqa: N102
        super().resizeEvent(event)
        self._badge.adjustSize()
        self._badge.move(self.width() - self._badge.width() - 1, 1)

    def mousePressEvent(self, event) -> None:  # noqa: N102
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def set_active(self, on: bool) -> None:
        if bool(self.property("active")) == on:
            return
        self.setProperty("active", on)
        self._repolish()

    def set_badge(self, text: str) -> None:
        if text:
            self._badge.setText(text)
            self._badge.adjustSize()
            self._badge.show()
        else:
            self._badge.hide()

    def _repolish(self) -> None:
        # Property-selector QSS only re-evaluates after a (un)polish round-trip.
        for w in (self, self._icon, self._text):
            w.style().unpolish(w)
            w.style().polish(w)


class ActivityRail(QFrame):
    """Vertical page picker for the main QTabWidget + a Settings pin."""

    page_requested = Signal(int)
    settings_requested = Signal()

    def __init__(self, tabs, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("activity_rail")
        self.setFixedWidth(84)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        self._buttons: Dict[str, _RailButton] = {}
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(6, 8, 6, 8)
        vbox.setSpacing(4)

        for i in range(tabs.count()):
            title = tabs.tabText(i)
            # Tab titles are tr()-translated but _PAGE_ICONS keys are the
            # English sources — match through tr(), or every zh tab falls
            # back to the diamond glyph (found by the 2026-09-25 site-shot
            # capture: the whole zh rail rendered as identical diamonds).
            icon = next((v for k, v in _PAGE_ICONS.items() if tr(k) == title),
                        "◈")
            btn = _RailButton(icon, title, f"Ctrl+{i + 1}", self)
            btn.clicked.connect(
                lambda _=False, idx=i: self.page_requested.emit(idx)
            )
            vbox.addWidget(btn)
            self._buttons[title] = btn
        vbox.addStretch(1)

        settings = _RailButton("⚙", tr("Settings"), "Ctrl+,", self)
        settings.clicked.connect(lambda _=False: self.settings_requested.emit())
        vbox.addWidget(settings)
        self._settings_btn = settings

        tabs.currentChanged.connect(lambda idx, *_: self.set_active(idx))
        self.set_active(tabs.currentIndex())

    # -- public API ---------------------------------------------------------
    def set_active(self, index: int) -> None:
        btn = self._button_at(index)
        if btn is not None:
            btn.set_active(True)
        for other in self._buttons.values():
            if other is not btn:
                other.set_active(False)

    def set_badge(self, page_title: str, text: str) -> None:
        """Show a small counter on a page's rail item ("" clears it)."""
        btn = self._buttons.get(page_title)
        if btn is not None:
            btn.set_badge(text)

    # -- internals ----------------------------------------------------------
    def _button_at(self, index: int) -> Optional[_RailButton]:
        # _buttons preserves tab insertion order (dicts are ordered).
        titles = list(self._buttons)
        if 0 <= index < len(titles):
            return self._buttons[titles[index]]
        return None
