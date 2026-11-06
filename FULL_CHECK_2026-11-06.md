# DeepFlux Full Application Check — 2026-11-06

Scope: the whole 7.2 tree (`agent`, `config.py`, `dlmgr`, `engine`, `gui`, `infra`, `iptv`,
`ircmgr`, `native_messaging`, `packaging`, `main.py`, `tests`, `website`, CI workflows).
Method: full test suite in the documented 8 chunks, `py_compile` of all 113 source files,
ruff (E4/E7/E9/F), security greps, durable-rules compliance audit (AGENTS.md), and a
packaging/build/release/CI review. **Nothing was modified during the check.**

## Results at a glance

| Area | Result |
|---|---|
| Test suite | ✅ **1256 tests, 0 failures** (8 chunks, all green, no flakes). 1 deprecation warning: libtorrent `session.status()` at `engine/torrent_engine.py:701` |
| Syntax | ✅ All 113 source `.py` files compile |
| Lint | ⚠️ 108 ruff findings — 2 are real runtime bugs (P0-1, P0-2), rest is hygiene (66 unused imports, 11 dead locals, inert annotations) |
| Security | ✅ No `eval`/`exec`, no `pickle`, no `os.system`, no hardcoded secrets, no bare `except`, no config-write bypasses; only `shell=True` is the confirmation-gated `run_shell` tool |
| Subprocess hygiene | ✅ Every Windows spawn carries `CREATE_NO_WINDOW` (POSIX branches correctly excluded) |
| Durable rules (AGENTS.md) | ✅ 16/19 PASS, 3 gaps (P0-3…P0-5 below) |
| Versions & feeds | ✅ 7.2 consistent everywhere; both deployed binaries byte-match their `latest.json` feeds; PINNED files fresh |
| Packaging | ⚠️ 2 pre-build bugs (P0-6), several MED/LOW items |
| Repo hygiene | ⚠️ untracked scratch state, no release tags, stale branches, stale `pyproject.toml` |

---

## P0 — Real bugs to fix now (small diffs, high impact)

1. **`_format_tool_args` raises `NameError` on every agent tool event** — `gui/main_window.py:4679`
   uses `_html.escape(...)` but `_html` is only ever imported function-locally elsewhere
   (`:5429`). The call at `:4793` is wrapped by the swallowing `except Exception:
   logger.debug` at `:4899`, so every `tool_start` progress line ("🔧 Searching for…")
   silently never renders, and the 2026-09-15 HTML-escape defense for LLM-controlled tool
   args is dead code. **Fix:** `import html` at module top and use `html.escape` (also drops
   the E731 lambda). **Add a regression test** that calls `_format_tool_args` for each
   formatted tool (none exists today).

2. **`remove_job` raises `NameError` for incomplete stream jobs** — `dlmgr/engine.py:983`
   calls `shutil.rmtree` but `shutil` is only imported inside *other* methods (`:654`,
   `:950`). Removing an errored HLS/DASH job crashes the cleanup, re-orphaning the
   multi-GB `.segments` dirs the 2026-09-15 review fixed. Callers:
   `gui/downloads_tab.py:926`, `gui/main_window.py:5370`, `dlmgr/control_api.py:308`.
   **Fix:** move `import shutil` to the module top + regression test.

3. **Settings import is non-atomic and can be silently reverted** — `gui/main_window.py:6440`
   writes the imported payload with `Path(config_path).write_bytes(...)` instead of the
   codebase's temp+`os.replace` pattern, and `_skip_config_save` covers `closeEvent`
   (`:6992`) and the browser-session save (`:3766`) but **not** the RSS worker
   (`:6798-6802`), the hourly Jackett sync (`infra/jackett.py:342`) or the agent RSS
   persist (`agent/tools.py:4471`). On the import "Later" branch a background tick can
   overwrite the imported file with the stale in-memory config.
   **Fix:** atomic write + extend `_skip_config_save` checks (or reload the imported config
   into memory so background writers persist the new one).

4. **`rss_monitor` event escapes nothing** — `gui/main_window.py:4837-4840` interpolates the
   agent-settable feed name raw into `_append_event` (`:5531-5533`), which inserts
   unescaped into the rich-text sink. Markup injection incl. a one-click no-confirmation
   magnet link is possible (the `rss_auto_download` twin is safe).
   **Fix:** escape like the `flaresolverr` branch (`:4875`) or route via `_append_agent`.

