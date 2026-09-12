"""Tool-calling layer: expose engine and discovery as JSON-schema tools."""
from __future__ import annotations

import binascii
import copy
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import uuid
from dataclasses import dataclass, fields as dataclass_fields, is_dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import httpx
import requests

try:
    from ddgs import DDGS
except ImportError:  # ddgs not installed — DuckDuckGo stage is skipped
    DDGS = None

from config import DeeptorrentConfig, LLM_PROVIDER_PRESETS, source_popularity
from engine import TorrentEngine
from agent.memory import MemoryStore
from agent.rss import RSSMonitor

logger = logging.getLogger(__name__)

# Browser UA — many torrent sites (Cloudflare et al.) block non-browser agents outright.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ToolError(Exception):
    """Raised when a tool call fails."""


@dataclass(frozen=True)
class ToolPolicy:
    effect: str = "reversible"
    confirmation: str = "never"
    watchdog_auto_heal: bool = False
    sensitive_fields: Tuple[str, ...] = ()


READ_ONLY_TOOL_NAMES = frozenset({
    "list_torrents", "get_torrent_status", "get_swarm_stats", "diagnose_swarm",
    "search_indexers", "find_alt_trackers", "find_alt_release", "refresh_tracker_list",
    "web_search", "web_fetch", "list_rss_feeds", "search_memory", "irc_status",
    "irc_list_messages", "irc_search_messages", "list_directory", "iptv_search",
    "iptv_list", "iptv_epg", "iptv_now_playing", "iptv_find_subtitles",
    "irc_list_nicks", "irc_list_channels", "browser_list_tabs", "browser_get_content", "browser_snapshot",
    "browser_wait", "browser_list_bookmarks", "list_downloads", "propose_rename_and_category",
    "analyze_organization", "list_memories", "agent_diagnostics",
    "iptv_list_sources", "list_api_keys", "list_settings", "list_torrent_sources",
})

CONFIRMATION_TOOL_NAMES = frozenset({
    "add_magnet", "add_torrent_file", "add_download", "remove_torrent", "add_tracker",
    "irc_send_message", "irc_join", "irc_part", "irc_connect", "irc_disconnect",
    "irc_send_action", "irc_send_notice", "irc_set_nick", "irc_send_raw",
    "create_folder", "copy_path", "move_path", "rename_path", "delete_path", "iptv_play",
    "cancel_download", "add_rss_feed", "remove_rss_feed", "update_rss_feed", "download_from_feed",
    "browser_click", "browser_fill", "browser_click_ref", "browser_type_ref",
    "browser_select_ref", "browser_check_ref", "browser_close_tab",
    "browser_add_bookmark", "browser_remove_bookmark", "edit_memory", "forget_memory",
    "apply_organization_plan",
    # App setup (user decision 2026-09-12: the agent may set up anything the
    # user could type into a dialog — secrets stay write-only).
    "iptv_add_source", "iptv_update_source", "iptv_remove_source",
    "set_api_key", "set_settings",
    "add_torrent_source", "remove_torrent_source",
    "irc_add_network", "irc_remove_network",
})

WATCHDOG_AUTO_HEAL_TOOL_NAMES = frozenset({
    "add_tracker", "force_reannounce", "force_recheck", "refresh_tracker_list",
    "find_alt_trackers", "find_alt_release", "search_indexers", "diagnose_swarm",
    "get_swarm_stats", "get_torrent_status", "list_torrents",
})

SENSITIVE_TOOL_FIELDS = {
    "browser_fill": ("value",),
    "browser_type_ref": ("value",),
    "irc_send_raw": ("line",),
    "save_memory": ("content",),
    "edit_memory": ("content",),
    "set_api_key": ("value",),
    "iptv_add_source": ("password",),
    "iptv_update_source": ("password",),
    "irc_add_network": ("password", "sasl_password"),
}

UNTRUSTED_RESULT_TOOLS = frozenset({
    "web_search", "web_fetch", "search_indexers", "find_alt_trackers",
    "find_alt_release", "get_rss_feed_items", "irc_list_messages",
    "irc_search_messages", "browser_get_content", "browser_snapshot", "browser_wait",
})


# ---------------------------------------------------------------------------
# App-setup surface (user decision 2026-09-12): the agent may set up anything
# the user could have typed into the app's dialogs. Secrets (API keys,
# passwords) are WRITE-ONLY — settable when the user hands one over or the
# agent legitimately finds one, never readable back into the LLM context.
# ---------------------------------------------------------------------------

# Key/credential slots addressable by set_api_key. Path tuples index into the
# live DeeptorrentConfig. Kept in sync with the slots File → API Keys edits.
API_KEY_SLOTS: Dict[str, Tuple[Tuple[str, ...], str]] = {
    "llm": (("llm", "api_key"), "LLM provider API key — the agent's own brain"),
    "perplexity": (("web_search", "api_key"), "Perplexity web-search API key"),
    "brave": (("web_search", "brave_api_key"), "Brave Search API key"),
    "jackett": (("indexer", "api_key"), "Jackett API key"),
    "tmdb": (("iptv", "tmdb_api_key"), "TMDb key — movie/series posters and info"),
    "omdb": (("iptv", "omdb_api_key"), "OMDb key — metadata fallback"),
    "fanarttv": (("iptv", "fanarttv_api_key"), "Fanart.tv key — backdrops"),
    "tpdb": (("iptv", "tpdb_api_key"), "ThePornDB key — adult VOD metadata"),
    "stashdb": (("iptv", "stashdb_api_key"), "StashDB key — adult VOD metadata"),
    "opensubtitles": (("iptv", "opensubtitles_api_key"), "OpenSubtitles.com API key"),
    "opensubtitles_username": (("iptv", "opensubtitles_username"), "OpenSubtitles account username (raises the daily quota)"),
    "opensubtitles_password": (("iptv", "opensubtitles_password"), "OpenSubtitles account password"),
}

_SECRET_NAME_PARTS = ("api_key", "password", "passwd", "token", "secret", "sasl_account", "username")

# Config sections whose SCALAR fields the agent may read (secrets masked) and
# write via set_settings. Structural lists (iptv.sources, irc.networks,
# rss.feeds, sources.sources) are handled by dedicated tools instead.
_SETTINGS_SECTIONS = (
    "llm", "indexer", "web_search", "watchdog", "rss", "browser",
    "download", "torrents", "iptv", "irc", "voice", "sources",
)

# Explicitly NOT agent-writable even though they are scalars: forced-on
# behavior, internal bookkeeping, or plumbing that generic writes would break.
_SETTINGS_DENY_PATHS = {
    ("llm", "stream"), ("llm", "memory_enabled"),  # from_file forces both True
    ("llm", "memory_dir"),                          # memory lives at a fixed root
    ("sources", "last_jackett_fetch"),              # internal sync bookkeeping
    ("download", "ytdlp_last_check"),               # internal freshness bookkeeping
    ("download", "control_api_port"),               # running server binding
}

# Fields that accept only a fixed set of values — validated on write so a
# typo can't brick a subsystem until the next settings dialog visit.
_SETTING_ENUMS: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("llm", "provider"): ("deepseek", "openrouter", "custom"),
    ("iptv", "vod_group_mode"): ("year", "category"),
    ("iptv", "preferred_player"): ("mpv", "vlc"),
    ("voice", "device"): ("auto", "cpu", "cuda"),
}


def _is_secret_leaf(name: str) -> bool:
    lowered = (name or "").lower()
    return any(part in lowered for part in _SECRET_NAME_PARTS)


def _iter_setting_fields(config: DeeptorrentConfig):
    """Yield (path_tuple, current_value) for every agent-visible scalar
    setting, secrets included (callers mask them). Keeps the whitelist in
    sync with config.py automatically — new scalar fields become
    agent-settable the moment they land in a dataclass."""
    for section_name in _SETTINGS_SECTIONS:
        section = getattr(config, section_name, None)
        if section is None or not is_dataclass(section):
            continue
        for field in dataclass_fields(section):
            path = (section_name, field.name)
            if path in _SETTINGS_DENY_PATHS:
                continue
            value = getattr(section, field.name)
            if isinstance(value, (bool, int, float, str)):
                yield path, value
    for name in ("default_save_path", "log_level"):
        yield (name,), getattr(config, name)


def _mask_setting(value: Any) -> str:
    return "<set>" if value else "<not set>"


def _coerce_setting_value(path: Tuple[str, ...], current: Any, value: Any) -> Any:
    """Coerce the JSON-ish tool argument to the field's Python type."""
    enum_values = _SETTING_ENUMS.get(path)
    if enum_values and isinstance(current, str):
        text = str(value).strip()
        if text not in enum_values:
            raise ToolError(
                f"'{'.'.join(path)}' must be one of: {', '.join(enum_values)}")
        return text
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
        raise ToolError(f"'{'.'.join(path)}' expects true or false")
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ToolError(f"'{'.'.join(path)}' expects a whole number")
    if isinstance(current, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ToolError(f"'{'.'.join(path)}' expects a number")
    if isinstance(current, str):
        return str(value)
    raise ToolError(f"'{'.'.join(path)} has an unsupported type")


def _peek_playlist(url: str, timeout: int = 20) -> Optional[str]:
    """Best-effort M3U payload check before adding an iptv source: follow
    redirects (validated like every agent URL) and stream the first few KB,
    looking for #EXTM3U. Returns an error string, or None when the URL looks
    like a playlist. Network failures count as errors — dead URLs should not
    become sources."""
    from urllib.parse import urljoin

    current = url
    for _ in range(6):
        try:
            current = validate_public_http_url(current)
            response = requests.get(
                current, timeout=timeout, allow_redirects=False, stream=True,
                headers={"User-Agent": BROWSER_UA},
            )
        except (requests.RequestException, ToolError, OSError) as exc:
            return f"request failed ({exc.__class__.__name__})"
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                return "redirect without a destination"
            current = urljoin(current, location)
            continue
        if response.status_code != 200:
            response.close()
            return f"HTTP {response.status_code}"
        chunk = b""
        try:
            for piece in response.iter_content(1024):
                chunk += piece
                if len(chunk) >= 4096:
                    break
        except requests.RequestException:
            return "download interrupted"
        finally:
            response.close()
        head = chunk.decode("utf-8", "ignore").lstrip("\ufeff \t\r\n")
        if head.startswith("#EXTM3U") or "#EXTINF" in head:
            return None
        if head.startswith("<"):
            return "content is HTML/XML, not a playlist (often the EPG link, not the playlist link)"
        return "content did not look like an M3U playlist (#EXTM3U not found)"
    return "too many redirects"


def redact_url_secrets(value: str) -> str:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.query:
        return value
    changed = False
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if any(part in key.lower() for part in ("password", "passphrase", "token", "secret", "key", "auth", "signature")):
            item = "<redacted>"
            changed = True
        query.append((key, item))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)) if changed else value


def redact_tool_arguments(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    sensitive = set(tool_policy(name).sensitive_fields)
    sensitive.update(
        key for key in arguments
        if any(part in key.lower() for part in ("password", "passphrase", "token", "secret", "api_key"))
    )
    return {
        key: "<redacted>" if key in sensitive
        else redact_url_secrets(value) if isinstance(value, str) and key.lower() in ("url", "uri")
        else value
        for key, value in arguments.items()
    }


def redact_sensitive_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if any(
                part in key.lower() for part in ("password", "passphrase", "token", "secret", "api_key")
            ) else redact_url_secrets(item) if isinstance(item, str) and key.lower() in ("url", "uri")
            else redact_sensitive_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    return value


def validate_public_http_url(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ToolError("URL must be a valid http:// or https:// address")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise ToolError("Local and private network URLs are not allowed")
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [literal]
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0].split("%", 1)[0])
                for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise ToolError(f"Could not resolve URL host: {hostname}") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ToolError("Local and private network URLs are not allowed")
    return parsed.geturl()


