# Boot & shutdown latency — open-from-video + close-hang study

Date: 2026-11-17 · Scope: why closing the app "hangs for a second or two",
why opening **from a video** is noticeably slower than a plain launch, and
what to change — with measured evidence for every claim.

> STATUS: **IMPLEMENTED same day (2026-11-17)** — P0 (interruptible monitor
> wait + serve_forever poll_interval), P1 (media-before-control-API timer
> order, idle mpv pre-warm, "Opening…" overlay), P2 (close resume export
> timeout=3 s, forward probe covers the configured control port), plus the
> owner's defaults refresh (micro font, dark_blue theme, max_concurrent 1 —
> ui_defaults_gen 2→3 once-migration). Re-measured dev-tree after the fix:
> `DownloadEngine.stop()` 995 ms median → **0.1 ms**; mini close path
> 1989 ms → **51 ms**; full closeEvent 1002 ms → **56 ms**. New regression
> tests: `test_dlmgr.py::test_stop_is_immediate_mid_monitor_sleep`,
> `test_mini_player.py` (overlay/prewarm/forward-probe/resume-timeout pins),
> `test_font_scale.py::test_config_defaults_gen3_migrate_once`. Frozen
> rebuild + live numbers happen at release time. Harnesses stay in
> `_boot_study/` (scratch, untracked): `dev_attrib.py` (per-step close
> attribution, dev tree), `dev_firstframe.py` (backend-stack costs),
> `frozen_e2e.ps1` (end-to-end frozen timings: window-visible /
> first-video-frame / WM_CLOSE→window-gone / WM_CLOSE→process-exit,
> isolated temp profile).

## 1. Problem (owner report 2026-11-17)

1. **Close**: the window freezes for ~1–2 s before it disappears.
2. **Open**: plain launch feels fast; double-clicking a video file
   (association open → mini player) does not.

## 2. Executive summary — one line each

* **Close hang = ONE join.** `DownloadEngine.stop()` must wait out the
  monitor loop's bare `time.sleep(2.0)`
  (`dlmgr/engine.py:1169`, join timeout `REBALANCE_INTERVAL + 1.0` =
  3.0 s at `:174`). Measured idle-stop latency: **min 140 / median 995 /
  max 1850 ms** — pure dead wait, on EVERY quit path (mini-only AND full
  app). 98 % of a measured full-app `closeEvent` (980 of 1002 ms).
* **Open-from-video = +0.7 s after the window shows** (frozen 8.2, warm:
  window ~1.03 s, first frame +~0.72 s ⇒ ≈1.75 s total), spent behind a
  BLACK surface: `start_control_api` is queued ahead of `open_media` on
  the GUI thread, then libmpv DLL load + `vo=gpu-next`/D3D/hwdec init +
  first demux/present. A second double-click while the app runs adds a
  ~0.6 s frozen-exe floor (bootloader+interpreter) just to POST one line.
* Everything else in both paths is already cheap (config writes ~2 ms,
  resume export ~0.1 ms idle, iptv/room/browser teardown <10 ms each,
  process-exit tail after window-gone only ~70–210 ms).

## 3. Measured evidence

### 3.1 Dev attribution (`_boot_study/dev_attrib.py`, offscreen, isolated profile)

| What | Cost |
|---|---|
| `DownloadEngine.stop()`, idle, 10× | **min 140 / median 995 / mean 995 / max 1850 ms** |
| `ControlAPI.stop()` standalone | 208 ms (`serve_forever` 0.5 s poll interval) |
| `MiniPlayerWindow()` construction | 19.3 ms |
| mini `start_control_api` (engine+api up, empty folders) | 9.5 ms |
| mini `_save_prefs` (config write) | 3.0 ms |
| mini `_stop_control_api` (api.stop + engine.stop) | **1989.5 ms** |
| `MainWindow()` construction (offscreen) | 1214 ms |
| **full `closeEvent` TOTAL** | **1002 ms**, accepted |
| └ `dl_engine.stop` | **980.6 ms (98 %)** |
| └ `control_api.stop` | 7.7 ms |
| └ `iptv_tab.shutdown` | 5.9 ms |
| └ `config.to_file` ×3 (persist browser session + UI state + …) | 1.6–1.8 ms each |
| └ `agent.cancel` / `save_torrent_state` / `engine.stop` (torrent, idle) / `local_model.stop_server` / `engine.export_resume_data` (idle) / watchdog / telemetry / room / tools | ≤ 1.4 ms each |

Notes: `engine.export_resume_data` is ~0.1 ms with zero torrents but its
default `timeout=15.0` (`engine/torrent_engine.py:273`) is a latent
worst-case hang with many active torrents (alerts that never arrive — e.g.
a handle invalidated mid-save — hold the loop until the deadline).
`telemetry.stop_heartbeat` is non-blocking by design (daemon thread).
Watchdog uses `_stop_watchdog.wait()` — already interruptible.

