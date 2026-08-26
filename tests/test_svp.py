"""SVP 4 integration: config round-trip, install detection, runtime PATH
prep, and the mpv creation-kwargs contract. No real SVP install or display
needed — everything is mocked/tmp dirs."""
import json
import os
from unittest import mock

import pytest

from iptv import svp
from iptv.player import MpvBackend


# -- config ------------------------------------------------------------------

def test_svp_config_defaults_off(tmp_path):
    from config import DeeptorrentConfig
    cfg = DeeptorrentConfig.from_file(str(tmp_path / "missing.json"))
    assert cfg.iptv.svp_enabled is False


def test_svp_config_round_trip(tmp_path):
    from config import DeeptorrentConfig
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"iptv": {"svp_enabled": True}}, f)
    assert DeeptorrentConfig.from_file(path).iptv.svp_enabled is True


# -- install detection --------------------------------------------------------

def _fake_install(root) -> str:
    """A minimal usable SVP layout under ``root``."""
    d = root / "SVP 4"
    (d / "mpv64").mkdir(parents=True)
    (d / svp.MANAGER_EXE).write_text("", encoding="utf-8")
    (d / "mpv64" / "vapoursynth.dll").write_text("", encoding="utf-8")
    return str(d)


def test_find_install_uses_default_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(svp, "_registry_install_dir", lambda: "")
    monkeypatch.setattr(svp, "_DEFAULT_DIRS", (str(tmp_path / "nope"), _fake_install(tmp_path)))
    inst = svp.find_install()
    assert inst is not None
    assert inst.manager_exe.endswith(svp.MANAGER_EXE)
    assert inst.runtime_dir.endswith("mpv64")
    assert inst.usable


def test_find_install_none_without_runtime(tmp_path, monkeypatch):
    """SVPManager.exe without the portable VapourSynth runtime = not usable."""
    d = tmp_path / "SVP 4"
    d.mkdir()
    (d / svp.MANAGER_EXE).write_text("", encoding="utf-8")  # no mpv64/
    monkeypatch.setattr(svp, "_registry_install_dir", lambda: "")
    monkeypatch.setattr(svp, "_DEFAULT_DIRS", (str(d),))
    assert svp.find_install() is None


def test_find_install_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(svp, "_registry_install_dir", lambda: "")
    monkeypatch.setattr(svp, "_DEFAULT_DIRS", (str(tmp_path / "nope"),))
    assert svp.find_install() is None


# -- environment prep ----------------------------------------------------------

def test_prepare_environment_appends_once(tmp_path, monkeypatch):
    d = str(tmp_path / "mpv64")
    monkeypatch.setenv("PATH", r"C:\bundled-mpv")
    monkeypatch.setenv("PYTHONPATH", r"C:\our-app")
    monkeypatch.setattr(svp, "_prepared", False)
    inst = svp.SvpInstall(install_dir="", manager_exe="", runtime_dir=d)

    svp.prepare_environment(inst)
    # Appended after our own mpv dir (bundled libmpv must keep winning).
    assert os.environ["PATH"] == r"C:\bundled-mpv" + os.pathsep + d
    # PYTHONPATH must NOT be touched: mpv64 holds a Python 3.12 stdlib whose
    # .pyd files would shadow our own interpreter's modules.
    assert os.environ["PYTHONPATH"] == r"C:\our-app"

    svp.prepare_environment(inst)  # idempotent
    assert os.environ["PATH"] == r"C:\bundled-mpv" + os.pathsep + d


# -- mpv creation contract -----------------------------------------------------

def test_mpv_kwargs_svp_off_unchanged():
    be = MpvBackend(None, svp_mode=False)
    kw = be._creation_kwargs("123", "gpu-next")
    assert kw["hwdec"] == "auto-safe"
    assert kw["vo"] == "gpu-next"
    assert "input_ipc_server" not in kw
    assert "hwdec_codecs" not in kw


def test_mpv_kwargs_svp_on_contract():
    be = MpvBackend(None, svp_mode=True)
    kw = be._creation_kwargs("123", "gpu-next")
    # SVP's mpv contract: discoverable pipe + copy-back hwdec for the
    # VapourSynth chain + their audio-desync/watch-later notes.
    assert kw["input_ipc_server"] == "mpvpipe"
    assert kw["hwdec"] == "auto-copy"
    assert kw["hwdec_codecs"] == "all"
    assert kw["hr_seek_framedrop"] is False
    assert kw["resume_playback"] is False


def test_mpv_set_hwdec_stays_copy_back_in_svp_mode():
    be = MpvBackend(None, svp_mode=True)
    be._mpv = mock.Mock()
    be.set_hwdec("d3d11va")  # user config must not break the SVP chain
    assert be._mpv.hwdec == "auto-copy"

    be2 = MpvBackend(None, svp_mode=False)
    be2._mpv = mock.Mock()
    be2.set_hwdec("d3d11va")
    assert be2._mpv.hwdec == "d3d11va"


def test_mpv_set_hwdec_empty_falls_back():
    be = MpvBackend(None, svp_mode=False)
    be._mpv = mock.Mock()
    be.set_hwdec("")
    assert be._mpv.hwdec == "auto-safe"
