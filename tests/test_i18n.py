"""gui/i18n — Simplified Chinese shell translations (phase 1).

Language state is process-global; the autouse fixture restores "en" after
every test so a Chinese run can never leak into the English-pinned GUI
tests regardless of test order.
"""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from config import DeeptorrentConfig  # noqa: E402
from gui.i18n import active_language, set_language, tr  # noqa: E402
from gui.i18n_zh_cn import ZH_CN  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_english():
    yield
    set_language("en")


def test_default_is_english_passthrough():
    set_language("en")
    assert active_language() == "en"
    assert tr("Agent") == "Agent"
    # unknown keys return the source — never blank, never a crash
    assert tr("never-a-catalog-key") == "never-a-catalog-key"


def test_unknown_language_falls_back_to_english():
    set_language("fr")
    assert active_language() == "en"
    assert tr("Agent") == "Agent"


def test_zh_translates_known_strings():
    set_language("zh")
    assert active_language() == "zh"
    assert tr("Agent") == "智能体"
    assert tr("File") == "文件"
    assert tr("Command") == "文件管理"
    # && mnemonics are part of the key verbatim
    assert tr("Set as Default App for Magnets && Media...") == "设为磁力链接和媒体的默认程序..."


def test_zh_missing_key_falls_back_to_source():
    set_language("zh")
    assert tr("Some untranslated string") == "Some untranslated string"


def test_catalog_sanity():
    """Keys/values are non-empty, trimmed strings and every translation
    actually contains CJK content (ideograph, or full-width punctuation for
    label-ish values like "SHA-256：") — catches accidentally-English
    entries."""
    def has_cjk(text: str) -> bool:
        return any(
            "\u4e00" <= ch <= "\u9fff"          # CJK unified ideographs
            or "\u3000" <= ch <= "\u303f"        # CJK punctuation （、。「」)
            or "\uff00" <= ch <= "\uffef"        # full-width forms ：！？
            for ch in text
        )
    assert ZH_CN, "catalog is not empty"
    for key, value in ZH_CN.items():
        assert isinstance(key, str) and key and key.strip() == key
        assert isinstance(value, str) and value and value.strip() == value
        assert has_cjk(value), key


def test_config_ui_language_roundtrip(tmp_path):
    path = str(tmp_path / "config.json")
    assert DeeptorrentConfig.from_file(path).ui_language == "en"

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ui_language": "zh"}, f)
    assert DeeptorrentConfig.from_file(path).ui_language == "zh"

    # invalid values fail closed to English, not to a crash
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ui_language": "klingon"}, f)
    assert DeeptorrentConfig.from_file(path).ui_language == "en"

    # to_file persists the field (asdict round trip)
    DeeptorrentConfig(ui_language="zh").to_file(path)
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["ui_language"] == "zh"


def test_settings_pages_constants_stay_english_but_translate():
    """gui/__init__ imports the whole package (via main_window) before any
    caller can set the language, so the shared page constants MUST stay
    English — tr() happens at render time and must cover every label."""
    pytest.importorskip("PySide6")
    from gui.settings_dialog import (
        API_KEY_PAGES,
        BROWSER_SETTINGS_PAGES,
        DOWNLOAD_SETTINGS_PAGES,
    )
    from gui.i18n_zh_cn import ZH_CN as _CATALOG
    labels = [lbl for lbl, _ in
              API_KEY_PAGES + BROWSER_SETTINGS_PAGES + DOWNLOAD_SETTINGS_PAGES]
    # the constants themselves are pure English (the freeze-proof rule)
    assert not any(any("\u4e00" <= ch <= "\u9fff" for ch in lbl) for lbl in labels)
    # and every one of them has a catalog entry that is actually Chinese
    for lbl in labels:
        assert lbl in _CATALOG, f"missing zh catalog entry: {lbl!r}"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in _CATALOG[lbl]), lbl
        # the Settings Hub rstrip("…") contract survives translation
        assert _CATALOG[lbl].rstrip("…")


def test_help_dialog_shows_chinese_guide_under_zh():
    """The in-app User Guide swaps to the translated copy (phase 3)."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QTextBrowser
    _app = QApplication.instance() or QApplication([])
    set_language("zh")
    try:
        from gui.help_dialog import HelpDialog
        dlg = HelpDialog()
        browser = dlg.findChild(QTextBrowser)
        assert browser is not None, "guide text browser not found"
        html = browser.toHtml()
        assert "用户指南" in html
        assert "DeepFlux 5.2" in html  # version string travels with the guide
        assert "Quick Start" not in html
        dlg.deleteLater()
    finally:
        set_language("en")
