# IPTV Tab — Developer Documentation

This documents the IPTV subsystem added to DeepFlux: the module map, how
the player backend and metadata pipeline work, and how to add a new metadata
provider.

## Module map

```
iptv/                      Qt-agnostic core (unit-testable, no GUI imports)
  models.py                Dataclasses: Channel, Movie, Series, Episode,
                           PlaylistSource, Playlist, Category. Section constants.
  m3u_parser.py            Streaming M3U/M3U8 parser. Tolerates malformed lines,
                           BOM, #EXTGRP, #EXTVLCOPT. Worker-thread friendly:
                           on_progress + is_cancelled callbacks. Extracts
                           tvg-id/name/logo, group-title, url-tvg, extra attrs.
  classify.py              Live/Movies/Series classification. Rules are data-
                           driven via CLASSIFICATION_RULES (group keywords +
                           URL patterns + extensions). Promotes parsed
                           channels into movies/series and groups series
                           episodes by normalized title.
  xtream.py                Xtream Codes player_api.php client. Fetches live/vod/
                           series categories + content. Credentials never logged.
  cache.py                 SQLite cache (playlists, metadata, EPG, favorites,
                           recent). Per-thread connections, WAL mode.
  artwork.py               Disk artwork cache + async downloader with thumbnail
                           downscaling (Pillow when available). Bounded thread
                           pool; dedupes concurrent fetches of the same URL.
  metadata.py              Metadata pipeline: title cleaning, TMDb + TVmaze
                           providers, iptv-org logos fallback, rate-limited
                           on-demand lookups with SQLite caching.
  epg.py                   XMLTV download + iterparse + cache. Reports now/next.
  player.py                PlayerBackend abstraction + MpvBackend (libmpv via
                           python-mpv) + LibVLCBackend (python-vlc). Bundled-DLL
                           discovery (ensure_mpv_dll_on_path). create_backend()
                           factory with automatic mpv -> VLC fallback.
  manager.py               IPTVManager: orchestrates sources, parsing, favorites,
                           recent, EPG, metadata, artwork. Runs all network/parse
                           work on background threads. The single bridge between
                           the GUI and the rest of the subsystem.

gui/
  iptv_tab.py              IPTVTab widget: toolbar, sidebar (section/category
                           tree), content grid/list (virtualized, lazy artwork),
                           detail panel, embedded PlayerWidget, status bar.
                           _IPTVSignals marshals worker-thread results to GUI.
  iptv_settings_dialog.py  IPTVSettingsDialog + _SourceEditDialog: sources CRUD,
                           TMDb key, cache location/limit, buffering, hwdec,
                           preferred player, EPG toggle.

config.py                  IPTVConfig + IPTVSourceConfig dataclasses; wired into
                           DeeptorrentConfig.from_file / to_file.

packaging/
  app.spec                 PyInstaller spec: bundles iptv modules, mpv/ffmpeg
                           DLLs (packaging/mpv/*.dll), VLC libs, license files,
                           python-mpv / python-vlc / Pillow hidden imports.
  installer.iss            Inno Setup: adds IPTV config section to first-run
                           config.json, a Licenses program-group shortcut, and
                           notes the LGPL bundling in the finish message.
  mpv/README.txt           Where to obtain libmpv + FFmpeg DLLs before building.
  licenses/mpv-ffmpeg-lgpl.txt  LGPL license notices for mpv/FFmpeg/libVLC.

tests/test_iptv.py         21 tests: parser, classifier, title cleaning, xtream
                           (mocked HTTP), cache, manager favorites/search/recent.
```

## Threading model

No network or parsing work runs on the Qt GUI thread:

- `IPTVManager.load_source_async()` spawns a worker thread that downloads/parses
  (M3U) or calls the Xtream API, classifies, persists to SQLite, and applies
  favorites. Progress and completion are reported via `_IPTVSignals` (Qt
  signals emitted on the GUI thread).
