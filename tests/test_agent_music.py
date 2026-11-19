"""Agent-tab background music (gui/agent_music.py, owner 2026-11-18).

Contract under test: the bundled MP3 loops on a dedicated AUDIO-ONLY mpv
(video=no so ID3 cover art can never open a window; loop-file=inf), the ♫
button is the only on-switch, other media opening stops it (never
auto-resumes), and a failed start reports instead of crashing. The mpv
layer is faked via a module seam — no audio hardware needed offscreen.
"""
from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from gui import agent_music
from gui.agent_music import AgentMusicController


class _FakePlayer:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.loaded = []
        self.terminated = 0

    def loadfile(self, path):
        self.loaded.append(path)

    def terminate(self):
        self.terminated += 1


@pytest.fixture
def fake_mpv(monkeypatch, tmp_path):
    """Fake the mpv module + bundled track + dll hook the controller uses."""
    made = []
    mod = types.ModuleType("mpv")

    def MPV(**kwargs):
        player = _FakePlayer(**kwargs)
        made.append(player)
        return player

    mod.MPV = MPV
    monkeypatch.setitem(sys.modules, "mpv", mod)
    track = tmp_path / "track.mp3"
    track.write_bytes(b"ID3fake")
    monkeypatch.setattr(agent_music, "resolve_music_path", lambda: str(track))
    import iptv.player as player_mod
    monkeypatch.setattr(player_mod, "ensure_mpv_dll_on_path", lambda: None)
    agent_music._CONTROLLER = None   # fresh singleton per test
    yield made
    agent_music._CONTROLLER = None


def test_start_builds_looping_audio_only_player(fake_mpv):
    states = []
    ctl = AgentMusicController()
    ctl.on_state_change = states.append
    assert ctl.start() is True
    player = fake_mpv[0]
    # Audio-only, looping, background-level volume — the owner contract.
    assert player.kwargs["video"] is False
    assert player.kwargs["loop_file"] == "inf"
    assert player.kwargs["volume"] == agent_music.VOLUME
    assert len(player.loaded) == 1 and player.loaded[0].endswith("track.mp3")
    assert ctl.is_playing()
    assert states == [True]


def test_stop_terminates_and_notifies_exactly_once(fake_mpv):
    ctl = AgentMusicController()
    states = []
    ctl.on_state_change = states.append
    ctl.start()
    ctl.stop()
    assert fake_mpv[0].terminated == 1
    assert not ctl.is_playing()
    assert states == [True, False]
    ctl.stop()   # idempotent — no double terminate, no double notify
    assert fake_mpv[0].terminated == 1
    assert states == [True, False]


def test_toggle_flips_both_ways(fake_mpv):
    ctl = AgentMusicController()
    assert ctl.toggle() is True
    assert ctl.is_playing()
    assert ctl.toggle() is False
    assert not ctl.is_playing()


def test_start_failure_is_false_not_crash(monkeypatch, fake_mpv):
    def boom(**kwargs):
        raise OSError("no libmpv")
    sys.modules["mpv"].MPV = boom
    ctl = AgentMusicController()
    assert ctl.start() is False
    assert not ctl.is_playing()


def test_missing_track_is_false(monkeypatch):
    monkeypatch.setattr(agent_music, "resolve_music_path", lambda: "")
    ctl = AgentMusicController()
    assert ctl.start() is False


def test_media_open_hook_stops_playing_music_only(fake_mpv):
    ctl = AgentMusicController()
    agent_music._CONTROLLER = ctl
    agent_music.stop_agent_music()          # idle: no-op, nothing to stop
    assert not ctl.is_playing()
    ctl.start()
    agent_music.stop_agent_music()          # playing: yields immediately
    assert not ctl.is_playing()
    assert fake_mpv[0].terminated == 1


def test_bundled_track_resolves_in_dev_tree():
    """The owner's track lives at the repo root and ships via app.spec /
    app_linux.spec datas — the dev-tree resolver must find it (this also
    fails loudly on fresh checkouts if the 74.5 MB file ever goes missing,
    which would silently kill the feature in every build)."""
    path = agent_music.resolve_music_path()
    assert path and path.endswith("backgroundmusic.mp3")
    assert os.path.getsize(path) > 1_000_000   # a real track, not a stub


def test_media_open_hooks_are_wired_into_every_player():
    """The music must yield in ALL three media paths (owner 2026-11-18):
    the mini player's open_media, the Play tab's PlayerWidget.play, and
    multiview tile assigns. Source-pin in the test_crash_log.py style."""
    from pathlib import Path
    import gui.iptv_tab
    import gui.mini_player
    import gui.multiview

    def _has_hook_in_function(module, func_sig):
        src = Path(module.__file__).read_text(encoding="utf-8")
        idx = src.find(func_sig)
        assert idx >= 0, f"{func_sig} not found in {module.__name__}"
        # The hook must sit INSIDE the function (before its next def).
        tail = src[idx: idx + src[idx:].find("\n    def ")]
        return "stop_agent_music" in tail

    assert _has_hook_in_function(gui.mini_player, "def open_media(")
    assert _has_hook_in_function(gui.iptv_tab, "def play(self, item")
    assert _has_hook_in_function(gui.multiview, "def assign(")


def test_maybe_autostart_is_unconditional(fake_mpv):
    """Owner 2026-11-19: "it should start white and playing" — every
    full-app open starts the track; the ♫ button is session-only, so no
    persisted preference is consulted (the old ui_background_music key is
    gone; a config still carrying it must not silence the boot either)."""
    import config as config_mod

    cfg = config_mod.DeeptorrentConfig()
    assert not hasattr(cfg, "ui_background_music")  # the key is retired
    # A legacy/stub config object exposing the old key must not gate it.
    legacy = type("C", (), {"ui_background_music": False})()
    agent_music._CONTROLLER = None
    assert agent_music.maybe_autostart(cfg) is True
    assert agent_music.controller().is_playing()

    agent_music._CONTROLLER = None
    assert agent_music.maybe_autostart(legacy) is True  # starts anyway
    assert agent_music.controller().is_playing()


def test_maybe_autostart_unsupported_platform(fake_mpv, monkeypatch):
    monkeypatch.setattr(agent_music, "MUSIC_SUPPORTED", False)
    assert agent_music.maybe_autostart(object()) is False


def test_state_listeners_support_multiple_buttons(fake_mpv):
    """The mini player AND the full app can both be alive (morphs) — every
    ♫ button must hear every transition."""
    ctl = AgentMusicController()
    a, b = [], []
    ctl.add_state_listener(a.append)
    ctl.add_state_listener(b.append)
    ctl.add_state_listener(a.append)   # dedup — one registration per button
    ctl.start()
    ctl.stop()
    assert a == [True, False]
    assert b == [True, False]

    # Single-assignment convenience still behaves (tests, simple hosts).
    c = []
    ctl.on_state_change = c.append
    ctl.start()
    ctl.stop()
    assert c == [True, False]


def test_config_no_longer_carries_background_music_key(tmp_path):
    """The retired key is dropped on load — an old config carrying False
    (which used to silence boot) must load clean and never re-persist."""
    import json
    from config import DeeptorrentConfig
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ui_background_music": False}, f)
    cfg = DeeptorrentConfig.from_file(path)
    assert not hasattr(cfg, "ui_background_music")
    cfg.to_file(path)
    on_disk = json.load(open(path, encoding="utf-8"))
    assert "ui_background_music" not in on_disk
