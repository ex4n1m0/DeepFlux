# Fast Video Open — association-launch speed + compact mode study

Date: 2026-11-10 (rev 2 — owner feedback folded in: two-way morph buttons,
stopped-playback contract, progress-bar transformation) · Scope: what happens
today when Windows launches DeepFlux via file association on a local
video/audio file, why it feels "super heavy", and how to make it open fast
directly in a compact player that can transform into the full app.

## 1. Problem

Double-clicking a video file that opens with DeepFlux (ProgID
`DeepFlux.Media`, or "Open With") boots the ENTIRE application — torrent
engine, agent stack, Chromium browser, IPTV manager, IRC room, five heavy
tabs, six background workers — and then, after a fixed 500 ms timer, switches
to the Play tab and starts the file. The user wanted to watch a video; they
waited for a small operating system's worth of startup first.

## 2. The current path, step by step

Association registration (`infra/file_associations.py:65` `_launch_command`)
runs `"DeepFlux.exe" "%1"` — the file path arrives as a bare positional
argument. Then:

1. **main.py module imports** (`main.py:12-16`) — `agent.loop`,
   `agent.tools`, `config`, `engine` (libtorrent), `gui` → `gui.main_window`
   → full PySide6 (Widgets + **QtWebEngine** + Svg), requests/urllib3,
   dlmgr/curl_cffi, every gui module. These run BEFORE argument parsing, so
   every mode pays them: `--register-associations`, the
   forward-to-running-instance probe, and `--help`.
2. **Single-instance forward** (`main.py:553` → `_forward_to_running_instance`,
   `main.py:366`) — if a full app is already running, the target is POSTed to
   its control API and this process exits. (Fast in principle — but it still
   pays the full import cost from step 1 before it can even probe.)
3. **Cold launch → `run_gui(open_targets=[path])`** (`gui/main_window.py:7604`):
   deepflux:// scheme registration, `QApplication`, config `from_file`,
   fonts, i18n — then **`MainWindow(config_path)`** builds everything
   eagerly:
   - synchronous: `TorrentEngine` + `.start()` (libtorrent session),
     `DownloadEngine` + `.start()`, `ControlAPI` HTTP server, `ToolRegistry`,
     `AgentLoop`, `RSSMonitor` — measured together ≈ **2 ms** (not the
     problem);
   - workers spawned (non-blocking, but part of boot noise): Jackett sync +
     hourly timer, yt-dlp probe, local-model start, updater check, GPU
     preference write, telemetry heartbeat;
   - tab construction: **Browser tab = `QWebEngineProfile("deeptorrent")`
     with on-disk storage + `BrowserHistory` sqlite + eager
     `QWebEngineView` + extension injection + AdBlock init** (the bulk of
     construction — see numbers), **IPTVTab ≈ 197 ms** (`IPTVManager` with
     8 metadata providers, EPG timer, then `load_all_async()` fires network
     loads for every enabled playlist source), **CommanderTab ≈ 94 ms**,
     Downloads/Agent/Room/torrents panels, menu chrome.
   - `gc.collect()` + `gc.freeze()`, `window.show()`.
4. **`QTimer.singleShot(500, open_target)`** (`gui/main_window.py:7652`) — a
   FIXED half-second delay before the association target is even dispatched.
5. `open_target` (`gui/main_window.py:5839`) → video/audio branch →
   `play_file_in_player` (`:6541`) → IPTVTab `play_file` (`gui/iptv_tab.py:4937`)
   → `PlayerWidget.play` → `_ensure_backend` → `create_backend` (libmpv load
   happens here, lazily — the only cheap part of the chain) → play.
6. Meanwhile the first event-loop spins initialize the Chromium subprocess /
   GPU (~1 s dev; frozen log shows GPU init ~2.2 s after launch).

## 3. Measured cost centers (this machine, 2026-11-10)

Dev venv (Python 3.11.9), offscreen QPA, warm page cache — absolute numbers
are warm-cache floors; ratios are what matter. Frozen = `dist/DeepFlux/`
onedir build.