5. **Download splitter clobbers user-saved ratios** — `gui/main_window.py:5252-5257` and
   `:5270-5273` unconditionally re-enforce 50/50 at `showEvent` AND on every visit to the
   Torrents tab, so user-adjusted splitter positions can never survive (the comment at
   `:5265` says "if no saved state restored it" but no such check exists).
   **Fix:** enforce only when `ui_splitters` has no saved `download_v5_stack` entry and
   gate the tab-change path with `_agent_ratio_applied`.

6. **Llama runtime bundling/extraction bugs (fix before the next installer build):**
   - `packaging/app.spec:93-98` globs **every** zip in `packaging/llama/` — the dir holds 6
     zips (b11729/b12040/b12065 × cpu/vulkan) while `PINNED.json` pins b12065, and
     `fetch_llama.py` never deletes superseded zips → the setup exe silently grows ~124 MB
     of stale runtimes.
   - `infra/local_model.py:766-780` `_bundled_zip()` returns the *first* `os.listdir`
     match (alphabetically the oldest, b11729) and `PINNED.json` isn't even shipped as
     app.spec data → `--setup-localmodel` can extract a non-pinned runtime.
   **Fix:** ship `packaging/llama/PINNED.json` as data, select the exact pinned asset name
   in `_bundled_zip()`, make `fetch_llama.py` delete zips not named in the new pin, and
   limit `app.spec` to the pinned names. Add a regression test (multi-zip dir).

7. **Delete `website/.mimosa/` + `website/deepflux/.mimosa/` before the next `vercel
   --prod`** — 209 files / 4.1 MB of internal agent-tooling state (session ledgers,
   source baselines, per-file diff data for `api/*.js`) sit inside the Vercel deploy tree
   with no `.vercelignore`, so a deploy risks shipping it to deepflux.space.
   **Fix:** delete both dirs, add `.mimosa/` to the root `.gitignore`, add a
   `website/.vercelignore`, and confirm production doesn't already serve `/.mimosa/`.

## P1 — Robustness & hardening

8. **Streamed byte caps for the remaining uncapped remote reads** (the codebase rule is
   streamed+hard-capped): `dlmgr/site_grabber.py:194,240`, `dlmgr/extractors/generic.py:104`
   (full `resp.text` of arbitrary video sites), `agent/tools.py:5056,5084` +
   `infra/jackett.py:174` (torznab XML bodies via `ET.fromstring(resp.text)`),
   `dlmgr/ytdlp_update.py:69` (PyPI probe), plus the Wikipedia about-fetch
   (`agent/tools.py:2502-2524`). Reuse the `public_http_get` cap pattern.
9. **OnlyHumans hub reads have no client-side byte cap** — `ircmgr/oh_room.py:445-536`
   materializes `r.json()` bounded only by server caps (`MAIL_CHUNK=16`). Add a streamed
   cap to `mail_drain`/`_request`. Also rewrite the **stale AGENTS.md Room section**
   (`MAX_WIRE_BYTES`/`_recv_line`/ping-budget mechanisms no longer exist after the
   OnlyHumans rewrite; the new invariants are `HISTORY_KEEP` trim and role re-check under
   lock).
10. **`requirements.txt` is incomplete** — missing `cryptography` (Settings export/import
    crashes without it), `Pillow` (logo/icon generation), `defusedxml` (`agent/rss.py:13-14`
    module-level import — RSS tools die without it). CI workflows paper over this ad hoc;
    a plain `pip install -r requirements.txt` app breaks Settings backup + RSS. Also sync
    `pyproject.toml`: `[project] version = "3.4.3"` is stale (7.2), and its dependency list
    diverges from requirements.txt.
11. **macOS spec missing `collect_all("nacl")` / `collect_all("argon2")`** that the Windows
    and Linux specs carry (`app_macos.spec` vs `app.spec:48-49`, `app_linux.spec:75-76`) —
    PyNaCl ships wheel data PyInstaller won't find, so Room crypto very likely fails in the
    frozen .app and the CI boot smoke never joins a room to catch it.
12. **libtorrent deprecation** — `engine/torrent_engine.py:701` `session.status()` is
    deprecated; use `session_stats_metrics` (it is already try/except-guarded; the warning
    is the only noise in the whole suite).
13. **Duplicate i18n dict keys** — `gui/i18n_zh_cn.py:781,782,819` ("Download", "Remove",
    "active") — later values silently shadow earlier translations. Fix the shadowed pairs.
14. **ffmpeg remux deadline only checked between stderr lines** (`dlmgr/ffmpeg.py:444-460`) —
    a hung ffmpeg emitting nothing is never killed. Add an overall wall-clock kill
    (e.g. `communicate(timeout=...)` in a watcher thread).
15. **`_list_torrent_sources` echoes raw source URLs** (`agent/tools.py:4152`) — apply
    `redact_source_credentials` for parity with `iptv_list_sources`/`_list_rss_feeds`.

