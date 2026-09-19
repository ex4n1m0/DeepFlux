"""Tests for the in-app auto-update engine (infra/updater.py) and its
dialogs (gui/update_dialog.py). All network and process spawning is mocked —
these tests never touch deepflux.space and never run PowerShell."""
from __future__ import annotations

import hashlib
import json
import os
import time
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from config import APP_VERSION, DeeptorrentConfig
from infra import updater
from infra.updater import UpdateInfo

# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

def _feed_bytes(**overrides) -> bytes:
    payload = {
        "version": "9.9",
        "file": "DeepFlux9.9Setup.exe",
        "size": 1234,
        "sha256": "a" * 64,
        "released": "2026-09-19",
        "notes": "test build",
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def test_parse_feed_valid():
    info = updater.parse_feed(_feed_bytes())
    assert info is not None
    assert (info.version, info.file, info.size) == ("9.9", "DeepFlux9.9Setup.exe", 1234)
    assert info.url == "https://deepflux.space/deepflux/DeepFlux9.9Setup.exe"


@pytest.mark.parametrize("body", [
    b"", b"not json", b"[]", b"null",
    _feed_bytes(version="abc"), _feed_bytes(version=""),
    _feed_bytes(file="../evil.exe"), _feed_bytes(file="a/b.exe"),
    _feed_bytes(file="a\\b.exe"), _feed_bytes(file=""),
    _feed_bytes(sha256="XYZ"), _feed_bytes(sha256="a" * 63),
    _feed_bytes(size=0), _feed_bytes(size=-5),
    _feed_bytes(size=3 * 1024 * 1024 * 1024),
])
def test_parse_feed_rejects_malformed(body):
    assert updater.parse_feed(body) is None


def test_parse_feed_rejects_oversized_body():
    assert updater.parse_feed(b"{" + b" " * (updater.FEED_BODY_LIMIT + 1)) is None


# ---------------------------------------------------------------------------
# Version comparison + check gating
# ---------------------------------------------------------------------------

def test_is_newer_multi_digit():
    assert updater.is_newer("4.9", "4.8") is True
    assert updater.is_newer("4.10", "4.9") is True   # not string compare
    assert updater.is_newer("4.8", "4.8") is False
    assert updater.is_newer("4.7", "4.8") is False


def _supported(monkeypatch, value=True):
    monkeypatch.setattr(updater, "updates_supported", lambda: value)


def test_maybe_check_unsupported_build(monkeypatch):
    _supported(monkeypatch, False)
    assert updater.maybe_check_update(DeeptorrentConfig(), force=True) is None


def test_maybe_check_disabled(monkeypatch):
    _supported(monkeypatch)
    config = DeeptorrentConfig()
    config.updater.check_enabled = False
    assert updater.maybe_check_update(config, force=True) is None


def test_maybe_check_daily_gate(monkeypatch):
    _supported(monkeypatch)
    config = DeeptorrentConfig()
    config.updater.last_check = time.time()
    assert updater.maybe_check_update(config) is None


def test_maybe_check_returns_newer_and_persists(monkeypatch, tmp_path):
    _supported(monkeypatch)
    config = DeeptorrentConfig()
    path = str(tmp_path / "config.json")
    info = UpdateInfo(version="999.0", file="x.exe", size=10, sha256="b" * 64)
    monkeypatch.setattr(updater, "fetch_feed", lambda timeout=10: info)
    result = updater.maybe_check_update(config, path)
    assert result is info
    assert config.updater.last_check > 0
    assert "updater" in json.load(open(path, encoding="utf-8"))


def test_maybe_check_skipped_version(monkeypatch):
    _supported(monkeypatch)
    config = DeeptorrentConfig()
    config.updater.skip_version = "999.0"
    info = UpdateInfo(version="999.0", file="x.exe", size=10, sha256="b" * 64)
    monkeypatch.setattr(updater, "fetch_feed", lambda timeout=10: info)
    # The automatic check respects "Skip this version"…
    assert updater.maybe_check_update(config) is None
    # …but Help → Check for Updates (force) re-offers it — the user asked.
    assert updater.maybe_check_update(config, force=True) is info


def test_maybe_check_not_newer(monkeypatch):
    _supported(monkeypatch)
    config = DeeptorrentConfig()
    info = UpdateInfo(version=APP_VERSION, file="x.exe", size=10, sha256="b" * 64)
    monkeypatch.setattr(updater, "fetch_feed", lambda timeout=10: info)
    assert updater.maybe_check_update(config, force=True) is None


def test_fetch_feed_streams_and_parses(monkeypatch):
    """Regression (found live in the 4.9 E2E): curl_cffi's iter_content
    ASSERTS unless the request was made with stream=True — a mocked
    response hides it, so pin the flag explicitly."""
    import dlmgr.http_client as http_client
    seen = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def iter_content(self, n):
            seen["chunk"] = n
            return iter([_feed_bytes()])

    def fake_get(url, headers=None, timeout=15, stream=False):
        seen["stream"] = stream
        return _Resp()

    monkeypatch.setattr(http_client, "get", fake_get)
    info = updater.fetch_feed()
    assert seen["stream"] is True
    assert info is not None and info.version == "9.9"


def test_maybe_check_network_failure_is_silent(monkeypatch):
    _supported(monkeypatch)
    def boom(timeout=10):
        raise OSError("down")
    monkeypatch.setattr(updater, "fetch_feed", boom)
    config = DeeptorrentConfig()
    assert updater.maybe_check_update(config, force=True) is None
    assert config.updater.last_check == 0.0  # gate only persists on success


# ---------------------------------------------------------------------------
# Download (mocked http_client.get)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, chunks, status_code=200):
        self._chunks = [c for c in chunks if c]
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        return iter(self._chunks)


def _info_for(data: bytes) -> UpdateInfo:
    return UpdateInfo(version="9.9", file="DeepFlux9.9Setup.exe",
                      size=len(data), sha256=hashlib.sha256(data).hexdigest())


@pytest.fixture
def stage_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "updates_dir", lambda: tmp_path / "updates")
    return tmp_path / "updates"


