"""Mini player (7.8) — the default lightweight boot surface.

Covers: the boot-mode decision (main.py::_decide_boot), the config section
roundtrip, the window's idle/playing states, the staged morph to the full
app (monotonic progress + all tabs + ControlAPI port handoff), the reverse
morph, control-API open routing, and the graceful-shutdown handoff when
closing the mini window while the full app is hidden.

Offscreen like the other GUI tests; conftest's autouse fixtures keep every
config/state path in a per-test temp dir.
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

import pytest

from config import DeeptorrentConfig


# --------------------------------------------------------------------------
# Boot decision (pure function, no Qt)
# --------------------------------------------------------------------------

def test_decide_boot_defaults_to_mini():
    import main
    assert main._decide_boot([], True) == "mini"
    assert main._decide_boot(["C:/x/video.mp4"], True) == "mini"
    assert main._decide_boot(["C:/x/song.flac", "C:/y/a.mkv"], True) == "mini"
    # A video with a MISSING file still goes mini (name says media; the
    # player surfaces "cannot open" instead of the full app's agent error).
    assert main._decide_boot(["C:/gone/movie.mp4"], True) == "mini"


def test_decide_boot_routes_non_media_to_full():
    import main
    assert main._decide_boot(["magnet:?xt=urn:btih:X"], True) == "full"
    assert main._decide_boot(["C:/x/movie.torrent"], True) == "full"
    assert main._decide_boot(["https://example.com/page"], True) == "full"
    assert main._decide_boot(["C:/x/page.html"], True) == "full"
    # Mixed targets: one non-media item drags everything to the full app.
    assert main._decide_boot(["C:/x/video.mp4", "magnet:?xt=urn:btih:X"],
                             True) == "full"
    # Scheme targets whose STRING ends in a media extension (magnet dn=
    # names, direct media URLs) must NOT boot the mini player.
    assert main._decide_boot(
        ["magnet:?xt=urn:btih:X&dn=movie.mp4"], True) == "full"
    assert main._decide_boot(["https://example.com/v.mp4"], True) == "full"
    assert main._decide_boot(["http://example.com/v.mkv"], True) == "full"


def test_decide_boot_unsupported_platform_is_full():
    import main
    # macOS has no embedded player — the full app boots as before.
    assert main._decide_boot([], False) == "full"
    assert main._decide_boot(["C:/x/video.mp4"], False) == "full"


def test_main_module_stays_light():
    """The whole point of the fast path: importing main must NOT drag the
    engine/agent/gui chains (regression guard for the 7.8 restructure)."""
    import subprocess
    import sys
    code = (
        "import sys, main; "
        "heavy = [m for m in sys.modules if m.split('.')[0] in "
        "('gui', 'agent', 'engine', 'dlmgr', 'requests', 'urllib3', 'PySide6')]; "
        "assert not heavy, heavy[:5]"
    )
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


def test_gui_package_stays_light_for_mini_boot():
    """Regression (review finding 1): importing gui.mini_player executes the
    gui package __init__ FIRST — an eager `from .main_window import …`
    there silently dragged the ENTIRE heavy chain (QtWebEngine, engine,
    agent) into the fast boot. The package must stay lazy."""
    import subprocess
    import sys
    code = (
        "import sys; import gui.mini_player; "
        "banned = ('gui.main_window', 'PySide6.QtWebEngineWidgets', "
        "'PySide6.QtWebEngineCore', 'engine', 'agent', 'dlmgr', 'requests'); "
        "leaked = [m for m in banned if m in sys.modules]; "
        "assert not leaked, leaked"
    )
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# Config section
# --------------------------------------------------------------------------

def test_mini_player_config_roundtrip(tmp_path):
    cfg = DeeptorrentConfig()
    cfg.mini_player.volume = 42
    cfg.mini_player.muted = True
    cfg.mini_player.geometry = "QUJD"
    cfg.mini_player.last_folder = "D:/videos"
    path = str(tmp_path / "config.json")
    cfg.to_file(path)
    loaded = DeeptorrentConfig.from_file(path)
    assert loaded.mini_player.volume == 42
    assert loaded.mini_player.muted is True
    assert loaded.mini_player.geometry == "QUJD"
    assert loaded.mini_player.last_folder == "D:/videos"


def test_mini_player_config_filters_unknown_and_clamps(tmp_path):
    import json
    path = str(tmp_path / "config.json")
    # Write a hand-edited section: unknown key + out-of-range volume.
    payload = {"mini_player": {"volume": 500, "muted": "yes", "hax": 1}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    loaded = DeeptorrentConfig.from_file(path)
    assert loaded.mini_player.volume == 100      # clamped
    assert loaded.mini_player.muted is True      # truthy
    assert not hasattr(loaded.mini_player, "hax")  # unknown key dropped


# --------------------------------------------------------------------------
# Window states (stub backend — no libmpv needed)
# --------------------------------------------------------------------------

class _StubBackend:
    name = "stub"

    def __init__(self):
        self.played = []
        self.stops = 0

    def play(self, url, headers=None):
        self.played.append(url)

    def stop(self):
        self.stops += 1

    def pause(self): pass

    def resume(self): pass

    def seek(self, s): pass

    def seek_by(self, d): pass

    def set_volume(self, v): pass

    def set_mute(self, m): pass

    def set_hwdec(self, h): pass

    def destroy(self): pass


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def mini(qapp, tmp_path):
    import gui.mini_player as mp
    from gui.mini_player import MiniPlayerWindow
    cfg_path = str(tmp_path / "config.json")
    cfg = DeeptorrentConfig()
    cfg.stats.ping_enabled = False
    cfg.to_file(cfg_path)
    stub = _StubBackend()
    win = MiniPlayerWindow(DeeptorrentConfig.from_file(cfg_path), cfg_path,
                           backend_factory=lambda: stub)
    mp._MINI_WINDOW = win   # production: run_mini_player owns this global
    yield win, stub, mp
    if mp._MINI_WINDOW is win:
        mp._MINI_WINDOW = None
    win._morphing = False   # never leave the guard latched on a failure


def test_idle_state_is_default(mini):
    win, stub, mp = mini
    win.show()
    assert win._stack.currentWidget() is win._idle_page
    # The owner banner resolves (dev tree; frozen ships it via app.spec).
    assert not win._idle_bg._banner.isNull()


def test_idle_banner_fits_whole_window(mini):
    """Owner 2026-11-10/11: the banner must be FULLY visible — fitted, never
    cropped (object-fit: contain), scaled as large as the window allows,
    black bars where the window's shape differs. Earlier attempts pinned
    (a) a sibling background widget that never resized with the window
    (small logo top-left) and (b) a crop/cover fill — both rejected. This
    test resizes the WINDOW and asserts the real path."""
    from PySide6.QtWidgets import QApplication
    win, _, _ = mini
    win.show()
    bg = win._idle_bg
    banner = bg._banner
    if banner.isNull():
        pytest.skip("banner art not present in this tree")
    win.resize(680, 440)
    QApplication.instance().processEvents()
    # The banner widget (the idle page) fills the stacked page.
    assert bg.width() >= 678 and bg.height() >= 438, \
        f"banner widget is {bg.width()}x{bg.height()}, not the whole window"
    scaled = bg._scaled
    # Fit: the WHOLE image is inside the viewport, one axis exactly matches,
    # aspect preserved (the wide banner fills the width, letterboxes top and
    # bottom in a 680x440 window).
    assert scaled.width() <= bg.width() and scaled.height() <= bg.height()
    assert scaled.width() == bg.width() or scaled.height() == bg.height()
    src_ar = banner.width() / banner.height()
    out_ar = scaled.width() / scaled.height()
    assert abs(src_ar - out_ar) < 0.02


def test_arrow_keys_seek_and_focus_returns_to_window(mini, tmp_path):
    """←/→ must seek ±10 s no matter what was clicked last: sliders never
    take focus and every transport click re-seats focus on the window (the
    PlayerWidget focus-stranding lesson)."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    win, stub, _ = mini
    app = QApplication.instance()
    win.show()
    media = str(tmp_path / "k.mp4")
    with open(media, "wb") as f:
        f.write(b"v" * 256)
    win.open_media(media)
    app.processEvents()

    # Sliders must never own the keyboard.
    assert int(win.seek.focusPolicy() & Qt.FocusPolicy.StrongFocus) == 0
    assert int(win.vol.focusPolicy() & Qt.FocusPolicy.StrongFocus) == 0

    # Arrow keys reach the window and drive the backend.
    seeks = []
    stub.seek_by = lambda d: seeks.append(d)
    QTest.keyClick(win, Qt.Key.Key_Left)
    QTest.keyClick(win, Qt.Key.Key_Right)
    assert seeks == [-10, 10]

    # After a transport click, focus returns to the window so Space/F/
    # arrows keep working (offscreen QPA still tracks focusWidget).
    QTest.mouseClick(win._play_btn, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert QApplication.focusWidget() is win


def test_open_media_plays_and_updates_transport(mini, tmp_path):
    win, stub, _ = mini
    media = str(tmp_path / "movie.mp4")
    with open(media, "wb") as f:
        f.write(b"v" * 1024)
    win.open_media(media)
    assert stub.played == [media]
    assert win._stack.currentWidget() is win._video_page
    assert "movie.mp4" in win.windowTitle()
    win._on_position(12.0, 120.0)
    assert win.seek.maximum() == 120
    assert win._time_lbl.text().startswith("0:12")


def test_open_media_rejects_non_media_silently(mini, tmp_path, monkeypatch):
    import gui.mini_player as mp_mod
    # Media-NAMED but missing files pop a warning dialog (blocking offscreen)
    # — neutralize it; the dialog itself is covered by its own test below.
    monkeypatch.setattr(mp_mod.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))
    win, stub, _ = mini
    win.open_media(str(tmp_path / "notes.txt"))
    win.open_media("C:/definitely/not/here.mp3")
    assert stub.played == []
    assert win._stack.currentWidget() is win._idle_page


