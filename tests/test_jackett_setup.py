"""Unit tests for infra.jackett_setup — the installer's Jackett final step.

All network, elevation and filesystem-probe seams are mocked: requests,
ShellExecuteEx (patched at the _shell_execute_runas boundary), and the
ServerConfig.json candidate list. Config writes always go through an
explicit tmp path (never the real ~/.deeptorrent/config.json).
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from infra import jackett, jackett_setup

_INDEXERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<indexers>
  <indexer id="iptorrents" configured="true">
    <title>IPTorrents</title><link>https://iptorrents.com/</link><type>private</type>
  </indexer>
  <indexer id="1337x" configured="true">
    <title>1337x</title><link>https://1337x.to/</link><type>public</type>
  </indexer>
  <indexer id="goodone" configured="false">
    <title>Good One</title><link>https://good.example/</link><type>public</type>
  </indexer>
  <indexer id="deadone" configured="false">
    <title>Dead One</title><link>https://dead.example/</link><type>public</type>
  </indexer>
  <indexer id="privatenew" configured="false">
    <title>Private New</title><link>https://p.example/</link><type>private</type>
  </indexer>
</indexers>
"""


def _server_cfg(api_key="test-key", admin_password=False):
    return {
        "api_key": api_key,
        "port": 9117,
        "base_path": "",
        "admin_password_set": admin_password,
    }


# ----------------------------------------------------------------------
# ServerConfig discovery
# ----------------------------------------------------------------------

def test_read_server_config_from_candidate_path(tmp_path):
    cfg_file = tmp_path / "ServerConfig.json"
    cfg_file.write_text(json.dumps({
        "APIKey": "abc123", "Port": 9118,
        "BasePathOverride": "/jkt", "AdminPassword": "",
    }), encoding="utf-8")
    with patch.object(jackett_setup, "server_config_candidates",
                      return_value=[str(cfg_file)]):
        cfg = jackett_setup.read_jackett_server_config()
    assert cfg == {"api_key": "abc123", "port": 9118,
                   "base_path": "/jkt", "admin_password_set": False}
    assert jackett_setup.base_url_from(cfg) == "http://127.0.0.1:9118/jkt"


def test_read_server_config_admin_password_flag(tmp_path):
    cfg_file = tmp_path / "ServerConfig.json"
    cfg_file.write_text(json.dumps({"APIKey": "k", "AdminPassword": "hash"}),
                        encoding="utf-8")
    with patch.object(jackett_setup, "server_config_candidates",
                      return_value=[str(cfg_file)]):
        assert jackett_setup.read_jackett_server_config()["admin_password_set"] is True


def test_read_server_config_missing_returns_none(tmp_path):
    with patch.object(jackett_setup, "server_config_candidates",
                      return_value=[str(tmp_path / "nope.json")]):
        assert jackett_setup.read_jackett_server_config() is None


def test_find_bundled_installer_env_override(tmp_path):
    fake = tmp_path / "Jackett.Installer.Windows.exe"
    fake.write_bytes(b"x")
    with patch.dict(os.environ, {"DEEPFLUX_JACKETT_INSTALLER": str(fake)}):
        assert jackett_setup.find_bundled_installer() == str(fake)


# ----------------------------------------------------------------------
# Admin session
# ----------------------------------------------------------------------

def _session_mock(final_url):
    session = MagicMock()
    resp = MagicMock()
    resp.url = final_url
    session.get.return_value = resp
    return session


@patch("infra.jackett_setup.requests.Session")
def test_open_admin_session_success_on_dashboard(mock_session_cls):
    session = _session_mock("http://127.0.0.1:9117/UI/Dashboard")
    mock_session_cls.return_value = session
    assert jackett_setup.open_admin_session("http://127.0.0.1:9117") is session
    params = session.get.call_args.kwargs["params"]
    assert params == {"cookiesChecked": "1"}


@patch("infra.jackett_setup.requests.Session")
def test_open_admin_session_none_when_password_set(mock_session_cls):
    session = _session_mock("http://127.0.0.1:9117/UI/Login?cookiesChecked=1")
    mock_session_cls.return_value = session
    assert jackett_setup.open_admin_session("http://127.0.0.1:9117") is None
    session.close.assert_called_once()


