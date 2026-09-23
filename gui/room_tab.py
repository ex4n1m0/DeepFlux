"""Room tab — the DeepFlux community chat: the OnlyHumans portal, embedded.

The Room page hosts the OnlyHumans browser portal
(https://onlyhumans.deepflux.space/join) in a QtWebEngine view — the exact
web app the site serves, so every portal improvement (UI, features, fixes)
lands in DeepFlux automatically with the site's next deploy. DeepFlux only
frames the page; the chat UI itself is not duplicated in the app.

The join gate opens with the community word pre-filled through the URL
hash (``#room=deepflux`` — the portal reads the fragment client-side, so
the word is never part of any HTTP request) and an EMPTY name: type a
name, press Enter. The portal's "remember my name and rooms" checkbox
persists in DeepFlux's own storage, so returning users can skip typing.

The web profile is DEDICATED, never the Browser tab's: its localStorage
holds the portal's identity key (``oh-portal-seed``), and clearing
browsing data must not erase the user's chat identity. Storage lives
under ``~/.deeptorrent/room-portal``.

``DF_NO_ROOM=1`` (boot smokes, CI, tests) skips creating the view
entirely — no page load, no /api/gk fetch, nothing registered against the
hub.

``ircmgr/oh_room.py`` (the native protocol member, 5.1) stays in the tree
as the protocol reference and test target; the GUI no longer runs it — a
second native member next to the portal page would double the user's
presence in the room.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config import DeeptorrentConfig

logger = logging.getLogger(__name__)

PORTAL_URL = "https://onlyhumans.deepflux.space/join"

# The portal's own page background — set as the QtWebEngine page
# background so the first composited frame doesn't flash white against
# the app's dark theme.
_PORTAL_BG = "#0e1518"


def _room_disabled_here() -> bool:
    """Boot smokes / CI / tests: never load the portal or touch the hub."""
    return bool(os.environ.get("DF_NO_ROOM"))


class RoomTab(QWidget):
    """The DeepFlux Room tab: the OnlyHumans join portal in a web view."""

    def __init__(self, config: DeeptorrentConfig,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        # CAUTION: MainWindow passes ``parent`` POSITIONALLY
        # (RoomTab(config, self)) — keep it second or startup breaks.
        word = (self._config.chat.default_word or "deepflux").strip() or "deepflux"
        # The room word rides in the FRAGMENT: the portal reads it
        # client-side (invitedWord in portal/app.ts) and it is never sent
        # to any server or log — the same privacy rule as the portal's own
        # #room=… invite links.
        self.portal_url = f"{PORTAL_URL}#room={quote(word, safe='')}"
        self._view = None
        self.web_profile = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Slim header: identity + the two controls the embedded page itself
        # cannot offer (reload after a hub hiccup, escape hatch to a real
        # browser tab).
        bar = QHBoxLayout()
        title = QLabel("DeepFlux Room")
        title.setStyleSheet("color:#a8edff;font-weight:600")
        bar.addWidget(title)
        bar.addStretch(1)
        reload_btn = QPushButton("↻ Reload")
        reload_btn.setToolTip("Reload the room page (use it after a "
                              "connection error)")
        reload_btn.clicked.connect(self._on_reload)
        bar.addWidget(reload_btn)
        open_btn = QPushButton("Open in browser")
        open_btn.setToolTip("Open the room in your web browser instead")
        open_btn.clicked.connect(self._on_open_external)
        bar.addWidget(open_btn)
        root.addLayout(bar)

        if _room_disabled_here():
            note = QLabel("Room is disabled in this environment "
                          "(DF_NO_ROOM is set).")
            note.setStyleSheet("color:#5b6b7c")
            note.setWordWrap(True)
            root.addWidget(note, 1)
            return

        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView

        # Dedicated persistent profile (NOT the Browser tab's): the
        # portal's identity key + remembered name live in its localStorage
        # and must survive app restarts — and must NOT be wiped when the
        # user clears browsing data in the Browser tab.
        storage = str(Path.home() / ".deeptorrent" / "room-portal")
        os.makedirs(storage, exist_ok=True)
        self.web_profile = QWebEngineProfile("deeptorrent-room", self)
        self.web_profile.setPersistentStoragePath(storage)
        self.web_profile.setCachePath(os.path.join(storage, "cache"))
        self.web_profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)

        page = QWebEnginePage(self.web_profile, self)
        page.setBackgroundColor(QColor(_PORTAL_BG))
        self._view = QWebEngineView()
        self._view.setPage(page)
        self._view.setUrl(QUrl(self.portal_url))
        root.addWidget(self._view, 1)

    # ------------------------------------------------------------------
    # downloads (portal file chips)
    # ------------------------------------------------------------------

    def attach_download_handler(self, handler: Callable) -> bool:
        """Let MainWindow route the portal's file downloads (shared files
        download via blob URLs; without a downloadRequested consumer
        QtWebEngine silently drops them). Returns whether a consumer was
        attached (False under DF_NO_ROOM, where no profile exists)."""
        if self.web_profile is None:
            return False
        self.web_profile.downloadRequested.connect(handler)
        return True

    # ------------------------------------------------------------------
    # toolbar actions
    # ------------------------------------------------------------------

    def _on_reload(self) -> None:
        if self._view is not None:
            self._view.reload()

    def _on_open_external(self) -> None:
        QDesktopServices.openUrl(QUrl(self.portal_url))

    # ------------------------------------------------------------------
    # shutdown (called from MainWindow.closeEvent)
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        if self._view is None:
            return
        try:
            # The portal's pagehide listener mails a best-effort Leave to
            # the host and beacons the presence counter down; fire it while
            # the render process can still send (both are idempotent if
            # the real pagehide follows during teardown). The protocol
            # also heals abrupt death on its own (host TTLs + re-seat), so
            # this is best-effort by design.
            self._view.page().runJavaScript(
                "window.dispatchEvent(new Event('pagehide'))")
        except Exception:
            logger.debug("room leave dispatch failed", exc_info=True)
        try:
            self._view.stop()
        except Exception:
            pass
