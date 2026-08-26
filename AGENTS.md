# DeepFlux — Agent Notes

## Run / stop the app
- Run: `python main.py` (GUI is default; `--cli` for the REPL).
- `kill_shell` does NOT terminate the GUI process — it detaches and keeps
  running. To stop it, kill the python process itself:
  `Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*main.py*' } | Stop-Process -Force`
- Closing the window (X) quits the app — no minimize-to-tray. Graceful
  shutdown (saves fast-resume data + config) happens via X, File → Exit, or
  tray → Quit; force-killing skips it. The tray icon exists only while the
  app runs (Show/Quit menu + completion toasts).

## Verify changes
- Quick check: `python -c "import ast; ast.parse(open(<file>, encoding='utf-8').read())"`
- Tests: `python -m pytest tests/ -v`
- Tests must NEVER write the real `~/.deeptorrent/config.json`: patch
  `DeeptorrentConfig.default_config_path` (or pass explicit paths) whenever
  code under test can persist — `IRCTab.shutdown` and the RSS feed tools both
  call `config.to_file(default_config_path())`. An unpatched GUI-tab test once
  wiped a user's real config (sources + API keys) with test defaults.

## Build / package
- App bundle: `python -m PyInstaller packaging/app.spec --clean --noconfirm`
  → `dist/DeepFlux/` (onedir; launcher `DeepFlux.exe` + `_internal/`).
- app.spec must `collect_all` every package that ships native extensions or
  `importlib.resources` data — currently yt_dlp, curl_cffi, ddgs, primp,
  fake_useragent (ddgs' DDG engine loads `browsers.jsonl`; missing data =
  "Failed to load or parse browsers.json" and DDG search silently dies in the
  frozen build), irc (codes.txt).
