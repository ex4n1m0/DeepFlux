"""Anonymous usage ping (infra/telemetry.py).

Every network call is mocked — these tests must never touch deepflux.space.
"""
from __future__ import annotations

import json
import os
import time
from unittest import mock

import pytest

from config import APP_VERSION, DeeptorrentConfig, StatsConfig
from infra import telemetry


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Isolate the module-level thread/id caches between tests."""
    telemetry.stop_heartbeat()
    telemetry._thread = None
    telemetry._install_id_cache = None
    telemetry._stop.clear()
    yield
    telemetry.stop_heartbeat()
    telemetry._thread = None
    telemetry._install_id_cache = None
    telemetry._stop.clear()


@pytest.fixture()
def config(tmp_path):
    cfg = DeeptorrentConfig()
    cfg.stats = StatsConfig(ping_enabled=True)
    return cfg


def test_config_defaults_to_enabled_and_survives_roundtrip(tmp_path):
    cfg = DeeptorrentConfig.from_file(str(tmp_path / "none.json"))
    assert cfg.stats.ping_enabled is True  # opt-out, not opt-in

    cfg.stats.ping_enabled = False
    path = str(tmp_path / "config.json")
    cfg.to_file(path)
    loaded = DeeptorrentConfig.from_file(path)
    assert loaded.stats.ping_enabled is False


def test_install_id_is_stable_and_separate_from_config(tmp_path):
    first = telemetry.install_id(str(tmp_path))
    assert 8 <= len(first) <= 64
    assert telemetry.install_id(str(tmp_path)) == first
    # Re-read from disk with the cache cleared: persistence, not memory.
    telemetry._install_id_cache = None
    assert telemetry.install_id(str(tmp_path)) == first
    assert (tmp_path / "install_id").is_file()
    # Deliberately not part of the config backup payload.
    assert "install_id" not in json.dumps(DeeptorrentConfig().sanitized_dict())


def test_beat_payload_is_minimal(tmp_path):
    install = telemetry.install_id(str(tmp_path))
    with mock.patch.object(telemetry.requests, "post") as post:
        post.return_value = mock.Mock(status_code=200)
        assert telemetry.send_beat(install) is True
    payload = post.call_args.kwargs["json"]
    assert set(payload) == {"id", "v", "os"}  # nothing else ever leaves the machine
    assert payload["id"] == install
    assert payload["v"] == APP_VERSION
    assert payload["os"] == os.name


def test_leave_payload_carries_leave_flag(tmp_path):
    install = telemetry.install_id(str(tmp_path))
    with mock.patch.object(telemetry.requests, "post") as post:
        post.return_value = mock.Mock(status_code=200)
        telemetry.send_leave(install)
    assert post.call_args.kwargs["json"]["leave"] is True


def test_network_failure_is_silent(tmp_path):
    install = telemetry.install_id(str(tmp_path))
    with mock.patch.object(telemetry.requests, "post",
                           side_effect=OSError("blocked")):
        assert telemetry.send_beat(install) is False  # no raise


def test_disabled_config_starts_nothing(config, tmp_path):
    config.stats.ping_enabled = False
    with mock.patch.object(telemetry.requests, "post") as post:
        telemetry.start_heartbeat(config, data_dir=str(tmp_path))
        assert telemetry._thread is None
        time.sleep(0.1)
        post.assert_not_called()


def test_heartbeat_beats_immediately_then_stops(tmp_path, config):
    with mock.patch.object(telemetry.requests, "post") as post:
        post.return_value = mock.Mock(status_code=200)
        telemetry.start_heartbeat(config, data_dir=str(tmp_path))
        assert telemetry._thread is not None
        # First beat is immediate — a user opening the app shows up at once.
        for _ in range(50):
            if post.call_count >= 1:
                break
            time.sleep(0.02)
        assert post.call_count == 1
        body = post.call_args.kwargs["json"]
        assert body["id"] == telemetry.install_id(str(tmp_path))

        telemetry.stop_heartbeat()
        # The leave note fires right away (own thread) — allow a moment.
        for _ in range(50):
            if post.call_count >= 2:
                break
            time.sleep(0.02)
        assert post.call_count == 2
        assert post.call_args_list[1].kwargs["json"]["leave"] is True


def test_double_start_is_one_thread(tmp_path, config):
    with mock.patch.object(telemetry.requests, "post") as post:
        post.return_value = mock.Mock(status_code=200)
        telemetry.start_heartbeat(config, data_dir=str(tmp_path))
        telemetry.start_heartbeat(config, data_dir=str(tmp_path))
        assert telemetry._thread is not None and telemetry._thread.is_alive()
        telemetry.stop_heartbeat()
