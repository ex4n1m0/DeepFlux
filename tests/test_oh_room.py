"""Tests for the OnlyHumans word-room member (ircmgr/oh_room.py).

The crypto layer is pinned against the OnlyHumans Rust core's own
known-answer vectors (the same kat.json that gates the browser portal —
a trimmed copy is embedded below, so this file needs nothing from the
OnlyHumans checkouts). The state-machine tests run two full controllers
against an in-memory FakeHub with the same semantics as the real
serverless hub (NX room election, destructive inbox drain, per-item
signatures), driving every pass synchronously — no sleeps.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ircmgr.oh_room import (  # noqa: E402
    ARGON2_M_KIB,
    ARGON2_P,
    ARGON2_T,
    KIND_CHAT,
    OhRoomController,
    RoomCrypto,
    admission_proof,
    b64,
    effective_gk,
    gen_room_phrase,
    generate_default_name,
    global_room_hex,
    load_identity,
    open_room_key,
    peer_id_from_public,
    ed25519_raw_from_protobuf,
    seal_room_key,
    unb64,
    unhex,
    verify_mail_item,
)
from ircmgr.state import ROOM_NET_ID, IRCState  # noqa: E402

# Known-answer vectors produced by the Rust core (core/examples/kat.rs —
# the same file the browser portal checks itself against).
KAT = {
    "admission_proof_hex": "4c0c703ecb695cfc531a4b8eebbc7b3283e0b57b69eae104cb4f33b20ed8d92c",
    "argon2": {"len": 32, "m_kib": 65536, "p": 1, "t": 3},
    "chat_plaintext": "hello from rust",
    "earth_gk_hex": "a7772354ba5664ac672a83137a1751f24bd07f4f85f9e1fbbd7d6c2fc655dd8d",
    "gk_hex": "4544e3ed1c48e35f25260764a6c067b1667d098cb28d35a093e4fb70ac090ff0",
    "key_ct_b64": "i2xtAuJD8n-taolaC0COV9tK6E9kzjyOwAMD_qqOowXzyjE76bYu6WFO65UwQ86J",
    "key_ct_epoch": 1,
    "mail_msg": "OH1-mail-v1|12D3KooWRY1NBdfrXxw1kZ1dMNL12jvoKXQAfRZiQGDWv1aXQFRr|recipient|1700000000001|{\"Ack\":{}}",
    "mail_sig_b64": "zI3YRXClEmpDqk7EUqrLoo4mfGWux5ypMoCyNE5KYkSwmN_bL8N0DSVh2UY8SNMJTBUw9JAAwAslNo2X9VTyBA",
    "peer_id": "12D3KooWRY1NBdfrXxw1kZ1dMNL12jvoKXQAfRZiQGDWv1aXQFRr",
    "proof_nonce_hex": "43d85b9ad9a82a7ed896c0e9e17817db",
    "public_key_b64": "CAESIOmKLRXxUCU2teCAfkSYOzyFJM52FaeZRmoffTkWrW4x",
    "room_hex": "594c1adef834af0a1d3c5e8190f5cc6b",
    "room_key_hex": "e36ed52f49947288606bd676d4732530e9a63a080008665559cde768920fc334",
    "sealed": {
        "ct_b64": "vWaj-_JSThEJKz-JgnQoFsljCgS0FntzofSYx4QuuzJ8ILKGEpubyuHCJdw0vbA4ipPzYszfJCIWEbsG5llNF1ixby-ZWU7M_a60Gmz-2VrBjfxmH509hVf6maCWyOEiQL4z4unSQvNp32Lg442288WL7J0FFYEG3U7cVhvfGL7psq3g-c3MP8rhhIn1j-nm",
        "epoch": 1,
        "nonce_b64": "uMpelCId9duZyfrR1OCZfB6K0tcdf1um",
        "room_id_hex": "594c1adef834af0a1d3c5e8190f5cc6b",
        "sender": "12D3KooWRY1NBdfrXxw1kZ1dMNL12jvoKXQAfRZiQGDWv1aXQFRr",
        "seq": 1700000000000,
    },
    "secret_gk_hex": "b9a08843e47ffe6f8230bede9afcb70ad3b4908bd9f0e5c0a0099d9aca3a4aa0",
}


# ------------------------------------------------------------------- crypto

class TestKat:
    """Every primitive must match the Rust core byte for byte."""

    def test_argon_params_recorded(self):
        assert KAT["argon2"] == {"len": 32, "m_kib": ARGON2_M_KIB,
                                 "p": ARGON2_P, "t": ARGON2_T}

    def test_effective_gk_earth(self):
        gk = unhex(KAT["gk_hex"])
        assert effective_gk(gk, "earth") == unhex(KAT["earth_gk_hex"])

    def test_effective_gk_normalizes_case_and_space(self):
        gk = unhex(KAT["gk_hex"])
        assert effective_gk(gk, "Secret") == unhex(KAT["secret_gk_hex"])
        assert effective_gk(gk, " secret\t") == unhex(KAT["secret_gk_hex"])

    def test_effective_gk_empty_word_returns_gk(self):
        gk = unhex(KAT["gk_hex"])
        assert effective_gk(gk, "") == gk
        assert effective_gk(gk, "   ") == gk
        assert effective_gk(gk, None) == gk

    def test_global_room_hex(self):
        assert global_room_hex(unhex(KAT["earth_gk_hex"])) == KAT["room_hex"]

    def test_admission_proof(self):
        proof = admission_proof(unhex(KAT["earth_gk_hex"]), KAT["peer_id"],
                                unhex(KAT["proof_nonce_hex"]))
        assert proof == unhex(KAT["admission_proof_hex"])

    def test_peer_id_from_libp2p_protobuf(self):
        raw = ed25519_raw_from_protobuf(unb64(KAT["public_key_b64"]))
        assert peer_id_from_public(raw) == KAT["peer_id"]

    def test_open_room_key(self):
        key = open_room_key(unhex(KAT["earth_gk_hex"]), unhex(KAT["room_hex"]),
                            KAT["key_ct_epoch"], KAT["peer_id"],
                            unb64(KAT["key_ct_b64"]))
        assert key == unhex(KAT["room_key_hex"])

    def test_room_key_delivery_is_epoch_bound(self):
        from ircmgr.oh_room import OhRoomError
        with pytest.raises(OhRoomError):
            open_room_key(unhex(KAT["earth_gk_hex"]), unhex(KAT["room_hex"]),
                          KAT["key_ct_epoch"] + 1, KAT["peer_id"],
                          unb64(KAT["key_ct_b64"]))

    def test_open_rust_sealed_chat_frame(self):
        rc = RoomCrypto(unhex(KAT["room_hex"]), 1, unhex(KAT["room_key_hex"]))
        assert rc.open(KAT["sealed"], KIND_CHAT).decode("utf-8") \
            == KAT["chat_plaintext"]

    def test_verify_rust_mail_signature(self):
        env_json = KAT["mail_msg"].split("|", 4)[4]
        item = {"from": KAT["peer_id"], "to": "recipient",
                "ts_ms": 1700000000001, "env_json": env_json,
                "sig_b64": KAT["mail_sig_b64"],
                "public_key_b64": KAT["public_key_b64"]}
        assert verify_mail_item(item) == KAT["peer_id"]

    def test_verify_mail_item_rejects_forged_sender(self):
        env_json = KAT["mail_msg"].split("|", 4)[4]
        item = {"from": "12D3KooWsomebodyelse", "to": "recipient",
                "ts_ms": 1700000000001, "env_json": env_json,
                "sig_b64": KAT["mail_sig_b64"],
                "public_key_b64": KAT["public_key_b64"]}
        from ircmgr.oh_room import OhRoomError
        with pytest.raises(OhRoomError):
            verify_mail_item(item)

    def test_seal_open_round_trip(self):
        rc = RoomCrypto(unhex(KAT["room_hex"]), 1, os.urandom(32))
        sealed = rc.seal(KAT["peer_id"], 1700000000999, KIND_CHAT,
                         "round trip ✓".encode("utf-8"))
        assert rc.open(sealed, KIND_CHAT).decode("utf-8") == "round trip ✓"

    def test_wrong_kind_does_not_open(self):
        from ircmgr.oh_room import OhRoomError, KIND_MEMBERS
        rc = RoomCrypto(unhex(KAT["room_hex"]), 1, unhex(KAT["room_key_hex"]))
        with pytest.raises(OhRoomError):
            rc.open(KAT["sealed"], KIND_MEMBERS)

    def test_seal_room_key_round_trip(self):
        gk, room, key = os.urandom(32), os.urandom(16), os.urandom(32)
        ct = seal_room_key(gk, room, 3, "12D3KooWguest", key)
        assert open_room_key(gk, room, 3, "12D3KooWguest", ct) == key

    def test_b64_matches_kat_encoding(self):
        assert b64(unb64(KAT["key_ct_b64"])) == KAT["key_ct_b64"]
        assert b64(b"") == ""


class TestHelpers:
    def test_generate_default_name(self):
        for _ in range(20):
            name = generate_default_name()
            assert name.startswith("deepfluxuser")
            assert name[12:].isdigit() and len(name[12:]) == 4

    def test_gen_room_phrase_shape(self):
        for _ in range(5):
            phrase = gen_room_phrase()
            parts = phrase.split("-")
            assert len(parts) == 6 and parts[-1].isdigit()

    def test_identity_persists(self, tmp_path):
        path = tmp_path / "id"
        seed1, _, peer1, pub1 = load_identity(path)
        seed2, _, peer2, pub2 = load_identity(path)
        assert seed1 == seed2 and peer1 == peer2 and pub1 == pub2
        assert path.exists()


# ------------------------------------------------------------------ fake hub

class FakeHub:
    """In-memory stand-in for the OnlyHumans hub with the same semantics:
    NX room election (current host may refresh), destructive inbox drain,
    and mail items carrying real signatures the recipients verify."""

    def __init__(self, gk: bytes | None = None) -> None:
        self.gk = gk or os.urandom(32)
        self._lock = threading.Lock()
        self.rooms: dict[str, dict] = {}
        self.inboxes: dict[str, list[dict]] = {}
        self.registered: set[str] = set()

    def fetch_gk(self):
        return "test-universe", self.gk

    def reg(self, peer_id, pub_b64, sign):
        self.registered.add(peer_id)

    def lookup_room(self, room_hex):
        with self._lock:
            rec = self.rooms.get(room_hex)
            return dict(rec) if rec else None

    def register_room(self, room_hex, peer_id, pub_b64, sign):
        with self._lock:
            existing = self.rooms.get(room_hex)
            if existing and existing["host_peer_id"] != peer_id:
                return False
            self.rooms[room_hex] = {"room_id": room_hex,
                                    "host_peer_id": peer_id}
            return True

    def mail_push_batch(self, peer_id, pub_b64, sign, batch):
        with self._lock:
            for to, env in batch:
                env_json = json.dumps(env, separators=(",", ":"),
                                      ensure_ascii=False)
                ts = int(time.time() * 1000)
                canonical = (f"OH1-mail-v1|{peer_id}|{to}|{ts}|{env_json}"
                             ).encode("utf-8")
                self.inboxes.setdefault(to, []).append({
                    "to": to, "from": peer_id, "public_key_b64": pub_b64,
                    "env_json": env_json, "ts_ms": ts,
                    "sig_b64": b64(sign(canonical)),
                })

    def mail_drain(self, peer_id, sign):
        with self._lock:
            return self.inboxes.pop(peer_id, [])

    def presence(self, token):
        return 7

    # -- test helpers -------------------------------------------------------

    def drop_room(self, room_hex):
        with self._lock:
            self.rooms.pop(room_hex, None)


def make_controller(tmp_path, hub, name="peer", word="deepflux",
                    state=None) -> OhRoomController:
    from config import ChatConfig
    cfg = ChatConfig(nickname=name)
    ctl = OhRoomController(
        cfg, state or IRCState(),
        hub=hub,
        identity_path=tmp_path / f"identity-{name}",
        history_dir=tmp_path / f"rooms-{name}",
        auto_loop=False,
    )
    return ctl


def events_of(ctl) -> list[dict]:
    out: list[dict] = []
    ctl.add_listener(out.append)
    return out


def seat_member(tmp_path, hub, host, member_name="guest"):
    """Join a second controller into the host's room, synchronously."""
    member = make_controller(tmp_path, hub, name=member_name)
    events = events_of(member)
    member.join(member_name, host.word)
    member._join_pass()          # sees the host record, mails a Join
    host._drain_pass()           # host seats the member (KeyDelivery)
    member._drain_pass()         # member adopts key + member table
    return member, events


