"""Room tab — the DeepFlux Room community chat (ircmgr/room.py).

Layout:
    +---------------------------------------------------------------+
    | join bar: DeepFlux Room [nickname] [Host…] [ip:port] [Join]   |
    |           [Make private…] · status…                           |
    +--------------------------------------------+------------------+
    | chat view (HTML transcript)               | members          |
    +--------------------------------------------+------------------+
    | topic: …                                                      |
    | [input line……………………………………] [Send]                           |
    +---------------------------------------------------------------+

The room is NOT IRC and uses no chat server: the first person to press
Join hosts a small chat server in-app; everyone else connects to them
directly (star topology, host takeover on leave). The controller
(:class:`ircmgr.room.RoomController`) owns all networking on daemon
threads and mirrors everything into an :class:`ircmgr.state.IRCState`;
events arrive here through a single queued Qt signal so no widget is
ever touched off the GUI thread. The room never auto-joins — nickname
+ Join, every session.
"""
from __future__ import annotations

import html
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QObject, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from config import DeeptorrentConfig, shared_room_secret
from ircmgr.room import ROOM_CHANNEL, RoomController
from ircmgr.state import IRCState, ROOM_NET_ID
from gui.responsive import ResponsiveRow, shrink_label

logger = logging.getLogger(__name__)

ROLE_NICK = Qt.ItemDataRole.UserRole

# Nick-color palette tuned for the app's dark theme.
_NICK_COLORS = [
    "#ff6e6e", "#ffa94d", "#ffd43b", "#a5d8ff", "#15aabf", "#a8edff",
    "#74c0fc", "#b197fc", "#f783ac", "#e9c46a", "#90e0ef", "#1971c2",
]
_SELF_COLOR = "#2a6aaf"
_MUTED_COLOR = "#5b6b7c"
_SERVER_COLOR = "#8a97a8"
_ACTION_COLOR = "#b197fc"
_ERROR_COLOR = "#ff6e6e"
_NOTICE_COLOR = "#ffa94d"

_LINK_RE = re.compile(r"(https?://[^\s<>\"']+)")


def _nick_color(nick: str) -> str:
    return _NICK_COLORS[sum(ord(c) for c in nick) % len(_NICK_COLORS)]


def _linkify(escaped_text: str) -> str:
    """Turn URLs into anchors. Input must already be HTML-escaped."""
    return _LINK_RE.sub(r'<a href="\1" style="color:#a8edff">\1</a>', escaped_text)


