"""i18n_wrap.py — wrap user-visible string literals in gui/i18n tr() calls.

Phase 2+ of the Chinese localization (2026-09-24): the shell was wrapped by
hand; the per-tab surfaces are mechanical, so they get a tool.

    python packaging/i18n_wrap.py gui/main_window.py gui/downloads_tab.py ...
    python packaging/i18n_wrap.py --report        # list tr() keys missing from the zh catalog

Why AST and not regex: only plain str Constants inside whitelisted CALLS are
wrapped, so f-strings, concatenations, QSS blocks, objectName values and
module-level constants are structurally out of reach, and re-running the tool
is a no-op (tr("x") is a Call node, not a Constant). Behavior is identical
under the default English language — tr() is the identity there — so the
wrap pass can never change what the English UI or the English-pinning tests
see; only the catalog decides what Chinese users get, and a missing key falls
back to English (see gui/i18n.py).

RULE (from phase 1, do not regress): never wrap strings at module/constant
definition time — gui/__init__ imports everything before the language is
set. This tool only touches call arguments, which in gui/ code effectively
means inside methods/builders running after startup.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Callable names whose positional str args (all of them, capped per name
# below) are user-visible labels.
_TEXTY_CALLS = {
    "setText": 1, "setToolTip": 1, "setPlaceholderText": 1,
    "setWindowTitle": 1, "setWhatsThis": 1, "setStatusTip": 1,
    "QPushButton": 1, "QLabel": 1, "QCheckBox": 1, "QRadioButton": 1,
    "QGroupBox": 1, "QMenu": 1, "QAction": 1, "QToolButton": 1,
    "QListWidgetItem": 1, "QListWidget": 1,
    "addAction": 1, "addMenu": 1, "addTab": 2, "insertTab": 3,
    "setTabText": 2, "addItem": 1, "setCurrentText": 1,
}

# Full dotted statics: (parent, name) -> positional indices of label args,
# counting from 0 (parent/title/text all wrapped; parent is never a str).
_STATIC_CALLS = {
    ("QMessageBox", "question"): (1, 2),
    ("QMessageBox", "information"): (1, 2),
    ("QMessageBox", "warning"): (1, 2),
    ("QMessageBox", "critical"): (1, 2),
    ("QMessageBox", "about"): (1, 2),
    ("QInputDialog", "getText"): (1, 2),
    ("QInputDialog", "getMultiLineText"): (1, 2),
    ("QInputDialog", "getInt"): (1, 2),
    ("QInputDialog", "getDouble"): (1, 2),
    ("QInputDialog", "getItem"): (1, 2),
}

# Keyword arguments that carry user-visible text on whitelisted calls.
_TEXTY_KEYWORDS = {"text", "title", "label", "tip", "placeholder", "buttonText"}

# Header lists: wrap every str Constant ELEMENT of the single list argument.
_LIST_CALLS = {"setHorizontalHeaderLabels", "setHeaderLabels"}

_SKIP_VALUE_PREFIXES = (":", "#", "qrc", "http://", "https://", "file://")
_SKIP_VALUE_CONTAINS = ("{", "}", "<html", "<div", "<style", "://")


def _is_wrappable_constant(node: ast.AST) -> bool:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    value = node.value
    if not value or not value.strip():
        return False
    if value.startswith(_SKIP_VALUE_PREFIXES):
        return False
    if any(marker in value for marker in _SKIP_VALUE_CONTAINS):
        return False
    # NOTE: single-word labels ("Pause", "Name") ARE wrapped — they are real
    # UI text. Value-like tokens ("mp4", "mpv") simply never get a catalog
    # entry, so tr() returns them unchanged and value comparisons keep
    # working under every language.
    return True


def _collect_targets(tree: ast.Module) -> list[tuple[int, int, int, int, str]]:
    """(lineno, col, end_lineno, end_col, replacement) for every wrap."""
    edits: list[tuple[int, int, int, int, str]] = []

    def add(node: ast.Constant) -> None:
        if _is_wrappable_constant(node):
            edits.append((node.lineno, node.col_offset, node.end_lineno,
                          node.end_col_offset, f"tr({node.value!r})"))

    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        # tr(...) itself: report mode handles it; never double-wrap (its arg
        # sits inside a Call, and we only reach Constants via whitelists).
        name = None
        parent = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
            base = func.value
            if isinstance(base, ast.Name):
                parent = base.id
        if name is None:
            continue
        if name == "tr":  # already wrapped
            continue
        if (parent, name) in _STATIC_CALLS:
            for idx in _STATIC_CALLS[(parent, name)]:
                if idx < len(call.args):
                    add(call.args[idx])
            for kw in call.keywords:
                if kw.arg in _TEXTY_KEYWORDS and kw.value is not None:
                    add(kw.value)
            continue
        if name in _LIST_CALLS and call.args and isinstance(call.args[0], ast.List):
            for element in call.args[0].elts:
                add(element)
            continue
        if name in _TEXTY_CALLS:
            limit = _TEXTY_CALLS[name]
            for idx, arg in enumerate(call.args[:limit]):
                add(arg)
            for kw in call.keywords:
                if kw.arg in _TEXTY_KEYWORDS and kw.value is not None:
                    add(kw.value)
    return edits


def wrap_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    edits = _collect_targets(tree)
    if not edits:
        return []
    lines = source.splitlines(keepends=True)
    # apply bottom-up so earlier offsets stay valid across lines. NOTE: ast
    # col_offset/end_col_offset are UTF-8 BYTE offsets, not character
    # indices — slice each line as bytes or astral chars (emoji, CJK) shift
    # the cut and eat the closing paren.
    for lineno, col, end_lineno, end_col, replacement in sorted(
            edits, key=lambda e: (e[0], e[1]), reverse=True):
        if lineno != end_lineno:
            print(f"{path}: SKIP multi-line literal at line {lineno} — wrap by hand")
            return wrap_file_skipping(path, skip=(lineno, col))
        line_b = lines[lineno - 1].encode("utf-8")
        line_b = line_b[:col] + replacement.encode("utf-8") + line_b[end_col:]
        lines[lineno - 1] = line_b.decode("utf-8")
    # module must import tr
    if "from gui.i18n import tr" not in source:
        raise SystemExit(f"{path}: missing 'from gui.i18n import tr' — add it first")
    new_source = "".join(lines)
    ast.parse(new_source)  # syntax guard before writing
    path.write_text(new_source, encoding="utf-8", newline="")
    return [edit[4][3:-1] for edit in edits]


def wrap_file_skipping(path: Path, skip: tuple[int, int]) -> list[str]:
    """Re-run the collection ignoring the node at `skip` (and any further
    multi-line literals — recursion bottoms out when none remain)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    edits = [e for e in _collect_targets(tree)
             if (e[0], e[1]) != skip and e[0] == e[2]]
    if not edits:
        return []
    lines = source.splitlines(keepends=True)
    for lineno, col, end_lineno, end_col, replacement in sorted(
            edits, key=lambda e: (e[0], e[1]), reverse=True):
        line_b = lines[lineno - 1].encode("utf-8")
        line_b = line_b[:col] + replacement.encode("utf-8") + line_b[end_col:]
        lines[lineno - 1] = line_b.decode("utf-8")
    # module must import tr
    if "from gui.i18n import tr" not in source:
        raise SystemExit(f"{path}: missing 'from gui.i18n import tr' — add it first")
    new_source = "".join(lines)
    ast.parse(new_source)  # syntax guard before writing
    path.write_text(new_source, encoding="utf-8", newline="")
    return [edit[4][3:-1] for edit in edits]


def collect_keys(paths: list[Path]) -> set[str]:
    """All plain-literal tr() keys in the given files (for --report)."""
    keys: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "tr" and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)):
                keys.add(call.args[0].value)
    return keys


def main(argv: list[str]) -> int:
    args = argv[1:]
    report_only = False
    if args and args[0] == "--report":
        report_only = True
        args = args[1:]
    if not args:
        gui_dir = REPO / "gui"
        paths = [p for p in sorted(gui_dir.glob("*.py"))]
    else:
        paths = [Path(a) for a in args]

    if report_only:
        sys.path.insert(0, str(REPO))
        from gui.i18n_zh_cn import ZH_CN
        missing = sorted(k for k in collect_keys(paths) if k not in ZH_CN)
        print(f"{len(missing)} keys missing from the zh catalog:")
        for key in missing:
            print(f"    {key!r},")
        return 0

    total = 0
    for path in paths:
        wrapped = wrap_file(path)
        if wrapped:
            print(f"{path}: wrapped {len(wrapped)} strings")
            total += len(wrapped)
    print(f"total wrapped: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