## P2 — Process, tooling & hygiene

16. **AGENTS.md release-gate commands don't work as written in PowerShell** — the documented
    chunks use shell globs (`tests/test_a*.py`), which PowerShell passes literally to pytest
    ("file or directory not found", exit 4). Add the PowerShell-safe form
    (`Get-ChildItem tests -Filter test_*.py | Where-Object { $_.Name.Substring(5,1) -match
    '^[abc]$' }` …) or note that the gate assumes a glob-expanding shell.
17. **Git hygiene**: tag releases (no tags exist for 7.0/7.1/7.2 — useful for updater
    forensics and worktree verification); prune the merged `app`/`linux-port`/`macos-port`
    branches; add `.mimosa/` and `_site_shots.py` to `.gitignore` (or deliberately track
    `_site_shots.py` — it is the documented screenshot tool and a fresh clone loses it);
    move the pre-commit version-sync hook into a tracked `.githooks/` via `core.hooksPath`
    (fresh clones lose it today).
18. **CI**: migrate `macos-build.yml` off the retiring `macos-14` runner (M4); the `push:`
    triggers are pinned to the old branch names (`linux-port`/`macos-port`) so pushes never
    trigger — drop or point at the default branch; bump `actions/checkout@v4`→v5,
    `setup-python@v5`→v6, `upload-artifact@v4`→v5.
19. **Test gaps**: `dlmgr/scheduler.py` (TokenBucketThrottler + PriorityScheduler — real
    logic, zero coverage) deserves unit tests; `gui/rss_dialog.py`, `gui/rss_viewer.py`,
    `gui/sources_dialog.py`, `gui/help_dialog_zh.py` are never referenced by any test
    (thin Qt shells — smoke tests suffice); add the P0-1/P0-2 regression tests above.
20. **Lint pass**: `ruff check --fix` auto-fixes 73 of the 108 findings (mostly unused
    imports); annotate the deliberate ones (`# noqa` for the `yt_dlp` availability probe at
    `dlmgr/engine.py:701` and the platform-gated E402 imports in `main_window.py:117-122`).
    Add `Any`/`Tuple`/`TYPE_CHECKING` imports where annotations reference them
    (`agent/rss.py:250`, `agent/tools.py:4718,4983`, `infra/local_model_setup.py:31,41`,
    `gui/main_window.py:1867,5016`).
21. **Uninstaller wipes `~/.deeptorrent` with no warning** (`packaging/installer.iss:215-236`
    — config.json, install_id, fast-resume, indexer health, extracted llama runtimes,
    multi-GB GGUFs). Add an uninstall-page warning or preserve the models dir; also fix the
    stale `infra/telemetry.py` docstring claiming the installer only deletes config.json.
22. **`parse_feed` accepts any extensionless basename** (`infra/updater.py:137-146`) — add an
    `.exe`/`.AppImage` whitelist for symmetry with the Linux apply check, and extend
    `test_updater.py::test_parse_feed_valid` to pin it.
23. **Icon/version drift**: converge the three logo sources (`gen_logo_assets.py` uses
    `DeepFlux.png`, `gen_macos_icon.py`/`app_macos.spec` and the in-app taskbar icons use
    `DeepFlux5.png`; AGENTS.md mentions `DeepFlux4.png`) and delete the unreferenced
    `DeepFlux22/23.png`; render the static "DeepFlux 7.2" strings in
    `gui/help_dialog.py:50,731` and `iptv/opensubtitles.py:26` from `APP_VERSION`; fix the
    stale `packaging/verify_boot.py:6-7` docstring (Play tab exists on Linux since the port).
24. **Optional hardening**: deny-list destructive built-ins (`del /s`, `format`, registry
    edits) in the confirmation-gated `run_shell` tool (`agent/tools.py:4232,4253`);
    move `BENCHMARK_REPORT.md`/`benchmark_models.py`/`benchmark_results.json` out of the
    tracked root or untrack them; delete stale setup exes from `dist/` after publish;
    drop the retired `-macOS-arm64.dmg` pattern from `website/api/download.js`.

## Verified clean (no action needed)

- 7.2 is consistent everywhere (config.py, installer.iss, README, Help dialog, OpenSubtitles
  UA, website EN+ZH, both feeds); the deployed exe and AppImage **byte-match** their
  `latest.json` feeds (size + sha256 verified with Get-FileHash).
- All required `collect_all`s present in the Windows spec (yt_dlp, curl_cffi, ddgs, primp,
  fake_useragent, cryptography, nacl, argon2, faster_whisper, ctranslate2, onnxruntime,
  tokenizers, huggingface_hub); VC-runtime hardening in place.
