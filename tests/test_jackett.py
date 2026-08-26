"""Unit tests for infra.jackett — service management and source sync."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from config import DeeptorrentConfig, SourceConfig
from infra import jackett

_INDEXERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<indexers>
  <indexer id="iptorrents" configured="true">
    <title>IPTorrents</title>
    <link>https://iptorrents.com/</link>
    <type>private</type>
  </indexer>
  <indexer id="1337x" configured="true">
    <title>1337x</title>
    <link>https://1337x.to/</link>
    <type>public</type>
  </indexer>
  <indexer id="notsetup" configured="false">
    <title>Not Setup</title>
    <link>https://example.com/</link>
    <type>public</type>
  </indexer>
</indexers>
"""


def _xml_response(text=_INDEXERS_XML, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.raise_for_status = lambda: None
    return resp


def _jackett_config() -> DeeptorrentConfig:
    config = DeeptorrentConfig()
    config.indexer.api_key = "test-key"
    return config


# ----------------------------------------------------------------------
# is_running
# ----------------------------------------------------------------------

@patch("infra.jackett.requests.get")
def test_is_running_true_on_200(mock_get):
    mock_get.return_value = _xml_response()
    config = _jackett_config()
    assert jackett.is_running(config) is True
    assert mock_get.call_args.kwargs["params"]["t"] == "caps"


@patch("infra.jackett.requests.get", side_effect=ConnectionError("refused"))
def test_is_running_false_on_error(mock_get):
    assert jackett.is_running(_jackett_config()) is False


def test_is_running_false_without_api_key():
    assert jackett.is_running(DeeptorrentConfig()) is False


# ----------------------------------------------------------------------
# fetch_indexers / merge_sources
# ----------------------------------------------------------------------

@patch("infra.jackett.requests.get")
def test_fetch_indexers_only_configured(mock_get):
    mock_get.return_value = _xml_response()
    sources = jackett.fetch_indexers(_jackett_config())
    assert [s.id for s in sources] == ["iptorrents", "1337x"]
    assert sources[0].type == "private"
    assert sources[0].name == "IPTorrents"
    assert sources[1].url == "https://1337x.to/"
    assert mock_get.call_args.kwargs["params"]["t"] == "indexers"


def test_merge_sources_preserves_disabled_and_enables_new():
    existing = [SourceConfig(id="iptorrents", name="IPTorrents", enabled=False),
                SourceConfig(id="oldgone", name="Old", enabled=True)]
    fetched = [SourceConfig(id="iptorrents", name="IPTorrents", type="private"),
               SourceConfig(id="1337x", name="1337x", type="public")]
    # Default policy: new indexers start enabled; explicit user choices survive.
    merged = jackett.merge_sources(existing, fetched)
    by_id = {s.id: s for s in merged}
    assert by_id["iptorrents"].enabled is False  # user disabled it — stays disabled
    assert by_id["1337x"].enabled is True        # new from Jackett — enabled by default
    assert "oldgone" not in by_id                # removed from Jackett — drops off


def test_merge_sources_opt_out_of_enabling_new():
    fetched = [SourceConfig(id="1337x", name="1337x", type="public")]
    merged = jackett.merge_sources([], fetched, enable_new=False)
    assert merged[0].enabled is False


def test_merge_sources_bootstrap_enables_all():
    fetched = [SourceConfig(id="1337x", name="1337x")]
    merged = jackett.merge_sources([], fetched, enable_new=True)
    assert merged[0].enabled is True


# ----------------------------------------------------------------------
# sync_sources
# ----------------------------------------------------------------------

@patch("infra.jackett.fetch_indexers")
def test_sync_sources_bootstrap_enables_and_stamps(mock_fetch, tmp_path):
    mock_fetch.return_value = [
        SourceConfig(id="iptorrents", name="IPTorrents", type="private"),
        SourceConfig(id="1337x", name="1337x", type="public"),
    ]
    config = _jackett_config()
    config_path = str(tmp_path / "config.json")
    result = jackett.sync_sources(config, config_path)
    assert result == {"total": 2, "enabled": 2, "bootstrap": True}
    assert all(s.enabled for s in config.sources.sources)
    assert config.sources.last_jackett_fetch > 0
    # Persisted to disk.
    reloaded = DeeptorrentConfig.from_file(config_path)
    assert len(reloaded.sources.sources) == 2
    assert reloaded.sources.last_jackett_fetch > 0


@patch("infra.jackett.fetch_indexers", return_value=[])
def test_sync_sources_keeps_list_when_jackett_empty(mock_fetch):
    config = _jackett_config()
    config.sources.sources = [SourceConfig(id="iptorrents", name="IPTorrents", enabled=True)]
    assert jackett.sync_sources(config) is None
    assert [s.id for s in config.sources.sources] == ["iptorrents"]


# ----------------------------------------------------------------------
# maybe_auto_sync — gating + auto-start
# ----------------------------------------------------------------------

def test_auto_sync_skips_without_jackett():
    assert jackett.maybe_auto_sync(DeeptorrentConfig()) is None


@patch("infra.jackett.sync_sources")
@patch("infra.jackett.is_running", return_value=True)
def test_auto_sync_noop_when_fresh(mock_running, mock_sync):
    config = _jackett_config()
    config.sources.sources = [SourceConfig(id="iptorrents", name="IPTorrents", enabled=True)]
    config.sources.last_jackett_fetch = time.time()  # synced just now
    assert jackett.maybe_auto_sync(config) is None
    mock_sync.assert_not_called()


@patch("infra.jackett.sync_sources",
       return_value={"total": 3, "enabled": 3, "bootstrap": True})
@patch("infra.jackett.is_running", return_value=True)
def test_auto_sync_bootstraps_empty_list(mock_running, mock_sync):
    config = _jackett_config()
    config.sources.last_jackett_fetch = time.time()  # fresh — but list is empty
    result = jackett.maybe_auto_sync(config)
    assert result["changed"] is True
    assert result["bootstrap"] is True
    assert result["total"] == 3


@patch("infra.jackett.sync_sources",
       return_value={"total": 5, "enabled": 2, "bootstrap": False})
@patch("infra.jackett.is_running", return_value=True)
def test_auto_sync_fetches_when_stale(mock_running, mock_sync):
    config = _jackett_config()
    config.sources.sources = [SourceConfig(id="iptorrents", name="IPTorrents", enabled=True)]
    config.sources.last_jackett_fetch = time.time() - jackett.SYNC_INTERVAL_SECONDS - 60
    result = jackett.maybe_auto_sync(config)
    assert result["changed"] is True
    assert result["enabled"] == 2


@patch("infra.jackett.wait_until_ready", return_value=True)
@patch("infra.jackett.start", return_value=True)
@patch("infra.jackett.sync_sources", return_value=None)
@patch("infra.jackett.is_running", return_value=False)
def test_auto_sync_starts_jackett_when_down(mock_running, mock_sync, mock_start, mock_wait):
    config = _jackett_config()
    config.sources.sources = [SourceConfig(id="iptorrents", name="IPTorrents", enabled=True)]
    config.sources.last_jackett_fetch = time.time()  # not due — start only
    result = jackett.maybe_auto_sync(config)
    mock_start.assert_called_once()
    assert result["started"] is True
    assert result["reachable"] is True
    assert result["changed"] is False


@patch("infra.jackett.wait_until_ready", return_value=False)
@patch("infra.jackett.start", return_value=True)
@patch("infra.jackett.is_running", return_value=False)
def test_auto_sync_reports_unreachable_when_start_fails(mock_running, mock_start, mock_wait):
    config = _jackett_config()
    result = jackett.maybe_auto_sync(config)
    assert result == {"reachable": False, "started": False, "changed": False,
                      "bootstrap": False, "total": 0, "enabled": 0}


@patch("infra.jackett.is_running", return_value=False)
def test_auto_sync_silent_when_auto_start_off(mock_running):
    config = _jackett_config()
    config.indexer.auto_start = False
    assert jackett.maybe_auto_sync(config) is None


@patch("infra.jackett.sync_sources",
       return_value={"total": 3, "enabled": 3, "bootstrap": False})
@patch("infra.jackett.is_running", return_value=True)
def test_auto_sync_force_ignores_daily_gate(mock_running, mock_sync):
    """Saving Jackett settings (OK) forces an immediate sync even when fresh."""
    config = _jackett_config()
    config.sources.sources = [SourceConfig(id="iptorrents", name="IPTorrents", enabled=True)]
    config.sources.last_jackett_fetch = time.time()  # synced just now
    result = jackett.maybe_auto_sync(config, force=True)
    mock_sync.assert_called_once()
    assert result["changed"] is True


@patch("infra.jackett.sync_sources", return_value=None)
@patch("infra.jackett.is_running", return_value=True)
def test_auto_sync_force_reports_sync_failure(mock_running, mock_sync):
    """Forced sync that reaches Jackett but fetches nothing is surfaced, not silent."""
    config = _jackett_config()
    result = jackett.maybe_auto_sync(config, force=True)
    assert result["sync_failed"] is True
    assert result["changed"] is False
    assert result["reachable"] is True


# ----------------------------------------------------------------------
# start() — service first, exe fallback
# ----------------------------------------------------------------------

@patch("infra.jackett.subprocess.run")
def test_start_via_service(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    assert jackett.start(_jackett_config()) is True
    assert mock_run.call_args.args[0][:2] == ["sc.exe", "start"]


@patch("infra.jackett.subprocess.Popen")
@patch("infra.jackett.find_executable", return_value=r"C:\Program Files\Jackett\JackettTray.exe")
@patch("infra.jackett.subprocess.run")
def test_start_falls_back_to_executable(mock_run, mock_find, mock_popen):
    # 1060 = service not installed.
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="[SC] StartService FAILED 1060")
    assert jackett.start(_jackett_config()) is True
    mock_popen.assert_called_once()
    assert "JackettTray.exe" in mock_popen.call_args.args[0][0]


@patch("infra.jackett.find_executable", return_value=None)
@patch("infra.jackett.subprocess.run")
def test_start_false_when_nothing_available(mock_run, mock_find):
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="FAILED 1060")
    assert jackett.start(_jackett_config()) is False


def test_find_executable_prefers_configured_path(tmp_path):
    exe = tmp_path / "JackettTray.exe"
    exe.write_text("x")
    assert jackett.find_executable(str(exe)) == str(exe)


def test_find_executable_none_when_nothing_exists(tmp_path):
    with patch("infra.jackett.os.path.isfile", return_value=False):
        assert jackett.find_executable(str(tmp_path / "missing.exe")) is None
