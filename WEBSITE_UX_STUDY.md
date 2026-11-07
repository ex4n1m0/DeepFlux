# DeepFlux Website — UX / HID Study & Simplification Proposal

Date: 2026-11-06 · Scope: `website/deepflux/index.html` (EN), `website/deepflux/zh/index.html` (ZH), `website/index.html` (locale redirect), `website/api/*` (read-only for this study)
Status: **implemented 2026-11-06** (Phases A, B and C — both languages; see the commit for the exact diff). The proposal below is kept as the rationale record.

---

## 1. What the site is today

One landing page, two languages, three download surfaces, five download buttons.

| Zone | Content |
|---|---|
| Topbar (sticky) | brand → `/`, nav (Capabilities · Install · Privacy · GitHub), language pill, primary Download pill |
| Hero | eyebrow "AI agent · built in", H1 "Super Intelligent Download Tool", 260px logo, one-line copy |
| CTA | `Download for Windows` (primary) + `Linux (AppImage)` (ghost), meta row (Free · no account · v7.4 · OS), live stats widget |
| Shot 1 | framed real capture: **Download page** (Ubuntu/Debian ISOs, Blender archive) |
| "The agent" | 3 bullets + framed **agent chat** capture |
| Capabilities | 6-tile bento: Torrent engine (span2), Embedded browser, Player, IPTV & EPG, Downloads, "…and more" (span2) |
| Install | AI-key note + Windows/Linux tabs (3 steps + sha256 with Copy) |
| Trust | 3 pillars under kicker "By design" |
| Footer | sibling-projects row, BuyMeCoffee icon, copyright |

What the site does **well** (keep): real app captures instead of mockups, honest SmartScreen warning, descriptive alt text with the exact torrent names, per-platform checksums, skip-link + focus-visible + reduced-motion support, ARIA tabs with arrow keys, graceful stats degradation, zh page self-hosts fonts for China access.

---

## 2. Findings

### F1 — The download action is repeated five times
Nav pill, hero ×2, and one button in each install panel = 5 download buttons. The hero row and the Install panels offer the same files with the same prominence, so the page reads as "download, download, download" instead of "here is what this software does". The owner already removed the closing CTA band for this exact reason (2026-10-21 redesign) — the duplication moved, it didn't disappear.

### F2 — Capabilities are under-promoted
The strongest, most differentiating features are the quietest content on the page:

- **Local AI** (uncensored open-weight models on your own GPU via bundled llama.cpp, 6.0+): only half a line inside the Install note.
- **Open source** (MIT, public GitHub): never stated anywhere.
- **Voice input**: buried inside the "…and more" tile.
- **RSS auto-downloads, the community Room, the dual-pane file manager**: all in the catch-all tile.
- **Jackett indexer search + FlareSolverr**: only as "optional: check Jackett" in installer step 2.
- **Auto-update**: only in the panel sub-lines.

### F3 — "…and more" is an anti-pattern
One tile is a catch-all hiding four real features. A catch-all tile is a signal that the grid ran out of room — and every feature inside it gets zero scannable identity.

### F4 — Stale in-app path in the Install note (accuracy bug)
The note says **File → API Keys**. The app has no such path. Verified in `gui/main_window.py:2299` (`settings_menu = menubar.addMenu(tr("Settings"))`) and `gui/main_window.py:2357` / `gui/settings_dialog.py:697`: the real path is **Settings → AI & API Keys → AI Agent Key…**. Both the menu name and the item name are wrong on the site; a user following the site's instructions hits a dead end. Affects EN and zh.

### F5 — Brand logo links bounce users to the wrong language
- EN page brand → `/` (`website/deepflux/index.html:685`). `/` locale-sniffs (`website/index.html:8-19`): on a zh-locale browser, clicking the brand **from the English page** silently jumps to the Chinese page.
- zh page brand → `/deepflux/` (`website/deepflux/zh/index.html:695`): clicking the brand from the Chinese page **forces English**.

Both should go to their own language's home. This is a real HID/wayfinding bug, not a nit.

### F6 — The differentiator appears below the fold
The first image a visitor sees is the download-page capture. The agent chat — the one thing competitors don't have — sits mid-page, after the bullets. The agent section (bullets + chat shot) should come immediately after the hero CTA; the download-page capture works better as proof in the engine story below.

