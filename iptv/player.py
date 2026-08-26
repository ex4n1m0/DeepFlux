"""Embedded media player backends.

A small abstraction (:class:`PlayerBackend`) so the playback engine can be
swapped. Two implementations:

- :class:`MpvBackend` — libmpv via ``python-mpv``, rendered into a Qt widget
  using mpv's ``wid`` embedding. Preferred for rendering quality and low
  overhead. Hardware decoding defaults to ``auto-safe`` with a software
  fallback.
- :class:`LibVLCBackend` — libVLC via ``python-vlc``, the documented fallback.

The mpv DLL (``mpv-2.dll`` / ``libmpv-2.dll``) and the FFmpeg libraries it
depends on are *bundled* in the app folder by the installer (see
``packaging/app.spec``). At runtime we prepend the bundled ``mpv`` directory
to ``PATH`` before importing ``mpv`` so the binding finds the DLL on a clean
machine with no external installs.

All backends emit state changes through Qt signals defined on the host widget
(:class:`PlayerWidget` in :mod:`gui.iptv_tab`) via callbacks; the backends
themselves are Qt-light so they can be unit-tested without a display.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bundled DLL discovery
# ---------------------------------------------------------------------------

def _bundled_dll_dir() -> str:
    """Return the directory that ships mpv + ffmpeg DLLs, or '' if not found."""
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "mpv"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "packaging", "mpv"))
    candidates.append(os.path.join(here, "..", "mpv"))
    for c in candidates:
        if c and os.path.isdir(c):
            return os.path.abspath(c)
    return ""


def ensure_mpv_dll_on_path() -> None:
    """Prepend the bundled mpv/ffmpeg DLL directory to PATH (idempotent)."""
    d = _bundled_dll_dir()
    if d and d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------

class PlayerBackend:
    """Abstract playback backend."""

    name = "base"

    def __init__(self, parent_widget: Any) -> None:
        self.parent = parent_widget
        # Callbacks set by the host widget:
        self.on_state: Optional[Callable[[str], None]] = None  # "playing"|"paused"|"stopped"|"error"
        self.on_position: Optional[Callable[[float, float], None]] = None  # (pos, duration) seconds
        self.on_buffer: Optional[Callable[[float], None]] = None  # 0..1
        on_meta = None  # placeholder
        self.on_meta: Optional[Callable[[dict], None]] = None
        self.on_tracks: Optional[Callable[[List[dict]], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

    # lifecycle
    def create(self) -> bool:
        raise NotImplementedError
    def destroy(self) -> None:
        raise NotImplementedError

    # transport
    def play(self, url: str, headers: Optional[dict] = None) -> None:
        raise NotImplementedError
    def pause(self) -> None:
        raise NotImplementedError
    def resume(self) -> None:
        raise NotImplementedError
    def stop(self) -> None:
        raise NotImplementedError
    def seek(self, seconds: float) -> None:
        raise NotImplementedError
    def seek_by(self, delta: float) -> None:
        """Relative seek in seconds (negative = rewind)."""
        raise NotImplementedError

    # settings
    def set_volume(self, pct: int) -> None:
        raise NotImplementedError
    def set_mute(self, muted: bool) -> None:
        raise NotImplementedError
    def set_hwdec(self, mode: str) -> None:
        raise NotImplementedError
    def set_cache(self, seconds: int) -> None:
        raise NotImplementedError
    def set_aspect(self, mode: str) -> None:
        raise NotImplementedError
    def set_deinterlace(self, on: bool) -> None:
        raise NotImplementedError
    def set_overscan(self, pct: float) -> None:
        raise NotImplementedError
    def set_interpolation(self, on: bool) -> None:
        pass
    def set_smooth_video(self, on: bool) -> None:
        """Enable display-locked presentation (files) or plain audio-clock
        sync (live streams)."""
        pass

    # tracks
    def audio_tracks(self) -> List[dict]:
        return []
    def subtitle_tracks(self) -> List[dict]:
        return []
    def set_audio_track(self, track_id: Any) -> None:
        """Select by track id; 'auto'/'no' where the backend supports them."""
        pass
    def set_subtitle_track(self, track_id: Any) -> None:
        """Select by track id; 'no'/−1 disables subtitles."""
        pass
    def current_audio_track(self) -> Any:
        return None
    def current_subtitle_track(self) -> Any:
        return None
    def cycle_audio_track(self) -> None:
        pass
    def cycle_subtitle_track(self) -> None:
        pass

    def set_playback_end(self, seconds: Optional[float]) -> None:
        """Cap playback at a position (seconds), None clears the cap.

        Used when playing a still-downloading file: the cap keeps the player
        inside the verified contiguous prefix so it never reads the zeroed /
        unverified region past the download frontier."""
        pass

    def add_subtitle_file(self, path: str) -> None:
        """Load an external subtitle file and select it immediately."""
        pass

    # fullscreen handled by the host widget (window), not the backend.

    @property
    def is_playing(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# libmpv backend
# ---------------------------------------------------------------------------

class MpvBackend(PlayerBackend):
    """libmpv via python-mpv, embedded into a Qt widget via ``wid``."""

    name = "mpv"

    def __init__(self, parent_widget: Any) -> None:
        super().__init__(parent_widget)
        self._mpv: Any = None
        self._created = False
        self._want_interpolation = False  # user setting; gated by smooth mode
        self._smooth = True               # False for live streams

    def _creation_kwargs(self, wid: str, vo: str) -> dict:
        """mpv.MPV constructor kwargs.

        NB: SVP cannot be driven from here. Its VapourSynth chain embeds a
        CPython runtime that will not initialize inside our own Python
        process (VSScript deadlocks), so SVP mode uses the separate
        :class:`iptv.mpv_process.MpvProcessBackend` instead."""
        return dict(
            wid=wid,
            vo=vo,
            hwdec="auto-safe",
            video_sync="display-resample",
            keep_open="always",
            input_default_bindings=True,
            input_vo_keyboard=False,
            input_cursor=False,
            osc=False,
        )

    def create(self) -> bool:
        ensure_mpv_dll_on_path()
        try:
            import mpv  # noqa: E402  (import after PATH tweak)
        except OSError as exc:
            logger.warning("libmpv unavailable: %s", exc)
            return False
        try:
            # Embed into the host widget's native window handle.
            wid = int(self.parent.winId())

            def _make(vo: str) -> Any:
                return mpv.MPV(**self._creation_kwargs(str(wid), vo))

            try:
                # gpu-next (libplacebo) is what renders Dolby Vision / HDR
                # tone mapping correctly — legacy vo=gpu plays DV P5 with
                # wrong colors (verified against Dolby's official P5 sample).
                self._mpv = _make("gpu-next")
            except Exception:
                # Ancient GPU/driver without libplacebo-capable backends:
                # fall back to the legacy vo so playback still works.
                logger.warning("vo=gpu-next failed to init; falling back to vo=gpu")
                self._mpv = _make("gpu")

            @self._mpv.property_observer("time-pos")
            def _pos(_name, value):
                if value is not None and self.on_position:
                    dur = self._mpv.duration or 0.0
                    self.on_position(float(value), float(dur))

            @self._mpv.property_observer("pause")
            def _pause(_name, value):
                if self.on_state:
                    self.on_state("paused" if value else "playing")

            @self._mpv.property_observer("eof-reached")
            def _eof(_name, value):
                if value and self.on_state:
                    self.on_state("stopped")

            @self._mpv.property_observer("track-list")
            def _tracks(_name, value):
                if value and self.on_tracks:
                    self.on_tracks(value)

            @self._mpv.event_callback("end-file")
            def _end_file(event):
                # The event is a ctypes MpvEvent struct (no .get()). Real
                # errors are signalled via data.reason == ERROR (4); normal
                # EOF and our own stop() arrive with reason EOF/STOP and must
                # not surface as errors.
                data = getattr(event, "data", None)
                reason = getattr(data, "reason", 0) if data is not None else 0
                if reason == 4 and self.on_error:  # 4 = END_FILE ERROR
                    self.on_error("Stream ended unexpectedly")

            self._created = True
            return True
        except Exception as exc:
            logger.exception("mpv create failed: %s", exc)
            self._mpv = None
            return False

    def destroy(self) -> None:
        if self._mpv is not None:
            try:
                self._mpv.terminate()
            except Exception:
                pass
            self._mpv = None
        self._created = False

    def play(self, url: str, headers: Optional[dict] = None) -> None:
        if self._mpv is None and not self.create():
            if self.on_error:
                self.on_error("Playback backend unavailable")
            return
        try:
            if headers:
                # Pass all headers through http-header-fields; also set the
                # dedicated UA/referrer options mpv uses for its own requests.
                fields = [f"{k}: {v}" for k, v in headers.items() if v]
                if fields:
                    self._mpv["http-header-fields"] = fields
                ua = headers.get("User-Agent", "")
                ref = headers.get("Referer", "")
                if ua:
                    self._mpv["user-agent"] = ua
                if ref:
                    self._mpv["referrer"] = ref
            self._mpv.play(url)
            if self.on_state:
                self.on_state("playing")
        except Exception as exc:
            logger.warning("mpv play failed: %s", exc)
            if self.on_error:
                self.on_error(str(exc))

    def pause(self) -> None:
        if self._mpv is not None:
            self._mpv.pause = True

    def resume(self) -> None:
        if self._mpv is not None:
            self._mpv.pause = False

    def stop(self) -> None:
        if self._mpv is not None:
            try:
                self._mpv.command("stop")
            except Exception:
                pass
            if self.on_state:
                self.on_state("stopped")

    def seek(self, seconds: float) -> None:
        if self._mpv is not None:
            try:
                self._mpv.command("seek", seconds, "absolute")
            except Exception:
                pass

    def seek_by(self, delta: float) -> None:
        if self._mpv is not None:
            try:
                # mpv clamps relative seeks to [0, duration] itself.
                self._mpv.command("seek", delta, "relative")
            except Exception:
                pass

    def set_volume(self, pct: int) -> None:
        if self._mpv is not None:
            self._mpv.volume = max(0, min(100, pct))

    def set_mute(self, muted: bool) -> None:
        if self._mpv is not None:
            self._mpv.mute = bool(muted)

    def set_hwdec(self, mode: str) -> None:
        if self._mpv is not None:
            try:
                self._mpv.hwdec = mode or "auto-safe"
            except Exception:
                pass

    def set_cache(self, seconds: int) -> None:
        if self._mpv is not None:
            try:
                self._mpv["cache-secs"] = max(1, int(seconds))
                self._mpv["cache"] = "yes"
            except Exception:
                pass

    def set_aspect(self, mode: str) -> None:
        if self._mpv is not None:
            # video-aspect was removed in modern mpv; video-aspect-override
            # replaces it ("no" = auto). Fall back for very old libmpv builds.
            value = mode if mode and mode != "auto" else "no"
            try:
                self._mpv["video-aspect-override"] = value
            except Exception:
                try:
                    self._mpv["video-aspect"] = value
                except Exception:
                    pass

    def set_deinterlace(self, on: bool) -> None:
        if self._mpv is not None:
            try:
                self._mpv["deinterlace"] = "yes" if on else "no"
            except Exception:
                pass

    def set_overscan(self, pct: float) -> None:
        """Zoom slightly so the outermost pixels land off-screen.

        Broadcast streams often carry dirty edge rows/columns (capture or
        encoding garbage, sometimes asymmetric) that show as a faint bright
        line hugging the frame edge. mpv's video-zoom is log2 of the scale
        factor; 0.5% ≈ 5px per side at 1080p — an imperceptible crop, same
        idea as TV overscan. 0 disables."""
        if self._mpv is not None:
            try:
                self._mpv.video_zoom = math.log2(1 + max(0.0, pct) / 100.0)
            except Exception:
                pass

    def set_interpolation(self, on: bool) -> None:
        """mpv "smoothmotion": blend frames so low-fps content tracks the
        display refresh. Requires video-sync=display-*, so it only takes
        effect in smooth mode (see :meth:`set_smooth_video`)."""
        self._want_interpolation = bool(on)
        self._apply_sync()

    def set_smooth_video(self, on: bool) -> None:
        """Frame-perfect presentation vs mpv's default audio-clock sync.

        display-resample locks video to the display refresh and resamples
        audio to match — great for files, bad for LIVE streams: their
        timestamps drift and the buffer is not seekable, so the player ends
        up stalling and restarting. Live playback therefore runs with the
        default `audio` sync and no interpolation."""
        self._smooth = bool(on)
        self._apply_sync()

    def _apply_sync(self) -> None:
        if self._mpv is None:
            return
        try:
            self._mpv["video-sync"] = "display-resample" if self._smooth else "audio"
            self._mpv["interpolation"] = "yes" if (self._smooth and self._want_interpolation) else "no"
            self._mpv["tscale"] = "oversample"
        except Exception:
            pass

    @staticmethod
    def is_audio_only(track_list) -> bool:
        """True when the file has audio but no real video.

        Cover art counts as a video track in mpv (``albumart``), but a static
        image doesn't make it a video."""
        has_audio = any(t.get("type") == "audio" for t in track_list)
        has_video = any(t.get("type") == "video" and not t.get("albumart")
                        for t in track_list)
        return has_audio and not has_video

    def audio_tracks(self) -> List[dict]:
        if self._mpv is None:
            return []
        try:
            n = int(self._mpv["track-list/count"])
            out = []
            for i in range(n):
                t = self._mpv[f"track-list/{i}/type"]
                if t == "audio":
                    out.append({
                        "index": i,
                        "id": self._mpv[f"track-list/{i}/id"],
                        "title": self._mpv[f"track-list/{i}/title"] or "",
                        "lang": self._mpv[f"track-list/{i}/lang"] or "",
                    })
            return out
        except Exception:
            return []

    def subtitle_tracks(self) -> List[dict]:
        if self._mpv is None:
            return []
        try:
            n = int(self._mpv["track-list/count"])
            out = []
            for i in range(n):
                t = self._mpv[f"track-list/{i}/type"]
                if t == "sub":
                    out.append({
                        "index": i,
                        "id": self._mpv[f"track-list/{i}/id"],
                        "title": self._mpv[f"track-list/{i}/title"] or "",
                        "lang": self._mpv[f"track-list/{i}/lang"] or "",
                    })
            return out
        except Exception:
            return []

    def set_audio_track(self, track_id: Any) -> None:
        # aid/sid take the track's *id* (or "auto"/"no"), NOT its position in
        # track-list — the two diverge whenever video tracks are interleaved.
        if self._mpv is not None:
            try:
                self._mpv["aid"] = track_id
            except Exception:
                pass

    def set_subtitle_track(self, track_id: Any) -> None:
        if self._mpv is not None:
            try:
                self._mpv["sid"] = track_id
            except Exception:
                pass

    def current_audio_track(self) -> Any:
        if self._mpv is None:
            return None
        try:
            return self._mpv["aid"]
        except Exception:
            return None

    def current_subtitle_track(self) -> Any:
        if self._mpv is None:
            return None
        try:
            return self._mpv["sid"]
        except Exception:
            return None

    def cycle_audio_track(self) -> None:
        if self._mpv is not None:
            try:
                self._mpv.command("cycle", "audio")
            except Exception:
                pass

    def cycle_subtitle_track(self) -> None:
        if self._mpv is not None:
            try:
                self._mpv.command("cycle", "sub")
            except Exception:
                pass

    def set_playback_end(self, seconds: Optional[float]) -> None:
        if self._mpv is not None:
            try:
                self._mpv["end"] = "no" if seconds is None else str(seconds)
            except Exception:
                pass

    def add_subtitle_file(self, path: str) -> None:
        if self._mpv is not None:
            try:
                # "select" makes the new track active right away.
                self._mpv.command("sub-add", path, "select")
            except Exception:
                pass

    @property
    def is_playing(self) -> bool:
        return self._mpv is not None and not bool(self._mpv.pause)


