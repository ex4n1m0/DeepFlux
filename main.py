"""CLI REPL for Deeptorrent: natural language torrent control."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.loop import AgentLoop
from agent.tools import ToolRegistry
from config import DeeptorrentConfig, LLM_PROVIDER_PRESETS
from engine import TorrentEngine
from gui import run_gui

logger = logging.getLogger(__name__)


def format_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def format_rate(num: int) -> str:
    if num < 1024:
        return f"{num} B/s"
    return f"{num / 1024:.1f} KB/s"


def print_torrent_table(torrents: List[Dict[str, Any]]) -> None:
    if not torrents:
        print("No torrents.")
        return
    headers = ["Name", "Hash", "State", "Progress", "Down", "Up", "Seeds", "Peers", "Size", "Health"]
    rows = [headers]
    for t in torrents:
        health = "OK" if t.get("num_peers", 0) > 0 or t.get("progress", 0) > 0 else "stalled"
        rows.append(
            [
                t.get("name", "")[:28],
                t.get("info_hash", "")[:12],
                t.get("state", "?"),
                f"{t.get('progress', 0) * 100:.1f}%",
                format_rate(t.get("download_rate", 0)),
                format_rate(t.get("upload_rate", 0)),
                str(t.get("num_seeds", 0)),
                str(t.get("num_peers", 0)),
                format_size(t.get("total_size", 0)),
                health,
            ]
        )
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(headers))]
    for i, row in enumerate(rows):
        print("  ".join(str(cell).ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            print("-" * (sum(widths) + 2 * (len(headers) - 1)))


class Repl:
    def __init__(self, config_path: Optional[str] = None) -> None:
        self.config_path = config_path or DeeptorrentConfig.default_config_path()
        self.config = DeeptorrentConfig.from_file(self.config_path)
        if self.config.llm.api_key or self.config.llm.provider == "custom":
            # A persisted "dummy" (GUI wrote it on a no-key shutdown) heals
            # back to DeepSeek once a key exists again.
            if self.config.llm.provider not in LLM_PROVIDER_PRESETS:
                self.config.llm.provider = "deepseek"
        else:
            self.config.llm.provider = "dummy"
            print("Note: No LLM API key configured. Using demo/placeholder mode.")
        self._streamed = False  # True while reply tokens are streaming to stdout
        try:
            self.engine = TorrentEngine()
            self.engine.start()
            self.tools = ToolRegistry(self.engine, self.config)
            self.agent = AgentLoop(self.engine, self.config, tools=self.tools, on_event=self._on_agent_event)
            if self.config.watchdog.enabled:
                self.agent.start_watchdog()
        except Exception as exc:
            # Fail cleanly with a readable message instead of a raw traceback.
            print(f"Failed to initialize Deeptorrent: {exc}")
            logger.exception("REPL initialization failed")
            raise SystemExit(1) from exc
        # Keep the Jackett source list fresh / start Jackett if needed (no-op
        # when Jackett isn't configured). Background — never blocks the REPL.
        threading.Thread(target=self._jackett_sync, daemon=True).start()
        # Daily-gated yt-dlp freshness probe (outdated = partial downloads).
        threading.Thread(target=self._ytdlp_update_check, daemon=True).start()
        # Anonymous usage ping — live user count on the website; silent.
        try:
            from infra import telemetry
            telemetry.start_heartbeat(self.config,
                                      data_dir=os.path.dirname(self.config_path))
        except Exception:
            logger.debug("telemetry heartbeat failed to start", exc_info=True)

    def _ytdlp_update_check(self) -> None:
        from dlmgr import ytdlp_update
        try:
            result = ytdlp_update.maybe_check_update(self.config, self.config_path)
        except Exception:
            logger.debug("yt-dlp update check failed", exc_info=True)
            return
        if result:
            print(f"\n[yt-dlp] YouTube downloader {result['installed']} is outdated "
                  f"(latest {result['latest']}) — YouTube downloads may fail part-way. "
                  f"Run: python -m pip install --upgrade yt-dlp")

    def _jackett_sync(self) -> None:
        from infra import jackett
        try:
            result = jackett.maybe_auto_sync(self.config, self.config_path)
        except Exception:
            logger.debug("Jackett auto-sync failed", exc_info=True)
            return
        if result and result.get("changed"):
            print(f"\n[Jackett] Synced {result['total']} indexer(s) "
                  f"({result['enabled']} enabled).")

    def run(self) -> None:
        print("Deeptorrent REPL. Type 'help' for hints or 'exit' to quit.")
        while True:
            try:
                user_input = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                break
            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break
            if user_input.lower() == "help":
                self._print_help()
                continue
            self._handle(user_input)
        self.shutdown()

    def _on_agent_event(self, event: dict) -> None:
        """Stream reply tokens and tool activity to the terminal."""
        etype = event.get("type", "")
        if etype == "stream_delta":
            text = event.get("text", "")
            if event.get("kind") == "reasoning":
                # Thinking trace: dim it (ANSI) so it's distinct from the reply.
                print(f"\033[2m{text}\033[0m", end="", flush=True)
            else:
                print(text, end="", flush=True)
            self._streamed = True
        elif etype == "tool_start":
            print(f"\n[tool] {event.get('tool', '?')} {event.get('args', {})}")
        elif etype == "error":
            print(f"\nError: {event.get('message', '')}")

    def _handle(self, user_input: str) -> None:
        user_input = user_input.strip()

        # Direct paste of a magnet link (with or without the 'magnet:' prefix), optionally followed by a category.
        if user_input.startswith(("magnet:", "?xt=urn:btih:")):
            return self._direct_add_magnet(user_input)

        # Direct load of a .torrent file by path, optionally followed by a category.
        path = self._strip_optional_category(user_input)
        if path.lower().endswith(".torrent") and os.path.isfile(path):
            return self._direct_add_torrent_file(user_input)

        # Handle the special "pause everything over X GB" command with direct filtering
        # so it works even in the demo/placeholder LLM mode.
        pause_match = re.match(r"pause everything over (\d+)\s*gb?", user_input, re.IGNORECASE)
        if pause_match:
            threshold_gb = int(pause_match.group(1))
            threshold = threshold_gb * 1024 * 1024 * 1024
            torrents = self.engine.list_torrents()
            paused = []
            for t in torrents:
                if t.get("total_size", 0) > threshold:
                    self.engine.pause(t["info_hash"])
                    paused.append(t["info_hash"])
            print(f"Paused {len(paused)} torrent(s) over {threshold_gb} GB: {paused}")
            print_torrent_table(self.engine.list_torrents())
            return

        self._streamed = False
        try:
            response = self.agent.chat(user_input)
        except Exception as exc:
            print(f"Error: {exc}")
            return

        content = response.get("content", "")
        if self._streamed:
            # Tokens were already printed live — just end the line.
            print()
        elif content:
            print(content)
        if response.get("pending_confirmation"):
            print("Type 'yes' to confirm the pending action(s).")
            return

        print_torrent_table(self.engine.list_torrents())

    @staticmethod
    def _strip_optional_category(user_input: str) -> str:
        m = re.search(r"\bto\s+(Movies|TV|Software|Other)\s*$", user_input, re.IGNORECASE)
        if m:
            return user_input[: m.start()].strip()
        return user_input

    def _extract_category(self, user_input: str) -> tuple[str, str]:
        m = re.search(r"\bto\s+(Movies|TV|Software|Other)\s*$", user_input, re.IGNORECASE)
        if m:
            category = m.group(1).capitalize()
            rest = user_input[: m.start()].strip()
            return rest, category
        return user_input, "Other"

    def _direct_add_magnet(self, user_input: str) -> None:
        uri, category = self._extract_category(user_input)
        try:
            result = self.tools.call("add_magnet", {"uri": uri, "save_path": self.config.default_save_path, "category": category})
            print(f"Added magnet: {result}")
        except Exception as exc:
            print(f"Error: {exc}")
        print_torrent_table(self.engine.list_torrents())

    def _direct_add_torrent_file(self, user_input: str) -> None:
        path, category = self._extract_category(user_input)
        if not os.path.isfile(path):
            print(f"Torrent file not found: {path}")
            return
        try:
            result = self.tools.call("add_torrent_file", {"path": path, "save_path": self.config.default_save_path, "category": category})
            print(f"Added torrent file: {result}")
        except Exception as exc:
            print(f"Error: {exc}")
        print_torrent_table(self.engine.list_torrents())

    def _print_help(self) -> None:
        print(
            "Commands:\n"
            "  <paste a magnet link>\n"
            "  <paste a .torrent file path>\n"
            "  add magnet:?xt=urn:btih:HASH to Movies\n"
            "  add C:\\path\\to\\file.torrent to Software\n"
            "  pause everything over 50GB\n"
            "  list torrents\n"
            "  pause/resume/remove HASH\n"
            "  diagnose HASH\n"
            "  yes  (confirm a pending action)\n"
            "  exit"
        )

    def shutdown(self) -> None:
        try:
            from infra import telemetry
            telemetry.stop_heartbeat()
        except Exception:
            pass
        self.agent.cancel()
        self.agent.stop_watchdog()
        self.tools.shutdown()
        self.engine.stop()
        print("Goodbye.")


def _configure_logging(level: str) -> None:
    """Configure logging to a rotating file in the user's app data dir.

    When the app is built windowed (no console), stdout/stderr are invalid
    handles, so we must not rely on them. We route logs to a file alongside
    the config so the user (and we) can still inspect them when debugging.
    """
    log_dir = os.path.join(os.path.expanduser("~"), ".deeptorrent")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        log_dir = os.path.expanduser("~")
    log_path = os.path.join(log_dir, "deeptorrent.log")

    handlers: list[logging.Handler] = []
    try:
        from logging.handlers import RotatingFileHandler

        handlers.append(
            RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        )
    except Exception:
        # Fallback to a plain file handler if RotatingFileHandler is unavailable.
        try:
            handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
        except Exception:
            pass

    # In a console session (e.g. --cli), also mirror to stdout for convenience.
    try:
        if sys.stdout is not None and sys.stdout.writable():
            handlers.append(logging.StreamHandler(sys.stdout))
    except Exception:
        pass

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers or None,
        force=True,
    )
    logging.getLogger(__name__).info("Logging initialized; log file: %s", log_path)


def _forward_to_running_instance(targets: List[str]) -> bool:
    """Send open targets to an already-running DeepFlux via its control API.

    Returns True if a running instance accepted them (this process should
    exit); False if no instance is running (start the GUI normally)."""
    import json as _json
    from urllib.request import Request, urlopen
    from dlmgr.control_api import load_control_api_token

    token = load_control_api_token()
    if not token:
        return False
    auth = {"Authorization": f"Bearer {token}"}
    for port in range(53742, 53752):
        base = f"http://127.0.0.1:{port}"
        try:
            with urlopen(Request(f"{base}/api/health", headers=auth), timeout=0.5) as resp:
                if resp.status != 200:
                    continue
        except Exception:
            continue
        ok = True
        for t in targets:
            try:
                # Relative paths from Explorer/shell must survive the hop.
                if not t.startswith(("magnet:", "http://", "https://")) and os.path.exists(t):
                    t = os.path.abspath(t)
                req = Request(
                    f"{base}/api/open",
                    data=_json.dumps({"target": t}).encode("utf-8"),
                    headers={"Content-Type": "application/json", **auth},
                    method="POST",
                )
                with urlopen(req, timeout=3) as resp:
                    if resp.status != 200:
                        ok = False
            except Exception:
                ok = False
        return ok
    return False


def main() -> None:
    # Give the built-in Chromium browser the same network privacy features as
    # mainstream browsers: DNS-over-HTTPS via Cloudflare and Encrypted Client
    # Hello (ECH). ISPs/middleboxes that reset TLS connections based on
    # plaintext SNI (and flaky system DNS) break streaming without these.
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") +
        " --enable-features=EncryptedClientHello,DnsOverHttps"
        " --dns-over-https-mode=secure"
        " --dns-over-https-templates=https://cloudflare-dns.com/dns-query"
    ).strip()

    parser = argparse.ArgumentParser(description="DeepFlux")
    parser.add_argument("--config", help="Path to config.json")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Launch the legacy command-line REPL (hidden by default; GUI is the default)",
    )
    parser.add_argument(
        "--native-messaging",
        action="store_true",
        help="Run as a Chrome native messaging host (stdio mode). Launched by the browser extension.",
    )
    parser.add_argument(
        "--register-native-host",
        action="store_true",
        help="Register the native messaging host in the Windows registry and exit.",
    )
    parser.add_argument(
        "--unregister-native-host",
        action="store_true",
        help="Remove the native messaging host registration and exit.",
    )
    parser.add_argument(
        "--extension-id",
        default="",
        help="Chrome extension ID for native messaging host registration (used with --register-native-host).",
    )
    parser.add_argument(
        "--register-associations",
        action="store_true",
        help="Register DeepFlux file/protocol associations (torrent, magnet, media, html) and exit.",
    )
    parser.add_argument(
        "--unregister-associations",
        action="store_true",
        help="Remove DeepFlux file/protocol associations and exit.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Files or URIs to open (.torrent, magnet:, media files, html) — used by file associations.",
    )
    # Chrome's native messaging launches the host as:
    #   Deeptorrent4.exe chrome-extension://<id>/ --parent-window=<hwnd>
    # with no --native-messaging flag, so detect the extension origin in argv.
    args, unknown = parser.parse_known_args()
    chrome_origin = any(a.startswith("chrome-extension://") for a in unknown + args.targets)
    if not args.native_messaging and chrome_origin:
        args.native_messaging = True
        args.targets = []

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    _configure_logging(args.log_level)

    # --- Native messaging host mode (stdio, no GUI) ---
    if args.native_messaging:
        from native_messaging.host import NativeMessagingHost
        native_config = DeeptorrentConfig.from_file(
            args.config or DeeptorrentConfig.default_config_path())
        host = NativeMessagingHost(api_port=native_config.download.control_api_port)
        host.run()
        return

    # --- Register/unregister native messaging host ---
    if args.register_native_host:
        from native_messaging.register import register_native_host
        success = register_native_host(extension_id=args.extension_id)
        print(f"Native host registration: {'success' if success else 'failed'}")
        return
    if args.unregister_native_host:
        from native_messaging.register import unregister_native_host
        success = unregister_native_host()
        print(f"Native host unregistration: {'success' if success else 'failed'}")
        return

    # --- Register/unregister file & protocol associations ---
    if args.register_associations:
        from infra.file_associations import register_associations
        print(f"File associations: {'registered' if register_associations() else 'failed'}")
        return
    if args.unregister_associations:
        from infra.file_associations import unregister_associations
        print(f"File associations: {'removed' if unregister_associations() else 'failed'}")
        return

    # --- Single instance: forward opened files/URIs to a running DeepFlux ---
    if args.targets and _forward_to_running_instance(args.targets):
        return

    # GUI is the default and only user-facing interface. The CLI REPL is still
    # available via --cli for debugging/automation, but the packaged build
    # runs windowed (no console) so the REPL is effectively hidden.
    if not args.cli:
        sys.exit(run_gui(config_path=args.config, open_targets=args.targets))

    repl = Repl(config_path=args.config)
    try:
        repl.run()
    finally:
        repl.shutdown()


if __name__ == "__main__":
    main()
