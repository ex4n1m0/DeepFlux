"""Unit tests for infra.flaresolverr — the installer's FlareSolverr step.

Everything network/process related is mocked; the extraction test builds a
tiny real zip with the same layout the official one has
(flaresolverr/flaresolverr.exe + _internal/...).
"""
from __future__ import annotations

import os
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from config import DeeptorrentConfig
from infra import flaresolverr


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.list_torrents.return_value = []
    return engine


# ----------------------------------------------------------------------
# find_executable / install_root
# ----------------------------------------------------------------------

def test_find_executable_none_on_posix(monkeypatch):
    monkeypatch.setattr(flaresolverr.sys, "platform", "linux")
    assert flaresolverr.find_executable() is None


def test_find_executable_found(monkeypatch, tmp_path):
    exe = tmp_path / "flaresolverr" / "flaresolverr.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(flaresolverr.sys, "platform", "win32")
    monkeypatch.setattr(flaresolverr, "install_root", lambda: str(tmp_path / "flaresolverr"))
    assert flaresolverr.find_executable() == str(exe)


# ----------------------------------------------------------------------
# is_running / start / ensure_running
# ----------------------------------------------------------------------

@patch("infra.flaresolverr.requests.get")
def test_is_running_health(mock_get):
    mock_get.return_value = MagicMock(status_code=200)
    assert flaresolverr.is_running() is True
    assert mock_get.call_args.args[0].endswith("/health")


@patch("infra.flaresolverr.requests.get", side_effect=ConnectionError("down"))
def test_is_running_false_on_error(mock_get):
    assert flaresolverr.is_running() is False


@patch("infra.flaresolverr.wait_until_ready", return_value=True)
@patch("infra.flaresolverr.start", return_value=True)
@patch("infra.flaresolverr.is_running", return_value=False)
def test_ensure_running_starts_ours(mock_running, mock_start, mock_wait):
    config = DeeptorrentConfig()
    config.indexer.flare_solverr_url = "http://127.0.0.1:8191"
    assert flaresolverr.ensure_running(config) is True
    mock_start.assert_called_once()


@patch("infra.flaresolverr.start")
@patch("infra.flaresolverr.is_running", return_value=True)
def test_ensure_running_noop_when_up(mock_running, mock_start):
    config = DeeptorrentConfig()
    config.indexer.flare_solverr_url = flaresolverr.DEFAULT_URL
    assert flaresolverr.ensure_running(config) is True
    mock_start.assert_not_called()


@patch("infra.flaresolverr.is_running", return_value=False)
@patch("infra.flaresolverr.start")
def test_ensure_running_ignores_custom_remote(mock_start, mock_running):
    config = DeeptorrentConfig()
    config.indexer.flare_solverr_url = "http://192.168.1.50:8191/"
    assert flaresolverr.ensure_running(config) is False
    mock_start.assert_not_called()   # never start someone else's solver


@patch("infra.flaresolverr.subprocess.Popen")
def test_start_uses_env_and_detached(mock_popen, monkeypatch, tmp_path):
    exe = tmp_path / "flaresolverr" / "flaresolverr.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(flaresolverr.sys, "platform", "win32")
    monkeypatch.setattr(flaresolverr, "install_root", lambda: str(tmp_path / "flaresolverr"))
    assert flaresolverr.start() is True
    kwargs = mock_popen.call_args.kwargs
    assert kwargs["env"]["PORT"] == "8191"
    assert kwargs["env"]["HOST"] == "127.0.0.1"   # loopback-only binding
    assert kwargs["creationflags"]  # detached + no console window


# ----------------------------------------------------------------------
# run_setup — extraction + link pipeline
# ----------------------------------------------------------------------