### 3.2 Frozen 8.2 end-to-end (`_boot_study/frozen_e2e.ps1`, warm, isolated temp profile + custom control port; owner's live instance untouched)

| Scenario | window visible | first video frame | WM_CLOSE → window gone | WM_CLOSE → process exit |
|---|---|---|---|---|
| plain boot → close (mini idle) ×4 | 1002–1038 ms | — | **2108–2133 ms** | 2183–2224 ms |
| video boot → play → close ×3 | 1016–1048 ms | **+713–743 ms after window** (≈1.75 s total) | 1368–1395 ms | 1532–1542 ms |
| full app (local html target) | 2078 ms | — | 710 ms | 919 ms |
| `--help` (second-instance floor, 4×) | 601–651 ms to exit | — | — | — |

**The close numbers triangulate the join model exactly.** The monitor
thread sleeps 2.0 s from engine start; each scenario closes at a
different offset into that sleep, and every measurement fits
`~100 ms fixed + remaining sleep`:

* plain: engine starts at window-show, closed immediately ⇒ waits ~2.0 s ⇒ 2.11 s
* video: closed ~0.7 s after engine start (first frame) ⇒ waits ~1.3 s ⇒ 1.38 s
* full app: engine started ~1.35 s before close ⇒ waits ~0.6 s ⇒ 0.71 s

Real-world closes happen minutes after boot ⇒ the wait is uniform 0–2 s,
mean ≈ 1.2 s — precisely "hangs for a second or two". The window stays
frozen-visible the whole time because all of this runs inside
`closeEvent` (mini: `_stop_control_api`; full: `dl_engine.stop`) before
`event.accept()`.

### 3.3 The video-open delta, decomposed

Frozen path after the window paints (all on the GUI thread, behind a
black video surface):

1. `QTimer.singleShot(0, start_control_api)` runs FIRST (queued at
   `gui/mini_player.py:899` before the media timer at `:903`):
   `DownloadEngine.start()` → `_load_folder_registry()` +
   `scan_for_incomplete()` (top-level `os.listdir` per known download
   folder + `.dtresume` loads) + possible auto-resume of interrupted
   downloads. ~10 ms with empty folders; scales with folder sizes and
   can re-ignite network workers — before the video even opens.
2. `import iptv.player` (21 ms dev) + `ensure_mpv_dll_on_path` + libmpv
   import (21.5 ms dev warm; frozen pays Defender-scanned DLL loads).
3. `mpv.MPV(wid=…, vo=gpu-next, hwdec=auto-safe)` ctor: 6.3 ms dev — but
   the frozen/native cost of `vo=gpu-next` (D3D11 device + libplacebo) +
   hwdec probe is paid at first render, not visible offscreen (dev probe
   cannot render; its total stack is ~50 ms vs +720 ms frozen).
4. `loadfile` → demux → first frame present. (Test clip has no audio
   track, so WASAPI init is NOT in the 720 ms — real videos with audio
   may add a little more.)

The second-double-click path (app already running): the new frozen exe
pays the ~600 ms bootloader+interpreter floor (`--help` proxy) before it
can even probe the port, POSTs, and exits; then the running instance
pays items 2–4 again for the new file. Forwarding itself is correct —
except see finding F6.

## 4. Findings (ranked)

* **F1 (HIGH, close):** monitor-loop `time.sleep(2.0)` makes
  `DownloadEngine.stop()` block 0–2 s on every quit
  (`dlmgr/engine.py:1169`, join `:173-174`). *The* close hang.
* **F2 (MED, close):** `ControlAPI.stop()` waits out
  `serve_forever`'s 0.5 s poll interval (~0.2–0.5 s per quit;
  `dlmgr/control_api.py:376,383`).
* **F3 (MED, open):** `run_mini_player` queues `start_control_api`
  before `open_media` (`gui/mini_player.py:899-903`) — disk scans and
  potential job-resume work sit ahead of the video open on the GUI
  thread.
* **F4 (MED, open):** the video open pays the full backend cold-start
  (DLL + `vo=gpu-next` + hwdec) behind a black surface with zero
  feedback; nothing is pre-warmed at idle boot even though the idle
  banner is on screen for however long the user looks at it.
* **F5 (LOW, close):** `export_resume_data()` default 15 s timeout in
  `closeEvent` (`gui/main_window.py:7706`) is a latent multi-second hang
  with many torrents (vs the 5 s used by the background saver at
  `:7624`); idle cost today is nil.
