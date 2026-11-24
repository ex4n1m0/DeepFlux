# Android App Development — feasibility & path study

Date: 2026-11-11 · Scope: whether/how to build an Android app for DeepFlux —
what can be reused from this codebase, which stacks are viable in 2026, what
the platform requires, and a phased path with a recommendation. Companion
(remote-control of the desktop app) vs standalone (on-device torrenting)
evaluated separately; they share infrastructure.

> **STATUS: DECIDED 2026-11-12 (owner) — STANDALONE app, phones + tablets,
> Kotlin/Jetpack Compose (Option C), shared DeepSeek key SHIPS in the app
> (same exposure stance as the Windows setup exe).** Distribution lane not
> explicitly decided; §8's recommendation (own-site APK on deepflux.space
> primary, Play optional later) is the working default. Phase 0 (companion
> enablement) is DEFERRED, not cancelled — Phase 1 (standalone MVP) comes
> first per the owner's framing. Implementation started 2026-11-12:
> `android/` Gradle project + `.github/workflows/android-build.yml` +
> `packaging/gen_android_key.py` (obfuscated-key ritual mirroring
> `packaging/gen_embedded_keys.py`). Web-sourced facts are marked with
> their source in §11; repo facts carry file:line. Nothing here has been
> measured on a device yet.

## 1. Request interpretation

"Android app development" for DeepFlux means one (or both) of:

- **Standalone**: a phone app that downloads torrents / plays IPTV / chats
  with the agent on its own, sharing the DeepFlux name, sources model, and
  config concepts.
- **Companion**: a phone app that remotely drives a running desktop
  instance — queue downloads from the phone, watch progress, send magnets,
  maybe stream from the PC's library.

These have different costs and different reuse profiles, so the study keeps
them separate. A third reading — "learn Android development in general" —
is not addressed; every section is DeepFlux-specific.

## 2. What DeepFlux is made of today (portability audit)

Layer inventory (line counts from `wc -l`, 2026-11-11). "Qt-free" means the
module imports no PySide6 — the portability boundary that matters.

| Layer | LOC | Qt-free? | Android outlook |
|---|---|---|---|
| `gui/` (36 modules) | 28,947 | no | **Dead for port.** Desktop widgets: QSS, tray, taskbar, win32 HWND/X11 window embedding, QtWebEngine. |
| `gui/main_window.py` alone | 7,770 | no | 5 tabs + menu system + workers; all desktop chrome. |
| `agent/` (llm, loop, tools, memory, rss) | ~7,500 | **yes** (llm/loop/tools verified Qt-free; bridges live in `gui/`) | Port = reimplement in Kotlin or embed via Chaquopy. The LLM client itself is small (`agent/llm.py` = 481 lines, requests + SSE). The mass is `tools.py` (5,235) — and most tools are desktop-bound (run_shell, set_settings, commander). |
| `dlmgr/` (HTTP engine, HLS/DASH, yt-dlp, control API, adblock) | ~5,400 | mostly (control_api, segment, engine are) | HTTP engine concept ports cleanly (OkHttp/Ktor + coroutines); yt-dlp does NOT exist on Android (see §3). |
| `engine/` (torrent_engine, state) | 1,115 | **yes** | Logic ports 1:1 conceptually — but to **libtorrent4j**, not Python bindings (§3). |
| `iptv/` (manager, metadata, xtream, epg, player, mpv_process) | ~6,600 | mixed (models.py is Qt-free; player/manager are not) | Data layer (m3u parser, Xtream player_api, XMLTV EPG) ports easily; playback = media3/ExoPlayer instead of libmpv. |
| `infra/` (jackett, flaresolverr, local_model, updater, telemetry…) | ~6,000 | mostly | **Desktop-OS-bound**: sc.exe/taskkill/PowerShell/installer rituals. Jackett/FlareSolverr *clients* port; their *server management* doesn't (§7). |
| `ircmgr/`, `native_messaging/`, `packaging/`, `website/` | — | — | Room (IRC) ports conceptually; native messaging is Chrome-desktop only. |

Two structural facts shape everything below:

1. **The app already has a local HTTP control plane.**
   `dlmgr/control_api.py` is a stdlib `ThreadingHTTPServer` with Bearer-token
   auth (`secrets.token_urlsafe(32)`, compare_digest —
   `control_api.py:65-86,100-103`), endpoints for job submit/list/pause/
   resume/cancel/retry/delete + `/api/open` (hand ANY target — file, magnet,
   URL — to the running app, `control_api.py:188-201`) + `/api/play`
   (`control_api.py:204-227`), and CORS/PNA headers for browser-extension
   callers. It is consumed today by the mini player handoff
   (`gui/mini_player.py:710`), the main window, and the Chrome extension's
   native-messaging host — a phone companion would be the fourth consumer.
   **But it binds 127.0.0.1 only** (`control_api.py:352`, class docstring
   :316-319) and exposes no torrent endpoints (dlmgr jobs only — the
   libtorrent engine in `engine/torrent_engine.py` has no API surface) and
   no event stream. LAN-remote is a configuration + feature delta, not a
   rewrite (§6, Phase 0).
2. **The backend is a queue/facade architecture already.**
   `engine/torrent_engine.py` runs libtorrent on a dedicated thread behind a
   command queue of futures (:70-119), v2-aware hashing (:29-46), resume
   data, rate limits, sequential mode — a shape that maps directly onto
   Kotlin coroutines + a `SessionModule`. The desktop's mini-player morph
   (7.8) proves the backend runs under a thin alternate surface.

## 3. Hard blockers for a code-level port (why 0% reuse of the running app)

Each is independently disqualifying for "ship the Python app on Android":

1. **The GUI is 29k lines of desktop Qt.** PySide6 *does* have an official
   Android deployment path now (`pyside6-android-deploy`, python-for-android
   based; single-architecture builds only — Qt for Python 6.12, Oct 2026,
   source §11.1). But this GUI embeds native windows (mpv via HWND/X11
   `--wid`), uses win32 taskbar/tray/Shell APIs, QSS theming with
   `scale_qss` machinery, and QtWebEngine — none of which exists on the
   Android Qt backend. The port would be a UI rewrite anyway, in a Python
   runtime with a toolchain this repo has never used.
2. **QtWebEngine (Chromium) is not available on Android.** Qt 6 WebEngine
   ships for Windows/Linux/macOS only; Android Qt uses the system WebView.
   The Browser tab — the app's largest construction cost per
   FAST_VIDEO_OPEN_STUDY.md §2 — cannot come along on ANY Python path.
3. **`import libtorrent` (Python bindings, `engine/torrent_engine.py:20`)
   has no Android wheels.** Chaquopy (§4) ships prebuilt packages for the
   scientific stack, not libtorrent; using it would mean maintaining a
   boost+libtorrent+CPython-extension NDK cross-compile recipe, forever,
   including the 16 KB ELF page-size alignment Play now mandates (§5).
   libtorrent4j already ships exactly those .so files — but they are JNI,
   not CPython, so they cannot back the Python import either.
