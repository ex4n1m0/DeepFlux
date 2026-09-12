"""DeepFlux Room — a serverless community chat shown on the IRC page.

The room is NOT IRC: no IRC server is involved anywhere. The first user who
presses Join hosts a small TCP chat server inside the app; everyone else
connects to that host directly (peer-to-peer star topology). When the host
leaves, the next member's client notices within seconds and takes over.

Discovery ("who is hosting right now?") needs one shared point — without any,
"first user online" is undecidable across the internet. That point is a tiny
key-value slot on the project website (``website/api/room.js``, Upstash Redis,
same backing store as the live counters): it ever holds a POINTER to the
current host (address + timestamp + host token), never a message. In the
encrypted build the pointer itself is sealed with the in-box room key, so a
source build cannot even see the encrypted room. A direct-address join
("ip:port") skips discovery entirely.

Crypto (setup build only — see ``config.shared_room_secret``):
  * the secret ships exclusively in the untracked ``_embedded_keys.py``
    (``SHARED_ROOM_KEY``), obfuscated like every other shared key, and is
    never a config field — there is nothing to persist or scrub;
  * AES-256-GCM per message, keys derived per direction (client→host and
    host→client are different keys, so a sealed record can never be
    reflected back), random 96-bit nonces, purpose-bound AAD;
  * join records carry an HMAC proof so a build without the key cannot
    connect, and the discovery blob is sealed + squatted-proof;
  * the ceiling is the shared-key ceiling: anyone holding the setup exe
    holds the key. Messages are sealed on the wire (and invisible to the
    discovery endpoint); they are not secret from other room members.

GitHub/source builds (no key) get the same room, unencrypted, as their own
separate lounge with its own discovery slot.

The controller writes into the SHARED ``IRCState`` under ``ROOM_NET_ID`` and
emits events shaped exactly like ``IRCClientCore`` events, so the IRC tab
renders the room with its existing code paths. Qt-free; the GUI bridges
events through its queued Qt signal like it does for IRC.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import requests

from ircmgr.state import (
    ChatMessage,
    IRCState,
    KIND_ACTION,
    KIND_JOIN,
    KIND_MESSAGE,
    KIND_PART,
    KIND_SERVER,
    ROOM_NET_ID,
)

logger = logging.getLogger(__name__)

# The room's single channel (display name on the IRC page tree).
ROOM_CHANNEL = "#lounge"

# Where the host pointer lives. Never carries chat content. The env var is
# for development against a local `vercel dev` instance.
ROOM_URL = os.environ.get("DEEPFLUX_ROOM_URL", "https://deepflux.space/api/room")

DEFAULT_LISTEN_PORT = 7766
PORT_SCAN_ATTEMPTS = 20          # 7766..7785 when the base port is busy
MAX_USERS = 64
MAX_TEXT_CHARS = 2000
MAX_WIRE_BYTES = 16 * 1024
HISTORY_RECORDS = 100            # replayed to every new joiner
RATE_MAX_MESSAGES = 8            # per connection…
RATE_WINDOW_SECONDS = 5.0        # …inside this window
PING_INTERVAL = 20.0
READ_TIMEOUT = 75.0              # pings keep both directions fed well inside this
JOIN_PROOF_WINDOW = 120.0        # seconds
POINTER_TTL_SECONDS = 120        # server-side expiry of the host pointer
REFRESH_INTERVAL = 30.0          # host re-announces well inside the TTL
POINTER_FRESH_SECONDS = 100.0    # joiners ignore pointers older than this
CONNECT_TIMEOUT = 5.0
WELCOME_TIMEOUT = 8.0
NICK_RE = re.compile(r"^[^\s,]{1,24}$")
RESERVED_NICKS = {"room", "lounge"}
# Rotation ("make private"): in-flight records sealed under the key that was
# JUST replaced stay readable for a short grace window, and a second rotation
# cannot be requested within the cooldown (a member spamming rekeys would
# churn the room's keys for no benefit).
REKEY_GRACE_SECONDS = 60.0
REKEY_COOLDOWN_SECONDS = 10.0
_SECRET_RE = re.compile(r"^[0-9a-f]{16,128}$")

# Controller tunables (tests shrink them).
MAINTENANCE_POLL = 2.0
RECONNECT_DELAYS = (2.0, 5.0, 10.0, 20.0, 20.0)
RECONNECT_ATTEMPTS = 5


def _topic_text(encrypted: bool) -> str:
    if encrypted:
        return ("DeepFlux community room — messages are sealed with the "
                "in-box room key (setup build)")
    return "DeepFlux community room — UNENCRYPTED (source build)"


def clean_nick(nick: str) -> str:
    return nick.strip()[:24]


def valid_nick(nick: str) -> bool:
    return bool(NICK_RE.match(nick)) and nick.lower() not in RESERVED_NICKS


def valid_room_secret(secret: str) -> bool:
    """Rotated room keys are hex strings from secrets.token_hex."""
    return bool(_SECRET_RE.match(secret or ""))


def clean_text(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").strip()[:MAX_TEXT_CHARS]


# ---------------------------------------------------------------------------
# key derivation + record sealing
# ---------------------------------------------------------------------------

def _derive(secret: str, label: str) -> bytes:
    return hashlib.sha256(
        ("dfroom/v1/" + label).encode("ascii") + b"\x00" + secret.encode("utf-8")
    ).digest()


class RoomCodec:
    """Seals room records with the in-box secret (or passes them through).

    ``label`` picks the derived key ("c2h" client→host, "h2c" host→client,
    "master" for the discovery blob and proofs); ``kind`` is bound as AES-GCM
    AAD so a sealed record for one purpose cannot be replayed as another.
    """

    def __init__(self, secret: str) -> None:
        self.encrypted = bool(secret)
        if self.encrypted:
            self._keys = {label: _derive(secret, label)
                          for label in ("master", "c2h", "h2c")}
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            self._aes = {label: AESGCM(key) for label, key in self._keys.items()}
            self.room_id = _derive(secret, "id")[:10].hex()
        else:
            self._keys = {}
            self.room_id = "lounge"

    # -- payload sealing ----------------------------------------------------

    def seal(self, label: str, kind: str, payload: Dict[str, Any]) -> str:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if not self.encrypted:
            return "p" + body
        nonce = os.urandom(12)
        ct = self._aes[label].encrypt(
            nonce, body.encode("utf-8"), f"dfroom/v1/{kind}".encode("ascii"))
        return (base64.b64encode(nonce).decode("ascii") + "."
                + base64.b64encode(ct).decode("ascii"))

    def open(self, label: str, kind: str, value: object) -> Optional[Dict[str, Any]]:
        if not isinstance(value, str) or not value:
            return None
        try:
            if not self.encrypted:
                if not value.startswith("p"):
                    return None
                payload = json.loads(value[1:])
                return payload if isinstance(payload, dict) else None
            nonce_b64, dot, ct_b64 = value.partition(".")
            if not dot:
                return None
            plain = self._aes[label].decrypt(
                base64.b64decode(nonce_b64, validate=True),
                base64.b64decode(ct_b64, validate=True),
                f"dfroom/v1/{kind}".encode("ascii"),
            )
            payload = json.loads(plain.decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    # -- join proof / discovery blob ------------------------------------------

    def proof(self, value: str) -> str:
        if not self.encrypted:
            return ""
        return hmac.new(self._keys["master"], value.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def check_proof(self, value: str, proof: str, ts: float) -> bool:
        if not self.encrypted:
            return True
        if abs(time.time() - ts) > JOIN_PROOF_WINDOW:
            return False
        return hmac.compare_digest(self.proof(value), str(proof or ""))

    def seal_pointer(self, pointer: Dict[str, Any]) -> str:
        return self.seal("master", "pointer", pointer)

    def open_pointer(self, blob: object) -> Optional[Dict[str, Any]]:
        pointer = self.open("master", "pointer", blob)
        if not isinstance(pointer, dict) or not isinstance(
                pointer.get("endpoints"), list):
            return None
        return pointer


# ---------------------------------------------------------------------------
# discovery (the website "phone book": pointers only, never messages)
# ---------------------------------------------------------------------------

class Rendezvous:
    """Client for website/api/room.js. Every failure is silent (None/False)."""

    def __init__(self, base_url: str = ROOM_URL) -> None:
        self._url = base_url

    def _post(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            r = requests.post(self._url, json=payload, timeout=6)
            data = r.json() if r.status_code == 200 else None
            r.close()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def lookup(self, room_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = requests.get(self._url, params={"room": room_id}, timeout=6)
            data = r.json() if r.status_code == 200 else None
            r.close()
            if not isinstance(data, dict) or not data.get("ok"):
                return None
            return data.get("blob")
        except Exception:
            return None

    def claim(self, room_id: str, token: str, blob: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """('ok', None) | ('taken', existing pointer) | ('unavailable', None).

        ``token`` is the controller's stable host token — the same one later
        used for refresh/withdraw, which is how the server knows the slot is
        still ours."""
        data = self._post({"action": "announce", "room": room_id,
                           "blob": blob, "token": token})
        if data is None or not data.get("ok"):
            return ("unavailable", None)
        if data.get("taken"):
            return ("taken", data.get("blob"))
        return ("ok", None)

    def refresh(self, room_id: str, token: str, blob: str) -> str:
        """'ok' | 'taken' (another host claimed the slot) | 'unavailable'.

        Only 'taken' demotes the host — an offline discovery endpoint must
        never tear down a working room."""
        data = self._post({"action": "refresh", "room": room_id,
                           "blob": blob, "token": token})
        if data is None or not data.get("ok"):
            if data and data.get("taken"):
                return "taken"
            return "unavailable"
        return "ok"

    def withdraw(self, room_id: str, token: str) -> None:
        self._post({"action": "leave", "room": room_id, "token": token})

    def my_ip(self) -> Optional[str]:
        try:
            r = requests.get(self._url, params={"mode": "myip"}, timeout=6)
            data = r.json() if r.status_code == 200 else None
            r.close()
            ip = data.get("ip") if isinstance(data, dict) else None
            return ip if isinstance(ip, str) and ip else None
        except Exception:
            return None


def pointer_still_fresh(pointer: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(pointer, dict):
        return False
    try:
        age = time.time() - float(pointer.get("ts", 0))
    except (TypeError, ValueError):
        return False
    return 0 <= age < POINTER_FRESH_SECONDS


# ---------------------------------------------------------------------------
# wire helpers
# ---------------------------------------------------------------------------

def _recv_line(file) -> Optional[str]:
    try:
        line = file.readline()
    except (OSError, ValueError):
        return None
    if not line:
        return None
    return line.strip()


def _send_obj(sock: socket.socket, lock: threading.Lock, rec: Dict[str, Any]) -> bool:
    data = json.dumps(rec, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_WIRE_BYTES:
        return False
    try:
        with lock:
            sock.sendall(data + b"\n")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# host
# ---------------------------------------------------------------------------

class RoomHost:
    """The chat server the first user runs. Star topology: every message goes
    through here, so the host is the authority for nick→connection mapping
    (clients cannot forge each other's nicks)."""

    def __init__(self, codec: RoomCodec, *, bind_host: str = "",
                 port: int = DEFAULT_LISTEN_PORT,
                 on_event: Optional[Callable[..., None]] = None) -> None:
        self._codec = codec
        self._bind_host = bind_host
        self._want_port = int(port) or DEFAULT_LISTEN_PORT
        self._on_event = on_event or (lambda *a, **k: None)
        self._users: Dict[int, Dict[str, Any]] = {}
        self._history: Deque[Dict[str, Any]] = deque(maxlen=HISTORY_RECORDS)
        self._lock = threading.Lock()
        self._srv: Optional[socket.socket] = None
        self._running = threading.Event()
        self._next_id = 1
        # Rotation state: records sealed under the immediately-previous key
        # stay decodable for a short grace window after a rekey.
        self._prev_codec: Optional[RoomCodec] = None
        self._rekeyed_at = 0.0
        self._last_rotate = 0.0
        self.local_nick = ""
        self.port = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self, nick: str) -> bool:
        self.local_nick = nick
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        for candidate in range(self._want_port, self._want_port + PORT_SCAN_ATTEMPTS):
            try:
                srv.bind((self._bind_host, candidate))
                break
            except OSError:
                continue
        else:
            srv.close()
            return False
        srv.listen(16)
        srv.settimeout(1.0)
        self._srv = srv
        self.port = srv.getsockname()[1]
        self._running.set()
        with self._lock:
            self._users[0] = {"id": 0, "nick": nick, "sock": None,
                              "send_lock": threading.Lock(), "times": deque(),
                              "addr": ("local", 0)}
        threading.Thread(target=self._accept_loop, daemon=True,
                         name="room-host-accept").start()
        self._on_event("started", port=self.port)
        return True

    def stop(self, reason: str = "closed") -> None:
        was_running = self._running.is_set()
        self._running.clear()
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        with self._lock:
            users = list(self._users.values())
            self._users.clear()
        if was_running:
            closing = {"t": "closing", "reason": reason}
            for user in users:
                if user["sock"] is None:
                    continue
                _send_obj(user["sock"], user["send_lock"], closing)
                self._close_user_handles(user)
            self._on_event("stopped", reason=reason)

    @staticmethod
    def _close_user_handles(user: Dict[str, Any]) -> None:
        # shutdown() first — it unblocks the member's reader thread parked in
        # readline() and sends the FIN at once (sock.close() alone is deferred
        # while the makefile object still references the socket).
        sock = user.get("sock")
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        file = user.get("file")
        if file is not None:
            try:
                file.close()
            except OSError:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def running(self) -> bool:
        return self._running.is_set()

    def user_nicks(self) -> List[str]:
        with self._lock:
            return [user["nick"] for user in self._users.values()]

    # -- local (host user) input ----------------------------------------------

    def say(self, text: str, action: bool = False) -> bool:
        text = clean_text(text)
        if not text or not self._running.is_set():
            return False
        self._broadcast_message(self.local_nick, text, action)
        return True

    # -- key rotation ("make private") ---------------------------------------

    def rotate(self) -> bool:
        """Generate a fresh room key and hand it to everyone connected.

        The new secret travels sealed under the CURRENT key, so only people
        already in the room receive it. From then on this host only accepts
        the new key — anyone joining later, holding only the previous key,
        is locked out (and the controller re-announces discovery under the
        new key-derived room id, so they cannot even find the room)."""
        now = time.monotonic()
        with self._lock:
            if not self._running.is_set():
                return False
            if now - self._last_rotate < REKEY_COOLDOWN_SECONDS:
                return False
            self._last_rotate = now
        new_secret = secrets.token_hex(32)
        record = {"t": "rekey",
                  "e": self._codec.seal("h2c", "rekey", {"secret": new_secret})}
        self._fanout(record)
        self._prev_codec = self._codec
        self._rekeyed_at = now
        self._codec = RoomCodec(new_secret)
        self._on_event("rotated", secret=new_secret)
        return True

    # -- internals -------------------------------------------------------------

    def _accept_loop(self) -> None:
        while self._running.is_set():
            try:
                sock, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            sock.settimeout(READ_TIMEOUT)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            threading.Thread(target=self._reader, args=(sock, addr),
                             daemon=True, name="room-host-reader").start()
        if self._running.is_set():
            # the listener died unexpectedly (not a deliberate stop) —
            # the controller notices via this event and recovers
            self._running.clear()
            self._on_event("stopped", reason="accept failed")

    def _reader(self, sock: socket.socket, addr) -> None:
        user: Optional[Dict[str, Any]] = None
        try:
            file = sock.makefile("r", encoding="utf-8", errors="replace")
            while self._running.is_set():
                line = _recv_line(file)
                if line is None or len(line) > MAX_WIRE_BYTES:
                    break
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    break
                if not isinstance(rec, dict):
                    break
                kind = rec.get("t")
                if kind == "ping":
                    # Pre-join only this thread writes to the socket, so a
                    # throwaway lock is enough; afterwards use the user's own.
                    lock = user["send_lock"] if user else threading.Lock()
                    if not _send_obj(sock, lock, {"t": "pong"}):
                        break
                    continue
                if user is None:
                    if kind != "join":
                        break
                    user = self._handle_join(sock, file, addr, rec)
                    if user is None:
                        break
                    continue
                if kind == "msg":
                    if not self._handle_msg(user, rec):
                        continue
                elif kind == "rekey_req":
                    # Only a member of the CURRENT room can produce a valid
                    # sealed request — that proof IS the permission.
                    if self._codec.open("c2h", "rekey_req", rec.get("e")) is None:
                        self._drop_user(user["sock"], user["nick"], "bad-record")
                        continue
                    self.rotate()
                # unknown kinds are ignored (forward compatibility)
        except Exception:
            logger.debug("room host reader failed", exc_info=True)
        finally:
            # Close the FILE too: sock.close() alone is deferred while a
            # makefile() object still references it, so the peer would never
            # see the FIN and the drop below would not fire.
            try:
                file.close()
            except OSError:
                pass
            nick = user["nick"] if user else ""
            self._drop_user(sock, nick or None, "disconnected")

    @staticmethod
    def _blank_lock(sock: socket.socket) -> threading.Lock:
        # pongs are tiny; a dedicated lock per socket is not worth it
        return _PONG_LOCKS.setdefault(sock, threading.Lock())

    def _handle_join(self, sock: socket.socket, file, addr,
                     rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        nick = clean_nick(str(rec.get("nick", "")))
        err = None
        with self._lock:
            if not valid_nick(nick):
                err = "bad-nick"
            elif any(u["nick"].lower() == nick.lower() for u in self._users.values()):
                err = "nick-in-use"
            elif len(self._users) >= MAX_USERS:
                err = "full"
        if err is None:
            try:
                ts = float(rec.get("ts", 0.0))
            except (TypeError, ValueError):
                ts = 0.0
            if not self._codec.check_proof(f"{nick}|{int(ts)}",
                                           str(rec.get("proof", "")), ts):
                err = "bad-proof"
        if err is not None:
            _send_obj(sock, threading.Lock(), {"t": "err", "code": err})
            try:
                sock.close()
            except OSError:
                pass
            return None

        with self._lock:
            uid = self._next_id
            self._next_id += 1
            user = {"id": uid, "nick": nick, "sock": sock, "file": file,
                    "addr": addr,
                    "send_lock": threading.Lock(),
                    "times": deque(maxlen=RATE_MAX_MESSAGES + 1)}
            self._users[uid] = user
            users = self._snapshot_users()
            history = list(self._history)
        welcome = {"t": "welcome", "v": 1, "you": nick,
                   "mode": "enc" if self._codec.encrypted else "plain",
                   "topic": _topic_text(self._codec.encrypted),
                   "users": users, "history": history}
        if not _send_obj(sock, user["send_lock"], welcome):
            self._drop_user(sock, nick, "disconnected")
            return None
        now = time.time()
        with self._lock:
            join_rec = {"t": "join", "nick": nick, "ts": now,
                        "users": self._snapshot_users()}
        self._remember(join_rec)
        self._fanout(join_rec)
        self._on_event("join", nick=nick, ts=now)
        return user

    def _open_member_record(self, kind: str, value: object) -> Optional[Dict[str, Any]]:
        """Decode a member record under the current key, falling back to the
        immediately-previous key inside the post-rotation grace window (a
        member's in-flight message may have been sealed moments before the
        rekey record reached it)."""
        payload = self._codec.open("c2h", kind, value)
        if payload is not None:
            return payload
        if (self._prev_codec is not None
                and time.monotonic() - self._rekeyed_at < REKEY_GRACE_SECONDS):
            return self._prev_codec.open("c2h", kind, value)
        return None

    def _handle_msg(self, user: Dict[str, Any], rec: Dict[str, Any]) -> bool:
        payload = self._open_member_record("msg", rec.get("e"))
        if payload is None:
            self._drop_user(user["sock"], user["nick"], "bad-record")
            return False
        text = clean_text(str(payload.get("text", "")))
        if not text:
            return True
        now = time.monotonic()
        times: Deque[float] = user["times"]
        times.append(now)
        while times and now - times[0] > RATE_WINDOW_SECONDS:
            times.popleft()
        if len(times) > RATE_MAX_MESSAGES:
            _send_obj(user["sock"], user["send_lock"], {"t": "err", "code": "rate"})
            self._drop_user(user["sock"], user["nick"], "flooding")
            return False
        self._broadcast_message(user["nick"], text, bool(payload.get("action")))
        return True

    def _broadcast_message(self, nick: str, text: str, action: bool) -> None:
        rec = {"t": "msg", "nick": nick, "ts": time.time(),
               "e": self._codec.seal("h2c", "msg", {"text": text, "action": action})}
        self._remember(rec)
        self._fanout(rec)
        self._on_event("message", nick=nick, text=text, action=action,
                       ts=rec["ts"], own=(nick == self.local_nick))

    def _drop_user(self, sock: socket.socket, nick: Optional[str], reason: str) -> None:
        with self._lock:
            uid = next((uid for uid, u in self._users.items()
                        if u["sock"] is sock), None)
            if uid is None:
                # never fully joined (no nick) — just close the socket
                try:
                    sock.close()
                except OSError:
                    pass
                return
            user = self._users.pop(uid)
        self._close_user_handles(user)
        if not self._running.is_set():
            return  # stopping: the closing broadcast already said goodbye
        part_rec = {"t": "part", "nick": user["nick"], "reason": reason,
                    "ts": time.time(), "users": self._snapshot_users()}
        self._remember(part_rec)
        self._fanout(part_rec)
        self._on_event("part", nick=user["nick"], reason=reason, ts=part_rec["ts"])

    def _snapshot_users(self) -> List[Dict[str, Any]]:
        # caller holds _lock (or is single-threaded startup)
        return [{"id": u["id"], "nick": u["nick"]}
                for u in self._users.values()]

    def _remember(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            self._history.append(rec)

    def _fanout(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            users = list(self._users.values())
        for user in users:
            if user["sock"] is None:
                continue
            _send_obj(user["sock"], user["send_lock"], rec)


# ---------------------------------------------------------------------------
# client (member)
# ---------------------------------------------------------------------------

class RoomClient:
    """One connection to the hosting peer."""

    def __init__(self, codec: RoomCodec, *,
                 on_event: Callable[..., None]) -> None:
        self._codec = codec
        self._on_event = on_event
        self._sock: Optional[socket.socket] = None
        self._file = None  # makefile reader — must be closed WITH the socket
        self._send_lock = threading.Lock()
        self._connected = threading.Event()
        self._closed = threading.Event()
        self._ping_thread: Optional[threading.Thread] = None
        # Rotation state: the immediately-previous key still decodes records
        # for a short grace window after a rekey lands.
        self._prev_codec: Optional[RoomCodec] = None
        self._prev_until = 0.0
        self.nick = ""

    def connect(self, endpoints: List[str], nick: str) -> bool:
        nick = clean_nick(nick)
        if not valid_nick(nick):
            self._on_event("error", detail="Invalid nickname.")
            return False
        candidates: List[Tuple[str, int]] = []
        for endpoint in endpoints[:4]:
            host, port, error = _parse_endpoint(endpoint)
            if not error and host:
                candidates.append((host, port))
        sock = self._parallel_connect(candidates) if candidates else None
        if sock is None:
            self._on_event("error", detail="Could not reach the room host.")
            return False
        sock.settimeout(READ_TIMEOUT)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._sock = sock
        self.nick = nick
        return self._handshake(sock, nick)

    @staticmethod
    def _parallel_connect(candidates: List[Tuple[str, int]]) -> Optional[socket.socket]:
        """Race every advertised endpoint; the first TCP connect wins.

        A host advertises its public + LAN address: LAN joiners must not sit
        through the public endpoint's timeout (and vice versa)."""
        if len(candidates) == 1:
            host, port = candidates[0]
            try:
                return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
            except OSError:
                return None
        winner: Dict[str, socket.socket] = {}
        lock = threading.Lock()

        def attempt(host: str, port: int) -> None:
            try:
                sock = socket.create_connection((host, port),
                                                timeout=CONNECT_TIMEOUT)
            except OSError:
                return
            with lock:
                if "sock" in winner:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    return
                winner["sock"] = sock

        threads = [threading.Thread(target=attempt, args=ep, daemon=True)
                   for ep in candidates]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(CONNECT_TIMEOUT + 2)
        return winner.get("sock")

    def _handshake(self, sock: socket.socket, nick: str) -> bool:
        ts = int(time.time())
        join = {"t": "join", "v": 1, "nick": nick, "ts": ts,
                "proof": self._codec.proof(f"{nick}|{ts}")}
        if not _send_obj(sock, self._send_lock, join):
            self._close_socket()
            return False
        sock.settimeout(WELCOME_TIMEOUT)
        try:
            file = sock.makefile("r", encoding="utf-8", errors="replace")
        except OSError:
            self._close_socket()
            return False
        # Own the file immediately: every failure below then releases both
        # handles via _close_socket (no orphaned fd on a failed handshake).
        self._file = file
        try:
            line = file.readline()
        except OSError:
            self._close_socket()
            return False
        if not line:
            self._close_socket()
            return False
        try:
            rec = json.loads(line)
        except ValueError:
            self._close_socket()
            return False
        if not isinstance(rec, dict) or rec.get("t") != "welcome":
            code = rec.get("code", "") if isinstance(rec, dict) else ""
            self._on_event("error", detail=_join_error(code))
            self._close_socket()
            return False
        sock.settimeout(READ_TIMEOUT)
        self._connected.set()
        self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True,
                                             name="room-ping")
        self._ping_thread.start()
        threading.Thread(target=self._read_loop, args=(sock, file, nick),
                         daemon=True, name="room-reader").start()
        self._on_event("connected", nick=nick, users=rec.get("users", []),
                       topic=str(rec.get("topic", "")),
                       history=rec.get("history", []))
        return True

    def _ping_loop(self) -> None:
        while self._connected.is_set() and not self._closed.is_set():
            if self._sock is None:
                return
            if not _send_obj(self._sock, self._send_lock, {"t": "ping"}):
                return
            time.sleep(PING_INTERVAL)

    def _read_loop(self, sock: socket.socket, file, nick: str) -> None:
        reason = "disconnected"
        try:
            while self._connected.is_set() and not self._closed.is_set():
                line = _recv_line(file)
                if line is None:
                    break
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    break
                kind = rec.get("t")
                if kind == "msg":
                    payload = self._open_host_record("msg", rec.get("e"))
                    if payload is None:
                        continue
                    text = clean_text(str(payload.get("text", "")))
                    if text:
                        self._on_event("message", nick=str(rec.get("nick", "")),
                                       text=text, action=bool(payload.get("action")),
                                       ts=float(rec.get("ts", time.time())),
                                       own=bool(str(rec.get("nick", "")) == nick))
                elif kind == "rekey":
                    payload = self._codec.open("h2c", "rekey", rec.get("e"))
                    secret = str(payload.get("secret", "")) if payload else ""
                    if not valid_room_secret(secret):
                        continue
                    self._prev_codec = self._codec
                    self._prev_until = time.monotonic() + REKEY_GRACE_SECONDS
                    self._codec = RoomCodec(secret)
                    self._on_event("rotated", secret=secret)
                elif kind == "join":
                    self._on_event("join", nick=str(rec.get("nick", "")),
                                   users=rec.get("users", []),
                                   ts=float(rec.get("ts", time.time())),
                                   own=bool(rec.get("nick") == nick))
                elif kind == "part":
                    self._on_event("part", nick=str(rec.get("nick", "")),
                                   reason=str(rec.get("reason", "")),
                                   users=rec.get("users", []),
                                   ts=float(rec.get("ts", time.time())),
                                   own=bool(rec.get("nick") == nick))
                elif kind == "err":
                    reason = _join_error(str(rec.get("code", "")))
                    self._on_event("error", detail=reason)
                elif kind == "closing":
                    reason = str(rec.get("reason", "host left"))
        except Exception:
            logger.debug("room client reader failed", exc_info=True)
        finally:
            self._connected.clear()
            self._close_socket()
            if not self._closed.is_set():
                self._on_event("disconnected", reason=reason)

    def connected(self) -> bool:
        return self._connected.is_set() and not self._closed.is_set()

    def send_message(self, text: str, action: bool = False) -> bool:
        text = clean_text(text)
        if not text or not self._connected.is_set() or self._sock is None:
            return False
        return _send_obj(self._sock, self._send_lock,
                         {"t": "msg", "e": self._codec.seal(
                             "c2h", "msg", {"text": text, "action": action})})

    def rekey_request(self) -> bool:
        """Ask the host to rotate the room key ("make private"). Sealed like
        any message — the ability to seal IS the proof of membership."""
        if not self._connected.is_set() or self._sock is None:
            return False
        return _send_obj(self._sock, self._send_lock,
                         {"t": "rekey_req", "e": self._codec.seal(
                             "c2h", "rekey_req", {"n": secrets.token_hex(8)})})

    def _open_host_record(self, kind: str, value: object) -> Optional[Dict[str, Any]]:
        payload = self._codec.open("h2c", kind, value)
        if payload is not None:
            return payload
        if self._prev_codec is not None and time.monotonic() < self._prev_until:
            return self._prev_codec.open("h2c", kind, value)
        return None

    def close(self, reason: str = "left") -> None:
        self._closed.set()
        self._connected.clear()
        self._close_socket()

    def _close_socket(self) -> None:
        # shutdown() FIRST: it delivers the FIN immediately (sock.close()
        # alone is deferred while the makefile reader still references the
        # socket) AND unblocks a reader parked in readline() — closing the
        # file while its reader holds the buffer lock would otherwise stall
        # until the read timeout.
        file, self._file = self._file, None
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if file is not None:
            try:
                file.close()
            except OSError:
                pass


def _parse_endpoint(endpoint: str) -> Tuple[str, int, Optional[str]]:
    endpoint = endpoint.strip()
    if endpoint.startswith("["):  # [ipv6]:port
        host, _, rest = endpoint[1:].partition("]")
        port = rest.lstrip(":")
        try:
            return host, int(port or DEFAULT_LISTEN_PORT), None
        except ValueError:
            return "", 0, "bad port"
    host, _, port = endpoint.rpartition(":")
    if not host:
        host, port = port, str(DEFAULT_LISTEN_PORT)
    try:
        return host.strip(), int(port), None
    except ValueError:
        return "", 0, "bad port"


def _join_error(code: str) -> str:
    return {
        "nick-in-use": "That nickname is already taken in the room.",
        "full": "The room is full.",
        "bad-proof": "The room rejected this build (key mismatch).",
        "bad-nick": "Invalid nickname (1-24 chars, no spaces or commas).",
        "rate": "Sending too fast — connection closed.",
    }.get(code, code or "connection error")


# ---------------------------------------------------------------------------
# best-effort UPnP port mapping (home routers) — enables cross-internet joins
# ---------------------------------------------------------------------------

_UPNP_RESULT: Optional[Tuple[str, int]] = None
_UPNP_TRIED = False
_UPNP_LOCK = threading.Lock()


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _upnp_map_port(internal_port: int) -> Optional[Tuple[str, int]]:
    """Map external→internal TCP port via UPnP IGD. Best-effort, cached."""
    global _UPNP_RESULT, _UPNP_TRIED
    with _UPNP_LOCK:
        if _UPNP_TRIED:
            return _UPNP_RESULT
        _UPNP_TRIED = True
        try:
            _UPNP_RESULT = _upnp_map_port_inner(internal_port)
        except Exception:
            _UPNP_RESULT = None
        return _UPNP_RESULT


def _upnp_map_port_inner(internal_port: int) -> Optional[Tuple[str, int]]:
    # 1) SSDP discover an Internet Gateway Device
    ssdp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ssdp.settimeout(3.0)
    location = None
    try:
        ssdp.sendto(
            ("\r\n".join([
                "M-SEARCH * HTTP/1.1",
                "HOST: 239.255.255.250:1900",
                'MAN: "ssdp:discover"',
                "MX: 2",
                "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1",
                "", ""])).encode("ascii"),
            ("239.255.255.250", 1900))
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                data, _ = ssdp.recvfrom(4096)
            except socket.timeout:
                break
            text = data.decode("latin-1", "replace")
            for line in text.splitlines():
                if line.lower().startswith("location:"):
                    location = line.split(":", 1)[1].strip()
                    break
            if location:
                break
    finally:
        ssdp.close()
    if not location:
        return None

    # 2) fetch the device description, find a WAN connection service
    desc = requests.get(location, timeout=4)
    if desc.status_code != 200:
        return None
    xml = desc.text
    desc.close()
    control_url = service_type = None
    for chunk in xml.split("<service>"):
        if "</service>" not in chunk:
            continue
        if "WANIPConnection" not in chunk and "WANPPPConnection" not in chunk:
            continue
        match = re.search(r"<controlURL>([^<]+)</controlURL>", chunk)
        if match:
            control_url = urllib.parse.urljoin(location, match.group(1).strip())
            stype = re.search(r"<serviceType>([^<]+)</serviceType>", chunk)
            service_type = stype.group(1).strip() if stype else \
                "urn:schemas-upnp-org:service:WANIPConnection:1"
            break
    if not control_url:
        return None

    def soap(action: str, body: str) -> str:
        envelope = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f"<s:Body>{body}</s:Body></s:Envelope>")
        r = requests.post(control_url, data=envelope.encode("utf-8"), timeout=5,
                          headers={
                              "Content-Type": "text/xml; charset=utf-8",
                              "SOAPAction": f'"{service_type}#{action}"',
                          })
        text = r.text
        r.close()
        return text

    lan_ip = _lan_ip()
    soap("AddPortMapping",
         f'<u:AddPortMapping xmlns:u="{service_type}">'
         "<NewRemoteHost></NewRemoteHost>"
         f"<NewExternalPort>{internal_port}</NewExternalPort>"
         f"<NewInternalPort>{internal_port}</NewInternalPort>"
         "<NewProtocol>TCP</NewProtocol>"
         f"<NewInternalClient>{lan_ip}</NewInternalClient>"
         "<NewEnabled>1</NewEnabled>"
         "<NewPortMappingDescription>DeepFlux Room</NewPortMappingDescription>"
         "<NewLeaseDuration>3600</NewLeaseDuration>"
         "</u:AddPortMapping>")
    ip_xml = soap("GetExternalIPAddress",
                  f'<u:GetExternalIPAddress xmlns:u="{service_type}"/>')
    match = re.search(r"<NewExternalIPAddress>([^<]+)</NewExternalIPAddress>", ip_xml)
    if not match:
        return None
    return match.group(1).strip(), internal_port


# ---------------------------------------------------------------------------
# controller
# ---------------------------------------------------------------------------

class RoomController:
    """Owns the room lifecycle and mirrors it into the shared IRCState.

    Role flow: left → connecting → (member | host) → left. A member whose
    connection dies re-runs discovery and, when nobody else hosts, promotes
    itself to host — the room survives its host leaving.
    """

    def __init__(self, chat_config, state: IRCState, *, secret: str = "",
                 rendezvous: Optional[Rendezvous] = None,
                 poll_interval: float = MAINTENANCE_POLL,
                 reconnect_delays: Tuple[float, ...] = RECONNECT_DELAYS) -> None:
        self._cfg = chat_config
        self._state = state
        self._codec = RoomCodec(secret)
        self._rendezvous = rendezvous or Rendezvous()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._lock = threading.RLock()
        self._role = "left"
        self._nick = ""
        self._manual_host = ""
        self._host: Optional[RoomHost] = None
        self._client: Optional[RoomClient] = None
        self._token = secrets.token_hex(16)
        self._endpoints: List[str] = []
        self._maintenance: Optional[threading.Thread] = None
        self._recover = threading.Event()
        self._poll_interval = poll_interval
        self._reconnect_delays = reconnect_delays
        self.encrypted = self._codec.encrypted
        state.ensure_network(ROOM_NET_ID, host="DeepFlux Room", port=0, tls=False)
        state.set_topic(ROOM_NET_ID, ROOM_CHANNEL, _topic_text(self.encrypted))

    # -- public API (thread-safe; join/leave async) ---------------------------

    @property
    def role(self) -> str:
        with self._lock:
            return self._role

    @property
    def nick(self) -> str:
        with self._lock:
            return self._nick

    def is_joined(self) -> bool:
        with self._lock:
            return self._role in ("member", "host")

    def endpoints(self) -> List[str]:
        with self._lock:
            return list(self._endpoints)

    def add_listener(self, cb: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(cb)

    def join(self, nick: str, manual_host: str = "") -> bool:
        nick = clean_nick(nick)
        if not valid_nick(nick):
            self._note("Invalid nickname (1-24 chars, no spaces or commas).")
            return False
        with self._lock:
            if self._role != "left":
                return False
            self._role = "connecting"
            self._manual_host = manual_host.strip()
        self._set_connecting_state()
        threading.Thread(target=self._join_worker, args=(nick,),
                         daemon=True, name="room-join").start()
        return True

    def leave(self) -> None:
        self._stop("You left the room.")

    def shutdown(self) -> None:
        """App close: leave silently, best-effort withdraw."""
        try:
            self._stop(None)
        except Exception:
            logger.debug("room shutdown failed", exc_info=True)

    def send_message(self, text: str, action: bool = False) -> bool:
        with self._lock:
            role, host, client = self._role, self._host, self._client
        if role == "host" and host is not None:
            return host.say(text, action)
        if role == "member" and client is not None:
            return client.send_message(text, action)
        return False

    def make_private(self) -> bool:
        """Rotate the room key ("make private"): everyone currently connected
        moves to a freshly generated key; people joining later — holding only
        the previous key — cannot read or even discover the rotated room."""
        with self._lock:
            role, host, client = self._role, self._host, self._client
        if role == "host" and host is not None:
            return host.rotate()
        if role == "member" and client is not None:
            return client.rekey_request()
        return False

    # -- join orchestration -----------------------------------------------------

    def _join_worker(self, nick: str) -> None:
        try:
            if not self._attempt(nick):
                self._set_role("left", detail="Could not join the room.")
        except Exception:
            logger.exception("room join failed")
            self._set_role("left", detail="Room error — try again.")

    def _attempt(self, nick: str) -> bool:
        with self._lock:
            manual = self._manual_host
        if manual:
            if self._connect_member(nick, [manual]):
                return True
            self._note(f"Could not reach the room at {manual}.")
            return False
        raw = self._rendezvous.lookup(self._codec.room_id)
        pointer = self._codec.open_pointer(raw) if isinstance(raw, str) else None
        if pointer_still_fresh(pointer):
            if self._connect_member(nick, list(pointer.get("endpoints", []))):
                return True
            self._note("Registered host was unreachable — hosting the room here instead.")
        return self._become_host(nick)

    def _connect_member(self, nick: str, endpoints: List[str]) -> bool:
        client = RoomClient(self._codec, on_event=self._on_client_event)
        if not client.connect(endpoints, nick):
            return False
        with self._lock:
            self._client = client
            self._host = None
            self._role = "member"
            self._nick = nick
        self._start_maintenance()
        return True

    def _become_host(self, nick: str) -> bool:
        want_port = int(getattr(self._cfg, "listen_port", DEFAULT_LISTEN_PORT) or 0)
        host = RoomHost(self._codec, port=want_port, on_event=self._on_host_event)
        if not host.start(nick):
            self._note("Could not open a local port for the room.")
            return False

        lan_ip = _lan_ip()
        endpoints: List[str] = []
        mapping = _upnp_map_port(host.port)
        public_ip = self._rendezvous.my_ip()
        if mapping and public_ip:
            endpoints.append(f"{mapping[0]}:{mapping[1]}")
        elif public_ip:
            endpoints.append(f"{public_ip}:{host.port}")  # manual-forward case
        endpoints.append(f"{lan_ip}:{host.port}")

        pointer = {"endpoints": endpoints[:3], "ts": int(time.time())}
        result, existing = self._rendezvous.claim(
            self._codec.room_id, self._token, self._codec.seal_pointer(pointer))
        if result == "taken":
            existing_pointer = (existing if isinstance(existing, dict)
                                else self._codec.open_pointer(existing))
            host.stop("Another host is already registered")
            if (isinstance(existing_pointer, dict)
                    and self._connect_member(nick, list(existing_pointer.get("endpoints", [])))):
                return True
            self._note("The room was taken by another host we could not reach.")
            return False
        if result == "unavailable":
            self._note("Room discovery is unreachable — sharing your direct "
                       "address only.")

        with self._lock:
            self._host = host
            self._client = None
            self._role = "host"
            self._nick = nick
            self._endpoints = endpoints
        self._on_own_join(nick, hosting=True)
        self._note("You are hosting the room."
                   + (f" Others can join via {endpoints[0]}." if endpoints else ""))
        self._start_maintenance()
        return True

    # -- maintenance: host refresh + member recovery ---------------------------

    def _start_maintenance(self) -> None:
        with self._lock:
            if self._maintenance is not None and self._maintenance.is_alive():
                return
            self._maintenance = threading.Thread(
                target=self._maintain_loop, daemon=True, name="room-maintain")
            self._maintenance.start()

    def _maintain_loop(self) -> None:
        attempts = 0
        last_refresh = time.monotonic()
        while True:
            self._recover.wait(self._poll_interval)
            self._recover.clear()
            with self._lock:
                role = self._role
                nick = self._nick
                host = self._host
            if role == "left":
                return

            if role == "host":
                if host is None or not host.running():
                    self._note("The local room server stopped — recovering…")
                    self._recover_role(nick)
                    continue
                if time.monotonic() - last_refresh < REFRESH_INTERVAL:
                    continue
                last_refresh = time.monotonic()
                pointer = {"endpoints": self.endpoints(), "ts": int(time.time())}
                if self._rendezvous.refresh(
                        self._codec.room_id, self._token,
                        self._codec.seal_pointer(pointer)) == "taken":
                    # demoted: another host claimed the slot — rejoin as member
                    host.stop("Room moved to another host")
                    with self._lock:
                        if self._host is host:
                            self._host = None
                    self._note("The room moved to another host — reconnecting.")
                    self._recover_role(nick)
                continue

            # member (or mid-recovery 'connecting'): watch the connection
            client = self._client
            if role == "member" and client is not None and client.connected():
                attempts = 0
                continue
            if attempts >= len(self._reconnect_delays):
                self._note("Lost the room — connection kept failing. "
                           "Press Join to retry.")
                self._stop(None)
                return
            time.sleep(self._reconnect_delays[attempts])
            attempts += 1
            self._recover_role(nick)

    def _recover_role(self, nick: str) -> None:
        """Switch to connecting and re-run the join attempt once. A failure
        leaves the role at 'connecting' so the maintenance loop retries with
        backoff (and eventually gives up) instead of dead-locking."""
        with self._lock:
            if self._role == "left":
                return
            self._role = "connecting"
        self._set_connecting_state()
        self._attempt(nick)

    # -- teardown ---------------------------------------------------------------

    def _stop(self, note: Optional[str]) -> None:
        with self._lock:
            role = self._role
            host, self._host = self._host, None
            client, self._client = self._client, None
            self._role = "left"
            self._endpoints = []
        if host is not None:
            if note is not None:
                host.stop(note)
            else:
                host.stop("The host left the room")
            self._rendezvous.withdraw(self._codec.room_id, self._token)
        if client is not None:
            client.close("left")
        if role in ("member", "host"):
            self._state.set_connected(ROOM_NET_ID, False)
            self._emit({"type": "state", "network": ROOM_NET_ID,
                        "state": "disconnected"})
        if note:
            self._note(note)

    # -- event plumbing -----------------------------------------------------------

    def _on_host_event(self, kind: str, **kw: Any) -> None:
        if kind == "message":
            self._record_message(kw["nick"], kw["text"], kw.get("action", False),
                                 kw.get("ts", time.time()),
                                 own=bool(kw.get("own")))
        elif kind == "join":
            self._record_membership("join", kw["nick"], ts=kw.get("ts", time.time()))
        elif kind == "part":
            self._record_membership("part", kw["nick"], ts=kw.get("ts", time.time()))
        elif kind == "rotated":
            self._on_rotated(kw.get("secret", ""), hosting=True)
        elif kind == "stopped":
            with self._lock:
                if self._host is None:  # expected teardown
                    return
            self._recover.set()  # unexpected host death → maintenance recovers

    def _on_client_event(self, kind: str, **kw: Any) -> None:
        if kind == "connected":
            with self._lock:
                self._nick = kw["nick"]
            # Replay history FIRST: the welcome note and our own join line must
            # land after (newer than) everything the host replays.
            for rec in kw.get("history", []):
                if isinstance(rec, dict):
                    self._replay_history_record(rec)
            users = kw.get("users", [])
            for entry in users:
                nick = entry.get("nick", "") if isinstance(entry, dict) else ""
                if nick and nick != kw["nick"]:
                    self._state.add_nick(ROOM_NET_ID, ROOM_CHANNEL, str(nick))
            self._state.set_connected(ROOM_NET_ID, True, nick=kw["nick"])
            self._emit({"type": "state", "network": ROOM_NET_ID,
                        "state": "connected", "nick": kw["nick"]})
            self._on_own_join(kw["nick"], hosting=False)
            self._note(f"Joined the DeepFlux Room as {kw['nick']}.")
            self._emit_names()
        elif kind == "message":
            self._record_message(kw["nick"], kw["text"], kw.get("action", False),
                                 kw.get("ts", time.time()), own=bool(kw.get("own")))
        elif kind == "join":
            if kw.get("own"):
                return  # recorded by the 'connected' handler above
            self._record_membership("join", kw["nick"], ts=kw.get("ts", time.time()))
        elif kind == "part":
            if kw.get("own"):
                return  # our own part is driven by leave()/stop()
            self._record_membership("part", kw["nick"], ts=kw.get("ts", time.time()))
        elif kind == "disconnected":
            with self._lock:
                if self._client is None:
                    return  # deliberate close
            self._note(f"Connection to the room lost ({kw.get('reason', '')}) — "
                       "reconnecting…")
            self._recover.set()
        elif kind == "rotated":
            self._on_rotated(kw.get("secret", ""), hosting=False)
        elif kind == "error":
            self._note(str(kw.get("detail", "room error")))

    def _on_rotated(self, secret: str, hosting: bool) -> None:
        if not valid_room_secret(secret):
            return
        with self._lock:
            old_room_id = self._codec.room_id
            self._codec = RoomCodec(secret)
            self.encrypted = True
        topic = _topic_text(True) + " · private room (rotated key)"
        self._state.set_topic(ROOM_NET_ID, ROOM_CHANNEL, topic)
        self._emit({"type": "topic", "network": ROOM_NET_ID,
                    "channel": ROOM_CHANNEL, "topic": topic})
        self._note("The room is now private: everyone here moved onto a newly "
                   "generated key. People who join later will not see this room.")
        if hosting:
            # Re-announce discovery under the new key-derived id and drop the
            # old slot, so key-less newcomers start a fresh lounge instead of
            # finding us. Off-thread: this runs on a member's reader thread.
            threading.Thread(target=self._reannounce, args=(old_room_id,),
                             daemon=True, name="room-reannounce").start()

    def _reannounce(self, old_room_id: str) -> None:
        try:
            self._rendezvous.withdraw(old_room_id, self._token)
            pointer = {"endpoints": self.endpoints(), "ts": int(time.time())}
            self._rendezvous.claim(self._codec.room_id, self._token,
                                   self._codec.seal_pointer(pointer))
        except Exception:
            logger.debug("room re-announce failed", exc_info=True)

    def _replay_history_record(self, rec: Dict[str, Any]) -> None:
        kind = rec.get("t")
        if kind == "msg":
            payload = self._codec.open("h2c", "msg", rec.get("e"))
            if payload is None:
                return
            text = clean_text(str(payload.get("text", "")))
            if text:
                self._record_message(str(rec.get("nick", "")), text,
                                     bool(payload.get("action")),
                                     float(rec.get("ts", time.time())), own=False,
                                     replay=True)
        elif kind in ("join", "part"):
            self._state.record(ROOM_NET_ID, ChatMessage(
                ts=float(rec.get("ts", time.time())),
                kind=KIND_JOIN if kind == "join" else KIND_PART,
                nick=str(rec.get("nick", "")),
                text=f"{rec.get('nick', '')} {'joined' if kind == 'join' else 'left'}",
            ), ROOM_CHANNEL)

    # -- state mirroring ------------------------------------------------------

    def _on_own_join(self, nick: str, hosting: bool) -> None:
        self._state.ensure_channel(ROOM_NET_ID, ROOM_CHANNEL)
        # add, not set: for members the welcome's user list was just merged
        self._state.add_nick(ROOM_NET_ID, ROOM_CHANNEL, nick)
        self._record_membership("join", nick, own=True, quiet=True)
        self._emit_names()

    def _record_membership(self, kind: str, nick: str, ts: float = 0.0,
                           own: bool = False, quiet: bool = False) -> None:
        ts = ts or time.time()
        if kind == "join":
            self._state.add_nick(ROOM_NET_ID, ROOM_CHANNEL, nick)
            msg = ChatMessage(ts=ts, kind=KIND_JOIN, nick=nick, text=f"{nick} joined")
            event = {"type": "join", "network": ROOM_NET_ID, "channel": ROOM_CHANNEL,
                     "nick": nick, "own": own, "ts": ts}
        else:
            self._state.remove_nick(ROOM_NET_ID, nick, ROOM_CHANNEL)
            msg = ChatMessage(ts=ts, kind=KIND_PART, nick=nick, text=f"{nick} left")
            event = {"type": "part", "network": ROOM_NET_ID, "channel": ROOM_CHANNEL,
                     "nick": nick, "own": own, "ts": ts}
        self._state.record(ROOM_NET_ID, msg, ROOM_CHANNEL)
        if not quiet:
            self._emit(event)
            self._emit_names()

    def _record_message(self, nick: str, text: str, action: bool, ts: float,
                        own: bool, replay: bool = False) -> None:
        msg = ChatMessage(ts=ts, kind=KIND_ACTION if action else KIND_MESSAGE,
                          nick=nick, text=text)
        self._state.record(ROOM_NET_ID, msg, ROOM_CHANNEL)
        if replay:
            return
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
        self._emit({"type": "state", "network": ROOM_NET_ID, "state": "connecting"})

    def _set_role(self, role: str, detail: str = "") -> None:
        with self._lock:
            self._role = role
        if role == "left":
            self._state.set_connected(ROOM_NET_ID, False)
            self._emit({"type": "state", "network": ROOM_NET_ID,
                        "state": "disconnected",
                        **({"detail": detail} if detail else {})})

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
