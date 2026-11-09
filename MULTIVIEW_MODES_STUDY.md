# Multiview modes study — add 1×1 (dual, left/right or top/down) and 2×2 beside the 4×4

Date: 2026-11-09 · Scope: Play tab multiview grid · Status: IMPLEMENTED (same
day — see git diff; review fixes folded in: from_file read-back, deferred
dispose, end-to-end combo wiring tests)

## 1. Request interpretation

Two new multiview layouts on the Play tab, functionality identical to the
existing 4×4 grid:

| Request wording | Meaning | Tiles | Geometry |
|---|---|---|---|
| "1x1 … left right" | screen split in TWO, side by side | 2 | 1 row × 2 cols (`1x2`) |
| "1x1 … top down" | screen split in TWO, stacked | 2 | 2 rows × 1 col (`2x1`) |
| "2x2 divide the screen in 4 parts" | four quadrants | 4 | 2 rows × 2 cols (`2x2`) |

A literal single-tile "1×1" cannot be "left right or top down", so the user's
"1x1" is read as **1+1 = a dual split** with a choice of orientation. The dual
mode therefore ships as two layout variants (`1x2` ↔ and `2x1` ↕); picking
between them is a layout switch, same as switching to 2×2 or back to 4×4.

## 2. Current state — how the 4×4 works today

All of the feature lives in **`gui/multiview.py`** with thin glue in
**`gui/iptv_tab.py`** (`PlayerWidget`). Nothing else in the app knows about it
(verified: no references in `gui/main_window.py`; only a doc section in
`gui/help_dialog.py`).

### 2.1 `gui/multiview.py`