def test_geometry_persists(mini, tmp_path):
    win, _, _ = mini
    win.show()
    win._save_prefs()
    cfg_path = str(tmp_path / "config.json")
    loaded = DeeptorrentConfig.from_file(cfg_path)
    assert loaded.mini_player.geometry, "geometry should persist on save"


def test_morph_cycle_full_roundtrip(mini, tmp_path):
    """The full two-way morph: staged build with monotonic progress, all
    tabs present, reverse morph keeps engines alive, routed opens go to the
    right surface, and closing the mini window runs MainWindow's graceful
    closeEvent."""
    from PySide6.QtWidgets import QApplication

    win, stub, mp = mini
    app = QApplication.instance()
    win.show()
    media = str(tmp_path / "movie.mp4")
    with open(media, "wb") as f:
        f.write(b"v" * 1024)
    win.open_media(media)

    stages = []
    orig = win._on_stage
    win._on_stage = lambda s, f: (stages.append((s, f)), orig(s, f))

    win.morph_to_full()
    # Drive the loop until the worker import + queued signal + staged build
    # complete (offscreen construction is ~1-2 s).
    deadline = time.time() + 90
    while not (win._main is not None and not win._morphing
               and not win.isVisible()):
        app.processEvents()
        assert time.time() < deadline, "morph did not complete"

    main = win._main
    names = [main.main_tabs.tabText(i)
             for i in range(main.main_tabs.count())]
    assert "Agent" in names and "Browse" in names
    assert "Download" in names and "Play" in names
    fracs = [f for _, f in stages]
    assert fracs == sorted(fracs), stages
    assert stages[0][0] == "loading" and stages[-1][0] == "done"
    assert fracs[-1] >= 100.0
    assert main.isVisible() and not win.isVisible()
    assert win._backend.stops >= 1, "playback must stop before the morph"

    # Reverse morph: hidden full app keeps its engines; mini is back.
    main.morph_to_mini()
    app.processEvents()
    assert win.isVisible() and not main.isVisible()
    assert main._mini_view is True

    # Control-API routing while in mini view: media → mini window…
    win.route_open_target(media)
    app.processEvents()
    assert stub.played == [media, media]

    # …non-media → un-hide the full app.
    class _FakeTools:
        def call(self, name, args):
            return {"info_hash": "abc"}

        def shutdown(self):
            pass

    main.tools = _FakeTools()
    main._refresh = lambda: None
    win.route_open_target("magnet:?xt=urn:btih:ABC")
    for _ in range(20):
        app.processEvents()
    assert main.isVisible() and not main._mini_view

    # Close from the mini side routes through MainWindow's graceful path.
    main.morph_to_mini()
    app.processEvents()
    closed = {"graceful": False}
    orig_close = main.closeEvent

    def spy(ev):
        closed["graceful"] = True
        orig_close(ev)

    main.closeEvent = spy
    win.close()
    app.processEvents()
    assert closed["graceful"], "graceful shutdown path did not run"
    assert main._dl_api is not None  # shutdown ran; object remains reachable


