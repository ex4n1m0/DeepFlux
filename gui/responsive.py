"""Responsive shrinking for wide widget rows.

A top-level window's minimum width is the widest page's layout minimum,
so ONE long toolbar or status label locks the whole window (measured
2026-09-12: the Play tab's toolbar alone forced 1138px, the Command tab's
pane status labels 1236px, IRC's toolbar 1232px). Three helpers:

* :class:`ResponsiveRow` is a QWidget whose layout minimum does not
  propagate to the window above its floor. Without this the window can
  never shrink enough to trigger any responsive behaviour — the layout
  minimum is the window minimum (the chicken-and-egg that made a plain
  hide-on-resize scheme useless).
* :class:`OverflowRow` watches such a row and moves optional buttons into
  a "⋯" menu when the row is too narrow, restoring them when it widens.
  Candidates must be widgets the app itself never hides — the row only
  ever un-hides widgets OverflowRow hid.
* :func:`shrink_label` fixes the QLabel trap behind the "sometimes I
  can't make the window smaller" report: a QLabel's minimumSizeHint is
  its FULL text width, so any dynamic status text silently grows the
  window's minimum while the user browses. ``setWordWrap(True)`` makes
  the hint tiny (a 1248px-hint label drops to ~100px) at the cost of the
  text wrapping instead of pushing the window wide. (A plain
  ``setMinimumWidth(0)`` does NOT work — the size hint still wins.)
"""
from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import QEvent, QObject, QSize, QTimer
from PySide6.QtWidgets import QMenu, QPushButton, QWidget


class ResponsiveRow(QWidget):
    """A widget row whose reported minimum never exceeds ``min_width``.

    The row itself may still be squeezed by its own squeeze logic (e.g.
    narrower buttons); the cap only stops the row's layout minimum from
    becoming the window's minimum. ``min_width`` should be the width the
    row's always-visible core needs, so nothing ever clips.
    """

    def __init__(self, min_width: int = 0, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._row_min_width = min_width

    def set_row_min_width(self, width: int) -> None:
        self._row_min_width = max(0, width)
        self.updateGeometry()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        ms = super().minimumSizeHint()
        if self._row_min_width > 0:
            ms.setWidth(min(ms.width(), self._row_min_width))
        return ms


class OverflowRow(QObject):
    """Hide a row's optional buttons into a "⋯" menu when space runs out.

    ``candidates`` is in hide-first order — the first widget is the first
    to disappear as the row narrows, the last stays visible longest. All
    candidates must be direct children of ``row``'s layout and must be
    widgets the app itself keeps visible (see the module docstring).

    If ``row`` is a :class:`ResponsiveRow` without a floor, the floor is
    measured here as the width the row needs with every candidate hidden
    (plus the "⋯" button) — that is the smallest width the row can honor
    without clipping.

    Hidden buttons stay fully functional: the "⋯" menu lists them and
    clicking an entry calls the button's own ``click()``, so every signal,
    toggle state and handler is the button's. Checkable buttons get a
    checkable menu action that mirrors their checked state.

    The row is re-fitted on every resize of ``row``; call :meth:`refit`
    by hand after adding widgets to the row.
    """

    def __init__(self, row: QWidget, candidates: Sequence[QWidget],
                 parent: Optional[QObject] = None,
                 layout=None) -> None:
        super().__init__(parent or row)
        self._row = row
        self._lay = layout if layout is not None else row.layout()
        self._cands = list(candidates)
        self._hidden_count = 0
        self.button = QPushButton("⋯")
        self.button.setObjectName("btn_secondary")
        self.button.setFixedWidth(48)
        self.button.setToolTip("More controls")
        self.button.setVisible(False)
        self._menu = QMenu(self.button)
        self.button.setMenu(self._menu)
        self._menu.aboutToShow.connect(self._rebuild_menu)
        # The overflow button goes at the very end of the row. ``layout``
        # lets the caller target a sub-layout when the row widget itself
        # holds a vbox of several rows.
        self._lay.addWidget(self.button)
        if isinstance(row, ResponsiveRow) and row._row_min_width == 0:
            row.set_row_min_width(self._core_width() + self.button.minimumSizeHint().width())
        row.installEventFilter(self)
        # Layout geometry isn't valid during __init__ — fit once laid out.
        QTimer.singleShot(0, self.refit)

    def _core_width(self) -> int:
        """Row minimum with every candidate hidden = the un-hideable core."""
        for w in self._cands:
            w.hide()
        try:
            width = self._lay.minimumSize().width()
        finally:
            for w in self._cands:
                w.show()
        return width

    # -- fitting -------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._row and event.type() == QEvent.Resize:
            self.refit()
        return super().eventFilter(obj, event)

    def hidden_widgets(self) -> list:
        """The candidates currently collapsed into the menu (tests)."""
        return list(self._cands[:self._hidden_count])

    def refit(self) -> None:
        """Hide/unhide candidates until the row's layout fits its width."""
        lay = self._lay
        avail = self._row.width()
        if lay is None or avail <= 0:
            return
        # Iterate to a fixpoint: each pass either hides one more candidate
        # (because the row still doesn't fit), un-hides the last hidden one
        # (because it fits again), or stops. The "⋯" button's own minimum
        # is part of the row whenever something is hidden, which the first
        # line of the loop keeps in sync.
        for _ in range(2 * len(self._cands) + 2):
            self.button.setVisible(self._hidden_count > 0)
            need = lay.minimumSize().width()
            if need > avail and self._hidden_count < len(self._cands):
                self._cands[self._hidden_count].hide()
                self._hidden_count += 1
                continue
            if need <= avail and self._hidden_count > 0:
                candidate = self._cands[self._hidden_count - 1]
                candidate.show()
                if lay.minimumSize().width() <= avail:
                    self._hidden_count -= 1
                    continue
                candidate.hide()  # doesn't fit — stop restoring
            break
        self.button.setVisible(self._hidden_count > 0)

    # -- menu ----------------------------------------------------------------
    def _rebuild_menu(self) -> None:
        self._menu.clear()
        for w in self.hidden_widgets():
            label = w.text().strip() if hasattr(w, "text") else ""
            if not label:
                label = w.toolTip() or w.accessibleName() or "…"
            act = self._menu.addAction(label)
            if w.isCheckable():
                act.setCheckable(True)
                act.setChecked(w.isChecked())
            act.triggered.connect(lambda _checked=False, w=w: w.click())


def shrink_label(label: QWidget) -> None:
    """Stop a dynamic-text QLabel from growing the window's minimum.

    See the module docstring — minimumSizeHint is the full text width
    otherwise, so live status text sets (and keeps changing) the smallest
    window size the user can achieve.
    """
    label.setWordWrap(True)
