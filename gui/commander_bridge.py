"""Thread-safe adapter between the agent's commander_* tools and the Command tab.

The agent runs its tools on worker threads; Qt widgets may only be touched
from the GUI thread. This bridge keeps a PLAIN-DATA snapshot of the tab
state (rebuilt on GUI-thread signals, swapped atomically) so reads never
touch Qt, and marshals the two calls that must run on the GUI thread
(navigate, select) through a queued signal plus a completion event.

Filesystem mutations (commander_transfer) do NOT pass through here: the
tab's headless runner executes them on the calling thread under the same
serialized operation controller the GUI uses.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from gui.commander_tab import CommanderTab, FilePane

logger = logging.getLogger(__name__)

_SNAPSHOT_SELECTION_CAP = 200


class _Timeout(Exception):
    pass


class CommanderBridge(QObject):
    """GUI-side bridge; must be CREATED on the GUI thread."""

    _call_requested = Signal(object)  # zero-arg callable (runs on the GUI thread)

    def __init__(self, tab: CommanderTab, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._tab = tab
        self._gui_thread = threading.current_thread()
        self._snapshot: Dict[str, object] = {}
        self._rebuild_snapshot()

        self._call_requested.connect(self._run_call)

        for side in ("left", "right"):
            tabs = tab._sides[side]
            tabs.currentChanged.connect(lambda _i, s=side: self._rebuild_snapshot())
            for index in range(tabs.count()):
                self._connect_pane(tabs.widget(index), side)
        tab.pane_added.connect(lambda pane, side: self._connect_pane(pane, side))
        tab.active_pane_changed.connect(lambda _side: self._rebuild_snapshot())

    # -- snapshot ---------------------------------------------------------------
    def _connect_pane(self, pane: FilePane, side: str) -> None:
        pane.selection_changed.connect(lambda _paths, s=side: self._rebuild_snapshot())
        pane.path_changed.connect(lambda _path, s=side: self._rebuild_snapshot())

    def _rebuild_snapshot(self) -> None:
        tab = self._tab
        panes: Dict[str, Dict[str, object]] = {}
        for side in ("left", "right"):
            tabs = tab._sides[side]
            paths: List[str] = []
            for index in range(tabs.count()):
                try:
                    paths.append(tabs.widget(index).current_path())
                except RuntimeError:  # tab already deleted
                    continue
            selected = tabs.currentWidget().selected_paths() if tabs.currentWidget() else []
            panes[side] = {
                "path": tabs.currentWidget().current_path() if tabs.currentWidget() else "",
                "tabs": paths,
                "selected_count": len(selected),
                "selected": list(selected)[:_SNAPSHOT_SELECTION_CAP],
            }
        snapshot = {"active": tab._side_of(tab._active), "panes": panes}
        self._snapshot = snapshot  # atomic swap; readers grab it once

    # -- agent-facing API (called from worker threads) ----------------------------
    def state(self) -> Dict[str, object]:
        return copy.deepcopy(self._snapshot)

    def navigate(self, side: str, path: str) -> Dict[str, object]:
        """Navigate SYNCHRONOUSLY on the GUI thread.

        Fire-and-forget navigation races a following transfer: the tool
        would read a pre-navigation snapshot and send files to the OLD
        destination (empirically proven in review). The GUI hop is fast,
        bounded by the shared 15s timeout, and does not record agent
        activity into the user's back/forward history.
        """
        if side not in ("left", "right"):
            return {"ok": False, "error": "side must be 'left' or 'right'"}
        path = os.path.abspath(path.strip().strip('"'))
        if not os.path.isdir(path):
            return {"ok": False, "error": f"path is not an existing folder: {path}"}
        try:
            self._call_on_gui(lambda: self._navigate_sync(side, path))
        except _Timeout:
            return {"ok": False, "error": "the GUI thread did not answer in time"}
        return {"ok": True, "side": side, "path": path}

    def select(self, side: str, pattern: str, include: bool = True) -> Dict[str, object]:
        if side not in ("left", "right"):
            return {"ok": False, "error": "side must be 'left' or 'right'"}
        if not pattern.strip():
            return {"ok": False, "error": "pattern must not be empty"}
        try:
            count = self._call_on_gui(
                lambda: self._tab._sides[side].currentWidget().select_pattern(pattern, include))
        except _Timeout:
            return {"ok": False, "error": "the GUI thread did not answer in time"}
        except RuntimeError as exc:
            return {"ok": False, "error": f"selection failed: {exc}"}
        verb = "selected" if include else "deselected"
        return {"ok": True, "side": side, "pattern": pattern,
                "verb": verb, "count": count}

    # -- GUI-thread marshalling -----------------------------------------------------
    def transfer(self, mode: str, paths: List[str], dst_dir: str,
                 conflict_policy: str) -> Dict[str, object]:
        """Blocking headless transfer; safe to call from any thread.

        The tab's runner does the file I/O on the CALLING thread under the
        same serialized guard the GUI uses; only outcome bookkeeping hops
        back to the GUI thread via a queued signal.
        """
        return self._tab.run_headless_transfer(mode, paths, dst_dir, conflict_policy)

    def _call_on_gui(self, fn: Callable):
        if threading.current_thread() is self._gui_thread:
            return fn()
        done = threading.Event()
        box: Dict[str, object] = {}

        def run() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:  # delivered to the caller below
                box["error"] = exc
            finally:
                done.set()

        self._call_requested.emit(run)
        if not done.wait(15.0):
            raise _Timeout()
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _run_call(self, fn: Callable) -> None:
        fn()

    # -- GUI-thread slots -------------------------------------------------------------
    def _navigate_sync(self, side: str, path: str) -> None:
        tabs = self._tab._sides.get(side)
        pane = tabs.currentWidget() if tabs is not None else None
        if pane is not None:
            # record_history=False: agent navigation must not pollute the
            # user's Alt+Left/Right history
            pane.navigate(path, record_history=False)
