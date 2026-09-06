"""MilkDrop audio visualization backend (Butterchurn in QtWebEngine).

Audio-only media gets a real MilkDrop visualizer instead of a black
rectangle. The renderer is `Butterchurn <https://github.com/jberg/butterchurn>`_
— a WebGL implementation of MilkDrop 2 — running in a ``QWebEngineView``, with
the ``.milk`` presets converted to its GLSL/JS form locally (no network).

Why the *page* owns playback: Butterchurn analyses a WebAudio node, so it can
only see audio the page itself plays. mpv's output isn't reachable from
JavaScript, so for audio files we hand playback to Chromium and keep mpv for
everything else. Formats Chromium can't decode report an error and
:class:`MilkdropBackend` reports ``unsupported`` so the caller can fall back.

Presets live in the repo's ``MilkDrop/`` folder (shipped with the app) plus
``~/.deeptorrent/presets`` — users add more by dropping ``.milk`` files in
either one.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Callable, List, Optional

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from iptv.player import PlayerBackend

logger = logging.getLogger(__name__)

# Audio containers Chromium decodes. Anything else goes back to mpv.
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav"}
# Recognised as audio but not decodable in Chromium — never routed here.
OTHER_AUDIO_EXTS = {".wma", ".alac", ".ape", ".aiff", ".aif", ".dsf", ".wv"}


def is_audio_url(url: str) -> bool:
    """True for media we can hand to the in-page player."""
    if not url:
        return False
    path = url.split("?", 1)[0].split("#", 1)[0]
    return os.path.splitext(path)[1].lower() in AUDIO_EXTS


def is_audio_file(url: str) -> bool:
    """True for any audio file, decodable here or not (mpv handles the rest)."""
    if not url:
        return False
    path = url.split("?", 1)[0].split("#", 1)[0]
    return os.path.splitext(path)[1].lower() in (AUDIO_EXTS | OTHER_AUDIO_EXTS)


def assets_dir() -> str:
    """Directory holding visualizer.html and the bundled JS."""
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "milkdrop"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "packaging", "milkdrop"))
    for c in candidates:
        if os.path.isfile(os.path.join(c, "visualizer.html")):
            return os.path.abspath(c)
    return ""


def shipped_preset_dir() -> str:
    """The ``MilkDrop/`` folder that ships with the app.

    Frozen builds keep it under the assets dir: a top-level ``MilkDrop``
    would merge into ``milkdrop`` on case-insensitive filesystems."""
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "milkdrop", "presets"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "MilkDrop"))
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c)
    return ""


def user_preset_dir() -> str:
    """Where the user drops extra ``.milk`` files."""
    return os.path.join(os.path.expanduser("~"), ".deeptorrent", "presets")


def list_presets() -> List[str]:
    """Paths of every available ``.milk`` preset, shipped ones first."""
    out: List[str] = []
    seen = set()
    for d in (shipped_preset_dir(), user_preset_dir()):
        if not d or not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".milk") and f.lower() not in seen:
                seen.add(f.lower())
                out.append(os.path.join(d, f))
    return out


def preset_name(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


class MilkdropBackend(PlayerBackend):
    """Plays audio in a QWebEngineView and visualizes it with Butterchurn."""

    name = "milkdrop"

    def __init__(self, parent_widget: Any) -> None:
        super().__init__(parent_widget)
        self.view: Optional[QWebEngineView] = None
        self._loaded = False           # page (not media) ready
        self._pending_url = ""
        self._pending_preset = ""
        self._preset = ""
        self._playing = False
        self._reported_state = ""
        self._poll = QTimer(parent_widget)
        self._poll.setInterval(250)
        self._poll.timeout.connect(self._tick)
        # Raised when Chromium can't decode the media, so the host can retry
        # with mpv instead of showing a dead player.
        self.on_unsupported: Optional[Callable[[], None]] = None

    # -- lifecycle -----------------------------------------------------------
    def create(self) -> bool:
        assets = assets_dir()
        if not assets:
            logger.warning("MilkDrop assets missing (packaging/milkdrop)")
            return False
        try:
            # Deliberately PARENTLESS: a QWebEngineView is a native window, and
            # adding one as a child of the mpv surface makes Qt re-create that
            # surface's HWND — mpv loses the window it embedded into and its
            # core shuts down (audio kept playing, video went black). The host
            # puts this view in a QStackedWidget *beside* the surface instead.
            self.view = QWebEngineView()
            s = self.view.settings()
            s.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
            s.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
            # No click reaches this page, so autoplay must not need a gesture.
            s.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
            s.setAttribute(QWebEngineSettings.ScrollAnimatorEnabled, False)
            self.view.setContextMenuPolicy(self.view.contextMenuPolicy())
            self.view.loadFinished.connect(self._on_load_finished)
            self.view.load(QUrl.fromLocalFile(os.path.join(assets, "visualizer.html")))
            self._poll.start()
            return True
        except Exception:
            logger.exception("MilkDrop backend create failed")
            self.view = None
            return False

    def destroy(self) -> None:
        self._poll.stop()
        if self.view is not None:
            try:
                self.view.stop()
                self.view.setParent(None)
                self.view.deleteLater()
            except Exception:
                pass
        self.view = None
        self._loaded = False
        self._playing = False

    # -- page plumbing -------------------------------------------------------
    def _js(self, script: str) -> None:
        if self.view is not None:
            self.view.page().runJavaScript(script)

    def _on_load_finished(self, ok: bool) -> None:
        self._loaded = bool(ok)
        if not ok:
            if self.on_error:
                self.on_error("MilkDrop visualizer failed to load")
            return
        if self._pending_preset:
            self.set_preset(self._pending_preset)
            self._pending_preset = ""
        if self._pending_url:
            url, self._pending_url = self._pending_url, ""
            self.play(url)

    def _tick(self) -> None:
        if not self._loaded or self.view is None:
            return
        self.view.page().runJavaScript("window.mpState ? window.mpState() : ''",
                                       self._on_state_json)

    def _on_state_json(self, raw: Any) -> None:
        if not raw:
            return
        try:
            st = json.loads(raw)
        except Exception:
            return
        if st.get("error", "").startswith("media error"):
            # Chromium can't decode this one — let the host fall back to mpv.
            if self.on_unsupported:
                cb, self.on_unsupported = self.on_unsupported, None
                cb()
            return
        if not st.get("ready"):
            return  # still buffering: don't report a spurious "paused"
        self._playing = bool(st.get("playing"))
        if self.on_position and st.get("duration"):
            self.on_position(float(st.get("position") or 0.0),
                             float(st.get("duration") or 0.0))
        state = "stopped" if st.get("ended") else ("playing" if self._playing else "paused")
        if state != self._reported_state:
            self._reported_state = state
            if self.on_state:
                self.on_state(state)

    # -- transport -----------------------------------------------------------
    def play(self, url: str, headers: Optional[dict] = None) -> None:
        if self.view is None and not self.create():
            if self.on_error:
                self.on_error("MilkDrop visualizer unavailable")
            return
        if not self._loaded:
            self._pending_url = url  # resumed from _on_load_finished
            return
        self._reported_state = ""
        src = QUrl.fromLocalFile(url).toString() if os.path.isfile(url) else url
        self._js(f"window.mpLoad({json.dumps(src)})")
        if self.on_state:
            self.on_state("playing")

    def pause(self) -> None:
        self._js("window.mpPause()")
        self._playing = False

    def resume(self) -> None:
        self._js("window.mpResume()")
        self._playing = True

    def stop(self) -> None:
        self._js("window.mpStop()")
        self._playing = False
        if self.on_state:
            self.on_state("stopped")

    def seek(self, seconds: float) -> None:
        self._js(f"window.mpSeek({float(seconds)})")

    def seek_by(self, delta: float) -> None:
        self._js(f"window.mpSeekBy({float(delta)})")

    def set_volume(self, pct: int) -> None:
        self._js(f"window.mpVolume({int(pct)})")

    def set_mute(self, muted: bool) -> None:
        self._js(f"window.mpMute({str(bool(muted)).lower()})")

    # -- presets -------------------------------------------------------------
    def set_preset(self, path: str, blend: float = 2.0) -> None:
        """Load a ``.milk`` file (converted in-page, cached per session)."""
        if not path or not os.path.isfile(path):
            return
        if not self._loaded:
            self._pending_preset = path
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            logger.warning("cannot read preset %s", path, exc_info=True)
            return
        self._preset = path
        self._js("window.mpSetPreset({}, {}, {})".format(
            json.dumps(preset_name(path)), json.dumps(text), float(blend)))

    def current_preset(self) -> str:
        return self._preset

    # -- unsupported knobs ---------------------------------------------------
    def set_hwdec(self, mode: str) -> None:
        pass

    def set_cache(self, seconds: int, max_bytes=None) -> None:
        pass

    def set_aspect(self, mode: str) -> None:
        pass

    def set_deinterlace(self, on: bool) -> None:
        pass

    def set_overscan(self, pct: float) -> None:
        pass

    @property
    def is_playing(self) -> bool:
        return self._playing
