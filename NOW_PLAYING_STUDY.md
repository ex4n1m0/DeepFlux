# Now-Playing Title — UI / UX / HID Study (windowed Play tab)

Date: 2026-11-02 · Scope: where to show "what is playing" (file name / movie
title) on the Play tab when NOT in fullscreen.

## 1. Problem

When a local file plays, nothing on screen says what it is. The user cannot
answer "what am I watching?" without opening the browse panes and hunting for
the highlighted tile.

## 2. Diagnosis — the data exists, every display path drops it

The player ALWAYS knows the title at play time:

- Every played item is a model object with a name: `Channel.display_name`
  (`tvg_name or name`), `Movie.name` (+ year/rating from the metadata
  pipeline), local files get `Channel(name=os.path.basename(path))` in
  `play_file`, folder scans produce clean names incl. "<show> SxxEyy"
  (`iptv/local_folder.py::_display_name`), web streams carry a title.
- `PlayerWidget._current_item` holds it for the whole session.

Every place that could show it, doesn't (or only for a moment):

1. **Status bar** — `play_file()` writes "Playing <name>"
   (`gui/iptv_tab.py:4754`), but the backend emits a state change ~1 s later
   and `_on_player_state` OVERWRITES it with "Player: playing"
   (`gui/iptv_tab.py:4126`). Also 17 px dim grey `#8a9ab0`, at the very
   bottom of the tab, shared with loading/artwork messages. Peripheral
   vision, no hierarchy, transient.
2. **Detail panel** — has the real metadata (poster, year, synopsis) but
   `_play_item()` HIDES it the moment playback starts (`gui/iptv_tab.py:4716`).
3. **Control bar** — two rows (transport + tools), no title label anywhere
   (`PlayerWidget._build_ui`, gui/iptv_tab.py:279-453).
4. **Window title** — static branding "DeepFlux <ver> - AI Deep Search"
   (`gui/main_window.py:952`), never updated on playback. Native title bar
   (DWM-dark), so the OS title text is real and free to use.
5. **mpv OSD** — disabled (`osc=False`, no osd-level/show_text anywhere).

## 3. Chrome map (windowed mode, default player-first layout)

```
┌──────────────────────────────────────────────────────────────────┐
│ Toolbar   [Source▾][⟳][📍][search................][▦][≡][▶ Open][⚙]│
│ Chips     [Live TV][Movies][Series][Favorites][Recent]           │
│ ┌──────────────────── Player pane (default: whole tab) ────────┐ │
│ │                                                              │ │
│ │                    VIDEO SURFACE                             │ │
│ │                    (no title anywhere on it)                 │ │
│ │                                                              │ │
│ │ [⏪][⏸][⏹][⏩][────seek────][00:00/00:00][🔇][──][Auto][🎧][CC][⛶]│ │  ← ctrl row
│ │ [Start Recording][Sleep: Off][⛔ Stop All][Compact][▦4×4][🖥 Pure]│ │  ← tools row
│ └──────────────────────────────────────────────────────────────┘ │
│ Status    "Player: playing" (dim, transient)     [artwork ∙ prog]│
└──────────────────────────────────────────────────────────────────┘
```

Visibility discipline that any new element must respect (from AGENTS.md +
code):
- Regular fullscreen: control bar auto-hides, chrome rows hide via
  `_on_player_fullscreen` tuple; pure fullscreen: control bar gone for good
  (`controls.hide()` in `_hide_controls`); compact PiP: a fixed `secondary`
  tuple of widgets is hidden (`_set_compact_controls`); multiview replaces
  the surface.
- Responsive floor: tab must stay shrinkable ≤ ~700 px
  (`test_responsive.py::test_tabs_stay_shrinkable`); the control panel is a
  `ResponsiveRow(min_width=580)` so a label inside it cannot lock the window
  wide — but it must be elided, never left to its natural width.
- Dynamic labels get `shrink_label` / manual elide; tooltips carry the full
  text.

## 4. HID criteria used to judge placements

1. **Association (Gestalt proximity)** — the title must read as belonging to
   the video, not to the app chrome around it.
2. **Glance distance** — fewest eye-movement degrees from the point of
   attention (center of video) to the answer.
3. **At-rest persistence vs. occlusion** — windowed mode has spare chrome
   space; nothing needs to cover the picture. Occluding overlays are the
   fullscreen answer, not the windowed one.
4. **Lane consistency** — control rows are "action" lanes; the status bar is
   a system-log lane; navigation (chips/toolbar) is a wayfinding lane. "What
   is playing" is state info and deserves its own anchor, not a squat in
   someone else's lane.
5. **OS-level surfaces** — the window title is the only info that survives
   outside the window: taskbar hover, alt-tab, taskbar thumbnail. Free
   awareness when minimized.
6. **Mode interactions** — must behave in: windowed, regular fullscreen
   (auto-hide reveal), pure fullscreen, compact PiP, multiview, idle.

## 5. Options considered

**A. Title row as the first row of the control-bar panel (below video,
above transport)** — title (white, semibold) + dim secondary segment
(" · file.mkv", " · News Hour 18:30–19:00" via the existing
`_fmt_now_title`), elided right, full text as tooltip.
+ Rides the existing dark panel: styling, auto-hide in fullscreen (title
  then reveals WITH the controls — the Netflix reveal, for free), hidden in
  pure mode by `controls.hide()`, can join the compact `secondary` tuple.