# --------------------------------------------------------------- controller

class TestRoomBasics:
    def test_first_joiner_hosts(self, tmp_path):
        hub = FakeHub()
        host = make_controller(tmp_path, hub, name="alice")
        events = events_of(host)
        assert host.join("alice", "deepflux")
        assert host.role == "connecting"
        host._join_pass()
        assert host.role == "host"
        assert host.is_joined()
        assert hub.lookup_room(host._room_hex)["host_peer_id"] == host._peer_id
        assert any(e.get("type") == "state" and e.get("state") == "connected"
                   for e in events)
        # default room word + nick persisted for the next launch
        assert host._cfg.last_word == "deepflux"
        assert host._cfg.nickname == "alice"

    def test_member_seats_and_members_sync(self, tmp_path):
        hub = FakeHub()
        host = make_controller(tmp_path, hub, name="alice")
        host.join("alice", "deepflux")
        host._join_pass()
        member, _ = seat_member(tmp_path, hub, host)
        assert member.role == "member"
        assert member.is_joined()
        nicks = member._state.nicks_of(ROOM_NET_ID, "#lounge")
        assert "alice" in nicks and "guest" in nicks
        # host sees the member too (join broadcast + Members frame)
        assert "guest" in host._state.nicks_of(ROOM_NET_ID, "#lounge")

    def test_chat_both_directions(self, tmp_path):
        hub = FakeHub()
        host = make_controller(tmp_path, hub, name="alice")
        host.join("alice", "deepflux")
        host._join_pass()
        member, _ = seat_member(tmp_path, hub, host)

        assert member.send_message("hello from the member")
        host._drain_pass()
        host_msgs = [m for m in host._state.get_messages(ROOM_NET_ID, "#lounge")
                     if m.kind == "msg"]
        assert any(m.text == "hello from the member" for m in host_msgs)

        assert host.send_message("and hello from the host")
        member._drain_pass()
        member_msgs = [m for m in
                       member._state.get_messages(ROOM_NET_ID, "#lounge")
                       if m.kind == "msg"]
        assert any(m.text == "and hello from the host" for m in member_msgs)

    def test_replay_guard(self, tmp_path):
        hub = FakeHub()
        host = make_controller(tmp_path, hub, name="alice")
        host.join("alice", "deepflux")
        host._join_pass()
        member, _ = seat_member(tmp_path, hub, host)
        member.send_message("once")
        # duplicate the frame BEFORE draining (the drain is destructive)
        with hub._lock:
            items = hub.inboxes[host._peer_id]
            items.append(dict(items[-1]))
        host._drain_pass()
        msgs = [m for m in host._state.get_messages(ROOM_NET_ID, "#lounge")
                if m.kind == "msg" and m.text == "once"]
        assert len(msgs) == 1

    def test_rename_member_updates_tables(self, tmp_path):
        hub = FakeHub()
        host = make_controller(tmp_path, hub, name="alice")
        host.join("alice", "deepflux")
        host._join_pass()
        member, _ = seat_member(tmp_path, hub, host)
        assert member.rename("renamed-guest")
        host._drain_pass()   # host handles the re-join with the new name
        member._drain_pass()  # member adopts the fresh member table
        assert host._members[member._peer_id] == "renamed-guest"

    def test_leave_mails_leave(self, tmp_path):
        hub = FakeHub()
        host = make_controller(tmp_path, hub, name="alice")
        host.join("alice", "deepflux")
        host._join_pass()
        member, _ = seat_member(tmp_path, hub, host)
        member.leave()
        host._drain_pass()
        assert member._peer_id not in host._members
        assert "guest" not in host._state.nicks_of(ROOM_NET_ID, "#lounge")

    def test_rotation_seals_room(self, tmp_path):
        hub = FakeHub()
        host = make_controller(tmp_path, hub, name="alice")
        host.join("alice", "deepflux")
        host._join_pass()
        member, _ = seat_member(tmp_path, hub, host)

        assert host.rotate()
        assert host._room.epoch == 2
        member._drain_pass()   # member applies the rotation
        assert member._room.epoch == 2
        assert member._room.key == host._room.key

        # chat still works after rotation
        member.send_message("post-rotation")
        host._drain_pass()
        assert any(m.text == "post-rotation"
                   for m in host._state.get_messages(ROOM_NET_ID, "#lounge")
                   if m.kind == "msg")

        # a NEWCOMER is refused and told the room is sealed
        stranger = make_controller(tmp_path, hub, name="mallory")
        stranger_events = events_of(stranger)
        stranger.join("mallory", "deepflux")
        stranger._join_pass()
        host._drain_pass()      # host refuses: epoch > 1 and not a member
        stranger._drain_pass()  # stranger receives the Error envelope
        assert stranger.role == "connecting"  # never seated
        assert any("sealed" in (e.get("text") or "")
                   for e in stranger_events
                   if e.get("type") == "notice")

    def test_existing_member_can_reseat_after_rotation(self, tmp_path):
        hub = FakeHub()
        host = make_controller(tmp_path, hub, name="alice")
        host.join("alice", "deepflux")
        host._join_pass()
        member, _ = seat_member(tmp_path, hub, host)
        host.rotate()
        member._drain_pass()
        # the member re-requests a seat (rename / resync path): allowed
        member._send_join_mail()
        host._drain_pass()
        member._drain_pass()
        assert member.role == "member"
        assert member._room.epoch == 2

    def test_member_takes_over_when_host_record_dies(self, tmp_path):
        hub = FakeHub()
        host = make_controller(tmp_path, hub, name="alice")
        host.join("alice", "deepflux")
        host._join_pass()
        member, _ = seat_member(tmp_path, hub, host)
        old_key, old_epoch = member._room.key, member._room.epoch

        room_hex = member._room_hex  # shutdown() clears the host's copy
        host.shutdown()
        hub.drop_room(room_hex)      # the 300 s record TTL lapses
        # skip the member's real-world silence/retry pacing gates
        member._last_host_contact = 0.0
        member._last_join_try = 0.0
        member._cycle_pass()           # rediscover → claim → take over

        assert member.role == "host"
        assert member._room.key == old_key  # same key, same epoch
        assert member._room.epoch == old_epoch

    def test_bad_proof_cannot_seat(self, tmp_path):
        hub = FakeHub()
        host = make_controller(tmp_path, hub, name="alice")
        host.join("alice", "deepflux")
        host._join_pass()
        # a Join envelope with a garbage admission proof is dropped
        evil = make_controller(tmp_path, hub, name="evil")
        evil.join("evil", "deepflux")
        evil._join_pass()
        # overwrite the proof with junk, then deliver it to the host
        with hub._lock:
            for item in hub.inboxes[host._peer_id]:
                env = json.loads(item["env_json"])
                if "Join" in env:
                    env["Join"]["guest_proof_b64"] = b64(b"\x00" * 32)
                    item["env_json"] = json.dumps(env)
        host._drain_pass()
        assert evil._peer_id not in host._members

    def test_history_persists_across_sessions(self, tmp_path):
        shared_dir = tmp_path / "shared-rooms"
        hub = FakeHub()

        from config import ChatConfig
        first = OhRoomController(ChatConfig(), IRCState(), hub=hub,
                                 identity_path=tmp_path / "identity-h",
                                 history_dir=shared_dir, auto_loop=False)
        first.join("alice", "deepflux")
        first._join_pass()
        first.send_message("remembered line")

        # a fresh controller on the SAME identity + history dir replays it
        second = OhRoomController(ChatConfig(), IRCState(), hub=hub,
                                  identity_path=tmp_path / "identity-h",
                                  history_dir=shared_dir, auto_loop=False)
        second.join("alice", "deepflux")
        second._join_pass()
        msgs = [m for m in second._state.get_messages(ROOM_NET_ID, "#lounge")
                if m.kind == "msg"]
        assert any(m.text == "remembered line" for m in msgs)

    def test_history_file_is_sealed_at_rest(self, tmp_path):
        hub = FakeHub()
        host = make_controller(tmp_path, hub, name="alice")
        host.join("alice", "deepflux")
        host._join_pass()
        host.send_message("secret words")
        files = list((tmp_path / "rooms-alice").glob("*.jsonl"))
        assert files, "history file missing"
        blob = files[0].read_text(encoding="ascii")
        assert "secret words" not in blob

    def test_word_isolation(self, tmp_path):
        hub = FakeHub()
        a = make_controller(tmp_path, hub, name="alice")
        a.join("alice", "deepflux")
        a._join_pass()
        b = make_controller(tmp_path, hub, name="bob")
        b.join("bob", "other-word")
        b._join_pass()
        assert a._room_hex != b._room_hex
        assert b.role == "host"  # different room, so nobody was in it


