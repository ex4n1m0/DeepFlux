"""Encrypted export/import of the user's settings (File → Export/Import Settings).

The payload is the full serialized DeeptorrentConfig — every user-entered
setting, including API keys and source lists — encrypted with a passphrase so
a stolen ``.dfc`` file is unreadable without it. The file format is a small
JSON envelope (magic + KDF parameters) around a Fernet token
(AES-128-CBC + HMAC-SHA256, authenticated encryption). Wrong passphrases and
tampered files both fail closed with :class:`SettingsBackupError`.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = "deepflux-settings"
VERSION = 1
KDF_ITERATIONS = 390_000  # OWASP floor for PBKDF2-SHA256; ~0.3s per attempt
SALT_BYTES = 16

# Top-level config keys used to sanity-check a decrypted payload.
_KNOWN_SECTIONS = {
    "llm", "web_search", "indexer", "download", "torrents", "iptv",
    "voice", "chat", "sources", "rss", "browser",
}


class SettingsBackupError(ValueError):
    """Wrong passphrase, corrupted file, or not a DeepFlux settings file."""


def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def encrypt_settings(payload: bytes, password: str) -> bytes:
    """Encrypt a serialized config with ``password``; returns the .dfc bytes."""
    if not password:
        raise ValueError("a passphrase is required")
    salt = os.urandom(SALT_BYTES)
    token = Fernet(_derive_key(password, salt, KDF_ITERATIONS)).encrypt(payload)
    envelope = {
        "magic": MAGIC,
        "version": VERSION,
        "kdf": "pbkdf2-sha256",
        "iterations": KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "token": token.decode("ascii"),
    }
    return json.dumps(envelope, indent=2).encode("utf-8")


def decrypt_settings(blob: bytes, password: str) -> bytes:
    """Decrypt .dfc bytes back to the serialized config payload.

    Raises :class:`SettingsBackupError` on wrong passphrase or any
    corruption/tampering — nothing is ever partially decrypted."""
    try:
        env = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsBackupError("not a DeepFlux settings file") from exc
    if not isinstance(env, dict) or env.get("magic") != MAGIC:
        raise SettingsBackupError("not a DeepFlux settings file")
    try:
        salt = base64.b64decode(env["salt"])
        iterations = int(env["iterations"])
        token = env["token"].encode("ascii")
    except (KeyError, TypeError, ValueError) as exc:
        raise SettingsBackupError("corrupted settings file") from exc
    try:
        return Fernet(_derive_key(password, salt, iterations)).decrypt(token)
    except InvalidToken as exc:
        raise SettingsBackupError("wrong passphrase or corrupted file") from exc


def validate_settings(payload: bytes) -> Dict[str, Any]:
    """Parse a decrypted payload and check it looks like a serialized config."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsBackupError("decrypted content is not valid settings") from exc
    if not isinstance(data, dict) or len(_KNOWN_SECTIONS & set(data)) < 2:
        raise SettingsBackupError("decrypted content is not a DeepFlux settings file")
    return data
