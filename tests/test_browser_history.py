from __future__ import annotations

from gui.browser_history import BrowserHistory


def test_history_records_updates_and_searches(tmp_path):
    history = BrowserHistory(str(tmp_path / "history.sqlite"))
    history.record("https://example.com/one", "First title")
    history.record("https://example.com/two", "Second title")
    history.record("https://example.com/one", "Updated title")

    results = history.suggestions("updated")

    assert len(results) == 1
    assert results[0]["url"] == "https://example.com/one"
    assert results[0]["visit_count"] == 2


def test_history_rejects_internal_and_file_urls(tmp_path):
    history = BrowserHistory(str(tmp_path / "history.sqlite"))
    history.record("file:///secret", "Secret")
    history.record("deepflux://start/", "Internal")

    assert history.suggestions() == []


def test_history_redacts_sensitive_query_values(tmp_path):
    history = BrowserHistory(str(tmp_path / "history.sqlite"))
    history.record("https://example.com/reset?token=secret#fragment", "Reset")

    url = history.suggestions()[0]["url"]
    assert "secret" not in url
    assert "fragment" not in url


def test_history_clear(tmp_path):
    history = BrowserHistory(str(tmp_path / "history.sqlite"))
    history.record("https://example.com", "Example")
    history.clear()
    assert history.suggestions() == []
