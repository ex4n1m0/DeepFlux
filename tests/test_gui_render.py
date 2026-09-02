"""Chat-render regression tests: long unbroken tokens (magnets, hashes, URLs)
must get zero-width-space break opportunities so result bubbles and tables
never overflow the chat viewport; HTML tags and entities must stay intact."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui.main_window import _break_long_tokens, _markdown_to_html


def test_long_token_gets_break_opportunities():
    magnet = "magnet:?xt=urn:btih:" + "a1" * 30
    out = _break_long_tokens(magnet)
    assert "\u200b" in out
    # The original token is recoverable by removing the break markers.
    assert out.replace("\u200b", "") == magnet


def test_entities_are_never_split():
    out = _break_long_tokens("x" * 30 + "&amp;" + "y" * 30)
    assert "&amp;" in out
    assert "&am\u200bp;" not in out


def test_tags_and_attributes_untouched():
    url = "u" * 60
    out = _break_long_tokens('<a href="' + url + '">label</a>')
    assert '<a href="' + url + '">' in out


def test_short_text_untouched():
    assert _break_long_tokens("hello world") == "hello world"


def test_markdown_table_with_magnet_is_wrappable():
    md = "| Name | Magnet |\n|---|---|\n| rel | `magnet:?xt=urn:btih:" + "b2" * 30 + "` |"
    out = _break_long_tokens(_markdown_to_html(md))
    assert "\u200b" in out
    assert "<table>" in out


def test_settings_menu_pages_fit_low_resolution():
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.settings_dialog import (
        API_KEY_PAGES,
        BROWSER_SETTINGS_PAGES,
        DOWNLOAD_SETTINGS_PAGES,
        APIKeysDialog,
        BrowserSettingsDialog,
        DownloadsSettingsDialog,
    )

    app = QApplication.instance() or QApplication([])
    config = DeeptorrentConfig()
    groups = (
        (DownloadsSettingsDialog, DOWNLOAD_SETTINGS_PAGES),
        (BrowserSettingsDialog, BROWSER_SETTINGS_PAGES),
        (APIKeysDialog, API_KEY_PAGES),
    )
    for dialog_class, pages in groups:
        for _label, page in pages:
            dialog = dialog_class(config, page=page)
            dialog.show()
            app.processEvents()
            assert dialog.width() <= 760
            assert dialog.height() <= 520
            dialog.close()


def test_download_settings_pages_save_only_their_section():
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.settings_dialog import DownloadsSettingsDialog

    QApplication.instance() or QApplication([])
    config = DeeptorrentConfig()
    original_path = config.default_save_path
    original_concurrent = config.download.max_concurrent
    dialog = DownloadsSettingsDialog(config, page="queue")
    dialog.tor_max_downloading.setValue(17)
    dialog.save_path.setText("must-not-save")
    dialog.dm_max_concurrent.setValue(original_concurrent + 1)
    dialog._save_and_accept()
    assert config.torrents.max_downloading_torrents == 17
    assert config.default_save_path == original_path
    assert config.download.max_concurrent == original_concurrent


def test_api_and_browser_pages_preserve_hidden_settings():
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.settings_dialog import APIKeysDialog, BrowserSettingsDialog

    QApplication.instance() or QApplication([])
    config = DeeptorrentConfig()
    config.llm.api_key = "original-llm-key"
    api_dialog = APIKeysDialog(config, page="adult")
    api_dialog.llm_key.setText("must-not-save")
    api_dialog.tpdb_key.setText("new-tpdb-key")
    api_dialog._save_and_accept()
    assert config.llm.api_key == "original-llm-key"
    assert config.iptv.tpdb_api_key == "new-tpdb-key"

    config.browser.homepage = "https://example.com/"
    browser_dialog = BrowserSettingsDialog(config, page="privacy")
    browser_dialog.browser_homepage.setText("invalid hidden URL")
    browser_dialog.browser_history_days.setValue(45)
    browser_dialog._save_and_accept()
    assert config.browser.homepage == "https://example.com/"
    assert config.browser.history_retention_days == 45