# ----------------------------------------------------------------- GUI tab

class TestRoomTab:
    """The Room tab hosts the OnlyHumans join portal in a web view.
    tests/conftest.py keeps DF_NO_ROOM=1, so the default build path here
    is fully offline — the web view itself is only created without it."""

    @staticmethod
    def _app():
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            app = QApplication([])
        return app

    def _tab(self, **cfg_overrides):
        from config import DeeptorrentConfig
        from gui.room_tab import RoomTab
        cfg = DeeptorrentConfig()
        for key, value in cfg_overrides.items():
            setattr(cfg.chat, key, value)
        return RoomTab(cfg, None), cfg

    def test_portal_url_prefills_default_word(self):
        app = self._app()
        tab, _ = self._tab()
        assert tab.portal_url == \
            "https://onlyhumans.deepflux.space/join#room=deepflux"
        tab.shutdown()

    def test_portal_url_uses_configured_word_and_quotes_it(self):
        app = self._app()
        tab, _ = self._tab(default_word="My Word")
        assert tab.portal_url.endswith("#room=My%20Word")
        tab.shutdown()

    def test_df_no_room_builds_no_view(self):
        app = self._app()  # conftest keeps DF_NO_ROOM=1
        tab, _ = self._tab()
        assert tab._view is None and tab.web_profile is None
        tab.shutdown()  # must be a safe no-op

    def test_view_created_with_dedicated_persistent_profile(self,
                                                            monkeypatch):
        """Full view-creation path, offline: the portal URL is swapped for
        about:blank so the real site is never fetched."""
        app = self._app()
        monkeypatch.delenv("DF_NO_ROOM", raising=False)
        import gui.room_tab as mod
        monkeypatch.setattr(mod, "PORTAL_URL", "about:blank")
        from config import DeeptorrentConfig
        try:
            tab = mod.RoomTab(DeeptorrentConfig(), None)
        except Exception:
            pytest.skip("QWebEngineView unavailable in this environment")
        try:
            assert tab._view is not None
            assert tab.portal_url == "about:blank#room=deepflux"
            # Persistent + dedicated: the portal identity key in its
            # localStorage must survive restarts and never share storage
            # with the Browser tab's profile.
            assert not tab.web_profile.isOffTheRecord()
            assert tab.web_profile.storageName() == "deeptorrent-room"
            # The download hook is attachable (MainWindow wires the
            # browser save flow through it).
            assert tab.attach_download_handler(lambda *a: None) is True
        finally:
            # Tear the page down before the profile (QtWebEngine warns
            # when a profile is released with live pages).
            tab._view.page().deleteLater()
            tab._view.deleteLater()
            app.processEvents()
            tab.shutdown()