| What | Cost |
|---|---|
| `import gui` (= gui.main_window, the whole app chain) | **~670 ms** dev / **~850 ms** frozen (`--help` timing, warm) |
| └ of which requests/urllib3 | ~160 ms |
| └ dlmgr → curl_cffi | ~100 ms |
| └ PySide6 core/widgets | ~80 ms |
| └ agent (+tools) | ~85 ms |
| └ gui.iptv_tab | ~57 ms |
| `MainWindow()` construction, total | **924 ms** |
| └ IPTVTab | 197 ms |
| └ CommanderTab | 94 ms |
| └ DownloadsTab | 16 ms |
| └ engines + ControlAPI + ToolRegistry + AgentLoop | ~2 ms |
| └ remainder ≈ Browser tab (QWebEngineProfile + views + adblock + history) + Agent panel + Room + chrome | **~615 ms** |
| First event-loop spin (Chromium profile/GPU init) | ~1,000 ms |
| Fixed `QTimer.singleShot(500, …)` before dispatch | 500 ms |
| **Frozen exe end-to-end** (isolated profile, offscreen, control-API health probe) | **~1.3 s** to engines-up; Chromium still initializing at ~2.2 s; window ready ≈ 2.5 s warm |
| **Composed total, video actually starts** | **≈ 3.1 s dev warm / ≈ 3–3.5 s frozen warm; cold disk + AV scan realistically 4–8 s** |
| **MINIMAL path: python + QApplication + `iptv.player` + `create_backend` + `play()`** (measured with real mpv playback of a system WAV) | **215 ms dev warm** — est. ~0.5–0.8 s frozen |

The minimal-path harness (`iptv/player.py` is deliberately Qt-light: stdlib
imports only, mpv/vlc imported inside the backends, DLL discovery bundled)
proves the playback stack itself costs ~35 ms after Qt. **Everything else the
user waits for is irrelevant to playing a local file.**

## 4. Compact mode today (what exists)

`PlayerWidget._toggle_compact` (`gui/iptv_tab.py:1400`), Windows-only
(`compact_mode_supported`, `:1309`):

