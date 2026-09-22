"""OnlyHumans word-room member — the Room tab's chat transport.

DeepFlux speaks the OnlyHumans protocol (https://onlyhumans.deepflux.space)
as a native Python "mailbox-only" member — the same peer class as the
browser portal: no direct libp2p/QUIC links, everything travels through
the site's sealed inbox, end-to-end encrypted on this device. Members of
the OnlyHumans Windows app and the /join browser portal who type the same
room word land in the same room.

Protocol (mirrors C:\\OnlyHumans-main\\portal\\portal.ts + app.ts, which
are byte-compatible with the Rust core in C:\\OnlyHumans-app\\core and are
verified against its known-answer vectors — a trimmed copy of those
vectors is embedded in tests/test_oh_room.py):

  word -> egk   = Argon2id(word, salt=SHA256("OH1-pass-v2|"|gk|word),
                          m=64 MiB, t=3, p=1, 32 bytes)
  room          = hex(SHA256("OH1-room-v1|"|egk))[:32]  (16 bytes)
  GK            = "universe key", PUBLIC by design, fetched from
                  GET /api/gk ({version, gk_b64}), rotated per OnlyHumans
                  release (that is why every app version is its own room
                  universe).
  identity      = local Ed25519 seed (~/.deeptorrent/room_identity);
                  peer id = base58btc(identity multihash of the libp2p
                  Ed25519 PublicKey protobuf).
  admission     = HKDF-SHA256(egk, salt=[], info="adm"|peer|nonce)
  room key      = random 32 B minted by the room's host at epoch 1,
                  delivered to members sealed with XChaCha20-Poly1305
                  under keys HKDF-derived from (egk, room, epoch, recipient).
  chat frames   = per-message subkey HKDF(room_key, room|epoch,
                  kind|sender|seq), XChaCha20-Poly1305, AAD binds
                  kind/room/epoch/sender/seq, plaintext padded to size
                  buckets so message lengths stay hidden.
  host election = first writer wins via Redis NX behind PUT /api/room
                  (409 = a live record exists); records live 300 s and the
                  host refreshes every ~45 s. A member whose host went
                  silent re-runs discovery and takes the room over with
                  the key it already holds (same epoch).
  sealed rooms  = a host key rotation (epoch+1) closes the room: valid GK
                  proofs no longer mint seats from epoch 2 onward.

Delivery is store-and-forward: the hub (untrusted storage) keeps sealed,
signed envelopes per peer for 24 h (last 32, ~4 s per-sender push
throttle) and peers drain their own inbox destructively. The OnlyHumans
desktop apps drain every 30 s, the portal every 2-3.5 s — this client
drains every ~3 s, so chat with portal users is near-realtime and chat
with desktop-app peers rides the same "via site" fallback the portal
uses (up to ~30 s each way).

History persists locally, sealed at rest with a device key derived from
the identity seed (same posture as the OnlyHumans app's SQLite store).
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from argon2.low_level import Type as Argon2Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError

from ircmgr.state import (
    KIND_ACTION,
    KIND_JOIN,
    KIND_MESSAGE,
    KIND_PART,
    KIND_SERVER,
    ROOM_NET_ID,
    ChatMessage,
    IRCState,
)

logger = logging.getLogger(__name__)

ROOM_CHANNEL = "#lounge"
HUB_BASE = "https://onlyhumans.deepflux.space"
DEFAULT_WORD = "deepflux"

# Cadences (seconds) — mirrors of the portal loop. Drains are signed GETs
# of an usually-empty inbox; the hub rate-limits pushes, not drains.
DRAIN_SEATED = 3.5
DRAIN_UNSEATED = 2.0
CYCLE = 5.0
HOST_REFRESH = 45.0
HOST_SILENT = 45.0        # member: re-run discovery after this silence
REJOIN_RETRY = 10.0       # whole-join retry while a first contact fails
REG_EVERY = 31.0          # hub rate-limits reg to one per 30 s per peer
JOIN_MAIL_EVERY = 15.0    # re-nudge the host at most this often
HTTP_TIMEOUT = (5.0, 10.0)
MAIL_CHUNK = 16           # hub caps one push at 16 envelopes
MAIL_THROTTLE_WAIT = 4.2  # 429 = per-sender throttle; ride one window out
GK_TTL = 60.0             # /api/gk sends cache-control max-age=60
MAX_NAME = 32
MAX_WORD = 64
HISTORY_LOAD = 200        # messages replayed into the transcript on join
HISTORY_KEEP = 800        # file is trimmed to this many lines

ARGON2_M_KIB = 65536
ARGON2_T = 3
ARGON2_P = 1

_B64_ALPHABET = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                 "abcdefghijklmnopqrstuvwxyz"
                 "0123456789-_")
_B64_INDEX = {c: i for i, c in enumerate(_B64_ALPHABET)}
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Sealed-frame kinds — fixed-width 8-byte tags (portal.ts KIND).
KIND_CHAT = b"chat\0\0\0\0"
KIND_ROTATE = b"rotate\0\0"
KIND_MEMBERS = b"members\0"
KIND_GK_DELIV = b"gk-deliv"

PAD_BUCKETS = (128, 256, 512, 1024, 2048, 4096, 8192, 16384)

# Room-phrase generator word list (same list as portal.ts GEN_WORDS, a
# curated memorable-words set — no dictionary affinity needed).
GEN_WORDS = [
    "amber", "anchor", "apple", "arrow", "atlas", "aurora", "autumn", "avian",
    "basil", "beacon", "birch", "bishop", "bloom", "brass", "breeze", "bronze",
    "cactus", "canyon", "cedar", "chalk", "cherry", "cinder", "cliff", "clover",
    "cobalt", "comet", "coral", "cotton", "crane", "crater", "creek", "cypress",
    "dahlia", "damask", "dawn", "delta", "denim", "diesel", "doodle", "dragon",
    "dune", "eagle", "ember", "emerald", "falcon", "fable", "fennel", "fern",
    "fjord", "flame", "flint", "forest", "fossil", "foxglove", "frost",
    "gadget", "galaxy", "garnet", "ginger", "glacier", "glider", "granite",
    "grotto", "harbor", "hazel", "heron", "hollow", "honey", "horizon",
    "ignite", "indigo", "iris", "island", "ivory", "jasmine", "jasper",
    "jigsaw", "jungle", "juniper", "kayak", "kelp", "kernel", "kestrel",
    "kitten", "koala", "lagoon", "lantern", "lattice", "laurel", "lavender",
    "ledge", "lemon", "lilac", "linen", "lotus", "lumber", "lunar", "lynx",
    "magnet", "mango", "maple", "marble", "marigold", "meadow", "mercury",
    "midnight", "mimosa", "mineral", "mirage", "mosaic", "moss", "mustard",
    "nebula", "nectar", "needle", "nest", "nickel", "nimbus", "noodle",
    "north", "oasis", "oat", "obsidian", "octave", "olive", "onyx", "opal",
    "orbit", "orchid", "osprey", "otter", "oyster", "paddle", "pancake",
    "papaya", "parsley", "pebble", "pelican", "pepper", "petal", "pewter",
    "pigeon", "pigment", "pine", "pistachio", "pixel", "plasma", "plume",
    "polar", "pollen", "pomelo", "prairie", "prism", "pumpkin", "quartz",
    "quasar", "quill", "radish", "rainbow", "raven", "ribbon", "ridge",
    "ripple", "river", "robin", "rocket", "rosemary", "rustic", "saffron",
    "sage", "sailor", "salmon", "sandal", "sapphire", "scarf", "sequoia",
    "shadow", "shale", "shrimp", "silver", "siren", "snorkel", "solar",
    "sparrow", "spiral", "spruce", "squid", "starling", "stratus", "sugar",
    "sulfur", "summit", "sunset", "syrup", "tagine", "tangent", "thistle",
    "thunder", "tiger", "tinsel", "topaz", "tulip", "tundra", "turquoise",
    "umbra", "vanilla", "velvet", "vertex", "violet", "vortex", "walnut",
    "wander", "wasabi", "willow", "winter", "wombat", "yarrow", "yonder",
    "zephyr", "zinnia", "zodiac", "zombie", "zucchini",
]


class OhRoomError(Exception):
    """Base error for the OnlyHumans room subsystem."""


# ---------------------------------------------------------------- encoding

def b64(data: bytes) -> str:
    """The protocol's base64 (URL-safe alphabet, no padding)."""
    out = []
    for i in range(0, len(data), 3):
        b0 = data[i]
        b1 = data[i + 1] if i + 1 < len(data) else 0
        b2 = data[i + 2] if i + 2 < len(data) else 0
        n = (b0 << 16) | (b1 << 8) | b2
        out.append(_B64_ALPHABET[(n >> 18) & 63])
        out.append(_B64_ALPHABET[(n >> 12) & 63])
        if i + 1 < len(data):
            out.append(_B64_ALPHABET[(n >> 6) & 63])
        if i + 2 < len(data):
            out.append(_B64_ALPHABET[n & 63])
    return "".join(out)


