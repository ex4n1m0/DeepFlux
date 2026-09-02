"""Security-focused tests for Xtream credential handling and load failures."""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import parse_qs, quote, quote_plus, urlparse

import pytest
import requests

from iptv import xtream
from iptv.models import Playlist, PlaylistSource


def _source(*, username: str = "test-user", password: str = "test-password") -> PlaylistSource:
    return PlaylistSource(
        id="secure-xtream",
        name="Secure Xtream",
        kind="xtream",
        url="https://provider.example:8443",
        username=username,
        password=password,
    )


@pytest.mark.parametrize("password", ["plus+pass", "amp&pass", "slash/pass", "space pass", "雪密碼"])
def test_api_query_round_trips_special_passwords(password):
    src = _source(username="user +/&雪", password=password)

    url = xtream._api(src, action="get_live_streams")
    query = parse_qs(urlparse(url).query)

    assert query["username"] == [src.username]
    assert query["password"] == [password]
    assert query["action"] == ["get_live_streams"]


def test_request_log_redacts_raw_and_percent_encoded_credentials(monkeypatch, caplog):
    src = _source(username="private/user+雪", password="secret+&/ space雪")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    requested = []

    def fake_get(url, headers=None, timeout=None):
        requested.append(url)
        return Response()

    monkeypatch.setattr(xtream.requests, "get", fake_get)
    caplog.set_level(logging.DEBUG, logger="iptv.xtream")

    xtream._get(src, action="get_live_categories")

    assert requested
    assert parse_qs(urlparse(requested[0]).query)["password"] == [src.password]
    logged = caplog.text
    for secret in (src.username, src.password):
        assert secret not in logged
        assert quote(secret, safe="") not in logged
        assert quote_plus(secret, safe="") not in logged
    assert "username=***" in logged
    assert "password=***" in logged


def test_safe_api_url_redacts_already_percent_encoded_password():
    safe = xtream._safe_api_url(
        "https://provider.example/player_api.php?"
        "username=user%2Fname&password=p%2B%26%2F%20%E9%9B%AA&action=get_live_categories"
    )

    assert safe == (
        "https://provider.example/player_api.php?"
        "username=***&password=***&action=get_live_categories"
    )
    assert "user%2Fname" not in safe
    assert "p%2B%26%2F%20%E9%9B%AA" not in safe


def test_generated_stream_urls_quote_credential_path_components(monkeypatch):
    src = _source(username="user +/&雪", password="pass+&/ word密")
    monkeypatch.setattr(
        xtream,
        "_get_raw",
        lambda _src: {
            "user_info": {"auth": 1},
            "server_info": {"url": "https://stream.example:9443"},
        },
    )

    def fake_get(_src, action, **extra):
        responses = {
            "get_live_categories": [{"category_id": "live", "category_name": "Live"}],
            "get_live_streams": [{"stream_id": "11", "name": "Channel"}],
            "get_vod_categories": [{"category_id": "vod", "category_name": "Movies"}],
            "get_vod_streams": [
                {"stream_id": "22", "name": "Movie", "container_extension": "mkv"}
            ],
            "get_series_categories": [{"category_id": "series", "category_name": "Series"}],
            "get_series": [{"series_id": "33", "name": "Show"}],
            "get_series_info": {
                "episodes": {
                    "1": [
                        {
                            "id": "44",
                            "title": "Pilot",
                            "episode_num": 1,
                            "container_extension": "mp4",
                        }
                    ]
                }
            },
        }
        return responses[action]

    monkeypatch.setattr(xtream, "_get", fake_get)

    playlist = xtream.load_playlist(src)
    user = quote(src.username, safe="")
    password = quote(src.password, safe="")

    assert playlist.error is None
    assert playlist.channels[0].url == f"https://stream.example:9443/live/{user}/{password}/11.m3u8"
    assert playlist.movies[0].url == f"https://stream.example:9443/movie/{user}/{password}/22.mkv"
    assert playlist.series[0].episodes[0].url == (
        f"https://stream.example:9443/series/{user}/{password}/44.mp4"
    )
    assert "/user +/&雪/" not in playlist.channels[0].url
    assert "/pass+&/ word密/" not in playlist.channels[0].url