- saves window geometry, `SetWindowPos(HWND_TOPMOST)` on the WHOLE
  MainWindow (no reparenting — the mpv/VLC embed stays tied to its native
  winId; that's why true PiP is avoided),
- `setMinimumSize(480, 320)` + `resize(640, 420)`,
- hides secondary transport controls (`_set_compact_controls`, `:1342`),
- `sig_compact` → IPTVTab `_on_player_compact` → `_on_player_fullscreen(True)`
  (`:4374`): hides toolbar, section chips, sidebar, content pane, status row,
  main menu bar — video + mini transport only.

So "compact" is a MODE of the fully-constructed window. It cannot make the
association open faster by itself — but it is exactly the look a fast path
should produce, and its no-reparent invariant is the design rule any new
window must follow.

## 5. Options

### Option A — Mini-player fast path (RECOMMENDED)

A tiny, Qt-light player window for the association case; the full app is
never built unless asked for.

**Trigger** (in a restructured `main.py`, see C1): all of —
- exactly one `target`, no other flags;
- existing local file with a media extension (reuse
  `file_associations.VIDEO_EXTS + AUDIO_EXTS`);
- not forwarded to a running instance (existing control-API probe).

**Window** (`gui/mini_player.py`, new): QWidget video surface +
`iptv.player.create_backend` (in-process mpv, VLC fallback — both already
lazy and DLL-bundled). Minimal auto-hiding transport: play/pause, seek,
volume/mute, fullscreen, close. Compact by construction: opens at ~640×420
(remembered geometry), topmost optional (default off — owner call), Space/F/Esc
keys like `PlayerWidget`. Drag-drop another media file to switch. Title =
file name. mpv OSD could substitute for most chrome if we want it even barer.

**Morph buttons — the two-way bridge (owner decision 2026-11-10, rev 2)**.

Contract: the morph is a VIEW switch with a **stopped-playback contract** —
it does not try to keep video playing across the transformation (the owner
explicitly accepted this). Both directions get a dedicated button:

- **Mini → full ("Open full app" ⤢ in the mini transport)**: if anything is
  playing, stop it first, then transform under a **progress bar**:
  1. The mini window immediately swaps its transport for a small overlay:
     phase label + `QProgressBar` (determinate, weighted by the measured
     stage costs in §3).
  2. *Loading modules…* (~30%): the heavy imports (`gui.main_window` chain,
     ~670 ms dev / ~850 ms frozen) run on a WORKER thread — the bar keeps
     animating (imports release the GIL around file/DLL IO; and with
     playback already stopped there is nothing else to starve).
  3. *Building the app…* (~65%): `MainWindow` construction on the GUI
     thread, executed in ~6–8 REAL stages (see below) with
     `app.processEvents()` between stages so the bar ticks stage by stage —
     the "slowly but surely" effect, driven by true milestones, not a fake
     animation. Honest weights from §3: engines ≈ 5%, Agent/tools ≈ 5%,
     Downloads tab ≈ 2%, IPTV tab ≈ 20%, Browser/Chromium ≈ 45%,
     Command/Room/chrome ≈ 15%, finish ≈ 8%.
  4. *Almost there…*: workers spawn, `gc.collect()` + `gc.freeze()` (the
     freeze moves to AFTER the last stage — it must never pin the
     half-built graph).
  5. Mini window hides; `MainWindow.show()` appears. The mini window object
     stays alive (hidden, ~free) so the reverse morph is instant.
  Staged construction requires splitting today's monolithic
  `MainWindow.__init__` into named stage functions — a contained refactor
  with a big side benefit: the NORMAL boot path can reuse the same staged
  driver later (Option C groundwork for free). Fallback if a stage refuses
  to be sliced (e.g. deep attribute coupling): run construction in 2 chunks
  (before-tabs / after-tabs) and let the bar jump — still honest.
- **Full → mini ("◱ Mini player" button, Play-tab toolbar next to the
  existing Compact button; also a menu entry)**: stops playback if any,
  then `MainWindow.hide()` + show the mini window (created lazily on first
  use if the session booted straight to the full app — the module is
  Qt-light). **The full app keeps RUNNING hidden**: torrent/download
  engines, agent, RSS keep working — downloads continuing while the user
  watches in the mini player is a feature, not a leak (engines cost ~2 ms
  to create and near-zero idle; the RAM delta of a hidden window is the
  price of resumable state). Closing the mini window in this state runs
  MainWindow's REAL shutdown path (fast-resume + config save — never a bare
  `app.quit()`), then quits. Closing mini in a mini-only session (full app
  never built) is a plain quit.

**Compact vs. Mini — not the same feature, both stay**: the existing
`Compact` button keeps video PLAYING and collapses chrome around the
surface inside the full window (win32 topmost). The new Mini button STOPS
playback and switches the whole view. Tooltips should say so.

**Second instance while mini is running**: `_forward_to_running_instance`
needs a listener. Start the `ControlAPI` in mini mode too (measured ≈ 2 ms
with a `DownloadEngine` — both effectively free) with its open/play handlers
routed at the mini window: another double-click switches the file in the
same mini window. Handoff on morph: mini→full stops mini's ControlAPI just
before MainWindow constructs its own (port 53742 must never double-bind);
full→mini keeps MainWindow's API but RE-ROUTES media opens to the mini
window (magnet/.torrent opens un-hide the full window — they need the
engine UI).

**Platform shape**: Windows + Linux identical (same in-process backends);
macOS keeps today's behavior (no embedded player in that build → full app →
`_reveal_path` fallback, unchanged).

**Config**: `iptv.assoc_open_mode` = `"mini" | "full"` (owner picks the
default; this study exists because "mini" is the wanted behavior) + a
remembered window geometry.