def unb64(text: str) -> bytes:
    vals = bytes(_B64_INDEX[c] for c in text)
    out = bytearray()
    for i in range(0, len(vals), 4):
        chunk = vals[i:i + 4]
        n = 0
        for k, v in enumerate(chunk):
            n |= v << (18 - 6 * k)
        out.append((n >> 16) & 0xFF)
        if len(chunk) > 2:
            out.append((n >> 8) & 0xFF)
        if len(chunk) > 3:
            out.append(n & 0xFF)
    return bytes(out)


def u64le(value: int) -> bytes:
    return value.to_bytes(8, "little")


def unhex(text: str) -> bytes:
    return bytes.fromhex(text)


def _sha256(*parts: bytes) -> bytes:
    h = hashes.Hash(hashes.SHA256())
    for p in parts:
        h.update(p)
    return h.finalize()


def _hkdf32(ikm: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=info).derive(ikm)


# ---------------------------------------------------------------- libp2p id

def public_key_protobuf(pub_raw: bytes) -> bytes:
    """libp2p Ed25519 PublicKey protobuf: {varint type=1; bytes data(32)}."""
    return b"\x08\x01\x12\x20" + pub_raw


def ed25519_raw_from_protobuf(protobuf: bytes) -> bytes:
    if (len(protobuf) != 36 or protobuf[0] != 0x08 or protobuf[1] != 0x01
            or protobuf[2] != 0x12 or protobuf[3] != 0x20):
        raise OhRoomError("bad libp2p public key protobuf")
    return protobuf[4:]


def peer_id_from_public(pub_raw: bytes) -> str:
    """base58btc of the identity multihash over the pubkey protobuf."""
    protobuf = public_key_protobuf(pub_raw)
    mh = b"\x00" + bytes([len(protobuf)]) + protobuf
    n = int.from_bytes(mh, "big")
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = _B58_ALPHABET[rem] + out
    for byte in mh:
        if byte != 0:
            break
        out = "1" + out
    return out or "1"


# ------------------------------------------------------------------- crypto

def normalize_word(word: str) -> str:
    return word.strip().lower()


def sanitize_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    return name[:MAX_NAME]


def effective_gk(gk: bytes, word: Optional[str]) -> bytes:
    """Stretch a room word into that word-room's effective GK (Argon2id,
    memory-hard — mirrors effective_gk() in the Rust core)."""
    w = "" if word is None else normalize_word(word)
    if not w:
        return gk
    salt = _sha256(b"OH1-pass-v2|", gk, w.encode("utf-8"))
    return hash_secret_raw(
        secret=w.encode("utf-8"), salt=salt,
        time_cost=ARGON2_T, memory_cost=ARGON2_M_KIB,
        parallelism=ARGON2_P, hash_len=32, type=Argon2Type.ID,
    )


def global_room_hex(gk: bytes) -> str:
    return _sha256(b"OH1-room-v1|", gk).hex()[:32]


def admission_proof(gk: bytes, prover_id: str, nonce: bytes) -> bytes:
    return _hkdf32(gk, b"", b"adm" + prover_id.encode("utf-8") + nonce)


def _aad(kind: bytes, room: bytes, epoch: int, sender: str, seq: int) -> bytes:
    return (b"OH1" + kind + room + u64le(epoch)
            + sender.encode("utf-8") + u64le(seq))


def _xchacha_seal(key: bytes, nonce: bytes, aad: bytes, plaintext: bytes) -> bytes:
    return crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, nonce, key)


def _xchacha_open(key: bytes, nonce: bytes, aad: bytes, ciphertext: bytes) -> bytes:
    try:
        return crypto_aead_xchacha20poly1305_ietf_decrypt(
            ciphertext, aad, nonce, key)
    except CryptoError as exc:
        raise OhRoomError("sealed frame failed authentication") from exc


def seal_room_key(gk: bytes, room_id: bytes, epoch: int, recipient: str,
                  key: bytes) -> bytes:
    """Deliver the room key to `recipient` bound to (GK, room, epoch):
    no two delivery targets share cipher output, so a former member with
    an old key plus captured deliveries can never XOR out a newer one."""
    info = b"gk-deliv-v2|" + u64le(epoch) + recipient.encode("utf-8")
    nonce = _hkdf32(gk, room_id, info + b"|n")[:24]
    key_enc = _hkdf32(gk, room_id, info + b"|k")
    return _xchacha_seal(key_enc, nonce,
                         _aad(KIND_GK_DELIV, room_id, epoch, recipient, 0), key)


def open_room_key(gk: bytes, room_id: bytes, epoch: int, recipient: str,
                  ciphertext: bytes) -> bytes:
    info = b"gk-deliv-v2|" + u64le(epoch) + recipient.encode("utf-8")
    nonce = _hkdf32(gk, room_id, info + b"|n")[:24]
    key_enc = _hkdf32(gk, room_id, info + b"|k")
    return _xchacha_open(key_enc, nonce,
                         _aad(KIND_GK_DELIV, room_id, epoch, recipient, 0),
                         ciphertext)