* **F6 (LOW, correctness):** `_forward_to_running_instance` probes only
  the hardcoded 53742–53751 range (`main.py:406`) — a user-configured
  `download.control_api_port` outside it silently loses forwarding and
  boots a second instance (observed live during this study's harness).
* **F7 (INFO):** everything else is already fast: three config writes
  ~2 ms each, iptv/room/browser teardown <10 ms, telemetry leave
  non-blocking, watchdog interruptible, process-exit tail ~70–210 ms.
* **F8 (INFO):** full-app window ~2.1 s warm is the known
  FAST_VIDEO_OPEN_STUDY C3/C4 territory (lazy Browser/IPTV tabs) — real,
  but not today's complaint and not touched here.

## 5. Recommendations

### P0 — kill the close hang (tiny, contained, huge win)

1. **Interruptible monitor wait** (`dlmgr/engine.py`): replace
   `time.sleep(REBALANCE_INTERVAL)` in `_monitor_loop` with
   `self._wake.wait(REBALANCE_INTERVAL)` on a `threading.Event` created
   in `__init__`; `stop()` sets it right after `self._running = False`.
   Expected: every close path drops from 0.1–2.2 s to **~100–300 ms**
   (fixed remainder: api.stop + config writes + mpv teardown).
   Risk: none — semantics identical; the loop still re-checks
   `self._running` each pass. Regression test: start engine, sleep 0.3 s,
   `stop()` must return < 200 ms (today it can take ~1.7 s).
2. **ControlAPI poll interval**: `serve_forever(poll_interval=0.05)`
   (`dlmgr/control_api.py`) → `api.stop()` ~200–450 ms → ~50 ms.

### P1 — open-from-video

3. **Reorder the boot timers** (`gui/mini_player.py:899-903`): open the
   media FIRST; move `start_control_api` behind it (it exists only for
   SECOND-instance forwarding — 200–500 ms later costs nothing). Also
   consider deferring the folder scan inside `DownloadEngine.start()`
   (background thread) so the dormant mini engine never blocks anything.
4. **Pre-warm the playback stack at idle boot**: on a background thread
   right after the idle window paints, `ensure_mpv_dll_on_path()` +
   `import mpv` (warms the Defender-scanned DLL loads), and optionally
   pre-create the backend against the video surface (ctor is ~6 ms dev;
   keeps `vo`/D3D context ready) so a double-click pays only
   `loadfile`+demux. Spike-measure the frozen gain first; if
   pre-creating the backend is risky in any way, ship the DLL warm-up
   alone (pure win, no behavior change).
5. **Perceptual**: paint "Opening <name>…" (small label over the video
   page) while the backend warms — the remaining few hundred ms should
   look deliberate.

### P2 — secondary / latent

6. `closeEvent`: pass an explicit short timeout to
   `export_resume_data` (e.g. 3 s, matching its real idle cost with
   margin) instead of the 15 s default.
7. `_forward_to_running_instance`: probe the configured
   `control_api_port` first, then the default range (F6).
8. (Optional, cosmetic) collapse the three `config.to_file` writes in
   `closeEvent` into one — saves ~4 ms, only worth it if touching the
   code anyway.
9. Full-app boot diet (lazy Browser/IPTV tabs) remains the known path to
   shrinking the 2.1 s full-app window — separate effort
   (FAST_VIDEO_OPEN_STUDY C3/C4).

### Expected results after P0+P1 (frozen, warm)

* Close (any path): **≤ ~0.3 s** to window-gone (from 0.7–2.2 s).
* Open from video, cold process: **~1.1–1.3 s** to first frame (from
  ~1.75 s), with visible feedback instead of black.
* Open from video, app already running: second exe floor (~0.6 s) is
  PyInstaller-onedir reality; first instance's open drops by the same
  pre-warm gain.

## 6. What was deliberately NOT touched

* Graceful-shutdown semantics (fast-resume save, config persistence,
  active-transfer prompt) — P0 removes dead wait only, never a save.
* The stopped-playback morph contract, the gc freeze ritual, the
  faulthandler rules — all pinned by tests and unaffected.
* The owner's live instance/config: all measurements ran under isolated
  temp profiles (`USERPROFILE`/`LOCALAPPDATA` + custom control port +
  telemetry/updater off); the one stray harness instance was killed and
  the owner's process left running.

## 7. Reproducing the numbers

```
.venv/Scripts/python.exe _boot_study/dev_attrib.py        # close attribution
.venv/Scripts/python.exe _boot_study/dev_firstframe.py    # backend stack costs
powershell -NoProfile -ExecutionPolicy Bypass -File _boot_study/frozen_e2e.ps1 -Scenario all
```

Test clip: `_boot_study/test_video.mp4` (ffmpeg `testsrc2`, 1280×720,
no audio — deliberately, so the frame delta excludes audio-device init).