@patch("infra.jackett_setup.requests.Session")
def test_open_admin_session_none_on_network_error(mock_session_cls):
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("refused")
    mock_session_cls.return_value = session
    assert jackett_setup.open_admin_session("http://127.0.0.1:9117") is None
    session.close.assert_called_once()


# ----------------------------------------------------------------------
# Indexer fan-out
# ----------------------------------------------------------------------

def _add_session(post_results):
    """A session whose POST outcome is decided per indexer id."""
    session = MagicMock()

    def _get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: [{"id": "sitelink", "value": "https://x/"}]
        return resp

    def _post(url, params=None, json=None, timeout=None):
        indexer_id = url.rstrip("/").split("/")[-2]
        outcome = post_results[indexer_id]
        if isinstance(outcome, Exception):
            raise outcome
        resp = MagicMock()
        resp.status_code = outcome
        return resp

    session.get.side_effect = _get
    session.post.side_effect = _post
    return session


@patch("infra.jackett_setup.open_admin_session")
@patch("infra.jackett_setup.requests.get")
def test_add_public_indexers_counts_and_skips(mock_get, mock_open):
    resp = MagicMock()
    resp.text = _INDEXERS_XML
    resp.raise_for_status = lambda: None
    mock_get.return_value = resp
    session = _add_session({"goodone": 204, "deadone": 500})
    mock_open.return_value = session

    config = jackett_setup.DeeptorrentConfig()
    config.indexer.api_key = "k"
    config.indexer.url = "http://127.0.0.1:9117"
    lines = []
    counts = jackett_setup.add_public_indexers(config, session, on_progress=lines.append)

    # Only the two UNCONFIGURED PUBLIC indexers are attempted — the
    # configured one and the unconfigured private one are left alone.
    assert counts == {"total": 2, "added": 1, "failed": 1, "listed": True}
    posted_ids = {c.args[0].rstrip("/").split("/")[-2]
                  for c in session.post.call_args_list}
    assert posted_ids == {"goodone", "deadone"}
    assert any("2/2" in line for line in lines)


@patch("infra.jackett_setup.requests.get")
def test_add_public_indexers_list_failure(mock_get):
    mock_get.side_effect = requests.ConnectionError("down")
    config = jackett_setup.DeeptorrentConfig()
    counts = jackett_setup.add_public_indexers(config, MagicMock())
    assert counts == {"total": 0, "added": 0, "failed": 0, "listed": False}


# ----------------------------------------------------------------------
# run_setup orchestration
# ----------------------------------------------------------------------

def _patched_setup(tmp_path, *, server_cfg, is_running,
                   runas=None, wait_cfg=None, admin_session=None,
                   sync=None, wait_ready=True, find_installer=None):
    """Bundle the usual seam patches for run_setup tests."""
    config_path = str(tmp_path / "config.json")
    jackett_setup.DeeptorrentConfig().to_file(config_path)  # seed a file
    return config_path, patch.multiple(
        jackett_setup,
        read_jackett_server_config=server_cfg and (lambda: server_cfg) or
            MagicMock(return_value=None),
        find_bundled_installer=MagicMock(return_value=find_installer),
        _shell_execute_runas=runas or MagicMock(return_value=(True, 0)),
        _wait_for_server_config=wait_cfg and (lambda t: wait_cfg) or
            MagicMock(return_value=None),
        open_admin_session=MagicMock(return_value=admin_session),
    ), patch.multiple(
        jackett,
        is_running=is_running if callable(is_running) else MagicMock(return_value=is_running),
        wait_until_ready=MagicMock(return_value=wait_ready),
        sync_sources=sync or MagicMock(return_value=None),
    )


def test_run_setup_links_existing_jackett(tmp_path):
    config_path, setup_patches, jkt_patches = _patched_setup(
        tmp_path,
        server_cfg=_server_cfg("abc"),
        is_running=True,
    )
    with setup_patches, jkt_patches:
        result = jackett_setup.run_setup(config_path=config_path)
    assert result["ok"] is True
    assert result["api_key_linked"] is True
    assert result["installed"] is False
    saved = json.loads(open(config_path, encoding="utf-8").read())
    assert saved["indexer"]["api_key"] == "abc"
    assert saved["indexer"]["url"] == "http://127.0.0.1:9117"


