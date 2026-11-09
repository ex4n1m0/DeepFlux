"""Tests for the multiview grid (gui/multiview.py + PlayerWidget glue):
the 4×4 classic plus the 2×2 and dual-split (1×2 / 2×1) layouts.

Real mpv backends are replaced with a recording fake so the tests can assert
on the tuning calls (muted start, small caches, no post-processing) without
a display or media files.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class FakeBackend:
    """Records every PlayerBackend call the tile/grid makes."""

    def __init__(self, parent_widget: Any) -> None:
        self.calls: List[tuple] = []

    def create(self) -> bool:
        return True

    def destroy(self) -> None:
        self.calls.append(("destroy",))

    def play(self, url: str, headers: Optional[dict] = None) -> None:
        self.calls.append(("play", url, dict(headers or {})))

    def stop(self) -> None:
        self.calls.append(("stop",))

    def pause(self) -> None:
        self.calls.append(("pause",))

    def resume(self) -> None:
        self.calls.append(("resume",))

    def seek_by(self, delta: float) -> None:
        self.calls.append(("seek_by", delta))

    def set_volume(self, pct: int) -> None:
        self.calls.append(("volume", pct))

    def set_mute(self, muted: bool) -> None:
        self.calls.append(("mute", muted))

    def set_hwdec(self, mode: str) -> None:
        self.calls.append(("hwdec", mode))

    def set_cache(self, seconds: int, max_bytes: Optional[int] = None) -> None:
        self.calls.append(("cache", seconds, max_bytes))

    def set_smooth_video(self, on: bool) -> None:
        self.calls.append(("smooth", on))

    def set_interpolation(self, on: bool) -> None:
        self.calls.append(("interpolation", on))


@dataclass
class FakeSource:
    user_agent: str = ""
    referer: str = ""


class FakeManager:
    """The manager surface multiview touches: active_source() + search()."""

    def __init__(self) -> None:
        self.source = FakeSource()

    def active_source(self) -> FakeSource:
        return self.source

    def search(self, text: str) -> Dict[str, list]:
        from iptv.models import Channel, SECTION_LIVE
        return {SECTION_LIVE: [Channel(id="1", name=f"Chan {text}", url="http://x/1.m3u8")]}


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def fake_backends(monkeypatch):
    """Patch multiview.create_backend to hand out recording fakes."""
    import gui.multiview as mv
    made: List[FakeBackend] = []
    original = mv.create_backend

    def _factory(parent_widget, preferred="mpv", svp=False):
        assert svp is False, "grid tiles must never build the SVP backend"
        b = FakeBackend(parent_widget)
        made.append(b)
        return b

    monkeypatch.setattr(mv, "create_backend", _factory)
    return made, original


def _grid(qapp, fake_backends):
    from config import DeeptorrentConfig
    from gui.multiview import MultiViewGrid
    return MultiViewGrid(FakeManager(), DeeptorrentConfig())


def test_tiles_start_muted_with_lightweight_tuning(qapp, fake_backends):
    made, _ = fake_backends
    grid = _grid(qapp, fake_backends)
    assert len(grid.tiles) == 16

    tile = grid.tiles[0]
    ch = type("Item", (), {"url": "http://x/v.mkv", "name": "V",
                           "extra": {}})()
    from iptv.models import Channel
    ok = tile.assign(Channel(id="v", name="V", url="C:/v.mkv"), is_stream=False)
    assert ok

    calls = made[0].calls
    # No post-processing, tiny cache, muted from the very first assignment —
    # there is never a window where sixteen tiles make noise.
    assert ("smooth", False) in calls
    assert ("interpolation", False) in calls
    assert any(c[0] == "cache" and c[1] == 2 and c[2] <= 16 * 1024 * 1024
               for c in calls)
    assert ("mute", True) in calls
    assert calls.count(("mute", True)) >= 1
    # The play call itself comes after the initial mute.
    assert ("play", "C:/v.mkv", {}) in calls
    assert calls.index(("mute", True)) < calls.index(("play", "C:/v.mkv", {}))


def test_multiple_stream_tiles_allowed(qapp, fake_backends):
    """Owner decision 2026-11-09: the old one-stream-per-grid cap is GONE —
    any number of tiles may carry IPTV streams simultaneously (a provider
    that caps concurrent connections resets what it resets; the affected
    tile surfaces its own error). Same-tile channel switching still works."""
    from iptv.models import Channel
    grid = _grid(qapp, fake_backends)
    a, b, c = grid.tiles[0], grid.tiles[1], grid.tiles[2]
    cnn = Channel(id="s", name="CNN", url="http://x/cnn.m3u8")
    bbc = Channel(id="s2", name="BBC", url="http://x/bbc.m3u8")
    sky = Channel(id="s3", name="Sky", url="http://x/sky.m3u8")

    assert grid.assign_stream(a, cnn) is True
    # a second and third stream tile are accepted now
    assert grid.assign_stream(b, bbc) is True
    assert grid.assign_stream(c, sky) is True
    assert a.channel is not None and b.channel is not None and c.channel is not None
    # stream_tile reports the FIRST stream tile (diagnostics convenience)
    assert grid.stream_tile is a
    # same-tile switch still replaces the channel in place
    assert grid.assign_stream(b, cnn) is True
    assert b.channel.id == "s"
    # clearing one stream tile leaves the others playing
    a.clear()
    assert grid.stream_tile is b
    assert c.is_stream


def test_assign_file_builds_channel_and_missing_file_refused(qapp, fake_backends, tmp_path):
    grid = _grid(qapp, fake_backends)
    tile = grid.tiles[3]
    assert grid.assign_file(tile, "C:/no/such/file.mkv") is False
    assert tile.channel is None

    f = tmp_path / "movie.mkv"
    f.write_bytes(b"0" * 16)
    assert grid.assign_file(tile, str(f)) is True
    assert tile.channel is not None
    assert tile.channel.name == "movie.mkv"
    assert tile.is_stream is False


def test_build_playback_headers_precedence():
    from gui.multiview import build_playback_headers
    from iptv.models import Channel

    src = FakeSource(user_agent="SourceUA", referer="http://src/")
    ch = Channel(id="1", name="n", url="http://x/",
                 extra={"extvlcopt": ["http-user-agent=EntryUA"]})
    h = build_playback_headers(ch, src)
    # EXTVLCOPT overrides the source UA; untouched source referer survives.
    assert h["User-Agent"] == "EntryUA"
    assert h["Referer"] == "http://src/"

    ch2 = Channel(id="2", name="n", url="http://x/",
                  extra={"headers": {"Cookie": "a=b"}})
    assert build_playback_headers(ch2, None) == {"Cookie": "a=b"}
    assert build_playback_headers(ch2, FakeSource()) == {"Cookie": "a=b"}


def test_bulk_fill_deals_files_to_empty_tiles_in_order(qapp, fake_backends, tmp_path):
    """Multi-select fill: sorted order, empty tiles only (a filled tile —
    e.g. the single IPTV stream — is never overwritten), extra files
    dropped, nonexistent paths skipped."""
    from iptv.models import Channel
    grid = _grid(qapp, fake_backends)

    # Tile 0 occupied by the stream; files must start at tile 1.
    stream = Channel(id="s", name="CNN", url="http://x/cnn.m3u8")
    assert grid.assign_stream(grid.tiles[0], stream) is True

    files = []
    for name in ("b_movie.mkv", "a_movie.mkv", "c_movie.mkv"):
        f = tmp_path / name
        f.write_bytes(b"0" * 8)
        files.append(str(f))
    files.append(str(tmp_path / "missing.mkv"))        # skipped
    files += [str(tmp_path / "e_movie.mkv"), str(tmp_path / "d_movie.mkv")]
    for name in ("e_movie.mkv", "d_movie.mkv"):
        (tmp_path / name).write_bytes(b"0" * 8)

    added = grid.fill_from_files(files)
    assert added == 5
    assert grid.tiles[0].channel is stream  # untouched
    names = [t.channel.name for t in grid.tiles[1:6]]
    assert names == ["a_movie.mkv", "b_movie.mkv", "c_movie.mkv",
                     "d_movie.mkv", "e_movie.mkv"]
    assert all(t.channel is None for t in grid.tiles[6:])
    assert all(not t.is_stream for t in grid.tiles[1:6])


def test_bulk_fill_caps_at_empty_tile_count(qapp, fake_backends, tmp_path):
    grid = _grid(qapp, fake_backends)
    files = []
    for i in range(30):
        f = tmp_path / f"v{i:02d}.mp4"
        f.write_bytes(b"0" * 8)
        files.append(str(f))
    assert grid.fill_from_files(files) == 16
    assert all(t.channel is not None for t in grid.tiles)
    # First 16 alphabetically: v00..v15.
    assert grid.tiles[0].channel.name == "v00.mp4"
    assert grid.tiles[15].channel.name == "v15.mp4"


def test_skip_all_seeks_every_loaded_tile(qapp, fake_backends, tmp_path):
    """The ⏩ button advances every loaded clip 10s at once; empty tiles are
    untouched; repeat presses keep advancing (stateless per-press seek)."""
    from iptv.models import Channel
    made, _ = fake_backends
    grid = _grid(qapp, fake_backends)
    for i in range(3):
        f = tmp_path / f"v{i}.mp4"
        f.write_bytes(b"0" * 8)
        grid.assign_file(grid.tiles[i], str(f))
    grid.tiles[0].clear()  # freed square — its (destroyed) backend isn't touched

    grid.skip_all(10)
    seekers = [b for b in made if ("seek_by", 10) in b.calls]
    assert len(seekers) == 2  # tiles 1 and 2 only

    # Repeated presses accumulate — one more press = one more seek each.
    grid.skip_all(10)
    for b in seekers:
        assert b.calls.count(("seek_by", 10)) == 2

    # Every bar button is wired to its own delta, in order.
    fired = []
    orig = grid.skip_all
    grid.skip_all = lambda s: fired.append(s)  # type: ignore[method-assign]
    for btn in grid.skip_buttons:
        btn.click()
    grid.skip_all = orig  # type: ignore[method-assign]
    assert fired == [-60, -10, 10, 60]


def test_assign_resumes_even_when_backend_sits_paused(qapp, fake_backends):
    """mpv's pause property survives loadfile: a tile that ended under
    keep-open stays paused, so every assign() must explicitly resume or the
    folder rotation loads the next video paused (reported bug)."""
    from iptv.models import Channel
    made, _ = fake_backends
    grid = _grid(qapp, fake_backends)
    tile = grid.tiles[0]
    tile.assign(Channel(id="a", name="A", url="a.mkv"), is_stream=False)
    calls = [c[0] for c in made[0].calls]
    assert "resume" in calls
    assert calls.index("play") < calls.index("resume") < len(calls)


def test_tile_play_pause_button(qapp, fake_backends, tmp_path):
    from iptv.models import Channel
    made, _ = fake_backends
    grid = _grid(qapp, fake_backends)
    tile = grid.tiles[0]
    # Empty tile: button disabled and toggling is a no-op.
    assert not tile.play_btn.isEnabled()
    tile._toggle_pause()
    assert all(("pause",) not in b.calls for b in made)

    f = tmp_path / "v.mp4"
    f.write_bytes(b"0" * 8)
    grid.assign_file(tile, str(f))
    assert tile.play_btn.isEnabled()
    assert tile.play_btn.text() == "⏸"

    tile._toggle_pause()
    assert ("pause",) in made[0].calls
    assert tile.play_btn.text() == "▶"
    tile._toggle_pause()
    assert ("resume",) in made[0].calls
    assert tile.play_btn.text() == "⏸"

    # Backend-driven transitions update the button too (mpv's own pause
    # observer): a stray "paused" flips it to ▶, "playing" flips it back.
    tile.backend.on_state("paused")
    assert tile.play_btn.text() == "▶"
    tile.backend.on_state("playing")
    assert tile.play_btn.text() == "⏸"


def test_folder_mode_rotates_ended_tiles_until_exhausted(qapp, fake_backends, tmp_path):
    """Folder mode: first 16 videos fill the grid in order; a tile whose
    video ends picks up the next unplayed file; once the folder is
    exhausted an ending tile is freed (each video plays exactly once)."""
    from gui.multiview import MultiViewGrid
    files = []
    for i in range(18):
        f = tmp_path / f"v{i:02d}.mp4"
        f.write_bytes(b"0" * 8)
        files.append(str(f))

    grid = _grid(qapp, fake_backends)
    assert grid.start_folder(str(tmp_path)) == 16
    assert [t.channel.name for t in grid.tiles[:4]] == ["v00.mp4", "v01.mp4",
                                                        "v02.mp4", "v03.mp4"]
    # EOF is delivered exactly like the real backend does: on_state("stopped")
    # from the event thread (the tile re-emits it via its queued Qt signal).
    grid.tiles[0].backend.on_state("stopped")
    assert grid.tiles[0].channel.name == "v16.mp4"
    grid.tiles[0].backend.on_state("stopped")
    assert grid.tiles[0].channel.name == "v17.mp4"
    # Folder exhausted now: the next natural end frees the tile instead of
    # refilling it, and never replays an earlier file.
    grid.tiles[0].backend.on_state("stopped")
    assert grid.tiles[0].channel is None
    grid.tiles[5].backend.on_state("stopped")
    assert grid.tiles[5].channel is None
    assert grid.tiles[1].channel.name == "v01.mp4"  # others keep playing


def test_folder_mode_replaces_grid_content_and_filters_scan(qapp, fake_backends, tmp_path):
    """Starting folder mode clears everything (stream tile included); the
    scan is recursive, video-extension-only, sorted, and skips samples."""
    from iptv.models import Channel
    (tmp_path / "b_sub").mkdir()
    (tmp_path / "a.mp4").write_bytes(b"0" * 8)
    (tmp_path / "z.nfo").write_bytes(b"x")            # not a video
    (tmp_path / "b_sub" / "sample.mkv").write_bytes(b"0")  # sample junk
    (tmp_path / "b_sub" / "m.mkv").write_bytes(b"0" * 8)
    (tmp_path / "c.txt").write_text("x")

    grid = _grid(qapp, fake_backends)
    assert grid.assign_stream(
        grid.tiles[0],
        Channel(id="s", name="CNN", url="http://x/cnn.m3u8")) is True
    assert grid.stream_tile is grid.tiles[0]

    assert grid.start_folder(str(tmp_path)) == 2
    assert grid.stream_tile is None
    names = [t.channel.name for t in grid.tiles if t.channel]
    assert names == ["a.mp4", "m.mkv"]  # subfolder file included, sorted


def test_folder_mode_user_cleared_tile_stays_empty(qapp, fake_backends, tmp_path):
    """A manual ✕ clear must not read as an EOF — the square stays free
    while the rest of the grid keeps rotating."""
    for i in range(18):
        (tmp_path / f"v{i:02d}.mp4").write_bytes(b"0" * 8)
    grid = _grid(qapp, fake_backends)
    grid.start_folder(str(tmp_path))
    grid.tiles[0].clear()  # user action
    assert grid.tiles[0].channel is None
    grid.tiles[1].backend.on_state("stopped")
    assert grid.tiles[1].channel.name == "v16.mp4"
    assert grid.tiles[0].channel is None  # not auto-refilled
    grid.shutdown()


def test_long_filenames_do_not_inflate_grid_minimum_size(qapp, fake_backends):
    """A QLabel's minimumSizeHint is its full text width: without the
    Ignored size policy + eliding, one long scene filename widened the
    tile (and the whole 4-column grid) far past the video pane."""
    from iptv.models import Channel
    grid = _grid(qapp, fake_backends)
    before = grid.minimumSizeHint()

    long_name = ("Some.Great.Movie.2026.2160p.WEB-DL.DDP5.1.Atmos.HDR.H.264-"
                 "RELEASEGROUP.mkv")
    grid.tiles[0].assign(Channel(id="x", name=long_name, url="C:/x.mkv"),
                         is_stream=False)
    grid.tiles[1].assign(Channel(id="y", name=long_name, url="C:/y.mkv"),
                         is_stream=False)
    grid.tiles[2].assign(Channel(id="z", name=long_name, url="C:/z.mkv"),
                         is_stream=False)

    after = grid.minimumSizeHint()
    # The buttons/slider still set a floor, but three 80-char names must not
    # widen it either — a single tile's min width stays under a quarter of a
    # sane player pane.
    assert after.width() <= before.width() + 40
    assert grid.tiles[0].name_lbl.text() != long_name  # elided, not full


def test_shutdown_stops_and_destroys_every_backend(qapp, fake_backends):
    made, _ = fake_backends
    from iptv.models import Channel
    grid = _grid(qapp, fake_backends)
    grid.tiles[0].assign(Channel(id="a", name="A", url="a.mkv"), False)
    grid.tiles[5].assign(Channel(id="b", name="B", url="b.mkv"), False)
    grid.shutdown()
    for b in made:
        assert ("stop",) in b.calls
        assert ("destroy",) in b.calls
    for t in grid.tiles:
        assert t.channel is None


def test_shutdown_does_not_wait_for_stuck_backend(qapp, fake_backends, monkeypatch):
    """Sixteen sequential mpv terminate() calls made app close take forever;
    teardown is parallel with a hard deadline, so a backend whose destroy()
    never returns cannot stall the GUI thread past DESTROY_DEADLINE_SECS."""
    import time as _time
    import gui.multiview as mv
    from config import DeeptorrentConfig
    from iptv.models import Channel

    class SlowBackend(FakeBackend):
        def destroy(self) -> None:
            self.calls.append(("destroy",))
            _time.sleep(5.0)

    monkeypatch.setattr(mv, "DESTROY_DEADLINE_SECS", 0.3)
    grid = mv.MultiViewGrid(FakeManager(), DeeptorrentConfig())
    for i, tile in enumerate(grid.tiles[:6]):
        tile._backend = SlowBackend(None)
        tile._channel = Channel(id=str(i), name=f"n{i}", url=f"{i}.mkv")
    t0 = _time.monotonic()
    grid.shutdown()
    elapsed = _time.monotonic() - t0
    assert elapsed < 2.0, f"shutdown blocked {elapsed:.1f}s on stuck destroy"
    for tile in grid.tiles:
        assert tile._backend is None
        assert tile.channel is None


def test_grid_is_a_video_stack_page_and_never_reparents_surface(qapp, fake_backends):
    from PySide6.QtWidgets import QStackedWidget
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget
    from iptv.manager import IPTVManager

    made, _ = fake_backends
    player = PlayerWidget(IPTVManager(sources=[], tmdb_api_key=""),
                          DeeptorrentConfig())
    assert isinstance(player.video_stack, QStackedWidget)

    surface_wid = player.surface.winId()
    grid = player._ensure_multiview()
    assert player.video_stack.indexOf(grid) >= 0
    # Sibling, never a child: nothing in the grid's chain is the surface.
    node = grid
    while node is not None:
        assert node is not player.surface
        node = node.parent()
    # Embedding the grid must not have recreated the surface's HWND.
    assert player.surface.winId() == surface_wid

    player.mv_btn.setChecked(True)
    assert player.video_stack.currentWidget() is grid
    player.mv_btn.setChecked(False)
    assert player.video_stack.currentWidget() is player.surface


def test_play_exits_multiview_and_promotion_uses_main_player(qapp, fake_backends, monkeypatch):
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget
    from iptv.manager import IPTVManager
    from iptv.models import Channel

    made, _ = fake_backends
    player = PlayerWidget(IPTVManager(sources=[], tmdb_api_key=""),
                          DeeptorrentConfig())
    grid = player._ensure_multiview()

    # The main player's backend is also faked so play() is side-effect free.
    import gui.iptv_tab as tab_mod
    main_backend = FakeBackend(None)
    monkeypatch.setattr(PlayerWidget, "_ensure_backend",
                        lambda self: (self.__dict__.setdefault("_backend", main_backend)
                                      or self.__dict__.setdefault("_media_backend", main_backend)
                                      or True))
    played: list = []
    monkeypatch.setattr(PlayerWidget, "play", lambda self, item, **kw: played.append(item))

    ch = Channel(id="g", name="GridVid", url="C:/v.mkv")
    assert grid.assign_file(grid.tiles[0], ch.url) is False  # not a real file
    grid.tiles[0]._channel = ch  # pretend it was assigned

    player.mv_btn.setChecked(True)
    assert player.video_stack.currentWidget() is grid

    # Promotion: tile is cleared, main player's play() gets the channel,
    # grid mode is off and every tile backend was shut down.
    grid._promote(grid.tiles[0])
    assert grid.tiles[0].channel is None
    assert played == [ch]
    assert player.video_stack.currentWidget() is player.surface
    for b in made:
        assert ("destroy",) in b.calls
    assert player.mv_btn.isChecked() is False


def test_force_stop_all_streams_shuts_down_grid_and_player(qapp, fake_backends):
    """The ⛔ Stop All force-stop tears the multiview grid down (every tile
    backend stopped AND destroyed — providers count lingering sockets) and
    leaves the main player fully idle."""
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget
    from iptv.manager import IPTVManager
    from iptv.models import Channel

    made, _ = fake_backends
    player = PlayerWidget(IPTVManager(sources=[], tmdb_api_key=""),
                          DeeptorrentConfig())
    grid = player._ensure_multiview()
    player.mv_btn.setChecked(True)
    assert player.video_stack.currentWidget() is grid
    stream = Channel(id="s", name="Live", url="http://x/live.m3u8")
    assert grid.assign_stream(grid.tiles[0], stream) is True
    assert grid.stream_tile is grid.tiles[0]

    player.force_stop_all_streams()

    assert player.video_stack.currentWidget() is player.surface
    assert player.mv_btn.isChecked() is False
    assert grid.stream_tile is None
    for tile in grid.tiles:
        assert tile.channel is None
    for backend in made:
        assert ("stop",) in backend.calls
        assert ("destroy",) in backend.calls
    assert player._current_item is None
    assert player._playback_active is False


# -- layouts -----------------------------------------------------------------

def _grid_layout(qapp, fake_backends, name):
    from config import DeeptorrentConfig
    from gui.multiview import MultiViewGrid
    return MultiViewGrid(FakeManager(), DeeptorrentConfig(), layout=name)


def test_layout_registry_and_fallback():
    from gui.multiview import (DEFAULT_LAYOUT, MULTIVIEW_LAYOUTS,
                               multiview_label, multiview_layout)
    assert DEFAULT_LAYOUT == "4x4"
    assert multiview_layout("4x4") == (4, 4)
    assert multiview_layout("2x2") == (2, 2)
    assert multiview_layout("1x2") == (1, 2)   # dual split, side by side
    assert multiview_layout("2x1") == (2, 1)   # dual split, stacked
    # Stale/hand-edited config values degrade to the 4×4, never crash.
    assert multiview_layout("nope") == (4, 4)
    assert multiview_layout("") == (4, 4)
    assert multiview_label("2x2") == MULTIVIEW_LAYOUTS["2x2"][2]
    assert multiview_label("nope") == MULTIVIEW_LAYOUTS["4x4"][2]


@pytest.mark.parametrize("name,count", [
    ("4x4", 16), ("2x2", 4), ("1x2", 2), ("2x1", 2),
])
def test_grid_tile_counts_and_geometry(qapp, fake_backends, name, count):
    """Every layout builds the right tile count, and the QGridLayout seats
    tiles in reading order: tile i at (i // cols, i % cols)."""
    from gui.multiview import multiview_layout
    grid = _grid_layout(qapp, fake_backends, name)
    assert len(grid.tiles) == count
    assert grid.layout_name == name
    rows, cols = multiview_layout(name)
    assert (grid.rows, grid.cols) == (rows, cols)
    lay = grid.tiles[0].parentWidget().layout()
    for i, tile in enumerate(grid.tiles):
        r, c = divmod(i, cols)
        assert lay.getItemPosition(lay.indexOf(tile)) == (r, c, 1, 1)


def test_grid_bar_combo_lists_layouts_and_emits(qapp, fake_backends):
    """The combo in the bar under the grid lists every layout (registry
    order), starts silent on the grid's own shape, and a user pick emits
    sig_layout_requested with the layout NAME."""
    from gui.multiview import MULTIVIEW_LAYOUTS
    grid = _grid_layout(qapp, fake_backends, "2x2")
    names = [grid.layout_combo.itemData(i)
             for i in range(grid.layout_combo.count())]
    assert names == list(MULTIVIEW_LAYOUTS)
    assert grid.layout_combo.currentData() == "2x2"

    got: list = []
    grid.sig_layout_requested.connect(got.append)
    grid.layout_combo.setCurrentIndex(names.index("1x2"))
    assert got == ["1x2"]


def test_dual_mode_hosts_two_simultaneous_streams(qapp, fake_backends):
    """Owner decision 2026-11-09: the multi-stream allowance is geometry-
    independent — even a two-tile dual split can host two IPTV streams."""
    from iptv.models import Channel
    grid = _grid_layout(qapp, fake_backends, "1x2")
    a, b = grid.tiles
    assert grid.assign_stream(
        a, Channel(id="s", name="CNN", url="http://x/cnn.m3u8")) is True
    assert grid.assign_stream(
        b, Channel(id="s2", name="BBC", url="http://x/bbc.m3u8")) is True
    assert a.is_stream and b.is_stream


def test_folder_mode_caps_at_tile_count_2x2(qapp, fake_backends, tmp_path):
    """Folder auto-rotate fills at most as many tiles as the layout has and
    keeps rotating the overflow in — count-agnostic, same as the 4×4."""
    for i in range(6):
        (tmp_path / f"v{i:02d}.mp4").write_bytes(b"0" * 8)
    grid = _grid_layout(qapp, fake_backends, "2x2")
    assert grid.start_folder(str(tmp_path)) == 4
    assert [t.channel.name for t in grid.tiles] == [
        "v00.mp4", "v01.mp4", "v02.mp4", "v03.mp4"]
    grid.tiles[0].backend.on_state("stopped")  # EOF → next unplayed file
    assert grid.tiles[0].channel.name == "v04.mp4"
    grid.tiles[0].backend.on_state("stopped")
    assert grid.tiles[0].channel.name == "v05.mp4"
    grid.tiles[0].backend.on_state("stopped")  # exhausted → tile freed
    assert grid.tiles[0].channel is None
    assert grid.tiles[1].channel.name == "v01.mp4"


def test_bulk_fill_caps_at_tile_count_2x2(qapp, fake_backends, tmp_path):
    files = []
    for i in range(6):
        f = tmp_path / f"v{i}.mp4"
        f.write_bytes(b"0" * 8)
        files.append(str(f))
    grid = _grid_layout(qapp, fake_backends, "2x2")
    assert grid.fill_from_files(files) == 4
    assert all(t.channel is not None for t in grid.tiles)
    assert grid.tiles[0].channel.name == "v0.mp4"
    assert grid.tiles[3].channel.name == "v3.mp4"


def _player(qapp, fake_backends, config=None):
    from config import DeeptorrentConfig
    from gui.iptv_tab import PlayerWidget
    from iptv.manager import IPTVManager
    cfg = config if config is not None else DeeptorrentConfig()
    return PlayerWidget(IPTVManager(sources=[], tmdb_api_key=""), cfg)


def test_set_multiview_layout_switches_live(qapp, fake_backends):
    """Switching layout while a grid is showing: every old tile backend is
    stopped AND destroyed, the old page leaves the video stack (no page
    accumulation, surfaces never reparented), a fresh grid of the new shape
    is shown, the ▦ button STAYS checked and its label moves, and the
    choice persists into the config."""
    from iptv.models import Channel

    made, _ = fake_backends
    from config import DeeptorrentConfig
    cfg = DeeptorrentConfig()
    player = _player(qapp, fake_backends, config=cfg)
    old = player._ensure_multiview()
    player.mv_btn.setChecked(True)
    assert player.video_stack.currentWidget() is old
    old.tiles[0].assign(Channel(id="a", name="A", url="a.mkv"), False)

    player._set_multiview_layout("2x2")

    for b in made:
        assert ("stop",) in b.calls
        assert ("destroy",) in b.calls
    assert player.video_stack.indexOf(old) == -1
    new = player._multiview_grid
    assert new is not None and new is not old
    assert len(new.tiles) == 4
    assert player.video_stack.currentWidget() is new
    assert player.mv_btn.isChecked() is True  # still in multiview
    assert "2×2" in player.mv_btn.text()
    assert cfg.iptv.multiview_layout == "2x2"


def test_set_multiview_layout_while_off_only_remembers(qapp, fake_backends):
    """With no grid up, a layout switch must not build anything — the next
    toggle opens the chosen shape. Unknown names are ignored entirely."""
    player = _player(qapp, fake_backends)
    player._set_multiview_layout("1x2")
    assert player._multiview_grid is None
    assert "1×2" in player.mv_btn.text()
    player.mv_btn.setChecked(True)
    assert player._multiview_grid is not None
    assert len(player._multiview_grid.tiles) == 2

    # Unknown names never change anything.
    before = player._multiview_layout
    player._set_multiview_layout("nonsense")
    assert player._multiview_layout == before


def test_layout_change_disposes_hidden_grid_page(qapp, fake_backends):
    """Leaving multiview keeps the emptied widget as a hidden stack page
    (today's behavior); a later layout change must drop it, so the next
    toggle builds the NEW shape instead of resurrecting the old one."""
    player = _player(qapp, fake_backends)
    grid = player._ensure_multiview()
    player.mv_btn.setChecked(True)
    player.mv_btn.setChecked(False)
    assert player._multiview_grid is grid

    player._set_multiview_layout("1x2")
    assert player._multiview_grid is None
    player.mv_btn.setChecked(True)
    assert player._multiview_grid is not grid
    assert len(player._multiview_grid.tiles) == 2


def test_repeated_layout_switches_do_not_accumulate_pages(qapp, fake_backends):
    player = _player(qapp, fake_backends)
    player.mv_btn.setChecked(True)
    n0 = player.video_stack.count()
    for name in ("2x2", "1x2", "2x1", "4x4"):
        player._set_multiview_layout(name)
        assert player.video_stack.count() == n0
        assert player.video_stack.currentWidget() is player._multiview_grid
    assert len(player._multiview_grid.tiles) == 16


def test_config_multiview_layout_default_and_stale_boot():
    """Fresh configs default to 4×4; a stale persisted value boots into the
    4×4 via the registry fallback instead of crashing."""
    from config import DeeptorrentConfig
    from gui.multiview import multiview_layout
    assert DeeptorrentConfig().iptv.multiview_layout == "4x4"
    assert multiview_layout("bogus") == (4, 4)


def test_config_round_trips_multiview_layout(tmp_path):
    """to_file/from_file must carry the layout across restarts. Regression:
    the field was once written by to_file but never read back by from_file,
    so every restart silently reset to 4×4."""
    from config import DeeptorrentConfig
    p = tmp_path / "config.json"
    cfg = DeeptorrentConfig()
    cfg.iptv.multiview_layout = "1x2"
    cfg.to_file(str(p))
    loaded = DeeptorrentConfig.from_file(str(p))
    assert loaded.iptv.multiview_layout == "1x2"


def test_combo_pick_reaches_player_and_switches_grid(qapp, fake_backends):
    """End-to-end wiring: the bar combo's signal is the ONLY wire to the
    player — picking 2×2 in a live 4×4 must rebuild the grid."""
    player = _player(qapp, fake_backends)
    player.mv_btn.setChecked(True)
    grid = player._multiview_grid
    assert grid is not None

    grid.layout_combo.setCurrentIndex(grid.layout_combo.findData("2x2"))

    assert player._multiview_layout == "2x2"
    assert player._multiview_grid is not grid
    assert len(player._multiview_grid.tiles) == 4
    assert player.video_stack.currentWidget() is player._multiview_grid
    assert player.mv_btn.isChecked() is True


def test_stale_config_layout_falls_back_at_every_guard(qapp, fake_backends):
    """A bogus persisted value is clamped at BOTH read sites: the
    PlayerWidget init expression and the MultiViewGrid constructor."""
    from config import DeeptorrentConfig
    from gui.multiview import MultiViewGrid
    cfg = DeeptorrentConfig()
    cfg.iptv.multiview_layout = "bogus"
    player = _player(qapp, fake_backends, config=cfg)
    assert player._multiview_layout == "4x4"
    assert "4×4" in player.mv_btn.text()
    player.mv_btn.setChecked(True)
    assert len(player._multiview_grid.tiles) == 16

    grid = MultiViewGrid(FakeManager(), DeeptorrentConfig(), layout="junk")
    assert grid.layout_name == "4x4"
    assert len(grid.tiles) == 16


def test_dispose_defers_widget_deletion_until_destroys_finish(
        qapp, fake_backends, monkeypatch):
    """dispose() = shutdown + SELF-DELETE only after the destroy threads
    exit: a stuck mpv terminate() must not have its tile surface HWND torn
    down underneath it (deleteLater before the threads die would)."""
    import time as _time
    import gui.multiview as mv

    class SlowBackend(FakeBackend):
        def destroy(self) -> None:
            self.calls.append(("destroy",))
            _time.sleep(0.4)

    # The bounded join gives up fast, so dispose() returns while the
    # destroys are still mid-flight — exactly the window the deferral
    # covers.
    monkeypatch.setattr(mv, "DESTROY_DEADLINE_SECS", 0.05)
    grid = _grid(qapp, fake_backends)
    for tile in grid.tiles[:3]:
        tile._backend = SlowBackend(None)

    deleted: list = []
    orig_delete = grid.deleteLater

    def _spy_delete() -> None:
        deleted.append(True)
        orig_delete()

    grid.deleteLater = _spy_delete  # type: ignore[method-assign]
    grid.dispose()
    assert not deleted  # threads still inside terminate()

    for _ in range(60):  # ≤3s: 0.4s destroys + 200ms poll cadence
        if deleted:
            break
        _time.sleep(0.05)
        qapp.processEvents()
    assert deleted  # deferred delete fired once the threads exited
