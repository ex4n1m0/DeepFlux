# DeepFlux IDM-Style Download Manager + Chrome Extension — Implementation Plan

## Architecture Overview

The feature is additive — no existing torrent functionality is touched. New code lives in a `dlmgr/` package (download manager engine + API), a `gui/downloads_tab.py` (UI), a `native_messaging/` module (Chrome extension bridge), and a `chrome_extension/` directory (the extension itself).

```
Chrome tab → content script (DOM/player scan)
           → background service worker (network sniff + job assembly)
           → chrome.runtime.connectNative()
           → DeepFlux.exe --native-messaging (stdio host mode)
           → Local Control API (loopback HTTP on port 53742)
           → Segmented Download Engine / HLS-DASH Pipeline
           → "Downloads" tab UI (progress, completion)
```

---

## New File Structure

```
Deeptorrent/
├── dlmgr/                          # Download manager package
│   ├── __init__.py
│   ├── engine.py                   # Segmented download engine core
│   ├── job.py                      # DownloadJob dataclass + state machine
│   ├── segment.py                  # Segment worker (Range request + write at offset)
│   ├── scheduler.py                # Priority queue + concurrency control + bandwidth throttle
│   ├── resume_state.py             # Byte-completion bitmap persistence (.dtresume files)
│   ├── hls_dash.py                 # HLS/DASH manifest parser + segment downloader + AES-128 decrypt
│   ├── ffmpeg.py                   # FFmpeg wrapper (remux/transcode via stream-copy)
│   ├── control_api.py              # Loopback HTTP server (port 53742) — job submit/query/control
│   └── extractors/                 # Pluggable site-specific extractors
│       ├── __init__.py             # Extractor registry + hot-reload
│       └── generic.py              # Generic manifest sniffer (default)
├── native_messaging/
│   ├── __init__.py
│   ├── host.py                     # Native messaging stdio protocol (4-byte length-prefixed JSON)
│   └── register.py                 # Windows registry registration for native messaging host
├── gui/
│   ├── downloads_tab.py            # New "Downloads" tab widget (queue list, details, settings sub-panel)
│   └── downloads_settings.py       # Download manager settings section for Settings dialog
├── chrome_extension/               # Standalone Manifest V3 extension
│   ├── manifest.json
│   ├── background.js               # Service worker: native messaging, download interception, network sniff
│   ├── content.js                  # Content script: DOM/player scan, video overlay button
│   ├── popup.html                  # Toolbar popup: detected media list
│   ├── popup.js
│   ├── options.html                # Options page: auto-capture toggles, thresholds
│   ├── options.js
│   ├── overlay.css                 # Floating "Download Video" button styles
│   └── icons/                      # Extension icons (16/32/48/128)
├── config.py                       # MODIFIED: add DownloadConfig dataclass
├── gui/main_window.py              # MODIFIED: add "Downloads" tab to main_tabs
├── gui/settings_dialog.py          # MODIFIED: add Downloads settings section
├── main.py                         # MODIFIED: --native-messaging flag for stdio host mode
├── packaging/
│   ├── app.spec                    # MODIFIED: bundle FFmpeg, chrome_extension/, new hidden imports
│   └── installer.iss               # MODIFIED: bundle ffmpeg.exe, register native messaging host
└── requirements.txt                # MODIFIED: add m3u8, pycryptodome (AES-128)
```

---

## Phase 1 — Downloads Tab UI + Segmented Download Engine + Local Control API

### 1.1 Config (`config.py`)

Add `DownloadConfig` dataclass:

```python
@dataclass
class DownloadCategory:
    name: str = ""           # video, audio, archive, program, document
    folder: str = ""         # per-category download folder
    extensions: List[str] = field(default_factory=list)

@dataclass
class DownloadConfig:
    max_concurrent: int = 3
    max_connections_per_download: int = 8       # 1–32
    default_folder: str = str(Path.home() / "Downloads" / "DeepFlux")
    bandwidth_limit_bps: int = 0                # 0 = unlimited
    auto_start: bool = True
    segment_threshold_mb: int = 1               # files smaller than this use single-stream
    categories: List[DownloadCategory] = field(default_factory=list)
    control_api_port: int = 53742
    ffmpeg_path: str = ""                       # empty = use bundled
```

Add `download: DownloadConfig` field to `DeeptorrentConfig`. Update `from_file()` and `to_file()`.

### 1.2 Download Job Model (`dlmgr/job.py`)

