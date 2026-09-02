from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from native_messaging.host import NativeMessagingHost
from native_messaging.register import write_host_manifest


def test_native_manifest_requires_exact_extension_id(tmp_path):
    with patch("native_messaging.register._get_host_manifest_path", return_value=str(tmp_path / "host.json")), \
         patch("native_messaging.register._get_exe_path", return_value="C:\\DeepFlux.exe"):
        with pytest.raises(ValueError):
            write_host_manifest("")
        path = write_host_manifest("a" * 32)

    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    assert manifest["allowed_origins"] == [f"chrome-extension://{'a' * 32}/"]


def test_native_host_sends_control_api_bearer():
    host = NativeMessagingHost(api_token="native-token")
    response = type("Response", (), {
        "read": lambda self: b'{"status":"ok"}',
        "__enter__": lambda self: self,
        "__exit__": lambda self, *args: None,
    })()
    with patch("native_messaging.host.urlopen", return_value=response) as open_url:
        result = host._api_call("GET", "/api/health")

    request = open_url.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer native-token"
    assert result == {"status": "ok"}
