# Agent video-grabbing study — why stream picking is slow, and how to fix it

Date: 2026-11-17 · Branch: `svp-out-of-process` (post-7.9) · Status: STUDY ONLY — no code changed.

Owner trigger (three asks, this session):
1. The agent finds and sorts videos fine, but then takes a very long time to do
   what a human does instantly: **pick the correct stream to download** from a
   video source.
2. The agent **should be able to see all tools/capabilities that exist in the
   software** — deep-study that direction.
3. When a site with videos is open in the built-in browser, the **video grabber
   detects them — the agent should be able to operate on those detections** for
   automated tasks.

---

## TL;DR verdict

The agent is slow at stream picking because it is playing the game **blind,
amnesiac, and with one hand tied**:

* **Blind** — the repo already contains a good generic stream resolver
  (`page_scan` + `GenericExtractor`: packed-JS unpacking, ranking,
  preview/trailer demotion, master-playlist detection) and an even smarter
  in-browser detector (the video grabber extension: per-site player globals,
  network interception, itag→quality labels) — **neither is reachable from any
  agent tool**. The agent's only lens is `web_fetch`'s href regex, which sees a
  flat, undifferentiated list of ≤10 URLs with no type/quality/ranking, and
  sees **nothing** when the stream lives in player JS.
* **Amnesiac about quality** — a human reads the player's quality menu. The
  agent gets URL strings. No resolution, no bandwidth, no size, no "this is
  the master playlist, that's a 480p variant".
* **One hand tied** — even a correctly picked URL fails: the agent's
  `add_download` drops the page context (Referer/UA headers, cookies) that
  every GUI lane passes, so hotlink-protected CDNs answer 403 and the agent
  misreads that as "wrong stream", retrying other URLs — **each retry costs a
  user confirmation** (add_download is confirm-gated), so latency compounds.

The fixes are cheap because ~90% of the machinery already exists and is
battle-tested in the GUI: expose the resolver as a read-only tool, add one
compound download tool that carries page context, and bridge the extension's
detections to Python. Details and ranked proposals below.

---

## 1. Current state — three disconnected lanes

```
┌─ GUI lane (works well) ────────────────────────────────────────────────┐
│ Site Grabber dialog (gui/grabber_dialog.py)                            │
│   search() → cards → resolve() → engine.add_stream_job(                 │
│       headers={Referer, UA}, referrer=page_url, source_url)             │
│ Video grabber extension (dlmgr/browser_extension.py, in-app browser)    │
│   detects {url, type, title, quality} in page JS → user clicks ⬇ →     │
│   browser_channel.sendDownload → cookies+referrer+YouTube routing      │
└─────────────────────────────────────────────────────────────────────────┘
┌─ Agent lane (weak) ────────────────────────────────────────────────────┐
│ web_search → web_fetch (href regex → download_links, flat) →           │
│   add_download(url) → engine bare call (NO headers/referrer/cookies)   │
└─────────────────────────────────────────────────────────────────────────┘
```

Key code facts (all verified this session):