def test_run_setup_admin_password_skips_fanout(tmp_path):
    lines = []
    config_path, setup_patches, jkt_patches = _patched_setup(
        tmp_path,
        server_cfg=_server_cfg("abc", admin_password=True),
        is_running=True,
    )
    with setup_patches, jkt_patches:
        result = jackett_setup.run_setup(config_path=config_path, on_progress=lines.append)
    assert result["admin_password"] is True
    assert result["added"] == 0
    assert any("admin password" in line for line in lines)


def test_run_setup_no_installer_available(tmp_path):
    config_path, setup_patches, jkt_patches = _patched_setup(
        tmp_path, server_cfg=None, is_running=False, find_installer=None)
    with setup_patches, jkt_patches:
        result = jackett_setup.run_setup(config_path=config_path)
    assert result["ok"] is False
    assert result["error"] == "no-installer"
    # Nothing was written — the seeded config is untouched.
    saved = json.loads(open(config_path, encoding="utf-8").read())
    assert saved["indexer"]["api_key"] == ""


def test_run_setup_installs_and_links(tmp_path):
    installer_path = str(tmp_path / "Jackett.Installer.Windows.exe")
    open(installer_path, "wb").write(b"x")
    runas = MagicMock(return_value=(True, 0))
    # is_running: False (initial check) then True (post-install verify);
    # wait_until_ready is mocked whole, so nothing else consumes a state.
    states = iter([False, True])

    config_path, setup_patches, jkt_patches = _patched_setup(
        tmp_path,
        server_cfg=None,
        is_running=lambda cfg, timeout=None: next(states),
        find_installer=installer_path,
        runas=runas,
        wait_cfg=_server_cfg("fresh-key"),
    )
    with setup_patches, jkt_patches:
        result = jackett_setup.run_setup(config_path=config_path)

    assert result["installed"] is True
    assert result["api_key_linked"] is True
    assert result["ok"] is True
    runas.assert_called_once_with(installer_path, jackett_setup.INSTALLER_SILENT_FLAGS)
    saved = json.loads(open(config_path, encoding="utf-8").read())
    assert saved["indexer"]["api_key"] == "fresh-key"


def test_run_setup_uac_declined(tmp_path):
    installer_path = str(tmp_path / "Jackett.Installer.Windows.exe")
    open(installer_path, "wb").write(b"x")
    config_path, setup_patches, jkt_patches = _patched_setup(
        tmp_path,
        server_cfg=None,
        is_running=False,
        find_installer=installer_path,
        runas=MagicMock(return_value=(False, jackett_setup.UAC_CANCELLED)),
    )
    with setup_patches, jkt_patches:
        result = jackett_setup.run_setup(config_path=config_path)
    assert result["ok"] is False
    assert result["error"] == "uac-declined"


def test_run_setup_installer_failure(tmp_path):
    installer_path = str(tmp_path / "Jackett.Installer.Windows.exe")
    open(installer_path, "wb").write(b"x")
    config_path, setup_patches, jkt_patches = _patched_setup(
        tmp_path,
        server_cfg=None,
        is_running=False,
        find_installer=installer_path,
        runas=MagicMock(return_value=(False, 2)),
    )
    with setup_patches, jkt_patches:
        result = jackett_setup.run_setup(config_path=config_path)
    assert result["ok"] is False
    assert result["error"] == "installer-failed:2"


# ----------------------------------------------------------------------
# GUI wrapper (offscreen)
# ----------------------------------------------------------------------

def test_setup_dialog_streams_lines_and_finishes(tmp_path):
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from gui.jackett_setup_dialog import JackettSetupDialog

    app = QApplication.instance() or QApplication([])
    lines_seen = []

    def fake_run_setup(config_path=None, on_progress=None):
        for msg in ("stage one", "stage two"):
            if on_progress:
                on_progress(msg)
            lines_seen.append(msg)
        return {"ok": True, "sources_enabled": 12}

    with patch.object(jackett_setup, "run_setup", fake_run_setup):
        dialog = JackettSetupDialog(config_path=str(tmp_path / "config.json"))
        dialog.show()
        for _ in range(200):  # ≤10s of event-loop pumping
            if dialog.close_btn.isEnabled():
                break
            QTest.qWait(50)
        assert dialog.close_btn.isEnabled()
        assert "stage one" in dialog.log.toPlainText()
        assert "12 torrent sources" in dialog.log.toPlainText()
        dialog.close()
        dialog.deleteLater()