**Expected result**: video in ~0.5–0.8 s frozen (vs 3–6 s), mini session RAM
on the order of 100 MB lighter (QtWebEngine + engines + agent never load).
Morph to full: ~1.6–2.5 s behind a ticking progress bar (perceived as
deliberate, not lag). Morph back: instant. Failure mode is graceful: if
`create_backend` fails (no mpv/VLC — shouldn't happen, DLLs are bundled),
fall through to the normal full-app boot.

**Future upgrade path (evidence gathered, deliberately NOT built)**: the
rev-2 spikes (appendix §8) proved the mpv HWND survives same-window AND
cross-window reparent and that mpv keeps decoding while the GUI thread is
blocked — a seamless playing-through morph is feasible later if the
stopped-playback contract ever feels limiting. The design keeps the door
open (backend destroy/recreate stays confined to one method).

**Risks / notes**:
- New UI surface — keep it transport-only; do not grow it into a second
  Play tab (no EPG/artwork/metadata in mini mode).
- SVP users: mini uses the plain mpv backend (`create_backend(svp=False)`);
  SVP's out-of-process mpv would add find_install + process spawn to the
  fast path. Apply SVP after a morph-to-full plays the file (the full app
  already re-creates backends per item). Optionally a setting later; don't
  pay it at boot.
- Hidden-MainWindow state must never double-run tray/telemetry/single-
  instance — the morph only toggles window visibility, never constructs a
  second MainWindow.
- Tests: an offscreen mini-boot smoke (mirrors `packaging/verify_boot.py`)
  + trigger-detection unit tests (media vs .torrent vs magnet vs html vs
  multiple targets) + morph lifecycle tests (mini→full stages advance the
  bar monotonically and end with a shown MainWindow; full→mini hides it,
  engines alive; quit from mini runs the graceful-shutdown path;
  ControlAPI port handoff).

### Option B — Auto-compact the full app (cheap complement, not a fix)

After `open_target` plays a file, auto-invoke the existing compact toggle
(one line: `iptv_tab._player._toggle_compact()` — or a public wrapper). The
window then LOOKS like the mini player, but the user still waited for the
full 3–6 s boot first. Worth shipping only as a config alongside A
(`assoc_open_mode = "full+compact"`), not as the answer.

### Option C — Boot diet for the full app (complementary; helps every launch)

1. **Restructure `main.py` imports** (prerequisite for A, zero behavior
   change): move `agent/engine/gui` imports into the branches of `main()`;
   parse args with stdlib first. Makes the second-instance forward,
   `--register-associations`, `--setup-*` etc. near-instant.
2. **Kill the fixed 500 ms dispatch delay** (`gui/main_window.py:7652`):
   dispatch `open_target` immediately after `show()` + one `processEvents()`
   (the timer was a "let the window settle" crutch). −500 ms for every
   association open.
3. **Lazy Browser tab**: don't create `QWebEngineProfile`/views/extension/
   adblock until the Browse tab is first activated. Moves ~600 ms of
   construction + ~1 s of Chromium spin off the critical path (paid later,
   on first Browse use). Medium effort — the profile is referenced across
   `main_window` (fs state, devtools, private windows), so it needs a lazy
   accessor.
4. **Lazy remaining tabs**: IPTVTab (197 ms — also defers the
   `load_all_async` network fan-out and EPG timer) and CommanderTab (94 ms)
   built on first visit. Requires placeholder wiring for `AgentIPTVBridge`
   and the `iptv_*` tools (they hold a reference to `iptv_tab`).
5. Worker threads (Jackett, yt-dlp, updater, telemetry) are already async —
   starting them after first paint is polish, not speed.

C1+C2 alone bring a full association open to ≈ 2.5–3 s frozen warm; C1–C4 to
≈ 1.5–2 s — still 2–4× slower than A, because QtWebEngine+IPTV+engine simply
are the app.

## 6. Recommendation

**Ship A (rev 2: mini player + two-way morph with progress-bar
transformation), built on C1+C2.** C2 is a one-line free win for every
launch; C1 is required by A anyway and fixes the forwarding/`--register`
latency as a side effect. B becomes a config choice (`full` mode) rather
than its own feature. C3/C4 are a worthwhile follow-up diet for general
"app feels heavy to open" perception (plain shortcut launch) — and the
staged-construction refactor that A's progress bar needs is most of C4's
groundwork.

Suggested acceptance criteria for A rev 2:
- frozen build, warm: association double-click → first video frame ≤ 1.5 s
  on this dev machine (measured proxy: ≤ 0.8 s to `play()` + decode);
- mini window shows file title, transport works (pause/seek/volume/
  fullscreen/close), drag-drop switches file;
- **mini → full morph**: button stops playback, shows the progress bar,
  bar advances monotonically through the real stages, MainWindow appears
  with the Play tab active; total ≤ 3 s frozen warm;
- **full → mini morph**: button stops playback, main window hides, mini
  window appears instantly; active downloads keep progressing (observable
  via the control API while hidden);
- closing the mini window after a full→mini morph performs the graceful
  shutdown (fast-resume + config written, telemetry leave note) — verified
  by the files it writes, not just exit code;
- second double-click while mini runs switches the file in the same window;
- `.torrent`/magnet/html/multi-target launches take the EXISTING full path,
  byte-for-byte;
- macOS behavior unchanged; `packaging/verify_boot.py` still green; new
  mini-boot smoke + morph lifecycle tests added.

## 7. Evidence appendix (harnesses)

- Dev import timings: per-module `perf_counter` + `-X importtime` on
  `import gui` (breakdown in §3).
- Construction timings: offscreen harness wrapping `TorrentEngine`,
  `DownloadEngine`, `ControlAPI`, `ToolRegistry`, `AgentLoop`, `IPTVTab`,
  `DownloadsTab`, `CommanderTab` `__init__` with timers around a real
  `MainWindow()` build (temp config, conftest-style redirects:
  `default_config_path` monkeypatch, `DF_INDEXER_HEALTH`,
  `DF_JACKETT_DEFINITIONS`, `DF_MODELS_DIR`, `DF_LLAMA_DIR`, `DF_NO_ROOM`).
- Frozen end-to-end: `dist/DeepFlux/DeepFlux.exe` under a temp `USERPROFILE`
  (fully isolated profile + token), offscreen, control-API `/api/health`
  polled with the real bearer token; app log timestamps corroborate
  (engines at +1.3 s, Chromium GPU init at +2.2 s).
- Minimal-path proof: offscreen `QApplication` + `create_backend` +
  real mpv playback of `C:\Windows\Media\Alarm01.wav` → `play()` called at
  **215 ms** from process start, playback confirmed through the 1.5 s event
  loop.

## 8. Evidence appendix — rev-2 morph spikes (2026-11-10)

Run natively (real windows, dev venv, `testsrc2` mp4 generated with the
bundled `dist/DeepFlux/_internal/ffmpeg/ffmpeg.exe`). These de-risk a
FUTURE seamless (playing-through) morph; rev 2 does not build on them —
the stopped-playback contract makes them optional.

1. **mpv decodes while the GUI thread is blocked** — during a hard
   `time.sleep(1.5)` on the GUI thread, playback position advanced exactly
   +1.5 s (twice, before and after reparents). mpv's decode/present run on
   its own native threads; heavy construction on the Qt thread cannot
   stall playback. (This is also why the progress bar needs no special
   tricks once playback is stopped.)
2. **The mpv HWND survives reparenting** — `surface.winId()` stayed
   byte-identical (3015716) through `setParent()` into a new container in
   the SAME window AND into a SECOND top-level window; playback position
   kept advancing, zero error callbacks, no crash. GDI pixel capture
   cannot see mpv's hardware swapchain, so *visual* continuity past the
   reparent was not machine-verifiable — an interactive eyeball gate would
   be required before ever shipping the seamless variant.
