"""SVP 4 integration: config round-trip, install detection, runtime PATH
prep, and the mpv creation-kwargs contract. No real SVP install or display
needed — everything is mocked/tmp dirs."""
import json
import os
import subprocess
import threading
import time
from unittest import mock

from iptv import mpv_process, svp
from iptv.mpv_process import MpvProcessBackend
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

def _fake_install(root, with_mpv: bool = True) -> str:
    """A minimal usable SVP layout under ``root``."""
    d = root / "SVP 4"
    (d / "mpv64").mkdir(parents=True)
    (d / svp.MANAGER_EXE).write_text("", encoding="utf-8")
    (d / "mpv64" / "vapoursynth.dll").write_text("", encoding="utf-8")
    if with_mpv:
        (d / "mpv64" / "mpv.exe").write_text("", encoding="utf-8")
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


def test_mpv_exe_reported_only_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(svp, "_registry_install_dir", lambda: "")
    monkeypatch.setattr(svp, "_DEFAULT_DIRS", (_fake_install(tmp_path),))
    assert svp.find_install().mpv_exe.endswith("mpv.exe")

    monkeypatch.setattr(svp, "_DEFAULT_DIRS",
                        (_fake_install(tmp_path / "b", with_mpv=False),))
    assert svp.find_install().mpv_exe == ""


# -- in-process mpv (non-SVP path) ---------------------------------------------

def test_in_process_mpv_kwargs_unchanged():
    """The normal backend must stay exactly as it was: zero-copy hwdec, no
    IPC pipe. SVP is handled by the out-of-process backend instead."""
    kw = MpvBackend(None)._creation_kwargs("123", "gpu-next")
    assert kw["hwdec"] == "auto-safe"
    assert kw["vo"] == "gpu-next"
    assert kw["video_sync"] == "display-resample"
    assert "input_ipc_server" not in kw


def test_in_process_mpv_set_hwdec():
    be = MpvBackend(None)
    be._mpv = mock.Mock()
    be.set_hwdec("d3d11va")
    assert be._mpv.hwdec == "d3d11va"
    be.set_hwdec("")
    assert be._mpv.hwdec == "auto-safe"


# -- out-of-process backend (SVP path) -----------------------------------------

def _wired_backend():
    """A process backend with the pipe faked out (no mpv.exe involved)."""
    be = MpvProcessBackend(None, mpv_exe="mpv.exe")
    be._pipe = mock.Mock()
    return be


def test_process_backend_create_requires_real_exe():
    assert MpvProcessBackend(None, mpv_exe="")._proc is None
    assert MpvProcessBackend(None, mpv_exe=r"C:\nope\mpv.exe").create() is False


def test_process_backend_launch_args_match_svp_contract(tmp_path, monkeypatch):
    exe = tmp_path / "mpv.exe"
    exe.write_text("", encoding="utf-8")
    host = mock.Mock()
    host.winId.return_value = 4242
    be = MpvProcessBackend(host, mpv_exe=str(exe))
    captured = {}

    def fake_popen(args, **kw):
        captured["args"] = args
        raise OSError("stop here — we only want the argv")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert be.create() is False
    args = captured["args"]
    assert "--wid=4242" in args
    assert f"--input-ipc-server={mpv_process.PIPE_NAME}" in args
    # VapourSynth needs copy-back frames; SVP's notes want these two too.
    assert "--hwdec=auto-copy" in args
    assert "--hwdec-codecs=all" in args
    assert "--hr-seek-framedrop=no" in args
    # gpu-next is what renders Dolby Vision correctly (see AGENTS.md).
    assert "--vo=gpu-next" in args
    # SVP's own mpv.conf sits next to this exe and would fight our options.
    assert "--no-config" in args


def test_process_backend_commands_are_json_lines():
    be = _wired_backend()
    be.pause()
    be.seek_by(-10)
    be.set_volume(150)          # clamped
    sent = [json.loads(c.args[0].decode()) for c in be._pipe.write.call_args_list]
    assert {"command": ["set_property", "pause", True]} in sent
    assert {"command": ["seek", -10, "relative"]} in sent
    assert {"command": ["set_property", "volume", 100]} in sent


