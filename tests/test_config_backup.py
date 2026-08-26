"""Settings backup encryption (infra/config_backup.py) — roundtrip, wrong
passphrase, tamper detection, and payload validation. Pure unit tests: no
real config path is ever touched.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from config import DeeptorrentConfig, IPTVSourceConfig
from infra.config_backup import (
    MAGIC,
    SettingsBackupError,
    decrypt_settings,
    encrypt_settings,
    validate_settings,
)


def _real_payload() -> bytes:
    """A real serialized config carrying the kinds of secrets we protect."""
    cfg = DeeptorrentConfig()
    cfg.llm.api_key = "sk-secret-test-key"
    cfg.iptv.sources = [
        IPTVSourceConfig(id="s1", name="My IPTV", kind="xtream",
                         url="http://provider", username="u", password="pw"),
    ]
    return json.dumps(dataclasses.asdict(cfg), ensure_ascii=False).encode("utf-8")


def test_roundtrip():
    payload = _real_payload()
    blob = encrypt_settings(payload, "correct horse")
    assert decrypt_settings(blob, "correct horse") == payload


def test_envelope_has_no_plaintext_secrets():
    blob = encrypt_settings(_real_payload(), "pw")
    assert b"sk-secret-test-key" not in blob
    assert b"http://provider" not in blob
    env = json.loads(blob.decode("utf-8"))
    assert env["magic"] == MAGIC
    assert env["kdf"] == "pbkdf2-sha256" and env["iterations"] > 0 and env["salt"]


def test_wrong_password_fails():
    blob = encrypt_settings(_real_payload(), "right")
    with pytest.raises(SettingsBackupError, match="wrong passphrase"):
        decrypt_settings(blob, "wrong")


def test_tampered_token_fails():
    env = json.loads(encrypt_settings(_real_payload(), "pw").decode("utf-8"))
    tok = env["token"]
    env["token"] = tok[:-2] + ("AA" if not tok.endswith("AA") else "BB")
    with pytest.raises(SettingsBackupError):
        decrypt_settings(json.dumps(env).encode("utf-8"), "pw")


def test_garbage_file_fails():
    with pytest.raises(SettingsBackupError, match="not a DeepFlux"):
        decrypt_settings(b"\x00\x01\x02 not json", "pw")
    with pytest.raises(SettingsBackupError, match="not a DeepFlux"):
        decrypt_settings(b'{"magic": "something-else"}', "pw")


def test_empty_password_rejected():
    with pytest.raises(ValueError, match="passphrase"):
        encrypt_settings(_real_payload(), "")


def test_validate_settings_accepts_real_config():
    data = validate_settings(_real_payload())
    assert data["llm"]["api_key"] == "sk-secret-test-key"
    assert data["iptv"]["sources"][0]["name"] == "My IPTV"


def test_validate_settings_rejects_non_config():
    with pytest.raises(SettingsBackupError):
        validate_settings(b"not json at all")
    with pytest.raises(SettingsBackupError):
        validate_settings(json.dumps({"unrelated": True}).encode("utf-8"))
