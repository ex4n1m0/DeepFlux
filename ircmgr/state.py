"""Shared IRC state: per-network/channel ring buffers and nick lists.

The network thread (IRCClientCore) writes here; the GUI and the agent tools
read. All public methods take the instance lock, so readers never observe a
half-applied event.
"""
from __future__ import annotations

import threading
import time
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


def is_channel(target: str) -> bool:
    return target.startswith(CHANNEL_PREFIXES)


@dataclass
class ChatMessage:
    ts: float
    kind: str
    nick: str = ""          # source nick ("" for server messages)
    text: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"ts": self.ts, "kind": self.kind, "nick": self.nick, "text": self.text}


@dataclass
class ChannelState:
    name: str
    topic: str = ""
    # nick -> membership prefix ("" / "@" / "+"), insertion-ordered
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
    channels: Dict[str, ChannelState] = field(default_factory=dict)
    server_buffer: Deque[ChatMessage] = field(default_factory=deque)
    # Last LIST result: [{"channel": str, "users": int, "topic": str}]
    chanlist: List[Dict[str, Any]] = field(default_factory=list)
    chanlist_ts: float = 0.0


class IRCState:
    """Thread-safe container for everything the client knows."""

    def __init__(self, buffer_lines: int = 500) -> None:
        self.buffer_lines = max(50, buffer_lines)
        self._networks: Dict[str, NetworkState] = {}
        self._lock = threading.RLock()

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

    def set_connecting(self, net_id: str, flag: bool) -> None:
        with self._lock:
            net = self._networks.get(net_id)
            if net:
                net.connecting = flag

    # -- channels ----------------------------------------------------------

    def ensure_channel(self, net_id: str, channel: str) -> Optional[ChannelState]:
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return None
            ch = net.channels.get(channel)
            if ch is None:
                ch = ChannelState(name=channel)
                ch.buffer = deque(maxlen=self.buffer_lines)
                net.channels[channel] = ch
            return ch

    def drop_channel(self, net_id: str, channel: str) -> None:
        with self._lock:
            net = self._networks.get(net_id)
            if net:
                net.channels.pop(channel, None)

    def set_topic(self, net_id: str, channel: str, topic: str) -> None:
        with self._lock:
            ch = self.ensure_channel(net_id, channel)
            if ch:
                ch.topic = topic

    def set_chanlist(self, net_id: str, rows: List[Dict[str, Any]]) -> None:
        with self._lock:
            net = self._networks.get(net_id)
            if net:
                net.chanlist = rows
                net.chanlist_ts = time.time()

    def chanlist_of(self, net_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            net = self._networks.get(net_id)
            return list(net.chanlist) if net else []

    def chanlist_ts(self, net_id: str) -> float:
        with self._lock:
            net = self._networks.get(net_id)
            return net.chanlist_ts if net else 0.0

    def set_nicks(self, net_id: str, channel: str, nicks: Dict[str, str]) -> None:
        with self._lock:
            ch = self.ensure_channel(net_id, channel)
            if ch is not None:
                ch.nicks = dict(nicks)

    def add_nick(self, net_id: str, channel: str, nick: str) -> None:
        with self._lock:
            ch = self.ensure_channel(net_id, channel)
            if ch is not None:
                ch.nicks[nick] = ""

    def remove_nick(self, net_id: str, nick: str, channel: Optional[str] = None) -> None:
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return
            targets = [net.channels[channel]] if channel and channel in net.channels \
                else list(net.channels.values())
            for ch in targets:
                ch.nicks.pop(nick, None)

    def rename_nick(self, net_id: str, old: str, new: str) -> None:
        with self._lock:
            net = self._networks.get(net_id)
            if net is None:
                return
            for ch in net.channels.values():
                if old in ch.nicks:
                    ch.nicks[new] = ch.nicks.pop(old)

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
                ch = net.channels.get(channel)
                buf = list(ch.buffer) if ch else []
            else:
                buf = list(net.server_buffer)
        msgs = [m for m in buf if m.ts >= since]
        return msgs[-limit:] if limit else msgs

    def search(self, query: str, net_id: Optional[str] = None,
               channel: Optional[str] = None, limit: int = 30) -> List[Dict[str, str]]:
        """Case-insensitive substring search across buffers. Returns flat hits."""
        q = query.lower()
        hits: List[Dict[str, str]] = []
        with self._lock:
            nets = [self._networks[net_id]] if net_id in self._networks else (
                [] if net_id else list(self._networks.values()))
            snapshots = []
            for net in nets:
                if channel:
                    chans = [net.channels[channel]] if channel in net.channels else []
                else:
                    chans = list(net.channels.values())
                for ch in chans:
                    snapshots.append((net.id, ch.name, list(ch.buffer)))
        for nid, chname, buf in snapshots:
            for m in buf:
                if q in m.text.lower() or (m.nick and q in m.nick.lower()):
                    hits.append({"network": nid, "channel": chname, "ts": m.ts,
                                 "kind": m.kind, "nick": m.nick, "text": m.text})
                    if len(hits) >= limit:
                        return hits
        return hits

    # -- snapshots for agent tools / GUI -----------------------------------

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "networks": [
                    {
                        "id": n.id,
                        "host": n.host,
                        "port": n.port,
                        "tls": n.tls,
                        "nick": n.nick,
                        "connected": n.connected,
                        "connecting": n.connecting,
                        "channels": [
                            {"name": c.name, "topic": c.topic,
                             "users": len(c.nicks), "buffered": len(c.buffer)}
                            for c in n.channels.values()
                        ],
                    }
                    for n in self._networks.values()
                ]
            }

    def network_ids(self) -> List[str]:
        with self._lock:
            return list(self._networks.keys())

    def nick_of(self, net_id: str) -> str:
        with self._lock:
            net = self._networks.get(net_id)
            return net.nick if net else ""

    def nicks_of(self, net_id: str, channel: str) -> Dict[str, str]:
        with self._lock:
            net = self._networks.get(net_id)
            if not net or channel not in net.channels:
                return {}
            return dict(net.channels[channel].nicks)

    def topic_of(self, net_id: str, channel: str) -> str:
        with self._lock:
            net = self._networks.get(net_id)
            if not net or channel not in net.channels:
                return ""
            return net.channels[channel].topic


def now_ts() -> float:
    return time.time()
