from __future__ import annotations

from unittest.mock import MagicMock

from gui.main_window import MainWindow


class CookieCache:
    _browser_cookie_key = staticmethod(MainWindow._browser_cookie_key)
    _browser_origins_related = staticmethod(MainWindow._browser_origins_related)
    _on_browser_cookie_added = MainWindow._on_browser_cookie_added
    _on_browser_cookie_removed = MainWindow._on_browser_cookie_removed
    _browser_cookies_for = MainWindow._browser_cookies_for

    def __init__(self):
        self._browser_cookies = {}


def cookie(domain=".example.com", path="/", secure=True, name=b"session", value=b"value"):
    item = MagicMock()
    item.domain.return_value = domain
    item.path.return_value = path
    item.name.return_value = name
    item.value.return_value = value
    item.isSecure.return_value = secure
    item.expirationDate.return_value.isValid.return_value = False
    return item


def test_cookie_cache_respects_source_origin_path_and_secure_scope():
    cache = CookieCache()
    cache._on_browser_cookie_added(cookie(path="/private"))

    assert cache._browser_cookies_for(
        "https://sub.example.com/private/file", "https://example.com/page") == "session=value"
    assert cache._browser_cookies_for(
        "https://sub.example.com/public/file", "https://example.com/page") == ""
    assert cache._browser_cookies_for(
        "http://sub.example.com/private/file", "http://example.com/page") == ""
    assert cache._browser_cookies_for(
        "https://sub.example.com/private/file", "https://attacker.test/page") == ""


def test_removed_cookie_is_not_forwarded():
    cache = CookieCache()
    item = cookie()
    cache._on_browser_cookie_added(item)
    cache._on_browser_cookie_removed(item)
    assert cache._browser_cookies_for("https://example.com/", "https://example.com/") == ""
