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

import base64
import functools
import ipaddress
import logging
import os
import queue
import re
import shlex
import ssl
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

import irc.client
import irc.connection

from config import IRCNetworkConfig
from ircmgr.history import IRCHistoryStore, MAX_QUERY_LIMIT
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
# Maximum wire-line length excluding CRLF.  Keeping some headroom below IRC's
# 510-byte payload limit also leaves room for servers which add tags/prefixes.
MAX_IRC_LINE = 400
DEFAULT_RECONNECT_ATTEMPTS = 5
IRC_V3_CAPABILITIES = frozenset({
    "multi-prefix", "server-time", "message-tags", "account-notify",
    "away-notify", "extended-join", "echo-message",
})


def strip_control_codes(text: str) -> str:
    return _CTRL_RE.sub("", text)


class _IRCv3ServerConnection(irc.client.ServerConnection):
    """ServerConnection that starts CAP before registration for every login.

    jaraco-irc 20.5 only starts CAP negotiation when SASL is configured. The
    core owns the CAP/SASL state machine, so this small subclass follows the
    installed connection API while ensuring non-SASL sessions negotiate safely
    before NICK/USER can complete registration.
    """

    def connect(self, server: str, port: int, nickname: str, password=None,
                username=None, ircname=None,
                connect_factory=irc.connection.Factory(), sasl_login=None):
        if self.connected:
            self.disconnect("Changing servers")
        self.buffer = self.buffer_class()
        self.handlers = {}
        self.real_server_name = ""
        self.real_nickname = nickname
        self.server = server
        self.port = port
        self.server_address = (server, port)
        self.nickname = nickname
        self.username = username or nickname
        self.ircname = ircname or nickname
        self.password = password
        self.connect_factory = connect_factory
        self.sasl_login = sasl_login
        try:
            self.socket = self.connect_factory(self.server_address)
        except OSError as exc:
            raise irc.client.ServerConnectionError(
                f"Couldn't connect to socket: {exc}") from exc
        self.connected = True
        self.reactor._on_connect(self.socket)

        # CAP 302 must precede registration completion. A SASL password is not
        # a server PASS and is therefore never sent here.
        self.cap("LS", "302")
        if self.password and not self.sasl_login:
            self.pass_(self.password)
        self.nick(self.nickname)
        self.user(self.username, self.ircname)
        return self