| Fact | Where |
|---|---|
| `web_fetch` extracts stream/file links with a bare `href="….(mp4\|m3u8\|…)"` regex, capped at 10, no ranking/metadata | `agent/tools.py:3386-3393` |
| The real scanner (`find_stream_urls`: JSON-escape normalize, recursive p,a,c,k,e,d unpack, `<video>/<source>`, ranking with master hints + `preview/trailer/sample` demotion) is pure string processing, used only by the GUI grabber | `dlmgr/extractors/page_scan.py:169-195`, `_rank` at `:155` |
| `GenericExtractor.extract` resolves direct URLs, Content-Type sniffing, and pages → `{manifest_url, type, title, headers(Referer+UA), page_url}` | `dlmgr/extractors/generic.py:51-115` |
| `SiteGrabber.resolve` is a thin wrapper over it; the dialog queues with full context | `dlmgr/site_grabber.py:370-377`, `gui/grabber_dialog.py:417-429` |
| Agent `_add_download` calls the engine with **only** url/filename/save_path — `add_job`/`add_stream_job` accept `headers/cookies/referrer/source_url/category` and the handler drops them all | `agent/tools.py:2126-2135` vs `dlmgr/engine.py:293,426` |
| The engine already auto-picks the best HLS rendition from a master playlist, honoring `config.stream_max_height` | `dlmgr/engine.py:421-424,446-449`, `dlmgr/hls_dash.py:178-218` |
| The extension detects `{url, type, title, quality}` incl. YouTube itag→quality mapping and master-only dedupe for HLS sites — and **never pushes detections to Python**; `detectedVideos` is page-local JS | `dlmgr/browser_extension.py` (`addVideo` ~:505, quality map ~:357, master dedupe ~:324) |
| The QWebChannel bridge (installed on every page) exposes only `sendDownload/sendPlay/addMagnet/getVersion/ping` — no detections slot | `gui/browser_channel.py:142-263` |
| The bridge's download path is the gold standard: `_browser_cookies_for` same-site cookies, page as referrer, YouTube→`add_youtube_job` routing | `gui/browser_channel.py:159-199` |
| The agent path has **no YouTube routing** — `add_download` on a watch URL would save the HTML page, while the system prompt *advertises* YouTube downloads | `agent/tools.py:2131-2135`, `agent/loop.py:76-77` |
| Prompt doctrine covers torrents/RSS/IPTV/browser in detail; **zero guidance** on stream picking | `agent/loop.py:61-194` |

---

## 2. Root causes of the slow stream picking

**RC1 — The agent cannot see what a human sees.**
`web_fetch`'s `download_links` is a href regex. Streams embedded in player
configuration scripts, JSON blobs, or packed `eval(function(p,a,c,k,e,d))`
blocks — the standard case on modern video sites — produce **nothing**. When
links do exist, they arrive as a flat list where the 1080p master playlist, a
480p variant, `preview.mp4`, `trailer.mp4`, and an unrelated podcast mp3 are
indistinguishable. `page_scan._rank` already encodes the right heuristics;
the agent just never receives their output.

**RC2 — A correct pick still fails without page context.**
Most video CDNs are hotlink-protected: a stream request without the page as
Referer (often plus cookies/UA) gets 403. Every GUI lane passes that context
(`grabber_dialog._queue_video`, `browser_channel.sendDownload` — the
Cloudflare/surrit 403 lesson is literally documented in `sendPlay`'s
docstring). The agent's `add_download` cannot even express it: the schema has
no headers/referrer parameters, and the handler calls the engine bare. So the
job errors, the agent concludes it picked the wrong stream, tries another
URL from the list, and loops.

**RC3 — No quality signal at decision time.**
The agent must choose among URLs with no resolution/bandwidth/size. The
engine would happily choose the best rendition IF given the master playlist —
so the only thing the agent really needs to know is "this one is the master"
(yet nothing marks it), or, for progressive files, "this one is 1080p" (yet
nothing sizes them).

**RC4 — The confirmation economy punishes exploration.**
`add_download` requires explicit user confirmation. A blind pick → confirm →
403 → re-pick → confirm cycle turns one human click into many minutes of
round trips (each retry is a full LLM turn plus a user click).

**RC5 — No compound action.**
The GUI grabber does search→resolve→queue in one worker with zero model
turns. The agent has no tool that combines resolution + context + queueing,
so a one-click human action decomposes into 5–15 model turns.

**RC6 — No doctrine in the prompt.**
Nothing tells the model: prefer master playlists (the engine picks the
rendition), previews are demoted, streams need the page as Referer, ask the
user's quality preference once (memory already has a "Prefers 1080p" example
in the prompt!), or that detections exist when the user is browsing.

---

## 3. Capability inventory — what the software has vs. what the agent sees

Policy context: within the *registry*, full visibility is already the rule —
`tool_names_for_context` returns every tool for every task (user decision
2026-09-15, `agent/tools.py:553-562`). The gap is **registration**: whole
features exist with no tool at all. Inventory (GUI feature → agent status):

