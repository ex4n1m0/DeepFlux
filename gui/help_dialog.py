"""Help dialog — comprehensive guide to DeepFlux's features and logic."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from gui.window_sizing import roomy


HELP_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<style>
  body {
    font-family: 'Inter', 'Segoe UI', Arial, sans-serif;
    color: #ffffff;
    background-color: #0a0a0f;
    font-size: 20px;
    line-height: 1.6;
    padding: 8px;
  }
  h1 { color: #2a7abf; font-size: 30px; border-bottom: 3px solid #1a2a4a; padding-bottom: 6px; }
  h2 { color: #2a7abf; font-size: 24px; margin-top: 20px; }
  h3 { color: #80f0ff; font-size: 20px; margin-top: 12px; }
  p { margin: 4px 0; }
  ul { margin: 4px 0; padding-left: 20px; }
  li { margin: 2px 0; }
  code { color: #a8edff; background-color: #0d1117; padding: 1px 4px; border-radius: 3px; font-size: 18px; font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace; }
  table { border-collapse: collapse; width: 100%; margin: 6px 0; }
  th { color: #2a7abf; text-align: left; border-bottom: 3px solid #1a2a4a; padding: 4px 6px; }
  td { border-bottom: 3px solid #111827; padding: 4px 6px; }
  .note { background-color: #0d1117; border-left: 3px solid #2a7abf; padding: 6px 10px; margin: 8px 0; border-radius: 0 4px 4px 0; }
  .warn { background-color: #0d1117; border-left: 3px solid #ffcc00; padding: 6px 10px; margin: 8px 0; border-radius: 0 4px 4px 0; }
</style>
</head>
<body>

<h1>DeepFlux 4.1 — User Guide</h1>

<p>DeepFlux is an AI-powered download manager with a built-in browser, media player,
community chat room, and file manager. The AI agent can search for and download
torrents, find direct downloads, manage your library — and drive the app itself:
it can navigate the browser, manage the download queue, play IPTV content, and
organize files, all with your confirmation for sensitive actions.</p>

<h2>Getting Started</h2>

<ol>
  <li><b>Add your API key:</b> File → API Keys: <i>AI Agent…</i> (pick the
      DeepSeek endpoint, or DeepSeek via OpenRouter), adjust the base URL if
      needed, and paste your key. Without one, the agent runs in offline
      demo mode.</li>
  <li><b>Set up Jackett (optional but recommended):</b> Download → Jackett Settings.
      Jackett connects to torrent indexers and gives the agent structured search results.
      DeepFlux starts it automatically when it's not running.</li>
  <li><b>Optional search keys:</b> add a Brave or Perplexity API key (environment
      variables <code>BRAVE_API_KEY</code> / <code>DEEPSEEK_API_KEY</code> also work).
      Web searches then query DuckDuckGo, Brave, and Perplexity in parallel and
      merge the results — Perplexity adds a synthesized answer summary.</li>
  <li><b>Start searching:</b> Type what you're looking for in the Agent tab input and
      press <code>Enter</code>. The agent will search, pick the best result, and
      ask before downloading.</li>
</ol>

<p><b>Window size:</b> the window can be made as small as you like. On narrow
windows, less-used toolbar buttons collapse into a <b>⋯</b> menu button (on the
Play tab, the Command tab's function-key bar, and the download lists) and
the player's transport row switches to compact buttons — everything stays
reachable, nothing disappears.</p>

<h2>Tabs</h2>

<table>
  <tr><th>Tab</th><th>What it does</th><th>Agent control</th></tr>
  <tr><td><b>Browse</b></td><td>Full Chromium browser (HTML5 fullscreen video works — ESC exits).
      Downloads route to the download manager with a notification; <code>.torrent</code> links add automatically.</td><td>Navigate, tabs, read pages, click, fill forms, bookmarks</td></tr>
  <tr><td><b>Agent</b></td><td>AI chat with voice input (🎤). The agent greets you automatically on every start. <code>Enter</code> = agent search; <code>Ctrl+Enter</code> = quick web search.</td><td>—</td></tr>
  <tr><td><b>Download</b></td><td>Torrents on top, download manager below. Auto-focused whenever a transfer is added.</td><td>Full queue control: list, pause, resume, retry, cancel</td></tr>
  <tr><td><b>Play</b></td><td>IPTV and media player — live TV, movies, series, and your own local media folders.</td><td>Search playlist, play, pause, stop, volume, EPG</td></tr>
  <tr><td><b>Command</b></td><td>Dual-pane file manager (Double Commander style).</td><td>Browse, copy, move, rename, delete, new folder</td></tr>
  <tr><td><b>Room</b></td><td>DeepFlux Room — serverless community chat (peer to peer; the first person to join hosts it).</td><td>—</td></tr>
</table>

<p>Switch tabs with <code>Ctrl+1</code>–<code>Ctrl+6</code>, or click a page button in the
menu bar: <b>Browse</b>, <b>Agent</b>, <b>Download</b>, <b>Play</b>, <b>Command</b> and
<b>Room</b> are pure page buttons — a click always takes you to that page. All commands
live under the two real menus, <b>File</b> and <b>Help</b>.</p>

<h2>Using the Agent</h2>

<p>Type naturally — the agent understands plain English:</p>

<ul>
  <li>"Download a recent movie in 1080p" — searches and downloads</li>
  <li>"Find a direct download link for a free video editor" — finds and queues direct HTTP downloads</li>
  <li>"Pause everything over 50GB" — batch operations</li>
  <li>"What's the status of my downloads?" — shows torrents AND the download queue</li>
  <li>"Retry the failed download" / "cancel it and delete the partial file"</li>
  <li>"Limit torrent download speed to 2 MB/s" — live session rate limits</li>
  <li>"Open a browser tab with a torrent page" — drives the browser</li>
  <li>"Read this page and click the 24.04 download link" — sees rendered pages (JS, logins)</li>
  <li>"Play a news channel" / "what's on now?" — IPTV playback and EPG</li>
  <li>"Organize the files in my downloads folder" — file operations</li>
  <li>"Subscribe to this RSS feed" — manages feed subscriptions</li>
  <li>"Find me some IPTV playlists and add them" — searches the web for public
    M3U playlists, validates them and adds them as Play-tab sources</li>
</ul>

<h3>Letting the agent set the app up</h3>
<p>The agent can configure anything you could have typed into a dialog yourself
— it always asks for confirmation first, and changes are saved to your
config just like the settings dialogs do:</p>
<ul>
  <li><b>IPTV sources</b> — "add this M3U URL as a source", "disable the second
    playlist", "remove that provider". The Play tab reloads immediately.</li>
  <li><b>API keys</b> — "here's my TMDb key: …" writes it. Keys are write-only:
    the agent can set or clear one but can never read existing values back.</li>
  <li><b>Settings</b> — "set the artwork cache to 5 GB", "turn off EPG", "use
    VLC as the player". Secret fields stay write-only via the key tool.</li>
  <li><b>Search sources</b> — add/remove torrent indexers, then use them
    right away.</li>
</ul>

<p>The agent searches in this order: Jackett (structured results) → source web searches →
RSS feeds → generic web search. If a quick search finds nothing, it automatically
escalates to a full sweep of all sources. Web search queries every configured
provider in parallel (DuckDuckGo always, plus Brave and Perplexity when their keys
are set) and merges the results — a Perplexity key adds a synthesized answer.</p>

<h3>Voice input</h3>
<p>The 🎤 button next to the agent input toggles dictation: click to start
recording (it turns red), click again to stop — the transcript is sent to the
agent automatically (or left in the input box if the agent is busy, so you can
review it first). Recognition is 100% local via faster-whisper — no cloud, no
API key, no audio ever leaves your PC. The speech model downloads once on first
use into <code>~/.deeptorrent/models/</code>. Tune it in
<code>~/.deeptorrent/config.json</code> under <code>voice</code>: <code>model</code>
(tiny / base / small / medium / large-v3 — bigger = more accurate, slower),
<code>language</code> (empty = auto-detect), and <code>auto_send</code>.</p>

<h3>Safety &amp; confirmations</h3>
<p>Actions with real-world side effects ask for your confirmation first: starting
downloads, deleting files, cancelling a download (which deletes the partial file),
playing IPTV content, clicking or submitting forms in the
browser, and every app-setup change (adding sources, writing keys, changing
settings). Read-only actions (searching, listing, status checks) run immediately.</p>

<div class="note">
<b>Tip:</b> Jackett gives much better results than web searches. Install it and add
your indexers in Download → Sources → Fetch from Jackett.
</div>

<h2>Downloading</h2>

<h3>Torrents</h3>
<ul>
  <li>Paste a magnet link or open a <code>.torrent</code> file (File menu, or drag &amp; drop).</li>
  <li>Completed video torrents show ▶ — double-click to play. Right-click for more options.</li>
  <li><b>Stream while downloading:</b> right-click a video torrent → "Stream while downloading".
      Playback starts as soon as a safe buffer is downloaded. If your connection is slower
      than the video's bitrate, DeepFlux measures both and pre-buffers the difference first —
      and during playback it never reads past the verified download frontier (no artifacts),
      prioritizing the pieces around the playhead.</li>
</ul>

<h3>Direct Downloads</h3>
<ul>
  <li>Paste a direct file URL in the Download panel, or ask the agent to find one.</li>
  <li>HLS (<code>.m3u8</code>) and DASH (<code>.mpd</code>) streams are captured automatically.</li>
  <li>The engine splits large files into segments for faster downloads.</li>
  <li>Servers that don't report a file size still download fine — the row shows an
      animated busy bar and a live "downloaded so far" counter.</li>
  <li>Interrupted downloads resume automatically.</li>
</ul>

<h3>List Columns</h3>
<p>In the torrent, download and segment lists — and in the Command tab's dual
file panes — columns auto-size so file names are always fully visible, and the
name column stretches to fill the window. Every column is also draggable — drag
any column edge to your preferred width and it stays put; double-click a column
edge to hand it back to auto-sizing. Hover a name for the full text as a
tooltip.</p>

<h3>Browser Downloads</h3>
<p>The built-in browser intercepts downloads automatically and routes them to the
internal segmented download manager — you'll get a notification and the Download
tab comes into focus. The browser's session cookies are carried over, so downloads
behind logins keep working. <code>.torrent</code> files are the exception: they use the
browser's own download (private trackers need the full session) and are added to
the torrent engine on completion.</p>

<h2>Play Tab (IPTV &amp; Media Library)</h2>

<ul>
  <li><b>Layout:</b> the tab opens player-first — the video area gets the
      full width. The <b>🗂 Tree</b> and <b>🖼 Content</b> toolbar buttons
      show/hide the category tree and the posters/channels pane; each pane
      reopens at the width you last dragged it to.</li>
  <li><b>Sources:</b> Play → Playlist Sources — an M3U URL or file, an Xtream
      Codes login, or a local media folder (your own movies/series library,
      scanned and poster-matched like any provider). All enabled sources load at
      once; the sidebar tree is Source → Section → Category.</li>
  <li><b>Year grouping:</b> Movies and Series sub-group by release year under each
      category (2026, 2025, …), newest first; titles with no known year sit under
      "Others". The "Group" picker above the tree switches back to flat categories.</li>
  <li><b>Browsing:</b> single click opens the info panel; double-click plays.
      Grid and list views, and a search box that filters across everything.</li>
  <li><b>Folder search:</b> toggle <b>📍 Folder</b> next to the search box and
      the query runs only inside the tree folder you clicked last (a category,
      year, section, or a whole source — subfolders included). The placeholder
      shows the active folder, and while scoped, clicking other folders
      re-filters them with the same query. Toggle off for playlist-wide
      search.</li>
  <li><b>Downloading VOD:</b> movies and series from your IPTV sources can be
      saved to disk. Right-click a movie or series (grid or list view), or use
      the <b>⬇ Download</b> button in the info panel; in a series' episode
      list, right-click an episode to download it or its whole season. Whole
      series/seasons ask first. Downloads run in the Download Manager (same
      place as browser and agent downloads): single files go to its default
      folder, episodes into a folder named after the series. HLS/DASH streams
      are captured and remuxed to MP4; the source's User-Agent/Referer
      settings are applied.</li>
  <li><b>Covers:</b> posters and channel logos fill in automatically while you
      browse, and a deliberately slow background sweep looks up the rest (the
      "Finding artwork n/N" counter in the status bar) to stay within API limits.
      The poster grid adapts to the window — between 3 and 9 covers per row
      depending on the space available: widen the window to see more covers,
      shrink it to see bigger ones.</li>
  <li><b>EPG:</b> now/next info for live channels when the playlist declares a
      guide URL — or set one per source in the source settings.</li>
  <li><b>Refresh resilience:</b> if a provider refresh fails, the last good channel
      list stays on screen, marked as cached data.</li>
</ul>

<h2>Media Player</h2>

<h3>Multiview — 4×4 grid</h3>
<p>The <b>▦ 4×4</b> button on the player's tool row replaces the video area
with a 4×4 grid of independent tiles. Each tile plays its own clip with its
own audio — every tile starts <b>muted</b>, so nothing blasts when you fill
the grid; unmute the ones you want with the 🔇 in the tile's strip.</p>
<ul>
  <li><b>Load a tile:</b> right-click an empty tile → <i>Assign local
      file…</i>, or <i>Assign IPTV channel…</i> (searches your playlists).
      Only <b>one</b> tile can carry a live stream at a time — providers
      reject multiple simultaneous connections; the other 15 are local
      files.</li>
  <li><b>Bulk fill:</b> right-click an empty tile → <i>Fill empty tiles with
      files…</i> — multi-select files and they're dealt to the empty squares
      in alphabetical order (numbered episodes line up).</li>
  <li><b>Folder auto-rotate:</b> right-click any tile → <i>Play folder
      (auto-rotate)…</i> — the folder's first 16 videos fill the grid, and
      whenever a tile's clip ends it automatically starts the next unplayed
      video from the folder. Every video plays exactly once; when the folder
      is exhausted, each tile frees itself as its last clip ends.</li>
  <li><b>Per-tile controls</b> (the strip under each tile): ⏸/▶ play/pause,
      🔇 mute, a volume slider, ⛶ to move that clip to the main player
      (full controls: tracks, subtitles, recording), and ✕ to clear the
      tile. A manually cleared tile stays empty; the rotation only reacts
      to clips that actually end.</li>
  <li><b>Skip all:</b> the bar under the grid has ⏪ −60s / ⏪ −10s /
      ⏩ +10s / ⏩ +60s buttons — every loaded clip jumps by that amount,
      and repeated presses keep going (each clip is clamped to its own
      start/end).</li>
  <li>Tiles run with no post-processing and minimal buffering, so sixteen
      play at once without weighing each other down. Starting playback in
      the main player (or promoting a tile) leaves grid mode and stops all
      tiles.</li>
</ul>

<ul>
  <li><b>Dolby Vision &amp; HDR:</b> Dolby Vision (profiles 5/7/8) and HDR10 play
      with correct colors and dynamic tone mapping out of the box (mpv backend).
      The VLC backend is available in Play → Playback for compatibility.</li>
  <li><b>Audio tracks:</b> click 🎧 in the control bar (or press <code>#</code> to cycle).</li>
  <li><b>Subtitles:</b> click CC in the control bar (or press <code>J</code> to cycle; "Off" is in the list).</li>
  <li><b>Download subtitles:</b> CC menu → "Find subtitles online…" searches
      OpenSubtitles.com and loads the chosen file into the playing video. Local
      files match by exact file hash; streams search by title. Saved next to the
      video (or in <code>~/.deeptorrent/subtitles/</code> for streams) so they
      auto-load next time. Requires a free API key — Play → Subtitles &amp;
      Languages; adding your account credentials raises the daily quota.
      The agent can do it too: "load English subtitles" auto-picks the best
      match (exact hash match &gt; your preferred language &gt; most downloaded).</li>
  <li><b>Preferred languages:</b> Play → Subtitles &amp; Languages.
      Multi-track files automatically switch audio/subtitles to your languages
      when playback starts.</li>
  <li><b>Music visualizer:</b> audio files play with a MilkDrop (Butterchurn)
      visualization instead of a black screen — the spiral button picks a preset,
      and you can drop your own <code>.milk</code> files into
      <code>~/.deeptorrent\presets</code>.</li>
  <li><b>Smooth motion:</b> video is locked to your screen's refresh rate, and
      Play → Playback can blend frames to remove judder (files and VOD only —
      live TV uses the plain sync it needs to avoid restarts). This smooths
      <i>mismatched</i> frame rates; it does not invent new frames, so a 24 fps
      film on a 120 Hz screen already lines up perfectly and looks unchanged.
      For true high-frame-rate motion, see SVP below.</li>
  <li><b>SVP motion interpolation (soap-opera effect):</b> turns 24 fps film
      into genuinely high-frame-rate video by generating intermediate frames
      (measured: 23.976 → 119.88 fps). This needs
      <a href="https://www.svp-team.com/">SVP 4</a>, a separate paid program
      (~$25 one-off, 30-day free trial) — nothing is bundled, and DeepFlux
      works exactly as before without it. Install SVP 4 <b>including its mpv
      player component</b>, then tick "SVP motion interpolation" in
      Play → Playback; it applies to the next file you play. Works with local
      files, VOD and live TV, and costs GPU power. If SVP isn't found the
      option stays greyed out, and if it ever fails DeepFlux falls back to
      normal playback.</li>
  <li>Aspect ratio cycles with <code>A</code>; <code>F</code> or double-click for fullscreen.</li>
</ul>

<h2>Site Grabber</h2>
<p>The magnifier button in the browser toolbar searches <b>any video site</b> by
keywords. The <b>Site</b> box is prefilled with the site open in the browser (paste
any address on a site to switch); type keywords and press Search. The grabber
finds the site's search page by itself (its search form or common URL patterns —
the pattern that worked is shown, and you can type your own using
<code>{query}</code>), lists the results (thumbnail, duration, title), and, on
Download, resolves each video page to the stream it embeds — including streams
hidden in obfuscated player scripts — and queues it in the download manager as a
normal HLS/DASH/file job. Failures stay checked so you can retry just those.</p>
<p>Prefer zero clicks? Turn on <b>Auto-queue</b>: every search (and every
page you browse to) resolves and queues up to the <b>max</b> limit
automatically — keywords in, downloads out. The setting is remembered.</p>

<h2>Chrome Extension</h2>
<p>The Chrome extension sends downloads from your external browser to DeepFlux.
Install it from the <code>_internal\chrome_extension\</code> folder in your install
directory (Chrome → <code>chrome://extensions</code> → Developer mode → Load unpacked).</p>

<h2>Settings</h2>

<p>Everything lives in the <b>File</b> menu, flat — grouped into zones, each
opened by a separator line and a bold header (API Keys, Browser Settings,
Download, Play) with its items slightly indented; no submenus to dig
through. The Bookmarks folder tree is the one exception — it lives on the
browser toolbar's Bookmarks button.</p>

<table>
  <tr><th>File section</th><th>What to configure</th></tr>
  <tr><td><b>API Keys</b></td><td>One page per service (AI Agent, Torrent &amp;
      Web Search, Movie &amp; TV Metadata, Adult Metadata, Subtitles): LLM
      endpoint, base URL and key, Jackett, Brave, Perplexity, TMDb,
      OpenSubtitles. Plus file associations, and <b>Export/Import
      Settings</b> — a passphrase-encrypted <code>.dfc</code> backup of
      everything (keys, sources, all settings) to keep safe or move to
      another PC. Import applies after a restart; your previous settings are
      kept as <code>config.json.bak</code>.</td></tr>
  <tr><td><b>Browser Settings</b></td><td>Homepage, privacy/data, History, Save Page as PDF, Developer Tools.</td></tr>
  <tr><td><b>Bookmarks button</b> (browser toolbar, left of Private)</td><td><b>Import Bookmarks…</b> / <b>Export Bookmarks…</b> and your whole bookmark folder tree in one popup — bookmarks live here, not in the File menu.</td></tr>
  <tr><td><b>Download</b></td><td>Add Magnet / Add Torrent File, <b>Jackett Settings</b> (URL; Test Connection syncs your indexer list), the Download Settings pages (Torrent Downloads, Torrent Queue, Download Manager — save paths, bandwidth limits, connections), <b>Sources</b> (which sites the agent searches), and <b>RSS Feeds</b> (subscriptions — or just ask the agent).</td></tr>
  <tr><td><b>Play</b></td><td>Four small pages: Playlist Sources, Metadata &amp; Cache (artwork cache, EPG), Subtitles &amp; Languages (preferred audio/subtitle language), and Playback (backend, decoding, buffer, smooth motion, SVP interpolation, throttling).</td></tr>
</table>

<h2>DeepFlux Room — Community Chat</h2>

<p>The <b>Room</b> page is the <span style="color:#a8edff">DeepFlux Room</span>,
the app's community chat. It uses no chat server: the <b>first person to
join hosts the room inside the app</b>, and everyone else connects to them
directly, peer to peer. When the host leaves, another member automatically
takes over, so the room keeps living.</p>

<ul>
  <li><b>Joining:</b> open the Room page, type a nickname, press
  <b>Join</b>. You are never joined automatically — every session starts at
  this bar. <b>Leave</b> is the same button.</li>
  <li><b>Finding the host:</b> a tiny pointer on deepflux.space says who is
  currently hosting (address + timestamp only — <b>no messages, ever</b>).
  The "Host…" button next to the nickname skips that and connects straight
  to an address you type (<code>ip:port</code>) — handy on tricky networks,
  or when discovery is down.</li>
  <li><b>Encryption:</b> in the downloaded setup build every message is
  sealed with a key that ships only inside the app (AES-GCM, per-message
  keys, tamper-proof). Builds compiled from the GitHub source have no key
  and their lounge runs unencrypted — a separate room that never sees the
  encrypted one. Sealed means sealed on the wire: anyone holding the same
  app can read the room; it is a group key, not a secret channel.</li>
  <li><b>Making the room private:</b> anyone in the room can press
  <b>Make private…</b> to generate a fresh random key that is handed only
  to the people in the room at that moment. From then on, people who join
  later cannot read — or even find — this room; they get a separate, empty
  lounge. The private room lives as long as its members stay connected
  (the rotated key is held in memory, never written to disk), and it
  survives its host leaving — the next member re-hosts it under the same
  private key.</li>
  <li><b>Hosting notes:</b> the host's app tries to open a port on your
  router automatically (UPnP) and shows the address others can use in the
  status line — right-click that line → <b>Copy room address</b>. Windows
  may ask once to allow DeepFlux through the firewall — the room works best
  if you allow it. No port open? Joiners on your local network still reach
  you, and anyone can use a direct address.</li>
</ul>

<h2>Privacy</h2>

<p>DeepFlux works fully offline if you want it to. Two things talk to the
internet by default:</p>

<ul>
  <li><b>Anonymous usage ping</b> — while the app runs, a tiny ping is sent to
  deepflux.space every few minutes so the website can show a live "users
  online" count. It carries only a random install id (a file on your disk, not
  tied to you), the app version and your OS name — nothing else: no username,
  no machine name, no file names, no browsing or download activity. Turn it
  off in File → Download → Download Manager Settings.</li>
  <li><b>Update checks</b> — a daily check for an outdated YouTube downloader
  (warn-only). Same settings page.</li>
</ul>

<p>The DeepFlux Room adds one more, only while you use it: the discovery
pointer described above (a sealed host address — chat itself never touches
any server).</p>

<p>Everything else — searching, downloading, playing, the agent — only uses
the network when you ask it to.</p>

<h2>Keyboard Shortcuts</h2>

<table>
  <tr><th>Key</th><th>Action</th></tr>
  <tr><td><code>Ctrl+1</code>–<code>Ctrl+6</code></td><td>Switch tabs (Browse / Agent / Download / Play / Command / Room)</td></tr>
  <tr><td><code>Ctrl+F</code></td><td>Jump to Agent input</td></tr>
  <tr><td><code>Ctrl+M</code> / <code>Ctrl+O</code></td><td>Add magnet / open torrent file</td></tr>
  <tr><td><code>Ctrl+T</code> / <code>Ctrl+W</code></td><td>New / close browser tab</td></tr>
  <tr><td><code>Ctrl+L</code></td><td>Focus URL bar</td></tr>
  <tr><td><code>Enter</code> / <code>Ctrl+Enter</code></td><td>Agent search / quick web search</td></tr>
  <tr><td><code>Space</code> / <code>M</code> / <code>F</code></td><td>Player: pause / mute / fullscreen</td></tr>
  <tr><td><code>A</code> / <code>J</code> / <code>#</code></td><td>Player: aspect ratio / subtitles / audio track</td></tr>
  <tr><td><code>F5</code> <code>F6</code> <code>F7</code> <code>F8</code> <code>F2</code></td><td>Command: copy, move, new folder, delete, rename</td></tr>
  <tr><td><code>Ctrl+Q</code></td><td>Quit</td></tr>
</table>

<h2>Troubleshooting</h2>

<div class="warn">
<b>Agent not responding:</b> Check your API key in File → API Keys.
</div>

<div class="warn">
<b>No search results:</b> Make sure Jackett is running and your sources are enabled
(Download → Sources). Without Jackett, the agent falls back to slower web searches.
</div>

<div class="warn">
<b>HLS download fails:</b> The stream is likely DRM-protected. DeepFlux only supports
non-DRM or AES-128 encrypted streams.
</div>

<div class="warn">
<b>Chrome extension not connecting:</b> Make sure DeepFlux is running. Reinstall if
the native host wasn't registered.
</div>

<h2>File Locations</h2>

<table>
  <tr><td><code>~/.deeptorrent/config.json</code></td><td>All settings</td></tr>
  <tr><td><code>~/.deeptorrent/memory/</code></td><td>Agent's persistent notes</td></tr>
  <tr><td><code>~/.deeptorrent/models/</code></td><td>Voice recognition model (whisper)</td></tr>
  <tr><td><code>~/Downloads/DeepFlux/</code></td><td>Default download folder</td></tr>
</table>

<h2>Version</h2>
<p>DeepFlux 3.8 — AI Deep Search</p>
<p>AI via DeepSeek / OpenRouter / custom · Web search via DuckDuckGo, Brave, Perplexity (parallel)</p>

</body>
</html>
"""


