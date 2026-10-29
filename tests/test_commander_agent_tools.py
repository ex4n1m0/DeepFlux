"""commander_* agent tools + the GUI bridge that backs them."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from agent.loop import DESTRUCTIVE_TOOLS, READ_ONLY_TOOLS
from agent.tools import CONFIRMATION_TOOL_NAMES, READ_ONLY_TOOL_NAMES, ToolRegistry
from config import DeeptorrentConfig
from gui.commander_bridge import CommanderBridge
from gui.commander_tab import CommanderTab, _OperationState


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def commander_env(qapp, tmp_path):
    """Offscreen Commander tab + bridge + ToolRegistry wired to a mock engine."""
    tab = CommanderTab(SimpleNamespace(default_save_path=str(tmp_path)))
    bridge = CommanderBridge(tab)
    tools = ToolRegistry(MagicMock(), DeeptorrentConfig(), commander_bridge=bridge)
    yield SimpleNamespace(tab=tab, bridge=bridge, tools=tools)
    operation = tab._active_operation
    if operation is not None:
        operation.cancel.set()
        tab._on_op_finished(operation, False, "Cancelled")
    tab.close()
    bridge.deleteLater()
    tab.deleteLater()
    qapp.processEvents()


def _settle(pane, folder, expected: int, timeout_ms: int = 5000) -> bool:
    target = os.path.normcase(os.path.abspath(str(folder)))
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        QApplication.processEvents()
        current = os.path.normcase(os.path.abspath(pane.current_path()))
        if (current == target and pane._model.rowCount(pane._view.rootIndex()) >= expected):
            QApplication.processEvents()
            return True
        time.sleep(0.02)
    return False


def _populate(tab, tmp_path, names, side="left"):
    for name in names:
        (tmp_path / name).write_text(name, encoding="utf-8")
    pane = tab._sides[side].currentWidget()
    pane.navigate(str(tmp_path), record_history=False)
    assert _settle(pane, tmp_path, len(names))
    return pane


def test_commander_tools_report_unavailability_without_bridge():
    from agent.tools import ToolError
    tools = ToolRegistry(MagicMock(), DeeptorrentConfig())
    result = tools.call("commander_state", {})
    assert result["success"] is False
    assert "GUI" in result["error"]
    result = tools.call("commander_transfer",
                        {"mode": "copy", "paths": ["C:/some/file.txt"]})
    assert result["success"] is False
    # missing required operands are rejected at the schema layer, like any
    # other tool — the confirmation preview can never lack the real paths
    with pytest.raises(ToolError, match="paths"):
        tools.call("commander_transfer", {"mode": "copy"})


def test_commander_tool_classification():
    from agent.tools import UNTRUSTED_RESULT_TOOLS
    assert "commander_state" in READ_ONLY_TOOL_NAMES
    assert "commander_state" in READ_ONLY_TOOLS
    assert "commander_state" in UNTRUSTED_RESULT_TOOLS  # filenames = external text
    assert "commander_transfer" in CONFIRMATION_TOOL_NAMES
    assert "commander_transfer" in DESTRUCTIVE_TOOLS
    # navigation and selection mutate UI state only — no confirmation gate
    assert "commander_navigate" not in CONFIRMATION_TOOL_NAMES
    assert "commander_select" not in CONFIRMATION_TOOL_NAMES
    assert "commander_navigate" not in READ_ONLY_TOOL_NAMES
    assert "commander_select" not in READ_ONLY_TOOL_NAMES


def test_commander_state_reflects_panes_and_selection(commander_env, tmp_path):
    env = commander_env
    left = _populate(env.tab, tmp_path, ["alpha.txt", "beta.log"])
    result = env.tools.call("commander_state", {})
    assert result["success"] is True
    assert result["active_pane"] in ("left", "right")
    panes = result["panes"]
    assert os.path.samefile(panes["left"]["path"], tmp_path)
    assert panes["left"]["selected_count"] == 0

    left.select_pattern("*.txt")
    QApplication.processEvents()
    result = env.tools.call("commander_state", {})
    assert result["panes"]["left"]["selected_count"] == 1
    assert result["panes"]["left"]["selected"][0].endswith("alpha.txt")


def test_commander_navigate_applies_before_returning(commander_env, tmp_path):
    """Navigation is SYNCHRONOUS: a transfer issued right after must see the
    new destination (review finding: fire-and-forget navigation moved files
    into the pre-navigation folder)."""
    env = commander_env
    old_dst = tmp_path / "old-dst"
    new_dst = tmp_path / "new-dst"
    old_dst.mkdir()
    new_dst.mkdir()
    (old_dst / "witness.txt").write_text("", encoding="utf-8")
    env.tab.set_pane_paths(str(tmp_path), str(old_dst))
    assert _settle(env.tab.right_pane, old_dst, 1)

    result = env.tools.call(
        "commander_navigate", {"side": "right", "path": str(new_dst)})
    assert result["success"] is True
    # applied BEFORE the tool returned — no processEvents needed
    assert os.path.samefile(env.tab.right_pane.current_path(), new_dst)
    state = env.tools.call("commander_state", {})
    assert os.path.samefile(state["panes"]["right"]["path"], new_dst)

    # agent navigation must not pollute the user's back/forward history
    assert env.tab.right_pane._back_history == []

    missing = env.tools.call(
        "commander_navigate", {"side": "right", "path": str(tmp_path / "nope")})
    assert missing["success"] is False


def test_commander_select_and_transfer_end_to_end(commander_env, tmp_path):
    env = commander_env
    source_dir = tmp_path / "agent-src"
    target_dir = tmp_path / "agent-dst"
    source_dir.mkdir()
    target_dir.mkdir()
    for name in ("keep.log", "skip.log", "data.bin"):
        (source_dir / name).write_text(name, encoding="utf-8")
    env.tab.set_pane_paths(str(source_dir), str(target_dir))
    assert _settle(env.tab.left_pane, source_dir, 3)

    selected = env.tools.call(
        "commander_select", {"side": "left", "pattern": "*.log"})
    assert selected["success"] is True and selected["count"] == 2

    paths = env.tools.call("commander_state", {})["panes"]["left"]["selected"]
    moved = env.tools.call(
        "commander_transfer",
        {"mode": "move", "paths": paths, "source": "left",
         "destination": "right", "conflict_policy": "skip"})
    assert moved["success"] is True, moved
    assert (target_dir / "keep.log").exists()
    assert (target_dir / "skip.log").exists()
    assert not (source_dir / "keep.log").exists()
    assert (source_dir / "data.bin").exists()  # untouched


def test_commander_transfer_conflict_policies(commander_env, tmp_path):
    env = commander_env
    source_dir = tmp_path / "pol-src"
    target_dir = tmp_path / "pol-dst"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "dupe.txt").write_text("new", encoding="utf-8")
    (target_dir / "dupe.txt").write_text("old", encoding="utf-8")
    env.tab.set_pane_paths(str(source_dir), str(target_dir))
    assert _settle(env.tab.left_pane, source_dir, 1)
    src = str(source_dir / "dupe.txt")

    skipped = env.tools.call(
        "commander_transfer", {"mode": "copy", "paths": [src]})
    assert skipped["success"] is True
    assert (target_dir / "dupe.txt").read_text(encoding="utf-8") == "old"
    assert not (target_dir / "dupe (copy).txt").exists()

    kept = env.tools.call(
        "commander_transfer",
        {"mode": "copy", "paths": [src], "conflict_policy": "keep_both"})
    assert kept["success"] is True
    assert (target_dir / "dupe (copy).txt").read_text(encoding="utf-8") == "new"


def test_commander_transfer_pins_operands_and_validates_pane_membership(
        commander_env, tmp_path):
    """Review findings: paths are REQUIRED (the confirmation preview must
    carry the real operands) and re-validated against the pane's folder."""
    env = commander_env
    source_dir = tmp_path / "pin-src"
    outside_dir = tmp_path / "pin-outside"
    source_dir.mkdir()
    outside_dir.mkdir()
    (source_dir / "inside.txt").write_text("x", encoding="utf-8")
    (outside_dir / "outside.txt").write_text("x", encoding="utf-8")
    env.tab.set_pane_paths(str(source_dir), str(outside_dir))
    assert _settle(env.tab.left_pane, source_dir, 1)

    from agent.tools import ToolError
    with pytest.raises(ToolError, match="paths"):
        env.tools.call("commander_transfer", {"mode": "copy"})

    # a path outside the source pane's folder fails closed
    outsider = env.tools.call(
        "commander_transfer",
        {"mode": "copy", "paths": [str(outside_dir / "outside.txt")],
         "source": "left"})
    assert outsider["success"] is False
    assert "pane" in outsider["error"]
    assert not (outside_dir / "outside (copy).txt").exists()

    vanished = env.tools.call(
        "commander_transfer",
        {"mode": "copy", "paths": [str(source_dir / "ghost.txt")],
         "source": "left"})
    assert vanished["success"] is False and "exists" in vanished["error"]

    # same-dir move is refused instead of no-op confusion
    env.tab.set_pane_paths(str(source_dir), str(source_dir))
    assert _settle(env.tab.left_pane, source_dir, 1)
    same = env.tools.call(
        "commander_transfer",
        {"mode": "move", "paths": [str(source_dir / "inside.txt")], "source": "left"})
    assert same["success"] is False