+ Below-video is the strongest player convention (YouTube windowed, Win11
  Media Player, SMPlayer info bar): video → title → timecode is one eye
  lane; "what" sits next to "where" (the 00:00/00:00 display).
+ Inside `ResponsiveRow(min_width=580)` → cannot grow the window floor.
+ Zero new chrome zones to police in `_on_player_fullscreen` (the panel is
  not in the hide tuple — it manages itself).
− Costs ~26–30 px of video height (video_stack shrinks when the row
  appears); only when something plays — hide the row when idle so the panel
  doesn't carry an empty strip.

**B. Title row above the video, inside the player pane**
+ Same association quality; never covered by the detail overlay.
− Same height cost; needs its own background/visibility wiring (would have
  to join fullscreen/compact handling itself); duplicates what the control
  panel already provides for free. No advantage over A.

**C. On-video overlay, fade after ~4 s (VLC marquee / Netflix pattern)**
+ Biggest possible font, zero layout cost, works in every mode, reuses the
  `buffer_badge`/`pure_hint` styling pattern.
− In windowed mode the title is then INVISIBLE most of the time — you must
  wiggle the mouse to recall it. Fails "at-rest persistence" for the exact
  scenario asked about (windowed). Right answer for fullscreen/pure, wrong
  one as the primary windowed placement.

**D. Window title: "DeepFlux <ver> — <title>"**
+ Zero UI cost; taskbar hover + alt-tab + thumbnail show the title even when
  the app is minimized — no in-window placement can do that. Classic desktop
  player behavior (VLC, MPC-HC, WMP).
− Far from the video, truncated by the OS on long names; weak association.
  Great companion, weak primary. Restore the static title on stop/idle.

**E. Status-bar left segment ("Now playing: …" persistent, state text moves
right)**
+ Smallest change; already the place a first-time implementer reaches.
− Bottom of the tab, dim 17 px, system-log lane → fails hierarchy; when the
  browse panes are open the player column ends far above it. As the ONLY
  fix it repeats today's problem with better punctuation.

**F. A chip in the section-chips row / toolbar text**
− Navigation/branding lanes; farthest from the video; misuses wayfinding
  chrome. Rejected.

## 6. Precedent survey (what other players do in windowed mode)

- VLC: window title + optional brief on-video title at start.
- MPC-HC / PotPlayer: window title; on-screen info on demand.
- mpv: window title templated from media-title; OSD on interaction.
- SMPlayer (closest cousin — Qt + mpv): window title + optional info bar
  under the video.
- YouTube (windowed): title block directly BELOW the video, persistent.
- Win11 Media Player: persistent title above/left of the video, fades only
  in fullscreen.
- Netflix / Plex: no windowed chrome — but they are fullscreen-first apps;
  their overlay-fade pattern maps to our fullscreen, not our windowed mode.

Consensus: windowed mode = persistent text adjacent to the video (above or
below) + the window title; overlay-fade is the fullscreen treatment.

## 7. Recommendation

**Primary — Option A**: a one-line "now playing" strip as the FIRST row of
the existing control-bar panel:

```
│ …video…                                    │
│ ▶ Now Playing   The Batman (2022) · The.Batman.2022.1080p.mkv… │  ← new, elided, tooltip = full path
│ [⏪][⏸][⏹][⏩][────seek────][00:00/00:00][🔇]… │
```

- Text: `<display_name>` in white semibold, then a dim `#8a9ab0` segment:
  local file → file name (tooltip: full path); folder source → clean name
  (tooltip: relative path); channel → EPG now-line if any
  (`_fmt_now_title`, already built for the loading status); movie/series →
  year/rating once the metadata pipeline has them; web stream → given title.
- Set in `PlayerWidget.play()` (it owns `_current_item`), clear + hide in
  `stop()`; single QLabel with rich text or two labels in a row with a
  trailing stretch; manual `fontMetrics().elidedText` on resize; tooltip
  carries the untruncated text (elision's HID partner).
- Visibility: nothing new to wire — it lives inside `controls`, which
  already auto-hides in fullscreen, vanishes in pure mode, and compact mode
  just needs the label added to the `secondary` tuple in
  `_set_compact_controls`.
- i18n: tr() the "Now playing" prefix; ZH guide + HELP_HTML update if
  shipped as a release feature (keep-user-guide-current rule).

**Companion — Option D**: on playback-started set
`DeepFlux <ver> — <title>` as the window title, restore the static title on
stop. Buys taskbar/alt-tab awareness for free; do it in the same change.

**Later / optional — Option C**: the fade-in top-left overlay for
fullscreen/pure (appear on track change and on control-bar reveal, fade
after ~4 s). Do not ship it as the windowed answer; add it when fullscreen
titling is asked for.

**Rejected**: B (duplicates A with extra wiring), E (hierarchy fail —
keep the status bar for state/log), F (wrong lane).

## 8. Implementation checklist (when green-lit)

- [ ] QLabel pair in `controls`' QVBoxLayout above `ctrl` row; elide on
      resize; tooltip; hide when idle.
- [ ] Populate from `_current_item` in `play()`; enrich: EPG now-line,
      movie year/rating, file name vs display name.
- [ ] Add to `_set_compact_controls` secondary tuple.
- [ ] Window title set/restore (playback started / stop / error-idle).
- [ ] No `_on_player_fullscreen` tuple change needed (inside player).
- [ ] Tests: title shows on play (local file + channel + EPG), elides,
      clears on stop, hidden in compact, floor test still green.
- [ ] HELP_HTML EN + ZH; version-bump ritual if released.
