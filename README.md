# DeepFlux

LLM-Powered Browser, Downloader, File Manager & Player (Windows).

DeepFlux is a Python desktop app where an LLM agent drives the whole
toolbox through structured tool calling: a Chromium browser, a BitTorrent
engine (libtorrent-rasterbar), an IDM-style segmented download manager with
HLS/DASH capture, an IPTV/media player, a dual-pane file manager and an IRC
client. The agent searches indexers and the web, diagnoses stalled swarms,
finds alternate trackers and releases, manages the download queue, drives the
browser, and organizes completed downloads — asking for confirmation before
anything destructive.

Website and installer: **https://deepflux.space**

**Current version: 3.5.5** (2026-09-10) — kept in sync with
`packaging/installer.iss` by the pre-commit hook in `.git/hooks/`.

## Features

- **Agent** (`agent/`): ReAct loop with JSON-schema tools, OpenAI-compatible
  providers (DeepSeek direct, OpenRouter, or any custom endpoint), streaming
  with reasoning capture, persistent memory (`~/.deeptorrent/memory/`),
  confirmation for destructive actions, a stalled-torrent watchdog, and
  voice input (local whisper).
- **Browse** (`gui/main_window.py`, `dlmgr/browser_extension.py`,
  `dlmgr/adblock.py`): full Chromium browser (QtWebEngine) with ad blocking,
  bookmarks/history, private tabs, an in-page video grabber, and the
  **Site Grabber** — keyword search on any video site, results with
  thumbnails, batch-queued downloads (streams are found by scanning the video
  page, including obfuscated player scripts). Browser downloads route to the
  download manager with the session's cookies.
- **Download** (`engine/`, `dlmgr/`): queue-backed `libtorrent` wrapper on its
  own thread; segmented multi-connection HTTP downloads with resume;
  HLS/DASH stream capture remuxed by the bundled FFmpeg (LGPL);
  stream-while-downloading; Jackett/Torznab search with automatic indexer
  sync; RSS feeds with auto-download.
- **Play** (`iptv/`, `gui/iptv_tab.py`): IPTV (M3U, Xtream Codes) and local
  media library, EPG, artwork/metadata (TMDb), subtitles (OpenSubtitles),
  mpv backend with HDR10 / Dolby Vision tone mapping (VLC backend optional).
- **Command** (`gui/commander_tab.py`): dual-pane file manager
  (Double Commander style) the agent can operate.
- **IRC** (`ircmgr/`, `gui/irc_tab.py`): multi-network client (TLS, SASL,
  flood-safe pacing, auto-reconnect); channels are buffered so the agent can
  read/search them and — with confirmation — join, part and post.
- **Chrome extension** (`chrome_extension/`, `native_messaging/`): sends
  downloads from an external Chrome to DeepFlux via a native messaging host
  the installer registers.
- **Settings backup**: File → Export/Import Settings writes a
  passphrase-encrypted `.dfc` with every setting and key.
- **Windows installer** (`packaging/`): PyInstaller onedir build + Inno Setup
  wizard.

No API keys are bundled: every key field defaults to empty, and the app
degrades gracefully without them (the agent runs in a placeholder mode,
integrations that need a key are skipped).

## Quick start (from source)