# ---------------------------------------------------------------------------
# libVLC fallback backend
# ---------------------------------------------------------------------------

class LibVLCBackend(PlayerBackend):
    """libVLC via python-vlc — the documented fallback backend."""

    name = "vlc"

    def __init__(self, parent_widget: Any) -> None:
        super().__init__(parent_widget)
        self._instance: Any = None
        self._player: Any = None
        self._media: Any = None

    def create(self) -> bool:
        try:
            import vlc  # noqa: E402
        except OSError as exc:
            logger.warning("libVLC unavailable: %s", exc)
            return False
        try:
            self._instance = vlc.Instance("--no-xlib")
            self._player = self._instance.media_player_new()
            # Embed into the host widget.
            wid = int(self.parent.winId())
            if sys.platform == "win32":
                self._player.set_hwnd(wid)
            else:
                self._player.set_xwindow(wid)
            return True
        except Exception as exc:
            logger.exception("libVLC create failed: %s", exc)
            return False

    def destroy(self) -> None:
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
        self._player = None
        self._instance = None
        self._media = None

    def play(self, url: str, headers: Optional[dict] = None) -> None:
        if self._player is None and not self.create():
            if self.on_error:
                self.on_error("Playback backend unavailable")
            return
        try:
            # Custom User-Agent/Referer for VLC is set via instance options.
            if headers:
                ua = headers.get("User-Agent", "")
                if ua:
                    self._instance.set_user_agent("DeepFlux-IPTV", ua)
            self._media = self._instance.media_new(url)
            self._player.set_media(self._media)
            self._player.play()
            if self.on_state:
                self.on_state("playing")
        except Exception as exc:
            logger.warning("vlc play failed: %s", exc)
            if self.on_error:
                self.on_error(str(exc))

    def pause(self) -> None:
        if self._player is not None:
            self._player.pause()

    def resume(self) -> None:
        if self._player is not None:
            self._player.play()

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()
            if self.on_state:
                self.on_state("stopped")

    def seek(self, seconds: float) -> None:
        if self._player is not None:
            try:
                self._player.set_time(int(seconds * 1000))
            except Exception:
                pass

    def seek_by(self, delta: float) -> None:
        if self._player is not None:
            try:
                t = self._player.get_time()  # ms, -1 when unknown
                if t >= 0:
                    self._player.set_time(max(0, t + int(delta * 1000)))
            except Exception:
                pass

    def set_volume(self, pct: int) -> None:
        if self._player is not None:
            self._player.audio_set_volume(max(0, min(100, pct)))

    def set_mute(self, muted: bool) -> None:
        if self._player is not None:
            self._player.audio_set_mute(bool(muted))

    # hwdec/cache/aspect/deinterlace/overscan are best-effort no-ops on the
    # VLC backend.

    @staticmethod
    def _vlc_tracks(desc: Any) -> List[dict]:
        """Normalize libvlc track-description tuples [(id, name), ...]."""
        out = []
        for t in desc or []:
            try:
                tid, name = t[0], t[1]
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "replace")
                out.append({"index": len(out), "id": int(tid),
                            "title": str(name), "lang": ""})
            except Exception:
                continue
        return out

    def audio_tracks(self) -> List[dict]:
        if self._player is None:
            return []
        try:
            # Skip libvlc's synthetic "Disable" entry (id -1).
            return [t for t in self._vlc_tracks(self._player.audio_get_track_description())
                    if t["id"] >= 0]
        except Exception:
            return []

    def subtitle_tracks(self) -> List[dict]:
        if self._player is None:
            return []
        try:
            return [t for t in self._vlc_tracks(self._player.video_get_spu_description())
                    if t["id"] >= 0]
        except Exception:
            return []

    def set_audio_track(self, track_id: Any) -> None:
        if self._player is not None:
            try:
                self._player.audio_set_track(int(track_id))
            except Exception:
                pass

    def set_subtitle_track(self, track_id: Any) -> None:
        if self._player is not None:
            try:
                # libvlc disables subtitles with -1.
                self._player.video_set_spu(-1 if track_id in ("no", None) else int(track_id))
            except Exception:
                pass

    def current_audio_track(self) -> Any:
        if self._player is None:
            return None
        try:
            tid = self._player.audio_get_track()
            return tid if tid >= 0 else "no"
        except Exception:
            return None

    def current_subtitle_track(self) -> Any:
        if self._player is None:
            return None
        try:
            tid = self._player.video_get_spu()
            return tid if tid >= 0 else "no"
        except Exception:
            return None

    def cycle_audio_track(self) -> None:
        tracks = self.audio_tracks()
        if not tracks:
            return
        cur = self.current_audio_track()
        ids = [t["id"] for t in tracks]
        nxt = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
        self.set_audio_track(nxt)

    def cycle_subtitle_track(self) -> None:
        tracks = self.subtitle_tracks()
        ids = [t["id"] for t in tracks] + [-1]  # include "off" in the cycle
        cur = self.current_subtitle_track()
        cur = -1 if cur in ("no", None) else cur
        nxt = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
        self.set_subtitle_track(nxt)

    def add_subtitle_file(self, path: str) -> None:
        if self._player is not None:
            try:
                self._player.video_set_subtitle_file(path)
            except Exception:
                pass
    def set_hwdec(self, mode: str) -> None:
        pass

    def set_cache(self, seconds: int) -> None:
        pass

    def set_aspect(self, mode: str) -> None:
        pass

    def set_deinterlace(self, on: bool) -> None:
        pass

    def set_overscan(self, pct: float) -> None:
        pass

    def set_interpolation(self, on: bool) -> None:
        pass

    def set_smooth_video(self, on: bool) -> None:
        pass

    @property
    def is_playing(self) -> bool:
        return self._player is not None and self._player.is_playing()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_svp_backend(parent_widget: Any) -> Optional[PlayerBackend]:
    """The out-of-process mpv backend used for SVP motion interpolation.

    Returns None whenever SVP isn't usable (not installed, mpv component
    missing, process/IPC failure) so the caller can fall back to the normal
    in-process backend — a broken SVP setup must never cost the user
    playback."""
    from . import svp as svp_mod
    inst = svp_mod.find_install()
    if inst is None or not inst.mpv_exe:
        logger.info("SVP requested but no usable SVP mpv.exe found")
        return None
    from .mpv_process import MpvProcessBackend
    b = MpvProcessBackend(parent_widget, mpv_exe=inst.mpv_exe)
    if b.create():
        logger.info("using out-of-process mpv for SVP: %s", inst.mpv_exe)
        return b
    b.destroy()
    return None


def create_backend(parent_widget: Any, preferred: str = "mpv",
                   svp: bool = False) -> Optional[PlayerBackend]:
    """Create the best available backend, falling back as needed.

    ``svp`` asks for SVP 4 motion interpolation, which requires the
    out-of-process mpv backend (VapourSynth cannot initialize inside our
    Python process — see :mod:`iptv.mpv_process`). Any failure there falls
    through to the normal in-process backends.
    """
    if svp and preferred != "vlc":
        b = create_svp_backend(parent_widget)
        if b is not None:
            return b
        logger.info("SVP backend unavailable; using in-process mpv")
    order = [("vlc", LibVLCBackend), ("mpv", MpvBackend)] if preferred == "vlc" else [("mpv", MpvBackend), ("vlc", LibVLCBackend)]
    for name, cls in order:
        b = cls(parent_widget)
        if b.create():
            if name != preferred:
                logger.info("%s backend unavailable; fell back to %s", preferred, name)
            return b
    return None