- Installer: `"$LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging/installer.iss`
  (Inno 7.0.2 also installed at `C:\Program Files\Inno Setup 7\`). Reads
  `dist/DeepFlux/*` and writes `dist/DeepFlux3.0.1Setup.exe` — `BuildDir` in
  installer.iss is relative to the script. The old `Documents\DeePFlux`
  output folder is no longer used.
- Logo: every derived asset (icon.ico, logo_48, wizard images, extension
  icons, website webp) is generated from the root `DeepFlux<version>.png` by
  `python packaging/gen_logo_assets.py` — update that script's source
  filename on a version bump and re-run it, never hand-edit the outputs.
- Website deploy: `website/` is a Vercel-linked project (`deep-flux`,
  aliased to deepflux.space; root index.html redirects to /deepflux/).
  Publish = copy the new `DeepFlux<X>.Setup.exe` into `website/deepflux/`,
  update index.html (title/href/meta date; there is no h1 any more — the logo is the top element), delete the old setup exe,
  then `vercel --prod --yes` from `website/`. Only files in the dir are
  deployed — removing the old exe from the dir removes it from production.
  Vercel Web Analytics is enabled on the project (dashboard toggle or CLI ≥59
  `vercel project web-analytics`) — the static page carries the
  `/_vercel/insights/script.js` tag; don't remove it. If the CLI reports
  "No existing credentials found", `vercel login` (device-code flow, user
  clicks Verify in the browser) fixes it — non-interactive shells work.
  Version bumps touch: installer.iss (MyAppVersion + OutputBaseFilename),
  main_window title, help_dialog (H1/Version/About), opensubtitles UA,
  website index.html.

## Conventions
- Settings backup: File → Export/Import Settings (`infra/config_backup.py`)
  serializes the whole live config (`asdict`) into a passphrase-encrypted
  `.dfc` — JSON envelope {magic `deepflux-settings`, PBKDF2-SHA256
  salt/iterations} around a Fernet token; wrong passphrase/tamper fail closed
  with `SettingsBackupError`. Import backs up the live config to
  `config.json.bak`, writes the decrypted payload to the config path, then
  restarts via `QProcess.startDetached` — `_skip_config_save` must stop
  `closeEvent` from re-saving the OLD in-memory config over the import.
  `cryptography` ships a Rust native ext → app.spec `collect_all`s it.
- Windows subprocess spawns must pass `creationflags=CREATE_NO_WINDOW`
  (see `dlmgr/ffmpeg.py`) or a console window flashes in the windowed build.
- Qt QSS does not support `text-transform`/`letter-spacing` — use
  `QFont.setCapitalization/setLetterSpacing` instead (see neon tab bar in
  `gui/main_window.py`).
- Splitter `setSizes()` before window show is overridden by size hints —
  enforce default ratios in `MainWindow.showEvent`.
- `main_tabs.tabBar()` is hidden for good (navigation lives in the menus) —
  never `setVisible(True)` it. Chrome-restoring code (e.g. IPTV's
  `_on_player_fullscreen`, run on every non-fullscreen window-state change
  via `MainWindow.changeEvent`) must skip it, or an empty ~21px strip shows
  up under the menu bar after a fullscreen/maximize cycle.
- No built-in torrent sources ship: `DEFAULT_SOURCES` in `config.py` is
  empty, and the installer deletes `~/.deeptorrent/config.json` on EVERY
  install — fresh installs and upgrades both start with zero sources, users
  add their own or fetch from Jackett. `DEFAULT_SOURCE_POPULARITY` still
  ranks user-added sources by `id`. If a built-in source is ever added back
  to `DEFAULT_SOURCES`, `from_file` merges it into existing user configs
  (match by `id`, Jackett-style).
- Jackett service + source sync live in `infra/jackett.py`: at startup (GUI
  background thread + CLI) and then hourly, `maybe_auto_sync` pings Jackett
  (`t=caps`), starts it when down (`indexer.auto_start`, default on —
  `sc.exe start Jackett` service first (needs elevation; usually denied),
  then JackettConsole.exe (serves in-process) / JackettTray.exe from
  `indexer.jackett_path` or standard install dirs incl. `%ProgramData%\Jackett`),
  and re-fetches the
  sources list when stale (>24h, `sources.last_jackett_fetch`) or EMPTY.
  `maybe_auto_sync(force=True)` skips the daily gate — wired to Config →
  Jackett Settings: OK forces an immediate sync; a successful Test
  Connection (button lives in File → API Keys under the Jackett key) fetches
  with the TYPED (unsaved) key + saved URL/path via a probe config
  (`APIKeysDialog._jkt_fetch_worker`) and merges into the live config;
  a forced run that reaches Jackett but fetches nothing reports
  `sync_failed` so the GUI can warn.
  Merge policy everywhere (auto-sync AND the manual "Fetch from Jackett"
  button — both share `fetch_indexers`/`merge_sources`): newly fetched
  indexers start ENABLED (configured-in-Jackett = meant to be searched);
  sources the user explicitly disabled keep their state across re-fetches.
  Results
  surface in the Agent tab as a dim `jackett_sync` event (unreachable
  notice shown once per session).

## LLM subsystem (agent/)
- `llm.py` — OpenAI-compatible client (`DeepSeekClient` by name, talks to any
  provider in `LLM_PROVIDER_PRESETS` in config.py: deepseek / openrouter /
  custom): retries transient failures (429/5xx/network, 3 attempts, backoff);
  streams SSE when given `on_delta(kind, text)`; captures `reasoning_content`
  (DeepSeek) or `reasoning` (OpenRouter) into `LLMMessage.reasoning`.
  `reasoning_effort` is sent ONLY when provider == "deepseek" — other
  endpoints may 400 on it. Per-call `model=` override: summaries use
  `config.llm.fast_model` + effort "low"; planning uses `model` +
  `reasoning_effort` from config. The Provider combo + Base URL live in the
  API Keys window (File → API Keys) — it swaps base URL presets on switch
  (user-typed custom URLs survive; preset values follow). Models are locked
  (deepseek-v4-pro / deepseek-v4-flash). There is NO AI Settings dialog:
  stream / memory / agent-debug are assumed always-on — `from_file` forces
  `llm.stream=True`, `llm.memory_enabled=True`, `ui_agent_debug=True`
  regardless of what's saved; `reasoning_effort` stays a config.json-only
  knob (default "high"). `DEEPSEEK_API_KEY` env injection is gated on
  provider deepseek.
- `memory.py` — OpenClaw-style persistent memory in `~/.deeptorrent/memory/`
  (`USER.md`, `MEMORY.md`, `daily/YYYY-MM-DD.md`). Curated files are injected
  into the system prompt (budgeted); daily notes are search-only. LLM tools:
  `save_memory` (scopes user/fact/note, deduped) + `search_memory`.
- `loop.py` — bounded history: `_pruned_history` caps at
  `llm.history_budget` msgs and only cuts at safe boundaries (never orphans
  a tool message from its assistant tool_calls). Streaming emits
  `stream_delta` events; GUI replaces the raw stream with the rendered
  bubble on finish (`_seal_stream` on tool turns).
- `search_indexers` has `deep` param: default quick pass = private tier +
  top-1 public source; zero hits auto-escalate to the full sweep. The GUI
  never passes `deep=True` directly — only the agent does; the Agent tab's
  Search button is a pure web sweep (`WebSearchClient`), not `search_indexers`.
  Jackett reliability rules: per-indexer failures surface in the result's
  `errors` list (a timeout is never silently "0 hits"); an indexer that errors
  is dead-marked and skipped for the remaining query variants + auto deep
  sweep; when EVERY queried indexer fails, the tool returns
  `success: False` + error (no web fallback, never cached) so the agent
  retries instead of reporting "no results". Results are paged: the 5-min
  cache holds the FULL ranked set, the agent gets `SEARCH_PAGE_SIZE` (40)
  rows per call and pages with `offset` (served from cache — no new indexer
  queries), so a deep sweep can never overflow the LLM context.
- Web search fan-out: `WebSearchClient.search` queries ALL configured
  providers in parallel — DuckDuckGo (`ddgs` package, keyless) always, Brave
  when `web_search.brave_api_key` set, Perplexity when `web_search.api_key`
  set (keyed providers are skipped entirely without a key) — then merges
  results deduped by URL (provider order preserved, capped at limit*2) and
  returns `providers` (backends that contributed). Perplexity's synthesized
  answer surfaces as a top-level `answer` field (magnets inside it are
  extracted too). Bulk internal sweeps (per-source `site:` queries in
  `_search_source_tier`, query-variant web fallback in `_search_web_sources`)
  pass `llm=False` to skip Perplexity so they don't burn paid quota — only
  the agent's `web_search` tool, the Agent-tab Search button, and
  `_find_alt_trackers` use the full fan-out. Brave free tier is 1 qps —
  `WebSearchClient` throttles to 1.0s spacing between searches when a Brave
  key exists (0.2s otherwise). (Scraped-Google stage tried & dropped:
  anonymous requests get JS-only shell pages with zero parsable results —
  2026 Google.)
- NO built-in/shared API keys, by design: every key field in `config.py`
  defaults to empty and `from_file` must not inject fallbacks — only the
  user's own env vars (`DEEPSEEK_API_KEY`, `JACKETT_API_KEY`,
  `BRAVE_API_KEY`) fill empty slots. Without an LLM key the GUI/CLI run the
  agent in dummy mode; without TMDb/Jackett keys those integrations degrade
  gracefully. The system prompt fixes the agent's identity as "DeepFlux"
  (never "DeepTorrent" — that name only survives in legacy paths).
- Adding a tool: register schema+handler in `tools.py`, then classify it in
  `loop.py` (`READ_ONLY_TOOLS` run concurrently / `DESTRUCTIVE_TOOLS` need
  confirmation) and add a GUI label in `main_window._TOOL_LABELS`.
- `add_download` submits direct file/HLS/DASH URLs to the dlmgr DownloadEngine.
  ToolRegistry takes `dl_engine=` (GUI injects the real one; CLI lazily creates
  its own). The queue is fully agent-managed: `list_downloads` (READ_ONLY),
  `pause/resume/retry/remove_download` (plain; job ids accept unique prefixes —
  `_resolve_dl_job`), `cancel_download` (DESTRUCTIVE — deletes the partial file
  by default). `web_fetch` extracts `magnets`, `torrent_urls`, and
  `download_links` (file/stream hrefs, resolved with urljoin) from raw HTML.
- Torrent session tools: `set_torrent_rate_limits` / `set_sequential_download`
  / `force_recheck` / `force_reannounce` (all plain — runtime, reversible).
  force_recheck/force_reannounce are engine methods added via the `_enqueue`
  serial-thread pattern (see `engine/torrent_engine.py::_force_recheck`).
- Stream-while-downloading (GUI "Stream" action, `_stream_torrent` /
  `_on_stream_status` in main_window.py): sequential download + mpv reads the
  growing file once a buffer threshold is met. The threshold is ADAPTIVE:
  per-file rate (EMA) + duration probed from the partial file
  (`dlmgr/ffmpeg.py::probe_duration` — ffprobe if present, else `ffmpeg -i`
  stderr Duration parse since only ffmpeg.exe ships in `packaging/ffmpeg/`;
  `find_ffmpeg` also checks repo `packaging/ffmpeg/` so dev runs match the
  frozen build) feed buffer = (bitrate − rate/1.25) × duration; slow
  connections grow the buffer (announced once as a 🐢 event), fast ones keep
  the fixed min(32MB, max(4MB, size/100)). duration=-1 = no ffmpeg → fixed
  threshold; non-faststart partial MP4 probes 0 and retries as data doubles.
  The deadline tracks the buffer ETA (rate-based) instead of a flat 120s.
- Anti-artifact frontier capping: libtorrent writes unverified blocks to
  disk and pre-allocation zero-fills the rest — raw `file_progress` counts
  pieces ANYWHERE (gaps), and mpv reading past the verified frontier decodes
  garbage. So: buffer thresholds gate on `engine.get_file_prefix` (gapless
  verified prefix via `have_piece` walk; status fetch attaches
  `_stream_prefix`), and during partial playback `_register_stream_playback`
  + 1s tick cap mpv's `end` 2MB before the frontier (`set_playback_end` on
  the backend, mpv-only) with auto-resume when the cap moves, plus
  `engine.set_stream_window` piece deadlines (top priority, 2s) for the 64MB
  after the playhead. Cap lifts on completion / when the player moves on.
- Player track switching: PlayerWidget has 🎧/CC menu buttons (+ J/# cycle
  keys) over `backend.audio_tracks/subtitle_tracks`. mpv `aid`/`sid` take
  the track **id** (not track-list index); VLC normalizes
  `audio_get_track_description`/`video_get_spu_description` tuples and
  disables subs with -1.
- The mpv backend MUST use `vo="gpu-next"` (libplacebo), with a `vo="gpu"`
  fallback if init fails (`MpvBackend.create`). Legacy vo=gpu never
  processes Dolby Vision: P5 files (no HDR10 base layer — most streaming DV)
  render with the classic washed-out purple/green cast; P8.x silently drops
  to the static HDR10 grade. VERIFIED by rendering Dolby's official P5
  sample (`DolbyLaboratories/dolby-vision-contents` repo, Git LFS) through
  both vos on this machine: gpu = wrong colors, gpu-next = correct. DV
  metadata flows through d3d11 zero-copy hwdec fine; `interpolation`,
  `tscale`, `video-sync` and `video_zoom` (overscan) are all vo-independent
  or libplacebo-native and were regression-tested on the switch. VLC has no
  real DV support (HDR10 base layer at best) — mpv is the DV path.
- Playback smoothness: `video-sync=display-resample` kills fps/refresh
  mismatch judder and `config.iptv.interpolation` (Play → Playback, "Smooth
  motion", default ON) adds mpv `interpolation` + `tscale=oversample`.
  BOTH are for files/VOD only — `PlayerWidget.play` calls
  `backend.set_smooth_video(section != SECTION_LIVE)`, so LIVE TV runs on
  mpv's default `audio` sync with interpolation off: display-resample
  assumes a seekable, steadily-timestamped source and makes live streams
  stall/restart. `_apply_sync` in MpvBackend is the single place that writes
  both properties (`_smooth` gates `_want_interpolation`); all of it is a
  no-op on the VLC backend.
- SVP 4 motion interpolation (`iptv/svp.py` + `iptv/mpv_process.py`, Play →
  Playback "SVP motion interpolation", default OFF) is the ONLY way to get
  real soap-opera-effect frame synthesis — mpv's own `interpolation` cannot
  do it (see the verified limit below). NOTHING is bundled: SVP is
  proprietary (~$25 lifetime, 30-day trial, one PC per license) and we only
  cooperate with the user's own install, like SMPlayer/VLC/Plex do. No SVP
  install = greyed-out checkbox, zero behavior change.
  ARCHITECTURE, and why it must stay this way (all verified live, 2026-08):
  * SVP injects its filter chain through VapourSynth, which EMBEDS ITS OWN
    CPython 3.12. libmpv running inside DeepFlux's Python 3.11 process
    therefore CANNOT use it: mpv logs "Failed to initialize VapourSynth
    VSScript library", and calling `getVSScriptAPI` from our process
    DEADLOCKS. Putting mpv64 on PYTHONPATH breaks `import ctypes` ("Module
    use of python312.dll conflicts with this version of Python"), and
    ctypes-preloading vapoursynth.dll hard-crashes the app (exit
    0xCFFFFFFF). Do NOT try to make the in-process backend do SVP.
  * So SVP mode runs SVP's OWN `mpv.exe` (`<SVP>\mpv64\mpv.exe`, a plain C
    host) as a child process, embedded via `--wid=<surface winId>` and
    driven over mpv's JSON IPC. `--input-ipc-server=mpvpipe` is what SVP
    Manager scans for; mpv accepts several IPC clients, so SVP and DeepFlux
    share the pipe. `--no-config` keeps SVP's own mpv.conf from fighting our
    options; `--hwdec=auto-copy --hwdec-codecs=all` is mandatory
    (VapourSynth takes software frames only); `--vo=gpu-next` preserves the
    Dolby Vision behavior documented above.
  * The IPC pipe handle is SYNCHRONOUS: a blocking read on the reader thread
    also blocks writes from the GUI thread on the same handle (verified
    deadlock). `_read_loop` therefore polls `PeekNamedPipe` and reads only
    what's buffered, with ONE lock covering both directions. Never go back
    to a blocking `read()`.
  * `svp.prepare_environment` may only touch PATH (for child processes).
    Never PYTHONPATH, never an in-process DLL preload — see above.
  * Any failure (no SVP, missing mpv component, spawn/IPC failure) falls
    back to the in-process backend in `create_backend`, so a broken SVP
    setup never costs playback. mpv's own interpolation/display-resample are
    deliberate no-ops in this backend: SVP already targets the display rate.
  * `duration` MUST be observed, not read once after `loadfile`: mpv doesn't
    know it until the file is demuxed, so a single read returns 0 and
    `PlayerWidget._on_position` then treats the media as live — grey,
    unclickable progress bar and disabled skip buttons (reported bug).
  MEASURED end-to-end: local `Mutiny (2026)` and an IPTV **VOD** stream both
  go 23.976 -> 119.88 fps with `vf=[svp]`; an IPTV **LIVE** channel goes
  25 -> 50 fps (`vf=[lavfi, svp]`) and holds steady (20.1s of playback in
  22s wall, no stall/restart) — so SVP is left enabled for live too.
  Pause/seek/tracks/duration/position callbacks all keep working.
  When a provider stream fails to open here, check the mpv log for
  `tls: IO error: Error number -10054` (WSAECONNRESET) BEFORE suspecting this
  backend: IPTV providers cap concurrent connections, so a second player
  (e.g. the main app still running) makes the provider reset the new one.
  Same URLs loaded fine once the extra connection was closed.
  VERIFIED LIMIT (2026-08, live probe of MpvBackend on this machine):
  mpv `interpolation` is frame BLENDING (smoothmotion), not motion
  synthesis — it only blends vsyncs that fall between source-frame times,
  so when video fps divides the refresh rate the blend weight is ~0 and it
  is visually INERT. This machine's display runs at 120 Hz: 23.976/24 fps
  movies (most of the library) and 30 fps hit a near-perfect 5:1/4:1
  cadence (`vsync-ratio` 5.0, `estimated-vf-fps` = container fps, 0 dropped
  frames) and play at native 24 fps motion no matter what; only non-integer
  content (25 fps → 4.8) actually gets blended. Users reporting "movies
  still look low frame rate" with everything set correctly are seeing the
  source's native 24 fps cadence — true motion interpolation (SVP /
  VapourSynth MVTools / ffmpeg `minterpolate`) is not bundled (1080p→120 is
  far from realtime on CPU).
- Channel logos (grey-tile bugs live here): `iptv/artwork.py` fetches through
  a per-thread `requests.Session` — logo CDNs (logo.m3uassets.com) reset
  connections when each image opens a fresh one (measured 45% loss at 6
  workers, ~1% with keep-alive + 3 retries at 4 workers), so never go back to
  bare `requests.get`. Payloads are validated (`_is_image_file`: magic bytes
  + PIL verify) so HTML error pages served with a 200 aren't cached forever.
  `ContentGrid` keeps `_pixmaps` (url -> QPixmap) because ~500 URLs are shared
  by ~2200 channels in a typical playlist — deduping requests by URL alone
  leaves every tile but the first grey. Dead URLs land in `_failed_urls` and
  fall back to iptv-org. That fallback (`IptvOrgLogos`) must join
  `logos.json` (keyed by channel *id*, no names, prefer raster over SVG —
  Qt here has no SVG plugin) against `channels.json` for names/alt_names;
  matching strips playlist decorations (leading channel number, country
  prefix, quality tags) — 58% -> 87% hit rate on real names. Channels with
  no logo at all get an initials tile (`_placeholder_pixmap`), not grey.
- VOD artwork: playlists put movie/series posters in `logo`, NOT `poster`
  (`poster` is 0% populated from m3u; the grid reads `logo or poster`, the
  detail panel `backdrop or poster or logo`). Entries with neither go through
  `IPTVManager.resolve_poster_async` -> `MetadataPipeline` (TMDb, TVmaze for
  series), the VOD twin of `resolve_channel_logo_async`; resolved URLs are
  written to `.poster`. That only works if `clean_title` strips scene junk —
  trailing release groups (` -LAMA`, ` -WORLD`, ` -MeGusta`) and audio
  layouts (`5.1` -> `5 1` once dots become spaces) took the TMDb hit rate on
  unmatched movies from 3% to 43%, so keep `_GROUP_SUFFIX_RE` /
  `_AUDIO_CH_RE` in the pipeline (and keep the hyphen rule space-anchored so
  "Spider-Man" and "Mission Impossible - Fallout" survive).
  VERIFIED LIVE (2026-08): TMDb `search/movie` returns ZERO results when a
  bare year sits in the query text ("Dunki 2023" → []; "Dunki" + `year=2023`
  → hit) — the year must travel in the year PARAMETER. `MetadataPipeline._worker`
  therefore derives `year` via `extract_year` when empty and cuts the query at
  the first year token (scene filenames carry only release junk after it:
  "Masters of the Universe 2026 TELESYNCx264-DKS" → query "Masters of the
  Universe"). If every movie lookup suddenly misses while series still work,
  check this first. Failed lookups negative-cache with a 6h TTL
  (`provider='none'` rows in the metadata table) — purge them after fixing a
  lookup bug or the app won't retry until the TTL expires.
- Adult VOD -> `TPDBProvider` (ThePornDB, `iptv.tpdb_api_key`, env
  `TPDB_API_KEY`). TMDb filters adult titles out of search entirely, so those
  entries can never resolve through it. `looks_adult(group, name)` routes
  them — provider order ONLY, there is no `SECTION_ADULT` and no UI gating;
  adult entries stay in Movies. Ambiguous words (`adult`, `erotic`) are
  trusted only on a group-title, never on a name, or films like "Adults in
  the Room" get diverted off TMDb.
  `/movies`, `/scenes` and `/jav` share one response shape
  (`{"data": [...]}`, art in `posters.*` / `background.*` size dicts whose
  values are often null — fall back to the flat `poster`). The API resets
  connections under rapid sequential requests, so the provider holds one
  keep-alive Session + one retry (same lesson as `iptv/artwork.py`).
  MEASURED on a 121k-entry playlist (48k adult, 3 group-titles). Key facts,
  all verified — do not re-litigate them by guessing:
  * TPDB's `q` is a loose keyword search: nonsense returns 0 rows but
    `"the and"` returns 20, so SHORT queries "match" everything. A 2-token
    query scored a 100% "hit rate" of which ~96% were WRONG posters. Every
    title-based hit must be verified.
  * Adult playlist names are STUDIO-LED, not performer-led: leading tokens
    matched a known TPDB site 96% of the time and a known performer 0%.
    That prefix is why a plain title query scores 4% — TPDB's `title` holds
    only the scene title.
  * So the primary path is `/scenes?parse=<raw name>` (TPDB's filename
    parser, what Stash uses). Parse returns rows for ~80% of entries but
    only ~half are real, so `_verify` gates every row on
    `title_containment >= 0.7` OR `site_stripped_similarity >= 0.5` — the
    latter strips the studio using the row's OWN `site.name`, costing no
    extra request. Both scored 0% false positives against a shuffled
    control.
  * Verified end-to-end on a fresh sample: JAV 93%, non-JAV 50%, weighted
    ~51% (was 7% before parse mode) — roughly 25k of 48k tiles.
  Do not "improve" recall by loosening the thresholds; that trades blank
  tiles for confidently wrong ones. The residual is covered by the frame-grab
  fallback below, not by fuzzier matching.
- Multiple sources are ALL loaded at once — there is no "switch source and
  reload" any more. `IPTVManager._playlists` was already a
  `{source_id: Playlist}` dict; `load_all_async` walks every enabled source
  SEQUENTIALLY (each is a big download + >100k-entry parse; in parallel they
  just starve whichever the user is looking at) and fires `on_done` per
  source so the first provider is usable while the rest arrive.
  `items_for` / `categories_for` / `favorites` / `recent` all take an
  optional `source_id` (None = active source, so `agent/tools.py` and older
  callers are unaffected).
  Loading must NOT claim "active": every assignment is
  `self._active_source_id = self._active_source_id or source.id`, otherwise
  the last source to finish yanks the user's selection mid-browse.
  Favourites/history are filed against the item's OWN source via
  `source_id_of` (ids are `<source_id>::<hash>` from `make_id`) — using the
  active source would misfile every entry from a non-active provider.
  Sidebar is Source > Section > Category; `_rebuild_sidebar` runs again each
  time a source lands, so it snapshots/restores expansion + selection
  (`_tree_state`) or the tree collapses under the user during startup.
  Sources still downloading are shown as "(loading…)" rather than omitted,
  and a lone source auto-expands so the extra level costs nothing.
  Movies/Series sub-group by RELEASE YEAR by default: the sidebar "Group:"
  combo (`config.iptv.vod_group_mode`, "year" default) adds year buckets
  UNDER each category (Source > Section > Category > Year) so provider
  separations like "Movie VOD" vs "XXX VOD" survive — do NOT flatten years
  directly under the section, that merges adult with regular VOD. Buckets
  come from `IPTVManager.years_by_category` (ONE pass per section — calling
  `years_for` per category re-scans the section each time and stalls the
  rebuild on 3k-category playlists), newest first, yearless entries trailing
  under `YEAR_OTHERS` = "Others". A year node key is a 4-tuple (src,
  section, category, year); `_on_tree_click` pads keys to unpack both
  shapes. Years are
  populated at load by `classify.populate_years` (regex on the name —
  measured ~100% of scene-named movies, ~1/3 of series; `_YEAR_RE` takes the
  FIRST year token, so "Blade Runner 2049" files under 2049 — accepted
  trade-off), from Xtream `get_series` `releaseDate` (free in the list
  payload), and lazily when metadata resolves (`resolve_poster_async` /
  DetailPanel write `meta["year"]` back onto the item). Old cached playlists
  are backfilled in `_playlist_from_cache` — no cache invalidation needed.
  Refresh hard-won lessons (provider connection-reset storm, 2026-08):
  a FAILED refresh keeps the previous cached playlist in memory and flags it
  `Playlist.stale` (GUI: "refresh failed — showing cached data") instead of
  blanking the source; `load_all_async` runs ONE sweep — extra calls set
  `_reload_requested` and the worker re-runs with the latest source list
  rather than downloading in parallel; `_refresh_source` dedupes per source
  (followers wait on `_refresh_events` and serve the leader's result);
  `_download_m3u` backs off 2s/5s/15s so retries ride out rate-limit windows.
- Local media folders are a 4th source kind: `kind="local_folder"`, `url` =
  directory path (source dialog: "Media folder (local)" + directory picker).
  `iptv/local_folder.py::scan_folder` walks the tree (video exts only, so
  .srt/.nfo/Screens sidecars never match; "sample" files skipped) and emits
  Channels flagged `extra["local_section"]` — the URL/extension heuristics in
  `classify_entry` misfire on filesystem paths (a local .m2ts is a movie, not
  live), so `classify()` honours the flag instead. Group (= tree category)
  collapses season dirs ("Show.S01…", "Season 01") to their parent and
  collapses a movie's same-named folder via `_same_named_dir` (exact alnum
  match, or cleaned year-less core prefix + same last token — release-renames
  like "Toy Story 5 (2026)…-LAMA/<dotted>.mkv" collapse, but a "Comedy/"
  subcategory doesn't). Nameless episode files ("S01E01.mkv") borrow the
  nearest non-season ancestor dir name so they still group into one Series.
  Posters flow through the same `resolve_poster_async` pipeline as M3U VOD
  (TMDb/TVmaze, then framegrab — which is cheap/local here); scans persist
  through the SQLite playlist cache and a refresh is just a re-scan, so new
  files appear on reload. Verified against a real Plex-style library
  (tests: `test_iptv.py -k local_folder`).
- Frame-grab poster fallback (`iptv/framegrab.py`, `iptv.framegrab_posters`,
  default on): when NO provider matches, FFmpeg pulls a frame from the stream
  itself — 100% coverage by construction. The frame is written straight into
  `ArtworkCache.full_path()` under a synthetic `framegrab:<sha1>` URL, so
  `fetch_async` serves it via `get_cached` and the existing thumbnail/decode
  path needs no special-casing.
  SAFETY: only ever runs for tiles ON SCREEN —
  `resolve_poster_async(..., allow_framegrab=True)` is passed solely from the
  non-prefetch branch of `_load_visible_artwork`. The background sweep must
  NEVER enable it: a 48k adult section would mean 48k video connections to
  the user's provider. Concurrency is capped at 2 on a dedicated pool so
  grabs can't starve ordinary artwork downloads.
  `-ss` goes BEFORE `-i` (input seek = HTTP range request, not a full
  decode); seeks 120s then 8s so short clips still yield a frame.
  MEASURED live: stream CDNs reset the TLS handshake under bursts while the
  content is perfectly fine (HEAD 200 / GET 206 on the same URL), so `_run`
  classifies stderr as transient vs permanent — transient failures are
  retried (`_RETRY_PASSES`) and NEVER negative-cached, permanent ones are
  remembered. Without that, 2/6 real grabs failed; with it, 14/14 succeeded
  (~4.3s each, 480x270 JPEG). Do not collapse that distinction back into a
  plain bool.
- Cover population is three-layered in `ContentGrid`: visible tiles resolve
  immediately, `_PREFETCH_TILES` (300) rows either side of the viewport are
  pre-downloaded (capped by `_MAX_PENDING_ARTWORK` so a fast scroll can't
  starve on-screen tiles), and a `_sweep_timer` walks `_sweep_queue` — every
  artwork-less entry in the *current section* — at `_SWEEP_BATCH`/sec, which
  deliberately matches the metadata limiter (5/s) so the queue never grows
  and on-demand lookups wait ~1s at most. Switching sections abandons the
  sweep (`set_items` clears it). `_visible_range` uses `indexAt` probes at
  several x fractions (icon-mode gaps return invalid indexes) with a
  scrollbar-ratio fallback — never a full 30k-item scan per scroll event.
  `DetailPanel.artwork_found` -> `ContentGrid.apply_external_artwork` pushes
  a poster discovered by clicking an entry back onto its tile. The sweep is
  slow ON PURPOSE (rate limited); `ContentGrid.sweep_progress` drives the
  "Finding artwork n/N" bar in the status row so that's visible — prefer
  showing progress over adding pressure to the API limits.
- Three non-obvious perf constraints in that grid, each found by measuring;
  don't undo them:
  1. `ArtworkCache` runs `_MAX_WORKERS = 12`. Measured on real playlists:
     4 workers = 5-9 img/s, 8 = 14-18, 12 = 16-25, 16+ negligible. With 4
     retries + jittered backoff, 12 workers download 200/200 clean.
  2. `_BoundedExecutor` is a PRIORITY queue, newest first
     (`PRIORITY_VISIBLE` / `PRIORITY_PREFETCH`). With a plain FIFO, scrolling
     buries the on-screen tiles behind ~1200 stale prefetches — measured
     decorated tiles collapsing from 520 to 43 while scrolling.
  3. Icons exist only near the viewport. A 120x160 QPixmap is ~77 KB and
     every decorated tile holds one, so per-name initials for a whole
     section measured 225 MB / 6000 tiles. `set_items` assigns one SHARED
     `_neutral_pixmap`, `_release_far_icons` reverts distant tiles to it, and
     `_pixmaps` is an LRU (`_MAX_CACHED_PIXMAPS`). Steady state is ~370 MB
     scrolling a 16k-movie section. Pixmaps are also downscaled to
     `_TILE_SIZE` on apply (3x saving; tiles never draw bigger).
  Decoding is ~1.5 ms/image and a warm-cache scan applies hundreds at once,
  so `_apply_artwork` queues into `_decode_queue` and `_decode_step` does
  `_DECODE_BATCH` per event-loop turn (a 600-tile scan was a 300 ms freeze
  before). Tests that call `_apply_artwork` directly must call
  `_decode_step()` to see the icon land.
- Audio files play with a MilkDrop visualization instead of a black rectangle:
  `gui/milkdrop.py::MilkdropBackend` is a third PlayerBackend that renders
  Butterchurn (MilkDrop 2 in WebGL) in a `QWebEngineView` over the player
  surface. The PAGE plays the audio (`packaging/milkdrop/visualizer.html`,
  `<audio>` -> WebAudio -> butterchurn) because the visualizer can only
  analyse a WebAudio node and mpv's output isn't reachable from JS. So
  `PlayerWidget` keeps three backends: `_media_backend` (mpv/VLC),
  `_milkdrop`, and `_backend` = whichever is active (`_use_backend` swaps
  them and shows the right `video_stack` page + the 🌀 preset button).
  CRITICAL: the web view is a PAGE OF `video_stack`, a SIBLING of the mpv
  surface — never a child of it. A QWebEngineView is a native window, so
  parenting one under the surface makes Qt re-create the surface's HWND;
  mpv loses the window it embedded into and its core dies ("libmpv core has
  been shutdown"), which showed up as mp4 audio playing over a black screen
  after an mp3. Measured: as a child the winId changes, as a stack sibling it
  is stable across repeated swaps. `MilkdropBackend.create()` therefore builds
  the view PARENTLESS and the host inserts it into the stack.
  Routing is by extension — `is_audio_url` lists only what Chromium decodes
  (mp3/m4a/aac/flac/ogg/opus/wav); wma/alac/ape and every stream stay on mpv.
  If Chromium still fails, the page reports `media error` and the backend
  fires `on_unsupported`, which replays via `play(item, allow_milkdrop=False)`.
  Everything is vendored and offline (verified by logging every page request:
  all `file://`): `packaging/milkdrop/butterchurn.min.js` +
  `milkdrop-preset-converter.min.js` — despite the upstream package being
  named `-aws`, the HLSL->GLSL conversion runs locally in `glsl-optimizer-js`.
  Presets are plain `.milk` files in `MilkDrop/` (shipped) and
  `~/.deeptorrent/presets` (user); both dirs go through `list_presets()`, and
  app.spec ships `MilkDrop/` + `packaging/milkdrop/`. Conversion is async and
  cached per session in the page.
  VLC's own projectM plugin was evaluated and REJECTED: on Windows it loads
  and then immediately unloads (`using module "projectm"` ->
  `removing module "projectm"`, textures looked up under a Linux build path),
  both embedded and in standalone VLC. Don't retry it.
  NOTE: python-mpv's `m["x"]` is an OPTION, `m.x` is a PROPERTY — a test
  reading `m["width"]` fails with "property does not exist".
- OpenSubtitles (`iptv/opensubtitles.py`): REST v1 client
  (api.opensubtitles.com/api/v1) — `Api-Key` + UA "DeepFlux v3.0.1" headers;
  hash search (classic size + 64KiB head/tail sum) first for local files,
  title query fallback; download is two-step (POST /download {file_id} →
  short-lived link → GET). 406 = daily quota (suggest account creds);
  optional username/password login → Bearer JWT raises it. Keys live in
  `config.iptv.opensubtitles_*` (env `OPENSUBTITLES_API_KEY` fills an empty
  key), entered in Config → IPTV → Subtitles & Languages. Player CC menu →
  "Find subtitles online…"
  opens `_SubtitleSearchDialog`; downloaded .srt lands next to the video
  (`stem.lang.srt`, deduped) or `~/.deeptorrent/subtitles/` for streams, and
  is loaded via `backend.add_subtitle_file` (mpv `sub-add … select`, VLC
  `video_set_subtitle_file`).
- Agent subtitle tools: `iptv_find_subtitles` (READ_ONLY) /
  `iptv_load_subtitle` (plain) in tools.py; no file_id = auto-pick via
  `pick_best` (hash_match > preferred lang > downloads). The bridge's
  `add_subs` signal loads the file on the GUI thread. Shared helpers live in
  opensubtitles.py: `clean_media_query`, `subtitle_dest_path`, `pick_best`.
- Preferred languages: `config.iptv.preferred_audio_lang/preferred_sub_lang`
  (ISO 639-1 or 639-2 — `PlayerWidget._lang_matches` aliases both). Applied
  once per file via mpv's `track-list` observer (`on_tracks` → `sig_tracks`)
  plus a 2.5s one-shot timer fallback for VLC; also the default language in
  the subtitle dialog and agent tools.
- RSS subscriptions are agent-managed too: `add_rss_feed` / `remove_rss_feed`
  (DESTRUCTIVE — they mutate and persist config via
  `DeeptorrentConfig.default_config_path()` and rebuild the ToolRegistry's
  RSSMonitor; call it on the CLASS, it's a self-less method).
- Deliberately NOT agent-controllable: settings-dialog config (AI/Jackett/
  IPTV/IRC network entries, API keys) — generic config mutation would leak
  secrets into the LLM context and can break the app; the dialogs stay the
  place for that.

## Voice input (gui/voice_input.py)
- Mic button (🎤) in the Agent-tab input row: toggle to record, click again
  to stop → transcribe → auto-send. If the agent is busy the transcript is
  left in the input box instead.
- Fully offline: `VoiceRecorder` captures the default mic via QtMultimedia
  `QAudioSource` (16 kHz mono Int16 preferred, falls back to the device's
  preferred format; `pcm_to_whisper_audio` normalizes to float32 mono 16 kHz
  — no ffmpeg needed since faster-whisper accepts numpy arrays).
- `VoiceTranscriber` lazy-loads faster-whisper on a daemon thread; the model
  downloads once from HuggingFace into `~/.deeptorrent/models/` (a dim 🎤
  status event announces the download). Settings live in `config.voice`
  (enabled/model/language/device/auto_send); no API keys involved.
- Packaging: app.spec collect_alls faster_whisper, ctranslate2, onnxruntime
  (VAD), tokenizers, huggingface_hub — all ship native extensions or
  importlib.resources data.
- Tests (`tests/test_voice_input.py`) cover only the numpy conversion +
  config round-trip; mic capture and model inference need real hardware.

## IRC subsystem (ircmgr/ + gui/irc_tab.py)
- Named `ircmgr` (not `irc/`) to avoid shadowing the PyPI `irc` (jaraco)
  dependency it builds on — pinned `irc>=20.4.0,<21`.
- `ircmgr/client.py` — `IRCClientCore`: ONE daemon thread owns the jaraco
  reactor; every socket op is marshalled in via a command queue (never touch
  the reactor cross-thread). Outgoing PRIVMSGs are paced per network
  (`irc.flood_delay`, default 2s) against excess-flood kicks; PING/PONG is
  handled by the library itself. TLS + SASL PLAIN supported; CTCP
  VERSION/PING/TIME auto-answered; DCC offers surfaced as events, never
  auto-accepted. LIST replies (321/322/323) are collected into
  `state.chanlist` (sorted by users desc).
- `ircmgr/state.py` — thread-safe `IRCState`: per-network nick lists, topics,
  and per-channel ring buffers (`irc.buffer_lines`, default 500) — this buffer
  is what the agent's `irc_*` tools read.
- GUI: `IRCTab` gets events via a single queued Qt signal (`_IRCSignals.event`),
  like `_IPTVSignals`. Joined channels persist to config on shutdown
  (`IRCTab.shutdown` in `closeEvent`).
- Defaults: `DEFAULT_IRC_NETWORKS` (config.py) = Libera + `#DeepFlux` with
  `auto_connect=True`, merged into user configs by `id`. The merge never
  touches existing ids, so `from_file` additionally migrates test-era entries
  in place: any saved network still carrying `#deepflux-test` gets the test
  channel swapped for `#DeepFlux` and `auto_connect` forced on; customized
  entries (no test channel) are untouched.
- Agent tools: `irc_status` / `irc_list_messages` / `irc_search_messages` are
  READ_ONLY; `irc_send_message` / `irc_join` / `irc_part` require confirmation.
  ToolRegistry takes `irc_client=` (GUI injects the shared core; CLI lazily
  starts its own — with no networks connected until configured).
- Tests: `tests/test_ircmgr.py` has a fake localhost IRC server — use it for
  any protocol-level regression (no external network in tests).

## IPTV + filesystem agent tools (Play / Command tabs)
- IPTV settings are SPLIT into small scrollable pages in
  `gui/iptv_settings_dialog.py` — `IPTV_SETTINGS_PAGES` (label, class) drives
  both the top-level IPTV menu and the Play-tab gear's picker popup
  (`MainWindow._open_iptv_settings` → `_open_iptv_page`). Every page derives
  from `_SettingsPage` (QScrollArea canvas + word-wrapped hints — never clip
  content on small windows). Sources mutate `config.iptv.sources` in place;
  scalar pages write fields in `accept()`. The source edit dialog also has an
  optional per-source `epg_url` (XMLTV) — it overrides/fills the playlist's
  own url-tvg in `IPTVManager._refresh_source` (providers often hand out the
  EPG link separately from the playlist link).
- M3U downloads are payload-validated: `m3u_parser.looks_like_m3u` rejects
  bodies starting with `<` (HTML error pages / XMLTV EPG endpoints pasted as
  the playlist URL) BEFORE parsing — fail fast, no retries, no cache write.
  Without it a 37MB XMLTV guide parses into ~475k garbage "channels" and
  poisons the SQLite playlist cache.
- EPG parser gotcha: `epg.parse_xmltv` (iterparse, end events) must NOT
  `elem.clear()` `<title>`/`<desc>` elements — their end events fire before
  the parent `<programme>`'s, so clearing them wipes the text the programme
  extraction reads next (every title/desc came back empty).
- Menu bar layout: File (API Keys, file associations) | Browse (Browser
  Settings, Import Bookmarks) | Agent | Download (Add Magnet/Torrent,
  Jackett Settings, Downloads Settings, Sources, RSS Feeds) | Play (the four
  IPTV_SETTINGS_PAGES) | Command | IRC (Networks) | Help | Bookmarks.
  `_TabMenuBar` (gui/main_window.py) makes the six tab-linked titles double
  as tab buttons: a click from another tab switches tabs (menu doesn't open),
  a click while on the tab opens the menu; empty linked menus (Agent,
  Command) swallow clicks entirely so no empty popup appears. The tab index
  is stored as a dynamic property on each menu's QAction — do NOT key a dict
  by QMenu/QObject wrappers: sip may hand out a fresh wrapper for the same
  C++ object in the full app, and `in`/`dict` lookups then silently fail
  (bare-widget tests won't catch it). `_TabMenuBar.addMenu` keeps every menu
  in `self._menus`: PySide returns PYTHON-OWNED QMenus from `addMenu()`, so
  losing the last Python reference destroys the C++ menu and its title
  silently vanishes from the bar on the next GC (this bit us when the
  `_tab_links` dict — the only thing holding them — was removed). Menus open on
  CLICK only — mouseMoveEvent is swallowed while any popup is visible, so
  hovering across titles never tears down/switches the open menu (Qt's
  default menu-mode mouse tracking). Click-opened popups auto-close when the
  cursor leaves the popup/submenus and the bar for ~450ms (`_auto_close_check`
  poll, 3×150ms ticks); keyboard-opened menus are never armed so arrow-key
  nav survives. There are no
  "Go to X" items anymore. All keys live in `APIKeysDialog` (File → API
  Keys); there is no AI Settings dialog or WebSearchSettingsDialog anymore.
- `gui/iptv_tab.py::AgentIPTVBridge` — thread-safe bridge between the agent's
  `iptv_*` tools (AgentLoop worker threads) and the Play tab: playback actions
  are emitted as queued Qt signals (`_AgentBridgeSignals`) so all player/Qt
  calls land on the GUI thread; the Play tab is auto-focused on agent-started
  playback. Reads use the shared `IPTVManager` (internally locked) plus a
  signal-fed state snapshot (`iptv_now_playing` never touches Qt off-thread).
  MainWindow injects it via `tools.set_iptv_bridge()` after tab creation AND
  in `_reload_agent` (which rebuilds the ToolRegistry).
- CLI has no bridge: iptv read tools lazily create their own `IPTVManager`
  (blocking first load of the active source, 60s timeout); playback tools
  return "requires the GUI" errors.
- `iptv_play` accepts exactly one of item_id / query (best name match) / url /
  file; Series items are rejected with an episode hint. `iptv_play` is
  DESTRUCTIVE (confirmation); `iptv_pause`/`iptv_stop`/`iptv_set_volume` are
  plain sequential (no confirmation); search/list/epg/now_playing READ_ONLY.
- Filesystem tools (`list_directory`, `create_folder`, `copy_path`,
  `move_path`, `rename_path`, `delete_path`) are agent-level ops, not wired to
  the CommanderTab widget — the Command tab simply reflects the results. All
  mutations are DESTRUCTIVE; overwrite guards default to refuse, `delete_path`
  needs `recursive=true` for non-empty dirs, and deletes are permanent (no
  recycle bin).

## Browser agent tools (Browse tab)
- `gui/browser_bridge.py::BrowserBridge` — a QObject parented to MainWindow.
  Agent tools call `bridge.call(op, **params)` from worker threads: the call
  is queued onto the GUI thread via one signal and the worker blocks on a
  `threading.Event` (35s timeout) until the GUI posts the result. JS actions
  (`get_content`/`click`/`fill`/`scroll`) complete the pending call from
  `runJavaScript`'s own callback. NOTE: neither `call` nor
  `ToolRegistry._browser_call` may name their first param `action` — the
  `browser_go` tool forwards an `action=` kwarg and Python would bind it
  twice.
- Tools: `browser_navigate` (address-bar semantics — domains load, anything
  else is a Google search), tab management (`list_tabs`/`switch_tab`/
  `close_tab`), `browser_go` (back/forward/reload/stop/home),
  `browser_get_content` (LIVE rendered DOM + the user's logged-in sessions,
  unlike `web_fetch`), `browser_scroll`, bookmark add/remove/list.
  `browser_click` (CSS selector or visible text) and `browser_fill`
  (selector+value, optional form submit) are DESTRUCTIVE — they can submit
  forms or start downloads under the user's accounts. Everything else runs
  without confirmation. Agent navigation auto-focuses the Browse tab.
- DOM fullscreen (YouTube ⛶ etc.): `_BrowserPage` enables
  `FullScreenSupportEnabled` and `MainWindow._browser_fullscreen_requested`
  accepts `fullScreenRequested` — Qt WebEngine NEVER honors the Fullscreen
  API by itself, an unaccepted request silently no-ops. The view is
  detached from `browser_tabs` (tab handlers all tolerate indexOf == -1)
  and shown as its own fullscreen window; ESC (QShortcut →
  `triggerAction(ExitFullScreen)`) and Alt+F4 (eventFilter reroutes Close)
  exit cleanly. `closeEvent` destroys a fullscreened view so it can't block
  the quit.
- IRC extras: `irc_connect`/`irc_disconnect` (config networks, not just
  connected ones), `irc_send_action`/`irc_send_notice`/`irc_set_nick`/
  `irc_send_raw` (all confirmation-gated), `irc_list_channels` (sends LIST,
  waits ≤20s for chanlist_ts, `refresh=false` reuses the cache — plain
  sequential, not READ_ONLY) and `irc_list_nicks` (READ_ONLY, nicks + topic
  from IRCState).
- Browser file downloads (`downloadRequested` →
  `MainWindow._on_browser_download_requested`): non-torrent files are routed
  to the internal segmented DownloadEngine (`_route_browser_download`) with a
  tray toast + Agent-tab event; `.m3u8/.mpd` become stream jobs. The job
  carries the browser session's cookies (live cache fed by
  `cookieStore().cookieAdded` → `_browser_cookies_for`) and the page URL as
  referrer. Only `.torrent` files still use QtWebEngine's own downloader
  (private-tracker cookies) and are auto-added to the torrent engine on
  completion. dlmgr note: `segment.py` must NOT use `with http_client.get()`
  — curl_cffi's Response isn't a context manager; close it explicitly.