class RoomInputLine(QLineEdit):
    """One-session input history and member-nick completion (Tab)."""

    def __init__(self, completer: Callable[[str, int], Optional[Tuple[str, int]]],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._completion = completer
        self._history: List[str] = []
        self._history_pos = 0
        self._draft = ""
        self.textEdited.connect(self._reset_history_position)

    def remember(self, text: str) -> None:
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
            self._history = self._history[-100:]
        self._history_pos = len(self._history)
        self._draft = ""

    def _reset_history_position(self, _text: str) -> None:
        self._history_pos = len(self._history)

    def _move_history(self, delta: int) -> None:
        if not self._history:
            return
        if self._history_pos == len(self._history):
            self._draft = self.text()
        self._history_pos = max(0, min(len(self._history), self._history_pos + delta))
        self.setText(self._history[self._history_pos]
                     if self._history_pos < len(self._history) else self._draft)
        self.setCursorPosition(len(self.text()))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Up:
            self._move_history(-1)
            return
        if event.key() == Qt.Key.Key_Down:
            self._move_history(1)
            return
        if event.key() == Qt.Key.Key_Tab:
            completed = self._completion(self.text(), self.cursorPosition())
            if completed:
                text, cursor = completed
                self.setText(text)
                self.setCursorPosition(cursor)
            return
        super().keyPressEvent(event)


class _RoomSignals(QObject):
    event = Signal(dict)


class RoomTab(QWidget):
    """The DeepFlux Room tab. ``room`` is injectable for tests."""

    def __init__(self, config: DeeptorrentConfig,
                 parent: Optional[QWidget] = None,
                 room: Optional[RoomController] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._signals = _RoomSignals()
        self._signals.event.connect(self._on_event)
        # CAUTION: MainWindow passes ``parent`` POSITIONALLY
        # (RoomTab(config, self)) — keep it second or startup breaks.
        self._state = IRCState()
        self._state.ensure_network(ROOM_NET_ID, host="DeepFlux Room", port=0,
                                   tls=False)
        self._state.ensure_channel(ROOM_NET_ID, ROOM_CHANNEL)
        self._room = room or RoomController(config.chat, self._state,
                                            secret=shared_room_secret())
        self._room.add_listener(self._signals.event.emit)

        # Transcript placeholder tracking (avoids serializing the whole
        # QTextDocument per appended line) and incremental search counting.
        self._chat_empty = True
        self._search_count = 0

        self._build_ui()
        self._render_buffer()
        self._refresh_nicks()
        self._topic_label.setText(self._state.topic_of(ROOM_NET_ID, "#lounge")
                                  or self._room_topic())
        self._update_room_bar()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Join bar. ResponsiveRow + OverflowRow: the row shrinks gracefully
        # on narrow windows instead of locking the window wide.
        self._join_bar_w = ResponsiveRow()
        bar = QHBoxLayout(self._join_bar_w)
        room_label = QLabel("DeepFlux Room")
        room_label.setStyleSheet("color:#a8edff;font-weight:600")
        bar.addWidget(room_label)
        self._nick_edit = QLineEdit()
        self._nick_edit.setPlaceholderText("Nickname")
        self._nick_edit.setMaximumWidth(140)
        self._nick_edit.setClearButtonEnabled(True)
        self._nick_edit.returnPressed.connect(self._on_join_clicked)
        self._nick_edit.setText(self._config.chat.nickname)
        bar.addWidget(self._nick_edit)
        self._advanced_btn = QPushButton("Host…")
        self._advanced_btn.setCheckable(True)
        self._advanced_btn.setToolTip(
            "Advanced: connect directly to a hosting peer (ip:port) instead "
            "of using automatic discovery")
        self._advanced_btn.toggled.connect(self._on_advanced_toggled)
        bar.addWidget(self._advanced_btn)
        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("ip:port — direct host")
        self._host_edit.setMaximumWidth(200)
        self._host_edit.setVisible(False)
        self._host_edit.setText(self._config.chat.manual_host)
        bar.addWidget(self._host_edit)
        self._join_btn = QPushButton("Join")
        self._join_btn.clicked.connect(self._on_join_clicked)
        bar.addWidget(self._join_btn)
        self._private_btn = QPushButton("Make private…")
        self._private_btn.setToolTip(
            "Generate a new random room key shared only with the people in "
            "the room right now — people joining later will not see this room")
        self._private_btn.clicked.connect(self._on_private_clicked)
        self._private_btn.setVisible(False)
        bar.addWidget(self._private_btn)
        bar.addStretch(1)
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color:{_MUTED_COLOR}")
        shrink_label(self._status_label)  # live status must not grow the window min
        # Right-click while hosting → copy the address others join through.
        self._status_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._status_label.customContextMenuRequested.connect(self._show_status_menu)
        bar.addWidget(self._status_label)
        root.addWidget(self._join_bar_w)

        search_bar = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search this transcript")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.returnPressed.connect(self._find_next)
        self.search_edit.textChanged.connect(self._on_search_changed)
        search_bar.addWidget(QLabel("Find:"))
        search_bar.addWidget(self.search_edit, 1)
        previous_btn = QPushButton("Previous")
        next_btn = QPushButton("Next")
        previous_btn.clicked.connect(self._find_previous)
        next_btn.clicked.connect(self._find_next)
        search_bar.addWidget(previous_btn)
        search_bar.addWidget(next_btn)
        self.search_status = QLabel("")
        self.search_status.setMinimumWidth(70)
        self.search_status.setStyleSheet(f"color:{_MUTED_COLOR}")
        search_bar.addWidget(self.search_status)
        root.addLayout(search_bar)

        # Main splitter: chat | members
        split = QSplitter(Qt.Orientation.Horizontal)
        self.chat = QTextBrowser()
        # Never navigate the transcript widget itself. Only validated HTTP(S)
        # anchors are handed to the operating system's external browser.
        self.chat.setOpenLinks(False)
        self.chat.setOpenExternalLinks(False)
        self.chat.anchorClicked.connect(self._open_external_link)
        self.chat.setStyleSheet(
            "QTextBrowser { background-color: #0a0f18; color: #cfd8e3; }")
        split.addWidget(self.chat)

        self.nicks = QListWidget()
        self.nicks.setMinimumWidth(110)
        self.nicks.setMaximumWidth(220)
        self.nicks.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.nicks.customContextMenuRequested.connect(self._show_nick_menu)
        split.addWidget(self.nicks)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([560, 140])
        root.addWidget(split, 1)

        # Topic bar
        self._topic_label = QLabel("")
        self._topic_label.setStyleSheet(f"color:{_SERVER_COLOR}")
        self._topic_label.setWordWrap(True)
        root.addWidget(self._topic_label)

        # Input
        bottom = QHBoxLayout()
        self.input = RoomInputLine(self._complete_input)
        self.input.setPlaceholderText("Message — /help for commands")
        self.input.returnPressed.connect(self._on_send)
        bottom.addWidget(self.input, 1)
        send_btn = QPushButton("Send")
        send_btn.clicked.connect(self._on_send)
        bottom.addWidget(send_btn)
        root.addLayout(bottom)

    # ------------------------------------------------------------------
    # join bar
    # ------------------------------------------------------------------

    def _room_topic(self) -> str:
        return self._state.topic_of(ROOM_NET_ID, ROOM_CHANNEL)

    def _on_advanced_toggled(self, checked: bool) -> None:
        self._host_edit.setVisible(checked)
        self._advanced_btn.setText("Host…" if not checked else "Hide")

    def _update_room_bar(self) -> None:
        joined = self._room.is_joined()
        self._join_btn.setText("Leave" if joined else "Join")
        self._nick_edit.setEnabled(not joined)
        self._private_btn.setVisible(joined)
        mode = "encrypted" if self._room.encrypted else "UNENCRYPTED (source build)"
        if joined and "private room" in (self._room_topic() or ""):
            mode = "private · " + mode
        role = self._room.role
        if joined and role == "host":
            endpoints = self._room.endpoints()
            where = f" — others join via {endpoints[0]}" if endpoints else ""
            self._status_label.setText(f"hosting{where} · {mode}")
        elif joined:
            self._status_label.setText(f"connected as {self._room.nick} · {mode}")
        elif role == "connecting":
            self._status_label.setText(f"connecting… · {mode}")
        else:
            self._status_label.setText("first one in hosts the room · " + mode)

    def _on_join_clicked(self) -> None:
        if self._room.is_joined():
            self._room.leave()
            self._update_room_bar()  # instant feedback; the state event re-syncs
            return
        nick = self._nick_edit.text().strip()
        if not nick:
            self._status_label.setText("Enter a nickname first.")
            self._nick_edit.setFocus()
            return
        if re.search(r"[\s,\r\n]", nick) or len(nick) > 24:
            self._show_local("error", "Invalid nickname (no spaces or commas, "
                                      "max 24 chars).")
            return
        manual = ""
        if self._advanced_btn.isChecked():
            manual = self._host_edit.text().strip()
        self._config.chat.nickname = nick
        self._config.chat.manual_host = manual
        self._save_config()
        self._room.join(nick, manual)
        self._update_room_bar()

    def _show_status_menu(self, pos) -> None:
        menu = QMenu(self)
        copy_action = menu.addAction("Copy room address")
        copy_action.setEnabled(self._room.role == "host"
                               and bool(self._room.endpoints()))
        chosen = menu.exec(self._status_label.mapToGlobal(pos))
        if chosen == copy_action:
            endpoints = self._room.endpoints()
            if endpoints:
                QApplication.clipboard().setText(endpoints[0])

    def _on_private_clicked(self) -> None:
        if not self._room.is_joined():
            return
        answer = QMessageBox.question(
            self, "Make the room private",
            "Generate a new random key and share it only with the people in "
            "the room right now?\n\n"
            "From that moment, people who join later will not see this room "
            "— they get a separate, empty lounge instead. Everyone currently "
            "here moves together onto the new key.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            if not self._room.make_private():
                self._show_local("error",
                                 "Could not rotate the room key — try again in "
                                 "a few seconds.")

    def _save_config(self) -> None:
        try:
            self._config.to_file(DeeptorrentConfig.default_config_path())
        except Exception:
            logger.debug("Failed to persist config", exc_info=True)

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------

    def _on_send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.remember(text)
        self.input.clear()
        if text.startswith("/"):
            parts = text.split(" ", 2)
            cmd = parts[0].lower()
            if cmd == "/clear":
                self._state.clear_buffer(ROOM_NET_ID, ROOM_CHANNEL)
                self._render_buffer()
            elif cmd == "/me":
                action = text.split(" ", 1)[1] if len(parts) > 1 else ""
                if action and self._room.is_joined():
                    if not self._room.send_message(action, action=True):
                        self._show_local("error", "Not connected — action not sent.")
                else:
                    self._show_local("error", "Join the room first.")
            elif cmd == "/help":
                self._show_local("server",
                                 "Room commands: /me action · /clear · /help")
            else:
                self._show_local("error", f"Unknown command: {cmd} — try /help")
            return
        if not self._room.is_joined():
            self._show_local("error", "Join the room first — nickname + Join above.")
            return
        if not self._room.send_message(text):
            self._show_local("error", "Not connected — message not sent.")

    def _show_local(self, kind: str, text: str) -> None:
        """Append a local-only line (errors, command notes) to the view."""
        self._append_line(kind, "", text)

    def _complete_input(self, text: str, cursor: int) -> Optional[Tuple[str, int]]:
        start = cursor
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        fragment = text[start:cursor]
        if not fragment or fragment.startswith("/"):
            return None
        nicks = list(self._state.nicks_of(ROOM_NET_ID, ROOM_CHANNEL).keys())
        matches = sorted(
            (nick for nick in nicks
             if nick.lower().startswith(fragment.lower())), key=str.lower)
        if not matches:
            return None
        replacement = matches[0]
        if len(matches) > 1:
            common = replacement
            for candidate in matches[1:]:
                length = 0
                for left, right in zip(common, candidate):
                    if left.lower() != right.lower():
                        break
                    length += 1
                common = common[:length]
            if len(common) > len(fragment):
                replacement = common
        suffix = ": " if start == 0 else " "
        result = text[:start] + replacement + suffix + text[cursor:]
        return result, start + len(replacement) + len(suffix)

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def _on_search_changed(self, query: str) -> None:
        """Full recount — runs on query edits and buffer re-renders only;
        appended lines update the count incrementally (see _append_html)."""
        if not query:
            self._search_count = 0
            self.search_status.clear()
            self.chat.setTextCursor(QTextCursor(self.chat.document()))
            return
        self._search_count = self.chat.toPlainText().lower().count(query.lower())
        self._refresh_search_status()

    def _refresh_search_status(self) -> None:
        if not self.search_edit.text():
            return
        self.search_status.setText(
            f"{self._search_count} match" + ("" if self._search_count == 1 else "es"))

    def _find_transcript(self, backwards: bool = False) -> None:
        query = self.search_edit.text()
        if not query:
            return
        flags = QTextDocument.FindFlag.FindBackward if backwards else QTextDocument.FindFlag(0)
        if not self.chat.find(query, flags):
            cursor = self.chat.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End if backwards
                                else QTextCursor.MoveOperation.Start)
            self.chat.setTextCursor(cursor)
            self.chat.find(query, flags)

    def _find_next(self) -> None:
        self._find_transcript(False)

    def _find_previous(self) -> None:
        self._find_transcript(True)

    # ------------------------------------------------------------------
    # nick list
    # ------------------------------------------------------------------

    def _show_nick_menu(self, pos) -> None:
        item = self.nicks.itemAt(pos)
        if item is None:
            return
        nick = item.data(ROLE_NICK) or ""
        menu = QMenu(self)
        copy_action = menu.addAction("Copy nickname")
        chosen = menu.exec(self.nicks.viewport().mapToGlobal(pos))
        if chosen == copy_action:
            QApplication.clipboard().setText(nick)

    def _refresh_nicks(self) -> None:
        self.nicks.clear()
        nicks = self._state.nicks_of(ROOM_NET_ID, ROOM_CHANNEL)
        for nick in sorted(nicks, key=str.lower):
            item = QListWidgetItem(nick)
            item.setData(ROLE_NICK, nick)
            self.nicks.addItem(item)

    # ------------------------------------------------------------------
    # event handling (GUI thread, via queued signal)
    # ------------------------------------------------------------------

    def _on_event(self, event: Dict[str, Any]) -> None:
        etype = event.get("type", "")
        if event.get("network") != ROOM_NET_ID:
            return
        if etype == "state":
            self._on_state_event(event)
            return
        if etype == "names":
            self._refresh_nicks()
            return
        if etype == "topic":
            self._topic_label.setText(event.get("topic", ""))
            self._update_room_bar()  # picks up the "private" status tag
            return
        kind = {
            "message": "msg", "action": "action", "notice": "notice",
            "join": "join", "part": "part",
        }.get(etype)
        if kind:
            nick = event.get("nick", "")
            text = event.get("text", "")
            if etype == "join":
                text = f"{nick} joined"
            elif etype == "part":
                text = f"{nick} left"
            self._append_line(kind, nick, text, ts=event.get("ts"),
                              highlighted=self._is_mention(nick, text, kind))
            if etype in ("join", "part"):
                self._refresh_nicks()

    def _on_state_event(self, event: Dict[str, Any]) -> None:
        state = event.get("state", "")
        self._update_room_bar()
        if state == "connected":
            # A member's welcome replays room history straight into the
            # state buffer WITHOUT emitting per-record events — re-render so
            # the transcript picks it up.
            self._render_buffer()
            self._refresh_nicks()
        elif state in ("disconnected", "error"):
            detail = event.get("detail") or state
            self._append_line("server", "", detail if detail != "disconnected"
                              else "You left the room.")
            self._refresh_nicks()

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _open_external_link(url: QUrl) -> None:
        """Open only absolute HTTP(S) transcript links outside the app."""
        if url.isValid() and url.scheme().lower() in ("http", "https") and url.host():
            QDesktopServices.openUrl(url)

    def _render_buffer(self) -> None:
        self.chat.clear()
        self._chat_empty = True
        self._search_count = 0
        msgs = [m.to_dict() for m in self._state.get_messages(
            ROOM_NET_ID, ROOM_CHANNEL, limit=500)]
        lines = [self._fmt_line(
            m["kind"], m["nick"], m["text"], m["ts"],
            highlighted=self._is_mention(m["nick"], m["text"], m["kind"]),
        ) for m in msgs]
        self.chat.setHtml("<br>".join(lines) if lines else
                          f'<span style="color:{_MUTED_COLOR}">'
                          "Nothing here yet — join the room to start chatting.</span>")
        self._chat_empty = not lines
        self._scroll_to_bottom()
        self._on_search_changed(self.search_edit.text())

    def _is_mention(self, nick: str, text: str, kind: str) -> bool:
        if kind not in ("msg", "action"):
            return False
        own = self._state.nick_of(ROOM_NET_ID)
        if not own or nick.lower() == own.lower():
            return False
        folded_own = re.escape(own.lower())
        return bool(re.search(rf"(?<![a-z0-9_]){folded_own}(?![a-z0-9_])",
                              text.lower()))

    def _is_own(self, nick: str) -> bool:
        own = self._state.nick_of(ROOM_NET_ID)
        return bool(own and nick.lower() == own.lower())

    def _fmt_line(self, kind: str, nick: str, text: str, ts: float,
                  highlighted: bool = False) -> str:
        stamp = time.strftime("%H:%M", time.localtime(ts))
        body = _linkify(html.escape(text))
        if kind == "msg":
            who = html.escape(nick)
            color = _SELF_COLOR if self._is_own(nick) else _nick_color(nick)
            line = (f'<span style="color:{_MUTED_COLOR}">[{stamp}]</span> '
                    f'<span style="color:{color}">&lt;{who}&gt;</span> {body}')
        elif kind == "action":
            line = (f'<span style="color:{_MUTED_COLOR}">[{stamp}]</span> '
                    f'<span style="color:{_ACTION_COLOR}">* {html.escape(nick)}</span> {body}')
        elif kind == "notice":
            line = (f'<span style="color:{_MUTED_COLOR}">[{stamp}]</span> '
                    f'<span style="color:{_NOTICE_COLOR}">-{html.escape(nick)}-</span> '
                    f'<span style="color:{_NOTICE_COLOR}">{body}</span>')
        elif kind == "error":
            line = (f'<span style="color:{_MUTED_COLOR}">[{stamp}]</span> '
                    f'<span style="color:{_ERROR_COLOR}">Warning: {body}</span>')
        else:
            # server / join / part
            line = (f'<span style="color:{_MUTED_COLOR}">[{stamp}]</span> '
                    f'<span style="color:{_SERVER_COLOR}">— {body}</span>')
        if highlighted:
            return f'<span style="background-color:#3a3018;font-weight:600">{line}</span>'
        return line

    def _append_line(self, kind: str, nick: str, text: str,
                     highlighted: bool = False,
                     ts: Optional[float] = None) -> None:
        line_html = self._fmt_line(kind, nick, text, float(ts or time.time()),
                                   highlighted=highlighted)
        self._append_html(line_html, plain_text=text)

    def _append_html(self, line_html: str, plain_text: str = "") -> None:
        # Track the placeholder with a flag: serializing the whole document
        # (chat.toHtml) per appended line made busy rooms O(n²).
        if self._chat_empty:
            self.chat.clear()
            self._chat_empty = False
        self.chat.append(line_html)
        self._scroll_to_bottom()
        # Incremental search-match counting: the new line only.
        query = self.search_edit.text()
        if query and plain_text:
            self._search_count += plain_text.lower().count(query.lower())
            self._refresh_search_status()

    def _scroll_to_bottom(self) -> None:
        cursor = self.chat.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.chat.setTextCursor(cursor)

    # ------------------------------------------------------------------
    # shutdown (called from MainWindow.closeEvent)
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        # Graceful exit first: it withdraws the discovery pointer and tells
        # members the host is going away (room continuity).
        try:
            self._room.shutdown()
        except Exception:
            logger.debug("room shutdown failed", exc_info=True)