def test_commander_transfer_guard_releases_between_sequential_calls(
        commander_env, tmp_path):
    """Two transfers back-to-back in one approved batch must both run —
    no phantom 'another file operation is already running'."""
    env = commander_env
    source_dir = tmp_path / "seq-src"
    target_dir = tmp_path / "seq-dst"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "one.txt").write_text("1", encoding="utf-8")
    (source_dir / "two.txt").write_text("2", encoding="utf-8")
    env.tab.set_pane_paths(str(source_dir), str(target_dir))
    assert _settle(env.tab.left_pane, source_dir, 2)

    first = env.tools.call("commander_transfer",
                           {"mode": "copy", "paths": [str(source_dir / "one.txt")]})
    second = env.tools.call("commander_transfer",
                            {"mode": "copy", "paths": [str(source_dir / "two.txt")]})
    # NOTE: no processEvents() between the calls
    assert first["success"] is True and second["success"] is True, (first, second)
    assert (target_dir / "one.txt").exists() and (target_dir / "two.txt").exists()


def test_commander_transfer_requires_selection_and_respects_guard(
        commander_env, tmp_path, monkeypatch):
    env = commander_env
    _populate(env.tab, tmp_path, ["guarded.txt"])
    env.tab.left_pane.select_pattern("*.txt")
    QApplication.processEvents()
    busy = _OperationState("copy", quiet=True)
    env.tab._active_operation = busy
    try:
        result = env.tools.call(
            "commander_transfer",
            {"mode": "copy", "paths": [str(tmp_path / "guarded.txt")]})
        assert result["success"] is False
        assert "running" in result["error"]
    finally:
        env.tab._active_operation = None


