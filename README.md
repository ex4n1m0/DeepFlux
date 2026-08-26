# Deeptorrent

LLM-Assisted Torrent Download Manager (local-first, swarm-aware, Windows installer).

Deeptorrent is a Python-based BitTorrent client where the download engine
(libtorrent-rasterbar) is controlled by an LLM agent through structured
function/tool calling. The LLM acts as an active participant: it diagnoses
stalled swarms, searches self-hosted Torznab indexers for better sources,
finds alternate trackers and releases, and organizes completed downloads.

## Features

- **Engine** (`engine/`): Queue-backed `libtorrent` wrapper running in its own
  thread with a Future-based command interface.
- **Tool-calling** (`agent/tools.py`): JSON-schema tool definitions compatible
  with DeepSeek function calling.
- **Swarm health**: `diagnose_swarm` returns healthy / stalled / dead with
  likely cause.
- **Discovery tools**:
  - `refresh_tracker_list`: curated public tracker lists.
  - `find_alt_trackers` & `find_alt_release`: web search fallbacks.
  - `search_indexers`: Torznab queries against Jackett.
- **ReAct agent loop** (`agent/loop.py`): Swappable LLM providers, built-in
  confirmation for destructive actions, autonomous watchdog mode.
- **Metadata organizer** (`agent/organizer.py`): Proposes clean filenames and
  categories on completion.
- **CLI REPL** (`main.py --cli`): Natural-language commands and live torrent table.
- **GUI** (`main.py` or `gui/`): PySide6 — agent chat, torrents + IDM-style
  download manager, built-in Chromium browser, IPTV/media player, dual-pane
  file manager.
- **IRC client** (`ircmgr/` + `gui/irc_tab.py`): Embedded multi-network IRC
  client (TLS, SASL, flood-safe pacing, auto-reconnect). Open channels are
  continuously buffered, and the agent can read/search them on demand
  (`irc_status`, `irc_list_messages`, `irc_search_messages`) or — with user
  confirmation — join/part channels and post messages.
- **Windows installer** (`packaging/`): PyInstaller onedir build + Inno Setup
  wizard.

## Quick start

### 1. Install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
```

`libtorrent` 2.0 Python wheels will be installed automatically on Windows.

### 2. Configure LLM

Copy the default config file and edit it:

```powershell
python -c "from config import DeeptorrentConfig; DeeptorrentConfig().to_file(DeeptorrentConfig.default_config_path())"
notepad $env:USERPROFILE\.deeptorrent\config.json
```

Example `config.json`:

```json
{
  "llm": {
    "provider": "deepseek",
    "api_key": "YOUR_DEEPSEEK_API_KEY",
    "base_url": "",
    "model": "openai/gpt-3.5-turbo",
    "local_only": false
  },
  "indexer": {
    "url": "http://localhost:9117",
    "api_key": "YOUR_JACKETT_API_KEY",
    "torznab_path": "/api/v2.0/indexers/all/results/torznab",
    "timeout": 30
  },
  "web_search": {
    "provider": "duckduckgo",
    "api_key": "",
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

The default cloud provider is DeepSeek. With no API key, the app runs in
`dummy` / placeholder mode for development and basic command demos.

### 3. Start the CLI REPL

```powershell
python main.py
```

Example session:

```
> add magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0 to Movies
I need your confirmation before executing: add_magnet. Reply 'yes' to proceed.
> yes
- Executed call_add_magnet: {"success": true, "info_hash": "dd8255ecdc7ca55fb0bbf81323d87062ec9b3ee0", ...}
Name  Hash          State                 Progress  Down   Up     Seeds  Peers  Size   Health
----------------------------------------------------------------------------------------------
      dd8255ecdc7c  downloading_metadata  0.0%      0 B/s  0 B/s  0      0      0.0 B  stalled
> list torrents
Here are the current torrents.
Name  Hash          State                 Progress  Down   Up     Seeds  Peers  Size   Health
----------------------------------------------------------------------------------------------
      dd8255ecdc7c  downloading_metadata  0.0%      0 B/s  0 B/s  0      0      0.0 B  stalled
> pause everything over 50GB
Paused 0 torrent(s) over 50 GB: []
No torrents.
```

### 4. Start the GUI

```powershell
python main.py --gui
```

### 5. Self-hosted indexer aggregator

A Docker Compose service for Jackett is provided in `infra/docker-compose.yml`:

```powershell
cd infra
docker-compose up -d jackett
```

Open http://localhost:9117, add public indexers, and copy the API key into
`config.json` under `indexer.api_key`.

### 6. Run tests

```powershell
python -m pytest tests/ -v
```

## Build pipeline

### PyInstaller onedir build

```powershell
python -m PyInstaller packaging/app.spec --clean --noconfirm
```

Output is written to `dist/DeepFlux/`.

### Inno Setup installer

1. Download and install [Inno Setup](https://jrsoftware.org/isinfo.php).
2. Open `packaging/installer.iss` in Inno Setup Compiler.
3. Click **Compile**.
4. The installer `dist/DeepFlux1.7Setup.exe` is produced.

The installer:
- Installs the PyInstaller output folder to `{app}`.
- Creates Start Menu and optional Desktop shortcuts.
- Writes a fresh config with all API-key fields empty (no keys are bundled;
  you enter your own in the GUI settings dialogs).
- Registers an uninstaller.

## Stalled-torrent recovery example

In this transcript the watchdog notices a torrent has made no progress for
more than the threshold, escalates through tracker discovery, alternate
trackers, indexer search, and finally an alternate release search. Each step
is logged with the LLM's reasoning summary.

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

If `watchdog.auto_heal` is `true`, the agent will add the new source and
update trackers automatically; otherwise it pauses at each destructive step
and asks for confirmation.

## Project structure

```
Deeptorrent/
├── engine/              # libtorrent wrapper
├── agent/               # tools, ReAct loop, LLM clients, organizer
├── dlmgr/               # IDM-style segmented download engine (+ HLS/DASH)
├── ircmgr/              # embedded IRC client core (thread-safe, jaraco/irc)
├── gui/                 # PySide6 main window and tabs
├── infra/               # docker-compose for Jackett
├── packaging/           # PyInstaller spec and Inno Setup script
├── tests/               # pytest suite
├── main.py              # CLI/GUI entry point
├── config.py            # configuration dataclasses and loader
├── pyproject.toml       # project metadata
└── README.md
```

## Safety and notes

- The LLM never implements BitTorrent piece-selection or peer-wire logic;
  that remains inside libtorrent.
- All destructive actions (file removal, adding new torrent sources, tracker
  changes, file moves) require confirmation unless auto-heal is enabled.
- Web-search and indexer queries are rate-limited and include retry logic.
- Public tracker list URLs are configurable; no private tracker credentials
  are hardcoded.

## License

MIT
