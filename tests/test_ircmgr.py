"""Tests for the embedded IRC client (ircmgr) and its agent tools.

State tests are pure unit tests. The client tests run against a fake IRC
server on a localhost socket — no external network access.
"""
from __future__ import annotations

import socket
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from agent.tools import ToolRegistry
from config import DeeptorrentConfig, IRCConfig, IRCNetworkConfig
from ircmgr.client import (
    IRCClientCore,
    IRC_V3_CAPABILITIES,
    MAX_IRC_LINE,
    strip_control_codes,
)
from ircmgr.history import IRCHistoryStore
from ircmgr.state import (
    ChatMessage,
    IRCState,
    KIND_ACTION,
    KIND_MESSAGE,
    irc_casefold,
    irc_equals,
    is_channel,
)


def _wait_for(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# state.py unit tests
# ---------------------------------------------------------------------------

class TestIRCState:
    def test_is_channel(self):
        assert is_channel("#chan") and is_channel("&local")
        assert not is_channel("nick") and not is_channel("")

    def test_ring_buffer_trims(self):
        state = IRCState(buffer_lines=50)
        state.ensure_network("net", "host")
        state.ensure_channel("net", "#c")
        for i in range(120):
            state.record("net", ChatMessage(ts=float(i), kind=KIND_MESSAGE,
                                            nick="n", text=f"m{i}"), "#c")
        msgs = state.get_messages("net", "#c", limit=1000)
        assert len(msgs) == 50
        assert msgs[0].text == "m70" and msgs[-1].text == "m119"

    def test_limit_and_since(self):
        state = IRCState()
        state.ensure_network("net", "host")
        state.ensure_channel("net", "#c")
        for i in range(10):
            state.record("net", ChatMessage(ts=1000.0 + i, kind=KIND_MESSAGE,
                                            text=f"m{i}"), "#c")
        assert [m.text for m in state.get_messages("net", "#c", limit=3)] == ["m7", "m8", "m9"]
        assert [m.text for m in state.get_messages("net", "#c", limit=100, since=1005.0)] == \
            ["m5", "m6", "m7", "m8", "m9"]

    def test_nick_tracking(self):
        state = IRCState()
        state.ensure_network("net", "host")
        state.set_nicks("net", "#c", {"alice": "@", "bob": ""})
        state.add_nick("net", "#c", "carol")
        state.rename_nick("net", "bob", "bobby")
        state.remove_nick("net", "alice", "#c")
        assert state.nicks_of("net", "#c") == {"bobby": "", "carol": ""}

    def test_irc_casemapping_applies_to_channels_and_nicks(self):
        state = IRCState()
        state.ensure_network("net", "host")
        state.ensure_channel("net", "#Chan[Ops]")
        state.ensure_channel("net", "#chan{ops}")
        state.set_nicks("net", "#CHAN{OPS}", {"[Alice]": "@+", "{alice}": "+"})

        assert irc_casefold("[Nick]\\^", "rfc1459") == "{nick}|~"
        assert irc_equals("[Nick]", "{nick}", "rfc1459")
        assert not irc_equals("^Nick", "~nick", "strict-rfc1459")
        assert len(state.snapshot()["networks"][0]["channels"]) == 1
        assert state.nicks_of("net", "#chan{ops}") == {"[Alice]": "@+"}
        state.remove_nick("net", "{ALICE}", "#chan[ops]")
        assert state.nicks_of("net", "#chan{ops}") == {}

    def test_disconnect_clears_nicks(self):
        state = IRCState()
        state.ensure_network("net", "host")
        state.set_nicks("net", "#c", {"alice": ""})
        state.set_connected("net", False)
        assert state.nicks_of("net", "#c") == {}

    def test_search(self):
        state = IRCState()
        state.ensure_network("net", "host")
        state.ensure_channel("net", "#a")
        state.ensure_channel("net", "#b")
        state.record("net", ChatMessage(ts=1.0, kind=KIND_MESSAGE, nick="x",
                                        text="hello ubuntu"), "#a")
        state.record("net", ChatMessage(ts=2.0, kind=KIND_MESSAGE, nick="y",
                                        text="UBUNTU again"), "#b")
        hits = state.search("ubuntu")
        assert len(hits) == 2
        assert {h["channel"] for h in hits} == {"#a", "#b"}
        assert len(state.search("ubuntu", channel="#a")) == 1
        assert state.search("nomatch") == []

    def test_snapshot(self):
        state = IRCState()
        state.ensure_network("net", "irc.example.org", 6697, True)
        state.set_connected("net", True, nick="me")
        state.set_nicks("net", "#c", {"a": "", "b": "@"})
        snap = state.snapshot()
        net = snap["networks"][0]
        assert net["connected"] and net["nick"] == "me"
        assert net["channels"][0]["users"] == 2

    def test_light_accessors_skip_snapshot_builds(self):
        """Hot-path accessors used by the GUI and QUIT/NICK event storms."""
        state = IRCState()
        state.ensure_network("net", "host")
        state.ensure_network("other", "host2")
        assert state.network_connected("net") is False
        assert state.network_link_state("net") == "offline"
        assert state.network_connected("missing") is False
        assert state.network_link_state("missing") == "offline"

        state.set_connecting("net", True)
        assert state.network_link_state("net") == "connecting"
        assert state.network_connected("net") is False

        state.set_connected("net", True, nick="me")
        assert state.network_connected("net") is True
        assert state.network_link_state("net") == "connected"
        assert state.network_link_state("other") == "offline"

        state.ensure_channel("net", "#a")
        state.ensure_channel("net", "#b")
        state.set_nicks("net", "#a", {"me": "", "alice": "@"})
        state.set_nicks("net", "#b", {"me": ""})
        assert state.channel_names("net") == ["#a", "#b"]
        assert state.channel_names("missing") == []
        assert state.channels_of_nick("net", "alice") == ["#a"]
        assert state.channels_of_nick("net", "me") == ["#a", "#b"]
        assert state.channels_of_nick("net", "ghost") == []

        state.record("net", ChatMessage(ts=1.0, kind=KIND_MESSAGE, text="x"), "#a")
        state.record("net", ChatMessage(ts=2.0, kind=KIND_MESSAGE, text="y"), None)
        state.clear_buffer("net", "#a")
        assert state.get_messages("net", "#a", 10) == []
        # Membership survives a buffer clear.
        assert state.nicks_of("net", "#a") == {"me": "", "alice": "@"}
        state.clear_buffer("net", None)
        assert state.get_messages("net", None, 10) == []


# ---------------------------------------------------------------------------
# encrypted persistent history (all paths are test-local)
# ---------------------------------------------------------------------------

class TestIRCHistory:
    def test_disabled_history_creates_no_files(self, tmp_path):
        db_path = tmp_path / "irc.sqlite3"
        key_path = tmp_path / "irc.key"
        client = IRCClientCore(
            IRCConfig(history_enabled=False), str(db_path), str(key_path))
        client.state.ensure_network("net", "host")
        client.state.ensure_channel("net", "#chan")
        client._record("net", KIND_MESSAGE, "do not persist", nick="alice", channel="#chan")

        assert not db_path.exists()
        assert not key_path.exists()
        assert client.get_messages("net", "#chan", 10)[0]["text"] == "do not persist"

    def test_encrypted_at_rest_retention_and_clear(self, tmp_path):
        db_path = tmp_path / "irc.sqlite3"
        key_path = tmp_path / "irc.key"
        store = IRCHistoryStore(str(db_path), str(key_path), retention_days=2)
        old = {
            "id": "old", "ts": 100.0, "kind": "msg", "nick": "SecretNick",
            "text": "Secret old body", "account": "SecretAccount", "tags": {},
        }
        recent = {
            "id": "recent", "ts": 250000.0, "kind": "msg", "nick": "OtherNick",
            "text": "Secret recent body", "account": "", "tags": {"label": "hidden"},
        }
        store.add("net", "PrivateTarget", old)
        store.add("net", "PrivateTarget", recent)

        database_bytes = db_path.read_bytes()
        for secret in (b"SecretNick", b"Secret old body", b"Secret recent body",
                       b"SecretAccount", b"PrivateTarget", b"hidden"):
            assert secret not in database_bytes
        assert [m["id"] for m in store.get_messages("net", "PrivateTarget", 10)] == \
            ["old", "recent"]

        store.cleanup(now=250000.0)
        assert [m["id"] for m in store.get_messages("net", "PrivateTarget", 10)] == \
            ["recent"]
        store.clear()
        assert store.get_messages("net", "PrivateTarget", 10) == []

    def test_merged_reads_search_and_private_opt_in(self, tmp_path):
        db_path = tmp_path / "irc.sqlite3"
        key_path = tmp_path / "irc.key"
        cfg = IRCConfig(history_enabled=True, history_private_messages=False,
                        history_retention_days=30)
        client = IRCClientCore(cfg, str(db_path), str(key_path))
        client.state.ensure_network("net", "host")
        public = client._record(
            "net", KIND_MESSAGE, "persistent needle", nick="alice", channel="#chan")
        client._record("net", KIND_MESSAGE, "private excluded", nick="buddy", channel="buddy")

        # The same event exists in RAM and SQLite but merged reads return it once.
        merged = client.get_messages("net", "#chan", 50)
        assert [m["id"] for m in merged].count(public.event_id) == 1
        assert len(client.search_messages("needle", "net", "#chan", 50)) == 1

        restarted = IRCClientCore(cfg, str(db_path), str(key_path))
        restarted.state.ensure_network("net", "host")
        assert restarted.get_messages("net", "#chan", 50)[0]["text"] == "persistent needle"
        assert restarted.get_messages("net", "buddy", 50) == []

        cfg.history_private_messages = True
        restarted._record(
            "net", KIND_MESSAGE, "private included", nick="buddy", channel="buddy")
        assert restarted.get_messages("net", "buddy", 50)[0]["text"] == "private included"
        restarted.clear_history()
        # In-memory events remain; a fresh instance proves persisted rows were cleared.
        cleared = IRCClientCore(cfg, str(db_path), str(key_path))
        cleared.state.ensure_network("net", "host")
        assert cleared.get_messages("net", "#chan", 50) == []
        assert cleared.search_messages("needle", "net", "#chan", 50) == []

    def test_cached_connection_wal_and_close_is_idempotent(self, tmp_path):
        """One cached WAL connection instead of a fresh fsync'd connection per
        message; close() is safe to call twice and data survives reopen."""
        store = IRCHistoryStore(str(tmp_path / "irc.sqlite3"),
                                str(tmp_path / "irc.key"), 30)
        mode = store._connect().execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        # Same connection object is reused across calls.
        assert store._connect() is store._connect()

        for i in range(50):
            store.add("net", "#chan", {"id": f"e{i}", "ts": time.time(),
                                       "kind": "msg", "nick": "a", "text": f"m{i}"})
        store.close()
        store.close()  # idempotent
        assert store._db is None
        # A new connection can be opened after close (lazy re-create).
        assert len(store.get_messages("net", "#chan", 10)) == 10

        reopened = IRCHistoryStore(str(tmp_path / "irc.sqlite3"),
                                   str(tmp_path / "irc.key"), 30)
        assert len(reopened.get_messages("net", "#chan", 100)) == 50
        reopened.close()


# ---------------------------------------------------------------------------
# Fake IRC server for client integration tests
# ---------------------------------------------------------------------------

class FakeIRCServer:
    """Speaks just enough IRC for the client: NICK/USER → 001, JOIN echo +
    NAMES, PONG collection, scripted pushes."""

    def __init__(self, nick_in_use_first: bool = False,
                 advertised_caps: Optional[List[str]] = None) -> None:
        self.nick_in_use_first = nick_in_use_first
        self.advertised_caps = list(advertised_caps or [])
        self.negotiated_caps: set[str] = set()
        self.received: List[str] = []
        self.received_at: List[tuple[str, float]] = []
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._client: Optional[socket.socket] = None
        self._running = threading.Event()
        self._buf = ""
        self._welcomed = False
        self._nick: str = ""
        self._user_seen = False
        self._cap_started = False
        self._cap_ended = False

    def start(self) -> int:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(0.2)
        self._running.set()
        threading.Thread(target=self._loop, daemon=True).start()
        return self._sock.getsockname()[1]

    def _loop(self) -> None:
        while self._running.is_set():
            if self._client is None:
                try:
                    self._client, _ = self._sock.accept()
                    self._client.settimeout(0.2)
                except socket.timeout:
                    continue
                except OSError:
                    return
            try:
                data = self._client.recv(4096)
                if not data:
                    self._client.close()
                    self._client = None
                    continue
                self._buf += data.decode("utf-8", errors="replace")
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    self._handle_line(line.strip())
            except socket.timeout:
                pass
            except OSError:
                self._client = None

    def _handle_line(self, line: str) -> None:
        with self._lock:
            self.received.append(line)
            self.received_at.append((line, time.monotonic()))
        parts = line.split()
        if not parts:
            return
        cmd = parts[0].upper()
        if cmd == "CAP" and len(parts) > 1:
            subcommand = parts[1].upper()
            if subcommand == "LS":
                self._cap_started = True
                self.send(":srv CAP * LS :" + " ".join(self.advertised_caps))
            elif subcommand == "REQ":
                requested = " ".join(parts[2:]).lstrip(":").split()
                self.negotiated_caps.update(requested)
                self.send(f":srv CAP {self._nick or '*'} ACK :" + " ".join(requested))
            elif subcommand == "END":
                self._cap_ended = True
                self._maybe_welcome()
        elif cmd == "AUTHENTICATE":
            if len(parts) > 1 and parts[1].upper() == "PLAIN":
                self.send("AUTHENTICATE +")
            else:
                self.send(f":srv 903 {self._nick} :SASL authentication successful")
        elif cmd == "NICK" and not self._welcomed:
            nick = parts[1]
            if self.nick_in_use_first and nick == "taken":
                self.send(":srv 433 * taken :Nickname is already in use")
                return
            self._nick = nick
            self._maybe_welcome()
        elif cmd == "USER":
            self._user_seen = True
            self._maybe_welcome()
        elif cmd == "JOIN":
            channel = parts[1].lstrip(":")
            self.send(f":{self._nick}!u@127.0.0.1 JOIN :{channel}")
            self.send(f":srv 353 {self._nick} = {channel} :@opnick {self._nick} someone")
            self.send(f":srv 366 {self._nick} {channel} :End of /NAMES list.")
            self.send(f":srv 332 {self._nick} {channel} :Test topic here")
            # Someone says hi, then does an action.
            self.send(f":someone!s@host PRIVMSG {channel} :hello from someone")
            self.send(f":someone!s@host PRIVMSG {channel} :\x01ACTION waves\x01")
        elif cmd == "LIST":
            self.send(f":srv 321 {self._nick} Channel :Users Name")
            self.send(f":srv 322 {self._nick} #small 3 :quiet corner")
            self.send(f":srv 322 {self._nick} #big 128 :busy place \x02with codes\x02")
            self.send(f":srv 323 {self._nick} :End of /LIST")
        elif cmd == "PRIVMSG" and "echo-message" in self.negotiated_caps:
            target = parts[1]
            body = line.split(" :", 1)[1] if " :" in line else ""
            self.send(f":{self._nick}!u@127.0.0.1 PRIVMSG {target} :{body}")

    def _maybe_welcome(self) -> None:
        if (self._nick and self._user_seen and not self._welcomed
                and (not self._cap_started or self._cap_ended)):
            self._welcomed = True
            self.send(f":srv 001 {self._nick} :Welcome to the test net")
            self.send(f":srv 005 {self._nick} CHANTYPES=# :are supported")

    def send(self, line: str) -> None:
        if self._client:
            try:
                self._client.sendall((line + "\r\n").encode())
            except OSError:
                pass

    def lines_starting(self, prefix: str) -> List[str]:
        with self._lock:
            return [l for l in self.received if l.startswith(prefix)]

    def stop(self) -> None:
        self._running.clear()
        for s in (self._client, self._sock):
            if s:
                try:
                    s.close()
                except OSError:
                    pass


@pytest.fixture
def irc_pair():
    """A running IRCClientCore connected to a fake server."""
    server = FakeIRCServer()
    port = server.start()
    cfg = IRCConfig(buffer_lines=100, flood_delay=0.5, reconnect_max_seconds=60)
    net = IRCNetworkConfig(id="test", host="127.0.0.1", port=port, tls=False,
                           nick="tester")
    client = IRCClientCore(cfg)
    client.connect_network(net)
    try:
        assert _wait_for(lambda: client.state.nick_of("test") == "tester"
                         and client.status()["networks"][0]["connected"]), \
            "client did not connect/register"
        yield client, server
    finally:
        client.shutdown()
        server.stop()


class TestIRCClientCore:
    def test_cap_negotiation_server_time_metadata_and_echo_dedupe(self):
        advertised = sorted(IRC_V3_CAPABILITIES) + ["draft/unsupported"]
        server = FakeIRCServer(advertised_caps=advertised)
        port = server.start()
        cfg = IRCConfig(buffer_lines=100, flood_delay=0.5)
        net = IRCNetworkConfig(id="caps", host="127.0.0.1", port=port,
                               tls=False, nick="tester", channels=["#test"])
        client = IRCClientCore(cfg)
        events = []
        client.add_listener(events.append)
        try:
            client.connect_network(net)
            assert _wait_for(lambda: client.status()["networks"][0]["connected"])
            assert _wait_for(lambda: set(client.status()["networks"][0][
                "negotiated_caps"]) == set(IRC_V3_CAPABILITIES))
            assert server.lines_starting("CAP LS") == ["CAP LS 302"]
            request = server.lines_starting("CAP REQ")[0]
            assert "draft/unsupported" not in request
            assert all(cap in request for cap in IRC_V3_CAPABILITIES)

            server.send(
                "@time=2024-01-02T03:04:05.000Z;account=alice "
                ":someone!s@host PRIVMSG #test :timestamped metadata")
            expected_ts = datetime.fromisoformat(
                "2024-01-02T03:04:05+00:00").timestamp()
            assert _wait_for(lambda: any(m["text"] == "timestamped metadata"
                                         for m in client.get_messages("caps", "#test", 50)))
            timestamped = next(m for m in client.get_messages("caps", "#test", 50)
                               if m["text"] == "timestamped metadata")
            assert timestamped["ts"] == expected_ts
            assert timestamped["account"] == "alice"
            assert timestamped["tags"]["time"].startswith("2024-01-02")

            # With echo-message active, no local copy is recorded; the fake
            # server echo is the one normalized event.
            client.send_message("caps", "#test", "one echoed copy")
            assert _wait_for(lambda: any(m["text"] == "one echoed copy"
                                         for m in client.get_messages("caps", "#test", 50)))
            echoed = [m for m in client.get_messages("caps", "#test", 50)
                      if m["text"] == "one echoed copy"]
            assert len(echoed) == 1 and echoed[0]["nick"] == "tester"
            emitted = [event for event in events if event.get("text") == "one echoed copy"]
            assert len(emitted) == 1 and emitted[0]["own"] is True
        finally:
            client.shutdown()
            server.stop()

    def test_cap_preserves_sasl_plain_and_tracks_account_away(self):
        server = FakeIRCServer(advertised_caps=[
            "sasl=PLAIN", "account-notify", "away-notify", "server-time"])
        port = server.start()
        cfg = IRCConfig(flood_delay=0.5)
        net = IRCNetworkConfig(
            id="sasl-cap", host="127.0.0.1", port=port, tls=False, nick="tester",
            sasl_account="account", sasl_password="sasl-secret", channels=["#test"])
        client = IRCClientCore(cfg)
        events = []
        client.add_listener(events.append)
        try:
            client.connect_network(net)
            assert _wait_for(lambda: client.status()["networks"][0]["connected"])
            request = server.lines_starting("CAP REQ")[0]
            assert "sasl" in request and "server-time" in request
            assert server.lines_starting("AUTHENTICATE PLAIN")
            assert any(line.startswith("AUTHENTICATE ") and line != "AUTHENTICATE PLAIN"
                       for line in server.received)

            server.send(":someone!u@host ACCOUNT logged-in")
            server.send(":someone!u@host AWAY :gone")
            assert _wait_for(lambda: client.state.user_metadata("sasl-cap", "someone") == {
                "account": "logged-in", "away": True})
            server.send(":someone!u@host AWAY")
            assert _wait_for(lambda: client.state.user_metadata(
                "sasl-cap", "someone")["away"] is False)
            assert any(event.get("type") == "user_metadata" for event in events)
        finally:
            client.shutdown()
            server.stop()

    def test_connect_join_chat(self, irc_pair):
        client, server = irc_pair
        client.join("test", "#test")
        assert _wait_for(lambda: "someone" in client.state.nicks_of("test", "#test"))
        nicks = client.state.nicks_of("test", "#test")
        assert nicks.get("opnick") == "@" and "tester" in nicks
        assert _wait_for(lambda: client.state.topic_of("test", "#test") == "Test topic here")

        msgs = client.get_messages("test", "#test", limit=50)
        texts = [(m["kind"], m["nick"], m["text"]) for m in msgs]
        assert (KIND_MESSAGE, "someone", "hello from someone") in texts
        assert (KIND_ACTION, "someone", "waves") in texts

    def test_send_message_and_throttle(self, irc_pair):
        client, server = irc_pair
        client.join("test", "#test")
        assert _wait_for(lambda: server.lines_starting("JOIN"))
        t0 = time.monotonic()
        client.send_message("test", "#test", "first message")
        client.send_message("test", "#test", "second message")
        assert _wait_for(lambda: len(server.lines_starting("PRIVMSG")) >= 2, timeout=10)
        privs = server.lines_starting("PRIVMSG")
        assert privs[0] == "PRIVMSG #test :first message"
        assert privs[1] == "PRIVMSG #test :second message"
        # flood_delay=0.5 → the second message must have been paced.
        assert time.monotonic() - t0 >= 0.4

    def test_unicode_splitting_counts_wire_bytes_and_action_overhead(self):
        target = "#" + "long-target" * 8
        text = "🙂" * 150

        message_chunks = IRCClientCore._split(text, target=target)
        action_chunks = IRCClientCore._split(text, target=target, is_action=True)

        assert "".join(message_chunks) == text
        assert "".join(action_chunks) == text
        assert len(action_chunks) >= len(message_chunks) > 1
        for chunk in message_chunks:
            assert len(f"PRIVMSG {target} :{chunk}".encode("utf-8")) <= MAX_IRC_LINE
        for chunk in action_chunks:
            wire = f"PRIVMSG {target} :\x01ACTION {chunk}\x01"
            assert len(wire.encode("utf-8")) <= MAX_IRC_LINE

    def test_notice_and_user_raw_share_outbound_pacing(self, irc_pair):
        client, server = irc_pair
        client.send_message("test", "#test", "paced-message")
        client.send_notice("test", "#test", "paced-notice")
        client.send_raw("test", "WHO #test")

        assert _wait_for(
            lambda: any(line.startswith("WHO #test") for line in server.received), timeout=5)
        paced = [
            (line, ts) for line, ts in server.received_at
            if line.startswith(("PRIVMSG #test :paced-message",
                                "NOTICE #test :paced-notice", "WHO #test"))
        ]
        assert [line.split(" ", 1)[0] for line, _ in paced] == ["PRIVMSG", "NOTICE", "WHO"]
        assert paced[-1][1] - paced[0][1] >= 0.8

    def test_ping_pong_automatic(self, irc_pair):
        client, server = irc_pair
        server.send("PING :keepalive123")
        assert _wait_for(lambda: server.lines_starting("PONG"))
        assert "keepalive123" in server.lines_starting("PONG")[0]

    def test_private_message_query_buffer(self, irc_pair):
        client, server = irc_pair
        server.send(":buddy!b@host PRIVMSG tester :psst hello")
        assert _wait_for(lambda: client.get_messages("test", "buddy", limit=5))
        msg = client.get_messages("test", "buddy", limit=5)[0]
        assert msg["nick"] == "buddy" and "psst" in msg["text"]

    def test_nick_in_use_fallback(self):
        server = FakeIRCServer(nick_in_use_first=True)
        port = server.start()
        cfg = IRCConfig(flood_delay=0.5)
        net = IRCNetworkConfig(id="t2", host="127.0.0.1", port=port, tls=False,
                               nick="taken")
        client = IRCClientCore(cfg)
        try:
            client.connect_network(net)
            # New fallback format: base + zero-padded two-digit suffix (taken01).
            assert _wait_for(lambda: client.state.nick_of("t2") == "taken01")
        finally:
            client.shutdown()
            server.stop()

    def test_disconnect_marks_state(self, irc_pair):
        client, server = irc_pair
        server.stop()  # drop the link
        assert _wait_for(lambda: not client.status()["networks"][0]["connected"])

    def test_strip_control_codes(self):
        assert strip_control_codes("\x02bold\x02 \x0312,4colored\x0f plain") == \
            "bold colored plain"

    def test_feature_casemapping_and_multi_prefix_names(self):
        client = IRCClientCore(IRCConfig())
        client.state.ensure_network("net", "host")
        client._on_featurelist(
            "net", MagicMock(),
            SimpleNamespace(arguments=["CHANTYPES=#", "CASEMAPPING=strict-rfc1459",
                                       "PREFIX=(qaohv)~&@%+"]),
        )
        client._on_namreply(
            "net", MagicMock(),
            SimpleNamespace(arguments=["=", "#Chan", "@+Alice +bob!u@host"]),
        )

        assert client.state.casemapping_of("net") == "strict-rfc1459"
        assert client.state.nicks_of("net", "#chan") == {"Alice": "@+", "bob": "+"}

    def test_dcc_send_is_sanitized_visible_metadata_and_never_accepted(self):
        client = IRCClientCore(IRCConfig())
        client.state.ensure_network("net", "host")
        events = []
        client.add_listener(events.append)
        conn = MagicMock()
        event = SimpleNamespace(
            source="bad<sender>!u@host",
            arguments=["DCC", 'SEND "../../evil <name>.txt" 3232235777 5000 12345'],
        )

        client._on_ctcp("net", conn, event)

        conn.assert_not_called()
        offer = events[-1]
        assert offer["type"] == "dcc_offer" and offer["dcc_type"] == "SEND"
        assert offer["filename"] == "evil <name>.txt"
        assert offer["address"] == "192.168.1.1" and offer["port"] == 5000
        assert offer["size"] == 12345
        assert "unsupported" in offer["detail"] and "never auto-accepted" in offer["detail"]
        visible = client.get_messages("net", limit=5)[-1]
        assert "bad<sender>" in visible["text"] and "12,345 bytes" in visible["text"]

    def test_invalid_sasl_does_not_reuse_server_password(self):
        server = FakeIRCServer()
        port = server.start()
        cfg = IRCConfig(flood_delay=0.5)
        net = IRCNetworkConfig(
            id="bad-sasl", host="127.0.0.1", port=port, tls=False,
            nick="tester", password="server-pass", sasl_account="account",
            sasl_password="",
        )
        client = IRCClientCore(cfg)
        events = []
        client.add_listener(events.append)
        try:
            client.connect_network(net)
            assert _wait_for(lambda: any(e.get("state") == "error" for e in events))
            assert server.received == []
            errors = client.get_messages("bad-sasl", limit=10)
            assert any("will not be reused for SASL" in msg["text"] for msg in errors)
            assert not client.status()["networks"][0]["connecting"]
        finally:
            client.shutdown()
            server.stop()

    def test_sasl_secret_is_distinct_from_server_pass(self):
        cfg = IRCConfig()
        net = IRCNetworkConfig(
            id="sasl", host="irc.example", tls=False, nick="tester",
            password="server-pass", sasl_account="account",
            sasl_password="sasl-pass",
        )
        client = IRCClientCore(cfg)
        client._reactor = MagicMock()

        client._do_connect(net)

        kwargs = client._reactor.server.return_value.connect.call_args.kwargs
        assert kwargs["sasl_login"] == "account"
        assert kwargs["password"] == "sasl-pass"
        assert kwargs["password"] != net.password

    def test_reconnect_failures_are_capped_and_visible(self):
        cfg = IRCConfig()
        cfg.reconnect_max_attempts = 2
        net = IRCNetworkConfig(id="down", host="irc.invalid", tls=False)
        client = IRCClientCore(cfg)
        client._reactor = MagicMock()
        conn = client._reactor.server.return_value
        conn.connect.side_effect = OSError("permanent failure")
        events = []
        client.add_listener(events.append)
        client._running.set()
        try:
            client._do_connect(net)
            assert client._nets["down"]["reconnect_attempts"] == 1
            client._nets["down"]["reconnect_at"] = 0.0
            client._check_reconnects()

            rt = client._nets["down"]
            assert rt["reconnect_stopped"] is True
            assert rt["reconnect_at"] is None
            assert conn.connect.call_count == 2
            assert any("Reconnect stopped after 2" in e.get("detail", "") for e in events)
            visible = client.status()["networks"][0]
            assert visible["reconnect_stopped"] is True
            assert "Reconnect stopped after 2" in visible["error"]
        finally:
            client._running.clear()

    def test_channel_list(self, irc_pair):
        client, server = irc_pair
        client.send_raw("test", "LIST")
        assert _wait_for(lambda: client.state.chanlist_of("test"))
        rows = client.state.chanlist_of("test")
        # Sorted by user count, control codes stripped from topics.
        assert [r["channel"] for r in rows] == ["#big", "#small"]
        assert rows[0]["users"] == 128
        assert rows[0]["topic"] == "busy place with codes"
        assert client.state.chanlist_ts("test") > 0

    def test_whois_and_away_numerics_surface_in_server_buffer(self, irc_pair):
        """WHOIS replies (311/317/319/330/318) and away acks (305/306) must be
        recorded in the server buffer so /whois, /away and /back have answers."""
        client, server = irc_pair
        server.send(":srv 311 tester buddy budy host * :Buddy Real")
        server.send(":srv 312 tester buddy srv.example :The test server")
        server.send(":srv 317 tester buddy 42 1700000000 :seconds idle")
        server.send(":srv 319 tester tester :@#chan +#other")
        server.send(":srv 330 tester buddy account :is logged in as")
        server.send(":srv 318 tester buddy :End of /WHOIS list.")
        server.send(":srv 306 tester :You have been marked as being away")
        server.send(":srv 305 tester :You are no longer marked as being away")

        def _joined() -> Optional[str]:
            texts = " | ".join(m["text"] for m in client.get_messages("test", limit=50))
            return texts if "End of /WHOIS list." in texts else None

        assert _wait_for(lambda: _joined() is not None)
        texts = _joined()
        assert "buddy is budy@host (Buddy Real)" in texts
        assert "buddy is on srv.example: The test server" in texts
        assert "buddy idle for 0h00m42s" in texts
        assert "signed on" in texts
        assert "tester is in @#chan +#other" in texts
        assert "buddy is logged in as account" in texts
        assert "You are marked as being away" in texts
        assert "You are no longer marked as being away" in texts

    def test_is_connected_is_a_light_single_network_check(self):
        client = IRCClientCore(IRCConfig())
        client.state.ensure_network("net", "host")
        assert client.is_connected("net") is False
        assert client.is_connected("missing") is False
        client.state.set_connected("net", True, nick="me")
        assert client.is_connected("net") is True

    def test_auto_list_on_connect_channelless(self):
        """A network with no configured channels auto-requests /LIST on connect
        and the chanlist arrives without any manual command."""
        server = FakeIRCServer()
        port = server.start()
        cfg = IRCConfig(buffer_lines=100, flood_delay=0.5)
        net = IRCNetworkConfig(id="channellless", host="127.0.0.1", port=port,
                               tls=False, nick="tester")  # no channels
        client = IRCClientCore(cfg)
        try:
            client.connect_network(net)
            assert _wait_for(lambda: client.status()["networks"][0]["connected"])
            # The client should have auto-sent LIST — wait for the chanlist.
            assert _wait_for(lambda: client.state.chanlist_of("channellless"))
            rows = client.state.chanlist_of("channellless")
            assert len(rows) == 2  # #big + #small from FakeIRCServer
            # Auto-list retry should have stopped after listend.
            rt = client._nets.get("channellless")
            assert rt and not rt.get("auto_list_pending")
        finally:
            client.shutdown()
            server.stop()

    def test_no_auto_list_when_channels_configured(self):
        """A network WITH configured channels joins them and does NOT auto-LIST."""
        server = FakeIRCServer()
        port = server.start()
        cfg = IRCConfig(buffer_lines=100, flood_delay=0.5)
        net = IRCNetworkConfig(id="withchans", host="127.0.0.1", port=port,
                               tls=False, nick="tester", channels=["#test"])
        client = IRCClientCore(cfg)
        try:
            client.connect_network(net)
            assert _wait_for(lambda: client.status()["networks"][0]["connected"])
            # Channel should be joined (nick list populated).
            assert _wait_for(lambda: "tester" in client.state.nicks_of("withchans", "#test"))
            # No auto-LIST — chanlist stays empty (give a moment to be sure).
            time.sleep(0.5)
            assert not client.state.chanlist_of("withchans")
            rt = client._nets.get("withchans")
            assert rt and not rt.get("auto_list_pending")
        finally:
            client.shutdown()
            server.stop()

    def test_multi_network_isolation(self):
        """Two networks on one client must not cross-contaminate channels,
        nicks, or messages. Regression test for the bug where the irc
        library's reactor-global handlers fired every network's per-connection
        handler for every event, so a JOIN on libera was also recorded under
        iptorrents (both networks showed both channels)."""
        s1, s2 = FakeIRCServer(), FakeIRCServer()
        p1, p2 = s1.start(), s2.start()
        cfg = IRCConfig(buffer_lines=100, flood_delay=0.5, reconnect_max_seconds=60)
        n1 = IRCNetworkConfig(id="libera", host="127.0.0.1", port=p1, tls=False,
                              nick="me", channels=["#DeepFlux"])
        n2 = IRCNetworkConfig(id="iptorrents", host="127.0.0.1", port=p2, tls=False,
                              nick="me", channels=["#iptorrents"])
        client = IRCClientCore(cfg)
        try:
            client.connect_network(n1)
            client.connect_network(n2)
            assert _wait_for(lambda: [x["connected"] for x in client.status()["networks"]]
                             == [True, True], timeout=10)
            assert _wait_for(
                lambda: all(n["channels"] for n in client.status()["networks"]), timeout=10)
            snap = {n["id"]: n for n in client.status()["networks"]}
            # Each network only owns the channel it joined — no mirroring.
            assert [c["name"] for c in snap["libera"]["channels"]] == ["#DeepFlux"]
            assert [c["name"] for c in snap["iptorrents"]["channels"]] == ["#iptorrents"]
            # A message pushed on one network must not appear in the other.
            s1.send(":stranger!s@host PRIVMSG #DeepFlux :only on libera")
            s2.send(":stranger!s@host PRIVMSG #iptorrents :only on iptorrents")
            assert _wait_for(lambda: any("only on libera" in m["text"]
                                         for m in client.get_messages("libera", "#DeepFlux", limit=10)))
            assert _wait_for(lambda: any("only on iptorrents" in m["text"]
                                         for m in client.get_messages("iptorrents", "#iptorrents", limit=10)))
            lib_msgs = [m["text"] for m in client.get_messages("libera", "#DeepFlux", limit=10)]
            ipt_msgs = [m["text"] for m in client.get_messages("iptorrents", "#iptorrents", limit=10)]
            assert "only on libera" in lib_msgs and "only on iptorrents" not in lib_msgs
            assert "only on iptorrents" in ipt_msgs and "only on libera" not in ipt_msgs
        finally:
            client.shutdown()
            s1.stop()
            s2.stop()


# ---------------------------------------------------------------------------
# agent tools (irc_*)
# ---------------------------------------------------------------------------

@pytest.fixture
def tools_with_irc():
    engine = MagicMock()
    config = DeeptorrentConfig()
    config.llm.memory_enabled = False
    config.irc = IRCConfig(buffer_lines=100, flood_delay=0.5)
    client = IRCClientCore(config.irc)
    tools = ToolRegistry(engine, config, irc_client=client)
    yield tools, client
    client.shutdown()


class TestIRCTools:
    def test_tools_registered(self, tools_with_irc):
        tools, _ = tools_with_irc
        names = [t["function"]["name"] for t in tools.list_tools()]
        for expected in ("irc_status", "irc_list_messages", "irc_search_messages",
                         "irc_send_message", "irc_join", "irc_part"):
            assert expected in names

    def test_status_no_networks(self, tools_with_irc):
        tools, _ = tools_with_irc
        result = tools.call("irc_status", {})
        assert result["success"] and result["networks"] == []

    def test_list_messages_unconnected(self, tools_with_irc):
        tools, _ = tools_with_irc
        result = tools.call("irc_list_messages", {"channel": "#x"})
        assert result["success"] is False
        assert "connect" in result["error"].lower()

    def test_tools_against_fake_server(self):
        server = FakeIRCServer()
        port = server.start()
        engine = MagicMock()
        config = DeeptorrentConfig()
        config.llm.memory_enabled = False
        config.irc = IRCConfig(buffer_lines=100, flood_delay=0.5)
        config.irc.networks.append(
            IRCNetworkConfig(id="test", host="127.0.0.1", port=port,
                             tls=False, nick="tester"))
        client = IRCClientCore(config.irc)
        tools = ToolRegistry(engine, config, irc_client=client)
        try:
            client.connect_network(config.irc.networks[0])
            assert _wait_for(lambda: client.status()["networks"][0]["connected"])

            # join via the tool
            result = tools.call("irc_join", {"channel": "#test"})
            assert result["success"]
            assert _wait_for(lambda: "someone" in client.state.nicks_of("test", "#test"))

            # read the buffer via the tool
            result = tools.call("irc_list_messages", {"channel": "#test", "limit": 20})
            assert result["success"] and result["count"] > 0
            assert any("hello from someone" in line for line in result["messages"])

            # search via the tool
            result = tools.call("irc_search_messages", {"query": "hello"})
            assert result["success"] and result["count"] >= 1
            assert "test/#test" in result["hits"][0]

            # send via the tool
            result = tools.call("irc_send_message",
                                {"target": "#test", "text": "agent says hi"})
            assert result["success"]
            assert _wait_for(lambda: server.lines_starting("PRIVMSG"))
            assert "agent says hi" in server.lines_starting("PRIVMSG")[0]

            # part via the tool
            result = tools.call("irc_part", {"channel": "#test"})
            assert result["success"]
            assert _wait_for(lambda: server.lines_starting("PART"))
        finally:
            client.shutdown()
            server.stop()

    def test_extended_tools_against_fake_server(self):
        server = FakeIRCServer()
        port = server.start()
        engine = MagicMock()
        config = DeeptorrentConfig()
        config.llm.memory_enabled = False
        config.irc = IRCConfig(buffer_lines=100, flood_delay=0.5)
        config.irc.networks.append(
            IRCNetworkConfig(id="test", host="127.0.0.1", port=port,
                             tls=False, nick="tester"))
        client = IRCClientCore(config.irc)
        tools = ToolRegistry(engine, config, irc_client=client)
        try:
            # connect via the tool
            result = tools.call("irc_connect", {"network": "test"})
            assert result["success"]
            assert _wait_for(lambda: client.status()["networks"][0]["connected"])

            # already-connected connect is a no-op note, not an error
            result = tools.call("irc_connect", {"network": "test"})
            assert result["success"] and "Already" in result["note"]

            result = tools.call("irc_connect", {"network": "nonexistent"})
            assert result["success"] is False

            # nick list via the tool
            result = tools.call("irc_join", {"channel": "#test"})
            assert result["success"]
            assert _wait_for(lambda: "someone" in client.state.nicks_of("test", "#test"))
            result = tools.call("irc_list_nicks", {"channel": "#test"})
            assert result["success"]
            assert any(n.endswith("someone") for n in result["nicks"])

            result = tools.call("irc_list_nicks", {"channel": "#notjoined"})
            assert result["success"] is False

            # action + notice via the tools
            result = tools.call("irc_send_action", {"target": "#test", "text": "waves"})
            assert result["success"]
            result = tools.call("irc_send_notice", {"target": "#test", "text": "psst"})
            assert result["success"]

            # A fresh channel-list request is nonblocking: it queues LIST and
            # returns the current cache (if any), then refresh=false reads it.
            previous_ts = client.state.chanlist_ts("test")
            started = time.monotonic()
            result = tools.call("irc_list_channels", {"network": "test"})
            assert result["success"] and result["pending"] is True
            assert time.monotonic() - started < 0.25
            assert _wait_for(lambda: client.state.chanlist_ts("test") > previous_ts)
            result = tools.call("irc_list_channels", {"network": "test", "refresh": False})
            assert result["success"] and result["pending"] is False
            assert result["count"] == 2
            assert [c["channel"] for c in result["channels"]] == ["#big", "#small"]

            # nick change via the tool
            result = tools.call("irc_set_nick", {"new_nick": "tester2"})
            assert result["success"]

            # disconnect via the tool
            result = tools.call("irc_disconnect", {"network": "test"})
            assert result["success"]
            assert _wait_for(lambda: not client.status()["networks"][0]["connected"])
        finally:
            client.shutdown()
            server.stop()

    def test_owned_irc_client_shutdown_uses_lifecycle_api(self):
        engine = MagicMock()
        config = DeeptorrentConfig()
        config.llm.memory_enabled = False
        tools = ToolRegistry(engine, config)
        owned = MagicMock()
        tools._irc_client = owned
        tools._owns_irc_client = True

        tools.shutdown()

        owned.shutdown.assert_called_once_with()
        owned.stop.assert_not_called()
        assert tools._irc_client is None
        assert tools._owns_irc_client is False

    def test_extended_tools_classification(self):
        from agent.loop import DESTRUCTIVE_TOOLS, READ_ONLY_TOOLS, REQUIRES_CONFIRMATION

        assert {"irc_list_nicks", "irc_list_channels"} <= READ_ONLY_TOOLS
        assert {"irc_connect", "irc_disconnect", "irc_send_action", "irc_send_notice",
                "irc_set_nick", "irc_send_raw"} <= REQUIRES_CONFIRMATION

    def test_read_only_and_confirmation_classification(self):
        from agent.loop import DESTRUCTIVE_TOOLS, READ_ONLY_TOOLS, REQUIRES_CONFIRMATION

        assert {"irc_status", "irc_list_messages", "irc_search_messages"} <= READ_ONLY_TOOLS
        assert {"irc_send_message", "irc_join", "irc_part"} <= REQUIRES_CONFIRMATION


# ---------------------------------------------------------------------------
# GUI smoke test (offscreen Qt) — widget construction + rendering path
# ---------------------------------------------------------------------------

class TestIRCTabGUI:
    def test_history_completion_search_unread_highlights_dcc_and_safe_nick_actions(self):
        pytest.importorskip("PySide6")
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from gui.irc_tab import IRCTab

        app = QApplication.instance() or QApplication([])
        config = DeeptorrentConfig()
        config.irc.networks.append(
            IRCNetworkConfig(id="net", host="irc.example", channels=["#Chan"]))
        client = IRCClientCore(config.irc)
        client.state.ensure_network("net", "irc.example")
        client.state.set_connected("net", True, nick="Me[1]")
        client.state.set_nicks("net", "#Chan", {"Alice": "@+", "Me[1]": ""})
        tab = IRCTab(config, client)
        try:
            root = tab.tree.topLevelItem(0)
            channel_item = tab._ensure_channel_item("net", "#Chan")
            tab.tree.setCurrentItem(channel_item)
            app.processEvents()

            tab.input.remember("first")
            tab.input.remember("second")
            tab.input.setText("draft")
            tab.input._move_history(-1)
            assert tab.input.text() == "second"
            tab.input._move_history(-1)
            assert tab.input.text() == "first"
            tab.input._move_history(1)
            tab.input._move_history(1)
            assert tab.input.text() == "draft"

            assert tab._complete_input("Al", 2) == ("Alice: ", 7)
            assert tab._complete_input("/join #ch", 9) == ("/join #Chan ", 12)

            tab.chat.setPlainText("Needle one; needle two")
            tab.search_edit.setText("needle")
            assert tab.search_status.text() == "2 matches"
            tab._find_next()
            assert tab.chat.textCursor().selectedText().lower() == "needle"

            tab.tree.setCurrentItem(root)
            tab._on_event({"type": "message", "network": "net", "channel": "#cHAN",
                           "nick": "Alice", "text": "hello me{1}"})
            assert "[1]" in channel_item.text(0)
            assert ("net", "#Chan") in tab._highlights
            tab.tree.setCurrentItem(channel_item)
            assert "[1]" not in channel_item.text(0)

            tab.tree.setCurrentItem(root)
            tab._on_event({
                "type": "dcc_offer", "network": "net", "nick": "bad<sender>",
                "detail": ('DCC SEND offer from bad<sender>: file "evil<name>.txt", '
                           "1,024 bytes, 192.0.2.1:5000. DCC transfers are unsupported; "
                           "this offer was ignored and never auto-accepted."),
            })
            transcript = tab.chat.toHtml()
            assert "unsupported" in transcript and "never auto-accepted" in transcript
            assert "bad&lt;sender&gt;" in transcript and "evil&lt;name&gt;.txt" in transcript

            with patch.object(client, "send_raw") as send_raw:
                tab._whois_nick("Alice")
                send_raw.assert_called_once_with("net", "WHOIS Alice")
            tab._copy_nick("Alice")
            assert QApplication.clipboard().text() == "Alice"
            tab._open_query("Alice")
            assert tab._current == ("net", "Alice")
        finally:
            tab.deleteLater()
            client.shutdown()

    def test_network_manager_crud_keeps_ids_unique(self):
        pytest.importorskip("PySide6")
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QMessageBox

        from gui.irc_tab import NetworkManagerDialog

        app = QApplication.instance() or QApplication([])
        networks = [IRCNetworkConfig(id="irc-example", host="irc.example")]
        manager = NetworkManagerDialog(networks)
        try:
            added = IRCNetworkConfig(id="irc-example", host="irc.example")
            with patch("gui.irc_tab.NetworkDialog.exec", return_value=1), \
                    patch("gui.irc_tab.NetworkDialog.to_config", return_value=added):
                manager._add()
            assert [net.id for net in networks] == ["irc-example", "irc-example-2"]

            manager.list.setCurrentRow(1)
            with patch("gui.irc_tab.NetworkDialog.exec", return_value=1), \
                    patch("gui.irc_tab.NetworkDialog.to_config", return_value=networks[1]):
                manager._edit()
            assert networks[1].id == "irc-example-2"

            manager.list.setCurrentRow(1)
            with patch("gui.irc_tab.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes):
                manager._delete()
            assert [net.id for net in networks] == ["irc-example"]
            assert manager.changed
        finally:
            manager.deleteLater()
            app.processEvents()

    def test_history_privacy_settings_and_clear(self):
        pytest.importorskip("PySide6")
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

        from gui.irc_tab import IRCSettingsDialog

        app = QApplication.instance() or QApplication([])
        irc_config = IRCConfig(history_enabled=False, history_private_messages=False,
                               history_retention_days=30)
        client = MagicMock()
        dialog = IRCSettingsDialog(irc_config, client)
        try:
            assert "off by default" in dialog.findChildren(QLabel)[0].text()
            dialog.history_check.setChecked(True)
            dialog.private_check.setChecked(True)
            dialog.retention_spin.setValue(45)
            with patch("gui.irc_tab.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes), \
                    patch("gui.irc_tab.QMessageBox.information"):
                dialog._clear_history()
            client.clear_history.assert_called_once_with()

            dialog.accept()
            assert irc_config.history_enabled is True
            assert irc_config.history_private_messages is True
            assert irc_config.history_retention_days == 45
            client.configure_history.assert_called_once_with()
        finally:
            dialog.deleteLater()
            app.processEvents()

    def test_tab_renders_state(self, tmp_path):
        pytest.importorskip("PySide6")
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from gui.irc_tab import IRCTab

        app = QApplication.instance() or QApplication([])
        config = DeeptorrentConfig()
        config.irc.networks.append(
            IRCNetworkConfig(id="libera", host="irc.libera.chat",
                             channels=["#deepflux-test"]))
        client = IRCClientCore(config.irc)
        try:
            # IRCTab.shutdown() persists the config — redirect it so the test
            # can NEVER overwrite the real ~/.deeptorrent/config.json.
            with patch.object(DeeptorrentConfig, "default_config_path",
                              return_value=str(tmp_path / "config.json")):
                tab = IRCTab(config, client)

                # Feed state the way the network-thread handlers would.
                client.state.ensure_network("libera", "irc.libera.chat")
                client.state.set_connected("libera", True, nick="tester")
                client.state.set_topic("libera", "#deepflux-test", "Welcome & enjoy")
                client.state.set_nicks("libera", "#deepflux-test",
                                       {"tester": "", "someone": "@"})
                client.state.record("libera", ChatMessage(
                    ts=time.time(), kind=KIND_MESSAGE, nick="someone",
                    text="hello <b>world</b> https://x.co/"), "#deepflux-test")
                client._emit({"type": "state", "network": "libera",
                              "state": "connected", "nick": "tester"})
                client._emit({"type": "message", "network": "libera",
                              "channel": "#deepflux-test", "nick": "someone",
                              "text": "hello <b>world</b> https://x.co/"})
                client._emit({"type": "topic", "network": "libera",
                              "channel": "#deepflux-test", "topic": "Welcome & enjoy"})
                for _ in range(10):
                    app.processEvents()
                    time.sleep(0.02)

                root = tab.tree.topLevelItem(0)
                assert root is not None and "irc.libera.chat" in root.text(0)
                assert root.childCount() == 1
                tab.tree.setCurrentItem(root.child(0))
                app.processEvents()

                out = tab.chat.toHtml()
                assert "hello" in out
                assert "&lt;b&gt;" in out        # HTML escaped
                assert "href=" in out             # URL linkified
                assert [tab.nicks.item(i).text() for i in range(tab.nicks.count())] == \
                    ["@someone", "tester"]
                assert tab.topic_label.text() == "Welcome & enjoy"

                # Transcript links open externally only for absolute HTTP(S).
                from PySide6.QtCore import QUrl
                with patch("gui.irc_tab.QDesktopServices.openUrl") as open_url:
                    tab._open_external_link(QUrl("https://example.org/path"))
                    tab._open_external_link(QUrl("javascript:alert(1)"))
                    tab._open_external_link(QUrl("file:///tmp/secret"))
                    open_url.assert_called_once()

                # Leaving/kicking the selected channel must not leave a stale
                # send target or recreate the removed tree node.
                client.state.drop_channel("libera", "#deepflux-test")
                client._emit({"type": "parted", "network": "libera",
                              "channel": "#deepflux-test"})
                client._emit({"type": "part", "network": "libera",
                              "channel": "#deepflux-test", "nick": "tester", "own": True})
                for _ in range(5):
                    app.processEvents()
                    time.sleep(0.01)
                assert tab._current == ("libera", None)
                assert root.childCount() == 0

                tab.shutdown()
            # The write went to the sandbox, not the user's real config.
            assert (tmp_path / "config.json").exists()
        finally:
            client.shutdown()


# ---------------------------------------------------------------------------
# Default network fixes + config migration (2026-09 verified endpoints)
# ---------------------------------------------------------------------------

class TestIRCDefaultsMigration:
    def _write(self, tmp_path, payload):
        import json
        path = tmp_path / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_defaults_carry_no_channels_and_no_dead_networks(self):
        from config import DEFAULT_IRC_NETWORKS

        by_id = {n.id: n for n in DEFAULT_IRC_NETWORKS}
        assert all(not n.channels for n in DEFAULT_IRC_NETWORKS)
        assert "morethantv" not in by_id
        # Verified-2026-09 endpoints (see the DEFAULT_IRC_NETWORKS comment):
        # TLS ports that were dead or legacy-cipher-only moved to plain 6667.
        for net_id in ("undernet", "geekshed", "p2p-network",
                       "brokensphere", "iptorrents"):
            assert (by_id[net_id].port, by_id[net_id].tls) == (6667, False), net_id
        # animebytes.tv was seized — the network lives at animefriends.moe.
        assert by_id["animebytes"].host == "irc.animefriends.moe"
        assert by_id["animebytes"].port == 7000 and by_id["animebytes"].tls

    def test_from_file_migrates_dead_entries_and_strips_default_channels(self, tmp_path):
        old = {"irc": {"networks": [
            {"id": "undernet", "host": "irc.undernet.org", "port": 6697,
             "tls": True, "nick": "DeepFluxUser"},
            {"id": "iptorrents", "host": "irc.iptorrents.com", "port": 7000,
             "tls": True, "nick": "DeepFluxUser", "channels": ["#iptorrents"]},
            {"id": "animebytes", "host": "irc.animebytes.tv", "port": 7000,
             "tls": True, "nick": "DeepFluxUser", "channels": ["#support"]},
            {"id": "morethantv", "host": "irc.morethan.tv", "port": 6669,
             "tls": True, "nick": "DeepFluxUser",
             "channels": ["#help", "#morethan.tv-disabled"]},
            {"id": "orpheus", "host": "irc.orpheus.network", "port": 7000,
             "tls": True, "nick": "DeepFluxUser", "channels": ["#disabled", "#help"]},
            {"id": "mine", "host": "irc.example.com", "port": 1234, "tls": False,
             "nick": "me", "channels": ["#mine"]},
        ]}}
        cfg = DeeptorrentConfig.from_file(self._write(tmp_path, old))
        nets = {n.id: n for n in cfg.irc.networks}
        assert (nets["undernet"].port, nets["undernet"].tls) == (6667, False)
        assert (nets["iptorrents"].port, nets["iptorrents"].tls) == (6667, False)
        assert nets["iptorrents"].channels == []
        assert nets["animebytes"].host == "irc.animefriends.moe"
        assert nets["animebytes"].channels == []
        assert "morethantv" not in nets  # dead network dropped
        assert nets["orpheus"].channels == []  # shipped default channels stripped
        assert nets["mine"].channels == ["#mine"]  # user channels survive
        assert nets["mine"].port == 1234  # custom entry untouched

    def test_from_file_leaves_customized_entries_alone(self, tmp_path):
        old = {"irc": {"networks": [
            {"id": "undernet", "host": "irc.undernet.org", "port": 7001,
             "tls": True, "nick": "x", "channels": ["#c"]},
            {"id": "iptorrents", "host": "irc.iptorrents.com", "port": 6667,
             "tls": False, "nick": "DeepFluxUser", "channels": ["#mychan"]},
        ]}}
        cfg = DeeptorrentConfig.from_file(self._write(tmp_path, old))
        nets = {n.id: n for n in cfg.irc.networks}
        assert (nets["undernet"].port, nets["undernet"].tls) == (7001, True)
        # A user-added channel keeps the whole entry intact (subset gate).
        assert nets["iptorrents"].channels == ["#mychan"]


# ---------------------------------------------------------------------------
# GUI improvements (offscreen Qt): multi-server UX, nick field, channel
# directory, new commands, perf regressions
# ---------------------------------------------------------------------------

class TestIRCTabImprovements:
    def _make_tab(self):
        pytest.importorskip("PySide6")
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from gui.irc_tab import IRCTab

        app = QApplication.instance() or QApplication([])
        config = DeeptorrentConfig()
        config.irc.networks = [
            IRCNetworkConfig(id="alpha", host="irc.alpha.net", nick="AlphaUser"),
            IRCNetworkConfig(id="beta", host="irc.beta.net", nick="BetaUser",
                             channels=["#beta-chan"]),
        ]
        client = IRCClientCore(config.irc)
        client.state.ensure_network("alpha", "irc.alpha.net")
        client.state.ensure_network("beta", "irc.beta.net")
        client.state.ensure_channel("beta", "#beta-chan")
        return app, config, client, IRCTab(config, client)

    def test_append_never_serializes_whole_document(self):
        """Regression: _append_html used chat.toHtml() per line (O(n^2))."""
        app, _config, client, tab = self._make_tab()
        try:
            tab.tree.setCurrentItem(tab._network_item("alpha"))
            app.processEvents()
            assert tab._chat_empty is True
            with patch.object(tab.chat, "toHtml",
                              side_effect=AssertionError("toHtml must not run")):
                tab._on_event({"type": "message", "network": "alpha",
                               "channel": None, "nick": "a", "text": "hi there"})
            assert tab._chat_empty is False
            assert "hi there" in tab.chat.toPlainText()
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_search_count_increments_per_line_without_rescans(self):
        app, _config, client, tab = self._make_tab()
        try:
            tab.tree.setCurrentItem(tab._network_item("alpha"))
            app.processEvents()
            tab.search_edit.setText("needle")  # full count on an empty view = 0
            assert tab.search_status.text() == "0 matches"
            with patch.object(tab.chat, "toPlainText",
                              side_effect=AssertionError("no rescan")), \
                    patch.object(tab.chat, "toHtml",
                                 side_effect=AssertionError("no serialize")):
                tab._on_event({"type": "message", "network": "alpha",
                               "channel": None, "nick": "a",
                               "text": "a needle here"})
                tab._on_event({"type": "message", "network": "alpha",
                               "channel": None, "nick": "a",
                               "text": "nothing interesting"})
            assert tab.search_status.text() == "1 match"
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_combo_status_dots_and_aggregate_label(self):
        app, _config, client, tab = self._make_tab()
        try:
            assert tab.network_combo.itemText(0).startswith("○ irc.alpha.net")
            assert tab.status_label.text() == "0/2 connected"
            client.state.set_connected("alpha", True, nick="AlphaUser")
            tab._on_event({"type": "state", "network": "alpha",
                           "state": "connected", "nick": "AlphaUser"})
            assert tab.network_combo.itemText(0).startswith("●")
            assert tab.network_combo.itemText(1).startswith("○")
            assert tab.status_label.text().startswith("connected as AlphaUser")
            assert tab.status_label.text().endswith("1/2 connected")
            client.state.set_connected("beta", True, nick="BetaUser")
            tab._on_event({"type": "state", "network": "beta",
                           "state": "connecting"})
            assert tab.status_label.text().endswith("2/2 connected")
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_join_follows_viewed_network_not_combo(self):
        app, _config, client, tab = self._make_tab()
        try:
            beta_item = tab._ensure_channel_item("beta", "#beta-chan")
            tab.tree.setCurrentItem(beta_item)
            app.processEvents()
            # Diverge the combo from the tree view deliberately.
            tab.network_combo.setCurrentIndex(0)  # alpha
            assert tab._current == ("beta", "#beta-chan")
            tab.join_edit.setText("#elsewhere")
            with patch.object(client, "join") as join_mock:
                tab._on_join_clicked()
                join_mock.assert_called_once_with("beta", "#elsewhere")
                assert not tab.join_edit.text()
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_connect_all_and_disconnect_all(self):
        app, _config, client, tab = self._make_tab()
        try:
            with patch.object(client, "connect_network") as connect_mock:
                tab._on_connect_all_clicked()
                assert [call.args[0].id
                        for call in connect_mock.call_args_list] == ["alpha", "beta"]
            with patch.object(client, "disconnect_network") as disconnect_mock:
                tab._on_disconnect_all_clicked()
                assert [call.args[0]
                        for call in disconnect_mock.call_args_list] == ["alpha", "beta"]
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_combo_activation_follows_tree(self):
        app, _config, client, tab = self._make_tab()
        try:
            tab.tree.setCurrentItem(tab._network_item("alpha"))
            app.processEvents()
            tab.network_combo.setCurrentIndex(1)  # beta, no activation yet
            assert tab._current == ("alpha", None)
            tab._on_combo_activated(1)
            assert tab._current == ("beta", None)
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_nick_field_renames_live_and_persists(self):
        app, _config, client, tab = self._make_tab()
        try:
            tab.tree.setCurrentItem(tab._network_item("beta"))
            app.processEvents()
            assert tab.nick_edit.text() == "BetaUser"
            client.state.set_connected("beta", True, nick="BetaUser")
            tab.nick_edit.setText("CoolNick")
            with patch.object(client, "change_nick") as nick_mock:
                tab._apply_nick()
                nick_mock.assert_called_once_with("beta", "CoolNick")
            assert tab._find_net_cfg("beta").nick == "CoolNick"
            # Invalid nick never leaves the client.
            tab.nick_edit.setText("bad nick")
            with patch.object(client, "change_nick") as nick_mock:
                tab._apply_nick()
                nick_mock.assert_not_called()
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_nick_field_saved_when_offline_and_syncs_per_network(self):
        app, _config, client, tab = self._make_tab()
        try:
            tab.tree.setCurrentItem(tab._network_item("beta"))
            app.processEvents()
            tab.nick_edit.setText("OfflineNick")
            tab._on_nick_edited("OfflineNick")
            # Switching networks resets the field; switching back keeps
            # nothing dirty (the edit belonged to the other network).
            tab._sync_nick_field("alpha")
            assert tab.nick_edit.text() == "AlphaUser"
            # Unapplied edits for the SAME network are preserved on resync.
            tab._sync_nick_field("beta")
            tab.nick_edit.setText("OfflineNick")
            tab._on_nick_edited("OfflineNick")
            tab._sync_nick_field("beta")
            assert tab.nick_edit.text() == "OfflineNick"
            # Applying while disconnected: saved for the next connect, no rename.
            with patch.object(client, "change_nick") as nick_mock:
                tab._apply_nick()
                nick_mock.assert_not_called()
            assert tab._find_net_cfg("beta").nick == "OfflineNick"
            assert "used on the next connect" in tab.chat.toPlainText()
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_own_nick_change_event_updates_field_and_status(self):
        app, _config, client, tab = self._make_tab()
        try:
            tab.tree.setCurrentItem(tab._network_item("beta"))
            app.processEvents()
            client.state.set_connected("beta", True, nick="BetaUser")
            tab._on_event({"type": "state", "network": "beta",
                           "state": "connected", "nick": "BetaUser"})
            # Server confirms a rename.
            client.state.rename_nick("beta", "BetaUser", "Renamed")
            client.state.set_connected("beta", True, nick="Renamed")
            tab._on_event({"type": "nick", "network": "beta",
                           "old": "BetaUser", "new": "Renamed"})
            assert tab.nick_edit.text() == "Renamed"
            assert "connected as Renamed" in tab.status_label.text()
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_tree_context_menu_actions(self):
        app, _config, client, tab = self._make_tab()
        try:
            def action_texts(item):
                menu = tab._build_tree_menu(item)
                assert menu is not None
                texts = [action.text() for action in menu.actions()]
                menu.deleteLater()
                return texts

            # Disconnected network node.
            texts = action_texts(tab._network_item("beta"))
            assert "Connect" in texts
            assert "Join channel…" in texts
            assert "Edit in Networks…" in texts

            # Connected network node flips to Disconnect.
            client.state.set_connected("beta", True, nick="BetaUser")
            texts = action_texts(tab._network_item("beta"))
            assert "Disconnect" in texts and "Connect" not in texts

            # Channel node offers Part, query node offers Close query.
            texts = action_texts(tab._ensure_channel_item("beta", "#beta-chan"))
            assert "Part" in texts and "Copy name" in texts
            texts = action_texts(tab._ensure_channel_item("beta", "buddy"))
            assert "Close query" in texts and "Part" not in texts
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_connect_and_join_from_tree_helpers(self):
        app, _config, client, tab = self._make_tab()
        try:
            with patch.object(client, "connect_network") as connect_mock:
                tab._connect_network("beta")
                connect_mock.assert_called_once_with(
                    tab._find_net_cfg("beta"))
            with patch("gui.irc_tab.QInputDialog.getText",
                       return_value=("#cool", True)), \
                    patch.object(client, "join") as join_mock:
                tab._prompt_join("beta")
                join_mock.assert_called_once_with("beta", "#cool")
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_close_query_removes_view(self):
        app, _config, client, tab = self._make_tab()
        try:
            beta_item = tab._ensure_channel_item("beta", "#beta-chan")
            tab.tree.setCurrentItem(beta_item)
            app.processEvents()
            tab._open_query("buddy")
            assert tab._current == ("beta", "buddy")
            tab._close_query("beta", "buddy")
            assert tab._current == ("beta", None)
            assert tab._channel_item("beta", "buddy") is None
            assert client.state.nicks_of("beta", "buddy") == {}  # buffer dropped
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_chanlist_panel_populate_filter_and_join(self):
        app, _config, client, tab = self._make_tab()
        try:
            client.state.set_chanlist("beta", [
                {"channel": "#big", "users": 128, "topic": "busy place"},
                {"channel": "#small", "users": 3, "topic": "quiet corner"},
            ])
            tab.tree.setCurrentItem(tab._network_item("beta"))
            app.processEvents()
            # Offscreen tabs are never shown: assert the explicit visibility
            # flag (setVisible state) instead of isVisible().
            assert not tab.chanlist_panel.isHidden()
            assert tab.chanlist_table.rowCount() == 2
            assert tab.chanlist_table.item(0, 0).text() == "#big"  # users desc
            assert "2 channels" in tab.chanlist_title.text()
            assert "double-click a row to join" in tab.chanlist_title.text()

            tab.chanlist_filter.setText("quiet")
            assert tab.chanlist_table.isRowHidden(0)  # #big hidden
            assert not tab.chanlist_table.isRowHidden(1)
            tab.chanlist_filter.setText("")
            assert not tab.chanlist_table.isRowHidden(0)

            with patch.object(client, "join") as join_mock:
                tab._on_chanlist_activated(tab.chanlist_table.model().index(0, 2))
                join_mock.assert_called_once_with("beta", "#big")

            # Selecting a channel hides the directory panel.
            tab.tree.setCurrentItem(tab._ensure_channel_item("beta", "#beta-chan"))
            app.processEvents()
            assert tab.chanlist_panel.isHidden()
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_chanlist_event_refreshes_visible_panel(self):
        app, _config, client, tab = self._make_tab()
        try:
            tab.tree.setCurrentItem(tab._network_item("beta"))
            app.processEvents()
            assert tab.chanlist_panel.isHidden()
            client.state.set_chanlist("beta", [
                {"channel": "#one", "users": 5, "topic": ""},
            ])
            tab._on_event({"type": "chanlist", "network": "beta", "channels": []})
            assert not tab.chanlist_panel.isHidden()
            assert tab.chanlist_table.rowCount() == 1
            # Not viewing the server node — unread marker on the network item.
            tab.tree.setCurrentItem(tab._ensure_channel_item("beta", "#beta-chan"))
            app.processEvents()
            client.state.set_chanlist("beta", [
                {"channel": "#two", "users": 9, "topic": ""},
            ])
            tab._on_event({"type": "chanlist", "network": "beta", "channels": []})
            assert tab.chanlist_panel.isHidden()
            assert tab._unread.get(("beta", None)) == 1
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_new_slash_commands(self):
        app, _config, client, tab = self._make_tab()
        try:
            beta_item = tab._ensure_channel_item("beta", "#beta-chan")
            tab.tree.setCurrentItem(beta_item)
            app.processEvents()
            client.state.set_connected("beta", True, nick="BetaUser")

            with patch.object(client, "send_notice") as notice_mock, \
                    patch.object(client, "send_raw") as raw_mock:
                assert tab._handle_command("beta", "/notice #beta-chan heads up")
                notice_mock.assert_called_once_with("beta", "#beta-chan", "heads up")
                # Target is required — a bare message is an explicit error.
                notice_mock.reset_mock()
                assert tab._handle_command("beta", "/notice hello")
                notice_mock.assert_not_called()
                assert "Usage: /notice" in tab.chat.toPlainText()
                notice_mock.reset_mock()
                assert tab._handle_command("beta", "/notice buddy direct words")
                notice_mock.assert_called_once_with("beta", "buddy", "direct words")
                assert tab._handle_command("beta", "/whois buddy")
                raw_mock.assert_any_call("beta", "WHOIS buddy")
                assert tab._handle_command("beta", "/away gone fishing")
                raw_mock.assert_any_call("beta", "AWAY :gone fishing")
                assert tab._handle_command("beta", "/back")
                raw_mock.assert_any_call("beta", "AWAY")
                assert "reply appears in the network view" in tab.chat.toPlainText()

            with patch.object(client, "part") as part_mock, \
                    patch.object(client, "join") as join_mock:
                assert tab._handle_command("beta", "/hop")
                part_mock.assert_called_once_with("beta", "#beta-chan")
                join_mock.assert_called_once_with("beta", "#beta-chan")

            assert tab._handle_command("beta", "/help")
            assert "/notice" in tab.chat.toPlainText()
            assert tab._handle_command("beta", "/bogus arg")  # handled with an error line
            assert "Unknown command" in tab.chat.toPlainText()
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_clear_and_close_commands(self):
        app, _config, client, tab = self._make_tab()
        try:
            beta_item = tab._ensure_channel_item("beta", "#beta-chan")
            tab.tree.setCurrentItem(beta_item)
            app.processEvents()
            client.state.record("beta", ChatMessage(ts=1.0, kind=KIND_MESSAGE,
                                                    nick="a", text="old line"),
                                "#beta-chan")
            tab._render_buffer("beta", "#beta-chan")
            assert "old line" in tab.chat.toPlainText()
            assert tab._handle_command("beta", "/clear")
            assert "old line" not in tab.chat.toPlainText()
            assert client.state.get_messages("beta", "#beta-chan", 10) == []

            tab._open_query("buddy")
            with patch.object(client, "part") as part_mock:
                assert tab._handle_command("beta", "/close")
                part_mock.assert_not_called()  # query closes without PART
            assert tab._channel_item("beta", "buddy") is None
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_nick_list_ranks_ops_first_and_marks_away(self):
        app, _config, client, tab = self._make_tab()
        try:
            beta_item = tab._ensure_channel_item("beta", "#beta-chan")
            tab.tree.setCurrentItem(beta_item)
            app.processEvents()
            client.state.set_nicks("beta", "#beta-chan", {
                "owner": "~", "alpha": "@", "voicey": "+", "zed": "", "Buddy": ""})
            client.state.set_user_metadata("beta", "Buddy", away=True)
            client.state.set_user_metadata("beta", "alpha", account="acc-name")
            tab._refresh_nicks("beta", "#beta-chan")
            texts = [tab.nicks.item(i).text() for i in range(tab.nicks.count())]
            assert texts == ["~owner", "@alpha", "+voicey", "Buddy", "zed"]
            buddy_item = next(tab.nicks.item(i)
                              for i in range(tab.nicks.count())
                              if tab.nicks.item(i).text() == "Buddy")
            assert buddy_item.font().italic()
            alpha_item = next(tab.nicks.item(i)
                              for i in range(tab.nicks.count())
                              if tab.nicks.item(i).text() == "@alpha")
            assert alpha_item.toolTip() == "Logged in as acc-name"
        finally:
            tab.deleteLater()
            client.shutdown()
            app.processEvents()

    def test_network_dialog_port_follows_tls(self):
        pytest.importorskip("PySide6")
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from gui.irc_tab import NetworkDialog

        app = QApplication.instance() or QApplication([])
        dialog = NetworkDialog()
        try:
            assert dialog.tls_check.isChecked() and dialog.port_spin.value() == 6697
            dialog.tls_check.setChecked(False)
            assert dialog.port_spin.value() == 6667
            dialog.tls_check.setChecked(True)
            assert dialog.port_spin.value() == 6697
            # Custom ports are never touched by the toggle.
            dialog.port_spin.setValue(7000)
            dialog.tls_check.setChecked(False)
            assert dialog.port_spin.value() == 7000
            dialog.tls_check.setChecked(True)
            assert dialog.port_spin.value() == 7000
            # Back at a standard port, the toggle follows again (needs an
            # actual state change to fire the toggled signal).
            dialog.port_spin.setValue(6667)
            dialog.tls_check.setChecked(False)
            assert dialog.port_spin.value() == 6667
            dialog.tls_check.setChecked(True)
            assert dialog.port_spin.value() == 6697
            # Range is enforced by the spin box (no silent fallbacks).
            dialog.port_spin.setValue(99999)
            assert dialog.port_spin.value() == 65535
            net = dialog.to_config()
            assert net.port == 65535 and net.tls is True
        finally:
            dialog.deleteLater()
            app.processEvents()
