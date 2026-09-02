from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt

from dlmgr import http_client
from dlmgr.extractors import ExtractorRegistry
from dlmgr.extractors.generic import GenericExtractor, NoStreamFound
from dlmgr.extractors.page_scan import find_stream, find_stream_urls, page_title, unpack_packed_js
from dlmgr.site_grabber import GrabberError, GrabberVideo, SiteGrabber

SITE = "https://videos.example"
WATCH_URL = f"{SITE}/en/abc-123"
MASTER = "https://cdn.example/v/abc/playlist.m3u8"


def _card(code: str, title_html: str, duration: str, split_links: bool = True) -> str:
    """One result card. ``split_links`` mimics sites that wrap the thumbnail,
    the duration badge and the title in three separate anchors to one href."""
    href = f"{SITE}/en/{code}"
    thumb = f'<img class="lazy" data-src="https://img.example/{code}/cover.jpg" src="data:image/png;base64,iVBOR" alt="{title_html}">'
    if split_links:
        return (
            f'<div class="card"><a href="{href}">{thumb}</a>'
            f'<a href="{href}"><span class="badge">{duration}</span></a>'
            f'<div class="t"><a href="{href}">{code.upper()} {title_html}</a></div></div>'
        )
    return f'<a href="{href}">{thumb}<span>{duration}</span><p>{title_html}</p></a>'


SEARCH_HTML = (
    "<html><body>"
    f'<a href="{SITE}/en">Home</a>'
    f'<a href="{SITE}/en/actresses/Someone"><img src="/a.jpg" alt="Someone"></a>'
    f'<a href="{SITE}/en/genres/x"><img src="/g.jpg" alt="Genre"></a>'
    f'<a href="{SITE}/en/new">New</a>'
    '<a href="https://other.example/en/zzz-1"><img src="/z.jpg" alt="Off-site card"></a>'
    + _card("abc-123", "Some &amp; title &#039;quoted&#039;", "2:52:14")
    + _card("def-456", "Another title", " 0:44:06 ", split_links=False)
    + _card("abc-123", "duplicate anchor for the same video", "2:52:14")
    + "</body></html>"
)

# Hand-built p,a,c,k,e,d block: payload tokens are radix-36 keys into the
# keyword list, punctuation stays literal. Unpacks to
#   source='https://cdn.example/v/abc/playlist.m3u8';alt='https://cdn.example/v/abc/720p/video.m3u8';
PACKED = (
    "eval(function(p,a,c,k,e,d){e=function(c){return c.toString(36)};if(!''.replace(/^/,String))"
    "{while(c--){d[c.toString(a)]=k[c]||c.toString(a)}k=[function(e){return d[e]}];e=function(){return'\\\\w+'};c=1};"
    "while(c--){if(k[c]){p=p.replace(new RegExp('\\\\b'+e(c)+'\\\\b','g'),k[c])}}return p}"
    "('0=\\'1://2.3/4/5/6.7\\';8=\\'1://2.3/4/5/9/a.7\\';',36,11,"
    "'source|https|cdn|example|v|abc|playlist|m3u8|alt|720p|video'.split('|'),0,{}))"
)