def test_download_happy_path(monkeypatch, stage_dir):
    data = os.urandom(5000)
    info = _info_for(data)
    seen = {}

    def fake_get(url, headers=None, timeout=15, stream=False):
        seen["range"] = headers.get("Range") if headers else None
        return _FakeResponse([data[:2048], data[2048:]])

    import dlmgr.http_client as http_client
    monkeypatch.setattr(http_client, "get", fake_get)
    path = updater.download_update(info)
    assert path.read_bytes() == data
    assert seen["range"] is None
    assert not (stage_dir / (info.file + ".part")).exists()


def test_download_resumes_from_part(monkeypatch, stage_dir):
    data = os.urandom(5000)
    info = _info_for(data)
    stage_dir.mkdir(parents=True)
    part = stage_dir / (info.file + ".part")
    part.write_bytes(data[:1000])  # earlier interrupted attempt
    ranges = {}

    def fake_get(url, headers=None, timeout=15, stream=False):
        ranges["value"] = headers.get("Range") if headers else None
        assert ranges["value"] == "bytes=1000-"
        return _FakeResponse([data[1000:3000], data[3000:]], status_code=206)

    import dlmgr.http_client as http_client
    monkeypatch.setattr(http_client, "get", fake_get)
    path = updater.download_update(info)
    assert path.read_bytes() == data


def test_download_rejects_hash_mismatch(monkeypatch, stage_dir):
    data = os.urandom(3000)
    # Size matches the feed, the hash does not — corrupted transfer.
    info = UpdateInfo(version="9.9", file="DeepFlux9.9Setup.exe",
                      size=len(data), sha256=hashlib.sha256(b"other").hexdigest())
    import dlmgr.http_client as http_client
    monkeypatch.setattr(http_client, "get",
                        lambda *a, **k: _FakeResponse([data]))
    with pytest.raises(updater.UpdateError):
        updater.download_update(info)
    assert not (stage_dir / info.file).exists()
    assert not (stage_dir / (info.file + ".part")).exists()


def test_download_rejects_oversize_body(monkeypatch, stage_dir):
    data = os.urandom(2048)
    info = _info_for(data[:100])  # feed announces 100 bytes
    import dlmgr.http_client as http_client
    monkeypatch.setattr(http_client, "get",
                        lambda *a, **k: _FakeResponse([data]))
    with pytest.raises(updater.UpdateError):
        updater.download_update(info)


def test_download_cancel(monkeypatch, stage_dir):
    import threading
    data = os.urandom(4096)
    info = _info_for(data)
    cancel = threading.Event()
    cancel.set()
    import dlmgr.http_client as http_client
    monkeypatch.setattr(http_client, "get",
                        lambda *a, **k: _FakeResponse([data]))
    with pytest.raises(updater.UpdateError, match="cancel"):
        updater.download_update(info, cancel=cancel)


def test_download_returns_cached_verified_file(monkeypatch, stage_dir):
    data = os.urandom(512)
    info = _info_for(data)
    stage_dir.mkdir(parents=True)
    (stage_dir / info.file).write_bytes(data)
    import dlmgr.http_client as http_client
    monkeypatch.setattr(http_client, "get",
                        lambda *a, **k: pytest.fail("should not download"))
    path = updater.download_update(info)
    assert path.read_bytes() == data