def test_rejected_auth_is_typed_without_breaking_playlist_callers(monkeypatch):
    src = _source(password="rejected+password")
    monkeypatch.setattr(
        xtream,
        "_get_raw",
        lambda _src: {"user_info": {"auth": 0}, "server_info": {}},
    )

    playlist = xtream.load_playlist(src)

    assert isinstance(playlist, Playlist)
    assert playlist.total == 0
    assert playlist.error is not None
    assert playlist.error.kind is xtream.XtreamErrorKind.AUTH_REJECTED
    assert "rejected+password" not in playlist.error.message
    assert xtream.authenticate(src) is None


@pytest.mark.parametrize(
    ("auth_result", "expected_kind"),
    [
        (requests.ConnectionError("failed URL password=encoded%2Fsecret"), xtream.XtreamErrorKind.UNREACHABLE),
        (ValueError("not JSON"), xtream.XtreamErrorKind.INVALID_RESPONSE),
        ({"unexpected": "payload"}, xtream.XtreamErrorKind.INVALID_RESPONSE),
    ],
)
def test_failed_auth_is_distinct_from_empty_playlist(monkeypatch, auth_result, expected_kind):
    src = _source()

    def fake_get_raw(_src):
        if isinstance(auth_result, Exception):
            raise auth_result
        return auth_result

    monkeypatch.setattr(xtream, "_get_raw", fake_get_raw)
    failed = xtream.load_playlist(src)

    assert failed.total == 0
    assert failed.error is not None
    assert failed.error.kind is expected_kind


def test_successful_genuinely_empty_playlist_has_no_error(monkeypatch):
    src = _source()
    monkeypatch.setattr(
        xtream,
        "_get_raw",
        lambda _src: {"user_info": {"auth": "1"}, "server_info": {}},
    )
    monkeypatch.setattr(xtream, "_get", lambda _src, action, **extra: [])

    playlist = xtream.load_playlist(src)

    assert playlist.total == 0
    assert playlist.error is None


def test_series_info_retries_rate_limit_and_honors_retry_after(monkeypatch):
    src = _source()
    xtream._clear_series_info_cache()
    calls = []
    sleeps = []

    def fake_get(_src, action, **extra):
        calls.append((action, extra))
        if len(calls) < 3:
            response = requests.Response()
            response.status_code = 429
            response.headers["Retry-After"] = "2"
            raise requests.HTTPError("rate limited", response=response)
        return {"episodes": {}}

    monkeypatch.setattr(xtream, "_get", fake_get)
    monkeypatch.setattr(
        xtream, "_sleep_unless_cancelled",
        lambda delay, is_cancelled: sleeps.append(delay) or True,
    )

    assert xtream._get_series_info(src, "rate-limited", None) == {"episodes": {}}
    assert len(calls) == 3
    assert sleeps == [2.0, 2.0]
    # Successful response is cached; a second request does not hit the provider.
    assert xtream._get_series_info(src, "rate-limited", None) == {"episodes": {}}
    assert len(calls) == 3


def test_series_info_concurrency_is_bounded(monkeypatch):
    src = _source()
    xtream._clear_series_info_cache()
    monkeypatch.setattr(
        xtream,
        "_get_raw",
        lambda _src: {
            "user_info": {"auth": 1},
            "server_info": {"url": "https://stream.example"},
        },
    )
    lock = threading.Lock()
    active = 0
    maximum = 0
    detail_calls = 0

    def fake_get(_src, action, **extra):
        nonlocal active, maximum, detail_calls
        if action in ("get_live_categories", "get_vod_categories"):
            return []
        if action == "get_series_categories":
            return [{"category_id": "series", "category_name": "Series"}]
        if action == "get_series":
            return [{"series_id": str(i), "name": f"Show {i}"} for i in range(10)]
        assert action == "get_series_info"
        with lock:
            active += 1
            detail_calls += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {"episodes": {}}

    monkeypatch.setattr(xtream, "_get", fake_get)
    playlist = xtream.load_playlist(src, series_info_concurrency=2)

    assert len(playlist.series) == 10
    assert detail_calls == 10
    assert maximum <= 2
    assert maximum == 2


def test_cancelled_series_info_does_not_issue_request(monkeypatch):
    src = _source()
    xtream._clear_series_info_cache()
    monkeypatch.setattr(
        xtream, "_get", lambda *a, **k: pytest.fail("request scheduled after cancellation"))
    assert xtream._get_series_info(src, "cancelled", lambda: True) == {}
