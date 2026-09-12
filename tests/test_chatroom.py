"""DeepFlux Room tests (ircmgr/room.py + IRC-page integration).

All networking is loopback (RoomHost binds 127.0.0.1, ephemeral ports) or
fully faked (FakeRendezvous replaces website/api/room.js; UPnP is patched
out). No test ever reaches the internet.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Dict, Optional, Tuple
from unittest.mock import patch

import pytest

from config import ChatConfig, DeeptorrentConfig
from ircmgr import room as room_mod
from ircmgr.room import (
    ROOM_CHANNEL,
    RoomClient,
    RoomCodec,
    RoomController,
    RoomHost,
    _parse_endpoint,
    clean_text,
    pointer_still_fresh,
    valid_nick,
    valid_room_secret,
)
from ircmgr.state import ChatMessage, IRCState, ROOM_NET_ID

SECRET_A = "test-room-secret-alpha"
SECRET_B = "test-room-secret-beta"


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class FakeRendezvous:
    """In-memory mirror of website/api/room.js semantics."""

    def __init__(self) -> None:
        self.store: Dict[str, Tuple[str, str]] = {}  # room -> (token, blob)
        self.offline = False

    def lookup(self, room_id: str) -> Optional[str]:
        if self.offline:
            return None
        entry = self.store.get(room_id)
        return entry[1] if entry else None

    def claim(self, room_id: str, token: str, blob: str):
        if self.offline:
            return ("unavailable", None)
        entry = self.store.get(room_id)
        if entry:
            return ("taken", entry[1])
        self.store[room_id] = (token, blob)
        return ("ok", None)

    def refresh(self, room_id: str, token: str, blob: str) -> str:
        if self.offline:
            return "unavailable"
        entry = self.store.get(room_id)
        if not entry:
            return "unavailable"
        if entry[0] != token:
            return "taken"
        self.store[room_id] = (token, blob)
        return "ok"

    def withdraw(self, room_id: str, token: str) -> None:
        entry = self.store.get(room_id)
        if entry and entry[0] == token:
            self.store.pop(room_id, None)

    def my_ip(self) -> Optional[str]:
        # Always None: the host then advertises only its LAN endpoint, so
        # loopback tests never dial a bogus public address.
        return None


@pytest.fixture(autouse=True)
def no_upnp(monkeypatch):
    monkeypatch.setattr(room_mod, "_upnp_map_port", lambda port: None)


def make_controller(secret: str = SECRET_A, rendezvous=None, state=None):
    return RoomController(
        ChatConfig(listen_port=0),  # 0 → default base; port scan picks a free one
        state or IRCState(),
        secret=secret,
        rendezvous=rendezvous or FakeRendezvous(),
        poll_interval=0.05,
        reconnect_delays=(0.05, 0.05, 0.05),
    )


# ---------------------------------------------------------------------------
# codec / helpers
# ---------------------------------------------------------------------------

class TestCodec:
    def test_encrypted_roundtrip(self):
        codec = RoomCodec(SECRET_A)
        assert codec.encrypted
        sealed = codec.seal("h2c", "msg", {"text": "hi", "action": False})
        assert codec.open("h2c", "msg", sealed) == {"text": "hi", "action": False}

    def test_direction_keys_differ(self):
        codec = RoomCodec(SECRET_A)
        sealed = codec.seal("c2h", "msg", {"text": "x"})
        assert codec.open("h2c", "msg", sealed) is None  # wrong direction key

    def test_kind_is_bound_as_aad(self):
        codec = RoomCodec(SECRET_A)
        sealed = codec.seal("h2c", "msg", {"text": "x"})
        assert codec.open("h2c", "pointer", sealed) is None  # wrong purpose

    def test_tamper_fails(self):
        codec = RoomCodec(SECRET_A)
        sealed = codec.seal("c2h", "msg", {"text": "x"})
        tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
        assert codec.open("c2h", "msg", tampered) is None

    def test_wrong_secret_fails(self):
        sealed = RoomCodec(SECRET_A).seal("c2h", "msg", {"text": "x"})
        assert RoomCodec(SECRET_B).open("c2h", "msg", sealed) is None

    def test_plaintext_mode_passthrough(self):
        codec = RoomCodec("")
        assert not codec.encrypted
        sealed = codec.seal("h2c", "msg", {"text": "hello"})
        assert sealed.startswith("p")
        assert codec.open("h2c", "msg", sealed) == {"text": "hello"}
        # a source build cannot open an encrypted build's records
        assert codec.open("h2c", "msg", RoomCodec(SECRET_A).seal("h2c", "msg", {})) is None

    def test_room_ids_differ_per_secret(self):
        assert RoomCodec(SECRET_A).room_id != RoomCodec(SECRET_B).room_id
        assert RoomCodec("").room_id == "lounge"

    def test_join_proof(self):
        codec = RoomCodec(SECRET_A)
        ts = int(time.time())
        assert codec.check_proof("nick|1", codec.proof("nick|1"), ts)
        assert not codec.check_proof("nick|1", codec.proof("nick|2"), ts)
        assert not codec.check_proof("nick|1", codec.proof("nick|1"), ts - 9999)
        plain = RoomCodec("")
        assert plain.check_proof("x", "", time.time())  # no proofs in plaintext


class TestHelpers:
    def test_valid_nick(self):
        assert valid_nick("alice")
        assert valid_nick("Big_Dog-99")
        assert not valid_nick("")
        assert not valid_nick("has space")
        assert not valid_nick("a" * 25)
        assert not valid_nick("room")  # reserved

    def test_clean_text(self):
        assert clean_text("a\r\nb") == "a  b"  # \r and \n each become one space
        assert clean_text("  padded  ") == "padded"
        assert len(clean_text("x" * 5000)) == 2000

    def test_parse_endpoint(self):
        assert _parse_endpoint("1.2.3.4:7766") == ("1.2.3.4", 7766, None)
        assert _parse_endpoint("example.com") == ("example.com", 7766, None)
        host, port, err = _parse_endpoint("[::1]:9000")
        assert (host, port, err) == ("::1", 9000, None)


# ---------------------------------------------------------------------------
# host + raw clients (wire protocol)
# ---------------------------------------------------------------------------

class _RawClient:
    """Minimal protocol client for exercising RoomHost directly."""

    def __init__(self, codec: RoomCodec):
        self.codec = codec

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        # shutdown() first: it sends the FIN immediately (sock.close() alone
        # is deferred while the makefile reader still references it).
        sock = getattr(self, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for handle in (getattr(self, "file", None), sock):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    def connect(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(5)
        self.file = self.sock.makefile("r", encoding="utf-8")
        return self

    def send(self, rec: dict):
        self.sock.sendall(
            json.dumps(rec, separators=(",", ":")).encode("utf-8") + b"\n")

    def join(self, nick: str, ts: Optional[int] = None, proof: Optional[str] = None):
        ts = int(time.time()) if ts is None else ts
        if proof is None:
            proof = self.codec.proof(f"{nick}|{ts}")
        self.send({"t": "join", "v": 1, "nick": nick, "ts": ts, "proof": proof})
        return self.recv()

    def say(self, text: str, action: bool = False):
        self.send({"t": "msg",
                   "e": self.codec.seal("c2h", "msg", {"text": text, "action": action})})

    def recv(self) -> Optional[dict]:
        try:
            line = self.file.readline()
        except OSError:
            # Windows: a drop with unread inbound data sends an RST that can
            # discard even delivered records — that IS a disconnect.
            return None
        if not line:
            return None
        return json.loads(line)

    def drain_until(self, kind: str, timeout: float = 5.0) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rec = self.recv()
            if rec is None:
                return None
            if rec.get("t") == kind:
                return rec
        return None


class TestRoomHost:
    def _start_host(self, codec=None, secret=SECRET_A) -> RoomHost:
        codec = codec or RoomCodec(secret)
        host = RoomHost(codec, bind_host="127.0.0.1", port=0)
        assert host.start("HostNick")
        return host

    def test_join_welcome_users_and_messages(self):
        host = self._start_host()
        codec = host._codec
        try:
            with _RawClient(codec) as a, _RawClient(codec) as b:
                a.connect(host.port)
                welcome = a.join("alice")
                assert welcome["t"] == "welcome"
                assert welcome["you"] == "alice"
                assert {u["nick"] for u in welcome["users"]} == {"HostNick", "alice"}

                b.connect(host.port)
                welcome_b = b.join("bob")
                assert {u["nick"] for u in welcome_b["users"]} == {
                    "HostNick", "alice", "bob"}

                a.say("hello from alice")
                rec_b = b.drain_until("msg")
                assert rec_b and rec_b["nick"] == "alice"
                payload = codec.open("h2c", "msg", rec_b["e"])
                assert payload["text"] == "hello from alice"

                host.say("hi from host")
                rec_b2 = b.drain_until("msg")
                assert rec_b2["nick"] == "HostNick"

                # part broadcast carries the refreshed user list
                a.close()
                parted = b.drain_until("part")
                assert parted["nick"] == "alice"
                assert {u["nick"] for u in parted["users"]} == {"HostNick", "bob"}
        finally:
            host.stop("test done")

    def test_history_replayed_to_late_joiner(self):
        host = self._start_host()
        try:
            host.say("first message")
            with _RawClient(host._codec) as late:
                late.connect(host.port)
                welcome = late.join("latecomer")
                texts = []
                for rec in welcome["history"]:
                    if rec.get("t") == "msg":
                        payload = host._codec.open("h2c", "msg", rec.get("e"))
                        texts.append(payload["text"])
                assert "first message" in texts
        finally:
            host.stop("test done")

    def test_nick_in_use_rejected(self):
        host = self._start_host()
        try:
            with _RawClient(host._codec) as a:
                a.connect(host.port)
                assert a.join("alice")["t"] == "welcome"
                # nested: alice stays connected while the collision arrives
                with _RawClient(host._codec) as b:
                    b.connect(host.port)
                    err = b.join("Alice")  # case-insensitive collision
                    assert err == {"t": "err", "code": "nick-in-use"}
        finally:
            host.stop("test done")

    def test_bad_proof_rejected_in_encrypted_mode(self):
        host = self._start_host()
        try:
            with _RawClient(host._codec) as imposter:
                imposter.connect(host.port)
                err = imposter.join("mallory", proof="00" * 32)
                assert err["t"] == "err"
                assert err["code"] == "bad-proof"
            # a build WITHOUT the key cannot produce a valid proof either
            source_codec = RoomCodec("")
            with _RawClient(source_codec) as source:
                source.connect(host.port)
                err = source.join("mallory", proof="")
                assert err["code"] == "bad-proof"
        finally:
            host.stop("test done")

    def test_rate_limit_drops_flooder(self):
        host = self._start_host()
        events: list = []
        host._on_event = lambda kind, **kw: events.append((kind, kw))
        try:
            with _RawClient(host._codec) as a:
                a.connect(host.port)
                a.join("flooder")
                for i in range(12):
                    try:
                        a.say(f"spam {i}")
                    except OSError:
                        break  # the drop caught up mid-flood — exactly right
                # The flood drop may tear the socket with an RST before the
                # err record is read — being disconnected IS the punishment.
                rec = a.drain_until("err")
                assert rec is None or rec.get("code") == "rate"
            assert wait_until(lambda: "flooder" not in host.user_nicks())
        finally:
            host.stop("test done")

    def test_plaintext_host_accepts_source_clients(self):
        codec = RoomCodec("")
        host = RoomHost(codec, bind_host="127.0.0.1", port=0)
        assert host.start("HostNick")
        try:
            with _RawClient(codec) as a:
                a.connect(host.port)
                welcome = a.join("alice")
                assert welcome["mode"] == "plain"
                host.say("hello plaintext")
                rec = a.drain_until("msg")
                payload = codec.open("h2c", "msg", rec["e"])
                assert payload["text"] == "hello plaintext"
        finally:
            host.stop("test done")


# ---------------------------------------------------------------------------
# controller: election, messaging, takeover
# ---------------------------------------------------------------------------

class TestRoomHostRotation:
    def _start(self, secret=SECRET_A):
        codec = RoomCodec(secret)
        host = RoomHost(codec, bind_host="127.0.0.1", port=0)
        assert host.start("HostNick")
        return host, codec

    def _rekey_request(self, cli: _RawClient, codec: RoomCodec):
        cli.send({"t": "rekey_req",
                  "e": codec.seal("c2h", "rekey_req", {"n": "00" * 8})})

    def test_rotation_locks_out_old_key_joiners(self):
        host, old_codec = self._start()
        try:
            with _RawClient(old_codec) as a:
                a.connect(host.port)
                assert a.join("alice")["t"] == "welcome"
                self._rekey_request(a, old_codec)
                rec = a.drain_until("rekey")
                assert rec and rec["t"] == "rekey"
                payload = old_codec.open("h2c", "rekey", rec["e"])
                assert valid_room_secret(payload["secret"])
                new_codec = RoomCodec(payload["secret"])
                assert new_codec.room_id != old_codec.room_id

                # a member on the NEW key keeps talking and is relayed
                with _RawClient(new_codec) as b:
                    b.connect(host.port)
                    # new-key joiners are welcomed...
                    welcome = b.join("bob")
                    assert welcome["t"] == "welcome"
                    # ...and old-key records still decode inside the grace
                    a.say("sealed under the old key moments ago")
                    rec_b = b.drain_until("msg")
                    payload = new_codec.open("h2c", "msg", rec_b["e"])
                    assert payload["text"] == "sealed under the old key moments ago"

            # old-key joiners are locked out after the rotation
            with _RawClient(old_codec) as outsider:
                outsider.connect(host.port)
                err = outsider.join("mallory")
                assert err["t"] == "err"
                assert err["code"] == "bad-proof"
        finally:
            host.stop("test done")

    def test_forged_rekey_request_drops_the_connection(self):
        host, codec = self._start()
        try:
            other = RoomCodec(SECRET_B)  # wrong key entirely
            with _RawClient(codec) as a:
                a.connect(host.port)
                a.join("alice")
                a.send({"t": "rekey_req",
                        "e": other.seal("c2h", "rekey_req", {"n": "x"})})
                assert wait_until(lambda: "alice" not in host.user_nicks())
        finally:
            host.stop("test done")

    def test_rotation_cooldown(self):
        host, codec = self._start()
        try:
            assert host.rotate() is True
            assert host.rotate() is False  # inside the cooldown
        finally:
            host.stop("test done")


class TestRoomControllerRotation:
    def test_make_private_splits_the_room(self):
        state = IRCState()
        rdv = FakeRendezvous()
        c1 = make_controller(SECRET_A, rdv, state)
        c2 = make_controller(SECRET_A, rdv, state)
        outsider_state = IRCState()
        c3 = make_controller(SECRET_A, rdv, outsider_state)
        try:
            c1.join("first")
            assert wait_until(lambda: c1.role == "host")
            original_id = c1._codec.room_id
            c2.join("second")
            assert wait_until(lambda: c2.role == "member")

            # a MEMBER presses the button — the host rotates for everyone
            assert c2.make_private()
            assert wait_until(lambda: c1._codec.room_id != original_id)
            assert wait_until(lambda: c2._codec.room_id == c1._codec.room_id)
            assert c1.encrypted and c2.encrypted
            # the old slot is withdrawn, the new one registered
            assert wait_until(lambda: original_id not in rdv.store)
            assert wait_until(lambda: c1._codec.room_id in rdv.store)
            assert "private room" in state.topic_of(ROOM_NET_ID, ROOM_CHANNEL)

            # messages keep flowing inside the rotated room
            c1.send_message("post-rotation hello")
            assert wait_until(lambda: any(
                m.text == "post-rotation hello"
                for m in state.get_messages(ROOM_NET_ID, ROOM_CHANNEL)))

            # an outsider with the ORIGINAL key lands in a fresh room
            c3.join("third")
            assert wait_until(lambda: c3.role == "host")
            c3.send_message("outsider message")
            assert wait_until(lambda: any(
                m.text == "outsider message"
                for m in outsider_state.get_messages(ROOM_NET_ID, ROOM_CHANNEL)))
            time.sleep(0.3)
            assert not any(m.text == "outsider message"
                           for m in state.get_messages(ROOM_NET_ID, ROOM_CHANNEL))
            assert not any(m.text == "post-rotation hello"
                           for m in outsider_state.get_messages(ROOM_NET_ID, ROOM_CHANNEL))
        finally:
            c1.shutdown()
            c2.shutdown()
            c3.shutdown()

    def test_takeover_after_rotation_uses_the_new_key(self):
        state = IRCState()
        rdv = FakeRendezvous()
        c1 = make_controller(SECRET_A, rdv, state)
        c2 = make_controller(SECRET_A, rdv, state)
        try:
            c1.join("first")
            assert wait_until(lambda: c1.role == "host")
            c2.join("second")
            assert wait_until(lambda: c2.role == "member")

            assert c1.make_private()  # the HOST presses the button this time
            new_id = c1._codec.room_id
            assert new_id != RoomCodec(SECRET_A).room_id
            assert wait_until(lambda: c2._codec.room_id == new_id)

            c1.leave()
            # the member takes over AND re-hosts under the rotated key
            assert wait_until(lambda: c2.role == "host"), c2.role
            assert wait_until(lambda: new_id in rdv.store)
            assert c2.send_message("still private")
            assert wait_until(lambda: any(
                m.text == "still private"
                for m in state.get_messages(ROOM_NET_ID, ROOM_CHANNEL)))
        finally:
            c1.shutdown()
            c2.shutdown()


class TestRoomController:
    def test_first_user_hosts_second_joins(self):
        state = IRCState()
        rdv = FakeRendezvous()
        c1 = make_controller(SECRET_A, rdv, state)
        c2 = make_controller(SECRET_A, rdv, state)
        try:
            assert c1.join("first")
            assert wait_until(lambda: c1.role == "host"), c1.role
            # pointer registered + sealed
            assert rdv.store[c1._codec.room_id][1]

            assert c2.join("second")
            assert wait_until(lambda: c2.role == "member"), c2.role
            assert c1.role == "host"

            c1.send_message("hello from host")
            assert wait_until(lambda: any(
                m.text == "hello from host"
                for m in state.get_messages(ROOM_NET_ID, ROOM_CHANNEL)))
            c2.send_message("hi from member")
            assert wait_until(lambda: any(
                m.text == "hi from member"
                for m in state.get_messages(ROOM_NET_ID, ROOM_CHANNEL)))

            # both see each other in the nick list
            nicks = state.nicks_of(ROOM_NET_ID, ROOM_CHANNEL)
            assert {"first", "second"} <= set(nicks)
            assert state.network_connected(ROOM_NET_ID)
        finally:
            c1.shutdown()
            c2.shutdown()

    def test_host_leave_promotes_member(self):
        state = IRCState()
        rdv = FakeRendezvous()
        c1 = make_controller(SECRET_A, rdv, state)
        c2 = make_controller(SECRET_A, rdv, state)
        try:
            c1.join("first")
            assert wait_until(lambda: c1.role == "host")  # deterministic order
            c2.join("second")
            assert wait_until(lambda: c2.role == "member")

            c1.leave()
            # the member notices the closing and takes over the room
            assert wait_until(lambda: c2.role == "host", timeout=15), c2.role
            assert c2.send_message("i host now")
            assert wait_until(lambda: any(
                m.text == "i host now"
                for m in state.get_messages(ROOM_NET_ID, ROOM_CHANNEL)))
        finally:
            c1.shutdown()
            c2.shutdown()

    def test_direct_host_join_skips_discovery(self):
        rdv = FakeRendezvous()
        c1 = make_controller(SECRET_A, rdv)
        try:
            assert c1.join("first")
            assert wait_until(lambda: c1.role == "host")
            port = c1._host.port
            assert rdv.store  # c1 registered its pointer

            c2 = make_controller(SECRET_A, FakeRendezvous())  # different board
            try:
                assert c2.join("second", manual_host=f"127.0.0.1:{port}")
                assert wait_until(lambda: c2.role == "member")
            finally:
                c2.shutdown()
        finally:
            c1.shutdown()

    def test_claim_taken_falls_back_to_joining(self):
        rdv = FakeRendezvous()
        c1 = make_controller(SECRET_A, rdv)
        try:
            assert c1.join("first")
            assert wait_until(lambda: c1.role == "host")

            # c2's discovery misses the pointer (as if it raced the TTL),
            # so it tries to host — claim says "taken" — it joins c1 instead.
            c2 = make_controller(SECRET_A, rdv)
            original_lookup = rdv.lookup
            rdv.lookup = lambda room: None  # one-shot miss
            try:
                assert c2.join("second")
                assert wait_until(lambda: c2.role == "member"), c2.role
            finally:
                rdv.lookup = original_lookup
                c2.shutdown()
            assert c1.role == "host"
        finally:
            c1.shutdown()

    def test_offline_discovery_still_hosts_locally(self):
        rdv = FakeRendezvous()
        rdv.offline = True
        c1 = make_controller(SECRET_A, rdv)
        try:
            assert c1.join("lonely")
            assert wait_until(lambda: c1.role == "host")
            assert c1.endpoints()  # LAN endpoint still advertised locally
        finally:
            c1.shutdown()

    def test_plaintext_controllers_share_the_public_room(self):
        state = IRCState()
        rdv = FakeRendezvous()
        c1 = make_controller("", rdv, state)
        c2 = make_controller("", rdv, state)
        try:
            c1.join("first")
            assert wait_until(lambda: c1.role == "host", timeout=20.0)
            c2.join("second")
            assert wait_until(lambda: c2.role == "member", timeout=20.0)
            c1.send_message("plain hello")
            assert wait_until(lambda: any(
                m.text == "plain hello"
                for m in state.get_messages(ROOM_NET_ID, ROOM_CHANNEL)))
            assert "UNENCRYPTED" in state.topic_of(ROOM_NET_ID, ROOM_CHANNEL)
        finally:
            c1.shutdown()
            c2.shutdown()

    def test_encrypted_room_invisible_to_source_build(self):
        # the source build's codec cannot open the setup build's pointer
        rdv = FakeRendezvous()
        c1 = make_controller(SECRET_A, rdv)
        try:
            c1.join("first")
            assert wait_until(lambda: c1.role == "host")
            blob = rdv.lookup(c1._codec.room_id)
            assert RoomCodec("").open_pointer(blob) is None
            assert RoomCodec(SECRET_A).open_pointer(blob) is not None
        finally:
            c1.shutdown()

    def test_pointer_freshness(self):
        assert pointer_still_fresh({"ts": time.time()})
        assert not pointer_still_fresh({"ts": time.time() - 10_000})
        assert not pointer_still_fresh(None)


# ---------------------------------------------------------------------------
# Room tab integration
# ---------------------------------------------------------------------------

class TestRoomTab:
    def _make_tab(self):
        pytest.importorskip("PySide6")
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from gui.room_tab import RoomTab

        app = QApplication.instance() or QApplication([])
        config = DeeptorrentConfig()

        class StubRoom:
            encrypted = True
            role = "left"
            nick = ""

            def __init__(self):
                self.joined = False
                self.joins = []

            def add_listener(self, cb):
                self.emit = cb

            def is_joined(self):
                return self.joined

            def endpoints(self):
                return ["203.0.113.7:7766"] if self.joined else []

            def join(self, nick, manual_host=""):
                self.joins.append((nick, manual_host))
                self.joined = True
                self.role = "host"
                self.nick = nick
                return True

            def leave(self):
                self.joined = False
                self.role = "left"

            def send_message(self, text, action=False):
                return self.joined

            def make_private(self):
                return self.joined

            def shutdown(self):
                pass

        stub = StubRoom()
        tab = RoomTab(config, room=stub)
        return app, tab, stub

    def test_join_bar_and_message_rendering(self):
        app, tab, stub = self._make_tab()
        try:
            # placeholder transcript before joining
            assert "Nothing here yet" in tab.chat.toPlainText()

            # Join needs a nickname
            tab._on_join_clicked()
            assert stub.joins == []
            assert "nickname" in tab._status_label.text().lower()

            tab._nick_edit.setText("alice")
            tab._on_join_clicked()
            assert stub.joins == [("alice", "")]
            assert tab._join_btn.text() == "Leave"
            assert tab._nick_edit.isEnabled() is False
            assert tab._config.chat.nickname == "alice"

            # controller events render through the normal paths
            stub.emit({"type": "state", "network": ROOM_NET_ID,
                       "state": "connected", "nick": "alice"})
            tab._state.set_nicks(ROOM_NET_ID, ROOM_CHANNEL, {"alice": ""})
            stub.emit({"type": "names", "network": ROOM_NET_ID,
                       "channel": ROOM_CHANNEL, "nicks": {"alice": ""}})
            stub.emit({"type": "message", "network": ROOM_NET_ID,
                       "channel": ROOM_CHANNEL, "nick": "alice",
                       "text": "hello room", "own": True})
            assert "hello room" in tab.chat.toPlainText()
            assert tab.nicks.count() == 1

            # plain text goes to the room (stub accepts, input clears)
            tab.input.setText("second line")
            tab._on_send()
            assert tab.input.text() == ""
            # Leave resets the bar
            tab._on_join_clicked()
            assert tab._join_btn.text() == "Join"
        finally:
            tab.deleteLater()
            app.processEvents()

    def test_history_replay_becomes_visible_on_connect(self):
        """A member's welcome replays history straight into the state buffer
        WITHOUT per-record events — the tab must re-render on connect."""
        app, tab, stub = self._make_tab()
        try:
            tab._state.record(ROOM_NET_ID, ChatMessage(
                ts=time.time(), kind="msg", nick="bob",
                text="replayed line"), ROOM_CHANNEL)
            stub.emit({"type": "state", "network": ROOM_NET_ID,
                       "state": "connected", "nick": "alice"})
            assert "replayed line" in tab.chat.toPlainText()
        finally:
            tab.deleteLater()
            app.processEvents()

    def test_positional_parent_construction_matches_main_window(self):
        """MainWindow calls ``RoomTab(config, self)`` — the second POSITIONAL
        argument is the parent QWidget (the 3.9 hotfix lesson: a parameter
        inserted before it binds the window to the wrong slot and crashes
        startup; no test built the real MainWindow, so the suite stayed
        green)."""
        pytest.importorskip("PySide6")
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QWidget
        from gui.room_tab import RoomTab

        app = QApplication.instance() or QApplication([])
        config = DeeptorrentConfig()
        parent = QWidget()

        class StubRoom:
            encrypted, role, nick = True, "left", ""

            def add_listener(self, cb):
                pass

            def is_joined(self):
                return False

            def endpoints(self):
                return []

            def make_private(self):
                return False

            def shutdown(self):
                pass

        tab = RoomTab(config, parent, StubRoom())
        try:
            assert tab.parent() is parent          # parent bound, not room
            assert isinstance(tab._room, StubRoom)  # room bound where expected
        finally:
            tab.deleteLater()
            parent.deleteLater()
            app.processEvents()

    def test_commands_and_guards(self):
        app, tab, stub = self._make_tab()
        try:
            tab.input.setText("/raw PRIVMSG x")
            tab._on_send()
            assert "Unknown command" in tab.chat.toPlainText()

            stub.joined = False
            tab.input.setText(" anybody there?")
            tab._on_send()
            assert "Join the room first" in tab.chat.toPlainText()

            # /me while joined goes through the controller as an action
            stub.joined = True
            sent = []
            stub.send_message = lambda text, action=False: sent.append((text, action)) or True
            tab.input.setText("/me waves")
            tab._on_send()
            assert sent == [("waves", True)]
        finally:
            tab.deleteLater()
            app.processEvents()

    def test_make_private_button_confirms_then_rotates(self):
        app, tab, stub = self._make_tab()
        try:
            # hidden while not joined
            assert tab._private_btn.isHidden()

            tab._nick_edit.setText("alice")
            tab._on_join_clicked()
            assert not tab._private_btn.isHidden()

            calls = []
            stub.make_private = lambda: calls.append(1) or True
            from PySide6.QtWidgets import QMessageBox
            with patch("gui.room_tab.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes):
                tab._on_private_clicked()
            assert calls == [1]

            # a "No" answer never reaches the controller
            with patch("gui.room_tab.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.No):
                tab._on_private_clicked()
            assert calls == [1]
        finally:
            tab.deleteLater()
            app.processEvents()
