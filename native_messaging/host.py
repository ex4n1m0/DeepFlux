"""Native messaging host — stdio protocol for Chrome extension communication.

Chrome's native messaging protocol uses 4-byte little-endian length-prefixed
JSON messages over stdin/stdout. This module implements that protocol and
acts as a thin proxy between the Chrome extension and DeepFlux's local
control API (127.0.0.1:53742).

When DeepFlux is launched with --native-messaging, it enters stdio host
mode instead of showing the GUI. The extension connects via
chrome.runtime.connectNative("com.deeptorrent.integration") and maintains
a persistent port for real-time progress updates.
"""
from __future__ import annotations

import json
import logging
import os
import struct
import sys
import threading
import time
from typing import Any, Dict, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from dlmgr.control_api import load_control_api_token

logger = logging.getLogger(__name__)

# Native messaging host name — must match the registry key and extension manifest.
HOST_NAME = "com.deeptorrent.integration"

# Default control API port (must match DownloadConfig).
DEFAULT_API_PORT = 53742


class NativeMessagingHost:
    """Implements the Chrome native messaging stdio protocol.

    Reads messages from stdin, forwards them to the local control API,
    and writes responses back to stdout. Also streams periodic status
    updates so the extension can show real-time progress."""

    def __init__(self, api_port: int = DEFAULT_API_PORT, api_token: str = "") -> None:
        self._api_port = api_port
        self._api_token = api_token or load_control_api_token()
        self._running = False
        self._status_thread: Optional[threading.Thread] = None

    def run(self) -> None:
        """Main loop: read messages from stdin and process them.

        Runs until stdin is closed (extension disconnects) or an error occurs."""
        self._running = True

        # Start a background thread to stream status updates.
        self._status_thread = threading.Thread(target=self._status_loop, daemon=True)
        self._status_thread.start()

        logger.info("Native messaging host started (API port %d)", self._api_port)

        while self._running:
            try:
                message = self._read_message()
                if message is None:
                    break
                self._handle_message(message)
            except Exception as exc:
                logger.warning("Native messaging error: %s", exc)
                break

        self._running = False
        logger.info("Native messaging host stopped")

    def _read_message(self) -> Optional[Dict[str, Any]]:
        """Read a single message from stdin using the native messaging protocol.

        Returns None if stdin is closed (EOF)."""
        # Read 4-byte little-endian length prefix.
        raw_len = self._read_exact(4)
        if raw_len is None:
            return None
        msg_len = struct.unpack("<I", raw_len)[0]
        if msg_len == 0:
            return {}
        if msg_len > 10 * 1024 * 1024:  # 10 MB safety limit
            logger.warning("Message too large: %d bytes", msg_len)
            return None
        # Read the message body.
        raw_msg = self._read_exact(msg_len)
        if raw_msg is None:
            return None
        try:
            return json.loads(raw_msg.decode("utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode message: %s", exc)
            return None

    def _read_exact(self, n: int) -> Optional[bytes]:
        """Read exactly n bytes from stdin. Returns None on EOF."""
        data = b""
        while len(data) < n:
            chunk = sys.stdin.buffer.read(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _send_message(self, message: Dict[str, Any]) -> None:
        """Write a message to stdout using the native messaging protocol."""
        try:
            encoded = json.dumps(message).encode("utf-8")
            sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()
        except Exception as exc:
            logger.warning("Failed to send message: %s", exc)

    def _handle_message(self, message: Dict[str, Any]) -> None:
        """Process an incoming message from the extension.

        Routes the message to the appropriate control API endpoint and
        sends the response back."""
        action = message.get("action", "")
        msg_id = message.get("id", "")

        try:
            if action == "ping":
                self._send_message({"id": msg_id, "status": "ok", "pong": True})
                return

            if action == "submit_download":
                result = self._api_call("POST", "/api/jobs", payload={
                    "url": message.get("url", ""),
                    "filename": message.get("filename", ""),
                    "headers": message.get("headers", {}),
                    "cookies": message.get("cookies", ""),
                    "referrer": message.get("referrer", ""),
                    "type": message.get("type", "file"),
                    "source_url": message.get("source_url", ""),
                })
                self._send_message({"id": msg_id, "result": result})
                return

            if action == "list_jobs":
                result = self._api_call("GET", "/api/jobs")
                self._send_message({"id": msg_id, "result": result})
                return

            if action == "get_job":
                job_id = message.get("job_id", "")
                result = self._api_call("GET", f"/api/jobs/{job_id}")
                self._send_message({"id": msg_id, "result": result})
                return

            if action == "pause_job":
                job_id = message.get("job_id", "")
                result = self._api_call("POST", f"/api/jobs/{job_id}/pause")
                self._send_message({"id": msg_id, "result": result})
                return

            if action == "resume_job":
                job_id = message.get("job_id", "")
                result = self._api_call("POST", f"/api/jobs/{job_id}/resume")
                self._send_message({"id": msg_id, "result": result})
                return

            if action == "cancel_job":
                job_id = message.get("job_id", "")
                result = self._api_call("POST", f"/api/jobs/{job_id}/cancel")
                self._send_message({"id": msg_id, "result": result})
                return

            if action == "health":
                result = self._api_call("GET", "/api/health")
                self._send_message({"id": msg_id, "result": result})
                return

            self._send_message({"id": msg_id, "error": f"Unknown action: {action}"})

        except Exception as exc:
            self._send_message({"id": msg_id, "error": str(exc)})

    def _api_call(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        """Make an HTTP request to the local control API.

        Returns the parsed JSON response, or an error dict."""
        if not self._api_token:
            return {"error": "DeepFlux control API token is unavailable"}
        data = json.dumps(payload).encode("utf-8") if payload else None
        last_error: Optional[Exception] = None
        ports = [self._api_port] + [port for port in range(self._api_port + 1, self._api_port + 10)]
        for port in ports:
            url = f"http://127.0.0.1:{port}{path}"
            try:
                req = Request(url, data=data, method=method)
                req.add_header("Authorization", f"Bearer {self._api_token}")
                if data:
                    req.add_header("Content-Type", "application/json")
                with urlopen(req, timeout=10) as resp:
                    body = resp.read().decode("utf-8")
                    self._api_port = port
                    return json.loads(body)
            except HTTPError as exc:
                last_error = exc
                continue
            except URLError as exc:
                last_error = exc
                continue
            except Exception as exc:
                return {"error": str(exc)}
        return {"error": f"API connection failed: {last_error}"}

    def _status_loop(self) -> None:
        """Periodically stream job status updates to the extension.

        Sends a 'status_update' message every 2 seconds with the current
        job list, so the extension popup and badge stay up to date."""
        while self._running:
            time.sleep(2.0)
            if not self._running:
                break
            try:
                result = self._api_call("GET", "/api/jobs")
                if "jobs" in result:
                    jobs = result["jobs"]
                    active = sum(1 for j in jobs if j.get("status") in ("downloading", "queued"))
                    self._send_message({
                        "type": "status_update",
                        "active_count": active,
                        "jobs": jobs,
                    })
            except Exception as exc:
                logger.debug("status update failed: %s", exc)