def test_instant_morph_back_and_forth(mini, tmp_path):
    """After the first morph, subsequent switches must not rebuild."""
    from PySide6.QtWidgets import QApplication
    win, _, _ = mini
    app = QApplication.instance()
    win.show()
    win.morph_to_full()
    deadline = time.time() + 90
    while not (win._main is not None and not win._morphing):
        app.processEvents()
        assert time.time() < deadline
    main = win._main
    main.morph_to_mini()
    app.processEvents()
    built_with = main  # identity of the singleton full app
    win.morph_to_full()          # instant path — no overlay, no rebuild
    app.processEvents()
    assert win._main is built_with
    assert not win._morphing
    assert built_with.isVisible()


def test_open_handler_stays_signal_marshaled_after_roundtrip(mini, tmp_path):
    """Review finding 3: returning from mini view used to rebind the
    control-API open handler to the RAW open_target method — /api/open then
    ran QWidget code on the API's HTTP thread."""
    from PySide6.QtWidgets import QApplication
    win, _, _ = mini
    app = QApplication.instance()
    win.show()
    win.morph_to_full()
    deadline = time.time() + 90
    while not (win._main is not None and not win._morphing):
        app.processEvents()
        assert time.time() < deadline
    main = win._main
    main.morph_to_mini()
    app.processEvents()
    # In mini view the handler must point at the mini window's SIGNAL…
    assert main._dl_api._open_handler is not main.open_target
    win.morph_to_full()          # back to full view
    app.processEvents()
    # …and back in full view at the signal emit, never the raw method.
    assert main._dl_api._open_handler is not main.open_target