### 1. Install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
```

`libtorrent` 2.0 Python wheels are installed automatically on Windows.
For the media player and stream capture, place `libmpv-2.dll` in
`packaging/mpv/` (see `packaging/mpv/README.txt`) and `ffmpeg.exe` in
`packaging/ffmpeg/`; they are not in the repository.

### 2. Run

```powershell
python main.py          # GUI (default)
python main.py --cli    # legacy command-line REPL
```

Enter your keys in **File → API Keys** (LLM endpoint/key, Jackett, Brave,
Perplexity, TMDb, OpenSubtitles). Settings live in
`%USERPROFILE%\.deeptorrent\config.json` (the folder keeps the project's
original name for compatibility) and can also be edited by hand:

```json
{
  "llm": {
    "provider": "deepseek",
    "api_key": "YOUR_DEEPSEEK_API_KEY",
    "base_url": "",
    "model": "deepseek-v4-pro",
    "fast_model": "deepseek-v4-flash",
    "reasoning_effort": "high"
  },
  "indexer": {
    "url": "http://localhost:9117",
    "api_key": "YOUR_JACKETT_API_KEY",
    "torznab_path": "/api/v2.0/indexers/all/results/torznab",
    "timeout": 30
  },
  "web_search": {
    "provider": "perplexity",
    "api_key": "",
    "brave_api_key": "",
    "cx": "",
    "base_url": ""
  },
  "watchdog": {
    "enabled": false,
    "stall_threshold_seconds": 300,
    "auto_heal": false
  },
  "default_save_path": "C:\\Users\\%USERNAME%\\Downloads\\DeepFlux",
  "categories": ["Movies", "TV", "Software", "Other"],
  "log_level": "INFO"
}
```

Web search always uses DuckDuckGo (keyless); Brave and Perplexity are added in
parallel when their keys are set. `DEEPSEEK_API_KEY`, `JACKETT_API_KEY` and
`BRAVE_API_KEY` environment variables fill empty slots.

### 3. Indexers (Jackett)

Install Jackett locally (DeepFlux starts it when it is down and syncs the
indexer list automatically) or run the provided Docker Compose service:

```powershell
cd infra
docker-compose up -d jackett
```

Open http://localhost:9117, add indexers, and enter the API key in
File → API Keys; **Test Connection** fetches your indexer list.

### 4. Chrome extension (optional)

Open `chrome://extensions`, enable Developer mode, **Load unpacked** and pick
the `chrome_extension/` folder (in an installed build:
`_internal\chrome_extension\`). The native messaging host is registered by the
installer, or manually with `python main.py --register-native-host`.

### 5. Run tests

```powershell
python -m pytest tests/ -v
```

## Build pipeline

### PyInstaller onedir build

```powershell
python -m PyInstaller packaging/app.spec --clean --noconfirm
```

Output is written to `dist/DeepFlux/` (launcher `DeepFlux.exe` + `_internal/`).

### Inno Setup installer

Install [Inno Setup](https://jrsoftware.org/isinfo.php), then:

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging/installer.iss
```

The installer `dist/DeepFlux<version>Setup.exe`:
- installs the PyInstaller output folder to `{app}` and creates Start Menu
  and optional Desktop shortcuts;
- registers file/protocol associations (`.torrent`, `magnet:`, media, html)
  and the Chrome native messaging host;
- writes a fresh config with every API-key field empty on each install (no
  keys are bundled; you enter your own in the GUI);
- registers an uninstaller that also removes the app's data directory.

## Stalled-torrent recovery example

The watchdog notices a torrent has made no progress for longer than the
threshold and escalates through tracker discovery, alternate trackers,
indexer search, and finally an alternate release search. Each step is logged
with the LLM's reasoning summary.

```
[WATCHDOG] stalled torrent aabbcc... (progress 0.0000 for 320s)
[WATCHDOG] diagnose_swarm -> dead (no trackers responding and zero DHT peers)
[WATCHDOG] reasoning: "Torrent has no responsive trackers. I will refresh the public tracker list first."
[WATCHDOG] refresh_tracker_list -> 12 trackers
[WATCHDOG] reasoning: "I found 12 public trackers. Adding them to the torrent may revive the swarm."
[WATCHDOG] add_tracker -> success (udp://tracker.opentrackr.org:1337/announce)
[WATCHDOG] reasoning: "Trackers added but still no peers. I will search indexers for a better-seeded copy."
[WATCHDOG] search_indexers -> 3 results, best: "Ubuntu 24.04 (magnet:...)" with 45 seeders
[WATCHDOG] reasoning: "A better-seeded copy exists. Asking user confirmation to add it."
> User: yes
Added better-seeded magnet, removed stale torrent.
```

If `watchdog.auto_heal` is `true`, the agent applies torrent-recovery actions
(a narrow allowlist) automatically; otherwise it pauses at each step and asks
for confirmation.

## Project structure

```
DeepFlux/
├── agent/               # tools, ReAct loop, LLM clients, memory, organizer, RSS
├── chrome_extension/    # Chrome extension (sends downloads to DeepFlux)
├── dlmgr/               # segmented download engine, HLS/DASH, extractors, site grabber, ad-block
├── engine/              # libtorrent wrapper
├── gui/                 # PySide6 main window, tabs and dialogs
├── infra/               # Jackett service/sync, settings backup, file associations, docker-compose
├── iptv/                # IPTV/media player backend (mpv), Xtream, EPG, subtitles
├── ircmgr/              # embedded IRC client core
├── native_messaging/    # Chrome native messaging host + registration
├── packaging/           # PyInstaller spec, Inno Setup script, icons, bundled binaries
├── tests/               # pytest suite
├── website/             # deepflux.space (Vercel)
├── main.py              # GUI/CLI entry point
├── config.py            # configuration dataclasses and loader
├── AGENTS.md            # engineering notes for contributors and coding agents
└── pyproject.toml       # project metadata
```

## Safety and notes

- The LLM never implements BitTorrent piece-selection or peer-wire logic;
  that remains inside libtorrent.
- Destructive actions (file removal, adding new torrent sources, tracker
  changes, file moves, cancelling downloads) require confirmation unless
  the watchdog's auto-heal is enabled — and even then only its
  torrent-recovery allowlist runs unattended.
- Web fetches and downloads reject local/private network targets and
  validate every redirect; tool arguments and results are redacted before
  they reach logs or the debug UI.
- Web-search and indexer queries are rate-limited and include retry logic.
- Public tracker list URLs are configurable; no private tracker credentials
  are hardcoded.

## License

DeepFlux is released under the [MIT License](LICENSE) — you are free to use,
modify, rebuild and redistribute it.

Bundled third-party components keep their own licenses: libmpv and FFmpeg
(LGPL — notices ship in the install directory under `_internal\licenses\`),
Qt/PySide6 (LGPL), libtorrent-rasterbar (BSD), plus the Python packages
listed in `pyproject.toml`.
