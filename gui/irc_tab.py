"""IRC tab widget for DeepFlux.

Layout:
    +------------------------------------------------------------------+
    | toolbar: [network▾] [nick] [Connect] [Disconnect] [Connect All]   |
    |          [Disconnect All] [Networks…] [Privacy…] | #chan [Join]  |
    +----------+-----------------------------------+--------------------+
    | tree     | chat view (HTML)                  | nick list          |
    | networks |                                   | (rank-sorted,      |
    |  > chans |                                   |  away-marked)      |
    +----------+-----------------------------------+--------------------+
    | channel directory (server view): filter + joinable /LIST table   |
    | topic: ...                                                       |
    | [input line............................................] [Send]  |
    +------------------------------------------------------------------+

The toolbar combo carries a live status dot per network (● connected,
◌ connecting, ✕ error) and follows the tree selection; Join acts on the
network of the channel being viewed. The nick field renames on the selected
network (live when connected, saved for the next connect otherwise).

All IRC work happens in :class:`ircmgr.client.IRCClientCore` on its own
daemon thread; events arrive here through a single Qt signal (queued
cross-thread delivery) so no widget is ever touched off the GUI thread.
"""
from __future__ import annotations

import html
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QObject, QPoint, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import DeeptorrentConfig, IRCNetworkConfig, shared_room_secret
from ircmgr.client import IRCClientCore
from ircmgr.room import ROOM_CHANNEL, RoomController
from ircmgr.state import CHANNEL_PREFIXES, ROOM_NET_ID, irc_casefold
from gui.responsive import OverflowRow, ResponsiveRow, shrink_label
from gui.window_sizing import roomy

logger = logging.getLogger(__name__)

ROLE_NET = Qt.ItemDataRole.UserRole
ROLE_CHAN = Qt.ItemDataRole.UserRole + 1
ROLE_BASE_LABEL = Qt.ItemDataRole.UserRole + 2
ROLE_NICK = Qt.ItemDataRole.UserRole + 3

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


