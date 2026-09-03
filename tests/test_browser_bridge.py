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
    })

    assert "me@example.com" not in result["text"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result["text"]
    assert "secret" not in result["url"]
    assert len(result["links"]) == 1
    assert "secret" not in result["links"][0]["href"]


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