def test_verify_file(tmp_path):
    data = os.urandom(256)
    info = _info_for(data)
    f = tmp_path / "x.bin"
    f.write_bytes(data)
    assert updater.verify_file(f, info) is True
    f.write_bytes(data + b"x")             # wrong size
    assert updater.verify_file(f, info) is False
    assert updater.verify_file(tmp_path / "missing.bin", info) is False


# ---------------------------------------------------------------------------
# Apply helper
# ---------------------------------------------------------------------------

def test_build_helper_content(tmp_path):
    setup = tmp_path / "DeepFlux9.9Setup.exe"
    setup.write_bytes(b"x")
    log = tmp_path / "logs" / "update.log"
    helper = updater.build_helper(setup, 4711, r"C:\app dir\DeepFlux.exe", log)
    text = helper.read_text(encoding="utf-8")
    assert "$appPid = 4711" in text
    assert r"'C:\app dir\DeepFlux.exe'" in text        # quoted, spaces safe
    assert "/VERYSILENT" in text and "/NORESTART" in text
    assert "Get-Process -Id $appPid" in text            # waits for our exit
    assert "Start-Process -FilePath $appExe" in text    # relaunch
    assert "Remove-Item $setup" in text                 # cleanup


def test_apply_update_spawns_detached_helper(monkeypatch, tmp_path):
    setup = tmp_path / "DeepFlux9.9Setup.exe"
    setup.write_bytes(b"x")
    spawned = {}

    def fake_popen(args, creationflags=0, close_fds=False):
        spawned["args"] = args
        spawned["flags"] = creationflags

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(updater, "updates_dir", lambda: tmp_path)
    updater.apply_update(setup, app_pid=123)
    assert spawned["args"][0] == "powershell.exe"
    assert spawned["args"][1:4] == ["-NoProfile", "-ExecutionPolicy", "Bypass"]


def test_apply_update_uses_spawnable_flags():
    """CREATE_NO_WINDOW | DETACHED_PROCESS is an invalid CreateProcess
    combination — the child dies instantly with no error (found live in
    the 4.9 E2E). The flags must be spawnable as-is."""
    import subprocess
    valid = {subprocess.CREATE_NEW_CONSOLE, subprocess.CREATE_NEW_PROCESS_GROUP,
             subprocess.CREATE_NO_WINDOW, subprocess.DETACHED_PROCESS,
             subprocess.CREATE_BREAKAWAY_FROM_JOB, 0}
    assert updater._CREATE_NO_WINDOW in valid
    # and the exact value used must not carry DETACHED_PROCESS
    assert not (updater._CREATE_NO_WINDOW & getattr(subprocess, "DETACHED_PROCESS", 0))


def test_apply_update_requires_existing_file(tmp_path):
    with pytest.raises(updater.UpdateError):
        updater.apply_update(tmp_path / "missing.exe")


# ---------------------------------------------------------------------------
# Config round-trip
# ---------------------------------------------------------------------------

def test_updater_config_round_trip(tmp_path):
    path = str(tmp_path / "config.json")
    config = DeeptorrentConfig()
    config.updater.check_enabled = False
    config.updater.skip_version = "4.8"
    config.updater.last_check = 123.5
    config.to_file(path)
    loaded = DeeptorrentConfig.from_file(path)
    assert loaded.updater.check_enabled is False
    assert loaded.updater.skip_version == "4.8"
    assert loaded.updater.last_check == 123.5


# ---------------------------------------------------------------------------
# Dialog (offscreen)
# ---------------------------------------------------------------------------

def test_update_dialog_button_results():
    from PySide6.QtWidgets import QApplication, QPushButton
    app = QApplication.instance() or QApplication([])
    from gui.update_dialog import (
        RESULT_LATER,
        RESULT_SKIP,
        RESULT_UPDATE_NOW,
        UpdateDialog,
    )
    info = UpdateInfo(version="9.9", file="x.exe", size=5 * 1024 * 1024,
                      sha256="b" * 64, notes="hello")
    for label, expected in (
        ("Update now", RESULT_UPDATE_NOW),
        ("Remind me later", RESULT_LATER),
        ("Skip this version", RESULT_SKIP),
    ):
        dialog = UpdateDialog(None, info, "4.8")
        buttons = {b.text(): b for b in dialog.findChildren(QPushButton)}
        buttons[label].click()
        assert dialog.result() == expected
        dialog.deleteLater()