def test_selection_does_not_survive_navigation(qapp, tmp_path):
    """Review finding: a QTreeView root change keeps the OLD folder's
    selection invisibly alive — F5 or an agent transfer would act on paths
    the pane no longer shows."""
    from types import SimpleNamespace as NS
    tab = CommanderTab(NS(default_save_path=str(tmp_path)))
    try:
        first = tmp_path / "sel-first"
        second = tmp_path / "sel-second"
        first.mkdir()
        second.mkdir()
        (first / "a.txt").write_text("a", encoding="utf-8")
        tab.left_pane.navigate(str(first), record_history=False)
        assert _settle(tab.left_pane, first, 1)
        tab.left_pane.select_pattern("*.txt")
        assert len(tab.left_pane.selected_paths()) == 1

        tab.left_pane.navigate(str(second), record_history=False)
        assert _settle(tab.left_pane, second, 0)
        assert tab.left_pane.selected_paths() == []
        state_selected = tab.left_pane.selected_paths()
        assert state_selected == []
    finally:
        tab.close()
        tab.deleteLater()
        qapp.processEvents()


def test_headless_failure_records_outcome_without_dialog(
        commander_env, tmp_path, monkeypatch):
    env = commander_env
    source_dir = tmp_path / "quiet-src"
    source_dir.mkdir()
    (source_dir / "f.txt").write_text("x", encoding="utf-8")
    env.tab.left_pane.navigate(str(source_dir), record_history=False)
    assert _settle(env.tab.left_pane, source_dir, 1)
    env.tab.left_pane.select_pattern("*.txt")
    QApplication.processEvents()

    def no_warning(*_args, **_kwargs):
        pytest.fail("quiet headless failures must not open a warning dialog")

    monkeypatch.setattr("gui.commander_tab.QMessageBox.warning", no_warning)
    # sabotage: make the destination pane point at a folder that will vanish
    doomed = tmp_path / "doomed-dst"
    doomed.mkdir()
    env.tab.set_pane_paths(str(source_dir), str(doomed))
    doomed.rmdir()
    result = env.tab.run_headless_transfer(
        "copy", [str(source_dir / "f.txt")], str(doomed), "skip")
    assert result["ok"] is False
    QApplication.processEvents()
    outcomes = env.tab.operation_outcomes()
    assert outcomes and outcomes[-1]["mode"] == "copy"
    assert outcomes[-1]["ok"] is False
