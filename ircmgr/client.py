"""Threaded IRC client core built on the ``irc`` (jaraco) library.

Design notes
------------
* One daemon thread owns the reactor: every IRC socket operation (connect,
  join, privmsg, ...) is marshalled into that thread through a command queue,
  so no cross-thread reactor access ever happens.
* Incoming events are normalized into plain dicts and fanned out to
  listeners (the GUI bridges them through Qt signals) and recorded into
  :class:`~ircmgr.state.IRCState` ring buffers (read by the agent tools).
* Outgoing PRIVMSGs are throttled per network (token pacing) so the client
  never gets kicked for "Excess Flood".
* PING/PONG keepalive is handled by the library itself.
"""
from __future__ import annotations

import functools
import logging
import queue
import re
import ssl
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import irc.client
import irc.connection

from config import IRCNetworkConfig
from ircmgr.state import (
    ChatMessage,
    IRCState,
    KIND_ACTION,
    KIND_ERROR,
    KIND_JOIN,
    KIND_KICK,
    KIND_MESSAGE,
    KIND_NICK,
    KIND_NOTICE,
    KIND_PART,
    KIND_QUIT,
    KIND_SERVER,
    KIND_TOPIC,
    is_channel,
)

logger = logging.getLogger(__name__)

# mIRC control codes: bold, color(+args), hex color, reset, italic, etc.
_CTRL_RE = re.compile(r"\x03(\d{1,2}(,\d{1,2})?)?|[\x02\x04\x0f\x16\x1d\x1f\x11\x1e]")
MAX_IRC_LINE = 400  # stay well under the 512-byte protocol limit


def strip_control_codes(text: str) -> str:
    return _CTRL_RE.sub("", text)


