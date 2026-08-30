"""IRC tab widget for DeepFlux.

Layout:
    +------------------------------------------------------------------+
    | toolbar: [network▾] [Connect] [Disconnect] | #channel [Join]      |
    +----------+-----------------------------------+--------------------+
    | tree     | chat view (HTML)                  | nick list          |
    | networks |                                   |                    |
    |  > chans |                                   |                    |
    +----------+-----------------------------------+--------------------+
    | topic: ...                                                       |
    | [input line............................................] [Send]  |
    +------------------------------------------------------------------+

All IRC work happens in :class:`ircmgr.client.IRCClientCore` on its own
daemon thread; events arrive here through a single Qt signal (queued
cross-thread delivery) so no widget is ever touched off the GUI thread.
"""
from __future__ import annotations

import html
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import DeeptorrentConfig, IRCNetworkConfig
from ircmgr.client import IRCClientCore

logger = logging.getLogger(__name__)

ROLE_NET = Qt.ItemDataRole.UserRole
ROLE_CHAN = Qt.ItemDataRole.UserRole + 1

# Nick-color palette tuned for the app's dark theme.
_NICK_COLORS = [
    "#ff6e6e", "#ffa94d", "#ffd43b", "#8ce99a", "#63e6be", "#4dd2ff",
    "#74c0fc", "#b197fc", "#f783ac", "#e9c46a", "#90e0ef", "#a9e34b",
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
    return _LINK_RE.sub(r'<a href="\1" style="color:#4dd2ff">\1</a>', escaped_text)


class _IRCSignals(QObject):
    event = Signal(dict)


class NetworkDialog(QDialog):
    """Add/edit a network entry."""

    def __init__(self, parent: Optional[QWidget] = None,
                 net: Optional[IRCNetworkConfig] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("IRC Network")
        self.setModal(True)
        form = QFormLayout(self)
        self.host_edit = QLineEdit(net.host if net else "")
        self.host_edit.setPlaceholderText("irc.libera.chat")
        self.port_edit = QLineEdit(str(net.port if net else 6697))
        self.tls_check = QCheckBox("Use TLS")
        self.tls_check.setChecked(net.tls if net else True)
        self.nick_edit = QLineEdit(net.nick if net else "")
        self.nick_edit.setPlaceholderText("DeepFluxUser")
        self.realname_edit = QLineEdit(net.realname if net else "DeepFlux")
        self.password_edit = QLineEdit(net.password if net else "")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.sasl_account_edit = QLineEdit(net.sasl_account if net else "")
        self.sasl_password_edit = QLineEdit(net.sasl_password if net else "")
        self.sasl_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.channels_edit = QLineEdit(", ".join(net.channels) if net else "")
        self.channels_edit.setPlaceholderText("#channel1, #channel2")

        form.addRow("Server", self.host_edit)
        form.addRow("Port", self.port_edit)
        form.addRow("", self.tls_check)
        form.addRow("Nickname", self.nick_edit)
        form.addRow("Real name", self.realname_edit)
        form.addRow("Server password", self.password_edit)
        form.addRow("SASL account", self.sasl_account_edit)
        form.addRow("SASL password", self.sasl_password_edit)
        form.addRow("Channels", self.channels_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:  # basic validation
        if not self.host_edit.text().strip():
            self.host_edit.setFocus()
            return
        super().accept()

    def to_config(self, existing: Optional[IRCNetworkConfig] = None) -> IRCNetworkConfig:
        host = self.host_edit.text().strip()
        try:
            port = int(self.port_edit.text().strip())
        except ValueError:
            port = 6697 if self.tls_check.isChecked() else 6667
        channels = [c.strip() for c in self.channels_edit.text().split(",") if c.strip()]
        channels = [c if c.startswith(("#", "&", "+", "!")) else "#" + c for c in channels]
        net = existing or IRCNetworkConfig()
        net.id = net.id or re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
        net.host = host
        net.port = port
        net.tls = self.tls_check.isChecked()
        net.nick = self.nick_edit.text().strip() or "DeepFluxUser"
        net.username = net.nick
        net.realname = self.realname_edit.text().strip() or "DeepFlux"
        net.password = self.password_edit.text()
        net.sasl_account = self.sasl_account_edit.text().strip()
        net.sasl_password = self.sasl_password_edit.text()
        net.channels = channels
        return net


class IRCTab(QWidget):
    """The IRC client tab. Shares its IRCClientCore with the agent tools."""

    def __init__(self, config: DeeptorrentConfig, irc_client: IRCClientCore,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._client = irc_client
        self._signals = _IRCSignals()
        self._signals.event.connect(self._on_event)
        self._client.add_listener(self._signals.event.emit)

        self._current: Optional[Tuple[str, Optional[str]]] = None  # (net_id, channel|None)
        self._unread: set = set()
        # Auto-select the first channel we join until the user clicks the tree.
        self._user_picked = False
        self._auto_selecting = False

        self._build_ui()
        self._reload_network_combo()
        # Start disconnected — the user connects manually from the toolbar.
        for net in self._config.irc.networks:
            self._add_network_tree_item(net.id, net.host)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Toolbar
        bar = QHBoxLayout()
        self.network_combo = QComboBox()
        self.network_combo.setMinimumWidth(220)
        bar.addWidget(self.network_combo)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        bar.addWidget(self.connect_btn)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        bar.addWidget(self.disconnect_btn)
        self.manage_btn = QPushButton("Networks…")
        self.manage_btn.clicked.connect(self._on_manage_networks)
        bar.addWidget(self.manage_btn)
        bar.addSpacing(16)
        self.join_edit = QLineEdit()
        self.join_edit.setPlaceholderText("#channel")
        self.join_edit.setMaximumWidth(180)
        bar.addWidget(self.join_edit)
        join_btn = QPushButton("Join")
        join_btn.clicked.connect(self._on_join_clicked)
        bar.addWidget(join_btn)
        bar.addStretch(1)
        self.status_label = QLabel("offline")
        self.status_label.setStyleSheet(f"color:{_MUTED_COLOR}")
        bar.addWidget(self.status_label)
        root.addLayout(bar)

        # Main splitter: tree | chat | nicks
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(150)
        self.tree.setMaximumWidth(280)
        self.tree.currentItemChanged.connect(self._on_tree_selection)
        split.addWidget(self.tree)

        self.chat = QTextBrowser()
        self.chat.setOpenLinks(True)
        self.chat.setStyleSheet(
            "QTextBrowser { background-color: #0a0f18; color: #cfd8e3; }")
        split.addWidget(self.chat)

        self.nicks = QListWidget()
        self.nicks.setMinimumWidth(110)
        self.nicks.setMaximumWidth(220)
        split.addWidget(self.nicks)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        root.addWidget(split, 1)

        # Topic bar
        self.topic_label = QLabel("")
        self.topic_label.setStyleSheet(f"color:{_SERVER_COLOR}")
        self.topic_label.setWordWrap(True)
        root.addWidget(self.topic_label)

        # Input
        bottom = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Message — /join /part /msg /me /nick /list /raw …")
        self.input.returnPressed.connect(self._on_send)
        bottom.addWidget(self.input, 1)
        send_btn = QPushButton("Send")
        send_btn.clicked.connect(self._on_send)
        bottom.addWidget(send_btn)
        root.addLayout(bottom)

    # ------------------------------------------------------------------
    # network combo / tree bookkeeping
    # ------------------------------------------------------------------

    def _reload_network_combo(self) -> None:
        self.network_combo.blockSignals(True)
        self.network_combo.clear()
        for net in self._config.irc.networks:
            label = f"{net.host}:{net.port}" + ("" if net.tls else " (plain)")
            self.network_combo.addItem(label, userData=net.id)
        self.network_combo.blockSignals(False)

    def _add_network_tree_item(self, net_id: str, host: str) -> QTreeWidgetItem:
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, ROLE_NET) == net_id:
                return item
        item = QTreeWidgetItem([f"◌ {host or net_id}"])
        item.setData(0, ROLE_NET, net_id)
        item.setData(0, ROLE_CHAN, None)
        f = item.font(0)
        f.setBold(True)
        item.setFont(0, f)
        self.tree.addTopLevelItem(item)
        item.setExpanded(True)
        return item

    def _network_item(self, net_id: str) -> Optional[QTreeWidgetItem]:
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, ROLE_NET) == net_id:
                return item
        return None

    def _channel_item(self, net_id: str, channel: str) -> Optional[QTreeWidgetItem]:
        parent = self._network_item(net_id)
        if not parent:
            return None
        for i in range(parent.childCount()):
            item = parent.child(i)
            if item.data(0, ROLE_CHAN) == channel:
                return item
        return None

    def _ensure_channel_item(self, net_id: str, channel: str) -> Optional[QTreeWidgetItem]:
        item = self._channel_item(net_id, channel)
        if item:
            return item
        parent = self._network_item(net_id)
        if not parent:
            return None
        item = QTreeWidgetItem([channel])
        item.setData(0, ROLE_NET, net_id)
        item.setData(0, ROLE_CHAN, channel)
        parent.addChild(item)
        parent.setExpanded(True)
        return item

    # ------------------------------------------------------------------
    # toolbar actions
    # ------------------------------------------------------------------

    def _selected_network_id(self) -> Optional[str]:
        idx = self.network_combo.currentIndex()
        if idx < 0:
            return None
        return self.network_combo.itemData(idx)

    def _on_connect_clicked(self) -> None:
        net_id = self._selected_network_id()
        if net_id is None:
            self._on_manage_networks()  # no networks yet → open the dialog
            return
        net = self._find_net_cfg(net_id)
        if net:
            self._client.connect_network(net)

    def _on_disconnect_clicked(self) -> None:
        net_id = self._selected_network_id()
        if net_id:
            self._client.disconnect_network(net_id)

    def _on_manage_networks(self) -> None:
        existing = self._find_net_cfg(self._selected_network_id() or "")
        dlg = NetworkDialog(self, net=existing)
        if not dlg.exec():
            return
        net = dlg.to_config(existing)
        if existing is None:
            self._config.irc.networks.append(net)
            self._add_network_tree_item(net.id, net.host)
        else:
            item = self._network_item(net.id)
            if item:
                item.setText(0, f"◌ {net.host}")
        self._reload_network_combo()
        self._save_config()
        self._client.connect_network(net)

    def _on_join_clicked(self) -> None:
        net_id = self._selected_network_id()
        channel = self.join_edit.text().strip()
        if net_id and channel:
            self._client.join(net_id, channel)
            self.join_edit.clear()

    def _find_net_cfg(self, net_id: str) -> Optional[IRCNetworkConfig]:
        for net in self._config.irc.networks:
            if net.id == net_id:
                return net
        return None

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
        self.input.clear()
        net_id = self._current[0] if self._current else self._selected_network_id()
        if not net_id:
            self._append_html(None, self._fmt_error("No network selected."))
            return

        if text.startswith("/"):
            if self._handle_command(net_id, text):
                return
        # Plain text → current channel
        channel = self._current[1] if self._current else None
        if not channel:
            self._append_html(None, self._fmt_error("Select a channel first (or use /join)."))
            return
        if not self._is_connected(net_id):
            self._append_html(None, self._fmt_error("Not connected — message not sent."))
            return
        self._client.send_message(net_id, channel, text)
        nick = self._client.state.nick_of(net_id)
        self._append_line(net_id, channel, "msg", nick, text)

    def _is_connected(self, net_id: str) -> bool:
        for n in self._client.status().get("networks", []):
            if n["id"] == net_id:
                return bool(n["connected"])
        return False

    def _handle_command(self, net_id: str, text: str) -> bool:
        parts = text.split(" ", 2)
        cmd = parts[0].lower()
        if cmd == "/join" and len(parts) > 1:
            self._client.join(net_id, parts[1].strip())
        elif cmd == "/part":
            channel = parts[1].strip() if len(parts) > 1 else (self._current[1] if self._current else "")
            if channel:
                self._client.part(net_id, channel)
        elif cmd == "/msg" and len(parts) > 2:
            target, msg = parts[1].strip(), parts[2]
            if not self._is_connected(net_id):
                self._append_html(None, self._fmt_error("Not connected — message not sent."))
                return True
            self._client.send_message(net_id, target, msg)
            nick = self._client.state.nick_of(net_id)
            self._ensure_channel_item(net_id, target)
            self._append_line(net_id, target, "msg", nick, f"→ {target}: {msg}")
        elif cmd == "/me" and len(parts) > 1:
            channel = self._current[1] if self._current else ""
            if not channel:
                self._append_html(None, self._fmt_error("/me needs a channel."))
                return True
            if not self._is_connected(net_id):
                self._append_html(None, self._fmt_error("Not connected — action not sent."))
                return True
            action = text.split(" ", 1)[1]
            self._client.send_action(net_id, channel, action)
            nick = self._client.state.nick_of(net_id)
            self._append_line(net_id, channel, "action", nick, action)
        elif cmd == "/nick" and len(parts) > 1:
            self._client.change_nick(net_id, parts[1].strip())
        elif cmd == "/list":
            if not self._is_connected(net_id):
                self._append_html(None, self._fmt_error("Not connected."))
                return True
            # Optional filter: /list #foo* — results land in the server view.
            arg = text.split(" ", 1)[1].strip() if len(parts) > 1 else ""
            self._client.send_raw(net_id, "LIST " + arg if arg else "LIST")
            self._append_html(None, self._fmt_server("Requesting channel list… "
                                                     "(results appear in the network view)"))
        elif cmd == "/raw" and len(parts) > 1:
            self._client.send_raw(net_id, text.split(" ", 1)[1])
        elif cmd == "/quit":
            self._client.disconnect_network(net_id)
        else:
            self._append_html(None, self._fmt_error(f"Unknown command: {cmd}"))
        return True

    # ------------------------------------------------------------------
    # selection / rendering
    # ------------------------------------------------------------------

    def _on_tree_selection(self, current: Optional[QTreeWidgetItem], _previous) -> None:
        if current is None:
            return
        if not self._auto_selecting:
            self._user_picked = True
        net_id = current.data(0, ROLE_NET)
        channel = current.data(0, ROLE_CHAN)
        self._current = (net_id, channel)
        self._unread.discard((net_id, channel))
        self._style_channel_item(net_id, channel, unread=False)
        self._render_buffer(net_id, channel)
        self._refresh_nicks(net_id, channel)
        topic = self._client.state.topic_of(net_id, channel) if channel else ""
        self.topic_label.setText(topic)
        idx = self.network_combo.findData(net_id)
        if idx >= 0:
            self.network_combo.setCurrentIndex(idx)

    def _render_buffer(self, net_id: str, channel: Optional[str]) -> None:
        self.chat.clear()
        msgs = self._client.get_messages(net_id, channel, limit=self._config.irc.buffer_lines)
        lines = [self._fmt_line(m["kind"], m["nick"], m["text"], m["ts"]) for m in msgs]
        if channel is None:
            lines.extend(self._fmt_chanlist(net_id))
        self.chat.setHtml("<br>".join(lines) if lines else
                          f'<span style="color:{_MUTED_COLOR}">Nothing here yet.</span>')
        self._scroll_to_bottom()

    _CHANLIST_DISPLAY_CAP = 500

    def _fmt_chanlist(self, net_id: str) -> List[str]:
        rows = self._client.state.chanlist_of(net_id)
        if not rows:
            return []
        ts = self._client.state.chanlist_ts(net_id)
        stamp = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
        out = [f'<span style="color:{_SERVER_COLOR}">— Channel list ({len(rows)} channels, '
               f'{stamp}) —</span>']
        for r in rows[: self._CHANLIST_DISPLAY_CAP]:
            chan = html.escape(r["channel"])
            topic = html.escape(r["topic"])
            out.append(
                f'<span style="color:{_NOTICE_COLOR}">{chan}</span> '
                f'<span style="color:{_MUTED_COLOR}">({r["users"]})</span> '
                f'<span style="color:{_SERVER_COLOR}">{topic}</span>')
        if len(rows) > self._CHANLIST_DISPLAY_CAP:
            out.append(f'<span style="color:{_MUTED_COLOR}">… and '
                       f'{len(rows) - self._CHANLIST_DISPLAY_CAP} more</span>')
        return out

    def _refresh_nicks(self, net_id: str, channel: Optional[str]) -> None:
        self.nicks.clear()
        if not channel:
            return
        nicks = self._client.state.nicks_of(net_id, channel)
        for nick in sorted(nicks, key=str.lower):
            prefix = nicks[nick]
            item_text = f"{prefix}{nick}" if prefix and prefix != "" else nick
            self.nicks.addItem(item_text)

    # ------------------------------------------------------------------
    # event handling (GUI thread, via queued signal)
    # ------------------------------------------------------------------

    def _on_event(self, event: Dict[str, Any]) -> None:
        etype = event.get("type", "")
        net_id = event.get("network", "")
        if etype == "state":
            self._on_state_event(net_id, event)
            return
        if etype == "names":
            if self._current == (net_id, event.get("channel")):
                self._refresh_nicks(net_id, event.get("channel"))
            return
        if etype == "chanlist":
            if self._current == (net_id, None):
                self._render_buffer(net_id, None)
            else:
                self._unread.add((net_id, None))
            return
        if etype == "topic":
            channel = event.get("channel", "")
            self._ensure_channel_item(net_id, channel)
            if self._current == (net_id, channel):
                self.topic_label.setText(event.get("topic", ""))
            self._maybe_show(net_id, channel, "topic", "", event.get("topic", ""))
            return
        if etype == "parted":
            item = self._channel_item(net_id, event.get("channel", ""))
            if item:
                parent = item.parent()
                if parent:
                    parent.removeChild(item)
            if self._current == (net_id, event.get("channel")):
                self.nicks.clear()
                self.topic_label.setText("")
            return

        channel = event.get("channel")
        if channel:
            self._ensure_channel_item(net_id, channel)
        kind = {
            "message": "msg", "action": "action", "notice": "notice",
            "join": "join", "part": "part", "quit": "quit", "kick": "kick",
        }.get(etype)
        if kind:
            nick = event.get("nick", "")
            text = event.get("text", "")
            if etype == "join":
                text = f"{nick} joined"
            elif etype == "part":
                text = f"{nick} left"
            self._maybe_show(net_id, channel, kind, nick, text)
            if etype in ("join", "part", "kick", "quit") and self._current == (net_id, channel):
                self._refresh_nicks(net_id, channel)
            # First time we land in a channel, focus it so the user opens the
            # tab on the channel instead of the bare server window. Once the
            # user clicks the tree themselves, we never steal focus again.
            if etype == "join" and event.get("own") and not self._user_picked:
                item = self._channel_item(net_id, channel)
                if item:
                    self._auto_selecting = True
                    self.tree.setCurrentItem(item)
                    self._auto_selecting = False

    def _maybe_show(self, net_id: str, channel: Optional[str], kind: str,
                    nick: str, text: str) -> None:
        if self._current == (net_id, channel):
            self._append_line(net_id, channel, kind, nick, text)
        else:
            self._unread.add((net_id, channel))
            self._style_channel_item(net_id, channel, unread=True)

    def _style_channel_item(self, net_id: str, channel: Optional[str], unread: bool) -> None:
        if channel is None:
            return
        item = self._channel_item(net_id, channel)
        if not item:
            return
        f = item.font(0)
        f.setBold(unread)
        item.setFont(0, f)
        item.setForeground(0, QColor("#2a6aaf" if unread else "#cfd8e3"))

    def _on_state_event(self, net_id: str, event: Dict[str, Any]) -> None:
        state = event.get("state", "")
        item = self._network_item(net_id)
        net = self._find_net_cfg(net_id)
        host = net.host if net else net_id
        if item:
            dot = {"connected": "●", "connecting": "◌", "disconnected": "○",
                   "error": "✕"}.get(state, "○")
            color = {"connected": "#8ce99a", "connecting": "#ffd43b",
                     "disconnected": "#5b6b7c", "error": "#ff6e6e"}.get(state, "#5b6b7c")
            item.setText(0, f"{dot} {host}")
            item.setForeground(0, QColor(color))
        if net_id == self._selected_network_id() or (self._current and self._current[0] == net_id):
            label = {"connected": f"connected as {event.get('nick', '')}",
                     "connecting": "connecting…", "disconnected": "offline",
                     "error": f"error: {event.get('detail', '')}"}.get(state, state)
            self.status_label.setText(label)
        if state == "connected":
            # Channelless networks auto-request /LIST on connect — auto-select
            # the server node so the channel list is visible when it arrives.
            # Once the user clicks the tree themselves, we never steal focus.
            net = self._find_net_cfg(net_id)
            if net and not net.channels and not self._user_picked:
                item = self._network_item(net_id)
                if item:
                    self._auto_selecting = True
                    self.tree.setCurrentItem(item)
                    self._auto_selecting = False
        if state in ("disconnected", "error"):
            self._append_line(net_id, None, "server", "",
                              event.get("detail") or state)
            if self._current is None or self._current[0] != net_id:
                self._unread.add((net_id, None))

    # ------------------------------------------------------------------
    # formatting
    # ------------------------------------------------------------------

    def _fmt_line(self, kind: str, nick: str, text: str, ts: float) -> str:
        stamp = time.strftime("%H:%M", time.localtime(ts))
        body = _linkify(html.escape(text))
        if kind == "msg":
            who = html.escape(nick)
            color = _SELF_COLOR if self._is_own(nick) else _nick_color(nick)
            return (f'<span style="color:{_MUTED_COLOR}">[{stamp}]</span> '
                    f'<span style="color:{color}">&lt;{who}&gt;</span> {body}')
        if kind == "action":
            color = _SELF_COLOR if self._is_own(nick) else _nick_color(nick)
            return (f'<span style="color:{_MUTED_COLOR}">[{stamp}]</span> '
                    f'<span style="color:{_ACTION_COLOR}">* {html.escape(nick)}</span> {body}')
        if kind == "notice":
            return (f'<span style="color:{_MUTED_COLOR}">[{stamp}]</span> '
                    f'<span style="color:{_NOTICE_COLOR}">-{html.escape(nick)}-</span> '
                    f'<span style="color:{_NOTICE_COLOR}">{body}</span>')
        if kind == "error":
            return (f'<span style="color:{_MUTED_COLOR}">[{stamp}]</span> '
                    f'<span style="color:{_ERROR_COLOR}">⚠ {body}</span>')
        # server / join / part / quit / nick / topic / kick
        return (f'<span style="color:{_MUTED_COLOR}">[{stamp}]</span> '
                f'<span style="color:{_SERVER_COLOR}">— {body}</span>')

    def _fmt_error(self, text: str) -> str:
        return self._fmt_line("error", "", text, time.time())

    def _fmt_server(self, text: str) -> str:
        return self._fmt_line("server", "", text, time.time())

    def _is_own(self, nick: str) -> bool:
        if not self._current:
            return False
        return nick == self._client.state.nick_of(self._current[0])

    def _append_line(self, net_id: str, channel: Optional[str], kind: str,
                     nick: str, text: str) -> None:
        self._append_html(None, self._fmt_line(kind, nick, text, time.time()))

    def _append_html(self, _unused, line_html: str) -> None:
        if "Nothing here yet" in self.chat.toHtml():
            self.chat.clear()
        self.chat.append(line_html)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        cursor = self.chat.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.chat.setTextCursor(cursor)

    # ------------------------------------------------------------------
    # shutdown (called from MainWindow.closeEvent)
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        # Persist channel lists per network (joined channels survive restarts).
        try:
            snap = self._client.status()
            for net in self._config.irc.networks:
                for nsnap in snap.get("networks", []):
                    if nsnap["id"] == net.id and nsnap["connected"]:
                        joined = [c["name"] for c in nsnap["channels"]
                                  if c["name"].startswith(("#", "&", "+", "!"))]
                        if joined:
                            net.channels = joined
            self._save_config()
        except Exception:
            logger.debug("IRC shutdown persist failed", exc_info=True)
