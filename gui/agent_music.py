"""Agent-tab background music (owner request 2026-11-18).

One bundled MP3 (repo-root ``backgroundmusic.mp3``, shipped via app.spec /
app_linux.spec datas) played on a dedicated AUDIO-ONLY libmpv instance with
``--loop-file=inf`` — it loops forever and costs one lightweight core, no
window, no video (``video=no`` also keeps ID3 cover art from ever opening
one). The lifecycle rules come straight from the owner:

* the Agent tab's ♫ button is the ONLY on-switch (no autostart, nothing
  persisted — music on boot would be obnoxious);
* opening media anywhere ELSE stops it: the mini player's ``open_media``
  and the Play tab's ``PlayerWidget.play`` call :func:`stop_agent_music`
  (browser-tab web video does NOT count — it is web content with its own
  audio);
* it stays off afterwards — no auto-resume when that media ends; the
  user brings it back with the button;
* MainWindow.closeEvent tears the mpv handle down (a live libmpv at
  interpreter shutdown is the known exit-crash class).

Qt-free and dependency-light on purpose (stdlib + a lazy python-mpv
import inside :meth:`AgentMusicController.start`), so ``iptv_tab`` and
``mini_player`` can call the stop hook without importing the heavy
``gui.main_window`` chain, and tests can stub the mpv layer entirely.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Background level — the music sits UNDER whatever the user is doing, not
# over it. No volume UI was asked for; this is the single knob.
VOLUME = 35

# Platforms where the bundled player stack exists (mirrors the Play tab's
# _PLAY_TAB_SUPPORTED tuple; macOS ships no libmpv).
MUSIC_SUPPORTED = sys.platform in ("win32", "linux")


def resolve_music_path() -> str:
    """Find the bundled track — dev tree or frozen ``_MEIPASS`` (same
    candidate pattern as gui.brand_assets)."""
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "backgroundmusic.mp3"),
        os.path.join(getattr(sys, "_MEIPASS", ""), "backgroundmusic.mp3"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return ""


class AgentMusicController:
    """Owns the one audio-only mpv instance. All methods are idempotent and
    GUI-thread-only by contract (button clicks + play-path hooks)."""

    def __init__(self) -> None:
        self._mpv: Optional[object] = None
        self._lock = threading.Lock()
        # State-change listeners (every ♫ button syncs with this — the mini
        # player and the full app can both be alive across morphs). Called
        # with the new playing state on EVERY transition.
        self._listeners: list = []

    def add_state_listener(self, cb: Callable[[bool], None]) -> None:
        if cb not in self._listeners:
            self._listeners.append(cb)

    @property
    def on_state_change(self) -> Optional[Callable[[bool], None]]:
        """Single-listener convenience (tests). Assigning replaces the list."""
        return self._listeners[0] if self._listeners else None

    @on_state_change.setter
    def on_state_change(self, cb: Optional[Callable[[bool], None]]) -> None:
        self._listeners = [cb] if cb is not None else []

    # ------------------------------------------------------------------ state
    def is_playing(self) -> bool:
        return self._mpv is not None

    def _notify(self, playing: bool) -> None:
        for cb in list(self._listeners):
            try:
                cb(playing)
            except Exception:
                logger.debug("music state listener failed", exc_info=True)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        """Start (or keep) the looping track. False = unavailable (no
        bundled track / no libmpv); the caller decides how to surface it."""
        with self._lock:
            if self._mpv is not None:
                return True
            path = resolve_music_path()
            if not path:
                logger.warning("agent music: bundled track not found")
                return False
            try:
                from iptv.player import ensure_mpv_dll_on_path
                ensure_mpv_dll_on_path()
                import mpv
                # video=no: audio-only core — no window, ever (also kills
                # ID3 cover art popups); loop-file=inf: the owner's loop.
                player = mpv.MPV(video=False, loop_file="inf", volume=VOLUME)
                player.loadfile(path)
            except Exception as exc:
                logger.warning("agent music: playback unavailable: %s", exc)
                return False
            self._mpv = player
        self._notify(True)
        return True

    def stop(self) -> None:
        """Stop and destroy the player (music never auto-resumes)."""
        with self._lock:
            player, self._mpv = self._mpv, None
        if player is not None:
            try:
                player.terminate()
            except Exception:
                logger.debug("agent music terminate failed", exc_info=True)
            self._notify(False)

    def toggle(self) -> bool:
        """Flip the state; returns the NEW playing state."""
        if self.is_playing():
            self.stop()
            return False
        return self.start()

    def shutdown(self) -> None:
        """App-exit teardown (same as stop; explicit alias for closeEvent)."""
        self.stop()


# ---------------------------------------------------------------------------
# Process-level singleton + hook entry points
# ---------------------------------------------------------------------------

_CONTROLLER: Optional[AgentMusicController] = None


def controller() -> AgentMusicController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = AgentMusicController()
    return _CONTROLLER


def stop_agent_music() -> None:
    """Media-open hook: called by the mini player and the Play tab whenever
    OTHER media starts — background music yields immediately."""
    ctl = _CONTROLLER
    if ctl is not None and ctl.is_playing():
        ctl.stop()


def maybe_autostart(config: Any) -> bool:
    """Boot path (owner 2026-11-18: "background music starts on"): start the
    track when the user hasn't turned it off. ``config.ui_background_music``
    defaults True; toggling either ♫ button persists the choice, so OFF
    stays OFF across launches. No-op (False) when unsupported."""
    if not MUSIC_SUPPORTED:
        return False
    if not getattr(config, "ui_background_music", True):
        return False
    return controller().start()
