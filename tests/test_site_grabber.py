from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt

from dlmgr import http_client
from dlmgr.extractors import ExtractorRegistry
from dlmgr.extractors.missav import MissAVExtractor
from dlmgr.site_grabber import GrabberError, GrabberVideo, MissAvSite

UUID = "229de474-85f8-4bd1-be02-86fb370ebeff"
PREVIEW_UUID = "f3c60d7a-ef99-4a65-9fec-56d898e4c0b0"
WATCH_URL = "https://missav.ws/en/goju-322"


def _card(code: str, title_html: str, duration: str, preview_uuid: str) -> str:
    """One search-result card, matching the live site's markup order."""
    return f'''
    <div>
        <div @mouseenter="setPreview('{preview_uuid}')" @mouseleave="setPreview()"
             @click="clickPreview('{preview_uuid}')" class="thumbnail group">
            <a href="https://missav.ws/en/{code}" alt="{code}" >
                <video loop muted playsinline
                       data-src="https://fourhoi.com/{code}/preview.mp4"></video>
                <img class="lozad w-full"
                     data-src="https://fourhoi.com/{code}/cover-t.jpg"
                     src="data:image/png;base64,iVBOR"
                     alt="{title_html}">
            </a>
            <a href="https://missav.ws/en/{code}" alt="{code}" >
                <span class="absolute bottom-1 right-1 rounded-lg px-2 py-1">{duration}</span>
            </a>
        </div>
    </div>
    '''


SEARCH_HTML = (
    "<html><body>"
    '<ul><li><a href="https://missav.ws/en/actresses/Amateur">Amateur</a></li></ul>'
    # A card-shaped actress hit — must be filtered out by parse_search.
    + _card("actresses/amateur-name", "Actress page", "1:00:00",
            "11111111-2222-3333-4444-555555555555")
    + _card("goju-322", "Some &amp; title &#039;quoted&#039; 16", "2:52:14", PREVIEW_UUID)
    + _card("drop-141", "Another title", " 2:22:52 ", "99999999-8888-7777-6666-555555555555")
    + "</body></html>"
)

# The stream UUID sits in an obfuscated script exactly 2 characters before
# the word "seek"; the preview UUID appears elsewhere and must be ignored.
WATCH_HTML = f'''
<html><head><title>GOJU-322 Some title - MissAV</title></head>
<body>
<div class="order-first"><h1>GOJU-322 Some title</h1></div>
<script>
  var previewId = "{PREVIEW_UUID}";
  var m = "{UUID}" seek;
</script>
</body></html>
'''


class _FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.content = b""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def fake_get(monkeypatch):
    calls: list = []

    def install(text: str = "", exc: Exception | None = None, status: int = 200) -> None:
        def _get(url, headers=None, timeout=None, **_kwargs):
            calls.append(url)
            if exc is not None:
                raise exc
            return _FakeResponse(text, status)

        monkeypatch.setattr(http_client, "get", _get)

    return SimpleNamespace(install=install, calls=calls)


# ------------------------------------------------------------- extractor

def test_find_stream_uuid():
    assert MissAVExtractor.find_stream_uuid(WATCH_HTML) == UUID
    assert MissAVExtractor.find_stream_uuid("no pattern here seek seek") == ""
    assert MissAVExtractor.find_stream_uuid("") == ""


def test_can_handle_domains():
    extractor = MissAVExtractor()
    assert extractor.can_handle("https://missav.ws/en/goju-322")
    assert extractor.can_handle("https://www.missav.ai/dm5/en/abc-1")
    assert extractor.can_handle("https://missav.live/en/x")
    assert not extractor.can_handle("https://missav.ws.evil.com/en/x")
    assert not extractor.can_handle("https://example.com/en/goju-322")
    assert not extractor.can_handle("")


