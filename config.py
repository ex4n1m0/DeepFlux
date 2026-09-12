"""Configuration loading and defaults for Deeptorrent."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

logger = logging.getLogger(__name__)

# The app's release version. Kept here (config is imported everywhere) so the
# telemetry ping and future callers share one source; the user-facing literals
# (window title, User Guide, installer.iss) are bumped by hand on release —
# see the version-bump checklist in AGENTS.md.
APP_VERSION = "3.9"


# Since 3.5.2 a SET of shared keys ships in the setup file so the app works
# out of the box for every download: the DeepSeek agent key plus the metadata/
# search provider keys (Perplexity, TMDb, OpenSubtitles, TPDB, StashDB, OMDb,
# Fanart.tv). The VALUES never live in git — they are loaded at import time
# from the untracked repo-root module ``_embedded_keys.py`` (present only on
# build machines; PyInstaller bundles it into the frozen app like any import);
# without it every shared key is simply empty and the app runs keyless.
# Precedence per slot: a saved user key, then the slot's own env var, then the
# shared key (DeepSeek additionally gated on the deepseek provider). Jackett
# stays env-only by decision. All other key fields still default to empty and
# the user enters their own (GUI settings dialogs or config.json).
#
# Since 3.5.9 the values in _embedded_keys.py are OBFUSCATED, never plaintext:
# each is a (salt_b64, blob_b64) pair where blob = key XOR sha256(salt+counter)
# keystream. That is obfuscation, not cryptography — the goal is that no key
# exists as a plaintext constant anywhere in the repo, the frozen PYZ or the
# installer, so a pyinstxtractor + constants dump comes up empty. Regenerate
# the file with ``python packaging/gen_embedded_keys.py``. Additionally,
# from_file remembers which config slots it filled with a shared key and
# to_file()/sanitized_dict() blank those slots again — the shared keys must
# never be persisted to config.json or a settings backup; they are re-injected
# from the bundle on every load instead.
def _decode_shared_value(raw: object) -> str:
    if isinstance(raw, str):  # legacy plaintext form (pre-3.5.9 file)
        return raw
    if not isinstance(raw, tuple) or len(raw) != 2:
        return ""
    try:
        salt = base64.b64decode(raw[0], validate=True)
        blob = base64.b64decode(raw[1], validate=True)
    except Exception:
        return ""
    pad = bytearray()
    counter = 0
    while len(pad) < len(blob):
        pad.extend(hashlib.sha256(salt + counter.to_bytes(4, "big")).digest())
        counter += 1
    value = bytes(b ^ p for b, p in zip(blob, bytes(pad))).decode("utf-8", "replace")
    # Fail closed: a wrong/tampered pair decodes to non-printable garbage.
    return value if value and all(32 <= ord(c) < 127 for c in value) else ""


def _load_shared_key(attr: str) -> str:
    try:
        import _embedded_keys  # local-only, gitignored
        return _decode_shared_value(getattr(_embedded_keys, attr, ""))
    except Exception:
        return ""


# Config slots (dotted path) that from_file filled with a SHARED key value in
# this process. to_file()/sanitized_dict() blank them again unless the user
# has since typed a different key into the slot (value mismatch = user-owned).
_SHARED_KEY_SLOTS: Dict[str, str] = {}


def _remember_shared_slot(path: str, value: str, shared: str) -> None:
    if shared and value == shared:
        _SHARED_KEY_SLOTS[path] = value


_SHARED_DEEPSEEK_API_KEY = _load_shared_key("SHARED_DEEPSEEK_API_KEY")
_SHARED_PERPLEXITY_API_KEY = _load_shared_key("SHARED_PERPLEXITY_API_KEY")
_SHARED_TMDB_API_KEY = _load_shared_key("SHARED_TMDB_API_KEY")
_SHARED_OPENSUBTITLES_API_KEY = _load_shared_key("SHARED_OPENSUBTITLES_API_KEY")
_SHARED_TPDB_API_KEY = _load_shared_key("SHARED_TPDB_API_KEY")
_SHARED_STASHDB_API_KEY = _load_shared_key("SHARED_STASHDB_API_KEY")
_SHARED_OMDB_API_KEY = _load_shared_key("SHARED_OMDB_API_KEY")
_SHARED_FANARTTV_API_KEY = _load_shared_key("SHARED_FANARTTV_API_KEY")
# Community-room crypto secret. Deliberately NOT a config field: it is read
# directly by ircmgr.room via shared_room_secret(), so there is no slot that
# to_file()/sanitized_dict() could ever persist or scrub. Empty (source
# builds) = the room runs unencrypted with its own public discovery slot.
_SHARED_ROOM_KEY = _load_shared_key("SHARED_ROOM_KEY")


def shared_room_secret() -> str:
    """In-box secret for the DeepFlux Room chat (setup builds only).

    Same hygiene as the other shared keys: obfuscated inside the untracked
    _embedded_keys.py, shipped only in the frozen bundle, absent from git."""
    return _SHARED_ROOM_KEY


# DeepFlux is DeepSeek-native and also supports OpenAI-compatible endpoints.
# Provider capability metadata controls payload differences instead of assuming
# every endpoint accepts DeepSeek-specific fields.
LLM_PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek API (direct)",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-flash"],
        "reasoning_effort": True,
        "effort_map": {},
        "streaming": True,
        "tools": True,
        "key_hint": "Get one at https://platform.deepseek.com/api_keys\n"
                    "Required for the agent — left blank, the app runs in offline demo mode.",
    },
    "openrouter": {
        "label": "DeepSeek via OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["deepseek/deepseek-v4.1-flash"],
        "reasoning_effort": True,
        "effort_map": {"max": "xhigh"},
        "streaming": True,
        "tools": True,
        "key_hint": "Get one at https://openrouter.ai/keys\n"
                    "Uses OpenRouter's vendor/model slugs.",
    },
    "custom": {
        "label": "Custom OpenAI-compatible",
        "base_url": "",
        "models": [],
        "reasoning_effort": False,
        "effort_map": {},
        "streaming": True,
        "tools": True,
        "key_hint": "Enter the endpoint base URL, model name, and API key required by your provider.",
    },
}


@dataclass
class LLMConfig:
    provider: str = "deepseek"  # deepseek (direct API) or openrouter
    api_key: str = ""
    base_url: str = ""
    # Model lineup per DeepSeek's pricing page (2026-09): deepseek-flash is
    # V4.1-Flash — DeepSeek's own current default, surpassing the retiring
    # deepseek-v4-pro in performance, cost and speed (v4-pro requests are
    # auto-routed to Flash from 2026-09-14). Both slots default to flash.
    model: str = "deepseek-flash"        # main agent model — locked to DeepSeek
    reasoning_effort: str = "high"       # planning turns; summaries always use "low"
    custom_reasoning_effort: bool = False
    fast_model: str = "deepseek-flash"   # summaries/quick replies
    max_turns: int = 40             # ReAct loop cap per user message; the loop self-terminates when the model stops calling tools
    max_llm_calls: int = 50
    max_tool_calls: int = 100
    task_timeout_seconds: int = 600
    repeated_call_limit: int = 3
    context_budget_tokens: int = 64000
    response_reserve_tokens: int = 8000
    history_budget: int = 60        # max history messages sent to the LLM; oldest turns dropped first (0 = unlimited)
    stream: bool = True             # stream tokens to the UI as they arrive
    memory_enabled: bool = True     # persistent local memory (markdown files on disk)
    memory_dir: str = ""            # empty = default (~/.deeptorrent/memory)


@dataclass
class IndexerConfig:
    url: str = "http://localhost:9117"  # Jackett base URL
    api_key: str = ""
    torznab_path: str = "/api/v2.0/indexers/all/results/torznab"
    timeout: int = 60  # aggregate 'all indexers' queries can be slow
    # Start Jackett automatically when it's configured but not running
    # (Windows service first, then the tray/console executable).
    auto_start: bool = True
    jackett_path: str = ""  # explicit Jackett exe path; empty = auto-detect


@dataclass
class WebSearchConfig:
    provider: str = "perplexity"  # perplexity (last-resort backup; skipped without a key)
    api_key: str = ""
    brave_api_key: str = ""       # Brave Search API key — used between DDG and Perplexity
    cx: str = ""
    base_url: str = ""


@dataclass
class TorrentsConfig:
    """libtorrent session settings."""
    download_rate_limit_kb: int = 0  # KB/s, 0 = unlimited
    upload_rate_limit_kb: int = 0
    listen_port: int = 0             # 0 = random port each start
    max_connections: int = 0         # 0 = libtorrent default
    restore_completed: bool = True   # keep completed torrents in the list across restarts
    # Queue / concurrency — 0 means unlimited / use libtorrent default.
    max_downloading_torrents: int = 5
    max_seeding_torrents: int = 5
    max_active_torrents: int = 10
    max_queued_torrents: int = 0
    auto_manage_interval_seconds: int = 30
    # Seeding limits — 0 means disabled (unlimited).
    seed_ratio_limit: float = 0.0
    seed_time_limit_minutes: int = 0
    # Crash-safe persistence cadence.
    auto_save_state_seconds: int = 60


@dataclass
class WatchdogConfig:
    enabled: bool = False
    stall_threshold_seconds: int = 300
    cooldown_seconds: int = 1800
    auto_heal: bool = False


@dataclass
class RSSFeed:
    """A single RSS feed subscription."""
    url: str = ""
    name: str = ""
    mode: str = "monitor"  # "monitor" = load only, "auto_download" = download all
    category: str = "Other"
    last_checked: str = ""
    seen_items: List[str] = field(default_factory=list)


@dataclass
class RSSConfig:
    feeds: List[RSSFeed] = field(default_factory=list)
    check_interval_seconds: int = 300  # 5 minutes


@dataclass
class Bookmark:
    title: str = ""
    url: str = ""
    folder: str = ""  # "/" -separated folder path (e.g. "News/Tech"); "" = top level


DEFAULT_BROWSER_HOMEPAGE = "https://deepflux.space/"
BROWSER_SEARCH_ENGINES = {
    "google": "https://www.google.com/search?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "bing": "https://www.bing.com/search?q={query}",
    "brave": "https://search.brave.com/search?q={query}",
}


@dataclass
class BrowserConfig:
    homepage: str = DEFAULT_BROWSER_HOMEPAGE  # empty migrates to the default on load
    search_engine: str = "google"
    bookmarks: List[Bookmark] = field(default_factory=list)
    adblock_enabled: bool = True
    adblock_disabled_sites: List[str] = field(default_factory=list)
    extension_enabled: bool = False  # Video grabber (browser extension JS injection) — off by default
    grabber_auto_queue: bool = False  # Site Grabber: queue search results without a Download click
    grabber_auto_limit: int = 5  # max videos the Site Grabber auto-queues per search/page
    grabber_last_site: str = ""  # Site Grabber: last site searched (prefill when no tab is open)
    grabber_search_templates: Dict[str, str] = field(default_factory=dict)  # host -> search URL pattern that worked
    agent_content_permissions: Dict[str, str] = field(default_factory=dict)
    restore_tabs: bool = True
    open_tabs: List[str] = field(default_factory=list)
    active_tab: int = 0
    history_enabled: bool = True
    history_retention_days: int = 90
    zoom_by_origin: Dict[str, float] = field(default_factory=dict)


@dataclass
class DownloadCategory:
    """Per-category download folder mapping."""
    name: str = ""
    folder: str = ""
    extensions: List[str] = field(default_factory=list)


@dataclass
class SourceConfig:
    """A single search source (torrent indexer or website)."""
    id: str = ""           # Jackett indexer id or unique slug
    name: str = ""         # Display name
    url: str = ""          # Site link / homepage
    type: str = "public"   # "public", "private"
    enabled: bool = True
    categories: List[str] = field(default_factory=list)  # e.g. ["Movies", "TV"]
    popularity: int = 0    # 0 = unknown (falls back to DEFAULT_SOURCE_POPULARITY); higher = searched earlier


# Rough popularity tiers for known sources (higher = more popular / better seeded).
# Used to search the most productive sources first and stop early.
DEFAULT_SOURCE_POPULARITY: Dict[str, int] = {
    "iptorrents": 100,
    "1337x": 95,
    "nyaasi": 95,
    "thepiratebay": 90,
    "subsplease": 90,
    "rutracker-ru": 90,
    "torrentgalaxy": 85,
    "noname-club": 85,
    "sukebeinyaasi": 85,
    "therarbg": 85,
    "torrentgalaxyclone": 85,
    "audiobookbay": 80,
    "internetarchive": 80,
    "rutor": 80,
    "torrentdownloads": 80,
    "byrutor": 75,
    "ehentai": 75,
    "ebookbay": 75,
    "torrentdownload": 75,
    "acgrip": 70,
    "dmhy": 70,
    "extto": 70,
    "mikan": 70,
    "showrss": 70,
    "torrent9": 70,
    "zamundarip": 70,
    "bangumi-moe": 65,
    "dontorrent": 65,
    "epublibre": 65,
    "limetorrents": 65,
    "mactorrentsdownload": 65,
    "shanaproject": 65,
    "torrentscsv": 65,
    "catorrent": 60,
    "linuxtracker": 60,
    "torrentkitty": 60,
    "megapeer": 55,
}


def source_popularity(src: SourceConfig) -> int:
    """Effective popularity: explicit per-source value wins, then the built-in map."""
    return src.popularity or DEFAULT_SOURCE_POPULARITY.get(src.id, 0)


# No built-in sources: fresh installs start with an empty list (the installer
# also replaces config.json on every install, so upgrades start empty too).
# Users add their own sources or fetch configured indexers from Jackett;
# DEFAULT_SOURCE_POPULARITY still ranks any user-added source whose id
# matches a known one. Anything added here is merged into existing user
# configs by from_file (match by id, Jackett-style).
DEFAULT_SOURCES: List[SourceConfig] = []


@dataclass
class SourcesConfig:
    """Configuration for agent search sources."""
    sources: List[SourceConfig] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    # When True, the agent uses Jackett to search all enabled sources.
    # When False, the agent uses web search (Perplexity) as a fallback.
    use_jackett: bool = True
    # Search strategy tunables (source web-search path).
    search_batch_size: int = 5   # sources queried concurrently per round, most popular first
    min_results: int = 5         # stop searching more sources once this many results are found
    min_seeders: int = 10        # Jackett path: private results with this many seeds make public results unnecessary
    last_jackett_fetch: float = 0.0  # epoch of last successful source sync (infra.jackett); 0 = never


@dataclass
class DownloadConfig:
    """Configuration for the IDM-style download manager."""
    max_concurrent: int = 3
    max_connections_per_download: int = 8       # 1–32
    default_folder: str = str(Path.home() / "Downloads" / "DeepFlux")
    bandwidth_limit_bps: int = 0                # 0 = unlimited
    auto_start: bool = True
    segment_threshold_mb: int = 1               # files smaller than this use single-stream
    control_api_port: int = 53742
    ffmpeg_path: str = ""                       # empty = use bundled
    stream_max_height: int = 0
    youtube_max_height: int = 1080
    youtube_subtitles: bool = False
    youtube_playlists: bool = False
    youtube_update_check: bool = True           # warn when yt-dlp is outdated (dlmgr/ytdlp_update.py)
    ytdlp_last_check: float = 0.0               # epoch of last freshness probe; 0 = never
    categories: List[DownloadCategory] = field(default_factory=list)


@dataclass
class IPTVSourceConfig:
    """A saved IPTV source (M3U URL, local M3U file, Xtream Codes login, or
    local media folder — url is the directory path for the latter).

    Credentials (Xtream password) are persisted in the user's config file like
    the rest of DeepFlux's settings, but are never logged by the IPTV
    subsystem.
    """
    id: str = ""
    name: str = ""
    kind: str = "m3u_url"          # "m3u_url" | "m3u_file" | "xtream" | "local_folder"
    url: str = ""
    user_agent: str = ""
    referer: str = ""
    username: str = ""
    password: str = ""
    enabled: bool = True
    auto_refresh_minutes: int = 0  # 0 = manual only
    epg_url: str = ""              # optional XMLTV EPG URL (when the playlist doesn't declare one)


@dataclass
class IPTVConfig:
    """Configuration for the IPTV tab."""
    sources: List[IPTVSourceConfig] = field(default_factory=list)
    tmdb_api_key: str = ""  # user enters their own in IPTV Settings; empty = TVmaze/Wikipedia fallback
    # OMDb — free key from omdbapi.com/apikey.aspx. Fallback when TMDb has
    # no key or misses (older/obscure/non-English titles).
    omdb_api_key: str = ""
    # Fanart.tv — free personal key from fanart.tv/get-an-api-key. Backdrop
    # supplement: enriches backdrops after TMDb/TVMaze resolve a poster.
    fanarttv_api_key: str = ""
    # Provider enable/disable flags. Default: all enabled. When a provider's
    # key is empty, it's automatically skipped regardless of this flag. These
    # flags let the user disable a provider even when a key is present (e.g.
    # disable StashDB if its responses are too slow).
    enable_javbus: bool = True
    enable_javlibrary: bool = True
    enable_fanza: bool = True
    enable_wikipedia: bool = True
    # ThePornDB — metadata/posters for adult VOD, which TMDb filters out of
    # its search results entirely. Empty = adult entries just fall through to
    # TMDb (and usually stay artwork-less).
    tpdb_api_key: str = ""
    # StashDB — community-driven adult metadata DB (GraphQL API). Second
    # source for western adult VOD alongside TPDB; needs a free key from
    # stashdb.org. Empty = adult chain uses TPDB only.
    stashdb_api_key: str = ""
    # Last-resort poster: grab a frame from the stream with FFmpeg when no
    # metadata provider matched. Only ever runs for tiles on screen.
    framegrab_posters: bool = True
    # OpenSubtitles.com subtitle downloads in the player (CC menu). Key is
    # required; account credentials are optional and raise the daily quota.
    opensubtitles_api_key: str = ""
    opensubtitles_username: str = ""
    opensubtitles_password: str = ""
    # Auto-selected on playback when a track with this language exists
    # (ISO 639-1 "en" or 639-2 "eng" both work). Empty = player default.
    preferred_audio_lang: str = ""
    preferred_sub_lang: str = ""
    # Global XMLTV EPG URL, applied to every source that doesn't have its own
    # per-source epg_url. Precedence: per-source > this > playlist url-tvg.
    # Times are stored as epoch (offsets honored), so the guide is correct in
    # whatever timezone the system is in.
    epg_url: str = ""
    cache_dir: str = ""             # empty = default (~/.deeptorrent/iptv)
    # Artwork disk cache cap, enforced by LRU eviction (ArtworkCache.
    # enforce_size_limit). 10 GB default: posters are small and re-downloading
    # evicted art is cheap, but a big playlist cache is worth keeping.
    cache_limit_mb: int = 10240
    # Xtream get_series_info is one request per show.  A low bounded default
    # avoids provider rate limits while still filling episode lists promptly.
    xtream_series_concurrency: int = 2
    cache_seconds: int = 15         # network stream startup buffer (seconds)
    # Volatile mpv live pause/rewind cache. This is bounded RAM/disk cache, not
    # durable timeshift; retained time varies with stream bitrate. 0 disables.
    live_pause_buffer_seconds: int = 300
    # Empty uses ~/Videos/DeepFlux Recordings.
    recording_dir: str = ""
    hwdec: str = "auto-safe"        # mpv hardware decoding mode
    interpolation: bool = True      # mpv smoothmotion frame blending (GPU cost)
    # SVP 4 (SmoothVideo Project) true motion interpolation. Nothing is
    # bundled — this only cooperates with the user's own SVP install (see
    # iptv/svp.py). Files/VOD only; forces copy-back hwdec while active.
    svp_enabled: bool = False
    # MilkDrop (Butterchurn) visualization while audio-only media plays.
    # milkdrop_preset is a .milk filename from the shipped MilkDrop/ folder or
    # ~/.deeptorrent/presets; empty = the first one found.
    milkdrop_enabled: bool = True
    milkdrop_preset: str = ""
    preferred_player: str = "mpv"   # "mpv" | "vlc"
    enable_epg: bool = True
    auto_try_next_source: bool = False
    # While a stream is playing, cap torrent rates so the video doesn't starve.
    throttle_torrents: bool = True
    throttle_download_kb: int = 4096   # KB/s while playing (only lowers, never raises)
    throttle_upload_kb: int = 512      # upload saturation is what usually kills streams
    # Player audio state — persisted across restarts.
    volume: int = 100
    muted: bool = False
    # Video overscan (% linear scale-up) — pushes dirty broadcast edge
    # rows/columns off-screen so they don't show as a faint bright line.
    overscan_pct: float = 0.5
    # Audio sync offset in seconds (audio-delay). Positive delays the audio
    # to compensate for video-path latency such as SVP 4 motion interpolation,
    # whose frame synthesis renders behind the audio clock. Adjusted live from
    # the player's audio menu / +/- keys and persisted for the next playback.
    audio_delay: float = 0.0
    # Movies/Series sidebar grouping: "year" (release year under each
    # category, yearless entries under "Others") or "category" (flat
    # provider groups).
    vod_group_mode: str = "year"


@dataclass
class IRCNetworkConfig:
    """A single IRC network/server entry for the IRC tab.

    Credentials (server password / SASL) are persisted in the user's config
    file like the rest of DeepFlux's settings, but are never logged.
    """
    id: str = ""                     # short slug, e.g. "libera"
    host: str = ""
    port: int = 6697
    tls: bool = True
    nick: str = "DeepFluxUser"
    username: str = ""               # empty = nick
    realname: str = "DeepFlux"
    password: str = ""               # server PASS (rarely needed)
    sasl_account: str = ""           # optional SASL PLAIN auth
    sasl_password: str = ""
    channels: List[str] = field(default_factory=list)


@dataclass
class IRCConfig:
    """Configuration for the IRC tab + agent IRC monitoring."""
    networks: List[IRCNetworkConfig] = field(default_factory=list)
    buffer_lines: int = 500          # per-channel ring buffer (agent reads this)
    flood_delay: float = 2.0         # min seconds between outgoing messages
    reconnect_max_seconds: int = 300
    reconnect_max_attempts: int = 5
    # Persistent transcripts are explicitly opt-in. Message bodies, targets,
    # nicknames and IRCv3 metadata are encrypted with a local key at rest.
    history_enabled: bool = False
    history_private_messages: bool = False
    history_retention_days: int = 30


# Built-in default IRC networks — merged into user configs by `from_file`
# (match by `id`, Jackett-style). A curated set of popular public networks plus
# private-tracker support networks so the user can connect with one click from
# the IRC tab. All start disconnected (the user connects manually) and NONE
# ships pre-joined channels — every network auto-requests /LIST on connect
# (retried until it arrives) so the channel browser offers a directory to join.
# Hosts/ports verified against the live servers 2026-09-01 with a registration
# probe (CAP LS + NICK/USER + CAP END → 001): networks whose shipped TLS port
# was dead or legacy-cipher-only (Undernet, GeekShed, P2P-Network, BrokenSphere,
# IPTorrents round-robin) ship plain 6667, which registered on every one of
# them. irc.animebytes.tv was seized (NXDOMAIN) — the network lives at
# irc.animefriends.moe:7000 (TLS) now. MoreThanTV's own network is gone; MTV
# support lives on DigitalIRC (already a default), so no morethantv entry.
DEFAULT_IRC_NETWORKS: List[IRCNetworkConfig] = [
    IRCNetworkConfig(
        id="libera",
        host="irc.libera.chat",
        port=6697,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="oftc",
        host="irc.oftc.net",
        port=6697,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="rizon",
        host="irc.rizon.net",
        port=6697,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="dalnet",
        host="irc.dal.net",
        port=6697,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="undernet",
        host="irc.undernet.org",
        port=6667,
        tls=False,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="efnet",
        host="efnet.deic.eu",
        port=6667,
        tls=False,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="quakenet",
        host="irc.quakenet.org",
        port=6667,
        tls=False,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="ircnet",
        host="open.ircnet.net",
        port=6667,
        tls=False,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="geekshed",
        host="irc.geekshed.net",
        port=6667,
        tls=False,
        nick="DeepFluxUser",
    ),
    # --- Private-tracker support networks ---
    IRCNetworkConfig(
        id="animebytes",
        host="irc.animefriends.moe",
        port=7000,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="p2p-network",
        host="irc.p2p-network.net",
        port=6667,
        tls=False,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="digitalirc",
        host="irc.digitalirc.org",
        port=6697,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="gazellegames",
        host="irc.gazellegames.net",
        port=7000,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="synirc",
        host="irc.synirc.net",
        port=6697,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="brokensphere",
        host="irc.brokensphere.net",
        port=6667,
        tls=False,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="orpheus",
        host="irc.orpheus.network",
        port=7000,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="passthepopcorn",
        host="irc.passthepopcorn.me",
        port=7000,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="scratch-network",
        host="irc.scratch-network.net",
        port=7000,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="torrentleech",
        host="irc.torrentleech.org",
        port=7021,
        tls=True,
        nick="DeepFluxUser",
    ),
    IRCNetworkConfig(
        id="iptorrents",
        host="irc.iptorrents.com",
        port=6667,
        tls=False,
        nick="DeepFluxUser",
    ),
]


@dataclass
class VoiceConfig:
    """Voice input for the agent box — local faster-whisper, fully offline.

    The whisper model is downloaded from HuggingFace on first use into
    ~/.deeptorrent/models/ and cached there."""
    enabled: bool = True
    model: str = "base"     # tiny / base / small / medium / large-v3
    language: str = ""      # ISO 639-1 ("en", "pt", …); empty = auto-detect
    device: str = "auto"    # auto / cpu / cuda
    auto_send: bool = True  # send the transcript straight to the agent


@dataclass
class ChatConfig:
    """DeepFlux Room — the serverless community chat on the IRC page
    (ircmgr/room.py). Nothing here auto-connects: the user types a nickname
    and presses Join every session (the nickname is only a prefill)."""
    nickname: str = ""       # prefill for the join bar
    listen_port: int = 7766  # host's preferred TCP port (scans +20 when busy)
    manual_host: str = ""    # last used direct "ip:port" (advanced join)


@dataclass
class StatsConfig:
    """Anonymous usage ping (see infra/telemetry.py).

    While the app runs it POSTs a tiny heartbeat to the project website
    (deepflux.space) every few minutes so the site can show a live
    "users online" count. The payload is a random install id (generated once,
    stored in ~/.deeptorrent/install_id), the app version and the OS —
    nothing else, no identifiers, no paths, no usage details."""
    ping_enabled: bool = True


@dataclass
class DeeptorrentConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    indexer: IndexerConfig = field(default_factory=IndexerConfig)
    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    rss: RSSConfig = field(default_factory=RSSConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    torrents: TorrentsConfig = field(default_factory=TorrentsConfig)
    iptv: IPTVConfig = field(default_factory=IPTVConfig)
    irc: IRCConfig = field(default_factory=IRCConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    default_save_path: str = str(Path.home() / "Downloads" / "DeepFlux")
    categories: List[str] = field(default_factory=lambda: ["Movies", "TV", "Software", "Other"])
    log_level: str = "INFO"
    # Reserved: auto-open completed videos in the Player (currently indicator-only).
    autoplay_completed_video: bool = True
    # UI state (window geometry base64, last active tab) — persisted on close.
    ui_geometry: str = ""
    ui_last_tab: int = 0
    # System tray: completion toasts. (Closing the window quits the app.)
    ui_notifications: bool = True
    # Splitter states (name -> base64 QSplitter.saveState) — persisted on close.
    ui_splitters: Dict[str, str] = field(default_factory=dict)
    # Commander locations are paths, not splitter integers/state. Keeping this
    # separate also lets invalid or unavailable folders fail closed on restore.
    ui_commander_paths: Dict[str, str] = field(default_factory=dict)
    # Agent debug mode: raw tool args/results, full reasoning, watchdog activity in chat.
    ui_agent_debug: bool = True

    @classmethod
    def from_file(cls, path: str) -> "DeeptorrentConfig":
        data: Dict[str, Any] = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:
                logger.warning("Failed to load config from %s: %s", path, exc)

        # Builds before 3.5.9 persisted the shared keys into config.json
        # (to_file serialized what from_file had injected). Forget any saved
        # value that IS a current shared key: the slots below re-inject it in
        # memory, and the next save scrubs the file for good. A genuinely
        # user-typed key never matches and is kept.
        for _path, _shared in (
            (("llm", "api_key"), _SHARED_DEEPSEEK_API_KEY),
            (("web_search", "api_key"), _SHARED_PERPLEXITY_API_KEY),
            (("iptv", "tmdb_api_key"), _SHARED_TMDB_API_KEY),
            (("iptv", "opensubtitles_api_key"), _SHARED_OPENSUBTITLES_API_KEY),
            (("iptv", "tpdb_api_key"), _SHARED_TPDB_API_KEY),
            (("iptv", "stashdb_api_key"), _SHARED_STASHDB_API_KEY),
            (("iptv", "omdb_api_key"), _SHARED_OMDB_API_KEY),
            (("iptv", "fanarttv_api_key"), _SHARED_FANARTTV_API_KEY),
        ):
            _node: Any = data
            for _key in _path[:-1]:
                _node = _node.get(_key, {}) if isinstance(_node, dict) else {}
            if _shared and isinstance(_node, dict) and _node.get(_path[-1]) == _shared:
                _node[_path[-1]] = ""

        # Environment variables fill in secrets only when the config file
        # doesn't already set them — a saved GUI edit must never be clobbered
        # by a machine-level env var on every launch.
        # The DeepSeek env var only fills the key slot when DeepSeek is the
        # configured provider — it must not leak into OpenRouter calls.
        # Since 3.5 the shared DeepSeek key (see _SHARED_DEEPSEEK_API_KEY) is
        # the last-resort fallback for the same slot, under the same gate.
        if (not data.get("llm", {}).get("api_key")
                and data.get("llm", {}).get("provider", "deepseek") in ("", "deepseek")):
            data.setdefault("llm", {})["api_key"] = (
                os.environ.get("DEEPSEEK_API_KEY") or _SHARED_DEEPSEEK_API_KEY)
            _remember_shared_slot("llm.api_key", data["llm"]["api_key"], _SHARED_DEEPSEEK_API_KEY)
        if not data.get("indexer", {}).get("api_key") and os.environ.get("JACKETT_API_KEY"):
            data.setdefault("indexer", {})["api_key"] = os.environ["JACKETT_API_KEY"]
        if not data.get("web_search", {}).get("brave_api_key") and os.environ.get("BRAVE_API_KEY"):
            data.setdefault("web_search", {})["brave_api_key"] = os.environ["BRAVE_API_KEY"]
        # Since 3.5.2 the remaining shared keys (see _embedded_keys.py) are
        # last-resort fallbacks for their slots under the same rules: a saved
        # user key always wins, then the machine-level env var, then the
        # shared in-box key. Jackett stays env-only by decision.
        if not data.get("web_search", {}).get("api_key"):
            data.setdefault("web_search", {})["api_key"] = (
                os.environ.get("PERPLEXITY_API_KEY") or _SHARED_PERPLEXITY_API_KEY)
            _remember_shared_slot("web_search.api_key", data["web_search"]["api_key"], _SHARED_PERPLEXITY_API_KEY)
        if not data.get("iptv", {}).get("tmdb_api_key"):
            data.setdefault("iptv", {})["tmdb_api_key"] = (
                os.environ.get("TMDB_API_KEY") or _SHARED_TMDB_API_KEY)
            _remember_shared_slot("iptv.tmdb_api_key", data["iptv"]["tmdb_api_key"], _SHARED_TMDB_API_KEY)
        if not data.get("iptv", {}).get("opensubtitles_api_key"):
            data.setdefault("iptv", {})["opensubtitles_api_key"] = (
                os.environ.get("OPENSUBTITLES_API_KEY") or _SHARED_OPENSUBTITLES_API_KEY)
            _remember_shared_slot("iptv.opensubtitles_api_key", data["iptv"]["opensubtitles_api_key"], _SHARED_OPENSUBTITLES_API_KEY)
        if not data.get("iptv", {}).get("tpdb_api_key"):
            data.setdefault("iptv", {})["tpdb_api_key"] = (
                os.environ.get("TPDB_API_KEY") or _SHARED_TPDB_API_KEY)
            _remember_shared_slot("iptv.tpdb_api_key", data["iptv"]["tpdb_api_key"], _SHARED_TPDB_API_KEY)
        if not data.get("iptv", {}).get("stashdb_api_key"):
            data.setdefault("iptv", {})["stashdb_api_key"] = (
                os.environ.get("STASHDB_API_KEY") or _SHARED_STASHDB_API_KEY)
            _remember_shared_slot("iptv.stashdb_api_key", data["iptv"]["stashdb_api_key"], _SHARED_STASHDB_API_KEY)
        if not data.get("iptv", {}).get("omdb_api_key"):
            data.setdefault("iptv", {})["omdb_api_key"] = (
                os.environ.get("OMDB_API_KEY") or _SHARED_OMDB_API_KEY)
            _remember_shared_slot("iptv.omdb_api_key", data["iptv"]["omdb_api_key"], _SHARED_OMDB_API_KEY)
        if not data.get("iptv", {}).get("fanarttv_api_key"):
            data.setdefault("iptv", {})["fanarttv_api_key"] = (
                os.environ.get("FANARTTV_API_KEY") or _SHARED_FANARTTV_API_KEY)
            _remember_shared_slot("iptv.fanarttv_api_key", data["iptv"]["fanarttv_api_key"], _SHARED_FANARTTV_API_KEY)

        # No OTHER built-in API keys: everything still empty here stays empty
        # (Jackett and Brave are env-only by decision). The GUI/CLI notice a
        # missing LLM key and run the agent in offline dummy mode until the
        # user enters their own key.
        llm_data = data.setdefault("llm", {})

        # Web search: Perplexity is the last-resort backup provider (only
        # reached when the user has entered a key for it).
        ws_data = data.setdefault("web_search", {})
        ws_data["provider"] = "perplexity"
        # Drop stale keys from older configs (e.g. the removed google_enabled).
        ws_data = {k: v for k, v in ws_data.items() if k in WebSearchConfig.__dataclass_fields__}

        # Migration: if the saved homepage is still a previous default,
        # upgrade it to the new default. Users who explicitly set a custom
        # homepage keep their value.
        _OLD_DEFAULT_HOMEPAGES = {
            "https://n38worth.com/", "https://www.youtube.com/",
            "https://marginalia-search.com/", "https://duckduckgo.com/",
            "https://www.startpage.com/",
        }
        browser_data = data.setdefault("browser", {})
        if browser_data.get("homepage") in _OLD_DEFAULT_HOMEPAGES:
            browser_data["homepage"] = BrowserConfig.homepage
            logger.info("Migrated homepage from old default to %s", BrowserConfig.homepage)

        # Migration: the default download folders moved from
        # ~/Downloads/DeepTorrent (and ~/Downloads/Deeptorrent) to
        # ~/Downloads/DeepFlux. Values still pointing at an old DEFAULT are
        # upgraded; custom user folders are kept. Empty values (seeded by the
        # installer) also resolve to the new default.
        _OLD_DEFAULT_DOWNLOAD_DIRS = {
            str(Path.home() / "Downloads" / "DeepTorrent"),
            str(Path.home() / "Downloads" / "Deeptorrent"),
        }
        _NEW_DEFAULT_DOWNLOAD_DIR = str(Path.home() / "Downloads" / "DeepFlux")
        save_path = (data.get("default_save_path") or "").strip()
        if not save_path or save_path in _OLD_DEFAULT_DOWNLOAD_DIRS:
            save_path = _NEW_DEFAULT_DOWNLOAD_DIR
        download_data = data.setdefault("download", {})
        dl_folder = (download_data.get("default_folder") or "").strip()
        if not dl_folder or dl_folder in _OLD_DEFAULT_DOWNLOAD_DIRS:
            dl_folder = _NEW_DEFAULT_DOWNLOAD_DIR

        # Migration: merge built-in default sources into the saved list so
        # newly shipped sources appear without wiping user customizations
        # (saved entries keep their enabled state; missing defaults append).
        merged_sources = [SourceConfig(**s) for s in data.get("sources", {}).get("sources", [])]
        if merged_sources:
            known_ids = {s.id for s in merged_sources}
            merged_sources.extend(d for d in DEFAULT_SOURCES if d.id not in known_ids)
        else:
            merged_sources = list(DEFAULT_SOURCES)

        # Merge built-in default IRC networks into the saved list (match by
        # `id`, Jackett-style). Existing user networks are preserved; missing
        # defaults append. No default ships channels — connecting opens the
        # channel browser (/LIST) instead of auto-joining anything.
        irc_data = data.get("irc", {})
        merged_irc_nets = [IRCNetworkConfig(**{k: v for k, v in n.items()
                                               if k in IRCNetworkConfig.__dataclass_fields__})
                           for n in irc_data.get("networks", [])]
        if merged_irc_nets:
            known_ids = {n.id for n in merged_irc_nets}
            merged_irc_nets.extend(d for d in DEFAULT_IRC_NETWORKS if d.id not in known_ids)
        else:
            merged_irc_nets = list(DEFAULT_IRC_NETWORKS)

        # Migration: the merge above never touches existing ids, so entries
        # created by the old test-era default keep "#deepflux-test" forever.
        # Upgrade those in place: just drop the test channel (we no longer
        # auto-join #DeepFlux — channelless networks auto-request /LIST instead).
        for n in merged_irc_nets:
            if any(c.lower() == "#deepflux-test" for c in n.channels):
                n.channels = [c for c in n.channels if c.lower() != "#deepflux-test"]

        # Migration: #DeepFlux is no longer auto-joined anywhere (the IRC tab
        # now auto-requests /LIST for networks without configured channels).
        # Strip #DeepFlux from every network where it was the shipped default;
        # user-added channels (private-tracker support channels, etc.) survive.
        for n in merged_irc_nets:
            n.channels = [c for c in n.channels if c.lower() != "#deepflux"]

        # Migration (2026-09): no default ships pre-joined channels any more —
        # every network opens the channel browser on connect instead. Strip
        # the old shipped channel sets (subset-gated, so an entry the user
        # customized with extra channels keeps everything).
        _LEGACY_DEFAULT_CHANNELS = {
            "animebytes": {"#support"},
            "p2p-network": {"#bibliotik-help", "#bitspyder"},
            "digitalirc": {"#empornium-help"},
            "gazellegames": {"#ggn-help"},
            "synirc": {"#jpopsuki-support"},
            "brokensphere": {"#kg-help"},
            "morethantv": {"#help", "#morethan.tv-disabled"},
            "orpheus": {"#help", "#disabled"},
            "passthepopcorn": {"#ptp-help", "#ptp-disabled"},
            "scratch-network": {"#red-help", "#red-disabled"},
            "torrentleech": {"#tlhelp"},
            "iptorrents": {"#iptorrents"},
        }
        for n in merged_irc_nets:
            shipped = _LEGACY_DEFAULT_CHANNELS.get(n.id)
            if shipped and {c.lower() for c in n.channels} <= shipped:
                n.channels = []

        # Migration (2026-09): dead or broken shipped endpoints, verified with
        # a live registration probe (see DEFAULT_IRC_NETWORKS). Retarget only
        # entries still carrying the old shipped host/port/TLS — customized
        # entries are left alone.
        _LEGACY_IRC_TARGETS = {
            # id: (old host, old port, old tls, new host, new port, new tls)
            "undernet": ("irc.undernet.org", 6697, True,
                         "irc.undernet.org", 6667, False),
            "geekshed": ("irc.geekshed.net", 6697, True,
                         "irc.geekshed.net", 6667, False),
            "p2p-network": ("irc.p2p-network.net", 6697, True,
                            "irc.p2p-network.net", 6667, False),
            "brokensphere": ("irc.brokensphere.net", 6697, True,
                             "irc.brokensphere.net", 6667, False),
            "iptorrents": ("irc.iptorrents.com", 7000, True,
                           "irc.iptorrents.com", 6667, False),
            "animebytes": ("irc.animebytes.tv", 7000, True,
                           "irc.animefriends.moe", 7000, True),
        }
        for n in merged_irc_nets:
            legacy = _LEGACY_IRC_TARGETS.get(n.id)
            if legacy and (n.host, n.port, n.tls) == legacy[:3]:
                n.host, n.port, n.tls = legacy[3:]

        # Migration (2026-09): MoreThanTV's own IRC network is gone (the host
        # no longer resolves; MTV support lives on DigitalIRC, which ships as
        # its own default). Drop the dead entry — user-added channels on it
        # cannot be reached anyway.
        merged_irc_nets = [n for n in merged_irc_nets
                           if not (n.id == "morethantv" and n.host == "irc.morethan.tv")]

        # Drop stale keys from older configs (e.g. the removed `local_only`)
        # so LLMConfig(**...) never fails on an unknown field.
        llm_clean = {k: v for k, v in llm_data.items() if k in LLMConfig.__dataclass_fields__}

        # Streaming and persistent memory are assumed always-on (no UI to
        # toggle them anymore) — normalize stale configs that saved them off.
        llm_clean["stream"] = True
        llm_clean["memory_enabled"] = True

        # Migration (2026-09, DeepSeek V4.1): the direct API renamed its lineup —
        # deepseek-flash (V4.1-Flash) replaces the retired deepseek-v4-flash /
        # -vision-exp aliases and the V3-era deepseek-chat / -reasoner names.
        # deepseek-v4-pro is ALSO migrated: DeepSeek's pricing page says Flash
        # surpasses it in performance/cost/speed and auto-routes v4-pro to
        # Flash from 2026-09-14, so configs saved on the old preset default
        # (written by <=3.5.8 settings saves) move to Flash now instead of
        # showing/being billed as the outgoing model. Custom endpoints are
        # never remapped — they may legitimately serve these exact names.
        _provider_now = llm_clean.get("provider", "deepseek")
        _direct_aliases = {
            "deepseek-v4-flash": "deepseek-flash",
            "deepseek-v4-flash-vision-exp": "deepseek-flash",
            "deepseek-chat": "deepseek-flash",
            "deepseek-reasoner": "deepseek-flash",
            "deepseek-v4-pro": "deepseek-flash",
        }
        _openrouter_aliases = {
            "deepseek/deepseek-v4-flash": "deepseek/deepseek-v4.1-flash",
            "deepseek/deepseek-v4-flash-vision-exp": "deepseek/deepseek-v4.1-flash",
            "deepseek/deepseek-v4-pro": "deepseek/deepseek-v4.1-flash",
        }
        for _field in ("model", "fast_model"):
            _saved = llm_clean.get(_field)
            if _provider_now == "deepseek" and _saved in _direct_aliases:
                llm_clean[_field] = _direct_aliases[_saved]
            elif _provider_now == "openrouter" and _saved in _openrouter_aliases:
                llm_clean[_field] = _openrouter_aliases[_saved]

        # Migration: a switch to OpenRouter/custom keeps the DeepSeek fast-model
        # default, which may not exist there — clear it so summary
        # calls fall back to the main model instead of 404ing.
        if llm_clean.get("provider") in ("openrouter", "custom") and llm_clean.get("fast_model") in LLM_PROVIDER_PRESETS["deepseek"]["models"]:
            llm_clean["fast_model"] = ""

        # Migration: the artwork cache cap was 500 MB and never enforced; the
        # default is now 10 GB (actually enforced via LRU eviction). Configs
        # still carrying the old default move up with it.
        iptv_cache_limit = data.get("iptv", {}).get("cache_limit_mb", 10240)
        if iptv_cache_limit == 500:
            iptv_cache_limit = 10240

        return cls(
            llm=LLMConfig(**llm_clean),
            indexer=IndexerConfig(**data.get("indexer", {})),
            web_search=WebSearchConfig(**ws_data),
            watchdog=WatchdogConfig(**data.get("watchdog", {})),
            rss=RSSConfig(
                feeds=[RSSFeed(**f) for f in data.get("rss", {}).get("feeds", [])],
                check_interval_seconds=data.get("rss", {}).get("check_interval_seconds", 300),
            ),
            browser=BrowserConfig(
                # Empty/missing homepage migrates to the default site; custom
                # user homepages are preserved.
                homepage=(data.get("browser", {}).get("homepage") or "").strip() or DEFAULT_BROWSER_HOMEPAGE,
                search_engine=(data.get("browser", {}).get("search_engine")
                               if data.get("browser", {}).get("search_engine") in BROWSER_SEARCH_ENGINES else "google"),
                bookmarks=[Bookmark(**b) for b in data.get("browser", {}).get("bookmarks", [])],
                adblock_enabled=data.get("browser", {}).get("adblock_enabled", True),
                adblock_disabled_sites=[str(host).lower() for host in data.get("browser", {}).get("adblock_disabled_sites", [])],
                extension_enabled=data.get("browser", {}).get("extension_enabled", False),
                grabber_auto_queue=bool(data.get("browser", {}).get("grabber_auto_queue", False)),
                grabber_auto_limit=max(1, int(data.get("browser", {}).get("grabber_auto_limit", 5) or 5)),
                grabber_last_site=str(data.get("browser", {}).get("grabber_last_site", "") or ""),
                grabber_search_templates=({str(k): str(v) for k, v in data.get("browser", {}).get("grabber_search_templates", {}).items()}
                                          if isinstance(data.get("browser", {}).get("grabber_search_templates", {}), dict) else {}),
                agent_content_permissions=(data.get("browser", {}).get("agent_content_permissions", {})
                                           if isinstance(data.get("browser", {}).get("agent_content_permissions", {}), dict) else {}),
                restore_tabs=bool(data.get("browser", {}).get("restore_tabs", True)),
                open_tabs=([str(url) for url in data.get("browser", {}).get("open_tabs", [])[:20]]
                           if isinstance(data.get("browser", {}).get("open_tabs", []), list) else []),
                active_tab=max(0, int(data.get("browser", {}).get("active_tab", 0) or 0)),
                history_enabled=bool(data.get("browser", {}).get("history_enabled", True)),
                history_retention_days=max(1, int(data.get("browser", {}).get("history_retention_days", 90) or 90)),
                zoom_by_origin=(data.get("browser", {}).get("zoom_by_origin", {})
                                if isinstance(data.get("browser", {}).get("zoom_by_origin", {}), dict) else {}),
            ),
            download=DownloadConfig(
                max_concurrent=data.get("download", {}).get("max_concurrent", 3),
                max_connections_per_download=data.get("download", {}).get("max_connections_per_download", 8),
                default_folder=dl_folder,
                bandwidth_limit_bps=data.get("download", {}).get("bandwidth_limit_bps", 0),
                auto_start=data.get("download", {}).get("auto_start", True),
                segment_threshold_mb=data.get("download", {}).get("segment_threshold_mb", 1),
                control_api_port=data.get("download", {}).get("control_api_port", 53742),
                ffmpeg_path=data.get("download", {}).get("ffmpeg_path", ""),
                stream_max_height=max(0, int(data.get("download", {}).get("stream_max_height", 0) or 0)),
                youtube_max_height=max(144, int(data.get("download", {}).get("youtube_max_height", 1080) or 1080)),
                youtube_subtitles=bool(data.get("download", {}).get("youtube_subtitles", False)),
                youtube_playlists=bool(data.get("download", {}).get("youtube_playlists", False)),
                youtube_update_check=bool(data.get("download", {}).get("youtube_update_check", True)),
                ytdlp_last_check=float(data.get("download", {}).get("ytdlp_last_check", 0.0) or 0.0),
                categories=[DownloadCategory(**c) for c in data.get("download", {}).get("categories", [])],
            ),
            torrents=TorrentsConfig(**data.get("torrents", {})),
            iptv=IPTVConfig(
                # Filter unknown keys: a config written by a NEWER build must
                # not crash an older one with a TypeError here.
                sources=[IPTVSourceConfig(**{k: v for k, v in s.items()
                                             if k in IPTVSourceConfig.__dataclass_fields__})
                         for s in data.get("iptv", {}).get("sources", [])],
                tmdb_api_key=data.get("iptv", {}).get("tmdb_api_key", ""),
                tpdb_api_key=data.get("iptv", {}).get("tpdb_api_key", ""),
                stashdb_api_key=data.get("iptv", {}).get("stashdb_api_key", ""),
                omdb_api_key=data.get("iptv", {}).get("omdb_api_key", ""),
                fanarttv_api_key=data.get("iptv", {}).get("fanarttv_api_key", ""),
                enable_javbus=bool(data.get("iptv", {}).get("enable_javbus", True)),
                enable_javlibrary=bool(data.get("iptv", {}).get("enable_javlibrary", True)),
                enable_fanza=bool(data.get("iptv", {}).get("enable_fanza", True)),
                enable_wikipedia=bool(data.get("iptv", {}).get("enable_wikipedia", True)),
                framegrab_posters=bool(data.get("iptv", {}).get("framegrab_posters", True)),
                opensubtitles_api_key=data.get("iptv", {}).get("opensubtitles_api_key", ""),
                opensubtitles_username=data.get("iptv", {}).get("opensubtitles_username", ""),
                opensubtitles_password=data.get("iptv", {}).get("opensubtitles_password", ""),
                preferred_audio_lang=data.get("iptv", {}).get("preferred_audio_lang", ""),
                preferred_sub_lang=data.get("iptv", {}).get("preferred_sub_lang", ""),
                epg_url=data.get("iptv", {}).get("epg_url", ""),
                cache_dir=data.get("iptv", {}).get("cache_dir", ""),
                cache_limit_mb=iptv_cache_limit,
                xtream_series_concurrency=max(1, min(6, int(
                    data.get("iptv", {}).get("xtream_series_concurrency", 2) or 2))),
                cache_seconds=max(1, min(120, int(
                    data.get("iptv", {}).get("cache_seconds", 15) or 15))),
                live_pause_buffer_seconds=max(0, min(3600, int(
                    data.get("iptv", {}).get("live_pause_buffer_seconds", 300) or 0))),
                recording_dir=data.get("iptv", {}).get("recording_dir", ""),
                hwdec=data.get("iptv", {}).get("hwdec", "auto-safe"),
                interpolation=bool(data.get("iptv", {}).get("interpolation", True)),
                svp_enabled=bool(data.get("iptv", {}).get("svp_enabled", False)),
                milkdrop_enabled=bool(data.get("iptv", {}).get("milkdrop_enabled", True)),
                milkdrop_preset=data.get("iptv", {}).get("milkdrop_preset", ""),
                preferred_player=data.get("iptv", {}).get("preferred_player", "mpv"),
                enable_epg=data.get("iptv", {}).get("enable_epg", True),
                auto_try_next_source=data.get("iptv", {}).get("auto_try_next_source", False),
                throttle_torrents=data.get("iptv", {}).get("throttle_torrents", True),
                throttle_download_kb=data.get("iptv", {}).get("throttle_download_kb", 4096),
                throttle_upload_kb=data.get("iptv", {}).get("throttle_upload_kb", 512),
                volume=int(data.get("iptv", {}).get("volume", 100)),
                muted=bool(data.get("iptv", {}).get("muted", False)),
                overscan_pct=float(data.get("iptv", {}).get("overscan_pct", 0.5)),
                audio_delay=float(data.get("iptv", {}).get("audio_delay", 0.0)),
                vod_group_mode=data.get("iptv", {}).get("vod_group_mode", "year"),
            ),
            irc=IRCConfig(
                networks=merged_irc_nets,
                buffer_lines=max(50, min(5000, int(irc_data.get("buffer_lines", 500)))),
                flood_delay=max(0.5, float(irc_data.get("flood_delay", 2.0))),
                reconnect_max_seconds=max(10, min(3600, int(
                    irc_data.get("reconnect_max_seconds", 300)))),
                reconnect_max_attempts=max(1, min(100, int(
                    irc_data.get("reconnect_max_attempts", 5)))),
                history_enabled=bool(irc_data.get("history_enabled", False)),
                history_private_messages=bool(
                    irc_data.get("history_private_messages", False)),
                history_retention_days=max(1, min(3650, int(
                    irc_data.get("history_retention_days", 30) or 30))),
            ),
            voice=VoiceConfig(**{k: v for k, v in data.get("voice", {}).items()
                                 if k in VoiceConfig.__dataclass_fields__}),
            chat=ChatConfig(
                nickname=str(data.get("chat", {}).get("nickname", "") or "")[:24],
                listen_port=max(1024, min(65535, int(
                    data.get("chat", {}).get("listen_port", 7766) or 7766))),
                manual_host=str(data.get("chat", {}).get("manual_host", "") or "")[:64],
            ),
            stats=StatsConfig(**{k: v for k, v in data.get("stats", {}).items()
                                 if k in StatsConfig.__dataclass_fields__}),
            sources=SourcesConfig(
                sources=merged_sources,
                use_jackett=data.get("sources", {}).get("use_jackett", True),
                search_batch_size=data.get("sources", {}).get("search_batch_size", 5),
                min_results=data.get("sources", {}).get("min_results", 5),
                min_seeders=data.get("sources", {}).get("min_seeders", 10),
                last_jackett_fetch=float(data.get("sources", {}).get("last_jackett_fetch", 0.0) or 0.0),
            ),
            default_save_path=save_path,
            categories=data.get("categories", ["Movies", "TV", "Software", "Other"]),
            log_level=data.get("log_level", "INFO"),
            autoplay_completed_video=bool(data.get("autoplay_completed_video", True)),
            ui_geometry=data.get("ui_geometry", ""),
            ui_last_tab=int(data.get("ui_last_tab", 0) or 0),
            ui_notifications=bool(data.get("ui_notifications", True)),
            ui_splitters=data.get("ui_splitters", {}) if isinstance(data.get("ui_splitters"), dict) else {},
            ui_commander_paths={
                key: value for key, value in data.get("ui_commander_paths", {}).items()
                if key in ("left", "right") and isinstance(value, str)
            } if isinstance(data.get("ui_commander_paths"), dict) else {},
            ui_agent_debug=True,  # assumed always-on (no UI toggle anymore)
        )

    def sanitized_dict(self) -> Dict[str, Any]:
        """asdict() with process-injected shared keys blanked.

        The shared in-box keys are re-injected by from_file on every load, so
        they must never be persisted (config.json, settings backups) — that
        would drop a plaintext copy of them on every installed machine. A slot
        whose value no longer matches the injected one carries a user-typed
        key and is kept.
        """
        data = asdict(self)
        for path, value in _SHARED_KEY_SLOTS.items():
            node: Any = data
            keys = path.split(".")
            for key in keys[:-1]:
                node = node.get(key, {}) if isinstance(node, dict) else {}
            if isinstance(node, dict) and node.get(keys[-1]) == value:
                node[keys[-1]] = ""
        return data

    def to_file(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.sanitized_dict(), f, indent=2)

    def default_config_path() -> str:
        return str(Path.home() / ".deeptorrent" / "config.json")