def _pad(body: bytes) -> bytes:
    real = 4 + len(body)
    target = next((b for b in PAD_BUCKETS if b >= real), None)
    if target is None:
        target = ((real + PAD_BUCKETS[-1] - 1) // PAD_BUCKETS[-1]) * PAD_BUCKETS[-1]
    out = bytearray(target)
    struct.pack_into("<I", out, 0, len(body))
    out[4:4 + len(body)] = body
    return bytes(out)


def _unpad(padded: bytes) -> bytes:
    (length,) = struct.unpack_from("<I", padded, 0)
    return padded[4:4 + length]


class RoomCrypto:
    """One (room key, epoch) generation — seals and opens chat frames."""

    def __init__(self, room_id: bytes, epoch: int, key: bytes) -> None:
        self.room_id = room_id
        self.epoch = epoch
        self.key = key

    @property
    def room_hex(self) -> str:
        return self.room_id.hex()

    def _message_key(self, sender: str, seq: int, kind: bytes) -> bytes:
        return _hkdf32(self.key, self.room_id + u64le(self.epoch),
                       kind + sender.encode("utf-8") + u64le(seq))

    def seal(self, sender: str, seq: int, kind: bytes,
             plaintext: bytes) -> Dict[str, Any]:
        mk = self._message_key(sender, seq, kind)
        nonce = os.urandom(24)
        ct = _xchacha_seal(mk, nonce, _aad(kind, self.room_id, self.epoch,
                                           sender, seq), _pad(plaintext))
        return {"room_id_hex": self.room_hex, "epoch": self.epoch,
                "sender": sender, "seq": seq,
                "nonce_b64": b64(nonce), "ct_b64": b64(ct)}

    def open(self, frame: Dict[str, Any], kind: bytes) -> bytes:
        if frame.get("room_id_hex") != self.room_hex:
            raise OhRoomError("frame belongs to a different room")
        epoch = int(frame.get("epoch", 0))
        if epoch != self.epoch:
            raise OhRoomError(f"frame epoch {epoch} != current {self.epoch}")
        sender = str(frame.get("sender", ""))
        seq = int(frame.get("seq", 0))
        mk = self._message_key(sender, seq, kind)
        padded = _xchacha_open(mk, unb64(str(frame.get("nonce_b64", ""))),
                               _aad(kind, self.room_id, self.epoch, sender, seq),
                               unb64(str(frame.get("ct_b64", ""))))
        return _unpad(padded)

    def apply_rotation(self, next_epoch: int, next_key: bytes) -> None:
        if next_epoch != self.epoch + 1:
            raise OhRoomError("rotation epoch does not follow")
        self.epoch = next_epoch
        self.key = next_key


def build_join(room_hex: str, peer_id: str, name: str, gk: bytes) -> Dict[str, Any]:
    nonce = os.urandom(16)
    proof = admission_proof(gk, peer_id, nonce)
    return {"Join": {"room_id_hex": room_hex, "guest_id": peer_id,
                     "guest_nonce_b64": b64(nonce),
                     "guest_proof_b64": b64(proof), "name": name}}


# ------------------------------------------------------------------- hub io

def _mail_canonical(sender: str, to: str, ts_ms: int, env_json: str) -> bytes:
    return f"OH1-mail-v1|{sender}|{to}|{ts_ms}|{env_json}".encode("utf-8")


def _drain_canonical(peer: str, ts_ms: int) -> bytes:
    return f"OH1-drain-v1|{peer}|{ts_ms}".encode("utf-8")


def _reg_canonical(peer: str, pub: str, addrs: List[str], ts_ms: int) -> bytes:
    return f"OH1-reg|{peer}|{pub}|{','.join(addrs)}|{ts_ms}".encode("utf-8")


def _room_canonical(room: str, host: str, pub: str, ts_ms: int) -> bytes:
    return f"OH1-room|{room}|{host}|{pub}|{ts_ms}".encode("utf-8")


def verify_mail_item(item: Dict[str, Any]) -> str:
    """Full recipient-side verification: the included key must derive the
    sender's peer id and the signature must cover the exact envelope JSON."""
    pub_raw = ed25519_raw_from_protobuf(unb64(str(item.get("public_key_b64", ""))))
    sender = str(item.get("from", ""))
    if peer_id_from_public(pub_raw) != sender:
        raise OhRoomError("mail item: key does not derive sender")
    Ed25519PublicKey.from_public_bytes(pub_raw).verify(
        unb64(str(item.get("sig_b64", ""))),
        _mail_canonical(sender, str(item.get("to", "")),
                        int(item.get("ts_ms", 0)), str(item.get("env_json", ""))))
    return sender


class Hub:
    """HTTP client for the OnlyHumans rendezvous hub (untrusted storage:
    every response is signature-verified before use)."""

    def __init__(self, base_url: str = HUB_BASE,
                 session: Optional[requests.Session] = None) -> None:
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._gk_cache: Optional[Tuple[str, bytes, float]] = None
        self._gk_lock = threading.Lock()

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", HTTP_TIMEOUT)
        return self._session.request(method, self._base + path, **kw)

    def fetch_gk(self) -> Tuple[str, bytes]:
        with self._gk_lock:
            if self._gk_cache and time.time() - self._gk_cache[2] < GK_TTL:
                return self._gk_cache[0], self._gk_cache[1]
        r = self._request("GET", "/api/gk", headers={"cache-control": "no-cache"})
        if r.status_code == 503:
            raise OhRoomError("the room hub is not serving a room key yet")
        r.raise_for_status()
        payload = r.json()
        try:
            gk = unb64(str(payload.get("gk_b64", "")))
        except KeyError as exc:
            raise OhRoomError("room hub served a malformed key") from exc
        if len(gk) != 32:
            raise OhRoomError("room hub served a malformed key")
        version = str(payload.get("version", ""))
        with self._gk_lock:
            self._gk_cache = (version, gk, time.time())
        return version, gk

    def reg(self, peer_id: str, pub_b64: str,
            sign: Callable[[bytes], bytes]) -> None:
        ts = _now_ms()
        body = {"peer_id": peer_id, "public_key_b64": pub_b64,
                "addrs": [], "ts_ms": ts,
                "sig_b64": b64(sign(_reg_canonical(peer_id, pub_b64, [], ts)))}
        r = self._request("PUT", "/api/reg", json=body)
        if not r.ok:
            raise OhRoomError(f"reg failed: {r.status_code}")

    def lookup_room(self, room_hex: str) -> Optional[Dict[str, Any]]:
        r = self._request("GET", f"/api/room/{room_hex}")
        if r.status_code == 404:
            return None
        if not r.ok:
            raise OhRoomError(f"room lookup failed: {r.status_code}")
        return r.json()

    def register_room(self, room_hex: str, peer_id: str, pub_b64: str,
                      sign: Callable[[bytes], bytes]) -> bool:
        """Claim hosting; False on 409 (a live record names someone else)."""
        ts = _now_ms()
        body = {"room_id": room_hex, "host_peer_id": peer_id,
                "host_public_key_b64": pub_b64, "ts_ms": ts,
                "sig_b64": b64(sign(_room_canonical(room_hex, peer_id,
                                                    pub_b64, ts)))}
        r = self._request("PUT", "/api/room", json=body)
        if r.status_code == 409:
            return False
        if not r.ok:
            raise OhRoomError(f"room register failed: {r.status_code}")
        return True

    def mail_push_batch(self, peer_id: str, pub_b64: str,
                        sign: Callable[[bytes], bytes],
                        batch: List[Tuple[str, Dict[str, Any]]]) -> None:
        """Chunked single requests (hub caps a push at 16 items); a 429 is
        the per-sender throttle window — wait one out and try once more
        instead of dropping mail the composer already echoed locally."""
        for i in range(0, len(batch), MAIL_CHUNK):
            items = []
            for to, env in batch[i:i + MAIL_CHUNK]:
                env_json = json.dumps(env, separators=(",", ":"),
                                      ensure_ascii=False)
                ts = _now_ms()
                items.append({
                    "to": to, "from": peer_id, "public_key_b64": pub_b64,
                    "env_json": env_json, "ts_ms": ts,
                    "sig_b64": b64(sign(_mail_canonical(peer_id, to, ts,
                                                        env_json))),
                })
            r = self._request("PUT", "/api/inbox", json={"items": items})
            if r.status_code == 429:
                time.sleep(MAIL_THROTTLE_WAIT)
                r = self._request("PUT", "/api/inbox", json={"items": items})
            if not r.ok:
                raise OhRoomError(f"mail push failed: {r.status_code}")

    def mail_drain(self, peer_id: str,
                   sign: Callable[[bytes], bytes]) -> List[Dict[str, Any]]:
        ts = _now_ms()
        sig = b64(sign(_drain_canonical(peer_id, ts)))
        r = self._request(
            "GET",
            f"/api/inbox/{peer_id}?ts_ms={ts}&sig_b64={sig}")
        if not r.ok:
            raise OhRoomError(f"mail drain failed: {r.status_code}")
        return list(r.json().get("items") or [])

    def presence(self, token: str) -> int:
        try:
            r = self._request("POST", "/api/presence", json={"token": token})
            if not r.ok:
                return 0
            return int(r.json().get("online", 0))
        except Exception:
            return 0


def _now_ms() -> int:
    return int(time.time() * 1000)


# ----------------------------------------------------------------- identity

def load_identity(path: Path) -> Tuple[bytes, bytes, str, str]:
    """Load (or create) the local Ed25519 identity.

    Returns (seed, public_raw, peer_id, public_key_b64). The seed lives in
    its own file — deliberately NOT in config.json, so a Settings Export
    can never transplant one machine's chat identity onto another."""
    path.parent.mkdir(parents=True, exist_ok=True)
    seed_hex = ""
    if path.exists():
        try:
            seed_hex = path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.debug("room identity read failed", exc_info=True)
    if len(seed_hex) != 64:
        seed_hex = secrets.token_hex(32)
        try:
            path.write_text(seed_hex + "\n", encoding="utf-8")
        except OSError:
            logger.debug("room identity write failed", exc_info=True)
    seed = bytes.fromhex(seed_hex)
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    pub_raw = pub.public_bytes_raw()
    return seed, pub_raw, peer_id_from_public(pub_raw), b64(public_key_protobuf(pub_raw))


def generate_default_name() -> str:
    """The auto-join default display name: deepfluxuser####."""
    return f"deepfluxuser{secrets.randbelow(9000) + 1000}"


def gen_room_phrase() -> str:
    """A generated unguessable room phrase (portal's genRoomPhrase)."""
    words = [secrets.choice(GEN_WORDS) for _ in range(5)]
    digits = str(secrets.randbelow(90) + 10)
    return "-".join(words + [digits])


# ------------------------------------------------------------------ history

class _History:
    """Sealed-at-rest per-room message log (JSONL under ~/.deeptorrent/rooms).

    Lines are XChaCha20-Poly1305 sealed under a device key HKDF-derived
    from the identity seed, so the plaintext files never contain
    conversation content — same posture as the OnlyHumans app's store."""

    def __init__(self, room_hex: str, seed: bytes, directory: Path) -> None:
        self._path = directory / f"{room_hex}.jsonl"
        self._key = _hkdf32(seed, b"", b"df-room-history-v1")
        self._aad = room_hex.encode("utf-8")

    def append(self, ts: float, nick: str, text: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            nonce = os.urandom(24)
            line = json.dumps({"ts": ts, "nick": nick, "text": text},
                              ensure_ascii=False).encode("utf-8")
            ct = _xchacha_seal(self._key, nonce, self._aad, line)
            with open(self._path, "ab") as fh:
                fh.write(b64(nonce).encode("ascii") + b":" +
                         b64(ct).encode("ascii") + b"\n")
        except Exception:
            logger.debug("room history append failed", exc_info=True)
        self._trim()

    def load(self, limit: int = HISTORY_LOAD) -> List[Dict[str, Any]]:
        try:
            rows = self._path.read_text(encoding="ascii").splitlines()
        except (OSError, ValueError):
            return []
        out: List[Dict[str, Any]] = []
        for row in rows[-limit:]:
            try:
                nonce_b64, ct_b64 = row.split(":", 1)
                pt = _xchacha_open(self._key, unb64(nonce_b64), self._aad,
                                   unb64(ct_b64))
                entry = json.loads(pt.decode("utf-8"))
                if isinstance(entry, dict) and entry.get("text"):
                    out.append(entry)
            except Exception:
                continue
        return out

    def _trim(self) -> None:
        try:
            if not self._path.exists() or \
                    self._path.stat().st_size < 256 * 1024:
                return
            rows = self._path.read_text(encoding="ascii").splitlines()
            if len(rows) > HISTORY_KEEP + 200:
                self._path.write_text(
                    "\n".join(rows[-HISTORY_KEEP:]) + "\n", encoding="ascii")
        except Exception:
            logger.debug("room history trim failed", exc_info=True)


# --------------------------------------------------------------- controller

class OhRoomController:
    """Owns the room lifecycle and mirrors it into the shared IRCState.

    Role flow: left → connecting → (member | host) → left. A member whose
    host went silent re-runs discovery and takes the room over with the
    key it already holds (same epoch) — the room survives its host
    leaving. Everything runs on ONE daemon thread; network calls never
    hold the state lock.
    """

    def __init__(self, chat_config, state: IRCState, *,
                 hub: Optional[Hub] = None,
                 identity_path: Optional[Path] = None,
                 history_dir: Optional[Path] = None,
                 auto_loop: bool = True) -> None:
        self._cfg = chat_config
        self._state = state
        self._hub = hub or Hub()
        home = Path.home() / ".deeptorrent"
        self._identity_path = identity_path or (home / "room_identity")
        self._history_dir = history_dir or (home / "rooms")
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

        self._lock = threading.RLock()
        self._role = "left"
        self._nick = ""
        self._word = ""
        self._egk: Optional[bytes] = None
        self._room_hex = ""
        self._room: Optional[RoomCrypto] = None
        self._members: Dict[str, str] = {}
        self._is_host = False
        self._host_id = ""          # only this host's KeyDelivery may seat us
        self._my_seq = 0
        self._seen_seq: Dict[str, int] = {}
        self._online = 0
        self._presence_token = secrets.token_hex(16)
        self._egk_cache: Dict[str, bytes] = {}
        self._history: Optional[_History] = None
        self._history_loaded = False

        # Pending join (name, word) set by join(); the loop retries the
        # whole sequence until it sticks (gk fetch can fail at startup).
        self._pending: Optional[Tuple[str, str]] = None
        self._last_join_try = 0.0
        self._last_join_mail = 0.0
        self._last_reg = 0.0
        self._last_refresh = 0.0
        self._last_host_contact = 0.0
        self._last_presence = 0.0

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._auto_loop = auto_loop

        seed, pub_raw, peer_id, pub_b64 = load_identity(self._identity_path)
        self._seed = seed
        self._peer_id = peer_id
        self._pub_b64 = pub_b64
        self._signer = Ed25519PrivateKey.from_private_bytes(seed)
        self._sign = self._signer.sign  # bound once; the hub takes callable

        state.ensure_network(ROOM_NET_ID, host="DeepFlux Room", port=0,
                             tls=False)
        state.set_topic(ROOM_NET_ID, ROOM_CHANNEL, self._topic_text())

    # -- public API (thread-safe; join/leave async) ---------------------------

    @property
    def role(self) -> str:
        with self._lock:
            if self._role == "left":
                return "left"
            if self._role == "connecting":
                return "connecting"
            return "host" if self._is_host else "member"

    @property
    def nick(self) -> str:
        with self._lock:
            return self._nick

    @property
    def word(self) -> str:
        with self._lock:
            return self._word

    @property
    def online(self) -> int:
        with self._lock:
            return self._online

    @property
    def host_id(self) -> str:
        with self._lock:
            return self._host_id

    def is_joined(self) -> bool:
        with self._lock:
            return self._role == "active"

    @property
    def encrypted(self) -> bool:
        return True  # sealed frames always; kept for the tab's status line

    def add_listener(self, cb: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(cb)

    def join(self, nick: str = "", word: str = "") -> bool:
        nick = sanitize_name(nick or self._cfg.nickname
                             or generate_default_name())
        word = normalize_word(word or self._cfg.last_word
                              or self._cfg.default_word or DEFAULT_WORD)
        if not nick or not word or len(word) > MAX_WORD:
            self._note("Enter a name and a room word first.")
            return False
        with self._lock:
            if self._role != "left":
                return False
            self._role = "connecting"
            self._nick = nick
            self._word = word
            self._pending = (nick, word)
            self._history_loaded = False
        self._set_connecting_state()
        self._start_loop()
        self._wake.set()
        return True

    def rename(self, name: str) -> bool:
        name = sanitize_name(name)
        if not name:
            return False
        with self._lock:
            old = self._nick
            self._nick = name
            seated = self._room is not None
            host = self._is_host
            host_id = self._host_id
        if not seated:
            return True
        self._cfg.nickname = name
        self._save_config()
        self._state.rename_nick(ROOM_NET_ID, old, name)
        self._emit_names()
        self._note(f"You are now {name}.")
        if host:
            with self._lock:
                self._members[self._peer_id] = name
            self._broadcast_members_async()
        elif host_id:
            # A Join from an existing peer updates the host's member table
            # and re-delivers the key + fresh member list.
            self._send_join_mail()
        return True

    def leave(self) -> None:
        self._stop_room("You left the room.")

    def shutdown(self) -> None:
        """App close: best-effort leave, then stop the loop deterministically."""
        try:
            self._stop_room(None)
        except Exception:
            logger.debug("room shutdown failed", exc_info=True)
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(2.0)

    def send_message(self, text: str, action: bool = False) -> bool:
        with self._lock:
            room = self._room
            members = dict(self._members)
        if room is None or not text.strip():
            return False
        seq = self._bump_seq()
        frame = room.seal(self._peer_id, seq, KIND_CHAT,
                          text.strip().encode("utf-8"))
        self._record_message(self._nick, text.strip(), action,
                             time.time(), own=True)
        self._fan_out({"Chat": {"frame": frame}}, members)
        return True

    def rotate(self) -> bool:
        """Host-only: mint the next room key and close the room to
        newcomers (OnlyHumans key rotation / "seal")."""
        with self._lock:
            room = self._room
            members = dict(self._members)
        if room is None or not self._is_host:
            return False
        next_key = os.urandom(32)
        next_epoch = room.epoch + 1
        body = json.dumps({"next_epoch": next_epoch,
                           "next_key_b64": b64(next_key)},
                          separators=(",", ":")).encode("utf-8")
        seq = self._bump_seq()
        frame = room.seal(self._peer_id, seq, KIND_ROTATE, body)
        self._fan_out({"Rotate": {"frame": frame}}, members)
        room.apply_rotation(next_epoch, next_key)
        topic = self._topic_text(sealed=True)
        self._state.set_topic(ROOM_NET_ID, ROOM_CHANNEL, topic)
        self._emit({"type": "topic", "network": ROOM_NET_ID,
                    "channel": ROOM_CHANNEL, "topic": topic})
        self._note(f"The room key was rotated (generation {next_epoch}) — "
                   "people who join later will not see this room.")
        return True

    # -- loop ------------------------------------------------------------------

    def _start_loop(self) -> None:
        if not self._auto_loop:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="oh-room-loop")
            self._thread.start()

    def _run(self) -> None:
        next_drain = 0.0
        next_cycle = 0.0
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                seated = self._room is not None
                active = self._role != "left"
            if active:
                if now >= next_drain:
                    self._safe(self._drain_pass)
                    next_drain = time.time() + (DRAIN_SEATED if seated
                                                else DRAIN_UNSEATED)
                if now >= next_cycle:
                    self._safe(self._cycle_pass)
                    next_cycle = time.time() + CYCLE
            self._wake.wait(0.5)
            self._wake.clear()

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            logger.debug("room loop pass failed", exc_info=True)

    # -- join sequence (one attempt; the loop retries) --------------------------

    def _join_pass(self) -> None:
        with self._lock:
            pending = self._pending
            if pending is None or self._role == "left":
                return
            nick, word = pending
            if time.time() - self._last_join_try < REJOIN_RETRY and self._room_hex:
                return
            self._last_join_try = time.time()
        _, gk = self._hub.fetch_gk()
        egk = self._egk_cache.get(word)
        if egk is None:
            egk = effective_gk(gk, word)
            self._egk_cache[word] = egk
        room_hex = global_room_hex(egk)
        with self._lock:
            self._egk = egk
            self._room_hex = room_hex
        # Best-effort first contact: a transient failure must not bounce the
        # user out — the loop retries the whole sequence.
        rec = None
        try:
            self._hub.reg(self._peer_id, self._pub_b64, self._sign)
            self._last_reg = time.time()
            rec = self._hub.lookup_room(room_hex)
        except Exception:
            logger.debug("room first contact failed", exc_info=True)
            return
        if rec and rec.get("host_peer_id") == self._peer_id:
            # Our own record survived a restart: re-claim it and mint a fresh
            # room — members converge back through their own re-discovery.
            self._try_found(force_register=True)
            return
        if rec:
            self._seek_seat(str(rec.get("host_peer_id", "")))
            return
        self._try_found()

    def _try_found(self, force_register: bool = False) -> None:
        with self._lock:
            room_hex, egk = self._room_hex, self._egk
            nick = self._nick
        if not room_hex or egk is None:
            return
        won = self._hub.register_room(room_hex, self._peer_id, self._pub_b64,
                                      self._sign)
        if not won and not force_register:
            rec = self._hub.lookup_room(room_hex)
            if rec and rec.get("host_peer_id") != self._peer_id:
                self._seek_seat(str(rec.get("host_peer_id", "")))
                return
            if not rec:
                return
        with self._lock:
            if self._pending is None or self._role == "left":
                return  # left while we were networking
            self._is_host = True
            self._host_id = self._peer_id
            self._room = RoomCrypto(bytes.fromhex(room_hex), 1, os.urandom(32))
            self._members = {self._peer_id: nick}
            self._my_seq = _now_ms()
            self._seen_seq.clear()
            self._role = "active"
            self._last_refresh = time.time()
        self._on_seated(hosting=True)

    def _seek_seat(self, host: str) -> None:
        with self._lock:
            if self._pending is None or self._role == "left":
                return
            self._is_host = False
            self._host_id = host
            self._last_join_mail = time.time()
        self._send_join_mail()

    def _send_join_mail(self) -> None:
        with self._lock:
            room_hex, egk = self._room_hex, self._egk
            host_id = self._host_id
            nick = self._nick
        if not (room_hex and egk and host_id):
            return
        env = build_join(room_hex, self._peer_id, nick, egk)
        try:
            self._hub.mail_push_batch(
                self._peer_id, self._pub_b64, self._sign,
                [(host_id, env)])
        except Exception:
            logger.debug("seat request mail failed", exc_info=True)

    # -- connection maintenance -------------------------------------------------

    def _cycle_pass(self) -> None:
        with self._lock:
            room = self._room
            pending = self._pending is not None
            room_hex = self._room_hex
            host = self._is_host
        if room is None:
            if pending:
                self._join_pass()
            return
        now = time.time()
        if host:
            if now - self._last_refresh > HOST_REFRESH:
                self._host_refresh()
        elif now - self._last_host_contact > HOST_SILENT:
            self._rediscover()
        if now - self._last_reg > REG_EVERY:
            self._last_reg = now
            try:
                self._hub.reg(self._peer_id, self._pub_b64, self._sign)
            except Exception:
                logger.debug("room re-register failed", exc_info=True)
        if now - self._last_presence > REG_EVERY:
            self._last_presence = now
            count = self._hub.presence(self._presence_token)
            if count:
                with self._lock:
                    self._online = count

    def _host_refresh(self) -> None:
        self._last_refresh = time.time()
        with self._lock:
            room_hex = self._room_hex
        try:
            won = self._hub.register_room(room_hex, self._peer_id,
                                          self._pub_b64, self._sign)
        except Exception:
            logger.debug("host refresh failed", exc_info=True)
            return
        if won:
            return
        try:
            rec = self._hub.lookup_room(room_hex)
        except Exception:
            return
        if rec and rec.get("host_peer_id") != self._peer_id:
            with self._lock:
                self._is_host = False
            self._note("Another live host holds the room now — staying "
                       "as a member.")

    def _rediscover(self) -> None:
        with self._lock:
            room_hex = self._room_hex
            host_id = self._host_id
            members = dict(self._members)
        try:
            rec = self._hub.lookup_room(room_hex)
        except Exception:
            return
        if not rec:
            if time.time() - self._last_join_try < REJOIN_RETRY:
                return
            self._last_join_try = time.time()
            try:
                won = self._hub.register_room(room_hex, self._peer_id,
                                              self._pub_b64,
                                              self._sign)
            except Exception:
                return
            if won:
                with self._lock:
                    # keep our RoomCrypto: same key, same epoch
                    self._is_host = True
                    self._host_id = self._peer_id
                    self._last_refresh = time.time()
                self._note("The host left — this device keeps the room open.")
            return
        holder = str(rec.get("host_peer_id", ""))
        healthy = (holder == host_id and holder in members
                   and time.time() - self._last_host_contact < 300.0)
        if healthy:
            return
        if time.time() - self._last_join_mail < JOIN_MAIL_EVERY:
            return
        self._seek_seat(holder)

    # -- inbox ------------------------------------------------------------------

    def _drain_pass(self) -> None:
        try:
            items = self._hub.mail_drain(self._peer_id, self._sign)
        except Exception:
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                sender = verify_mail_item(item)
            except Exception:
                continue
            try:
                env = json.loads(str(item.get("env_json", "")))
            except ValueError:
                continue
            if not isinstance(env, dict):
                continue
            try:
                self._handle(sender, env)
            except Exception:
                logger.debug("room envelope failed", exc_info=True)

    # -- envelope handling --------------------------------------------------------

    def _handle(self, sender: str, env: Dict[str, Any]) -> None:
        if "KeyDelivery" in env:
            self._handle_key_delivery(sender, env["KeyDelivery"])
        elif "Chat" in env:
            self._handle_chat(sender, env["Chat"])
        elif "Members" in env:
            self._handle_members(sender, env["Members"])
        elif "Rotate" in env:
            self._handle_rotate(sender, env["Rotate"])
        elif "Join" in env:
            self._host_handle_join(sender, env["Join"])
        elif "Leave" in env:
            self._handle_leave(sender, env["Leave"])
        elif "Error" in env:
            message = str(env["Error"].get("message", ""))
            if message:
                self._note(message)

    def _handle_key_delivery(self, sender: str, kd: Dict[str, Any]) -> None:
        with self._lock:
            if self._is_host or sender != self._host_id:
                return  # only the host we asked may seat or re-key us
            if kd.get("room_id_hex") != self._room_hex:
                return
            epoch = kd.get("epoch")
            if not isinstance(epoch, int) or epoch < 1:
                return
            if self._room is not None and epoch < self._room.epoch:
                return  # no regression
            egk = self._egk
            room_hex = self._room_hex
        key = open_room_key(egk, bytes.fromhex(room_hex), epoch, self._peer_id,
                            unb64(str(kd.get("key_ct_b64", ""))))
        members = {str(m.get("peer", "")): str(m.get("name", ""))
                   for m in kd.get("members", []) if isinstance(m, dict)}
        with self._lock:
            if self._pending is None or self._role == "left":
                return
            first_seat = self._role == "connecting"
            self._room = RoomCrypto(bytes.fromhex(room_hex), epoch, key)
            self._members = members
            self._my_seq = _now_ms()
            self._seen_seq.clear()
            self._last_host_contact = time.time()
            self._role = "active"
        if first_seat:
            self._on_seated(hosting=False)
        else:
            # A silent re-seat (rename, missed rotation, takeover fork):
            # adopt the fresh key/member table without re-announcing.
            self._sync_members(dict(self._members), quiet_own=True)

    def _handle_chat(self, sender: str, chat: Dict[str, Any]) -> None:
        with self._lock:
            room = self._room
            host_id = self._host_id
        frame = chat.get("frame") if isinstance(chat, dict) else None
        if room is None or not isinstance(frame, dict):
            return
        if int(frame.get("epoch", 0)) > room.epoch:
            self._send_join_mail()  # missed a rotation: ask for a re-seat
            return
        body = room.open(frame, KIND_CHAT).decode("utf-8", "replace")
        if sender == host_id:
            self._last_host_contact = time.time()
        who = str(frame.get("sender", ""))
        seq = int(frame.get("seq", 0))
        if seq <= self._seen_seq.get(who, 0):
            return  # replay guard
        self._seen_seq[who] = seq
        name = self._members.get(who) or who[:10]
        self._record_message(name, body, False, time.time(), own=False)

    def _handle_members(self, sender: str, members_env: Dict[str, Any]) -> None:
        with self._lock:
            room = self._room
            host_id = self._host_id
        frame = members_env.get("frame") if isinstance(members_env, dict) else None
        if room is None or not isinstance(frame, dict):
            return
        if int(frame.get("epoch", 0)) > room.epoch:
            self._send_join_mail()
            return
        body = room.open(frame, KIND_MEMBERS).decode("utf-8", "replace")
        if sender == host_id:
            self._last_host_contact = time.time()
        try:
            listing = json.loads(body).get("members", [])
        except ValueError:
            return
        self._sync_members({str(m.get("peer", "")): str(m.get("name", ""))
                            for m in listing if isinstance(m, dict)})

    def _handle_rotate(self, sender: str, rotate_env: Dict[str, Any]) -> None:
        with self._lock:
            room = self._room
            host_id = self._host_id
            if self._is_host or sender != host_id:
                return
        frame = rotate_env.get("frame") if isinstance(rotate_env, dict) else None
        if room is None or not isinstance(frame, dict):
            return
        body = room.open(frame, KIND_ROTATE).decode("utf-8", "replace")
        secret = json.loads(body)
        room.apply_rotation(int(secret["next_epoch"]),
                            unb64(str(secret["next_key_b64"])))
        self._last_host_contact = time.time()
        topic = self._topic_text(sealed=True)
        self._state.set_topic(ROOM_NET_ID, ROOM_CHANNEL, topic)
        self._emit({"type": "topic", "network": ROOM_NET_ID,
                    "channel": ROOM_CHANNEL, "topic": topic})
        self._note(f"The room key was rotated (generation {room.epoch}) — "
                   "the room is closed to newcomers.")

    def _handle_leave(self, sender: str, leave: Dict[str, Any]) -> None:
        with self._lock:
            if not self._is_host:
                return
            if leave.get("room_id_hex") != self._room_hex:
                return
            name = self._members.pop(sender, None)
        if name is None:
            return
        self._record_membership("part", name)
        self._broadcast_members_async()

    def _host_handle_join(self, sender: str, j: Dict[str, Any]) -> None:
        with self._lock:
            room = self._room
            egk = self._egk
            room_hex = self._room_hex
            is_host = self._is_host
        if not is_host or room is None:
            return
        if j.get("room_id_hex") != room_hex or j.get("guest_id") != sender:
            return
        # The first rotation seals the room: a valid GK proof no longer
        # mints a seat from epoch 2 onward.
        if room.epoch > 1 and sender not in self._members:
            self._reply_error(sender, "the room is sealed (key rotated) — "
                                      "ask a member for a new room word")
            return
        nonce = unb64(str(j.get("guest_nonce_b64", "")))
        expect = admission_proof(egk, sender, nonce)
        got = unb64(str(j.get("guest_proof_b64", "")))
        if not secrets.compare_digest(expect, got):
            return  # proof failed
        name = sanitize_name(str(j.get("name", ""))) or sender[:10]
        with self._lock:
            is_new = sender not in self._members
            self._members[sender] = name
        if is_new:
            self._record_membership("join", name)
        kd = {"KeyDelivery": {
            "room_id_hex": room_hex,
            "epoch": room.epoch,
            "key_ct_b64": b64(seal_room_key(egk, room.room_id, room.epoch,
                                            sender, room.key)),
            "members": [{"peer": p, "name": n}
                        for p, n in self._members.items()],
        }}
        batch = [(sender, kd)]
        members_frame = self._seal_members_frame()
        if members_frame is not None:
            for peer in self._members:
                if peer != self._peer_id:
                    batch.append((peer, {"Members": {"frame": members_frame}}))
        try:
            self._hub.mail_push_batch(self._peer_id, self._pub_b64,
                                      self._sign, batch)
        except Exception:
            logger.debug("key delivery mail failed", exc_info=True)

    def _reply_error(self, to: str, message: str) -> None:
        try:
            self._hub.mail_push_batch(
                self._peer_id, self._pub_b64, self._sign,
                [(to, {"Error": {"message": message}})])
        except Exception:
            logger.debug("error reply mail failed", exc_info=True)

    # -- host helpers -----------------------------------------------------------

    def _seal_members_frame(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            room = self._room
        if room is None:
            return None
        seq = self._bump_seq()
        body = json.dumps(
            {"members": [{"peer": p, "name": n}
                         for p, n in self._members.items()]},
            separators=(",", ":")).encode("utf-8")
        return room.seal(self._peer_id, seq, KIND_MEMBERS, body)

    def _broadcast_members_async(self) -> None:
        frame = self._seal_members_frame()
        if frame is None:
            return
        with self._lock:
            members = dict(self._members)
        self._fan_out({"Members": {"frame": frame}}, members)

    def _fan_out(self, env: Dict[str, Any], members: Dict[str, str]) -> None:
        batch = [(peer, env) for peer in members if peer != self._peer_id]
        if not batch:
            return
        try:
            self._hub.mail_push_batch(self._peer_id, self._pub_b64,
                                      self._sign, batch)
        except Exception:
            logger.debug("room fan-out failed", exc_info=True)

    def _bump_seq(self) -> int:
        with self._lock:
            self._my_seq = max(self._my_seq + 1, _now_ms())
            return self._my_seq

    # -- seating / membership mirroring -------------------------------------------

    def _on_seated(self, hosting: bool) -> None:
        with self._lock:
            nick = self._nick
            word = self._word
        self._history = _History(self._room_hex, self._seed, self._history_dir)
        # History lands BEFORE the connected event so the tab's re-render
        # picks it up with everything else (replay: no per-line events).
        if not self._history_loaded:
            self._history_loaded = True
            for entry in self._history.load():
                try:
                    self._state.record(ROOM_NET_ID, ChatMessage(
                        ts=float(entry.get("ts", time.time())),
                        kind=KIND_MESSAGE,
                        nick=str(entry.get("nick", "")),
                        text=str(entry.get("text", ""))), ROOM_CHANNEL)
                except Exception:
                    continue
        self._state.ensure_channel(ROOM_NET_ID, ROOM_CHANNEL)
        self._sync_members(dict(self._members), quiet_own=True)
        self._state.set_connected(ROOM_NET_ID, True, nick=nick)
        self._emit({"type": "state", "network": ROOM_NET_ID,
                    "state": "connected", "nick": nick})
        where = "hosting" if hosting else "seated"
        self._note(f"Joined the “{word}” room as {nick} ({where} — messages "
                   "are sealed end-to-end and travel via the room hub).")
        self._emit_names()
        self._cfg.nickname = nick
        self._cfg.last_word = word
        self._save_config()

    def _sync_members(self, members: Dict[str, str], quiet_own: bool = False) -> None:
        """Mirror the host's member table into the nick list, emitting
        join/part events for the deltas."""
        with self._lock:
            self._members = dict(members)
            own = self._nick
        names = [sanitize_name(n) or p[:10] for p, n in members.items()]
        current = set(self._state.nicks_of(ROOM_NET_ID, ROOM_CHANNEL).keys())
        fresh = [n for n in dict.fromkeys(names) if n and n not in current]
        gone = [n for n in current if n not in set(names)]
        for n in fresh:
            own_join = quiet_own and n == own
            self._state.add_nick(ROOM_NET_ID, ROOM_CHANNEL, n)
            if not own_join:
                self._record_membership("join", n, quiet=own_join)
        for n in gone:
            if n == own and quiet_own:
                continue
            self._record_membership("part", n)
        if fresh or gone:
            self._emit_names()

    # -- teardown -----------------------------------------------------------------

    def _stop_room(self, detail: Optional[str]) -> None:
        with self._lock:
            room = self._room
            members = dict(self._members)
            pending = self._pending
            self._pending = None
            self._role = "left"
            self._room = None
            self._members = {}
            self._is_host = False
            self._host_id = ""
            self._room_hex = ""
            self._egk = None
            self._seen_seq.clear()
            self._history_loaded = False
            self._last_join_try = 0.0
            self._last_join_mail = 0.0
        if room is not None and members:
            # Polite exit: members/host drop us from their tables at once
            # instead of waiting out the host record's TTL.
            try:
                self._hub.mail_push_batch(
                    self._peer_id, self._pub_b64, self._sign,
                    [(peer, {"Leave": {"room_id_hex": room.room_hex}})
                     for peer in members if peer != self._peer_id])
            except Exception:
                logger.debug("leave mail failed", exc_info=True)
        if pending is not None:
            self._state.set_connected(ROOM_NET_ID, False)
            self._emit({"type": "state", "network": ROOM_NET_ID,
                        "state": "disconnected",
                        **({"detail": detail} if detail else {})})
            self._emit_names()

    # -- state mirroring --------------------------------------------------------

    def _record_membership(self, kind: str, nick: str, ts: float = 0.0,
                           quiet: bool = False) -> None:
        ts = ts or time.time()
        if kind == "join":
            self._state.add_nick(ROOM_NET_ID, ROOM_CHANNEL, nick)
            msg = ChatMessage(ts=ts, kind=KIND_JOIN, nick=nick,
                              text=f"{nick} joined")
            event = {"type": "join", "network": ROOM_NET_ID,
                     "channel": ROOM_CHANNEL, "nick": nick, "ts": ts}
        else:
            self._state.remove_nick(ROOM_NET_ID, nick, ROOM_CHANNEL)
            msg = ChatMessage(ts=ts, kind=KIND_PART, nick=nick,
                              text=f"{nick} left")
            event = {"type": "part", "network": ROOM_NET_ID,
                     "channel": ROOM_CHANNEL, "nick": nick, "ts": ts}
        self._state.record(ROOM_NET_ID, msg, ROOM_CHANNEL)
        if not quiet:
            self._emit(event)
            self._emit_names()

    def _record_message(self, nick: str, text: str, action: bool, ts: float,
                        own: bool) -> None:
        msg = ChatMessage(ts=ts, kind=KIND_ACTION if action else KIND_MESSAGE,
                          nick=nick, text=text)
        self._state.record(ROOM_NET_ID, msg, ROOM_CHANNEL)
        history = getattr(self, "_history", None)
        if history is not None and not action:
            history.append(ts, nick, text)
        self._emit({
            "type": "action" if action else "message",
            "network": ROOM_NET_ID, "channel": ROOM_CHANNEL,
            "nick": nick, "text": text, "own": own, "ts": ts,
        })

    def _emit_names(self) -> None:
        self._emit({"type": "names", "network": ROOM_NET_ID,
                    "channel": ROOM_CHANNEL,
                    "nicks": self._state.nicks_of(ROOM_NET_ID, ROOM_CHANNEL)})

    def _set_connecting_state(self) -> None:
        self._state.set_connecting(ROOM_NET_ID, True)
        self._emit({"type": "state", "network": ROOM_NET_ID,
                    "state": "connecting"})

    def _topic_text(self, sealed: bool = False) -> str:
        base = "DeepFlux Room — every word is a room (OnlyHumans protocol, " \
               "end-to-end sealed)"
        return base + (" · SEALED (key rotated — closed to newcomers)"
                       if sealed else "")

    def _note(self, text: str) -> None:
        self._state.record(ROOM_NET_ID, ChatMessage(
            ts=time.time(), kind=KIND_SERVER, text=text), ROOM_CHANNEL)
        self._emit({"type": "notice", "network": ROOM_NET_ID,
                    "channel": ROOM_CHANNEL, "nick": "", "text": text,
                    "ts": time.time()})

    def _emit(self, event: Dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(event)
            except Exception:
                logger.debug("room listener failed", exc_info=True)

    def _save_config(self) -> None:
        try:
            from config import DeeptorrentConfig
            self._cfg.to_file(DeeptorrentConfig.default_config_path())
        except Exception:
            logger.debug("room config save failed", exc_info=True)