def test_extract_builds_surrit_manifest(fake_get):
    fake_get.install(WATCH_HTML)
    result = MissAVExtractor().extract(WATCH_URL)
    assert result["manifest_url"] == f"https://surrit.com/{UUID}/playlist.m3u8"
    assert result["type"] == "hls"
    assert result["title"] == "GOJU-322 Some title"
    assert result["headers"]["Referer"] == WATCH_URL


def test_extract_title_fallbacks(fake_get):
    html = f'<html><title>Only a title - MissAV</title><script>var m="{UUID}" seek;</script></html>'
    fake_get.install(html)
    assert MissAVExtractor().extract(WATCH_URL)["title"] == "Only a title"

    html = f'<html><script>var m="{UUID}" seek;</script></html>'
    fake_get.install(html)
    # No h1/title → fall back to the URL code.
    assert MissAVExtractor().extract(WATCH_URL)["title"] == "goju-322"


def test_extract_raises_without_uuid(fake_get):
    fake_get.install("<html>no uuid</html>")
    with pytest.raises(ValueError):
        MissAVExtractor().extract(WATCH_URL)


def test_registry_loads_missav_before_generic(fake_get):
    registry = ExtractorRegistry()
    names = [extractor.name for extractor in registry._extractors]
    assert names[0] == "missav"
    assert names[-1] == "generic"

    fake_get.install(WATCH_HTML)
    result = registry.extract(WATCH_URL)
    assert result["manifest_url"] == f"https://surrit.com/{UUID}/playlist.m3u8"
    assert result["type"] == "hls"


# ---------------------------------------------------------------- search

def test_build_search_url():
    site = MissAvSite()
    assert site.build_search_url("two words") == "https://missav.ws/en/search/two%20words"
    assert site.build_search_url("two words", page=1) == "https://missav.ws/en/search/two%20words"
    assert site.build_search_url("two words", page=3) == "https://missav.ws/en/search/two%20words?page=3"
    assert site.build_search_url("") == "https://missav.ws/en/search/"


def test_parse_search_cards():
    videos = MissAvSite().parse_search(SEARCH_HTML)
    assert [v.code for v in videos] == ["goju-322", "drop-141"]
    first = videos[0]
    assert first.url == WATCH_URL
    assert first.title == "Some & title 'quoted' 16"  # entities unescaped
    assert first.duration == "2:52:14"
    assert first.thumbnail == "https://fourhoi.com/goju-322/cover-t.jpg"
    assert first.site == "missav"
    assert videos[1].duration == "2:22:52"  # whitespace stripped


def test_parse_search_filters_actress_links():
    videos = MissAvSite().parse_search(SEARCH_HTML)
    assert all("/actresses/" not in v.url for v in videos)


def test_search_success(fake_get):
    fake_get.install(SEARCH_HTML)
    videos = MissAvSite().search("anything")
    assert len(videos) == 2
    assert fake_get.calls == ["https://missav.ws/en/search/anything"]


def test_search_http_failure(fake_get):
    fake_get.install(exc=RuntimeError("boom"))
    with pytest.raises(GrabberError):
        MissAvSite().search("anything")


def test_search_no_results(fake_get):
    fake_get.install("<html>no cards here</html>")
    with pytest.raises(GrabberError):
        MissAvSite().search("anything")


# ----------------------------------------------------------------- dialog

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _video(code: str) -> GrabberVideo:
    return GrabberVideo(
        title=f"Title {code}", url=f"https://missav.ws/en/{code}",
        code=code, thumbnail="", duration="1:00:00")


def test_dialog_populate_and_selection(qapp):
    from gui.grabber_dialog import SiteGrabberDialog
    dialog = SiteGrabberDialog(SimpleNamespace(), engine=None, parent=None)
    dialog._on_search_done([_video("abc-1"), _video("def-2")])
    assert dialog.results_list.count() == 2
    assert len(dialog._selected_videos()) == 2  # results start checked

    dialog._set_all_checked(False)
    assert dialog._selected_videos() == []
    dialog.results_list.item(1).setCheckState(Qt.Checked)
    assert [v.code for v in dialog._selected_videos()] == ["def-2"]
    assert "2 results — page 1" in dialog.status_label.text()


