"""Tests for the 2026-10-11 Linux full-feature port.

Everything here runs on Windows too: platform behaviour is simulated by
monkeypatching sys.platform / sys.frozen / HOME, and only pure-logic paths
are covered (no real sockets, spawns, or downloads).
"""
from __future__ import annotations

import ctypes.util
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

import infra.first_run as first_run
import infra.updater as updater
from config import DeeptorrentConfig


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Point expanduser at a tmp dir on every platform (HOME + USERPROFILE)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


@pytest.fixture()
def linux_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    yield
    # monkeypatch restores sys.platform automatically


# ---------------------------------------------------------------------------
# Play-tab gate
# ---------------------------------------------------------------------------

def test_play_tab_supported_on_windows_and_linux():
    from gui.main_window import _PLAY_TAB_SUPPORTED
    expected = sys.platform in ("win32", "linux")
    assert _PLAY_TAB_SUPPORTED is expected
    if sys.platform == "win32":
        assert _PLAY_TAB_SUPPORTED is True


def test_mpv_process_windows_constants_unchanged():
    import iptv.mpv_process as mp
    if os.name == "nt":
        assert mp.PIPE_NAME == "mpvpipe"
        assert mp.PIPE_PATH == r"\\.\pipe\mpvpipe"


# ---------------------------------------------------------------------------
# iptv/player.py — bundled .so discovery + find_library bridge
# ---------------------------------------------------------------------------

@pytest.fixture()
def reset_find_library():
    yield
    import iptv.player as player
    if player._orig_find_library is not None:
        ctypes.util.find_library = player._orig_find_library
        player._orig_find_library = None


def test_bundled_so_base_requires_mpvlibs(tmp_path, monkeypatch):
    import iptv.player as player
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    (tmp_path / "libmpv.so.2").touch()
    assert player._bundled_so_base() == ""          # loose .so is not enough
    mpvlibs = tmp_path / "mpvlibs"
    mpvlibs.mkdir()
    (mpvlibs / "libmpv.so.2").touch()
    assert player._bundled_so_base() == str(mpvlibs)


def test_find_library_bridge_prefers_bundled(tmp_path, monkeypatch, reset_find_library):
    import iptv.player as player
    mpvlibs = tmp_path / "mpvlibs"
    mpvlibs.mkdir()
    (mpvlibs / "libmpv.so.2.0.0").touch()
    (mpvlibs / "libmpv.so.2").touch()
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    calls = []

    def fake_orig(name):
        calls.append(name)
        return f"system-{name}"

    monkeypatch.setattr(player, "_orig_find_library", fake_orig)
    # Force re-wrap: simulate a fresh call by invoking the wrapper logic.
    monkeypatch.setattr(ctypes.util, "find_library", fake_orig)
    player._orig_find_library = None
    monkeypatch.setattr(sys, "platform", "linux")
    player._ensure_bundled_so_findable()
    assert ctypes.util.find_library("mpv") == str(mpvlibs / "libmpv.so.2")
    # Unknown names fall through to the original lookup.
    assert ctypes.util.find_library("nobody-has-this") == "system-nobody-has-this"
    assert calls == ["nobody-has-this"]


# ---------------------------------------------------------------------------
# infra/first_run.py — fresh-install seed
# ---------------------------------------------------------------------------

def test_seed_only_frozen_linux_fresh(tmp_path, monkeypatch):
    cfg_path = str(tmp_path / "config.json")
    cfg = DeeptorrentConfig()
    # Not frozen / not linux / config existed / sources present — all no-ops.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert first_run.maybe_seed_fresh_install(cfg, cfg_path, False) is False
    monkeypatch.setattr(sys, "platform", "linux")
    assert first_run.maybe_seed_fresh_install(cfg, cfg_path, True) is False
    cfg2 = DeeptorrentConfig()
    assert first_run.maybe_seed_fresh_install(cfg2, cfg_path, False) is True
    assert [s.id for s in cfg2.iptv.sources] == ["macau-iptv-org"]
    assert cfg2.iptv.sources[0].url == "https://iptv-org.github.io/iptv/countries/mo.m3u"
    # Seeded file must reload cleanly and keep exactly one source.
    reloaded = DeeptorrentConfig.from_file(cfg_path)
    assert [s.id for s in reloaded.iptv.sources] == ["macau-iptv-org"]


