from __future__ import annotations

from pathlib import Path

from dlmgr.browser_extension import BROWSER_EXTENSION_JS


ROOT = Path(__file__).resolve().parents[1]


def test_in_app_extension_uses_authenticated_injected_endpoint():
    assert "__DEEPFLUX_API_PORT__" in BROWSER_EXTENSION_JS
    assert "__DEEPFLUX_API_TOKEN__" in BROWSER_EXTENSION_JS
    assert "Authorization" in BROWSER_EXTENSION_JS
    assert "window.deepflux.sendDownload" in BROWSER_EXTENSION_JS
    assert "window.deepflux.sendPlay" in BROWSER_EXTENSION_JS


def test_chrome_extension_threshold_is_saved_in_bytes():
    options = (ROOT / "chrome_extension" / "options.js").read_text(encoding="utf-8")
    background = (ROOT / "chrome_extension" / "background.js").read_text(encoding="utf-8")

    assert "* 1024 * 1024" in options
    assert "DEFAULT_SIZE_THRESHOLD = 10 * 1024 * 1024" in background