class IRCClientCore:
    """Multi-network IRC client; all public methods are thread-safe."""

    def __init__(self, irc_config) -> None:
        self._config = irc_config
        self.state = IRCState(getattr(irc_config, "buffer_lines", 500))
        self._flood_delay = max(0.5, float(getattr(irc_config, "flood_delay", 2.0)))
        self._reconnect_max = max(10, int(getattr(irc_config, "reconnect_max_seconds", 300)))

        self._cmd_q: "queue.Queue[Tuple[str, tuple]]" = queue.Queue()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.RLock()

        self._reactor: Optional[irc.client.Reactor] = None
        # net_id -> runtime dict (only touched by the network thread,
        # except manual_disconnect which is set under _lock before queueing)
        self._nets: Dict[str, Dict[str, Any]] = {}
        # ServerConnection -> net_id. The irc library's `add_global_handler`
        # registers on the REACTOR, whose handlers fire for EVERY connection's
        # events — so per-connection handlers baked with a fixed net_id would
        # cross-contaminate networks (a JOIN on libera would also be recorded
        # under iptorrents). Instead we install one set of reactor-global
        # handlers and resolve the owning network from the connection object.
        self._conn_to_net: Dict[Any, str] = {}
        self._handlers_installed = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="irc-client", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._running.clear()
        self._cmd_q.put(("quit_all", ()))
        if self._thread:
            self._thread.join(timeout=5)

    def add_listener(self, cb: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(cb)

    def _emit(self, event: Dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(event)
            except Exception:
                logger.debug("IRC listener failed", exc_info=True)

    # ------------------------------------------------------------------
    # public commands (any thread — queued onto the network thread)
    # ------------------------------------------------------------------

    def connect_network(self, net: IRCNetworkConfig) -> None:
        self.state.ensure_network(net.id, net.host, net.port, net.tls)
        self.start()
        with self._lock:
            rt = self._nets.get(net.id)
            if rt:
                rt["manual_disconnect"] = False
        self._cmd_q.put(("connect", (net,)))

    def disconnect_network(self, net_id: str, message: str = "DeepFlux") -> None:
        with self._lock:
            rt = self._nets.get(net_id)
            if rt:
                rt["manual_disconnect"] = True
        self._cmd_q.put(("disconnect", (net_id, message)))

    def join(self, net_id: str, channel: str) -> None:
        self._cmd_q.put(("join", (net_id, channel)))

    def part(self, net_id: str, channel: str) -> None:
        self._cmd_q.put(("part", (net_id, channel)))

    def send_message(self, net_id: str, target: str, text: str) -> None:
        self._cmd_q.put(("say", (net_id, target, text, False)))

    def send_action(self, net_id: str, target: str, text: str) -> None:
        self._cmd_q.put(("say", (net_id, target, text, True)))

    def send_notice(self, net_id: str, target: str, text: str) -> None:
        self._cmd_q.put(("notice", (net_id, target, text)))

    def change_nick(self, net_id: str, newnick: str) -> None:
        self._cmd_q.put(("nick", (net_id, newnick)))

    def send_raw(self, net_id: str, line: str) -> None:
        self._cmd_q.put(("raw", (net_id, line)))

    # ------------------------------------------------------------------
    # read API (agent tools / GUI)
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return self.state.snapshot()

    def get_messages(self, net_id: str, channel: Optional[str] = None,
                     limit: int = 50, since: float = 0.0) -> List[Dict[str, Any]]:
        return [m.to_dict() for m in self.state.get_messages(net_id, channel, limit, since)]

    def search_messages(self, query: str, net_id: Optional[str] = None,
                        channel: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        return self.state.search(query, net_id, channel, limit)

    def resolve_network(self, net_id: str = "", channel: str = "") -> Tuple[Optional[str], Optional[str]]:
        """Pick a network when the caller didn't name one. Returns (id, error)."""
        snap = self.state.snapshot()["networks"]
        nets = {n["id"]: n for n in snap}
        if net_id:
            if net_id not in nets:
                return None, f"Unknown network '{net_id}'. Known: {sorted(nets)}"
            return net_id, None
        if channel:
            matches = [n["id"] for n in snap
                       if any(c["name"].lower() == channel.lower() for c in n["channels"])]
            if len(matches) == 1:
                return matches[0], None
            if len(matches) > 1:
                return None, f"Channel {channel} is open on several networks {matches} — pass network=."
        connected = [n["id"] for n in snap if n["connected"] or n["connecting"]]
        if len(connected) == 1:
            return connected[0], None
        if not connected:
            return None, "No IRC network is connected. Connect from the IRC tab first."
        return None, f"Several networks are connected {connected} — pass network=. "

    # ------------------------------------------------------------------
    # network thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        self._reactor = irc.client.Reactor()
        logger.info("IRC client thread started")
        while self._running.is_set():
            self._drain_commands()
            self._flush_outboxes()
            self._check_reconnects()
            self._check_pending_lists()
            try:
                self._reactor.process_once(timeout=0.2)
            except Exception:
                logger.exception("reactor.process_once failed")
                time.sleep(0.2)
        # Graceful QUIT on the way out.
        try:
            self._reactor.disconnect_all("DeepFlux closing")
            self._reactor.process_once(timeout=0.5)
        except Exception:
            pass
        logger.info("IRC client thread stopped")

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd, args = self._cmd_q.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle_command(cmd, args)
            except Exception:
                logger.exception("IRC command %s failed", cmd)

    def _handle_command(self, cmd: str, args: tuple) -> None:
        if cmd == "connect":
            self._do_connect(args[0])
        elif cmd == "disconnect":
            self._do_disconnect(args[0], args[1])
        elif cmd == "quit_all":
            self._running.clear()
        elif cmd == "join":
            self._do_join(*args)
        elif cmd == "part":
            self._do_part(*args)
        elif cmd == "say":
            net_id, target, text, is_action = args
            rt = self._nets.get(net_id)
            if rt and rt.get("conn") and rt["conn"].is_connected():
                for chunk in self._split(text):
                    rt["outbox"].append((target, chunk, is_action))
            else:
                self._record(net_id, KIND_ERROR, f"Not connected — message to {target} dropped.")
        elif cmd == "notice":
            net_id, target, text = args
            rt = self._nets.get(net_id)
            if rt and rt.get("conn") and rt["conn"].is_connected():
                rt["conn"].notice(target, text[:MAX_IRC_LINE])
        elif cmd == "nick":
            net_id, newnick = args
            rt = self._nets.get(net_id)
            if rt and rt.get("conn") and rt["conn"].is_connected():
                rt["conn"].nick(newnick)
        elif cmd == "raw":
            net_id, line = args
            rt = self._nets.get(net_id)
            if rt and rt.get("conn") and rt["conn"].is_connected():
                rt["conn"].send_raw(line[:MAX_IRC_LINE])

    # -- connection management --------------------------------------------

    def _do_connect(self, net: IRCNetworkConfig) -> None:
        rt = self._nets.get(net.id)
        if rt and rt.get("conn") and rt["conn"].is_connected():
            return  # already connected
        self.state.ensure_network(net.id, net.host, net.port, net.tls)
        self.state.set_connecting(net.id, True)
        self._emit({"type": "state", "network": net.id, "state": "connecting"})

        connect_factory = irc.connection.Factory()
        if net.tls:
            context = ssl.create_default_context()
            wrapper = functools.partial(context.wrap_socket, server_hostname=net.host)
            connect_factory = irc.connection.Factory(wrapper=wrapper)

        try:
            conn = self._reactor.server()
            # Map this connection to its network BEFORE we connect so events
            # (processed later by the reactor loop) resolve to the right net.
            # Drop any stale mapping for a previous connection of this net.
            old = rt.get("conn") if rt else None
            if old is not None:
                self._conn_to_net.pop(old, None)
            self._conn_to_net[conn] = net.id
            self._install_handlers()
            sasl_login = net.sasl_account or None
            sasl_password = net.sasl_password if sasl_login else None
            conn.connect(
                net.host, net.port, net.nick,
                password=sasl_password or (net.password or None),
                username=net.username or net.nick,
                ircname=net.realname or net.nick,
                connect_factory=connect_factory,
                sasl_login=sasl_login,
            )
        except Exception as exc:
            logger.warning("IRC connect to %s failed: %s", net.host, exc)
            self.state.set_connecting(net.id, False)
            self._record(net.id, KIND_ERROR, f"Connect failed: {exc}")
            self._emit({"type": "state", "network": net.id, "state": "error",
                        "detail": str(exc)})
            self._schedule_reconnect(net.id)
            return

        self._nets[net.id] = {
            "cfg": net,
            "conn": conn,
            "outbox": deque(),
            "next_send": 0.0,
            "reconnect_at": None,
            "backoff": 2.0,
            "manual_disconnect": rt.get("manual_disconnect", False) if rt else False,
            "welcomed": False,
            "nick_try": 0,
            # Auto-/LIST retry: channelless networks request the channel
            # directory on connect and retry every 10s until it arrives
            # (some servers throttle LIST for ~60s after connect).
            "auto_list_pending": False,
            "auto_list_at": None,
        }

    def _do_disconnect(self, net_id: str, message: str) -> None:
        rt = self._nets.get(net_id)
        if not rt:
            return
        rt["manual_disconnect"] = True
        rt["reconnect_at"] = None
        conn = rt.get("conn")
        if conn and conn.is_connected():
            try:
                conn.quit(message)
                conn.close()
            except Exception:
                pass
        self._on_conn_down(net_id)

    def _schedule_reconnect(self, net_id: str) -> None:
        rt = self._nets.get(net_id)
        if not rt or rt.get("manual_disconnect") or not self._running.is_set():
            return
        delay = min(rt.get("backoff", 2.0), self._reconnect_max)
        rt["reconnect_at"] = time.monotonic() + delay
        rt["backoff"] = min(delay * 2, self._reconnect_max)
        self._record(net_id, KIND_SERVER, f"Reconnecting in {int(delay)}s…")

    def _check_reconnects(self) -> None:
        now = time.monotonic()
        for net_id, rt in list(self._nets.items()):
            at = rt.get("reconnect_at")
            if at is None or now < at or rt.get("manual_disconnect"):
                continue
            conn = rt.get("conn")
            if conn and conn.is_connected():
                rt["reconnect_at"] = None
                continue
            rt["reconnect_at"] = None
            self._do_connect(rt["cfg"])

    def _check_pending_lists(self) -> None:
        """Retry /LIST for channelless networks that haven't received a list yet."""
        now = time.monotonic()
        for net_id, rt in list(self._nets.items()):
            if not rt.get("auto_list_pending"):
                continue
            at = rt.get("auto_list_at")
            if at is None or now < at:
                continue
            conn = rt.get("conn")
            if not conn or not conn.is_connected():
                rt["auto_list_pending"] = False
                continue
            conn.send_raw("LIST")
            rt["auto_list_at"] = now + 10.0  # retry interval

    def _on_conn_down(self, net_id: str) -> None:
        self.state.set_connected(net_id, False)
        rt = self._nets.get(net_id)
        if rt:
            rt["welcomed"] = False
            rt["auto_list_pending"] = False
            rt["auto_list_at"] = None
        self._emit({"type": "state", "network": net_id, "state": "disconnected"})

    # -- outgoing pacing ----------------------------------------------------

    @staticmethod
    def _split(text: str) -> List[str]:
        """Split long text so each IRC line stays under the byte limit."""
        text = text.replace("\r", " ").replace("\n", " ")
        if len(text) <= MAX_IRC_LINE:
            return [text]
        chunks, cur = [], ""
        for word in text.split(" "):
            if cur and len(cur) + len(word) + 1 > MAX_IRC_LINE:
                chunks.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        if cur:
            chunks.append(cur)
        return chunks or [text[:MAX_IRC_LINE]]

    def _flush_outboxes(self) -> None:
        now = time.monotonic()
        for rt in self._nets.values():
            outbox: Deque = rt.get("outbox") or ()
            conn = rt.get("conn")
            if not outbox or not conn or not conn.is_connected():
                continue
            if now < rt.get("next_send", 0.0):
                continue
            target, text, is_action = outbox.popleft()
            try:
                if is_action:
                    conn.action(target, text)
                else:
                    conn.privmsg(target, text)
                rt["next_send"] = now + self._flood_delay
            except Exception:
                logger.exception("IRC send failed")

    # -- channel tracking ----------------------------------------------------

    def _do_join(self, net_id: str, channel: str) -> None:
        rt = self._nets.get(net_id)
        if not rt or not rt.get("conn") or not rt["conn"].is_connected():
            self._record(net_id, KIND_ERROR, "Not connected — cannot join.")
            return
        if not is_channel(channel):
            channel = "#" + channel
        cfg = rt["cfg"]
        if channel not in cfg.channels:
            cfg.channels.append(channel)
        rt["conn"].join(channel)

    def _do_part(self, net_id: str, channel: str) -> None:
        rt = self._nets.get(net_id)
        if not rt or not rt.get("conn") or not rt["conn"].is_connected():
            return
        cfg = rt["cfg"]
        if channel in cfg.channels:
            cfg.channels.remove(channel)
        rt["conn"].part(channel)
        self.state.drop_channel(net_id, channel)
        self._emit({"type": "parted", "network": net_id, "channel": channel})

    # ------------------------------------------------------------------
    # event handlers (network thread)
    # ------------------------------------------------------------------

    def _install_handlers(self) -> None:
        """Register one set of reactor-global handlers (once).

        The irc library fires global handlers for every connection's events,
        so we register them a single time and resolve the owning network from
        the connection object inside ``_dispatch`` (see ``_conn_to_net``).
        """
        if self._handlers_installed or self._reactor is None:
            return
        self._handlers_installed = True
        for name in ("welcome", "nicknameinuse", "pubmsg", "privmsg", "action",
                     "pubnotice", "privnotice", "join", "part", "quit", "kick",
                     "nick", "topic", "currenttopic", "namreply", "endofnames",
                     "disconnect", "error", "motd", "motdstart", "endofmotd",
                     "nomotd", "featurelist", "liststart", "list", "listend",
                     "bannedfromchan", "inviteonlychan",
                     "badchannelkey", "channelisfull", "nosuchchannel",
                     "nosuchnick", "cannotsendtochan", "ctcp", "ctcpreply"):
            self._reactor.add_global_handler(
                name, functools.partial(self._dispatch, name))

    def _dispatch(self, name: str,
                  conn: "irc.client.ServerConnection", event: "irc.client.Event") -> None:
        net_id = self._conn_to_net.get(conn)
        if net_id is None:
            return  # event from an unknown/unmapped connection — ignore
        try:
            handler = getattr(self, f"_on_{name}", None)
            if handler:
                handler(net_id, conn, event)
            elif name in ("motd", "motdstart", "endofmotd", "nomotd"):
                text = event.arguments[-1] if event.arguments else ""
                self._record(net_id, KIND_SERVER, strip_control_codes(text))
        except Exception:
            logger.exception("IRC handler %s failed", name)

    # -- individual events ---------------------------------------------------

    def _src_nick(self, event: "irc.client.Event") -> str:
        try:
            return irc.client.NickMask(event.source).nick
        except Exception:
            return str(event.source or "")

    def _record(self, net_id: str, kind: str, text: str, nick: str = "",
                channel: Optional[str] = None) -> None:
        msg = ChatMessage(ts=time.time(), kind=kind, nick=nick, text=text)
        self.state.record(net_id, msg, channel)

    def _on_welcome(self, net_id, conn, event) -> None:
        nick = event.target or ""
        rt = self._nets.get(net_id)
        if rt:
            rt["welcomed"] = True
            rt["backoff"] = 2.0
            cfg = rt["cfg"]
            for ch in list(cfg.channels):
                conn.join(ch)
            # No configured channels → auto-request /LIST so the user can
            # browse and pick channels. Retried every 10s until it arrives
            # (some servers throttle LIST for ~60s after connect).
            if not cfg.channels:
                rt["auto_list_pending"] = True
                rt["auto_list_at"] = time.monotonic()
                self._record(net_id, KIND_SERVER,
                             "Requesting channel list (retrying every 10s until it arrives)…")
        self.state.set_connected(net_id, True, nick=nick)
        self._record(net_id, KIND_SERVER, f"Connected as {nick}")
        self._emit({"type": "state", "network": net_id, "state": "connected",
                    "nick": nick})

    def _on_nicknameinuse(self, net_id, conn, event) -> None:
        rt = self._nets.get(net_id)
        if rt and not rt.get("welcomed"):
            rt["nick_try"] = rt.get("nick_try", 0) + 1
            base = rt["cfg"].nick
            # Try base nick first, then DeepFluxUser01, DeepFluxUser02, …
            # (zero-padded two-digit suffix so nicks sort cleanly in user lists).
            fallback = f"{base}{rt['nick_try']:02d}" if rt["nick_try"] >= 1 else base
            self._record(net_id, KIND_SERVER, f"Nick in use — trying {fallback}")
            conn.nick(fallback)
        else:
            self._record(net_id, KIND_ERROR, "Nickname already in use.")

    def _on_pubmsg(self, net_id, conn, event) -> None:
        channel = event.target
        nick = self._src_nick(event)
        text = strip_control_codes(event.arguments[0] if event.arguments else "")
        self._record(net_id, KIND_MESSAGE, text, nick=nick, channel=channel)
        self._emit({"type": "message", "network": net_id, "channel": channel,
                    "nick": nick, "text": text})

    def _on_privmsg(self, net_id, conn, event) -> None:
        nick = self._src_nick(event)
        text = strip_control_codes(event.arguments[0] if event.arguments else "")
        self._record(net_id, KIND_MESSAGE, text, nick=nick, channel=nick)
        self._ensure_query(net_id, nick)
        self._emit({"type": "message", "network": net_id, "channel": nick,
                    "nick": nick, "text": text, "private": True})

    def _on_action(self, net_id, conn, event) -> None:
        target = event.target if is_channel(event.target) else self._src_nick(event)
        nick = self._src_nick(event)
        text = strip_control_codes(event.arguments[0] if event.arguments else "")
        if not is_channel(event.target):
            self._ensure_query(net_id, nick)
        self._record(net_id, KIND_ACTION, text, nick=nick, channel=target)
        self._emit({"type": "action", "network": net_id, "channel": target,
                    "nick": nick, "text": text})

    def _on_pubnotice(self, net_id, conn, event) -> None:
        nick = self._src_nick(event)
        text = strip_control_codes(event.arguments[0] if event.arguments else "")
        target = event.target if is_channel(event.target) else None
        self._record(net_id, KIND_NOTICE, text, nick=nick, channel=target)
        self._emit({"type": "notice", "network": net_id, "channel": target,
                    "nick": nick, "text": text})

    def _on_privnotice(self, net_id, conn, event) -> None:
        self._on_pubnotice(net_id, conn, event)

    def _on_join(self, net_id, conn, event) -> None:
        channel = event.target
        nick = self._src_nick(event)
        mine = self.state.nick_of(net_id)
        self.state.add_nick(net_id, channel, nick)
        self.state.ensure_channel(net_id, channel)
        self._record(net_id, KIND_JOIN, f"{nick} joined", nick=nick, channel=channel)
        self._emit({"type": "join", "network": net_id, "channel": channel,
                    "nick": nick, "own": nick == mine})

    def _on_part(self, net_id, conn, event) -> None:
        channel = event.target
        nick = self._src_nick(event)
        reason = strip_control_codes(event.arguments[0]) if event.arguments else ""
        self.state.remove_nick(net_id, nick, channel)
        if nick == self.state.nick_of(net_id):
            self.state.drop_channel(net_id, channel)
            self._emit({"type": "parted", "network": net_id, "channel": channel})
        self._record(net_id, KIND_PART, f"{nick} left {reason}".strip(), nick=nick,
                     channel=channel)
        self._emit({"type": "part", "network": net_id, "channel": channel,
                    "nick": nick})

    def _on_quit(self, net_id, conn, event) -> None:
        nick = self._src_nick(event)
        reason = strip_control_codes(event.arguments[0]) if event.arguments else ""
        snap_channels = []
        net_snap = {n["id"]: n for n in self.state.snapshot()["networks"]}.get(net_id)
        if net_snap:
            snap_channels = [c["name"] for c in net_snap["channels"]
                             if nick in self.state.nicks_of(net_id, c["name"])]
        self.state.remove_nick(net_id, nick)
        for ch in snap_channels:
            self._record(net_id, KIND_QUIT, f"{nick} quit {reason}".strip(),
                         nick=nick, channel=ch)
        self._emit({"type": "quit", "network": net_id, "nick": nick})

    def _on_kick(self, net_id, conn, event) -> None:
        channel = event.target
        kicked = event.arguments[0] if event.arguments else ""
        by = self._src_nick(event)
        self.state.remove_nick(net_id, kicked, channel)
        if kicked == self.state.nick_of(net_id):
            self.state.drop_channel(net_id, channel)
            self._emit({"type": "parted", "network": net_id, "channel": channel})
        self._record(net_id, KIND_KICK, f"{kicked} was kicked by {by}",
                     nick=by, channel=channel)
        self._emit({"type": "kick", "network": net_id, "channel": channel,
                    "nick": kicked, "by": by})

    def _on_nick(self, net_id, conn, event) -> None:
        old = self._src_nick(event)
        new = event.target or ""
        self.state.rename_nick(net_id, old, new)
        if old == self.state.nick_of(net_id):
            self.state.set_connected(net_id, True, nick=new)
        net_snap = {n["id"]: n for n in self.state.snapshot()["networks"]}.get(net_id)
        if net_snap:
            for c in net_snap["channels"]:
                if new in self.state.nicks_of(net_id, c["name"]):
                    self._record(net_id, KIND_NICK, f"{old} is now {new}",
                                 channel=c["name"])
        self._emit({"type": "nick", "network": net_id, "old": old, "new": new})

    def _on_topic(self, net_id, conn, event) -> None:
        channel = event.target
        topic = strip_control_codes(event.arguments[0]) if event.arguments else ""
        self.state.set_topic(net_id, channel, topic)
        self._record(net_id, KIND_TOPIC, f"Topic: {topic}", channel=channel)
        self._emit({"type": "topic", "network": net_id, "channel": channel,
                    "topic": topic})

    def _on_currenttopic(self, net_id, conn, event) -> None:
        # RPL_TOPIC (332): arguments = [channel, topic]
        if len(event.arguments) >= 2:
            channel, topic = event.arguments[0], strip_control_codes(event.arguments[1])
            self.state.set_topic(net_id, channel, topic)
            self._emit({"type": "topic", "network": net_id, "channel": channel,
                        "topic": topic})

    def _on_namreply(self, net_id, conn, event) -> None:
        # RPL_NAMREPLY (353): arguments = [channel_type, channel, "nick nick ..."]
        if len(event.arguments) < 2:
            return
        channel = event.arguments[-2]
        names = event.arguments[-1].split()
        nicks: Dict[str, str] = {}
        for raw in names:
            prefix = raw[0] if raw and raw[0] in "@+%&~" else ""
            nick = raw[1:] if prefix else raw
            if nick:
                nicks[nick] = prefix
        existing = self.state.nicks_of(net_id, channel)
        existing.update(nicks)
        self.state.set_nicks(net_id, channel, existing)

    def _on_endofnames(self, net_id, conn, event) -> None:
        channel = event.arguments[0] if event.arguments else ""
        self._emit({"type": "names", "network": net_id, "channel": channel,
                    "nicks": self.state.nicks_of(net_id, channel)})

    # -- LIST (channel directory) -------------------------------------------

    def _on_liststart(self, net_id, conn, event) -> None:
        rt = self._nets.get(net_id)
        if rt is not None:
            rt["pending_list"] = []

    def _on_list(self, net_id, conn, event) -> None:
        # RPL_LIST (322): arguments = [channel, visible_count, topic]
        rt = self._nets.get(net_id)
        if rt is None:
            return
        args = event.arguments or []
        if not args:
            return
        try:
            users = int(args[1])
        except (IndexError, ValueError):
            users = 0
        rt.setdefault("pending_list", []).append({
            "channel": args[0],
            "users": users,
            "topic": strip_control_codes(args[2]) if len(args) > 2 else "",
        })

    def _on_listend(self, net_id, conn, event) -> None:
        rt = self._nets.get(net_id)
        rows = rt.pop("pending_list", []) if rt else []
        rows.sort(key=lambda r: -r["users"])
        self.state.set_chanlist(net_id, rows)
        # Auto-/LIST retry complete — stop retrying.
        if rt:
            rt["auto_list_pending"] = False
            rt["auto_list_at"] = None
        self._record(net_id, KIND_SERVER,
                     f"Channel list received: {len(rows)} channels")
        self._emit({"type": "chanlist", "network": net_id, "channels": rows})

    def _on_disconnect(self, net_id, conn, event) -> None:
        self._record(net_id, KIND_SERVER, "Disconnected")
        self._on_conn_down(net_id)
        self._schedule_reconnect(net_id)

    def _on_error(self, net_id, conn, event) -> None:
        text = event.arguments[0] if event.arguments else "connection error"
        self._record(net_id, KIND_ERROR, text)
        self._emit({"type": "state", "network": net_id, "state": "error",
                    "detail": text})

    def _on_ctcp(self, net_id, conn, event) -> None:
        # Answer VERSION/PING/TIME politely — some networks k-line silent clients.
        args = event.arguments or []
        command = args[0].upper() if args else ""
        nick = self._src_nick(event)
        if command == "VERSION":
            conn.ctcp_reply(nick, "VERSION DeepFlux IRC")
        elif command == "PING" and len(args) > 1:
            conn.ctcp_reply(nick, f"PING {args[1]}")
        elif command == "TIME":
            conn.ctcp_reply(nick, time.strftime("TIME %a %b %d %H:%M:%S %Y"))
        elif command == "DCC":
            # File/chat offers: surface to the user, never auto-accept.
            detail = " ".join(args[1:])[:200]
            self._record(net_id, KIND_NOTICE, f"DCC offer from {nick}: {detail}")
            self._emit({"type": "dcc_offer", "network": net_id, "nick": nick,
                        "detail": detail})

    def _on_ctcpreply(self, net_id, conn, event) -> None:
        nick = self._src_nick(event)
        text = " ".join(event.arguments or [])
        self._record(net_id, KIND_NOTICE, f"CTCP reply from {nick}: {strip_control_codes(text)}")

    # -- generic error numerics worth surfacing -------------------------------

    def _on_bannedfromchan(self, net_id, conn, event):
        self._record(net_id, KIND_ERROR, f"Banned from {self._arg(event)}: {self._arg(event, 1)}")

    def _on_inviteonlychan(self, net_id, conn, event):
        self._record(net_id, KIND_ERROR, f"{self._arg(event)} is invite-only")

    def _on_badchannelkey(self, net_id, conn, event):
        self._record(net_id, KIND_ERROR, f"{self._arg(event)} needs a channel key")

    def _on_channelisfull(self, net_id, conn, event):
        self._record(net_id, KIND_ERROR, f"{self._arg(event)} is full")

    def _on_nosuchchannel(self, net_id, conn, event):
        self._record(net_id, KIND_ERROR, f"No such channel: {self._arg(event)}")

    def _on_nosuchnick(self, net_id, conn, event):
        self._record(net_id, KIND_ERROR, f"No such nick: {self._arg(event)}")

    def _on_cannotsendtochan(self, net_id, conn, event):
        self._record(net_id, KIND_ERROR, f"Cannot send to {self._arg(event)}")

    @staticmethod
    def _arg(event, idx: int = 0) -> str:
        try:
            return event.arguments[idx]
        except Exception:
            return ""

    def _ensure_query(self, net_id: str, nick: str) -> None:
        self.state.ensure_channel(net_id, nick)
