from __future__ import annotations

import os
import sys

import pytest
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QObject
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineScript

from gui.browser_bridge import BrowserBridge, _PendingCall, accept_language_header, normalize_browser_target
from dlmgr.browser_extension import inject_into_profile
from gui.browser_channel import BrowserChannelBridge, channel_injection_script


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_url_normalization_supports_hosts_ips_and_encoded_searches():
    assert normalize_browser_target("example.com/a") == "https://example.com/a"
    assert normalize_browser_target("localhost:8080/a") == "http://localhost:8080/a"
    assert normalize_browser_target("192.168.1.2") == "https://192.168.1.2"
    assert normalize_browser_target("two words & more", "duckduckgo") == \
        "https://duckduckgo.com/?q=two+words+%26+more"


def test_url_normalization_rejects_active_and_misleading_urls():
    with pytest.raises(ValueError):
        normalize_browser_target("javascript:alert(1)")
    with pytest.raises(ValueError):
        normalize_browser_target("https://attacker@example.com")


@pytest.mark.parametrize("locale_name, expected", [
    ("en_US", "en-US,en;q=0.9"),
    ("en_MO", "en-MO,en;q=0.9"),
    ("pt_PT", "pt-PT,pt;q=0.9,en;q=0.8"),
    ("es_419", "es-419,es;q=0.9,en;q=0.8"),
    ("de", "de,en;q=0.9"),
    ("en", "en"),
])
def test_accept_language_header_is_a_chrome_style_bcp47_list(locale_name, expected):
    assert accept_language_header(locale_name) == expected


@pytest.mark.parametrize("bad", ["English_Macao SAR", "Portuguese_Portugal", "C", "POSIX", "", None, "en_Latn_GB"])
def test_accept_language_header_never_emits_malformed_tags(bad):
    # Windows locale names / "C" are not language tags; a malformed header
    # got every Google search answered with the "unusual traffic" captcha.
    assert accept_language_header(bad) == "en-US,en;q=0.9"