4. **yt-dlp cannot run inside an Android app** (needs spawned Python +
   youtube-dl internals; the frozen-desktop workaround "update DeepFlux"
   doesn't map to a phone). Android YouTube extraction exists in other
   apps, but it is a separate, legally-fragile dependency — out of MVP
   scope (§6).
5. **The infra layer assumes a service-oriented desktop**: Jackett
   installed as a Windows service / spawned binary, FlareSolverr extracted
   next to the app, llama.cpp managed via taskkill/PowerShell
   (`infra/local_model.py`), Inno installers, auto-update via detached
   PowerShell. None of this is shippable on Android; the phone must be a
   *client* of these services (§7).

Conclusion: Android is a **reimplementation that reuses DeepFlux's
architecture, formats, config concepts, and brand** — not its code. The one
exception is the companion path, which reuses the desktop *instance itself*
as the backend (§6 Phase 0).

## 4. Stack options (2026 ground truth)

### Option A — Python reuse via Chaquopy (Kotlin shell + Python core)
Chaquopy 17.1.0 (open-source, MIT): Python 3.10–3.15, Gradle-integrated,
16 KB-page aware since 17.0 (Dec 2025) (source §11.2). Could host the
Qt-free backend (`agent/`, `engine/` *logic*, `dlmgr/http_client`-style
fetchers). **Rejected as the primary path**: the GUI still needs a full
Kotlin rewrite (29k lines can't come), libtorrent bindings need a custom
NDK recipe (blocker §3.3), and every desktop feature touched by the app
would drag Python↔JVM bridging forever. Two runtimes, two debuggers, one
developer. Revisit only if a specific subsystem is judged too costly to
rewrite (none qualifies — the biggest Qt-free subsystem, the agent, is an
OpenAI-compatible SSE client + prompt engineering, ~1k lines of real logic).

### Option B — Flutter (Dart) cross-platform
`libtorrent_flutter` (native libtorrent 2.0 bindings + built-in HTTP
streaming server for video-in-torrent) and `libtorrent_dart` exist on
pub.dev but are hobby-grade next to libtorrent4j (source §11.3). The only
strong argument is a future iOS app — and the owner removed macOS from the
desktop story in 2026-09-24, i.e. Apple breadth is not currently a goal.
**Second choice.**

### Option C — Native Kotlin + Jetpack Compose (RECOMMENDED)
- **Torrent engine: libtorrent4j** (`org.libtorrent4j`, actively maintained
  SWIG wrapper tracking libtorrent master; latest release 2025-09-14:
  Android API 28+, NDK r29, OpenSSL 3.5.5, boost 1.90; per-ABI artifacts
  `libtorrent4j-android-{arm,arm64,x86,x86_64}` on Maven Central, currently
  2.1.0-39 — source §11.4). Same libtorrent core as the desktop, so
  magnets, v1/v2 hybrid, sequential download, rate limits, DHT/UPnP,
  resume data — the exact surface `engine/torrent_engine.py` uses — are all
  available; the desktop engine's queue/facade design ports 1:1 onto a
  coroutine + `SessionModule` shape.
- **Playback: media3/ExoPlayer** — first-class HLS with live config
  (official Android media stack — source §11.5); covers IPTV m3u8 streams
  and local files. Xtream Codes player_api + M3U parsing + XMLTV EPG are
  plain HTTP/JSON/XML to port from `iptv/xtream.py`, `iptv/m3u_parser.py`,
  `iptv/epg.py`. (mpv-on-Android exists for exotic codecs; media3 first,
  mpv as an add-on if codec gaps show up.)
- **Agent chat: OkHttp SSE** against the same OpenAI-compatible endpoints
  (`agent/llm.py`'s contract: stream deltas, tool calls,
  `reasoning_content` dimming). The mobile agent ships a SMALL toolset
  (search_indexers via Jackett, add_torrent, play) — not the 5,235-line
  desktop registry, most of which is desktop-privileged by design.
- **Language cost is the real cost**: this repo has zero Kotlin. Toolchain
  learning (Gradle, Compose, coroutines) is the dominant estimate driver in
  §10 — mitigated by Phase 0 needing almost none (§6).

### Option D — Companion-only (ControlAPI over LAN)
No Android stack decision needed to start: the desktop app gains a LAN mode
(§6 Phase 0), and the phone surface can even be the existing
`website/deepflux/` as a PWA initially. Cheapest possible Android presence;
validates demand before committing to Kotlin. Standalone (Option C) can
absorb the companion features later — the pairing/token model is shared.

### Option E — Termux (not a product)
DeepFlux could in principle run headless in Termux, but Termux ships
libtorrent (C++) WITHOUT Python bindings (open feature request
termux-packages#32706 — source §11.6), and Termux is an enthusiast
environment, not a distribution channel. Documented here only so nobody
re-derives it.

## 5. Android platform requirements that shape the design (2026)

| Requirement | Fact (source §11.7) | Impact |
|---|---|---|
| 16 KB page size | Mandatory since 2025-11-01 for new apps/updates targeting API 35+ (existing-app updates deadline extended to 2026-05-31). All bundled .so must be NDK r27+ aligned. | libtorrent4j latest is NDK r29 — compliant. Any custom native code must be too. |
| Target SDK | Play requires API 36 (Android 16) for new apps/updates in 2026. minSdk 28 if libtorrent4j. | Set `minSdk 28 / targetSdk 36`. |
| Play developer verification | Mandatory since 2026-09-30 for ALL developers. | $25 once + identity verification; applies only if we choose Play (§8 allows skipping it). |
| Torrent clients on Play | Permitted (Flud live on Play); policy bans facilitating piracy, not the BitTorrent protocol. LibreTorrent was removed from Play in 2019 and lives on F-Droid — precedent for policy risk. | Play listing is viable but NOT required; own-site APK matches the existing deepflux.space lane exactly. |
| Foreground services | Downloads need a foreground service. Android 15 introduced a ~6 h cumulative/day cap on `dataSync` FGS (behavior-changes doc; **verify at implementation time**). | Long seeding on a phone is niche; design as download-then-pause-when-backgrounded, charging/wifi-gated. Needs a spike (§10). |
| Scoped storage | App-private dirs unrestricted; `MANAGE_EXTERNAL_STORAGE` (all-files) is restricted on Play but fine for own-APK. | v1: downloads to app dir + SAF-picked folder; request all-files only in the APK build if needed. |
| Doze / battery | Network dies in Doze unless FGS or top. | Same answer as FGS above; the watchdog concept from `WatchdogConfig` ports as a WorkManager reconciliation pass. |

## 6. Recommended path — phased

**Phase 0 — companion enablement (desktop-side, pure Python; ~1–2 weeks).**
No Android code yet, and it is useful even if the Android app never ships
(any LAN browser becomes a remote):
1. Opt-in LAN mode for ControlAPI: bind option + separate
   LAN-scoped token + QR code (host:port:token) — loopback stays the
   default and the desktop-off behavior stays as-is. This is a new network
   attack surface on the DESKTOP → security review required before merge
   (AGENTS.md rule 9-class change; `validate_control_url`'s private-host
   rejection for submitted URLs stays intact — it guards a different thing).
2. New endpoints: `/api/torrents` CRUD mirroring the dlmgr job surface
   (the torrent engine currently has zero API exposure),
   `/api/events` (SSE of job/torrent/agent events), magnet share via the
   existing `/api/open`.
3. A minimal remote web page (extend `website/deepflux/` or a served page)
   → immediate "Android app" via PWA, and the API contract the native app
   will consume is now pinned by tests.

**Phase 1 — standalone MVP (Kotlin + Compose; ~6–10 weeks incl. toolchain
ramp).** Torrents & Downloads only: libtorrent4j engine (magnet + .torrent
via share-sheet intents — the Android analog of the file-association
ritual), FGS with wifi/charging policy, sequential download + stream-into-
media3 for video-in-torrent, basic settings, deepflux.space APK
distribution. Companion pairing client included (it's just OkHttp against
Phase 0's API).

**Phase 2 — parity features (~4–8 weeks).** Jackett as a REMOTE source
(user's own desktop URL or any LAN Jackett; no on-phone Jackett), search
tier + indexer health concepts from `infra/indexer_health.py` ported,
RSS, IPTV (m3u/Xtream/EPG port + media3 playback), agent chat with the
small toolset, website gain a /deepflux/android page + `latest-android.json`
feed mirroring the updater ritual.

**Phase 3 — optional.** On-device llama.cpp (builds for Android; small
Qwen-class models on flagship SoCs — sources §11.8; this is the
`infra/local_model.py` concept reborn with the SAME model manifest), remote
FlareSolverr URL, Play listing if the owner wants one.

## 7. Feature mapping — desktop concept → Android equivalent

| Desktop | Android |
|---|---|
| File associations / `deepflux://` scheme (`infra/file_associations.py`) | Intent filters: `magnet:` scheme, `.torrent` mime, share-sheet target |
| Single-instance forward via ControlAPI `/api/open` | Companion: same endpoint over LAN; standalone: n/a |
| libtorrent session (`engine/torrent_engine.py`) | libtorrent4j `SessionModule` behind coroutines (same queue/facade shape) |
| mpv/libvlc embedded players (`iptv/player.py`) | media3/ExoPlayer (+ optional mpv later) |
| Jackett service management (`infra/jackett.py`) | REMOTE-ONLY: user-entered Jackett URL (desktop box or LAN server) |
| FlareSolverr bundling | REMOTE-ONLY: optional URL field |
| yt-dlp YouTube jobs | OUT of MVP; revisit separately |
| Auto-updater (`infra/updater.py`, Inno/PowerShell) | Own-lane APK: in-app download of new APK from `latest-android.json` (sideload update prompt); Play lane: Play's updater |
| Agent + memory + tools (`agent/`) | SSE client + small mobile toolset; memory store = app-private files (same markdown format = desktop-importable) |
| Telemetry heartbeat (`infra/telemetry.py`) | Same heartbeat endpoint, platform="android" |
| Tray/taskbar/win_taskbar | Notifications (download complete = notification channel; FGS notification) |

## 8. Build, CI, distribution (mirrors existing rituals)

- **Repo layout**: `android/` Gradle project in THIS repo (like `website/`);
  no code sharing with the Python tree — shared artifacts are the API
  contract, the model manifest (`infra/local_models.json`), and the brand
  (`DeepFlux.png`).
- **CI**: `.github/workflows/android-build.yml` on ubuntu-24.04 (SDK
  preinstalled on runners; repo is public → free minutes), release-artifact
  upload via the same optional-draft-release transport lane the Linux
  build gained in 7.7 (AGENTS.md 2026-11-10 lesson — machine's push lane is
  slow, release lane is fast).
- **Signing**: a keystore checked in as a GitHub encrypted secret. Play
  App Signing only if we list on Play. Keep ONE keystore forever (same
  "one identity" lesson as the code-signing note in AGENTS.md).
- **Distribution**: deepflux.space (`/deepflux/android/` page + direct APK
  + `latest-android.json`) as the primary lane — mirrors today's
  Windows/Linux ritual exactly; F-Droid submission possible later (FOSS
  license audit needed first — libtorrent4j is BSD/MIT-lineage, fine);
  Play optional per §5.

## 9. Owner decisions requested

1. **Scope**: companion-first (Phase 0 → cheap validation), standalone-first
   (Kotlin commitment now), or both in the phased order of §6?
2. **Stack**: accept Kotlin/Compose recommendation, or prefer Flutter for a
   future iOS option?
3. **Distribution**: own-site APK only (recommended, matches current
   lane), or Play listing too (verification + policy exposure)?
4. **Embedded shared DeepSeek key** in the mobile app (same exposure as the
   Windows setup exe — owner already accepted that trade) or keyless-on-
   first-launch with the desktop handoff carrying the key?

## 10. Effort, risks, unknowns

**Estimates (solo, this repo's velocity as calibration):** Phase 0 ≈ 1–2 wks
(pure Python, testable by the existing suite + browser). Phase 1 ≈ 6–10 wks
(the Kotlin/Gradle/Compose ramp is inside this figure and is the widest
error bar — first Android project in this repo). Phase 2 ≈ 4–8 wks.
Phase 3 unestimated (optional).

**Risks:**
- *Toolchain ramp* (dominant): no Kotlin anywhere in repo history.
- *FGS dataSync 6 h cap* (§5): long-seed UX needs a design spike on real
  devices; mitigation is scoped-down seeding (ratio/time caps — the config
  fields already exist in `TorrentsConfig`).
- *Play policy ambiguity for torrent clients*: mitigated by own-APK-first
  distribution; Play stays optional forever.
- *Companion LAN mode = new attack surface on the desktop app*: token-only
  + opt-in + off-by-default + security review before merge (§6 Phase 0).
- *libtorrent4j bus factor*: single maintainer; mitigations — it's a thin
  SWIG wrapper over upstream libtorrent, and pinning a version is fine for
  years (the desktop pins libtorrent>=2.0 likewise).

**Explicitly out of scope / unknowns needing spikes:** Android 15/16 FGS
behavior on target devices; media3 codec coverage vs the desktop's
ffmpeg-bundled coverage; YouTube extraction (legal + technical — deferred);
on-device LLM memory/thermal envelope (Phase 3).

## 11. Evidence & sources

Repo facts: `dlmgr/control_api.py` (auth :65-103, /api/open :188-201,
/api/play :204-227, loopback bind :352, alt-port probe :360-369),
`engine/torrent_engine.py` (:20 import, :29-46 v2 hashing, :70-119 queue
facade), `requirements.txt`, `gui/` inventory + line counts (wc -l,
2026-11-11), FAST_VIDEO_OPEN_STUDY.md §2 (browser-tab construction cost),
AGENTS.md (Linux port history, mac removal 2026-09-24, distribution
rituals, 7.7 transport-lane lesson).

Web facts (retrieved 2026-11-11):
1. PySide6 Android deployment (pyside6-android-deploy, single-arch, Qt for
   Python 6.12): https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-android-deploy.html
   and https://www.qt.io/blog/qt-for-python-release-6-12-is-here
2. Chaquopy 17.1.0 / Python 3.10–3.15 / 16 KB pages: https://chaquo.com/chaquopy/news
   and https://chaquo.com/chaquopy/chaquopy-version-17-0-0
3. Flutter libtorrent packages (hobby-grade): https://pub.dev/packages/libtorrent_flutter ,
   https://pub.dev/packages/libtorrent_dart ; LibreTorrent (jlibtorrent,
   F-Droid primary, removed from Play 2019):
   https://github.com/proninyaroslav/libretorrent
4. libtorrent4j (latest release 2025-09-14, API 28+, NDK r29, Maven
   Central artifacts): https://github.com/aldenml/libtorrent4j/releases and
   https://repo1.maven.org/maven2/org/libtorrent4j
5. media3 HLS support: https://developer.android.com/media/media3/exoplayer/hls
6. Termux libtorrent without Python bindings:
   https://github.com/termux/termux-packages/issues/32706
7. Play requirements 2026 (16 KB pages, API 36, developer verification,
   torrent-client policy): https://developer.android.com/guide/practices/page-sizes ,
   https://developer.android.com/distribute/play-policies ,
   https://play.google.com/store/apps/details?id=com.delphicoder.flud
8. llama.cpp on Android (flagship-SoC performance discussions):
   https://github.com/ggml-org/llama.cpp/discussions/14356

## 12. REVISED roadmap — owner decision 2026-11-14 (supersedes §6 priorities)

The owner reviewed the phases against the PC version and re-ranked by what
the Android app MUST mirror. Explicit priority order:

1. **Web browser (the major part)** — with video downloading, the video
   grabber, play-all-formats, torrent-file acquisition, and general file
   downloads. "The browser is a major part of the software."
2. **File commander (second major part)** — the dual-pane orthodox file
   manager (gui/commander_tab.py concept) adapted to touch.
3. **Chat tab (less important, also cool)** — exists since v0.4; deepen.

Old Phase-2 leftovers re-sequence BEHIND these (see "Re-sequenced" below).
§6 stays above for history.

### Phase A — Browser & the download core (the major part)

- **A1. HTTP download engine** — the dlmgr twin and the FOUNDATION for
  everything else in A: queued workers over HttpURLConnection, progress /
  pause / resume / retry, Range-resume sidecar (`.part`), completion →
  app storage + the existing MediaStore export ritual, surfaced in the
  Downloads tab next to torrents. Today the app has NO plain-HTTP
  downloader at all (only MediaStoreExport.kt in dlmgr/).
- **A2. Browser tab** — system WebView (Chromium-backed, the QtWebEngine
  stand-in): URL bar, multi-tab strip, bookmarks + history (DataStore),
  adapted dark chrome; intercept `magnet:` navigations AND
  `application/x-bittorrent` responses straight into the torrent engine
  (the in-app version of the intent filters); "open in system browser"
  escape hatch; downloads not claimed as torrents route to A1.
- **A3. Video grabber v1** — the site_grabber/dialog concept, in-browser
  edition: `shouldInterceptRequest` sniffing for video content-types and
  extensions (mp4/webm/ts/m3u8/mpd) + a JS DOM scan for `<video>`/
  `<source>`/blob sources; a "video found" chip offers the download via
  A1. Progressive first; HLS-by-segments (dlmgr/segment.py concept) can
  land incrementally — flag m3u8 finds as such rather than blocking.
- **A3b. Site grabber** (keyword search over ANY video site, the generic
  card-parser of dlmgr/site_grabber.py) — depends on a page-scan port;
  scheduled after A1-A4 prove out, not on the critical path.
- **A4. Play-all-formats hardening** — codec/container coverage matrix on
  the emulator (mp4/webm/mkv/ts/HLS/DASH stock media3 extractors first);
  ffmpeg extension (custom NDK build) ONLY for measured real gaps
  (AC3/EAC3 class). Bundling ffmpeg binaries is a Play-policy review item.
- **A5. Torrent acquisition completeness** — .torrent via intents already
  works (engine.addTorrentFile); A2 adds the in-browser path; share-sheet
  already lands .torrent attachments.

### Phase B — File commander (second major part)

- **B1. Files tab** — browse app storage + MediaStore collections
  (Downloads/Video), list/grid, open-with (video → in-app player, reusing
  the loopback stream), share, delete, rename (MediaStore-aware),
  properties, storage usage summary.
- **B2. Dual-pane on the 16:9 tablet** (the owner's target device) —
  Commander-style two panes; F-keys become long-press context menus +
  multi-select batch actions.
- **B3. Agent file tools** — list_files / open_file so the chat can
  browse and play anything (completes the chat-covers-the-app story).

### Phase C — Chat tab deepening (cool, less critical)

- **C1. Memory store** — markdown files in app-private storage, same
  format as desktop agent/memory.py (desktop-importable), save/search
  tools.
- **C2. More tools** — download_url / list_http_downloads (A1 surface),
  open_url (A2 surface); the toolset then covers every app surface.
- **C3. Polish** — persisted history exists; stream/reasoning UX exists.

### Re-sequenced (was Phase 2 leftovers)

- **Release lane** (signed APK + website /deepflux/android page +
  latest-android.json feed + in-app sideload-update prompt per §7):
  moved to AFTER Phase A — 2.0 ships with the browser as the headline.
  Pull earlier on owner request.
- **Indexer health port** and **RSS**: after Phase C.
- **Phase 3** (on-device llama.cpp, remote FlareSolverr URL, Play
  listing): unchanged, optional.

### §12 progress log

- **A1 (HTTP engine) — done 2026-11-14.** Queued workers, Range resume via
  `.part`, pause/resume, retry with permanent-4xx fast-fail, eager atomic
  persistence, MediaStore export. Verified live: real download+export+play,
  pause frozen/resumed-from-point, force-kill auto-resume from `.part`,
  error persistence, `.part` cleanup. Agent `download_url` + Files section.
- **A2 (browser tab) — done 2026-11-14.** One WebView per tab in a shared
  container; URL bar (auto-https/DDG), tabs, fullscreen <video>. Magnet
  links → engine directly; DownloadListener = single choke point carrying
  cookies/UA/Referer: .torrent → fetched+validated → engine, other files →
  A1. Browser is the first rail item.
- **A3 (video grabber) — done 2026-11-14.** shouldInterceptRequest URL
  sniffing (mp4/webm/mkv/mov/avi/flv/m3u8/mpd; .ts segments deliberately
  ignored) + JS DOM scan (video/source, blob: flagged) → 🎬 chip → sheet →
  A1 download with page session headers.
- **A3 HLS downloading — done 2026-11-14.** Master → best variant that
  carries its own audio; TS → `.ts`, fMP4 (init+segments) → `.mp4`;
  AES-128 (explicit IV, and RFC-8216 sequence-number IV) decrypted with
  javax.crypto; per-segment retries; resume via an atomically-written
  sidecar (segments done/bytes/total). VERIFIED byte-identical against
  ffmpeg-constructed expected outputs (plain/AES×2/seq-IV/master/fMP4),
  plus kill-resume and pause-resume mid-stream; real-world: mux
  test-streams 64-segment VOD full download (decode-clean), Apple advanced
  example correctly refused (separate audio), mux master picks 1080p.
  NOT supported (clear error): live playlists, EXT-X-BYTERANGE,
  SAMPLE-AES/DRM, separate-audio renditions (needs a muxer).
- **A4 (format coverage) — measured 2026-11-14 on the API-36 emulator**
  (33 clips; container support is app-level, codec support is
  DEVICE-dependent — real devices often decode AC3/EAC3 that the emulator
  cannot). Plays fully: mp4/mkv/mov/3gp/ts/flv/avi/webm × H.264, HEVC,
  VP8, VP9, AV1, MPEG-4(XVID); audio AAC/MP3/Opus/Vorbis/FLAC/PCM (all 6
  audio-only containers too). Silent-video gaps (now warned on screen):
  AC-3, E-AC-3, DTS, TrueHD. Won't open: WMV/ASF, MPEG-1 .mpg, Sorenson
  FLV1. The player now logs per-track support and warns "no sound/no
  picture (CODEC)" instead of playing silently; playable-extension lists
  widened to the measured set; the HLS fallback no longer masks the real
  error for local files (first error is shown). OPEN DECISION for the
  audio gap (most movie MKVs carry AC3/DTS): a media3 ffmpeg decoder
  extension — prebuilt Jellyfin AAR is GPL-3 (licensing call), a custom
  NDK build is LGPL but heavier; ~10–20 MB APK either way.
- **Engine lessons (2026-11-14, all fixed the same day):** resume() used to
  clear the OLD worker's pause flag → pause+quick-resume = two workers on
  one `.part` ("could not move .part into place"); a finished-but-unrenamed
  `.part` got deleted on 416 instead of promoted; the engine was created
  per UI composition → Back+reopen = two ENGINES (now a DeepFluxApp
  singleton); no mutation may persist before the saved list loads.
- **B1+B2+B3 (file commander) — done 2026-11-14.** Commander tab, 5th nav
  item (hand-built folder icon — material-icons-core has none and extended
  would bloat the unminified release). Scoped-storage roots per pane:
  app storage tree (the shared download dir, with breadcrumb + Up) and the
  public MediaStore Downloads/Videos collections. Dual independent panes on
  wide screens (one repository PER PANE — a shared listing flow would have
  each pane repaint the other), single pane on phones. Long-press
  multi-select (adb note: emulate with `input motionevent DOWN/UP`, the
  same-point swipe trick does not register); actions: play (app files and
  public items both via the loopback server — public content URIs stream
  through a new range-capable StreamSource path), share (FileProvider),
  open-with, rename (File.renameTo / MediaStore DISPLAY_NAME update),
  delete (batch; the confirm dialog warns public deletions affect every
  app), details (name/size/modified/type/source), storage-usage line.
  Verified on the tablet: both roots side by side, folder nav + Up, play
  from BOTH roots (track-level READY logs), rename on disk, 2-item
  multi-select delete, details, system share chooser. Agent gained
  list_files (traversal-guarded) + open_file (plays media by partial name)
  — both verified live in chat.
- **C1+C2+C3 (chat deepening) — done 2026-11-14.** MemoryStore ported from
  agent/memory.py byte-compatible: USER.md / MEMORY.md / daily/YYYY-MM-DD.md
  with `- [id:<12hex>]` lines, same 2000/3000-char prompt budgets, same
  secret-rejection patterns, atomic writes — a copied directory IS a desktop
  memory. Agent tools save/search/list/edit/forget_memory + open_url (opens
  the in-app browser; tool code runs on the chat thread, so the AppRoot
  callback hops to the main Looper — WebView creation off-Looper throws).
  The system prompt is REBUILT every send so entries saved this conversation
  are visible next turn. Verified live: save (USER.md format exact), recall
  via list_memories, search, forget (entry removed from file), open_url
  (browser tab opens on the URL). download_url / list-both-lanes shipped
  with A1. Known quirk (pre-existing): one assistant reply after a tool
  round occasionally renders as an empty bubble.

## 13. Polish + Windows-import audit (2026-11-14, post-§12)

Desktop surface inventoried from gui/ (tabs), agent tool registry, dlmgr/
and infra/; Android surface = everything shipped in §12. Per the owner:
import CONCEPTS, execute Android-natively. Effort: S <1h · M half-day ·
L multi-day. Recommendation tiers: NOW (next sprint) / LATER / SKIP
(doesn't fit Android reality).

### Import candidates

| # | Desktop concept | Android execution | Effort | Tier |
|---|---|---|---|---|
| 1 | Tray/download notifications | Extend TorrentService to cover the HTTP/HLS lane too: FGS notification with active-transfer progress (also removes the OS-kill-while-backgrounded risk noted in A1) | M | **NOW** |
| 2 | Browser adblock (hosts list) | WebView shouldInterceptRequest returns empty for hosts-list matches; list ships in assets, desktop parity | S | **NOW** |
| 3 | Browser bookmarks + history | Star in URL bar → DataStore; tab-strip long-press; history page. (Importing Chrome's DB is impossible on Android — no import, manual only) | M | **NOW** |
| 4 | RSS feeds + auto-download | Feeds in DataStore, polled by the service loop, matched items → engine; agent tools add_rss_feed / get_rss_feed_items / download_from_feed | L | **NOW** (biggest missing desktop staple) |
| 5 | Indexer health (cooldowns) | JSON sidecar of per-indexer failures; Jackett search skips cooling sources; failure summary to the LLM (desktop-proven against CF-walled indexers) | M | **NOW** |
| 6 | Settings export/import (.dfc) | Passphrase-encrypted export via SAF file picker; same concept as config_backup.py (memory + settings + sources) | M | LATER (release-adjacent) |
| 7 | In-app auto-update | latest-android.json check + APK download via the A1 engine + PackageInstaller prompt (needs the parked release lane) | M | LATER (blocked on release) |
| 8 | Subtitles (OpenSubtitles) | ExoPlayer sidecar .srt via MediaItem.subtitleConfigurations; fetch by video name/hash; ✕-bar toggle | M | LATER |
| 9 | Site grabber (keyword search on any video site) | A3b in the plan: port site_grabber.py's card heuristics over the in-app browser's own page fetch | L | LATER |
| 10 | find_alt_release / find_alt_trackers / swarm diagnostics | libtorrent4j exposes trackers/swarm; port as agent tools | M | LATER |
| 11 | Voice input | Android SpeechRecognizer → chat input mic button | S | NOW (cheap, very visible) |
| 12 | Play-while-downloading for HTTP/HLS | LocalStreamServer already follows growing files — register the .part of RUNNING downloads (HLS .ts concatenations stream cleanly; MP4 needs moov-at-front, warn when not) | S | NOW |
| 13 | Player resume positions | Save position per URL in DataStore, offer resume on reopen | S | NOW |
| 14 | Browser automation tools (browser_click/…) | Drive our WebView via evaluateJavascript + bridge; grabber covers most download needs already | L | LATER |
| 15 | Multiview (4-stream grid) | 2×2 PlayerView grid, one audio | M | LATER (fun) |
| 16 | YouTube downloads (yt-dlp) | No Python on Android; concept alternative = NewPipe extractor (licensing review) | L | SKIP for now |
| 17 | Room (OnlyHumans hub) | Big IRC port; no Android demand voiced | L | SKIP |
| 18 | Local AI (llama.cpp on-device) | Plan §12 Phase 3, still parked | L | PARKED |
| 19 | Font size / text-color themes | Android convention: follow system font/dark settings | — | SKIP |
| 20 | FlareSolverr / Jackett hosting | Desktop hosts these; Android uses REMOTE Jackett by design (§7) | — | SKIP |

Already ported (no action): EPG now/next + XMLTV cache, magnet/.torrent
intake, sequential streaming, memory store, Jackett remote search, IPTV
playback, dual-pane commander, mini "now playing" ✕ player UX.

### Polish list (observed this week or visible in code)

| P# | Item | Size |
|---|---|---|
| P1 | Empty assistant bubble occasionally rendered after a tool round (seen twice: search_memory, one earlier) | S |
| P2 | apply the HTTP engine's eager+atomic persistence hardening (incl. load-gate) to TorrentRepository's state file — the empty-torrents_state.json class of loss | S |
| P3 | ffmpeg decoder extension decision (AC3/EAC3/DTS/TrueHD + MPEG-2) — Jellyfin prebuilt = GPL-3 vs custom NDK LGPL; ~10–20 MB | decision |
| P4 | Playable-extension regex duplicated (DownloadsScreen, CommanderScreen, ChatSession) — one shared constant | S |
| P5 | Repository notices are hardcoded English (postNotice etc.) while the UI is localized — needs a strings pass | M |
| P6 | Accessibility: pause glyph lacks contentDescription; nav icons near-identical (three List icons) | S |
| P7 | Commander: no sort/search, no copy/move between panes (desktop F5-transfer concept) | M |
| P8 | Zero test infrastructure (no unit tests on the Android side) — add Robolectric/JUnit for HlsPlaylist, MemoryStore, HttpDownloadRepository resume logic before release | M |
| P9 | Release hygiene: R8/minify disabled, lint pas, screenshot refresh for the store listing | M |
| P10 | Agent: tool-round turns have no visible per-tool progress beyond the ⚡ line (desktop streams tool events) | M |

### Recommended next sprint (polish pass 1)

NOW-tier imports #1 (service+notifications), #2 (adblock), #5 (indexer
health), #11 (voice), #12 (stream-while-downloading), #13 (resume
positions) + polish P1, P2, P4, P6 — then #3 bookmarks and #4 RSS as the
following feature sprint.

### Polish pass 1 — 2026-11-14 (built + mostly verified; gaps noted)

Shipped: #1 service+notifications now cover the HTTP/HLS lane (TorrentService
mirrors both lanes; HTTP engine fires onActiveChanged → ensureService;
verified live: service started with an agent-triggered download, 55
notification enqueues/54 updates during transfer). #2 adblock — the desktop's
curated 202-domain blocklist extracted VERBATIM into browser/Adblock.kt,
suffix-matched in shouldInterceptRequest (verified live: doubleclick.net
intercepted, page probe saw the empty response). #5 indexer health —
IndexerHealth.kt sidecar (2 fails → 30 min doubling cooldown, 8 h cap,
Jackett-level failures never recorded) + JackettClient rewritten to
per-indexer parallel fan-out with failure/cooling summaries to the LLM.
#11 voice input — SpeechRecognizer mic button in the chat input (hidden
gracefully where no recognizer exists). #12 play-while-downloading on
RUNNING HTTP/HLS cards via the growing .part (verified: button appears and
opens the player; MP4 moov-at-end files fail decode by design, HLS .ts is
the reliable case). #13 resume positions — player_positions.json keyed by
title, 5s tick + save-on-destroy, monotonic (reopening never rewinds),
cleared at natural end, "Resume at / Start over" bar (saving, destroy-save,
END-clear, and threshold gating all log-verified; the bar's VISUAL not
captured). P1 empty assistant replies after tool rounds — DeepSeek
occasionally returns blank content; the bubble is now replaced by a visible
"(empty reply — say go on)" hint instead of silence. P2 torrent engine got
the HTTP engine's load-gate (mutations wait for the saved list). P4 one
Playables regex everywhere. P6 distinct hand-built nav icons
(Download/Chat/TV/Folder/Mic) + pause-glyph contentDescription. Bonus: the
browser URL bar now submits on hardware-Enter (KeyEventType API — soft-IME
Go was unreachable without an IME).

VERIFICATION GAPS (host turned unstable mid-pass: emulator input lag,
hardware H.264 decode failing even on cold boot — software codecs fine,
matches this machine's NVIDIA history; one adb wedge + one emulator wedge
killed two test runs): #5's live search verified only up to the tool
executing with zero recorded failures (Jackett reachable, eztv 3 ms from
host); the model-reply half and a forced-cooldown scenario unverified.
#13's bar never captured on screen. #11 mic-absence not dumped. Recommend
re-verifying these three after a host reboot.

ALL THREE GAPS RE-VERIFIED 2026-11-14 late evening, post host reboot
(host booted 23:11; H.264 hardware decode healthy again — avc1 track
`supported=true selected=true`, played clean, no input lag, no adb wedges;
that confirms the instability was the documented NVIDIA-pattern host
state, not the app):

* **#5 forced cooldown + model reply — VERIFIED.** Seeded
  indexer_health.json (run-as, app force-stopped first: the in-memory
  IndexerHealth re-persists on every record — seed must land before
  process start) with 1337x fails=2 + 25 min cooldown, then ran a real
  chat round ("search indexers for ubuntu…"). The tool SKIPPED 1337x
  (sidecar entry byte-identical after the search — a query-with-results
  would have cleared it via recordSuccess, a failed query would have
  bumped fails to 3 and rewritten cooldownUntil), the summary reached the
  tool result, and the model's reply relayed BOTH the seeded cooldown
  ("⏳ Skipped — cooling down: 1337x (~25 min left, last error: HTTP
  500…)") AND a genuinely live per-indexer failure ("⚠️ Failed: eztv
  (HTTP 400 API error)") — eztv got fails=1 recorded with Jackett's
  torznab error body (code 900 IndexerException) parsed into lastError,
  exactly the desktop's parse-body-first behavior. Seeded entry removed
  afterward; the legit eztv record kept.
* **#13 resume bar — VERIFIED RENDERED.** Full end-to-end: played
  big_buck_bunny (597 s) ~90 s, closed via ✕ (destroy-save wrote
  pos=87.9 s), reopened — `dumpsys activity top` (NOT uiautomator) shows
  the bar attached and visible: `LinearLayout 0,1008-1920-1080` with two
  `Button`s at x782–978 / x978–1137, and the 8 s hide callback pending in
  the main-looper message queue; screenshot pixels confirm two filled
  buttons with glyph text at exactly those coordinates. GOTCHA that cost
  an hour: the bar sits at y1008–1080 INSIDE the navigation-bar overlay
  zone — uiautomator PRUNES it from dumps entirely (✕/title survive, the
  bar never appears), and the media3 controller (also bottom-anchored,
  auto-hides ~3 s) is a convincing look-alike in pixel diffs. For
  bottom-edge overlays on this AVD: trust `dumpsys activity top`, not
  uiautomator. Artifacts: v4_bar_final.png / resume_bar_buttons.png saved
  to the session visualization dir.
* **#11 mic-absence — VERIFIED.** uiautomator dump of the Agent input row
  on this recognizer-less AOSP image: only the text field + Send node,
  zero mic/speech nodes (`SpeechRecognizer.isRecognitionAvailable` false →
  button correctly hidden).
* Monotonic resume re-confirmed incidentally: a second session that
  played only ~60 s (less than the saved 87.9 s) never rewound the saved
  position (the monotonic guard kept 87.9 s).

### RESUME HERE (next session)

State after the "continue the plan until the chat room" lane (2026-11-15
01:35): §13's import list is done from #6 through #17 — settings backup,
subtitles, page-links grabber, torrent diagnostics, multiview, and the
FULL OnlyHumans Room port (crypto + hub client + controller + UI),
live-verified against the desktop's own python primitives over the real
hub. Remaining §13 stragglers: P5 (strings pass for repo notices), P7
(Commander sort/copy), P8 (Robolectric tests), P9 (release hygiene), P10
(per-tool progress); #7 auto-update stays blocked on the parked release
lane; #14 browser automation stays dropped (grabber covers it); #16/#18
stay skipped/parked.

### The #6→#17 lane — 2026-11-15 (owner: "continue the plan until the chat room")

* **#6 Settings export/import — DONE + verified both directions.**
  `data/SettingsBackup.kt` reimplements the desktop's .dfc format on
  javax.crypto: the same JSON envelope (magic/kdf/iterations/salt) around
  a spec-correct Fernet token (PBKDF2-SHA256 390k, 32-byte key split
  signing/encryption). Settings card with SAF pickers + passphrase dialog
  (twice on export) + restart-to-apply. VERIFIED: desktop-encrypted .dfc
  imported on Android (mapped Jackett url/key + user LLM key into
  DataStore); Android export decrypted by infra/config_backup.py on the
  host (payload carried prefs + sidecars + memory). GOTCHA fixed live:
  Fernet tokens MUST keep '=' padding — python's urlsafe_b64decode rejects
  unpadded. Android payloads restore prefs + bookmark/history/rss/
  positions sidecars + memory/ verbatim; desktop payloads map the portable
  fields. Import overwrites live prefs — the verification accidentally
  clobbered the real Jackett key and it was restored via a generated .dfc.
* **#7 auto-update — STILL BLOCKED** on the parked release lane (needs
  latest-android.json + signed artifacts). No code written.
* **#8 Subtitles — DONE + verified.** `iptv/OpenSubtitles.kt` (REST v1
  port: hash search → title fallback, download link flow, UA versioned);
  Settings key field (nothing embedded — same stance as desktop); CC
  button in the player top bar → search sheet → download → sidecar srt
  next to the file (local) or in filesDir/subtitles (streams) → MediaItem
  rebuilt with SubtitleConfiguration + position preserved; auto-load of an
  existing sidecar on open; CC tap toggles the text track. VERIFIED live
  with the desktop's embedded key (injected via Settings, never printed):
  real search results (Big Buck Bunny 2008 top hit), toggle flips
  selected=true/false/true in the track log, auto-load attached + selected
  a crafted sidecar. The /download endpoint was 503 SERVICE-SIDE the whole
  evening (host-confirmed with the same key) — the download path is
  desktop-identical code and its error surfaces cleanly; re-verify the
  download click when the service recovers.
* **#9 Site grabber v1 (page-links scan) — DONE + verified.**
  BrowserController scans the page DOM (evaluateJavascript one-shot, no
  bridge): every anchor as an absolute URL + loose magnets matched from
  the HTML; classified magnet/.torrent/video/page; 🔗 chip (bottom-start,
  mirrors the 🎬 chip) → LinksSheet with per-row Download/Open using the
  page session (cookies/UA/referer). Agent tool `scan_page` returns the
  formatted list synchronously (evaluateJavascript posted to main +
  latch — EVERY WebView touch must be main-thread; reading view.url on
  the chat thread threw, caught live by the agent round). Verified: chip
  count, all four categories incl. a NON-ANCHOR magnet, magnet row →
  engine, agent relay of the full list.
* **#10 torrent_info + find_alt_trackers — DONE + verified.** Agent
  tools; trackers read from libtorrent4j via a new engine accessor on
  the engine dispatcher (withContextBlocking genericized). torrent_info
  verified live on a real (fake-hash) torrent: state/swarm/speed/size/
  trackers-none-DHT-branch all relayed by the model; find_alt executed
  (Jackett search path proven in #5).
* **#15 Multiview — DONE + verified.** `iptv/MultiViewController.kt` +
  a ⊞ 2×2 toggle in Live TV: 4 slots, per-slot channel picker dialog,
  one audio (first filled slot, rest muted), tap-a-tile → full-screen
  PlayerActivity, long-press → clear, DisposableEffect releases all
  players when the grid closes. Verified: grid renders, picker works,
  one tile playing live video in-grid; the second tile with the SAME
  url stayed dark (stream-side single-connection limit, not a framework
  failure).
* **#17 Room (OnlyHumans chat room) — THE DESTINATION — FULLY PORTED +
  CROSS-LANGUAGE VERIFIED against the live hub.** New `room/` package:
  `RoomCrypto.kt` (b64url, HKDF-SHA256, Argon2id egk, room hex, admission
  proofs, AAD, XChaCha20-Poly1305 = hand-rolled HChaCha20 + BC's IETF
  AEAD, gk-deliv key sealing, padding buckets, frame Generation, Ed25519
  identity + libp2p base58 peer ids), `Hub.kt` (reg/room/inbox push+drain,
  2 MiB caps, HUB-CLOCK COMPENSATION from response Date headers — the hub
  runs 40–85 s ahead of this network and rejects "stale timestamps"),
  `OhRoom.kt` (the full controller state machine: join → host-or-seat,
  2–3.5 s drain loop, sealed chat, host-side admission + key delivery +
  members fan-out, rotation, leave, history JSONL, config sidecar), and
  the RoomScreen (7th nav item, forum icon: join card, member chips,
  transcript, composer, host badge, 🔒 rotate when hosting). Dependency:
  BouncyCastle (bcprov-jdk18on 1.78.1). VERIFICATION (both directions,
  desktop python driving the app's own ircmgr.oh_room primitives): python
  HOST ↔ android MEMBER (join proof verified by the host, key delivery
  OPENED by android, sealed chat decrypted both ways) AND android HOST ↔
  python MEMBER (python OPENED the Kotlin-sealed gk-delivery, android
  admitted the member + fanned members/chat, python OPENED the
  Kotlin-sealed chat — final verdict PASS). SIX live-caught bugs fixed:
  Ed25519Signer init flag wrong in sign (private cast as public), the
  twin bug in verify (every mail item was silently signature-rejected),
  HChaCha20 quarter-round using shl instead of ROTATE (wrong subkey —
  every sealed frame failed auth while all pre-XChaCha steps passed),
  NetworkOnMainThread on chat/leave fan-out (chat echoed locally but
  never left the device), reg-429 aborting join passes (now non-fatal +
  backed off), and the hub clock skew. Byte-exactness was proven
  independently on the JVM against PyNaCl vectors (XChaCha seal) and a
  python seal_room_key fixed vector (nonce/keyenc/open all match).
  NOT yet exercised live: key rotation (the code paths mirror the
  desktop; the desktop-side rotate flow is the reference) and presence.
  Android history is plaintext JSONL in private storage (the desktop's
  is sealed-at-rest — noted gap, fine for v1).

FIRST PUBLIC ANDROID DEPLOYMENT — LIVE 2026-11-15 ~02:45 (owner: "deploy
the apk to the same website"): signed release build (assembleRelease with
keystore.properties + deepflux-upload.jks, cert CN=DeepFlux O=ex4n1m0,
versionCode 8 / versionName 1.2.0, 84,670,355 bytes, sha256 a9b4d01f…,
embedded DeepSeek key via app/embedded_key.properties) shipped as
website/deepflux/DeepFlux-1.2.0-Android.apk. Site: third "Android · APK"
install tab on BOTH language pages (v1.2.0 · 80.7 MB · arm64-v8a ·
armeabi-v7a · x86_64, sideload steps, sha256 block), api/download.js
FILE_RE extended with -[0-9.]+-Android\.apk (counted downloads), and
deepflux/latest-android.json written (desktop latest.json format — the
app has no updater yet; the feed is ready for one). All verified live:
counter 302s to the file, CDN serves the exact bytes (sha re-verified
after download), and the downloaded APK was installed on the tablet
emulator (debug build uninstalled first — signatures correctly conflict)
where it boots into the full 7-tab app, notification permission asked
once. website/**/*.apk added to .gitignore (deploy dir = distribution).
NOTE: the tablet emulator now runs the RELEASE build with fresh data —
the dev Jackett key must be re-entered there for further agent testing.
Owner decision still open: AC3/DTS licensing. Play Store: untouched.
ALL work remains UNCOMMITTED (android/ untracked) — owner's call.

### Feature sprint: bookmarks + history (#3), RSS + auto-download (#4) — 2026-11-15

**#3 Bookmarks + history** — `browser/BrowserStore.kt` (JSON sidecars
browser_bookmarks.json / browser_history.json, atomic writes, history cap
500, consecutive-URL dedup at the TOP only); BrowserController records
visits at onPageFinished and patches the newest entry's title from
onReceivedTitle (titles land AFTER finish); toolbar gained ☆/★ toggle
(contentDescription swaps) + 🔖 sheet button; `BookmarksSheet` = AlertDialog
with Bookmarks/History FilterChips, tap-a-row opens (loadInput routes
magnet bookmarks to the engine), per-row ✕ delete, Clear-history action.
FLAT list by design (no folder tree — Chrome-on-Android reality; no import
possible). Verified live: star toggle + persistence with the real page
title, sheet listing both tabs, history dedup + title patch, per-entry
delete, clear, bookmark-open navigation (server hit + bar URL + ★),
persistence across force-stop (sidecars survive while tabs reset — by
design, tabs aren't persisted).

**#4 RSS + auto-download** — `rss/RssClient.kt` (RSS 2.0 + Atom via
XmlPullParser, 5 MiB streamed body cap per the desktop rule, magnet
extraction from descriptions — captured at </description> close because
the per-element text buffer resets on the next START_TAG; atom rel=-self
links skipped; item link = magnet if present else <link>) +
`rss/RssRepository.kt` (feeds+seen-guIds in rss_feeds.json; addFeed
baselines current items as seen — NO backlog mass-download; refresh
applies auto-download to unseen keyword matches, so a manual refresh IS a
"check now"; pollIfDue = interval(15 min)+overlap guard, called from the
TorrentService loop and an app-start thread; seen marked ONLY when an
engine accepted the item — magnets pre-validated via MagnetParser so an
invalid one never marks seen; .torrent links fetched capped 32 MiB with
bencode sanity; other http → the A1 engine). UI: third RSS chip in
Downloads (chip count COLLECTS the flow — a .value read went stale after
the first add, fixed mid-verification), feed cards with ⚡+keywords line,
expandable item lists with per-item Download, Refresh/Edit/Delete, add +
edit dialogs (URL/keywords/auto toggle). Agent tools: add_rss_feed,
list_rss_feeds, get_rss_feed_items, download_from_feed, remove_rss_feed.
Verified live against a crafted host feed (10.0.2.2:8765): add + channel
title (after fixing the parser: TEXT must append ALWAYS, not just
inItem — the channel <title> sits before the first item), baseline no
auto-downloads, keywords "ubuntu" + auto → refresh auto-downloaded ONLY
the new ubuntu item (fedora + baselined items untouched; log
"rss auto-download: Ubuntu 25.04 daily ISO torrent"; Torrents 0→1),
manual item download (debian13, 1→2), items list (5), feed remove,
persistence across restarts, and the agent round (list_rss_feeds +
get_rss_feed_items relayed feed/keywords/items accurately).

Bug found by verification and fixed: feed titles showed the URL host —
the TEXT-events append was gated on inItem, so the channel title read
empty. All fixes rebuilt + reinstalled + re-verified.

Parked owner decisions: ffmpeg decoder extension licensing (AC3/DTS
gap); the release lane. ALL Android work remains UNCOMMITTED (whole
android/ tree untracked) — committing is the owner's call.

## 14. IPTV covers loading study (2026-11-15)

Owner ask: "improve how the IPTV covers area loads". Scope = the artwork
lane of the Live TV screen (poster grid + list rows): `rememberArtwork`
(IPTVScreen.kt:384), `Metadata.IptvOrgLogos` / `Metadata.Covers`
(Metadata.kt), the Coil `AsyncImage` painting, and their caches. The
channel-list lane (OOM-proof M3U streaming, 1.3.6) is OUT of scope.

### How it loads today

Each visible cell/row runs its OWN `LaunchedEffect` →
`withContext(Dispatchers.IO)` → `logoResolver.logoFor(name)` for live
channels / `covers.cover(title,…)` for local files + VOD groups. `Covers`
serializes every provider chain behind ONE lock with a hard 250 ms sleep
between lookups (Metadata.kt:167-180); the resolved URL goes to a plain
Coil `AsyncImage` (default loader). Playlist `tvg-logo` wins when present.

### Verified findings (evidence: live fetches 2026-11-15 + code)

* **F1 — the keyless channel-logo lane is 100 % BROKEN against today's
  iptv-org API (HIGH, correctness).** The Android parser reads
  `name`/`logo` fields (Metadata.kt:63-67). The LIVE
  `api/logos.json` (fetched today: 5.8 MB, 35,711 entries) has ZERO
  occurrences of either key — the schema is now
  `{channel:"002RadioTV.do", url, in_use, format, width, height, feed}`.
  So `obtain()` builds an EMPTY map: every live channel without a
  playlist `tvg-logo` resolves nothing, and the 5.8 MB file is still
  re-downloaded weekly. The DESKTOP already handles the new schema
  (iptv/metadata.py::IptvOrgLogos — joins logos.json with channels.json
  (8 MB, 31,843 entries) into a name→URL index, `_logo_score` prefers
  raster+in_use+main-feed, `CACHE_VERSION` invalidates stale indexes) —
  the Android port predates the change. Bonus bug: the
  `coerceAtMost(6000)` cap (Metadata.kt:63) would keep only 17 % of the
  DB even after a schema fix.
* **F2 — cover disk cache is NEVER written (HIGH, perf).** `Covers.flush()`
  (Metadata.kt:145) has NO call site anywhere. `cover_cache.json` is read
  (init) but never written, so the class doc's "survives restarts" is
  false: EVERY app restart re-resolves every visible cover from scratch.
  Combined with F3's 4 lookups/s ceiling, a screenful of ~24 VOD cells
  refills in ~15-25 s on every launch. (Image BYTES are fine — Coil 2.6's
  default loader memory+disk-caches them; the URL RESOLUTION is what
  re-runs.)
* **F3 — serialized lookups park IO threads, no priority/order (HIGH,
  architecture).** One unfair `synchronized` + `Thread.sleep(250)` for
  ALL providers (TMDb/TVmaze included, which need no such spacing —
  desktop gates politeness with a 5/s token bucket and 16 workers; the
  250 ms note in the code is TPDB-specific, applied globally by the
  port). Consequences: ~4 lookups/s ceiling; each unresolved visible
  cell parks a Dispatchers.IO thread (a VOD screenful = 20-40 parked
  threads); no FIFO (synchronized is unfair → cells resolve out of
  order); no visible-first priority; scroll-past cells keep their
  orphans running inside the lock (LaunchedEffect cancellation can't
  interrupt synchronized/sleep).
* **F4 — caches are rebuilt on EVERY tab switch (MED).** AppRoot hosts
  screens in a `when` — leaving Live TV DISPOSES IPTVScreen, and
  `remember { Covers(...) }` / `remember { IptvOrgLogos(...) }` die with
  it: in-memory cover cache + the parsed logos index are dropped each
  time. Re-entering re-parses cover_cache.json ON THE MAIN THREAD
  (`init { cache.putAll(diskCache) }` forces the lazy during
  composition — latent jank today only because the file never exists,
  becomes real the moment flush is wired) and re-parses the 5.8 MB logos
  file cache under the loadLock before the first logo can resolve.
* **F5 — no loading/loaded distinction (MED, UX).** While a cover
  resolves, the cell shows the SAME fallback icon as "no artwork"; the
  image then pops in (no crossfade, no Coil placeholder). Posters
  (2:3) letterbox inside square `aspectRatio(1f)` + `Fit` cells —
  wasted area vs the desktop poster-grid look. No prefetch of the next
  screenful.
* **F6 — negative results are permanent and conflate failure with miss
  (LOW, hygiene).** `lookup` returns null on network failure AND on a
  genuine no-match; both are cached "" forever (in memory today; on disk
  too once flush is wired) — a launch during no-network would poison
  titles for life. Desktop explicitly separates "index unavailable ≠
  miss" (backoff 5 min, no memoize) and uses per-provider negative
  windows.
* **F7 — desktop fallbacks not ported (LOW).** `ChannelLogoChain`
  favicon fallbacks (Google S2 128 px, DuckDuckGo ip3) cover channels
  iptv-org doesn't know; Android has nothing behind the (broken) index.

### Improvement plan (mirrors the desktop's proven artwork design)

* **P0 — fix the iptv-org schema break** (F1): port the desktop join
  (logos.json + channels.json → name→URL index; `_logo_score`; country
  split/normalize; drop the 6000 cap; cache the built INDEX on disk with
  a version stamp so it's parsed once per week, not per process, and
  re-serve it while a refresh runs). Live-channel logos go from 0 %
  (broken) to real coverage.
* **P1 — persist + hoist the caches** (F2/F4): call `flush()` on
  Activity onStop + a debounced periodic flush (e.g. 30 s after the last
  write); load `cover_cache.json` lazily OFF the main thread; move
  Covers + IptvOrgLogos to app-scope singletons (DeepFluxApp or a
  process-wide object) so tab switches stop rebuilding them. Warm
  restart ≈ instant (URL cache hits + Coil disk cache paints images).
* **P2 — resolver actor with priorities** (F3): replace per-cell
  LaunchedEffect lookups with one shared coroutine resolver fed by a
  PRIORITY channel (visible/newest-first, desktop `_BoundedExecutor`
  semantics): bounded concurrency 4-8, a 5/s token bucket for
  TMDb/TVmaze, the 250 ms spacing kept ONLY for TPDB. Cells observe a
  `StateFlow` keyed by url/title; disposal drops interest without
  aborting shared work; same-title cells dedupe. Expected 3-5× faster
  first screenful, zero parked IO threads.
* **P3 — UX pass** (F5/F6/F7): tri-state cell art (shimmer while
  resolving → crossfade image → no-art icon only after a real miss);
  negative TTL (retry "" after ~24-48 h) and don't persist failures;
  favicon fallback chain behind the index; optional 2:3 `Crop` for
  movie/series posters vs `Fit` for channel logos; prefetch the next
  screenful at low priority.

Suggested order: P0+P1 first (small, big wins, unblock honest
measurement), then P2, then P3. All emulator verification on
deepflux_tablet per the standing rule.

Owner decision points: (a) poster cells square+Fit vs 2:3+Crop for VOD;
(b) ship the favicon fallback chain (extra keyless requests to
Google/DuckDuckGo) or stay iptv-org-only.

### §14 IMPLEMENTATION — all four phases shipped + verified same day (2026-11-15)

Resolved decision points from standing rules: (a) **Fit stays** — the
owner's explicit 1.3.7 "fit, don't crop" decision overrides §14's Crop
option; (b) **favicon chain shipped** (desktop parity default).

* **P0** — `Metadata.IptvOrgLogos` rewritten to the live schema:
  logos.json (35,711 entries; channel-id→url, `_logo_score` =
  raster+10/in_use+4/no-feed+2) JOINED with channels.json (31,843;
  name+alt_names+country variants+id-stem) exactly like the desktop. The
  built index (72,387 keys measured) is cached as `cache/iptvorg/
  index.tsv` (fast TSV parse per launch, version-stamped header) with
  `meta.json` carrying Last-Modified strings → weekly refreshes are
  CONDITIONAL GETs (304 = zero bytes; verified the headers are stored).
  Raw pair cached in the same dir; refresh failure serves the stale
  index; nothing on disk + failure = Failed with a 5-min backoff (an
  iptv-org outage never memoizes as misses — desktop rule). SVG-only
  channel entries count as MISSES (`.svg` URL check before `?`) — Coil
  can't decode SVG, so the favicon chain supplies a PNG instead. The
  6000-entry cap is gone; the dead 1.4.0 `iptvorg_logos.json` (5.8 MB)
  is deleted on first load.
* **P1** — `Metadata.Covers` disk cache loaded LAZILY off the main
  thread (the 1.4.0 `init {}` parse-during-composition is gone), format
  v2 `{"_v":2,"e":{key:[url,ts]}}` with a 48 h TTL on misses; `flush()`
  finally has call sites: a 30 s debounce after every persisted resolve
  AND `MainActivity.onStop` (`ArtworkResolver.flushNow`). Failures are
  NEVER persisted and retry-throttle at 30 s in memory.
* **P2** — new `iptv/ArtworkResolver.kt` owned by `DeepFluxApp` (app
  singleton — caches survive tab switches; `warmup()` at app start
  loads the index + cover cache before the user reaches Live TV). Cells
  subscribe to per-key `StateFlow<State>` (Loading/Ready(url)/
  Empty(failed)); lookups run on 6 worker coroutines fed by a two-lane
  queue (visible = LIFO/newest-first, prefetch = FIFO, caps 400/200 — a
  fling can't enqueue an unbounded mobile-data sweep). Pacing: 5/s
  token bucket shared by TMDb/TVmaze/StashDB, a 250 ms min-interval
  ONLY for TPDB (`PaceGate` injectable into `Covers`); no more
  lock+sleep parking a thread per cell. Same-title/same-name cells
  dedupe onto one flow; the flow map evicts terminal entries at 2.5k;
  transient failures re-enqueue on re-compose after the 30 s backoff.
  `titleFrom` upgraded to FULL desktop `clean_title` parity (audio-ch
  layouts, bracketed release-tag groups with the bracket-safe tag set,
  parenthesized-year removal, iterative release-group suffixes, the
  desktop's broader quality list) — see verification below for why.
* **P3** — tri-state cell art: `PulsingArt` (panel-blue #1a2a4a alpha
  pulse) while resolving → `AsyncImage` with `crossfade(220)` and an
  `onError` fallback to the no-art icon (dead URLs degrade) → icon only
  after a real miss. `PrefetchAhead` warms the next 10 items at low
  priority off the LazyGrid/LazyList state in both browse modes.
* **Favicon fallback chain** (live channels after an iptv-org miss):
  domain guess from the normalized name (`skynews.com`) → Google S2
  128 px → DuckDuckGo ip3, HEAD-verified content-type (desktop
  ChannelLogoChain), paced 100 ms.

**Verified live on deepflux_tablet** (release build installed over the
owner's data, emulator rooted for cache inspection):

* Warmup at app start: logos.json (5,808,181 B — exact live size) +
  channels.json (8,001,902 B) fetched, 4.2 MB index.tsv + meta.json
  written before Live TV was ever opened; "bbcnews" resolves to a real
  PNG via exact key.
* Keyless logos on the owner's real 131k playlist: rows that 1.4.0 left
  icon-only (Discovery ID, REELZ, Oxygen True Crime, TNT Sports 1, AMC)
  render logos — via the index or the SVG-skip → favicon path.
* Cover hit rate, before/after `titleFrom` parity, same Movie VOD
  screenful: old keys like `m|Last Summer (2013) -LAMA [ ] [ ] [WEB]|2013`
  are ALL misses; the new clean keys (`m|Last Summer|2013`, `m|Fire
  Tornado|2026`, `m|Mon Ange|2016`, `m|Soldados Da Borracha|2019`,
  `m|The Whole Truth|1958`, …) are HITS — 0 → 9 posters on one screen.
* Coil disk cache grows as lookups resolve (123 → 235 files over the
  session); `cover_cache.json` written on HOME (onStop → flushNow) in
  v2 format with timestamps, misses persisted as `["", ts]`.
* Restart persistence: after force-stop + relaunch, the same Movie VOD
  screenful shows identical artwork within ~2.5 s of selection — disk
  cache + Coil cache, zero provider round-trips.
* Favicon lane: loopback playlist with "Sky News"/"CNN" (SVG-only in
  the index) + a fake channel name — all three render artwork (S2
  favicons; unknown domains get Google's default icon, desktop
  behavior).
* Nav regression (Agent → Browser → Downloads) clean; the owner's
  playlist store restored exactly after testing.

Known behavior notes: a BARE year in a VOD name ("The Matrix 1999")
stays in the query and can miss — desktop parity ("Blade Runner 2049"
protection); real providers use parenthesized years, which strip fine.
TPDB/StashDB adult lane still pending live verification (needs the
owner's XXX groups; unchanged logic from 1.4.0, now gated). versionName
still 1.4.0 — bump to 1.4.2 at release time (installer ritual
untouched).

### §14.1 — independent review round + fixes (same day)

A read-only reviewer pass over the integrated change surfaced 2 HIGH +
1 MED + 3 LOW findings; all but one fixed and re-verified:

* **Queue-eviction orphaned Loading flows (HIGH, FIXED)**: a dropped
  over-cap task left its flow `Loading` forever (flowFor only re-enqueues
  `Empty(failed)`). Dropped tasks now flip their flow to
  `Empty(failed)` + record a failure timestamp → the next compose
  re-enqueues (retry path).
* **Duplicate lazy-list keys (HIGH, FIXED — pre-existing since the
  grid landed)**: `key = { it.url }` crashes Compose
  (`IllegalArgumentException`) on playlists that list one URL under
  several names — the 131k-class target. Keys are now
  `"url|name"` everywhere (grid, list, multiview picker) — strictly more
  unique, so no regression for previously-working lists.
* **iptv-org backoff defeated under concurrency (MED, FIXED)**: the
  `unavailableUntil` check ran outside the mutex, so at cold start
  against an outage all 6 workers each ran a full timeout-bound retry
  round. Now re-checked inside `loadMu.withLock`, and the field is
  `@Volatile`.
* **failures map unbounded (LOW, FIXED)**: capped at 4000 like
  `Covers.failures`.
* **multiview `slotsVersion` never read (LOW, FIXED — pre-existing)**:
  Compose invalidation is reader-based; the counter was only written, so
  the long-press CLEAR path left the tile on screen. A `key(slotsVersion)`
  read now invalidates the grid scope.
* **In-process weekly refresh never happens (LOW, DOCUMENTED)**: an
  in-memory index is kept for the process lifetime; the weekly refresh
  re-runs on the next cold start (kdoc states the deviation — on Android
  that is within the day; a synchronous 14 MB refresh mid-lookup would
  be worse).
* Reviewer also verified CLEAN (quoted for future reference): the
  worker loop has no deadlock/lost-signal; the non-local `return`
  inside `Mutex.withLock`/`synchronized` inline blocks is monitor-safe;
  no suspend entry point is reachable from the main thread; the logo
  join/split/normalize/score logic matches iptv/metadata.py exactly.
* Re-verified after the fixes: Live TV + Movie VOD render identically
  (same artwork, instant from cache), app healthy.

### §14.2 — Agent chat clear button (owner request 2026-11-15, same build)

`ChatSession.clear()` stops any running turn, empties the bubbles and
persists an empty history (a restart does not resurrect the chat); the
chat screen gained a 🗑 overlay button at the top-right of the message
area, visible only while a conversation exists — the banner idle state
returns after clearing. Emulator-verified end to end: tap → bubbles
gone → banner + version label back → force-stop + relaunch still empty
(the persisted history is gone). UI-dump gotcha: uiautomator escapes
emoji as decimal entities (🗑 = `&#128465;`) and the button sits
partially off the right edge (align TopEnd) — both cost a detour.

**v1.4.2 DEPLOYED 2026-11-15 late** (versionCode 18, 86,340,451 bytes =
82.3 MB, sha256 533359b4…99209; deployment deep-flux-74eed727k-timedivision):
the §14 covers rework + review fixes + the agent-chat clear button, owner-
confirmed working ("movie posters working well") before the deploy.
Version-note pick: the owner chose 1.4.2 over 1.4.1 (matches the §14.x
section numbering; 1.4.1 was never shipped). Verified live: feed 200 with
version 1.4.2 + matching sha/size, APK 200 Content-Length 86340451, the
DEPLOYED bytes' sha256 equals the build's, old 1.4.0 APK 404, EN+ZH pages
carry the new href+sha (4 refs each), inline scripts node --check clean.

---

## §15 — Windows-parity work + two review rounds (2026-11-23/24, UNRELEASED)

Source-verified parity review of v1.4.12 against the Windows app, then
implementation of everything recommended, in the suggested order, plus two
independent review rounds over the result. All of it is in the working
tree, not committed, version not bumped, not deployed. No emulator on this
machine: unit-tested and compiled (`assembleDebug` builds; 285 tests, 0
failures, 4 host-dependent skips) but NOT run on a device.

Durable rules live in AGENTS.md → "Android parity + review rounds
(2026-11-23/24)". What was delivered, by area:

- Fix-first (review §1): Xtream live-stream API, playlist kinds, EPG
  channel matching + XMLTV offsets + boot restore, ad-block sub-resources,
  tab cap, renderer-crash recovery, bridge tokens, Stop that really stops,
  Clear that clears the context, untrusted tool results, agent URL policy,
  HTTP download policy/naming, service routing (Downloads tab, Pause/Resume
  all), updater honesty, backup allow-lists, confirmations for destructive
  taps and page-initiated adds, Commander sizes/search/back, PublicUrls.
- Review round 1 (R1–R8): file-name traversal, chat turn isolation, EPG
  cache per guide, service start/locks/notices, RSS link policy, torrent
  repository hardening (sequencing, cancel suppression, capped import,
  save race, engine start retry), browser bridge/crash/scan guards,
  FileProvider scope, smaller items.
- Review round 2 (R10): service start follows activity (restored torrents
  and the HTTP lane), EPG loader can no longer strand + parser bounded,
  "Resume all" notice survives the process, updater supersession, add-flow
  (user requests never displaced, page cap), HTTP pause-all/name repair,
  per-tab downloads + agent-tab LAN block + single-claim bridge, RSS feed
  address policy + seen-on-success, symbolic-link defence end to end, chat
  residuals.
- Features: IPTV sections + per-source content override + search; player
  basics (focus, buffering, immersive, rotation/aspect/audio track);
  markdown chat with tappable links.

Drafted with tests but NOT wired: `LlmPresets` (provider/model/effort),
`SessionStore` (tasks sidebar). Not started: Commander copy/move + new
folder, segmented HTTP + rate limits, RSS background polling (WorkManager),
browser tab persistence, Site Grabber / agent video lane, Xtream VOD +
series, metadata lanes, Help/User Guide, icon/bottom-bar polish.

Owner decisions 2026-11-24 ("yes, yes, make it work"): page-initiated
plain-file downloads stay unconfirmed (rate-capped only); `scan_page` may
read a LAN page the user opened themselves; the "Save location" row is
now REAL (below).

**1.4.13 TEST BUILD ON SITE 2026-11-24** (owner: "deploy to the website so
that I can test it"): `assembleRelease` with the upload keystore,
versionCode 29 / 1.4.13, 58,324,672 bytes, sha256 383ae248…a8c47, signer
cert identical to the deployed 1.4.12 (installs over it), R8 reported no
missing classes, same single-dex layout as 1.4.12. Live as
`/deepflux/DeepFlux-1.4.13-Android.apk` (200, live sha256 equals the
build's; `/api/download?f=` 302s to it). DELIBERATELY NOT a release: page
buttons and `latest-android.json` stay on 1.4.12 until the owner confirms
on a device — the pre-deploy emulator smoke could not run here.

**SAVE LOCATION MADE REAL (2026-11-24, same evening)** —
`dlmgr/FinishedExport.kt`: finished downloads are copied into the folder
picked in Settings (a SAF tree URI; a torrent's own sub-folders kept,
never overwrites, no half files, a persisted-write-permission gate, `..`
never leaves the folder) or, with none chosen, to the public Downloads
collection as before (video files only for torrents). FOUR independent
review passes (two rounds, correctness + security each) shaped the final
rules: a folder the app can no longer open counts as none (Downloads,
never "copied nowhere"); only a file the provider has just made is
written to; a failed copy is told in the app AND by a system
notification; both lanes run on one guarded pool; each folder is listed
once per job; the download service and the CPU stay until the copy is
done — and let go of a copy that hangs; the file type comes from the
file's own extension. 42 JVM tests through a real
`DocumentsProvider` under Robolectric (1 skips on this Windows host),
47 mutations teeth-checked against the final code. The test
file on the site was REBUILT from this source (58325596 bytes,
sha256 ae44530c10f1da51cac255b2251de83688bc7d2c3173316e0d598381d9cb3b42), superseding the first build. Committed on branch
`android-1.4.13`, one commit ahead of `svp-out-of-process` (not pushed).
Rules: AGENTS.md "Android parity +
review rounds" → SAVE LOCATION.

**RELEASED (2026-11-24)** — the owner said "release": both pages
(EN + ZH) and `latest-android.json` now point at the 1.4.13 APK
(58,325,596 bytes, sha256 ae44530c…b3b42, versionCode 29), the 1.4.12
APK is gone from the deploy dir, 19 live checks passed (feed field for
field, pages, APK downloaded back, counter route, Windows feed
untouched). The owner had been told the two gaps before saying so: the
build never ran on a device or emulator, and 5 unit tests only run on
Linux — no CI job runs the Android unit tests at all.

**RUN ON THE EMULATOR, same evening** — the owner pointed out that
ZCode ships an Android-emulator plugin. It could not see the SDK on this
machine; with the emulator package and an Android 15 image added to that
SDK, the published 1.4.13 APK ran end to end on a Pixel 6 profile:
every Save-location path, background downloads with their service and
notifications, the torrent lane fed by a seeder on this PC, the player,
Live TV sections and search, the browser, and the in-app update
1.4.12 → 1.4.13 — zero crashes, zero ANRs. It produced four small
findings for 1.4.14 (a refused link-torrent says "not a valid file";
file-added torrents are listed by info-hash; "-1%" in the background
notification; the app is not reopened after an update) and settled the
symbolic-link question. Details and the emulator recipe: `AGENTS.md`,
end of the "Android parity + review rounds" section.

**1.4.14 TEST BUILD (2026-11-25)** — the owner: "keep polishing what you
find that needs polish … deploy to the website, I will test on a real
phone". A second emulator pass on 1.4.13 found eight issues; two
independent reviews (correctness, security) and two more emulator rounds
found as many again in the fixes themselves — each one caught before it
shipped: a kept-torrent file that was no torrent while every unit test
was green; a label fix that hid the label at the normal font size; an
announcement rule that re-announced a re-checked torrent; a Play button
lost on finished videos; a Remove that could be cancelled by a rename.
What it fixes: a magnet's metadata is kept (a finished magnet torrent is
playable after a restart with nobody seeding), real names, refusals that
say why, no "Download finished" and no second copy at every launch (the
engine re-reports "finished" at each start), no numbered copies in
Downloads, the notification's wording, the install consent before the
installer, Open for finished files, a feed item's refusal reported, the
tab label that shrinks instead of being cut, and a failed settings save
that no longer ends the process. A third pass — a review of this
version's own changes, then the emulator — added: a torrent restored
from the kept file is told the magnet's own peer addresses again (a
trackerless magnet had none after a restart), the "copied" mark that
comes off a still-downloading torrent is saved at once (a failed
copy is retried at the next start), names read from the saved list
are cleaned, a kept file the engine refuses is written over when the
metadata arrives, and "Update now" starts the installer again after
one that was cancelled. A fourth pass began with a magnet in the v1
form — how sites link torrents — and found that a hybrid torrent
(v1 and v2 hash) was known at a start only by the hash the engine
names it by: its own kept metadata deleted at every start, no
"Download finished", a copy at every launch. It is recognised by
either of its hashes now, and everything the engine reports is
taken to the entry's key. Two independent reviews of all of it
followed — kept files the first test build had left under another
hash are adopted at a start, an update message that reached no one is
held until the app can say it, a double tap on "Update now" no longer
stacks two installer screens, a magnet's peer addresses are capped,
a copy mark can no longer outlive its entry; narrow cases are written
down in AGENTS.md. The system's own exit records then showed a native
crash no check had caught: at a torrent's finish the app read the
finished alert's handle after the callback had returned — the binding
does not own that handle. The files are read from the session now.
 423 unit tests (5 skip on a
Windows host), 119 mutations each fail their own test, zero
crashes and zero ANRs on the emulator. versionCode 30,
58359944 bytes, sha256 41251d18ac73a0b6bd3d8d817a8d8d00d91cb300e15b14d99d2f0df2aae29588, signer as before. Live as
`/deepflux/DeepFlux-1.4.14-Android.apk`, DELIBERATELY UNLINKED: the
pages and `latest-android.json` stay on 1.4.13 until the owner's word.
Rules: `AGENTS.md`, end of the "Android parity + review rounds"
section.