- **`GridTile`** (gui/multiview.py:96) — one tile: stable `QFrame` surface +
  a control *strip below* the video (name label, 🔇 mute, ⏸/▶, volume slider,
  ⛶ promote, ✕ clear). Backend is created lazily via
  `create_backend(surface, preferred="mpv", svp=False)` — in-process mpv only
  (the SVP backend's fixed pipe name hosts one instance), smooth-video and
  interpolation forced OFF, tiny cache (`TILE_CACHE_SECS=2`,
  `TILE_CACHE_BYTES=8 MiB`), always starts muted. Context menu on the tile:
  assign local file / bulk fill / folder auto-rotate / assign IPTV channel.
  **Completely layout-agnostic** — it neither knows nor cares how many
  siblings it has.
- **`ChannelPickDialog`** (gui/multiview.py:355) — searches the playlist for
  one channel (live + movies). Layout-agnostic.
- **`MultiViewGrid`** (gui/multiview.py:404) — the only layout-aware piece:
  - `GRID = 4` class constant (gui/multiview.py:407);
  - tile creation loop `for r in range(self.GRID): for c in range(self.GRID)`
    into a `QGridLayout` with equal row/column stretch
    (gui/multiview.py:426-440);
  - everything else already iterates `self.tiles` and is **count-agnostic**:
    single-stream rule (`stream_tile`/`assign_stream`, :479-497), file assign
    (:499), bulk fill (:528-548), folder scan + auto-rotate (:551-608),
    `skip_all` seek-every-tile (:467), promote (:610), parallel shutdown with
    `DESTROY_DEADLINE_SECS` deadline (:619-646);
  - the ONLY places the count is hardcoded: the two `self.GRID * self.GRID`
    slices in `start_folder` (gui/multiview.py:588,591) and docstrings.
- Design constraints recorded in the module docstring (do not "fix"): tiles
  never reparented (native winId), SVP off, one network stream per grid max
  (provider connection caps), all tiles start muted.

### 2.2 `gui/iptv_tab.py` glue (PlayerWidget)

- `mv_btn` — checkable `QPushButton "▦ 4×4"` in the tools row
  (gui/iptv_tab.py:434-440), an **OverflowRow candidate**
  (gui/iptv_tab.py:471-473 — hides into the "⋯" menu when narrow, so it must
  stay a simple checkable button the app never hides itself).
- `_ensure_multiview()` (gui/iptv_tab.py:720) — lazily builds ONE
  `MultiViewGrid`, connects `sig_promote_requested`, adds it as a
  `video_stack` **page** (sibling of the main surface — never a child; the
  surface's HWND must not be recreated).
- `_on_multiview_toggled` / `_set_multiview` (gui/iptv_tab.py:730-756) —
  toggling shows the grid page; leaving grid mode **shuts the grid down**
  (stops + destroys every tile backend) and returns to the surface. The grid
  *widget* survives as a hidden, emptied stack page and is reused next time.
- Exit paths that tear the grid down: `play()` (any main-player playback,
  :903-906), `force_stop_all_streams()` (:1143-1144), tab `shutdown()`
  (:2170-2172), promote (:761).
- `video_stack` fullscreen/pure-fullscreen behavior is inherited by any stack
  page — no per-layout work needed.

### 2.3 Tests — `tests/test_multiview.py` (23 tests)

Fake-backend harness pins: 16 tiles, muted start + tuning order, one-stream
rule, bulk fill order/caps, skip-all wiring, resume-after-assign,
play/pause button states, folder rotation (fill/replace/user-clear/exhaust),
long-filename minimum-size containment, parallel shutdown deadline,
stack-page/never-reparent invariant, promote path, force-stop teardown.
Constructors call `MultiViewGrid(FakeManager(), DeeptorrentConfig())` with no
layout argument — **the parameterized constructor must default to 4×4** to
keep every existing test green unchanged.

## 3. Design

### 3.1 Layout registry (new, in `gui/multiview.py`)

```python
# name -> (rows, cols, button label)
MULTIVIEW_LAYOUTS = {
    "4x4": (4, 4, "▦ 4×4"),
    "2x2": (2, 2, "▦ 2×2"),
    "1x2": (1, 2, "▦ 1×2 ↔"),   # dual, left|right
    "2x1": (2, 1, "▦ 2×1 ↕"),   # dual, top/down
}

def multiview_layout(name: str) -> tuple[int, int]:
    """(rows, cols) for a layout name; unknown names fall back to 4×4."""
```

A single flat registry (not "mode + orientation" objects) keeps dual↔/dual↕
as just two entries — switching orientation is the same code path as
switching mode, and the persisted config value is one string.

### 3.2 `MultiViewGrid(manager, config, rows=4, cols=4, parent=None)`

- Defaults preserve every existing call site and test.
- Replace `GRID` with `self._rows` / `self._cols` in the creation and stretch
  loops; `start_folder` slices with `n = self._rows * self._cols` and its
  docstring says "first N videos".
- Class docstring generalized ("an R×C tile grid"); the module docstring's
  sixteen-specific wording updated, constraints kept verbatim.
- **Everything else is untouched** — that satisfies "all functionalities copy
  the 4×4 mode" by construction: per-tile controls, muted starts, one IPTV
  stream per grid, bulk fill, folder auto-rotate, skip-all bar, promote,
  parallel shutdown. The single-stream rule stays **one stream per grid** in
  every layout (the provider connection cap is about connections, not
  geometry — 2×2 does NOT get two stream slots).
- Performance note: 2/4 concurrent decodes are strictly lighter than 16; the
  tile tuning (tiny cache, no post-processing) is kept identical for
  consistency even though it is no longer forced by necessity.

### 3.3 Live layout switching (the only new *behavior*)

New `PlayerWidget._set_multiview_layout(name)`:

1. Validate via the registry (unknown → ignore, keep current).
2. Persist: `self._config.iptv.multiview_layout = name` (survives restart via
   the normal config save; adding the field is forward/backward-safe because
   every config section constructor filters unknown keys).
3. Update `mv_btn` text/tooltip ("▦ 2×2", "up to 4 tiles…").
4. If a grid exists and is the current stack page: shut it down, remove the
   widget from `video_stack` (`removeWidget` + `deleteLater` — pages must
   not accumulate), null `self._multiview_grid`, `_ensure_multiview()` the
   new one and show it. The checked state of `mv_btn` is untouched, so the
   user stays "in multiview" while the layout swaps.
   Switching = teardown + rebuild, i.e. all tiles stop (consistent with every
   existing exit path: play/promote/leave also stop everything). Carrying
   assignments from the old grid into the new one (first-N tiles) is a
   possible later enhancement, deliberately NOT in v1: it adds state-transfer
   complexity for a rarely-hit path, and bulk-fill/folder-mode re-populate in
   two clicks.
5. If no grid is showing: just remember the layout — the next toggle builds
   it.

`_ensure_multiview()` reads the resolved layout from the instance state
(initialized from `config.iptv.multiview_layout`), so its zero-arg signature
and semantics stay as tests expect.

### 3.4 UI entry points

- **Toolbar `mv_btn` stays a plain checkable QPushButton** (OverflowRow
  constraint — candidates must be widgets only OverflowRow hides, and its
  "⋯" menu calls `click()`; a menu-bearing QToolButton would be awkward
  there). Its label now reflects the active layout; clicking enters/exits the
  last-used layout exactly as today.
- **A "Layout" switcher is added INSIDE the grid's bottom bar** (the slim bar
  under the tiles that already carries the ⏪/⏩ skip buttons,
  gui/multiview.py:444-464): recommended a compact `QComboBox`
  (`4×4 · 2×2 · 1×2 ↔ · 2×1 ↕`) at the left of the bar. Rationale: mode
  switching is a grid-mode concern, the bar is only visible while a grid is
  up (discoverable exactly when relevant), and the toolbar/responsive
  machinery is untouched. The combo inherits the bar's existing
  `rgba(5,7,12,0.9)` styling — no new QSS colors, no `font-size:` literals,
  so no `scale_qss` concerns beyond what the bar already does.
  The grid emits a `sig_layout_requested = Signal(str)` that PlayerWidget
  wires to `_set_multiview_layout` (keeps MultiViewGrid free of PlayerWidget
  knowledge, same pattern as `sig_promote_requested`).
  Alternative (rejected for v1): QToolButton+menu in the toolbar — more
  moving parts inside the OverflowRow contract, and unreachable while the
  button is collapsed into "⋯".

### 3.5 Config

`IPTVConfig.multiview_layout: str = "4x4"` (config.py:479 section). Values:
`"4x4" | "2x2" | "1x2" | "2x1"`; the registry validates at use so a stale or
hand-edited value degrades to 4×4 instead of crashing startup. Harmless if
the agent's settings tool can see it (pure UI preference).

### 3.6 Docs

`gui/help_dialog.py:418-450`: retitle "Multiview — grid", add a Layout bullet
(four layouts, switch live from the bar under the grid, remembered across
restarts), and generalize the count-dependent phrasing ("the other 15 are
local files" → "the rest are local files"; "first 16 videos" → "the first N
videos"). The ⛔ Stop All paragraph already speaks generically of "the
multiview stream tile" — no change.

## 4. Change list (per file)

| File | Change | Size |
|---|---|---|
| `gui/multiview.py` | layout registry + `multiview_layout()`; ctor `rows/cols` params; loop/stretch/`start_folder` generalization; layout combo in the bar + `sig_layout_requested`; docstring updates | ~60 lines |
| `gui/iptv_tab.py` | `mv_btn` label/tooltip from registry; `_set_multiview_layout()`; `_ensure_multiview()` builds current layout; wire `sig_layout_requested`; `__init__` reads config layout | ~50 lines |
| `config.py` | `IPTVConfig.multiview_layout: str = "4x4"` + comment | ~4 lines |
| `gui/help_dialog.py` | multiview section rewrite (§3.6) | ~15 lines |
| `tests/test_multiview.py` | new tests (§5); module docstring tweak | ~120 lines |
| `AGENTS.md` | one bullet under Conventions when implemented | ~6 lines |

No changes to: `gui/responsive.py` (mv_btn stays a candidate, unchanged
type), `gui/main_window.py`, packaging, installer, website.

## 5. Test plan (extend `tests/test_multiview.py`)

Existing 23 tests must pass **unmodified** (defaults = 4×4). New tests, all
on the existing FakeBackend/FakeManager harness:

1. **Tile counts & geometry** — parametrized: `4x4`→16, `2x2`→4, `1x2`→2,
   `2x1`→2; and the QGridLayout actually places tiles at the right (row,
   col): `1x2` tile 1 at (0,1); `2x1` tile 1 at (1,0); `2x2` tile 3 at (1,1)
   (`layout.getItemPosition(layout.indexOf(tile))`).
2. **`multiview_layout()` fallback** — unknown/garbage name → (4,4); all
   four names resolve.
3. **One-stream rule in dual mode** — second tile stream refused, clear
   frees the slot (2-tile grid).
4. **Folder mode caps at N** — 2×2 grid, 6 files: 4 filled (v00-v03), 2
   pending; EOF on tile 0 pulls v04; exhaustion frees tiles.
5. **Bulk fill caps at N** — 2×2, 6 valid files → 4 added.
6. **Live switch via `PlayerWidget._set_multiview_layout`** — with 4×4 shown
   and tiles assigned: switch to 2×2 → every old backend got
   `("stop",)`+`("destroy",)`, old grid removed from `video_stack`
   (`indexOf(old) == -1`), current widget is the new grid with 4 tiles,
   `mv_btn` still checked, its text "▦ 2×2", `config.iptv.multiview_layout`
   == "2x2". Switch while OFF → no grid built, only config/label change.
7. **Combo wiring** — the bar combo's activated signal emits
   `sig_layout_requested` with the right name (grid-level) and reaching
   `_set_multiview_layout` (player-level).
8. **Config default** — fresh `DeeptorrentConfig().iptv.multiview_layout ==
   "4x4"`; unknown persisted value still boots 4×4 via the registry
   fallback.
9. **Repeat-switch page hygiene** — toggle on, switch 4×4→2×2→1×2→2x1:
   `video_stack.count()` stays constant (old pages removed), only the latest
   grid is current.

Also worth adding: dual-orientation visual sanity via
`getItemPosition` already covered by (1); no offscreen-painting test needed.

## 6. Risks & open points

- **Page accumulation** — the one real leak risk of live switching; pinned
  by test 9 (`removeWidget`+`deleteLater`).
- **Tile surfaces are created up-front and never reparented** — rebuilding
  the whole grid widget on switch sidesteps reparenting entirely (fresh
  surfaces, fresh winIds). Consistent with the module's own constraint.
- **Window minimum size** — fewer/bigger tiles only *lower* the grid page's
  minimum; the combo adds ~110px to a bar that sits beside ~4 skip buttons
  already (~600px). `tests/test_responsive.py::test_tabs_stay_shrinkable`
  builds the tab without opening multiview (lazy creation), so no new
  pressure there — but keep the combo narrow (no long translated strings).
- **Stream tile loss on switch** — accepted v1 behavior (see §3.3); every
  pre-existing exit path already stops everything, so nothing new for users
  to unlearn.
- **"16" wording** — docstrings/tooltips/help must not promise sixteen in
  the new modes; the study's test 1 + a help-dialog grep cover it.
- **AGENTS.md release-gate caveat** — the pytest chunk for these tests is
  `loop + memory + m n o` (`tests/test_m*.py`), plus the i-chunk if
  `gui/iptv_tab.py` import surface changes materially. **Python and git are
  currently GONE machine-wide (2026-11-08 drift, see AGENTS.md)** — none of
  the verification commands can run until a Python is reinstalled; this
  study is therefore code-reading only, no commands were executed.

## 7. Suggested implementation order

1. `config.py` field (+ registry in multiview.py) — trivial, isolated.
2. `MultiViewGrid(rows, cols)` parameterization, generalize loops/folder
   count/docstrings (run existing tests — must be untouched-green).
3. Bar combo + `sig_layout_requested`.
4. `PlayerWidget._set_multiview_layout` + mv_btn label + config wiring.
5. New tests (§5), help_dialog rewrite, AGENTS.md bullet.
6. Verify per AGENTS.md split (once Python exists again):
   `python -m pytest tests/test_multiview.py tests/test_responsive.py -q`
   inside the `loop+memory+m-n-o` chunk convention, plus a manual native
   pass: enter each layout, assign files, switch layouts live, promote a
   tile, force-stop, restart app to confirm the layout persists.

Estimated total: ~130 lines of product code + ~120 lines of tests — the
feature is small precisely because the 4×4 was built count-agnostic everywhere
except its constructor loop.
