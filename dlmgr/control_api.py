"""Local control API — loopback HTTP server for job submission and control.

Runs on 127.0.0.1:53742 (configurable). Serves both the in-app UI and
the Chrome extension's native messaging bridge. All endpoints are
JSON request/response.

Endpoints:
  POST   /api/jobs            — submit a new download job
  GET    /api/jobs            — list all jobs
  GET    /api/jobs/{id}       — get single job status
  POST   /api/jobs/{id}/pause — pause a job
  POST   /api/jobs/{id}/resume— resume a job
  POST   /api/jobs/{id}/cancel— cancel + delete partial file
  DELETE /api/jobs/{id}       — remove from list (keeps file)
  POST   /api/jobs/{id}/retry — retry an errored job
  GET    /api/health          — health check
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .engine import DownloadEngine

logger = logging.getLogger(__name__)


class _ControlAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the control API."""

    # Suppress default logging to stderr.
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(self, code: int, data: Any) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self) -> None:
        """Allow cross-origin requests from the built-in browser's pages.

        The injected extension script runs in the origin of whatever page the
        user is browsing (e.g. https://www.youtube.com) and calls this API at
        http://127.0.0.1:53742. Without these headers Chromium blocks the
        request/response entirely. This server only listens on loopback, so
        an open origin policy is safe here."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Required by Chromium's Private Network Access spec for requests from
        # public (HTTPS) pages to loopback addresses.
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests."""
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        engine = self.server.engine  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/health":
            return self._send_json(200, {"status": "ok"})

        if path == "/api/jobs":
            jobs = [j.to_dict() for j in engine.list_jobs()]
            return self._send_json(200, {"jobs": jobs})

        # /api/jobs/{id}
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "jobs":
            job = engine.get_job(parts[3])
            if job:
                return self._send_json(200, job.to_dict())
            return self._send_json(404, {"error": "Job not found"})

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        engine = self.server.engine  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = path.split("/")

        # POST /api/open — open a file/URI in the running app (single-instance
        # forwarding: file associations launch a second process that hands the
        # target to us and exits).
        if path == "/api/open":
            body = self._read_body()
            target = body.get("target", "").strip()
            if not target:
                return self._send_json(400, {"error": "Missing 'target'"})
            handler = getattr(self.server, "open_handler", None)  # type: ignore[attr-defined]
            if handler is None:
                return self._send_json(503, {"error": "No open handler registered"})
            try:
                handler(target)
                return self._send_json(200, {"status": "accepted"})
            except Exception as exc:
                logger.warning("open handler failed: %s", exc)
                return self._send_json(500, {"error": str(exc)})

        # POST /api/play — play a captured web stream in the in-app mpv player
        if path == "/api/play":
            body = self._read_body()
            url = body.get("url", "").strip()
            if not url:
                return self._send_json(400, {"error": "Missing 'url'"})
            handler = getattr(self.server, "play_handler", None)  # type: ignore[attr-defined]
            if handler is None:
                return self._send_json(503, {"error": "No play handler registered"})
            try:
                handler({
                    "url": url,
                    "title": body.get("title", ""),
                    "headers": body.get("headers", {}),
                    "cookies": body.get("cookies", ""),
                    "referrer": body.get("referrer", ""),
                })
                return self._send_json(200, {"status": "playing"})
            except Exception as exc:
                logger.warning("play handler failed: %s", exc)
                return self._send_json(500, {"error": str(exc)})

        # POST /api/jobs — submit new job
        if path == "/api/jobs":
            body = self._read_body()
            url = body.get("url", "").strip()
            if not url:
                return self._send_json(400, {"error": "Missing 'url'"})
            try:
                job_type = body.get("type", "file")
                if job_type == "youtube":
                    job = engine.add_youtube_job(
                        url=url,
                        filename=body.get("filename", ""),
                        save_path=body.get("save_path", ""),
                        source_url=body.get("source_url", ""),
                    )
                elif job_type in ("hls", "dash"):
                    job = engine.add_stream_job(
                        url=url,
                        filename=body.get("filename", ""),
                        save_path=body.get("save_path", ""),
                        headers=body.get("headers", {}),
                        cookies=body.get("cookies", ""),
                        referrer=body.get("referrer", ""),
                        source_url=body.get("source_url", ""),
                    )
                else:
                    job = engine.add_job(
                        url=url,
                        filename=body.get("filename", ""),
                        save_path=body.get("save_path", ""),
                        headers=body.get("headers", {}),
                        cookies=body.get("cookies", ""),
                        referrer=body.get("referrer", ""),
                        job_type=job_type,
                        source_url=body.get("source_url", ""),
                    )
                return self._send_json(201, job.to_dict())
            except Exception as exc:
                logger.warning("Failed to create job: %s", exc)
                return self._send_json(500, {"error": str(exc)})

        # POST /api/jobs/{id}/{action}
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "jobs":
            job_id = parts[3]
            action = parts[4]
            if action == "pause":
                if engine.pause_job(job_id):
                    return self._send_json(200, {"status": "paused"})
                return self._send_json(404, {"error": "Job not found or not pausable"})
            if action == "resume":
                if engine.resume_job(job_id):
                    return self._send_json(200, {"status": "resumed"})
                return self._send_json(404, {"error": "Job not found or not resumable"})
            if action == "cancel":
                if engine.cancel_job(job_id):
                    return self._send_json(200, {"status": "cancelled"})
                return self._send_json(404, {"error": "Job not found"})
            if action == "retry":
                job = engine.retry_job(job_id)
                if job:
                    return self._send_json(200, job.to_dict())
                return self._send_json(404, {"error": "Job not found"})

        self._send_json(404, {"error": "Not found"})

    def do_DELETE(self) -> None:
        engine = self.server.engine  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = path.split("/")

        # DELETE /api/jobs/{id}
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "jobs":
            if engine.remove_job(parts[3]):
                return self._send_json(200, {"status": "removed"})
            return self._send_json(404, {"error": "Job not found or still active"})

        self._send_json(404, {"error": "Not found"})