def test_full_boot_first_morph_routes_media_to_mini(tmp_path, monkeypatch):
    """Review finding 4: a session that booted the FULL app directly (e.g.
    a magnet open) has no mini window when ◱ Mini is pressed — media opens
    must still land in the freshly created mini window, not un-hide the
    full app."""
    import gui.mini_player as mp
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    cfg_path = str(tmp_path / "config.json")
    cfg = DeeptorrentConfig()
    cfg.stats.ping_enabled = False
    cfg.to_file(cfg_path)
    main = MainWindow(cfg_path)
    app.processEvents()

    stub = _StubBackend()
    win = mp.MiniPlayerWindow(DeeptorrentConfig.from_file(cfg_path),
                              cfg_path, backend_factory=lambda: stub)
    mp._MINI_WINDOW = None   # simulate: full app booted first, no mini yet
    main.morph_to_mini()
    app.processEvents()
    assert main._mini_view and not main.isVisible()
    mini_now = mp.get_mini_window()
    assert mini_now is not None

    media = str(tmp_path / "video.mp4")
    with open(media, "wb") as f:
        f.write(b"v" * 512)
    # The freshly created mini window has the REAL backend factory; swap in
    # the stub so the assertion observes the play call deterministically.
    mini_now._backend = stub
    # Routed media must play in the mini window, and the full app must stay
    # hidden.
    handler = main._dl_api._open_handler
    handler(media)               # exactly what the HTTP thread calls
    for _ in range(10):
        app.processEvents()
    assert stub.played == [media]
    assert not main.isVisible()

    # Teardown: run the real graceful close so this test's ControlAPI port
    # and engines never leak into later tests in the same process.
    main._mini_view = False
    main.close()
    app.processEvents()
    mp._MINI_WINDOW = None
    mini_now._morphing = False


def test_declined_quit_prompt_cancels_mini_close(mini, monkeypatch):
    """Requirement 6: closing the mini window while the hidden full app has
    active transfers PROMPTS via MainWindow.closeEvent — a declined prompt
    must leave the mini window open (closing it anyway would strand the app
    with no visible window and quit unclean)."""
    from PySide6.QtWidgets import QApplication
    win, _, _ = mini
    app = QApplication.instance()
    win.show()

    class _DecliningMain:
        config = win._config
        config_path = win._config_path

        def close(self):
            return False   # user pressed "No" in the transfers prompt

    win._main = _DecliningMain()
    win.close()
    app.processEvents()
    assert win.isVisible(), "declined quit must keep the mini window open"
    win._main = None


def test_media_queue_autoplays(mini, tmp_path):
    """Multi-select "Open with" queues every target; each stop advances."""
    from PySide6.QtWidgets import QApplication
    win, stub, _ = mini
    app = QApplication.instance()
    first = str(tmp_path / "a.mp4")
    second = str(tmp_path / "b.mp4")
    for p in (first, second):
        with open(p, "wb") as f:
            f.write(b"v" * 512)
    win.show()
    win.open_media(first)
    assert stub.played == [first]
    win._queue = [second]
    win._on_state("stopped")
    for _ in range(10):
        app.processEvents()
    assert stub.played == [first, second]


def test_missing_media_file_warns(mini, tmp_path, monkeypatch):
    """A media-NAMED file that cannot be opened surfaces a message, not a
    silent idle banner."""
    import gui.mini_player as mp_mod
    calls = []
    monkeypatch.setattr(
        mp_mod.QMessageBox, "warning",
        staticmethod(lambda *a, **k: calls.append(a)))
    win, stub, _ = mini
    win.open_media(str(tmp_path / "vanished.mp4"))
    assert calls, "expected a warning dialog for the missing media file"
    assert stub.played == []