def test_seed_never_touches_config_with_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    cfg = DeeptorrentConfig()
    cfg.iptv.sources = [first_run.SAMPLE_SOURCE]
    before = len(cfg.iptv.sources)
    assert first_run.maybe_seed_fresh_install(cfg, str(tmp_path / "c.json"), False) is False
    assert len(cfg.iptv.sources) == before


# ---------------------------------------------------------------------------
# infra/updater.py — AppImage apply path
# ---------------------------------------------------------------------------

def test_updates_supported_covers_linux_and_windows(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert updater.updates_supported() is True
    monkeypatch.setattr(sys, "platform", "win32")
    assert updater.updates_supported() is True
    monkeypatch.setattr(sys, "platform", "darwin")
    assert updater.updates_supported() is False


def test_posix_helper_script_shape(tmp_path, linux_platform):
    new = tmp_path / "DeepFlux-9.9.AppImage"
    new.write_bytes(b"")
    target = tmp_path / "DeepFlux.AppImage"
    helper = updater.build_posix_helper(new, 4242, target,
                                        tmp_path / "update.log")
    text = helper.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "APP_PID=4242" in text
    assert "chmod +x" in text
    assert "mv -f" in text and str(target) in text
    assert "rm -f \"$0\"" in text
    # Paths with spaces must survive quoting.
    assert "'" + str(new) + "'" in text


def test_appimage_path_and_writability(monkeypatch, linux_platform, tmp_path):
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert updater.appimage_path() is None
    assert updater.install_dir_writable() is False
    img = tmp_path / "DeepFlux.AppImage"
    img.write_bytes(b"")
    monkeypatch.setenv("APPIMAGE", str(img))
    assert updater.appimage_path() == img
    assert updater.install_dir_writable() is True


# ---------------------------------------------------------------------------
# infra/file_associations.py — .desktop registration
# ---------------------------------------------------------------------------

def test_linux_associations_register_and_unregister(linux_platform, fake_home):
    from infra import file_associations as fa
    monkeypatch_home = fake_home
    assert fa.register_associations() is True
    desktop = Path(monkeypatch_home) / ".local" / "share" / "applications" / "DeepFlux.desktop"
    assert desktop.is_file()
    text = desktop.read_text(encoding="utf-8")
    assert "application/x-bittorrent" in text
    assert "x-scheme-handler/magnet" in text
    assert "video/mp4" in text
    assert text.startswith("[Desktop Entry]")
    icon = (monkeypatch_home / ".local" / "share" / "icons" / "hicolor"
            / "512x512" / "apps" / "DeepFlux.png")
    assert icon.is_file()
    assert fa.unregister_associations() is True
    assert not desktop.exists()
    assert not icon.exists()


# ---------------------------------------------------------------------------
# native_messaging/register.py — Linux manifests
# ---------------------------------------------------------------------------

_VALID_EXT = "a" * 32  # valid Chrome extension id charset [a-p]


def test_native_host_linux_manifests(linux_platform, fake_home):
    from native_messaging import register
    assert register.register_native_host(_VALID_EXT) is True
    base = fake_home / ".config" / "google-chrome" / "NativeMessagingHosts"
    manifest_path = base / "com.deeptorrent.integration.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["name"] == "com.deeptorrent.integration"
    assert data["allowed_origins"] == [f"chrome-extension://{_VALID_EXT}/"]
    # Every supported browser dir gets its copy.
    for browser in ("chromium", "microsoft-edge", "brave-browser"):
        assert (fake_home / ".config" / browser / "NativeMessagingHosts"
                / "com.deeptorrent.integration.json").is_file()
    assert register.unregister_native_host() is True
    assert not manifest_path.exists()
    # Invalid ids are rejected (no wildcard manifests, ever).
    assert register.register_native_host("short") is False


# ---------------------------------------------------------------------------
# dlmgr/bookmarks_import.py — Linux profile paths
# ---------------------------------------------------------------------------

def test_detect_linux_browser_sources(linux_platform, fake_home):
    from dlmgr import bookmarks_import as bi
    chrome_prof = fake_home / ".config" / "google-chrome" / "Default"
    chrome_prof.mkdir(parents=True)
    (chrome_prof / "Bookmarks").write_text("{}", encoding="utf-8")
    ff_prof = fake_home / ".mozilla" / "firefox" / "abc123.default-release"
    ff_prof.mkdir(parents=True)
    (ff_prof / "places.sqlite").write_bytes(b"")
    sources = bi.detect_browser_sources()
    kinds = {(s.browser, s.kind) for s in sources}
    assert ("Chrome", "chromium") in kinds
    assert any(b.startswith("Firefox") and k == "firefox" for b, k in kinds)


# ---------------------------------------------------------------------------
# infra/jackett_setup.py — Linux discovery constants
# ---------------------------------------------------------------------------

def test_jackett_server_config_candidates_linux(linux_platform, fake_home):
    from infra import jackett_setup as js
    paths = js.server_config_candidates()
    assert paths[0].replace("/", os.sep) == str(
        fake_home / ".config" / "Jackett" / "ServerConfig.json").replace("/", os.sep)
    # The portable getattr idiom, not the hardcoded 0x08000000 (which makes
    # POSIX Popen raise ValueError).
    import subprocess as sp
    assert js._CREATE_NO_WINDOW == getattr(sp, "CREATE_NO_WINDOW", 0)


def test_jackett_linux_binary_path(linux_platform, fake_home):
    from infra import jackett_setup as js
    assert js.linux_jackett_binary() is None
    binary = fake_home / ".local" / "share" / "Jackett" / "Jackett" / "jackett"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    assert js.linux_jackett_binary() == str(binary)


# ---------------------------------------------------------------------------
# iptv/svp.py — Linux install discovery (module reload with patched platform)
# ---------------------------------------------------------------------------

def test_svp_linux_discovery(fake_home, monkeypatch):
    import iptv.svp as svp
    monkeypatch.setattr(sys, "platform", "linux")
    try:
        svp = importlib.reload(svp)
        assert svp.MANAGER_EXE == "SVPManager"
        assert str(fake_home / "SVP4") in svp._DEFAULT_DIRS
        install = fake_home / "SVP4"
        mpv_dir = install / "mpv"
        mpv_dir.mkdir(parents=True)
        (mpv_dir / "mpv").write_bytes(b"")
        (install / "SVPManager").write_bytes(b"")
        inst = svp.find_install()
        assert inst is not None
        assert inst.mpv_exe == str(mpv_dir / "mpv")
    finally:
        monkeypatch.setattr(sys, "platform", "win32")
        importlib.reload(svp)
        # Windows shape restored (registry absent on CI → defaults only).
        assert svp.MANAGER_EXE == "SVPManager.exe"


def test_svp_windows_constants_unchanged():
    import iptv.svp as svp
    if os.name == "nt":
        assert svp.MANAGER_EXE == "SVPManager.exe"
        assert svp._DEFAULT_DIRS[0] == r"C:\Program Files (x86)\SVP 4"


# ---------------------------------------------------------------------------
# agent SYSTEM_PROMPT — Linux note must no longer claim "no Play tab"
# ---------------------------------------------------------------------------

def test_linux_system_prompt_mentions_play_tab():
    import agent.loop as loop
    if sys.platform.startswith("linux"):
        assert "NO Play/IPTV tab" not in loop.SYSTEM_PROMPT
        assert "iptv_* tools ARE available" in loop.SYSTEM_PROMPT