def test_page_content_sanitization_redacts_tokens_emails_and_link_queries():
    result = BrowserBridge._sanitize_page_content({
        "text": "mail me@example.com token sk-abcdefghijklmnopqrstuvwxyz",
        "url": "https://example.com/?token=secret",
        "links": [
            {"text": "signed", "href": "https://example.com/a?api_key=secret"},
            {"text": "bad", "href": "javascript:alert(1)"},
        ],
        # snapshot controls + click_ref-style scalar fields carry the same
        # risk (review 2026-09-15) — labels are page text, hrefs live URLs.
        "label": "contact me@example.com",
        "href": "https://example.com/click?signature=abc",
        "url_before": "https://example.com/from?token=xyz",
        "controls": [
            {"ref": "e0", "tag": "a", "label": "mail me@example.com",
             "href": "https://example.com/a?token=secret"},
            {"ref": "e1", "tag": "a", "label": "x", "href": "javascript:alert(1)"},
        ],
    })

    assert "me@example.com" not in result["text"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result["text"]
    assert "secret" not in result["url"]
    assert len(result["links"]) == 1
    assert "secret" not in result["links"][0]["href"]
    assert "me@example.com" not in result["label"]
    assert "abc" not in result["href"]
    assert "xyz" not in result["url_before"]
    assert len(result["controls"]) == 2
    assert "me@example.com" not in result["controls"][0]["label"]
    assert "secret" not in result["controls"][0]["href"]
    assert result["controls"][1]["href"] == ""  # javascript: dropped, not passed through


def test_wrap_script_survives_trailing_comment(app):
    # _wrap_script parenthesizes so a trailing // comment can't swallow the
    # JSON.stringify call (review LOW, 2026-09-15).
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineWidgets import QWebEngineView
    view = QWebEngineView()
    loaded = []
    view.loadFinished.connect(lambda ok: loaded.append(ok))
    view.setHtml("<html><body>ok</body></html>", QUrl("https://x.test/"))
    window = QObject()
    window._current_browser_view = lambda: view
    window._browser_agent_content_allowed = lambda url: True
    bridge = BrowserBridge(window)
    assert _pump(lambda: bool(loaded))
    result = _run_bridge_op(bridge, "_run_js", script="(() => 1)() // note")
    # Scalars arrive JSON-quoted (stringify of 1 is "1") — coercion only
    # parses object/array strings; that's the documented invariant.
    assert result == {"success": True, "value": "1"}
    view.deleteLater()


def test_run_js_fails_loud_on_throwing_script(app):
    # A script that throws (or a stringify failure) must NEVER come back as
    # success — the PySide6 6.11 blank-result lesson (review LOW, 2026-09-15).
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineWidgets import QWebEngineView
    view = QWebEngineView()
    loaded = []
    view.loadFinished.connect(lambda ok: loaded.append(ok))
    view.setHtml("<html><body>ok</body></html>", QUrl("https://x.test/"))
    window = QObject()
    window._current_browser_view = lambda: view
    window._browser_agent_content_allowed = lambda url: True
    bridge = BrowserBridge(window)
    assert _pump(lambda: bool(loaded))
    result = _run_bridge_op(bridge, "_run_js", script="(() => { throw new Error('boom'); })()")
    assert result["success"] is False
    assert "no result" in result["error"]
    view.deleteLater()


def test_expired_pending_call_ignores_late_result():
    pending = _PendingCall()
    pending.expired = True
    BrowserBridge._finish(pending, {"success": True})
    assert pending.result["error"] == "no result"
    assert not pending.event.is_set()


def test_official_qwebchannel_loader_is_used(app):
    source = channel_injection_script()
    assert "invokeMethod: 6" in source
    assert "window.deepflux" in source


def test_in_app_extension_uses_isolated_world_and_injected_credentials(app):
    profile = QWebEngineProfile()
    script = inject_into_profile(profile, 54000, "isolated-token")
    try:
        assert script.worldId() == QWebEngineScript.ApplicationWorld
        assert script.runsOnSubFrames() is False
        assert "__DEEPFLUX_API_PORT__" not in script.sourceCode()
        assert "54000" in script.sourceCode()
        assert "isolated-token" in script.sourceCode()
    finally:
        profile.scripts().remove(script)
        profile.deleteLater()


def test_agent_navigation_is_blocked_in_private_tab(app):
    window = QObject()
    current = MagicMock()
    current.property.return_value = True
    window._current_browser_view = MagicMock(return_value=current)
    window._browser_new_tab = MagicMock()
    window.config = MagicMock()
    window.config.browser.search_engine = "google"
    bridge = BrowserBridge(window)
    pending = _PendingCall()

    bridge._do_navigate(pending, "example.com")

    assert pending.result["success"] is False
    assert "private" in pending.result["error"].lower()
    window._browser_new_tab.assert_not_called()


# ---------------------------------------------------------------------------
# Real-page regression tests (no network: setHtml with a synthetic origin).
#
# PySide6 6.11 regression (2026-09-15): runJavaScript callbacks return '' for
# OBJECT results in every world — scalars/strings still marshal. Every bridge
# op that builds its result in JS came back as {'success': True, 'value': ''}
# and agent browser control silently broke (unit tests all faked the bridge).
# The bridge now wraps scripts in JSON.stringify and parses centrally; these
# tests drive the REAL view + page IPC so a future PySide6 bump can't
# reintroduce the blank results unnoticed.
# ---------------------------------------------------------------------------

def _pump(predicate, timeout_s=15.0):
    """Spin the Qt loop until predicate() is true (JS callbacks arrive via IPC)."""
    import time as _time
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(25)
    deadline = _time.monotonic() + timeout_s

    def tick():
        if predicate() or _time.monotonic() >= deadline:
            timer.stop()
            loop.quit()

    timer.timeout.connect(tick)
    timer.start()
    loop.exec()
    return predicate()


def _make_real_bridge():
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineWidgets import QWebEngineView
    view = QWebEngineView()
    loaded = []
    view.loadFinished.connect(lambda ok: loaded.append(ok))
    view.setHtml(
        "<html><body><h1>DeepFlux Test</h1>"
        "<a id='lnk' href='https://x.test/a'>A link</a>"
        "<a id='signed' href='https://x.test/b?token=secret-value'>Signed link</a>"
        "<input id='q' type='text' placeholder='Search here'>"
        "<button id='go'>Go</button></body></html>",
        QUrl("https://x.test/"))
    window = QObject()
    window._current_browser_view = lambda: view
    window._browser_agent_content_allowed = lambda url: True
    return BrowserBridge(window), view, loaded


def _run_bridge_op(bridge, method, **kwargs):
    pending = _PendingCall()
    getattr(bridge, method)(pending, **kwargs)
    assert _pump(lambda: pending.event.is_set()), f"{method} never completed"
    return pending.result


def test_real_page_snapshot_returns_controls(app):
    bridge, view, loaded = _make_real_bridge()
    assert _pump(lambda: bool(loaded))
    result = _run_bridge_op(bridge, "_do_snapshot")
    assert result["success"] is True
    controls = result["controls"]
    tags = {c["tag"] for c in controls}
    assert {"a", "button", "input"} <= tags
    link = next(c for c in controls if c["tag"] == "a" and c["href"] == "https://x.test/a")
    assert link["href"] == "https://x.test/a"
    # Signed-link query values in control hrefs must be redacted end-to-end
    # before the result reaches the LLM.
    signed = next(c for c in controls if "x.test/b" in c["href"])
    assert "secret-value" not in signed["href"]
    view.deleteLater()


def test_real_page_get_content_returns_text(app):
    bridge, view, loaded = _make_real_bridge()
    assert _pump(lambda: bool(loaded))
    result = _run_bridge_op(bridge, "_do_get_content", max_chars=2000)
    assert result["success"] is True
    assert "DeepFlux Test" in result["text"]
    assert any(l["href"] == "https://x.test/a" for l in result["links"])
    view.deleteLater()


def test_real_page_type_and_click_refs(app):
    bridge, view, loaded = _make_real_bridge()
    assert _pump(lambda: bool(loaded))
    snap = _run_bridge_op(bridge, "_do_snapshot")
    box = next(c for c in snap["controls"] if c["tag"] == "input")
    typed = _run_bridge_op(bridge, "_do_type_ref", ref=box["ref"], value="hello deepflux")
    assert typed["success"] is True and typed.get("typed") is True
    button = next(c for c in snap["controls"] if c["tag"] == "button")
    clicked = _run_bridge_op(bridge, "_do_click_ref", ref=button["ref"])
    assert clicked["success"] is True
    # The typed value really landed in the DOM.
    checked = _run_bridge_op(bridge, "_do_get_content", max_chars=2000)
    assert checked["success"] is True
    view.deleteLater()


def test_channel_download_requires_browser_authorization(app):
    window = QObject()
    window._confirm_browser_action = MagicMock(return_value=False)
    current = MagicMock()
    current.url.return_value.toString.return_value = "https://source.example/page"
    window._current_browser_view = MagicMock(return_value=current)
    window._browser_cookies_for = MagicMock(return_value="")
    window._dl_engine = MagicMock()
    bridge = BrowserChannelBridge(window, parent=window)

    assert bridge.sendDownload("https://source.example/file.zip", "file", "File") is False
    window._dl_engine.add_job.assert_not_called()


def _routable_window():
    window = QObject()
    window._confirm_browser_action = MagicMock(return_value=True)
    current = MagicMock()
    current.url.return_value.toString.return_value = "https://source.example/page"
    window._current_browser_view = MagicMock(return_value=current)
    window._browser_cookies_for = MagicMock(return_value="")
    window._notify = MagicMock()
    window.main_tabs = MagicMock()
    window._torrents_tab = MagicMock()
    window._dl_engine = MagicMock()
    return window


def test_channel_download_routes_youtube_to_ytdlp(app):
    # The watch page must go to yt-dlp — the plain HTTP downloader saves
    # YouTube's HTML page as a small unplayable "video" (regression, 3.4.7).
    window = _routable_window()
    bridge = BrowserChannelBridge(window, parent=window)

    assert bridge.sendDownload(
        "https://www.youtube.com/watch?v=abc123", "youtube", "My Video") is True
    window._dl_engine.add_youtube_job.assert_called_once_with(
        url="https://www.youtube.com/watch?v=abc123",
        filename="My Video",
        source_url="https://source.example/page",
    )
    window._dl_engine.add_job.assert_not_called()


def test_channel_download_routes_youtube_by_url_even_without_type(app):
    window = _routable_window()
    bridge = BrowserChannelBridge(window, parent=window)

    assert bridge.sendDownload("https://youtu.be/abc123", "file", "") is True
    window._dl_engine.add_youtube_job.assert_called_once()
    window._dl_engine.add_job.assert_not_called()


def test_channel_download_routes_playlists_by_extension(app):
    window = _routable_window()
    bridge = BrowserChannelBridge(window, parent=window)

    assert bridge.sendDownload(
        "https://cdn.example/stream.m3u8?tok=1", "hls", "Stream") is True
    window._dl_engine.add_stream_job.assert_called_once()
    window._dl_engine.add_job.assert_not_called()
    window._dl_engine.add_youtube_job.assert_not_called()


def test_channel_download_plain_file_uses_http_job(app):
    window = _routable_window()
    bridge = BrowserChannelBridge(window, parent=window)

    assert bridge.sendDownload(
        "https://source.example/file.zip", "file", "File") is True
    window._dl_engine.add_job.assert_called_once()
    window._dl_engine.add_youtube_job.assert_not_called()
