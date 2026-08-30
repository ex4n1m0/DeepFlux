"""Out-of-process mpv backend, controlled over mpv's JSON IPC.

Why this exists: SVP 4 injects its motion-interpolation chain through
VapourSynth, and VapourSynth embeds its own CPython. libmpv running *inside*
DeepFlux's Python process therefore cannot initialize it — VSScript either
fails or deadlocks against our interpreter (see :mod:`iptv.svp`). A separate
``mpv.exe`` is a plain C host, so VapourSynth works there exactly as it does
for every other SVP-supported player.

VERIFIED live on 23.976 fps content with SVP 4.7: mpv embeds into the Qt
surface via ``--wid``, SVP attaches to the same ``mpvpipe`` named pipe we
use for control, injects ``vf=vapoursynth[svp]``, and output rises to
``estimated-vf-fps`` 119.88 — while our own pause/seek commands keep
returning ``error: success``. mpv accepts several simultaneous IPC clients,
so sharing the pipe with SVP Manager is fine.

This backend is used ONLY in SVP mode; everything else keeps the in-process
:class:`iptv.player.MpvBackend`, which stays faster (no IPC hop) and needs
no external executable.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from .player import PlayerBackend

logger = logging.getLogger(__name__)

_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# SVP Manager scans for mpv's default pipe name; using it is what makes SVP
# discover the player at all.
PIPE_NAME = "mpvpipe"
PIPE_PATH = r"\\.\pipe" + "\\" + PIPE_NAME

# Properties we mirror into the PlayerBackend callbacks. "duration" must be
# OBSERVED, not read once after loadfile: mpv doesn't know it until the file
# is demuxed, so a single read returns 0 and the host would treat the media
# as unseekable live content (grey progress bar, disabled skip buttons).
_OBSERVED = ("time-pos", "pause", "eof-reached", "track-list", "duration",
             "paused-for-cache")

# How often the reader polls the pipe for new data. mpv's own property
# updates are ~10/s, so this is comfortably below the noticeable threshold.
_POLL_INTERVAL = 0.02


def _bytes_available(pipe: Any) -> int:
    """Bytes waiting in the pipe, via PeekNamedPipe (0 on any problem).

    Why polling instead of a blocking read: mpv's IPC pipe handle is opened
    in SYNCHRONOUS mode, so a blocking ReadFile on the reader thread also
    blocks WriteFile from the GUI thread on the same handle — verified as a
    hard deadlock (main thread stuck in _send while the reader sat in read).
    Peeking first keeps every actual read short, so one I/O lock can safely
    serialize both directions."""
    try:
        import msvcrt
        handle = msvcrt.get_osfhandle(pipe.fileno())
    except Exception:
        return 0
    avail = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.PeekNamedPipe(
        ctypes.c_void_p(handle), None, 0, None, ctypes.byref(avail), None)
    return int(avail.value) if ok else 0


class MpvProcessBackend(PlayerBackend):
    """mpv.exe in its own process, embedded via ``--wid`` and driven by IPC."""

    name = "mpv"          # same public identity as the in-process backend
    is_out_of_process = True

    def __init__(self, parent_widget: Any, mpv_exe: str = "") -> None:
        super().__init__(parent_widget)
        self._exe = mpv_exe
        self._proc: Optional[subprocess.Popen] = None
        self._pipe: Any = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # ONE lock for both directions — the pipe handle is synchronous.
        self._io_lock = threading.RLock()
        self._rid = 0
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._tracks: List[dict] = []
        self._paused = False
        self._paused_for_cache = False
        self._duration = 0.0

    # -- lifecycle -----------------------------------------------------------
    def create(self) -> bool:
        if not self._exe or not os.path.isfile(self._exe):
            logger.warning("out-of-process mpv: executable not found: %r", self._exe)
            return False
        try:
            wid = int(self.parent.winId())
        except Exception:
            logger.warning("out-of-process mpv: host widget has no window handle")
            return False
        args = [
            self._exe,
            f"--wid={wid}",
            f"--input-ipc-server={PIPE_NAME}",
            # No config: SVP's own mpv.conf lives next to this exe and would
            # otherwise fight the options we set explicitly.
            "--no-config",
            "--idle=yes",              # stay alive until we load a file
            "--force-window=yes",
            "--keep-open=always",
            "--osc=no",
            "--input-default-bindings=no",
            "--input-vo-keyboard=no",
            "--input-cursor=no",
            # SVP's documented requirements: VapourSynth takes software
            # frames (copy-back hwdec) and these two avoid desync/watch-later.
            "--hwdec=auto-copy",
            "--hwdec-codecs=all",
            "--hr-seek-framedrop=no",
            "--no-resume-playback",
            # gpu-next for correct Dolby Vision / HDR, same as in-process.
            "--vo=gpu-next",
        ]
        try:
            self._proc = subprocess.Popen(args, creationflags=_CREATION_FLAGS)
        except OSError as exc:
            logger.warning("out-of-process mpv: spawn failed: %s", exc)
            return False
        if not self._connect():
            logger.warning("out-of-process mpv: IPC pipe never appeared")
            self.destroy()
            return False
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="mpv-ipc-reader")
        self._reader.start()
        for i, prop in enumerate(_OBSERVED, start=1):
            self._send({"command": ["observe_property", i, prop]})
        return True

    def _connect(self, timeout: float = 15.0) -> bool:
        """Open mpv's IPC pipe (it appears shortly after the process starts)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False        # mpv died on startup
            try:
                self._pipe = open(PIPE_PATH, "r+b", buffering=0)
                return True
            except OSError:
                time.sleep(0.2)
        return False

    def destroy(self) -> None:
        self._stop.set()
        if self._pipe is not None:
            try:
                self._send({"command": ["quit"]})
            except Exception:
                pass
            try:
                self._pipe.close()
            except Exception:
                pass
            self._pipe = None
        if self._proc is not None:
            try:
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        self._reader = None

    # -- IPC plumbing --------------------------------------------------------
    def _send(self, payload: dict) -> None:
        """Fire-and-forget one IPC message."""
        pipe = self._pipe
        if pipe is None:
            return
        data = (json.dumps(payload) + "\n").encode("utf-8")
        with self._io_lock:      # same lock as reads: the handle is synchronous
            pipe.write(data)

    def _command(self, *args: Any, timeout: float = 3.0) -> Any:
        """Send a command and wait for its response; returns ``data`` or None.

        Never raises: a dead or slow player must degrade to a no-op rather
        than take the GUI thread down with it."""
        if self._pipe is None:
            return None
        with self._pending_lock:
            self._rid += 1
            rid = self._rid
            slot: Dict[str, Any] = {"event": threading.Event(), "msg": None}
            self._pending[rid] = slot
        try:
            self._send({"command": list(args), "request_id": rid})
        except Exception as exc:
            logger.debug("mpv ipc write failed: %s", exc)
            with self._pending_lock:
                self._pending.pop(rid, None)
            return None
        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            return None
        msg = slot["msg"] or {}
        return msg.get("data")

    def _set(self, prop: str, value: Any) -> None:
        self._send({"command": ["set_property", prop, value]})

    def _get(self, prop: str, default: Any = None) -> Any:
        val = self._command("get_property", prop)
        return default if val is None else val

    def _read_loop(self) -> None:
        """Parse newline-delimited JSON from mpv until the pipe closes.

        Polls with PeekNamedPipe and only reads what is already buffered, so
        the shared handle is never held in a blocking read (see
        :func:`_bytes_available`). Partial lines carry over between reads."""
        buf = b""
        while not self._stop.is_set():
            pipe = self._pipe
            if pipe is None:
                break
            chunk = b""
            try:
                with self._io_lock:
                    n = _bytes_available(pipe)
                    if n:
                        chunk = pipe.read(n)
            except Exception:
                break
            if not chunk:
                time.sleep(_POLL_INTERVAL)
                continue
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                line = raw.strip()
                if not line:
                    continue
                try:
                    self._dispatch(json.loads(line.decode("utf-8", "replace")))
                except ValueError:
                    continue
                except Exception:
                    logger.debug("mpv ipc dispatch failed", exc_info=True)

    def _dispatch(self, msg: dict) -> None:
        rid = msg.get("request_id")
        if rid is not None:
            with self._pending_lock:
                slot = self._pending.pop(rid, None)
            if slot is not None:
                slot["msg"] = msg
                slot["event"].set()
            return
        event = msg.get("event")
        if event == "property-change":
            self._on_property(msg.get("name", ""), msg.get("data"))
        elif event == "end-file":
            # "error" is a real failure; eof/stop/quit are normal.
            if msg.get("reason") == "error" and self.on_error:
                self.on_error(str(msg.get("file_error") or "Playback failed"))

    def _on_property(self, name: str, data: Any) -> None:
        if name == "duration":
            # Live streams report None/0 — keep 0.0 so the host disables
            # seeking, exactly as it does for the in-process backend.
            self._duration = float(data or 0.0)
        elif name == "time-pos" and data is not None and self.on_position:
            self.on_position(float(data), float(self._duration or 0.0))
        elif name == "pause":
            self._paused = data is True or str(data).lower() == "yes"
            if self.on_state:
                # Cache-starvation pauses are buffering, not a user pause.
                if not data and self._paused_for_cache:
                    self.on_state("buffering")
                else:
                    self.on_state("paused" if data else "playing")
        elif name == "paused-for-cache":
            self._paused_for_cache = data is True or str(data).lower() == "yes"
        elif name == "eof-reached":
            if data and self.on_state:
                self.on_state("stopped")
        elif name == "track-list":
            self._tracks = list(data or [])
            if data and self.on_tracks:
                self.on_tracks(self._tracks)

    # -- transport -----------------------------------------------------------
    def play(self, url: str, headers: Optional[dict] = None) -> None:
        if self._pipe is None and not self.create():
            if self.on_error:
                self.on_error("Playback backend unavailable")
            return
        if headers:
            fields = [f"{k}: {v}" for k, v in headers.items() if v]
            if fields:
                self._set("http-header-fields", fields)
            if headers.get("User-Agent"):
                self._set("user-agent", headers["User-Agent"])
            if headers.get("Referer"):
                self._set("referrer", headers["Referer"])
        self._duration = 0.0     # refreshed by the "duration" observer
        self._command("loadfile", url, "replace")
        if self.on_state:
            self.on_state("playing")

    def pause(self) -> None:
        self._set("pause", True)

    def resume(self) -> None:
        self._set("pause", False)

    def stop(self) -> None:
        self._send({"command": ["stop"]})
        if self.on_state:
            self.on_state("stopped")

    def seek(self, seconds: float) -> None:
        self._send({"command": ["seek", seconds, "absolute"]})

    def seek_by(self, delta: float) -> None:
        self._send({"command": ["seek", delta, "relative"]})

    # -- settings ------------------------------------------------------------
    def set_volume(self, pct: int) -> None:
        self._set("volume", max(0, min(100, int(pct))))

    def set_mute(self, muted: bool) -> None:
        self._set("mute", bool(muted))

    def set_hwdec(self, mode: str) -> None:
        # Always copy-back: this backend only runs in SVP mode, and the
        # VapourSynth chain cannot take zero-copy GPU frames.
        self._set("hwdec", "auto-copy")

    def set_cache(self, seconds: int) -> None:
        self._set("cache", "yes")
        self._set("cache-secs", max(1, int(seconds)))

    def set_aspect(self, mode: str) -> None:
        self._set("video-aspect-override", mode if mode and mode != "auto" else "no")

    def set_deinterlace(self, on: bool) -> None:
        self._set("deinterlace", "yes" if on else "no")

    def set_overscan(self, pct: float) -> None:
        import math
        self._set("video-zoom", math.log2(1 + max(0.0, pct) / 100.0))

    def set_audio_delay(self, seconds: float) -> None:
        # Same mpv property as the in-process backend; SVP's frame synthesis
        # is exactly the video-path latency this is meant to compensate for.
        self._set("audio-delay", float(seconds))

    def audio_delay(self) -> float:
        val = self._get("audio-delay")
        try:
            return float(val or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def set_interpolation(self, on: bool) -> None:
        # No-op by design: SVP is doing real frame synthesis here, so mpv's
        # own blending would only add cost (and fight SVP's cadence).
        pass

    def set_smooth_video(self, on: bool) -> None:
        # Same reasoning — SVP targets the display refresh itself. Matches
        # SVP's shipped mpv.conf, which leaves video-sync at the default.
        pass

    # -- tracks --------------------------------------------------------------
    def _tracks_of(self, kind: str) -> List[dict]:
        out = []
        for t in self._tracks or self._get("track-list", []) or []:
            if t.get("type") == kind:
                out.append({
                    "index": len(out),
                    "id": t.get("id"),
                    "title": t.get("title") or "",
                    "lang": t.get("lang") or "",
                })
        return out

    def audio_tracks(self) -> List[dict]:
        return self._tracks_of("audio")

    def subtitle_tracks(self) -> List[dict]:
        return self._tracks_of("sub")

    def set_audio_track(self, track_id: Any) -> None:
        self._set("aid", track_id)

    def set_subtitle_track(self, track_id: Any) -> None:
        self._set("sid", track_id)

    def current_audio_track(self) -> Any:
        return self._get("aid")

    def current_subtitle_track(self) -> Any:
        return self._get("sid")

    def cycle_audio_track(self) -> None:
        self._send({"command": ["cycle", "audio"]})

    def cycle_subtitle_track(self) -> None:
        self._send({"command": ["cycle", "sub"]})

    def set_playback_end(self, seconds: Optional[float]) -> None:
        self._set("end", "no" if seconds is None else str(seconds))

    def add_subtitle_file(self, path: str) -> None:
        self._send({"command": ["sub-add", path, "select"]})

    @staticmethod
    def is_audio_only(track_list) -> bool:
        has_audio = any(t.get("type") == "audio" for t in track_list)
        has_video = any(t.get("type") == "video" and not t.get("albumart")
                        for t in track_list)
        return has_audio and not has_video

    def buffer_status(self) -> Dict[str, Any]:
        """Return live mpv cache/playback state over IPC."""
        if self._pipe is None:
            return {}
        try:
            pct = self._get("cache-buffering-state")
            pfc = self._get("paused-for-cache")
            dcd = self._get("demuxer-cache-duration")
            idle = self._get("core-idle")
            tpos = self._get("time-pos")
            pct = int(pct) if pct is not None else -1
            pfc_bool = str(pfc).lower() == "yes" if pfc is not None else False
            dcd_f = float(dcd or 0.0)
            core_idle = str(idle).lower() == "yes" if idle is not None else True
            tpos_f = float(tpos or 0.0)
            if tpos_f > 0.0:
                core_idle = False
            if tpos_f > 0.0 and not pfc_bool and not core_idle:
                state = "playing"
            elif pfc_bool or (0 <= pct < 100) or (dcd_f > 0.0):
                state = "buffering"
            elif core_idle:
                state = "opening"
            else:
                state = "buffering"
            return {
                "state": state,
                "percent": pct,
                "paused_for_cache": pfc_bool,
                "demuxer_cache_duration": dcd_f,
                "core_idle": core_idle,
                "time_pos": tpos_f,
            }
        except Exception:
            return {}

    @property
    def is_playing(self) -> bool:
        return self._pipe is not None and not self._paused