class ControlAPI:
    """Loopback HTTP server wrapping the download engine.

    Binds to 127.0.0.1 only (never exposes to the network). Runs in a
    daemon thread so it doesn't block app shutdown."""

    def __init__(self, engine: DownloadEngine, port: int = 53742) -> None:
        self._engine = engine
        self._port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._open_handler = None
        self._play_handler = None

    def set_play_handler(self, handler) -> None:
        """Register a callable(payload: dict) for POST /api/play requests.

        Same threading rules as set_open_handler — marshal to the GUI thread."""
        self._play_handler = handler
        if self._server is not None:
            self._server.play_handler = handler  # type: ignore[attr-defined]

    def set_open_handler(self, handler) -> None:
        """Register a callable(target: str) for POST /api/open requests.

        The handler runs on the HTTP thread — GUI implementations must
        marshal to their main thread (e.g. via a Qt signal)."""
        self._open_handler = handler
        if self._server is not None:
            self._server.open_handler = handler  # type: ignore[attr-defined]

    def start(self) -> None:
        """Start the HTTP server in a background thread."""
        if self._server is not None:
            return
        try:
            self._server = HTTPServer(("127.0.0.1", self._port), _ControlAPIHandler)
            self._server.engine = self._engine  # type: ignore[attr-defined]
            self._server.open_handler = self._open_handler  # type: ignore[attr-defined]
            self._server.play_handler = self._play_handler  # type: ignore[attr-defined]
        except OSError as exc:
            logger.warning("Failed to bind control API on port %d: %s — trying next port", self._port, exc)
            # Try a few alternative ports.
            for alt in range(self._port + 1, self._port + 10):
                try:
                    self._server = HTTPServer(("127.0.0.1", alt), _ControlAPIHandler)
                    self._server.engine = self._engine  # type: ignore[attr-defined]
                    self._server.open_handler = self._open_handler  # type: ignore[attr-defined]
                    self._server.play_handler = self._play_handler  # type: ignore[attr-defined]
                    self._port = alt
                    logger.info("Control API bound to port %d", alt)
                    break
                except OSError:
                    continue
            if self._server is None:
                logger.error("Could not start control API on any port")
                return

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="dl-control-api")
        self._thread.start()
        logger.info("Control API started on 127.0.0.1:%d", self._port)

    def stop(self) -> None:
        """Stop the HTTP server."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("Control API stopped")

    @property
    def port(self) -> int:
        return self._port
