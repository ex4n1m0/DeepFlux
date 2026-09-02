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
import re
import threading
import time
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEngineScript

logger = logging.getLogger(__name__)


def normalize_browser_target(text: str, search_engine: str = "google") -> str:
    import ipaddress
    from urllib.parse import quote_plus, urlsplit
    from config import BROWSER_SEARCH_ENGINES

    value = (text or "").strip()
    if not value:
        raise ValueError("URL or search text is required")
    explicit = urlsplit(value)
    host_port = bool(re.match(r"^[^/\s:]+:\d+(?:/|$)", value))
    if explicit.scheme and not host_port:
        scheme = explicit.scheme.lower()
        if scheme not in ("http", "https", "about", "deepflux"):
            raise ValueError(f"Unsupported browser scheme: {scheme}")
        if scheme == "about" and value != "about:blank":
            raise ValueError("Only about:blank is allowed")
        if scheme in ("http", "https") and (not explicit.hostname or explicit.username or explicit.password):
            raise ValueError("URL host is invalid or contains misleading credentials")
        return value
    first = value.split("/", 1)[0]
    host = first.rsplit(":", 1)[0] if first.count(":") == 1 else first
    looks_like_host = host.lower() == "localhost" or "." in host
    try:
        ipaddress.ip_address(host.strip("[]"))
        looks_like_host = True
    except ValueError:
        pass
    if looks_like_host and " " not in value:
        target = "http://" + value if host.lower() == "localhost" else "https://" + value
        parsed = urlsplit(target)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("URL host is invalid or contains misleading credentials")
        return target
    template = BROWSER_SEARCH_ENGINES.get(search_engine, BROWSER_SEARCH_ENGINES["google"])
    return template.format(query=quote_plus(value))