WATCH_HTML = f"""
<html><head><title>ABC-123 Some title - Videos Example</title>
<meta property="og:title" content="ABC-123 Some title"></head>
<body>
<h1>ABC-123 Some title</h1>
<div class="related"><a href="{SITE}/en/xyz-9"><video data-src="https://img.example/xyz/preview.mp4"></video></a></div>
<script>{PACKED}</script>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200, url: str = "",
                 content_type: str = "text/html; charset=utf-8") -> None:
        self.text = text
        self.status_code = status_code
        self.content = text.encode("utf-8")
        self.url = url
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def fake_http(monkeypatch):
    """Route http_client.get/head through a URL -> response table."""
    table: dict = {}
    calls: list = []

    def _lookup(url):
        for key, value in table.items():
            if callable(key) and key(url):
                return value
            if key == url:
                return value
        return _FakeResponse("<html>not found</html>", 404, url)

    def _get(url, headers=None, timeout=None, **_kwargs):
        calls.append(("GET", url))
        value = _lookup(url)
        if isinstance(value, Exception):
            raise value
        value.url = value.url or url
        return value

    def _head(url, headers=None, timeout=None, allow_redirects=True, **_kwargs):
        calls.append(("HEAD", url))
        value = _lookup(url)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(http_client, "get", _get)
    monkeypatch.setattr(http_client, "head", _head)
    return SimpleNamespace(table=table, calls=calls)


# --------------------------------------------------------------- page_scan

def test_unpack_packed_js_radix36_and_nested():
    out = unpack_packed_js(PACKED)
    assert out and out[0].startswith("source='https://cdn.example/v/abc/playlist.m3u8'")


def _js_str(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def test_unpack_packed_js_recurses_into_nested_packers():
    inner = ("eval(function(p,a,c,k,e,d){}('%s',36,6,'source|https|cdn|example|playlist|m3u8'"
             ".split('|'),0,{}))" % _js_str("0='1://2.3/4.5';"))
    # An outer packer with an empty dictionary unpacks to exactly its payload.
    outer = "eval(function(p,a,c,k,e,d){}('%s',36,0,''.split('|'),0,{}))" % _js_str(inner)
    chunks = unpack_packed_js(outer)
    assert chunks[0] == inner
    assert chunks[1] == "source='https://cdn.example/playlist.m3u8';"


def test_unpack_packed_js_radix62_uppercase_keys():
    # radix 62: 'A' is key 36; single letters x/y/z are keys 33/34/35 too, so
    # the dictionary maps them back to themselves. "m3u8" decodes to an index
    # far beyond the dictionary and is kept verbatim.
    keywords = ["k%d" % i for i in range(33)] + ["x", "y", "z", "https"]
    packed = ("eval(function(p,a,c,k,e,r){}('A://x.y/z.m3u8',62,37,'%s'.split('|'),0,{}))"
              % "|".join(keywords))
    assert unpack_packed_js(packed) == ["https://x.y/z.m3u8"]


def test_find_stream_urls_ranks_master_first_and_demotes_previews():
    urls = find_stream_urls(WATCH_HTML, WATCH_URL)
    assert urls[0] == MASTER
    assert urls[1] == "https://cdn.example/v/abc/720p/video.m3u8"
    # The related-video preview mp4 is found by the raw scan but demoted last.
    assert urls[-1] == "https://img.example/xyz/preview.mp4"
    html = '<video src="/media/preview.mp4"></video><source src="/media/full.mp4">'
    ranked = find_stream_urls(html, "https://s.example/p")
    assert ranked == ["https://s.example/media/full.mp4", "https://s.example/media/preview.mp4"]


def test_find_stream_urls_handles_json_escaped_slashes():
    html = '{"file":"https:\\/\\/cdn.example\\/a\\/index.m3u8"}'
    assert find_stream_urls(html) == ["https://cdn.example/a/index.m3u8"]


def test_page_title_prefers_og_then_h1_then_title_without_site_suffix():
    assert page_title(WATCH_HTML, WATCH_URL) == "ABC-123 Some title"
    assert page_title("<h1>Only <b>h1</b></h1>", WATCH_URL) == "Only h1"
    assert page_title("<title>Movie name - Videos Example</title>", WATCH_URL) == "Movie name"
    assert page_title("<html></html>", WATCH_URL) == "abc-123"


def test_find_stream_returns_none_without_media():
    assert find_stream("<html><body>nothing</body></html>", WATCH_URL) is None
    assert find_stream(WATCH_HTML, WATCH_URL)["type"] == "hls"


# ---------------------------------------------------------------- extractor

def test_generic_extractor_direct_urls_need_no_network(fake_http):
    ext = GenericExtractor()
    assert ext.extract("https://c.example/x/master.m3u8")["type"] == "hls"
    assert ext.extract("https://c.example/x/stream.mpd")["type"] == "dash"
    assert ext.extract("https://c.example/x/clip.mp4")["type"] == "file"
    assert fake_http.calls == []


def test_generic_extractor_scans_web_pages(fake_http):
    fake_http.table[WATCH_URL] = _FakeResponse(WATCH_HTML, url=WATCH_URL)
    result = GenericExtractor().extract(WATCH_URL)
    assert result["manifest_url"] == MASTER
    assert result["type"] == "hls"
    assert result["title"] == "ABC-123 Some title"
    assert result["headers"]["Referer"] == WATCH_URL
    assert result["page_url"] == WATCH_URL


def test_generic_extractor_sniffs_content_type_before_scanning(fake_http):
    url = "https://c.example/stream/live"
    fake_http.table[url] = _FakeResponse("", url=url, content_type="application/vnd.apple.mpegurl")
    assert GenericExtractor().extract(url) == {"manifest_url": url, "type": "hls", "title": "live"}


def test_generic_extractor_raises_when_page_has_no_stream(fake_http):
    fake_http.table[WATCH_URL] = _FakeResponse("<html>no media here</html>", url=WATCH_URL)
    with pytest.raises(NoStreamFound):
        GenericExtractor().extract(WATCH_URL)


def test_registry_has_only_generic_builtin_and_tries_it_last(fake_http):
    registry = ExtractorRegistry(user_extractors_dir=os.path.join(os.getcwd(), "no-such-dir"))
    assert [ext.name for ext in registry._extractors] == ["generic"]
    fake_http.table[WATCH_URL] = _FakeResponse(WATCH_HTML, url=WATCH_URL)
    assert registry.extract(WATCH_URL)["manifest_url"] == MASTER


# ------------------------------------------------------------------ search

def test_normalize_site_and_fill_template():
    assert SiteGrabber.normalize_site("videos.example/en/abc") == "https://videos.example"
    assert SiteGrabber.normalize_site("http://videos.example:8080/x") == "http://videos.example:8080"
    with pytest.raises(GrabberError):
        SiteGrabber.normalize_site("")
    tpl = f"{SITE}/en/search/{{query}}"
    assert SiteGrabber.fill_template(tpl, "two words") == f"{SITE}/en/search/two%20words"
    assert SiteGrabber.fill_template(tpl, "two words", page=3) == f"{SITE}/en/search/two%20words?page=3"
    assert SiteGrabber.fill_template(f"{SITE}/search?q={{query}}", "x", page=2) == f"{SITE}/search?q=x&page=2"
    assert SiteGrabber.fill_template(f"{SITE}/s/{{query}}/p/{{page}}", "x", page=4) == f"{SITE}/s/x/p/4"


def test_parse_cards_merges_split_anchors_and_filters_navigation():
    videos = SiteGrabber().parse_cards(SEARCH_HTML, f"{SITE}/en/search/q")
    assert [v.code for v in videos] == ["abc-123", "def-456"]
    first = videos[0]
    assert first.url == WATCH_URL
    assert first.title == "Some & title 'quoted'"  # img alt wins, entities unescaped
    assert first.duration == "2:52:14"
    assert first.thumbnail == "https://img.example/abc-123/cover.jpg"  # data-src beats data: src
    assert first.site == "videos.example"
    second = videos[1]
    assert second.duration == "0:44:06"
    assert second.title == "Another title"


def test_parse_cards_query_urls_get_distinct_slugs():
    html = ('<a href="/watch?v=AAA111"><img src="/1.jpg" alt="First video"></a>'
            '<a href="/watch?v=BBB222"><img src="/2.jpg" alt="Second video"></a>')
    videos = SiteGrabber().parse_cards(html, "https://tube.example/results?q=x")
    assert [v.code for v in videos] == ["watch-AAA111", "watch-BBB222"]
    assert videos[0].url == "https://tube.example/watch?v=AAA111"


def test_search_discovers_pattern_from_form_and_remembers_it(fake_http):
    home = f'<html><form action="/find" method="get"><input type="text" name="q"></form></html>'
    fake_http.table[SITE] = _FakeResponse(home, url=SITE)
    fake_http.table[f"{SITE}/find?q=hello"] = _FakeResponse(SEARCH_HTML)
    grabber = SiteGrabber()
    videos, template = grabber.search(SITE, "hello")
    assert len(videos) == 2
    assert template == f"{SITE}/find?q={{query}}"
    assert grabber.templates["videos.example"] == template
    # Second search uses the remembered template directly (no discovery fetch).
    fake_http.calls.clear()
    grabber.search(SITE, "hello")
    assert fake_http.calls == [("GET", f"{SITE}/find?q=hello")]


def test_search_probes_language_prefixed_patterns(fake_http):
    fake_http.table[f"{SITE}/en/abc-123"] = _FakeResponse("<html>no form</html>", url=f"{SITE}/en/abc-123")
    fake_http.table[f"{SITE}/en/search/hello"] = _FakeResponse(SEARCH_HTML)
    videos, template = SiteGrabber().search(f"{SITE}/en/abc-123", "hello")
    assert template == f"{SITE}/en/search/{{query}}"
    assert len(videos) == 2


def test_search_forced_template_and_paging(fake_http):
    tpl = f"{SITE}/en/search/{{query}}"
    fake_http.table[f"{SITE}/en/search/hello?page=2"] = _FakeResponse(SEARCH_HTML)
    videos, used = SiteGrabber().search(SITE, "hello", page=2, template=tpl)
    assert used == tpl and len(videos) == 2
    with pytest.raises(GrabberError):
        SiteGrabber().search(SITE, "hello", template="https://x.example/nope")  # no {query}


def test_search_errors(fake_http):
    fake_http.table[SITE] = _FakeResponse("<html></html>", url=SITE)
    with pytest.raises(GrabberError):
        SiteGrabber().search(SITE, "hello")  # every pattern 404s → no results
    with pytest.raises(GrabberError):
        SiteGrabber().search(SITE, "   ")
    fake_http.table[f"{SITE}/en/search/hello?page=2"] = _FakeResponse("<html>empty</html>")
    with pytest.raises(GrabberError, match="No more results"):
        SiteGrabber().search(SITE, "hello", page=2, template=f"{SITE}/en/search/{{query}}")


def test_resolve_wraps_extractor_errors(fake_http):
    fake_http.table[WATCH_URL] = _FakeResponse(WATCH_HTML, url=WATCH_URL)
    info = SiteGrabber().resolve(WATCH_URL)
    assert info["manifest_url"] == MASTER and info["headers"]["Referer"] == WATCH_URL
    fake_http.table[WATCH_URL] = _FakeResponse("<html>none</html>", url=WATCH_URL)
    with pytest.raises(GrabberError):
        SiteGrabber().resolve(WATCH_URL)


# ------------------------------------------------------------------ dialog

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _video(code: str) -> GrabberVideo:
    return GrabberVideo(title=f"Title {code}", url=f"{SITE}/en/{code}", code=code, duration="1:00:00")


def _config(**browser):
    defaults = dict(grabber_auto_queue=False, grabber_auto_limit=5, grabber_last_site="",
                    grabber_search_templates={})
    defaults.update(browser)
    return SimpleNamespace(browser=SimpleNamespace(**defaults))


def test_dialog_populate_and_selection(qapp):
    from gui.grabber_dialog import SiteGrabberDialog
    dialog = SiteGrabberDialog(_config(), engine=None, parent=None)
    dialog._on_search_done(([_video("abc-1"), _video("def-2")], f"{SITE}/s/{{query}}"))
    assert dialog.results_list.count() == 2
    assert len(dialog._selected_videos()) == 2  # results start checked
    assert dialog.pattern_input.text() == f"{SITE}/s/{{query}}"  # discovered pattern is shown

    dialog._set_all_checked(False)
    assert dialog._selected_videos() == []
    dialog.results_list.item(1).setCheckState(Qt.Checked)
    assert [v.code for v in dialog._selected_videos()] == ["def-2"]
    assert "2 results — page 1" in dialog.status_label.text()


def test_dialog_prefills_last_site_and_persists_templates(qapp):
    from gui.grabber_dialog import SiteGrabberDialog
    config = _config(grabber_last_site="https://videos.example/en")
    dialog = SiteGrabberDialog(config, engine=None, parent=None)
    assert dialog.site_input.text() == "https://videos.example/en"
    dialog._grabber.templates["videos.example"] = f"{SITE}/s/{{query}}"
    dialog._on_search_done(([_video("abc-1")], f"{SITE}/s/{{query}}"))
    assert config.browser.grabber_search_templates == {"videos.example": f"{SITE}/s/{{query}}"}
    assert config.browser.grabber_last_site == "https://videos.example/en"


def test_dialog_queue_done_unchecks_queued(qapp):
    from gui.grabber_dialog import SiteGrabberDialog
    dialog = SiteGrabberDialog(_config(), engine=None, parent=None)
    videos = [_video("abc-1"), _video("def-2")]
    dialog._on_search_done((videos, ""))

    job = SimpleNamespace(save_path="C:\\downloads\\abc-1.mp4")
    dialog._on_queue_done([(videos[0], job), (videos[1], RuntimeError("resolve failed"))])
    states = [dialog.results_list.item(i).checkState() for i in range(2)]
    assert states[0] == Qt.Unchecked  # queued → unchecked
    assert states[1] == Qt.Checked  # failed → stays checked for retry
    assert "Queued 1" in dialog.status_label.text()
    assert "failed: def-2" in dialog.status_label.text()


def test_dialog_queue_video_routes_streams_and_files(qapp, monkeypatch):
    from gui.grabber_dialog import SiteGrabberDialog
    calls = []
    engine = SimpleNamespace(
        add_stream_job=lambda **kw: calls.append(("stream", kw)) or SimpleNamespace(save_path="s"),
        add_job=lambda **kw: calls.append(("file", kw)) or SimpleNamespace(save_path="f"),
    )
    grabber = SiteGrabber()
    monkeypatch.setattr(grabber, "resolve", lambda url: {
        "manifest_url": MASTER, "type": "hls", "title": "t",
        "headers": {"Referer": url}, "page_url": url})
    SiteGrabberDialog._queue_video(engine, grabber, _video("abc-1"))
    monkeypatch.setattr(grabber, "resolve", lambda url: {
        "manifest_url": "https://cdn.example/clip.webm", "type": "file", "title": "t",
        "headers": {"Referer": url}, "page_url": url})
    SiteGrabberDialog._queue_video(engine, grabber, _video("def-2"))
    assert calls[0][0] == "stream" and calls[0][1]["filename"] == "abc-1.mp4"
    assert calls[0][1]["headers"] == {"Referer": f"{SITE}/en/abc-1"}
    assert calls[1][0] == "file" and calls[1][1]["filename"] == "def-2.webm"


def test_dialog_auto_queue_triggers_download(qapp, monkeypatch):
    from gui.grabber_dialog import SiteGrabberDialog
    dialog = SiteGrabberDialog(_config(grabber_auto_queue=True, grabber_auto_limit=2), engine=None, parent=None)
    assert dialog.auto_queue_cb.isChecked()
    assert dialog.auto_limit_spin.value() == 2

    recorded = []
    monkeypatch.setattr(dialog, "_download", lambda videos: recorded.append(list(videos)))
    dialog._on_search_done(([_video("a-1"), _video("b-2"), _video("c-3")], ""))
    # Search with auto-queue on → download called immediately, capped at the limit.
    assert [[v.code for v in batch] for batch in recorded] == [["a-1", "b-2"]]

    # Already-queued (unchecked) items are never re-picked; the next batch
    # continues from the remaining checked ones.
    dialog.results_list.item(0).setCheckState(Qt.Unchecked)
    dialog.results_list.item(1).setCheckState(Qt.Unchecked)
    assert [v.code for v in dialog._auto_queue_candidates()] == ["c-3"]


def test_dialog_auto_queue_off_by_default(qapp, monkeypatch):
    from gui.grabber_dialog import SiteGrabberDialog
    dialog = SiteGrabberDialog(_config(), engine=None, parent=None)
    assert not dialog.auto_queue_cb.isChecked()
    recorded = []
    monkeypatch.setattr(dialog, "_download", lambda videos: recorded.append(list(videos)))
    dialog._on_search_done(([_video("a-1")], ""))
    assert recorded == []  # manual mode: search never queues by itself


def test_dialog_persists_auto_options(qapp):
    from gui.grabber_dialog import SiteGrabberDialog
    config = _config()
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
    cfg.browser.grabber_last_site = "https://videos.example/en"
    cfg.browser.grabber_search_templates = {"videos.example": f"{SITE}/s/{{query}}"}
    path = str(tmp_path / "config.json")
    cfg.to_file(path)
    loaded = DeeptorrentConfig.from_file(path)
    assert loaded.browser.grabber_auto_queue is True
    assert loaded.browser.grabber_auto_limit == 12
    assert loaded.browser.grabber_last_site == "https://videos.example/en"
    assert loaded.browser.grabber_search_templates == {"videos.example": f"{SITE}/s/{{query}}"}

    fresh = DeeptorrentConfig.from_file(str(tmp_path / "missing.json"))
    assert fresh.browser.grabber_auto_queue is False
    assert fresh.browser.grabber_auto_limit == 5
    assert fresh.browser.grabber_last_site == ""
    assert fresh.browser.grabber_search_templates == {}