def test_process_backend_hwdec_always_copy_back():
    """The configured hwdec must never knock SVP's chain off copy-back."""
    be = _wired_backend()
    be.set_hwdec("d3d11va")
    sent = [json.loads(c.args[0].decode()) for c in be._pipe.write.call_args_list]
    assert {"command": ["set_property", "hwdec", "auto-copy"]} in sent


def test_process_backend_mpv_interpolation_is_noop():
    """SVP synthesizes frames; mpv's own blending would just add cost."""
    be = _wired_backend()
    be.set_interpolation(True)
    be.set_smooth_video(True)
    assert be._pipe.write.call_count == 0


def test_process_backend_dispatch_routes_events():
    be = MpvProcessBackend(None)
    states, positions, tracks, errors = [], [], [], []
    be.on_state = states.append
    be.on_position = lambda p, d: positions.append(p)
    be.on_tracks = tracks.append
    be.on_error = errors.append

    be._dispatch({"event": "property-change", "name": "pause", "data": True})
    be._dispatch({"event": "property-change", "name": "time-pos", "data": 12.5})
    be._dispatch({"event": "property-change", "name": "eof-reached", "data": True})
    be._dispatch({"event": "property-change", "name": "track-list",
                  "data": [{"type": "audio", "id": 1, "lang": "eng"}]})
    assert states == ["paused", "stopped"]
    assert positions == [12.5]
    assert tracks and tracks[0][0]["lang"] == "eng"

    # Normal EOF is not an error; a real failure is.
    be._dispatch({"event": "end-file", "reason": "eof"})
    assert errors == []
    be._dispatch({"event": "end-file", "reason": "error", "file_error": "boom"})
    assert errors == ["boom"]


def test_process_backend_response_wakes_waiting_command():
    """A request_id reply must resolve the pending command slot."""
    be = _wired_backend()
    result = {}

    def run():
        result["data"] = be._command("get_property", "duration", timeout=5)

    t = threading.Thread(target=run)
    t.start()
    for _ in range(50):          # wait for the request to be registered
        if be._pending:
            break
        time.sleep(0.02)
    rid = next(iter(be._pending))
    be._dispatch({"request_id": rid, "error": "success", "data": 42.0})
    t.join(timeout=5)
    assert result["data"] == 42.0
    assert be._pending == {}


def test_process_backend_command_times_out_cleanly():
    """A dead player must degrade to None, not hang or raise."""
    be = _wired_backend()
    assert be._command("get_property", "duration", timeout=0.2) is None
    assert be._pending == {}


def test_process_backend_track_helpers():
    be = MpvProcessBackend(None)
    be._tracks = [
        {"type": "video", "id": 1},
        {"type": "audio", "id": 2, "lang": "eng", "title": "Main"},
        {"type": "sub", "id": 3, "lang": "nld"},
    ]
    assert [t["id"] for t in be.audio_tracks()] == [2]
    assert [t["id"] for t in be.subtitle_tracks()] == [3]
    assert be.audio_tracks()[0]["lang"] == "eng"


def test_process_backend_audio_only_detection():
    assert MpvProcessBackend.is_audio_only([{"type": "audio"}]) is True
    assert MpvProcessBackend.is_audio_only(
        [{"type": "audio"}, {"type": "video", "albumart": True}]) is True
    assert MpvProcessBackend.is_audio_only(
        [{"type": "audio"}, {"type": "video"}]) is False


# -- factory fallback ----------------------------------------------------------

def test_create_backend_falls_back_when_svp_unusable(monkeypatch):
    """A broken/absent SVP setup must never cost the user playback."""
    import iptv.player as player_mod
    monkeypatch.setattr(player_mod, "create_svp_backend", lambda parent: None)
    made = []

    class FakeMpv(MpvBackend):
        def create(self):
            made.append("in-process")
            return True

    monkeypatch.setattr(player_mod, "MpvBackend", FakeMpv)
    be = player_mod.create_backend(None, preferred="mpv", svp=True)
    assert made == ["in-process"]
    assert be is not None