### F7 — Marginal contrast + tiny type for small caps
`--ghost` is white at 0.52α and `--faint` 0.62α over `#050507`. Effective contrast is ≈5.6:1 and ≈7.8:1 — passes AA, but the ghost-tier is used for **uppercase micro-labels**: section kickers (0.72rem, 0.22em tracking), checksum labels (0.7rem), tab subtitles (0.66rem), meta row (0.82rem). Small tracked caps need more margin than 4.5:1; bumping ghost → 0.66α and the smallest sizes one step is cheap legibility insurance.

### F8 — Touch targets under 44px on mobile
At ≤640px the nav Download pill is ~31px tall, the checksum Copy button ~26px, the tabs ~36px. Apple/Google HID guidance is ≥44px (40px absolute floor). No `:active` state exists anywhere — touch users get zero press feedback.

### F9 — Nav label and section kicker disagree
Nav says "Privacy", section kicker says "By design" (zh: 隐私 / 设计如此). Mild cognitive dissonance for a user who clicked "Privacy".

### F10 — Copy repeats itself (owner's standing rule: "reduce the copy, stop repeating")
- "Free · no account" (meta row) vs "No account, no lock-in" (trust pillar).
- The approval theme appears three times (agent bullet "Asks first", trust pillar "You approve everything", the live-stats tooltip).
- "stream while you download" appears in both the Torrent engine tile and an agent bullet.

### F11 — EN page depends on Google Fonts while the fonts already ship locally
The EN page loads Inter + Space Grotesk from `fonts.googleapis.com` (render-blocking, two third-party origins, unreachable in mainland China) — while `website/deepflux/fonts/Inter-var.woff2` and `SpaceGrotesk-var.woff2` are already deployed and the zh page already self-hosts them. Self-hosting the EN page costs zero bytes and removes a privacy/performance dependency. Both fonts are SIL OFL — licensing is already satisfied by the files in the repo.

### F12 — Four orphan assets deploy to production
`DeepFluxBanner.webp`, `GitHub.png`, `Linked.png`, `X.png` are never referenced by any page (grep-verified) but ship on every deploy. Delete them (or, if a footer icon row is wanted back, re-use `GitHub.png` next to BuyMeCoffee — one icon, not three dead ones).

### F13 — Keyboard/HID polish items
- Esc does not close the language `<details>` (needs a 3-line JS handler).
- The live-stats widget updates silently — add `role="status"` so screen readers announce it without focus stealing.
- The tab-list and copy-button JS is duplicated verbatim in both HTML files (fine for a static site, noted only because any JS change must land in both).

---

## 3. Proposed changes

Split into **Phase A** (safe, no design risk, landable immediately) and **Phase B** (the simplification + capability-promotion restructure). Every copy change must land in **both** language files.

### Phase A — accuracy, links, fonts, contrast, touch

**A1. Fix the Install-note path (F4).**
EN: `Settings&nbsp;&rarr;&nbsp;AI &amp; API Keys&nbsp;&rarr;&nbsp;AI Agent Key…`
ZH: `设置 → AI 与 API 密钥 → AI 智能体密钥…`

**A2. Fix brand hrefs (F5).**
EN `index.html:685` → `href="/deepflux/"`; zh `zh/index.html:695` → `href="/deepflux/zh/"`.

**A3. Self-host fonts on EN (F11).**
Copy the two `@font-face` blocks from the zh `<head>` (they already exist there) into the EN page, drop the `fonts.googleapis.com` stylesheet + both preconnects. Update the EN `--font-display`/`--font-body` stacks to include the same CJK fallbacks as zh (harmless, and keeps fallback identical across pages).

**A4. Delete orphan assets (F12).**
Remove `website/deepflux/DeepFluxBanner.webp`, `GitHub.png`, `Linked.png`, `X.png` from the deploy dir. Optionally add a GitHub icon to the footer row beside BuyMeCoffee using the existing `GitHub.png`.

**A5. Contrast + micro-type bump (F7).**
```css
--ghost: rgba(238, 244, 255, 0.66);   /* was 0.52 */
--faint: rgba(238, 244, 255, 0.72);   /* was 0.62 */
.section-head .kicker { font-size: 0.75rem; }
.tab small { font-size: 0.68rem; }
.tile p { font-size: 0.875rem; }
.checksum .lbl { font-size: 0.72rem; }
```