```python
class JobStatus(Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"

@dataclass
class DownloadJob:
    id: str                          # UUID
    url: str
    filename: str
    save_path: str
    file_size: int = 0
    downloaded: int = 0
    status: JobStatus = JobStatus.QUEUED
    speed_bps: int = 0
    eta_seconds: int = 0
    source_url: str = ""
    job_type: str = "file"           # "file" | "hls" | "dash"
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: str = ""
    referrer: str = ""
    supports_ranges: bool = False
    segments: List[SegmentState] = field(default_factory=list)
    error_message: str = ""
    created_at: float = field(default_factory=time.time)
    priority: int = 0
```

### 1.3 Segment Worker (`dlmgr/segment.py`)

- Each segment: `(start_byte, end_byte, current_pos, status, speed)`
- Uses `requests.get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True)`
- Writes to pre-allocated file at offset via `f.seek(start); f.write(chunk)`
- Reports progress via callback (bytes downloaded this tick)
- On stall (no data for N seconds), signals the scheduler for rebalancing

### 1.4 Segmented Download Engine (`dlmgr/engine.py`)

Core orchestration:
1. **Job creation**: `HEAD` request → check `Accept-Ranges: bytes` + `Content-Length`
2. **Pre-allocate**: `f.seek(file_size - 1); f.write(b'\x00')` to sparse-allocate
3. **Segment split**: Divide file into N segments (default 8, max 32)
4. **Concurrent download**: Thread pool per segment, each writing at its offset
5. **Dynamic rebalancing**: Monitor segment throughput every 2s. If a segment is stalled (0 bytes/s for 10s) or finished, split its remaining range and assign to a new worker. Slow segments donate their tail to faster workers.
6. **Resume state**: Persist `.dtresume` file alongside download (JSON: job_id, url, segment bitmap, headers). On restart, load incomplete jobs and re-request only missing ranges.
7. **No-range fallback**: Single sequential stream with `Range: bytes={resume_pos}-` resume support.
8. **Auth support**: Forward cookies, Referer, User-Agent, Authorization headers.
9. **Bandwidth throttling**: Token-bucket per-job and global, enforced in segment write loop.

### 1.5 Scheduler (`dlmgr/scheduler.py`)

- Priority queue (heapq by `priority` then `created_at`)
- Respects `max_concurrent` limit
- Global bandwidth limiter (shared token bucket)
- Per-job bandwidth limiter
- Emits status updates via callback → UI timer picks them up

### 1.6 Resume State (`dlmgr/resume_state.py`)

- `.dtresume` JSON file per job in the download folder
- Stores: job_id, url, filename, file_size, segments `[{start, end, completed_bytes}]`, headers, cookies
- Atomic write (write to `.tmp` then rename)
- On engine startup, scans download folder for `.dtresume` files and re-queues incomplete jobs

### 1.7 Local Control API (`dlmgr/control_api.py`)

Loopback HTTP server on `127.0.0.1:53742` using `http.server` + `threading`:

| Method | Path | Body | Purpose |
|--------|------|------|---------|
| POST | `/api/jobs` | `{url, filename, headers, cookies, referrer, type}` | Submit new download job |
| GET | `/api/jobs` | — | List all jobs with status |
| GET | `/api/jobs/{id}` | — | Get single job status (segments, speed, ETA) |
| POST | `/api/jobs/{id}/pause` | — | Pause job |
| POST | `/api/jobs/{id}/resume` | — | Resume job |
| POST | `/api/jobs/{id}/cancel` | — | Cancel + delete partial file |
| DELETE | `/api/jobs/{id}` | — | Remove from list |
| GET | `/api/health` | — | Health check (used by extension) |

- Binds to loopback only (security)
- JSON request/response
- Runs in a daemon thread

### 1.8 Downloads Tab UI (`gui/downloads_tab.py`)

Widget structure:
```
QVBoxLayout
├── Toolbar (QHBoxLayout)
│   ├── "+ Add URL" button
│   ├── "Pause" / "Resume" / "Cancel" / "Retry" buttons
│   ├── "Open File" / "Open Folder" buttons
│   └── "Clear Completed" button
├── Downloads table (QTableWidget)
│   Columns: Filename | Progress (QProgressBar) | Speed | ETA | Status | Size | Source URL
│   Right-click context menu: Pause/Resume/Cancel/Open/Open Folder/Copy URL
├── Details panel (QGroupBox, collapsible)
│   ├── Active segments count
│   ├── Per-segment progress (mini progress bars or a list)
│   └── Connection count
└── Settings sub-panel (QGroupBox, collapsible)
    ├── Max concurrent downloads (QSpinBox)
    ├── Max connections per download (QSpinBox 1–32)
    ├── Default download folder (QLineEdit + Browse)
    ├── Bandwidth limit (QSpinBox, 0 = unlimited)
    ├── Auto-start (QCheckBox)
    └── Category folders (QTableWidget: category | folder | extensions)
```