class _PendingCall:
    """One-shot result slot shared between the worker thread and the GUI."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Dict[str, Any] = {"success": False, "error": "no result"}
        self.expired = False
        self.lock = threading.Lock()


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
            with pending.lock:
                pending.expired = True
            return {"success": False, "error": f"Browser action '{op}' timed out."}
        return pending.result

    # ------------------------------------------------------------------
    # GUI thread dispatch
    # ------------------------------------------------------------------
    def _dispatch(self, action: str, params: dict, pending: _PendingCall) -> None:
        try:
            handler = getattr(self, f"_do_{action}", None)
            if handler is None:
                self._finish(pending, {"success": False, "error": f"Unknown browser action: {action}"})
                return
            handler(pending, **params)  # JS actions complete `pending` later
        except Exception as exc:
            logger.exception("browser bridge action %s failed", action)
            self._finish(pending, {"success": False, "error": str(exc)})

    # -- helpers -------------------------------------------------------------
    def _view(self):
        return self._window._current_browser_view()

    def _focus(self) -> None:
        """Bring the Browse tab forward so the user sees agent navigation."""
        self._window.main_tabs.setCurrentWidget(self._window._browser_tab)

    @staticmethod
    def _finish(pending: _PendingCall, result: Dict[str, Any]) -> None:
        with pending.lock:
            if pending.expired:
                return
            pending.result = result
            pending.event.set()

    def _run_js(
        self,
        pending: _PendingCall,
        script: str,
        transform: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        """Run JS in the current page; the callback completes the pending call."""
        view = self._view()
        if view.property("deepflux_private"):
            self._finish(pending, {"success": False, "error": "Agent actions are disabled in private tabs."})
            return

        def _cb(res: Any) -> None:
            try:
                if transform is not None:
                    res = transform(res)
                if isinstance(res, dict):
                    res.setdefault("success", True)
                    result = res
                else:
                    result = {"success": True, "value": res}
            except Exception as exc:
                result = {"success": False, "error": str(exc)}
            self._finish(pending, result)

        view.page().runJavaScript(script, QWebEngineScript.ApplicationWorld, _cb)

    def _normalize_url(self, text: str) -> str:
        """Normalize URL/search input using the configured search provider."""
        return normalize_browser_target(text, self._window.config.browser.search_engine)

    # -- tabs ----------------------------------------------------------------
    def _do_list_tabs(self, pending: _PendingCall) -> None:
        tabs = self._window.browser_tabs
        current = tabs.currentIndex()
        out = []
        for i in range(tabs.count()):
            w = tabs.widget(i)
            private = bool(w.property("deepflux_private")) if hasattr(w, "property") else False
            out.append({
                "index": i,
                "title": "Private tab" if private else tabs.tabText(i),
                "url": "" if private else w.url().toString() if hasattr(w, "url") else "",
                "active": i == current,
                "private": private,
            })
        self._finish(pending, {"success": True, "count": len(out),
                               "active_index": current, "tabs": out})

    def _do_navigate(self, pending: _PendingCall, url: str = "", new_tab: bool = False) -> None:
        if not url.strip():
            self._finish(pending, {"success": False, "error": "url is required"})
            return
        target = self._normalize_url(url)
        if self._view().property("deepflux_private"):
            self._finish(pending, {
                "success": False,
                "error": "Agent navigation is disabled while a private tab is active.",
            })
            return
        if new_tab:
            self._window._browser_new_tab(url=QUrl(target))
        else:
            self._view().load(QUrl(target))
        self._focus()
        self._finish(pending, {
            "success": True,
            "navigation_started": True,
            "url": target,
            "new_tab": bool(new_tab),
            "note": "Use browser_wait before reading or interacting with the destination page.",
        })

    def _do_close_tab(self, pending: _PendingCall, index: int = -1) -> None:
        tabs = self._window.browser_tabs
        idx = tabs.currentIndex() if int(index) == -1 else int(index)
        if not 0 <= idx < tabs.count():
            self._finish(pending, {"success": False, "error": f"Tab index {idx} is out of range."})
            return
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
        if view.property("deepflux_private"):
            self._finish(pending, {"success": False, "error": "Agent actions are disabled in private tabs."})
            return
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
    @staticmethod
    def _sanitize_page_content(result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        from agent.tools import redact_url_secrets

        safe = dict(result)
        text = str(safe.get("text", ""))
        text = re.sub(
            r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b",
            "[redacted-email]",
            text,
        )
        text = re.sub(
            r"(?i)\b(?:sk|pplx|ghp|github_pat|xox[baprs])[-_][a-z0-9_-]{12,}\b",
            "[redacted-token]",
            text,
        )
        text = re.sub(
            r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b",
            "[redacted-token]",
            text,
        )
        safe["text"] = text
        safe["url"] = redact_url_secrets(str(safe.get("url", "")))
        links = []
        for link in safe.get("links", []) if isinstance(safe.get("links"), list) else []:
            if not isinstance(link, dict):
                continue
            href = str(link.get("href", ""))
            if href.startswith(("javascript:", "data:", "file:")):
                continue
            links.append({**link, "href": redact_url_secrets(href)})
        if "links" in safe:
            safe["links"] = links
        return safe

    def _do_get_content(self, pending: _PendingCall, max_chars: int = 8000,
                        include_links: bool = True) -> None:
        view = self._view()
        url = view.url().toString()
        if not self._window._browser_agent_content_allowed(url):
            self._finish(pending, {
                "success": False,
                "error": f"Agent page access was denied for {url or 'the current page'}.",
            })
            return
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
        self._run_js(pending, script, self._sanitize_page_content)

    def _do_snapshot(self, pending: _PendingCall, limit: int = 120) -> None:
        view = self._view()
        url = view.url().toString()
        if not self._window._browser_agent_content_allowed(url):
            self._finish(pending, {"success": False, "error": f"Agent page access was denied for {url}."})
            return
        script = """
(() => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const label = el => {
    const id = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    return ((id && id.innerText) || el.getAttribute("aria-label") || el.placeholder ||
      el.innerText || el.value || "").trim().replace(/\\s+/g, " ").slice(0, 160);
  };
  const nodes = [...document.querySelectorAll(
    "a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[contenteditable=true],summary"
  )].filter(visible).slice(0, %d);
  window.__deepfluxAgentElements = nodes;
  return {
    success: true,
    title: document.title || "",
    url: location.href,
    controls: nodes.map((el, i) => ({
      ref: `e${i}`,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute("role") || "",
      type: el.getAttribute("type") || "",
      label: label(el),
      href: el.href || "",
      checked: typeof el.checked === "boolean" ? el.checked : null,
      disabled: !!el.disabled
    }))
  };
})()
""" % max(1, min(int(limit or 120), 250))
        self._run_js(pending, script, self._sanitize_page_content)

    @staticmethod
    def _ref_index(ref: str) -> Optional[int]:
        match = re.fullmatch(r"e(\d{1,3})", (ref or "").strip())
        return int(match.group(1)) if match else None

    def _do_click_ref(self, pending: _PendingCall, ref: str = "") -> None:
        index = self._ref_index(ref)
        if index is None:
            self._finish(pending, {"success": False, "error": "Invalid element ref; call browser_snapshot again."})
            return
        script = """
(() => {
  const el = (window.__deepfluxAgentElements || [])[%d];
  if (!el || !el.isConnected) return {success:false, error:"stale element ref; take a new snapshot"};
  if (el.disabled) return {success:false, error:"element is disabled"};
  const before = location.href;
  el.scrollIntoView({block:"center"});
  el.click();
  return {success:true, ref:"e%d", tag:el.tagName.toLowerCase(),
    label:(el.innerText || el.value || el.getAttribute("aria-label") || "").trim().slice(0,160),
    href:el.href || "", url_before:before};
})()
""" % (index, index)
        self._run_js(pending, script, self._sanitize_page_content)

    def _do_type_ref(self, pending: _PendingCall, ref: str = "", value: str = "") -> None:
        index = self._ref_index(ref)
        if index is None:
            self._finish(pending, {"success": False, "error": "Invalid element ref; call browser_snapshot again."})
            return
        script = """
(() => {
  const el = (window.__deepfluxAgentElements || [])[%d];
  if (!el || !el.isConnected) return {success:false, error:"stale element ref; take a new snapshot"};
  if (el.disabled) return {success:false, error:"element is disabled"};
  const value = %s;
  el.focus();
  if (el.isContentEditable) el.textContent = value;
  else {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (!setter || !setter.set) return {success:false, error:"element is not text-editable"};
    setter.set.call(el, value);
  }
  el.dispatchEvent(new InputEvent("input", {bubbles:true, inputType:"insertText", data:value}));
  el.dispatchEvent(new Event("change", {bubbles:true}));
  return {success:true, ref:"e%d", typed:true};
})()
""" % (index, json.dumps(value), index)
        self._run_js(pending, script)

    def _do_select_ref(self, pending: _PendingCall, ref: str = "", value: str = "") -> None:
        index = self._ref_index(ref)
        if index is None:
            self._finish(pending, {"success": False, "error": "Invalid element ref; call browser_snapshot again."})
            return
        script = """
(() => {
  const el = (window.__deepfluxAgentElements || [])[%d], wanted = %s;
  if (!el || !el.isConnected) return {success:false, error:"stale element ref; take a new snapshot"};
  if (el.tagName !== "SELECT") return {success:false, error:"element is not a select"};
  const option = [...el.options].find(o => o.value === wanted || o.text.trim() === wanted);
  if (!option) return {success:false, error:"option not found"};
  el.value = option.value;
  el.dispatchEvent(new Event("input", {bubbles:true}));
  el.dispatchEvent(new Event("change", {bubbles:true}));
  return {success:true, ref:"e%d", selected:option.text.trim(), value:option.value};
})()
""" % (index, json.dumps(value), index)
        self._run_js(pending, script)

    def _do_check_ref(self, pending: _PendingCall, ref: str = "", checked: bool = True) -> None:
        index = self._ref_index(ref)
        if index is None:
            self._finish(pending, {"success": False, "error": "Invalid element ref; call browser_snapshot again."})
            return
        script = """
