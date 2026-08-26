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
