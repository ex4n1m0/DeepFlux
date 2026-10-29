"""Batch rename for the Commander tab — planning + live-preview dialog.

XYplorer-inspired feature set, fitted to the Commander's safety model:
nothing renames until the preview table shows a valid plan, and the worker
(``CommanderTab`` operation mode "batch_rename") applies the plan as ONE
serialized operation with a rollback journal.

The planner is deliberately pure (paths in, plan out) so it can be tested
without a QApplication.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui.i18n import tr

_WIN_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}
_NAME_BUDGET = 200  # stay clear of filesystem + path-length ceilings


@dataclass
class RenamePlanItem:
    source: str
    target: str = ""
    error: str = ""


def _name_error(name: str) -> str:
    """Platform-appropriate validation of one file/folder name."""
    if not name or name in (".", ".."):
        return tr("empty or reserved name")
    if os.name == "nt":
        bad = _WIN_INVALID.search(name)
        if bad:
            return f"{tr('character not allowed')}: {bad.group(0)!r}"
        if name.upper().split(".")[0] in _WIN_RESERVED:
            return tr("reserved device name")
        if name.endswith((" ", ".")):
            return tr("cannot end with a space or dot on Windows")
    else:
        if "/" in name or "\x00" in name:
            return tr("character not allowed")
    if len(name) > _NAME_BUDGET:
        return tr("name too long")
    return ""


def _apply_number_pattern(pattern: str, stem: str, index: int) -> str:
    """Replace runs of '#' with a zero-padded counter; '{name}' = old stem."""
    def pad(match: "re.Match[str]") -> str:
        return str(index).zfill(len(match.group(0)))
    result = pattern.replace("{name}", stem)
    return re.sub(r"#+", pad, result)


def plan_renames(paths: List[str], *,
                 find: str = "", replace: str = "", use_regex: bool = False,
                 stem_only: bool = True, case: str = "",
                 number_pattern: str = "",
                 new_names: Optional[List[str]] = None) -> List[RenamePlanItem]:
    """Compute the full rename plan for `paths` and validate every target.

    Exactly one transformation source is expected: either `new_names`
    (one complete new name per path) or the find/replace + case + numbering
    options. Invalid options (bad regex, wrong list length) mark every item
    with the same error instead of raising — the preview shows why.
    """
    shared_error = ""
    regex = None
    if use_regex and find:
        try:
            regex = re.compile(find)
        except re.error as exc:
            shared_error = f"{tr('invalid regular expression')}: {exc}"
    if new_names is not None and len(new_names) != len(paths):
        shared_error = tr("the pasted list must have exactly one name per item")

    items: List[RenamePlanItem] = []
    taken = {os.path.normcase(os.path.abspath(p)) for p in paths}
    for index, source in enumerate(paths):
        source_name = os.path.basename(source)
        if shared_error:
            items.append(RenamePlanItem(source=source, error=shared_error))
            continue
        try:
            if new_names is not None:
                candidate = new_names[index]
            else:
                stem, extension = os.path.splitext(source_name)
                work = stem if stem_only else source_name
                if number_pattern:
                    work = _apply_number_pattern(number_pattern, work, index + 1)
                if find:
                    if regex is not None:
                        work = regex.sub(replace, work)
                    else:
                        work = work.replace(find, replace)
                candidate = (work + extension) if stem_only else work
            # case transforms apply to the assembled name (extension included)
            if case == "lower":
                candidate = candidate.lower()
            elif case == "upper":
                candidate = candidate.upper()
            elif case == "title":
                candidate = candidate.title()
        except (ValueError, IndexError) as exc:
            items.append(RenamePlanItem(source=source, error=str(exc)))
            continue

        target = os.path.join(os.path.dirname(source), candidate)
        error = _name_error(candidate)
        if not error and os.path.basename(target) != candidate:
            error = tr("name must not contain path separators")
        if not error:
            key = os.path.normcase(os.path.abspath(target))
            if key not in taken and os.path.lexists(target):
                error = tr("destination already exists")
            # case-only renames (same file, different case) are legitimate
            elif key in taken and os.path.normcase(os.path.abspath(source)) != key:
                error = tr("another item already maps to this name")
        items.append(RenamePlanItem(source=source, target=target, error=error))
    # second pass: intra-batch collisions (two sources -> one target)
    seen: dict = {}
    for item in items:
        if item.error:
            continue
        key = os.path.normcase(os.path.abspath(item.target))
        if key in seen:
            item.error = tr("another item already maps to this name")
        elif key != os.path.normcase(os.path.abspath(item.source)):
            seen[key] = item.source
    return items


class BatchRenameDialog(QDialog):
    """Live-preview batch rename over the pane's selection."""

    def __init__(self, parent: QWidget, paths: List[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Batch Rename"))
        self._paths = list(paths)
        self.plans: List[RenamePlanItem] = []

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget(self)
        layout.addWidget(self._tabs, 1)

        # -- find & replace page -------------------------------------------------
        replace_page = QWidget(self)
        replace_layout = QVBoxLayout(replace_page)
        replace_layout.setContentsMargins(8, 8, 8, 8)
        grid_top = QHBoxLayout()
        self._find_edit = QLineEdit()
        self._find_edit.setPlaceholderText(tr("Find"))
        self._find_edit.setAccessibleName("Batch rename find text")
        self._find_edit.setToolTip(tr('Text to search for in every name'))
        self._replace_edit = QLineEdit()
        self._replace_edit.setPlaceholderText(tr("Replace"))
        self._replace_edit.setAccessibleName("Batch rename replace text")
        self._replace_edit.setToolTip(tr('Replacement text'))
        grid_top.addWidget(QLabel(tr("Find:")))
        grid_top.addWidget(self._find_edit, 1)
        grid_top.addWidget(QLabel(tr("Replace:")))
        grid_top.addWidget(self._replace_edit, 1)
        replace_layout.addLayout(grid_top)

        options_row = QHBoxLayout()
        self._regex_check = QCheckBox(tr("Regular expression"))
        self._regex_check.setToolTip(tr('Treat Find as a regular expression'))
        self._stem_check = QCheckBox(tr("Keep extension unchanged"))
        self._stem_check.setChecked(True)
        self._stem_check.setToolTip(tr('Rename the part before the extension only'))
        self._case_combo = QComboBox()
        self._case_combo.setAccessibleName("Batch rename case transform")
        self._case_combo.setToolTip(tr('Change the letter case of names'))
        self._case_combo.addItems(
            [tr("Case: unchanged"), tr("Case: lower"), tr("Case: upper"), tr("Case: title")])
        self._number_edit = QLineEdit()
        self._number_edit.setPlaceholderText(tr("Numbering pattern (e.g. photo ###)"))
        self._number_edit.setAccessibleName("Batch rename numbering pattern")
        self._number_edit.setToolTip(
            tr('Optional numbering template: # becomes a digit ("photo ###" → photo 001); {name} inserts the current name'))
        options_row.addWidget(self._regex_check)
        options_row.addWidget(self._stem_check)
        options_row.addWidget(self._case_combo)
        options_row.addWidget(self._number_edit, 1)
        replace_layout.addLayout(options_row)
        self._tabs.addTab(replace_page, tr("Find && Replace"))

        # -- paste list page -------------------------------------------------
        list_page = QWidget(self)
        list_layout = QVBoxLayout(list_page)
        list_layout.setContentsMargins(8, 8, 8, 8)
        hint = QLabel(tr("Paste one new full name per line — the count must match the selection."))
        hint.setWordWrap(True)
        self._names_edit = QPlainTextEdit()
        self._names_edit.setAccessibleName("Batch rename pasted name list")
        self._names_edit.setToolTip(tr('One new name per selected item, in the same order'))
        self._names_edit.setPlaceholderText("\n".join(os.path.basename(p) for p in paths[:3]))
        list_layout.addWidget(hint)
        list_layout.addWidget(self._names_edit, 1)
        self._tabs.addTab(list_page, tr("Paste Name List"))

        # -- preview table -------------------------------------------------
        self._table = QTableWidget(0, 3, self)
        self._table.setHorizontalHeaderLabels([tr("Current"), tr("New"), tr("Problem")])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setAccessibleName("Batch rename preview")
        layout.addWidget(self._table, 2)

        self._summary = QLabel("")
        self._summary.setAccessibleName("Batch rename summary")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Rename"))
        buttons.accepted.connect(self._accept_plan)
        buttons.rejected.connect(self.reject)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(buttons)

        for edit in (self._find_edit, self._replace_edit, self._number_edit):
            edit.textChanged.connect(self.recompute)
        self._names_edit.textChanged.connect(self.recompute)
        for check in (self._regex_check, self._stem_check):
            check.toggled.connect(self.recompute)
        self._case_combo.currentIndexChanged.connect(lambda _i: self.recompute())
        self._tabs.currentChanged.connect(lambda _i: self.recompute())
        self.recompute()

    # -- planning -----------------------------------------------------------------
    def recompute(self) -> None:
        paths = self._paths
        if self._tabs.currentIndex() == 1:
            names = [line for line in self._names_edit.toPlainText().splitlines()]
            self.plans = plan_renames(paths, new_names=names or None)
        else:
            case = ("lower", "upper", "title")[
                max(0, self._case_combo.currentIndex() - 1)] if self._case_combo.currentIndex() else ""
            self.plans = plan_renames(
                paths,
                find=self._find_edit.text(),
                replace=self._replace_edit.text(),
                use_regex=self._regex_check.isChecked(),
                stem_only=self._stem_check.isChecked(),
                case=case,
                number_pattern=self._number_edit.text().strip(),
            )
        self._fill_table()

    def _fill_table(self) -> None:
        self._table.setRowCount(len(self.plans))
        errors = 0
        changes = 0
        for row, item in enumerate(self.plans):
            current = QTableWidgetItem(os.path.basename(item.source))
            new_name = os.path.basename(item.target) if item.target else ""
            new_item = QTableWidgetItem(new_name)
            if item.error:
                problem = QTableWidgetItem(item.error)
                for widget in (current, new_item, problem):
                    widget.setForeground(Qt.GlobalColor.red)
                errors += 1
            else:
                problem = QTableWidgetItem("")
                # exact (case-sensitive) comparison: case-only IS a change
                if os.path.abspath(item.source) != os.path.abspath(item.target):
                    changes += 1
            self._table.setItem(row, 0, current)
            self._table.setItem(row, 1, new_item)
            self._table.setItem(row, 2, problem)
        self._summary.setText(
            tr("changes") + f": {changes}  " + tr("problems") + f": {errors}")
        self._ok_button.setEnabled(errors == 0 and changes > 0)

    def _accept_plan(self) -> None:
        # Only rows that actually rename survive into the worker plan.
        self.plans = [
            item for item in self.plans
            if not item.error and item.target
            and os.path.abspath(item.source) != os.path.abspath(item.target)]
        if self.plans:
            self.accept()