(() => {
  const el = (window.__deepfluxAgentElements || [])[%d], wanted = %s;
  if (!el || !el.isConnected) return {success:false, error:"stale element ref; take a new snapshot"};
  if (!(el instanceof HTMLInputElement) || !["checkbox","radio"].includes(el.type))
    return {success:false, error:"element is not a checkbox or radio"};
  if (el.checked !== wanted) el.click();
  return {success:true, ref:"e%d", checked:el.checked};
})()
""" % (index, "true" if checked else "false", index)
        self._run_js(pending, script)

    def _do_wait(
        self,
        pending: _PendingCall,
        selector: str = "",
        url_contains: str = "",
        text: str = "",
        timeout_seconds: int = 10,
    ) -> None:
        view = self._view()
        if view.property("deepflux_private"):
            self._finish(pending, {"success": False, "error": "Agent actions are disabled in private tabs."})
            return
        if not selector and not url_contains and not text:
            self._finish(pending, {"success": False, "error": "Provide selector, url_contains, or text."})
            return
        deadline = time.monotonic() + max(1, min(int(timeout_seconds or 10), 30))
        timer = QTimer(self)
        timer.setInterval(250)

        def check() -> None:
            script = """
(() => {
  let selectorMatch = true;
  try { selectorMatch = !%s || !!document.querySelector(%s); }
  catch (e) { return {success:false, error:"bad selector: " + e}; }
  const urlMatch = !%s || location.href.includes(%s);
  const textMatch = !%s || (document.body && document.body.innerText.includes(%s));
  return {success:true, matched:selectorMatch && urlMatch && textMatch,
    url:location.href, title:document.title || ""};
})()
""" % (
                json.dumps(selector), json.dumps(selector),
                json.dumps(url_contains), json.dumps(url_contains),
                json.dumps(text), json.dumps(text),
            )

            def complete(result: Any) -> None:
                if isinstance(result, dict) and result.get("success") is False:
                    timer.stop()
                    timer.deleteLater()
                    self._finish(pending, result)
                elif isinstance(result, dict) and result.get("matched"):
                    timer.stop()
                    timer.deleteLater()
                    self._finish(pending, self._sanitize_page_content(result))
                elif time.monotonic() >= deadline:
                    timer.stop()
                    timer.deleteLater()
                    self._finish(pending, {"success": False, "error": "Browser wait timed out."})

            view.page().runJavaScript(
                script, QWebEngineScript.ApplicationWorld, complete)

        timer.timeout.connect(check)
        timer.start()
        check()

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
  let el = null;
  try { el = document.querySelector(%s); }
  catch (e) { return {success:false, error:"bad selector: " + e}; }
  if (!el) return {success: false, error: "no element matched selector"};
  const value = %s;
  el.focus();
  if (el.isContentEditable) el.textContent = value;
  else {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (!setter || !setter.set) return {success:false, error:"element is not text-editable"};
    setter.set.call(el, value);
  }
  el.dispatchEvent(new InputEvent("input", {bubbles:true, inputType:"insertText", data:value}));
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
        if not url or url.startswith(("about:", "data:", "file:", "deepflux:")):
            self._finish(pending, {"success": False, "error": "No bookmarkable page."})
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        cfg = self._window.config.browser
        if any(b.url == url for b in cfg.bookmarks):
            self._finish(pending, {"success": True, "url": url, "note": "Already bookmarked."})
            return
        cfg.bookmarks.append(Bookmark(title=title or url, url=url))
        self._window._save_config()
        self._window._rebuild_bookmarks_bar()
        self._finish(pending, {"success": True, "url": url, "title": title or url})

    def _do_remove_bookmark(self, pending: _PendingCall, url: str = "") -> None:
        cfg = self._window.config.browser
        before = len(cfg.bookmarks)
        cfg.bookmarks = [b for b in cfg.bookmarks if b.url != url]
        if len(cfg.bookmarks) == before:
            self._finish(pending, {"success": False, "error": f"No bookmark for {url}"})
            return
        self._window._save_config()
        self._window._rebuild_bookmarks_bar()
        self._finish(pending, {"success": True, "removed": url})