def public_http_get(url: str, *, timeout: int, headers: Dict[str, str]) -> requests.Response:
    from urllib.parse import urljoin

    current = url
    for _ in range(6):
        current = validate_public_http_url(current)
        response = requests.get(current, timeout=timeout, headers=headers, allow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise ToolError("Redirect response did not include a destination")
        current = urljoin(current, location)
    raise ToolError("Too many redirects")


def tool_policy(name: str) -> ToolPolicy:
    if name in READ_ONLY_TOOL_NAMES:
        return ToolPolicy(effect="read")
    if name in CONFIRMATION_TOOL_NAMES:
        return ToolPolicy(
            effect="external" if name.startswith(("irc_", "browser_")) else "destructive",
            confirmation="always",
            watchdog_auto_heal=name in WATCHDOG_AUTO_HEAL_TOOL_NAMES,
            sensitive_fields=SENSITIVE_TOOL_FIELDS.get(name, ()),
        )
    return ToolPolicy(
        effect="persistent" if name in {"save_memory", "browser_add_bookmark", "browser_remove_bookmark"} else "reversible",
        watchdog_auto_heal=name in WATCHDOG_AUTO_HEAL_TOOL_NAMES,
        sensitive_fields=SENSITIVE_TOOL_FIELDS.get(name, ()),
    )


class ToolRegistry:
    """Registry of JSON-schema tools backed by the TorrentEngine and web/indexer clients."""

    def __init__(self, engine: TorrentEngine, config: DeeptorrentConfig,
                 on_progress: Optional[Callable[[str], None]] = None,
                 dl_engine=None, irc_client=None, iptv_bridge=None,
                 browser_bridge=None) -> None:
        self.engine = engine
        self.config = config
        self.on_progress = on_progress  # sub-step reporter, wired up by AgentLoop
        self._resource_lock = threading.Lock()
        # The IDM-style download manager (dlmgr.DownloadEngine). Injected by
        # the GUI; created lazily on first add_download otherwise (CLI/REPL).
        self._dl_engine = dl_engine
        self._owns_dl_engine = False
        # The embedded IRC client (ircmgr.IRCClientCore). Injected by the GUI
        # (shared with the IRC tab); lazily created on first irc_* tool use
        # otherwise. A lazy client has no connected networks until the user
        # connects from the IRC tab.
        self._irc_client = irc_client
        self._owns_irc_client = False
        # IPTV bridge (gui.iptv_tab.AgentIPTVBridge). Injected by the GUI via
        # set_iptv_bridge() once the Play tab exists — it marshals playback
        # actions onto the Qt thread. Without it (CLI) reads fall back to a
        # lazily created IPTVManager and playback tools report unavailability.
        self._iptv_bridge = iptv_bridge
        self._iptv_manager = None
        self._owns_iptv_manager = False
        # Browser bridge (gui.browser_bridge.BrowserBridge) — injected by the
        # GUI; browser_* tools report unavailability without it (CLI).
        self._browser_bridge = browser_bridge
        self._tools = self._build_tools()
        self._web_search = WebSearchClient(config.web_search)
        self._indexer = TorznabClient(config.indexer)
        self._rss_monitor = RSSMonitor(config.rss)
        # Persistent local memory (shared with AgentLoop for prompt injection).
        self.memory = MemoryStore(config.llm.memory_dir) if config.llm.memory_enabled else None
        # TTL cache for search_indexers — the agent often re-searches query
        # variants across turns; results are side-effect free so reuse is safe.
        self._search_cache: Dict[Tuple[str, str, bool], Tuple[float, Dict[str, Any]]] = {}
        self._search_cache_lock = threading.Lock()

    def list_tools(self, names: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        """Return tool definitions in OpenAI function-calling format."""
        selected = set(names) if names is not None else None
        return [
            tool["schema"] for name, tool in self._tools.items()
            if selected is None or name in selected
        ]

    def tool_names_for_context(self, message: str) -> set[str]:
        text = (message or "").lower()
        all_names = set(self._tools)
        if any(phrase in text for phrase in ("what can you do", "capabilities", "available tools", "agent help")):
            return all_names
        selected = {
            "add_magnet", "add_torrent_file", "add_download", "list_downloads",
            "pause_download", "resume_download", "retry_download", "cancel_download",
            "remove_download", "pause_torrent", "resume_torrent", "remove_torrent",
            "list_torrents", "get_torrent_status", "set_file_priority", "add_tracker",
            "set_torrent_rate_limits", "set_sequential_download", "force_recheck",
            "force_reannounce", "get_swarm_stats", "diagnose_swarm", "refresh_tracker_list",
            "find_alt_trackers", "search_indexers", "find_alt_release",
            "propose_rename_and_category", "analyze_organization", "apply_organization_plan",
            "web_search", "web_fetch", "save_memory",
            "search_memory", "list_memories", "edit_memory", "forget_memory", "agent_diagnostics",
            # Cheap setup reads stay visible so the model knows it CAN set the
            # app up (the mutations themselves are keyword-gated below).
            "iptv_list_sources", "list_api_keys", "list_settings", "list_torrent_sources",
        }
        groups = {
            "rss": {name for name in all_names if name.endswith("rss_feed") or "rss_feed" in name or name == "download_from_feed"},
            "irc": {name for name in all_names if name.startswith("irc_")},
            "files": {"list_directory", "create_folder", "copy_path", "move_path", "rename_path", "delete_path"},
            "iptv": {name for name in all_names if name.startswith("iptv_")},
            "browser": {name for name in all_names if name.startswith("browser_")},
            "setup": {"iptv_list_sources", "iptv_add_source", "iptv_update_source",
                      "iptv_remove_source", "list_api_keys", "set_api_key",
                      "list_settings", "set_settings", "list_torrent_sources",
                      "add_torrent_source", "remove_torrent_source",
                      "irc_add_network", "irc_remove_network"},
        }
        if any(word in text for word in ("rss", "feed", "subscription")):
            selected.update(groups["rss"])
        if any(word in text for word in ("irc", "channel", "nickname", "nick ", "chat room")) or "#" in text:
            selected.update(groups["irc"])
        if any(word in text for word in ("file", "folder", "directory", "path", "rename", "move", "copy", "delete")):
            selected.update(groups["files"])
        if any(word in text for word in ("iptv", "playlist", "m3u", "play", "player", "channel", "epg", "subtitle", "volume", "movie", "series", "episode")):
            selected.update(groups["iptv"])
        if any(word in text for word in ("browser", "browse", "bookmark", "tab", "click", "form", "navigate", "web page", "website", "login", "log in")):
            selected.update(groups["browser"])
        if any(word in text for word in (
            "setting", "settings", "config", "preference", "api key", "apikey",
            "source", "indexer", "provider", "credential",
        )):
            selected.update(groups["setup"])
        return selected & all_names

    def shutdown(self) -> None:
        if self._owns_dl_engine and self._dl_engine is not None:
            try:
                self._dl_engine.stop()
            except Exception:
                logger.debug("owned download engine shutdown failed", exc_info=True)
            self._dl_engine = None
            self._owns_dl_engine = False
        if self._owns_irc_client and self._irc_client is not None:
            try:
                self._irc_client.shutdown()
            except Exception:
                logger.debug("owned IRC client shutdown failed", exc_info=True)
            self._irc_client = None
            self._owns_irc_client = False
        if self._owns_iptv_manager and self._iptv_manager is not None:
            try:
                self._iptv_manager.shutdown()
            except Exception:
                logger.debug("owned IPTV manager shutdown failed", exc_info=True)
            self._iptv_manager = None
            self._owns_iptv_manager = False

    def normalize_arguments(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name not in self._tools:
            raise ToolError(f"Unknown tool: {name}")
        normalized = copy.deepcopy(arguments)
        schema = self._tools[name]["schema"]["function"]["parameters"]
        self._validate_args(normalized, schema)
        return normalized

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Validate, execute and log a tool call; return structured JSON."""
        arguments = self.normalize_arguments(name, arguments)
        tool = self._tools[name]

        logger.info("TOOL_CALL name=%s args=%s", name, json.dumps(redact_tool_arguments(name, arguments)))
        try:
            result = tool["handler"](**arguments)
            if not isinstance(result, dict):
                raise ToolError(f"Tool {name} returned a non-object result")
            result.setdefault("success", not bool(result.get("error")))
            if name in UNTRUSTED_RESULT_TOOLS:
                result.setdefault("_trust", "untrusted_external_content")
            logger.info("TOOL_RESULT name=%s result_keys=%s", name, list(result.keys()))
            return result
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            raise ToolError(f"Tool {name} failed: {exc}") from exc

    def _progress(self, message: str) -> None:
        """Report a sub-step of a long-running tool to the UI (if wired)."""
        if self.on_progress:
            try:
                self.on_progress(message)
            except Exception:
                logger.debug("progress callback failed", exc_info=True)

    # ------------------------------------------------------------------
    # Schema + handlers
    # ------------------------------------------------------------------

    def _build_tools(self) -> Dict[str, Dict[str, Any]]:
        return {
            "agent_diagnostics": {
                "schema": self._tool_schema(
                    name="agent_diagnostics",
                    description="Report Agent capabilities, integration availability, safety policy counts, and execution limits without exposing secrets.",
                    properties={},
                    required=[],
                ),
                "handler": self._agent_diagnostics,
            },
            "add_magnet": {
                "schema": self._tool_schema(
                    name="add_magnet",
                    description="Add a torrent from a magnet URI to the engine.",
                    properties={
                        "uri": {"type": "string", "description": "Magnet URI."},
                        "save_path": {"type": "string", "description": "Directory to save the torrent data."},
                        "category": {
                            "type": "string",
                            "description": "Category for organization (Movies, TV, Software, Other).",
                            "default": "Other",
                        },
                    },
                    required=["uri", "save_path"],
                ),
                "handler": self._add_magnet,
            },
            "add_torrent_file": {
                "schema": self._tool_schema(
                    name="add_torrent_file",
                    description="Add a torrent from a .torrent file. The path may be a local file path or an http(s) download URL (e.g. an indexer/Jackett download link).",
                    properties={
                        "path": {"type": "string", "description": "Local path to the .torrent file, or an http(s) URL that downloads one."},
                        "save_path": {"type": "string", "description": "Directory to save the torrent data."},
                        "category": {
                            "type": "string",
                            "description": "Category for organization.",
                            "default": "Other",
                        },
                    },
                    required=["path", "save_path"],
                ),
                "handler": self._add_torrent_file,
            },
            "add_download": {
                "schema": self._tool_schema(
                    name="add_download",
                    description="Start a direct download in the download manager (segmented, multi-connection, resumable) from a direct file URL. Also captures HLS (.m3u8) and DASH (.mpd) streams — downloaded and remuxed to MP4. Use when the user provides a direct link or when web_fetch reported download_links for a page.",
                    properties={
                        "url": {"type": "string", "description": "Direct file URL (http/https), or an .m3u8/.mpd stream URL."},
                        "filename": {"type": "string", "description": "Optional output filename — defaults to the URL's basename.", "default": ""},
                        "save_path": {"type": "string", "description": "Optional destination folder; defaults to Download Manager settings.", "default": ""},
                    },
                    required=["url"],
                ),
                "handler": self._add_download,
            },
            "list_downloads": {
                "schema": self._tool_schema(
                    name="list_downloads",
                    description="List the download manager's jobs (the Downloads tab): id, filename, "
                    "status, progress, speed, save path. Use this to find job ids for the other "
                    "download tools.",
                    properties={
                        "status": {"type": "string", "enum": ["", "queued", "downloading", "processing", "paused", "completed", "error"], "description": "Filter: queued|downloading|processing|paused|completed|error (default: all).", "default": ""},
                        "offset": {"type": "integer", "minimum": 0, "description": "Zero-based result offset.", "default": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Maximum jobs to return (default 50).", "default": 50},
                    },
                    required=[],
                ),
                "handler": self._list_downloads,
            },
            "pause_download": {
                "schema": self._tool_schema(
                    name="pause_download",
                    description="Pause a running or queued download job.",
                    properties={
                        "job_id": {"type": "string", "description": "Job id (from list_downloads; unique prefix works)."},
                    },
                    required=["job_id"],
                ),
                "handler": self._pause_download,
            },
            "resume_download": {
                "schema": self._tool_schema(
                    name="resume_download",
                    description="Resume a paused or errored download job.",
                    properties={
                        "job_id": {"type": "string", "description": "Job id (unique prefix works)."},
                    },
                    required=["job_id"],
                ),
                "handler": self._resume_download,
            },
            "retry_download": {
                "schema": self._tool_schema(
                    name="retry_download",
                    description="Retry an errored download job (re-queues it).",
                    properties={
                        "job_id": {"type": "string", "description": "Job id (unique prefix works)."},
                    },
                    required=["job_id"],
                ),
                "handler": self._retry_download,
            },
            "cancel_download": {
                "schema": self._tool_schema(
                    name="cancel_download",
                    description="Cancel a download job and remove it from the list. By default the "
                    "partial file is deleted — pass delete_file=false to keep it.",
                    properties={
                        "job_id": {"type": "string", "description": "Job id (unique prefix works)."},
                        "delete_file": {"type": "boolean", "description": "Delete the partial file (default true).", "default": True},
                    },
                    required=["job_id"],
                ),
                "handler": self._cancel_download,
            },
            "remove_download": {
                "schema": self._tool_schema(
                    name="remove_download",
                    description="Remove a completed/errored download job from the list (keeps the "
                    "downloaded file).",
                    properties={
                        "job_id": {"type": "string", "description": "Job id (unique prefix works)."},
                    },
                    required=["job_id"],
                ),
                "handler": self._remove_download,
            },
            "pause_torrent": {
                "schema": self._tool_schema(
                    name="pause_torrent",
                    description="Pause a single torrent by info-hash.",
                    properties={"info_hash": {"type": "string", "description": "Torrent info hash (hex)."}},
                    required=["info_hash"],
                ),
                "handler": self._pause_torrent,
            },
            "resume_torrent": {
                "schema": self._tool_schema(
                    name="resume_torrent",
                    description="Resume a single torrent by info-hash.",
                    properties={"info_hash": {"type": "string", "description": "Torrent info hash (hex)."}},
                    required=["info_hash"],
                ),
                "handler": self._resume_torrent,
            },
            "remove_torrent": {
                "schema": self._tool_schema(
                    name="remove_torrent",
                    description="Remove a torrent from the engine; optionally delete files.",
                    properties={
                        "info_hash": {"type": "string", "description": "Torrent info hash (hex)."},
                        "delete_files": {
                            "type": "boolean",
                            "description": "Whether to delete downloaded files.",
                            "default": False,
                        },
                    },
                    required=["info_hash"],
                ),
                "handler": self._remove_torrent,
            },
            "list_torrents": {
                "schema": self._tool_schema(
                    name="list_torrents",
                    description="List managed torrents, optionally filtered by state, with pagination.",
                    properties={
                        "filter": {
                            "type": "string",
                            "description": "Optional state filter (e.g. downloading, seeding, stalled).",
                            "default": "",
                        },
                        "offset": {"type": "integer", "minimum": 0, "description": "Zero-based result offset.", "default": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Maximum torrents to return (default 50).", "default": 50},
                    },
                    required=[],
                ),
                "handler": self._list_torrents,
            },
            "get_torrent_status": {
                "schema": self._tool_schema(
                    name="get_torrent_status",
                    description="Get detailed status for a single torrent, with a paged file list.",
                    properties={
                        "info_hash": {"type": "string", "description": "Torrent info hash (hex)."},
                        "file_offset": {"type": "integer", "minimum": 0, "description": "Zero-based file offset.", "default": 0},
                        "file_limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Maximum files to return (default 100).", "default": 100},
                    },
                    required=["info_hash"],
                ),
                "handler": self._get_torrent_status,
            },
            "set_file_priority": {
                "schema": self._tool_schema(
                    name="set_file_priority",
                    description="Set a file's download priority (0=off, 1=low, 4=normal, 7=high).",
                    properties={
                        "info_hash": {"type": "string", "description": "Torrent info hash (hex)."},
                        "file_id": {"type": "integer", "description": "File index."},
                        "level": {"type": "integer", "description": "Priority level 0-7.", "minimum": 0, "maximum": 7},
                    },
                    required=["info_hash", "file_id", "level"],
                ),
                "handler": self._set_file_priority,
            },
            "add_tracker": {
                "schema": self._tool_schema(
                    name="add_tracker",
                    description="Add a tracker to a torrent.",
                    properties={
                        "info_hash": {"type": "string", "description": "Torrent info hash (hex)."},
                        "url": {"type": "string", "description": "Tracker announce URL."},
                    },
                    required=["info_hash", "url"],
                ),
                "handler": self._add_tracker,
            },
            "set_torrent_rate_limits": {
                "schema": self._tool_schema(
                    name="set_torrent_rate_limits",
                    description="Set global BitTorrent session rate limits in KB/s (0 = unlimited). "
                    "Applies immediately, at runtime.",
                    properties={
                        "download_kb": {"type": "integer", "description": "Download limit in KB/s (0 = unlimited).", "default": 0},
                        "upload_kb": {"type": "integer", "description": "Upload limit in KB/s (0 = unlimited).", "default": 0},
                    },
                    required=[],
                ),
                "handler": self._set_torrent_rate_limits,
            },
            "set_sequential_download": {
                "schema": self._tool_schema(
                    name="set_sequential_download",
                    description="Toggle sequential piece download for a torrent — needed to stream/"
                    "play a video while it is still downloading.",
                    properties={
                        "info_hash": {"type": "string", "description": "Torrent info hash (hex)."},
                        "on": {"type": "boolean", "description": "Enable (default true).", "default": True},
                    },
                    required=["info_hash"],
                ),
                "handler": self._set_sequential_download,
            },
            "force_recheck": {
                "schema": self._tool_schema(
                    name="force_recheck",
                    description="Force a full hash re-check of a torrent's files on disk (use after "
                    "files were moved or modified outside the app).",
                    properties={"info_hash": {"type": "string", "description": "Torrent info hash (hex)."}},
                    required=["info_hash"],
                ),
                "handler": self._force_recheck,
            },
            "force_reannounce": {
                "schema": self._tool_schema(
                    name="force_reannounce",
                    description="Force an immediate tracker reannounce for a torrent — helps find "
                    "peers when a swarm looks stalled.",
                    properties={"info_hash": {"type": "string", "description": "Torrent info hash (hex)."}},
                    required=["info_hash"],
                ),
                "handler": self._force_reannounce,
            },
            "get_swarm_stats": {
                "schema": self._tool_schema(
                    name="get_swarm_stats",
                    description="Get raw swarm statistics for a torrent.",
                    properties={"info_hash": {"type": "string", "description": "Torrent info hash (hex)."}},
                    required=["info_hash"],
                ),
                "handler": self._get_swarm_stats,
            },
            "diagnose_swarm": {
                "schema": self._tool_schema(
                    name="diagnose_swarm",
                    description="Diagnose the health of a torrent swarm and return a structured summary.",
                    properties={"info_hash": {"type": "string", "description": "Torrent info hash (hex)."}},
                    required=["info_hash"],
                ),
                "handler": self._diagnose_swarm,
            },
            "refresh_tracker_list": {
                "schema": self._tool_schema(
                    name="refresh_tracker_list",
                    description="Fetch curated public tracker lists and return deduplicated URLs.",
                    properties={},
                    required=[],
                ),
                "handler": self._refresh_tracker_list,
            },
            "find_alt_trackers": {
                "schema": self._tool_schema(
                    name="find_alt_trackers",
                    description="Search the web for niche/updated trackers specific to a torrent name.",
                    properties={"torrent_name": {"type": "string", "description": "Name of the torrent to search for."}},
                    required=["torrent_name"],
                ),
                "handler": self._find_alt_trackers,
            },
            "search_indexers": {
                "schema": self._tool_schema(
                    name="search_indexers",
                    description="Search configured Torznab-compatible indexers for a better-seeded copy. "
                    "By default runs a QUICK pass over the top source(s) only — fast. "
                    "Pass deep=true for a full sweep of every enabled source (slower). "
                    "If a quick pass returns nothing, the deep sweep runs automatically. "
                    "Large result sets are cached locally and returned in pages — pass offset "
                    "for the next page without re-querying indexers. If the result has an "
                    "'error' field, the indexers failed to respond (retry shortly) — that is "
                    "not the same as zero hits.",
                    properties={
                        "query": {"type": "string", "description": "Search query."},
                        "category": {"type": "string", "description": "Optional Torznab category ID.", "default": ""},
                        "deep": {
                            "type": "boolean",
                            "description": "Full sweep of all sources. Default false = quick pass on top source(s) only.",
                            "default": False,
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Page offset into the locally cached result set (default 0). Paging never re-queries indexers.",
                            "default": 0,
                        },
                    },
                    required=["query"],
                ),
                "handler": self._search_indexers,
            },
            "find_alt_release": {
                "schema": self._tool_schema(
                    name="find_alt_release",
                    description="Web search fallback for alternate uploads/release groups.",
                    properties={"torrent_name": {"type": "string", "description": "Name of the torrent to search for."}},
                    required=["torrent_name"],
                ),
                "handler": self._find_alt_release,
            },
            "propose_rename_and_category": {
                "schema": self._tool_schema(
                    name="propose_rename_and_category",
                    description="Compatibility alias for analyze_organization. Builds a read-only organization proposal.",
                    properties={
                        "info_hash": {"type": "string", "description": "Torrent info hash (hex)."},
                        "categories": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Candidate categories.",
                            "default": ["Movies", "TV", "Software", "Other"],
                        },
                    },
                    required=["info_hash"],
                ),
                "handler": self._propose_rename_and_category,
            },
            "analyze_organization": {
                "schema": self._tool_schema(
                    name="analyze_organization",
                    description="Build a read-only organization proposal for a completed torrent. Review the returned destination and files before applying.",
                    properties={
                        "info_hash": {"type": "string", "description": "Torrent info hash (hex)."},
                        "categories": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Allowed categories.",
                            "default": ["Movies", "TV", "Software", "Other"],
                        },
                    },
                    required=["info_hash"],
                ),
                "handler": self._analyze_organization,
            },
            "apply_organization_plan": {
                "schema": self._tool_schema(
                    name="apply_organization_plan",
                    description="Apply an explicitly reviewed organization plan through libtorrent. The torrent must be complete and confirmation is required.",
                    properties={
                        "info_hash": {"type": "string", "description": "Torrent info hash (hex)."},
                        "category": {"type": "string", "minLength": 1, "description": "New category."},
                        "destination": {"type": "string", "minLength": 1, "description": "Exact destination directory shown to the user."},
                        "file_renames": {
                            "type": "array",
                            "maxItems": 500,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "file_id": {"type": "integer", "minimum": 0},
                                    "new_path": {"type": "string", "minLength": 1},
                                },
                                "required": ["file_id", "new_path"],
                                "additionalProperties": False,
                            },
                            "description": "Optional relative file paths keyed by file_id.",
                            "default": [],
                        },
                    },
                    required=["info_hash", "category", "destination"],
                ),
                "handler": self._apply_organization_plan,
            },
            "web_search": {
                "schema": self._tool_schema(
                    name="web_search",
                    description="Search the web (DuckDuckGo, Brave and Perplexity queried in "
                    "parallel — keyed providers join when their API keys are configured) "
                    "for any query. Use this to find torrent releases, tracker lists, release info, "
                    "reviews, or anything else on the public web. When the user's request is vague "
                    "or descriptive, call this FIRST to identify the exact title/year/version, then "
                    "search_indexers with the refined terms. Results from all providers are merged "
                    "and deduped; when Perplexity is configured an 'answer' field carries a "
                    "synthesized summary of the findings.",
                    properties={
                        "query": {"type": "string", "description": "Search query."},
                        "limit": {"type": "integer", "description": "Max results (default 10).", "default": 10},
                    },
                    required=["query"],
                ),
                "handler": self._web_search_tool,
            },
            "web_fetch": {
                "schema": self._tool_schema(
                    name="web_fetch",
                    description="Fetch the text content of one or more web pages. Use this to read the "
                    "full content of pages found via web_search (e.g. a torrent site, a forum "
                    "post, a tracker list, or any public web page). When you need to read "
                    "several pages, ALWAYS pass them together in 'urls' — they are fetched "
                    "in parallel in a single call, which is much faster than one call per page. "
                    "Results may include magnets, torrent_urls and download_links (direct "
                    "file/stream URLs) extracted from the page HTML — ready for add_magnet / "
                    "add_torrent_file / add_download.",
                    properties={
                        "url": {"type": "string", "description": "Full URL to fetch (single page)."},
                        "urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Multiple URLs to fetch in parallel. Prefer this over repeated single-url calls.",
                        },
                        "max_chars": {"type": "integer", "description": "Max characters to return per page (default 8000).", "default": 8000, "minimum": 500, "maximum": 50000},
                    },
                    required=[],
                    any_of=[["url"], ["urls"]],
                ),
                "handler": self._web_fetch_tool,
            },
            "list_rss_feeds": {
                "schema": self._tool_schema(
                    name="list_rss_feeds",
                    description="List all configured RSS feed subscriptions with their mode "
                    "(monitor = load only, auto_download = download all new items).",
                    properties={},
                    required=[],
                ),
                "handler": self._list_rss_feeds,
            },
            "get_rss_feed_items": {
                "schema": self._tool_schema(
                    name="get_rss_feed_items",
                    description="Fetch and return items from an RSS feed. Returns new items not "
                    "yet seen. Use this when the user asks about their feeds or wants to see "
                    "what's available. Items may include magnet_uri or torrent_url fields.",
                    properties={
                        "feed_url": {"type": "string", "description": "URL of the RSS feed to fetch. Use list_rss_feeds first to see available feeds."},
                        "include_seen": {"type": "boolean", "description": "If true, return all items including previously seen ones. Default false (new only).", "default": False},
                        "offset": {"type": "integer", "minimum": 0, "description": "Zero-based item offset.", "default": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Maximum items to return (default 40).", "default": 40},
                    },
                    required=["feed_url"],
                ),
                "handler": self._get_rss_feed_items,
            },
            "download_from_feed": {
                "schema": self._tool_schema(
                    name="download_from_feed",
                    description="Download specific items from an RSS feed. Call get_rss_feed_items "
                    "first, then pass its stable item_id values. Positional item_indices remain "
                    "available for compatibility but item_ids are safer across feed refreshes.",
                    properties={
                        "feed_url": {"type": "string", "description": "URL of the RSS feed."},
                        "item_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Stable item_id values returned by get_rss_feed_items.",
                        },
                        "item_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Legacy indices into the current new-items list.",
                        },
                        "category": {"type": "string", "description": "Category for the downloads.", "default": "Other"},
                    },
                    required=["feed_url"],
                    any_of=[["item_ids"], ["item_indices"]],
                ),
                "handler": self._download_from_feed,
            },
            "add_rss_feed": {
                "schema": self._tool_schema(
                    name="add_rss_feed",
                    description="Subscribe to a new RSS feed. 'monitor' mode just loads items; "
                    "'auto_download' automatically downloads every new item (use with care).",
                    properties={
                        "url": {"type": "string", "description": "Feed URL (RSS/Atom)."},
                        "name": {"type": "string", "description": "Display name (default: the URL).", "default": ""},
                        "mode": {"type": "string", "description": "monitor | auto_download (default monitor).", "default": "monitor"},
                        "category": {"type": "string", "description": "Category for auto-downloaded items.", "default": "Other"},
                    },
                    required=["url"],
                ),
                "handler": self._add_rss_feed,
            },
            "remove_rss_feed": {
                "schema": self._tool_schema(
                    name="remove_rss_feed",
                    description="Unsubscribe from an RSS feed by URL.",
                    properties={
                        "url": {"type": "string", "description": "The feed URL to remove."},
                    },
                    required=["url"],
                ),
                "handler": self._remove_rss_feed,
            },
            "update_rss_feed": {
                "schema": self._tool_schema(
                    name="update_rss_feed",
                    description="Update the name, mode, or category of an existing RSS subscription.",
                    properties={
                        "url": {"type": "string", "description": "Current subscribed feed URL."},
                        "name": {"type": "string", "description": "New display name."},
                        "mode": {"type": "string", "enum": ["monitor", "auto_download"], "description": "New feed mode."},
                        "category": {"type": "string", "description": "New download category."},
                    },
                    required=["url"],
                ),
                "handler": self._update_rss_feed,
            },
            "save_memory": {
                "schema": self._tool_schema(
                    name="save_memory",
                    description="Save a durable memory to local disk so you remember it in future "
                    "sessions. Use scope 'user' for stable user preferences/profile facts (write "
                    "them as imperative directives, e.g. 'Prefers 1080p releases'), 'fact' for "
                    "durable facts/decisions/lessons, 'note' for a dated working note about what "
                    "happened today. Do NOT save transient task state or secrets.",
                    properties={
                        "content": {"type": "string", "description": "One concise line to remember."},
                        "scope": {
                            "type": "string",
                            "enum": ["user", "fact", "note"],
                            "description": "user = preference/profile, fact = durable knowledge, note = dated daily note.",
                            "default": "fact",
                        },
                    },
                    required=["content"],
                ),
                "handler": self._save_memory_tool,
            },
            "search_memory": {
                "schema": self._tool_schema(
                    name="search_memory",
                    description="Search your persistent local memory (user preferences, remembered "
                    "facts, daily notes) from previous sessions. Use this to recall what the user "
                    "told you before, before asking them again.",
                    properties={
                        "query": {"type": "string", "description": "Keywords to search memory for."},
                    },
                    required=["query"],
                ),
                "handler": self._search_memory_tool,
            },
            "list_memories": {
                "schema": self._tool_schema(
                    name="list_memories",
                    description="List saved memories with stable ids so the user can review, edit, or forget them.",
                    properties={
                        "scope": {"type": "string", "enum": ["", "user", "fact", "note"], "description": "Optional memory scope.", "default": ""},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Maximum entries (default 200).", "default": 200},
                    },
                    required=[],
                ),
                "handler": self._list_memories_tool,
            },
            "edit_memory": {
                "schema": self._tool_schema(
                    name="edit_memory",
                    description="Replace a saved memory by id. Requires confirmation because it changes persistent memory.",
                    properties={
                        "memory_id": {"type": "string", "description": "Stable id from list_memories or search_memory."},
                        "content": {"type": "string", "minLength": 1, "description": "Replacement memory text without credentials or secrets."},
                    },
                    required=["memory_id", "content"],
                ),
                "handler": self._edit_memory_tool,
            },
            "forget_memory": {
                "schema": self._tool_schema(
                    name="forget_memory",
                    description="Permanently remove a saved memory by id. Requires confirmation.",
                    properties={
                        "memory_id": {"type": "string", "description": "Stable id from list_memories or search_memory."},
                    },
                    required=["memory_id"],
                ),
                "handler": self._forget_memory_tool,
            },
            "irc_status": {
                "schema": self._tool_schema(
                    name="irc_status",
                    description="Show IRC connection state: connected networks, your nickname, "
                    "joined channels with user counts, and how many messages are buffered. "
                    "Use before other irc_* tools when unsure what's connected.",
                    properties={},
                    required=[],
                ),
                "handler": self._irc_status,
            },
            "irc_list_messages": {
                "schema": self._tool_schema(
                    name="irc_list_messages",
                    description="Read recent messages from an IRC channel (or a network's server "
                    "buffer when channel is omitted). The client keeps a rolling buffer of every "
                    "open channel, so this answers 'what's happening in #chan?' or 'what did they "
                    "say about X lately?' without joining anything new.",
                    properties={
                        "channel": {"type": "string", "description": "Channel name (e.g. #linux). Omit for the server buffer."},
                        "network": {"type": "string", "description": "Network id (from irc_status). Optional when unambiguous.", "default": ""},
                        "limit": {"type": "integer", "description": "Max messages (default 50).", "default": 50},
                        "minutes": {"type": "integer", "description": "Only messages from the last N minutes (0 = all buffered).", "default": 0},
                    },
                    required=[],
                ),
                "handler": self._irc_list_messages,
            },
            "irc_search_messages": {
                "schema": self._tool_schema(
                    name="irc_search_messages",
                    description="Search the buffered IRC messages of all open channels (or one "
                    "channel) for a keyword — e.g. 'did anyone mention <release name>?'",
                    properties={
                        "query": {"type": "string", "description": "Case-insensitive text to find in messages or nicks."},
                        "channel": {"type": "string", "description": "Restrict to one channel.", "default": ""},
                        "network": {"type": "string", "description": "Network id (from irc_status). Optional.", "default": ""},
                        "limit": {"type": "integer", "description": "Max hits (default 30).", "default": 30},
                    },
                    required=["query"],
                ),
                "handler": self._irc_search_messages,
            },
            "irc_send_message": {
                "schema": self._tool_schema(
                    name="irc_send_message",
                    description="Send a message to an IRC channel or user. This posts publicly "
                    "under the user's nickname — always confirm the exact text with the user first.",
                    properties={
                        "target": {"type": "string", "description": "Channel (#chan) or nickname."},
                        "text": {"type": "string", "description": "Message text."},
                        "network": {"type": "string", "description": "Network id (from irc_status). Optional when unambiguous.", "default": ""},
                    },
                    required=["target", "text"],
                ),
                "handler": self._irc_send_message,
            },
            "irc_join": {
                "schema": self._tool_schema(
                    name="irc_join",
                    description="Join an IRC channel on a connected network. Once joined, its "
                    "messages are buffered and can be read with irc_list_messages.",
                    properties={
                        "channel": {"type": "string", "description": "Channel to join (e.g. #linux)."},
                        "network": {"type": "string", "description": "Network id (from irc_status). Optional when unambiguous.", "default": ""},
                    },
                    required=["channel"],
                ),
                "handler": self._irc_join,
            },
            "irc_part": {
                "schema": self._tool_schema(
                    name="irc_part",
                    description="Leave an IRC channel; its buffer stops updating.",
                    properties={
                        "channel": {"type": "string", "description": "Channel to leave."},
                        "network": {"type": "string", "description": "Network id (from irc_status). Optional when unambiguous.", "default": ""},
                    },
                    required=["channel"],
                ),
                "handler": self._irc_part,
            },
            "irc_connect": {
                "schema": self._tool_schema(
                    name="irc_connect",
                    description="Connect to a configured IRC network (from the IRC tab's network list).",
                    properties={
                        "network": {"type": "string", "description": "Network id (from irc_status/config). Optional when only one is configured.", "default": ""},
                    },
                    required=[],
                ),
                "handler": self._irc_connect,
            },
            "irc_disconnect": {
                "schema": self._tool_schema(
                    name="irc_disconnect",
                    description="Disconnect from an IRC network.",
                    properties={
                        "network": {"type": "string", "description": "Network id. Optional when unambiguous.", "default": ""},
                        "message": {"type": "string", "description": "Quit message.", "default": ""},
                    },
                    required=[],
                ),
                "handler": self._irc_disconnect,
            },
            "irc_send_action": {
                "schema": self._tool_schema(
                    name="irc_send_action",
                    description="Send a /me action to a channel or nick (e.g. '* nick waves').",
                    properties={
                        "target": {"type": "string", "description": "Channel or nick."},
                        "text": {"type": "string", "description": "Action text."},
                        "network": {"type": "string", "description": "Network id. Optional when unambiguous.", "default": ""},
                    },
                    required=["target", "text"],
                ),
                "handler": self._irc_send_action,
            },
            "irc_send_notice": {
                "schema": self._tool_schema(
                    name="irc_send_notice",
                    description="Send an IRC notice (low-priority message) to a channel or nick.",
                    properties={
                        "target": {"type": "string", "description": "Channel or nick."},
                        "text": {"type": "string", "description": "Notice text."},
                        "network": {"type": "string", "description": "Network id. Optional when unambiguous.", "default": ""},
                    },
                    required=["target", "text"],
                ),
                "handler": self._irc_send_notice,
            },
            "irc_set_nick": {
                "schema": self._tool_schema(
                    name="irc_set_nick",
                    description="Change your nickname on an IRC network.",
                    properties={
                        "new_nick": {"type": "string", "description": "The new nickname."},
                        "network": {"type": "string", "description": "Network id. Optional when unambiguous.", "default": ""},
                    },
                    required=["new_nick"],
                ),
                "handler": self._irc_set_nick,
            },
            "irc_send_raw": {
                "schema": self._tool_schema(
                    name="irc_send_raw",
                    description="Send a raw IRC protocol line (like /raw or /quote in the IRC tab). "
                    "Powerful — only use when no dedicated tool covers the task.",
                    properties={
                        "line": {"type": "string", "description": "Raw IRC command line (e.g. 'MODE #chan +o nick')."},
                        "network": {"type": "string", "description": "Network id. Optional when unambiguous.", "default": ""},
                    },
                    required=["line"],
                ),
                "handler": self._irc_send_raw,
            },
            "irc_list_channels": {
                "schema": self._tool_schema(
                    name="irc_list_channels",
                    description="List channels on an IRC network (server LIST reply), sorted by user "
                    "count. A refresh queues LIST and returns immediately with the current cache; "
                    "call again with refresh=false to read the completed reply.",
                    properties={
                        "network": {"type": "string", "description": "Network id. Optional when unambiguous.", "default": ""},
                        "filter": {"type": "string", "description": "Optional LIST mask, e.g. '#python*'.", "default": ""},
                        "refresh": {"type": "boolean", "description": "Send a fresh LIST to the server (default true).", "default": True},
                        "limit": {"type": "integer", "description": "Max channels (default 50).", "default": 50},
                    },
                    required=[],
                ),
                "handler": self._irc_list_channels,
            },
            "irc_list_nicks": {
                "schema": self._tool_schema(
                    name="irc_list_nicks",
                    description="List the users currently in a joined IRC channel (@ = op, + = voice).",
                    properties={
                        "channel": {"type": "string", "description": "Joined channel name."},
                        "network": {"type": "string", "description": "Network id. Optional when unambiguous.", "default": ""},
                    },
                    required=["channel"],
                ),
                "handler": self._irc_list_nicks,
            },
            # --- Filesystem (the Command tab's domain — agent-level file ops) ---
            "list_directory": {
                "schema": self._tool_schema(
                    name="list_directory",
                    description="List a local directory's entries (name, type, size, modified time). "
                    "Use this to browse the user's downloads or any folder before file operations.",
                    properties={
                        "path": {"type": "string", "description": "Directory path to list."},
                        "limit": {"type": "integer", "description": "Max entries (default 200).", "default": 200},
                    },
                    required=["path"],
                ),
                "handler": self._list_directory,
            },
            "create_folder": {
                "schema": self._tool_schema(
                    name="create_folder",
                    description="Create a folder (including missing parents).",
                    properties={
                        "path": {"type": "string", "description": "Folder path to create."},
                    },
                    required=["path"],
                ),
                "handler": self._create_folder,
            },
            "copy_path": {
                "schema": self._tool_schema(
                    name="copy_path",
                    description="Copy a file or folder to a destination path.",
                    properties={
                        "source": {"type": "string", "description": "Existing file/folder path."},
                        "destination": {"type": "string", "description": "Destination path (file path, or existing directory to copy into)."},
                        "overwrite": {"type": "boolean", "description": "Replace an existing destination (default false).", "default": False},
                    },
                    required=["source", "destination"],
                ),
                "handler": self._copy_path,
            },
            "move_path": {
                "schema": self._tool_schema(
                    name="move_path",
                    description="Move a file or folder to a destination path.",
                    properties={
                        "source": {"type": "string", "description": "Existing file/folder path."},
                        "destination": {"type": "string", "description": "Destination path (file path, or existing directory to move into)."},
                        "overwrite": {"type": "boolean", "description": "Replace an existing destination (default false).", "default": False},
                    },
                    required=["source", "destination"],
                ),
                "handler": self._move_path,
            },
            "rename_path": {
                "schema": self._tool_schema(
                    name="rename_path",
                    description="Rename a file or folder in place (new name, same directory).",
                    properties={
                        "path": {"type": "string", "description": "Existing file/folder path."},
                        "new_name": {"type": "string", "description": "New name (not a full path)."},
                    },
                    required=["path", "new_name"],
                ),
                "handler": self._rename_path,
            },
            "delete_path": {
                "schema": self._tool_schema(
                    name="delete_path",
                    description="Delete a file or folder. Folders require recursive=true unless empty. "
                    "This is permanent — always confirm the exact path with the user first.",
                    properties={
                        "path": {"type": "string", "description": "File/folder path to delete."},
                        "recursive": {"type": "boolean", "description": "Delete a non-empty folder and everything inside it (default false).", "default": False},
                    },
                    required=["path"],
                ),
                "handler": self._delete_path,
            },
            # --- IPTV (the Play tab) ---
            "iptv_search": {
                "schema": self._tool_schema(
                    name="iptv_search",
                    description="Search the loaded IPTV playlist (Play tab) across live channels, "
                    "movies and series. Returns matching items with ids usable by iptv_play.",
                    properties={
                        "query": {"type": "string", "description": "Name or category text to search for."},
                        "section": {"type": "string", "description": "Restrict to 'live', 'movies' or 'series' (default: all).", "default": ""},
                        "limit": {"type": "integer", "description": "Max results per section (default 20).", "default": 20},
                    },
                    required=["query"],
                ),
                "handler": self._iptv_search,
            },
            "iptv_list": {
                "schema": self._tool_schema(
                    name="iptv_list",
                    description="List IPTV content from the loaded playlist (Play tab). Without a "
                    "category, also returns the available categories for the section.",
                    properties={
                        "section": {"type": "string", "description": "'live', 'movies' or 'series'.", "default": "live"},
                        "category": {"type": "string", "description": "Category/group filter (default: all).", "default": ""},
                        "limit": {"type": "integer", "description": "Max items (default 50).", "default": 50},
                    },
                    required=[],
                ),
                "handler": self._iptv_list,
            },
            "iptv_epg": {
                "schema": self._tool_schema(
                    name="iptv_epg",
                    description="Show what's on now and next for a live IPTV channel (EPG), when the "
                    "playlist provides guide data.",
                    properties={
                        "channel": {"type": "string", "description": "Channel name (from iptv_search/iptv_list)."},
                    },
                    required=["channel"],
                ),
                "handler": self._iptv_epg,
            },
            "iptv_now_playing": {
                "schema": self._tool_schema(
                    name="iptv_now_playing",
                    description="Show the Play tab's player state: current title, playing/paused/"
                    "stopped, position/duration, volume and mute.",
                    properties={},
                    required=[],
                ),
                "handler": self._iptv_now_playing,
            },
            "iptv_find_subtitles": {
                "schema": self._tool_schema(
                    name="iptv_find_subtitles",
                    description="Search OpenSubtitles.com for subtitles for the video currently "
                    "playing in the Play tab (or an explicit title query). Local files are "
                    "matched by exact file hash first — results with hash_match=true are "
                    "guaranteed in sync. Requires an OpenSubtitles API key in IPTV Settings.",
                    properties={
                        "query": {"type": "string", "description": "Title to search (default: the current video's title).", "default": ""},
                        "languages": {"type": "string", "description": "Comma-separated ISO codes (default: the user's preferred subtitle language, else 'en').", "default": ""},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Maximum subtitle matches (default 20).", "default": 20},
                    },
                    required=[],
                ),
                "handler": self._iptv_find_subtitles,
            },
            "iptv_load_subtitle": {
                "schema": self._tool_schema(
                    name="iptv_load_subtitle",
                    description="Download a subtitle from OpenSubtitles and load it into the "
                    "playing video. Pass file_id from iptv_find_subtitles, or omit it to "
                    "auto-pick the best match (hash match > preferred language > most "
                    "downloaded) for the currently playing video.",
                    properties={
                        "file_id": {"type": "integer", "description": "OpenSubtitles file_id from iptv_find_subtitles (omit to auto-pick).", "default": 0},
                        "query": {"type": "string", "description": "Title search for auto-pick (default: current video title).", "default": ""},
                        "languages": {"type": "string", "description": "ISO codes for auto-pick (default: preferred subtitle language, else 'en').", "default": ""},
                    },
                    required=[],
                ),
                "handler": self._iptv_load_subtitle,
            },
            "iptv_play": {
                "schema": self._tool_schema(
                    name="iptv_play",
                    description="Play content in the Play tab. Provide exactly one of: item_id (from "
                    "iptv_search/iptv_list), query (plays the best playlist match by name), url "
                    "(direct stream/HLS/DASH), or file (local media path). Series can't be played "
                    "directly — name a specific episode in query instead.",
                    properties={
                        "item_id": {"type": "string", "description": "Item id from iptv_search/iptv_list.", "default": ""},
                        "query": {"type": "string", "description": "Name to match in the playlist (best hit plays).", "default": ""},
                        "url": {"type": "string", "description": "Direct stream URL to play.", "default": ""},
                        "title": {"type": "string", "description": "Display title for url playback.", "default": ""},
                        "file": {"type": "string", "description": "Local media file path to play.", "default": ""},
                    },
                    required=[],
                    any_of=[["item_id"], ["query"], ["url"], ["file"]],
                ),
                "handler": self._iptv_play,
            },
            "iptv_pause": {
                "schema": self._tool_schema(
                    name="iptv_pause",
                    description="Toggle pause/resume in the Play tab's player.",
                    properties={},
                    required=[],
                ),
                "handler": self._iptv_pause,
            },
            "iptv_stop": {
                "schema": self._tool_schema(
                    name="iptv_stop",
                    description="Stop playback in the Play tab.",
                    properties={},
                    required=[],
                ),
                "handler": self._iptv_stop,
            },
            "iptv_set_volume": {
                "schema": self._tool_schema(
                    name="iptv_set_volume",
                    description="Set the Play tab's player volume (0-100).",
                    properties={
                        "level": {"type": "integer", "description": "Volume 0-100."},
                    },
                    required=["level"],
                ),
                "handler": self._iptv_set_volume,
            },
            # --- App setup: IPTV sources (anything the user could add in the GUI) ---
            "iptv_list_sources": {
                "schema": self._tool_schema(
                    name="iptv_list_sources",
                    description="List the configured IPTV playlist sources (Play tab): id, name, "
                    "type, URL, EPG URL and enabled state. Ids/names feed iptv_update_source "
                    "and iptv_remove_source. Credentials are never included.",
                    properties={},
                    required=[],
                ),
                "handler": self._iptv_list_sources,
            },
            "iptv_add_source": {
                "schema": self._tool_schema(
                    name="iptv_add_source",
                    description="Add an IPTV playlist source to the Play tab (like the GUI's "
                    "Settings → Playlist Sources). For m3u_url the URL is validated first — "
                    "it must serve a real #EXTM3U playlist (dead links and EPG/XML links are "
                    "rejected). The Play tab starts loading it immediately. Xtream logins need "
                    "username/password; local_folder takes a directory path.",
                    properties={
                        "url": {"type": "string", "description": "Playlist URL (m3u_url/xtream) or folder/file path (local_folder/m3u_file)."},
                        "name": {"type": "string", "description": "Display name (default: the hostname).", "default": ""},
                        "kind": {"type": "string", "description": "'m3u_url' (default), 'xtream', 'local_folder' or 'm3u_file'.", "default": "m3u_url"},
                        "epg_url": {"type": "string", "description": "Optional XMLTV EPG URL for this source.", "default": ""},
                        "username": {"type": "string", "description": "Xtream Codes username (xtream only).", "default": ""},
                        "password": {"type": "string", "description": "Xtream Codes password (xtream only).", "default": ""},
                        "user_agent": {"type": "string", "description": "Custom User-Agent header for playlist/stream requests.", "default": ""},
                        "referer": {"type": "string", "description": "Custom Referer header for playlist/stream requests.", "default": ""},
                        "enabled": {"type": "boolean", "description": "Whether the source loads (default true).", "default": True},
                        "auto_refresh_minutes": {"type": "integer", "description": "Auto-refresh interval in minutes; 0 = manual only.", "default": 0},
                        "validate": {"type": "boolean", "description": "For m3u_url: fetch the first bytes and require #EXTM3U (default true).", "default": True},
                    },
                    required=["url"],
                ),
                "handler": self._iptv_add_source,
            },
            "iptv_update_source": {
                "schema": self._tool_schema(
                    name="iptv_update_source",
                    description="Update an existing IPTV source. Only the fields you pass "
                    "change — omit everything else to leave it untouched.",
                    properties={
                        "source": {"type": "string", "description": "Source id, id prefix or name (from iptv_list_sources)."},
                        "name": {"type": "string", "description": "New display name (omit to keep)."},
                        "url": {"type": "string", "description": "New playlist URL (omit to keep)."},
                        "epg_url": {"type": "string", "description": "New XMLTV EPG URL; pass an empty string to clear it."},
                        "enabled": {"type": "boolean", "description": "Enable/disable the source (omit to keep)."},
                        "username": {"type": "string", "description": "Xtream username (omit to keep)."},
                        "password": {"type": "string", "description": "Xtream password (omit to keep)."},
                        "user_agent": {"type": "string", "description": "Custom User-Agent header (omit to keep)."},
                        "referer": {"type": "string", "description": "Custom Referer header (omit to keep)."},
                        "auto_refresh_minutes": {"type": "integer", "description": "Auto-refresh interval in minutes; 0 = manual only (omit to keep)."},
                    },
                    required=["source"],
                ),
                "handler": self._iptv_update_source,
            },
            "iptv_remove_source": {
                "schema": self._tool_schema(
                    name="iptv_remove_source",
                    description="Remove an IPTV playlist source from the Play tab.",
                    properties={
                        "source": {"type": "string", "description": "Source id, id prefix or name (from iptv_list_sources)."},
                    },
                    required=["source"],
                ),
                "handler": self._iptv_remove_source,
            },
            # --- App setup: API keys (write-only) ---
            "list_api_keys": {
                "schema": self._tool_schema(
                    name="list_api_keys",
                    description="Show every API key / credential slot and whether it is "
                    "configured. Values are never readable — use set_api_key to write one.",
                    properties={},
                    required=[],
                ),
                "handler": self._list_api_keys,
            },
            "set_api_key": {
                "schema": self._tool_schema(
                    name="set_api_key",
                    description="Write (or clear, with an empty value) an API key or credential "
                    "slot. Only use a value the user gave you explicitly or that you obtained "
                    "for them (e.g. a provider's documented key page). Never ask the user to "
                    "paste a key they don't want to share; existing values are never readable.",
                    properties={
                        "slot": {"type": "string", "description": "Slot name from list_api_keys (e.g. 'tmdb', 'llm', 'jackett')."},
                        "value": {"type": "string", "description": "The key/credential value (empty string clears the slot)."},
                    },
                    required=["slot", "value"],
                ),
                "handler": self._set_api_key,
            },
            # --- App setup: general settings ---
            "list_settings": {
                "schema": self._tool_schema(
                    name="list_settings",
                    description="List the app's agent-visible settings with their current values "
                    "(secrets show as <set>/<not set>) and dotted paths for set_settings. "
                    "Optional section filter: llm, indexer, web_search, watchdog, rss, browser, "
                    "download, torrents, iptv, irc, voice, sources.",
                    properties={
                        "section": {"type": "string", "description": "Restrict to one config section (default: all).", "default": ""},
                    },
                    required=[],
                ),
                "handler": self._list_settings,
            },
            "set_settings": {
                "schema": self._tool_schema(
                    name="set_settings",
                    description="Change one app setting by its dotted path (e.g. "
                    "'iptv.epg_url', 'download.max_concurrent'). Only non-secret scalar "
                    "settings are settable — keys and passwords go through set_api_key. "
                    "IPTV changes apply live; most others apply on next use or restart.",
                    properties={
                        "path": {"type": "string", "description": "Dotted setting path from list_settings."},
                        "value": {"description": "New value (string, number or boolean).", "type": ["string", "number", "boolean"]},
                    },
                    required=["path", "value"],
                ),
                "handler": self._set_settings,
            },
            # --- App setup: torrent indexer sources + IRC networks ---
            "list_torrent_sources": {
                "schema": self._tool_schema(
                    name="list_torrent_sources",
                    description="List the configured torrent search sources (indexers/sites) "
                    "that search_indexers queries, with their type and enabled state.",
                    properties={},
                    required=[],
                ),
                "handler": self._list_torrent_sources,
            },
            "add_torrent_source": {
                "schema": self._tool_schema(
                    name="add_torrent_source",
                    description="Add a torrent search source (indexer or site) that "
                    "search_indexers will query from now on.",
                    properties={
                        "name": {"type": "string", "description": "Display name."},
                        "url": {"type": "string", "description": "Site homepage/search URL."},
                        "id": {"type": "string", "description": "Optional slug (default: derived from the hostname; a known Jackett id gets its popularity ranking).", "default": ""},
                        "type": {"type": "string", "description": "'public' (default) or 'private'.", "default": "public"},
                        "categories": {"type": "array", "items": {"type": "string"}, "description": "App categories this source is good for, e.g. ['Movies','TV'].", "default": []},
                        "enabled": {"type": "boolean", "description": "Whether searches use it (default true).", "default": True},
                    },
                    required=["name", "url"],
                ),
                "handler": self._add_torrent_source,
            },
            "remove_torrent_source": {
                "schema": self._tool_schema(
                    name="remove_torrent_source",
                    description="Remove a torrent search source by id, name or URL.",
                    properties={
                        "source": {"type": "string", "description": "Source id, name or URL (from list_torrent_sources)."},
                    },
                    required=["source"],
                ),
                "handler": self._remove_torrent_source,
            },
            "irc_add_network": {
                "schema": self._tool_schema(
                    name="irc_add_network",
                    description="Add an IRC network to the configured list (like the IRC tab's "
                    "Networks dialog). Connect afterwards with irc_connect.",
                    properties={
                        "host": {"type": "string", "description": "Server hostname."},
                        "port": {"type": "integer", "description": "Port (default 6697).", "default": 6697},
                        "tls": {"type": "boolean", "description": "Use TLS (default true).", "default": True},
                        "id": {"type": "string", "description": "Short slug (default: derived from the host).", "default": ""},
                        "nick": {"type": "string", "description": "Nickname (default: a DeepFlux-style default).", "default": ""},
                        "username": {"type": "string", "description": "IRC username (default: nick).", "default": ""},
                        "realname": {"type": "string", "description": "Real name field.", "default": ""},
                        "password": {"type": "string", "description": "Server PASS password (rarely needed).", "default": ""},
                        "sasl_account": {"type": "string", "description": "SASL PLAIN account name.", "default": ""},
                        "sasl_password": {"type": "string", "description": "SASL PLAIN password.", "default": ""},
                        "channels": {"type": "array", "items": {"type": "string"}, "description": "Channels to auto-join on connect, e.g. ['#help'].", "default": []},
                    },
                    required=["host"],
                ),
                "handler": self._irc_add_network,
            },
            "irc_remove_network": {
                "schema": self._tool_schema(
                    name="irc_remove_network",
                    description="Remove a configured IRC network by id or host. A currently "
                    "connected network stays connected until disconnected.",
                    properties={
                        "network": {"type": "string", "description": "Network id or hostname."},
                    },
                    required=["network"],
                ),
                "handler": self._irc_remove_network,
            },
            # --- Browser (the Browse tab — full control of the embedded browser) ---
            "browser_list_tabs": {
                "schema": self._tool_schema(
                    name="browser_list_tabs",
                    description="List the embedded browser's open tabs (index, title, URL, which is "
                    "active).",
                    properties={},
                    required=[],
                ),
                "handler": self._browser_list_tabs,
            },
            "browser_navigate": {
                "schema": self._tool_schema(
                    name="browser_navigate",
                    description="Navigate the embedded browser to a URL (or search terms — handled "
                    "like the address bar). Use new_tab=true to keep the current page open.",
                    properties={
                        "url": {"type": "string", "description": "URL or search terms."},
                        "new_tab": {"type": "boolean", "description": "Open in a new browser tab (default false).", "default": False},
                    },
                    required=["url"],
                ),
                "handler": self._browser_navigate,
            },
            "browser_close_tab": {
                "schema": self._tool_schema(
                    name="browser_close_tab",
                    description="Close a browser tab by index (default: the active tab). The last "
                    "tab cannot be closed.",
                    properties={
                        "index": {"type": "integer", "description": "Tab index from browser_list_tabs (-1 = active).", "default": -1},
                    },
                    required=[],
                ),
                "handler": self._browser_close_tab,
            },
            "browser_switch_tab": {
                "schema": self._tool_schema(
                    name="browser_switch_tab",
                    description="Switch the active browser tab by index.",
                    properties={
                        "index": {"type": "integer", "description": "Tab index from browser_list_tabs."},
                    },
                    required=["index"],
                ),
                "handler": self._browser_switch_tab,
            },
            "browser_go": {
                "schema": self._tool_schema(
                    name="browser_go",
                    description="Browser navigation controls: back, forward, reload, stop, or home "
                    "(the configured homepage).",
                    properties={
                        "action": {"type": "string", "description": "back | forward | reload | stop | home."},
                    },
                    required=["action"],
                ),
                "handler": self._browser_go,
            },
            "browser_get_content": {
                "schema": self._tool_schema(
                    name="browser_get_content",
                    description="Read the live page in the active browser tab: title, URL, visible "
                    "text, and links. Unlike web_fetch this sees the rendered DOM (JavaScript "
                    "executed, user logged in), so use it for JS-heavy or session-gated pages.",
                    properties={
                        "max_chars": {"type": "integer", "description": "Max text characters (default 8000).", "default": 8000},
                        "include_links": {"type": "boolean", "description": "Include the page's links (default true).", "default": True},
                    },
                    required=[],
                ),
                "handler": self._browser_get_content,
            },
            "browser_snapshot": {
                "schema": self._tool_schema(
                    name="browser_snapshot",
                    description="Inspect visible interactive controls on the current rendered page and return stable refs for safe follow-up actions. Requires per-origin page-sharing consent.",
                    properties={
                        "limit": {"type": "integer", "minimum": 1, "maximum": 250, "description": "Maximum visible controls (default 120).", "default": 120},
                    },
                    required=[],
                ),
                "handler": self._browser_snapshot,
            },
            "browser_wait": {
                "schema": self._tool_schema(
                    name="browser_wait",
                    description="Wait for a selector, URL fragment, or visible text after navigation or interaction.",
                    properties={
                        "selector": {"type": "string", "description": "CSS selector to wait for.", "default": ""},
                        "url_contains": {"type": "string", "description": "URL fragment to wait for.", "default": ""},
                        "text": {"type": "string", "description": "Visible text to wait for.", "default": ""},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Timeout in seconds (default 10).", "default": 10},
                    },
                    required=[],
                    any_of=[["selector"], ["url_contains"], ["text"]],
                ),
                "handler": self._browser_wait,
            },
            "browser_click_ref": {
                "schema": self._tool_schema(
                    name="browser_click_ref",
                    description="Click one stable element ref returned by browser_snapshot. Requires confirmation and a fresh snapshot.",
                    properties={"ref": {"type": "string", "pattern": "^e[0-9]{1,3}$", "description": "Element ref from browser_snapshot."}},
                    required=["ref"],
                ),
                "handler": self._browser_click_ref,
            },
            "browser_type_ref": {
                "schema": self._tool_schema(
                    name="browser_type_ref",
                    description="Type into one stable input/contenteditable ref using framework-compatible input events. Requires confirmation.",
                    properties={
                        "ref": {"type": "string", "pattern": "^e[0-9]{1,3}$", "description": "Element ref from browser_snapshot."},
                        "value": {"type": "string", "description": "Text to type."},
                    },
                    required=["ref", "value"],
                ),
                "handler": self._browser_type_ref,
            },
            "browser_select_ref": {
                "schema": self._tool_schema(
                    name="browser_select_ref",
                    description="Select an option by value or exact label on a stable select ref. Requires confirmation.",
                    properties={
                        "ref": {"type": "string", "pattern": "^e[0-9]{1,3}$", "description": "Element ref from browser_snapshot."},
                        "value": {"type": "string", "description": "Option value or exact visible label."},
                    },
                    required=["ref", "value"],
                ),
                "handler": self._browser_select_ref,
            },
            "browser_check_ref": {
                "schema": self._tool_schema(
                    name="browser_check_ref",
                    description="Set a stable checkbox/radio ref to the requested state. Requires confirmation.",
                    properties={
                        "ref": {"type": "string", "pattern": "^e[0-9]{1,3}$", "description": "Element ref from browser_snapshot."},
                        "checked": {"type": "boolean", "description": "Requested checked state.", "default": True},
                    },
                    required=["ref"],
                ),
                "handler": self._browser_check_ref,
            },
            "browser_click": {
                "schema": self._tool_schema(
                    name="browser_click",
                    description="Click an element on the live browser page — by CSS selector, or by "
                    "visible text (matches links, buttons, submit inputs). Can trigger navigation, "
                    "downloads, or form submission, so confirm with the user first.",
                    properties={
                        "selector": {"type": "string", "description": "CSS selector (preferred).", "default": ""},
                        "text": {"type": "string", "description": "Visible text to match when no selector.", "default": ""},
                    },
                    required=[],
                ),
                "handler": self._browser_click,
            },
            "browser_fill": {
                "schema": self._tool_schema(
                    name="browser_fill",
                    description="Fill a form field on the live browser page (CSS selector + value, "
                    "with input/change events). Set submit=true to also submit the enclosing form. "
                    "Confirm with the user first.",
                    properties={
                        "selector": {"type": "string", "description": "CSS selector of the input/textarea."},
                        "value": {"type": "string", "description": "Value to enter."},
                        "submit": {"type": "boolean", "description": "Submit the enclosing form afterwards (default false).", "default": False},
                    },
                    required=["selector", "value"],
                ),
                "handler": self._browser_fill,
            },
            "browser_scroll": {
                "schema": self._tool_schema(
                    name="browser_scroll",
                    description="Scroll the live browser page: up, down, top, or bottom.",
                    properties={
                        "direction": {"type": "string", "description": "up | down | top | bottom (default down).", "default": "down"},
                        "pixels": {"type": "integer", "description": "Pixels for up/down (default: ~one viewport).", "default": 0},
                    },
                    required=[],
                ),
                "handler": self._browser_scroll,
            },
            "browser_add_bookmark": {
                "schema": self._tool_schema(
                    name="browser_add_bookmark",
                    description="Bookmark a page in the embedded browser (defaults to the current "
                    "tab's page).",
                    properties={
                        "url": {"type": "string", "description": "URL to bookmark (default: current page).", "default": ""},
                        "title": {"type": "string", "description": "Bookmark title (default: page title).", "default": ""},
                    },
                    required=[],
                ),
                "handler": self._browser_add_bookmark,
            },
            "browser_remove_bookmark": {
                "schema": self._tool_schema(
                    name="browser_remove_bookmark",
                    description="Remove a browser bookmark by URL.",
                    properties={
                        "url": {"type": "string", "description": "Bookmarked URL to remove."},
                    },
                    required=["url"],
                ),
                "handler": self._browser_remove_bookmark,
            },
            "browser_list_bookmarks": {
                "schema": self._tool_schema(
                    name="browser_list_bookmarks",
                    description="List the embedded browser's bookmarks.",
                    properties={},
                    required=[],
                ),
                "handler": self._browser_list_bookmarks,
            },
        }

    @staticmethod
    def _tool_schema(
        name: str,
        description: str,
        properties: Dict[str, Any],
        required: List[str],
        any_of: Optional[List[List[str]]] = None,
    ) -> Dict[str, Any]:
        parameters: Dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        if any_of:
            parameters["anyOf"] = [{"required": keys} for keys in any_of]
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }

    @classmethod
    def _validate_args(cls, args: Dict[str, Any], schema: Dict[str, Any]) -> None:
        if not isinstance(args, dict):
            raise ToolError("Tool arguments must be an object")
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in args:
                raise ToolError(f"Missing required argument: {key}")
        any_of = schema.get("anyOf", [])
        if any_of and not any(
            all(key in args and args[key] not in (None, "", []) for key in branch.get("required", []))
            for branch in any_of
        ):
            choices = [" + ".join(branch.get("required", [])) for branch in any_of]
            raise ToolError(f"Provide one of: {', '.join(choices)}")
        for key in args:
            if key not in props:
                raise ToolError(f"Unknown argument: {key}")
        for key, prop in props.items():
            if key in args:
                args[key] = cls._validate_value(args[key], prop, key)
            elif "default" in prop:
                args[key] = prop["default"]

    @classmethod
    def _validate_value(cls, value: Any, schema: Dict[str, Any], path: str) -> Any:
        ptype = schema.get("type")
        if ptype == "integer":
            if isinstance(value, bool):
                raise ToolError(f"Argument {path} must be an integer")
            if not isinstance(value, int):
                try:
                    value = int(value)
                except (TypeError, ValueError) as exc:
                    raise ToolError(f"Argument {path} must be an integer") from exc
        elif ptype == "number":
            if isinstance(value, bool):
                raise ToolError(f"Argument {path} must be a number")
            if not isinstance(value, (int, float)):
                try:
                    value = float(value)
                except (TypeError, ValueError) as exc:
                    raise ToolError(f"Argument {path} must be a number") from exc
        elif ptype == "boolean":
            if not isinstance(value, bool):
                if isinstance(value, str) and value.lower() in ("true", "1", "yes", "on", "false", "0", "no", "off"):
                    value = value.lower() in ("true", "1", "yes", "on")
                else:
                    raise ToolError(f"Argument {path} must be a boolean")
        elif ptype == "string":
            if not isinstance(value, str):
                raise ToolError(f"Argument {path} must be a string")
        elif ptype == "array":
            if not isinstance(value, list):
                raise ToolError(f"Argument {path} must be an array")
            item_schema = schema.get("items", {})
            value = [cls._validate_value(item, item_schema, f"{path}[{index}]") for index, item in enumerate(value)]
        elif ptype == "object":
            if not isinstance(value, dict):
                raise ToolError(f"Argument {path} must be an object")
            properties = schema.get("properties", {})
            for key in schema.get("required", []):
                if key not in value:
                    raise ToolError(f"Missing required argument: {path}.{key}")
            if schema.get("additionalProperties") is False:
                unknown = set(value) - set(properties)
                if unknown:
                    raise ToolError(f"Unknown argument: {path}.{sorted(unknown)[0]}")
            value = {
                key: cls._validate_value(item, properties[key], f"{path}.{key}") if key in properties else item
                for key, item in value.items()
            }
        if "enum" in schema and value not in schema["enum"]:
            raise ToolError(f"Argument {path} must be one of: {', '.join(map(str, schema['enum']))}")
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError(f"Argument {path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError(f"Argument {path} must be at most {schema['maximum']}")
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ToolError(f"Argument {path} must contain at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolError(f"Argument {path} must contain at most {schema['maxItems']} item(s)")
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ToolError(f"Argument {path} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolError(f"Argument {path} is too long")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ToolError(f"Argument {path} has an invalid format")
        return value

    # ------------------------------------------------------------------
    # Engine-backed handlers
    # ------------------------------------------------------------------

    def _agent_diagnostics(self) -> Dict[str, Any]:
        provider = self.config.llm.provider
        preset = LLM_PROVIDER_PRESETS.get(provider, {})
        policies = [tool_policy(name) for name in self._tools]
        return {
            "success": True,
            "llm": {
                "provider": provider,
                "model": self.config.llm.model,
                "base_url": redact_url_secrets(self.config.llm.base_url or preset.get("base_url", "")),
                "api_key_configured": bool(self.config.llm.api_key),
                "streaming": bool(preset.get("streaming", True)),
                "tools": bool(preset.get("tools", True)),
                "reasoning_effort": bool(
                    self.config.llm.custom_reasoning_effort if provider == "custom"
                    else preset.get("reasoning_effort", False)
                ),
            },
            "integrations": {
                "jackett": bool(self.config.indexer.api_key),
                "brave": bool(self.config.web_search.brave_api_key),
                "perplexity": bool(self.config.web_search.api_key),
                "memory": self.memory is not None,
                "rss_feeds": len(self.config.rss.feeds),
                "irc": self._irc_client is not None,
                "iptv_gui": self._iptv_bridge is not None,
                "browser_gui": self._browser_bridge is not None,
                "download_manager": self._dl_engine is not None,
            },
            "tools": {
                "total": len(self._tools),
                "read_only": sum(policy.effect == "read" for policy in policies),
                "confirmation_required": sum(policy.confirmation == "always" for policy in policies),
            },
            "limits": {
                "max_turns": self.config.llm.max_turns,
                "max_llm_calls": self.config.llm.max_llm_calls,
                "max_tool_calls": self.config.llm.max_tool_calls,
                "task_timeout_seconds": self.config.llm.task_timeout_seconds,
                "context_budget_tokens": self.config.llm.context_budget_tokens,
            },
            "watchdog": {
                "enabled": self.config.watchdog.enabled,
                "auto_heal": self.config.watchdog.auto_heal,
                "stall_threshold_seconds": self.config.watchdog.stall_threshold_seconds,
                "cooldown_seconds": self.config.watchdog.cooldown_seconds,
            },
        }

    def _add_magnet(self, uri: str, save_path: str, category: str = "Other") -> Dict[str, Any]:
        uri = uri.strip()
        if uri.startswith("?xt=urn:btih:"):
            uri = "magnet:" + uri
        if not uri.startswith("magnet:"):
            raise ToolError("Invalid magnet URI")
        info_hash = self.engine.add_magnet(uri, save_path, category)
        return {"success": True, "info_hash": info_hash, "save_path": save_path, "category": category}

    def _add_torrent_file(self, path: str, save_path: str, category: str = "Other") -> Dict[str, Any]:
        if path.startswith(("http://", "https://")):
            return self._add_torrent_from_url(path, save_path, category)
        if not os.path.isfile(path):
            raise ToolError(f"Torrent file not found: {path}")
        info_hash = self.engine.add_torrent_file(path, save_path, category)
        return {"success": True, "info_hash": info_hash, "save_path": save_path, "category": category}

    def _get_dl_engine(self):
        """The download-manager engine — injected by the GUI; lazily created
        (and started) on first use elsewhere, e.g. the CLI REPL."""
        if self._dl_engine is None:
            with self._resource_lock:
                if self._dl_engine is None:
                    from dlmgr.engine import DownloadEngine

                    self._dl_engine = DownloadEngine(self.config.download)
                    self._owns_dl_engine = True
                    self._dl_engine.start()
                    logger.info("add_download: lazily started a DownloadEngine")
        return self._dl_engine

    def _add_download(self, url: str, filename: str = "", save_path: str = "") -> Dict[str, Any]:
        url = validate_public_http_url(url)
        if save_path:
            save_path = os.path.join(os.path.abspath(os.path.expanduser(save_path)), "")
        engine = self._get_dl_engine()
        low = url.lower().split("?", 1)[0]
        if low.endswith((".m3u8", ".mpd")):
            job = engine.add_stream_job(url=url, filename=filename, save_path=save_path)
        else:
            job = engine.add_job(url=url, filename=filename, save_path=save_path)
        self._progress(f"Download queued: {job.filename}")
        return {
            "success": True,
            "job_id": job.id,
            "filename": job.filename,
            "save_path": job.save_path,
            "job_type": job.job_type,
            "note": "Queued in the download manager — visible in the Download tab's Downloads panel.",
        }

    @staticmethod
    def _fmt_dl_job(job: Any) -> Dict[str, Any]:
        total = getattr(job, "file_size", 0) or 0
        done = getattr(job, "downloaded", 0) or 0
        out: Dict[str, Any] = {
            "job_id": job.id,
            "filename": job.filename,
            "status": job.status.value,
            "job_type": job.job_type,
            "save_path": job.save_path,
            "url": job.url,
        }
        if total:
            out["progress_pct"] = round(done * 100.0 / total, 1)
            out["file_size"] = total
        if getattr(job, "speed_bps", 0):
            out["speed_bps"] = job.speed_bps
        if getattr(job, "error_message", ""):
            out["error"] = job.error_message
        return out

    def _resolve_dl_job(self, engine: Any, job_id: str):
        """Match a job by full id or unique prefix."""
        job = engine.get_job(job_id)
        if job is not None:
            return job
        matches = [j for j in engine.list_jobs() if j.id.startswith(job_id)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ToolError(f"Ambiguous job id '{job_id}' — matches: "
                            + ", ".join(j.id for j in matches))
        raise ToolError(f"Unknown job id: {job_id} — use list_downloads to see jobs")

    def _list_downloads(self, status: str = "", offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        engine = self._get_dl_engine()
        jobs = engine.list_jobs()
        if status:
            jobs = [j for j in jobs if j.status.value == status.lower()]
        total = len(jobs)
        page = jobs[offset:offset + limit]
        return {
            "success": True,
            "count": len(page),
            "total": total,
            "offset": offset,
            "has_more": offset + len(page) < total,
            "downloads": [self._fmt_dl_job(j) for j in page],
        }

    def _pause_download(self, job_id: str) -> Dict[str, Any]:
        engine = self._get_dl_engine()
        job = self._resolve_dl_job(engine, job_id)
        ok = engine.pause_job(job.id)
        return {"success": ok, "job_id": job.id, "filename": job.filename,
                "error": None if ok else f"Job is {job.status.value} — only queued/downloading jobs can be paused"}

    def _resume_download(self, job_id: str) -> Dict[str, Any]:
        engine = self._get_dl_engine()
        job = self._resolve_dl_job(engine, job_id)
        ok = engine.resume_job(job.id)
        return {"success": ok, "job_id": job.id, "filename": job.filename,
                "error": None if ok else f"Job is {job.status.value} — only paused/error jobs can be resumed"}

    def _retry_download(self, job_id: str) -> Dict[str, Any]:
        engine = self._get_dl_engine()
        job = self._resolve_dl_job(engine, job_id)
        ok = engine.retry_job(job.id) is not None
        return {"success": ok, "job_id": job.id, "filename": job.filename,
                "error": None if ok else f"Job is {job.status.value} — only paused/error jobs can be retried"}

    def _cancel_download(self, job_id: str, delete_file: bool = True) -> Dict[str, Any]:
        engine = self._get_dl_engine()
        job = self._resolve_dl_job(engine, job_id)
        ok = engine.cancel_job(job.id, delete_file=delete_file)
        return {"success": ok, "job_id": job.id, "filename": job.filename,
                "deleted_file": bool(delete_file)}

    def _remove_download(self, job_id: str) -> Dict[str, Any]:
        engine = self._get_dl_engine()
        job = self._resolve_dl_job(engine, job_id)
        ok = engine.remove_job(job.id)
        return {"success": ok, "job_id": job.id, "filename": job.filename,
                "error": None if ok else "Active jobs can't be removed — cancel or wait for completion"}

    def _pause_torrent(self, info_hash: str) -> Dict[str, Any]:
        return {"success": self.engine.pause(info_hash), "info_hash": info_hash, "action": "pause"}

    def _resume_torrent(self, info_hash: str) -> Dict[str, Any]:
        return {"success": self.engine.resume(info_hash), "info_hash": info_hash, "action": "resume"}

    def _remove_torrent(self, info_hash: str, delete_files: bool = False) -> Dict[str, Any]:
        return {"success": self.engine.remove(info_hash, delete_files), "info_hash": info_hash, "deleted": delete_files}

    def _list_torrents(self, filter: str = "", offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        torrents = self.engine.list_torrents(filter=filter or None)
        total = len(torrents)
        page = torrents[offset:offset + limit]
        return {
            "torrents": page,
            "count": len(page),
            "total": total,
            "offset": offset,
            "has_more": offset + len(page) < total,
        }

    def _get_torrent_status(self, info_hash: str, file_offset: int = 0, file_limit: int = 100) -> Dict[str, Any]:
        status = dict(self.engine.get_torrent_status(info_hash))
        files = status.get("files")
        if isinstance(files, list):
            total = len(files)
            page = files[file_offset:file_offset + file_limit]
            status["files"] = page
            status["file_page"] = {
                "count": len(page),
                "total": total,
                "offset": file_offset,
                "has_more": file_offset + len(page) < total,
            }
        return {"status": status}

    def _set_file_priority(self, info_hash: str, file_id: int, level: int) -> Dict[str, Any]:
        return {"success": self.engine.set_file_priority(info_hash, file_id, level), "info_hash": info_hash}

    def _add_tracker(self, info_hash: str, url: str) -> Dict[str, Any]:
        return {"success": self.engine.add_tracker(info_hash, url), "info_hash": info_hash, "tracker": url}

    def _set_torrent_rate_limits(self, download_kb: int = 0, upload_kb: int = 0) -> Dict[str, Any]:
        download_kb = max(0, int(download_kb or 0))
        upload_kb = max(0, int(upload_kb or 0))
        self.engine.set_rate_limits(download_kb, upload_kb)
        self.config.torrents.download_rate_limit_kb = download_kb
        self.config.torrents.upload_rate_limit_kb = upload_kb
        return {"success": True, "download_kb": download_kb, "upload_kb": upload_kb,
                "note": "Applied at runtime; 0 means unlimited."}

    def _set_sequential_download(self, info_hash: str, on: bool = True) -> Dict[str, Any]:
        ok = self.engine.set_sequential_download(info_hash, on)
        return {"success": ok, "info_hash": info_hash, "sequential": bool(on)}

    def _force_recheck(self, info_hash: str) -> Dict[str, Any]:
        ok = self.engine.force_recheck(info_hash)
        return {"success": ok, "info_hash": info_hash,
                "note": "Re-checking files on disk — progress pauses until it finishes."}

    def _force_reannounce(self, info_hash: str) -> Dict[str, Any]:
        ok = self.engine.force_reannounce(info_hash)
        return {"success": ok, "info_hash": info_hash,
                "note": "Reannounce sent to all trackers."}

    def _get_swarm_stats(self, info_hash: str) -> Dict[str, Any]:
        return self.engine.get_swarm_stats(info_hash)

    # ------------------------------------------------------------------
    # Swarm health
    # ------------------------------------------------------------------

    def _diagnose_swarm(self, info_hash: str) -> Dict[str, Any]:
        stats = self.engine.get_swarm_stats(info_hash)
        health = "healthy"
        cause: Optional[str] = None

        if stats["progress"] >= 1.0:
            health = "completed"
        elif stats["num_peers"] == 0 and stats["dht_nodes"] == 0 and not stats["trackers"]:
            health = "dead"
            cause = "no trackers responding and zero DHT peers"
        elif stats["num_peers"] == 0 and stats["dht_nodes"] == 0:
            health = "dead"
            cause = "zero DHT peers and no tracker replies"
        elif stats["num_peers"] == 0:
            health = "stalled"
            cause = "zero connected peers"
        elif stats["download_rate"] == 0 and all(p["down_speed"] == 0 for p in stats["peers"]):
            health = "stalled"
            cause = "all connected peers choked or not sending data"
        elif stats["piece_availability_histogram"].get("zeros", 0) > 0 and stats["num_seeds"] == 0:
            health = "stalled"
            cause = "missing seeders and incomplete piece availability"

        return {
            "info_hash": info_hash,
            "health": health,
            "cause": cause,
            "summary": f"Swarm is {health}" + (f" ({cause})" if cause else "."),
            "stats": stats,
        }

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _refresh_tracker_list(self) -> Dict[str, Any]:
        return self._web_search.fetch_tracker_lists()

    def _find_alt_trackers(self, torrent_name: str) -> Dict[str, Any]:
        query = f'"{torrent_name}" torrent tracker list'
        return self._web_search.search(query, limit=10)

    # App category names → Torznab category IDs.
    CATEGORY_TO_TORZNAB = {"movies": "2000", "tv": "5000", "software": "4000", "other": ""}

    SEARCH_CACHE_TTL = 300  # seconds

    def _search_indexers(self, query: str, category: str = "", deep: bool = False,
                         offset: int = 0) -> Dict[str, Any]:
        """Search for torrents, with a 5-minute result cache.

        Default is a QUICK pass (top source(s) only) for speed; deep=True sweeps
        every enabled source. A quick pass that finds nothing escalates to a
        deep sweep automatically.
        Priority: Jackett (private indexers first) → source web search
        (private tier, then public) → RSS subscriptions → generic web search.

        The cache holds the FULL result set; the agent only ever receives one
        page (SEARCH_PAGE_SIZE rows) so a deep sweep can never blow up the LLM
        context. offset>0 pages the cached set without re-querying indexers.
        """
        category = self._normalize_category(category)
        offset = max(0, int(offset or 0))
        cache_key = (query.strip().lower(), category, deep)
        with self._search_cache_lock:
            cached = self._search_cache.get(cache_key)
            if cached and time.time() - cached[0] < self.SEARCH_CACHE_TTL:
                logger.info("search_indexers cache hit for %r", query)
                self._progress(f"Cache hit — reusing recent results for '{query}'")
                return self._page_result(cached[1], offset)

        # Fetch generic "about" info (TMDb/Wikipedia) on a side thread so the
        # torrent cascade never waits for it — the lookup is best-effort.
        about_box: Dict[str, Any] = {}

        def _about_worker() -> None:
            try:
                about_box["value"] = self._fetch_about(query)
            except Exception as exc:
                logger.debug("about lookup crashed for %r: %s", query, exc)

        about_thread = threading.Thread(target=_about_worker, daemon=True)
        about_thread.start()

        result = self._search_indexers_uncached(query, category, deep)
        about_thread.join(timeout=8)  # never stall results on the info lookup
        about = about_box.get("value")
        if about:
            result["about"] = about
        if result.get("success") and isinstance(result.get("results"), list):
            with self._search_cache_lock:
                self._search_cache[cache_key] = (time.time(), result)
        return self._page_result(result, offset)

    SEARCH_PAGE_SIZE = 40  # ranked rows per LLM-visible page

    def _page_result(self, result: Dict[str, Any], offset: int) -> Dict[str, Any]:
        """Slice a full result set into an LLM-sized page.

        The uncut list stays in the search cache, so paging never re-queries
        indexers — the agent analyzes already-local results."""
        results = result.get("results")
        if not isinstance(results, list) or (len(results) <= self.SEARCH_PAGE_SIZE and offset <= 0):
            return result
        total = len(results)
        page = results[offset:offset + self.SEARCH_PAGE_SIZE]
        paged = dict(result)
        paged["results"] = page
        paged["count"] = len(page)
        paged["total_found"] = total
        paged["offset"] = offset
        paged["has_more"] = offset + len(page) < total
        if page:
            hint = f"Showing {offset + 1}–{offset + len(page)} of {total} locally cached results. "
        else:
            hint = f"Offset {offset} is past the {total} cached result(s). "
        if paged["has_more"]:
            hint += (f"Call search_indexers again with offset={offset + len(page)} for the next page — "
                     "served from the local cache, no new indexer queries.")
        else:
            hint += "No more pages."
        paged["note"] = f"{result.get('note', '')} {hint}".strip()
        return paged

    # ------------------------------------------------------------------
    # "About" complement — generic info about the searched title/topic
    # ------------------------------------------------------------------

    _ABOUT_TIMEOUT = 6  # seconds per HTTP call

    def _fetch_about(self, query: str) -> Optional[Dict[str, Any]]:
        """Generic context for the thing being searched: TMDb for movies/TV,
        Wikipedia for any other topic. None on any failure — torrent results
        must never depend on this lookup."""
        if not query.strip():
            return None
        try:
            about = self._about_from_tmdb(query)
            if about:
                return about
        except Exception as exc:
            logger.debug("TMDb about lookup failed for %r: %s", query, exc)
        try:
            return self._about_from_wikipedia(query)
        except Exception as exc:
            logger.debug("Wikipedia about lookup failed for %r: %s", query, exc)
            return None

    def _about_from_tmdb(self, query: str) -> Optional[Dict[str, Any]]:
        """Movie/TV match from TMDb — only when the title matches confidently."""
        import difflib

        from iptv.metadata import TMDBProvider, clean_title, extract_year

        api_key = self.config.iptv.tmdb_api_key
        title = clean_title(query)
        year = extract_year(query)
        # clean_title only strips *parenthesized* years — drop a bare year too,
        # it's passed separately and "The Matrix 1999" as query text misses.
        title = re.sub(r"\b(?:19|20)\d{2}\b", " ", title)
        title = re.sub(r"\s+", " ", title).strip()
        if not api_key or len(title) < 2:
            return None
        provider = TMDBProvider(api_key)
        for kind, section in (("movie", "movies"), ("tv", "series")):
            meta = provider.fetch(title, year, section)
            if not meta:
                continue
            ratio = difflib.SequenceMatcher(
                None, title.lower(), (meta.get("title") or "").lower()).ratio()
            if ratio < 0.75:
                continue  # fuzzy hit for something else (e.g. a subtitle
                # expansion like "John Digweed: Structures" for "john digweed")
            if year and meta.get("year") and meta["year"] != year:
                continue  # same name, different year — a different work
            return {
                "source": "tmdb",
                "kind": kind,
                "title": meta.get("title", ""),
                "year": meta.get("year", ""),
                "rating": meta.get("rating", 0),
                "genres": meta.get("genres", []),
                "overview": (meta.get("synopsis") or "")[:600],
            }
        return None

    def _about_from_wikipedia(self, query: str) -> Optional[Dict[str, Any]]:
        """Topic summary from Wikipedia (keyless): opensearch resolve + REST summary."""
        # Use the most-stripped query variant so release tags don't derail it.
        title = self._query_variants(query)[-1]
        r = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "opensearch", "search": title, "limit": 1,
                    "namespace": 0, "format": "json"},
            headers={"User-Agent": BROWSER_UA},
            timeout=self._ABOUT_TIMEOUT,
        )
        r.raise_for_status()
        r.encoding = "utf-8"  # Wikimedia omits the charset — avoid mojibake
        data = r.json()
        pages = data[1] if len(data) > 1 else []
        if not pages:
            return None
        page = pages[0]
        s = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(page)}",
            headers={"User-Agent": BROWSER_UA},
            timeout=self._ABOUT_TIMEOUT,
        )
        if not s.ok:
            return None
        s.encoding = "utf-8"
        sj = s.json()
        extract = (sj.get("extract") or "").strip()
        if not extract:
            return None
        return {
            "source": "wikipedia",
            "kind": "topic",
            "title": sj.get("title") or page,
            "description": sj.get("description", ""),
            "overview": extract[:600],
            "url": (sj.get("content_urls", {}).get("desktop") or {}).get("page", ""),
        }

    def _normalize_category(self, category: str) -> str:
        """Translate an app category name (Movies/TV/Software) to a Torznab ID."""
        if not category or category.isdigit():
            return category
        return self.CATEGORY_TO_TORZNAB.get(category.strip().lower(), "")

    @staticmethod
    def _query_variants(query: str) -> List[str]:
        """Query fallbacks: original, stripped of quality tags, stripped of year."""
        variants = [query]
        stripped = re.sub(
            r"\b(2160p|1080p|720p|480p|x264|x265|h264|h265|hevc|bluray|blu-ray|"
            r"web[- ]?dl|webrip|hdrip|dvdrip|proper|repack|remux|hdr|dts|aac)\b",
            "", query, flags=re.IGNORECASE,
        )
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if stripped and stripped != query:
            variants.append(stripped)
        no_year = re.sub(r"\b(19|20)\d{2}\b", "", stripped)
        no_year = re.sub(r"\s+", " ", no_year).strip()
        if no_year and no_year != stripped:
            variants.append(no_year)
        return variants

    def _search_indexers_uncached(self, query: str, category: str, deep: bool) -> Dict[str, Any]:
        """Full search cascade (see _search_indexers for the priority order).

        Quick mode (deep=False) only queries the top source(s) and escalates
        to the full sweep automatically when nothing is found."""
        # 1. Jackett — private indexers first, public in popularity batches.
        #    If a query returns nothing, retry with simplified variants before
        #    giving up on the seeder-rich path. Indexers that fail (timeout etc.)
        #    are tracked in `dead` and skipped for the rest of this search —
        #    one hanging indexer must not multiply the lag across every query
        #    variant and the auto deep sweep.
        if self.config.indexer.api_key and self.config.sources.use_jackett:
            dead: Dict[str, str] = {}
            failure: Optional[Dict[str, Any]] = None
            result = self._jackett_cascade(query, category, quick=not deep, dead=dead)
            if result is not None:
                if result.get("success"):
                    return result
                failure = result
            if not deep:
                self._progress("Quick pass found nothing — sweeping all indexers")
                result = self._jackett_cascade(query, category, quick=False, dead=dead)
                if result is not None:
                    if result.get("success"):
                        result["scope"] = "deep"
                        result["note"] = f"{result.get('note', '')} (auto deep sweep: quick pass found nothing)".strip()
                        return result
                    failure = result
            if failure is not None:
                # Jackett itself failed (timeouts/network/auth) — tell the agent
                # explicitly so it can retry, instead of silently substituting
                # web results. Failures are never cached.
                return failure
            self._progress("No results from Jackett — falling back to web sources")
            # If Jackett failed entirely, fall through to source-specific search.

        # 2. Source-specific web search — private trackers first, public only if needed.
        #    Quick mode: only the single most popular source; zero hits there
        #    escalate to the remaining sources automatically.
        enabled_sources = [s for s in self.config.sources.sources if s.enabled and s.url]
        private_sources = sorted(
            (s for s in enabled_sources if s.type == "private"),
            key=source_popularity, reverse=True,
        )
        public_sources = sorted(
            (s for s in enabled_sources if s.type != "private"),
            key=source_popularity, reverse=True,
        )
        scope = "deep" if deep else "quick"
        if not deep and enabled_sources:
            top = (private_sources or public_sources)[0]
            results = self._search_source_tier(query, [top])
            if results:
                remaining = len(enabled_sources) - 1
                note = f"Quick pass on {top.name} only. "
                if remaining:
                    note += (f"Call again with deep=true to sweep {remaining} more source(s) "
                             "if these results aren't good enough. ")
                note += "Use web_fetch to read the pages and extract magnet or .torrent download links (or download_links for direct downloads via add_download)."
                return {
                    "success": True,
                    "count": len(results),
                    "results": results,
                    "source": "source_web_search",
                    "tier": top.type,
                    "scope": "quick",
                    "deep_available": remaining > 0,
                    "note": note,
                }
            self._progress(f"Nothing on {top.name} — sweeping remaining source(s)")
            scope = "deep"
            private_sources = [s for s in private_sources if s is not top]
            public_sources = [s for s in public_sources if s is not top]
        for tier_name, tier in (("private", private_sources), ("public", public_sources)):
            if not tier:
                continue
            results = self._search_source_tier(query, tier)
            if results:
                return {
                    "success": True,
                    "count": len(results),
                    "results": results,
                    "source": "source_web_search",
                    "tier": tier_name,
                    "scope": scope,
                    "note": "Results from enabled source websites. Use web_fetch to read the pages and extract magnet or .torrent download links (or download_links for direct downloads via add_download).",
                }

        # 3. RSS subscriptions — direct magnets from feeds the user already follows.
        rss_results = self._search_rss_feeds(query)
        if rss_results:
            return {
                "success": True,
                "count": len(rss_results),
                "results": rss_results,
                "source": "rss_feeds",
                "scope": scope,
                "note": "Matches from the user's RSS subscriptions. Items have magnet_uri or torrent_url ready to use with add_magnet.",
            }

        # 4. Generic web search fallback, with simplified variants.
        for variant in self._query_variants(query):
            logger.info("No source results; falling back to generic web search for %r", variant)
            web_results = self._web_search.search(f"{variant} torrent", limit=15, llm=False)
            if web_results.get("results"):
                return {
                    "success": True,
                    "count": len(web_results.get("results", [])),
                    "results": web_results.get("results", []),
                    "source": "web_search",
                    "scope": scope,
                    "note": "Use web_fetch to read the pages and extract magnet or .torrent download links (or download_links for direct downloads via add_download).",
                }
        return {
            "success": False,
            "count": 0,
            "results": [],
            "source": "web_search",
            "scope": scope,
            "note": "No results found on any source.",
        }

    def _jackett_cascade(self, query: str, category: str, quick: bool,
                         dead: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
        """Try query variants against Jackett; return the first result with hits.

        `dead` collects indexer-id → error for the whole search (quick pass AND
        the auto deep sweep) so a failing indexer is never queried twice.
        Returns a failure dict when every queried indexer errored — a clean
        zero-hit returns None so the caller falls back to web sources."""
        dead = dead if dead is not None else {}
        last_error: Optional[Dict[str, Any]] = None
        for variant in self._query_variants(query):
            if variant != query:
                self._progress(f"No hits — retrying with simplified query: '{variant}'")
            result = self._jackett_search_tiered(variant, category, quick=quick, dead=dead)
            if result.get("success") and result.get("results"):
                if variant != query:
                    note = result.get("note", "")
                    result["note"] = f"{note} (searched as: {variant})".strip()
                return result
            if result.get("error"):
                # A failing indexer won't resurrect for a simplified query —
                # stop here instead of multiplying the lag across variants.
                last_error = result
                break
        if last_error is not None:
            if dead:
                last_error["errors"] = list(dead.values())
            last_error.setdefault(
                "note",
                "Jackett queries failed — this is a connectivity problem, not a "
                "lack of results. Retry the search shortly; do not report it as 'no results'.",
            )
        return last_error

    def _jackett_search_tiered(self, query: str, category: str = "", quick: bool = False,
                               dead: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Per-indexer Jackett search: private indexers first (in parallel), then
        public indexers in popularity-ordered batches with early exit.

        Quick mode caps the public tier at the single most popular indexer so
        a first pass returns fast; the caller escalates to a full sweep when
        quick finds nothing.
        Per-indexer queries return quickly, so private-first actually saves time
        here — unlike the all-in-one 'all' endpoint, which waits for every indexer.
        Falls back to the 'all' endpoint when no sources are configured.

        `dead` (indexer-id → error) is shared across the whole search: indexers
        that failed once are skipped for later variants/sweeps. Per-indexer
        failures are surfaced in the result's `errors` list instead of being
        silently treated as zero hits; when EVERY queried indexer fails, the
        result is an explicit failure dict so the agent knows to retry."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        dead = dead if dead is not None else {}
        sources = self.config.sources.sources
        if not sources:
            result = self._indexer.search(query, category)
            if result.get("success") and result.get("results"):
                return self._prioritize_private_results(result)
            return result

        local_errors: List[str] = []
        queried = 0
        failed = 0

        def _search_one(src) -> Tuple[List[Dict[str, Any]], Optional[str]]:
            try:
                outcome = self._indexer.search_indexer(src.id, query, category)
            except Exception as exc:  # search_indexer normally returns errors, never raises
                logger.warning("Indexer %s search failed: %s", src.id, exc)
                outcome = {"success": False, "results": [], "error": str(exc)}
            error = outcome.get("error")
            if error:
                logger.warning("Indexer %s search failed: %s", src.id, error)
                dead[src.id] = f"{src.name}: {error}"
                return [], error
            results = outcome.get("results", [])
            for r in results:
                r.setdefault("indexer", src.name)
                r["source_type"] = src.type
            return results, None

        private = sorted(
            (s for s in sources if s.enabled and s.id and s.type == "private" and s.id not in dead),
            key=source_popularity, reverse=True,
        )
        public = sorted(
            (s for s in sources if s.enabled and s.id and s.type != "private" and s.id not in dead),
            key=source_popularity, reverse=True,
        )
        full_public_count = len(public)
        if quick:
            public = public[:1]
        min_seeders = self.config.sources.min_seeders
        batch_size = max(1, self.config.sources.search_batch_size)
        min_results = max(1, self.config.sources.min_results)

        def _failure_or_empty() -> Dict[str, Any]:
            """Zero hits: an explicit failure when every queried indexer errored
            (the agent must retry, not conclude 'no results'), a clean empty
            result when at least one indexer answered."""
            if queried and failed == queried:
                return {
                    "success": False,
                    "results": [],
                    "error": f"All {queried} Jackett indexer(s) failed to respond",
                    "errors": list(local_errors),
                }
            result: Dict[str, Any] = {"success": False, "results": []}
            if local_errors:
                result["errors"] = list(local_errors)
            return result

        # Private tier: query all private indexers in parallel.
        private_results: List[Dict[str, Any]] = []
        if private:
            self._progress(f"Querying {len(private)} private indexer(s): {', '.join(s.name for s in private)}…")
            with ThreadPoolExecutor(max_workers=min(6, len(private))) as pool:
                futures = {pool.submit(_search_one, s): s for s in private}
                for future in as_completed(futures):
                    src = futures[future]
                    queried += 1
                    results, error = future.result()
                    if error:
                        failed += 1
                        local_errors.append(f"{src.name}: {error}")
                        self._progress(f"{src.name}: query failed — {error}")
                        continue
                    best = max((r.get("seeders", 0) or 0 for r in results), default=0)
                    detail = f", best {best} seeds" if results else ""
                    self._progress(f"{src.name}: {len(results)} result(s){detail}")
                    private_results.extend(results)
            best_private = max((r.get("seeders", 0) or 0 for r in private_results), default=0)
            if best_private >= min_seeders:
                self._progress(
                    f"Private trackers have enough seeds ({best_private}) — "
                    f"skipping {len(public)} public indexer(s)"
                )
                ranked = self._rank_results(private_results, query)
                result: Dict[str, Any] = {
                    "success": True,
                    "count": len(ranked),
                    "results": ranked,
                    "note": f"Private trackers already have up to {best_private} seeders; public indexers were not searched.",
                }
                if local_errors:
                    result["errors"] = list(local_errors)
                    result["note"] += f" {len(local_errors)} indexer(s) failed — results may be incomplete."
                return self._annotate_quick(result, quick, remaining_public=full_public_count)
            if private_results:
                self._progress(f"Private results too weak (best {best_private} seeds) — expanding to public indexers")

        # Public tier: popularity-ordered batches, stop once good enough.
        all_results = list(private_results)
        for start in range(0, len(public), batch_size):
            batch = public[start:start + batch_size]
            self._progress(
                f"Querying public indexers ({start + 1}–{min(start + batch_size, len(public))} of {len(public)}): "
                f"{', '.join(s.name for s in batch)}…"
            )
            with ThreadPoolExecutor(max_workers=min(12, len(batch))) as pool:
                futures = {pool.submit(_search_one, s): s for s in batch}
                for future in as_completed(futures):
                    src = futures[future]
                    queried += 1
                    results, error = future.result()
                    if error:
                        failed += 1
                        local_errors.append(f"{src.name}: {error}")
                        self._progress(f"{src.name}: query failed — {error}")
                        continue
                    if results:
                        best = max((r.get("seeders", 0) or 0 for r in results), default=0)
                        self._progress(f"{src.name}: {len(results)} result(s), best {best} seeds")
                    all_results.extend(results)
            best = max((r.get("seeders", 0) or 0 for r in all_results), default=0)
            if best >= min_seeders or len(all_results) >= min_results:
                logger.info(
                    "Jackett public search stopped early: %d results, best %d seeds, after %d/%d indexers",
                    len(all_results), best, min(start + batch_size, len(public)), len(public),
                )
                self._progress(
                    f"Good enough (best {best} seeds) — skipping remaining "
                    f"{max(0, len(public) - start - batch_size)} public indexer(s)"
                )
                break

        if not all_results:
            return _failure_or_empty()
        ranked = self._rank_results(all_results, query)
        result = {"success": True, "count": len(ranked), "results": ranked}
        if local_errors:
            result["errors"] = list(local_errors)
            result["note"] = f"{len(local_errors)} indexer(s) failed — results may be incomplete."
        return self._annotate_quick(
            result,
            quick,
            remaining_public=max(0, full_public_count - len(public)),
        )

    @staticmethod
    def _annotate_quick(result: Dict[str, Any], quick: bool, remaining_public: int) -> Dict[str, Any]:
        """Tag a quick-pass result with scope + how to go deeper."""
        if not quick:
            result["scope"] = "deep"
            return result
        result["scope"] = "quick"
        result["deep_available"] = remaining_public > 0
        note = result.get("note", "")
        if remaining_public:
            note += (f" Quick pass: only the top source(s) were searched — call again "
                     f"with deep=true to sweep {remaining_public} more source(s) if these "
                     f"results aren't good enough.")
        result["note"] = note.strip()
        return result

    def _rank_results(self, results: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        """Dedupe by normalized title, then rank by query similarity + seeders.

        Private-tracker results get a small boost so they win ties, but a clearly
        better-seeded public result still outranks a weak private one."""
        import difflib
        import math

        def _norm(name: str) -> str:
            return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()

        q = _norm(query)
        best_by_name: Dict[str, Dict[str, Any]] = {}
        for r in results:
            key = _norm(r.get("name", ""))
            if not key:
                continue
            current = best_by_name.get(key)
            if current is None or (r.get("seeders", 0) or 0) > (current.get("seeders", 0) or 0):
                best_by_name[key] = r

        def _score(r: Dict[str, Any]) -> float:
            sim = difflib.SequenceMatcher(None, q, _norm(r.get("name", ""))).ratio() if q else 1.0
            seeds = math.log1p(r.get("seeders", 0) or 0) / math.log1p(1000)
            private_boost = 0.15 if r.get("source_type") == "private" else 0.0
            return 0.6 * sim + 0.4 * seeds + private_boost

        return sorted(best_by_name.values(), key=_score, reverse=True)

    def _search_rss_feeds(self, query: str) -> List[Dict[str, Any]]:
        """Match a query against items from configured RSS feeds (parallel fetch)."""
        feeds = self.config.rss.feeds
        if not feeds:
            return []
        tokens = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2][:4]
        if not tokens:
            return []

        def _check(feed) -> List[Dict[str, Any]]:
            try:
                result = self._rss_monitor.check_feed(feed)
            except Exception as exc:
                logger.debug("RSS search failed for %s: %s", feed.url, exc)
                return []
            matches = []
            for item in result.get("all_items", []):
                title = (item.get("title") or "").lower()
                if all(t in title for t in tokens) and (item.get("magnet_uri") or item.get("torrent_url")):
                    item = dict(item)
                    item["feed_name"] = result.get("feed_name", feed.url)
                    matches.append(item)
            return matches

        from concurrent.futures import ThreadPoolExecutor
        self._progress(f"Checking {len(feeds)} RSS subscription(s) for matches…")
        matches: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(6, len(feeds))) as pool:
            for feed_matches in pool.map(_check, feeds):
                matches.extend(feed_matches)
        if matches:
            self._progress(f"RSS: {len(matches)} matching item(s) with direct download links")
        return matches

    def _search_source_tier(self, query: str, sources: List[Any]) -> List[Dict[str, Any]]:
        """Search a tier of sources in popularity-ordered concurrent batches.

        Stops early once `config.sources.min_results` results have been found,
        so the most popular sites are queried first and the rest are skipped
        when they are not needed."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        batch_size = max(1, self.config.sources.search_batch_size)
        min_results = max(1, self.config.sources.min_results)

        def _search_one(src):
            site_domain = self._extract_domain(src.url)
            if not site_domain:
                return []
            site_query = f"{query} site:{site_domain}"
            try:
                web_results = self._web_search.search(site_query, limit=5, llm=False)
                results = []
                for r in web_results.get("results", []):
                    r["source_name"] = src.name
                    r["source_url"] = src.url
                    r["source_type"] = src.type
                    results.append(r)
                return results
            except Exception as exc:
                logger.warning("Source search failed for %s: %s", src.name, exc)
                self._progress(f"{src.name}: search failed — {exc}")
                return []

        all_results: List[Dict[str, Any]] = []
        for start in range(0, len(sources), batch_size):
            batch = sources[start:start + batch_size]
            self._progress(
                f"Searching sites ({start + 1}–{min(start + batch_size, len(sources))} of {len(sources)}): "
                f"{', '.join(s.name for s in batch)}…"
            )
            with ThreadPoolExecutor(max_workers=min(12, len(batch))) as pool:
                futures = {pool.submit(_search_one, src): src for src in batch}
                for future in as_completed(futures):
                    src = futures[future]
                    hits = future.result()
                    self._progress(f"{src.name}: {len(hits)} hit(s)")
                    all_results.extend(hits)
            if len(all_results) >= min_results:
                logger.info(
                    "Source search stopped early: %d results after %d/%d sources",
                    len(all_results), min(start + batch_size, len(sources)), len(sources),
                )
                self._progress(
                    f"Found {len(all_results)} results — skipping remaining "
                    f"{max(0, len(sources) - start - batch_size)} site(s)"
                )
                break
        return all_results

    def _prioritize_private_results(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Prefer private-tracker results in a Jackett response.

        Jackett queries all indexers in one call, but if a private tracker
        already offers a result with enough seeds, the public hits are just
        noise and get dropped. Otherwise results are ordered private-first,
        then by seeders."""
        type_by_indexer: Dict[str, str] = {}
        for s in self.config.sources.sources:
            type_by_indexer[s.id.lower()] = s.type
            type_by_indexer[s.name.lower()] = s.type

        results = result.get("results", [])
        for r in results:
            r["source_type"] = type_by_indexer.get(str(r.get("indexer", "")).lower(), "public")

        private_hits = [r for r in results if r["source_type"] == "private"]
        min_seeders = self.config.sources.min_seeders
        best_private_seeds = max((r.get("seeders", 0) or 0 for r in private_hits), default=0)
        if private_hits and best_private_seeds >= min_seeders:
            kept = sorted(private_hits, key=lambda r: r.get("seeders", 0) or 0, reverse=True)
            return {
                **result,
                "count": len(kept),
                "results": kept,
                "note": f"Private tracker results already have up to {best_private_seeds} seeders; public results were skipped.",
            }

        ordered = sorted(
            results,
            key=lambda r: (r["source_type"] != "private", -(r.get("seeders", 0) or 0)),
        )
        return {**result, "results": ordered}

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract the domain from a URL for site: queries."""
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            domain = parsed.netloc or parsed.path.split("/")[0]
            # Remove www. prefix.
            if domain.startswith("www."):
                domain = domain[4:]
            return domain.strip("/")
        except Exception:
            return ""

    def _find_alt_release(self, torrent_name: str) -> Dict[str, Any]:
        query = f'"{torrent_name}" alternate release group torrent'
        return self._web_search.search(query, limit=10)

    def _propose_rename_and_category(self, info_hash: str, categories: Optional[List[str]] = None) -> Dict[str, Any]:
        # Compatibility alias for the read-only organization analysis.
        return self._analyze_organization(info_hash, categories)

    def _analyze_organization(self, info_hash: str, categories: Optional[List[str]] = None) -> Dict[str, Any]:
        from agent.organizer import Organizer

        status = self.engine.get_torrent_status(info_hash)
        if status.get("progress", 0) < 1.0:
            return {"success": False, "error": "Torrent must be complete before it can be organized"}
        organizer = Organizer(self.config.default_save_path)
        proposal = organizer.build_proposal(status, categories or self.config.categories)
        proposal["success"] = True
        return proposal

    def _apply_organization_plan(
        self,
        info_hash: str,
        category: str,
        destination: str,
        file_renames: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        base = os.path.realpath(self.config.default_save_path)
        destination = os.path.realpath(destination)
        try:
            inside_base = os.path.commonpath([base, destination]) == base
        except ValueError:
            inside_base = False
        if not inside_base:
            raise ToolError(f"Organization destination must stay inside the default save path: {base}")
        if category not in self.config.categories:
            raise ToolError(f"Unknown category: {category}")
        return self.engine.organize_torrent(info_hash, destination, category, file_renames or [])

    # ------------------------------------------------------------------
    # General web access
    # ------------------------------------------------------------------

    def _web_search_tool(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """General-purpose web search available to the agent."""
        self._progress(f"Web search: '{query}'")
        result = self._web_search.search(query, limit=limit)
        self._progress(f"Web search returned {len(result.get('results', []))} result(s)")
        return result

    def _web_fetch_tool(self, url: str = "", urls: Optional[List[str]] = None, max_chars: int = 8000) -> Dict[str, Any]:
        """Fetch and extract text content from one or more web pages.

        Multiple URLs are fetched concurrently inside a single tool call so
        the agent doesn't pay a full LLM round-trip per page."""
        targets = ([url] if url else []) + list(urls or [])
        targets = [u for u in targets if u]
        if not targets:
            raise ToolError("web_fetch requires 'url' or a non-empty 'urls' list")
        if len(targets) == 1:
            return self._fetch_one(targets[0], max_chars)

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            results = list(pool.map(lambda u: self._fetch_one(u, max_chars), targets))
        succeeded = sum(1 for result in results if result.get("success"))
        return {
            "success": succeeded > 0,
            "partial": 0 < succeeded < len(results),
            "count": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "results": results,
        }

    def _fetch_one(self, url: str, max_chars: int) -> Dict[str, Any]:
        """Fetch a single page and extract its text content."""
        self._progress(f"Fetching {url}…")
        try:
            resp = public_http_get(url, timeout=30, headers={"User-Agent": BROWSER_UA})
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("web_fetch failed for %s: %s", url, exc)
            self._progress(f"Fetch failed: {url} — {exc}")
            return {"success": False, "url": url, "error": str(exc)}

        text = resp.text
        result: Dict[str, Any] = {
            "success": True,
            "url": url,
            "status_code": resp.status_code,
        }
        # Extract magnet links, .torrent URLs and direct-download links from
        # the RAW html before tag stripping — they live in href attributes,
        # which the text extraction would otherwise destroy.
        from urllib.parse import urljoin

        magnets = re.findall(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+[^\"\s<>']*", resp.text)
        if magnets:
            result["magnets"] = list(dict.fromkeys(magnets))[:10]
        torrent_urls = re.findall(r"href=[\"']([^\"']+\.torrent[^\"']*)[\"']", resp.text, flags=re.IGNORECASE)
        if torrent_urls:
            result["torrent_urls"] = list(dict.fromkeys(
                urljoin(url, u) for u in torrent_urls))[:10]
        # Direct file/stream links — the add_download tool can grab these.
        file_links = re.findall(
            r"href=[\"']([^\"']+\.(?:zip|rar|7z|iso|img|exe|msi|apk|dmg|pkg|deb|rpm|"
            r"tar|gz|xz|mp4|mkv|avi|mov|mp3|flac|wav|pdf|epub|m3u8|mpd)"
            r"(?:\?[^\"']*)?)[\"']",
            resp.text, flags=re.IGNORECASE)
        if file_links:
            result["download_links"] = list(dict.fromkeys(
                urljoin(url, u) for u in file_links))[:10]

        # Strip HTML tags for a clean text payload.
        if "html" in resp.headers.get("content-type", "").lower():
            text = self._html_to_text(text)
        truncated = len(text) > max_chars
        result.update({
            "content": text[:max_chars],
            "truncated": truncated,
            "total_chars": len(text),
        })
        extra = f", {len(result['magnets'])} magnet(s) found" if result.get("magnets") else ""
        self._progress(f"Fetched {url} — {len(text)} chars{extra}")
        return result

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Crude HTML-to-text conversion: remove tags, scripts, styles, collapse whitespace."""
        # Remove script and style blocks entirely.
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.IGNORECASE | re.DOTALL)
        # Remove all remaining tags.
        text = re.sub(r"<[^>]+>", " ", html)
        # Unescape HTML entities.
        import html as html_mod
        text = html_mod.unescape(text)
        # Collapse whitespace.
        text = re.sub(r"\s+", " ", text).strip()
        return text

    # ------------------------------------------------------------------
    # Memory handlers (persistent local markdown memory)
    # ------------------------------------------------------------------

    def _save_memory_tool(self, content: str, scope: str = "fact") -> Dict[str, Any]:
        if self.memory is None:
            return {"success": False, "error": "Memory is disabled in settings."}
        result = self.memory.save(content, scope)
        if result.get("success"):
            self._progress(
                "Memory already saved" if result.get("duplicate")
                else f"Memory saved ({result.get('scope')})"
            )
        return result

    def _search_memory_tool(self, query: str) -> Dict[str, Any]:
        if self.memory is None:
            return {"success": False, "error": "Memory is disabled in settings."}
        return self.memory.search(query)

    def _list_memories_tool(self, scope: str = "", limit: int = 200) -> Dict[str, Any]:
        if self.memory is None:
            return {"success": False, "error": "Memory is disabled in settings."}
        return self.memory.list_entries(scope, limit)

    def _edit_memory_tool(self, memory_id: str, content: str) -> Dict[str, Any]:
        if self.memory is None:
            return {"success": False, "error": "Memory is disabled in settings."}
        return self.memory.edit(memory_id, content)

    def _forget_memory_tool(self, memory_id: str) -> Dict[str, Any]:
        if self.memory is None:
            return {"success": False, "error": "Memory is disabled in settings."}
        return self.memory.forget(memory_id)

    # ------------------------------------------------------------------
    # IRC handlers (embedded IRC client — shared with the GUI's IRC tab)
    # ------------------------------------------------------------------

    def _get_irc_client(self):
        """The IRC client core — injected by the GUI; lazily created (and
        started) on first use elsewhere, e.g. the CLI REPL."""
        if self._irc_client is None:
            with self._resource_lock:
                if self._irc_client is None:
                    from ircmgr.client import IRCClientCore

                    self._irc_client = IRCClientCore(self.config.irc)
                    self._owns_irc_client = True
                    self._irc_client.start()
                    logger.info("irc tool: lazily started an IRCClientCore")
        return self._irc_client

    @staticmethod
    def _fmt_irc_messages(messages: List[Dict[str, Any]]) -> List[str]:
        import datetime as _dt

        lines = []
        for m in messages:
            stamp = _dt.datetime.fromtimestamp(m.get("ts", 0)).strftime("%H:%M")
            kind = m.get("kind", "msg")
            nick = m.get("nick", "")
            text = m.get("text", "")
            if kind == "msg":
                lines.append(f"[{stamp}] <{nick}> {text}")
            elif kind == "action":
                lines.append(f"[{stamp}] * {nick} {text}")
            elif kind == "notice":
                lines.append(f"[{stamp}] -{nick}- {text}")
            else:
                lines.append(f"[{stamp}] — {text}")
        return lines

    def _irc_status(self) -> Dict[str, Any]:
        client = self._get_irc_client()
        snap = client.status()
        snap["success"] = True
        if not snap["networks"]:
            snap["note"] = ("No IRC networks configured yet. The user can add one from the "
                            "IRC tab (Networks… button).")
        return snap

    def _irc_list_messages(self, channel: str = "", network: str = "",
                           limit: int = 50, minutes: int = 0) -> Dict[str, Any]:
        client = self._get_irc_client()
        net_id, err = client.resolve_network(network, channel)
        if err:
            return {"success": False, "error": err}
        limit = max(1, min(int(limit or 50), 200))
        since = time.time() - minutes * 60 if minutes else 0.0
        messages = client.get_messages(net_id, channel or None, limit=limit, since=since)
        return {
            "success": True,
            "network": net_id,
            "channel": channel or "(server)",
            "count": len(messages),
            "messages": self._fmt_irc_messages(messages),
            "note": "Only buffered messages are available (buffer size: "
                    f"{self.config.irc.buffer_lines} lines/channel).",
        }

    def _irc_search_messages(self, query: str, channel: str = "",
                             network: str = "", limit: int = 30) -> Dict[str, Any]:
        client = self._get_irc_client()
        if network:
            net_id, err = client.resolve_network(network, channel)
            if err:
                return {"success": False, "error": err}
        else:
            net_id = None
        limit = max(1, min(int(limit or 30), 100))
        hits = client.search_messages(query, net_id=net_id, channel=channel or None,
                                      limit=limit)
        return {
            "success": True,
            "query": query,
            "count": len(hits),
            "hits": [
                f"[{time.strftime('%H:%M', time.localtime(h['ts']))}] "
                f"{h['network']}/{h['channel']} <{h['nick']}> {h['text']}"
                for h in hits
            ],
        }

    def _irc_send_message(self, target: str, text: str, network: str = "") -> Dict[str, Any]:
        client = self._get_irc_client()
        net_id, err = client.resolve_network(network, target)
        if err:
            return {"success": False, "error": err}
        client.send_message(net_id, target, text)
        return {"success": True, "network": net_id, "target": target,
                "note": "Queued (outgoing messages are flood-throttled)."}

    def _irc_join(self, channel: str, network: str = "") -> Dict[str, Any]:
        client = self._get_irc_client()
        net_id, err = client.resolve_network(network)
        if err:
            return {"success": False, "error": err}
        client.join(net_id, channel)
        chan = channel if channel.startswith(("#", "&", "+", "!")) else "#" + channel
        return {"success": True, "network": net_id, "channel": chan,
                "note": "Join sent — messages will start buffering once the server confirms."}

    def _irc_part(self, channel: str, network: str = "") -> Dict[str, Any]:
        client = self._get_irc_client()
        net_id, err = client.resolve_network(network, channel)
        if err:
            return {"success": False, "error": err}
        client.part(net_id, channel)
        return {"success": True, "network": net_id, "channel": channel}

    def _irc_connect(self, network: str = "") -> Dict[str, Any]:
        client = self._get_irc_client()
        # Resolve against the CONFIGURED networks (not just connected ones).
        candidates = self.config.irc.networks
        if network:
            candidates = [n for n in candidates if network.lower() in (n.id or "").lower()
                          or network.lower() in (n.host or "").lower()]
        if not candidates:
            return {"success": False, "error": f"No configured network matching '{network}'. "
                                               "The user can add one from the IRC tab (Networks…)."}
        if len(candidates) > 1:
            return {"success": False, "error": "Ambiguous network — matches: "
                                               + ", ".join(n.id for n in candidates)}
        net = candidates[0]
        connected = {n["id"] for n in client.status().get("networks", []) if n.get("connected")}
        if net.id in connected:
            return {"success": True, "network": net.id, "note": "Already connected."}
        client.connect_network(net)
        return {"success": True, "network": net.id,
                "note": "Connecting — check irc_status for the link state."}

    def _irc_disconnect(self, network: str = "", message: str = "") -> Dict[str, Any]:
        client = self._get_irc_client()
        net_id, err = client.resolve_network(network)
        if err:
            return {"success": False, "error": err}
        client.disconnect_network(net_id, message or "DeepFlux")
        return {"success": True, "network": net_id}

    def _irc_send_action(self, target: str, text: str, network: str = "") -> Dict[str, Any]:
        client = self._get_irc_client()
        net_id, err = client.resolve_network(network, target)
        if err:
            return {"success": False, "error": err}
        client.send_action(net_id, target, text)
        return {"success": True, "network": net_id, "target": target,
                "note": "Queued (outgoing messages are flood-throttled)."}

    def _irc_send_notice(self, target: str, text: str, network: str = "") -> Dict[str, Any]:
        client = self._get_irc_client()
        net_id, err = client.resolve_network(network, target)
        if err:
            return {"success": False, "error": err}
        client.send_notice(net_id, target, text)
        return {"success": True, "network": net_id, "target": target,
                "note": "Queued (outgoing messages are flood-throttled)."}

    def _irc_set_nick(self, new_nick: str, network: str = "") -> Dict[str, Any]:
        client = self._get_irc_client()
        net_id, err = client.resolve_network(network)
        if err:
            return {"success": False, "error": err}
        client.change_nick(net_id, new_nick)
        return {"success": True, "network": net_id, "new_nick": new_nick,
                "note": "Nick change sent — the server may reject taken/invalid nicks."}

    def _irc_send_raw(self, line: str, network: str = "") -> Dict[str, Any]:
        client = self._get_irc_client()
        net_id, err = client.resolve_network(network)
        if err:
            return {"success": False, "error": err}
        client.send_raw(net_id, line)
        return {"success": True, "network": net_id, "line": line}

    def _irc_list_channels(self, network: str = "", filter: str = "",
                           refresh: bool = True, limit: int = 50) -> Dict[str, Any]:
        client = self._get_irc_client()
        net_id, err = client.resolve_network(network)
        if err:
            return {"success": False, "error": err}
        limit = max(1, min(int(limit or 50), 500))
        if refresh:
            client.send_raw(net_id, "LIST " + filter.strip() if filter.strip() else "LIST")
        rows = client.state.chanlist_of(net_id)
        out: Dict[str, Any] = {
            "success": True,
            "network": net_id,
            "count": len(rows),
            "channels": rows[:limit],
            "pending": bool(refresh),
            "cached_at": client.state.chanlist_ts(net_id),
        }
        if refresh:
            out["note"] = ("LIST queued; returning the current cache without waiting. "
                           "Call again with refresh=false for the completed reply.")
        elif not rows:
            out["note"] = "No cached channel list is available yet."
        return out

    def _irc_list_nicks(self, channel: str, network: str = "") -> Dict[str, Any]:
        client = self._get_irc_client()
        net_id, err = client.resolve_network(network, channel)
        if err:
            return {"success": False, "error": err}
        nicks = client.state.nicks_of(net_id, channel)
        if not nicks:
            return {"success": False, "error": f"No nick list for {channel} — is it joined?"}
        formatted = [f"{prefix}{nick}" for nick, prefix in nicks.items()]
        return {"success": True, "network": net_id, "channel": channel,
                "count": len(formatted), "nicks": formatted,
                "topic": client.state.topic_of(net_id, channel)}

    # ------------------------------------------------------------------
    # Filesystem handlers (agent-level file ops — the Command tab's domain)
    # ------------------------------------------------------------------

    def _list_directory(self, path: str, limit: int = 200) -> Dict[str, Any]:
        limit = max(1, min(int(limit or 200), 1000))
        entries: List[Dict[str, Any]] = []
        with os.scandir(path) as it:
            for i, e in enumerate(it):
                if i >= limit:
                    break
                try:
                    st = e.stat()
                    entries.append({
                        "name": e.name,
                        "type": "dir" if e.is_dir() else "file",
                        "size": 0 if e.is_dir() else st.st_size,
                        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                    })
                except OSError:
                    entries.append({"name": e.name, "type": "unknown"})
        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return {"success": True, "path": os.path.abspath(path), "count": len(entries), "entries": entries}

    def _create_folder(self, path: str) -> Dict[str, Any]:
        os.makedirs(path, exist_ok=True)
        return {"success": True, "path": os.path.abspath(path)}

    @staticmethod
    def _resolve_destination(source: str, destination: str, overwrite: bool) -> str:
        """Resolve the final destination path and enforce the overwrite guard."""
        if not os.path.exists(source):
            raise ToolError(f"Source not found: {source}")
        dst = destination
        if os.path.isdir(dst):
            dst = os.path.join(dst, os.path.basename(source))
        if os.path.exists(dst):
            if not overwrite:
                raise ToolError(f"Destination exists (pass overwrite=true to replace): {dst}")
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            else:
                os.remove(dst)
        return dst

    def _copy_path(self, source: str, destination: str, overwrite: bool = False) -> Dict[str, Any]:
        dst = self._resolve_destination(source, destination, overwrite)
        if os.path.isdir(source):
            shutil.copytree(source, dst)
        else:
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copy2(source, dst)
        return {"success": True, "source": source, "destination": dst}

    def _move_path(self, source: str, destination: str, overwrite: bool = False) -> Dict[str, Any]:
        dst = self._resolve_destination(source, destination, overwrite)
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.move(source, dst)
        return {"success": True, "source": source, "destination": dst}

    def _rename_path(self, path: str, new_name: str) -> Dict[str, Any]:
        if not os.path.exists(path):
            raise ToolError(f"Path not found: {path}")
        new_name = new_name.strip()
        if not new_name or os.path.basename(new_name) != new_name:
            raise ToolError("new_name must be a plain name, not a path")
        dst = os.path.join(os.path.dirname(os.path.abspath(path)), new_name)
        if os.path.exists(dst):
            raise ToolError(f"A file/folder named '{new_name}' already exists there")
        os.rename(path, dst)
        return {"success": True, "old_path": os.path.abspath(path), "new_path": dst}

    def _delete_path(self, path: str, recursive: bool = False) -> Dict[str, Any]:
        if not os.path.exists(path):
            raise ToolError(f"Path not found: {path}")
        if os.path.isdir(path) and not os.path.islink(path):
            if recursive:
                shutil.rmtree(path)
            else:
                os.rmdir(path)  # raises OSError when non-empty — surfaced as an error
        else:
            os.remove(path)
        return {"success": True, "deleted": os.path.abspath(path)}

    # ------------------------------------------------------------------
    # IPTV handlers (the Play tab — reads via IPTVManager, playback via the
    # GUI-injected bridge which marshals actions onto the Qt thread)
    # ------------------------------------------------------------------

    def set_iptv_bridge(self, bridge) -> None:
        """Inject the GUI's IPTV bridge (called once the Play tab exists)."""
        self._iptv_bridge = bridge

    def _get_iptv_manager(self):
        """The IPTVManager — the GUI bridge's when available (its playlist is
        already loaded); otherwise lazily created from config (CLI) with a
        blocking first load of the active source."""
        if self._iptv_bridge is not None:
            return self._iptv_bridge.manager
        if self._iptv_manager is None:
            with self._resource_lock:
                if self._iptv_manager is None:
                    from iptv.manager import IPTVManager
                    from iptv.models import PlaylistSource

                    cfg = self.config.iptv
                    self._iptv_manager = IPTVManager(
                        sources=[
                            PlaylistSource(
                                id=s.id, name=s.name, kind=s.kind, url=s.url,
                                user_agent=s.user_agent, referer=s.referer,
                                username=s.username, password=s.password,
                                enabled=s.enabled, auto_refresh_minutes=s.auto_refresh_minutes,
                                epg_url=getattr(s, "epg_url", ""),
                            )
                            for s in cfg.sources
                        ],
                        tmdb_api_key=cfg.tmdb_api_key,
                        data_dir=cfg.cache_dir or None,
                        cache_seconds=cfg.cache_seconds,
                        hwdec=cfg.hwdec,
                    )
                    self._owns_iptv_manager = True
                    src = self._iptv_manager.active_source()
                    if src is not None:
                        logger.info("iptv tool: lazily loading playlist for %s", src.name)
                        self._iptv_manager.load_source_async(src).join(timeout=60)
        return self._iptv_manager

    @staticmethod
    def _fmt_iptv_item(item: Any) -> Dict[str, Any]:
        from iptv.models import Channel, Movie, Series

        out: Dict[str, Any] = {
            "id": getattr(item, "id", ""),
            "name": getattr(item, "name", ""),
            "section": getattr(item, "section", ""),
            "group": getattr(item, "group", ""),
        }
        if isinstance(item, Channel):
            out["display_name"] = item.display_name
            if item.epg_now:
                out["epg_now"] = item.epg_now
        elif isinstance(item, (Movie, Series)):
            if getattr(item, "year", ""):
                out["year"] = item.year
            if getattr(item, "rating", 0.0):
                out["rating"] = item.rating
        if isinstance(item, Series):
            out["episodes"] = len(item.episodes)
        return out

    def _iptv_search(self, query: str, section: str = "", limit: int = 20) -> Dict[str, Any]:
        limit = max(1, min(int(limit or 20), 100))
        manager = self._get_iptv_manager()
        hits = manager.search(query)
        if section:
            hits = {section: hits.get(section, [])}
        sections: Dict[str, Any] = {}
        total = 0
        for sec, items in hits.items():
            sections[sec] = [self._fmt_iptv_item(i) for i in items[:limit]]
            total += len(items)
        out: Dict[str, Any] = {"success": True, "query": query, "total_found": total, "sections": sections}
        if not total:
            out["note"] = ("No matches. The playlist may not be loaded yet — the user can pick a "
                           "source in the Play tab, or add one in Settings → IPTV.")
        return out

    def _iptv_list(self, section: str = "live", category: str = "", limit: int = 50) -> Dict[str, Any]:
        from iptv.models import ALL_SECTIONS

        limit = max(1, min(int(limit or 50), 200))
        if section not in ALL_SECTIONS:
            raise ToolError(f"section must be one of: {', '.join(ALL_SECTIONS)}")
        manager = self._get_iptv_manager()
        items = manager.items_for(section, category)
        out: Dict[str, Any] = {
            "success": True,
            "section": section,
            "category": category,
            "total": len(items),
            "items": [self._fmt_iptv_item(i) for i in items[:limit]],
        }
        if not category:
            out["categories"] = [
                {"name": c.name, "count": c.count} for c in manager.categories_for(section)
            ]
        return out

    def _iptv_epg(self, channel: str) -> Dict[str, Any]:
        from iptv.models import SECTION_LIVE

        manager = self._get_iptv_manager()
        q = channel.lower()
        match = None
        for ch in manager.items_for(SECTION_LIVE):
            if q in (ch.display_name or "").lower() or q in (ch.name or "").lower():
                match = ch
                if (ch.display_name or "").lower() == q or (ch.name or "").lower() == q:
                    break  # exact match wins
        if match is None:
            return {"success": False, "error": f"No live channel matching '{channel}'."}
        now_next = manager.epg_now_next(match.tvg_id) if match.tvg_id else {}
        out: Dict[str, Any] = {"success": True, "channel": match.display_name, "tvg_id": match.tvg_id}
        out.update(now_next)
        if not any(now_next.values()):
            out["note"] = "No EPG data for this channel (playlist may not declare an XMLTV guide)."
        return out

    def _iptv_now_playing(self) -> Dict[str, Any]:
        if self._iptv_bridge is None:
            return {"success": False, "error": "Player status is only available in the GUI."}
        status = self._iptv_bridge.status()
        status["success"] = True
        return status

    # -- OpenSubtitles (player subtitle downloads) -----------------------------

    def _ost_client(self):
        from iptv.opensubtitles import OpenSubtitlesClient
        cfg = self.config.iptv
        return OpenSubtitlesClient(cfg.opensubtitles_api_key,
                                   cfg.opensubtitles_username,
                                   cfg.opensubtitles_password)

    def _ost_context(self, query: str, languages: str) -> Dict[str, str]:
        """Resolve file path / title / languages for the playing video."""
        file_path, title = "", ""
        if self._iptv_bridge is not None:
            st = self._iptv_bridge.status()
            url = st.get("url", "")
            if url and os.path.isfile(url):
                file_path = url
            title = st.get("title", "")
        from iptv.opensubtitles import clean_media_query
        return {
            "file_path": file_path,
            "query": query or clean_media_query(file_path or title),
            "languages": languages or self.config.iptv.preferred_sub_lang or "en",
        }

    def _iptv_find_subtitles(self, query: str = "", languages: str = "", limit: int = 20) -> Dict[str, Any]:
        from iptv.opensubtitles import OpenSubtitlesError
        ctx = self._ost_context(query, languages)
        if not ctx["query"] and not ctx["file_path"]:
            return {"success": False,
                    "error": "Nothing is playing and no query was given."}
        try:
            results = self._ost_client().search(
                query=ctx["query"], file_path=ctx["file_path"], languages=ctx["languages"])
        except OpenSubtitlesError as exc:
            return {"success": False, "error": str(exc)}
        total = len(results)
        results = results[:limit]
        return {"success": True, "count": len(results), "total": total, "results": results,
                "note": "Load one with iptv_load_subtitle (file_id), or call iptv_load_subtitle "
                        "with no file_id to auto-pick the best match."}

    def _iptv_load_subtitle(self, file_id: int = 0, query: str = "",
                            languages: str = "") -> Dict[str, Any]:
        from iptv.opensubtitles import OpenSubtitlesError, pick_best, subtitle_dest_path
        if self._iptv_bridge is None:
            return {"success": False, "error": "Loading subtitles requires the GUI player."}
        if not self._iptv_bridge.status().get("url"):
            return {"success": False, "error": "Nothing is playing right now."}
        ctx = self._ost_context(query, languages)
        client = self._ost_client()
        try:
            if file_id:
                entry = {"file_id": int(file_id),
                         "language": ctx["languages"].split(",")[0], "release": ""}
            else:
                results = client.search(query=ctx["query"], file_path=ctx["file_path"],
                                        languages=ctx["languages"])
                entry = pick_best(results, self.config.iptv.preferred_sub_lang)
                if entry is None:
                    return {"success": False, "error": "No subtitles found for this video."}
            dest = subtitle_dest_path(ctx["file_path"],
                                      entry.get("release") or ctx["query"],
                                      entry.get("language", ""))
            client.download(int(entry["file_id"]), dest)
        except OpenSubtitlesError as exc:
            return {"success": False, "error": str(exc)}
        self._iptv_bridge.add_subtitle_file(dest)
        return {"success": True, "path": dest, "release": entry.get("release", ""),
                "language": entry.get("language", ""),
                "hash_match": bool(entry.get("hash_match"))}

    def _iptv_play(self, item_id: str = "", query: str = "", url: str = "",
                   title: str = "", file: str = "") -> Dict[str, Any]:
        from iptv.models import SECTION_SERIES, Series

        bridge = self._iptv_bridge
        if bridge is None:
            return {"success": False, "error": "IPTV playback requires the GUI (the Play tab)."}
        if file:
            if not os.path.isfile(file):
                raise ToolError(f"File not found: {file}")
            bridge.play_file(file)
            return {"success": True, "playing": os.path.basename(file),
                    "note": "Play request sent to the Play tab."}
        if url:
            bridge.play_url(url, title)
            return {"success": True, "playing": title or url,
                    "note": "Play request sent to the Play tab."}

        manager = self._get_iptv_manager()
        item = None
        if item_id:
            playlist = manager.current_playlist()
            pool = (playlist.channels + playlist.movies + playlist.series) if playlist else []
            item = next((i for i in pool if i.id == item_id), None)
            if item is None:
                return {"success": False, "error": f"No playlist item with id '{item_id}'."}
        elif query:
            hits = manager.search(query)
            q = query.lower()
            for items in hits.values():
                exact = next((i for i in items if (i.name or "").lower() == q), None)
                item = exact or (items[0] if items else None)
                if item is not None:
                    break
            if item is None:
                return {"success": False,
                        "error": f"No playlist item matching '{query}'. Try iptv_search first."}
        else:
            raise ToolError("Provide one of: item_id, query, url, or file")

        if isinstance(item, Series) or getattr(item, "section", "") == SECTION_SERIES:
            return {"success": False,
                    "error": f"'{item.name}' is a series with {len(getattr(item, 'episodes', []))} "
                             "episode(s) — name a specific episode in query, or let the user pick "
                             "one in the Play tab."}
        bridge.play_item(item)
        return {"success": True, "playing": getattr(item, "name", ""),
                "section": getattr(item, "section", ""),
                "note": "Play request sent to the Play tab."}

    def _iptv_pause(self) -> Dict[str, Any]:
        if self._iptv_bridge is None:
            return {"success": False, "error": "Playback control requires the GUI (the Play tab)."}
        self._iptv_bridge.pause()
        return {"success": True, "note": "Pause/resume toggled."}

    def _iptv_stop(self) -> Dict[str, Any]:
        if self._iptv_bridge is None:
            return {"success": False, "error": "Playback control requires the GUI (the Play tab)."}
        self._iptv_bridge.stop()
        return {"success": True, "note": "Stop sent."}

    def _iptv_set_volume(self, level: int) -> Dict[str, Any]:
        if self._iptv_bridge is None:
            return {"success": False, "error": "Playback control requires the GUI (the Play tab)."}
        level = max(0, min(100, int(level)))
        self._iptv_bridge.set_volume(level)
        return {"success": True, "volume": level}

    # ------------------------------------------------------------------
    # App-setup handlers (user decision 2026-09-12): the agent may configure
    # anything the user could have typed into a dialog — IPTV sources, API
    # keys (write-only), general settings, torrent sources, IRC networks.
    # ------------------------------------------------------------------

    def _persist_config(self) -> None:
        """Persist the live config to disk (same path every other writer uses)."""
        self.config.to_file(DeeptorrentConfig.default_config_path())

    def _apply_iptv_config_live(self) -> str:
        """Re-apply IPTV config to the running Play tab (queued onto the GUI
        thread by the bridge); returns a user-facing note either way."""
        if self._iptv_bridge is not None:
            try:
                self._iptv_bridge.request_reload()
                return "The Play tab is reloading with the new configuration."
            except Exception:
                logger.warning("iptv bridge reload failed", exc_info=True)
        return "It will be picked up on the next Play-tab refresh or app restart."

    def _find_iptv_source(self, ref: str):
        """Resolve a source by id, unique id prefix or (unique) name."""
        ref = (ref or "").strip()
        if not ref:
            return None, "source reference is required"
        sources = self.config.iptv.sources
        for source in sources:
            if source.id == ref:
                return source, None
        prefixed = [s for s in sources if s.id.startswith(ref)]
        if len(prefixed) == 1:
            return prefixed[0], None
        exact = [s for s in sources if s.name.lower() == ref.lower()]
        if len(exact) == 1:
            return exact[0], None
        partial = [s for s in sources if ref.lower() in s.name.lower()]
        if len(partial) == 1:
            return partial[0], None
        return None, f"No unique IPTV source matching '{ref}' — use iptv_list_sources for exact ids"

    def _iptv_list_sources(self) -> Dict[str, Any]:
        sources = []
        for s in self.config.iptv.sources:
            entry: Dict[str, Any] = {
                "id": s.id, "name": s.name, "kind": s.kind, "url": s.url,
                "enabled": s.enabled, "auto_refresh_minutes": s.auto_refresh_minutes,
            }
            if s.epg_url:
                entry["epg_url"] = s.epg_url
            sources.append(entry)
        return {
            "success": True,
            "sources": sources,
            "count": len(sources),
            "note": "Pass id or name to iptv_update_source / iptv_remove_source. "
                    "Credentials are never included." if sources
                    else "No sources configured — add one with iptv_add_source "
                         "(web_search can find public playlist URLs).",
        }

    def _iptv_add_source(self, url: str, name: str = "", kind: str = "m3u_url",
                         epg_url: str = "", username: str = "", password: str = "",
                         user_agent: str = "", referer: str = "", enabled: bool = True,
                         auto_refresh_minutes: int = 0, validate: bool = True) -> Dict[str, Any]:
        from config import IPTVSourceConfig

        kind = (kind or "m3u_url").strip().lower()
        if kind not in ("m3u_url", "m3u_file", "xtream", "local_folder"):
            raise ToolError("kind must be 'm3u_url', 'm3u_file', 'xtream' or 'local_folder'")
        url = (url or "").strip()
        if not url:
            raise ToolError("url is required")
        if kind in ("m3u_url", "xtream"):
            url = validate_public_http_url(url)
            if epg_url:
                epg_url = validate_public_http_url(epg_url)
        elif not os.path.exists(url):
            raise ToolError(f"Path does not exist: {url}")

        duplicate = [s.name for s in self.config.iptv.sources if s.url == url]
        if duplicate:
            return {"success": False, "error": "A source with this URL already exists: " + ", ".join(duplicate)}

        if kind == "m3u_url" and validate:
            problem = _peek_playlist(url)
            if problem:
                return {"success": False, "error": f"Not added — {problem}. If you are sure the URL is right, retry with validate=false.", "url": url}

        if not name:
            try:
                from urllib.parse import urlparse
                name = urlparse(url).hostname or "IPTV source"
            except ValueError:
                name = "IPTV source"

        source = IPTVSourceConfig(
            id=str(uuid.uuid4()), name=name, kind=kind, url=url,
            user_agent=(user_agent or "").strip(), referer=(referer or "").strip(),
            username=(username or "").strip(), password=password or "",
            enabled=bool(enabled), auto_refresh_minutes=max(0, int(auto_refresh_minutes or 0)),
            epg_url=(epg_url or "").strip(),
        )
        self.config.iptv.sources.append(source)
        self._persist_config()
        return {
            "success": True,
            "source": {"id": source.id, "name": source.name, "kind": source.kind,
                       "url": source.url, "enabled": source.enabled},
            "note": f"Source saved. {self._apply_iptv_config_live()}",
        }

    def _iptv_update_source(self, source: str, name: Optional[str] = None, url: Optional[str] = None,
                            epg_url: Optional[str] = None, enabled: Optional[bool] = None,
                            username: Optional[str] = None, password: Optional[str] = None,
                            user_agent: Optional[str] = None, referer: Optional[str] = None,
                            auto_refresh_minutes: Optional[int] = None) -> Dict[str, Any]:
        target, error = self._find_iptv_source(source)
        if target is None:
            return {"success": False, "error": error}
        if url:
            if target.kind in ("m3u_url", "xtream"):
                url = validate_public_http_url(url)
            elif not os.path.exists(url):
                return {"success": False, "error": f"Path does not exist: {url}"}
            clash = [s.name for s in self.config.iptv.sources if s.url == url and s.id != target.id]
            if clash:
                return {"success": False, "error": "Another source already uses this URL: " + ", ".join(clash)}
            target.url = url
        if epg_url is not None and target.kind != "local_folder":
            target.epg_url = validate_public_http_url(epg_url) if epg_url else ""
        if name:
            target.name = name.strip()
        if enabled is not None:
            target.enabled = bool(enabled)
        if username is not None:
            target.username = username.strip()
        if password is not None:
            target.password = password
        if user_agent is not None:
            target.user_agent = user_agent.strip()
        if referer is not None:
            target.referer = referer.strip()
        if auto_refresh_minutes is not None:
            target.auto_refresh_minutes = max(0, int(auto_refresh_minutes))
        self._persist_config()
        return {
            "success": True,
            "source": {"id": target.id, "name": target.name, "kind": target.kind,
                       "url": target.url, "enabled": target.enabled},
            "note": f"Source updated. {self._apply_iptv_config_live()}",
        }

    def _iptv_remove_source(self, source: str) -> Dict[str, Any]:
        target, error = self._find_iptv_source(source)
        if target is None:
            return {"success": False, "error": error}
        self.config.iptv.sources = [s for s in self.config.iptv.sources if s.id != target.id]
        self._persist_config()
        return {"success": True, "removed": target.name,
                "note": f"Source removed. {self._apply_iptv_config_live()}"}

    def _list_api_keys(self) -> Dict[str, Any]:
        keys = []
        for slot, (path, description) in API_KEY_SLOTS.items():
            node: Any = self.config
            try:
                for part in path:
                    node = getattr(node, part)
            except AttributeError:  # slot removed from config — skip gracefully
                continue
            keys.append({"slot": slot, "description": description, "configured": bool(node)})
        return {
            "success": True,
            "keys": keys,
            "note": "Values are write-only — never readable. Use set_api_key to write or clear one.",
        }

    def _set_api_key(self, slot: str, value: str) -> Dict[str, Any]:
        if slot not in API_KEY_SLOTS:
            raise ToolError("Unknown slot. Valid slots: " + ", ".join(sorted(API_KEY_SLOTS)))
        path, _description = API_KEY_SLOTS[slot]
        node: Any = self.config
        for part in path[:-1]:
            node = getattr(node, part)
        setattr(node, path[-1], (value or "").strip())
        self._persist_config()
        note = "Key saved."
        if path[0] == "iptv":
            note += " " + self._apply_iptv_config_live()
        elif path[0] == "llm":
            note += " The agent picks it up on the next conversation or app restart."
        return {"success": True, "slot": slot, "configured": bool((value or "").strip()), "note": note}

    def _list_settings(self, section: str = "") -> Dict[str, Any]:
        section = (section or "").strip().lower()
        rows = []
        for path, value in _iter_setting_fields(self.config):
            if section and path[0] != section:
                continue
            leaf = path[-1]
            rows.append({
                "path": ".".join(path),
                "value": _mask_setting(value) if _is_secret_leaf(leaf) else value,
                "type": type(value).__name__,
            })
        return {
            "success": True,
            "settings": rows,
            "count": len(rows),
            "note": "Use set_settings with the dotted path. Fields showing <set>/<not set> are "
                    "secrets — write-only via set_api_key.",
        }

    def _set_settings(self, path: str, value: Any) -> Dict[str, Any]:
        parts = tuple((path or "").strip().lower().split("."))
        writable = {p: v for p, v in _iter_setting_fields(self.config)}
        if parts not in writable:
            raise ToolError(
                f"Unknown or non-settable setting '{path}'. Use list_settings for valid dotted paths.")
        current = writable[parts]
        if _is_secret_leaf(parts[-1]):
            raise ToolError(
                f"'{path}' is a secret — set it with set_api_key instead (values are write-only).")
        new_value = _coerce_setting_value(parts, current, value)
        node: Any = self.config
        for part in parts[:-1]:
            node = getattr(node, part)
        setattr(node, parts[-1], new_value)
        self._persist_config()
        note = "Saved."
        if parts[0] == "iptv":
            note += " " + self._apply_iptv_config_live()
        else:
            note += " Applies on next use or restart unless the related subsystem reads it live."
        return {"success": True, "path": ".".join(parts), "value": new_value, "note": note}

    def _list_torrent_sources(self) -> Dict[str, Any]:
        rows = [{
            "id": s.id, "name": s.name, "url": s.url, "type": s.type,
            "enabled": s.enabled, "categories": list(s.categories),
        } for s in self.config.sources.sources]
        return {
            "success": True,
            "sources": rows,
            "count": len(rows),
            "use_jackett": self.config.sources.use_jackett,
            "note": "search_indexers queries the enabled ones immediately." if rows
                    else "No torrent sources configured — add one with add_torrent_source "
                         "or fetch the Jackett set (the app syncs it automatically when configured).",
        }

    def _add_torrent_source(self, name: str, url: str, id: str = "", type: str = "public",
                            categories: Optional[List[str]] = None, enabled: bool = True) -> Dict[str, Any]:
        from config import SourceConfig

        name = (name or "").strip()
        url = validate_public_http_url((url or "").strip())
        if not name:
            raise ToolError("name is required")
        type = (type or "public").strip().lower()
        if type not in ("public", "private"):
            raise ToolError("type must be 'public' or 'private'")
        slug = (id or "").strip().lower() or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        existing = self.config.sources.sources
        if any(s.id == slug for s in existing):
            return {"success": False, "error": f"A source with id '{slug}' already exists"}
        if any(s.url == url for s in existing):
            return {"success": False, "error": "A source with this URL already exists"}
        source = SourceConfig(
            id=slug, name=name, url=url, type=type, enabled=bool(enabled),
            categories=[c for c in (categories or []) if str(c).strip()],
        )
        existing.append(source)
        self._persist_config()
        return {
            "success": True,
            "source": {"id": source.id, "name": source.name, "url": source.url, "type": source.type},
            "note": "Saved — search_indexers includes it right away (a known Jackett id also "
                    "inherits its popularity ranking).",
        }

    def _remove_torrent_source(self, source: str) -> Dict[str, Any]:
        ref = (source or "").strip().lower()
        rows = self.config.sources.sources
        target = next((s for s in rows if s.id == ref), None) \
            or next((s for s in rows if s.name.lower() == ref), None) \
            or next((s for s in rows if s.url.lower() == ref), None)
        if target is None:
            return {"success": False, "error": f"No torrent source matching '{source}'"}
        self.config.sources.sources = [s for s in rows if s.id != target.id]
        self._persist_config()
        return {"success": True, "removed": target.name}

    def _irc_add_network(self, host: str, port: int = 6697, tls: bool = True,
                          id: str = "", nick: str = "", username: str = "",
                          realname: str = "", password: str = "",
                          sasl_account: str = "", sasl_password: str = "",
                          channels: Optional[List[str]] = None) -> Dict[str, Any]:
        from config import IRCNetworkConfig

        host = (host or "").strip()
        if not host:
            raise ToolError("host is required")
        port = int(port or 6697)
        if not 1 <= port <= 65535:
            raise ToolError("port must be between 1 and 65535")
        slug = (id or "").strip().lower() or re.sub(r"[^a-z0-9]+", "", host.split(".")[0].lower()) or "network"
        networks = self.config.irc.networks
        if any(n.id == slug for n in networks):
            return {"success": False, "error": f"A network with id '{slug}' already exists"}
        if any((n.host or "").lower() == host.lower() for n in networks):
            return {"success": False, "error": f"A network for {host} already exists: use irc_connect"}
        joined = []
        for channel in (channels or []):
            name = str(channel).strip()
            if name and not name.startswith(("#", "&", "+")):
                name = "#" + name
            if name:
                joined.append(name)
        network = IRCNetworkConfig(
            id=slug, host=host, port=port, tls=bool(tls),
            nick=(nick or "").strip() or "DeepFluxUser",
            username=(username or "").strip(), realname=(realname or "").strip() or "DeepFlux",
            password=password or "", sasl_account=(sasl_account or "").strip(),
            sasl_password=sasl_password or "", channels=joined,
        )
        networks.append(network)
        self._persist_config()
        return {
            "success": True,
            "network": {"id": network.id, "host": network.host, "port": network.port,
                        "tls": network.tls, "channels": joined},
            "note": "Saved. Connect with irc_connect"
                    + (f" — it will auto-join {', '.join(joined)}" if joined else "")
                    + ". The IRC tab's network picker shows it after a restart.",
        }

    def _irc_remove_network(self, network: str) -> Dict[str, Any]:
        ref = (network or "").strip().lower()
        rows = self.config.irc.networks
        target = next((n for n in rows if ref in (n.id or "").lower()
                       or ref in (n.host or "").lower()), None)
        if target is None:
            return {"success": False, "error": f"No configured IRC network matching '{network}'"}
        self.config.irc.networks = [n for n in rows if n.id != target.id]
        self._persist_config()
        return {
            "success": True,
            "removed": target.id,
            "note": "Removed from config. If it is currently connected it stays connected "
                    "until disconnected (irc_disconnect).",
        }

    # ------------------------------------------------------------------
    # Browser handlers (the Browse tab — via the GUI-injected bridge which
    # marshals every action onto the Qt thread and blocks for the result)
    # ------------------------------------------------------------------

    def set_browser_bridge(self, bridge) -> None:
        """Inject the GUI's browser bridge (called once the Browse tab exists)."""
        self._browser_bridge = bridge

    def _browser_call(self, op: str, **params) -> Dict[str, Any]:
        if self._browser_bridge is None:
            return {"success": False, "error": "Browser control requires the GUI (the Browse tab)."}
        return self._browser_bridge.call(op, **params)

    def _browser_list_tabs(self) -> Dict[str, Any]:
        return self._browser_call("list_tabs")

    def _browser_navigate(self, url: str, new_tab: bool = False) -> Dict[str, Any]:
        self._progress(f"Browser: navigate to {url}")
        return self._browser_call("navigate", url=url, new_tab=bool(new_tab))

    def _browser_close_tab(self, index: int = -1) -> Dict[str, Any]:
        return self._browser_call("close_tab", index=int(index))

    def _browser_switch_tab(self, index: int) -> Dict[str, Any]:
        return self._browser_call("switch_tab", index=int(index))

    def _browser_go(self, action: str) -> Dict[str, Any]:
        return self._browser_call("go", action=action)

    def _browser_get_content(self, max_chars: int = 8000, include_links: bool = True) -> Dict[str, Any]:
        return self._browser_call("get_content", max_chars=int(max_chars),
                                  include_links=bool(include_links))

    def _browser_snapshot(self, limit: int = 120) -> Dict[str, Any]:
        return self._browser_call("snapshot", limit=int(limit))

    def _browser_wait(
        self,
        selector: str = "",
        url_contains: str = "",
        text: str = "",
        timeout_seconds: int = 10,
    ) -> Dict[str, Any]:
        return self._browser_call(
            "wait", selector=selector, url_contains=url_contains, text=text,
            timeout_seconds=int(timeout_seconds),
        )

    def _browser_click_ref(self, ref: str) -> Dict[str, Any]:
        return self._browser_call("click_ref", ref=ref)

    def _browser_type_ref(self, ref: str, value: str) -> Dict[str, Any]:
        return self._browser_call("type_ref", ref=ref, value=value)

    def _browser_select_ref(self, ref: str, value: str) -> Dict[str, Any]:
        return self._browser_call("select_ref", ref=ref, value=value)

    def _browser_check_ref(self, ref: str, checked: bool = True) -> Dict[str, Any]:
        return self._browser_call("check_ref", ref=ref, checked=bool(checked))

    def _browser_click(self, selector: str = "", text: str = "") -> Dict[str, Any]:
        self._progress(f"Browser: click {selector or text}")
        return self._browser_call("click", selector=selector, text=text)

    def _browser_fill(self, selector: str, value: str, submit: bool = False) -> Dict[str, Any]:
        return self._browser_call("fill", selector=selector, value=value, submit=bool(submit))

    def _browser_scroll(self, direction: str = "down", pixels: int = 0) -> Dict[str, Any]:
        return self._browser_call("scroll", direction=direction, pixels=int(pixels or 0))

    def _browser_add_bookmark(self, url: str = "", title: str = "") -> Dict[str, Any]:
        return self._browser_call("add_bookmark", url=url, title=title)

    def _browser_remove_bookmark(self, url: str) -> Dict[str, Any]:
        return self._browser_call("remove_bookmark", url=url)

    def _browser_list_bookmarks(self) -> Dict[str, Any]:
        return self._browser_call("list_bookmarks")

    # ------------------------------------------------------------------
    # RSS feed handlers
    # ------------------------------------------------------------------

    def _list_rss_feeds(self) -> Dict[str, Any]:
        """List all configured RSS feeds."""
        feeds = self.config.rss.feeds
        return {
            "success": True,
            "count": len(feeds),
            "feeds": [
                {
                    "name": f.name or f.url,
                    "url": f.url,
                    "mode": f.mode,
                    "category": f.category,
                    "seen_items": len(f.seen_items),
                }
                for f in feeds
            ],
        }

    def _get_rss_feed_items(
        self,
        feed_url: str,
        include_seen: bool = False,
        offset: int = 0,
        limit: int = 40,
    ) -> Dict[str, Any]:
        """Fetch items from an RSS feed."""
        feed = self._find_feed(feed_url)
        if not feed:
            return {"success": False, "error": f"Feed not found: {feed_url}"}

        result = self._rss_monitor.check_feed(feed)
        if result.get("error"):
            return {"success": False, "feed_url": feed_url, "error": result["error"]}

        items = result.get("all_items", []) if include_seen else result.get("items", [])
        # Reading a feed is side-effect free; items are marked seen only after download.
        item_total = len(items)
        page = items[offset:offset + limit]

        return {
            "success": True,
            "feed_name": result["feed_name"],
            "feed_url": feed_url,
            "mode": feed.mode,
            "total_items": result["total_items"],
            "new_items": result["new_items"],
            "count": len(page),
            "item_total": item_total,
            "offset": offset,
            "has_more": offset + len(page) < item_total,
            "items": page,
            "note": "Items with magnet_uri or torrent_url can be downloaded. Pass their item_id values to download_from_feed.",
        }

    def _persist_rss_config(self) -> None:
        """Save config and rebuild the monitor so feed changes take effect."""
        self.config.to_file(DeeptorrentConfig.default_config_path())
        self._rss_monitor = RSSMonitor(self.config.rss)

    def _add_rss_feed(self, url: str, name: str = "", mode: str = "monitor",
                      category: str = "Other") -> Dict[str, Any]:
        from config import RSSFeed

        url = url.strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ToolError("Invalid feed URL — must start with http:// or https://")
        if mode not in ("monitor", "auto_download"):
            raise ToolError("mode must be 'monitor' or 'auto_download'")
        if any(f.url == url for f in self.config.rss.feeds):
            return {"success": True, "url": url, "note": "Feed already subscribed."}
        feed = RSSFeed(url=url, name=name.strip() or url, mode=mode, category=category)
        self.config.rss.feeds.append(feed)
        self._persist_rss_config()
        return {"success": True, "url": url, "name": feed.name, "mode": mode,
                "note": "Feed saved. auto_download feeds fetch new items on the monitor's schedule."}

    def _remove_rss_feed(self, url: str) -> Dict[str, Any]:
        before = len(self.config.rss.feeds)
        self.config.rss.feeds = [f for f in self.config.rss.feeds if f.url != url]
        if len(self.config.rss.feeds) == before:
            return {"success": False, "error": f"No subscribed feed with URL: {url}"}
        self._persist_rss_config()
        return {"success": True, "removed": url}

    def _update_rss_feed(
        self,
        url: str,
        name: Optional[str] = None,
        mode: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        feed = self._find_feed(url)
        if feed is None:
            return {"success": False, "error": f"Feed not found: {url}"}
        if name is None and mode is None and category is None:
            return {"success": False, "error": "Provide at least one of name, mode, or category"}
        if name is not None:
            feed.name = name.strip() or url
        if mode is not None:
            feed.mode = mode
        if category is not None:
            feed.category = category.strip() or "Other"
        self._persist_rss_config()
        return {
            "success": True,
            "url": feed.url,
            "name": feed.name,
            "mode": feed.mode,
            "category": feed.category,
        }

    def _download_from_feed(
        self,
        feed_url: str,
        item_ids: Optional[List[str]] = None,
        item_indices: Optional[List[int]] = None,
        category: str = "Other",
    ) -> Dict[str, Any]:
        """Download specific items from an RSS feed by stable id or legacy index."""
        feed = self._find_feed(feed_url)
        if not feed:
            return {"success": False, "error": f"Feed not found: {feed_url}"}

        result = self._rss_monitor.check_feed(feed)
        all_items = result.get("all_items", [])
        new_items = result.get("items", [])
        if not all_items:
            return {"success": False, "error": "No items in feed"}
        if not item_ids and not item_indices:
            return {"success": False, "error": "Provide item_ids from get_rss_feed_items or legacy item_indices"}

        selected = []
        failed = []
        if item_ids:
            by_id = {str(item.get("item_id") or item.get("guid") or ""): item for item in all_items}
            for item_id in dict.fromkeys(str(value) for value in item_ids):
                item = by_id.get(item_id)
                if item is None:
                    failed.append({"item_id": item_id, "error": "Item id not found in current feed"})
                else:
                    selected.append((item_id, None, item))
        else:
            for idx in dict.fromkeys(item_indices or []):
                if idx < 0 or idx >= len(new_items):
                    failed.append({"index": idx, "error": "Index out of range for current new items"})
                else:
                    item = new_items[idx]
                    selected.append((str(item.get("item_id") or item.get("guid") or ""), idx, item))

        downloaded = []
        seen_items = []
        for item_id, idx, item in selected:
            magnet = item.get("magnet_uri", "")
            torrent_url = item.get("torrent_url", "")
            title = item.get("title", item_id or f"item_{idx}")
            output = {"item_id": item_id, "title": title}
            if idx is not None:
                output["index"] = idx

            if magnet:
                try:
                    download_result = self._add_magnet(magnet, self.config.default_save_path, category)
                    downloaded.append({**output, "info_hash": download_result.get("info_hash", "")})
                    seen_items.append(item)
                except Exception as exc:
                    failed.append({**output, "error": str(exc)})
            elif torrent_url:
                try:
                    download_result = self._add_torrent_from_url(torrent_url, self.config.default_save_path, category)
                    downloaded.append({**output, "info_hash": download_result.get("info_hash", "")})
                    seen_items.append(item)
                except Exception as exc:
                    failed.append({**output, "error": str(exc)})
            else:
                failed.append({**output, "error": "No magnet or torrent URL in item"})

        # Mark only successfully downloaded items as seen.
        if seen_items:
            self._rss_monitor.mark_seen(feed, seen_items)
            self._persist_rss_config()

        return {
            "success": len(downloaded) > 0,
            "downloaded": downloaded,
            "failed": failed,
            "total_downloaded": len(downloaded),
            "total_failed": len(failed),
        }

    def _find_feed(self, url: str) -> Optional[Any]:
        """Find a feed by URL."""
        for f in self.config.rss.feeds:
            if f.url == url:
                return f
        return None

    def _validate_torrent_download_url(self, url: str) -> str:
        from urllib.parse import urlsplit

        candidate = urlsplit((url or "").strip())
        configured = urlsplit(self.config.indexer.url or "")
        candidate_origin = (candidate.scheme.lower(), candidate.hostname, candidate.port)
        configured_origin = (configured.scheme.lower(), configured.hostname, configured.port)
        if self.config.indexer.api_key and candidate_origin == configured_origin and candidate.hostname:
            return candidate.geturl()
        return validate_public_http_url(url)

    def _add_torrent_from_url(self, url: str, save_path: str, category: str = "Other") -> Dict[str, Any]:
        """Download a .torrent file from URL and add it to the engine."""
        import tempfile
        from urllib.parse import urljoin

        try:
            current = url
            for _ in range(6):
                current = self._validate_torrent_download_url(current)
                resp = requests.get(
                    current, timeout=30, headers={"User-Agent": BROWSER_UA}, allow_redirects=False)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                location = resp.headers.get("Location", "")
                resp.close()
                # Indexer download links (e.g. Jackett /dl/) sometimes redirect
                # straight to a magnet: URI, which requests cannot follow.
                if location.startswith("magnet:"):
                    return self._add_magnet(location, save_path, category)
                if not location:
                    raise ToolError("Torrent redirect did not include a destination")
                current = urljoin(current, location)
            else:
                raise ToolError("Too many torrent download redirects")
            resp.raise_for_status()
            body = resp.content
            # Some indexers return the magnet URI as the response body.
            if body.lstrip().startswith(b"magnet:"):
                return self._add_magnet(body.decode("utf-8", "replace").strip(), save_path, category)
            # A real .torrent is a bencoded dict; anything else (HTML error
            # page, JSON, etc.) means the download link didn't resolve.
            if not body.lstrip().startswith(b"d"):
                raise ToolError(f"URL did not return a .torrent file (got {resp.headers.get('Content-Type', 'unknown content')})")
            # Persist the .torrent under ~/.deeptorrent/torrents/<hash>.torrent
            # (instead of a deleted temp file) so the torrent survives an app
            # restart — the restore flow re-adds from this file.
            try:
                import libtorrent as lt
                ti = lt.torrent_info(body)
                try:
                    ih = str(ti.info_hashes().v1)
                except AttributeError:
                    ih = str(ti.info_hash())
                torrents_dir = os.path.join(os.path.expanduser("~"), ".deeptorrent", "torrents")
                os.makedirs(torrents_dir, exist_ok=True)
                stored_path = os.path.join(torrents_dir, f"{ih}.torrent")
                with open(stored_path, "wb") as f:
                    f.write(body)
                return self._add_torrent_file(stored_path, save_path, category)
            except ToolError:
                raise
            except Exception:
                # Fall back to a temp file if parsing/persisting failed.
                with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as tmp:
                    tmp.write(body)
                    tmp_path = tmp.name
                return self._add_torrent_file(tmp_path, save_path, category)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Failed to download torrent from {url}: {exc}")


# ----------------------------------------------------------------------
# Discovery clients
# ----------------------------------------------------------------------

class WebSearchClient:
    """Web search with configurable provider and tracker-list fetch helpers."""

    TRACKER_LISTS = [
        "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
        "https://raw.githubusercontent.com/XIU2/TrackersListCollection/master/best.txt",
        "https://raw.githubusercontent.com/XIU2/TrackersListCollection/master/all.txt",
    ]

    BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, config: "WebSearchConfig") -> None:
        self.config = config
        self._last_request = 0.0
        # Brave's free tier allows 1 query/second — space requests out when a
        # Brave key is configured. Otherwise small spacing only, so parallel
        # source searches don't serialize on a global delay.
        # The lock makes the spacing thread-safe.
        self._rate_limit = 1.0 if getattr(config, "brave_api_key", "") else 0.2
        self._throttle_lock = threading.Lock()

    def search(self, query: str, limit: int = 10, llm: bool = True) -> Dict[str, Any]:
        """Web search fan-out: query every available provider in parallel and merge.

        DuckDuckGo (keyless) always runs; Brave and Perplexity join when their
        API keys are configured. Results are deduped by URL (first occurrence
        wins, provider order preserved) and capped at limit*2. When Perplexity
        contributes, its synthesized answer is surfaced as the top-level
        `answer` summary. `providers` lists which backends returned results.
        (A scraped-Google stage was tried and dropped: Google serves JS-only
        shell pages to anonymous requests, so it never produced results.)

        `llm=False` skips Perplexity — used by bulk internal sweeps (per-source
        site queries, query-variant loops) so they don't burn paid API quota.
        """
        attempts: List[Tuple[str, Callable[[str, int], List[Dict[str, Any]]]]] = [
            ("duckduckgo", self._ddg_search),
        ]
        if getattr(self.config, "brave_api_key", ""):
            attempts.append(("brave", self._brave_search))
        if llm and getattr(self.config, "api_key", ""):
            attempts.append(("perplexity", self._perplexity_search))

        self._throttle()
        per_provider: Dict[str, List[Dict[str, Any]]] = {}
        if len(attempts) == 1:
            name, fn = attempts[0]
            per_provider[name] = fn(query, limit)
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(attempts)) as pool:
                for name, provider_results in zip(
                    (name for name, _ in attempts),
                    pool.map(lambda a: a[1](query, limit), attempts),
                ):
                    per_provider[name] = provider_results

        # Merge in provider order, deduped by URL. Items without a URL (the
        # synthetic "Perplexity answer" entry) are always kept.
        results: List[Dict[str, Any]] = []
        seen_urls = set()
        answer = ""
        for name, _ in attempts:
            provider_results = per_provider.get(name) or []
            if provider_results:
                logger.info("%s search returned %d result(s)", name, len(provider_results))
            for r in provider_results:
                if not answer and r.get("answer"):
                    answer = r["answer"]
                u = r.get("url", "")
                if u:
                    if u in seen_urls:
                        continue
                    seen_urls.add(u)
                results.append(r)
        results = results[: limit * 2]

        # Sonar answers sometimes contain direct magnet links — surface them.
        magnets: List[str] = []
        if answer:
            magnets = re.findall(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+[^\"\s<>')\]]*", answer)
        out: Dict[str, Any] = {
            "provider": "+".join(name for name, _ in attempts),
            "providers": [name for name, _ in attempts if per_provider.get(name)],
            "query": query,
            "results": results,
        }
        if answer:
            out["answer"] = answer
        if magnets:
            out["magnets"] = list(dict.fromkeys(magnets))[:10]
        return out

    def _ddg_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        """DuckDuckGo web search — keyless, via the ddgs package."""
        if DDGS is None:
            return []
        try:
            raw = DDGS().text(query, max_results=limit)
        except Exception as exc:
            logger.warning("DuckDuckGo search failed: %s", exc)
            return []

        results: List[Dict[str, Any]] = []
        seen_urls = set()
        for item in raw or []:
            u = item.get("href") or ""
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            results.append({
                "title": item.get("title") or u,
                "url": u,
                "snippet": item.get("body", ""),
                "source": "duckduckgo",
            })
        return results

    def _brave_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        """Search via the Brave Search API — plain web results (no synthesized answer)."""
        try:
            resp = requests.get(
                self.BRAVE_ENDPOINT,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self.config.brave_api_key,
                },
                params={"q": query, "count": min(limit, 20)},
                timeout=30,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Brave search failed: %s", exc)
            return []

        results: List[Dict[str, Any]] = []
        seen_urls = set()
        for item in ((resp.json().get("web") or {}).get("results") or []):
            u = item.get("url") or ""
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            results.append({
                "title": item.get("title") or u,
                "url": u,
                "snippet": item.get("description", ""),
                "source": "brave",
            })
            if len(results) >= limit:
                break
        return results

    def fetch_tracker_lists(self) -> Dict[str, Any]:
        def _fetch_one(url: str) -> Tuple[List[str], Optional[str]]:
            try:
                self._throttle()
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                lines = [l.strip() for l in resp.text.splitlines()]
                return [l for l in lines if l and not l.startswith("#")], None
            except Exception as exc:
                logger.warning("Failed to fetch tracker list %s: %s", url, exc)
                return [], f"{url}: {exc}"

        trackers: List[str] = []
        errors: List[str] = []
        # Fetch all lists concurrently — they are independent.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(self.TRACKER_LISTS)) as pool:
            for lines, error in pool.map(_fetch_one, self.TRACKER_LISTS):
                if error:
                    errors.append(error)
                for line in lines:
                    if line not in trackers:
                        trackers.append(line)
        return {
            "success": len(trackers) > 0,
            "count": len(trackers),
            "trackers": trackers,
            "errors": errors,
        }

    def _throttle(self) -> None:
        with self._throttle_lock:
            elapsed = time.time() - self._last_request
            if elapsed < self._rate_limit:
                time.sleep(self._rate_limit - elapsed)
            self._last_request = time.time()

    def _perplexity_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        """Search via Perplexity's sonar API. Returns answer + cited sources."""
        url = (self.config.base_url or "https://api.perplexity.ai") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "sonar",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a web search assistant for a torrent download manager. "
                        "Be concise and cite sources. Return the most relevant, current "
                        "results with direct URLs (prefer magnet links, .torrent files, "
                        "or official download pages when the user is looking for torrents)."
                    ),
                },
                {"role": "user", "content": query},
            ],
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Perplexity search failed: %s", exc)
            return []

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {}) or {}
        answer = message.get("content", "").strip()
        citations = data.get("citations", []) or []
        search_results = data.get("search_results", []) or []

        results: List[Dict[str, Any]] = []
        seen_urls = set()
        # Prefer structured search_results when available.
        for sr in search_results:
            u = sr.get("url") or sr.get("link") or ""
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            results.append({
                "title": sr.get("title") or u,
                "url": u,
                "source": "perplexity",
            })
            if len(results) >= limit:
                break
        # Otherwise build results from the citations list.
        if not results:
            for u in citations:
                if not u or u in seen_urls:
                    continue
                seen_urls.add(u)
                results.append({"title": u, "url": u, "source": "perplexity"})
                if len(results) >= limit:
                    break

        # Always include the synthesized answer as the first item's snippet,
        # or as a synthetic top result so the agent can use the LLM summary.
        if answer and results:
            results[0]["answer"] = answer
        elif answer and not results:
            results.append({"title": "Perplexity answer", "url": "", "source": "perplexity", "answer": answer})
        return results


class TorznabClient:
    """Torznab/Jackett search client."""

    def __init__(self, config: "IndexerConfig") -> None:
        self.config = config
        self._last_request = 0.0
        # Guards _last_request — per-indexer searches run on a thread pool.
        self._throttle_lock = threading.Lock()
        # Jackett is a local service — heavy throttling only serializes
        # parallel per-indexer queries. A small gap is enough.
        self._rate_limit = 0.15

    def search(self, query: str, category: str = "") -> Dict[str, Any]:
        if not self.config.api_key:
            return {"success": False, "results": [], "error": "No indexer API key configured"}

        self._throttle()
        params: Dict[str, Any] = {
            "apikey": self.config.api_key,
            "t": "search",
            "q": query,
            "cat": category,
        }
        try:
            url = f"{self.config.url.rstrip('/')}{self.config.torznab_path}"
            resp = requests.get(url, params=params, timeout=self.config.timeout)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Indexer search failed: %s", exc)
            return {"success": False, "results": [], "error": str(exc)}

        return self._parse_torznab(resp.text)

    def search_indexer(self, indexer_id: str, query: str, category: str = "") -> Dict[str, Any]:
        """Search a single Jackett indexer by id (substituted into the torznab path)."""
        if not self.config.api_key:
            return {"success": False, "results": [], "error": "No indexer API key configured"}

        path = self.config.torznab_path.replace("/all/", f"/{indexer_id}/")
        if path == self.config.torznab_path:
            path = f"/api/v2.0/indexers/{indexer_id}/results/torznab"

        self._throttle()
        params: Dict[str, Any] = {
            "apikey": self.config.api_key,
            "t": "search",
            "q": query,
            "cat": category,
        }
        try:
            url = f"{self.config.url.rstrip('/')}{path}"
            resp = requests.get(url, params=params, timeout=self.config.timeout)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Indexer %s search failed: %s", indexer_id, exc)
            return {"success": False, "results": [], "error": str(exc)}

        return self._parse_torznab(resp.text)

    def _throttle(self) -> None:
        # Locked: without this, parallel workers read the same timestamp and
        # stampede Jackett instead of spacing requests out.
        with self._throttle_lock:
            elapsed = time.time() - self._last_request
            if elapsed < self._rate_limit:
                time.sleep(self._rate_limit - elapsed)
            self._last_request = time.time()

    def _parse_torznab(self, xml_text: str) -> Dict[str, Any]:
        try:
            import xml.etree.ElementTree as ET

            root = ET.fromstring(xml_text)
            # Jackett/torznab report failures as HTTP 200 with an <error> body
            # (e.g. code 100 "Invalid API Key") — surface them instead of
            # silently reporting zero results.
            err = root if root.tag == "error" else root.find(".//error")
            if err is not None:
                desc = err.get("description") or (err.text or "").strip() or "unknown torznab error"
                logger.warning("Torznab error response: %s", desc)
                return {"success": False, "results": [], "error": f"Indexer error: {desc}"}
            channel = root.find("channel")
            items = channel.findall("item") if channel is not None else root.findall(".//item")
            results = []
            for item in items:
                attrs = {a.get("name"): a.get("value") for a in item.findall("torznab:attr") or item.findall("{http://torznab.com/schemas/2015/feed}attr")}
                size = item.findtext("size") or attrs.get("size", "0")
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                indexer = attrs.get("indexer", "")
                seeders = int(attrs.get("seeders", 0) or 0)
                leechers = int(attrs.get("leechers", 0) or 0)
                grabs = int(attrs.get("grabs", 0) or 0)
                category_id = attrs.get("category", "") or item.findtext("category") or ""
                results.append(
                    {
                        "name": title,
                        "size": int(size),
                        "seeders": seeders,
                        "leechers": leechers,
                        "grabs": grabs,
                        "indexer": indexer,
                        "category": category_id,
                        "magnet": link if link.startswith("magnet:") else "",
                        "download_url": link if not link.startswith("magnet:") else "",
                    }
                )
            return {"success": True, "count": len(results), "results": results}
        except Exception as exc:
            logger.warning("Torznab parse failed: %s", exc)
            return {"success": False, "results": [], "error": f"Parse error: {exc}"}