- 1-second QTimer refreshes the table from the engine's job list
- Progress bar column uses a delegate for smooth rendering
- Same dark theme as the rest of the app

### 1.9 Integration into MainWindow (`gui/main_window.py`)

- Import `DownloadsTab` from `gui/downloads_tab`
- Add `self.downloads_tab = DownloadsTab(...)` 
- `self.main_tabs.addTab(self.downloads_tab, "Downloads")` — inserted after "Torrents" tab (index 2)
- Tab order becomes: Agent | Torrents | Downloads | Search | Browser
- Start the `ControlAPI` server in `MainWindow.__init__` (daemon thread)
- Start the `DownloadEngine` in `MainWindow.__init__`

### 1.10 Settings Dialog (`gui/settings_dialog.py`)

Add a "Downloads" group box:
- Max concurrent downloads
- Max connections per download (1–32)
- Default download folder
- Bandwidth limit
- Auto-start toggle
- Category folder configuration table

---

## Phase 2 — HLS/DASH Stream Capture Pipeline

### 2.1 Manifest Parser (`dlmgr/hls_dash.py`)

**HLS (`.m3u8`)**:
- Parse with `m3u8` library (add to requirements)
- Enumerate `#EXT-X-STREAM-INF` renditions (bandwidth, resolution)
- Parse segment list (`.ts` files) from chosen rendition
- Detect `#EXT-X-KEY` tags → extract key URI, IV, method (AES-128)
- Detect `#EXT-X-MAP` for init segments (fMP4)
- Live stream detection: `#EXT-X-PLAYLIST-TYPE:EVENT` or no `#EXT-X-ENDLIST`

**DASH (`.mpd`)**:
- Parse XML with `xml.etree.ElementTree`
- Extract `Representation` list (codecs, bandwidth, height)
- Extract `SegmentTemplate` / `SegmentList` / `SegmentBase`
- Build segment URL list from template + timeline

### 2.2 Segment Downloader

- Reuse the segmented engine for parallel segment downloads
- Each segment is a separate "mini-job" with its own range
- Segments written to temp directory: `{job_id}/seg_00001.ts`
- AES-128 decryption: fetch key from key URI, decrypt each segment with `pycryptodome` AES-128-CBC
- DRM detection: check for `#EXT-X-KEY:METHOD=SAMPLE-AES` or Widevine PSSH boxes → refuse with "protected content" message

### 2.3 FFmpeg Remux (`dlmgr/ffmpeg.py`)

- Locate FFmpeg: `config.download.ffmpeg_path` or bundled `ffmpeg.exe` in app directory
- Remux: `ffmpeg -i "concat:seg_00001.ts|seg_00002.ts|..." -c copy output.mp4`
- For fMP4/DASH: `ffmpeg -i init.mp4 -i "concat:seg_00001.m4s|..." -c copy output.mp4`
- Optional transcode: `ffmpeg -i input -c:v libx264 -c:a aac output.mp4`
- Live capture: append segments as they arrive, final remux on stop
- Progress parsing from FFmpeg stderr

### 2.4 Bundling FFmpeg