| Capability | Implementation | Agent surface | Verdict |
|---|---|---|---|
| Site Grabber (site search → cards → resolve → queue) | `site_grabber.py` + `grabber_dialog.py` | none | **full gap** — core of ask #1 |
| Video grabber detections in the browser | `browser_extension.py` | none (page-local) | **full gap** — ask #3 |
| Context-aware download (cookies/referrer/YouTube routing) | `browser_channel.sendDownload` | `add_download` is strictly weaker | **regression gap** |
| YouTube download | `engine.add_youtube_job` (3 GUI callers) | none; prompt advertises it | **broken promise** |
| HLS/DASH with headers, rendition selection, `stream_max_height` | `add_stream_job` + `_select_stream_rendition` | params dropped | **partial** |
| Stream quality preference | `config.stream_max_height` | settable via `set_settings` (not deny-listed) but undiscoverable | **docs gap** |
| Browser history read | `browser_history.py` | bookmarks only | minor gap |
| IPTV favorites/recent, record, sleep timer, multiview, A/V tracks, speed | `iptv_tab.py` | play/pause/stop/volume/subtitles only | partial; candidates for later |
| yt-dlp update check, app update check | `ytdlp_update.py`, `update_dialog.py` | none | minor |
| Settings encrypted export/import | `config_backup.py` | none | likely deliberate (passphrase+restart) — confirm |
| Jackett/FlareSolverr/local-model setup | `infra/*` | none | setup-time, fine |
| Torrents, organization, RSS, IPTV playback+EPG+subtitles, bookmarks, commander, memory, settings/keys | — | present | OK |

**Recommendation shape:** complete the table's "full gap" and "regression gap"
rows first (proposals P0–P2); the partial/minor rows are follow-ups. Do NOT
chase UI-only surfaces (tray toasts, visualizer, window chrome) — they are
not automatable value.

---

## 4. Proposals (ranked)

### P0 — `scan_video_page` tool (read-only resolver) — kills RC1+RC3

Wrap `GenericExtractor.extract` (the exact code the GUI grabber uses) as an
agent tool:

```
scan_video_page(url) →
{
  title, page_url,
  recommended: {url, type: hls|dash|file|youtube, reason},
  alternates: [ ≤5 × {url, type, demoted_reason?} ],   # page_scan ranking
  renditions: [ ≤8 × {resolution, bandwidth, codecs} ], # when master m3u8: peek via parse_manifest (bounded)
  note: "engine picks the best rendition from a master; pass this URL to download_video"
}
```

* Read-only → no confirmation; member of `UNTRUSTED_RESULT_TOOLS` (page data).
* SSRF guard `validate_public_url` on the URL; body via `http_client.read_capped`
  (5 MiB cap) — same rules as every agent fetch.
* Do NOT put cookie values in the output; the Referer/page_url is enough for
  the agent to pass along as context.
