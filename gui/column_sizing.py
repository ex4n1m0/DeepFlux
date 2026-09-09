"""Auto-fitting yet user-draggable column widths.

QHeaderView.Stretch fills leftover viewport space but ignores cell content
(long file names get cut) and locks the section against dragging — the
user cannot resize it at all. ResizeToContents tracks content but is
equally undraggable. The download tables want both behaviours at once:
names must always be fully visible AND every column must stay
user-resizable.

AutoColumnSizer keeps every section Interactive (draggable) and resizes
them programmatically instead:

* ``auto_fit()`` sizes each column to its contents (widest of header hint
  and cell text + padding, with optional per-column max clamps) and then
  gives the fill column the remaining viewport width when that is wider —
  so names are always fully shown and the view has no dead space.
* A column the user drags is remembered and never auto-sized again;
  double-click its header separator to hand it back to auto-fit.
* The fill column re-fits (debounced) when the table resizes or after a
  user drag settles, so the view stays gapless without fighting the
  pointer mid-drag.

``sectionResized`` fires only from explicit resizeSection calls (verified
against Qt 6: model resets, row inserts and header re-layout do NOT emit
it), which is what lets us attribute every unguarded emission to the user.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QHeaderView, QTableView

# Cell-text scan cap per column: content sizing stays cheap even on a
# queue left accumulating for weeks; beyond this, widths settle at the
# longest of the first rows (still draggable / auto-fit on demand).
_MAX_SCAN_ROWS = 5000


class AutoColumnSizer(QObject):
    """Content-driven column widths that the user can still override."""

    def __init__(
        self,
        table: QTableView,
        fill_column: Optional[int] = 0,
        padding: int = 12,
        max_widths: Optional[dict[int, int]] = None,
    ) -> None:
        super().__init__(table)
        self._table = table
        self._header = table.horizontalHeader()
        self._fill_column = fill_column
        self._padding = padding
        self._max_widths = max_widths or {}
        self._user_sized: set[int] = set()
        self._fitting = False
        self._refit_timer = QTimer(self)
        self._refit_timer.setSingleShot(True)
        self._refit_timer.setInterval(150)
        self._refit_timer.timeout.connect(self.refit_fill)
        self._fitting = True
        try:
            for column in range(self._header.count()):
                self._header.setSectionResizeMode(column, QHeaderView.Interactive)
        finally:
            self._fitting = False
        self._header.sectionResized.connect(self._on_section_resized)
        self._header.sectionHandleDoubleClicked.connect(self._on_handle_double_clicked)
        table.installEventFilter(self)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def auto_fit(self) -> None:
        """Size every non-user-touched column to its contents, then let the
        fill column absorb the remaining viewport width (never below its
        content width, so long names always stay fully visible)."""
        columns = self._column_count()
        if columns <= 0:
            return
        self._fitting = True
        try:
            for column in range(columns):
                self._header.setSectionResizeMode(column, QHeaderView.Interactive)
            fixed_total = 0
            for column in range(columns):
                if column == self._fill_column:
                    continue
                if column in self._user_sized:
                    fixed_total += self._header.sectionSize(column)
                    continue
                width = self._content_width(column)
                self._header.resizeSection(column, width)
                fixed_total += width
            if self._fill_column is not None and 0 <= self._fill_column < columns \
                    and self._fill_column not in self._user_sized:
                content = self._content_width(self._fill_column)
                available = self._table.viewport().width() - fixed_total
                self._header.resizeSection(self._fill_column, max(content, available))
        finally:
            self._fitting = False

    def refit_fill(self) -> None:
        """Re-run only the fill step (table was shown/resized, or a user
        drag just settled) — cheap and jitter-free for value columns."""
        columns = self._column_count()
        if (self._fill_column is None or not 0 <= self._fill_column < columns
                or self._fill_column in self._user_sized):
            return
        fixed_total = sum(
            self._header.sectionSize(column)
            for column in range(columns) if column != self._fill_column)
        content = self._content_width(self._fill_column)
        available = self._table.viewport().width() - fixed_total
        self._fitting = True
        try:
            self._header.resizeSection(self._fill_column, max(content, available))
        finally:
            self._fitting = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _column_count(self) -> int:
        model = self._table.model()
        if model is not None and model.columnCount() > 0:
            return model.columnCount()
        return self._header.count()

    def _content_width(self, column: int) -> int:
        """Widest of header hint and cell display texts, plus padding."""
        model = self._table.model()
        metrics = self._table.fontMetrics()
        width = 0
        if model is not None:
            for row in range(min(model.rowCount(), _MAX_SCAN_ROWS)):
                text = model.index(row, column).data(Qt.DisplayRole)
                if text:
                    width = max(width, metrics.horizontalAdvance(str(text)))
        width = max(width, self._header.sectionSizeHint(column)) + self._padding
        return min(width, self._max_widths[column]) if column in self._max_widths else width

    def _on_section_resized(self, logical_index: int, _old_size: int, _new_size: int) -> None:
        if self._fitting:
            return
        # sectionResized only fires from resizeSection (ours are guarded),
        # so this is the user dragging: the column is manual from now on.
        self._user_sized.add(logical_index)
        self._refit_timer.start()

    def _on_handle_double_clicked(self, logical_index: int) -> None:
        self._user_sized.discard(logical_index)
        self._refit_timer.stop()
        self.auto_fit()

    def eventFilter(self, obj, event) -> bool:
        if obj is self._table and event.type() in (QEvent.Resize, QEvent.Show):
            self._refit_timer.start()
        return False