- Artwork and metadata lookups run on bounded thread pools
  (`artwork._BoundedExecutor`, `metadata._BoundedExecutor`) with a token-bucket
  rate limiter so a 50k-entry playlist never fires thousands of simultaneous
  API calls. Results are marshalled back via `QTimer.singleShot(0, ...)`.
- EPG download/parse runs on its own daemon thread.
- All background work is cancellable via per-source `threading.Event` flags.

## Player backend

`PlayerBackend` is a small abstract interface (play/pause/stop/seek/volume/mute/
hwdec/cache/aspect/deinterlace/track selection). Two implementations:

- **MpvBackend** — libmpv via `python-mpv`, embedded into a Qt `QFrame` using
  mpv's `wid` embedding. Hardware decoding defaults to `auto-safe` with a
  software fallback. Track lists are read from mpv's `track-list/*` properties.
- **LibVLCBackend** — `python-vlc`, the documented fallback. Embedded via
  `set_hwnd` (Windows). Some advanced options (hwdec/cache/aspect) are best-
  effort no-ops here.

`create_backend(parent, preferred)` tries the preferred backend and falls back
to the other. The mpv DLL is found at runtime by `ensure_mpv_dll_on_path()`,
which prepends the bundled `mpv/` directory (from `_MEIPASS/mpv` in a
PyInstaller build, or `packaging/mpv` in dev) to `PATH` before `import mpv`.

To add a third backend (e.g. a direct FFmpeg/libav renderer), subclass
`PlayerBackend`, implement the transport/settings methods, and add it to
`create_backend()`.

## Metadata pipeline

`MetadataPipeline` resolves metadata **on demand** for visible items only:

1. Compute `metadata_key(section, name, year)` (normalized title + year).
2. Cache hit in SQLite -> immediate callback.
3. Otherwise queue a rate-limited lookup: TMDb first (if API key configured),
   then TVmaze (keyless, series only). Result is cached and the callback fired.

Channel logos: `tvg-logo` is preferred; when missing, `IptvOrgLogos` matches
the channel name against the iptv-org logos index (cached locally, refreshed
weekly). Title cleaning (`clean_title`) strips quality/codec tags, country
prefixes, and season/episode markers before querying APIs.

### Adding a new metadata provider

1. Subclass `MetadataProvider` and implement `fetch(title, year, section) ->
   Optional[dict]`. Return a dict with keys: `title, year, rating, synopsis,
   genres, poster, backdrop, provider`.
2. Instantiate it in `MetadataPipeline.__init__` and add it to the lookup chain
   in `MetadataPipeline._worker()` (e.g. after TMDb, before/after TVmaze).
3. If it needs an API key, add a field to `IPTVConfig` + `IPTVSettingsDialog`
   and pass it into the pipeline via `IPTVManager`.

No other changes are needed — caching, rate limiting, and the GUI callback
path are provider-agnostic.

## Performance notes (50k+ entries)

- The M3U parser streams line-by-line; classification is a single in-place pass.
- Parsed playlists, metadata, EPG, favorites, and recent are all in SQLite
  (WAL mode) so restarts are instant and network failures degrade gracefully.
- The content grid uses `QListWidget` in icon mode with `uniformItemSizes` and
  lazy artwork; the list view uses `QTableWidget`. Artwork is downscaled to
  thumbnails (Pillow) and cached on disk under hashed filenames.
- Metadata/artwork are resolved for visible items only, never the whole list.

## Building the installer

1. Drop `mpv-2.dll` + the FFmpeg DLLs into `packaging/mpv/` (see
   `packaging/mpv/README.txt`).
2. `pip install python-mpv python-vlc Pillow` (Pillow is optional but enables
   thumbnail downscaling).
3. `pyinstaller packaging/app.spec --clean`
4. Open `packaging/installer.iss` in Inno Setup and Compile.

The resulting installer works on a clean Windows machine with no external
software (no VLC, no codec packs) — all codecs ship inside the bundle.