def test_dialog_queue_done_unchecks_queued(qapp):
    from gui.grabber_dialog import SiteGrabberDialog
    dialog = SiteGrabberDialog(SimpleNamespace(), engine=None, parent=None)
    videos = [_video("abc-1"), _video("def-2")]
    dialog._on_search_done(videos)

    job = SimpleNamespace(save_path="C:\\downloads\\abc-1.mp4")
    dialog._on_queue_done([
        (videos[0], job),
        (videos[1], RuntimeError("resolve failed")),
    ])
    states = [dialog.results_list.item(i).checkState() for i in range(2)]
    assert states[0] == Qt.Unchecked  # queued → unchecked
    assert states[1] == Qt.Checked  # failed → stays checked for retry
    assert "Queued 1" in dialog.status_label.text()
    assert "failed: def-2" in dialog.status_label.text()


def test_dialog_auto_queue_triggers_download(qapp, monkeypatch):
    from gui.grabber_dialog import SiteGrabberDialog
    config = SimpleNamespace(
        browser=SimpleNamespace(grabber_auto_queue=True, grabber_auto_limit=2))
    dialog = SiteGrabberDialog(config, engine=None, parent=None)
    assert dialog.auto_queue_cb.isChecked()
    assert dialog.auto_limit_spin.value() == 2

    recorded = []
    monkeypatch.setattr(dialog, "_download", lambda videos: recorded.append(list(videos)))
    dialog._on_search_done([_video("a-1"), _video("b-2"), _video("c-3")])
    # Search with auto-queue on → download called immediately, capped at the limit.
    assert [[v.code for v in batch] for batch in recorded] == [["a-1", "b-2"]]

    # Already-queued (unchecked) items are never re-picked; the next batch
    # continues from the remaining checked ones.
    dialog.results_list.item(0).setCheckState(Qt.Unchecked)
    dialog.results_list.item(1).setCheckState(Qt.Unchecked)
    assert [v.code for v in dialog._auto_queue_candidates()] == ["c-3"]


def test_dialog_auto_queue_off_by_default(qapp, monkeypatch):
    from gui.grabber_dialog import SiteGrabberDialog
    dialog = SiteGrabberDialog(SimpleNamespace(), engine=None, parent=None)
    assert not dialog.auto_queue_cb.isChecked()

    recorded = []
    monkeypatch.setattr(dialog, "_download", lambda videos: recorded.append(list(videos)))
    dialog._on_search_done([_video("a-1")])
    assert recorded == []  # manual mode: search never queues by itself


def test_dialog_persists_auto_options(qapp):
    from gui.grabber_dialog import SiteGrabberDialog
    config = SimpleNamespace(
        browser=SimpleNamespace(grabber_auto_queue=False, grabber_auto_limit=5))
    dialog = SiteGrabberDialog(config, engine=None, parent=None)
    dialog.auto_queue_cb.setChecked(True)  # fires the toggle handler → persists
    dialog.auto_limit_spin.setValue(9)
    assert config.browser.grabber_auto_queue is True
    assert config.browser.grabber_auto_limit == 9


def test_config_roundtrip_grabber_options(tmp_path):
    from config import DeeptorrentConfig
    cfg = DeeptorrentConfig()
    cfg.browser.grabber_auto_queue = True
    cfg.browser.grabber_auto_limit = 12
    path = str(tmp_path / "config.json")
    cfg.to_file(path)
    loaded = DeeptorrentConfig.from_file(path)
    assert loaded.browser.grabber_auto_queue is True
    assert loaded.browser.grabber_auto_limit == 12

    fresh = DeeptorrentConfig.from_file(str(tmp_path / "missing.json"))
    assert fresh.browser.grabber_auto_queue is False
    assert fresh.browser.grabber_auto_limit == 5