* Cloudflare-walled pages will fail here (plain HTTP client) — that's what P2
  covers; the failure message should say exactly that ("try the browser
  detection lane").

### P1 — `download_video` compound tool — kills RC2+RC4+RC5

```
download_video(url, filename="", save_path="", max_height=0) →
  url: video page OR stream URL OR watch URL
```

Behavior (mirrors `grabber_dialog._queue_video` + `browser_channel.sendDownload`):
1. YouTube/watch URL → `engine.add_youtube_job`.
2. Page URL → resolve via the P0 resolver → stream/file with
   `headers={Referer, UA}`, `referrer=page_url`, `source_url=original url`.
3. Direct stream/file → same context from the tool's referer argument if given.
4. Master playlist → engine auto-rendition (`stream_max_height` /
   `max_height` arg); the result reports which rendition was selected.
5. **One confirmation** for the whole chain; the confirmation preview shows
   title + chosen stream + quality (the user finally sees what a human would
   have seen in the player's quality menu before clicking once).

Optional (cheap, high value): accept a LIST of page URLs — the Site Grabber
dialog's multi-select queue semantics, one confirmation for the batch.

### P2 — Extension detections → Python → agent — ask #3, "video detection on the browser"

The detections with quality labels already exist in page JS; they only need
a bridge:

1. JS side (`browser_extension.py`): on every `addVideo`/dedupe update, call
   `window.deepflux.reportDetections(detectedVideos)` when the channel object
   exists (keep the XHR/control-API path as fallback; it already exists).
2. Python side: new `@Slot` `reportDetections(list)` on
   `BrowserChannelBridge` → MainWindow stores the latest list **per tab**
   (bounded, e.g. last 20) + the page URL. No dialogs, no LLM traffic —
   pure state.
3. Agent tools:
   * `browser_list_videos` (read-only, untrusted-marked): per tab →
     `[{url, type, title, quality, page_url}]`.
   * `browser_download_video(index_or_url)`: routes through the SAME lane as
     `sendDownload` (cookies + referrer + YouTube routing) with normal
     confirmation.

This lane is uniquely powerful for exactly the pages that defeat P0:
Cloudflare-walled or login-gated sites, because the WebView carries the real
session (same physics as the verified Android lesson: the in-page stack has
the cookies and fingerprint; out-of-band HTTP does not). It also covers
"download the video I'm watching right now" with zero search work.

Safety invariants (non-negotiable, consistent with existing rules):
detections are page-controlled data → `_trust: untrusted_external_content`;
URLs still pass `validate_public_url` before hitting the engine; cookie
VALUES never enter LLM-visible output; downloads stay confirm-gated (YOLO
mode already provides the unattended variant, and it can never be toggled by
the agent).

### P3 — `web_fetch` stream awareness (drive-by upgrade)

When `web_fetch` already holds the HTML, run `page_scan.find_stream_urls` on
it and return a ranked, typed `streams` field (demote hints applied) instead
of relying on the href regex for streams (`download_links` stays for
archives/installers). Pure string processing, no extra fetch, linear-time
(audited 2026-11-09), removes a whole tool call in the common case.

### P4 — Prompt doctrine + capability discovery — ask #2

* New SYSTEM_PROMPT section "Video grabbing": use `scan_video_page` →
  `download_video`; prefer master playlists (engine picks the rendition);
  ask the user's quality preference once and `save_memory` it (the prompt's
  own "Prefers 1080p" example becomes real); when the user mentions a page
  they have open, use `browser_list_videos` first.
* Extend `agent_diagnostics` with a capability map (per-domain availability
  flags + counts + the deliberate GUI-only exceptions), so the model can
  answer "what can you do here" truthfully from live state instead of the
  prompt prose — the same pattern as the platform-conditional prompt blocks.
* Register the Site Grabber's search (`video_site_search`) for parity with
  the dialog — optional, lower priority since find+sort already worked for
  the owner via web_search.

### Sequencing

P0+P1 together (one PR, one test file) → immediate win on ask #1.
P2 second (JS + bridge + two tools) → ask #3, biggest new power. P3+P4 ride
along with either. Expected effect on the owner's scenario: from ~5–15
turns-with-confirmations down to **1 resolve + 1 confirmed download** (or
zero extra turns when detections already exist in the open browser tab).

---

## 5. Test plan (repo conventions: per-domain files, chunk-safe splits)

* `tests/test_agent_video_tools.py` — scan_video_page: fixture pages (plain
  hrefs; packed-JS embedded master; preview/trailer demotion; direct-URL and
  content-type sniffing; untrusted marker; SSRF rejection). download_video:
  YouTube routing, page→headers/referrer pass-through (engine stub asserting
  kwargs), master→rendition note, single-confirmation payload, batch URLs.
* `tests/test_browser_video_detect.py` — bridge `reportDetections` (direct
  slot call, offscreen), per-tab storage/bounds, `browser_list_videos` shape
  + untrusted marker, `browser_download_video` lane reuse, cookie-value
  never in tool output.
* No changes to `site_grabber.py`/`page_scan.py` behavior expected; if P0
  reuses `SiteGrabber.resolve` verbatim, `tests/test_site_grabber.py`
  already pins it.
* Release gate: the standard chunked pytest invocation (AGENTS.md).

## 6. Anti-goals

* No per-site hardcoding in Python — the grabber's genericity is a design
  principle; site smarts stay in the extension JS where they already live.
* No bypassing/loosening of confirmation policy — the fix is making ONE
  confirmation cover a correctly-chosen action, not removing the gate.
* No cookie/credential values into LLM-visible output, ever.
* No auto-pushing page detections into the conversation unprompted —
  pull-only (`browser_list_videos`) keeps the prompt-injection surface
  closed until the agent explicitly asks.
* Don't expose UI-chrome operations (tray, visualizer, window management)
  as "capabilities" — automatable value only.

## 7. Open owner decisions

1. Batch confirmations in `download_video` (one click for N videos)? —
   recommended yes, mirrors the Site Grabber dialog.
2. Should `scan_video_page` peek at master playlists (extra manifest fetch)
   to list renditions, or stay fetch-cheap? — recommended peek, bounded to
   top 8, it's one small GET.
3. Settings export/import as an agent tool? — currently none; likely
   deliberate (passphrase + restart semantics). Confirm before registering.
4. Default quality preference source: reuse `config.stream_max_height`
   (engine-wide) vs a per-request `max_height` arg only — recommended both,
  request arg wins, config is the default.

---

## 8. Related UX fix SHIPPED with this study (same session, 2026-11-17)

**No automatic main-tab jumps** (owner: "when the agent is searching for
pages, don't jump to the browser window… automatic tabs jumping are
annoying"). The agent-browsing experience was being wrecked by focus theft:
every `browser_navigate` forced the Browse tab forward, and every queued
transfer forced the Torrents tab. Removed (teeth-verified regression tests):

| Removed jump | Was at | Why it annoyed |
|---|---|---|
| `BrowserBridge._focus()` (navigate / switch-tab / go-home) | `gui/browser_bridge.py` | every agent search/page-read ripped the user off the Agent tab |
| `_on_agent_event` add_* success → Torrents | `gui/main_window.py` | every agent download/magnet yanked mid-chat |
| `_route_browser_download` → Torrents | `gui/main_window.py` | page-started download left the page |
| `_on_browser_download_state` auto-add → Torrents | `gui/main_window.py` | completion-time jump |
| `_on_downloaded_torrent_file` auto-add → Torrents | `gui/main_window.py` | completion-time jump |
| `sendDownload` → Torrents | `gui/browser_channel.py` | extension click left the video page (toast remains) |

  Kept (direct user intent): menus/shortcuts/tab icons, Ctrl+L/find,
  drag&drop, chat-link clicks, Add-Magnet dialog, external `open_target`
  (raises the window), IPTV play focus. Pinned by
  `tests/test_browser_bridge.py` (4 no-steal tests) and
  `tests/test_v5_ui.py::test_agent_event_tool_end_does_not_jump_tabs`;
  durable rule recorded in AGENTS.md.

---

## 9. P0 + P1 SHIPPED (same session, 2026-11-17)

**`scan_video_page`** (read-only, untrusted-marked): wraps
`GenericExtractor.extract` — recommended stream + ≤5 ranked alternates
(new `alternates` field on `extract_from_page`) + HLS master rendition
peek (top 8, Referer-aware, fail-soft) + YouTube short-circuit + a
CF/login failure hint pointing at the browser lane.

**`download_video`** (confirmation class of add_download): resolves →
queues with `headers={Referer, UA}` + `referrer` + `source_url` (the GUI
grabber's exact semantics); YouTube → `add_youtube_job`; per-request
`max_height` caps HLS rendition choice (new `add_stream_job(max_height=)`
+ `DownloadJob.max_height` so restarts keep the cap; per-call beats
`config.stream_max_height`). `add_download` itself now also routes
YouTube URLs to yt-dlp (was: HTML-page save while the prompt advertised
YouTube). Prompt gained a "Video grabbing" doctrine section; `_TOOL_LABELS`
entries added. Tests: 12 new in tests/test_tools.py, alternates pinned in
tests/test_site_grabber.py, rendition caps in tests/test_dlmgr.py —
chunks t / d–g / r–z / loop+memory all green (595 tests).

**LIVE VERIFICATION (owner-requested, missav test site):**
- `missav.ai` (the classic): `scan_video_page` resolves the page → surrit
  MASTER playlist ranked first, 720p/1080p variants as alternates, full
  rendition table (1080/720/480/360 with bandwidths), correct titles.
  `download_video` (max_height=720) then pulled 3 full videos through the
  REAL DownloadEngine at 10–38 MB/s and remuxed to MP4 — the Referer
  context is what surrit demands (the old add_download lane 403s there).
- `missavtv.com` (the clone the owner tried): fully JS-gated — the video
  page and its `embedUrl` player host both serve JS shells with NO m3u8/
  UUID/packed-JS in the static HTML, and the mirror `/t/` links 403 any
  non-browser client even with curl_cffi's Chrome fingerprint. This site
  class is exactly what P2 (extension detections → agent) must cover;
  P0's failure hint already steers the agent there.