- Updater: strict feed parsing, sha256-verified apply, no-UAC downgrade path, sidecar
  resume, NOT exposed as an agent tool — 32 tests.
- Telemetry: payload exactly `{id, v, os}`, silent-failure contract, opt-out, stopped on
  shutdown — 8 tests pin the payload.
- PINNED files fresh (Jackett v0.24.3009, FlareSolverr v3.5.2, llama b12065); installer seed
  minimal and valid; Jackett→FlareSolverr→launch [Run] ordering correct.
- Website API functions support both Upstash env-var names and degrade gracefully;
  `.vercel/project.json` scope correct; analytics tag present.
- Durable rules: atomic config writes, streamed caps for agent/RSS/.torrent fetches,
  credential redaction, v2 torrent keying, RSS single-worker guard, ranged-file
  O_CREAT semantics, pure-fullscreen invariants, font/theme QSS discipline, 1px borders,
  responsive floor caps — all verified in code AND pinned by passing tests.
- Security posture: zero `eval`/`exec`/`pickle`/`os.system`/hardcoded secrets/bare excepts,
  zero debug leftovers or TODO markers in app code, no main-thread sleeps or `input()`.

## Suggested fix order

1. P0-1 + P0-2 (one-line import fixes + 2 regression tests) — ~15 min.
2. P0-6 + P0-7 (llama pinning/cleanup + delete `.mimosa`) — before the next build/deploy.
3. P0-3, P0-4, P0-5 (settings import, rss_monitor escaping, splitter) — small GUI diffs
   with tests.
4. P1 batch (caps, requirements.txt, mac spec, i18n dups, deprecation) — one focused PR.
5. P2 process items as time allows (lint pass is one `ruff --fix` + review).

---

## IMPLEMENTED (2026-11-06, all in the recommended order)

Everything above was applied to the working tree (uncommitted — review with
`git diff`). Highlights:

- **P0**: `_html` NameError fixed + tests; `shutil` NameError fixed + test;
  settings import now atomic (`_write_bytes_atomic`) with live in-memory
  reload (`_reload_config_in_place`); `rss_monitor` feed name escaped;
  splitter respects saved ratios; llama bundling pinned (fetch cleanup,
  spec ships only PINNED names + the pin file, `_pick_bundled_zip` selects
  the pinned asset, stale zips deleted); `website/.mimosa*` deleted,
  `.vercelignore` added (production verified 404 on `/.mimosa/`).
- **P1**: streamed byte caps everywhere (http_client `read_capped`/`get_text`,
  site grabber, generic extractor, torznab, Jackett `t=indexers`, PyPI
  probe, Wikipedia about-fetch, OnlyHumans hub `_read_capped_json`);
  requirements.txt + pyproject.toml synced; mac spec nacl/argon2
  `collect_all`s; i18n duplicate keys fixed (`in use` vs `active`); libtorrent
  deprecation resolved via `session_stats`; ffmpeg remux now kills
  silent-hung processes (pump thread + deadline); `_list_torrent_sources`
  redacts credentials.
- **P2**: ruff clean (0 violations; 69 auto-fixed + manual F841/F821/E741
  fixes + noqa annotations); git tags v7.0/v7.1/v7.2 created; merged
  `app`/`linux-port`/`macos-port` branches deleted locally AND on origin;
  pre-commit hook now tracked in `.githooks/` via `core.hooksPath`;
  benchmark scratch untracked; CI: macos-15, push triggers dropped,
  checkout/setup-python/upload-artifact bumped to v5/v6/v5; new
  tests/test_scheduler.py + dialog smoke tests; uninstaller asks before
  wiping `~/.deeptorrent`; `parse_feed` whitelists `.exe`/`.AppImage`;
  logos converged on `DeepFlux.png` (orphan `DeepFlux22/23.png` deleted);
  Help/OpenSubtitles versions now travel from `APP_VERSION`; AGENTS.md
  updated (Room invariants, PowerShell-safe gate commands, logo source);
  stale `dist/` installers deleted (kept 7.2, ~2.3 GB freed); download.js
  dmg pattern dropped; verify_boot docstring corrected; `run_shell` gains a
  destructive-command deny-list.

**Bonus bug found by the new tests**: `TokenBucketThrottler.acquire(n)`
with `n > capacity` deadlocked forever (the refill caps at capacity, so
`tokens >= n` never fired) — any engine chunk write larger than the
configured rate hung the monitor thread. Fixed (oversized requests are
granted once the bucket is full) + regression-pinned.