class HelpDialog(QDialog):
    """Comprehensive help/guide dialog for DeepFlux."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DeepFlux — User Guide")
        self.setMinimumSize(520, 360)
        roomy(self)
        self.setStyleSheet("QDialog { background-color: #0a0a0f; }")

        layout = QVBoxLayout(self)

        browser = QTextBrowser()
        browser.setHtml(HELP_HTML)
        browser.setOpenExternalLinks(True)
        browser.setStyleSheet("""
            QTextBrowser {
                background-color: #0a0a0f;
                color: #ffffff;
                border: none;
            }
        """)
        layout.addWidget(browser)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #111827;
                color: #ffffff;
                border: 3px solid #1a2a4a;
                padding: 2px 16px;
                border-radius: 6px;
                font-size: 20px;
            }
            QPushButton:hover {
                background-color: #1a2a4a;
                border: 3px solid #2a7abf;
                color: #2a7abf;
            }
        """)
        layout.addWidget(close_btn)


class AboutDialog(QDialog):
    """Simple About dialog with version info."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About DeepFlux")
        self.setMinimumWidth(400)
        self.setStyleSheet("QDialog { background-color: #0a0a0f; color: #ffffff; }")

        layout = QVBoxLayout(self)

        from PySide6.QtWidgets import QLabel
        title = QLabel("DeepFlux")
        title.setStyleSheet("color: #2a7abf; font-size: 36px; font-weight: 700;")
        layout.addWidget(title)

        version = QLabel("4.1 — AI Deep Search")
        version.setStyleSheet("color: #ffffff; font-size: 21px;")
        layout.addWidget(version)

        desc = QLabel(
            "AI-powered download manager with browser, player, community room, and file manager.\n\n"
            "AI via DeepSeek / OpenRouter / custom\n"
            "Web search via DuckDuckGo, Brave, Perplexity (parallel)"
        )
        desc.setStyleSheet("color: #8a9ab0; font-size: 18px;")
        layout.addWidget(desc)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #111827;
                color: #ffffff;
                border: 3px solid #1a2a4a;
                padding: 2px 16px;
                border-radius: 6px;
                margin-top: 12px;
            }
            QPushButton:hover {
                background-color: #1a2a4a;
                border: 3px solid #2a7abf;
                color: #2a7abf;
            }
        """)
        layout.addWidget(close_btn)
