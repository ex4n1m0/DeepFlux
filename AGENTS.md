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
- Tests: `python -m pytest tests/ -v` — CAVEAT (2026-09-13, this dev
  machine): a SINGLE-process run of the whole suite can die with a native
  access violation while GC walks the heap (2026-09-13:
  `state.py::_channel_locked` frames; 2026-10-11: Shiboken's
  signature-loader `inspect.getsource` inside
  `gui/jackett_setup_dialog.py`'s import, with iptv artwork workers alive —
  reproduced on PRISTINE HEAD, i.e. NOT tied to any change). It is a
  heap-layout / GC-timing artifact of giant single processes — native libs
  loaded via collection; every individual file and small subset is 100%
  stable. KNOWN POISON PAIR: `test_iptv.py` + `test_jackett_setup.py` in
  ONE process segfaults (proven pre-existing 2026-10-11 via git worktree
  of HEAD) — keep them in different chunks. Release gate: run the suite in
  FOUR chunks —
  `python -m pytest tests/test_a*.py tests/test_b*.py tests/test_c*.py -q`
  then d–i (d e f g h i), then j–o (j l m n o), then r–z
  (r s t u v x y — re-check `ls tests/ | cut -c9 | sort -u` when a test
  file with a NEW first letter lands, like test_updater.py's "u" in
  2026-09-19).
  2026-10-20 UPDATE (do not re-derive — full forensic session in git
  history): the AV is ASLR/heap-layout PROBABILISTIC, not deterministic —
  identical commands can pass then crash on consecutive runs, and ANY
  bytecode-shape change to a module imported by tests re-rolls the dice
  (proven: 4 semantically identical shapes of the local_model GPU-fallback
  change all crashed with the new code paths demonstrably NEVER EXECUTING
  — tracers showed zero calls; an arbitrary other edit of the same file
  passed; `PYTHONMALLOC=malloc` passed once then crashed). After landing
  the 6.1 hardware work, both the d–i and j–o chunks became unstable;
  the split verified green REPEATEDLY on 2026-10-20 (1142 tests total) is
  SIX commands:
  `a-c`, `d-g`, `h`, `i`, `local_model` SOLO (that file carries an
  `import agent.llm` heap-fragility guard at its top — keep it),
  `j* + llm`, `loop + memory + m n o`, and `r-z (r s t u v x y)`.
  In full:
  `python -m pytest tests/test_a*.py tests/test_b*.py tests/test_c*.py -q`
  `python -m pytest tests/test_d*.py tests/test_e*.py tests/test_f*.py tests/test_g*.py -q`
  `python -m pytest tests/test_h*.py -q`
  `python -m pytest tests/test_i*.py -q`
  `python -m pytest tests/test_local_model.py -q`
  `python -m pytest tests/test_j*.py tests/test_llm.py -q`
  `python -m pytest tests/test_loop.py tests/test_memory.py tests/test_m*.py tests/test_n*.py tests/test_o*.py -q`
  2026-11-19 UPDATE: the r–z line is now TWO commands — test_tools.py
  went SOLO (3 identical native aborts inside
  test_search_indexers_private_tier_before_public when it shares a
  process with the new tests/test_system_health.py; both halves green
  separately — classic dice re-roll, NOT a code bug):
  `python -m pytest tests/test_tools.py -q`
  `files=$(ls tests/test_r*.py tests/test_s*.py tests/test_t*.py tests/test_u*.py tests/test_v*.py tests/test_x*.py tests/test_y*.py | grep -v "tests/test_tools.py"); python -m pytest $files -q`
  (nine commands total; d–g+i in ONE process, j–o in one process, and
  r–z-with-tools in one process are the current poison shapes). If a
  chunk native-AVs, suspect the dice first: re-run / re-split before
  hunting code.
  2026-11-21 UPDATE (fresh-venv forensics, root cause of a whole-day
  crash wave): the dice is PER-BOOT DETERMINISTIC, not per-run — Windows
  assigns each DLL's ASLR base once per boot, and some boots hand
  libtorrent.pyd + Qt6Core.dll bases that CANNOT coexist when libtorrent
  maps FIRST: `import libtorrent; from PySide6.QtCore import Qt` segfaulted
  5/5 while the reverse order passed 3/3, on the SAME wheels (libtorrent
  2.1.1 + PySide6 6.12.0, byte-identical to the shipped 9.2 bundle) that
  were gate-green on 11-19's boot. The app never hits this (main.py
  imports gui/Qt before the agent/engine chain), but pytest collects test
  modules alphabetically, so any chunk whose collection imports
  agent/engine (llm, jackett, loop…) before its first Qt import died —
  that day j*+llm, loop-mno, linux_port+llm were 4/4–5/5 reproducible
  segfaults and test_iptv passed 382/382 but poisoned its EXIT CODE.
  FIX (keep, same philosophy as the local_model guard): tests/conftest.py
  imports PySide6.QtCore BEFORE anything else can pull libtorrent —
  conftest loads before test modules, pinning Qt-first for every process;
  with the guard all poison shapes went green incl. clean exit codes. If
  the pattern ever returns WITHOUT libtorrent-first being provable, re-
  run the two-line order repro before hunting code. Also note:
  test_linux_port.py matches NO letter glob in the nine commands — run it
  with the j chunk or solo so it is never silently skipped.
  2026-11-22 EVENING UPDATE (tasks-sidebar change re-rolled the dice):
  test_gui_render.py grew 11 MainWindow-building tests, and on THAT boot
  the d–g, i, and loop+memory+m-n-o mega-shapes native-AVed 2–4/4 runs
  (roving innocent victims — yt_dlp bytecode compile, requests.send in a
  settings worker — never sidebar code). Pristine-HEAD stash runs of d–g
  passed 3/3, so the SHAPE, not the code, is the trigger. Split verified
  green REPEATEDLY same evening (1621 tests total) — TWELVE commands:
  `a-c`, `d-f`, `g` SOLO, `h`, `iptv` SOLO, `i*` minus iptv,
  `local_model` SOLO, `j*+llm+linux_port`, `loop+memory`, `m*`, `n-o`,
  `tools` SOLO, `r-z minus tools` (that's thirteen lines counting the
  tools line's swap-in below — keep every SOLO). In full:
  `python -m pytest tests/test_a*.py tests/test_b*.py tests/test_c*.py -q`
  `python -m pytest tests/test_d*.py tests/test_e*.py tests/test_f*.py -q`
  `python -m pytest tests/test_g*.py -q`
  `python -m pytest tests/test_h*.py -q`
  `python -m pytest tests/test_iptv.py -q`
  `python -m pytest $(ls tests/test_i*.py | grep -v iptv) -q`
  `python -m pytest tests/test_local_model.py -q`
  `python -m pytest tests/test_j*.py tests/test_llm.py tests/test_linux_port.py -q`
  `python -m pytest tests/test_loop.py tests/test_memory.py -q`
  `python -m pytest tests/test_m*.py -q`
  `python -m pytest tests/test_n*.py tests/test_o*.py -q`
  `python -m pytest tests/test_tools.py -q`
  `files=$(ls tests/test_r*.py tests/test_s*.py tests/test_t*.py tests/test_u*.py tests/test_v*.py tests/test_x*.py tests/test_y*.py | grep -v "tests/test_tools.py"); python -m pytest $files -q`
  The dice is PER-BOOT: on a fresh boot first try the old nine shapes,
  and only re-split (these twelve) if a mega-shape AVs. Do NOT chase a
  mega-shape AV with code changes when every constituent file is green
  solo — stash-and-run-HEAD discriminates shape from bug in one minute.
  Related trap found same evening: a per-test `mw.close()` fixture in
  test_gui_render HUNG the g glob (closeEvent teardown deadlock with
  gpu_preference in-process) — do not add MainWindow close-teardown to
  these tests; build-and-forget is the proven shape.
  POWERSHELL NOTE (2026-11-06): the chunk commands assume a
  glob-expanding shell (bash). PowerShell passes wildcards to native
  commands LITERALLY — `pytest tests/test_a*.py` exits 4 with
  "file or directory not found". PowerShell-safe form per chunk:
  `$files = Get-ChildItem tests -Filter 'test_*.py' -File |
  Where-Object { $_.Name.Substring(5,1) -match '^[abc]$' } |
  ForEach-Object { $_.FullName }; python -m pytest $files -q --tb=short`
  (letter groups: a-c / d-f / g / h / iptv / i / local_model solo /
  j+llm / loop+memory / m / n-o / r-y).
- Tests must NEVER write the real `~/.deeptorrent/config.json`: the autouse
  fixture in `tests/conftest.py` redirects `DeeptorrentConfig.default_config_path`
  for every test; still pass explicit paths whenever
  code under test can persist — `RoomTab.shutdown` and the RSS feed tools both
  call `config.to_file(default_config_path())`. An unpatched GUI-tab test once
  wiped a user's real config (sources + API keys) with test defaults.

## Build / package
- App bundle: `python -m PyInstaller packaging/app.spec --clean --noconfirm`
  → `dist/DeepFlux/` (onedir; launcher `DeepFlux.exe` + `_internal/`).
  SVP Manager can keep the old bundle's `_internal/msvcp140.dll` loaded after
  DeepFlux exits, making COLLECT fail with WinError 5. For a local test build,
  leave SVP running and use `--distpath dist-local`; before an installer build,
  close SVP Manager and rebuild the canonical `dist/DeepFlux` output.
- app.spec must `collect_all` every package that ships native extensions or
  `importlib.resources` data — currently yt_dlp, curl_cffi, ddgs, primp,
  fake_useragent (ddgs' DDG engine loads `browsers.jsonl`; missing data =
  "Failed to load or parse browsers.json" and DDG search silently dies in the
  frozen build).
- Installer: `"$LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging/installer.iss`
  (Inno 7.0.2 also installed at `C:\Program Files (Inno Setup 7\`). Reads
  `dist/DeepFlux/*` and writes `dist/DeepFlux<version>Setup.exe` by default.
  `BuildDir` and `BuildOutputDir` are command-line-overridable ISPP defines,
  both relative to installer.iss; this lets a locked-bundle build read
  `dist-local` and emit directly into `website/deepflux`. The old
  `Documents\DeePFlux` output folder is no longer used. The finished-page
  `FinishedLabel` is fixed-height (no scroll/auto-grow): keep it ≤ ~8
  rendered lines or it gets cropped on machines with larger system fonts /
  text scaling — long instructions belong in the in-app User Guide, not the
  installer final page.
- Jackett installer final step (added 2026-09-16): the finished page carries a
  DEFAULT-CHECKED postinstall checkbox "Set up Jackett torrent search now"
  (default-on is a `CurPageChanged(wpFinished)` hook that checks the RunList
  item — Inno always creates postinstall checkboxes unchecked) which runs
  `DeepFlux.exe --setup-jackett` (entry is BEFORE the launch entry, without
  `nowait`, so the app only starts once the service is linked). The step is
  `infra/jackett_setup.py::run_setup` with a standalone progress window
  (`gui/jackett_setup_dialog.py`): discover ServerConfig.json → run the
  BUNDLED official installer elevated (one UAC) → link `indexer.url/api_key`
  into config.json → add every unconfigured public indexer → test + sync.
  Idempotent: upgrades re-run it (config.json is wiped every install) and it
  just re-links in seconds. VERIFIED facts — do not re-derive by guessing:
  * Jackett's own installer is Inno Setup; its `windowsService` task is
    CHECKED BY DEFAULT and its non-postinstall `[Run]` entries
    (`JackettConsole --Install` / `--Start`) execute under `/VERYSILENT
    /SUPPRESSMSGBOXES /NORESTART` → a silent install leaves the service
    INSTALLED AND RUNNING. It requires elevation (default
    PrivilegesRequired=admin), so we launch it via ctypes `ShellExecuteExW`
    verb "runas" and wait on the process handle (declared argtypes — see the
    compact-mode ctypes lesson); UAC decline surfaces as GetLastError 1223.
  * Everything lands in `C:\ProgramData\Jackett` with `everyone-modify`, so
    an unelevated process reads `ServerConfig.json` there (fields: `APIKey`,
    `Port`, `BasePathOverride`, `AdminPassword`); user-mode installs keep it
    under `%LOCALAPPDATA%\Jackett`.
  * A FRESH Jackett has ZERO configured indexers (the 98 on this machine were
    added over time) — installing alone gives no sources; the fan-out that
    adds them is the point of the step.
  * The API key alone is NOT accepted on the admin routes (plain apikey →
    302 to the login page; the v2.2 admin prefix is gone and the v2.0 list
    route 400s through redirect chains). Working recipe, verified live:
    `GET /UI/Login?cookiesChecked=1` with a cookie jar (no admin password =
    instant auth cookie), then per indexer `GET` + `POST` the SAME array to
    `/api/v2.0/indexers/{id}/Config` — the web UI's OK button → 204. Jackett
    saves an indexer only if its connection test passes, so dead sites /
    FlareSolverr challenges / i2p hosts 500 and are counted and SKIPPED (that
    is correct, not a bug to fix). An admin password blocks the session — the
    step then skips the fan-out with an explanatory line.
  * NEVER uninstall or reset Jackett from DeepFlux — the service is shared
    infrastructure (Sonarr & co.).
  * Build ritual: run `python packaging/fetch_jackett.py` on the build
    machine before ISCC (like gen_logo_assets.py). It downloads the latest
    official installer into `packaging/jackett/` (gitignored, ~42 MB) +
    writes `PINNED.json` (version/sha256 — tracked) and the GPL-2.0 notice
    (tracked; ships into `_internal\licenses\` next to the mpv/ffmpeg LGPL
    notices). installer.iss uses `skipifsourcedoesntexist`, so a build
    without the file still compiles — it just offers no Jackett step.
  * Tests: `tests/test_jackett_setup.py` (mocked HTTP/ctypes/config writes,
    plus an offscreen dialog run).
- FlareSolverr installer final step (added 2026-10-13): a second
  DEFAULT-CHECKED postinstall checkbox "Install FlareSolverr to unblock
  Cloudflare-protected torrent sites (about 700 MB on disk)" runs
  `DeepFlux.exe --setup-flaresolverr` AFTER the Jackett entry (it pushes the
  URL into Jackett) and BEFORE launch. The step is
  `infra/flaresolverr.py::run_setup` (same dialog, mode="flaresolverr"):
  extract the bundled zip next to the app → DELETE the zip (~378 MB saved;
  Inno reinstalls it on every install) → start detached (ENV-only config:
  PORT=8191 HOST=127.0.0.1 loopback-only, no CLI flags exist) → persist
  `indexer.flare_solverr_url` → `jackett.set_flaresolverr_url`. VERIFIED
  facts — do not re-derive by guessing:
  * The OFFICIAL FlareSolverr repo ships Windows binaries now (the 21hsmw
    fork is NOT needed): v3.5.2 `flaresolverr_windows_x64.zip`, MIT, a
    self-contained PyInstaller onedir that bundles its OWN Chromium at
    `_internal/chrome/chrome.exe` — NO system-Chrome dependency. Health:
    `GET /health` → 200 `{"status": "ok"}` on port 8191.
  * MEASURED live on this machine (96-indexer Jackett): linking FlareSolverr
    made 15 of 19 Cloudflare-walled indexers functional (8 answered with
    results incl. eztv + both kickasstorrents; 7 more solved the challenge
    and returned clean zero-hits). 1337x's CURRENT challenge is NOT solvable
    by it ("FlareSolverr was unable to pass"), two sites exceed the ~55s
    solve window, torrentproject2's failure isn't CF at all. First solves
    take 40-90s, cookies cached afterwards — expect a slow first search.
  * `fetch_flaresolverr.py` pins version/sha256 (asset digest from the
    GitHub API) into `packaging/flaresolverr/PINNED.json` + the MIT notice
    into `_internal\licenses\`; the zip is gitignored and reuses a local
    copy when the digest matches (no 378 MB re-download on rebuilds).
    Setup exe grows by ~378 MB (zip stored near-as-is inside Inno).
    CHANGED 2026-10-19 (6.0): the zip is NO LONGER BUNDLED — adding the
    llama runtimes pushed the setup exe to 802.4 MB and Vercel hard-500s
    files over ~750 MiB (5.9's 751.3 MB still served). --setup-flaresolverr
    now DOWNLOADS the pinned official release at setup time
    (infra/flaresolverr.py::_download_pinned_zip, digest-checked against
    packaging/flaresolverr/PINNED.json which ships via app.spec datas);
    offline installs fail soft with a retry hint. Keep future setup-exe
    growth under ~750 MiB or move more payload to setup-time downloads.
  * Auto-start (never a service): the GUI's hourly Jackett-sync thread and
    the search tier's flare-failure path both call
    `flaresolverr.ensure_running` — only ever for OUR default local URL
    (`http://127.0.0.1:8191/`; a custom remote URL is never started), only
    when `find_executable()` finds the extracted install. POSIX:
    FlareSolverr is Windows-setup-only (find_executable → None → clean
    no-op; the URL field still works for manual instances).
  * Tests: `tests/test_flaresolverr.py` (fake zip with the official layout,
    env/detached spawn, ensure_running gating, link pipeline, POSIX refusal).
- Installer-seeded config.json (CurStepChanged ssPostInstall): the seed is
  MINIMAL and must stay that way — ONLY real overrides, never a copy of
  defaults (config.py is the single source of truth; from_file fills the
  rest). Shipped today: one sample IPTV playlist (user decision 2026-09-16)
  — iptv-org's Macau country list (`macau-iptv-org`,
  https://iptv-org.github.io/iptv/countries/mo.m3u, 7 free-to-air channels,
  #EXTM3U-verified) so the Play tab has content on first launch; removable
  by the user in Play → Sources, fixed id (not a uuid) keeps re-installs
  idempotent. HISTORY (found 2026-09-16): the seed used to duplicate the
  whole default config WITH trailing commas — invalid JSON that from_file
  silently discarded, so fresh installs had ALWAYS run on config.py
  defaults while the stale copy fossilized (youtube homepage, adblock off,
  timeout 30…). tests/test_installer_seed.py pins: valid strict JSON,
  loads through from_file, no seeded value equals its default, and the
  playlist URL still serves #EXTM3U. This is an IPTV source, NOT a torrent
  source — `DEFAULT_SOURCES` stays empty (rule below).
- Code signing (SmartScreen, added 2026-09-13): builds are UNSIGNED until a
  certificate exists — downloaded setup exes therefore show the "Windows
  protected your PC" warning, and every release restarts file reputation
  from zero (Microsoft's own doc; EV no longer bypasses SmartScreen).
  installer.iss has the conditional wiring: set env `DF_SIGNTOOL=<name>` and
  pass ISCC `/S<name>="...signtool.exe" sign /fd SHA256 /tr
  http://timestamp.digicert.com /td SHA256 $f` — see the comment block in
  installer.iss; sign `dist/DeepFlux/DeepFlux.exe` the same way BEFORE ISCC
  (Win11 Smart App Control checks installed executables too). signtool is at
  `C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe`.
  Keep ONE signing identity across releases — publisher reputation does not
  transfer between certificates.
- Logo: every derived asset (icon.ico, logo_48, wizard images, extension
  icons, website webp) is generated from the root `DeepFlux<version>.png` by
  `python packaging/gen_logo_assets.py` — update that script's source
  filename on a version bump and re-run it, never hand-edit the outputs.
- Website deploy: `website/` is a Vercel-linked project (`deep-flux`,
  aliased to deepflux.space; root index.html redirects to /deepflux/).
  WINDOWS + LINUX (dual-platform since 5.5, 2026-10-11): both pages (EN +
  /deepflux/zh/) carry the Windows setup exe AND the Linux AppImage with
  install tabs (win/linux) + per-panel sha256s; macOS stays REMOVED
  (2026-09-24 owner decision) — do NOT re-add the macOS button or re-host
  the dmg without a new owner decision. 2026-10-21 copy redesign
  (owner-approved "reduce the copy, stop repeating"): ONE AI-key note above
  the tabs (was one per panel), no news badge, no version pill in the nav,
  no closing CTA band (the sticky nav carries Download), and the "From the
  same maker" sibling links moved from the top strip into the FOOTER next
  to the Buy Me a Coffee icon. The macOS/Linux paragraphs below describe
  the RETIRED ritual, kept for history in case it is ever revived.
  Publish = copy the new `DeepFlux<X>.Setup.exe` into `website/deepflux/`,
  update index.html AND zh/index.html (download hrefs, the hero meta line's
  version, tab label, size line, sha256; there is no h1 any more — the logo
  is the top element), delete the old setup exe,
  then `vercel --prod --yes` from `website/`. Only files in the dir are
  deployed — removing the old exe from the dir removes it from production.
  `latest-ytdlp.json` (the in-app yt-dlp updater's feed, written by
  `packaging/pin_ytdlp.py`) lives in the same dir and rides the same
  deploy; re-pin it whenever a newer yt-dlp should reach users before the
  next DeepFlux build.
  Landing-page IMAGES are REAL app captures (since 2026-09-25, owner call:
  the old CSS mockups didn't match the software): `shot-download-en/zh.webp`
  + `shot-agent-en/zh.webp` in website/deepflux/, framed by the decorative
  `.shot-titlebar` chrome. DOWNLOAD EXAMPLES in the shots must be official,
  unambiguous artifacts ONLY (Ubuntu/Debian ISOs, blender tarball as
  torrents; ffmpeg/godot/inkscape packages as direct downloads) — owner
  rule 2026-09-25: no movie-release-style names (BigBuckBunny/Sintel/
  Tears-of-Steel scene naming looked like warez even though the films are
  CC-licensed). Regenerate with the scratch tool
  `python _site_shots.py <en|zh> _site_shots_out` (real engines, loopback
  seeder + throttled HTTP server, staged chat through _append_user/event/
  agent, per-language process) → webp q85 → copy into the deploy dir. Key
  gotchas baked into that script: native windows QPA (offscreen = tofu
  emoji), NO QT_SCALE_FACTOR (it shrinks the logical desktop and Qt clamps
  the window to it), WA_ShowWithoutActivating + lower() (a fully
  off-desktop move gets clamped+resized by Windows), libtorrent
  `ignore_limits_on_local_network: False` or the loopback seeder saturates,
  patch `dlmgr.http_client.validate_public_url` for 127.0.0.1 targets, and
  NO graceful close after the grabs (engine teardown hung for minutes).
  Retake both languages after any major UI change so the site stays honest.
  macOS (since 4.7, 2026-09-17, RETIRED from the site 2026-09-24): the page also carried
  `DeepFlux-<X>-macOS-arm64.dmg` — fetch it from the CI artifact
  (`gh run download <run-id> -n "DeepFlux-<X>-macOS-arm64" -D <tmp>`), move
  the dmg into `website/deepflux/`, update BOTH download hrefs + the meta
  line, delete the old dmg, redeploy. Both binaries are gitignored
  (`website/**/*.exe`, `website/**/*.dmg`) — the deploy dir IS the
  distribution, so a stale artifact on disk ships forever until deleted;
  CI artifacts expire after 30 days, so a mac version bump means a fresh
  CI run → fresh download → redeploy. The mac button routed through the
  same `/api/download?f=…` counter (download.js's FILE_RE accepts the dmg
  pattern); the smartscreen note covered Gatekeeper (right-click → Open).
  Vercel Web Analytics is enabled on the project (dashboard toggle or CLI ≥59
  `vercel project web-analytics`) — the static page carries the
  `/_vercel/insights/script.js` tag; don't remove it. If the CLI reports
  "No existing credentials found", `vercel login` (device-code flow, user
  clicks Verify in the browser) fixes it — non-interactive shells work.
  `vercel --prod --yes` may fail with "Not authorized" even while
  `vercel whoami` succeeds: the project lives under the **timedivision**
  team, so pass `--scope timedivision` (the orgId in `website/.vercel/
  project.json` does not resolve to the CLI's default personal scope).
  Version bumps touch: installer.iss (MyAppVersion + OutputBaseFilename),
  main_window title, help_dialog (H1/Version/About), opensubtitles UA,
  config.py APP_VERSION (the telemetry ping's version), website index.html
  (meta line AND the /api/download?f=… href), website/deepflux/latest.json
  (the auto-update feed: version, file, size, sha256 of the exe just
  deployed — compute from the file in the deploy dir), README.md ("Current
  version" line — a pre-commit hook in .git/hooks/ re-syncs it from
  installer.iss MyAppVersion on every commit and auto-stages the change;
  if the hook is missing on a fresh clone, reinstall it from this note).
- Live stats (since 3.8): the app sends an anonymous usage ping
  (`infra/telemetry.py`, started by GUI `__init__` and the CLI Repl, stopped
  in closeEvent/shutdown with a best-effort "leave" note) to
  `https://deepflux.space/api/heartbeat` every 5 min — payload is ONLY
  {install id, APP_VERSION, os.name}; the id is a random hex file at
  `~/.deeptorrent/install_id` (deliberately NOT in config.json so Settings
  Export can't transplant one machine's identity onto another; survives
  installs since the installer never touches it). Off-switch:
  `stats.ping_enabled` (Download Manager settings page, applies next launch),
  default ON by owner decision 2026-09-13. Every failure is silent by
  contract. The website side lives in `website/api/` (heartbeat.js =
  presence ZSET with a 15-min window, stats.js = {online, downloads},
  download.js = INCR + 302 to the static exe — the landing page's Download
  button goes through it). Backing store is Upstash Redis via the Vercel
  Marketplace, read from `UPSTASH_REDIS_REST_URL/_TOKEN` **or**
  `KV_REST_API_URL/_TOKEN` (the names the Vercel integration actually sets —
  LIVE since 2026-09-13); without them all three functions degrade
  gracefully (heartbeats answer 200 {ok:false}, stats returns nulls and the
  page hides the widgets, downloads redirect uncounted). New env vars only
  reach the functions after a redeploy. Tests:
  tests/test_telemetry.py (all network mocked).

## macOS build (CI-only, added 2026-09-17)
- Strategy: macOS ships the priority features (Browser, Agent, Torrents &
  Downloads, plus Room/Commander which came free). The Play tab is
  Windows-ONLY: `_PLAY_TAB_SUPPORTED = sys.platform == "win32"` in
  gui/main_window.py guards the iptv_tab imports, tab creation, File-menu
  Play zone, tab-button indices, and every `self.iptv_tab` call site
  (play_file_in_player falls back to `_reveal_path`, _stream_torrent and
  play_web_stream show "no player in this build" notices).
  `ToolRegistry(..., enable_iptv_tools=...)` filters every `iptv_*` tool
  from the registry when False. Do NOT regress these guards — an unguarded
  `self.iptv_tab` reference crashes the mac AND Linux builds at startup.
- Build machine: `.github/workflows/macos-build.yml` (manual dispatch) on a
  free macos-14 ARM runner — the repo is PUBLIC (macOS minutes are free),
  and PyInstaller cannot cross-compile, so CI IS the macOS build machine.
  There is no Apple Developer account: builds are UNSIGNED (Gatekeeper
  shows "unidentified developer"; right-click → Open) and KEYLESS by design
  (`_embedded_keys.py` is untracked, so users enter their own API keys —
  everything degrades gracefully). Artifacts: `DeepFlux-<ver>-macOS-arm64.dmg`
  (hdiutil UDZO; Actions' zip artifacts would strip the exec bit).
- Files: `packaging/app_macos.spec` (BUNDLE .app; NO mpv/vlc/ffmpeg/
  milkdrop; Info.plist carries NSMicrophoneUsageDescription for voice —
  missing it makes macOS KILL the process on first mic access; version is
  parsed from installer.iss MyAppVersion), `packaging/gen_macos_icon.py`
  (Pillow writes packaging/icon.icns from the tracked DeepFlux.png — the
  SINGLE logo source everywhere since 2026-11-06, when the window/tray/
  file-association icons and all three specs converged off the retired
  DeepFlux5.png; keep the PNG ≥512px), `packaging/verify_boot.py` (offscreen
  boot smoke:
  builds the REAL MainWindow, asserts tabs, no iptv_tab attr, no iptv_*
  tools in the registry; run locally on Windows too — asserts flip by
  platform. Needs QT_QPA_PLATFORM=offscreen +
  QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu --no-sandbox" or Chromium
  fails on a headless runner).
- No ffmpeg is bundled on macOS: `find_ffmpeg` falls back to PATH, and the
  agent's SYSTEM_PROMPT has a darwin-only note (run_shell
  `/opt/homebrew/bin/brew install ffmpeg` then set download.ffmpeg_path —
  Finder/Dock-launched apps do NOT inherit the shell PATH, so the explicit
  path is mandatory; winget doesn't exist there).
- POSIX gotchas fixed along the way (do not regress): `infra/jackett.py`
  creationflags MUST come from `getattr(subprocess, "CREATE_NO_WINDOW", 0)`
  (a hardcoded 0x08000000 makes POSIX Popen raise ValueError), the
  `sc.exe` service path is behind `os.name == "nt"`, and Jackett's mac
  executable candidates are the .app bundle console host; `_TabMenuBar`
  calls `setNativeMenuBar(False)` on darwin (the native top-of-screen menu
  bar ignores our custom painting — the tab-button row would vanish); the
  `sc()` shortcut helper registers Meta (Cmd) twins for every Ctrl+…
  sequence on darwin (Qt never translates "Ctrl+" strings).
- macOS CI runs ONLY the boot smoke, not pytest: the suite has
  Windows-specific tests (winreg paths, file associations) that would be
  red noise there. Local gate before pushing mac-affecting changes: the
  split a-c/d-z invocation PLUS `python packaging/verify_boot.py`.

## Linux build (CI-only; FULL FEATURE since the 2026-10-11 port)
- The Linux build is now a FULL build (owner decision 2026-10-11, reversing
  the 2026-09-18 scope cut): Browser, Agent, Torrents & Downloads, Room,
  Commander AND the Play tab. `_PLAY_TAB_SUPPORTED =
  sys.platform in ("win32", "linux")` (gui/main_window.py); macOS stays
  excluded (libmpv cannot embed into Qt there without a render-API
  rewrite). `packaging/verify_boot.py` asserts the right shape per
  platform off the same flag. Do NOT regress the guard: an unguarded
  `self.iptv_tab` reference crashes the macOS build at startup.
- PLAY-ON-LINUX architecture (all verified in the 2026-10-11 study, do not
  re-derive): video embeds via X11 window IDs (mpv `--wid`, libvlc
  `set_xwindow`); native Wayland has NO cross-client window embedding
  (mpv docs; VLC issue #16106), so main.py defaults
  `QT_QPA_PLATFORM=xcb` on linux (XWayland; an explicit env var wins — CI's
  offscreen QPA relies on that). libmpv ships in `_internal/mpvlibs/`: the
  workflow apt-installs libmpv2 + patchelf, ldd-walks libmpv.so.2's
  closure (skipping glibc core + the driver-matched GL/EGL/GBM/DRM/vulkan
  stack), copies it into mpvlibs and sets `--set-rpath '$ORIGIN'` on every
  lib so loading needs NO LD_LIBRARY_PATH (spawned subprocesses like
  system ffmpeg can never pick the bundled libav* up). python-mpv loads
  via `ctypes.util.find_library('mpv')`, which cannot see AppImage-bundled
  libs — `iptv/player.py::_ensure_bundled_so_findable` wraps find_library
  to return the bundled absolute path (an absolute path short-circuits the
  search). VLC is NOT bundled: python-vlc ships, the fallback activates
  only on machines with a system VLC (its import raises OSError otherwise
  — caught, degrades to mpv). Compact PiP (SetWindowPos topmost) and the
  WS_EX_NOACTIVATE SVP-child patch remain win32-only by design.
- The DeepSeek shared key IS embedded (owner decision 2026-10-11): GitHub
  Actions secret `EMBED_SHARED_DEEPSEEK_API_KEY` →
  `packaging/gen_embedded_keys_from_env.py` writes an obfuscated
  `_embedded_keys.py` before PyInstaller (values never printed; the script
  REFUSES to overwrite an existing local key file without
  DF_OVERWRITE_EMBEDDED_KEYS=1). Same exposure as the public Windows setup
  exe; adding more slots later = add the secret + one env line. All other
  shared keys stay Windows-setup-only.
- No signing concept exists on Linux at all: AppImages run with zero
  warnings once the exec bit is set — no Gatekeeper/SmartScreen analog.
  Users just `chmod +x DeepFlux-*.AppImage && ./DeepFlux-*.AppImage`.
- Build machine: `.github/workflows/linux-build.yml` on ubuntu-24.04
  (pin the version — `ubuntu-latest` can move under you; ubuntu-22.04
  entered deprecation 2026-09-17, actions/runner-images#14254). The runner
  choice sets the glibc floor: 24.04 = glibc 2.39 (Ubuntu 24.04+, Debian
  13, Fedora 40+, Arch; Debian 12/Ubuntu 22.04 users are out until a
  manylinux_2_28 container job exists). The workflow apt-installs the
  QtWebEngine runtime libs (libnss3, libasound2t64, libatk-bridge, the
  libxcb set incl. libxcb-cursor0 — a classic Qt6-on-Debian gap) plus
  libmpv2/patchelf for the player stack.
- AUTO-UPDATE (Linux, since the port): same ritual as Windows but against
  `website/deepflux/latest-linux.json` (feed file lands at release time
  with the AppImage's size+sha256); `infra/updater.py` gates on
  win32|linux + frozen, stages in `~/.local/state/DeepFlux/updates`, and a
  detached `/bin/sh` helper (apply_update.sh) waits for our PID, chmod +x,
  `mv`s over the AppImage path (POSIX rename over a running image is safe
  — the process keeps its mount inode) and relaunches `$APPIMAGE`. Not an
  AppImage (tar.gz/source) → "downloaded, run it manually" path.
  `other_instances_running` pgrep-matches the full APPIMAGE path.
- Linux parity reworks shipped with the port: file associations =
  .desktop + hicon icon + xdg-mime default ONLY for x-bittorrent and
  x-scheme-handler/magnet (forcing video/audio defaults would be rude;
  `--register-associations` works on linux); native messaging manifests
  land in `~/.config/{google-chrome,chromium,microsoft-edge,brave-browser,
  }/NativeMessagingHosts/` (host path = $APPIMAGE — sys.executable inside
  an AppImage is the ephemeral /tmp/.mount_* dir and must never be used);
  bookmark import scans ~/.config chrome/chromium/edge/brave/vivaldi +
  ~/.mozilla and the flatpak Firefox dir; fresh frozen installs seed the
  sample Macau playlist via `infra/first_run.py` (run_gui, frozen+linux+
  no-config-file-only — the mirror of the Windows installer seed).
- CI gotchas hit on the way to green (2026-09-18, do not re-derive):
  * QtMultimedia links PulseAudio on Linux — `libpulse0` must be apt'd or
    `import gui.voice_input` (module-level QAudioSource import in
    main_window) dies with `libpulse.so.0: cannot open shared object`.
  * linuxdeploy does NOT search appdir SUBDIRECTORIES when resolving
    NEEDED entries, and PyInstaller keeps wheel libs (incl. hashed names
    like ctranslate2's libgomp-e985bcbb.so.1.0.0) in `_internal/` — export
    `LD_LIBRARY_PATH="$PWD/dist/DeepFlux/_internal"` before invoking it,
    or it dies with "Could not find dependency".
  * PyInstaller's PySide6 hooks drag in the whole Qt QML tree, whose
    QtWayland compositor plugins need `libwayland-server0` (not on a
    headless runner) — apt the three libwayland-* runtime packages.
  * linuxdeploy rejects non-square icons ("invalid x resolution") — the
    workflow pads the logo onto a square canvas and downscales to
    512x512 with Pillow before calling it.
- Artifacts: `DeepFlux-<ver>-Linux-x86_64.AppImage` (PRIMARY — linuxdeploy
  bundles the SYSTEM library closure so users need zero apt installs;
  APPIMAGE_EXTRACT_AND_RUN=1 because 24.04 runners lack libfuse2) and the
  same onedir as `...-Linux-x86_64.tar.gz` (fallback; needs the distro's
  Qt/Chromium runtime libs). CI also smokes the packaged AppImage itself
  (`--help` under offscreen) — catches bootloader/lib problems the source
  boot test can't. linuxdeploy's continuous build URL is the standard one.
- Spec: `packaging/app_linux.spec` = full app scope (iptv hiddenimports,
  milkdrop datas, nacl/argon2 collect_alls — previously only pulled in
  transitively); mpv/vlc NOT in excludes anymore.
- No ffmpeg is bundled on Linux: `find_ffmpeg` PATH-finds
  /usr/bin/ffmpeg after the user (or the agent, via run_shell) installs
  the distro package — the SYSTEM_PROMPT has a linux note (agent/loop.py)
  with the apt/dnf/pacman guidance + Jackett at /opt/jackett/jackett or
  ~/.config/Jackett/jackett (systemd service; starting it needs elevation
  the app doesn't have — infra/jackett.py spawns the binary directly
  instead, same as the mac .app path).
- SVP on Linux: SVP 4 for Linux EXISTS (svp-team.com, $24.99 lifetime;
  needs VapourSynth R60+, Python 3.12, SVP's own mpv build) — the Windows
  out-of-process architecture maps to it, but `iptv/mpv_process.py`'s IPC
  is Windows-named-pipe only (PIPE_PATH `\\.\pipe\mpvpipe`, PeekNamedPipe
  reader with no POSIX path). Until that is rewritten onto
  `--input-ipc-server` unix sockets, SVP stays Windows-only in practice
  (find_install returns None on Linux → clean in-process mpv fallback).
- Telemetry now sends `sys.platform` (win32/darwin/linux) instead of
  `os.name` — os.name collapsed mac+linux into one "posix" bucket; the
  heartbeat payload is the only place they're distinguishable.
- Website: the AppImage button was LIVE 2026-09-18→2026-09-24 and was
  REMOVED when the site went Windows-only (commit 8dc7666; old URLs 404).
  Reviving it is an owner decision; the mechanics are unchanged — fresh
  CI run → `gh run download -n "DeepFlux-<X>-Linux-x86_64"` → swap the
  AppImage into website/deepflux/ → update hrefs + meta date → delete the
  old file → `vercel --prod --yes --scope timedivision`, and now ALSO
  write `latest-linux.json` (version/file/size/sha256 of the deployed
  AppImage) for the auto-updater.

## Auto-update (Windows, since 4.9)
- The Windows build updates ITSELF: `infra/updater.py` (Qt-free) checks
  `https://deepflux.space/deepflux/latest.json` daily (startup background
  thread + Help → "Check for Updates…"), the GUI (`gui/update_dialog.py` +
  main_window `_update_*` methods) offers Update now / Later / Skip this
  version, downloads the setup exe to `%LOCALAPPDATA%\DeepFlux\updates\`
  with a SEGMENTED PARALLEL transfer (since 5.9 — see below), verifies
  size + sha256 against the feed, then a detached PowerShell helper
  (apply_update → apply_update.ps1)
  waits for our PID to exit, runs `setup.exe /VERYSILENT
  /SUPPRESSMSGBOXES /NORESTART`, relaunches DeepFlux and deletes the setup
  file. Log: `~/.deeptorrent/logs/update.log`. Nothing ever installs
  without the user's click, and the updater is deliberately NOT an agent
  tool (prompt injection must never trigger an installer run).
- Segmented download (5.9): payloads are ~750 MB now, so
  `updater._download_segmented` drives the ENGINE'S OWN
  `dlmgr/segment.py::SegmentWorker` — 6 parallel byte-range connections
  writing into ONE preallocated (sparse-truncated) .part file, same
  boundaries formula as the engine (`SEGMENT_MIN_SPAN` 512 KiB floor).
  A `Range: bytes=0-0` probe gates the mode: 206 + Content-Range total ==
  feed size → segmented; 200/ignored → single-stream fallback
  (`_download_stream`, the old append-resume path). A probe total that
  DISAGREES with the feed fails loudly ("feed is stale") instead of
  downloading wrong bytes. Resume is per-segment via a sidecar
  `<file>.part.segs.json` (completed-bytes per range + the probe ETag —
  an ETag change discards the state; flushed every 2s and on every exit
  path; deleted on success). Stalled workers (SegmentWorker.is_stalled)
  are stopped and respawned from their persisted offset up to
  `SEGMENT_RESPAWNS` per attempt. MEASURED live 2026-10-15 (751 MB exe on
  Vercel): ranged GETs answer 206 + stable strong ETag, but the FIRST
  ranged GET on a cold edge took ~49s while the next answered in ~0.1s —
  the probe timeout is 60s so it absorbs the cold start and the parallel
  workers begin warm. Tests: tests/test_updater.py (parallel layout,
  sidecar resume, etag change, cancel-keeps-state + retry, respawn
  budget, no-range fallback, stale-feed error).
- VERIFIED facts (2026-09-19 study) — do not re-derive:
  * The install is PER-USER: installer.iss `PrivilegesRequired=lowest` →
    `{autopf}` = `%LOCALAPPDATA%\Programs\DeepFlux` (HKCU uninstall key), so
    the silent reinstall needs NO UAC. A machine-wide install (the user
    picked "for everyone" in the installer dialog) is detected via
    `install_dir_writable()` and degrades to "downloaded, run it manually".
  * `/VERYSILENT` skips BOTH postinstall entries (they carry `skipifsilent`
    — the Jackett setup step and the launch entry) but still runs the
    `--register-associations` entry; Inno reuses the previous install dir
    from the uninstall key.
  * A file downloaded by the app carries NO Mark-of-the-Web → no
    SmartScreen on the silent run (only Smart App Control, opt-in, remains
    — same as a manual install). Integrity = HTTPS + sha256 pinned in the
    feed (same trust root as the website).
- Feed: `website/deepflux/latest.json` — {version, file, size, sha256,
  released, notes}. `parse_feed` is STRICT (basename-only file, hex sha256,
  size sanity) and returns None on anything malformed. Release ritual:
  during the website exe swap, write the new version + the sha256/size of
  the exe sitting in the deploy dir (the feed is TRACKED in git, unlike the
  exe). `is_newer` reuses `ytdlp_update.is_outdated`'s tuple compare
  (4.10 > 4.9). Gating: `updates_supported()` = win32 AND frozen; config
  `updater` section (`check_enabled` default on — toggle on the Download
  Manager settings page; `skip_version`/`last_check` are bookkeeping and
  DENY-listed from the agent settings tool). A manual check (force=True)
  re-offers a skipped version.
- The installer no longer deletes config.json (owner decision 2026-09-19 —
  see the seed paragraph under Build/package): updates keep sources, API
  keys and settings. The helper waits up to 90s for the graceful close
  (fast-resume + config save) before installing; closeEvent skips its
  active-transfers confirm when `_update_in_progress` (already confirmed in
  the apply dialog). Multiple instances: `other_instances_running()` blocks
  the apply until the extra windows are closed.

## Conventions

- MINI PLAYER BOOT + TWO-WAY MORPH (7.8, 2026-11-10; design + measurements
  in FAST_VIDEO_OPEN_STUDY.md, implemented same day after an independent
  review): the app's DEFAULT boot surface is the Qt-light mini player
  (`gui/mini_player.py`) — plain shortcut launches AND video/audio
  association opens (owner decision: "the first to open is always the mini
  player"). Its idle state paints the owner banner (same art as the Agent
  tab — shared resolver in `gui/brand_assets.py`, the single source of
  truth). Durable rules — do not regress:
  * `main.py` imports NOTHING heavy at module level (engine/agent/gui load
    lazily per branch), and `gui/__init__.py` is LAZY (PEP 562 __getattr__
    for run_gui/MainWindow) — importing `gui.mini_player` must never drag
    `gui.main_window`/QtWebEngine/libtorrent (measured: 244 ms vs ~670 ms;
    pinned by tests/test_mini_player.py::test_gui_package_stays_light_for_
    mini_boot). NEVER add a module-level import to gui/__init__.py.
  * `_decide_boot` (main.py): every target media-extension-or-none → mini;
    ANY scheme target (`magnet:`, `://`) or non-media ext → full app
    (scheme check is BEFORE the extension check — `magnet:?…&dn=movie.mp4`
    and `https://…/v.mp4` must go full). `_MINI_BOOT_SUPPORTED` duplicates
    `_PLAY_TAB_SUPPORTED`'s platform tuple ON PURPOSE (main.py must not
    import the heavy chain to ask); keep them in sync.
  * Morph contract = STOPPED PLAYBACK (owner-accepted). Mini→full ("⤢ Full
    app"): heavy imports on a worker thread (marshaled back via a Qt
    SIGNAL — QTimer.singleShot from a thread with no event loop never
    fires), MainWindow built in STAGES on the GUI thread
    (`MainWindow(progress=cb)` — `_stage()` hooks; the cb pumps
    processEvents), behind a progress bar with measured stage weights. When
    `progress` is set, MainWindow DEFERS `_dl_api.start()` to the end of
    __init__ — a queued control-API open must never land in a half-built
    window. Full→mini ("◱ Mini" button in the Play toolbar next to Compact,
    overflow-first): stops playback INCLUDING multiview grid streams
    (`force_stop_all_streams` — plain stop() leaves grid tiles playing),
    persists UI state, hides; engines/agent/downloads KEEP RUNNING hidden.
  * Control-API port 53742 handoff: the mini player stops ITS ControlAPI
    (started post-show, ~2 ms with a dormant DownloadEngine) before
    MainWindow constructs its own; on full→mini the MainWindow's API stays
    up with handlers RE-ROUTED. Handlers are ALWAYS signal EMITS
    (`openRequested.emit` / `_open_signals.open_target.emit`) — the raw
    methods touch QWidgets and the handler runs on the HTTP thread (pinned
    by test_open_handler_stays_signal_marshaled_after_roundtrip). Backend
    callbacks (on_state/on_position) fire on mpv's EVENT thread — the mini
    player marshals them through its backendState/backendPosition signals,
    exactly like PlayerWidget's sig_* in the full app.
  * Quit semantics: closing the mini window when a MainWindow exists (even
    hidden) calls `main.close()` FIRST — the declined active-transfers
    prompt cancels the whole quit (`if not close(): event.ignore()`); a
    mini-only session destroys the backend (live libmpv at interpreter
    shutdown = exit-crash class) and stops the telemetry heartbeat.
  * Morph-built MainWindows run the same gc.collect()+gc.freeze() ritual as
    run_gui after construction; run_gui's association dispatch timer is
    0 ms (was 500 ms in ≤7.7).
  * Version 7.8 sites bumped (config.APP_VERSION, installer.iss,
    README) — the WEBSITE/latest.json ritual is release-time work, NOT done
    yet.
  * Tests: tests/test_mini_player.py (18) — boot decision incl. scheme-
    suffixed magnets/URLs, lazy package, config section, idle/play states,
    full morph cycle (monotonic stages, routing, graceful close),
    instant re-morph, full-boot-first routing, declined-quit cancel,
    media queue, missing-file warning.
  * TRANSPORT PARITY (owner 2026-11-18, "both players control time
    skipping equally"): the mini transport mirrors the full player's
    ⏪ ⏸ ⏹ ⏩ order — it HAS a ⏹ stop button (`_user_stop`: drops the
    on-deck queue so a DELIBERATE stop never auto-advances, resets to
    the idle page, exits fullscreen) and the S key stops in BOTH
    players; the full PlayerWidget gained ↑/↓ volume nudges (±5,
    matching the mini). Both surfaces skip ±10 s via ⏪/⏩ buttons and
    ←/→ keys — PlayerWidget.SKIP_SECONDS (10) and the mini's hardcoded
    ±10 are pinned in lockstep by test_mini_player.py; keep the two in
    sync if either ever changes.
- MINI-PLAYER LOGO INTRO (9.6 shipped an endless loop; owner 2026-11-23
  SAME-DAY REVERSAL after seeing it live: "the video still loops
  endlessly, it should play only once and after show the static image"):
  the mini player's IDLE STATE is the STATIC owner banner (_IdleBanner);
  the bundled repo-root `logoloop.mp4` (owner file, 9.3 MB / 5.04 s h264
  2362x876 + AAC audio bed; committed; ships via app.spec +
  app_linux.spec datas — macOS has no mini player) plays ONE audible,
  NON-looping pass per WINDOW LIFETIME and lands on the banner at natural
  EOF. Durable rules (these SUPERSEDE the 9.6 loop contract — the
  wrap-muting `_logo_loop_wrapped` and the `_LOGO_MAX_RESTARTS` restart
  machinery are GONE):
  * ONE pass per window (`_logo_played` gate in `_start_logo_loop`): on
    the mini's first idle after open (run_mini_player's 400 ms timer) —
    or, if media opened immediately, on the FIRST ⏹-stop / morph-back.
    Every later idle return goes straight to the banner. A fresh app run
    is a fresh window → the intro plays again.
  * AUDIBLE pass unless the user muted the mini (config wins); `set_loop_file(False)`
    explicitly. While `_logo_mode`, M and the volume slider are inert; real
    media (`open_media`) restores the user's mute/volume.
  * Any backend "stopped" while `_logo_mode` = intro finished OR broken
    file → banner (the graceful fallback; no restarts, no loops).
  * ⏹/S during the intro SKIPS to the banner (backend stop + idle page).
    `_last_media` stays "" during the intro: NOT resumable media (a morph
    from it lands on the music-autostart branch, never a Play-tab resume).
  * Exits: natural EOF, ⏹/S skip, real media, morph_to_full/_stop_playback
    (loop off — no video under the full app), window close. The intro never
    touches agent music (mini-only sessions are silent by design).
  * Tests: tests/test_mini_player.py — the `mini` fixture's stub backend
    records mutes/loop_file/volumes; tests that spin a REAL window via
    show_mini_player must monkeypatch `resolve_logo_video_path` to "" (the
    same seam rule as the music autostart — else a real libmpv plays).
- MENU BAR + NAVIGATION (7.6 restructure, owner decision 2026-11-07):
  the v5 left activity rail is GONE (gui/activity_rail.py deleted). Tab
  navigation = small icon-only checkable QActions LEFT of File
  (`_TAB_ICONS` map, `_sync_tab_icon_actions` keeps checked = current
  tab, `_set_download_badge` puts the active-transfer count on the ⬇
  icon's text). Menu order: icons · File · Settings · API · Downloads ·
  Browser · Play (Play gated by _PLAY_TAB_SUPPORTED) · Help. The
  Settings menu keeps ONLY app-level items (Hub, Language, Font Size,
  Text Color); the feature zones moved to their own top-level menus
  (API = AI & API Keys pages; Downloads = Jackett + download pages +
  Sources + RSS; Browser = browser pages + History + DevTools; Play =
  IPTV pages). The browser left-edge RAIL auto-hide died with the rail —
  the hover poll is CHROME-ONLY now (`_rail_*` symbols all removed);
  pure fullscreen (`set_video_pure_chrome`) snapshots only the agent
  panel. Do not re-add a left rail or rail-adjacent pollers.
- AGENT TASK SESSIONS sidebar (2026-11-22, owner: "the agent should have a
  left side bar with sessions, like zcode, we can call them tasks"):
  every Agent-tab conversation AUTO-SAVES as a local task under
  `~/.deeptorrent/agent_sessions/` (env `DF_AGENT_SESSIONS`, redirected by
  tests/conftest.py) and the left sidebar (`gui/agent_sessions_bar.py`,
  pure view — no store access) lists them newest-first (name over a dim
  preview, click/Enter/double-click to reopen, "✎ Rename" /
  "🗑 Delete" / "➕ New task" LABELED buttons — owner 2026-11-23 after the
  icon-only versions read as mystery glyphs; task text sized UP the same
  day: name +2 font steps DemiBold, preview +1 dim, 48px rows). Durable
  rules — do not regress:
  * AUTO-SAVE ALL (owner decision): a task is created lazily on the first
    send (auto-named from the first message, ≤48 chars), then saved
    atomically (tmp+os.replace, `agent/sessions.py::SessionStore`) after
    `_append_user` (with the outgoing message SPLICED IN — agent.chat
    appends it only on the worker thread, so the pre-turn save captures
    exactly the crash-mid-turn state), on `_on_agent_finished` (both the
    agent and the web-sweep branches), on errors, on switch/new/delete,
    and in closeEvent. Boot starts BLANK by owner decision — nothing
    reopens; saved tasks open only on click.
  * TWO payloads per file (`<uuid-hex>.json`, schema-versioned): the
    OpenAI-style `messages` (so a reopened task resumes FULL LLM context
    — `_load_session` replaces `agent.history`; `_reload_agent` carries
    history across settings reloads via `_prev_agent_history`) and the
    rendered `transcript` lines `{"k","t"}` (user/search_user/agent/
    event/error/pending/search_html) so reopening looks identical.
    `_replay_transcript` deliberately does NOT call the `_append_*`
    builders (they mirror to the agent panel + status bell + re-log).
    `sanitize_messages` strips a trailing dangling tool-call exchange
    before every save — INCLUDING a PARTIAL reply set
    (assistant(c1,c2)+tool(c1); loop.py appends the assistant with all
    tool_calls before replies land one-by-one) — and `_load_session`
    re-sanitizes on restore; a broken exchange anywhere in the tail must
    never reach the LLM API (review HIGH 2026-11-22, pinned twice in
    tests/test_agent_sessions.py). NOT captured in transcripts by design
    (documented v1 scope): streamed token spans sealed mid-turn + debug
    dumps — the final agent bubble + full LLM history always are.
  * Clear/➕ New DETACH (the old task stays saved until deleted);
    delete of the OPEN task blanks the chat. Web sweeps are tasks too
    (search_user + search_html, empty messages). The sidebar is DISABLED
    while a turn or sweep runs (`_set_busy`/`_set_search_busy` evaluate
    BOTH flags — a sweep ending must not re-enable mid-turn) AND
    `_tasks_locked` also checks `_agent_thread`/`_search_thread`
    ALIVENESS: a sweep past its 60s watchdog clears `_search_busy` while
    its worker still renders late results — a switch then would persist
    those results into the WRONG task's file (review MED 2026-11-22;
    `_search_timed_out` re-locks the bar for the same reason).
  * ALL LOCAL-ONLY: no network, no telemetry, nothing in config.json.
  * RESPONSIVE retrofit that shipped with the sidebar: the Agent tab used
    to demand ~1250px (onboarding cards + input row — never covered by
    test_responsive because the tab is built inline); it is now a
    ResponsiveRow+OverflowRow citizen (cards fold rightmost-first; input
    row folds ♫/🎤/Status/YOLO/Clear) and the "agents" param in
    test_responsive.py pins ≤700×460 (measured 490px).
  * PySide6 NATIVE-CRASH LESSON (bisected via bare QListWidget, 2026-11-22):
    `QSize.expandedTo` has NO (int, int) overload — the TypeError escaping
    a QStyledItemDelegate override (sizeHint/paint) into the C++ item-view
    layout call SEGFAULTS the process with NO Python traceback (looks
    exactly like the environmental AV family — check WER is empty for
    today and bisect with a bare widget before blaming the dice). Build
    QSize explicitly in delegates; pinned by
    test_gui_render.py::test_task_delegate_sizehint_is_native_safe.
- AGENT BACKGROUND MUSIC (8.3/8.4, owner 2026-11-18, `gui/agent_music.py`):
  a ♫ toggle in the agent input row ONLY plays the bundled repo-root
  `backgroundmusic.mp3` (owner file, 74.5 MB / 1h17m, committed; ships via
  app.spec + app_linux.spec datas — macOS has NO button, no file; the mini
  player has NO button either, owner removed it after 8.3 — mini-only
  sessions are silent by design). Audio-only second libmpv (`video=False`
  so ID3 cover art can never open a window, `loop_file="inf"`, volume knob
  = `agent_music.VOLUME`). STARTS when the FULL app opens (owner: "once we
  open the full player the music didn't start") — both entry points call
  `maybe_autostart`: run_gui's post-show timer (skipped on association
  launches) and the mini→full morph completion's else-branch (suppressed
  when a target routes or the last video auto-resumes — media wins).
  UNCONDITIONAL since owner request 2026-11-19 ("it should start white
  and playing"): the music starts on EVERY full-app open; the ♫ button is
  a SESSION-ONLY stop (no preference consulted or persisted — the old
  top-level `ui_background_music` config key is RETIRED; from_file drops
  a stale copy from old configs, and a legacy object exposing the attr
  must not gate the boot either — pinned by
  test_maybe_autostart_is_unconditional). tests/test_mini_player.py's
  shared `mini` fixture therefore neutralizes the maybe_autostart SEAM
  (monkeypatch to no-op) — the config can no longer suppress it, and an
  unpatched morph would spin a REAL mpv.
  Contract: ANY media opening stops it (`stop_agent_music()` hooks:
  mini open_media, PlayerWidget.play, GridTile.assign — browser-tab web
  video does NOT count) and it never auto-resumes; music playing when the
  full app morphs back to mini KEEPS playing (no toggle there — the
  contract hooks stop it on media); MainWindow.closeEvent terminates the
  handle (live-libmpv-at-exit class). Button colors (owner 2026-11-18;
  idle darkened 2026-11-19, then idle changed to BLUE accent like the
  sibling buttons by owner request 2026-11-22 — "pure white while playing
  and normal blue when not playing, like the other buttons"):
  WHITE fill while playing (`QPushButton#btn_music_on`, dark #0a1a2e text
  — the one non-white color literal, in SEMANTIC_TEXT_COLORS), BLUE
  btn_accent chip (#2a7abf, same as Search/Ask Agent) when idle; swapped
  via the YOLO objectName+repolish pattern in `_sync_music_button`.
  Tests: tests/test_agent_music.py
  (fake-mpv seam + source-pinned hooks), tests/test_mini_player.py (button
  e2e incl. color contract, morph-autostart + media suppression,
  no-mini-button pin).
- MATRIX RAIN SPEED (owner 2026-11-18 "reduce the speed"):
  `gui/matrix_rain.py` SPEED_MIN/MAX 0.12–0.55 cells/tick (was 0.25–1.1
  at the same 18 fps); pinned by tests/test_matrix_rain.py.
- RESOURCE FRUGALITY (owner directive 2026-11-20: "only take the
  resources needed for the current tasks, park other processes"):
  * **curl_cffi streaming has NO backpressure** — its sync lane runs the
    WHOLE transfer on its own thread into an UNBOUNDED queue (verified in
    its session.py: `c.perform()` on an executor thread, plain
    `queue.Queue`); a consumer that blocks (old token bucket) or just
    writes slower than the network (6 workers random-seeking an HDD) lets
    the ENTIRE remaining response park in RAM. MEASURED live: a 9 GB
    Windows 11 ISO → a 10.3 GB process on 8.9. Fixes (all pinned by
    tests/test_dlmgr.py): `http_client.USING_CURL` tells the lanes apart;
    the curl lane throttles at the SOCKET (`max_recv_speed` =
    CURLOPT_MAX_RECV_SPEED_LARGE, split across the worker budget —
    consumer-side `limiter.acquire` is SKIPPED there and stays only on
    the requests fallback, which has natural TCP backpressure);
    SegmentWorker carries a BUFFER GUARD (response queue > 512 chunks ≈
    32 MB → abort + park PENDING, close() sets quit_now and aborts the
    producer); the engine's `_respawn_orphan_segments` monitor step
    restarts PENDING segments with NO live worker WITHOUT consuming
    retries (also the general fix for the 2026-09-15 orphan-hang class
    the rebalancer used to paper over with ERROR+retry).
  * **IPTV `_BoundedExecutor` is LAZY + SELF-RETIRING** (iptv/artwork.py,
    shared by artwork 12 / metadata 16 / framegrab 2 = 30 EAGER threads
    the moment the full app opened, all idle unless Play is used):
    construction spawns ZERO threads, workers grow on demand when the
    queue backs up, each retires after 30 s idle; shutdown sentinels
    still jump queued ADVISORY work (old semantics, pinned). MEASURED:
    full app 36 → 6 threads at idle, RSS unchanged ~240 MB.
  * **Commander folder-size walks ABORT on tab-hide/Size-off**:
    `set_sizes_active(False)` and `set_folder_sizes(False)` call the
    engine's `forget(paths)` (drops the wanted-map entries —
    `wanted.get(path) is None` matches no generation, so in-flight
    walkers raise _Cancelled within 64 entries) AND bump the pane gen.
    A pane-side gen bump ALONE aborts nothing (the wanted entry still
    matches the walk's generation). MEASURED: a C:\ root walk held ~98%
    of a core; after leaving the tab → 7.3% idle (rain only). While the
    tab is VISIBLE the walk at full speed is the feature working on
    demand.
  * Mini player: 1 thread / 59 MB (already the frugality benchmark);
    morph cycles leak nothing (MainWindow reuse, -1 thread delta).
    Matrix rain: ~7% of one core while visible (intended visual), timer
    already stops when hidden/after first message — do not "optimize"
    it further without an owner decision.
- COMMANDER SIZE COLUMN (owner request 2026-11-19, "show file sizes and
  folder sizes, default on"): a checkable "Size" button in the Commander
  F-key bar (first to overflow into the ⋯ menu) toggles the panes' Size
  column; ON (default) additionally shows RECURSIVE FOLDER SIZES —
  QFileSystemModel leaves a directory's Size cell blank, so a
  `_FolderSizeDelegate` (gui/commander_tab.py) paints the computed size
  (or "…" while walking) through the view's own item style; files keep
  the model's text. Durable rules — do not regress:
  * ONE shared background engine (`_folder_size_engine()` module
    singleton, single daemon thread, session cache keyed by path,
    generation-based stale-skip): a full tab strip carries up to 24
    panes — never one thread per pane. The walker is ITERATIVE
    (deep trees), never crosses link/junction boundaries
    (CommanderTab._is_boundary), polls a cancellation callback every 64
    entries, and a walker exception is logged-and-skipped (a dead worker
    would freeze every folder at "…" forever).
  * ACTIVITY GATE (System Health doctrine): walks run ONLY while the
    Command tab is the current main tab — main_window's
    _on_main_tab_changed flips commander_tab.set_active both directions.
    Without this, every launch would background-walk the whole system
    drive (the right pane starts at `_fs_root()`). An invalid view root
    (the drive-list fallback) never queues walks either. Pinned by
    test_folder_sizes_wait_for_tab_activity.
  * Config `ui_commander_folder_sizes` (top-level bool, default True —
    the agent can never switch it off, same rule as the other ui_*
    fields); the toggle applies to every pane incl. tabs opened later.
  * AutoColumnSizer gained an optional `text_override` callable
    (gui/column_sizing.py) so auto-fit widths account for
    delegate-painted cell text the model doesn't carry; the downloads
    tables construct it without the param (unchanged).
  * Tests: tests/test_commander_tab.py (walker/engine/gate/delegate/
    toggle/config round-trip) + test_column_sizing.py text_override.
- NO AUTOMATIC MAIN-TAB JUMPS (owner decision 2026-11-17, "automatic tabs
  jumping are annoying"): background work must NEVER switch the main tab —
  the user keeps reading whatever tab they are on. Removed that day:
  BrowserBridge._focus (agent browser_navigate/browser_switch_tab/go-home
  used to force the Browse tab forward on every agent search),
  `_on_agent_event`'s add_download/add_magnet/add_torrent_file success jump,
  `_route_browser_download`'s, `_on_browser_download_state`'s and
  `_on_downloaded_torrent_file`'s jumps, and browser_channel.sendDownload's
  jump (the toast/agent-message feedback stays). Deliberately KEPT (direct
  user intent, do not "fix" these): menus/shortcuts/tab-icon actions,
  Ctrl+L/find, drag&drop adds, chat search-result link clicks, the Add
  Magnet dialog, `open_target` external opens (they also raise the window),
  and IPTV play focus (iptv_tab `_focus_tab` — playing means watching).
  Pinned by tests/test_browser_bridge.py (navigate/go-home/switch/channel
  no-steal tests) + tests/test_v5_ui.py::test_agent_event_tool_end_does_
  not_jump_tabs. Re-adding a jump anywhere needs a new owner decision.
- AGENT VIDEO LANE (P0/P1 of AGENT_VIDEO_STUDY.md, shipped 2026-11-17):
  `scan_video_page` (read-only, UNTRUSTED_RESULT_TOOLS) wraps
  GenericExtractor.extract — the SAME resolver the GUI Site Grabber uses
  (packed-JS unpack, ranking, preview demotion); extract_from_page now
  also returns `alternates` (≤5 ranked runner-ups). `download_video`
  (CONFIRMATION class, like add_download) resolves a page/stream/YouTube
  URL and queues with headers={Referer, UA} + referrer + source_url —
  NEVER queue a page-found stream via bare add_download (drops Referer →
  hotlink CDN 403; surrit verified live). YouTube routing lives in
  download_video AND add_download now (yt-dlp, not the HTML-saving HTTP
  downloader). Per-request `max_height` caps HLS renditions:
  add_stream_job(max_height=) + DownloadJob.max_height (restart-safe),
  per-call cap beats config stream_max_height; unknown-height renditions
  stay eligible (existing semantics). HLS masters auto-select the best
  rendition in the engine — the prompt doctrine tells the model to prefer
  masters. MissAV class split, verified live 2026-11-17: missav.ai AND
  missav.live work end-to-end through the plain-HTTP lane (surrit master in
  packed JS; missav.live listing pages like /dm265/en enumerate cleanly);
  missavtv.com (and its embedUrl player hosts) is fully JS-gated —
  scanner-invisible, needs the browser detection lane (study P2, not yet
  built). LISTING-PAGE DISCOVERY (owner report "agent has a hard time to
  pick videos from <listing url>", 2026-11-17): the agent's web_fetch lens
  only sees media/torrent HREFS — a listing page has none, so the model
  guessed page URLs from truncated text. `list_page_videos` (read-only,
  untrusted-marked) wraps SiteGrabber.parse_cards → title/url/duration per
  card, limit 30/100, JS-shell hint when no cards parse; the prompt's
  Video-grabbing section says ALWAYS call it first on a page full of
  videos. Tests: test_tools.py video
  section, test_site_grabber.py alternates, test_dlmgr.py rendition
  caps. DEAD-SEGMENT TOLERANCE (owner 2026-11-17, same day): a CDN that
  403/404s a handful of segments DETERMINISTICALLY (measured live: 1 dead
  object of 1920 on surrit, failing all 10 retries) no longer kills the
  whole capture — up to 0.5% of segments (strictly proportional, NO floor:
  streams < 200 segments get zero tolerance; half a short video missing
  must still fail) are skipped, the remux concatenates around the gap, and
  the job COMPLETES with "Completed with N dead segment(s) skipped" in
  error_message (the Downloads table message column shows it on the
  completed row; status stays Completed). HLSDownloader.failed_segments
  carries the (index, error) list; beyond tolerance raises the old
  "N/M segments failed" error.
- FAULTHANDLER CRASH LOG (main.py::_enable_crash_log, added 7.4 2026-11-06;
  fixed 2026-11-07 after it KILLED the app 11x that night): on Windows
  `faulthandler.enable()` registers a VECTORED exception handler that fires
  on EVERY raised exception on ANY thread — including ones the raiser
  catches itself. libmpv's native threads raise-and-catch code
  0xe24c4a02 (~2x/h during IPTV playback; benign, invisible before 7.4);
  faulthandler's default ALL-THREADS traceback walk then ran from that
  foreign thread and itself AV'd (WER dumps: AV in
  Py_DumpTracebackThreads under RtlpCallVectoredHandlers on a libmpv
  thread), killing the process. RULE: the call MUST pass
  `all_threads=False` (dumps only the faulting thread; verified safe for
  no-tstate foreign threads via a ctypes RaiseException harness — benign
  mpv exceptions just leave a 2-line header in
  `%LOCALAPPDATA%\DeepFlux\faulthandler.log`). Pinned by
  tests/test_crash_log.py — which since 2026-11-07 pins EVERY repo
  `faulthandler.enable()` site (app code AND packaging): the same bug was
  found still live in `packaging/verify_boot.py` (bare enable → the boot
  smoke was killing ITSELF on Windows; one WER dump died inside
  Py_DumpTraceback itself). Diagnosis tools that earned their keep:
  `%LOCALAPPDATA%\CrashDumps` (Win11 default WER dumps) + winget
  Microsoft.WinDbg (cdbX64.exe `-z <dmp> -c ".ecxr; k"`) + the
  faulthandler.log itself.
- 2026-11-07 STABILITY INVESTIGATION (owner: "app became crash-prone after
  the 7.4 AI review" — verdict: NOT 7.4's code): a release-tree bisect
  (pre-7.4 / 7.4 / 7.5, boot smoke 5x + a 4-file pytest combo 3x per tree)
  came out FLAT — the combo crashes identically on all three trees and
  WER ReportArchive shows this machine's python3.11.exe native crashes go
  back to 2026-09-13, two months before 7.4. The real story: (a) 7.4's
  faulthandler genuinely killed the app for one night (fixed in 7.5, and
  its verify_boot twin fixed 2026-11-07, see above); (b) the same logger
  made pre-existing crash noise VISIBLE (9 of 15 faulthandler.log entries
  are benign mpv 0xe24c4a02 headers); (c) the residual family — AVs
  detonating in innocent pure-Python frames (EPG ElementTree, artwork
  os.stat, agent json.dumps, urllib3 reads) in giant single processes —
  is an environment-level heap/layout artifact (Store-Python 3.11 + the
  native stack; bytecode-shape sensitive). Real app bugs fixed on the way:
  mpv observers must NEVER re-enter libmpv natively from inside a
  callback (`self._mpv.pause` read in `_cache_stall` raced teardown —
  track state in a Python attr, see iptv/player.py), and shellapi ctypes
  structs need `_pack_ = 1` (SHFILEOPSTRUCTW in commander_tab read pFrom
  from padding bytes — latent since 3.4.1). `run_gui`'s gc.freeze() now
  collect()s first (frozen cycles leak for the session). Do NOT re-open
  the "which release broke stability" hunt — the bisect data is in
  /tmp/bisect_* logs and the memory file. Open follow-up (owner
  decision): try a python.org (non-Store) Python 3.11 + clean venv to
  test the environmental-family theory; prime in-app suspect for deeper
  audit: `_rasterize_svg`'s off-GUI-thread QtSvg/QFont work
  (iptv/artwork.py, audit confidence 25%).
- 2026-11-08 CRASH WAVE, RESOLVED — environmental (NVIDIA half-install);
  do NOT chase as an app bug. Owner report: "client closes when connecting
  to Jackett". Forensics (faulthandler.log + 5 WER dumps + event log):
  SEVEN native crashes in one day (07:53→21:22), ALL byte-identical —
  python311.dll+0x49245 (object-allocator region, nearest export
  PyType_GenericAlloc), READ of the same garbage pointer 0x400000002, on
  whatever worker thread was allocating. Trigger paths = everything
  allocation-heavy: Jackett Test Connection ×3
  (settings_dialog._jkt_fetch_worker → jackett.fetch_indexers_detailed),
  the hourly Jackett auto-sync, an agent search_indexers call (died the
  instant a 1337x timeout fired), the segmented updater download, and
  graceful SHUTDOWN 3 s after the state save. Cross-BUILD (the 07:53 crash
  ran the pre-7.6 binary — proven via the updater's strict is_newer gate +
  last_check; evening crashes ran 7.6) → release-independent, matching the
  11-07 bisect; only DeepFlux.exe crashed machine-wide (3-day event log).
  ROOT CAUSE: a half-completed NVIDIA installation — driver package
  staged into DriverStore 19:59 while the App/overlay stayed mismatched
  (nvspcap64.dll is injected into every DeepFlux process since ≥09-05;
  the half-state likely left by overnight KB5127691). FIX = letting the
  NVIDIA App finish its installation ended the wave (owner-verified).
  The 11-07 "residual family" above is this same actor. RULE: if the
  signature returns (CPython allocator AV + fixed garbage pointer +
  innocent Python frame on top), suspect the environment FIRST (NVIDIA
  overlay/driver state, recent Windows updates), never the code path the
  faulthandler stack shows. Forensic kit that worked (cdb is NO LONGER
  installed on this machine): %LOCALAPPDATA%\CrashDumps +
  %LOCALAPPDATA%\DeepFlux\faulthandler.log + `Get-WinEvent
  -FilterHashtable @{LogName='Application'; Id=1000}`; parse minidumps
  with a PowerShell BinaryReader (exception stream type 6, module list
  type 4 with 108-byte entries; the exe's in-dump VersionInfo is 0.0.0.0
  — useless), then PE-parse the faulting DLL ON DISK for bytes/nearest
  export at the fault RVA (PE32+: NumberOfRvaAndSizes @optStart+108, data
  dirs @+112; export dir: NumberOfFunctions @+20, AddressOfFunctions
  @+28). CPython runs a public symbol server,
  https://symbols.python.org/python311.pdb/<GUID+age>/python311.pdb
  (bundled DLL = 3.11.9, sig FC457C591F7D4CC09AC2DB42401CE2C7, age 1).
  Leftover: %LOCALAPPDATA%\DeepFlux\updates\DeepFlUX7.6Setup.exe.part
  (414 MB) is a dead interrupted download — deletable. Machine drift
  noticed 11-08: git AND Python are gone machine-wide (registry, Appx
  packages, PATH all empty). RESOLVED 11-09: git works again and a
  python.org 3.11.9 + the repo venv (.venv\Scripts\python.exe, pytest
  9.2.0) run the verify/test commands in this file fine — only the BARE
  `python`/`py` PATH shim is missing, so the README pre-commit hook needs
  the venv on PATH: `PATH=".venv/Scripts:$PATH" git commit ...`.
- 2026-11-10 RELEASE 7.7 + two incidents, durable lessons:
  * 7.6.1 SHIPPED BOOT-BROKEN: its bundle carried backports/zstd/ with ONLY
    the .pyd (namespace dir) — urllib3 2.8's `backports.zstd.ZstdError`
    lookup crashed AT IMPORT ("Failed to execute script 'main'"), before
    any logging. Compounding: installer.iss had NO [InstallDelete], so the
    stale dir SURVIVED upgrades and poisoned 7.7-installed-over-7.6.1 too.
    FIX (shipped in 7.7's setup): [InstallDelete] wipes {app}\_internal on
    every install (safe — per-user data is in ~/.deeptorrent, the
    flaresolverr/ tree sits NEXT TO the app, PINNED.json re-ships). RULE:
    any bundle-content change across releases NEEDS this wipe; a boot
    crash leaves NO log — reproduce with the frozen exe, read the dialog.
  * git-push + artifact downloads from this machine's network CRAWL
    (~50 KiB/s, hours for 600 MB) while RELEASE assets are fast (9+ MB/s).
    linux-build.yml gained an optional dispatch input `upload_release`
    that uploads the AppImage to a DRAFT `linux-staging` release —
    download that, then DELETE the release (transport lane only). The
    GITHUB_TOKEN needs `permissions: contents: write` (added).
  * The deploy PAT is fine-grained: needs Contents+Workflows+Actions
    (read and write) on this repo — one toggle each, or push/dispatch
    403s with confusing messages.
  * win_taskbar's _GUID must stay on plain ctypes primitives — a
    module-level wintypes reference crashes the Linux/mac builds AT
    IMPORT (CI run 42809567604); pinned by a POSIX-import subprocess test.
- 2026-11-21/22 CRASH WAVE #2 (owner: "started crashing, even on 9.2"),
  RESOLVED environmental — the 11-08 signature returned: 5 identical
  python311.dll+0x49245 allocator AVs (22:27–22:46), victims all innocent
  worker allocators (IPTV playlist json.loads ×4, local_model
  server_status ×1); identical on 9.2 AND 9.3, SURVIVED a full reboot
  (fast startup off → NOT the per-boot ASLR dice), the pinned VC runtime
  loaded consistently (no dual CRT in the module list), nvspcap64 NOT
  injected, kernel/UMD driver versions matched. Machine state: NVIDIA
  610.88 was re-staged 11-20 22:41 with IObit Driver Booster active — a
  BARE driver install: NVIDIA App + every companion component ABSENT
  (11-08's fix actor was the App completing). FIX = clean reinstall
  (owner: Custom → "perform a clean installation", then updated to
  621.24 + reboot) — full component suite restored, no recurrence since
  (watcher-monitored through the release session). RULES: Driver Booster
  must never touch GPU drivers again; the missing
  HKLM\SOFTWARE\Khronos\Vulkan\Drivers key observed during forensics is
  NORMAL on this machine (still absent after the known-complete 621.24
  clean install) — NOT a defect marker; and DeepFlux's GPU lane is
  D3D11 (WebEngine), not Vulkan, so that key is irrelevant to crashes.
- IPTV VOD CLASSIFICATION — dated upload paths (9.4, 2026-11-22): whole
  providers score zero on every VOD signal — atsushi444's xxx.m3u carries
  74,761 entries with Chinese group names (国产视频, 日本有码…) that match
  no English group keyword, and every URL ends in the live-extension
  .m3u8 — so the entire library fell into Live TV and the ⬇ Download
  button never appeared. iptv/classify.py now treats a DATED URL path
  (/video/m3u8/2025/09/06/<id>/index.m3u8 — live streams never carry an
  upload date) as a VOD signal → Movies, or Series when the name has
  SxxExx. Precedence kept: 24/7-channel naming ("UK: FILM4 HD") still
  wins, plain .m3u8 without a date path stays live. Pinned by
  tests/test_iptv.py::test_classify_entry_dated_url_path_is_vod. NOTE:
  the sqlite cache stores the CLASSIFIED result — existing sources keep
  their old (wrong) sections until refreshed.
  PER-SOURCE OVERRIDE (owner request 2026-11-22: "the user can also decide
  if this is a live stream or a VOD, so like this would cover the gap for
  all cases"): `content_type` field on IPTVSourceConfig/PlaylistSource —
  "auto" (default, heuristics) | "live" | "vod". `classify(pl,
  forced=source.content_type)` — forced LIVE pins every entry to channels
  (even .mp4 URLs); forced VOD promotes every entry (SxxExx names →
  Series, rest → Movies); the override beats BOTH the heuristics AND
  local_folder's extra["local_section"] stamp (explicit user intent
  wins). UI = "Content:" dropdown (Auto (detected)/Live TV/VOD) in the
  source edit dialog (gui/iptv_settings_dialog.py); agent gets
  content_type on iptv_add_source/iptv_update_source (validated, ToolError
  on bad values) and in iptv_list_sources. Takes effect via the normal
  refresh (dialog OK → reload_config → background re-fetch re-classifies
  and replaces the cached sections — classify() is NOT idempotent, never
  run it twice on one playlist). Pinned by
  test_classify_forced_override_beats_heuristics +
  test_refresh_source_content_type_override +
  test_config_source_content_type_roundtrip +
  test_gui_render.py::test_iptv_source_edit_dialog_content_type_roundtrip +
  test_agent_setup_tools.py::test_iptv_source_content_type_roundtrip.
  zh: 直播/点播 (i18n_zh_cn.py).
- IPTV METADATA FOR CHINESE-NAMED VOD + LOADING COMFORT (2026-11-22,
  owner: "can we find the correct metadata and movie posters?" + "comfort
  is more important than speed"). Durable rules — do not regress:
  * **missav provider** (iptv/metadata.py::MissavProvider): keyless JAV
    search by CHINESE title via missav's Recombee frontend API — POST
    https://client-rapi-missav.recombee.com/missav-default/search/users/
    <uuid>/items/?frontend_timestamp=<epoch>&frontend_sign=<HMAC-SHA1 hex
    of the path-with-timestamp, key = the public token baked in their JS>;
    item id = JAV code; posters are the deterministic CDN pattern
    https://fourhoi.com/<code>/cover.jpg|cover-t.jpg (plain HTTP, no CF;
    raw requests suffice — NO missav.ai page fetches). MEASURED on
    atsushi444 xxx.m3u: playlist names are independent re-phrasings, exact
    title match = 3/108 → `fetch` accepts ONLY ≥0.85 CJK similarity
    (cjk_similarity; ≥8-char containment scores 0.9 for truncated names)
    with actress-suffix (split_actress_suffix) corroboration as a
    tie-break — a WRONG poster is worse than none. Lives FIRST in the
    adult-no-code chain branch; `enable_missav` (IPTVConfig, default on)
    gates the auto chain only — the DetailPanel Identify picker keeps
    working when it's off (provider object always constructed). Old
    negative-cache rows auto-retry it (per-provider misses, 6h TTL).
  * **CJK adult routing**: `_ADULT_CJK_GROUP_RE` (group-title-only) sends
    有码/无码/国产视频/国产新片/猫步毒药 groups down the adult chain —
    欧美高清 is DELIBERATELY excluded (a legit western-movies group could
    carry that exact name); existing Latin regexes are unchanged.
  * **Manual overrides are USER DATA**: cache table metadata_overrides
    (source_id, item_id PK — NOT title-keyed) survives clear_metadata;
    set_entry_override MERGES into the stored payload (an AI translation
    must never drop an earlier Identify poster); `_apply_overrides` runs
    beside `_apply_favorites` on every load (both the cache-first copy and
    the fresh parse). display_title/original_title/override_code are
    RUNTIME attrs stamped from the table — never model fields.
  * **Identify picker** (DetailPanel): 🔍 button → inline candidates from
    manager.missav_search_async (live, uncached, ≤8) → click applies the
    override + title2/second-line + poster; ✕ Clear match reverts. All
    remote strings pass through _clip_text at the sink. "文A Translate"
    button (visible only when an LLM key is configured) makes ONE
    on-demand low-effort fast-model call per click, stores the result as
    the entry's display_title override — NEVER automatic/bulk (API-spend
    guard). Provider misses now CLEAR "Loading metadata…" (the old
    forever-stuck state); the canonical title rides the tile's dim second
    line (_TITLE2_ROLE, painted like the EPG line).
  * **Loading comfort doctrine (owner directive)**: (1) grid populate is
    CHUNKED (ContentGrid/ContentList._populate_step, 1000 rows per 0-ms
    timer turn, first batch synchronous — a 74k-row build was a 1-3 s GUI
    freeze; measured now: first batch 13 ms). (2) the WHOLE-SECTION
    artwork sweep is RETIRED — artwork/metadata resolve ONLY for the
    visible window + prefetch margin and detail-panel opens (the old sweep
    churned 5 lookups/s for ~4 h per view on a 74k logo-less section);
    pinned by test_grid_never_bulk_resolves_logo_less_entries. (3) bulk
    loads run the process at BELOW_NORMAL priority (_LoadPriorityGuard,
    ref-counted; GetCurrentProcess's pseudo-handle is REJECTED by
    SetPriorityClass in some hosts — use OpenProcess on os.getpid()) and
    pools are trimmed (metadata 16→8, artwork 12→8, prefetch 250→120).
    (4) `_playlist_signature` (sha1 over entry ids + url_tvg, on the
    playlist object) skips BOTH the redundant multi-MB cache re-serialize
    AND the grid/sidebar rebuild for unchanged fresh publishes — the
    cache-copy + fresh-copy double publish used to freeze the UI twice
    per refresh. CRITICAL invariant (review HIGH finding, pinned by
    test_identical_republish_keeps_live_generation): on an identical
    republish the manager keeps the LIVE playlist objects installed
    (`_refresh_source_inner` returns the previous copy instead of the
    fresh parse) — swapping in a byte-identical new generation would
    orphan the objects the grid binds to, and any Identify pick/favorite
    toggled after the swap would silently vanish on the next view switch.
    (5) global search runs off the GUI thread (manager.search_async +
    _signals.search_done, seq-guarded — the seq is bumped on EVERY
    _on_search path, incl. clearing the box and the folder-scope toggle,
    so a late result can never overwrite a restored view).
  * Known gaps (pre-existing, kept): reload_config does not re-apply the
    enable_* provider flags (manager is constructed once); the scoped
    folder search still filters on the GUI thread (usually a small slice).
- 2026-11-09 full-codebase review (8 reviewers over the churn since
  3c9d492: 134 commits, 181 files, +31k/−7k lines). Findings + fix status
  live in REVIEW-2026-11-09.md (tracked). All 4 HIGH + 11 MED + Tier 3
  fixed the same day, each with teeth-verified regression tests (stash →
  fail → restore → pass). Durable lessons — do not regress:
  * local_model's `_server_lock` MUST stay an RLock (start_server calls
    stop_server under it on the model-switch path).
  * `engine._BandwidthLimiter.acquire` must grant oversized requests once
    the bucket is full (chunk 64 KiB > a 1–63 KB/s limit hung every
    download); scheduler.py's TokenBucketThrottler is DEAD CODE — the
    engine never used it.
  * GGUF/remote-body parsers: at EOF reads return b"" WITHOUT raising —
    every count/length needs a cap and short reads must fail closed.
  * python-mpv observers NEVER read native properties (not even
    `self._mpv.duration`) — mirror observed state into Python attrs.
  * Lazy `X+?\.Y` regexes WITHOUT nested quantifiers are LINEAR (measured:
    the "page_scan ReDoS" was a false positive, 0.1–0.3 s on 5 MB pages).
    The REAL hostile-shape hazard is tag-pairing regexes (`<a…>(.*?)</a>`)
    and no-`>` attr bombs — site_grabber now scans SEGMENTED
    (`_iter_pairs`, split on the close tag) and every attr class excludes
    `<` and is bounded; keep new HTML regexes in that shape.
  * The Sources dialog's table shows 5 columns but the config carries
    origin/torznab_caps/popularity/categories — `_save_and_accept` must
    carry them over by id (wiping origin zombified manual sources).
  * Windows ctypes: `ctypes.byref` needs a real ctypes struct (uuid.UUID
    raised TypeError and silently killed the taskbar feature for a
    release); ITaskbarList3 progress = vtable slots 9/10, NOT 11/12.
  * Runtime-downloaded payloads are digest-pinned: Linux Jackett reads
    packaging/jackett/PINNED.json's `linux` entry (shipped via app.spec +
    app_linux.spec), and the Windows bundled installer is sha256-gated
    before its elevated run. fetch_jackett.py writes both entries.
- 2026-09-15 total-codebase review (6 parallel reviewer agents; 37 findings,
  ~30 fixed, all with file-level rationale comments + regression tests).
  Durable rules that came out of it — do not regress:
  * `config.to_file` is ATOMIC (module lock + temp + os.replace) — many
    threads write config.json; a truncated file loads as {} and silently
    wipes sources/keys. NEVER reintroduce a bare `open(path, "w")` write.
  * EVERY config section constructor filters unknown keys (`__dataclass_fields__`)
    — a config written by a newer/hand-edited build must not crash startup.
  * Jackett `merge_sources` KEEPS entries missing from the fetched snapshot
    (manual sources were silently wiped hourly); stale Jackett-gone entries
    linger by design — removable by hand.
  * Agent HTTP fetches are STREAMED with hard byte caps (public_http_get
    FETCH_BODY_LIMIT; RSS 5 MiB during read; .torrent 32 MiB) — never buffer
    an unbounded remote body into the GUI process.
  * Source/feed URLs shown to the LLM go through `redact_source_credentials`
    (Xtream username/password); `_find_feed` resolves the redacted form.
    Jackett error text is query-scrubbed (`_scrub_query_secrets`) so the
    apikey never rides exception strings.
  * Room (OnlyHumans hub since the IRC-room rewrite): client history is
    trimmed to HISTORY_KEEP (800) so a bloated log never rides every rejoin;
    hub reads are STREAMED with a 2 MiB body cap (`_read_capped_json` in
    ircmgr/oh_room.py) and unread responses are `_discard`-closed; leaving
    mid-connect must not resurrect the join (role re-check under the lock —
    `_join_pass`/`_try_found`/`_seek_seat`/`_handle_key_delivery`, and
    `_stop_room` clears `_pending`+role).
  * EPG table PK is (url, channel_id, start) with an in-place migration —
    concurrent guides must not overwrite each other's shared channels.
  * `iptv_list_sources`/`_list_rss_feeds`/`_format_tool_args`/markdown link
    URLs are HTML-escaped or redacted before any rich-text sink (a crafted
    page could inject markup, incl. a one-click no-confirmation magnet).
  * RSS auto-download marks only SUCCESSFULLY downloaded items seen, one
    worker at a time (overlap guard).
  * v2-only torrents key as "v2:<hash>" in the engine (all-zero v1 collapsed
    them); the engine worker crash path drains queued futures instead of
    hanging every caller.
  * `_download_m3u`/segment workers: fan-out rebalance marks the truncated
    head ERROR (auto-retried); ranged file creation never truncates
    (O_CREAT, not "w+b"); .dtresume names include the extension; HLS init
    sections are fetched with decrypt=False (RFC 8216); ffmpeg remux has a
    wall-clock kill; settings import writes config.json atomically
    (temp+fsync+os.replace via `_write_bytes_atomic`), reloads the imported
    config into the live object (`_reload_config_in_place` — background
    writers then persist the NEW settings) and sets _skip_config_save on
    BOTH dialog branches; target=_blank inherits the private flag;
    downloads re-sniff is once-per-job; the cookie cache is LRU.
  * Accepted residuals (documented, not fixed): DNS-rebinding TOCTOU on
    validate-then-fetch (needs pinned-IP connections — too invasive);
    Room fan-out can block ≤75s per wedged peer (needs per-member queues);
    LLM streaming has no total-duration bound (per-read timeout only).
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
- Font size setting (2026-11-03): Settings → Font Size (Micro/Small/Medium/
  Large, config `ui_font_size`, top-level like `ui_language` so the agent can
  NEVER set it) scales the whole UI. DEFAULT = "small" since 2026-11-23,
  owner (same-day chain: medium → small 2026-11-04 → micro 2026-11-17 →
  medium → "let's try small and blue instead" → small). Because config
  saves persist the then-default into every config.json, `from_file`
  remaps each old default to the next via the `ui_defaults_gen` stamp
  (gen 1→2 medium→small, 2→3 small→micro, 3→4 micro→medium, 4→5
  medium→small — each ONCE; explicit picks survive, post-stamp choices
  persist normally — tests/test_font_scale.py::
  test_config_ui_defaults_migrate_once + test_config_defaults_gen4/5_migrate_once).
  Engine = `gui/fonts.py`: `set_font_scale()`
  must run BEFORE `load_app_fonts()` at boot (`run_gui` loads the config
  first for exactly this reason), and EVERY `setStyleSheet`/`setHtml`/
  `setDefaultStyleSheet` string that contains `font-size:` OR themeable
  white text (`color: #ffffff`) must be wrapped in `scale_qss(...)` — QSS px
  sizes do NOT follow the app font, and colors bake at setStyleSheet time
  (tests/test_font_scale.py pins the wrap coverage for both). Painted QFont
  sizes fitted to geometry (channel-number tiles, HUD strip) deliberately do
  NOT scale. Restart-to-apply, same convention as Language.
- Text color themes (2026-11-03, same day as the font-size setting):
  Settings → Text Color (Blue DEFAULT since 2026-11-23, owner / White /
  Light Blue / Dark Blue, config `ui_text_theme`, top-level like
  `ui_font_size` — agent can NEVER set it) recolors every stylesheet text.
  Default lineage: white → light_blue 2026-11-04 → dark_blue 2026-11-17 →
  light_blue → blue (2026-11-23, same day), each hop riding the
  `ui_defaults_gen` remap chain (a saved light_blue remaps once at gen 4→5).
  `set_text_theme()` runs at boot
  next to `set_font_scale()` (mirrored in jackett_setup_dialog), and
  `scale_qss` rewrites bare `color: #ffffff` → the theme hex, QSS-BLOCK
  AWARE: `selection-color`, `background-color`, `border-color`,
  `gridline-color` are never rewritten, and a declaration block whose
  background is one of the BRIGHT fills (#1a2a4a hover/selected, #2a7abf
  accent, rgba(42,122,191,…), or a gradient carrying them) keeps its white
  text — blue text on the accent blue would vanish; a `keep-white` comment
  on a block's selector side or body pins white for text that INHERITS a
  bright parent fill (the active rail button's label). A NAMED `color:
  white` literal means "pinned white on an accent fill" (record/play chips)
  and is never themed. Python-PAINTED text (HUD captions/values, downloads
  progress-delegate %, IPTV tile titles + dark-chip labels) follows the
  theme via `fonts.text_color()`; dim painted secondaries (unselected tile
  titles, EPG lines) and semantic status colors keep their own colors.
  Theme hexes live ONLY in `fonts.TEXT_THEMES` — never hardcode one into a
  stylesheet string (test_all_text_is_pure_white pins the #ffffff-source
  rule). Restart-to-apply.
- Background ladder DARKENED (owner decision 2026-11-03, applies always,
  not only under blue themes — dark blue text must stay readable):
  `#0a0a0f`→`#05070c` (window base; `rgba(10,10,15,α)` overlays →
  `rgba(5,7,12,α)`, painted `QColor(10,10,15,α)` chips likewise),
  `#0d1117`→`#080b12` (panels/menus/tables/inputs + WebEngine page bg +
  the room portal), `#111827`→`#0b1120` (cards/buttons/hover/HUD bar
  back), `#0f1520`→`#0a0e16` (zebra rows), painted
  `QColor(24,26,34)`→`QColor(12,14,22)` (artwork fills). KEPT unchanged:
  #1a2a4a (hover/selection/borders), #2a7abf accent, #0a1a2e/#0a1a2a,
  all #000000, semantic fills. tests/test_font_scale.py::
  test_background_ladder_is_dark pins it. dlmgr/browser_extension.py is a
  separate surface (Chrome popup) and keeps its own colors.
- Outlines are 1px (owner decision 2026-11-03): QSS border widths are 1px
  everywhere — do NOT re-introduce 2px/3px borders (`border-radius` values
  are corner ROUNDING and stay as-is). Pinned by
  tests/test_font_scale.py::test_no_thick_qss_borders.
- Text SOURCE is pure white (owner decision 2026-11-03; themeable since
  same day): every QSS `color:`/`selection-color:` LITERAL is `#ffffff` —
  theme colors are applied at runtime by scale_qss, never written as
  literals. Exempt (see SEMANTIC_TEXT_COLORS + the theme bullet above):
  semantic status colors (error reds, warning oranges, queued yellow,
  quiet-ok green) and the two accent DATA texts (`#a8edff` downloads stat
  values, `#2a7abf` active browser-tab label). Python-side status palettes
  (downloads _STATUS_COLORS, record-state colours) are data, not QSS, and
  keep their own colors. The HUD's painted captions/values follow the
  theme (warn/danger values keep color).

- Splitter `setSizes()` before window show is overridden by size hints —
  enforce default ratios in `MainWindow.showEvent`.
- Player fullscreen is TWO layers (5.4): regular fullscreen (F/⛶/double-click
  = window fullscreen + chrome collapse + auto-hiding control bar — the
  activity rail stays VISIBLE, that's the look the owner likes) and PURE
  fullscreen layered on top (Shift+F or the 🖥 Pure button = control bar gone
  for good AND MainWindow.set_video_pure_chrome hides the rail + agent side
  panel: no cursor polling, no reveal on key press, blank cursor, one 3s
  entry hint). Esc steps back ONE layer (pure → regular → windowed).
  Invariants, all pinned by tests/test_iptv.py::test_pure_fullscreen_*:
  `_set_controls_autohide(False)` must clear `_pure_fullscreen` AND emit
  `sig_pure(False)` (it is the MainWindow.changeEvent safety-net path — a
  stale pure flag would make the next fullscreen permanently dark AND keep
  the rail hidden); `_set_pure_fullscreen(True)` sets the flag BEFORE
  calling `_set_fullscreen` (the guard keeps autohide from starting the
  poll timers); compact mode and pure don't compose.
  5.4.2 lessons (both owner-reported in pure fullscreen):
  * `_on_player_fullscreen` hides `_toolbar_w, _section_chips_w, _sidebar,
    _content, _status_w` — the section quick-jump chips (Live TV/Movies/…)
    landed in 5.1 AFTER that list and stayed visible over fullscreen video.
    Any NEW always-visible row in the Play tab must join this tuple.
  * Player keys (Esc/F/Space) live in `PlayerWidget.keyPressEvent`, which
    only runs when the player HAS keyboard focus. The widget is
    StrongFocus and re-seats focus (`self.setFocus()`) on every path that
    hides the bar (`_set_fullscreen` entry, `_set_pure_fullscreen` entry as
    the LAST op, `_hide_controls`) — without that, focus strands on a
    hidden control-bar widget and Esc can never leave pure fullscreen
    (offscreen tests can't catch it: the offscreen QPA grants no focus;
    verified natively with QTest + a focus-widget assertion).
  * The SVP out-of-process mpv creates its OWN child HWND under the
    surface; a video click hands Windows focus to that foreign-process
    window and Qt cannot SetFocus back cross-process.
    `mpv_process._make_child_no_activate` patches it WS_EX_NOACTIVATE
    (pointer-sized argtypes mandatory, best-effort, silently skipped on
    failure/POSIX) so clicks never steal focus in the first place.
- `main_tabs.tabBar()` is hidden for good (navigation lives in the menus) —
  never `setVisible(True)` it. Chrome-restoring code (e.g. IPTV's
  `_on_player_fullscreen`, run on every non-fullscreen window-state change
  via `MainWindow.changeEvent`) must skip it, or an empty ~21px strip shows
  up under the menu bar after a fullscreen/maximize cycle.
- Responsive window sizing (`gui/responsive.py`, added 2026-09-12): a
  top-level window's minimum is the WIDEST TAB PAGE's layout minimum, and
  one fat row used to lock the whole window (measured: Play toolbar 1138px,
  player transport 961px, Command 1236px — 612px of that ONE FilePane
  status label — IRC 1232px, Downloads 1034px; the window couldn't shrink
  below ~1250px, and dynamic status text made the floor MOVE, the
  "sometimes I can't make it smaller" bug). Three helpers, all measured:
  * `ResponsiveRow` — a QWidget whose `minimumSizeHint` is capped at its
    floor, so a fat row no longer sets the window minimum. Without this
    cap nothing responsive can ever trigger (the window can't shrink to
    fire the resize events — chicken-and-egg; a hide-on-resize scheme
    alone is useless).
  * `OverflowRow(row, candidates)` — hides the row's optional buttons into
    a "⋯" menu in hide-first order as the row narrows, restores them as it
    widens; hidden buttons still work (the menu calls the button's own
    `click()`; checkable buttons get checkable menu actions). Candidates
    must be widgets the app itself NEVER hides. When the row is a
    floor-less ResponsiveRow, OverflowRow measures the floor itself (all
    candidates hidden + the ⋯ button). Wired: Play toolbar, player tools
    row (mv/compact only — record/sleep buttons are hidden by compact PiP
    mode, which would fight the row), Commander F-key bar,
    Downloads toolbar, torrent quick-action row (main_window).
  * `shrink_label` — `setWordWrap(True)` on every QLabel whose text can
    change at runtime: a QLabel's minimumSizeHint is its FULL text width,
    so live status text grows the window's minimum (a `setMinimumWidth(0)`
    does NOT defeat the hint — verified). Applied: FilePane `_pane_status`,
  Commander `_status`/`_outcome_status`, Room `_status_label` (topic already
  wrapped), Downloads `summary_label`, Play `_status_lbl`/`_art_lbl`,
  player `record_status_lbl`. Any NEW dynamic label must get this too.
  The player transport bar compacts instead of overflowing
  (`PlayerWidget._refit_controls`/`_set_narrow_controls`: below its
  wide-mode layout minimum the emoji buttons get max width 46 + tighter
  QSS padding and "⛶ Full" hides — fullscreen stays on ⛶/F/double-click).
  Regression cap: `tests/test_responsive.py::test_tabs_stay_shrinkable`
  builds each tab and asserts minimumSizeHint ≤ 700×460, with long status
  text; every tab landed at ≤ ~620px.
- No built-in torrent sources ship: `DEFAULT_SOURCES` in `config.py` is
  empty — users add their own or fetch from Jackett. The installer's seed
  (the sample Macau playlist) only lands on FRESH installs; since 2026-09-19
  (owner decision) an EXISTING `~/.deeptorrent/config.json` is PRESERVED on
  every install/upgrade — the auto-updater relies on it, and key rotation
  stays safe because `from_file` re-injects the CURRENT shared key on every
  load (a saved copy of a shared key is scrubbed at load). `DEFAULT_SOURCE_POPULARITY`
  still ranks user-added sources by `id`. If a built-in source is ever added
  back to `DEFAULT_SOURCES`, `from_file` merges it into existing user
  configs (match by `id`, Jackett-style).
- YouTube downloads run yt-dlp (`add_youtube_job` in dlmgr/engine.py).
  YouTube breaks older yt-dlp releases with mid-download HTTP 403s from the
  googlevideo CDN (verified 2026-09: 2026.07.04 died at 8% of a real video,
  2026.8.19 downloaded it clean) — requirements.txt AND pyproject.toml keep
  a recent floor (2026.11.23 since 9.8), so bump BOTH on release rebuilds.
  YouTube job errors carrying 403/Forbidden get the same hint appended to
  `job.error_message`.
- IN-APP yt-dlp UPDATE (9.8, 2026-11-25, `infra/ytdlp_overlay.py`): the
  frozen Windows/Linux builds replace the bundled yt-dlp BETWEEN DeepFlux
  releases, so a YouTube break no longer waits for a ~500 MB app update.
  Durable rules — do not regress:
  * MECHANISM: all 1,044 yt_dlp modules are compiled into DeepFlux.exe's
    embedded PYZ archive (nothing on disk to swap; `_internal/yt_dlp/*.py`
    are inert data), BUT PyInstaller ≥6.10's frozen importer is a
    `sys.path_hooks` finder, not a meta_path one — so a newer `yt_dlp` tree
    under `~/.deeptorrent/ytdlp/versions/<ver>-<sha8>/` wins when it is
    resolved FIRST. The app installs ONE scoped `sys.meta_path` finder that
    answers only `yt_dlp`/`yt_dlp.*` (adds nothing to sys.path, ASCII
    identifiers only — no other name can be shadowed, no traversal is
    expressible). Proven 2026-11-25 in a throwaway frozen build with the
    same PyInstaller 6.22.3 whose loader code disassembles IDENTICALLY to
    the shipped 9.7 exe: every module came from the override, a real
    YouTube download was byte-identical to the bundled copy's, and import
    costs 0.20 s pre-compiled vs 0.38 s raw (the frozen app sets
    `sys.dont_write_bytecode`, so install() pre-compiles with `dfile=`
    pointing at the FINAL path).
  * `import_yt_dlp()` is the ONLY sanctioned import path (engine.py ×2 +
    ytdlp_update.installed_version) — pinned by an AST scan test. It
    decides ONCE per process and uses an override only when STRICTLY newer
    than the bundled copy, which is read from `_internal/yt_dlp/version.py`
    WITHOUT importing (so an app update shipping an equal/newer yt-dlp
    retires the override by itself). The daily probe calls it on its own
    worker thread — keep yt-dlp's first import off the GUI thread
    (test_gui_render's `_build_main` documents the native-AV history).
  * SAFETY NET (pinned): a broken override can never take yt-dlp down —
    the first import failure removes the finder, purges every `yt_dlp*`
    module it loaded, records `skipped.json` (stage "load"), unlinks the
    pointer and imports the bundle. A damaged pointer/manifest/version file
    is ignored outright. If the release dir vanishes under a live finder it
    steps aside (returns None) instead of failing every later import.
    BEFORE any of its code runs, the whole release tree is re-hashed against
    the `tree_sha256` recorded at install (~150 ms for 1,046 files / 10 MB,
    measured; `__pycache__` excluded): yt-dlp imports most extractors
    LAZILY, so a release damaged after activation would otherwise surface
    much later as a failed download with no fallback (review 2026-11-26;
    verified in a frozen build by tampering one deep module — the next
    start refused the release, recorded "integrity check failed" and
    collected it). A mismatch fails CLOSED.
  * Nothing is switched on before a CHILD-process start-up check
    (`DeepFlux --ytdlp-selfcheck <dir>`, handled in main.py BEFORE
    `_configure_logging`; result via `<dir>.selfcheck.json` since the
    windowed build has no stdout; 90 s timeout). The child loads the
    candidate through the same finder, asserts no module resolved outside
    it, builds `YoutubeDL` with the engine's option names, counts
    extractors, and imports the youtube/generic/curl-cffi/http/ffmpeg paths
    DeepFlux actually uses.
  * TRUST: owner-pinned feed `website/deepflux/latest-ytdlp.json` (HTTPS,
    deepflux.space — the same trust root as latest.json); size AND sha256
    must match before a byte is unpacked, and the wheel is then read
    defensively anyway (member/size caps, unsafe-path + symlink refusal,
    per-file digests against the wheel's RECORD — whose OWN row carries no
    digest, a real-wheel detail that broke the first implementation).
    PyPI's content URL is `/packages/<2hex>/<2hex>/<60-hex>/<file>` — that
    hash segment is 60 chars, NOT 64 (measured 2026-11-25).
  * FAILURE CLASSES: `TransientUpdateError` (offline/incomplete — nothing
    recorded) · `_LocalTransientError` (check couldn't run, rename blocked,
    size/sha mismatch — a TLS-intercepting proxy produces those on every
    attempt, so they are COUNTED in `transient.json` and only give up after
    3) · `OverlayError` (release defect — `skipped.json` at once, auto mode
    never retries that exact version+sha). `install(force=True)` is the
    user's own click and may always retry a recorded release; success
    clears both markers. `_mark_skipped` unlinks the pointer ONLY when it
    points at the release being recorded — a failed install of Y must never
    switch off a healthy X (security review 2026-11-26, M1).
  * DELETION TIMING: `install()` and `revert()` NEVER delete release dirs —
    a running DeepFlux keeps importing extractors lazily from the release
    it started with. Retired dirs are collected only by the next start's
    one-time decision, only for names matching `<ver>-<sha8>`, and the sweep
    is SKIPPED while another DeepFlux window is open
    (`infra.updater.other_instances_running`, probed only when there is
    something to collect). Accepted residual: two windows can each download
    the same release once (idempotent — `_publish` reuses an identical
    dir); there is no cross-process lock.
  * FEED ROLLBACK does NOT retract an installed release: the override is
    retired only by a bundled copy that is equal/newer, by its own integrity
    or load failure, or by the user's Revert. To pull a bad release, pin a
    NEWER one (yt-dlp ships often); `packaging/pin_ytdlp.py` refuses a pin
    whose start-up check fails, which is the main guard against publishing
    one.
  * UX: ASK by default. The Agent-tab event carries an exact-match
    `deepflux-action:ytdlp-update` link (wrapped in `<u>` — chat anchors
    are styled as plain text) handled FIRST in `_on_chat_link_clicked`; a
    confirmation dialog precedes every download. Opt-in automatic mode =
    `download.ytdlp_auto_update`, parsed STRICTLY (only JSON `true`) and
    DENY-listed for the agent settings tool — a prompt injection must never
    switch on code installation (same doctrine as the app updater, which is
    not an agent tool either). Settings → Download Manager: auto checkbox,
    plain-text status line, Check Now / Update now / Use bundled version
    (revert records stage "user" so auto mode won't re-download it).
    Restart-to-apply uses `_restart_app(skip_config_save=False)`, which
    closes FIRST and launches a new process only if the close was accepted
    (a declined active-transfers prompt must not leave two windows).
  * Scope: NOT an agent tool; macOS and source checkouts are out
    (`supported()` = frozen win32|linux; dev runs use the venv's copy and
    pip). Tests: tests/test_ytdlp_overlay.py (122) + 10 GUI tests in
    test_gui_render.py; 21 mutations were teeth-checked (each protection
    removed → its own test fails → restored byte-identical). NOTE for the
    gate: tests/test_gui_render.py's process HANGS at teardown on this
    machine — pre-existing, not these tests (the 31 without them pass in
    ~14 s and then need reaping). Read pytest's summary line; don't wait
    for the process to exit.
  * RELEASE RITUAL: `python packaging/pin_ytdlp.py [version]` resolves the
    release on PyPI, downloads + digest-checks it, refuses unknown
    dependencies (KNOWN_DEPENDENCIES), runs the SAME start-up check, then
    writes the feed. Publishing is the normal website deploy — the feed
    rides it. Until the feed is live (or when it can't be read) the app
    falls back to the old behaviour: PyPI probe + "update DeepFlux".
  * KNOWN GAP (deliberate; owner decision pending): the bundle ships
    yt_dlp ALONE — no `yt-dlp-ejs` (yt-dlp's README: "Required for full
    YouTube support"), no JS runtime (deno), no mutagen/brotli/websockets.
    Measured 2026-11-25 on this machine: extraction and real downloads
    still worked, with "YouTube extraction without a JS runtime has been
    deprecated, and some formats may be missing" and one "n challenge
    solving failed". Closing it needs an "engine pack" (yt-dlp-ejs 0.05 MB
    + deno ~42 MB native; yt-dlp looks for the runtime next to the frozen
    exe on Windows) — a separate change, and the pin script's dependency
    guard is what stops it being needed by accident.
- Jackett service + source sync live in `infra/jackett.py`: at startup (GUI
  background thread + CLI) and then hourly, `maybe_auto_sync` pings Jackett
  (`t=caps`), starts it when down (`indexer.auto_start`, default on —
  `sc.exe start Jackett` service first (needs elevation; usually denied),
  then JackettConsole.exe (serves in-process) / JackettTray.exe from
  `indexer.jackett_path` or standard install dirs incl. `%ProgramData%\Jackett`),
  and re-fetches the
  sources list when stale (>24h, `sources.last_jackett_fetch`) or EMPTY.
  `maybe_auto_sync(force=True)` skips the daily gate — wired to Settings →
  Jackett Indexer Settings…: OK forces an immediate sync; a successful Test
  Connection (button lives in API menu, Search Keys page,
  under the Jackett key) fetches
  with the TYPED (unsaved) key + saved URL/path via a probe config
  (`APIKeysDialog._jkt_fetch_worker`) and merges into the live config;
  a forced run that reaches Jackett but fetches nothing reports
  `sync_failed` so the GUI can warn.
  Merge policy everywhere (auto-sync AND the manual "Fetch from Jackett"
  button — both share `merge_sources`; the sync path additionally runs the
  zombie cleanup below): newly fetched
  indexers start ENABLED (configured-in-Jackett = meant to be searched);
  sources the user explicitly disabled keep their state across re-fetches;
  per-source popularity overrides and `origin` survive too.
  Results
  surface in the Agent tab as a dim `jackett_sync` event (unreachable
  notice shown once per session).
- Indexer health + cleanup (2026-10-13 study follow-up — measured live on 96
  indexers: 22 failed every search, 19 of them Cloudflare-walled because
  Jackett had NO FlareSolverr, incl. 1337x which the quick pass queries
  FIRST; 4 snapshot sources no longer existed in Jackett):
  * `infra/indexer_health.py` — persistent per-indexer failure memory in
    `~/.deeptorrent/indexer_health.json` (env `DF_INDEXER_HEALTH` override;
    tests/conftest.py autouse fixture redirects it — never let a test touch
    the real file). 2 consecutive failures → 30-min cooldown, doubling to an
    8h cap; cooling indexers are SKIPPED by `_jackett_search_tiered` and
    re-probed automatically when the cooldown expires (CF walls are
    intermittent — never permanently disable on cooldown). Jackett-level
    failures ("Max retries exceeded"/refused — the service itself is down)
    are classified KIND_JACKETT and NEVER recorded against an indexer, or a
    Jackett outage mass-cooldowns the whole list. KIND_UNKNOWN ("Unknown
    indexer" — Jackett's deterministic answer) bypasses cooldown so the
    count climbs every search; at 3 strikes `is_dead_zombie` flips and the
    Jackett tier stops querying the source. SECURITY-REVIEW RULE (HIGH
    finding 2026-10-13): a search NEVER mutates or persists the config —
    struck-out sources stay ENABLED (their web-search tier value must
    survive; legacy manual website sources draw the same "Unknown indexer"
    and used to get auto-disabled, killing them). Recovery: sync_sources
    calls record_success for every CONFIGURED fetched id, so re-configuring
    an indexer in Jackett revives it at the next sync ("is not configured"
    also classifies unknown — without the reset that skip would be a dead
    end).
  * `classify_indexer_error` maps real Jackett error strings to stable kinds
    (flare/down/moved/unknown/rate/jackett/timeout/auth/parse); the search
    result carries a compact `failure_summary` (kind → count + example
    indexers + a hint like "configure FlareSolverr"; plus cooling and
    zombie_skipped counts) so the LLM can ADVISE instead of retrying
    blindly. Raw error entries are first-line-truncated (Jackett
    descriptions carry full .NET stack traces) and body-parsed error text
    is `_scrub_query_secrets`-scrubbed like every other error path.
  * `TorznabClient._interpret_response` (bug found live 2026-10-13): Jackett
    reports per-indexer failures as HTTP 400/500 WITH a torznab <error> XML
    body (code 900 wrapping the real IndexerException) or a JSON
    {"error": ...} envelope ("Unknown indexer: <id>"). The old
    raise_for_status()-first collapsed every one of those to "400 Client
    Error", hiding the reason — parse the body FIRST, HTTP status last.
  * Provenance: `SourceConfig.origin` — "jackett" (managed, auto-removed by
    sync when the id vanishes from Jackett's FULL definition list returned
    by t=indexers, ~650 ids incl. unconfigured), "manual" (agent/user adds —
    never dropped; NOT queried in the Jackett tier UNLESS the id is a
    genuine Jackett id present in the definitions sidecar — a manual re-add
    of "1337x" should still be searched through Jackett), "" (legacy —
    upgraded to "jackett" on first sync when Jackett knows the id, otherwise
    protected like manual). The sync also persists the definition-id list as
    a sidecar (`~/.deeptorrent/jackett_definitions.json`, env
    DF_JACKETT_DEFINITIONS; conftest redirects it) — legacy ""-origin
    sources ABSENT from it are probed FIRST on every search (they sit at
    the popularity tail, so early-exiting sweeps would otherwise never
    reach them; the probe excludes dead/cooling/struck-out ids so query
    variants and the auto deep sweep never re-probe a suspect that already
    failed this search). Verified live: the machine's 3 true zombies struck
    out after exactly 3 searches (zamundarip is NOT one — Jackett answers
    it 200 with an empty RSS, harmless zero-hit).
  * GUI: the new `flaresolverr` event message is HTML-escaped at the
    `_append_event` sink like its sibling branches (tool_progress etc.) —
    it embeds the agent-settable flare URL and exception text.
  * `SourceConfig.torznab_caps` — per-indexer category ids, free from the
    same t=indexers `<caps>` element. When a category is requested the tier
    skips indexers whose caps clearly don't cover it (anime-only site for a
    movie query); EMPTY caps never filter. Parent matching both directions
    (`_caps_support`).
  * FlareSolverr wiring: `IndexerConfig.flare_solverr_url` + a field in the
    Jackett Settings dialog; saving a NON-empty URL pushes it into Jackett
    via `jackett.set_flaresolverr_url` (admin session = jackett_setup's
    /UI/Login cookie dance; verified live: GET+POST
    `/api/v2.0/server/config`, lowercase key `flaresolverrurl`, full-object
    POST back is exactly what the WebUI does). Empty field NEVER wipes a URL
    set directly in Jackett. Result surfaces as a dim `flaresolverr` event.
  * `IndexerConfig.timeout` default dropped 60→25s (measured p95 7.8s, max
    12.2s; only dead-site 522s ran ~20s). Saved configs keep their value.

## Local AI — uncensored models via bundled llama.cpp (since 6.0)
- GPU DETECTION (6.1 hardening — owner-reported "GPU not found" on an RTX
  5080 Mobile): `local_model._vulkan_devices()` is now a CHAIN — the Vulkan
  `--list-devices` probe (utf-8/replace decode, stderr parsed too) falls
  back to `_fallback_devices()` = DXGI adapter enumeration, then
  nvidia-smi, all ranked discrete-first by `_is_integrated_gpu`. A working
  GPU can no longer be reported missing just because the llama runtime
  isn't extracted or the probe hiccuped. The DXGI ctypes code lives in
  `infra/hardware_monitor.py::dxgi_adapters` — VERIFIED GUIDs/vtable
  indexes against Windows Kits dxgi.h (IID_IDXGIFactory1
  {770aae78-f26f-4dba-a829-253c83d1b387}; EnumAdapters1 = vtable slot 12
  — CreateSoftwareAdapter sits at 11 —; IDXGIAdapter1::GetDesc1 = slot
  10; the DESC1 field order is VIDEO/system/shared — swapping them reads
  0 VRAM; and COM vtable access needs a DOUBLE dereference: object →
  lpVtbl → slots). DedicatedVideoMemory is a SIZE_T (no WMI AdapterRAM
  4 GB wrap) and matched Vulkan's 16003 MiB exactly on the 5080 Mobile.
- SYSTEM HEALTH TAB (2026-11-19, owner request — the HUD's sanctioned
  successor: "only does readings when that tab is selected, not always"):
  `gui/system_health_tab.py` + `infra/system_health.py` + the read-only
  `system_health` agent tool. Durable rules — do not regress:
  * SAMPLING CONTRACT: the tab's QTimer runs ONLY while the tab is the
    CURRENT tab (`MainWindow._on_main_tab_changed` → `set_active` both
    directions), on hide and on shutdown; each tick spawns ONE
    short-lived collector thread (skipped while the previous one runs)
    that hands the Qt-free snapshot back via the queued `sampled`
    signal — the mini-player marshal pattern. NO always-on thread of any
    kind (the 7.6 HUD bisect is the reason this shape exists). The
    agent tool samples per call (`cpu_percent_quick` seeds a one-shot
    baseline + 0.2 s settle so a cold process gets a real CPU%).
  * Data sources (all dependency-free, psutil stays out of
    requirements): CPU/RAM via hardware_monitor (GetSystemTimes /
    GlobalMemoryStatusEx + the new `pagefile_stats` commit-charge
    helper); GPU live via the EXTENDED smi query
    (`nvidia_smi_query` now also returns temp_c / fan_percent /
    power_w / pcie_gen / pcie_width — legacy keys unchanged,
    "[Not Supported]" → None) with its 60 s failure backoff; static
    fallback dxgi_adapters. Disk SPEEDS = IOCTL_DISK_PERFORMANCE
    (0x70020 — FIXED 2026-11-19: 8.5 shipped 0x70000, which is
    GET_DRIVE_GEOMETRY; the driver answered with a 24-byte geometry
    struct misread as counters → constant deltas → disk speeds 0 B/s
    everywhere; value = CTL_CODE(7, 0x8, METHOD_BUFFERED, FILE_ANY_ACCESS),
    derivation pinned by test_ioctl_disk_performance_code_is_derived_not_
    guessed) deltas on `\\.\C:` volume handles opened with access 0
    (works unelevated; a refusing volume is marked cold in
    `_disk_dead` and never re-asked). CPU/board temp = ACPI
    MSAcpi_ThermalZoneTemperature via one-shot PowerShell (30 s TTL,
    5-min failure backoff — many boards expose NONE, None is honest);
    /sys/class/thermal on Linux. INVENTORY = ONE PowerShell CIM query
    (CPU, RAM sticks incl. Speed/ConfiguredClockSpeed MT/s, board,
    BIOS, GPUs+drivers, Get-PhysicalDisk bus/media, enabled NICs, OS)
    JSON-cached 10 min, failure backoff 60 s; /proc digest on POSIX.
  * Warnings (`warnings_from`): GPU ≥ temp_warn_gpu_c (85) / CPU ≥
    temp_warn_cpu_c (90) / RAM ≥ 95% / per-volume free below
    disk_min_free_gb (10) OR disk_min_free_percent (10); severity
    danger escalates past threshold+10 or half the floor. None-valued
    readings NEVER warn. Config section `system_health`
    (refresh_seconds clamped 1–30) is deliberately ABSENT from
    `_SETTINGS_SECTIONS` — thresholds are user-only so prompt injection
    can never silence a warning.
  * Tab is the LAST main tab on purpose (owner: "the tab icon goes …
    on the far right") — icon kind "health" is a painted pulse line in
    `_tab_icon_pixmap`; `_TAB_ICONS` ends with "System Health".
    Meters are painted (theme text via fonts.text_color, warn
    #e67e22 / danger #ff3366 / ok #2a7abf at 75/90 breakpoints); the
    whole tab sits in a QScrollArea (responsive cap); Ctrl+7 comes
    free. `shutdown()` is wired in closeEvent next to iptv/room.
  * Tests: tests/test_system_health.py (28 — every external sampler
    faked via the autouse `_isolated` fixture; real-function restore
    via `_REAL_QUICK`/`_REAL_CPU_PCT` for the cpu_percent_quick unit
    tests), test_tools.py system_health tool test, test_v5_ui icon
    set, test_responsive parametrize. LIVE-VERIFIED 2026-11-19
    (offscreen real-MainWindow smoke: samples arrive, inventory 15
    rows, timer stops on tab switch).
  * STUDY + P0/P1 EXPANSION (2026-11-19, same day — read
    SYSTEM_HEALTH_STUDY.md for the full fail-safe doctrine + sources):
    every new reading follows "SDK-verify the layout, live-probe it,
    THEN write the parser; degrade to absence; per-source backoff;
    never a new vendor DLL in the GUI process". Shipped: negative-delta
    clamps on ALL CPU counters (Windows counters go backwards —
    psutil/htop/btop4win all clamp; a backwards step reads 0.0);
    IOCTL_DISK_PERFORMANCE error ladder (INSUFFICIENT_BUFFER → double
    buffer to 1 MiB, INVALID_FUNCTION/NOT_SUPPORTED → "exotic disk"
    cold-mark, short writes distrusted); GPU plausibility clamps
    (_clamp — a 54000 °C reading is None, LHM's rule); per-core CPU
    via NtQuerySystemInformation(8) — buffer must be EXACTLY
    count×stride (oversized non-multiple → C0000004; probed 816 ok /
    776+1024 fail), stride 48 with 40 fallback, hard fail = sticky
    off; network throughput (GetIfTable2 parsed from RAW BYTES at
    pinned offsets — stride 1352; a ctypes Structure missing one tail
    field shifts every later row, that exact probe bug is why; decode
    the full description BEFORE splitting on NUL or the name tail
    corrupts; admission = up AND (HardwareInterface bit OR live
    traffic) minus name-prefix mirrors + Miniports); drive health
    (NVMe via IOCTL_STORAGE_QUERY_PROPERTY protocol query —
    STORAGE_PROTOCOL_SPECIFIC_DATA has ProtocolDataOffset BEFORE
    ProtocolDataLength, the common mis-transcription is wrong; SATA
    via IOCTL_STORAGE_PREDICT_FAILURE SMART attrs 194/190 — NOT yet
    validated on a real SATA drive, fail-closed); battery
    (GetSystemPowerStatus, 0x80 flag = absent = hidden chip). Drive
    names join the CACHED inventory's DeviceId —
    inventory(cached_only=True) NEVER spawns PowerShell. New user-only
    threshold temp_warn_disk_c (60) + disk_temp/disk_health warning
    kinds. The live CPU CLOCK was probed and DROPPED (ACPI
    CurrentMhz stays at base under load — dishonest to show). P2
    (D3DKMT subprocess GPU lane, external LHM/HWiNFO bridge) remains
    study-only. Tests 57 in test_system_health.py; full nine-chunk
    gate green 2026-11-19 (1,481 tests).
  * 8.8 + 8.9 LAPTOP CRASH ARC (2026-11-19 evening, AMD GPD laptop):
    8.8 = dxgi_adapters() TTL-cached ~10 min — on non-NVIDIA machines the
    tab's GPU fallback walked the DXGI COM factory EVERY 2 s tick
    (driving the AMD In-Game Overlay's dxgi hook in-process; laptop died
    with python311.dll+0x49245 AVs ONLY while the health tab was open,
    faulthandler stacks were innocent EPG bystanders, survived an
    Adrenalin update). dxgi_adapters(force=True) bypasses the cache.
    8.9 = STEP-BY-STEP DIAGNOSIS (owner idea): a "Step-by-step" toggle
    on the tab runs each reading SEQUENTIALLY (2 s dwell) with a visible
    per-step state, logging BEGIN before every attempt to
    ~/.deeptorrent/logs/health_steps.log (crash-safe open-flush-close,
    `_STEPS_LOG_PATH` redirects in tests) — if the app dies, the last
    BEGIN without OK names the culprit reading. DURABLE RULES:
    health_steps() in infra/system_health.py is the SINGLE SOURCE OF
    TRUTH (snapshot() = snapshot_base() + every step + snapshot_finalize()
    — parity-pinned by test; a new reading = a new step closure, never a
    parallel code path); step mode passes force=True so the raw dxgi walk
    is re-driven deliberately; the collector thread NEVER emits sampled
    after shutdown; and the timer-gating test DRAINS the collector before
    fixture teardown — a zombie collector surviving into the restored
    REAL native readers mid-flight is the deterministic 0xc0000374
    heap-corruption detonation found 2026-11-19 (latent since 8.5; ANY
    test that kicks the real collector must drain
    `while tab._collector_busy` before returning).
    9.1 = AMD GPU READING OFF (owner directive 2026-11-20): the laptop
    STILL crashed and the step-by-step tool named the GPU step as the
    death point — dxgi_adapters() is now NEVER called from System Health
    on AMD machines. Gate = `_amd_display_present()` in
    infra/system_health.py: a PURE-REGISTRY probe (display class
    {4d36e968-…} subkeys' DriverDesc vs amd/radeon/ati markers — no COM,
    no subprocess; detecting AMD must not itself drive the overlay's dxgi
    hook), process-cached, POSIX → False. `_gpu_block` runs nvidia-smi
    FIRST (hybrid AMD+NVIDIA boxes keep live NVIDIA data), then gates the
    dxgi fallback → `gpu["skipped"] = "amd"`; the tab's GPU/VRAM meters
    show "skipped on AMD (stability)" and step mode's note column adds
    "skipped (amd)" (generic: any step part carrying a `skipped` dict key
    gets the note). Do NOT "fix" this by re-enabling dxgi on AMD without
    a new owner decision; the local-model picker's dxgi chain is a
    separate call site (still TTL-cached, untouched).
    OWNER-CONFIRMED 2026-11-20: the laptop is STABLE with the skip in —
    the dxgi-walk→overlay-hook mechanism is now causally proven, not
    just correlated. Any future AMD live-GPU-stats work must use the
    study's P2a shape (D3DKMTQueryStatistics in a SUBPROCESS — the hook
    dies with the child, never the GUI), never an in-process walk.
    DEEP STUDY 2026-11-20 (owner request): **read AMD_GPU_STUDY.md**
    before touching any GPU lane — four research lanes, all claims
    VERIFIED-vs-REPORTED with URLs + local-SDK cross-checks. Headlines:
    the crash twin is published (tapestry #194 — amdihk64.dll spawns a
    worker thread then UNLOADS; a late thread jumps into unmapped code;
    the hook engine lives INSIDE the AMD D3D user-mode driver, so any
    process with a D3D device hosts it and NO overlay toggle or AMD
    opt-out removes it); the laptop's driver is a GPD OEM branch with no
    fix in any 2024-2026 notes; ADLX ("AMD Device Library eXtra",
    driver-resident amdadlx64.dll, full sensors incl. hotspot/NPU/
    iGPU-shared) is the vendor lane and AMD's OWN docs prescribe the
    user-process+IPC boundary; PDH GPU counters + D3DKMT
    ADAPTERPERFDATA (temp deci-C, IN THE OFFICIAL SDK HEADER — enum 62;
    0 = no sensor) are the vendor-free lanes. Roadmap P1 (PDH+D3DKMT in
    the collector subprocess) / P2 (ADLX long-lived read-only helper
    child) / P3 (event-driven DXGI on the non-AMD path) — all
    owner-pending, NONE implemented.
- Hardware HUD (6.1) — **REMOVED in 7.6 (owner decision 2026-11-07)**:
  `gui/hardware_hud.py` + `tests/test_hardware_hud.py` are gone. The HUD's
  2 s sampler (GetSystemTimes + GlobalMemoryStatusEx ctypes + nvidia-smi
  subprocess + llama health requests) was WORKER-BISECT PROVEN to be the
  boot-crash trigger on the dev machine: with ALL background workers
  quiesced the full GUI (incl. a manual agent chat) ran indefinitely;
  re-enabling ONLY the HUD killed the process with an AV in ~30 s —
  and after the removal the fully normal app (greeting + all workers)
  boots and runs clean where 7 consecutive full launches had died.
  Do NOT re-add an always-on polling strip to this machine's build; if a
  successor is ever wanted, sample on demand (button/dropdown) instead of
  a cadence thread. `infra/hardware_monitor.py` STAYS — the local-AI
  model picker's GPU detection (`dxgi_adapters` chain) uses it; its
  ctypes declarations were re-audited clean (FILETIME/MEMORYSTATUSEX
  layouts correct — the corruption mechanism on this machine was never
  pinned to a specific line; the sampler's external interactions
  (nvidia-smi spawn cadence / injected hooks) remain the prime suspect
  given SVPManager.exe + LightingService.exe crash independently too).
  The 6.1-era doc follows for history: the strip was the always-visible
  top of the Agent page (CPU/RAM/GPU/VRAM meters + the local-AI status
  chip opening API menu), sampling = ONE daemon thread
  (2 s) gated by `set_active` from `_on_main_tab_changed`.
- API Keys → AI page (6.1 UX rework): per-model FIT BADGES
  (`gpu_full`=GPU green / `gpu_partial`=GPU+CPU blue / `ram_only`=CPU
  grey / None=won't fit red) + sizes in the status, ★ marks the
  recommended row, `Use` stages an installed model as the agent's model
  (`_pending_local_entry` contract — config mutates only on OK), active
  model shows ✓. Endpoint-aware form:
  Local hides the key row entirely (keyless by design) and locks the
  base URL read-only; presets keep the base URL read-only (custom is the
  only editable one). The key field got a trailing 👁 reveal QAction.
  6.4.1 RULE (owner, do NOT regress): downloading a model — ladder,
  Hugging Face, installer step — NEVER changes the provider. The built-in
  DeepSeek key stays the default regardless; switching to local is always
  an EXPLICIT user action (endpoint combo or Use). The old
  `_local_auto_switch_ok` post-download auto-offer is GONE — do not
  re-add an auto-switch of any kind.
- The "local" provider (LLM_PROVIDER_PRESETS["local"]) runs an UNCENSORED
  model on the user's machine through the official llama.cpp
  `llama-server` (MIT; pinned by `packaging/fetch_llama.py` — CPU + Vulkan
  Windows x64 zips, ~52 MB; llama.cpp marks EVERY release prerelease, that
  is just their convention). Installer step `--setup-localmodel` (third
  postinstall checkbox, after FlareSolverr) — OPT-IN since 6.4 (owner
  decision 2026-10-21: the shipped DeepSeek key is the out-of-the-box
  default agent; local models are a later choice from Settings → AI &
  API Keys → AI Agent Key…; the CurPageChanged default-check hook no longer touches it):
  extract both runtimes into `~/.deeptorrent/llama/{cpu,vulkan}/` (frozen
  builds DELETE the zips after extraction; dev keeps them — the
  flaresolverr lesson), detect hardware, download the matched GGUF, switch
  the provider. Server: `127.0.0.1:8199` ONLY, `--alias <model-id> -c <ctx>
  --no-webui --parallel 1 -t cores/2` + `-ngl all` only when
  the weights fit 90% VRAM (b11727 defaults `-ngl` to AUTO otherwise —
  fits layers to VRAM itself). VERIFIED live 2026-10-19 on the RTX 5080:
  health in 8s, chat 1.2s, SSE stream + tool call parse clean;
  `--reasoning-format deepseek` maps Qwen3.5's inline thinking onto
  `reasoning_content`, which `DeepSeekClient._consume_stream` already
  dims in the UI — without it thinking leaks into content.
- Per-model context (2026-10-21): `<ctx>` is NOT the old fixed 32768 — it is
  `entry_ctx()` = the GGUF's native context_length (probed from the file's
  metadata header via `probe_gguf` — NEVER re-derive: GGUF v3 metadata keys
  are ARCHITECTURE-prefixed (`qwen35.context_length`, not `llama.`), so the
  probe matches by SUFFIX; tokenizer string arrays are skipped by seek),
  capped by `ctx_cap_for` (90% RAM − weights − 256 MB; when the weights fit
  the GPU the leftover VRAM budget binds instead, so the KV cache stays in
  VRAM and decode stays fast — a RAM-resident KV makes GPU decode
  CPU-bound), floored at 4096, falling back to CONTEXT_SIZE when metadata
  is unreadable. `--parallel 1` is MANDATORY: llama-server allocates
  n_ctx × parallel slots of KV, and the cap math assumes one slot.
  MEASURED live on the RTX 5080: the 9B Q8 serves 42528 ctx (was 32768),
  the 4B Q4_K_M 94560 — both KV-resident in VRAM. `--reasoning-format
  deepseek` is now gated on the probed architecture containing "qwen3"
  (other archs get no flag — it would mis-parse their output). The agent
  loop's `_fit_context` clamps to `context_for_config(config)` (same
  number the server is started with), `_local_guidance` states the real
  window, and the AI-page rows show "… · 42K ctx" per installed model.
  Tests: test_local_model.py probe/kv/cap/entry_ctx/argv sections.
- Local server on/off control (2026-10-21): API Keys → AI has a
  Start server / Stop server toggle with a progress bar. START resolves the
  config model (else first installed), ensure_runtime + start_server, then
  polls `server_status()` (loopback /health) — VERIFIED live: while a model
  loads, b11727's /health answers 503 {"error":{"message":"Loading model"}}
  with NO percentage, so the bar is INDETERMINATE and the label carries the
  phase + elapsed seconds (GPU budget 600 s / CPU 240 s). STOP runs
  `stop_server_manual()` = stop_server + a `manual_off` flag in
  server.json; `ensure_running` returns False while it is set, so the app's
  lazy auto-start CANNOT silently undo the user's Off (off means off —
  don't regress that). start_server's fresh state write clears the flag on
  the next Start. main_window `_local_model_worker` emits a dim notice when
  provider=local but the server is manually off. The dialog polls the
  state every 5 s while open (background thread; signals: hf_*/server_*).
  Tests: test_local_model.py on/off section + test_hardware_hud.py
  test_local_server_on_off_control.
- Local context management (owner-reported intermittent "400 Client Error"
  from the local agent, root-caused 2026-10-20): the server's 32K window is
  SMALLER than the default 64K context budget — llama-server rejects any
  request past 32K tokens with a 400 whose BODY says why ("request (N
  tokens) exceeds the available context size (32768 tokens)"), and the old
  client swallowed it (bare raise_for_status). Do not regress any of this:
  * `DeepSeekClient._post_with_retry` appends the server's error body to
    the HTTPError message (`_server_error_detail` in agent/llm.py) — local
    agent failures must never go opaque again.
  * `_fit_context` clamps the token budget to the SERVED per-model context
    (`context_for_config` in infra/local_model.py — see the per-model
    context bullet above) when provider == "local" (agent/loop.py), so the
    app trims history BEFORE the server rejects it.
  * Oversized local tool results are auto-ARCHIVED to the memory store's
    daily notes instead of silently cut: `MemoryStore.archive(key,
    tool_name, content)` writes numbered chunks each carrying the archive
    key (derived from tool_call_id) + tool name, so `search_memory('<key>')`
    pages the full result back. The context copy = the search_memory
    pointer FIRST (the fitter trims tool content from the tail — a trailing
    pointer gets cut; leading it survives the 1000-char floor) + a digest
    (6000 chars for the freshest results, TOOL_HISTORY_MAX_CHARS for older
    ones). Archive failures (memory disabled, secret-like content) fall
    back to the old truncation; cloud providers are untouched. The
    `_local_guidance()` block tells the model about the 32K window and to
    search_memory instead of re-running searches. Tests:
    test_loop.py local-archive/clamp tests, test_memory.py archive tests,
    test_llm.py error-body test.
- Model ladder = TRACKED manifest `infra/local_models.json` (pinned by
  `packaging/pin_local_models.py`; the HF tree API's LFS `oid` IS the
  sha256 — pinning needs NO multi-GB download). Families:
  HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive Q4_K_M/Q6_K/Q8_0
  (primary GPU) + mradermacher/Huihui-Qwen3.5-4B-abliterated Q4_K_M/Q6_K/
  Q8_0 (CPU/small-VRAM fallback; same Qwen3.5 template). Fit matrix is
  ModelSwarm's, ported verbatim: `ram_est = ceil128(weights + 128 + 256MB)`
  must fit 90% RAM; weights ≤ 90% VRAM = gpu_full (bigger quant wins);
  else gpu_partial; else ram_only where SMALLER wins (deliberate deviation
  — agent loops are multi-turn, CPU decode speed is king; MSP peaked at
  8 GiB). GPU detection = the Vulkan build's own `--list-devices` parse —
  INTEGRATED GPUs rank LAST regardless of size (live gotcha: this
  machine's "Intel(R) Graphics" reports 18275 MiB of SHARED memory and
  outranks the RTX 5080's real 16003 — `_is_integrated_gpu`).
- Downloader (`local_model.download_model`): plain requests against
  `{repo}/resolve/{commit}/{file}`, Range-resumable `.part` sidecar, 4
  attempts with backoffs, final size + full-file sha256 against the
  manifest (LFS files are content-addressed, so resume concatenation is
  byte-exact across edge nodes). Downloads land in
  `~/.deeptorrent/models/<id>/` (shared home with the whisper model).
  MEASURED: 2.52 GiB in 19s (~140 MB/s avg). Cancel keeps the .part.
- Custom Hugging Face models (6.3 feature, GUI-ONLY — never an agent tool):
  API menu → AI Agent Key… has a "Custom model from Hugging Face" section —
  `local_model.hf_search` (GET /api/models, filter=gguf, downloads-sorted,
  LIVE-VERIFIED field names) → double-click a repo →
  `local_model.hf_gguf_files` (GET /api/models/<repo>/tree/main; the LFS
  `oid` IS the sha256, same pinning trick as the manifest) → pick a file →
  the SAME download_model path (resumable, size-verified; sha256 check
  skipped only when the tree carried no LFS oid) → Use stages it like any
  ladder model. Custom entries live in a sidecar
  `~/.deeptorrent/models/custom_models.json` (atomic writes, written at
  download success only) and merge into `entry_by_id` / `installed_models`,
  so ensure_running/start_server/main_window see them with zero special
  casing; `remove_model` drops the sidecar record too. `custom_entry()`
  builds the id (`hf-<repo>-<quant>-<sha1-6>`, path- and alias-safe) and
  validates repo/file (HF_REPO_RE, no `..`) — a crafted repo id can never
  escape models_root(). Quant tags (Q4_K_M/IQ3_XS/F16…) come from
  `_QUANT_RE` (no trailing \b — underscores are word chars; `[_-]`-only
  continuation so ".gguf" is never swallowed). GUI rows for custom models
  are added dynamically (rows container + `_sync_local_rows`); the row must
  exist BEFORE `_local_download` runs (it gates on the row's Download
  button). Tests: tests/test_local_model.py custom/HF section.
- Provider gates (keyless local must NEVER fossilize to "dummy"):
  `settings_dialog._save_and_accept`, `MainWindow.__init__`, and the
  `main.py` Repl gate all exempt `"local"` like `"custom"`; `from_file`
  never injects the shared DeepSeek key into it and never remaps its
  models (it IS in the stale-fast_model clearing tuple). Provider
  changes are USER-ONLY since 6.4.1 (owner): no code path auto-switches
  to local — not the installer step, not a finished download, nothing.
  A deliberate cloud choice is never stomped.
- Server lifecycle mirrors flaresolverr: `ensure_running` (provider local
  + OUR base URL only; restarts when the running server's model_id
  differs from config), health `/health`, startup budgets 600s GPU /
  240s CPU, `server.json` state (pid/port/variant/model_id), `reap_orphan`
  at boot kills a recorded-but-unhealthy pid (a HEALTHY one is adopted —
  the installer step's process exits after starting it, on Windows the
  child survives), `stop_server` via taskkill /T /F from closeEvent /
  Repl.shutdown. Variant choice: vulkan when GPU detected (falls back to
  the cpu build on spawn failure). GUI: `_local_model_worker` startup
  thread + a `local_model` dim event (HTML-escaped like flaresolverr's);
  API menu → AI Agent Key… has the whole ladder with hardware line,
  recommendation, Download/Remove/Test.
- Packaging: installer.iss [Files] wildcards `llama-*-bin-win-cpu-x64.zip`
  etc. into `_internal\llama` (skipifsourcedoesntexist); app.spec globs the
  zips into datas dest "llama" + `local_models.json` into dest "infra"
  (resolved via `__file__`'s dir — works dev AND frozen) +
  hiddenimports infra.local_model{,_setup}. Manifest/model-id literals are
  pinned in THREE places (config preset models[0],
  local_model.DEFAULT_MODEL_ID, the manifest) — test_manifest_agrees pins
  them together. conftest redirects DF_MODELS_DIR/DF_LLAMA_DIR — tests
  must never touch the real models dir (real whisper + GGUFs live there).

## LLM subsystem (agent/)
- `llm.py` — OpenAI-compatible client (`DeepSeekClient` by name, talks to any
  provider in `LLM_PROVIDER_PRESETS` in config.py: deepseek / openrouter /
  custom): retries transient failures (429/5xx/network, 3 attempts, backoff);
  streams SSE when given `on_delta(kind, text)`; captures `reasoning_content`
  (DeepSeek) or `reasoning` (OpenRouter) into `LLMMessage.reasoning`.
  Provider capability metadata controls `reasoning_effort`, effort-name mapping,
  streaming and tools: DeepSeek/OpenRouter send effort (`max` maps to `xhigh`
  on OpenRouter); custom endpoints omit it unless explicitly enabled. Per-call
  `model=` override: summaries use `config.llm.fast_model` + effort "low";
  planning uses `model` + `reasoning_effort` from config. The Provider combo,
  Base URL and custom model live in API menu; keyless custom endpoints
  are supported. DeepSeek/OpenRouter models remain preset-locked. There is NO
  separate AI Settings dialog:
  stream / memory / agent-debug are assumed always-on — `from_file` forces
  `llm.stream=True`, `llm.memory_enabled=True`, `ui_agent_debug=True`
  regardless of what's saved; `reasoning_effort` stays a config.json-only
  knob (default "high"). `DEEPSEEK_API_KEY` env injection and the shared
  DeepSeek key are gated on provider ("", "deepseek", "dummy" — never
  openrouter/custom). "dummy" is the no-key placeholder a keyless boot
  writes, not a choice: since 2026-09-19 from_file injects the shared key
  into a dummy config AND heals the provider back to "deepseek", so a
  fossilized dummy (old keyless build + preserved config) can never keep a
  fresh install keyless (tests/test_shared_keys.py::test_dummy_fossil_*).
- `memory.py` — OpenClaw-style persistent memory in `~/.deeptorrent/memory/`
  (`USER.md`, `MEMORY.md`, `daily/YYYY-MM-DD.md`). Curated files are injected
  into the system prompt (budgeted); daily notes are search-only. Entries carry
  stable ids; LLM tools are `save_memory` / `search_memory` / `list_memories`
  plus confirmed `edit_memory` / `forget_memory`. Secret-like values are rejected.
- `loop.py` — bounded history: `_pruned_history` caps at
  `llm.history_budget` msgs and only cuts at safe boundaries (never orphans
  a tool message from its assistant tool_calls); `_fit_context` also enforces
  an approximate token budget on every LLM call. Tool schemas are selected by
  task domain instead of always sending the full registry. Turns, LLM calls,
  tool calls and repeated identical calls are bounded (defaults 80 / 100 /
  200 since 2026-11-17, owner — were 40 / 50 / 100; from_file remaps saved
  old defaults to the new ones once via the limits_defaults_gen stamp,
  tests/test_agent_limits.py); the GUI Stop
  button sets cooperative cancellation. WALL-CLOCK TASK LIMIT REMOVED
  2026-10-20 (owner: long deep searches hit the 600 s cap) — the check is
  gone from `_task_limit_reason` and `llm.task_timeout_seconds` defaults to
  0; the field survives ONLY as the cap `run_shell`'s wait timeout is
  clamped to (`or 600` when 0), and the Agent Status dialog prints
  "unlimited" for it. Do NOT re-add a wall-clock cap. PROMPT RULES (6.2,
  owner): the system prompt carries a DEEP REQUESTS rule — the word "deep"
  in a user message means `search_indexers` with `deep=true` IMMEDIATELY
  (no quick pass first, no asking) plus web query variants until sources
  are exhausted; and `_local_guidance()` renders a local-model block when
  provider == "local" (nothing billed, quality beats speed, don't rush —
  small local models otherwise stop early). Streaming emits
  `stream_delta` events;
  GUI replaces the raw stream with the rendered bubble on finish (`_seal_stream`
  on tool turns). Confirmation preserves the complete mixed tool batch, previews
  exact redacted arguments, expires after five minutes, and never inherits the
  watchdog auto-heal setting. The enabled watchdog is started by GUI/CLI, records
  first observations without false stalls, has per-torrent cooldowns, and can
  mutate only through its torrent-recovery allowlist.
- YOLO mode (owner request 2026-10-13, shipped 5.8; DEFAULT ON since 7.6,
  owner decision 2026-11-07): `config.llm.yolo_mode`
  disables ALL tool confirmations — `_queue_confirmation` returns None, so
  run_shell/deletions/settings writes execute immediately. SECURITY RULE:
  `("llm", "yolo_mode")` is in `_SETTINGS_DENY_PATHS` — the agent can NEVER
  set it via set_settings (a prompt injection must not unlock its own gates);
  only the user's GUI toggle (🔓 YOLO button by the agent input, warning
  dialog on manual enable, `MainWindow._on_yolo_toggle`) can. DEFAULT
  MIGRATION: the old default (False, shipped since 5.8) was persisted into
  every config.json by any save, so `from_file` remaps saved False → True
  ONCE via the `yolo_defaults_gen` 1→2 stamp (same pattern as
  ui_defaults_gen); a deliberate later Off persists (gen=2 blocks
  re-migration). The system prompt
  gains an UNATTENDED MODE paragraph when on (otherwise models narrate
  waiting for approvals that never come). The watchdog auto-heal allowlist
  is NOT affected — yolo changes chat confirmations only. Tests:
  tests/test_yolo_mode.py (immediate execution, default-on, migration-once,
  deny, mid-conversation toggle, prompt paragraph); tests/test_loop.py
  confirmation-path tests set yolo_mode=False explicitly.
- Browser tab strip AUTO-FITS (owner report 2026-11-07: many tabs ballooned
  the window to a huge minimum): tab chips use `_ElideLabel` (zero-width
  minimumSizeHint, paintEvent draws elidedText; a plain QLabel's minimum =
  its FULL text width is the bug), each chip is capped at
  `_BROWSER_TAB_MAX_WIDTH` (220), and `_tab_strip_container` is a
  `ResponsiveRow(min_width=240)` — no tab count can ever grow the window
  minimum again; tabs shrink/elide to share the strip (full title in the
  tooltip). Pinned by tests/test_browser_chrome.py
  (test_tab_strip_never_grows_the_window_minimum,
  test_elide_label_elides_instead_of_demanding_width).
- BOOKMARKS POPUP right-click delete (2026-11-07, shipped 7.6): the popup
  and every folder submenu are `_BookmarksPopup` (QMenu subclass) — QMenu
  swallows right-clicks on plain actions, so the subclass maps them by
  POSITION: top-level bookmark actions via `bookmark_urls`, folder submenu
  TITLES via `folder_paths` (full path, so nested "News/Tech" deletes
  exactly its subtree), folder bookmark ROWS via CustomContextMenu on the
  row widget. Dispatch climbs the parent chain to MainWindow (a submenu's
  QObject parent is its parent MENU, not the window — pinned by
  test_nested_submenu_right_click_reaches_main_window). Both context menus
  use NON-BLOCKING popup() (a nested exec() inside the menu's own
  mouse-press dispatch wedges its event loop — the offscreen tests hung
  on exactly that) and NEVER close the bookmarks popup: picking Remove
  rebuilds the OPEN menu in place (QMenu handles dynamic action changes
  while shown; owner wants the window to stay, not vanish). The row ✕
  buttons still close-then-remove (deliberate "done" signal).
- Chrome hide grace is `_CHROME_HIDE_GRACE_MS` (2000 since 7.6, owner
  2026-11-07: the 400ms default made the banner vanish while the user was
  still aiming around it); re-entering the banner zone cancels the pending
  hide. Pinned by test_chrome_hide_grace_is_two_seconds.
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
- Built-in keys: since 3.5.2 (owner decision 2026-09-09, extended and
  moved out of git 2026-09-10) a SET of shared keys ships in the SETUP FILE
  ONLY — never in git: Venice (agent, since 7.9), DeepSeek (agent
  fallback), Perplexity (web search), TMDb, OpenSubtitles, TPDB, StashDB,
  OMDb, Fanart.tv. Jackett and Brave stay env-only by decision.
  DEFAULT AGENT SINCE 7.9 (owner decision 2026-11-17): the "venice"
  provider — Venice AI (`https://api.venice.ai/api/v1`, OpenAI-compatible)
  serving `deepseek-v4-1-flash-uncensored`, an ABLITERATED DeepSeek V4.1
  Flash (the SAME base model as the direct API's deepseek-flash; verified
  live: function calling + reasoning effort none/low/high/max — NO
  "medium", preset effort_map sends it to high — + SSE streaming; 1M ctx).
  Out-of-the-box key = `_SHARED_VENICE_API_KEY` (same obfuscated
  `_embedded_keys.py` ritual; env override `VENICE_API_KEY`). from_file's
  7.9 migration flips provider deepseek/dummy → venice ONLY for
  shared-key-riding/keyless-with-a-venice-key configs (a saved OWN key, a
  `DEEPSEEK_API_KEY` env user, or an explicit openrouter/custom/stamped-
  local choice is never touched); `LLMConfig.model` defaults to "" (= the
  provider preset's default — an explicit model name as the DATACLASS
  default would ride along in configs that omit the field and 404 on
  openrouter's slugs). Android mirrors this: ChatClient's base
  URL/model, `EmbeddedKey.veniceKey()` + `EMBEDDED_VENICE_*` BuildConfig
  pair, `gen_android_key.py` embeds the Venice key (properties
  `venice_salt_hex`/`venice_blob_hex`), CI secret
  `EMBED_SHARED_VENICE_API_KEY`. Local models remain an opt-in later
  choice (the installer's --setup-localmodel checkbox is default-OFF
  since 6.4). Since 6.4.2, `from_file` ALSO migrates any
  provider == "local" config WITHOUT the `local_chosen` stamp back to the
  built-in default (venice since 7.9; clearing the loopback base_url and
  model fields, BEFORE the shared-key injection) — that is how
  auto-updated installs whose provider
  was auto-switched by a ≤6.4.0 build get forced onto the built-in key;
  the GUI stamps `local_chosen` when the user explicitly saves local, and
  that stamp is the ONLY thing that keeps a local config local. The agent
  can never set provider=local (settings whitelist excludes it), so no
  other writer exists. `config.py` loads each `_SHARED_*_API_KEY` at import
  time from the UNTRACKED repo-root module `_embedded_keys.py` (gitignored;
  exists only on build machines; PyInstaller bundles it like any import via
  app.spec's conditional hiddenimport); without the file every shared key is
  empty and nothing breaks. Each is injected by `from_file` as the
  LAST-resort fallback for its slot — a saved user key wins, then the slot's
  env var (`VENICE_API_KEY`, `DEEPSEEK_API_KEY`, `PERPLEXITY_API_KEY`,
  `TMDB_API_KEY`, `OPENSUBTITLES_API_KEY`, `TPDB_API_KEY`, `STASHDB_API_KEY`,
  `OMDB_API_KEY`, `FANARTTV_API_KEY`), then the shared key; Venice/DeepSeek
  additionally only when the provider matches. History was rewritten (git-filter-repo) to purge
  the literals from all pushed commits — NEVER paste a key value into any
  tracked file, commit message, or this file. Without an LLM key the GUI/CLI
  run the agent in dummy mode; missing metadata keys degrade gracefully. The
  system prompt fixes the agent's identity as "DeepFlux" (never
  "DeepTorrent" — that name only survives in legacy paths).
  KEY HYGIENE (since 3.5.9 — audit demanded no extractable key in the
  installer/installed app):
  * `_embedded_keys.py` stores each value OBFUSCATED as a
    `(salt_b64, blob_b64)` pair (`blob = value XOR sha256(salt+counter)`
    keystream) — no plaintext constant exists anywhere in the repo or the
    frozen PYZ, so pyinstxtractor + a pyc constants dump comes up empty.
    `config._decode_shared_value` decodes (fails closed to "" on tamper);
    regenerate the file after changing a key with
    `python packaging/gen_embedded_keys.py` (never prints values). The
    scheme is obfuscation, not crypto — that is the ceiling for any
    client-side embedded key.
  * Shared keys are NEVER persisted: `from_file` records the slots it
    filled (`_SHARED_KEY_SLOTS`) and `to_file()`/`sanitized_dict()` blank
    them again (they re-inject on every load). Before 3.5.9 the injected
    values were serialized straight into `~/.deeptorrent/config.json` by
    every save — `from_file` also scrubs saved values that equal a current
    shared key, so upgrades self-heal on first save. Settings Export uses
    `sanitized_dict()` for the same reason (a .dfc backup must not carry
    the shared keys out of the app).
  * Residual, accepted: a determined user can still recover a key at
    runtime from process memory or by MITM-ing their own machine's TLS —
    impossible to prevent for an embedded key; the goal was no trivially
    extractable static artifact. The API Keys dialogs show password-masked
    fields only.
  * Rotation gotcha: the load-time scrub matches only CURRENT shared
    values, so a rotated-out key saved into a config.json by a ≤3.5.8
    build keeps winning (saved keys beat the shared fallback) and the
    agent 401s. Since the installer now PRESERVES config.json (2026-09-19)
    such a fossil can survive upgrades; only ≤3.5.8 installs could ever
    persist one (post-3.5.9 to_file never writes shared keys) and the
    User Guide's "Agent not responding" entry tells those users to clear
    or replace the key. A dev machine may need the stale `llm.api_key`
    cleared once by hand (done on this machine 2026-09-10, backup at
    `config.json.bak-stalekey`). DeepSeek key rotations to date:
    2026-09-10, 2026-09-19, 2026-10-11 (5.5, first touching the Linux CI
    secret), 2026-10-21 (6.4, again both the build-machine file and the
    EMBED_SHARED_DEEPSEEK_API_KEY gh secret; new key verified live against
    api.deepseek.com before building).
- DeepSeek model lineup (checked against the pricing page 2026-09-10):
  `deepseek-flash` (= V4.1-Flash, DeepSeek's own current default — cheaper
  AND better than v4-pro per their page; 1M ctx) is the ONLY preset model
  and the default for BOTH `llm.model` and `llm.fast_model`. `deepseek-v4-pro`
  is outgoing (auto-routes to Flash at Flash pricing from 2026-09-14) —
  SAVED configs carrying it (or the retired `deepseek-v4-flash` /
  `-vision-exp` / V3-era `deepseek-chat` / `-reasoner`) are remapped by
  `from_file` to flash per provider (custom providers are NEVER remapped —
  they may legitimately serve those names). OpenRouter slugs differ:
  `deepseek/deepseek-v4.1-flash` (versioned! a bare
  `deepseek/deepseek-flash` does NOT exist there) — `DeepSeekClient`
  maps bare direct names to OR slugs instead of blind-prefixing.
- Adding a tool: register schema+handler in `tools.py`, classify it in the
  centralized policy sets there (`READ_ONLY_TOOL_NAMES` can run concurrently /
  `CONFIRMATION_TOOL_NAMES` need approval), include it in context routing, and
  add a GUI label in `main_window._TOOL_LABELS`. `loop.py` exports compatibility
  aliases for older tests/callers. Watchdog mutations also need the narrow
  `WATCHDOG_AUTO_HEAL_TOOL_NAMES` allowlist.
- `add_download` submits public direct file/HLS/DASH URLs to the dlmgr
  DownloadEngine and accepts an optional destination. ToolRegistry takes
  `dl_engine=` (GUI injects the real one; CLI lazily creates
  its own). The queue is fully agent-managed: `list_downloads` (READ_ONLY),
  `pause/resume/retry/remove_download` (plain; job ids accept unique prefixes —
  `_resolve_dl_job`), `cancel_download` (DESTRUCTIVE — deletes the partial file
  by default). `web_fetch` extracts `magnets`, `torrent_urls`, and
  `download_links` (file/stream hrefs, resolved with urljoin) from raw HTML.
  `web_fetch` and `add_download` reject local/private targets and validate every
  redirect. Torrent-URL downloads do the same except for the exact configured
  Jackett origin (when its key is configured). External tool results carry an
  untrusted-content marker; tool args/results are redacted before logs/debug UI.
- Site Grabber (Browser toolbar magnifier → `gui/grabber_dialog.py`): keyword
  search on ANY video site → checkable result list (thumbnails loaded async)
  → batch "Download selected/this page". Fully generic — no per-site code
  or site names anywhere (the tool was developed against one adult site,
  which must never be named in code/docs). Backend is Qt-free in
  `dlmgr/site_grabber.py::SiteGrabber`: (1) search-URL discovery — a GET
  search form on the site page wins, else `SEARCH_PATTERNS` are probed
  (`/search?q=`, `/search/{q}`, `/?s=`, …, with the site's `/xx/` language
  prefix taken from the URL the user gives — pass the CURRENT TAB URL, not
  the bare origin, or you may get the site's default language); a pattern
  "works" when the page yields video cards; winners persist per host in
  `browser.grabber_search_templates`, and the dialog's "Search pattern"
  box shows/overrides it (`{query}`, optional `{page}`; paging otherwise
  appends `?page=N`). (2) `parse_cards` heuristic: same-site anchors that
  contain a thumbnail `<img>`, grouped by href (sites split thumb / duration
  badge / title into separate links), title from img alt → link text,
  thumb from `data-src`/`src`, duration `h:mm:ss`, taxonomy paths
  (`_SKIP_PATH_WORDS`) and text-only links dropped, `?v=id` URLs get
  `slug-id` filenames. (3) `resolve` = `GenericExtractor.extract` →
  `extract_from_page`: `dlmgr/extractors/page_scan.py` scans the HTML for
  m3u8/mpd/media URLs, including inside `eval(function(p,a,c,k,e,d)…)`
  packed scripts (recursive unpacker, radix ≤ 62) and `<video>/<source>`
  srcs; ranks master playlists first, demotes preview/trailer URLs;
  returns `headers={Referer: page, UA}` — CDNs want the page as Referer.
  `NoStreamFound` → `GrabberError` (item stays checked for retry). HLS/DASH
  go to `add_stream_job`, plain files to `add_job`. All page/CDN requests
  use `dlmgr/http_client.py` (curl_cffi Chrome TLS) + `BROWSER_HEADERS`
  (lives there). `ExtractorRegistry.extract` now always tries the
  always-matching generic extractor LAST (user plugins can take
  precedence). Dialog workers keep engine calls on ONE thread (serialized
  mutation). Opt-in Auto-queue (`browser.grabber_auto_queue` +
  `grabber_auto_limit`): each search/page auto-resolves and queues up to
  the limit, only still-checked items are picked (no re-queue of done
  ones); toggling it on mid-session immediately queues the current batch.
  `grabber_last_site` prefills the Site box when no http tab is open.
- Torrent session tools: `set_torrent_rate_limits` / `set_sequential_download`
  / `force_recheck` / `force_reannounce` (all plain — runtime, reversible).
  force_recheck/force_reannounce are engine methods added via the `_enqueue`
  serial-thread pattern (see `engine/torrent_engine.py::_force_recheck`).
- Torrent organization is two-step: `analyze_organization` is read-only;
  confirmed `apply_organization_plan` is restricted under the configured save
  root and calls `TorrentEngine.organize_torrent`, which rejects incomplete
  torrents and uses libtorrent `rename_file` / `move_storage` on its serial thread.
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
  keys) over `backend.audio_tracks/subtitle_tracks`. mpv `aid`/`sid` take the
  track **id** (not track-list index); VLC normalizes
  `audio_get_track_description`/`video_get_spu_description` tuples and
  disables subs with -1.
- Multiview grid (gui/multiview.py, 4×4 since 5.x; 2×2 + dual split added
  2026-11-09): layout registry `MULTIVIEW_LAYOUTS` name → (rows, cols, mv_btn
  label) — "4x4"/"2x2"/"1x2" (dual, side by side)/"2x1" (dual, stacked);
  unknown names fall back to 4×4 at EVERY guard (registry helper,
  PlayerWidget init, MultiViewGrid ctor). Persisted as
  `iptv.multiview_layout` — REMEMBER: `from_file` constructs IPTVConfig
  field-by-field; a new field must be added to BOTH the dataclass and the
  from_file enumeration or it silently resets on restart (that exact bug
  shipped for one review cycle). Everything except the ctor loop is
  count-agnostic (multi-stream allowance, bulk fill, folder rotate, skip-all,
  promote, parallel shutdown). Layout switching (combo in the bar under the
  grid → sig_layout_requested → PlayerWidget._set_multiview_layout):
  teardown + REBUILD a fresh grid widget — tile surfaces must never be
  reparented, and the old widget is `dispose()`d = shutdown + deleteLater
  DEFERRED until the destroy threads exit (a stuck mpv terminate must not
  have its HWND torn down under it; plain shutdown keeps the emptied widget
  as a hidden stack page — that path is unchanged). mv_btn stays a plain
  checkable QPushButton (OverflowRow candidate) whose label follows the
  layout; leaving multiview STILL keeps the hidden emptied page, but a
  later layout change drops it so the next toggle builds the new shape.
  MULTI-STREAM since 2026-11-09 (owner decision, same day as the 2×2/dual
  layouts): the old one-IPTV-stream-per-grid cap is REMOVED — any number of
  tiles may stream in every layout; a provider that caps concurrent
  connections resets what it resets and the affected tile shows its own
  error (the ⛔ force-stop-all path is the escape hatch). Tests:
  tests/test_multiview.py.
- Force-stop all provider streams (2026-11-01, providers cap concurrent
  streams): `PlayerWidget.force_stop_all_streams` ("⛔ Stop All" tools-row
  button + "⛔ Close All Streams" on stream-LIMIT errors in `_show_error`)
  = stop() bookkeeping (generation bump kills pending retries) + multiview
  `_set_multiview(False, force=True)` + `_teardown_media_backend()` (STOP +
  DESTROY — kills the SVP mpv.exe child; `stop()` alone idles the core and a
  wedged demuxer can hold the socket counted). `_ensure_backend` rebuilds on
  the next play; download jobs are NEVER touched. The auto-retry path
  (`_run_scheduled_retry`) also stops the backend before re-opening (same
  stale-connection reason `play()` stops first).
- Player Compact mode is Windows-only pseudo-PiP: it keeps the native video
  surface in place, makes the top-level host topmost, hides surrounding and
- Player Compact mode is Windows-only pseudo-PiP: it keeps the native video
  surface in place, makes the top-level host topmost, hides surrounding and
  secondary player controls, temporarily lowers the host minimum size, and
  resizes to 640x420; exit restores control visibility, minimum size and saved
  geometry. `SetWindowPos` MUST use `ctypes.WinDLL(..., use_last_error=True)`
  with pointer-sized `wintypes.HWND` argtypes — untyped `ctypes.windll` returns
  false on 64-bit Windows and makes the button silently do nothing.
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
  Playback "SVP motion interpolation", default ON since 2026-09-13 — a
  no-op without an SVP install, so SVP users get true motion for files/VOD
  out of the box) is the ONLY way to get
  real soap-opera-effect frame synthesis — mpv's own `interpolation` cannot
  do it (see the verified limit below). NOTHING is bundled: SVP is
  proprietary (~$25 lifetime, 30-day trial, one PC per license) and we only
  cooperate with the user's own install, like SMPlayer/VLC/Plex do. No SVP
  install = greyed-out checkbox, zero behavior change.
  LIVE TV NEVER USES SVP (user decision 2026-09-13): SVP mode is baked into
  the backend at creation, so `PlayerWidget._svp_wanted_for(item)` (setting
  on AND item not live) drives creation, `play()` rebuilds the backend when
  crossing the live/non-live boundary, and `_media_backend_svp` snapshots
  the WANTED state so apply_config/VLC/missing-SVP never loop-recreate.
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
  go 23.976 -> 119.88 fps with `vf=[svp]`; an IPTV **LIVE** channel also
  worked (25 -> 50 fps, 20.1s of playback in 22s wall, no stall/restart)
  when SVP was allowed on live — but live is EXCLUDED by default since
  2026-09-13 (see `_svp_wanted_for` above); the measurement stays here so
  nobody has to re-probe it if that decision is ever revisited.
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
- Audio sync offset (`PlayerBackend.set_audio_delay`/`audio_delay`,
  `iptv.audio_delay` config, default 0.0s): SVP's frame synthesis adds
  video-path latency, so the audio runs AHEAD of the picture. A positive
  `audio-delay` (mpv property / VLC `audio_set_delay` microseconds) shifts
  the audio later to realign it. Adjusted live from the 🎧 audio menu's
  "Audio sync" submenu (presets ±0.1..0.5s, ±0.05s nudge, reset) or the
  `+`/`-` keys (0.05s steps, clamped to ±1s, tooltip OSD), persisted to
  config and re-applied on every playback + `apply_config`. Works on all
  three backends (in-process mpv, out-of-process SVP mpv, VLC).
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
  adult entries stay in Movies. All adult content shares ONE folder
  structure (user decision 2026-09-10): the old JAV sub-category split
  (synthetic "<group> JAV" renaming in classify.populate_years) was removed;
  `_playlist_from_cache` merges any cached legacy "<group> JAV" groups back
  into the base group. Ambiguous words (`adult`, `erotic`) are
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
  * The plain `/movies`+`/scenes` keyword fallbacks only run when parse
    returns ZERO rows (2026-09-12, speed fix). Parse answering with rows
    that failed `_verify` is definitive for that name, and re-querying with
    the shorter studio-stripped title is exactly the loose-keyword noise
    mode — it cost 2 extra sequential requests on the most common miss
    shape (~40% of entries) for ~4% yield, and the extra TPDB volume fed
    the connection-reset/retry cycle. Tests:
    `test_tpdb_skips_keyword_fallback_when_parse_returned_rows`,
    `test_tpdb_keyword_fallback_when_parse_returns_nothing`.
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
- Category matching is SUBTREE-based everywhere (owner rule 2026-11-03, fix
  for "clicking a tree folder only shows some posters"): groups are
  hierarchies — provider group-titles like `VOD|Movies|Action` and local
  disk paths like `MOVIES\Comedy` — so `items_for(section, category)`
  returns the category AND every descendant (`_group_at_or_under`: equal or
  prefix + a separator char `| / \ >`; `:` is punctuation, NOT a separator;
  "MOVIES" never matches "MOVIES 2"). `_rebuild_categories` and
  `years_by_category` synthesize ANCESTOR categories with subtree counts via
  `_group_path_parts`, so every folder level appears in the sidebar (even
  with zero direct items) and its count + year labels match what a click
  shows. The sidebar itself stays FLAT (full-path labels as siblings) —
  nesting the tree visually is a separate, not-requested change.
  GROUPLESS items (local files loose in the library root, provider entries
  without group-title) are the "Uncategorized" folder — `items_for`
  normalizes `group or "Uncategorized"` to match (the old `group or ""`
  compared against a name no item ever had, so that node — and its year
  children — showed NOTHING while the section above showed everything;
  owner report 2026-11-03).
  Tests: tests/test_iptv.py test_items_for_category_matches_whole_subtree /
  test_categories_synthesize_ancestors_with_subtree_counts /
  test_years_by_category_credits_ancestors /
  test_uncategorized_folder_shows_ungrouped_items.
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
  default on; GUI checkbox: Play → Metadata & Cache → "Frame-grab poster
  fallback", applied live via `IPTVManager.set_framegrab_enabled` which lazily
  creates/shuts down the FrameGrabber): when NO provider matches, FFmpeg
  pulls a frame from the stream
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
  decode); seeks 120s then 8s so short clips still yield a frame. Some
  redirecting MP4 sources make FFmpeg reject the optional HTTP reconnect args
  with `Option reconnect not found` after opening the input; `_run` retries that
  exact compatibility failure without reconnect args rather than blanking the tile.
  MEASURED live: stream CDNs reset the TLS handshake under bursts while the
  content is perfectly fine (HEAD 200 / GET 206 on the same URL), so `_run`
  classifies stderr as transient vs permanent — transient failures are
  retried (`_RETRY_PASSES`) and NEVER negative-cached, permanent ones are
  remembered. Without that, 2/6 real grabs failed; with it, 14/14 succeeded
  (~4.3s each, 480x270 JPEG). Do not collapse that distinction back into a
  plain bool.
- Cover population is three-layered in `ContentGrid`: visible tiles resolve
  immediately, `_PREFETCH_TILES` (250) rows either side of the viewport are
  pre-downloaded (capped by `_MAX_PENDING_ARTWORK` so a fast scroll can't
  starve on-screen tiles), and a `_sweep_timer` walks `_sweep_queue` — every
  artwork-less entry in the *current section* — at `_SWEEP_BATCH`/sec, which
  deliberately matches the metadata limiter (5/s) so the queue never grows
  and on-demand lookups wait ~1s at most. The MetadataPipeline executor is
  16 workers (2026-09-12, speed fix): a provider chain lasts ~2-4s of
  SEQUENTIAL round-trips, and the old 4-worker pool starved the 5/s limiter
  to ~1.6 lookups/s — the sweep over a 48k adult section took 8+ hours
  instead of 2.7h and visible tiles queued behind busy workers. Keep
  workers comfortably above rate × latency so the limiter stays the
  bottleneck; provider sessions are built by `_pooled_session()` (pool 20)
  because urllib3's default 10-connection pool discards keep-alive
  connections at that concurrency. Switching sections abandons the
  sweep (`set_items` clears it). `_visible_range` uses `indexAt` probes at
  several x fractions (icon-mode gaps return invalid indexes) with a
  scrollbar-ratio fallback — never a full 30k-item scan per scroll event.
  `DetailPanel.artwork_found` -> `ContentGrid.apply_external_artwork` pushes
  a poster discovered by clicking an entry back onto its tile. The sweep is
  slow ON PURPOSE (rate limited); `ContentGrid.sweep_progress` drives the
  "Finding artwork n/N" bar in the status row so that's visible — prefer
  showing progress over adding pressure to the API limits.
- Artwork cache maintenance (2026-08: the cache had silently grown to 2.6 GB
  against a dead 500 MB setting — `cache_limit_mb` was never read anywhere):
  * Thumbnails are lossy WebP q85 (`<sha1>_300x450.webp`, ~5-10x smaller than
    the old PNGs, keeps alpha for logos); legacy `.png` thumbs stay readable
    via `_legacy_thumb_path`/`_thumb_candidates`. Qt + Pillow both do WebP.
  * `ArtworkCache._touch` bumps atime on every cache hit — Windows disables
    NTFS last-access updates by default, and `enforce_size_limit` is LRU by
    max(atime, mtime), evicting full+thumbs(+.meta) as one URL-keyed group.
    Never drop the touch or eviction degrades to oldest-first FIFO.
  * `cache_limit_mb` (default 10240 = 10 GB; a saved 500 migrates up in
    `from_file`) is enforced by `IPTVManager.enforce_cache_limit_async()`
    after every load sweep and when the Metadata & Cache page is saved.
  * Play → Metadata & Cache shows live disk usage (`cache_stats`, computed
    off-thread) and "Delete All Cached Artwork & Metadata" → confirm →
    `IPTVManager.clear_caches_async`: artwork files + the metadata table
    (incl. negative rows — the sanctioned way to purge them after a lookup
    fix) + VACUUM + `FrameGrabber.reset()`. Playlists/EPG/favorites/recent
    deliberately survive (user data / instant startup). The dialog emits
    `cache_cleared` → `ContentGrid.reset_artwork_state()` drops ALL in-memory
    memos (`_pixmaps`, `_failed_urls`, `_logo_tried`, `_logo_pending`…) and
    restarts the sweep — skip that and the grid serves dead pixmaps forever.
- Wikipedia provider (last-resort poster): `/api/rest_v1/page/summary/{key}`
  returns the infobox image as structured `originalimage`/`thumbnail` (plus
  `extract` as a free synopsis) — never regex page HTML again. Disambiguation
  pages are rejected; candidates are ranked media-suffix-first
  (`_MEDIA_TITLE_RE` — "(1984 film)" / "(TV series)") because Wikipedia's
  primary topic is often not the film ("Dune" → the landform). The known year
  goes IN the Wikipedia query ("Dune 1984" → "Dune (1984 film)" first) — the
  exact OPPOSITE of TMDb, where a bare year in the query returns zero hits.
  TMDb itself now prefers the result whose year matches the requested one
  (remakes outrank the right film by popularity otherwise).
- IPTV grid layout (gui/iptv_tab.py): posters are DYNAMIC, not fixed 240x320.
  `ContentGrid._recompute_tile_size` fits the viewport — 3 rows always visible
  (height-driven), column count = how many ~240px (native poster width) tiles
  fit, CLAMPED to 3–9 (user decision 2026-09-12: min 3, max 9 fully open);
  tiles then stretch to fill the width exactly for the chosen count, posters
  shrink when the window shrinks to keep the 3-row invariant. The fill width
  subtracts `_PACK_SLACK` (12px) — QListWidget needs a few px beyond
  N*(w+spacing)+spacing to pack the Nth column, and a zero-slack exact fill
  silently drops the last column at exact boundaries (measured: 4 computed,
  3 packed at ~1200px). It runs on show/resize (showEvent also defers one pass
  for post-layout geometry). `PosterDelegate` (a QStyledItemDelegate) paints
  each tile: poster aspect-kept, title wrapped, and two overlaid buttons at
  the top
  — Select (filled accent #2a7abf when the tile is selected) and Play. Buttons
  show while hovered or selected. Hit-testing is in `ContentGrid.mousePressEvent`
  via `PosterDelegate.tile_layout`/`button_rects` (shared with paint so geometry
  matches); Play emits `itemActivated`, Select emits `itemSelected`. The delegate
  reads the icon size from `option.decorationSize` (NOT `option.iconSize` — that
  attr doesn't exist on QStyleOptionViewItem in PySide6 and crashes sizeHint).
- The metadata `DetailPanel` is NO LONGER in the content splitter under the grid
  (that used to squeeze the other posters). It's now a separate zone: a child of
  `PlayerWidget` floating over the `video_stack` (left ~40%, never covering the
  control bar), positioned by `IPTVTab._layout_detail_overlay` on player resize
  (eventFilter on `self._player`). It shows on selection and HIDES on play
  (`_play_item` calls `self._detail.hide()`) so it never sits on active video;
  fullscreen saves/restores its visibility. Series are the exception — Play on a
  Series opens the overlay (episode list) instead of playing.
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
  (api.opensubtitles.com/api/v1) — `Api-Key` + UA "DeepFlux v<version>" headers;
  hash search (classic size + 64KiB head/tail sum) first for local files,
  title query fallback; download is two-step (POST /download {file_id} →
  short-lived link → GET). 406 = daily quota (suggest account creds);
  optional username/password login → Bearer JWT raises it. Keys live in
  `config.iptv.opensubtitles_*` (env `OPENSUBTITLES_API_KEY` fills an empty
  key), entered in API menu → OpenSubtitles Key…. Player CC menu →
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
- RSS subscriptions are agent-managed too: `add_rss_feed` / `update_rss_feed` /
  `remove_rss_feed` require confirmation and persist via
  `DeeptorrentConfig.default_config_path()`. Feed results preserve GUID-backed
  stable item ids; reads do not mark items seen, and `download_from_feed`
  confirms and marks only successfully downloaded ids. Fetches reject
  local/private targets and redirect pivots; XML uses `defusedxml` with a 5 MiB
  response cap. These operations rebuild the ToolRegistry's RSSMonitor; call
  `default_config_path` on the CLASS.
- App-setup tools (user decision 2026-09-12): the agent CAN configure
  anything the user could have typed into a dialog — the old blanket "settings
  are not agent-controllable" policy was replaced. In `agent/tools.py`:
  * `iptv_list_sources` / `iptv_add_source` / `iptv_update_source` /
    `iptv_remove_source` manage `config.iptv.sources` (all CONFIRMATION-gated).
    `iptv_add_source` validates m3u_url/xtream URLs with
    `validate_public_http_url` AND streams the first KB through
    `_peek_playlist` — the URL must serve `#EXTM3U` (dead links and
    EPG/XMLTV links pasted as playlists are rejected; `validate=false`
    skips). After ANY iptv config write the handler calls
    `bridge.request_reload()` → `_AgentBridgeSignals.reload_config` →
    `IPTVTab.reload_config` on the GUI thread — the exact settings-OK path,
    so sources/keys/player options apply live. Update semantics use
    None-sentinels: schema defaults are NOT injected for optional update
    fields, so "rename only" never resets enabled/credentials.
  * `list_api_keys` / `set_api_key` — key & credential slots
    (`API_KEY_SLOTS`) are WRITE-ONLY: the agent may write one (value
    redacted everywhere via SENSITIVE_TOOL_FIELDS / redact_tool_arguments)
    but can never read values back; `list_api_keys` shows only
    configured true/false. Covers llm/perplexity/brave/jackett/tmdb/omdb/
    fanarttv/tpdb/stashdb/opensubtitles(+account).
  * `list_settings` / `set_settings` — non-secret SCALAR settings by dotted
    path, whitelist derived automatically from the config dataclasses
    (`_iter_setting_fields`) minus `_SETTINGS_DENY_PATHS` (forced-on fields,
    internal bookkeeping) minus secret leaf names (`_SECRET_NAME_PARTS`
    includes "username"). Enum-ish fields validated via `_SETTING_ENUMS`;
    values coerced to the field's type. iptv.* writes trigger the live
    reload; most other sections apply on next use/restart (result says so).
  * `list_torrent_sources` / `add_torrent_source` / `remove_torrent_source`
    — `config.sources.sources`; search_indexers reads that list live per
    call, so new sources are used immediately.
  * All setup mutations persist via `config.to_file(default_config_path())`
    — call `default_config_path` on the CLASS (same as the RSS tools).
  * Context routing is GONE (user decision 2026-09-15, after a live 4.5
    failure): `tool_names_for_context` returns the FULL toolset for every
    task. The old keyword gating hid the setup mutations whenever a message
    missed the magic words — "here's my jackett key" contains no "api key"
    phrase, so `set_api_key` wasn't even in the schema list and the agent
    truthfully told the user it couldn't write settings (same blind spot
    broke installs). Cost: ~89 schemas ≈ 12k tokens per request — accepted.
    Do NOT reintroduce keyword routing for mutations; if context cost ever
    matters, trim schema DESCRIPTIONS, not visibility.
- `run_shell` (user decision 2026-09-14): the agent may run shell commands
  to finish the machine's setup (winget-install FFmpeg/Jackett, run a
  downloaded installer, check what's installed). CONFIRMATION-gated like the
  other setup mutations, NEVER in the watchdog allowlist, result marked
  untrusted (command output can carry prompt injections), output
  head+tail-truncated to 16 KB. The SYSTEM_PROMPT carries the FFmpeg winget
  gotcha: `winget install Gyan.FFmpeg` lands in
  `%LOCALAPPDATA%\Microsoft\WinGet\Links`, which is NOT on the running
  app's PATH until restart — the agent must point `download.ffmpeg_path`
  at it via set_settings right after installing. CRITICAL implementation
  facts (both found by review, 2026-09-14 — do not regress):
  * NEVER use `subprocess.run(shell=True, timeout=...)` here: at timeout it
    kills only cmd.exe, then its second communicate() blocks WITHOUT a
    timeout until every grandchild holding the stdout pipe exits — a hung
    installer blocked the single agent worker forever. The handler uses
    Popen + communicate(timeout), then on TimeoutExpired kills the whole
    TREE (`taskkill /F /T /PID` on Windows, `os.killpg` + start_new_session
    on POSIX), then a bounded 10s drain, then abandons the output if a
    survivor outside the tree (elevated installer, `start /b` orphan) still
    clings to the pipe. Regression tests pin the wall-clock bound
    (`test_run_shell_timeout*`).
  * The per-call wait is clamped to `min(600, llm.task_timeout_seconds)` —
    the loop only enforces the task budget BETWEEN tool calls.
  * Commands whose output likely contains stored secrets (env dumps,
    `config.json`, `.dfc`, `control_api.token` — patterns in
    `_SECRET_DUMP_PATTERNS`) get an informed-consent warning appended to the
    confirmation ask (loop `_queue_confirmation`) AND a `warning` field in
    the result; shell output itself is never key-scrubbed — the exact
    command preview is the real gate.
- The SYSTEM_PROMPT identity is now "all-in-one media and download center"
  (NOT "BitTorrent download manager") with an Introductions block governing
  the auto-greeting: short, plain-language, whole-app breadth, no tables, no
  fixed template. The greeting itself is generated by the LLM — the GUI just
  auto-sends "Hello" 1.5s after launch (MainWindow._send_startup_greeting).
- Deliberately still NOT agent-controllable: generic arbitrary config JSON
  injection (the whitelist is the boundary), reading secret values of any
  kind, and UI-state fields (ui_*, geometry, splitters).

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

## Room subsystem (ircmgr/ + gui/room_tab.py)
- `ircmgr` is a HISTORICAL name: the IRC client was REMOVED completely in
  4.1, and the homegrown P2P room (ircmgr/room.py, TCP star topology +
  SHARED_ROOM_KEY + website/api/room.js rendezvous) was RETIRED in 5.1.
  The package now holds `state.py` (thread-safe chat state: ring buffers +
  nick lists, keyed by the ROOM_NET_ID pseudo-network) and `oh_room.py`.
  Do not re-add IRC client code, IRC agent tools, `config.irc`, the PyPI
  `irc` dependency, or the old room.py protocol. `config.shared_room_secret`
  and the SHARED_ROOM_KEY slot are KEPT but UNUSED (old builds keep
  loading; gen_embedded_keys.py unchanged). website/api/room.js stays
  DEPLOYED (old exes 4.1–5.0 still call it) — never delete it from the
  site; it costs nothing.
- `gui/room_tab.py::RoomTab` (tab title "Room", index 5, Ctrl+6), since
  2026-09-23: the tab EMBEDS the OnlyHumans join portal
  (https://onlyhumans.deepflux.space/join) in a QWebEngineView — the exact
  web app the site serves, so portal chat updates land in the app with the
  site's next deploy and the chat UI is not duplicated here. The room word
  is prefilled through the URL FRAGMENT (`/join#room=deepflux` from
  `chat.default_word` — the portal reads the hash client-side as
  `invitedWord`; the word is never part of any HTTP request), and the name
  field is deliberately LEFT EMPTY (owner choice: the user types a name;
  the portal's own "remember my name and rooms" checkbox then persists in
  the app's storage). DEDICATED persistent QWebEngineProfile
  ("deeptorrent-room", storage ~/.deeptorrent/room-portal) — never the
  Browser tab's profile: the portal identity key (localStorage
  `oh-portal-seed`) must survive restarts and must not be wiped by
  Clear-Browsing-Data. Portal file chips download via blob URLs — without
  a downloadRequested consumer QtWebEngine silently drops them, so
  MainWindow calls `room_tab.attach_download_handler(
  self._on_browser_download_requested)` right after construction.
  `DF_NO_ROOM=1` skips creating the view entirely (no page load, no
  /api/gk fetch — set by packaging/verify_boot.py AND tests/conftest.py
  for every test; the one view test delenvs it and swaps PORTAL_URL for
  about:blank so the real site is never fetched). `RoomTab(config,
  parent)` — MainWindow passes the parent POSITIONALLY as the second arg;
  keep it there (3.9 lesson). Shutdown (`closeEvent` → `room_tab.
  shutdown()`) best-effort dispatches a synthetic `pagehide` into the page
  (the portal's listener mails the Leave + presence beacon; abrupt death
  also heals via host TTLs, so it is strictly best-effort).
- DeepFlux Room (`ircmgr/oh_room.py`, 5.1): the native Python
  MAILBOX-ONLY member of the same protocol. Since the 2026-09-23 portal
  switch the GUI no longer RUNS it (a second member next to the portal
  page would double the user's presence in the room) — the module + its
  tests stay in the tree as the protocol reference and spec port; keep it
  working. Every word is a room; Room "deepflux" is shared with the
  OnlyHumans Windows app and the browser portal — same word, same room.
  PROTOCOL (port of C:\OnlyHumans-main\portal\portal.ts + app.ts,
  KAT-verified against the Rust core's kat.json — a copy is embedded in
  tests/test_oh_room.py; the OnlyHumans repos are the spec, do not
  re-derive): word → egk = Argon2id(m=64MiB t=3 p=1, salt=SHA256(
  "OH1-pass-v2|"|gk|word)); room = SHA256("OH1-room-v1|"|egk)[:16];
  GK ("universe key") is PUBLIC by design, fetched from /api/gk (60s
  cache) and rotated per OnlyHumans release; identity = local Ed25519
  seed at ~/.deeptorrent/room_identity (NOT in config.json — Settings
  Export must never transplant a chat identity), peer id = base58btc
  identity multihash of the libp2p pubkey protobuf; admission proof =
  HKDF(egk, salt=[], info="adm"|peer|nonce); host mints a random room
  key at epoch 1, delivers it sealed (XChaCha20-Poly1305, HKDF-derived
  per (egk,room,epoch,recipient) — epoch-bound so old deliveries can't
  XOR out newer keys); chat/members/rotate frames use per-message
  subkeys HKDF(room_key, room|epoch, kind|sender|seq) with AAD binding
  kind/room/epoch/sender/seq and PADDED to size buckets; replay guard =
  per-sender monotonic seq; host election = PUT /api/room NX
  (first-writer-wins, 409 = live record); a member silent-hosted >45s
  re-runs discovery and TAKES OVER keeping the same key+epoch; epoch>1 =
  sealed (Join refused with an Error envelope). All delivery goes
  through the hub's sealed inbox (PUT /api/inbox ≤16 items, ~4s
  per-sender throttle → one 4.2s retry; destructive drain every ~2-3.5s
  seated/2s seeking; reg + presence every 31s; host refresh 45s vs the
  record's 300s TTL). Latency: ↔ portal ≈ seconds, ↔ OnlyHumans DESKTOP
  peers up to ~30s (their 30s drain) — that is the documented "⇄ site"
  fallback, not a bug.
  GOTCHAS (measured/verified 2026-09-22 — do not re-derive): (1)
  `cryptography` 46 has NO XChaCha20Poly1305 AEAD — PyNaCl's
  `nacl.bindings.crypto_aead_xchacha20poly1305_ietf_*` is the AEAD;
  Ed25519/HKDF/SHA256 come from `cryptography`, Argon2id from
  `argon2-cffi` (requirements floor + app.spec collect_all both; the
  other two specs list ircmgr.oh_room in hiddenimports).
  (2) argon2-cffi's `Type.I` is Argon2i — the protocol needs
  `Type.ID` (Argon2id); the argon2d KAT row is what proves the call
  path when debugging this again. (3) The b64 is the URL-safe alphabet
  without padding, hand-rolled (exact round trip pinned by KAT).
  (4) The controller keeps `_role` ∈ {left, connecting, active} and
  DERIVES the public role ("host"/"member") from `_is_host` — takeover
  and 409-demotion flip only `_is_host`; never store them separately or
  they desync. (5) `_stop_room` must clear `_room_hex` or the next
  join's retry pacing (REJOIN_RETRY on `_room_hex` set) delays the
  first attempt. (6) KeyDelivery acceptance is gated on
  sender == host_id (GK is public — anyone can SEAL a key; only the
  host we asked may seat us) and epoch ≥ current (adopts missed
  rotations). (7) History: sealed-at-rest JSONL under
  ~/.deeptorrent/rooms/<roomhex>.jsonl, XChaCha key HKDF-derived from
  the identity seed, loaded once per join session (_history_loaded)
  BEFORE the connected event so the tab's re-render shows it.
  (8) The 5.1 GUI auto-joined "deepflux" at launch — RETIRED 2026-09-23
  with the portal switch (the join gate needs a typed name; the portal's
  remember checkbox replaces it). `chat.nickname`/`auto_join`/`last_word`
  are kept in ChatConfig for old configs but no longer consumed; only
  `chat.default_word` feeds the embedded URL. `DF_NO_ROOM=1` (verify_boot
  + tests/conftest.py) now means NO web view at all.
  (9) E2E recipe (proved 2026-09-22): two temp-identity controllers
  against the REAL hub seat in ~12s; the browser portal at /join joins
  the same word and exchanges messages both ways — full cross-client
  interop. Chat content is NEVER fed to the LLM and must stay
  non-agent-callable. Tests: tests/test_oh_room.py (KAT + FakeHub
  flows + the portal-host RoomTab tests; FakeHub mirrors the real
  endpoints including NX election + destructive drain).
- PySide6 test gotcha: patching `QMenu.exec` (or any C++ method) on the
  CLASS does NOT intercept instance calls — shiboken resolves instance
  methods through the C++ method table, bypassing Python class attributes,
  so a test that patches it hangs on a real modal menu. Split menu
  construction from execution, or call via the class (static methods like
  `QInputDialog.getText` / `QMessageBox.question` DO patch fine because the
  code calls them on the class).

## IPTV + filesystem agent tools (Play / Command tabs)
- Play-tab folder search: the search box has a 📍 Folder scope toggle.
  Scoped queries run only inside the tree node recorded in
  `IPTVTab._scope_key` (set on tree click, source-combo jump and group-mode
  change; `_scope_label` drives the placeholder text), and while scoped,
  `_show_section` re-filters every opened folder through `_folder_filter` —
  one query can be carried across folders by clicking around. The toggle
  swaps its objectName to `btn_accent` while checked: the global QSS has no
  `QPushButton:checked` rule, so without that a checked toggle is visually
  indistinguishable from unchecked.
- Play-tab VOD downloads (movies / series / adult — adult is just Movies):
  DetailPanel's ⬇ button, the grid AND list context menus, and the
  episode-list right-click (single episode or whole season) all funnel into
  `IPTVTab._download_batch` → the dlmgr DownloadEngine (injected via
  `set_download_engine` in MainWindow). Rules: engine calls run on ONE daemon
  thread per batch — `add_stream_job` parses the HLS/DASH manifest over the
  network and must never run on the GUI thread; `.m3u8`/`.mpd` URLs →
  `add_stream_job`, everything else → `add_job`; episodes land in
  `<download default_folder>/<series name>/` and the folder is passed with a
  TRAILING `os.sep` — that's how `_prepare_save_path` knows it's a directory,
  not a file path; request headers come from `build_playback_headers(item,
  owning source)` so source-level UA/Referer survive. Whole series/seasons
  confirm first (count + destination); single items queue immediately. Live
  channels are never offered a download (endless streams).
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
- Global EPG URL: `config.iptv.epg_url` (Play → Metadata & Cache, "EPG URL
  (all sources)") applies to every source without its own per-source
  `epg_url`. Precedence: per-source > global > playlist url-tvg; wired in
  `_refresh_source_inner` and applied live via `IPTVManager.set_epg`
  (fetches immediately when the URL changes). The `enable_epg` checkbox
  gates the guide download (`enable_epg` was a dead setting before 3.2.8).
  Timezones: `_parse_xmltv_date` honors XMLTV offsets → epoch, and
  `epg_now_next` returns start/end epochs (`now_start`…); the Now Playing
  column renders `HH:MM–HH:MM` via `time.localtime` — automatically the
  system TZ, no setting needed. parse_xmltv tolerates two provider faults
  (VERIFIED on epg.mybunny.tv, 2026-08): truncated chunked downloads
  (ParseError "no element found" → keep the partial guide, 14.6k/22k
  programmes) and latin-1 payloads with a UTF-8 declaration (expat reports
  this as "invalid token" ParseError, NOT UnicodeDecodeError — retry through
  a latin-1 text wrapper and keep the pass that got further). An empty parse
  never overwrites a good cached guide (save_epg replaces per-url rows).
  EPG downloads retry 4× with the 2s/5s/15s backoff (same flakiness as M3U
  downloads — a bare reset used to kill the whole guide update).
- EPG visibility (3.2.8): `epg_now_next` returns now/next + epoch times
  (`now_start`…); surfaces are ContentList "Now Playing" column, grid live
  tiles (`_EPG_ROLE` second text line, visible tiles only, 60s
  `_update_epg_tiles` refresh + fill-missing on scroll), the channel detail
  panel (Now + elapsed %, Next, 24h "Upcoming" guide — display-only rows,
  `epg_guide_for`/`cache.epg_programmes`), and the player status line on
  Play. Channels resolve via `IPTVManager.epg_channel_id`: tvg-id when the
  guide carries it (`epg_has_channel`), else the name normalized through
  `IptvOrgLogos._split_country` matched against the guide's OWN `<channel>`
  display names (parsed by `parse_xmltv(with_channels=True)` into the
  `epg_channels` table; name map rebuilt on `EPGManager.channels_version`).
  VERIFIED on the user's playlist + epg.mybunny.tv: 96% of PT channels
  resolve (tvg-id + name fallback); misses are feed coverage gaps (US/UK
  channels in a PT guide). `maybe_refresh_epg` (hourly QTimer in IPTVTab)
  re-fetches guides older than 6h. `ch.epg_now`/`epg_next` model attributes
  are legacy — nothing populates them; always query the EPG store.
- Low-resolution settings: `gui/settings_dialog.py` exposes
  `API_KEY_PAGES`, `BROWSER_SETTINGS_PAGES`, and `DOWNLOAD_SETTINGS_PAGES`;
  each drives a submenu whose entries open one focused 580–600×420–440 page.
  The dialog classes retain `page="all"` for compatibility, keep every page
  scrollable, and save only the selected section so hidden fields cannot
  overwrite unrelated settings. Menu-launched scalable viewers cap their
  defaults/minimums to fit within an 800×600 desktop.
- Menu bar layout (3.5.4): **File** and **Help** are the only real menus.
  The six page titles — Browse, Agent, Download, Play, Command, Room — are
  PURE tab buttons (menu-less QActions on the bar; a click always switches
  the page, `triggered` also switches for keyboard activation). Everything
  the old per-tab menus carried lives under File in flat labeled zones:
  API Keys, Export/Import Settings + file associations, Browser Settings
  (pages, History, Save PDF, DevTools), Download
  (Add Magnet/Torrent, Jackett, Download Settings pages, Sources, RSS),
  Play (the four IPTV_SETTINGS_PAGES), and Exit.
  Zones are built with `_file_zone` (separator line + bold disabled header)
  + `_file_item` (small text indent) — `QMenu.addSection()` must NOT be
  used: its text does not render under the app stylesheet (verified
  offscreen 2026-09-10 — only an unlabeled thin line appeared, so the
  shipped menu had invisible zones until then).
  Bookmarks are NOT in File (3.5.6): the browser toolbar's Bookmarks
  button (left of Private) owns a STANDALONE QMenu with Import/Export +
  the folder tree (`_rebuild_bookmarks_bar` repopulates it; never
  `setEnabled(False)` it — exec() on a disabled menu silently does
  nothing, which made the button look dead in 3.5.5). The app
  STARTS on the Agent tab (set in `MainWindow.__init__` right after
  `_build_ui()` — NOT inside `_build_ui`, which runs first; don't re-add a
  tab selection there, __init__ runs after and would override it).
  `_TabMenuBar` (gui/main_window.py) paints the active page's button with
  the turquoise box. The tab index is stored as a dynamic property on each
  action — do NOT key a dict by QMenu/QObject wrappers: shiboken may hand
  out a fresh wrapper for the same C++ object in the full app, and
  `in`/`dict` lookups then silently fail (bare-widget tests won't catch
  it). `_TabMenuBar.addMenu` keeps every menu in `self._menus`: PySide
  returns PYTHON-OWNED QMenus from `addMenu()`, so losing the last Python
  reference destroys the C++ menu and its title silently vanishes from the
  bar on the next GC (this bit us when the `_tab_links` dict — the only
  thing holding them — was removed). Menus open on CLICK only —
  mouseMoveEvent is swallowed while any popup is visible, so hovering
  across titles never tears down/switches the open menu (Qt's default
  menu-mode mouse tracking). Click-opened popups auto-close when the cursor
  leaves the popup/submenus and the bar for ~450ms (`_auto_close_check`
  poll, 3×150ms ticks); keyboard-opened menus are never armed so arrow-key
  nav survives. All keys live in `APIKeysDialog` (File → API
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
  is queued onto the GUI thread and blocks ≤35s. Late JS callbacks are ignored
  after timeout. All Agent JavaScript runs in `QWebEngineScript.ApplicationWorld`,
  never the page's MainWorld. NOTE: neither `call` nor
  `ToolRegistry._browser_call` may name their first param `action` — the
  `browser_go` tool forwards an `action=` kwarg and Python would bind it twice.
- PySide6 6.11 REGRESSION (found live 2026-09-15, do not regress):
  `QWebEnginePage.runJavaScript` callbacks return `''` for any JS OBJECT
  result — every world; strings/scalars still marshal. Every bridge op that
  built its result in JS silently returned `{'success': True, 'value': ''}`
  and agent browser control was COMPLETELY broken (navigate/list_tabs worked
  because they build results in Python; unit tests all faked the bridge, so
  only a live run caught it). The bridge now wraps every script via
  `_wrap_script` (`JSON.stringify((<script>))`) and parses centrally in
  `_coerce_js_result` — `gui/milkdrop.py` used the same string pattern all
  along. `_run_js` treats None/'' as a loud failure, never success.
  Regression tests drive the REAL view (`test_real_page_*` in
  tests/test_browser_bridge.py — real page IPC, no network via setHtml);
  they fail on the pre-fix code. If a future PySide6 bump breaks string
  results too, those tests will catch it.
- `_sanitize_page_content` redacts EVERYTHING that reaches the LLM: body
  text (email/token/JWT regexes), top-level url, `links[]` AND `controls[]`
  and click-result `label`/`href`/`url_before` (signed-link query values
  via redact_url_secrets; javascript:/data:/file: hrefs dropped/blanked —
  review 2026-09-15: control hrefs once sailed into the model context).
- Tools: navigation/tab/go/scroll/bookmarks plus consent-gated
  `browser_get_content`; preferred automation is `browser_snapshot` → stable
  `eN` refs → confirmed `browser_click_ref` / `browser_type_ref` /
  `browser_select_ref` / `browser_check_ref`, with `browser_wait` after async
  actions. Legacy selector/text click/fill remain confirmation-gated. Page
  sharing is remembered per origin and always denied in private tabs; text,
  token-like values, email addresses and signed-link query values are redacted
  locally before reaching the LLM. Closing tabs and bookmark mutations also
  require Agent confirmation. Agent navigation auto-focuses the Browse tab.
- Browser UX/state: configurable Google/DDG/Bing/Brave search with shared URL
  normalization; Ctrl+F find bar, progress/error/renderer recovery, accessible
  toolbar names, per-origin zoom, F12 DevTools, PDF save, history dialog + SQLite
  autocomplete (`gui/browser_history.py`), bookmark HTML export, and restore of
  up to 20 non-private tabs. Off-the-record tabs never persist, enter history,
  expose the Agent bridge, or receive persistent cookies. Browser Settings can
  clear history, cookies/cache and remembered Agent-origin permissions separately.
- Zoom: besides Ctrl+=/-/0, Ctrl+scroll-wheel zoom works and the nav-bar
  percentage badge is clickable (menu: in/out/reset, `tests/test_browser_zoom.py`).
  The wheel interception MUST be an app-level event filter
  (`MainWindow.eventFilter` → `_browser_view_for_widget` parent-walk): wheel
  events target QWebEngineView's internal render widget, so per-widget /
  ancestor filters never fire (same reason the chrome hover reveal uses
  a poll timer — the nav buttons + tab strip form ONE layout row block
  above the web view (`_browser_chrome`) that pushes the page down when
  revealed, never covering site content; owner change 2026-10-26 retired
  the old floating overlay, 2026-10-28 folded the address bar into the
  auto-hide and added the left-edge rail reveal, 2026-11-04 moved the
  address bar back OUT into the always-visible menu row — see the next
  bullet).
  Detached fullscreen views are matched via `_browser_fs_state` and zoom
  without touching the badge (it tracks the active tab only).
- Menu-bar address bar (2026-11-04): the browser URL bar lives in the MENU
  BAR row, right of Help, filling the remaining width while the Browse tab
  is active (`browser_url_bar` is created in the Browse-tab build, hosted in
  `_MenuUrlHost` — a QWidget subclass whose sizeHint tracks the fill width
  because QMenuBar anchors corner widgets by sizeHint and never expands
  them). `_fit_menubar_url_host` recomputes width + explicit geometry
  (setCornerWidget/layout passes use a STALE hint otherwise — the bar
  overhangs the right edge; verified offscreen) on every menubar Resize via
  the app-level eventFilter and deferred (singleShot(0)) on tab activation.
  Entering/leaving Browse SWAPS the corner widget in/out via
  setCornerWidget(host/None) — merely hiding it does NOT shrink the menu
  row (pinned in tests/test_browser_chrome.py::test_menubar_url_bar_*).
- Browser auto-hide chrome + rail (2026-10-28, `tests/test_browser_chrome.py`):
  while the Browse tab is active the nav-button + tab-strip chrome
  (`_browser_chrome`) and the activity rail are hover-hidden — the
  site gets the full window (the address bar itself stays reachable in
  the menu row). The 120ms `_strip_poll` cursor poll drives
  everything (`_poll_browser_hover` → `_check_chrome_hover` on
  `_browser_tab` coords / `_check_rail_hover` on `main_tabs` coords,
  negative x = over the rail). Rules, do not regress:
  * Reveal zones are a THIN edge (10px, `_EDGE_REVEAL_PX`) while hidden;
    once visible the zone widens (chrome height + 6 / rail width + 6) so
    the bar doesn't flicker-hide at its own boundary. A full-height reveal
    zone would pop the bar while the user aims at site headers.
  * Visibility is tracked in FLAGS (`_chrome_hidden`,
    `_rail_hidden_for_browser`), not isVisible() — the poll must keep
    working while the tab page itself is hidden (offscreen tests rely on
    this too).
  * The rail hides ONLY on the Browse tab; the poll's non-browser branch
    restores it — but only when the `_rail_hidden_for_browser` flag says
    WE hid it, so pure video fullscreen (`set_video_pure_chrome`, which
    hides the rail itself) is never fought.
  * DOM fullscreen (`_browser_fs_state`) force-hides chrome + rail.
  * Progress bar + find bar stay OUTSIDE `_browser_chrome` (visible
    during loads / Ctrl+F with chrome hidden); `_browser_focus_url`
    (Ctrl+L) calls `_show_browser_chrome()` so focus never lands on an
    invisible bar.
  * Link context menu: `_BrowserView.contextMenuEvent` rebuilds Qt's
    standard menu with `promote_open_link_action()` — "Open link in new
    tab" moved to the FIRST row (most-used right-click action, owner
    request). Non-link right-clicks are untouched (QtWebEngine only adds
    link actions for link contexts). DevTools views stay plain
    QWebEngineView.
- Address bar editing (`tests/test_browser_url_bar.py`): Qt sends the focus
  widget `FocusIn(PopupFocusReason)` whenever a Qt::Popup closes — including
  the history QCompleter's popup hiding mid-typing when the prefix stops
  matching — so the select-all-on-focus filter (`_url_bar_event`) MUST skip
  `PopupFocusReason`/`ActiveWindowFocusReason`, or the next keystroke
  replaces everything typed ("letters disappear"). `_url_bar_editing()`
  (focused + `isModified()`) gates `urlChanged`/`loadFinished` writes to the
  bar so a redirect/pushState in the current tab can't clobber typed text;
  `_browser_navigate` clears the modified flag so the bar follows the load
  again, the completer's `highlighted` re-sets it, Esc reverts to the page URL.
- DOM fullscreen (YouTube ⛶ etc.): `_BrowserPage` enables
  `FullScreenSupportEnabled` and `MainWindow._browser_fullscreen_requested`
  accepts `fullScreenRequested` — Qt WebEngine NEVER honors the Fullscreen
  API by itself, an unaccepted request silently no-ops. The view is
  detached from `browser_tabs` (tab handlers all tolerate indexOf == -1)
  and shown as its own fullscreen window; ESC (QShortcut →
  `triggerAction(ExitFullScreen)`) and Alt+F4 (eventFilter reroutes Close)
  exit cleanly. `closeEvent` destroys a fullscreened view so it can't block
  the quit.
  waits ≤20s for chanlist_ts, `refresh=false` reuses the cache — plain
  sequential, not READ_ONLY) and `irc_list_nicks` (READ_ONLY, nicks + topic
  from IRCState).
- Browser downloads/magnets/playback are deny-by-default: every page-triggered
  action shows source + exact target before execution. Non-torrent files route
  to DownloadEngine; `.torrent` files use QtWebEngine then enter the torrent
  engine after completion. Cookie forwarding tracks add/remove, domain/host-only,
  path, Secure and expiry scope; only approved same-site downloads receive
  cookies, and private tabs never reuse persistent-profile cookies. dlmgr note:
  `segment.py` must NOT use `with http_client.get()` — curl_cffi's Response
  isn't a context manager; close it explicitly.
- Browser engine capabilities (`_BrowserPage` + profile setup in
  `MainWindow.__init__`):
  - **Navigation policy**: `acceptNavigationRequest` intercepts `magnet:`
    links, then asks before handing them to the engine. `javascript:`,
    `vbscript:`, `file:`, top-level `data:`/`blob:` and unsupported schemes are
    blocked regardless of navigation source; only `about:blank` is accepted.
  - **TLS certificate errors**: `certificateError` defers overridable
    errors and emits `certificateErrorRequested` → `_browser_cert_error`
    shows a Yes/No dialog (default: reject). Non-overridable errors are
    always rejected. Pending error objects are kept alive on
    `_pending_cert_errors` until the dialog resolves.
  - **JavaScript dialogs**: `javaScriptAlert`/`Confirm`/`Prompt` show
    native Qt dialogs (`QMessageBox`/`QInputDialog`) instead of silently
    no-op'ing. The dialog title is the page title or host, never empty.
  - **Permission requests**: `permissionRequest`/`featurePermissionRequest`
    → `_decide_permission`: sensitive capture (camera/mic/screen/mouselock)
    is denied without prompting; geolocation/notifications/clipboard/fonts
    get a Yes/No dialog (default: deny).
  - **Profile tuning**: normalized UA/language, persistent cookies, 100MB disk
    cache, spellcheck, scroll animation, back-forward cache, favicons, PDF,
    WebGL and accelerated canvas. DNS prefetch, local-content remote access and
    hyperlink auditing are OFF for privacy. Fresh configs enable the compact
    ad/tracker blocker; right-click its toolbar button for per-site exceptions
    and its tooltip reports the session block count.
    `Accept-Language` comes from `accept_language_header(QLocale.system().name())`
    (browser_bridge.py) — a Chrome-style list of VALID BCP 47 tags
    (`en-MO,en;q=0.9`, `pt-PT,pt;q=0.9,en;q=0.8`; fallback `en-US,en;q=0.9`).
    QtWebEngine sends no Accept-Language at all by default, and never build
    it from `locale.getlocale()`: on Windows that returns names like
    `English_Macao SAR`, and the resulting `English-MacaoSAR,...` header made
    Google answer EVERY search from the embedded browser (address bar or the
    website's search box) with its "unusual traffic" captcha — VERIFIED live
    2026-09-03 with a probe through the real `_BrowserPage.createWindow`
    path: malformed header → `/sorry/`, valid header → results, same IP,
    ad blocker on. The UA keeps only the `QtWebEngine/x.y` token stripped.
  - **Custom `deepflux://` scheme**: registered before the first profile with
    only Secure+Local flags; no local-file access, CORS, or CSP bypass. The
    built-in start page carries a strict CSP and `_display_url` hides its URL.
  - **QWebChannel** (`gui/browser_channel.py`): Qt's official resource
    `qwebchannel.js` and bootstrap are injected at DocumentCreation in
    ApplicationWorld. Persistent pages bind the channel in that same isolated
    world; private tabs never get it. The media detector also runs isolated and
    calls confirmed `sendDownload`/`sendPlay`; arbitrary page JS and iframes
    cannot access `window.deepflux`. The loopback ControlAPI requires a
    profile token on every request; single-instance and native-host clients
    load it from `~/.deeptorrent/control_api.token`. Native registration fails
    closed without one exact 32-character Chrome extension ID.
  - **H.264/AAC codecs**: stock PySide6 QtWebEngine lacks proprietary
    codecs → in-page `<video>` on most streaming sites won't decode. This
    is a build-time limitation (needs a custom Qt build with
    `-proprietary-codecs`); the app works around it by handing stream URLs
    to the embedded mpv (full FFmpeg codec support) via the extension's
    "Play in DeepFlux" action. Do NOT try to fix this in code.

## Commander tab (dual-pane file manager — 2026-10-29 XYplorer-inspired overhaul)
All shipped same day (UNCOMMITTED until release): gui/commander_tab.py,
gui/commander_rename.py, gui/commander_bridge.py. Durable rules — do not regress:
- SELECTION KEYS live in `_PaneView.keyPressEvent` (Insert toggle+advance, "+"/"-"
  group select/deselect, Ctrl+I invert, Ctrl+C/X/V commander clipboard,
  Ctrl+Shift+C copy path, printable-char incremental search with a status echo).
  They are deliberately NOT tab-level QShortcuts: a plain-character
  WidgetWithChildren shortcut fires while a QLineEdit child has focus and would
  hijack typing "+" into the path/filter boxes.
- QFileSystemModel race rules (all three verified live, all three were latent
  pre-existing bugs): (1) `index(path)` can return invalid for a just-created
  folder whose watcher event hasn't run — `_show_path_now` retries on a 40ms
  QTimer with a token guard (never `setRootPath(path)` to force it: swapping the
  model root re-fetches and can prune the node the view holds); (2) a hidden pane
  never lays out, so nothing calls fetchMore — `_show_path_now` calls
  `model.fetchMore(index)` explicitly; (3) navigating into a folder whose PARENT
  listing is in flight hands the view a node the model re-creates during
  reconciliation — `rowsRemoved`/`modelReset` are wired to `_heal_root_index`,
  which re-resolves by `_shown_path` (also the source of `current_path()`, so it
  reads the INTENDED folder during retries).
- A QTreeView root change does NOT clear the selection model: `_show_path_now`
  clears it explicitly. Without that, an old folder's selection survives
  invisibly and the next F5/F6 (or agent transfer) acts on paths the pane no
  longer shows (proven in review 2026-10-29).
- Folder tabs: each side is a QTabWidget of full FilePane widgets (one
  QFileSystemModel per tab — name/dir filters are model state), capped at 12,
  persisted in `ui_commander_tabs` (+ `ui_commander_paths` keeps the CURRENT
  tab of each side; the constructor re-selects it). Ctrl+T duplicates the
  current folder (classic behavior), Ctrl+W refuses the last tab, the "+"
  corner button adds. Only USER-driven tab switches (strip hasFocus) move the
  active-pane highlight; `_new_tab` activates its pane explicitly.
- Batch rename (Ctrl+R — NOT Ctrl+M, that's the global add-magnet shortcut,
  ambiguous-QShortcut hazard): `gui/commander_rename.py::plan_renames` is PURE
  (paths in, validated plan out — regex/case/numbering "###"/pasted-list
  modes); the dialog never enables Apply with a red row; the worker
  (`_batch_rename`) re-validates at execution time and rolls back completed
  renames in reverse on failure, naming EVERY stuck entry. Case-only renames
  are legitimate: planning skips them via normcase, but the worker compares
  abspath (case-sensitive) so they actually execute on Windows.
- Agent tools (the "scripting" answer to XYplorer): commander_state
  (READ_ONLY + UNTRUSTED_RESULT_TOOLS — filenames are external text),
  commander_navigate / commander_select (plain), commander_transfer
  (CONFIRMATION-gated). `gui/commander_bridge.py` keeps a plain-data snapshot
  (rebuilt on GUI signals, swapped atomically) and marshals GUI-thread calls
  via a queued signal + Event (15s). Review-driven invariants (2026-10-29,
  both reviewers PASS-WITH-FIXES → fixed):
  * commander_transfer REQUIRES an explicit `paths` array — the confirmation
    preview IS the real operand list — and re-validates every path: parent ==
    source pane's current folder, path exists, ≤500 items. Fail closed.
  * bridge.navigate is SYNCHRONOUS (_call_on_gui) and records no back/forward
    history: a fire-and-forget navigation raced a following transfer into the
    OLD destination (empirically proven).
  * `run_headless_transfer` releases `_operation_guard` synchronously in
    finally and records the outcome inline; the queued op_finished is GUI
    cosmetics only (the guard check in `_on_op_finished` tolerates that).
    Two transfers in one approved batch must both run.
  * `_OperationState.quiet=True` suppresses only the failure QMessageBox;
    agent ops DO get a lazily-created progress dialog in `_on_op_progress`
    (visibility + Cancel, wired to operation.cancel).
- i18n gotcha (zh catalog): menu labels with tab mnemonics must be written with
  a single-backslash `\t` ESCAPE in the catalog source. Writing entries via
  `repr()` produced double-backslash keys that never match the runtime (real
  tab) strings — tr() silently fell back to English. Diff tool: extract tr()
  literals with ast.literal_eval, compare against imported ZH_CN.

## Android app (since 2026-11-12, Phase 1 — standalone Kotlin/Compose)
- Owner decisions 2026-11-12 (recorded in ANDROID_STUDY.md): STANDALONE app
  for phones + tablets, Kotlin + Jetpack Compose, the shared DeepSeek key
  SHIPS in the app (same exposure stance as the Windows setup exe).
  Distribution lane undecided — working default: own-site APK via
  deepflux.space, Play optional later. Phase 0 (companion/ControlAPI LAN)
  deferred, not cancelled.
- Layout: `android/` Gradle project (like `website/` — shares only brand +
  concepts with the Python tree) + `.github/workflows/android-build.yml`
  (manual dispatch, debug + UNSIGNED release APKs, draft `android-staging`
  release lane mirroring linux-build) + `packaging/gen_android_key.py`.
  Study + roadmap: ANDROID_STUDY.md (tracked). Read android/README.md
  before touching the Gradle files.
- Toolchain on this dev machine (D:\android-toolchain, installed
  2026-11-12): JDK 21 (jdk-21.0.13.0.2+1), Gradle 8.14.3, SDK at
  D:\android-toolchain\sdk. Build: `cd android && JAVA_HOME="D:\android-
  toolchain\jdk\jdk-21.0.13.0.2+1" ./gradlew assembleDebug`. local.properties
  (gitignored) must carry `sdk.dir=D:/android-toolchain/sdk` — FORWARD
  SLASHES; the backslash-escaped form made AGP die with "filename, directory
  name, or volume label syntax is incorrect".
- Pinned stack & WHY: AGP 8.13.0 + Gradle 8.14.3 + Kotlin 2.4.21 + Compose
  BOM 2026.06.01 (Compose 1.11.4 / material3 1.4.0) + compileSdk/targetSdk
  36 + minSdk 28 (libtorrent4j floor). BOM ≥2026.08 maps to Compose 1.12
  whose AARs demand compileSdk 37 → AGP 9.1+ (the SDK 37 platform installs
  as dir android-37.0 which AGP 8.x can't even resolve). Bumping the BOM and
  the AGP 9 migration (built-in Kotlin) must happen TOGETHER as one change.
  Do NOT bump either casually.
- libtorrent4j 2.1.0-39 verified API facts (javap'd against the real jar —
  do not re-derive by guessing):
  * No `Sha1Hash(String)` — use `Sha1Hash.parseHex(hex)`.
  * SessionManager has NO per-hash pause/resume/remove — use
    `find(hash)?.pause() / ?.resume()`, and `remove(handle)` for remove.
    Session-level pause()/resume() DO exist (the wifi/charging gates use
    them).
  * No `setSequentialDownload` on TorrentHandle — closest is
    `setSequentialRange(n)`; whole-torrent sequential = pass the piece
    count (only knowable AFTER metadata — the repository retries per poll).
  * `download(magnet, saveDir, torrent_flags_t)` — the flags arg is
    or_'d onto defaults; passing an EMPTY `new torrent_flags_t()` keeps
    defaults, passing null NPEs inside the swig layer. There is NO 2-arg
    magnet overload.
  * TorrentStatus: NO paused() (pause is app-level state — the repository
    tracks a paused-set), error is `errorCode(): ErrorCode` (Java-style
    isError()/getMessage()), downloadRate()/uploadRate() return Int not
    Long. State enum has NO ALLOCATING/QUEUED_CHECKS.
  * Alerts: no torrentName() — name comes from
    `(alert as TorrentAlert<*>).handle().status().name()`; only
    TorrentErrorAlert has error().
- Key ritual: `python packaging/gen_android_key.py` (reads the repo-root
  _embedded_keys.py) or `--from-env` (CI secret EMBED_SHARED_DEEPSEEK_API_KEY,
  same secret as the Linux build) → writes gitignored
  `android/app/embedded_key.properties` (salt_hex/blob_hex, XOR-sha256
  scheme SHARED with the desktop by importing gen_embedded_keys.py by
  path). Overwrite guard DF_OVERWRITE_EMBEDDED_KEYS=1, values never
  printed. Verified end-to-end: desktop-decoder round-trip OK + the salt
  pair lands in the APK dex via BuildConfig (checked 2026-11-12).
- Gradle version-catalog gotcha: accessor aliases must not contain a
  segment starting with a DIGIT (`libtorrent4j-android-x86_64` →
  `libs.libtorrent4j.android.x86.64` is not parseable Kotlin) — hence the
  `x64` alias. Also: material-icons-core is NOT a transitive dep of
  material3 1.4.0 (Icons.Filled.* needs the explicit artifact).
- Design contracts: engine calls confined to a single-thread dispatcher
  (desktop queue/facade twin); magnet-first persistence
  (torrents_state.json, atomic temp+Files.move) re-fetches metadata via
  DHT after restart; FGS dataSync stops itself after 20 s idle (Android
  15's ~6 h cumulative dataSync cap makes phone seeding a non-goal);
  launcher icons regenerate from DeepFlux.png (recipe in android/README.md).
- NOT yet done (honest gaps): no unit tests, no on-device run, release APK
  UNSIGNED (keystore + deepflux.space lane is the next release ritual),
  agent chat/IPTV/Jackett-remote are Phase 2 (ANDROID_STUDY.md §6).

## Android on Google Play (since 2026-11-12 — owner account ex4n1m0)
- Play Console state: app DeepFlux (space.deepflux.android, internal
  numeric app id 4973165332904172092 — app URLs use THIS id, package-name
  URLs redirect home until the app is "entered" in the session). Internal
  testing track id 4700282969798098607. RELEASE 2 (0.1.0) PUBLISHED
  2026-11-12 (versionCode 1 is BURNED — a stuck synthetic upload claimed
  it; every future release bumps versionCode). Opt-in link:
  https://play.google.com/apps/internaltest/4700282969798098607
  (email-list alternative exists; its chip input ignores synthetic Enter —
  use the join link, or a real human types the list).
- Setup state: ALL 11 store tasks green (listing copy + icon/feature
  graphic live on lh3.googleusercontent.com; category Tools; contact
  igorcarlier / deepflux.space; ads/data-safety/target-audience/content
  rating (Everyone/PEGI 3/USK 16+) all declared; privacy policy DEPLOYED
  at deepflux.space/deepflux/privacy.html — content must stay truthful:
  no analytics/no ads/no collection, BitTorrent peers see your IP,
  optional user AI key goes only to the chosen provider).
- REMAINING owner items: Google's one-time new-app review must clear
  before testers can install ("Not reviewed" status; temporary app name
  'space.deepflux.android (unreviewed)' until then); trader-status
  declaration by Feb 2027 (blocks publishing eventually); production
  access needs closed testing with 12+ testers for 14+ days (dashboard
  states 12, not the generic 20).
- VERIFIED console-automation facts (do not re-derive):
  * App-content declaration pages: drive via Dashboard task buttons
    (View tasks for Provide app information...) or the app-content URLs;
    each form ends in "Change saved. Send for review in Publishing
    overview".
  * Material radio groups: YES and NO live in SEPARATE sibling
    material-radio-group elements with the SAME aria-label — scope by
    label text content, click the native input inside.
  * Upload drop-zones (graphics): a fan-out of dragenter/dragover/drop
    DragEvents with a DataTransfer holding a File fetched from a local
    CORS server (http://127.0.0.1:8765) onto the zone AND all its
    descendants works (icon + feature graphic landed on Google's CDN).
    The same technique does NOT stream large files — the 26.8 MB AAB sat
    at 0/100 forever. Large artifacts need the REAL file picker: click
    Upload in EDGE (ZCode IAB cannot open file choosers) and drive the
    Windows #32770 dialog (File-name edit + Open button).
  * Release notes MUST wrap each language with tags ON THEIR OWN LINES:
    "\n<en-US>\nnotes text\n</en-US>\n" — single-line tags fail with
    "tag not closed", and keystroke/fill paths STRIP the angle brackets
    (sanitizer); the working path is a real clipboard paste (Windows
    Set-Clipboard via a temp file, then Ctrl+V in Edge).
  * Play's survey iframe (google-hats-survey) re-injects on every page
    and covers buttons — remove it (IAB evaluate) or it blocks clicks.
  * Publishing overview is where declarations + releases are finally
    "sent for review".
- EMULATOR DEMO SESSION 2026-11-12 (FGS video for Play) — two REAL bugs found
  and fixed, plus durable environment facts:
  * TorrentRepository.addMagnet raced: two rapid calls (VIEW intent redelivered
    on activity recreation) both passed the dedupe check → duplicate entries →
    LazyColumn duplicate-key CRASH on launch (persisted state poisoned!).
    Fix: dedupe check + entries.add moved INSIDE the single-threaded
    engineDispatcher; DownloadsScreen key now "${infoHash}-${addedAt}".
  * libtorrent mmap on Android's scoped-storage FUSE crashes the system
    MediaProvider (NodeTracker abort, "emulated;0" thread) on huge sparse
    files — TWICE, even with mmap_file_size_cutoff semantics double-checked
    (cutoff = files SMALLER are mapped; 0 did NOT disable; now set to 1) and
    the posix path ALSO tripping it on the API-36 EMULATOR image. Release
    builds keep external storage; DEBUG builds save to filesDir (no FUSE) —
    see TorrentRepository.saveDir. Real devices may not share the emulator's
    fuse-daemon bug, but watch for it.
  * Emulator ritual that worked: system-images;android-36;default;x86_64 +
  hand-written AVD config (avdmanager.bat rejected valid paths; the launcher
  also misreads "arm" without abi.type=x86_64 in config.ini) → boot ~15 s.
  screenrecord must run as its OWN background task and be stopped with
  `pkill -INT screenrecord` (killing adb/shell loses the moov atom).
  Emulator FUSE crashes wedge /sdcard ("Transport endpoint not connected")
  AND corrupt window focus (launcher stuck on top) — restart the emulator,
  don't fight it. Verify video takes by per-act frame luminance + the
  analyze_image vision tool (app dark theme ≈ luminance 9-12, shade ≈ 69,
  launcher ≈ 185).

## Android app v0.8 (2026-11-13) — 5 milestones in one day
- Full feature set now SHIPPED and on the emulator (versionCode 5, v0.8.0):
  * v0.2: stream-while-downloading (ExoPlayer + NanoHTTPD loopback server
    with tail-following InputStream for growing files). Owner-verified.
  * v0.3: AI agent chat (DeepSeek SSE, reasoning dim, embedded key).
  * v0.4: agent TOOLS (add_torrent, list_downloads) via OpenAI function
    calling; tool execution loop up to 4 rounds; tool schemas sent with
    each request. search_indexers added in v0.4b (Jackett remote via
    torznab XML endpoint — QUICK TIER: eztv|1337x|thepiratebay, NOT "all"
    which times out on 96 indexers).
  * v0.5: IPTV/Live TV — M3U parser, playlist browser, HLS playback in
    PlayerActivity (media3 handles HLS natively). 4th tab "Live TV".
  * v0.6: chat persistence (DataStore, last 30 bubbles) + Stop button
    mid-generation + web_search tool (DuckDuckGo HTML, keyless).
  * v0.7: notification tap opens app (unique PendingIntent request code,
    NEW_TASK|SINGLE_TOP); SAF save-dir setting infra (not yet wired to
    engine — getExternalFilesDir still used; SAF wiring is the next
    engine change).
- Agent tool count: 5 (search_indexers, web_search, add_torrent,
  list_downloads + implicit system prompt guidance).
- Jackett quick-tier VERIFIED: eztv responds in ~2s, 1337x in ~3s;
  "all" times out (>30s across 96 indexers, many dead/CF-walled).
  adb reverse tcp:9117 tcp:9117 maps emulator localhost to host Jackett.
- Chat persistence: DataStore stringPreferencesKey("chat_history_json"),
  JSONArray of {role, content, reasoning}, max 30 bubbles, loaded on
  ChatSession init in a daemon thread. Persisted after each assistant
  reply (not during streaming).
- Web search: DuckDuckGo HTML parsing (regex, no JSoup), results
  deduped by URL, uddg redirect unwrapping, HTML entity stripping.
  Keyless (same as desktop's ddgs DuckDuckGo provider).
- Release AAB v5 (0.8.0) built and signed (jarsigner verified).
  29.3 MB — bigger than vc4 (26.8 MB) due to media3 player + IPTV.

## Android app v1.0 (2026-11-13) — FULL FEATURE SET
- v1.0.0, versionCode 6. Release AAB 29.3 MB, jarsigner verified.
- New in v0.9-v1.0 (on top of v0.2-v0.8):
  * EPG: XMLTV parser (iptv/EPGParser.kt), EPGRepository with disk cache,
    "now playing" line on each IPTV channel row (blue accent, times).
  * Xtream Codes API: full client (iptv/XtreamClient.kt) — authenticate,
    getLiveChannels, getVodChannels, getCategories, toM3UChannels.
    IPTV tab now has a 2-mode source config: M3U URL or Xtream login
    (server URL + username + password → live channels injected).
  * MediaStore export: finished torrent video files auto-copied to the
    public Downloads collection (dlmgr/MediaStoreExport.kt) so users
    see them in any file manager.
  * Agent deep-search: system prompt updated to combine search_indexers
    AND web_search for broad requests.
- Full feature inventory (v1.0):
  * Torrents: libtorrent engine, magnets, .torrent, pause/resume/remove,
    sequential stream-first, background FGS with notification, Wi-Fi/
    charging gates, persistence across restarts, MediaStore export.
  * Streaming: ExoPlayer + NanoHTTPD loopback server, plays while
    downloading, Range request support, growing-file tail-following.
  * IPTV: M3U playlists + Xtream Codes login, channel browser with EPG
    "now playing" (XMLTV), HLS playback in the player screen.
  * Agent: DeepSeek chat with SSE streaming, reasoning dim, 6 tools
    (search_indexers, web_search, add_torrent, list_downloads, + system
    guidance for deep-search combining both search tools), chat
    persistence (last 30 bubbles), Stop button, custom key override.
  * UI: 4 tabs (Downloads, Agent, Live TV, Settings), adaptive phone/
    tablet layout, dark brand theme matching the desktop.

## Android v1.1 (2026-11-13 evening) — agent torrent control + i18n + SAF
- versionCode 7, v1.1.0, release AAB 29.3 MB signed.
- NEW agent tools (9 total): pause_torrent, resume_torrent, remove_torrent —
  the agent can now fully manage downloads from chat (find by partial name
  or info-hash). Combined with search_indexers + web_search + add_torrent +
  list_downloads, the full desktop agent loop runs on Android.
- zh-CN string catalog (ui/ZhStrings.kt) — ~70 strings covering all tabs,
  notifications, and dialogs. Language toggle in Settings (English/中文);
  strings are ready to wire into screens via Strings.isChinese.
- SAF folder picker (ACTION_OPEN_DOCUMENT_TREE) in Settings → "Save location"
  card — opens the system folder picker; result pending wiring to
  engine save dir (the infrastructure exists, engine still uses
  getExternalFilesDir by default).
- Tablet 16:9 verified on 1920×1080 landscape AVD — NavigationRail
  (left side) correctly replaces the phone bottom bar.

## Android v1.2 (2026-11-13 late) — zh-CN live, SAF picker, two-pane tablet
- versionCode 8, v1.2.0, release AAB signed.
- zh-CN STRINGS WIRED INTO SCREENS: DownloadsScreen (all labels, states,
  empty state, buttons), AppRoot (tab names), ChatScreen (placeholder,
  empty state). Language toggle in Settings swaps Strings.isChinese.
- SAF FOLDER PICKER END-TO-END: Settings → "Choose folder" →
  ActivityResultContracts.OpenDocumentTree → takePersistableUriPermission →
  repository.setCustomSaveDir. The engine still uses getExternalFilesDir
  by default; the SAF URI is stored and ready for the DocumentFile
  engine integration (next step).
- TWO-PANE TABLET DOWNLOADS: on wide screens (isWide=true via WindowSizeClass),
  the Downloads tab shows a compact list on the left + a detail pane on the
  right (name, progress bar, speeds, sizes, peers, Play FAB). Tap a torrent
  in the list to select it. Phone layout unchanged.

## Android agent guidelines review (2026-11-16, owner directives)
- ALWAYS-YOLO (owner: "make it yolo mode always"): every agent tool call
  executes immediately — the Android chat has NO confirmation UI and none
  may be added. The system prompt (ChatSession.systemPrompt) opens with the
  desktop's UNATTENDED MODE paragraph (the desktop YOLO lesson: without it
  models narrate waiting for approvals that never come) — keep it in sync
  with agent/loop.py's yolo block.
- NO LIMITS (owner: "don't add limits"): the 4-round tool-loop cap is
  REMOVED (was `while (rounds < 4)`; the `tooManyRounds` string deleted
  from all three string files). The loop runs until the model returns final
  content or the user presses Stop — the Stop button is the only bound. Do
  NOT re-add round/turn caps (mirrors the desktop's 2026-10-20 wall-clock
  cap removal). Output-size coercions (RSS items ≤50, IPTV list ≤100) are
  display caps, not agent limits — they stay.
- NO JACKETT on Android (owner 2026-11-15): search_indexers is WEB-ONLY
  (WebTorrentSearch: DuckDuckGo + known sites). Guidelines must never
  mention/suggest Jackett, private trackers, search categories or seed
  counts — web results carry none; the prompt tells the model to write "—"
  instead of inventing numbers. Leftover `setJackettUrl/ApiKey` setters in
  AppSettings are intentional no-op tombstones for old settings imports.
- Guidelines ported from the desktop SYSTEM_PROMPT: reply in the user's
  language, short intro on greeting, batch independent tool calls in one
  turn, search-path choice (vague query → web_search first to pin the
  title, then search_indexers; specific title → straight to search_indexers),
  DEEP REQUESTS ("deep" = keep sweeping query variants until exhausted),
  numbered results + "type the number" selection (a bare number reply =
  download that choice), today's date injected (rebuilt every send).
- Bug fixed in the review: find_alt_trackers' host label used
  substringBefore('/') which stops at the scheme slash and printed
  "[web · https:]" — now uses hostLabel() (URL-parsed) like
  torrentSearchTool.
- Known gaps noted, NOT changed (roadmap material, owner call): no LLM
  retry/backoff (desktop has 3 attempts); in-session `history` is never
  pruned/fitted (desktop prunes + caps tokens) — a very long single
  session can outgrow the model context and start erroring; chat history
  is display-only across restarts (memory files are the cross-session
  context).

## Android player top-bar auto-hide (v1.4.11, owner 2026-11-16)
- The PlayerActivity title bar (✕ + stream title + CC) AUTO-HIDES 10s
  (TOP_BAR_AUTOHIDE_MS) after playback actually runs and ANY touch
  (dispatchTouchEvent ACTION_DOWN) brings it back and restarts the timer.
  Gate = STATE_READY && playWhenReady (a rebuffer stall must not flicker
  the chrome); paused/connecting/errored keeps the bar PINNED — the
  2026-11-13 "always a visible way out" rule survives as: while playing,
  the way out is one tap away. Do not regress the paused-pinned behavior.
- Emulator test gotchas hit (2026-11-16): `-no-boot-animate` is NOT a
  valid flag on emulator 37.x (use none — `-no-snapshot-save` +
  `-gpu swiftshader_indirect` work); PlayerActivity is exported=false —
  `adb root` first, then `am start` with the whole command QUOTED through
  `adb shell` (unquoted `--es title 'Two Words'` parses "Test" as pkg);
  uiautomator dump does NOT expose contentDescription on these plain
  TextViews — probe the bar by its title TEXT, and run timed hide/show
  assertions inside ONE adb command (inter-command latency exceeds the
  10s window and produces false "still hidden" readings).

## Android parity + review rounds (2026-11-23/24) — durable rules (1.4.13, RELEASED 2026-11-24)

STATE (2026-11-26): RELEASED. Android 1.4.14 / versionCode 30 is LIVE —
the test build byte for byte (58359944 bytes, sha256
41251d18ac73a0b6bd3d8d817a8d8d00d91cb300e15b14d99d2f0df2aae29588):
both pages (EN + ZH) and `latest-android.json` point at it, the 1.4.13
APK is gone from the deploy dir, 23 live checks passed (feed equal to
the local file and valid under `AppUpdater.parseFeed` / `isNewer`, both
pages, APK downloaded back — hash and size, 1.4.13 at 404, the counter
route, the Windows 9.8 / Linux 7.8 / yt-dlp feeds untouched). Released
on the owner's word ("do what is missing to do for the android version",
then "Release 1.4.14" ticked with the gate stated). It has STILL never
run on a physical phone. Branch `android-1.4.13` and
`svp-out-of-process` are both pushed and equal (every commit of that
day went to both). Rollback: the 1.4.13 APK is in
`%LOCALAPPDATA%\Temp\df_release_backup\`. Before the release, on the
emulator, against this exact build (sha compared on the device):
 * A torrent restored UNFINISHED and then finishing — the DOWNLOADING-
   state rule had only ever run through unit-test fakes. Twice (51.8 %
   and 24 % of a 21.5 MB hybrid torrent; app stopped; seeder back
   BEFORE the app starts): announced once, copied once, mark saved, same
   pid start to finish. `onFinished` posts only for a hash in
   `unfinishedSeen`, and nothing but `watchDownloading` adds a restored
   one — so the real engine DOES report `TorrentState.DOWNLOADING`.
   Quiet restart with nobody seeding: the row is back "Seeding · 100 %"
   with Play, no re-post, no second copy. Only libtorrent's literal
   DOWNLOADING counts; a torrent waiting for peers (`stalled_download`)
   reads UNKNOWN until data flows — consistent with the narrow gap.
 * Android 9 (API 28 = minSdk; AOSP x86_64 image, AVD `api28`): the app
   installs, starts and runs there with no crash (first run, the
   browser's WebView, the downloads list, settings, the system folder
   picker and its persisted permission). No folder chosen: a finished
   download is "Download finished" — NOT a copy failure — and stays
   inside the app (`Result.NoDestination`, as designed; the Settings
   card tells those users so). Folder chosen through the real API 28
   picker: "copied … into the chosen folder".
 * The in-app update 1.4.13 -> 1.4.14: the published 1.4.13 offers
   "Update available — v1.4.14" by itself at start, downloads and
   verifies the 58 MB from the site in under 8 s, then the OLD consent
   order (system dialog -> SETTINGS -> "Allow from this source" -> back
   -> tap "Update now" again) -> the system installer -> "App
   installed." -> OPEN runs 1.4.14 (installed sha = published sha) with
   settings kept. The only exit record: `reason 16 PACKAGE UPDATED`.
 * CI: `android-build.yml` now runs the unit tests on GitHub, on Linux,
   at every push that touches the app (the APK build stays manual
   dispatch — and had NEVER run before 2026-11-26). Green: 423 tests,
   0 failures, 22 skipped — the five that could not run on Windows run
   there and pass; the 22 need an agent key and skip because the
   repository secret `EMBED_SHARED_VENICE_API_KEY` DOES NOT EXIST (the
   workflow has named it since 2026-11-17; the owner has to create it).
   What the first runs said is in TOOLING LESSONS (t)–(z) below.

STATE (2026-11-25): Android 1.4.14 / versionCode 30 went on the site as
an UNLINKED TEST BUILD (released 2026-11-26 — the STATE above). Owner,
then, to the test build: "keep polishing what you find that needs
polish … deploy to the website, I will test on a real phone"):
`website/deepflux/DeepFlux-1.4.14-Android.apk` (58359944 bytes, sha256
41251d18ac73a0b6bd3d8d817a8d8d00d91cb300e15b14d99d2f0df2aae29588, same signing cert, installs over 1.4.13). Page buttons and
`latest-android.json` stayed on 1.4.13 until the owner's word (given
2026-11-26). The release ritual:
swap the 4 page hrefs + the version/size/sha lines (EN + ZH), write the
feed from the file in the deploy dir, delete the 1.4.13 APK there,
`vercel --prod --yes --scope timedivision`, verify live. Unit suite
423 tests / 0 failures / 5 Windows-host skips; 119 mutations
each fail their own test against this code; two independent review
passes (correctness + security), a re-review of the final diff and a review
of the last round's changes — every finding fixed or written down, and
every fix run on the emulator. The LAST round came from the emulator, not
a review: a hybrid torrent added from a magnet in the v1 form (how sites
link torrents) was not recognised at a start — its kept file deleted
every time (see IDENTITY below); three rounds had passed with magnets in
the v2 form only. Fixed, and both magnet forms run on the emulator.
A last pair of independent reviews (correctness; security and
reliability) of all of it found no blocker: kept files the first test
build had left under another hash (adopted at a start now), an update
message that reached no one, a double tap that stacked two installer
screens, an uncapped list of peer addresses, copy marks that could
outlive their entry — each fixed with its test; the rest written down
("KNOWN GAPS IN 1.4.14" below). Then the system's exit records on the
emulator showed one SIGSEGV that no check had caught — a torrent's finish
read the finished alert's handle after the callback (fixed: see NO ALERT
OUTLIVES ITS CALLBACK).
("1.4.14" rules below). Code on branch `android-1.4.13` — pushed
2026-11-26, together with `svp-out-of-process` fast-forwarded to it.

STATE (2026-11-24, evening): RELEASED. Android 1.4.13 / versionCode 29
is LIVE (owner: "release", told about the two gaps below): both pages (EN
+ ZH) and `latest-android.json` point at
`website/deepflux/DeepFlux-1.4.13-Android.apk` (58325596 bytes, sha256
ae44530c10f1da51cac255b2251de83688bc7d2c3173316e0d598381d9cb3b42, same
signing cert as 1.4.12, so it installs over it), the 1.4.12 APK is gone
from the deploy dir (404), and 19 live checks passed (feed field for
field, both pages, APK downloaded back, counter route, Windows feed
untouched). The code is COMMITTED on branch `android-1.4.13` (two
commits ahead of `svp-out-of-process` at the time; both pushed and equal
since 2026-11-26).
THIS RELEASE WENT OUT WITH TWO GAPS: (1) it had not been run on a device
or an emulator when it shipped — CLOSED the same evening on the emulator
that now exists on this machine (Android 15 x86_64; see "Verified on the
emulator" at the end of this section: the published APK, zero crashes,
four small findings for 1.4.14); (2) STILL OPEN: 5 unit tests skip on a
Windows host (symlink privilege x4, the real FileProvider round trip)
and NO CI job runs the Android unit tests at all (`android-build.yml`
only builds) — CLOSED 2026-11-26: they run on GitHub, on Linux, at
every push, and those five run there (STATE 2026-11-26). Baseline
before this work was 35 tests; `./gradlew :app:testDebugUnitTest` →
330 tests, 0 failures, 5 skips on Windows. `assembleDebug` builds. A rollback copy of the 1.4.12
APK was left in `%LOCALAPPDATA%\Temp\df_release_backup\` (deletable).

Toolchain on THIS machine (user `igorc`): portable JDK 21 + SDK under
`C:\Users\igorc\android-toolchain` (the `D:\android-toolchain` notes above
belong to the other machine). Helper: `bash
/c/Users/igorc/android-toolchain/scripts/adb-build.sh compile|test
["pkg.Class"]|assemble`.

EMULATOR on this machine (since 2026-11-24 evening — the owner pointed
out that ZCode ships an Android-emulator plugin; it could not see this
SDK and the packages were missing): `emulator` (37.3.10) and
`system-images;android-35;default;x86_64` live in the same SDK; the AVD
is `medium_phone` (pixel_6 profile — the plugin's default name and
image); acceleration is WHPX (`emulator -accel-check`: "installed and
usable"; a hypervisor already runs on this Windows). Start it headless
from Git Bash:
`SDK=C:/Users/igorc/android-toolchain/sdk;
"$SDK/emulator/emulator.exe" -avd medium_phone -no-window -no-audio
-no-boot-anim -no-snapshot -gpu swiftshader -memory 4096 -cores 4
-netspeed full -netdelay none` — booted in ~45 s
(`adb -e shell getprop sys.boot_completed` → 1); stop it with
`adb -e emu kill`. Drive it with the SDK's adb (the Bash tool allows an
installed-SDK adb): `adb -e install -r <apk>`, `adb -e shell am start -W
-n space.deepflux.android/.MainActivity`, `adb -e exec-out screencap -p
> shot.png` (works headless; Read the PNG), `adb -e shell uiautomator
dump /sdcard/ui.xml` + `adb -e pull /sdcard/ui.xml` for the element tree,
`adb -e shell input tap|text|keyevent`; `adb root` works on this image.
TRAPS: Git Bash rewrites device paths (`/sdcard/x` becomes a Windows
path) — `export MSYS_NO_PATHCONV=1` first; an `&` in `adb shell input
text` must be `\&`; Compose labels only the SELECTED bottom tab (switch
tabs by coordinates); a dialog title can carry a button's exact label
(tap buttons by coordinates); exact-label matches must outrank
substrings. The plugin's own MCP tools (screenshot, ui_describe, tap)
stay unusable until its `sdk_path` option points at this SDK (plugin
settings, or `~/.zcode/cli/config.json` →
plugins.options["android-emulator@zcode-plugins-official"]; jdk_major
21). NEVER `adb shell settings put global http_proxy …` to intercept
traffic: clearing it leaves running apps proxied (the WebView kept it
across an app restart) — reset with `settings put global http_proxy
':0'`, delete every `*proxy*` global setting, then force-stop the app.
The emulator reaches this PC as 10.0.2.2 (the PC's loopback): serve
test files from a loopback listener, or seed a torrent from one. A
torrent-lane seeder must NOT flag its torrent private (a libtorrent
seed then does not advertise `ut_metadata`, so a magnet stays on
"Fetching info" forever); hand the peer over in the magnet itself
(`&x.pe=10.0.2.2:<port>`).

Seams the tests rely on (keep them; nothing else lets the logic run on the
JVM): `TorrentEngineApi` (+ `hasSymlinkEntries`), `RecordingContext`,
`TinyHttpServer`, `ChatSession(clientFor, publicUrlCheck, isSymlink,
openInBrowser: (String) -> Boolean)`, `TorrentRepository(gateOverride,
clock, engineStartRetryMs, readStagedTorrent, activeOverride)`,
`FolderSizer(isLink, cancelled)` — `isLink` sits BEFORE `cancelled` on
purpose: a function-typed parameter added AFTER a trailing-lambda parameter
silently rebinds every `sizeOf(dir) { … }` call site —
`CommanderRepository(appFileUri, isLink)`,
`HttpDownloadRepository(urlPolicy)`, `RssRepository(feedUrlPolicy)`,
`EPGRepository(clock)`, `FinishedExport.export(…, access, run,
`fallbackToDownloads)` + `keepAccess(take)` + `stallMs`,
`TorrentRepository.exportTorrentFile(…, run)` and the synchronous
`exportFinishedFiles(downloadFolder, paths, copyOne)`,
`HttpDownloadRepository(exportFolder, notify, onCopyFailed)`.

Durable rules — do not regress:
- FOREGROUND SERVICE follows ACTIVITY, never app launch: the gate's first
  look at startup is not a transition; `refreshNow()` starts the service on
  the false→true edge of "any torrent active" (restored torrents included),
  the HTTP lane calls `checkActive()` at the end of `scheduleLocked()`
  (callers persist BEFORE scheduling), `MainActivity.onStart` checks both
  lanes. The HTTP lane is nudged (`onGateOpened`) on EVERY poll while the
  gate is open — its workers sample the gate per chunk and can pause an
  item during a blip the poller never sees; transition-only nudges stranded
  such downloads (and stranded the EPG loader the same way).
- `TorrentRepository.persist()` is serialised (`persistLock` — two writers
  shared one tmp file and the older, shorter list could win) and waits for
  the saved list; `start()` loads the list BEFORE starting the engine and
  retries the engine start; an add that arrives meanwhile WAITS for it.
- Torrent pause state (`p`, `pa`) is saved per entry and re-applied at start.
- Add confirmation: `userInitiated` (tapped link = `request.hasGesture()`,
  links sheet, another app's share) is never displaced or held back; a
  page-fired request is (a) dropped while a user request is showing or on
  its way (`openUserRequests`), (b) dropped for a FIXED 5 s after the user
  declined that same torrent (per torrent, not restarted by the asks it
  swallows), (c) capped at 3 dialogs per minute (`pageIsFlooding`, one
  notice per window) — the dialog is modal, so a volley is a lock-out.
  `offer()` is one step under `pendingAddLock`; `.torrent` requests carry
  the sequence of their ARRIVAL, not of their finished read. One share
  import at a time; the copy is capped; staged files are swept at startup.
- SYMBOLIC LINKS: a torrent may carry link entries pointing anywhere in
  private storage. The engine refuses such torrents (`.torrent` at add,
  magnet when its metadata arrives → `removeSymlinkTorrent`), the Commander
  does not list links and `deleteTree` never follows one (Kotlin's
  `deleteRecursively()` walks THROUGH a linked folder), the public-Downloads
  export skips links, the HTTP lane removes a link where it is about to
  write (`.part`).
- NETWORK BOUNDARY (agent-added URLs): `PublicUrls.rejection` on EVERY
  redirect hop — plain HTTP downloads, HLS sub-requests, RSS feed address
  AND item links (agent-added feeds are `publicOnly`, persisted), the
  updater, `fetchPublic`. `open_url` checks its first URL and the tab it
  opens is `agentTabs`-marked: non-public main-frame requests (redirect,
  meta refresh, script) are refused until the user navigates by hand.
  OWNER DECISION 2026-11-24 ("yes, yes" to both — do not change): (a)
  `scan_page` MAY read a LAN page the USER opened (only agent-opened tabs
  are blocked); (b) page-initiated plain-file downloads stay
  UNCONFIRMED, rate-capped per tab (5/min) — no confirmation dialog.
- PAGE BRIDGE (`BridgeMailbox`): in-page .torrent fetches wait per TOKEN; a
  foreign token consumes nothing; `claimScan` makes a scan token good for
  ONE report; tokens are read by string positions (`envelopeToken`) before
  any JSON parsing, and a payload is parsed on the bridge thread, never on
  main. Every tab has its OWN download listener (a shared one sent the
  ACTIVE tab's URL as Referer for another tab's download).
- AGENT TOOL RESULTS: every result is wrapped `<untrusted>…</untrusted>`
  (one wrap for all — no list of "safe" tools) with the tag's own spellings
  neutralised, capped at 24k chars; `list_files`/`open_file` stay inside
  the download folder (canonical + separator; no link following). `open_url`
  reports failure when the browser is at its tab limit.
- EPG: the cache file is named per guide URL (3 kept), the guide is dropped
  the moment the URL changes, a superseded fetch can neither install nor
  cache (generation + `installLock`), each load has its own tmp, EVERY exit
  of `run()` goes through `settle()`, `OutOfMemoryError` is a failure not a
  death, and the parser keeps only programmes within 12 h past / 7 days
  ahead of now (≤400k, texts ≤400 chars) — a guide served to tests must
  therefore be built around the real clock (`guideAround(now)`).
- NOTICES: "Resume all" is authorised by a per-INSTALL token kept in
  `filesDir/notification_token` (not on the backup allow-list) — the
  standing "paused" notice is a normal notification and OUTLIVES its
  process, so a per-process token left its button dead after a kill.
  `syncPausedNotice()` runs from the service, MainActivity.onStart and
  `TorrentRepository.onPauseAllChanged`.
- SERVICE LOCKS: `TransferLockPolicy` holds the wake/Wi-Fi locks only while
  bytes move with the gate open (60 s stall window, elapsed-realtime
  clock); the watchdog loop is exception-guarded; a page cycling magnets
  gets the crash-loop guard (`CrashWindow`, 3 in 30 s).
- CHAT: one `Turn` per turn (own stop flag, own history list, generation);
  every bubble-list change goes through `updateMessages`/`streamPiece`
  under `messagesLock` (a `clear()` between check and write used to
  resurrect a "…" bubble); `Thread.start()` failure clears `_busy`; bubbles
  have ids (`ChatBubble.id`) and UI state follows the id. Agent replies
  render as markdown (`MarkdownText`); a magnet tapped in a reply is a
  user-initiated confirmed add, a page link opens in the in-app browser.
  The agent's `add_rss_feed` is `publicOnly = true`.
- IPTV: `Sections` (Live TV / Movies / Series) are computed on the load
  thread and published as a parallel list in `IPTVState.sections`;
  `PlaylistStore.Playlist.contentType` ("auto"|"live"|"vod", field `ct`) is
  the per-source override; local sources are VOD by nature. Section, group
  and search filters run off the main thread (`produceState` + 180 ms
  debounce) — never walk a 75k-entry list while composing.
- PLAYER: audio focus + becoming-noisy handled; buffering spinner and text;
  edge-to-edge under the cutout; rotation / aspect / audio-track buttons in
  the top bar (`PlayerModes` holds the cycles and the labels).
- SAVE LOCATION (Settings → "Save location", owner 2026-11-24: "make it
  work" — the picked folder used to be stored and never read):
  `dlmgr/FinishedExport.kt`. Downloads always run in app-private storage
  (libtorrent cannot write through scoped storage); when one finishes a
  COPY is made for the user — into the chosen folder (a SAF tree URI,
  through `DocumentsContract`) or, when none is chosen, into the public
  Downloads collection (`MediaStoreExport`). FOUR review passes (two
  rounds x correctness + security, same day); durable rules — do not
  regress:
  * Plain HTTP downloads: every finished file. Torrents
    (`exportFinishedFiles(downloadFolder, paths, copyOne)` →
    `exportTorrentFile` per file): EVERY file into a chosen folder,
    keeping the torrent's own sub-folders; with none chosen VIDEO files
    only (`.mp4 .mkv .webm .avi .mov .iso` — the pre-existing rule, so a
    torrent of thousands of small files does not flood Downloads).
  * The torrent lane settles on its destination ONCE per torrent
    (`effectiveFolder`: the chosen folder while the app may write to it,
    else "") and calls `export(…, access = { true },
    fallbackToDownloads = false)` — it is never re-routed behind its
    back, and the permission is not asked of the system per file. The
    plain lane passes the stored folder and lets `export` decide.
  * An unusable chosen folder — permission revoked, provider gone, not an
    address — COUNTS AS NONE for `export`: Downloads, never "copied
    nowhere" (and for torrents the video-only rule comes back with it).
    The Settings row says so (`saveLocationLost`, worded true on every
    Android version); on Android 9 (no Downloads collection) the default
    row says nothing is copied out (`saveLocationNoDownloads`) and
    `Result.NoDestination` is not a failure.
  * `canWriteTo` = a PERSISTED write grant on the tree URI.
    `keepAccess` (= `takePersistableUriPermission` READ|WRITE, guarded)
    runs when the user picks a folder; when the system refuses,
    `pickedFinishedDownloadsFolder(uri, kept = false)` stores NOTHING,
    the earlier choice stands and the user gets `noticeFolderNotKept`.
    The old grants are deliberately NOT released on change/clear (the
    Commander and the IPTV local folder may share that tree; the per-app
    cap is 512 since Android 11).
  * Inside the chosen folder: only real path segments ("."/".." dropped);
    same name + same size → `AlreadyThere`; a same-name DIFFERENT file is
    never overwritten; a FILE where a folder should go fails with "in the
    way" (asked at the first look AND after the create request); a folder
    another export just made is looked up again (`folderId`) and, when
    the provider does not list it yet, remembered.
  * ONLY a file the provider has just made is written to: after
    `createDocument` the returned document is asked about ITSELF
    (`readDocument`: a directory, ANY size above zero, or no answer means
    "that is not a new file") and compared with the listing — a provider
    that answers a create with a file that is already there hands over
    the user's file, which is neither opened nor deleted. The job's
    listing memory can be hours old; the provider's answer cannot.
  * A failed copy DELETES its half-written document (only ever its own);
    a failed Downloads copy deletes its "pending" row.
  * The type a file is created with comes from its own extension through
    `MimeTypeMap` first (`mimeFor`): a system folder provider renames
    "notes.pdf" to "notes.pdf.bin" when asked for
    "application/octet-stream".
  * A job (`Run`) lists each folder ONCE (it was once per file —
    quadratic for a flat multi-file torrent); child queries use the
    BUNDLE form and columns are read BY NAME (traps below).
  * Before any copy, a torrent file must be a real file INSIDE the
    download folder (`exportTorrentFile`: symlink refused, canonical
    containment) — the second lock behind the add-time refusal. The file
    list is read on `engineDispatcher` (the libtorrent contract).
  * A copy may never take the app down or hold the app up: both lanes
    run on `FinishedExport.submit` (a pool of 2; a job may not throw);
    `busy()` is true only while a job is queued or running AND has
    written a byte inside `EXPORT_STALL_MS` (5 min) — a folder provider
    that hangs stops counting, so the service's idle grace and the CPU
    lock let go (the stuck threads stay pinned: accepted residual).
  * The download service's idle grace WAITS while `busy()`, and
    `TransferLockPolicy` holds the CPU for a progressing copy with the
    gate open or closed (both locks are taken together — a few minutes
    of Wi-Fi lock for a local copy is accepted), giving it back after the
    same 60 s stall window. (The two service call sites and the wait loop
    are verified by READING only — no JVM test builds the service.)
  * A failed copy is TOLD twice: a snackbar notice
    (`noticeExportFailedFmt` / `…CountFmt`, once per torrent — a copy
    that blows up counts as failed) AND a durable system notification
    `EventKind.COPY_FAILED` ("Not copied to your folder") through
    `postEventNotification` — the torrent lane directly, the plain lane
    via `HttpDownloadRepository.onCopyFailed` (wired in `DeepFluxApp`).
    A snackbar alone is seen only while the app is open.
  * `finishedDownloadFolder()` is the non-coroutine read for the HTTP
    lane (the settings flow emits only once the stored values are in) and
    is total (falls back to "" on any failure).
  * Settings row: `remember(folderUri)` around the permission question
    (a call to another process, not something to do per recomposition).
  * Tests: `FinishedExportTest.kt` (42; a REAL
    `DocumentsProvider` over a temp folder, registered with Robolectric)
    + the copy cases in `ServicePolicyTest`. Teeth: 47 mutations
    against the FINAL code (`teeth_saveloc_v4.json` beside `teeth.py`).
  * Accepted residuals: a copy cut by PROCESS DEATH keeps its final name
    (a later export then makes "name (2)") — temp-name + `renameDocument`
    is the fix if it ever matters; two hung providers pin the pool until
    restart; logcat carries download and folder names (control characters
    stripped).

Verification ritual that earned its keep: every fix was "teeth-checked" —
temporarily disabled, its test re-run (must fail), restored. Driver:
`python -X utf8 C:\Users\igorc\android-toolchain\scripts\teeth.py <spec>`
(JSON specs `teeth_*.json` beside it; LF/CRLF-aware). ALWAYS take an
external backup + `sha256sum` list first and verify with `sha256sum -c`
after — a script that stored the wrong "original" once restored a mutated
file. Test flakiness can be a real bug: chase it with `--rerun` loops
before calling it the dice (the persist race and a racy `onDone` were found
that way). A test must await state the unit under test RECORDS (saved file,
flow value, callback), never a fake's internal list. `teeth.py`
discards failure details: when a mutation also fails a test it should
NOT affect, re-run that one mutation by hand and READ the failure (that
is how a test waiting for a file to EXIST — not to be COMPLETE — was
caught). A test can lose its teeth silently: adding a column to a
listing made the test provider answer in the same order the app asks
for, so "read by name" was no longer exercised — keep the provider's
own order visibly different, and a branch is only covered by a test
that REACHES it (a "file in the way" test was caught by the first
check and never touched the second). Independent reviews are worth
their cost on anything with a trust boundary: four passes found real
defects the author and 40 green mutations had missed (a lost folder
copied nowhere while the UI promised Downloads; the per-file listing
was quadratic; a provider can hand back an existing user file).

Traps: `File(parent, "a", "b")` has no 3-argument constructor;
`Uri.lastPathSegment` is ALREADY decoded; `WebView.pauseTimers()` is an
instance method with process-wide effect; `const val` only in a companion;
":" and "|" cannot be in a file name on Windows; `ActivityController.create()`
returns the controller (`.get()` is the activity).

Kotlin builds ONE `$WhenMappings` table per class for EVERY enum the
class switches on with `when`. `TorrentRepository` switches on a
libtorrent4j enum, so one new `when (kind)` over a plain Kotlin enum in
that class made its JVM tests die with a libtorrent4j `LinkageError`
(native library) the first time that `when` ran. In a class that
switches on a libtorrent enum, use if/else chains over enums.

Robolectric + a real `DocumentsProvider` (save-location tests): attach
it with `readPermission`/`writePermission` = `MANAGE_DOCUMENTS` (the
framework refuses otherwise) and grant the caller through
`ShadowInstrumentation.grantPermissions` (reflection).
`ShadowContentResolver` INTERCEPTS the legacy `ContentResolver.query(uri,
projection, selection, …)` and calls the provider's LEGACY overload —
which the real `DocumentsProvider` rejects ("Pre-Android-O query format
not supported") — so code under test must use the bundle form
`query(uri, cols, Bundle.EMPTY, null)` (on a device the legacy form is a
wrapper around it). `MatrixCursor.put` is not in the SDK stubs (build
rows with `newRow().add(…)`). A failure injected through a PIPE is
invisible to the client (the bytes vanish into the pipe buffer) — make
the provider refuse to OPEN the document. Robolectric enforces no
permission, so a gate that "fails closed" needs its own assertion.
The settings DataStore is ONE file for the whole test run (stored
settings leak between tests AND classes — start from a known state and
restore the default), and so are `insertStatements`, posted
notifications and the granted-permission list (assert on names unique
to the test, or on counts before/after). An uncaught-exception handler
swapped in by a test must be swapped back only after a pause: the pool's
counter drops before the dying thread reaches the handler.

Verified on the EMULATOR (2026-11-24, Android 15 x86_64, the published
1.4.13 APK; zero crashes, zero ANRs and zero foreground-service policy
violations in the whole session): launch, notification permission and
first-run prompt; SAVE LOCATION end to end through the real system
picker (kept grant `persisted=0x3`; a PDF, a 58 MB APK and an
extension-less file copied under their exact names with identical
hashes — no ".bin"; nothing in public Downloads while a folder is
chosen; "Use Downloads folder" → a MediaStore row; survives force-stop;
a deleted folder → a clean failure and a "Not copied to your folder"
notification); a 58 MB download finishing in the background (service up
one second after Home, "Download finished" notification, service stops
by itself); a dead link → "Download failed"; the torrent lane (9.2 MB in
3 s from the PC seeder; all four files incl. the non-video ones copied
with their sub-folders; no padding files); the player (a video file and
a live HLS channel, the rotation button, audio focus taken and
released); the Live TV section chip and search; the browser (a rotation
round trip and the ".torrent" hand-off dialog); Commander; Room; the
in-app updater 1.4.12 → 1.4.13 (unknown-sources consent, "App
installed", data kept). Symbolic links: libtorrent 2.x flags a link
entry ONLY for `"attr": "l"` (a string) plus a `"symlink path"` list —
the old integer `attr: 1` is ignored and becomes a plain EMPTY file; for
a real link entry the app refuses the torrent on the device
("addTorrentFile: refused"), and libtorrent itself rewrites a link
target outside the torrent folder to point at itself.

1.4.14 (2026-11-25) — durable rules, do not regress:
- KEPT METADATA. A magnet's metadata is written as a .torrent when it
  arrives (`TorrentRepository.metadataArrived` →
  `TorrentEngine.saveTorrentFile`), so a restart needs no peer — a
  FINISHED magnet torrent came back as "Fetching info" with nothing to
  play whenever nobody was seeding (1.4.13 never kept it; verified fixed
  with the seeder switched off). The bytes come from the PURE builder
  `engine/KeptTorrent.kt` (`d`, `announce`, `announce-list` by tier,
  `url-list`, `info` = the raw info section); `KeptTorrentTest` decodes
  them with an independent decoder. LESSON (cost a build): the first
  writer lived inside the engine — where no JVM test can run it — and
  wrote no opening `d`; every unit test was green while the file was no
  torrent, and the restart silently fell back to the magnet. Only
  pulling the file off the emulator and parsing it showed it. So the
  engine now reads the file back with the parser the restart uses and
  keeps it only if it is the same torrent without symbolic links, and a
  source pin makes it write through the builder. After ANY change to
  what only the real engine can do: pull the artifact off the emulator
  and parse it — a green unit suite says nothing about it.
  A failed keep is retried on later refreshes (3 per process, a minute
  apart — not at the very next poll; reserved where it is launched:
  reserved when the coroutine got going, two refreshes a moment apart
  spent a try on nothing). At start a kept file counts only if it is
  THIS torrent — by EITHER of its hashes, see IDENTITY below — else the
  other torrent is removed from the session (unless an entry owns it by
  either hash), the file dropped and the magnet used. A kept
  file that carries a link, or is no torrent, is dropped so the metadata
  is kept again — "no torrent" only when the app's own reader agrees
  (`TorrentMeta`): a moment's read error is no verdict on what may be a
  private torrent's only metadata. Half-written `*.tmp` keeps are swept
  at start once they are a minute old (an import writes its own copy
  through such a file). The binding cannot hand out v2 piece layers:
  the kept file is info section + trackers + web seeds (libtorrent 2.1
  loads that for the hybrid test torrent).
- A MAGNET'S OWN PEER ADDRESSES (`x.pe=`) ARE NOT IN A .TORRENT: a
  torrent restored from the kept file is handed them again
  (`MagnetParser.peerHints` → `TorrentEngineApi.addPeers` →
  `connect_peer`); one added as a magnet is not — the engine reads them
  itself. Without that a trackerless magnet had NO peer after a restart
  — 0 B, 0 peers — where 1.4.13, which re-added the magnet at every
  start, still downloaded (emulator, 2026-11-25). The engine's OWN
  reading of `x.pe`, measured on the shipped libtorrent 2.1.0 at a
  magnet add and again with 2.1.1's Python binding: a plain `host:port`
  is used; a comma list — raw OR `%2C`-encoded — yields only its FIRST
  entry; `%3A`, a hostname or a port outside 1..65535 yield nothing;
  repeated `&x.pe=` add up; `[::1]:51413` works. So `peerHints` reads
  the value as a URL does (`%XX` decoded, "+" left alone) and hands over
  EVERY well-formed entry — never fewer than the add used: read raw,
  `a:1%2Cb:2` was one junk address, and the restored torrent had lost
  the one the add had. Only what can be a literal address passes
  (`[0-9A-Fa-f:.]+`, port 1..65535, each once) — a magnet is anyone's
  text, and a name or an escaped line break is left out there, not
  passed on to be logged. At most 64, the first as listed, and nothing
  past them is even read — a hostile magnet must not be a list of knocks
at every start (`MAX_PEER_HINTS`; the chain is lazy). A kept file the engine would not take but that
  was kept is SUSPECT (`suspectKept`): the metadata is written over it
  when the info arrives — the app's lenient reader accepts a file
  truncated in its last byte, and "there is a file" kept the good one
  from ever coming.
- IDENTITY — A HYBRID TORRENT HAS TWO HASHES (emulator, 2026-11-25; it
  made the kept-metadata fix void for the commonest magnet, through
  three "verified" rounds). The ENGINE names a hybrid torrent by its
  v2 hash (the first 40 hex) — `handle.infoHash()`, `TorrentInfo
  .infoHash()` — whichever of its hashes found it. An ENTRY is keyed by
  the magnet's `btih`: for every torrent a site links by v1 that is the
  torrent's v1 hash (current tools create hybrid torrents by default).
  Two strings, one torrent; compared directly they never matched, and
  such a torrent (a) read its OWN kept file as another torrent's —
  deleted at every start, the magnet asked again, "Fetching info" with
  nobody seeding; (b) got no "Download finished" (the alert is said by
  the v2 hash, the watch list holds the entry's); (c) was copied at every
  start (the mark was saved under a hash no entry has); (d) kept its row
  after a symbolic-link removal. Rules now: `TorrentFileAdd.Added(infoHash,
  name, v1Hash)` — the engine's name and the v1 hash; there is no third
  (the engine's name IS the first 40 hex of the v2 hash, so a
  `v2Prefix` of its own was dead logic: once the fake told the truth, no
  break of it failed a test); start() accepts a kept file when the
  entry's hash is EITHER and takes another torrent's out of the session
  only when no entry owns it by either; a kept file NAMED by a hash no
  entry has — what the first 1.4.14 test build left for such torrents,
  under the engine's name — is given to the entry whose torrent it
  holds and renamed to that entry's name (`adoptOrphanKeptFiles`, before
  the entries are restored); a `.torrent` file for a torrent already
  listed under its OTHER hash is "already in the list", and the file
  is kept for that entry under its key; `TorrentEngineApi
  .engineHashOf` is the engine's name for a torrent, found by either
  hash; `TorrentRepository.entryOfEngineHash` translates what comes FROM
  the engine to the entry's key — `metadataArrived` keeps, renames and
  remembers under the ENTRY's key, `finishedFromEngine` announces,
  copies and marks by it. NEVER compare an engine-reported hash with an
  entry key directly. (The kept file the engine writes carries no v2
  piece layers; libtorrent reads it back with BOTH hashes all the same
  — pulled off the emulator and parsed, 2026-11-25.) WHY NO TEST SAW IT: the test fake answered
  `addTorrentFile` with the file's v1 hash as "the engine's name" (a
  natural guess) and every emulator magnet was in the v2 form (the
  Python seeder prints `info_hash()`, which is v2). The fake now names
  torrents as the real engine does (`v2Prefixes`), and the "a hybrid
  torrent from a v1 magnet" tests in TorrentIdentityTest pin all four
  symptoms. After ANY change touching identity, run BOTH magnet forms on
  the emulator. Still a gap (pre-existing): a MAGNET for a torrent
  already listed under its other hash becomes a second entry — the
  magnet's hash is neither that entry's key nor the engine's name, and
  finding it would ask the engine for both hashes of every entry.
- NO ALERT OUTLIVES ITS CALLBACK (a native crash at a torrent's finish;
emulator, 2026-11-25: SIGSEGV in `torrent_handle.is_valid`). The binding
wraps an alert's handle WITHOUT owning it — a view into the alert — and the
engine frees an alert once the listener has returned. The copy-out read the
finished alert's handle in a coroutine AFTER the callback (older than this
version, and its comment called it safe: "null once it is gone"); it
worked only while the delay stayed under the engine's recycling. The
identity change put a lookup and a refresh in front of that read, and it
went off on the first try: the app died a quarter of a second after the
metadata was kept, the system restarted it at once, and three checks
read the new process as a pass. It surfaced only in the system's exit
records, as one SIGNALED exit. Now the copy-out asks the SESSION for the
torrent (`TorrentEngineApi.filePathsOf`, a handle of the app's own), and
inside the listener only the alert's own hash and message are read. Rule:
take from an alert what is needed IN the callback; never keep the alert.
- "FINISHED" RE-FIRES AT EVERY START. libtorrent reports
  TorrentFinishedAlert for every complete torrent at each start (the
  app saves no resume data; the files are re-checked). That was
  1.4.13's real cause of "ep1 (1).mp4, (2).mp4" in Downloads — a copy
  at every launch — and would have been a "Download finished" on every
  launch once magnet torrents come back complete. Rules: (a) announced
  only when this process watched the torrent DOWNLOADING
  (`unfinishedSeen`: added here, or a poll reporting
  `TorrentState.DOWNLOADING` — NOT "bytes still to get": a complete
  torrent being re-checked reads that way, and was announced at every
  launch); (b) copied out ONCE per torrent (`copiedHashes`, saved with
  the entry as `"c"`, set after a run with no failure; a run with a
  failure is not marked, so it is finished at the next start; a copy
  the user deleted is not put back; the "being copied" mark is held
  until the copy job itself is over — the pool queues it). A list saved
  by 1.4.13 says nothing about copies: such entries count as copied from
  the start (copied again at the first start after the update, a
  deleted copy came back), and a torrent a poll sees DOWNLOADING is
  taken off the mark — SAVED AT ONCE (in memory alone the entry read as
  "copied" again at the next start, so a copy that failed was never
  retried; on the emulator the saved mark flips within a second) — so
  one still downloading is downloaded, announced, copied and marked when
  it finishes. Files that cannot be listed are no copy: nothing is marked.
  The mark is a FINISH'S only while the entry is that torrent: not for
  one whose entry was removed while the copy ran (the mark would outlive
  it and greet the same torrent added afresh), not for one a poll saw
  DOWNLOADING again meanwhile (what was copied is not its final files;
  its next finish copies again) — `markCopied` asks both. The copy pool
  SAYS whether it took the job (`FinishedExport.submit` returns a
  Boolean): before, a job it did not take left the torrent "being
  copied" for the rest of the process.
  and the next start asks again (`copyFinishedOnce`).
- NO SECOND COPY IN DOWNLOADS (`MediaStoreExport.alreadyInDownloads`:
  name + size + `is_pending = 0`, asked of a recording provider in a
  test; then `FinishedExport.sameContent` compares the FIRST 64 KiB —
  in the chosen folder as well, so another file of the same name and
  size goes beside / is copied again). Only the beginning: a chosen
  folder may be a cloud provider's, and the end of a big file cannot be
  had from a pipe without pulling the whole file through. Checked
  against the emulator's real MediaProvider (Android 15): rows the app
  inserts into the Downloads collection with a video MIME type ARE
  returned by it.
- A FAILED SETTINGS SAVE IS TOLD, NOT FATAL (`saveSetting` wraps every
  write in TorrentRepository, the updater's daily stamp likewise;
  `SettingsSaveGuardTest` pins it, incl. a source pin that no setter
  launches a bare coroutine). Found by experiment: the exception used
  to escape a plain `scope.launch` — on a phone that ends the process
  (a full disk at a toggle, or at the chat-history save after every
  reply). The settings library keeps handing out the NEW value after a
  failed write until a later save writes everything, so the change is
  lost if the app goes first — which is why the user is told.
- WINDOWS TEST-RIG TRAP (this machine): an atomic move is refused while
  another handle has the target open ("The process cannot access the
  file because it is being used by another process") — the state
  save's and DataStore's alike. A test that polls a file in a tight loop
  can make the app's own save fail (seen: a torrent's name stayed
  unsaved) or DataStore's (seen twice in 21 full runs: each time five
  15 s timeouts in FinishedExportTest, all five at once — one refused
  save leaves the shared store stuck for the rest of that run; the next
  runs were clean). The app's save now retries the move (8 × 15 ms);
  test helpers answer a colliding read with the last good one. If
  FinishedExportTest times out on "the public Downloads folder being
  the destination", suspect this first and re-run before hunting code.
- NAMES: file-added torrents by their torrent's name (were listed by
  info-hash); a magnet by `dn=` until the metadata arrives, then the
  real name. The engine's status never carries a name (`QUERY_NAME` is
  never asked): `TorrentEngine.torrentName` asks the handle.
  `renameEntry` replaces entries in one step and `remove()` (and the
  removal for symbolic links) go by `infoHash` — a captured copy whose
  name had changed meanwhile used to survive a Remove and come back at
  the next start. Listed names are cleaned exactly as the confirmation
  shows them (`confirmationName`: no line breaks, no invisible direction
  marks — a right-to-left override made a name read as another — and at
  most 200 characters) — a name READ FROM THE SAVED LIST too: a list
  saved by 1.4.13 may hold a magnet's title raw, invisible marks and
  line breaks included.
- REFUSALS SAY WHY: a .torrent with a symbolic link →
  `noticeTorrentHasSymlinksFile`; one the engine could not take →
  `noticeCouldNotAddTorrent` (`TorrentFileAdd`, a sealed result).
- NOTIFICATION: `TorrentService.describeProgress` (pure) — plurals,
  "…" for an unknown size (was "-1%"), and no bar while idle (the
  idle grace used to show a moving bar under "No active downloads").
- UPDATER: the "install unknown apps" consent is asked BEFORE the
  installer (a dialog in AppRoot; the path survives process death
  through `rememberSaveable`; asked once) — asked by the installer
  itself, the system screen stayed in the back stack and the user was
  left in Settings. `install()` looks at the file's size again at the
  last moment — with the size kept beside the path, so the look
  survives Android starting the process again for that answer — and
  drops a file that is no longer whole; the installer is started ONCE
  per download BY THE APP — but the USER'S OWN TAP on "Update now"
  starts it again (`install(…, userTap = true)`): that one may have been
  cancelled, or Android may have said "not installed" — except a tap
  within 800 ms of the start (`RETAP_DEBOUNCE_MS`): a double tap stacked
  two installer screens. The consent screen's answer is the user's own
  act too (`userTap = true`). An update trouble with NO dialog to show
  it — a process started again for the consent answer — is HELD by the
  updater (`AppUpdater.undialogged`, a StateFlow) and said by the app
  once it is up; sent to the notice flow (a no-replay SharedFlow) it was
  dropped, nobody listening yet — which is all the first fix of the
  not-whole file did, its test wiring the flow to a list. The guard as
  first written made the button dead for the rest of the session — a
  regression the delta review caught. The file's address and the
  start of the installer are constructor parameters of `AppUpdater`,
  so the installer's path is tested on any host (the real FileProvider
  call refuses every Windows path — it compares separators; the test
  that used to skip on Windows for that now runs).
- DOWNLOADS TAB: "Open" (the system's viewer) for finished files the
  app cannot play; finished videos keep Play; the row itself opens or
  plays (`filecmd/FileIntents`, `DownloadOpener`; Commander's share /
  open-with delegate to FileIntents). One tap on a downloaded APK
  reaches the SYSTEM installer prompt — intended (it is the system's
  own confirmation); an in-app confirm first is the owner's call. The
  three lists leave 96dp below the last card: the floating "Add
  download" button covers the bottom of the list and the last card's
  buttons sat under it for good (pre-existing).
- BOTTOM TAB LABEL shrinks to fit (`TextAutoSize.StepBased`, 7sp
  floor): at 1.3× "Downloads" was cut to "Downlo"; hiding what did
  not fit (a first attempt) took the label away at the NORMAL font
  size too.
- RSS: a feed item refused on the spot is reported
  (`noticeRssItemRefused`); plain-file links are queued and refused
  later by the engine as the item's error (intended). A feed's items
  show only when its card is expanded, and everything present when a
  feed is added is baselined as seen — give a test feed new guids.
- A direct video link in the browser plays in the page (WebView); it
  does not download — "Add download" is the way for a video URL.
- TOOLING LESSONS (2026-11-25): (a) A failed assertion in an edit script
  stops the chain BEFORE the build — the old APK stays on disk next to
  BUILD_EXIT=1; compare the APK's timestamp and sha256 before trusting
  a build (it happened twice). (b) The mutation check can run on a
  private COPY of android/ while the working tree stays frozen — no
  secrets needed (the key, keystore and local.properties files are
  optional to the build script): `teeth_dir.py <spec> <copy's android
  dir>` in the toolchain's scripts dir, with `make_teeth_1_4_14.py`
  reading DF_SRC_ROOT / DF_RESULTS_DIR / DF_TEETH_OUT / DF_HALF /
  DF_ONLY; two copies halve the time. On Windows the driver must launch
  `gradlew` through bash (to CreateProcess it is "not a valid Win32
  application" — a run that ends in 5 s tested nothing). List the
  copy's sources with sha256sum before and verify after. (c) A break
  that does not compile is not a mutation — the compiler enforces that
  rule — and a test listed under a break must really FAIL under it: an
  over-listed expectation is a finding about the test, not the code.
  (d) Deleting a file on the emulator with a raw `rm` leaves its
  media-store row behind; the app's copy then fails once ("UNIQUE
  constraint failed: files._data"), says so, and is recognised at the
  next start. Delete through the media store instead (`content delete
  --uri content://media/external_primary/downloads --where
  "_display_name='…'"`). (e) Edit files by exact-text anchors; a
  line-range edit doubled an `@Test` and broke a test file — restore
  it from the frozen copy and redo; a wrap-around edit kept a second
  copy of the first lines (a second, unregistered recorder). (f) A test that waits for a test
  double's internal list and then asks the unit under test has a race
  (TorrentAddConfirmationTest had one for weeks; it surfaced when a
  name-cleaning change widened the gap by a regex): await what the unit
  RECORDS. (g) The emulator helper sets MSYS_NO_PATHCONV=1: give adb
  WINDOWS paths (C:/…). With /c/… `adb install` fails and the OLD build
  keeps running — a staged scenario was consumed by the old build that
  way. After `adb root` adbd restarts; after an emulator reboot it is
  not root. Install first, compare the installed package's sha256 with
  the build's, THEN start the scenario. (h) Read the app's own log
  before concluding: a copy that was "not marked" had FAILED on a
  stale media row (my raw `rm`), it was not a lost mark. (i) A fresh
  emulator's link to the host can be slow and drop (uTP timeouts,
  18 KB/s) — give a transfer minutes, and suspect the link before the
  code. (j) An edit script's anchor is exact text: count 1 or it stops
  BEFORE writing, and a dry run with the writes disabled comes first —
  a wrapped line and a replacement that would have repeated the
  sentence it replaced were both caught that way on 2026-11-25.
  (k) A FAKE MUST SAY WHAT THE REAL THING SAYS: the test engine's
  `addTorrentFile` answered with the v1 hash as the engine's name — a
  guess — and so could not show that the real engine names a hybrid
  torrent by its v2 hash. Every identity a fake reports is a claim
  about the real one: measure it on the device and write the fake to
  match. (l) VARY THE SHAPE OF THE INPUT ON THE DEVICE: every emulator
  magnet was in the v2 form because the seeder prints that hash. One
  magnet in the v1 form — how sites link torrents — showed the kept
  file deleted at every start. (m) A BARE TCP CONNECT IS NO SIGNAL: the
  emulator's network path shows a TCP connection to the seeder that
  carries no handshake in EVERY case, hints or none. The proof that an
  address was used is a completed handshake naming the torrent (the
  seeder's `peer_connect_alert` with the torrent's hash), or data.
  (n) A number on the list with 0 peers is protocol chatter in a
  trailing window (the engine's `downloadRate` includes it) — the app
  announces to the public DHT, so crawlers and anything answering for a
  dummy hash come; stage entries only with content that may be public.
  (o) A MUTATION RUN NEEDS THE MACHINE TO ITSELF: with two full-suite
  runs competing, steps finished in 0 s and reported the previous
  step's failures as their own — stale result files where a Gradle
  invocation died at once. A run with 0 s steps is no run. (p) A CHECK
  CAN BE WRONG WHERE THE PRODUCT IS RIGHT: a live check looked for the
  file size in decimal MB; the page says MiB. Read the page (the log,
  the device) before believing a FAIL. (q) A CRASH CHECK BY LOG TEXT IS
  NO CRASH CHECK: a native crash is "F libc: Fatal signal 11" and
  "F DEBUG" frames — not "AndroidRuntime" — and Android restarts a
  foreground activity at once, so a later read sees a healthy process
  (that is how a SIGSEGV at a torrent's finish passed three checks).
  The proof of "no crash" is `dumpsys activity exit-info <pkg>` grouped
  by REASON — anything but one's own force-stops (10/21) is a crash — and
  the pid being the same before and after the step. (r) A CHECK WHOSE
  PRECONDITION DID NOT HAPPEN PROVES NOTHING: nine "no crash" rounds
  were empty — the torrent never got its metadata (mark still False, no
  kept file), because the emulator's network path makes TCP and uTP
  attempts to the SAME peer race and the app dropped the connection that
  carried it. A test seeder listens on ONE transport
  (`enable_incoming_tcp`/`enable_outgoing_tcp` false), and a script
  verifies what it staged (the orphan exists, the kept file is there)
  BEFORE it starts the app. (s) Nothing can be handed to the app as a
  `.torrent` from adb on this rig (file:// and content:// addresses
  arrive, no confirmation shows) — that path is unit-tested only.
- TOOLING LESSONS (2026-11-26, the 1.4.14 release day — each one paid for):
  (t) THE EMULATOR'S USER-MODE NETWORK MIS-ADDRESSES A LOOPBACK
  SEEDER'S PACKETS. With the seeder on the PC's 127.0.0.1 (reached as
  10.0.2.2), at some point in a flow its packets reach the guest with
  the RAW host address 127.0.0.1 instead of the 10.0.2.2 alias. The app
  rightly ignores them, the seeder retransmits unacknowledged and times
  out: transfers went in bursts with a minute or more between, and the
  first connection after each seeder restart died at the extension
  handshake. Nothing in the app. See it with the device's own tcpdump —
  on ALL interfaces (`tcpdump -i any -n -tttt 'udp port <port>'`; app
  traffic rides wlan0, so eth0 alone shows nothing). Two wrong theories
  were tested and discarded first (a stale port; my own uiautomator
  load), and an hour went before the capture: CAPTURE FIRST.
  (u) libtorrent ignores every rate limit for a peer it sees as local
  network (`ignore_limits_on_local_network`, default true — the emulator
  is 127.0.0.1 to the seeder). The site-screenshot notes say so; set it
  False on a throttled test seeder.
  (v) ON THE ANDROID 9 GUEST A FAST LOCAL HTTP SERVER LOSES THE TAIL OF A
  TCP STREAM ("unexpected end of stream"; a different count per attempt;
  the server unaware) — the emulator acknowledges to the sender before
  the guest has taken the data. The same file, server and app completes
  when the server is paced (1.5 MB/s). PACE TEST SERVERS. And a failure
  seen on ONE API level only is not yet an app bug: run the same thing
  on the other emulator before reading code.
  (w) ANDROID 9 (API 28) DIFFERS AS A TEST TARGET: `uiautomator dump`
  answers "could not get idle state" while a screen animates (drive it
  by screenshot and coordinates — and delete the local ui.xml first, a
  stale one reads as "nothing happened"); its layout sits ~190 px HIGHER
  than the API 35 device's, so coordinates do not carry over;
  `dumpsys activity exit-info` exists only from Android 11 — there read
  `logcat -b crash` and `dumpsys dropbox`; adb stops accepting `-e`
  once a second emulator runs (name the serial: emulator-5554 =
  medium_phone, emulator-5556 = `api28`, started with `-port 5556`).
  The AOSP x86_64 image for API 28 installs with sdkmanager; the AVD
  boots in ~30 s beside the other one (16 GB free of 32).
  (x) A .TORRENT "FROM ANOTHER APP" CAN BE DELIVERED AFTER ALL (the note
  "adb could not deliver one" was true only for root). Push it to
  /sdcard/Download; `am broadcast -a android.intent.action
  .MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/Download/X.torrent` gives
  it a MediaStore row (content://media/external/file — look the id up
  with `content query`); then AS THE SHELL USER, NOT ROOT (`adb unroot`
  first: as root the system refuses the grant — "cannot issue a Uri
  permission grant … use startActivityAsCaller()" — and the app rightly
  says "Could not read the .torrent file"): `am start -a
  android.intent.action.VIEW -d content://media/external/file/<id> -t
  application/x-bittorrent --grant-read-uri-permission`. `adb root`
  again afterwards to read the app's files.
  (y) GITHUB ACTIONS: a step WITHOUT `shell: bash` runs as plain `bash
  -e` — a failing command piped into `tee` is then NOT a failed step
  (declare `shell: bash`, or take the exit from PIPESTATUS). Maven
  Central rate-limits the shared runners: HTTP 429, "could not resolve
  plugin artifact", seconds after the daemon starts — android/
  gradle.properties carries the retry settings, whose names were read
  out of the Gradle 9.6.0 binary (`org.gradle.internal.repository
  .max.tentatives` — there is NO "max.retries" — and `…
  .initial.backoff`). Robolectric on Gradle's default 512 MB test heap
  passes on a fast machine and times out on a runner, one different
  test per run: the worker gets 2 GB. The `gh` CLI is not installed
  here; the Actions API works with the machine's stored git credential
  (`git credential fill`, held in a shell variable, never printed) —
  run lists are public, logs are not; anonymous calls: 60 an hour.
  Pushing two refs with one commit starts the push-triggered workflow
  twice (once per ref).
  (z) A TEST THAT SKIPS ITSELF ON YOUR MACHINE HAS NEVER RUN. Five did;
  the first Linux run found one whose expectation its own layout could
  not produce (CommanderTest.folderSizerDoesNotFollowSymbolicLinks
  expected 300 where following no link gives 9300) — and I reported it
  as an app bug before doing the arithmetic. When a never-run test
  fails, compute the right answer from the layout FIRST.

- KNOWN GAPS IN 1.4.14 (from the reviews of 2026-11-25 — written down,
  not changed): (1) A torrent that completes within ONE POLL of its
  check at a start is not announced (no poll saw it DOWNLOADING; the
  copy still runs). Polls cannot tell it from a complete torrent being
  re-checked — it wants resume data saved with the engine. (2) The
  same window on a list saved by 1.4.13: an INCOMPLETE torrent that
  finishes before a poll sees it downloading is neither announced nor
  copied ("no `c`" counts as copied). (3) A copy that can never succeed
  (a different file of the same name where it would go) is not marked,
  so "finished" at every start tells the user at every launch. (4) A
  magnet for a torrent listed under its other hash makes a second
  entry. (5) Raw torrent and file names reach logcat and the "failed"
  notification uncleaned. (6) The saved list's `h` is not validated when
  read (a private file; the `.torrent` suffix bounds what it can name).
  (7) "Later" during an update download lets it finish to Ready. (8)
  Peer addresses are handed to a restored torrent even when it is
  paused — deliberate: a paused torrent connects to nothing, and on
  resume the magnet's own addresses are what it needs. (9) The
  bracketed-IPv6 form `[fe80::1%3A51413]` (an encoded colon inside the
  brackets) is dropped. (10) FOUND 2026-11-26, in the released build: a
  V2-ONLY torrent cannot be added by MAGNET — `MagnetParser
  .infoHashHex` accepts a 40-hex or 32-char base32 btih, and a v2-only
  magnet carries the full 64-hex v2 hash, so every caller treats it as
  no magnet (the confirmation never appears; a notice says so). Hybrid
  torrents are linked by their v1 hash, so they are unaffected; by
  .torrent file a v2-only torrent works. Supporting it touches IDENTITY
  (the engine named that torrent by a 20-byte value of its own, not its
  v2 hash): 1.4.15 work with tests, not a quick patch. Cosmetic, same
  day: the confirmation shows a torrent's name ("V2Only"), the list
  shows its single file ("v2.bin").

Still unverified anywhere: the wake lock after a 60 s stall (does an
inbound peer wake the radio?), a physical device's battery policy for
the foreground service (the emulator is always plugged in), the AUDIBLE
side of the player's audio-track switch (its selection path ran on the
emulator, which has no audio device), Compose
rendering of agent markdown (not exercised — it would spend the owner's
AI key), a hung folder provider, multi-GB copies; and from 1.4.14: the
install-consent dialog's CONSENT-FIRST return path as 1.4.14 does it (it
needs a build newer than 1.4.14 — its pieces are unit-tested; the
1.4.13 -> 1.4.14 update through the OLD order ran on the emulator,
2026-11-26), and the "…" wording for an unknown-size
download (unit-tested only). RUN ON THE EMULATOR 2026-11-26, no longer
in this list: a torrent restored UNFINISHED and then finishing; `Result
.NoDestination` on an Android 9 image (no folder chosen = finished, not
copied, not a failure; a folder chosen through the real picker gets the
copy); a v2-only torrent added by a .torrent FILE (its kept file is the
original, piece layers included; after a restart with nobody seeding the
torrent is back with its size — by MAGNET such a torrent is refused,
KNOWN GAP 10); and a .torrent handed over "from another app" (TOOLING
LESSON (x): as the shell user, with a granted content URI).
Accepted trade-off (narrow): a torrent restored UNFINISHED whose whole
downloading phase falls between two 1 s polls is not
announced (its copy still runs — only the announcement depends on being
watched); for an entry saved by 1.4.13, which counts as copied until a
poll sees it download, that case is not copied either. Settled since:
TorrentFinishedAlert DOES re-fire for an already-complete torrent at
each start (see 1.4.14 above).
