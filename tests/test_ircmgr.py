"""Tests for the embedded IRC client (ircmgr) and its agent tools.

State tests are pure unit tests. The client tests run against a fake IRC
server on a localhost socket — no external network access.
"""
from __future__ import annotations

import socket
import threading
import time
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from agent.tools import ToolRegistry
from config import DeeptorrentConfig, IRCConfig, IRCNetworkConfig
from ircmgr.client import IRCClientCore, strip_control_codes
from ircmgr.state import (
    ChatMessage,
    IRCState,
    KIND_ACTION,
    KIND_MESSAGE,
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


# ---------------------------------------------------------------------------
# Fake IRC server for client integration tests
# ---------------------------------------------------------------------------

class FakeIRCServer:
    """Speaks just enough IRC for the client: NICK/USER → 001, JOIN echo +
    NAMES, PONG collection, scripted pushes."""

    def __init__(self, nick_in_use_first: bool = False) -> None:
        self.nick_in_use_first = nick_in_use_first
        self.received: List[str] = []
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._client: Optional[socket.socket] = None
        self._running = threading.Event()
        self._buf = ""
        self._welcomed = False
        self._nick: str = ""
        self._user_seen = False

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
        parts = line.split()
        if not parts:
            return
        cmd = parts[0].upper()
        if cmd == "NICK" and not self._welcomed:
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

    def _maybe_welcome(self) -> None:
        if self._nick and self._user_seen and not self._welcomed:
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

            # channel list via the tool (fake server replies to LIST)
            result = tools.call("irc_list_channels", {"network": "test"})
            assert result["success"]
            assert [c["channel"] for c in result["channels"]] == ["#big", "#small"]
            # cached re-read without a fresh LIST
            result = tools.call("irc_list_channels", {"network": "test", "refresh": False})
            assert result["success"] and result["count"] == 2

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

    def test_extended_tools_classification(self):
        from agent.loop import DESTRUCTIVE_TOOLS, READ_ONLY_TOOLS, REQUIRES_CONFIRMATION

        assert "irc_list_nicks" in READ_ONLY_TOOLS
        assert "irc_list_channels" not in READ_ONLY_TOOLS  # sends a server query
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
                tab.shutdown()
            # The write went to the sandbox, not the user's real config.
            assert (tmp_path / "config.json").exists()
        finally:
            client.shutdown()
