"""Bridge between the agent's browser_* tools and the MainWindow browser.

Agent tools run on AgentLoop worker threads, so every browser action is
queued onto the GUI thread via a signal; the calling thread blocks on an
Event until the GUI reports the result. JavaScript evaluations complete the
pending call from runJavaScript's own callback (also delivered on the GUI
thread), so the worker never touches Qt objects directly.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, Qt, QUrl, Signal

logger = logging.getLogger(__name__)


class _PendingCall:
    """One-shot result slot shared between the worker thread and the GUI."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Dict[str, Any] = {"success": False, "error": "no result"}


class BrowserBridge(QObject):
    """Synchronous facade (any thread) over the MainWindow browser (GUI)."""

    _invoke = Signal(str, dict, object)  # (action, params, _PendingCall)

    TIMEOUT = 35.0  # seconds — page JS on heavy sites can be slow

    def __init__(self, window: Any) -> None:
        super().__init__(window)  # parented to MainWindow -> GUI thread affinity
        self._window = window
        self._invoke.connect(self._dispatch, Qt.QueuedConnection)

    # ------------------------------------------------------------------
    # Agent-thread entry point
    # ------------------------------------------------------------------
    def call(self, op: str, **params: Any) -> Dict[str, Any]:
        pending = _PendingCall()
        self._invoke.emit(op, params, pending)
        if not pending.event.wait(self.TIMEOUT):
            return {"success": False, "error": f"Browser action '{op}' timed out."}
        return pending.result

    # ------------------------------------------------------------------
    # GUI thread dispatch
    # ------------------------------------------------------------------
    def _dispatch(self, action: str, params: dict, pending: _PendingCall) -> None:
        try:
            handler = getattr(self, f"_do_{action}", None)
            if handler is None:
                pending.result = {"success": False, "error": f"Unknown browser action: {action}"}
                pending.event.set()
                return
            handler(pending, **params)  # JS actions complete `pending` later
        except Exception as exc:
            logger.exception("browser bridge action %s failed", action)
            pending.result = {"success": False, "error": str(exc)}
            pending.event.set()

    # -- helpers -------------------------------------------------------------
    def _view(self):
        return self._window._current_browser_view()

    def _focus(self) -> None:
        """Bring the Browse tab forward so the user sees agent navigation."""
        self._window.main_tabs.setCurrentWidget(self._window._browser_tab)

    @staticmethod
    def _finish(pending: _PendingCall, result: Dict[str, Any]) -> None:
        pending.result = result
        pending.event.set()

    def _run_js(self, pending: _PendingCall, script: str) -> None:
        """Run JS in the current page; the callback completes the pending call."""
        def _cb(res: Any) -> None:
            try:
                if isinstance(res, dict):
                    res.setdefault("success", True)
                    pending.result = res
                else:
                    pending.result = {"success": True, "value": res}
            finally:
                pending.event.set()

        self._view().page().runJavaScript(script, _cb)

    @staticmethod
    def _normalize_url(text: str) -> str:
        """Same rule as the address bar: domains load directly, anything else
        is a Google search."""
        text = text.strip()
        if "." in text and " " not in text:
            if not text.startswith(("http://", "https://", "about:")):
                text = "https://" + text
            return text
        return f"https://www.google.com/search?q={text}"

    # -- tabs ----------------------------------------------------------------
    def _do_list_tabs(self, pending: _PendingCall) -> None:
        tabs = self._window.browser_tabs
        current = tabs.currentIndex()
        out = []
        for i in range(tabs.count()):
            w = tabs.widget(i)
            out.append({
                "index": i,
                "title": tabs.tabText(i),
                "url": w.url().toString() if hasattr(w, "url") else "",
                "active": i == current,
            })
        self._finish(pending, {"success": True, "count": len(out),
                               "active_index": current, "tabs": out})

    def _do_navigate(self, pending: _PendingCall, url: str = "", new_tab: bool = False) -> None:
        if not url.strip():
            self._finish(pending, {"success": False, "error": "url is required"})
            return
        target = self._normalize_url(url)
        if new_tab:
            self._window._browser_new_tab(url=QUrl(target))
        else:
            self._view().load(QUrl(target))
        self._focus()
        self._finish(pending, {"success": True, "url": target, "new_tab": bool(new_tab)})

    def _do_close_tab(self, pending: _PendingCall, index: int = -1) -> None:
        tabs = self._window.browser_tabs
        idx = int(index) if 0 <= int(index) < tabs.count() else tabs.currentIndex()
        if tabs.count() <= 1:
            self._finish(pending, {"success": False, "error": "Cannot close the last tab."})
            return
        url = ""
        w = tabs.widget(idx)
        if hasattr(w, "url"):
            url = w.url().toString()
        self._window._browser_close_tab(idx)
        self._finish(pending, {"success": True, "closed_index": idx, "url": url})

    def _do_switch_tab(self, pending: _PendingCall, index: int = 0) -> None:
        tabs = self._window.browser_tabs
        idx = int(index)
        if not 0 <= idx < tabs.count():
            self._finish(pending, {"success": False,
                                   "error": f"Tab index {idx} out of range (0-{tabs.count() - 1})."})
            return
        tabs.setCurrentIndex(idx)
        self._focus()
        self._finish(pending, {"success": True, "active_index": idx})

    def _do_go(self, pending: _PendingCall, action: str = "") -> None:
        view = self._view()
        action = (action or "").lower()
        if action == "back":
            view.back()
        elif action == "forward":
            view.forward()
        elif action == "reload":
            view.reload()
        elif action == "stop":
            view.stop()
        elif action == "home":
            self._window._browser_go_home()
            self._focus()
        else:
            self._finish(pending, {"success": False,
                                   "error": "action must be back|forward|reload|stop|home"})
            return
        self._finish(pending, {"success": True, "action": action})

    # -- page interaction (JavaScript) ---------------------------------------
    def _do_get_content(self, pending: _PendingCall, max_chars: int = 8000,
                        include_links: bool = True) -> None:
        script = """
(() => {
  const text = (document.body ? document.body.innerText : "").slice(0, %d);
  const out = {success: true, title: document.title || "", url: location.href, text};
  if (%s) {
    out.links = [...document.querySelectorAll("a[href]")]
      .map(a => ({text: (a.innerText || "").trim().slice(0, 80), href: a.href}))
      .filter(l => l.text)
      .slice(0, 60);
  }
  return out;
})()
""" % (max(500, min(int(max_chars or 8000), 50000)),
       "true" if include_links else "false")
        self._run_js(pending, script)

    def _do_click(self, pending: _PendingCall, selector: str = "", text: str = "") -> None:
        if not selector and not text:
            self._finish(pending, {"success": False, "error": "Provide selector or text."})
            return
        script = """
(() => {
  const sel = %s, txt = %s.toLowerCase();
  let el = null;
  if (sel) {
    try { el = document.querySelector(sel); }
    catch (e) { return {success: false, error: "bad selector: " + e}; }
  }
  if (!el && txt) {
    el = [...document.querySelectorAll(
      "a,button,input[type=submit],input[type=button],[role=button],summary,label")]
      .find(e => ((e.innerText || e.value || e.getAttribute("aria-label") || "")
        .trim().toLowerCase().includes(txt)));
  }
  if (!el) return {success: false, error: "no element matched"};
  el.scrollIntoView({block: "center"});
  el.click();
  return {success: true, clicked: true, tag: el.tagName.toLowerCase(),
          text: (el.innerText || el.value || "").trim().slice(0, 100),
          href: el.href || ""};
})()
""" % (json.dumps(selector), json.dumps(text))
        self._run_js(pending, script)

    def _do_fill(self, pending: _PendingCall, selector: str = "", value: str = "",
                 submit: bool = False) -> None:
        if not selector:
            self._finish(pending, {"success": False, "error": "selector is required"})
            return
        script = """
(() => {
  const el = document.querySelector(%s);
  if (!el) return {success: false, error: "no element matched selector"};
  el.focus();
  el.value = %s;
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
  let submitted = false;
  if (%s) {
    const f = el.closest("form");
    if (f) { f.requestSubmit(); submitted = true; }
  }
  return {success: true, filled: true, submitted};
})()
""" % (json.dumps(selector), json.dumps(value), "true" if submit else "false")
        self._run_js(pending, script)

    def _do_scroll(self, pending: _PendingCall, direction: str = "down",
                   pixels: int = 0) -> None:
        direction = (direction or "down").lower()
        if direction not in ("up", "down", "top", "bottom"):
            self._finish(pending, {"success": False,
                                   "error": "direction must be up|down|top|bottom"})
            return
        script = """
(() => {
  const d = %s, px = %d || Math.round(innerHeight * 0.85);
  if (d === "top") scrollTo(0, 0);
  else if (d === "bottom") scrollTo(0, document.body.scrollHeight);
  else scrollBy(0, d === "up" ? -px : px);
  return {success: true, scrollY: Math.round(scrollY)};
})()
""" % (json.dumps(direction), int(pixels or 0))
        self._run_js(pending, script)

    # -- bookmarks -------------------------------------------------------------
    def _do_list_bookmarks(self, pending: _PendingCall) -> None:
        marks = [{"title": b.title, "url": b.url, "folder": b.folder}
                 for b in self._window.config.browser.bookmarks]
        self._finish(pending, {"success": True, "count": len(marks), "bookmarks": marks})

    def _do_add_bookmark(self, pending: _PendingCall, url: str = "", title: str = "") -> None:
        from config import Bookmark

        url = (url or "").strip()
        if not url:
            # Default to the current page.
            view = self._view()
            url = view.url().toString()
            if not title:
                title = view.title() or url
        if not url or url.startswith(("about:", "data:")):
            self._finish(pending, {"success": False, "error": "No bookmarkable page."})
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        cfg = self._window.config.browser
        if any(b.url == url for b in cfg.bookmarks):
            self._finish(pending, {"success": True, "url": url, "note": "Already bookmarked."})
            return
        cfg.bookmarks.append(Bookmark(title=title or url, url=url))
        self._window._rebuild_bookmarks_bar()
        self._finish(pending, {"success": True, "url": url, "title": title or url})

    def _do_remove_bookmark(self, pending: _PendingCall, url: str = "") -> None:
        cfg = self._window.config.browser
        before = len(cfg.bookmarks)
        cfg.bookmarks = [b for b in cfg.bookmarks if b.url != url]
        if len(cfg.bookmarks) == before:
            self._finish(pending, {"success": False, "error": f"No bookmark for {url}"})
            return
        self._window._rebuild_bookmarks_bar()
        self._finish(pending, {"success": True, "removed": url})