def _make_fake_zip(path):
    """Same top-level layout as the official zip: flaresolverr/flaresolverr.exe."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("flaresolverr/flaresolverr.exe", b"MZ" + b"#" * 64)
        zf.writestr("flaresolverr/_internal/some.dll", b"dll")
        zf.writestr("flaresolverr/_internal/chrome/chrome.exe", b"MZ")


@patch("infra.flaresolverr.wait_until_ready", return_value=True)
@patch("infra.flaresolverr.start", return_value=True)
@patch("infra.flaresolverr.is_running", return_value=False)
def test_run_setup_extracts_starts_links(mock_running, mock_start, mock_wait, tmp_path, monkeypatch):
    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config_path = str(tmp_path / "config.json")
    config.to_file(config_path)  # run_setup reloads from disk — persist the key

    zip_path = tmp_path / "bundled.zip"
    _make_fake_zip(zip_path)
    install = tmp_path / "install" / "flaresolverr"
    monkeypatch.setattr(flaresolverr, "install_root", lambda: str(install))

    with patch("infra.jackett.set_flaresolverr_url",
               return_value={"success": True, "message": "saved"}) as mock_link:
        result = flaresolverr.run_setup(config_path=config_path, zip_path=str(zip_path))

    assert result["ok"] is True
    assert result["extracted"] is True and result["started"] is True
    assert result["jackett_linked"] is True
    mock_link.assert_called_once()
    # exe extracted at the expected place; zip removed to save disk
    assert os.path.isfile(install / "flaresolverr.exe")
    assert not zip_path.exists()
    # URL persisted in DeepFlux config
    reloaded = DeeptorrentConfig.from_file(config_path)
    assert reloaded.indexer.flare_solverr_url == flaresolverr.DEFAULT_URL


@patch("infra.flaresolverr.wait_until_ready", return_value=True)
@patch("infra.flaresolverr.start", return_value=True)
@patch("infra.flaresolverr.is_running", return_value=True)
def test_run_setup_idempotent_when_already_running(mock_running, mock_start, mock_wait, tmp_path, monkeypatch):
    exe = tmp_path / "flaresolverr" / "flaresolverr.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(flaresolverr, "install_root", lambda: str(tmp_path / "flaresolverr"))
    config_path = str(tmp_path / "config.json")
    with patch("infra.jackett.set_flaresolverr_url",
               return_value={"success": True, "message": "saved"}):
        result = flaresolverr.run_setup(config_path=config_path)
    assert result["ok"] is True and result["already_running"] is True


def test_cleanup_bundled_zip_removes_zip_when_extracted(tmp_path, monkeypatch):
    """FROZEN builds: installs/updates (incl. silent self-updates) re-copy the
    bundled zip; with the extracted install present it must be deleted."""
    exe = tmp_path / "flaresolverr" / "flaresolverr.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(flaresolverr.sys, "platform", "win32")
    monkeypatch.setattr(flaresolverr.sys, "frozen", True, raising=False)
    monkeypatch.setattr(flaresolverr, "install_root", lambda: str(tmp_path / "flaresolverr"))
    zip_copy = tmp_path / "bundled.zip"
    zip_copy.write_bytes(b"zip")
    monkeypatch.setattr(flaresolverr, "_zip_candidates", lambda: [str(zip_copy)])
    flaresolverr.cleanup_bundled_zip()
    assert not zip_copy.exists()
    # Without the extracted exe the zip is NOT touched (setup needs it).
    zip_copy.write_bytes(b"zip")
    exe.unlink()
    flaresolverr.cleanup_bundled_zip()
    assert zip_copy.exists()


def test_cleanup_bundled_zip_never_runs_in_dev(tmp_path, monkeypatch):
    """DEV runs must not clean: the 'bundled zip' is the fetch-script build
    input in packaging/ and the E2E extraction sits next to it — cleaning
    there eats the build input (regression: happened 2026-10-13)."""
    exe = tmp_path / "flaresolverr" / "flaresolverr.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(flaresolverr.sys, "platform", "win32")
    monkeypatch.delattr(flaresolverr.sys, "frozen", raising=False)
    monkeypatch.setattr(flaresolverr, "install_root", lambda: str(tmp_path / "flaresolverr"))
    zip_copy = tmp_path / "bundled.zip"
    zip_copy.write_bytes(b"zip")
    monkeypatch.setattr(flaresolverr, "_zip_candidates", lambda: [str(zip_copy)])
    flaresolverr.cleanup_bundled_zip()
    assert zip_copy.exists()  # untouched in a dev tree


def test_run_setup_without_zip_downloads_pinned_release(tmp_path, monkeypatch):
    """Since 6.0 the zip is not bundled: the step downloads the pinned
    official release (Vercel per-file cap). A failed download fails soft."""
    monkeypatch.setattr(flaresolverr.sys, "platform", "win32")
    monkeypatch.setattr(flaresolverr, "install_root", lambda: str(tmp_path / "nowhere" / "flaresolverr"))
    with patch.object(flaresolverr, "_zip_candidates", return_value=[]):
        # Download unavailable (offline) -> soft failure with retry hint.
        with patch.object(flaresolverr, "_download_pinned_zip", return_value=None):
            result = flaresolverr.run_setup(config_path=str(tmp_path / "c.json"), zip_path=None)
        assert result["ok"] is False
        assert "could not be downloaded" in result["error"]

        # Download succeeds -> extraction proceeds from the downloaded zip.
        dl_zip = tmp_path / "downloaded.zip"
        _make_fake_zip(dl_zip)
        with patch.object(flaresolverr, "_download_pinned_zip",
                          side_effect=lambda say: str(dl_zip)):
            with patch.object(flaresolverr, "start", return_value=True),                  patch.object(flaresolverr, "wait_until_ready", return_value=True):
                result = flaresolverr.run_setup(config_path=str(tmp_path / "c.json"), zip_path=None)
        assert result["ok"] is True and result["extracted"] is True


@patch("infra.flaresolverr.is_running", return_value=False)
@patch("infra.flaresolverr.start", return_value=True)
@patch("infra.flaresolverr.wait_until_ready", return_value=False)
def test_run_setup_reports_unresponsive_service(mock_wait, mock_start, mock_running, tmp_path, monkeypatch):
    zip_path = tmp_path / "bundled.zip"
    _make_fake_zip(zip_path)
    install = tmp_path / "install" / "flaresolverr"
    monkeypatch.setattr(flaresolverr, "install_root", lambda: str(install))
    result = flaresolverr.run_setup(config_path=str(tmp_path / "c.json"), zip_path=str(zip_path))
    assert result["ok"] is False
    assert "8191" in result["error"]


def test_run_setup_posix_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(flaresolverr.sys, "platform", "linux")
    result = flaresolverr.run_setup(config_path=str(tmp_path / "c.json"))
    assert result["ok"] is False
    assert "Windows-only" in result["error"]


# ----------------------------------------------------------------------
# Search-tier auto-start trigger
# ----------------------------------------------------------------------

@patch("agent.tools.TorznabClient.search_indexer")
def test_flare_failure_triggers_autostart_dispatch(mock_search, mock_engine):
    from agent.tools import ToolRegistry
    from config import SourceConfig

    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    config.indexer.flare_solverr_url = flaresolverr.DEFAULT_URL
    config.sources.sources = [
        SourceConfig(id="flarewall", name="FlareWall", type="public", origin="jackett"),
    ]
    with patch("agent.tools.TorznabClient.search_indexer") as ms:
        ms.return_value = {"success": False, "results": [],
                           "error": "Challenge detected but FlareSolverr is not configured"}
        with patch.object(ToolRegistry, "_maybe_start_flaresolverr") as maybe:
            tools = ToolRegistry(mock_engine, config)
            tools.call("search_indexers", {"query": "Ubuntu", "deep": True})
            assert maybe.call_count >= 1
