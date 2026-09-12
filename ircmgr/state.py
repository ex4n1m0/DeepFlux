"""Shared chat state for the DeepFlux Room: ring buffers and nick lists.

The room controller (ircmgr/room.py) writes here from its daemon threads;
the GUI reads. All public methods take the instance lock, so readers never
observe a half-applied event. The "network/channel" shape is historical —
the room is the only writer and lives under ROOM_NET_ID.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

# Event kinds stored in the buffers.
KIND_MESSAGE = "msg"        # channel/PM chat text
KIND_ACTION = "action"      # CTCP ACTION (/me)
KIND_NOTICE = "notice"
KIND_JOIN = "join"
KIND_PART = "part"
KIND_QUIT = "quit"
KIND_KICK = "kick"
KIND_NICK = "nick"
KIND_TOPIC = "topic"
KIND_SERVER = "server"      # MOTD, numerics, connection chatter
KIND_ERROR = "error"

CHANNEL_PREFIXES = ("#", "&", "+", "!")
CASEMAPPINGS = ("ascii", "strict-rfc1459", "rfc1459")
_PREFIX_ORDER = "~&@%+"

# Pseudo-network id for the DeepFlux Room (ircmgr/room.py): the serverless
# community chat. It lives in this IRCState like a network so the buffer and
# nick-list machinery has a stable key — RoomController owns it.
ROOM_NET_ID = "dfroom"


def is_channel(target: str) -> bool:
    return target.startswith(CHANNEL_PREFIXES)


# Prebuilt translate tables: building maketrans() per call made every fold
# allocate (and the room folds per nick/channel per event — a GC trigger
# hot spot under load).
_FOLD_RFC1459 = str.maketrans({"[": "{", "]": "}", "\\": "|", "^": "~"})
_FOLD_STRICT = str.maketrans({"[": "{", "]": "}", "\\": "|"})


def irc_casefold(value: str, casemapping: str = "rfc1459") -> str:
    """Fold an IRC identifier according to the advertised case mapping."""
    if casemapping == "ascii":
        return value.lower()
    return value.lower().translate(
        _FOLD_RFC1459 if casemapping != "strict-rfc1459" else _FOLD_STRICT)


def irc_equals(left: str, right: str, casemapping: str = "rfc1459") -> bool:
    return irc_casefold(left, casemapping) == irc_casefold(right, casemapping)


def normalize_prefix(prefix: str) -> str:
    """Return all known membership prefixes in strongest-to-weakest order."""
    return "".join(symbol for symbol in _PREFIX_ORDER if symbol in prefix)


@dataclass
class ChatMessage:
    ts: float
    kind: str
    nick: str = ""          # source nick ("" for server messages)
    text: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    account: str = ""
    away: Optional[bool] = None
    tags: Dict[str, Optional[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.event_id, "ts": self.ts, "kind": self.kind,
            "nick": self.nick, "text": self.text, "account": self.account,
            "away": self.away, "tags": dict(self.tags),
        }


@dataclass
class ChannelState:
    name: str
    topic: str = ""
    # nick -> membership prefixes ("" / "@" / "@+" / etc.), insertion-ordered
    nicks: Dict[str, str] = field(default_factory=dict)
    buffer: Deque[ChatMessage] = field(default_factory=deque)


@dataclass
class NetworkState:
    id: str
    host: str = ""
    port: int = 6697
    tls: bool = True
    nick: str = ""
    connected: bool = False
    connecting: bool = False
    casemapping: str = "rfc1459"
    channels: Dict[str, ChannelState] = field(default_factory=dict)
    server_buffer: Deque[ChatMessage] = field(default_factory=deque)
    # account-notify / away-notify metadata keyed by last-seen nick.
    accounts: Dict[str, str] = field(default_factory=dict)
    away: Dict[str, bool] = field(default_factory=dict)


class IRCState:
    """Thread-safe container for everything the client knows."""

    def __init__(self, buffer_lines: int = 500) -> None:
        self.buffer_lines = max(50, buffer_lines)
        self._networks: Dict[str, NetworkState] = {}
        self._lock = threading.RLock()

    def _channel_locked(self, net: NetworkState, channel: str) -> Optional[ChannelState]:
        folded = irc_casefold(channel, net.casemapping)
        return next((ch for ch in net.channels.values()
                     if irc_casefold(ch.name, net.casemapping) == folded), None)

    @staticmethod
    def _nick_key(nicks: Dict[str, Any], nick: str, casemapping: str) -> Optional[str]:
        folded = irc_casefold(nick, casemapping)
        return next((known for known in nicks
                     if irc_casefold(known, casemapping) == folded), None)

    # -- network lifecycle -------------------------------------------------

    def ensure_network(self, net_id: str, host: str = "", port: int = 6697,
                       tls: bool = True) -> NetworkState:
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                net = NetworkState(id=net_id)
                self._networks[net_id] = net
            if host:
                net.host = host
            net.port = port
            net.tls = tls
            return net

    def remove_network(self, net_id: str) -> None:
        with self._lock:
            self._networks.pop(net_id, None)

    def set_connected(self, net_id: str, connected: bool, nick: str = "") -> None:
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return
            net.connected = connected
            net.connecting = False
            if nick:
                net.nick = nick
            if not connected:
                for ch in net.channels.values():
                    ch.nicks.clear()
                net.accounts.clear()
                net.away.clear()

    def set_connecting(self, net_id: str, flag: bool) -> None:
        with self._lock:
            net = self._networks.get(net_id)
            if net:
                net.connecting = flag

    def casemapping_of(self, net_id: str) -> str:
        with self._lock:
            net = self._networks.get(net_id)
            return net.casemapping if net else "rfc1459"

    def identifiers_equal(self, net_id: str, left: str, right: str) -> bool:
        return irc_equals(left, right, self.casemapping_of(net_id))

    # -- channels ----------------------------------------------------------

    def ensure_channel(self, net_id: str, channel: str) -> Optional[ChannelState]:
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return None
            ch = self._channel_locked(net, channel)
            if ch is None:
                ch = ChannelState(name=channel)
                ch.buffer = deque(maxlen=self.buffer_lines)
                net.channels[channel] = ch
            return ch

    def drop_channel(self, net_id: str, channel: str) -> None:
        with self._lock:
            net = self._networks.get(net_id)
            if net:
                ch = self._channel_locked(net, channel)
                if ch:
                    net.channels.pop(ch.name, None)

    def set_topic(self, net_id: str, channel: str, topic: str) -> None:
        with self._lock:
            ch = self.ensure_channel(net_id, channel)
            if ch:
                ch.topic = topic

    def set_nicks(self, net_id: str, channel: str, nicks: Dict[str, str]) -> None:
        with self._lock:
            ch = self.ensure_channel(net_id, channel)
            net = self._networks.get(net_id)
            if ch is None or net is None:
                return
            merged: Dict[str, str] = {}
            for nick, prefix in nicks.items():
                key = self._nick_key(merged, nick, net.casemapping)
                if key is None:
                    merged[nick] = normalize_prefix(prefix)
                else:
                    merged[key] = normalize_prefix(merged[key] + prefix)
            ch.nicks = merged

    def add_nick(self, net_id: str, channel: str, nick: str) -> None:
        with self._lock:
            ch = self.ensure_channel(net_id, channel)
            net = self._networks.get(net_id)
            if ch is not None and net is not None:
                key = self._nick_key(ch.nicks, nick, net.casemapping)
                if key is None:
                    ch.nicks[nick] = ""

    def remove_nick(self, net_id: str, nick: str, channel: Optional[str] = None) -> None:
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return
            selected = self._channel_locked(net, channel) if channel else None
            targets = [selected] if selected else list(net.channels.values())
            for ch in targets:
                if ch is None:
                    continue
                key = self._nick_key(ch.nicks, nick, net.casemapping)
                if key is not None:
                    ch.nicks.pop(key, None)
            if channel is None:
                account_key = self._nick_key(net.accounts, nick, net.casemapping)
                away_key = self._nick_key(net.away, nick, net.casemapping)
                if account_key is not None:
                    net.accounts.pop(account_key, None)
                if away_key is not None:
                    net.away.pop(away_key, None)

    def rename_nick(self, net_id: str, old: str, new: str) -> None:
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return
            for ch in net.channels.values():
                key = self._nick_key(ch.nicks, old, net.casemapping)
                if key is not None:
                    prefix = ch.nicks.pop(key)
                    new_key = self._nick_key(ch.nicks, new, net.casemapping)
                    if new_key is None:
                        ch.nicks[new] = prefix
                    else:
                        ch.nicks[new_key] = normalize_prefix(ch.nicks[new_key] + prefix)
            account_key = self._nick_key(net.accounts, old, net.casemapping)
            if account_key is not None:
                net.accounts[new] = net.accounts.pop(account_key)
            away_key = self._nick_key(net.away, old, net.casemapping)
            if away_key is not None:
                net.away[new] = net.away.pop(away_key)

    def set_user_metadata(self, net_id: str, nick: str, account: Optional[str] = None,
                          away: Optional[bool] = None) -> None:
        with self._lock:
            net = self._networks.get(net_id)
            if net is None or not nick:
                return
            if account is not None:
                key = self._nick_key(net.accounts, nick, net.casemapping)
                if account and account != "*":
                    net.accounts[key or nick] = account
                elif key is not None:
                    net.accounts.pop(key, None)
            if away is not None:
                key = self._nick_key(net.away, nick, net.casemapping)
                net.away[key or nick] = bool(away)

    def user_metadata(self, net_id: str, nick: str) -> Dict[str, Any]:
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return {"account": "", "away": None}
            account_key = self._nick_key(net.accounts, nick, net.casemapping)
            away_key = self._nick_key(net.away, nick, net.casemapping)
            return {
                "account": net.accounts.get(account_key, "") if account_key else "",
                "away": net.away.get(away_key) if away_key else None,
            }

    # -- messages ----------------------------------------------------------

    def record(self, net_id: str, msg: ChatMessage, channel: Optional[str] = None) -> None:
        """Append a message to a channel buffer (or the server buffer)."""
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return
            if channel:
                ch = self.ensure_channel(net_id, channel)
                if ch is not None:
                    ch.buffer.append(msg)
            else:
                if net.server_buffer.maxlen != self.buffer_lines:
                    net.server_buffer = deque(net.server_buffer, maxlen=self.buffer_lines)
                net.server_buffer.append(msg)

    def get_messages(self, net_id: str, channel: Optional[str] = None,
                     limit: int = 50, since: float = 0.0) -> List[ChatMessage]:
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return []
            if channel:
                ch = self._channel_locked(net, channel)
                buf = list(ch.buffer) if ch else []
            else:
                buf = list(net.server_buffer)
        msgs = [m for m in buf if m.ts >= since]
        return msgs[-limit:] if limit else msgs

    def search(self, query: str, net_id: Optional[str] = None,
               channel: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        """Case-insensitive substring search across buffers. Returns flat hits."""
        q = query.lower()
        hits: List[Dict[str, str]] = []
        with self._lock:
            nets = [self._networks[net_id]] if net_id in self._networks else (
                [] if net_id else list(self._networks.values()))
            snapshots = []
            for net in nets:
                if channel:
                    selected = self._channel_locked(net, channel)
                    chans = [selected] if selected else []
                else:
                    chans = list(net.channels.values())
                for ch in chans:
                    snapshots.append((net.id, ch.name, list(ch.buffer)))
        for nid, chname, buf in snapshots:
            for msg in buf:
                if q in msg.text.lower() or (msg.nick and q in msg.nick.lower()):
                    hit = msg.to_dict()
                    hit.update({"network": nid, "channel": chname})
                    hits.append(hit)
                    if len(hits) >= limit:
                        return hits
        return hits

    # -- snapshots for agent tools / GUI -----------------------------------

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "networks": [
                    {
                        "id": net.id,
                        "host": net.host,
                        "port": net.port,
                        "tls": net.tls,
                        "nick": net.nick,
                        "connected": net.connected,
                        "connecting": net.connecting,
                        "casemapping": net.casemapping,
                        "channels": [
                            {"name": channel.name, "topic": channel.topic,
                             "users": len(channel.nicks), "buffered": len(channel.buffer)}
                            for channel in net.channels.values()
                        ],
                    }
                    for net in self._networks.values()
                ]
            }

    def network_ids(self) -> List[str]:
        with self._lock:
            return list(self._networks.keys())

    def network_connected(self, net_id: str) -> bool:
        """Cheap single-network connected check (no snapshot build)."""
        with self._lock:
            net = self._networks.get(net_id)
            return bool(net and net.connected)

    def network_link_state(self, net_id: str) -> str:
        """connected / connecting / offline for one network (no snapshot)."""
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return "offline"
            if net.connected:
                return "connected"
            if net.connecting:
                return "connecting"
            return "offline"

    def channel_names(self, net_id: str) -> List[str]:
        """Names of every open channel/query on a network (no snapshot build)."""
        with self._lock:
            net = self._networks.get(net_id)
            return [ch.name for ch in net.channels.values()] if net else []

    def channels_of_nick(self, net_id: str, nick: str) -> List[str]:
        """Channels where a nick is present (no snapshot build). Used by hot
        per-event paths (QUIT/NICK storms during netsplits)."""
        with self._lock:
            net = self._networks.get(net_id)
            if not net:
                return []
            return [ch.name for ch in net.channels.values()
                    if self._nick_key(ch.nicks, nick, net.casemapping) is not None]

    def clear_buffer(self, net_id: str, channel: Optional[str]) -> None:
        """Drop a channel's (or the server) buffered transcript, keep membership."""
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return
            if channel:
                ch = self._channel_locked(net, channel)
                if ch is not None:
                    ch.buffer.clear()
            else:
                net.server_buffer.clear()

    def nick_of(self, net_id: str) -> str:
        with self._lock:
            net = self._networks.get(net_id)
            return net.nick if net else ""

    def nick_equals(self, net_id: str, left: str, right: str) -> bool:
        return self.identifiers_equal(net_id, left, right)

    def nicks_of(self, net_id: str, channel: str) -> Dict[str, str]:
        with self._lock:
            net = self._networks.get(net_id)
            if not net:
                return {}
            ch = self._channel_locked(net, channel)
            return dict(ch.nicks) if ch else {}

    def topic_of(self, net_id: str, channel: str) -> str:
        with self._lock:
            net = self._networks.get(net_id)
            if not net:
                return ""
            ch = self._channel_locked(net, channel)
            return ch.topic if ch else ""


def now_ts() -> float:
    return time.time()