- Download a static FFmpeg Windows build (from https://github.com/BtbN/FFmpeg-Builds)
- Place `ffmpeg.exe` in `packaging/ffmpeg/ffmpeg.exe`
- `app.spec`: add `(os.path.join(project_root, "packaging", "ffmpeg", "ffmpeg.exe"), "ffmpeg")` to `datas`
- `installer.iss`: the `[Files]` section already uses `recursesubdirs` so it'll be included
- At runtime: `ffmpeg_path = config.download.ffmpeg_path or os.path.join(sys._MEIPASS, "ffmpeg", "ffmpeg.exe")` (or alongside exe for onedir)

---

## Phase 3 — Chrome Extension + Native Messaging

### 3.1 Native Messaging Host (`native_messaging/host.py`)

- DeepFlux.exe launched with `--native-messaging` flag enters stdio host mode
- Reads 4-byte little-endian length prefix from stdin, then that many bytes of JSON
- Writes responses the same way (4-byte length + JSON)
- Forwards messages to the Local Control API via HTTP to `127.0.0.1:53742`
- Streams job status updates back to the extension (persistent port)
- `main.py` modification: if `--native-messaging` arg is present, run `native_messaging.host.run()` instead of GUI

### 3.2 Native Messaging Registration (`native_messaging/register.py`)

Windows registry key:
```
HKCU\SOFTWARE\Google\Chrome\NativeMessagingHosts\com.deeptorrent.integration
  (Default) = "C:\Users\{user}\AppData\Roaming\Deeptorrent\native_messaging_host.json"
```

Host manifest JSON (`native_messaging_host.json`):
```json
{
  "name": "com.deeptorrent.integration",
  "description": "DeepFlux Integration Module",
  "path": "C:\\Program Files\\DeepFlux\\Deeptorrent2.exe",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://{EXTENSION_ID}/"]
}
```

- Registration runs during app startup (or installer post-step)
- The extension ID is determined after publishing; for development, use a wildcard or the temporary extension ID
- `installer.iss` `[Run]` section: call `Deeptorrent2.exe --register-native-host` after install

### 3.3 Chrome Extension — `manifest.json` (Manifest V3)

```json
{
  "manifest_version": 3,
  "name": "DeepFlux Integration Module",
  "version": "1.0.0",
  "description": "Detect and download files and videos with DeepFlux",
  "permissions": ["nativeMessaging", "downloads", "contextMenus", "cookies", "storage", "activeTab", "webRequest"],
  "host_permissions": ["<all_urls>"],
  "background": { "service_worker": "background.js" },
  "content_scripts": [{ "matches": ["<all_urls>"], "js": ["content.js"], "css": ["overlay.css"], "run_at": "document_idle" }],
  "action": { "default_popup": "popup.html", "default_icon": {...} },
  "options_page": "options.html",
  "icons": { "16": "icons/16.png", "48": "icons/48.png", "128": "icons/128.png" }
}
```

### 3.4 Background Service Worker (`background.js`)

- `chrome.runtime.connectNative("com.deeptorrent.integration")` — persistent port
- `chrome.downloads.onDeterminingFilename` — intercept downloads above size threshold; cancel native download; send URL + cookies to DeepFlux
- `chrome.webRequest.onHeadersReceived` (observational, non-blocking) — detect `Content-Type: video/*`, `application/vnd.apple.mpegurl`, `application/dash+xml`, `Content-Disposition: attachment`; tag with tab ID, referrer, cookies
- `chrome.contextMenus.create` — "Download with DeepFlux" on links and media
- Badge text: active download count (updated from native messaging status updates)
- Message relay between content scripts and native messaging port

### 3.5 Content Script (`content.js`)

- Scan for `<video>`, `<source>`, `<audio>` elements → extract `src`
- Detect JW Player (`jwplayer().getPlaylistItem().file`), Video.js (`videojs.players`), Shaka Player (`shaka.Player.getManifestUri()`)
- Inject floating "Download Video" button overlay near detected video players
- On click, send `{type: "download_video", url, pageUrl, cookies}` to background script
- MutationObserver to catch dynamically loaded video elements

### 3.6 Popup (`popup.html` / `popup.js`)

- Query background script for detected media on current tab
- Display list: thumbnail/icon, filename, quality, type (file/HLS/DASH)
- "Download" button per item → sends to native host
- "Open DeepFlux" link → opens the app (via native messaging or `window.open`)
- Active downloads summary at bottom

### 3.7 Options Page (`options.html` / `options.js`)

- Toggle auto-capture on/off (global)
- Per-site allow-list / block-list
- Size threshold for intercepting native downloads
- Enable/disable video overlay button
- Persisted via `chrome.storage.sync`

---

## Phase 4 — Packaging & Installer Updates

### 4.1 `packaging/app.spec`

- Add `dlmgr`, `dlmgr.extractors`, `native_messaging` to `hiddenimports`
- Add `m3u8`, `Crypto` (pycryptodome) to `hiddenimports`
- Add `chrome_extension/` directory to `datas` (so it can be found/installed)
- Add `packaging/ffmpeg/ffmpeg.exe` to `datas`
- Add `packaging/native_messaging_host.json` to `datas`

### 4.2 `packaging/installer.iss`

- `[Files]`: already recursive, will pick up ffmpeg and extension files
- `[Run]`: add `Deeptorrent2.exe --register-native-host` (registers native messaging host in registry)
- `[UninstallDelete]`: remove native messaging registry key on uninstall
- Installer size increases by ~80MB (FFmpeg static build)

### 4.3 `requirements.txt`

Add:
```
m3u8>=3.0.0
pycryptodome>=3.19.0
```

---

## Phase 5 — Extractor Plugin System (`dlmgr/extractors/`)

- `ExtractorRegistry` class: loads all `.py` files from `~/.deeptorrent/extractors/` at startup
- Each extractor: `name`, `can_handle(url)`, `extract(url, headers, cookies) → {manifest_url, type, title}`
- `generic.py`: handles any URL with `.m3u8` or `.mpd` extension, or `Content-Type` sniffing
- Hot-reload: file watcher on the extractors directory; reload on change without app restart
- Site-specific extractors added later as needed (YouTube, Vimeo, etc.) — each in its own file

---

## Implementation Order (Recommended)

| Step | Component | Effort | Dependencies |
|------|-----------|--------|--------------|
| 1 | `config.py` — `DownloadConfig` dataclass | Small | None |
| 2 | `dlmgr/job.py` — Job model + state machine | Small | None |
| 3 | `dlmgr/segment.py` — Segment worker | Medium | job.py |
| 4 | `dlmgr/resume_state.py` — Resume persistence | Medium | job.py |
| 5 | `dlmgr/engine.py` — Segmented engine + dynamic rebalancing | Large | segment.py, resume_state.py |
| 6 | `dlmgr/scheduler.py` — Priority queue + bandwidth | Medium | engine.py |
| 7 | `dlmgr/control_api.py` — Loopback HTTP API | Medium | engine.py |
| 8 | `gui/downloads_tab.py` — Downloads tab UI | Large | control_api.py |
| 9 | `gui/main_window.py` — Wire in Downloads tab | Small | downloads_tab.py |
| 10 | `gui/settings_dialog.py` — Downloads settings | Small | config.py |
| 11 | Build + test Phase 1 end-to-end | Medium | Steps 1–10 |
| 12 | `dlmgr/hls_dash.py` — Manifest parser + segment downloader | Large | engine.py |
| 13 | `dlmgr/ffmpeg.py` — FFmpeg wrapper | Medium | hls_dash.py |
| 14 | Bundle FFmpeg in `app.spec` + `installer.iss` | Small | ffmpeg.py |
| 15 | `dlmgr/extractors/` — Plugin system | Medium | hls_dash.py |
| 16 | Build + test Phase 2 end-to-end | Medium | Steps 12–15 |
| 17 | `native_messaging/host.py` — stdio protocol | Medium | control_api.py |
| 18 | `native_messaging/register.py` — Registry registration | Small | host.py |
| 19 | `main.py` — `--native-messaging` + `--register-native-host` flags | Small | host.py, register.py |
| 20 | `chrome_extension/` — Full extension | Large | native_messaging |
| 21 | `packaging/app.spec` + `installer.iss` — Bundle everything | Medium | All |
| 22 | Build + test Phase 3 end-to-end | Large | All |

---

## Key Design Decisions

1. **DeepFlux.exe as native messaging host**: The main executable handles stdio when launched with `--native-messaging`. No separate host exe needed. Simpler packaging, matches the user's preference.

2. **Loopback HTTP as internal API**: The control API on `127.0.0.1:53742` serves both the in-app UI and the native messaging bridge. Clean separation — the native messaging host is just a thin stdio-to-HTTP proxy.

3. **FFmpeg bundled**: Static FFmpeg build included in the installer (~80MB increase). Located at runtime via config or alongside the exe.

4. **Resume files alongside downloads**: `.dtresume` files in the download folder. On engine startup, scan for incomplete jobs and re-queue them.

5. **Dynamic segment rebalancing**: Rather than fixed chunks, the engine monitors per-segment throughput every 2 seconds. Stalled segments donate their remaining range to faster workers. This is the key IDM-like acceleration feature.

6. **Extractor hot-reload**: Site-specific extractors loaded from `~/.deeptorrent/extractors/` at startup, with file-watching for hot-reload. No reinstall needed to update site logic.

7. **DRM detection, not bypass**: HLS streams with `SAMPLE-AES` or PSSH/Widevine boxes are detected and refused with a clear message. No DRM circumvention.

8. **Tab order**: Agent | Torrents | Downloads | Search | Browser. Downloads tab inserted at index 2 (after Torrents).

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Chrome Web Store review may reject extension | Keep heavy logic in native app; extension is thin. Use observational webRequest only. |
| FFmpeg bundle size (~80MB) | Use a minimal FFmpeg build (just demuxers/muxers for mp4/ts). Or offer as optional download. |
| Native messaging host path changes | Registration runs on every app startup, not just install. Self-healing. |
| Site-specific extractors break | Hot-reload system + generic manifest sniffer as fallback |
| Segment write corruption | Pre-allocate file; each segment writes to its own offset; no merge step needed |
| Port 53742 conflict | Configurable port; fallback to next available |