def _unique_network_id(host: str, existing_ids: List[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "network"
    used = set(existing_ids)
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"


def _dedupe_network_ids(networks: List[IRCNetworkConfig]) -> None:
    used: List[str] = []
    for net in networks:
        candidate = net.id or re.sub(r"[^a-z0-9]+", "-", net.host.lower()).strip("-")
        if not candidate or candidate in used:
            candidate = _unique_network_id(net.host, used)
        net.id = candidate
        used.append(candidate)


class IRCInputLine(QLineEdit):
    """One-session input history and owner-provided IRC completion."""

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
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(net.port) if net else 6697)
        self.tls_check = QCheckBox("Use TLS")
        self.tls_check.setChecked(net.tls if net else True)
        # Standard ports follow the TLS toggle; a custom port (7000, 7021, …)
        # is left exactly as the user set it.
        self.tls_check.toggled.connect(self._sync_port_for_tls)
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
        form.addRow("Port", self.port_spin)
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

    def _sync_port_for_tls(self, checked: bool) -> None:
        if self.port_spin.value() in (6667, 6697):
            self.port_spin.setValue(6697 if checked else 6667)

    def accept(self) -> None:  # basic validation
        if not self.host_edit.text().strip():
            self.host_edit.setFocus()
            return
        sasl_account = bool(self.sasl_account_edit.text().strip())
        sasl_password = bool(self.sasl_password_edit.text())
        if sasl_account != sasl_password:
            missing = "password" if sasl_account else "account"
            QMessageBox.warning(
                self, "Invalid SASL configuration",
                f"Enter a SASL {missing}, or clear both SASL fields. "
                "The server password is separate and is never used for SASL.",
            )
            (self.sasl_password_edit if sasl_account else self.sasl_account_edit).setFocus()
            return
        super().accept()

    def to_config(self, existing: Optional[IRCNetworkConfig] = None) -> IRCNetworkConfig:
        host = self.host_edit.text().strip()
        channels = [c.strip() for c in self.channels_edit.text().split(",") if c.strip()]
        channels = [c if c.startswith(CHANNEL_PREFIXES) else "#" + c for c in channels]
        net = existing or IRCNetworkConfig()
        net.id = net.id or re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
        net.host = host
        net.port = self.port_spin.value()
        net.tls = self.tls_check.isChecked()
        net.nick = self.nick_edit.text().strip() or "DeepFluxUser"
        net.username = net.nick
        net.realname = self.realname_edit.text().strip() or "DeepFlux"
        net.password = self.password_edit.text()
        net.sasl_account = self.sasl_account_edit.text().strip()
        net.sasl_password = self.sasl_password_edit.text()
        net.channels = channels
        return net


class IRCSettingsDialog(QDialog):
    """Opt-in encrypted transcript settings and privacy actions."""

    def __init__(self, irc_config, client: IRCClientCore,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("IRC History & Privacy")
        self.setModal(True)
        self._config = irc_config
        self._client = client
        layout = QVBoxLayout(self)

        privacy = QLabel(
            "Persistent IRC history is off by default. When enabled, targets, "
            "nicknames, message bodies, and account/away metadata are encrypted "
            "with a local device key. Network IDs and timestamps remain visible "
            "for bounded lookup. Private messages require a separate opt-in.")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)

        self.history_check = QCheckBox("Store encrypted IRC history on this device")
        self.history_check.setChecked(bool(irc_config.history_enabled))
        layout.addWidget(self.history_check)
        self.private_check = QCheckBox("Include private messages in encrypted history")
        self.private_check.setChecked(bool(irc_config.history_private_messages))
        self.private_check.setEnabled(self.history_check.isChecked())
        self.history_check.toggled.connect(self.private_check.setEnabled)
        layout.addWidget(self.private_check)

        retention_row = QHBoxLayout()
        retention_row.addWidget(QLabel("Delete history older than"))
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(1, 3650)
        self.retention_spin.setValue(int(irc_config.history_retention_days))
        self.retention_spin.setSuffix(" days")
        retention_row.addWidget(self.retention_spin)
        retention_row.addStretch(1)
        layout.addLayout(retention_row)

        clear_btn = QPushButton("Clear Stored History…")
        clear_btn.clicked.connect(self._clear_history)
        layout.addWidget(clear_btn)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        roomy(self)

    def _clear_history(self) -> None:
        answer = QMessageBox.question(
            self, "Clear IRC history",
            "Permanently delete all stored IRC transcripts from this device?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            try:
                self._client.clear_history()
            except Exception:
                logger.warning("Failed to clear encrypted IRC history", exc_info=True)
                QMessageBox.warning(self, "IRC history", "Stored history could not be cleared.")
            else:
                QMessageBox.information(self, "IRC history", "Stored IRC history was cleared.")

    def accept(self) -> None:
        self._config.history_enabled = self.history_check.isChecked()
        self._config.history_private_messages = (
            self.private_check.isChecked() and self.history_check.isChecked())
        self._config.history_retention_days = self.retention_spin.value()
        try:
            self._client.configure_history()
        except Exception:
            logger.warning("Failed to apply encrypted IRC history settings", exc_info=True)
            QMessageBox.warning(
                self, "IRC history", "History settings could not be applied on this device.")
            return
        super().accept()


class NetworkManagerDialog(QDialog):
    """Small CRUD surface for configured IRC networks."""

    def __init__(self, networks: List[IRCNetworkConfig],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("IRC Networks")
        self.setModal(True)
        self._networks = networks
        self.changed = False
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self._edit())
        layout.addWidget(self.list)
        buttons = QHBoxLayout()
        add_btn = QPushButton("Add…")
        edit_btn = QPushButton("Edit…")
        delete_btn = QPushButton("Delete")
        add_btn.clicked.connect(self._add)
        edit_btn.clicked.connect(self._edit)
        delete_btn.clicked.connect(self._delete)
        buttons.addWidget(add_btn)
        buttons.addWidget(edit_btn)
        buttons.addWidget(delete_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.reject)
        layout.addWidget(close_buttons)
        roomy(self)
        self._refresh()

    def _refresh(self, selected_id: str = "") -> None:
        self.list.clear()
        for net in self._networks:
            label = f"{net.host}:{net.port}" + ("" if net.tls else " (plain)")
            self.list.addItem(label)
            item = self.list.item(self.list.count() - 1)
            item.setData(ROLE_NET, net.id)
            if net.id == selected_id:
                self.list.setCurrentItem(item)
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)

    def _selected(self) -> Optional[IRCNetworkConfig]:
        item = self.list.currentItem()
        if item:
            net_id = item.data(ROLE_NET)
            return next((net for net in self._networks if net.id == net_id), None)
        return None

    def _add(self) -> None:
        dialog = NetworkDialog(self)
        if not dialog.exec():
            return
        net = dialog.to_config()
        net.id = _unique_network_id(net.host, [known.id for known in self._networks])
        self._networks.append(net)
        self.changed = True
        self._refresh(net.id)

    def _edit(self) -> None:
        net = self._selected()
        if net is None:
            return
        dialog = NetworkDialog(self, net)
        if not dialog.exec():
            return
        dialog.to_config(net)  # IDs are stable across edits.
        self.changed = True
        self._refresh(net.id)

    def _delete(self) -> None:
        net = self._selected()
        if net is None:
            return
        answer = QMessageBox.question(
            self, "Delete IRC network",
            f"Delete {net.host}:{net.port}? Encrypted history, if enabled, is not deleted; "
            "use Privacy → Clear Stored History to remove it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._networks.remove(net)
        self.changed = True
        self._refresh()


class IRCTab(QWidget):
    """The IRC client tab. Shares its IRCClientCore with the agent tools.

    Also hosts the DeepFlux Room (ircmgr/room.py) — a serverless community
    chat rendered as a pseudo-network pinned at the top of the tree, sharing
    this page's chat view, nick list and input. ``room`` is injectable for
    tests."""

    def __init__(self, config: DeeptorrentConfig, irc_client: IRCClientCore,
                 room: Optional[RoomController] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        _dedupe_network_ids(self._config.irc.networks)
        self._client = irc_client
        self._signals = _IRCSignals()
        self._signals.event.connect(self._on_event)
        self._client.add_listener(self._signals.event.emit)
        self._room = room or RoomController(config.chat, self._client.state,
                                            secret=shared_room_secret())
        self._room.add_listener(self._signals.event.emit)

        self._current: Optional[Tuple[str, Optional[str]]] = None  # (net_id, channel|None)
        self._unread: Dict[Tuple[str, Optional[str]], int] = {}
        self._highlights: set = set()
        # Auto-select the first channel we join until the user clicks the tree.
        self._user_picked = False
        self._auto_selecting = False
        # Transcript placeholder tracking (avoids serializing the whole
        # QTextDocument per appended line) and incremental search counting.
        self._chat_empty = True
        self._search_count = 0
        # Nick field state: which network it mirrors + unapplied user edits.
        self._nick_field_net: Optional[str] = None
        self._nick_edit_dirty = False
        # Channel-directory panel state.
        self._chanlist_net: Optional[str] = None

        self._build_ui()
        self._reload_network_combo()
        # The DeepFlux Room is pinned above the IRC networks in tree + combo.
        self._add_network_tree_item(ROOM_NET_ID, "DeepFlux Room")
        self._ensure_channel_item(ROOM_NET_ID, ROOM_CHANNEL)
        self._room_nick_edit.setText(self._config.chat.nickname)
        self._room_host_edit.setText(self._config.chat.manual_host)
        # Start disconnected — the user connects manually from the toolbar.
        for net in self._config.irc.networks:
            self._add_network_tree_item(net.id, net.host)
            # Configured channel transcripts remain browsable while offline;
            # get_messages() transparently merges encrypted disk history.
            for channel in net.channels:
                self._ensure_channel_item(net.id, channel)
        self._sync_nick_field(self._selected_network_id())
        self._refresh_status_label()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Toolbar. ResponsiveRow: the full row sums to ~1220px of button
        # minimums — optional buttons overflow into a "⋯" menu on narrow
        # windows instead of locking the window wide.
        self._toolbar_w = ResponsiveRow()
        bar = QHBoxLayout(self._toolbar_w)
        self.network_combo = QComboBox()
        self.network_combo.setMinimumWidth(170)
        self.network_combo.activated.connect(self._on_combo_activated)
        bar.addWidget(self.network_combo)
        # Visible, always-available nickname control for the selected network.
        self.nick_edit = QLineEdit()
        self.nick_edit.setPlaceholderText("Nickname")
        self.nick_edit.setMaximumWidth(110)
        self.nick_edit.setClearButtonEnabled(True)
        self.nick_edit.setToolTip(
            "Your nickname on the selected network. Press Enter to apply — "
            "renames immediately while connected, saved for the next connect "
            "otherwise.")
        self.nick_edit.textEdited.connect(self._on_nick_edited)
        self.nick_edit.returnPressed.connect(self._apply_nick)
        bar.addWidget(self.nick_edit)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        bar.addWidget(self.connect_btn)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        bar.addWidget(self.disconnect_btn)
        self.connect_all_btn = QPushButton("Connect All")
        self.connect_all_btn.setToolTip("Connect every configured network")
        self.connect_all_btn.clicked.connect(self._on_connect_all_clicked)
        bar.addWidget(self.connect_all_btn)
        self.disconnect_all_btn = QPushButton("Disconnect All")
        self.disconnect_all_btn.setToolTip("Disconnect every network")
        self.disconnect_all_btn.clicked.connect(self._on_disconnect_all_clicked)
        bar.addWidget(self.disconnect_all_btn)
        self.manage_btn = QPushButton("Networks…")
        self.manage_btn.clicked.connect(self._on_manage_networks)
        bar.addWidget(self.manage_btn)
        self.settings_btn = QPushButton("Privacy…")
        self.settings_btn.clicked.connect(self._on_irc_settings)
        bar.addWidget(self.settings_btn)
        bar.addSpacing(16)
        self.join_edit = QLineEdit()
        self.join_edit.setPlaceholderText("#channel")
        self.join_edit.setMaximumWidth(140)
        self.join_edit.returnPressed.connect(self._on_join_clicked)
        bar.addWidget(self.join_edit)
        join_btn = QPushButton("Join")
        join_btn.clicked.connect(self._on_join_clicked)
        bar.addWidget(join_btn)
        bar.addStretch(1)
        self.status_label = QLabel("offline")
        self.status_label.setMinimumWidth(60)
        self.status_label.setStyleSheet(f"color:{_MUTED_COLOR}")
        shrink_label(self.status_label)  # live "N/M connected" must not grow the window min
        bar.addWidget(self.status_label)
        root.addWidget(self._toolbar_w)
        # Hide-first order: dialogs first, the per-network Connect/Disconnect
        # pair stays on the row as long as possible.
        self._toolbar_overflow = OverflowRow(self._toolbar_w, (
            self.settings_btn, self.manage_btn,
            self.disconnect_all_btn, self.connect_all_btn,
            self.disconnect_btn, self.connect_btn,
        ))

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

        # Main splitter: tree | chat | nicks
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(150)
        self.tree.setMaximumWidth(280)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_menu)
        self.tree.currentItemChanged.connect(self._on_tree_selection)
        split.addWidget(self.tree)

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
        self.nicks.itemDoubleClicked.connect(lambda item: self._open_query(item.data(ROLE_NICK)))
        split.addWidget(self.nicks)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        root.addWidget(split, 1)

        # Channel directory (visible on a network's server view): /LIST
        # results as a filterable, double-click-to-join table.
        self.chanlist_panel = QWidget()
        self.chanlist_panel.setVisible(False)
        panel = QVBoxLayout(self.chanlist_panel)
        panel.setContentsMargins(0, 0, 0, 0)
        panel.setSpacing(2)
        panel_head = QHBoxLayout()
        self.chanlist_title = QLabel("Channel directory")
        self.chanlist_title.setStyleSheet(f"color:{_SERVER_COLOR}")
        panel_head.addWidget(self.chanlist_title)
        self.chanlist_filter = QLineEdit()
        self.chanlist_filter.setPlaceholderText("Filter channels or topics…")
        self.chanlist_filter.setClearButtonEnabled(True)
        self.chanlist_filter.textChanged.connect(self._on_chanlist_filter)
        panel_head.addWidget(self.chanlist_filter, 1)
        chanlist_refresh_btn = QPushButton("Refresh")
        chanlist_refresh_btn.setToolTip("Ask the server for a fresh channel list (LIST)")
        chanlist_refresh_btn.clicked.connect(self._on_chanlist_refresh)
        panel_head.addWidget(chanlist_refresh_btn)
        panel.addLayout(panel_head)
        self.chanlist_table = QTableWidget(0, 3)
        self.chanlist_table.setHorizontalHeaderLabels(["Channel", "Users", "Topic"])
        self.chanlist_table.verticalHeader().setVisible(False)
        self.chanlist_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.chanlist_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.chanlist_table.setSortingEnabled(True)
        # Default directory order: most users first (Qt's fresh-table default
        # is column-0 DESCENDING, which would show channels Z→A instead).
        self.chanlist_table.horizontalHeader().setSortIndicator(
            1, Qt.SortOrder.DescendingOrder)
        self.chanlist_table.horizontalHeader().setStretchLastSection(True)
        self.chanlist_table.doubleClicked.connect(self._on_chanlist_activated)
        self.chanlist_table.setMinimumHeight(110)
        self.chanlist_table.setMaximumHeight(240)
        panel.addWidget(self.chanlist_table)
        root.addWidget(self.chanlist_panel)

        # Topic bar
        self.topic_label = QLabel("")
        self.topic_label.setStyleSheet(f"color:{_SERVER_COLOR}")
        self.topic_label.setWordWrap(True)
        root.addWidget(self.topic_label)

        # DeepFlux Room join bar (visible only while the room view is open).
        # The room never auto-connects: nickname + Join, every session.
        self._room_bar = QWidget()
        self._room_bar.setVisible(False)
        room_row = QHBoxLayout(self._room_bar)
        room_row.setContentsMargins(0, 0, 0, 0)
        room_row.setSpacing(6)
        room_label = QLabel("DeepFlux Room")
        room_label.setStyleSheet(f"color:#a8edff;font-weight:600")
        room_row.addWidget(room_label)
        self._room_nick_edit = QLineEdit()
        self._room_nick_edit.setPlaceholderText("Nickname")
        self._room_nick_edit.setMaximumWidth(140)
        self._room_nick_edit.setClearButtonEnabled(True)
        self._room_nick_edit.returnPressed.connect(self._on_room_join_clicked)
        room_row.addWidget(self._room_nick_edit)
        self._room_advanced_btn = QPushButton("Host…")
        self._room_advanced_btn.setCheckable(True)
        self._room_advanced_btn.setToolTip(
            "Advanced: connect directly to a hosting peer (ip:port) instead "
            "of using automatic discovery")
        self._room_advanced_btn.toggled.connect(self._on_room_advanced_toggled)
        room_row.addWidget(self._room_advanced_btn)
        self._room_host_edit = QLineEdit()
        self._room_host_edit.setPlaceholderText("ip:port — direct host")
        self._room_host_edit.setMaximumWidth(200)
        self._room_host_edit.setVisible(False)
        room_row.addWidget(self._room_host_edit)
        self._room_join_btn = QPushButton("Join")
        self._room_join_btn.clicked.connect(self._on_room_join_clicked)
        room_row.addWidget(self._room_join_btn)
        self._room_private_btn = QPushButton("Make private…")
        self._room_private_btn.setToolTip(
            "Generate a new random room key shared only with the people in "
            "the room right now — people joining later will not see this room")
        self._room_private_btn.clicked.connect(self._on_room_private_clicked)
        self._room_private_btn.setVisible(False)
        room_row.addWidget(self._room_private_btn)
        self._room_status = QLabel("")
        self._room_status.setStyleSheet(f"color:{_MUTED_COLOR}")
        shrink_label(self._room_status)  # live status must not grow the window min
        room_row.addWidget(self._room_status, 1)
        root.addWidget(self._room_bar)

        # Input
        bottom = QHBoxLayout()
        self.input = IRCInputLine(self._complete_input)
        self.input.setPlaceholderText("Message — /help for commands")
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
        self.network_combo.addItem(self._room_combo_label(), userData=ROOM_NET_ID)
        for net in self._config.irc.networks:
            self.network_combo.addItem(self._network_combo_label(net), userData=net.id)
        self.network_combo.blockSignals(False)

    def _network_combo_label(self, net: IRCNetworkConfig, state: str = "") -> str:
        """Combo text with a live per-network status dot."""
        if not state:
            state = self._client.state.network_link_state(net.id)
        dot = {"connected": "●", "connecting": "◌", "error": "✕"}.get(state, "○")
        return f"{dot} {net.host}:{net.port}" + ("" if net.tls else " (plain)")

    def _refresh_status_label(self, note: str = "") -> None:
        """Aggregate connection status, optionally annotated for one network."""
        networks = self._config.irc.networks
        if networks:
            connected = sum(1 for net in networks if self._client.is_connected(net.id))
            base = f"{connected}/{len(networks)} connected"
            color = "#a8edff" if connected else _MUTED_COLOR
        else:
            base = "no networks configured"
            color = _MUTED_COLOR
        self.status_label.setStyleSheet(f"color:{color}")
        self.status_label.setText(f"{note} · {base}" if note else base)

    def _add_network_tree_item(self, net_id: str, host: str) -> QTreeWidgetItem:
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, ROLE_NET) == net_id:
                return item
        base = f"◌ {host or net_id}"
        item = QTreeWidgetItem([base])
        item.setData(0, ROLE_NET, net_id)
        item.setData(0, ROLE_CHAN, None)
        item.setData(0, ROLE_BASE_LABEL, base)
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
            if self._client.state.identifiers_equal(
                    net_id, item.data(0, ROLE_CHAN) or "", channel):
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
        item.setData(0, ROLE_BASE_LABEL, channel)
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
        if net_id == ROOM_NET_ID:
            self._on_room_join_clicked()
            return
        self._connect_network(net_id)

    def _connect_network(self, net_id: str) -> None:
        net = self._find_net_cfg(net_id)
        if net:
            self._client.connect_network(net)

    def _on_disconnect_clicked(self) -> None:
        net_id = self._selected_network_id()
        if net_id == ROOM_NET_ID:
            self._room.leave()
            return
        if net_id:
            self._client.disconnect_network(net_id)

    def _on_connect_all_clicked(self) -> None:
        for net in self._config.irc.networks:
            self._client.connect_network(net)

    def _on_disconnect_all_clicked(self) -> None:
        for net in self._config.irc.networks:
            self._client.disconnect_network(net.id)

    # ------------------------------------------------------------------
    # DeepFlux Room (serverless community chat, ircmgr/room.py)
    # ------------------------------------------------------------------

    def _room_combo_label(self, state: str = "") -> str:
        if not state:
            state = {"host": "connected", "member": "connected",
                     "connecting": "connecting"}.get(self._room.role, "offline")
        dot = {"connected": "●", "connecting": "◌", "error": "✕"}.get(state, "○")
        return f"{dot} DeepFlux Room"

    def _on_room_advanced_toggled(self, checked: bool) -> None:
        self._room_host_edit.setVisible(checked)
        self._room_advanced_btn.setText("Host…" if not checked else "Hide")

    def _update_room_bar(self) -> None:
        room_selected = bool(self._current and self._current[0] == ROOM_NET_ID)
        self._room_bar.setVisible(room_selected)
        if not room_selected:
            return
        joined = self._room.is_joined()
        self._room_join_btn.setText("Leave" if joined else "Join")
        self._room_nick_edit.setEnabled(not joined)
        self._room_private_btn.setVisible(joined)
        mode = "encrypted" if self._room.encrypted else "UNENCRYPTED (source build)"
        if joined and "private room" in (self._client.state.topic_of(
                ROOM_NET_ID, ROOM_CHANNEL) or ""):
            mode = "private · " + mode
        role = self._room.role
        if joined and role == "host":
            endpoints = self._room.endpoints()
            where = f" — others join via {endpoints[0]}" if endpoints else ""
            self._room_status.setText(f"hosting{where} · {mode}")
        elif joined:
            self._room_status.setText(f"connected as {self._room.nick} · {mode}")
        elif role == "connecting":
            self._room_status.setText(f"connecting… · {mode}")
        else:
            self._room_status.setText(
                "first one in hosts the room · " + mode)

    def _on_room_join_clicked(self) -> None:
        if self._room.is_joined():
            self._room.leave()
            self._update_room_bar()  # instant feedback; the state event re-syncs
            return
        nick = self._room_nick_edit.text().strip()
        if not nick:
            self._room_status.setText("Enter a nickname first.")
            self._room_nick_edit.setFocus()
            return
        if re.search(r"[\s,\r\n]", nick) or len(nick) > 24:
            self._show_local("error", "Invalid nickname (no spaces or commas, "
                                      "max 24 chars).")
            return
        manual = ""
        if self._room_advanced_btn.isChecked():
            manual = self._room_host_edit.text().strip()
        self._config.chat.nickname = nick
        self._config.chat.manual_host = manual
        self._save_config()
        self._room.join(nick, manual)
        self._update_room_bar()

    def _on_room_private_clicked(self) -> None:
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

    def _room_send(self, text: str) -> None:
        # Query tabs under the room are view-only: everything sent from the
        # room goes to the whole room — never pretend it is private.
        if (self._current and self._current[0] == ROOM_NET_ID
                and self._current[1] is not None
                and not self._client.state.identifiers_equal(
                    ROOM_NET_ID, self._current[1], ROOM_CHANNEL)):
            self._show_local("error",
                             "Private messages are not supported in the room.")
            return
        if text.startswith("/"):
            parts = text.split(" ", 2)
            cmd = parts[0].lower()
            if cmd == "/clear":
                self._client.state.clear_buffer(ROOM_NET_ID, ROOM_CHANNEL)
                self._render_buffer(ROOM_NET_ID, ROOM_CHANNEL)
            elif cmd == "/me":
                action = text.split(" ", 1)[1] if len(parts) > 1 else ""
                if action and self._room.is_joined():
                    if not self._room.send_message(action, action=True):
                        self._show_local("error", "Not connected — action not sent.")
                else:
                    self._show_local("error", "Join the room first.")
            elif cmd == "/help":
                self._show_local("server",
                                 "Room commands: /me action · /clear · /help — "
                                 "everything else is IRC-only.")
            else:
                self._show_local("error",
                                 f"{cmd} is not available in the DeepFlux Room.")
            return
        if not self._room.is_joined():
            self._show_local("error", "Join the room first — nickname + Join below.")
            return
        if not self._room.send_message(text):
            self._show_local("error", "Not connected — message not sent.")

    def _copy_room_address(self) -> None:
        from PySide6.QtWidgets import QApplication
        endpoints = self._room.endpoints()
        if endpoints:
            QApplication.clipboard().setText(endpoints[0])

    def _on_combo_activated(self, index: int) -> None:
        """User picked a network in the combo → follow it in the tree."""
        net_id = self.network_combo.itemData(index) if index >= 0 else None
        if not net_id:
            return
        item = self._network_item(net_id)
        if item:
            self.tree.setCurrentItem(item)
        else:
            self._sync_nick_field(net_id)

    def _on_nick_edited(self, _text: str) -> None:
        self._nick_edit_dirty = True

    def _sync_nick_field(self, net_id: Optional[str], force: bool = False) -> None:
        """Mirror a network's nickname in the toolbar field. Unapplied user
        edits for the SAME network are preserved; switching networks resets."""
        if not net_id:
            return
        if force or net_id != self._nick_field_net or not self._nick_edit_dirty:
            current = self._client.state.nick_of(net_id)
            cfg = self._find_net_cfg(net_id)
            self.nick_edit.setText(current or (cfg.nick if cfg else "") or "")
            self._nick_edit_dirty = False
        self._nick_field_net = net_id

    def _apply_nick(self) -> None:
        nick = self.nick_edit.text().strip()
        net_id = self._current[0] if self._current else self._selected_network_id()
        if not net_id:
            self._show_local("error", "No network selected.")
            return
        if not nick:
            self._sync_nick_field(net_id, force=True)
            return
        if re.search(r"[\s,\r\n]", nick) or len(nick) > 30:
            self._show_local("error", "Invalid nickname (no spaces or commas, max 30 chars).")
            return
        if net_id == ROOM_NET_ID:
            self._config.chat.nickname = nick[:24]
            self._save_config()
            self._room_nick_edit.setText(nick[:24])
            self._show_local("server", "Room nickname saved — it is used when "
                                        "you press Join.")
            self._nick_edit_dirty = False
            return
        cfg = self._find_net_cfg(net_id)
        if cfg and cfg.nick != nick:
            cfg.nick = nick
            self._save_config()
        if self._client.is_connected(net_id):
            self._client.change_nick(net_id, nick)
        else:
            host = cfg.host if cfg else net_id
            self._show_local("server", f"Nickname saved for {host} — used on the next connect.")
            self._sync_nick_field(net_id, force=True)
        self._nick_edit_dirty = False
        self.input.setFocus()

    def _on_manage_networks(self) -> None:
        selected_id = self._selected_network_id() or ""
        before_ids = {net.id for net in self._config.irc.networks}
        dialog = NetworkManagerDialog(self._config.irc.networks, self)
        dialog.exec()
        if not dialog.changed:
            return
        after_ids = {net.id for net in self._config.irc.networks}
        for removed_id in before_ids - after_ids:
            self._client.disconnect_network(removed_id)
            self._client.state.remove_network(removed_id)
            item = self._network_item(removed_id)
            if item:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
            if self._current and self._current[0] == removed_id:
                self._current = None
                self.chat.clear()
                self._chat_empty = True
                self._search_count = 0
                self.nicks.clear()
                self.topic_label.clear()
                self.chanlist_panel.setVisible(False)
            for key in [key for key in self._unread if key[0] == removed_id]:
                self._unread.pop(key, None)
                self._highlights.discard(key)
        for net in self._config.irc.networks:
            item = self._network_item(net.id) or self._add_network_tree_item(net.id, net.host)
            base = f"◌ {net.host}"
            item.setData(0, ROLE_BASE_LABEL, base)
            item.setText(0, base)
        self._reload_network_combo()
        index = self.network_combo.findData(selected_id)
        if index >= 0:
            self.network_combo.setCurrentIndex(index)
        self._refresh_status_label()
        self._save_config()

    def _on_irc_settings(self) -> None:
        dialog = IRCSettingsDialog(self._config.irc, self._client, self)
        if dialog.exec():
            self._save_config()
            if self._current:
                self._render_buffer(*self._current)

    def _on_join_clicked(self) -> None:
        # Join acts on the network of the channel being VIEWED (tree
        # selection), not whatever the combo happens to show.
        net_id = (self._current[0] if self._current else None) or self._selected_network_id()
        channel = self.join_edit.text().strip()
        if net_id == ROOM_NET_ID:
            self._show_local("server",
                             "The DeepFlux Room is joined from the nickname "
                             "bar below its view.")
            return
        if net_id and channel:
            self._client.join(net_id, channel)
            self.join_edit.clear()

    def _prompt_join(self, net_id: str) -> None:
        channel, ok = QInputDialog.getText(self, "Join channel", "Channel:", text="#")
        if not ok:
            return
        channel = channel.strip()
        if channel:
            self._client.join(net_id, channel)

    def _show_tree_menu(self, pos: QPoint) -> None:
        menu = self._build_tree_menu(self.tree.itemAt(pos))
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _build_tree_menu(self, item: Optional[QTreeWidgetItem]) -> Optional[QMenu]:
        if item is None:
            return None
        net_id = item.data(0, ROLE_NET)
        channel = item.data(0, ROLE_CHAN)
        if not net_id:
            return None
        menu = QMenu(self)
        if net_id == ROOM_NET_ID:
            if self._room.is_joined():
                menu.addAction("Leave the room", lambda: self._room.leave())
                if self._room.role == "host" and self._room.endpoints():
                    menu.addAction("Copy room address", self._copy_room_address)
            else:
                menu.addAction("Join the room…", self._on_room_join_clicked)
            return menu
        if channel is None:
            if self._client.is_connected(net_id):
                menu.addAction("Disconnect", lambda: self._client.disconnect_network(net_id))
            else:
                menu.addAction("Connect", lambda: self._connect_network(net_id))
            menu.addAction("Join channel…", lambda: self._prompt_join(net_id))
            menu.addSeparator()
            menu.addAction("Edit in Networks…", self._on_manage_networks)
        else:
            if channel.startswith(CHANNEL_PREFIXES):
                menu.addAction("Part", lambda: self._client.part(net_id, channel))
            else:
                menu.addAction("Close query", lambda: self._close_query(net_id, channel))
            menu.addAction("Copy name", lambda: self._copy_nick(channel))
        return menu

    def _close_query(self, net_id: str, nick: str) -> None:
        self._client.state.drop_channel(net_id, nick)
        self._remove_channel_view(net_id, nick)

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
        self.input.remember(text)
        self.input.clear()
        net_id = self._current[0] if self._current else self._selected_network_id()
        if not net_id:
            self._show_local("error", "No network selected.")
            return

        if net_id == ROOM_NET_ID:
            self._room_send(text)
            return
        if text.startswith("/"):
            if self._handle_command(net_id, text):
                return
        # Plain text → current channel
        channel = self._current[1] if self._current else None
        if not channel:
            self._show_local("error", "Select a channel first (or use /join).")
            return
        if not self._is_connected(net_id):
            self._show_local("error", "Not connected — message not sent.")
            return
        # The core emits either a local normalized event after the write, or
        # the server's echo when echo-message is negotiated. Rendering only
        # those events prevents duplicate local echoes.
        self._client.send_message(net_id, channel, text)

    def _is_connected(self, net_id: str) -> bool:
        if net_id == ROOM_NET_ID:
            return self._room.is_joined()
        return self._client.is_connected(net_id)

    def _show_local(self, kind: str, text: str) -> None:
        """Append a local-only line (errors, command notes) to the view."""
        self._append_line(None, None, kind, "", text)

    _HELP_TEXT = (
        "Commands: /join #channel · /part [channel] · /msg nick text · "
        "/me action · /nick new · /notice target text · /whois nick · "
        "/away [reason] · /back · /hop · /close · /clear · /list [filter] · "
        "/raw line · /quit"
    )

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
                self._show_local("error", "Not connected — message not sent.")
                return True
            self._client.send_message(net_id, target, msg)
            self._ensure_channel_item(net_id, target)
        elif cmd == "/me" and len(parts) > 1:
            channel = self._current[1] if self._current else ""
            if not channel:
                self._show_local("error", "/me needs a channel.")
                return True
            if not self._is_connected(net_id):
                self._show_local("error", "Not connected — action not sent.")
                return True
            action = text.split(" ", 1)[1]
            self._client.send_action(net_id, channel, action)
        elif cmd == "/nick" and len(parts) > 1:
            self._client.change_nick(net_id, parts[1].strip())
        elif cmd == "/notice":
            # /notice <target> text — standard NOTICE semantics (explicit
            # target; a bare message would be ambiguous).
            if len(parts) >= 3:
                target, msg = parts[1].strip(), parts[2]
            else:
                self._show_local("error", "Usage: /notice <target> text")
                return True
            if not self._is_connected(net_id):
                self._show_local("error", "Not connected — notice not sent.")
                return True
            self._client.send_notice(net_id, target, msg)
            if not target.startswith(CHANNEL_PREFIXES):
                self._ensure_channel_item(net_id, target)
        elif cmd == "/whois" and len(parts) > 1:
            if not self._is_connected(net_id):
                self._show_local("error", "Not connected.")
                return True
            nick = parts[1].strip()
            self._client.send_raw(net_id, f"WHOIS {nick}")
            self._show_local("server", f"WHOIS {nick} — reply appears in the network view.")
        elif cmd == "/away":
            if not self._is_connected(net_id):
                self._show_local("error", "Not connected.")
                return True
            reason = text.split(" ", 1)[1] if len(parts) > 1 else ""
            self._client.send_raw(net_id, f"AWAY :{reason}" if reason else "AWAY")
        elif cmd == "/back":
            if not self._is_connected(net_id):
                self._show_local("error", "Not connected.")
                return True
            self._client.send_raw(net_id, "AWAY")
        elif cmd == "/hop":
            # Cycle the current channel: part and re-join immediately.
            channel = self._current[1] if self._current else ""
            if not channel or not channel.startswith(CHANNEL_PREFIXES):
                self._show_local("error", "/hop needs a channel view.")
                return True
            self._client.part(net_id, channel)
            self._client.join(net_id, channel)
        elif cmd == "/close":
            channel = self._current[1] if self._current else ""
            if not channel:
                self._show_local("error", "/close needs a channel or query view.")
            elif channel.startswith(CHANNEL_PREFIXES):
                self._client.part(net_id, channel)
            else:
                self._close_query(net_id, channel)
        elif cmd == "/clear":
            if self._current:
                self._client.state.clear_buffer(self._current[0], self._current[1])
                self._render_buffer(*self._current)
        elif cmd == "/help":
            self._show_local("server", self._HELP_TEXT)
        elif cmd == "/list":
            if not self._is_connected(net_id):
                self._show_local("error", "Not connected.")
                return True
            # Optional filter: /list #foo* — results land in the server view.
            arg = text.split(" ", 1)[1].strip() if len(parts) > 1 else ""
            self._client.send_raw(net_id, "LIST " + arg if arg else "LIST")
            self._show_local("server", "Requesting channel list… "
                                         "(results appear in the network view)")
        elif cmd == "/raw" and len(parts) > 1:
            self._client.send_raw(net_id, text.split(" ", 1)[1])
        elif cmd == "/quit":
            self._client.disconnect_network(net_id)
        else:
            self._show_local("error", f"Unknown command: {cmd} — try /help")
        return True

    def _complete_input(self, text: str, cursor: int) -> Optional[Tuple[str, int]]:
        net_id = self._current[0] if self._current else self._selected_network_id()
        if not net_id:
            return None
        start = cursor
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        fragment = text[start:cursor]
        if not fragment or fragment.startswith("/"):
            return None
        channels: List[str] = self._client.state.channel_names(net_id)
        channels.extend(row["channel"] for row in self._client.state.chanlist_of(net_id))
        net = self._find_net_cfg(net_id)
        if net:
            channels.extend(net.channels)
        nicks = list(self._client.state.nicks_of(
            net_id, self._current[1]).keys()) if self._current and self._current[1] else []
        candidates = channels if fragment.startswith(CHANNEL_PREFIXES) else nicks
        mapping = self._client.state.casemapping_of(net_id)
        unique: Dict[str, str] = {}
        for candidate in candidates:
            unique.setdefault(irc_casefold(candidate, mapping), candidate)
        folded_fragment = irc_casefold(fragment, mapping)
        matches = sorted(
            (candidate for folded, candidate in unique.items()
             if folded.startswith(folded_fragment)), key=str.lower,
        )
        if not matches:
            return None
        replacement = matches[0]
        if len(matches) > 1:
            common = replacement
            for candidate in matches[1:]:
                length = 0
                for left, right in zip(common, candidate):
                    if not self._client.state.identifiers_equal(net_id, left, right):
                        break
                    length += 1
                common = common[:length]
            if len(common) > len(fragment):
                replacement = common
        suffix = ": " if start == 0 and not fragment.startswith(CHANNEL_PREFIXES) else " "
        result = text[:start] + replacement + suffix + text[cursor:]
        return result, start + len(replacement) + len(suffix)

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

    def _show_nick_menu(self, pos: QPoint) -> None:
        item = self.nicks.itemAt(pos)
        if item is None:
            return
        nick = item.data(ROLE_NICK) or ""
        menu = QMenu(self)
        query_action = menu.addAction("Open query")
        whois_action = menu.addAction("WHOIS")
        menu.addSeparator()
        copy_action = menu.addAction("Copy nickname")
        chosen = menu.exec(self.nicks.viewport().mapToGlobal(pos))
        if chosen == query_action:
            self._open_query(nick)
        elif chosen == whois_action:
            self._whois_nick(nick)
        elif chosen == copy_action:
            self._copy_nick(nick)

    def _safe_nick(self, nick: str) -> str:
        return nick.strip()[:80] if nick and not re.search(r"[\s,\r\n]", nick) else ""

    def _open_query(self, nick: str) -> None:
        nick = self._safe_nick(nick)
        net_id = self._current[0] if self._current else self._selected_network_id()
        if not nick or not net_id:
            return
        self._client.state.ensure_channel(net_id, nick)
        item = self._ensure_channel_item(net_id, nick)
        if item:
            self.tree.setCurrentItem(item)
            self.input.setFocus()

    def _whois_nick(self, nick: str) -> None:
        nick = self._safe_nick(nick)
        net_id = self._current[0] if self._current else self._selected_network_id()
        if nick and net_id and self._is_connected(net_id):
            self._client.send_raw(net_id, f"WHOIS {nick}")

    @staticmethod
    def _copy_nick(nick: str) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(nick)

    def _same_target(self, left: Optional[Tuple[str, Optional[str]]], net_id: str,
                     channel: Optional[str]) -> bool:
        if left is None or left[0] != net_id:
            return False
        if left[1] is None or channel is None:
            return left[1] is channel
        return self._client.state.identifiers_equal(net_id, left[1], channel)

    # ------------------------------------------------------------------
    # selection / rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _open_external_link(url: QUrl) -> None:
        """Open only absolute HTTP(S) transcript links outside the app."""
        if url.isValid() and url.scheme().lower() in ("http", "https") and url.host():
            QDesktopServices.openUrl(url)

    def _on_tree_selection(self, current: Optional[QTreeWidgetItem], _previous) -> None:
        if current is None:
            return
        if not self._auto_selecting:
            self._user_picked = True
        net_id = current.data(0, ROLE_NET)
        channel = current.data(0, ROLE_CHAN)
        self._current = (net_id, channel)
        for key in list(self._unread):
            if self._same_target(key, net_id, channel):
                self._unread.pop(key, None)
                self._highlights.discard(key)
        self._style_channel_item(net_id, channel, unread=False)
        self._render_buffer(net_id, channel)
        self._refresh_nicks(net_id, channel)
        topic = self._client.state.topic_of(net_id, channel) if channel else ""
        self.topic_label.setText(topic)
        idx = self.network_combo.findData(net_id)
        if idx >= 0:
            self.network_combo.setCurrentIndex(idx)
        self._sync_nick_field(net_id)
        # The channel directory is part of the server (network) view.
        if channel is None:
            self._populate_chanlist(net_id)
        else:
            self.chanlist_panel.setVisible(False)
        self._update_room_bar()

    def _render_buffer(self, net_id: str, channel: Optional[str]) -> None:
        self.chat.clear()
        self._chat_empty = True
        self._search_count = 0
        if net_id == ROOM_NET_ID:
            # The room is not an IRC network: read the shared state's memory
            # buffer directly (no encrypted IRC history store involved).
            msgs = [m.to_dict() for m in self._client.state.get_messages(
                net_id, channel, limit=self._config.irc.buffer_lines)]
        else:
            msgs = self._client.get_messages(net_id, channel,
                                             limit=self._config.irc.buffer_lines)
        lines = [self._fmt_line(
            m["kind"], m["nick"], m["text"], m["ts"],
            highlighted=self._is_mention(net_id, m["nick"], m["text"], m["kind"]),
        ) for m in msgs]
        self.chat.setHtml("<br>".join(lines) if lines else
                          f'<span style="color:{_MUTED_COLOR}">Nothing here yet.</span>')
        self._chat_empty = not lines
        self._scroll_to_bottom()
        self._on_search_changed(self.search_edit.text())

    # -- channel directory (/LIST) panel ------------------------------------

    _CHANLIST_PANEL_CAP = 2000

    def _populate_chanlist(self, net_id: str) -> None:
        """Fill the channel-directory table for a network's server view."""
        self._chanlist_net = net_id
        rows = self._client.state.chanlist_of(net_id)
        self.chanlist_table.setSortingEnabled(False)
        self.chanlist_table.setRowCount(0)
        if not rows:
            self.chanlist_panel.setVisible(False)
            self.chanlist_table.setSortingEnabled(True)
            return
        shown = rows[: self._CHANLIST_PANEL_CAP]
        self.chanlist_table.setRowCount(len(shown))
        for row, entry in enumerate(shown):
            channel_item = QTableWidgetItem(str(entry.get("channel", "")))
            users_item = QTableWidgetItem()
            try:
                users_item.setData(Qt.ItemDataRole.DisplayRole, int(entry.get("users", 0)))
            except (TypeError, ValueError):
                users_item.setData(Qt.ItemDataRole.DisplayRole, 0)
            topic_item = QTableWidgetItem(str(entry.get("topic", "")))
            for column, item in enumerate((channel_item, users_item, topic_item)):
                self.chanlist_table.setItem(row, column, item)
        self.chanlist_table.setSortingEnabled(True)
        ts = self._client.state.chanlist_ts(net_id)
        stamp = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
        capped = (f" — showing top {len(shown)} of {len(rows)} by users"
                  if len(rows) > len(shown) else "")
        self.chanlist_title.setText(
            f"Channel directory — {len(rows)} channels, {stamp}{capped} "
            "(double-click a row to join)")
        self.chanlist_panel.setVisible(True)
        self._on_chanlist_filter(self.chanlist_filter.text())

    def _on_chanlist_filter(self, text: str) -> None:
        query = text.strip().lower()
        for row in range(self.chanlist_table.rowCount()):
            visible = not query
            if not visible:
                for column in (0, 2):  # channel name + topic (skip Users)
                    item = self.chanlist_table.item(row, column)
                    if item and query in item.text().lower():
                        visible = True
                        break
            self.chanlist_table.setRowHidden(row, not visible)

    def _on_chanlist_activated(self, index) -> None:
        item = self.chanlist_table.item(index.row(), 0) if index.row() >= 0 else None
        net_id = self._chanlist_net
        if item and net_id:
            channel = item.text().strip()
            if channel:
                self._client.join(net_id, channel)

    def _on_chanlist_refresh(self) -> None:
        net_id = self._chanlist_net or (self._current[0] if self._current else None)
        if net_id and self._client.is_connected(net_id):
            self._client.send_raw(net_id, "LIST")
            self._show_local("server", "Requesting channel list…")

    def _refresh_nicks(self, net_id: str, channel: Optional[str]) -> None:
        self.nicks.clear()
        if not channel:
            return
        nicks = self._client.state.nicks_of(net_id, channel)
        mapping = self._client.state.casemapping_of(net_id)
        # Ops first (strongest prefix wins), then case-folded name.
        rank = {symbol: index for index, symbol in enumerate("~&@%+")}
        ordered = sorted(
            nicks,
            key=lambda value: (
                min((rank[symbol] for symbol in nicks[value] if symbol in rank),
                    default=len(rank)),
                irc_casefold(value, mapping)))
        for nick in ordered:
            prefix = nicks[nick]
            item = QListWidgetItem(f"{prefix}{nick}" if prefix else nick)
            metadata = self._client.state.user_metadata(net_id, nick)
            if metadata.get("away"):
                font = item.font()
                font.setItalic(True)
                item.setFont(font)
                item.setForeground(QColor(_MUTED_COLOR))
            account = metadata.get("account") or ""
            if account:
                item.setToolTip(f"Logged in as {account}")
            item.setData(ROLE_NICK, nick)
            self.nicks.addItem(item)

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
            if self._same_target(self._current, net_id, event.get("channel")):
                self._refresh_nicks(net_id, event.get("channel"))
            return
        if etype == "chanlist":
            if self._same_target(self._current, net_id, None):
                self._populate_chanlist(net_id)
            else:
                self._mark_unread(net_id, None)
            return
        if etype == "user_metadata":
            # account-notify / away-notify: refresh the visible nick list
            # (away styling + account tooltip) when the nick is on screen.
            if self._current and self._current[0] == net_id and self._current[1]:
                nick = event.get("nick", "")
                if nick and any(
                        self._client.state.nick_equals(net_id, nick, known)
                        for known in self._client.state.nicks_of(net_id, self._current[1])):
                    self._refresh_nicks(net_id, self._current[1])
            return
        if etype == "nick":
            new = event.get("new", "")
            if new and self._client.state.nick_equals(
                    net_id, new, self._client.state.nick_of(net_id)):
                # Own rename confirmed by the server: update the nick field
                # and the aggregate status note.
                self._sync_nick_field(net_id, force=True)
                if net_id == self._selected_network_id() or (
                        self._current and self._current[0] == net_id):
                    self._refresh_status_label(f"connected as {new}")
            if self._current and self._current[0] == net_id and self._current[1]:
                self._refresh_nicks(net_id, self._current[1])
            return
        if etype == "dcc_offer":
            # DCC is display-only: the core never accepts it and the GUI offers no accept action.
            self._maybe_show(net_id, None, "notice", event.get("nick", ""),
                             event.get("detail", "DCC offer ignored (unsupported)."),
                             highlight=True)
            return
        if etype == "topic":
            channel = event.get("channel", "")
            self._ensure_channel_item(net_id, channel)
            if self._same_target(self._current, net_id, channel):
                self.topic_label.setText(event.get("topic", ""))
            self._maybe_show(net_id, channel, "topic", "", event.get("topic", ""))
            if net_id == ROOM_NET_ID:
                self._update_room_bar()  # picks up the "private" status tag
            return
        if etype == "parted":
            self._remove_channel_view(net_id, event.get("channel", ""))
            return

        # The core emits a final own PART/KICK after its dedicated `parted`
        # event. Do not recreate the just-removed channel for that event.
        if etype in ("part", "kick") and event.get("own"):
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
            self._maybe_show(net_id, channel, kind, nick, text,
                             ts=event.get("ts"))
            if (etype in ("join", "part", "kick", "quit")
                    and self._same_target(self._current, net_id, channel)):
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

    def _remove_channel_view(self, net_id: str, channel: str) -> None:
        """Drop a channel/query tree item and clean up selection + unread."""
        item = self._channel_item(net_id, channel)
        parent = item.parent() if item else self._network_item(net_id)
        if self._same_target(self._current, net_id, channel):
            # Move selection before deleting the selected item so _current
            # cannot keep pointing at a channel we have left/been kicked from.
            self._auto_selecting = True
            if parent:
                self.tree.setCurrentItem(parent)
            else:
                self.tree.clearSelection()
                self._current = (net_id, None)
                self._render_buffer(net_id, None)
            self._auto_selecting = False
            self.nicks.clear()
            self.topic_label.setText("")
        if item and item.parent():
            item.parent().removeChild(item)
        for key in list(self._unread):
            if self._same_target(key, net_id, channel):
                self._unread.pop(key, None)
                self._highlights.discard(key)
        self._refresh_unread_labels(net_id)

    def _maybe_show(self, net_id: str, channel: Optional[str], kind: str,
                    nick: str, text: str, highlight: bool = False,
                    ts: Optional[float] = None) -> None:
        highlight = highlight or self._is_mention(net_id, nick, text, kind)
        if (channel and not channel.startswith(CHANNEL_PREFIXES)
                and kind in ("msg", "action")
                and not self._client.state.nick_equals(
                    net_id, nick, self._client.state.nick_of(net_id))):
            highlight = True  # private query
        if self._same_target(self._current, net_id, channel):
            self._append_line(net_id, channel, kind, nick, text,
                              highlighted=highlight, ts=ts)
        else:
            self._mark_unread(net_id, channel, highlight)

    def _target_key(self, net_id: str,
                    channel: Optional[str]) -> Tuple[str, Optional[str]]:
        item = self._channel_item(net_id, channel) if channel else None
        return net_id, item.data(0, ROLE_CHAN) if item else channel

    def _mark_unread(self, net_id: str, channel: Optional[str],
                     highlight: bool = False) -> None:
        key = self._target_key(net_id, channel)
        self._unread[key] = self._unread.get(key, 0) + 1
        if highlight:
            self._highlights.add(key)
        self._refresh_unread_labels(net_id)

    def _refresh_unread_labels(self, net_id: str) -> None:
        parent = self._network_item(net_id)
        if not parent:
            return
        total = sum(count for (network, _channel), count in self._unread.items()
                    if network == net_id)
        parent_base = parent.data(0, ROLE_BASE_LABEL) or parent.text(0).split(" [", 1)[0]
        parent.setText(0, f"{parent_base} [{total}]" if total else parent_base)
        parent_font = parent.font(0)
        parent_font.setBold(True)
        parent.setFont(0, parent_font)
        for index in range(parent.childCount()):
            item = parent.child(index)
            channel = item.data(0, ROLE_CHAN)
            key = next((candidate for candidate in self._unread
                        if self._same_target(candidate, net_id, channel)), None)
            count = self._unread.get(key, 0) if key else 0
            base = item.data(0, ROLE_BASE_LABEL) or channel
            item.setText(0, f"{base} [{count}]" if count else base)
            font = item.font(0)
            font.setBold(bool(count))
            item.setFont(0, font)
            highlighted = bool(key and key in self._highlights)
            item.setForeground(0, QColor(
                _NOTICE_COLOR if highlighted else ("#2a6aaf" if count else "#cfd8e3")))

    def _style_channel_item(self, net_id: str, channel: Optional[str], unread: bool) -> None:
        # Compatibility helper used by selection paths; labels derive from counters.
        if not unread:
            self._refresh_unread_labels(net_id)

    def _is_mention(self, net_id: str, nick: str, text: str, kind: str) -> bool:
        if kind not in ("msg", "action"):
            return False
        own = self._client.state.nick_of(net_id)
        if not own or self._client.state.nick_equals(net_id, nick, own):
            return False
        mapping = self._client.state.casemapping_of(net_id)
        folded_text = irc_casefold(text, mapping)
        folded_own = re.escape(irc_casefold(own, mapping))
        return bool(re.search(rf"(?<![a-z0-9_]){folded_own}(?![a-z0-9_])", folded_text))

    def _on_state_event(self, net_id: str, event: Dict[str, Any]) -> None:
        state = event.get("state", "")
        item = self._network_item(net_id)
        net = self._find_net_cfg(net_id)
        host = "DeepFlux Room" if net_id == ROOM_NET_ID else (
            net.host if net else net_id)
        if item:
            dot = {"connected": "●", "connecting": "◌", "disconnected": "○",
                   "error": "✕"}.get(state, "○")
            color = {"connected": "#a8edff", "connecting": "#ffd43b",
                     "disconnected": "#5b6b7c", "error": "#ff6e6e"}.get(state, "#5b6b7c")
            base = f"{dot} {host}"
            item.setData(0, ROLE_BASE_LABEL, base)
            item.setText(0, base)
            item.setForeground(0, QColor(color))
            self._refresh_unread_labels(net_id)
        combo_index = self.network_combo.findData(net_id)
        if combo_index >= 0:
            if net_id == ROOM_NET_ID:
                self.network_combo.setItemText(
                    combo_index, self._room_combo_label(state))
            elif net:
                self.network_combo.setItemText(
                    combo_index, self._network_combo_label(net, state))
        if net_id == ROOM_NET_ID:
            self._update_room_bar()
        if net_id == self._selected_network_id() or (self._current and self._current[0] == net_id):
            note = {"connected": f"connected as {event.get('nick', '')}",
                    "connecting": "connecting…", "disconnected": "offline",
                    "error": f"error: {event.get('detail', '')}"}.get(state, state)
            self._refresh_status_label(note)
        else:
            self._refresh_status_label()
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
                self._mark_unread(net_id, None)

    # ------------------------------------------------------------------
    # formatting
    # ------------------------------------------------------------------

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
            # server / join / part / quit / nick / topic / kick
            line = (f'<span style="color:{_MUTED_COLOR}">[{stamp}]</span> '
                    f'<span style="color:{_SERVER_COLOR}">— {body}</span>')
        if highlighted:
            return f'<span style="background-color:#3a3018;font-weight:600">{line}</span>'
        return line

    def _is_own(self, nick: str) -> bool:
        if not self._current:
            return False
        return self._client.state.nick_equals(
            self._current[0], nick, self._client.state.nick_of(self._current[0]))

    def _append_line(self, net_id: str, channel: Optional[str], kind: str,
                     nick: str, text: str, highlighted: bool = False,
                     ts: Optional[float] = None) -> None:
        line_html = self._fmt_line(kind, nick, text, float(ts or time.time()),
                                   highlighted=highlighted)
        self._append_html(None, line_html, plain_text=text)

    def _append_html(self, _unused, line_html: str, plain_text: str = "") -> None:
        # Track the placeholder with a flag: serializing the whole document
        # (chat.toHtml) per appended line made busy channels O(n²).
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
        # Graceful room exit first: it withdraws the discovery pointer and
        # tells members the host is going away (room continuity).
        try:
            self._room.shutdown()
        except Exception:
            logger.debug("room shutdown failed", exc_info=True)
        # Persist channel lists per network (joined channels survive restarts).
        try:
            snap = self._client.status()
            for net in self._config.irc.networks:
                for nsnap in snap.get("networks", []):
                    if nsnap["id"] == net.id and nsnap["connected"]:
                        joined = [c["name"] for c in nsnap["channels"]
                                  if c["name"].startswith(CHANNEL_PREFIXES)]
                        if joined:
                            net.channels = joined
            self._save_config()
        except Exception:
            logger.debug("IRC shutdown persist failed", exc_info=True)