**A6. Touch targets + press feedback (F8).** In the ≤640px media query:
```css
.btn-nav, .tab, .checksum .copy, details.lang > summary { min-height: 44px; }
a.btn:active, button.btn:active, .tab:active, .checksum .copy:active {
  transform: translateY(1px) scale(0.99);
}
```

**A7. Esc closes the language menu (F13).**
```js
document.querySelectorAll('details.lang').forEach(function (d) {
  d.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && d.open) { d.removeAttribute('open'); d.querySelector('summary').focus(); }
  });
});
```

**A8. Live stats announcement (F13).**
Add `role="status"` to the `<span class="live">` element (implicit aria-live=polite).

### Phase B — simplify while promoting capabilities

**B1. Reorder the story: agent first (F6).**
New order: Hero → CTA → **Agent section (bullets + chat capture)** → **Engine section (download-page capture + one caption line)** → Capabilities → Install → Trust → Footer.

- The agent chat becomes the first image on the page — the differentiator leads.
- The download-page capture moves below with a one-line caption, e.g.
  EN: *"The engine at work — official Ubuntu and Debian ISOs plus a Blender archive in one queue."*
  ZH: *"引擎在工作 —— 官方 Ubuntu、Debian 安装镜像与 Blender 压缩包，同队列并行下载。"*
- No screenshots need retaking — only HTML order changes, so the captures stay honest.
- Mark the first image `loading="eager" fetchpriority="high"`; the second stays lazy.

