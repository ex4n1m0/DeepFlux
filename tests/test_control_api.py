from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import MagicMock

import pytest

from dlmgr.control_api import ControlAPI


@pytest.fixture
def running_api():
    engine = MagicMock()
    engine.list_jobs.return_value = []
    job = MagicMock()
    job.to_dict.return_value = {"id": "job-1", "status": "queued"}
    engine.add_job.return_value = job
    api = ControlAPI(engine, port=0, api_token="test-control-token-abcdefghijklmnopqrstuvwxyz")
    api.start()
    port = api._server.server_port
    try:
        yield api, engine, f"http://127.0.0.1:{port}"
    finally:
        api.stop()


def _request(url: str, token: str = "", method: str = "GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, data=data, headers=headers, method=method)


def test_control_api_requires_authentication(running_api):
    _api, _engine, base = running_api
    with pytest.raises(HTTPError) as exc:
        urlopen(_request(f"{base}/api/health"), timeout=2)
    assert exc.value.code == 401


def test_control_api_accepts_bearer_and_returns_cors_headers(running_api):
    api, _engine, base = running_api
    with urlopen(_request(f"{base}/api/health", api.api_token), timeout=2) as response:
        assert json.loads(response.read()) == {"status": "ok"}
        assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_control_api_rejects_unauthorized_mutation(running_api):
    _api, engine, base = running_api
    with pytest.raises(HTTPError) as exc:
        urlopen(_request(f"{base}/api/jobs", method="POST", payload={"url": "https://example.com/a"}), timeout=2)
    assert exc.value.code == 401
    engine.add_job.assert_not_called()


def test_control_api_rejects_private_download_url(running_api):
    api, engine, base = running_api
    request = _request(
        f"{base}/api/jobs", api.api_token, "POST",
        {"url": "http://127.0.0.1/private"},
    )
    with pytest.raises(HTTPError) as exc:
        urlopen(request, timeout=2)
    assert exc.value.code == 400
    engine.add_job.assert_not_called()


def test_control_api_authorized_job_submission(running_api):
    api, engine, base = running_api
    request = _request(
        f"{base}/api/jobs", api.api_token, "POST",
        {"url": "https://example.com/a.zip", "filename": "a.zip"},
    )
    with urlopen(request, timeout=2) as response:
        assert response.status == 201
        assert json.loads(response.read())["id"] == "job-1"
    engine.add_job.assert_called_once()


def test_control_api_preflight_allows_authorization_header(running_api):
    _api, _engine, base = running_api
    with urlopen(_request(f"{base}/api/jobs", method="OPTIONS"), timeout=2) as response:
        assert response.status == 204
        assert "Authorization" in response.headers["Access-Control-Allow-Headers"]