class IRCClientCore:
    """Multi-network IRC client; all public methods are thread-safe."""

    def __init__(self, irc_config, history_db_path: Optional[str] = None,
                 history_key_path: Optional[str] = None) -> None:
        self._config = irc_config
        self.state = IRCState(getattr(irc_config, "buffer_lines", 500))
        self._history_db_path = history_db_path
        self._history_key_path = history_key_path
        self._history: Optional[IRCHistoryStore] = None
        self._flood_delay = max(0.5, float(getattr(irc_config, "flood_delay", 2.0)))
        self._reconnect_max = max(10, int(getattr(irc_config, "reconnect_max_seconds", 300)))
        self._reconnect_attempt_max = max(
            1, int(getattr(irc_config, "reconnect_max_attempts", DEFAULT_RECONNECT_ATTEMPTS)))

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
        self.configure_history()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._running.set()
            self._thread = threading.Thread(target=self._run, name="irc-client", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop the network thread and disconnect all sockets (idempotently)."""
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._running.clear()
                self._thread = None
                return
            # Let the owning thread consume the command. Clearing _running here
            # would make it leave its loop before seeing the queued shutdown.
            self._cmd_q.put(("quit_all", ()))
        thread.join(timeout=5)
        if thread.is_alive():
            logger.warning("IRC client thread did not stop within 5 seconds")
            self._running.clear()
        else:
            with self._lock:
                if self._thread is thread:
                    self._thread = None

    def add_listener(self, cb: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(cb)

    def configure_history(self) -> None:
        """Apply live history settings without creating files while disabled."""
        enabled = bool(getattr(self._config, "history_enabled", False))
        retention = max(1, min(3650, int(
            getattr(self._config, "history_retention_days", 30) or 30)))
        with self._lock:
            if not enabled:
                self._history = None
                return
            if self._history is None:
                self._history = IRCHistoryStore(
                    self._history_db_path, self._history_key_path, retention)
            else:
                self._history.retention_days = retention
                self._history.cleanup()

    def clear_history(self) -> None:
        with self._lock:
            history = self._history
        if history is None:
            db_path = self._history_db_path or IRCHistoryStore.default_db_path()
            key_path = self._history_key_path or IRCHistoryStore.default_key_path()
            # A privacy action must not create history files when none exist.
            if not (os.path.isfile(db_path) and os.path.isfile(key_path)):
                return
            history = IRCHistoryStore(
                db_path, key_path,
                getattr(self._config, "history_retention_days", 30))
        history.clear()

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
                rt["reconnect_attempts"] = 0
                rt["reconnect_stopped"] = False
                rt["last_error"] = ""
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
        snapshot = self.state.snapshot()
        with self._lock:
            runtime = {
                net_id: {
                    "reconnect_attempts": int(rt.get("reconnect_attempts", 0)),
                    "reconnect_stopped": bool(rt.get("reconnect_stopped", False)),
                    "last_error": str(rt.get("last_error", "")),
                    "advertised_caps": sorted(rt.get("advertised_caps", set())),
                    "negotiated_caps": sorted(rt.get("negotiated_caps", set())),
                }
                for net_id, rt in self._nets.items()
            }
        for network in snapshot.get("networks", []):
            rt = runtime.get(network["id"])
            if rt:
                network.update({
                    "reconnect_attempts": rt["reconnect_attempts"],
                    "reconnect_stopped": rt["reconnect_stopped"],
                    "advertised_caps": rt["advertised_caps"],
                    "negotiated_caps": rt["negotiated_caps"],
                })
                if rt["last_error"]:
                    network["error"] = rt["last_error"]
        return snapshot

    @staticmethod
    def _message_key(message: Dict[str, Any]) -> tuple:
        return ("id", message["id"]) if message.get("id") else (
            "content", message.get("network"), message.get("channel"),
            round(float(message.get("ts", 0.0)), 6), message.get("kind"),
            message.get("nick"), message.get("text"),
        )

    @classmethod
    def _merge_messages(cls, messages: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        deduped: Dict[tuple, Dict[str, Any]] = {}
        for message in messages:
            deduped[cls._message_key(message)] = message
        merged = sorted(deduped.values(), key=lambda item: (
            float(item.get("ts", 0.0)), str(item.get("id", ""))))
        return merged[-limit:] if limit else merged

    def get_messages(self, net_id: str, channel: Optional[str] = None,
                     limit: int = 50, since: float = 0.0) -> List[Dict[str, Any]]:
        limit = max(0, min(MAX_QUERY_LIMIT, int(limit)))
        memory = [m.to_dict() for m in self.state.get_messages(
            net_id, channel, limit, since)]
        with self._lock:
            history = self._history
        persisted = history.get_messages(net_id, channel, limit, since) if history else []
        return self._merge_messages(persisted + memory, limit)

    def search_messages(self, query: str, net_id: Optional[str] = None,
                        channel: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        limit = max(0, min(MAX_QUERY_LIMIT, int(limit)))
        memory = self.state.search(query, net_id, channel, limit)
        with self._lock:
            history = self._history
        persisted = history.search(query, net_id, channel, limit) if history else []
        return self._merge_messages(persisted + memory, limit)

    def is_connected(self, net_id: str) -> bool:
        """Cheap connected check for one network (GUI hot paths — no snapshot)."""
        return self.state.network_connected(net_id)

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
                       if any(self.state.identifiers_equal(n["id"], c["name"], channel)
                              for c in n["channels"])]
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
        self._reactor.connection_class = _IRCv3ServerConnection
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
                kind = "action" if is_action else "privmsg"
                for chunk in self._split(text, target=target, is_action=is_action):
                    rt["outbox"].append((kind, target, chunk))
            else:
                self._record(net_id, KIND_ERROR, f"Not connected — message to {target} dropped.")
        elif cmd == "notice":
            net_id, target, text = args
            rt = self._nets.get(net_id)
            if rt and rt.get("conn") and rt["conn"].is_connected():
                for chunk in self._split(text, target=target, command="NOTICE"):
                    rt["outbox"].append(("notice", target, chunk))
            else:
                self._record(net_id, KIND_ERROR, f"Not connected — notice to {target} dropped.")
        elif cmd == "nick":
            net_id, newnick = args
            rt = self._nets.get(net_id)
            if rt and rt.get("conn") and rt["conn"].is_connected():
                rt["conn"].nick(newnick)
        elif cmd == "raw":
            net_id, line = args
            rt = self._nets.get(net_id)
            if rt and rt.get("conn") and rt["conn"].is_connected():
                # Public raw commands are user traffic and must obey the same
                # pacing as chat. Internal registration/keepalive operations
                # continue to use the connection directly.
                clean = line.replace("\r", " ").replace("\n", " ")
                rt["outbox"].append(("raw", "", self._truncate_utf8(clean, MAX_IRC_LINE)))
            else:
                self._record(net_id, KIND_ERROR, "Not connected — raw command dropped.")

    # -- connection management --------------------------------------------

    @staticmethod
    def _sasl_config_error(net: IRCNetworkConfig) -> Optional[str]:
        account = bool((net.sasl_account or "").strip())
        sasl_password = bool(net.sasl_password)
        if account and not sasl_password:
            return ("SASL account is configured without a SASL password. "
                    "The server password is separate and will not be reused for SASL.")
        if sasl_password and not account:
            return "SASL password is configured without a SASL account."
        return None

    def _new_runtime(self, net: IRCNetworkConfig) -> Dict[str, Any]:
        return {
            "cfg": net,
            "conn": None,
            "outbox": deque(),
            "next_send": 0.0,
            "reconnect_at": None,
            "backoff": 2.0,
            "reconnect_attempts": 0,
            "reconnect_stopped": False,
            "last_error": "",
            "manual_disconnect": False,
            "welcomed": False,
            "nick_try": 0,
            "advertised_caps": set(),
            "advertised_cap_values": {},
            "negotiated_caps": set(),
            "cap_ls_parts": [],
            "cap_requested": set(),
            "cap_end_sent": False,
            "sasl_in_progress": False,
            # Auto-/LIST retry: channelless networks request the channel
            # directory on connect and retry every 10s until it arrives
            # (some servers throttle LIST for ~60s after connect).
            "auto_list_pending": False,
            "auto_list_at": None,
        }

    def _do_connect(self, net: IRCNetworkConfig) -> None:
        rt = self._nets.get(net.id)
        if rt and rt.get("conn") and rt["conn"].is_connected():
            return  # already connected
        if rt is None:
            rt = self._new_runtime(net)
            self._nets[net.id] = rt
        else:
            rt["cfg"] = net
            rt["reconnect_at"] = None
            rt["welcomed"] = False
            rt["nick_try"] = 0
        rt["advertised_caps"] = set()
        rt["advertised_cap_values"] = {}
        rt["negotiated_caps"] = set()
        rt["cap_ls_parts"] = []
        rt["cap_requested"] = set()
        rt["cap_end_sent"] = False
        rt["sasl_in_progress"] = False

        self.state.ensure_network(net.id, net.host, net.port, net.tls)
        config_error = self._sasl_config_error(net)
        if config_error:
            self.state.set_connecting(net.id, False)
            rt["reconnect_stopped"] = True
            rt["reconnect_at"] = None
            rt["last_error"] = config_error
            self._record(net.id, KIND_ERROR, f"Invalid IRC configuration: {config_error}")
            self._emit({"type": "state", "network": net.id, "state": "error",
                        "detail": config_error})
            return

        self.state.set_connecting(net.id, True)
        self._emit({"type": "state", "network": net.id, "state": "connecting"})

        connect_factory = irc.connection.Factory()
        if net.tls:
            context = ssl.create_default_context()
            wrapper = functools.partial(context.wrap_socket, server_hostname=net.host)
            connect_factory = irc.connection.Factory(wrapper=wrapper)

        conn = None
        try:
            conn = self._reactor.server()
            # Map this connection to its network BEFORE we connect so events
            # (processed later by the reactor loop) resolve to the right net.
            # Drop any stale mapping for a previous connection of this net.
            old = rt.get("conn")
            if old is not None:
                self._conn_to_net.pop(old, None)
            self._conn_to_net[conn] = net.id
            rt["conn"] = conn
            self._install_handlers()
            sasl_login = (net.sasl_account or "").strip() or None
            # jaraco-irc uses `password` as the SASL secret whenever
            # sasl_login is set. Never fall back to the unrelated server PASS.
            password = net.sasl_password if sasl_login else (net.password or None)
            conn.connect(
                net.host, net.port, net.nick,
                password=password,
                username=net.username or net.nick,
                ircname=net.realname or net.nick,
                connect_factory=connect_factory,
                sasl_login=sasl_login,
            )
        except Exception as exc:
            if conn is not None:
                self._conn_to_net.pop(conn, None)
                if rt.get("conn") is conn:
                    rt["conn"] = None
            logger.warning("IRC connect to %s failed: %s", net.host, exc)
            self.state.set_connecting(net.id, False)
            rt["last_error"] = str(exc)
            self._record(net.id, KIND_ERROR, f"Connect failed: {exc}")
            self._emit({"type": "state", "network": net.id, "state": "error",
                        "detail": str(exc)})
            self._schedule_reconnect(net.id)
            return

        rt["auto_list_pending"] = False
        rt["auto_list_at"] = None

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
        if (not rt or rt.get("manual_disconnect") or rt.get("reconnect_stopped")
                or not self._running.is_set()):
            return
        attempts = int(rt.get("reconnect_attempts", 0)) + 1
        rt["reconnect_attempts"] = attempts
        if attempts >= self._reconnect_attempt_max:
            rt["reconnect_at"] = None
            rt["reconnect_stopped"] = True
            detail = (f"Reconnect stopped after {attempts} consecutive failures. "
                      "Connect manually to try again.")
            rt["last_error"] = detail
            self._record(net_id, KIND_ERROR, detail)
            self._emit({"type": "state", "network": net_id, "state": "error",
                        "detail": detail})
            return
        delay = min(rt.get("backoff", 2.0), self._reconnect_max)
        rt["reconnect_at"] = time.monotonic() + delay
        rt["backoff"] = min(delay * 2, self._reconnect_max)
        self._record(net_id, KIND_SERVER,
                     f"Reconnecting in {int(delay)}s (attempt {attempts}/{self._reconnect_attempt_max})…")

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
            rt["negotiated_caps"] = set()
            rt["sasl_in_progress"] = False
        if rt and rt.get("reconnect_stopped"):
            self._emit({"type": "state", "network": net_id, "state": "error",
                        "detail": rt.get("last_error", "Reconnect stopped")})
        else:
            self._emit({"type": "state", "network": net_id, "state": "disconnected"})

    # -- outgoing pacing ----------------------------------------------------

    @staticmethod
    def _truncate_utf8(text: str, byte_limit: int) -> str:
        """Return a valid-Unicode prefix whose UTF-8 encoding fits byte_limit."""
        if byte_limit <= 0:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) <= byte_limit:
            return text
        return encoded[:byte_limit].decode("utf-8", errors="ignore")

    @staticmethod
    def _split(text: str, target: str = "", is_action: bool = False,
               command: str = "PRIVMSG") -> List[str]:
        """Split text by UTF-8 bytes, accounting for its complete wire prefix."""
        text = text.replace("\r", " ").replace("\n", " ")
        overhead = len(f"{command} {target} :".encode("utf-8"))
        if is_action:
            overhead += len("\x01ACTION \x01".encode("utf-8"))
        budget = max(1, MAX_IRC_LINE - overhead)
        if len(text.encode("utf-8")) <= budget:
            return [text]

        chunks: List[str] = []
        remaining = text
        while remaining:
            if len(remaining.encode("utf-8")) <= budget:
                chunks.append(remaining)
                break
            prefix = IRCClientCore._truncate_utf8(remaining, budget)
            if not prefix:  # only possible with an impossibly long target
                prefix = remaining[0]
            split_at = prefix.rfind(" ")
            if split_at > 0:
                chunk = prefix[:split_at]
                remaining = remaining[split_at + 1:]
            else:
                chunk = prefix
                remaining = remaining[len(prefix):]
            chunks.append(chunk)
        return chunks or [""]

    def _flush_outboxes(self) -> None:
        now = time.monotonic()
        for net_id, rt in self._nets.items():
            outbox: Deque = rt.get("outbox") or ()
            conn = rt.get("conn")
            if not outbox or not conn or not conn.is_connected():
                continue
            if now < rt.get("next_send", 0.0):
                continue
            kind, target, text = outbox.popleft()
            try:
                if kind == "action":
                    conn.action(target, text)
                elif kind == "notice":
                    conn.notice(target, text)
                elif kind == "raw":
                    conn.send_raw(text)
                else:
                    conn.privmsg(target, text)
                rt["next_send"] = now + self._flood_delay
                if kind in ("privmsg", "action") and \
                        "echo-message" not in rt.get("negotiated_caps", set()):
                    nick = self.state.nick_of(net_id) or rt["cfg"].nick
                    message_kind = KIND_ACTION if kind == "action" else KIND_MESSAGE
                    msg = self._record(net_id, message_kind, text, nick=nick, channel=target)
                    if not is_channel(target):
                        self._ensure_query(net_id, target)
                    self._emit({
                        "type": "action" if kind == "action" else "message",
                        "network": net_id, "channel": target, "nick": nick,
                        "text": text, "own": True, "private": not is_channel(target),
                        **self._event_payload(msg),
                    })
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
        if not any(self.state.identifiers_equal(net_id, known, channel)
                   for known in cfg.channels):
            cfg.channels.append(channel)
        rt["conn"].join(channel)

    def _do_part(self, net_id: str, channel: str) -> None:
        rt = self._nets.get(net_id)
        if not rt or not rt.get("conn") or not rt["conn"].is_connected():
            return
        cfg = rt["cfg"]
        configured = next((known for known in cfg.channels
                           if self.state.identifiers_equal(net_id, known, channel)), None)
        if configured is not None:
            cfg.channels.remove(configured)
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
                     "cap", "authenticate", "saslsuccess", "login_failed", "saslfail",
                     "account", "away", "passwordmismatch",
                     "bannedfromchan", "inviteonlychan",
                     "badchannelkey", "channelisfull", "nosuchchannel",
                     "nosuchnick", "cannotsendtochan", "ctcp", "ctcpreply",
                     # WHOIS replies (311-319/330) and away-state acks (305/306)
                     # so /whois and /away answers land in the server buffer.
                     "whoisuser", "whoisserver", "whoisoperator", "whoisidle",
                     "whoischannels", "whoisaccount", "endofwhois",
                     "nowaway", "unaway"):
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

    @staticmethod
    def _event_tags(event) -> Dict[str, Optional[str]]:
        tags: Dict[str, Optional[str]] = {}
        for tag in (getattr(event, "tags", None) or [])[:64]:
            if isinstance(tag, dict) and tag.get("key"):
                tags[str(tag["key"])[:128]] = (
                    str(tag.get("value"))[:1024] if tag.get("value") is not None else None)
        return tags

    @classmethod
    def _event_time(cls, event) -> float:
        value = cls._event_tags(event).get("time")
        if not value:
            return time.time()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return time.time()

    def _message_metadata(self, net_id: str, nick: str, event=None) -> Dict[str, Any]:
        tags = self._event_tags(event) if event is not None else {}
        current = self.state.user_metadata(net_id, nick)
        tagged_account = tags.get("account")
        if tagged_account == "*":
            account = ""
            if nick:
                self.state.set_user_metadata(net_id, nick, account="*")
        else:
            account = tagged_account or current.get("account") or ""
        away = current.get("away")
        if nick and account:
            self.state.set_user_metadata(net_id, nick, account=str(account))
        return {"account": str(account), "away": away, "tags": tags}

    def _record(self, net_id: str, kind: str, text: str, nick: str = "",
                channel: Optional[str] = None, event=None,
                ts: Optional[float] = None) -> ChatMessage:
        metadata = self._message_metadata(net_id, nick, event)
        msg = ChatMessage(
            ts=float(ts if ts is not None else (
                self._event_time(event) if event is not None else time.time())),
            kind=kind, nick=nick, text=text, account=metadata["account"],
            away=metadata["away"], tags=metadata["tags"],
        )
        self.state.record(net_id, msg, channel)
        with self._lock:
            history = self._history
        is_private = bool(channel and not is_channel(channel))
        private_enabled = bool(getattr(self._config, "history_private_messages", False))
        history_enabled = bool(getattr(self._config, "history_enabled", False))
        if history_enabled and history is not None and (not is_private or private_enabled):
            try:
                history.add(net_id, channel, msg.to_dict())
            except Exception:
                # Never include message content, nicknames, or credentials in logs.
                logger.warning("Could not persist IRC history event", exc_info=True)
        return msg

    @staticmethod
    def _event_payload(msg: ChatMessage) -> Dict[str, Any]:
        return {
            "ts": msg.ts, "id": msg.event_id, "account": msg.account,
            "away": msg.away, "tags": dict(msg.tags),
        }

    @staticmethod
    def _cap_names(arguments: List[str]) -> List[str]:
        payload = " ".join(arguments)
        return [token.lstrip("-~=").split("=", 1)[0].lower()
                for token in payload.split() if token and token != "*"]

    def _end_cap(self, rt: Dict[str, Any], conn) -> None:
        if not rt.get("cap_end_sent"):
            conn.cap("END")
            rt["cap_end_sent"] = True

    def _on_cap(self, net_id, conn, event) -> None:
        rt = self._nets.get(net_id)
        if rt is None:
            return
        args = list(event.arguments or [])
        if not args:
            return
        subcommand = args[0].upper()
        if subcommand == "LS":
            continuation = len(args) > 1 and args[1] == "*"
            tokens = " ".join(args[2:] if continuation else args[1:]).split()
            rt.setdefault("cap_ls_parts", []).extend(tokens)
            if continuation:
                return
            values: Dict[str, str] = {}
            for token in rt.pop("cap_ls_parts", []):
                clean = token.lstrip("-~=")
                name, separator, value = clean.partition("=")
                if name:
                    values[name.lower()] = value if separator else ""
            advertised = set(values)
            rt["advertised_caps"] = advertised
            rt["advertised_cap_values"] = values
            requested: Set[str] = set(IRC_V3_CAPABILITIES) & advertised
            cfg = rt["cfg"]
            wants_sasl = bool((cfg.sasl_account or "").strip() and cfg.sasl_password)
            if wants_sasl:
                mechanisms = {item.upper() for item in values.get("sasl", "").split(",") if item}
                if "sasl" not in advertised or (mechanisms and "PLAIN" not in mechanisms):
                    detail = "SASL authentication failed: server does not advertise SASL PLAIN"
                    self._end_cap(rt, conn)
                    self._permanent_connection_failure(net_id, conn, detail)
                    return
                requested.add("sasl")
            rt["cap_requested"] = requested
            if requested:
                # jaraco-irc's cap() inserts the required ':' for multi-cap REQ.
                conn.cap("REQ", *sorted(requested))
            else:
                self._end_cap(rt, conn)
            return

        names = set(self._cap_names(args[1:]))
        if subcommand == "ACK":
            rt["negotiated_caps"].update(names & rt.get("cap_requested", set()))
            if "sasl" in rt.get("cap_requested", set()):
                if "sasl" in names:
                    rt["sasl_in_progress"] = True
                    conn.send_items("AUTHENTICATE", "PLAIN")
                else:
                    self._end_cap(rt, conn)
                    self._permanent_connection_failure(
                        net_id, conn, "SASL authentication failed: server did not acknowledge SASL")
            else:
                self._end_cap(rt, conn)
        elif subcommand == "NAK":
            if "sasl" in rt.get("cap_requested", set()):
                self._end_cap(rt, conn)
                self._permanent_connection_failure(
                    net_id, conn, "SASL authentication failed: server refused capabilities")
            else:
                self._end_cap(rt, conn)

    def _on_authenticate(self, net_id, conn, event) -> None:
        rt = self._nets.get(net_id)
        if not rt or not rt.get("sasl_in_progress") or event.target != "+":
            return
        cfg = rt["cfg"]
        account = (cfg.sasl_account or "").strip()
        payload = base64.b64encode(conn.encode(
            f"\x00{account}\x00{cfg.sasl_password}"))
        encoded = payload.decode("ascii")
        for offset in range(0, len(encoded), 400):
            conn.send_items("AUTHENTICATE", encoded[offset:offset + 400])
        if len(encoded) % 400 == 0:
            conn.send_items("AUTHENTICATE", "+")
        rt["sasl_in_progress"] = False

    def _on_saslsuccess(self, net_id, conn, event) -> None:
        rt = self._nets.get(net_id)
        if rt:
            self._end_cap(rt, conn)

    def _on_welcome(self, net_id, conn, event) -> None:
        nick = event.target or ""
        rt = self._nets.get(net_id)
        if rt:
            rt["welcomed"] = True
            rt["backoff"] = 2.0
            rt["reconnect_attempts"] = 0
            rt["reconnect_stopped"] = False
            rt["last_error"] = ""
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
                    "nick": nick,
                    "negotiated_caps": sorted(rt.get("negotiated_caps", set())) if rt else []})

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
        msg = self._record(net_id, KIND_MESSAGE, text, nick=nick, channel=channel,
                           event=event)
        self._emit({"type": "message", "network": net_id, "channel": channel,
                    "nick": nick, "text": text,
                    "own": self.state.nick_equals(net_id, nick, self.state.nick_of(net_id)),
                    **self._event_payload(msg)})

    def _on_privmsg(self, net_id, conn, event) -> None:
        nick = self._src_nick(event)
        own = self.state.nick_equals(net_id, nick, self.state.nick_of(net_id))
        target = event.target if own else nick
        text = strip_control_codes(event.arguments[0] if event.arguments else "")
        msg = self._record(net_id, KIND_MESSAGE, text, nick=nick, channel=target,
                           event=event)
        self._ensure_query(net_id, target)
        self._emit({"type": "message", "network": net_id, "channel": target,
                    "nick": nick, "text": text, "private": True, "own": own,
                    **self._event_payload(msg)})

    def _on_action(self, net_id, conn, event) -> None:
        nick = self._src_nick(event)
        own = self.state.nick_equals(net_id, nick, self.state.nick_of(net_id))
        target = event.target if is_channel(event.target) or own else nick
        text = strip_control_codes(event.arguments[0] if event.arguments else "")
        if not is_channel(event.target):
            self._ensure_query(net_id, target)
        msg = self._record(net_id, KIND_ACTION, text, nick=nick, channel=target,
                           event=event)
        self._emit({"type": "action", "network": net_id, "channel": target,
                    "nick": nick, "text": text, "own": own,
                    **self._event_payload(msg)})

    def _on_pubnotice(self, net_id, conn, event) -> None:
        nick = self._src_nick(event)
        text = strip_control_codes(event.arguments[0] if event.arguments else "")
        target = event.target if is_channel(event.target) else None
        msg = self._record(net_id, KIND_NOTICE, text, nick=nick, channel=target,
                           event=event)
        self._emit({"type": "notice", "network": net_id, "channel": target,
                    "nick": nick, "text": text, **self._event_payload(msg)})

    def _on_privnotice(self, net_id, conn, event) -> None:
        self._on_pubnotice(net_id, conn, event)

    def _on_join(self, net_id, conn, event) -> None:
        channel = event.target
        nick = self._src_nick(event)
        mine = self.state.nick_of(net_id)
        # extended-join arguments are account-name and realname.
        account = event.arguments[0] if event.arguments else None
        if account is not None:
            self.state.set_user_metadata(net_id, nick, account=account)
        self.state.add_nick(net_id, channel, nick)
        self.state.ensure_channel(net_id, channel)
        msg = self._record(net_id, KIND_JOIN, f"{nick} joined", nick=nick,
                           channel=channel, event=event)
        self._emit({"type": "join", "network": net_id, "channel": channel,
                    "nick": nick, "own": self.state.nick_equals(net_id, nick, mine),
                    **self._event_payload(msg)})

    def _on_account(self, net_id, conn, event) -> None:
        nick = self._src_nick(event)
        account = event.target or (event.arguments[0] if event.arguments else "*")
        self.state.set_user_metadata(net_id, nick, account=account)
        metadata = self.state.user_metadata(net_id, nick)
        self._emit({"type": "user_metadata", "network": net_id, "nick": nick,
                    **metadata})

    def _on_away(self, net_id, conn, event) -> None:
        nick = self._src_nick(event)
        # AWAY with a reason means away; no parameter means back.
        away = bool(event.target or event.arguments)
        self.state.set_user_metadata(net_id, nick, away=away)
        metadata = self.state.user_metadata(net_id, nick)
        self._emit({"type": "user_metadata", "network": net_id, "nick": nick,
                    **metadata})

    def _on_part(self, net_id, conn, event) -> None:
        channel = event.target
        nick = self._src_nick(event)
        reason = strip_control_codes(event.arguments[0]) if event.arguments else ""
        self.state.remove_nick(net_id, nick, channel)
        own = self.state.nick_equals(net_id, nick, self.state.nick_of(net_id))
        self._record(net_id, KIND_PART, f"{nick} left {reason}".strip(), nick=nick,
                     channel=channel)
        if own:
            # Record before dropping: IRCState.record() creates a missing
            # channel, so the opposite order resurrects a stale channel.
            self.state.drop_channel(net_id, channel)
            self._emit({"type": "parted", "network": net_id, "channel": channel})
        self._emit({"type": "part", "network": net_id, "channel": channel,
                    "nick": nick, "own": own})

    def _on_quit(self, net_id, conn, event) -> None:
        nick = self._src_nick(event)
        reason = strip_control_codes(event.arguments[0]) if event.arguments else ""
        # Light per-event lookup: a full state snapshot per QUIT stalled the
        # network thread during netsplit storms.
        channels = self.state.channels_of_nick(net_id, nick)
        self.state.remove_nick(net_id, nick)
        for ch in channels:
            self._record(net_id, KIND_QUIT, f"{nick} quit {reason}".strip(),
                         nick=nick, channel=ch)
        self._emit({"type": "quit", "network": net_id, "nick": nick})

    def _on_kick(self, net_id, conn, event) -> None:
        channel = event.target
        kicked = event.arguments[0] if event.arguments else ""
        by = self._src_nick(event)
        self.state.remove_nick(net_id, kicked, channel)
        own = self.state.nick_equals(net_id, kicked, self.state.nick_of(net_id))
        self._record(net_id, KIND_KICK, f"{kicked} was kicked by {by}",
                     nick=by, channel=channel)
        if own:
            self.state.drop_channel(net_id, channel)
            self._emit({"type": "parted", "network": net_id, "channel": channel})
        self._emit({"type": "kick", "network": net_id, "channel": channel,
                    "nick": kicked, "by": by, "own": own})

    def _on_nick(self, net_id, conn, event) -> None:
        old = self._src_nick(event)
        new = event.target or ""
        self.state.rename_nick(net_id, old, new)
        if self.state.nick_equals(net_id, old, self.state.nick_of(net_id)):
            self.state.set_connected(net_id, True, nick=new)
        for channel in self.state.channels_of_nick(net_id, new):
            self._record(net_id, KIND_NICK, f"{old} is now {new}",
                         channel=channel)
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

    def _on_featurelist(self, net_id, conn, event) -> None:
        # RPL_ISUPPORT (005), e.g. CASEMAPPING=strict-rfc1459 PREFIX=(qaohv)~&@%+.
        for token in event.arguments or []:
            if token.upper().startswith("CASEMAPPING="):
                self.state.set_casemapping(net_id, token.split("=", 1)[1])

    def _on_namreply(self, net_id, conn, event) -> None:
        # RPL_NAMREPLY (353): arguments = [channel_type, channel, "nick nick ..."]
        if len(event.arguments) < 2:
            return
        channel = event.arguments[-2]
        names = event.arguments[-1].split()
        nicks: Dict[str, str] = {}
        for raw in names:
            prefix_len = 0
            while prefix_len < len(raw) and raw[prefix_len] in "~&@%+":
                prefix_len += 1
            prefixes = raw[:prefix_len]
            # USERHOST-in-NAMES may append !user@host; membership tracks the nick only.
            nick = raw[prefix_len:].split("!", 1)[0]
            if nick:
                nicks[nick] = prefixes
        existing = self.state.nicks_of(net_id, channel)
        for nick, prefixes in nicks.items():
            old = next((known for known in existing
                        if self.state.nick_equals(net_id, known, nick)), None)
            if old is None:
                existing[nick] = prefixes
            else:
                existing[old] += prefixes
        self.state.set_nicks(net_id, channel, existing)

    def _on_endofnames(self, net_id, conn, event) -> None:
        channel = event.arguments[0] if event.arguments else ""
        self._emit({"type": "names", "network": net_id, "channel": channel,
                    "nicks": self.state.nicks_of(net_id, channel)})

    # -- WHOIS replies & away-state acks ------------------------------------

    def _on_whoisuser(self, net_id, conn, event) -> None:
        # RPL_WHOISUSER (311): [nick, user, host, '*', realname]
        if len(event.arguments) >= 3:
            text = f"{event.arguments[0]} is {event.arguments[1]}@{event.arguments[2]}"
            if len(event.arguments) >= 5 and event.arguments[4]:
                text += f" ({event.arguments[4]})"
            self._record(net_id, KIND_SERVER, strip_control_codes(text))

    def _on_whoisserver(self, net_id, conn, event) -> None:
        # RPL_WHOISSERVER (312): [nick, server, server info]
        if len(event.arguments) >= 2:
            info = strip_control_codes(event.arguments[2]) if len(event.arguments) > 2 else ""
            self._record(net_id, KIND_SERVER, strip_control_codes(
                f"{event.arguments[0]} is on {event.arguments[1]}"
                + (f": {info}" if info else "")))

    def _on_whoisoperator(self, net_id, conn, event) -> None:
        if event.arguments:
            self._record(net_id, KIND_SERVER,
                         strip_control_codes(" ".join(event.arguments)))

    def _on_whoisidle(self, net_id, conn, event) -> None:
        # RPL_WHOISIDLE (317): [nick, idle seconds, signon epoch, text]
        if len(event.arguments) >= 2:
            try:
                idle = int(event.arguments[1])
            except ValueError:
                return
            line = (f"{event.arguments[0]} idle for "
                    f"{idle // 3600}h{(idle % 3600) // 60:02d}m{idle % 60:02d}s")
            if len(event.arguments) >= 3 and event.arguments[2].isdigit():
                line += (f", signed on "
                         f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(int(event.arguments[2])))}")
            self._record(net_id, KIND_SERVER, line)

    def _on_whoischannels(self, net_id, conn, event) -> None:
        # RPL_WHOISCHANNELS (319): [nick, "#chan @#chan2 ..."]
        if len(event.arguments) >= 2:
            self._record(net_id, KIND_SERVER, strip_control_codes(
                f"{event.arguments[0]} is in {event.arguments[1]}"))

    def _on_whoisaccount(self, net_id, conn, event) -> None:
        # RPL_WHOISACCOUNT (330): [nick, account, "is logged in as"]
        if len(event.arguments) >= 2:
            self._record(net_id, KIND_SERVER, strip_control_codes(
                f"{event.arguments[0]} is logged in as {event.arguments[1]}"))

    def _on_endofwhois(self, net_id, conn, event) -> None:
        if event.arguments:
            self._record(net_id, KIND_SERVER,
                         strip_control_codes(" ".join(event.arguments)))

    def _on_nowaway(self, net_id, conn, event) -> None:
        self._record(net_id, KIND_SERVER, "You are marked as being away")

    def _on_unaway(self, net_id, conn, event) -> None:
        self._record(net_id, KIND_SERVER, "You are no longer marked as being away")

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

    def _permanent_connection_failure(self, net_id: str, conn, detail: str) -> None:
        rt = self._nets.get(net_id)
        if rt and rt.get("reconnect_stopped"):
            return
        if rt:
            rt["reconnect_stopped"] = True
            rt["reconnect_at"] = None
            rt["last_error"] = detail
        self.state.set_connected(net_id, False)
        self._record(net_id, KIND_ERROR, detail)
        self._emit({"type": "state", "network": net_id, "state": "error",
                    "detail": detail})
        try:
            conn.close()
        except Exception:
            pass

    def _on_login_failed(self, net_id, conn, event) -> None:
        detail = event.arguments[-1] if event.arguments else "SASL authentication failed"
        rt = self._nets.get(net_id)
        if rt:
            self._end_cap(rt, conn)
        self._permanent_connection_failure(net_id, conn, f"SASL authentication failed: {detail}")

    def _on_saslfail(self, net_id, conn, event) -> None:
        detail = event.arguments[-1] if event.arguments else "credentials rejected"
        rt = self._nets.get(net_id)
        if rt:
            self._end_cap(rt, conn)
        self._permanent_connection_failure(net_id, conn, f"SASL authentication failed: {detail}")

    def _on_passwordmismatch(self, net_id, conn, event) -> None:
        detail = event.arguments[-1] if event.arguments else "server password rejected"
        self._permanent_connection_failure(net_id, conn, f"Server password rejected: {detail}")

    @staticmethod
    def _safe_dcc_text(value: Any, limit: int = 160) -> str:
        clean = strip_control_codes(str(value or "")).replace("\r", " ").replace("\n", " ")
        clean = "".join(char for char in clean if char.isprintable()).strip()
        return clean[:limit]

    @classmethod
    def _parse_dcc_offer(cls, args: List[str]) -> Dict[str, Any]:
        """Parse display-only DCC metadata. This never opens a socket or accepts."""
        payload = " ".join(args[1:])
        try:
            fields = shlex.split(payload, posix=True)
        except ValueError:
            fields = payload.split()
        dcc_type = cls._safe_dcc_text(fields[0] if fields else "UNKNOWN", 16).upper()
        result: Dict[str, Any] = {
            "dcc_type": dcc_type or "UNKNOWN", "filename": "", "address": "",
            "port": None, "size": None,
        }
        offset = 1
        if result["dcc_type"] == "SEND" and len(fields) > 1:
            # Display a basename only: remote path components have no useful meaning here.
            raw_name = cls._safe_dcc_text(fields[1]).replace("\\", "/")
            result["filename"] = raw_name.rsplit("/", 1)[-1] or "unnamed file"
            offset = 2
        elif result["dcc_type"] == "CHAT" and len(fields) > 1:
            offset = 2  # usually the literal protocol token "chat"
        if len(fields) > offset:
            raw_address = cls._safe_dcc_text(fields[offset], 80)
            try:
                numeric = int(raw_address)
                result["address"] = str(ipaddress.IPv4Address(numeric))
            except (ipaddress.AddressValueError, ValueError):
                result["address"] = raw_address
        if len(fields) > offset + 1:
            try:
                port = int(fields[offset + 1])
                result["port"] = port if 0 < port <= 65535 else None
            except ValueError:
                pass
        if result["dcc_type"] == "SEND" and len(fields) > offset + 2:
            try:
                size = int(fields[offset + 2])
                result["size"] = size if size >= 0 else None
            except ValueError:
                pass
        return result

    @classmethod
    def _dcc_message(cls, sender: str, offer: Dict[str, Any]) -> str:
        kind = offer["dcc_type"]
        details: List[str] = []
        if offer.get("filename"):
            details.append(f'file "{offer["filename"]}"')
        if offer.get("size") is not None:
            details.append(f'{offer["size"]:,} bytes')
        address = offer.get("address") or "unknown address"
        if offer.get("port") is not None:
            address += f':{offer["port"]}'
        details.append(address)
        summary = ", ".join(details)
        return (f"DCC {kind} offer from {sender}: {summary}. "
                "DCC transfers are unsupported; this offer was ignored and never auto-accepted.")

    def _on_ctcp(self, net_id, conn, event) -> None:
        # Answer VERSION/PING/TIME politely — some networks k-line silent clients.
        args = event.arguments or []
        command = args[0].upper() if args else ""
        nick = self._safe_dcc_text(self._src_nick(event), 80) or "unknown sender"
        if command == "VERSION":
            conn.ctcp_reply(nick, "VERSION DeepFlux IRC")
        elif command == "PING" and len(args) > 1:
            conn.ctcp_reply(nick, f"PING {args[1]}")
        elif command == "TIME":
            conn.ctcp_reply(nick, time.strftime("TIME %a %b %d %H:%M:%S %Y"))
        elif command == "DCC":
            # Surface metadata only. There is deliberately no accept/download path.
            offer = self._parse_dcc_offer(args)
            detail = self._dcc_message(nick, offer)
            self._record(net_id, KIND_NOTICE, detail, nick=nick)
            self._emit({"type": "dcc_offer", "network": net_id, "nick": nick,
                        "detail": detail, **offer})

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