**B2. De-duplicate the copy (F10).**
- Meta row: `Free &middot; open source &middot; v7.4 &middot; Windows 10/11 &amp; Linux` (drops "no account" — the pillar says it; adds "open source", the site's best unclaimed trust signal). ZH: `免费 · 开源 · v7.4 · Windows 10/11 与 Linux`.
- Torrent engine tile keeps its line; the agent bullet drops the duplicated "streams while it downloads" clause (see B3).
- Kicker "By design" → **"Privacy by design"** (F9). ZH: `隐私优先设计`. Nav stays "Privacy"/隐私.

**B3. Agent bullets — tighter, and voice input gets named (F2).**
EN:
1. **Searches and fetches.** Indexers and the web — then downloads, or streams as it goes.
2. **Runs the app.** Browser, queue, player, files — by chat or voice.
3. **Asks first.** Installs, deletions and downloads wait for your yes.

ZH:
1. **搜索并获取。** 索引站和全网都能搜 —— 找到就下，也可以边下边播。
2. **替你操作应用。** 浏览器、队列、播放器、文件 —— 打字或语音，一句就办。
3. **先问再动。** 安装、删除、下载，每件事都要你先点头。

**B4. Capabilities: 6 mixed tiles → 8 equal tiles (F2, F3).**
Drop the two `span2` tiles and the "…and more" catch-all. A uniform 4×2 grid is simpler CSS (one `repeat(4, 1fr)`, no span rules in the media queries) and every surface gets a named tile:

| # | EN title | EN line | ZH title | ZH line |
|---|---|---|---|---|
| 1 | Torrents | Full BitTorrent engine — stream while it downloads, control it by chat. | 种子下载 | 完整 BitTorrent 引擎 —— 边下边播，一句聊天就能控制。 |
| 2 | Direct downloads | HTTP, HLS/DASH and YouTube — resumable, segmented parallel speed. | 直接下载 | HTTP、HLS/DASH 与 YouTube —— 断点续传、分段并行加速。 |
| 3 | Player | Dolby Vision, SVP motion, subtitles, MilkDrop visuals. | 播放器 | 杜比视界、SVP 补帧、字幕、MilkDrop 可视化。 |
| 4 | Live TV | IPTV with a full EPG guide — live channels, movies, series. | 直播电视 | IPTV 附完整 EPG 节目单 —— 直播频道、电影、剧集。 |
| 5 | Browser | Chromium with private tabs and bookmarks. | 内置浏览器 | Chromium，支持隐私标签页与书签。 |
| 6 | Local AI | Run open-weight models on your own GPU — uncensored and offline. | 本地 AI | 在自家 GPU 上运行开放权重模型 —— 无审查、可离线。 |
| 7 | Community | The Room — chat with the community from inside the app. | 社区聊天室 | Room —— 在应用内与社区聊天。 |
| 8 | Feeds & files | RSS auto-downloads and a dual-pane file manager. | 订阅与文件 | RSS 自动下载 + 双栏文件管理器。 |

New tiles 6–8 replace the "…and more" tile and promote the three quietest differentiators (local AI, open source is covered in the meta row, community, RSS/files). The two span2 tiles return to regular width, so the old "big" tile CSS can be deleted.

**B5. Install section: one loud CTA on the page (F1).**
- Keep the hero CTA row as the page's only primary download buttons.
- Demote the two panel buttons to `btn-ghost btn-panel`.
- Collapse each checksum into a `<details><summary>Verify sha256</summary>…</details>` (keeps the security value for the 1% who verify, removes 60px of noise for the 99%).
- Keep the SmartScreen step — it's essential for unsigned builds and it's honest.

**B6. Metadata refresh.**
Add "open source" to `meta description`, `og:description`, `twitter:description` (both languages). Consider regenerating `DeepOG6.jpg` from the logo source if any hero visuals change later (none change in this proposal).

### Phase C — optional (owner decisions)

- **C1. Platform-aware primary CTA.** ~15 lines of JS: if the visitor's UA reports Linux, swap the hero primary button to the AppImage (label "Download for Linux") and make Windows the ghost; same for the nav pill. Progressive enhancement — no-JS visitors see the current default. This makes the #1 action correct for every visitor instead of 98% of them. Risk: counter URLs are already per-file, so no API change.
- **C2. Footer GitHub icon** next to BuyMeCoffee (uses the existing `GitHub.png` before deletion, see A4).
- **C3. "Try asking it" chip row** under the hero CTA with three copyright-free sample prompts (see §4) that are click-to-copy. Promotes the agent concretely; costs one row.

---

## 4. Copyright-free content policy

Rules for any example content in copy, screenshots, or prompts (all examples below verified copyright-free):

**Allowed example artifacts (use only these):**
- OS installers: Ubuntu / Debian / Fedora official ISOs.
- FOSS software: Blender, Godot, Inkscape, Krita, LibreOffice, ffmpeg, VLC.
- Public-domain video: NASA TV streams (US government work).
- IPTV: the iptv-org Macau free-to-air playlist (the app's own seeded sample).
- Local AI: llama.cpp (MIT), generic "open-weight models".
- Fonts: Inter and Space Grotesk (both SIL OFL, already bundled in the repo).

**Banned patterns (owner rule, keep enforcing):**
- Movie or CC-film names in screenshots — Big Buck Bunny / Sintel / Tears of Steel "scene" naming read as warez even though the films are CC-licensed.
- No stock-photo hero imagery (the real-capture rule, 2026-09-25).
- No icon-font CDNs; the inline SVG icons are self-drawn and stay.

---

## 5. Deployment & QA notes

- Both HTML files must change in lockstep; the page JS is duplicated in both, so A7/A8/C1 land twice.
- Deploy ritual (unchanged): copy new `DeepFlux<X>.Setup.exe` / AppImage into `website/deepflux/`, update `latest.json` / `latest-linux.json`, then `vercel --prod --yes --scope timedivision` from `website/`. Delete old binaries from the dir (the dir IS production).
- Do not touch `website/api/*` (no API change is required by any proposal).
- Manual smoke after deploy: tab keyboard arrows, checksum copy, language dropdown + Esc, live-stats hide-on-failure, hreflang tags, mobile 390px layout, brand-link behavior on a zh-locale browser.
- If the hero image order changes and any capture ever gets retaken: `python _site_shots.py <en|zh> _site_shots_out` — keep the Ubuntu/Debian/Blender content standard.

## 6. Owner-decision items (nothing here is assumed)

1. B1 reorder — agent capture above the download capture.
2. B4 — the 8-tile grid replaces the current span2 bento ("Local AI" as a first-class tile).
3. B5 — demote Install-panel buttons + collapse checksums.
4. C1 — platform-aware CTA (JS on a static page).
5. C3 — sample-prompt chip row.
