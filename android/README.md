# DeepFlux for Android

Standalone DeepFlux for phones and tablets — Kotlin + Jetpack Compose over
the same libtorrent core as the desktop app (via **libtorrent4j**). This is
Phase 1 of the plan in `../ANDROID_STUDY.md` (owner decisions 2026-11-12:
standalone app, phones + tablets, key ships in the app).

## What works in this milestone (v0.1)

- Add torrents from magnet links (dialog, browser `magnet:` links,
  share-sheet text) and `.torrent` files (file manager / share)
- Live list: progress, state, speeds, peers/seeds, per-torrent
  pause/resume/remove
- Sequential (stream-first) download mode, per-add and as a default
- Foreground service keeps transfers alive with the app closed; progress
  notification; finished/error notifications
- Wi-Fi-only and charging-only gates (session-level pause)
- Magnet-first persistence: the list survives restarts (metadata is
  re-fetched via DHT; existing files are re-checked, not re-downloaded)
- Adaptive layout: bottom bar on phones, navigation rail on tablets
- Settings: gates, sequential default, custom agent key (wins over the
  built-in shared key — desktop parity), about screen with libtorrent
  version

## Next milestones (ANDROID_STUDY.md §6)

- Stream-while-downloading into media3/ExoPlayer
- Jackett as a REMOTE source (user's desktop URL) + search tier
- Agent chat (OkHttp SSE against the same OpenAI-compatible endpoints; the
  key plumbing already ships — `data/EmbeddedKey.kt` + `AppPrefs.customApiKey`)
- User-pickable save folder (SAF), i18n (desktop has zh-CN), on-device
  llama.cpp, signing + `deepflux.space` distribution lane

## Known IPTV channel issues (measured 2026-11-13, do not re-derive)

The seeded Macau playlist (iptv-org mo.m3u) — 5 of 7 channels play clean
(all `live3.tdm.com.mo`, verified rendering in the emulator):

- **TDM Ou Mun** (`http://61.244.22.4/...`) — host is DEAD/unreachable
  (connect timeout for every UA, any client). Player shows the localized
  error + Retry after ~40s of ExoPlayer retries.
- **Lotus TV** (`cdn4.skygo.mn`) — CDN-side client discrimination: the
  Anevia edge serves Android's TLS stack (Conscrypt) a MASTER playlist
  with sessionized `_shared_<hash>/...-seq=…` variant URLs, then 403s the
  variant request. Same URL + same UA from curl (Schannel), Java
  HttpClient, and with/without `Accept-Encoding: gzip` all get the PLAIN
  master + 200s — only the on-device stack fails. NOT app-fixable without
  swapping the network stack (Cronet/OkHttp with a Chrome-like TLS
  fingerprint might pass); revisit only if the channel matters. The player
  honors the playlist's per-channel `http-user-agent` (EXTINF attr +
  `#EXTVLCOPT`, desktop-parity) — that is NOT this channel's problem.

## Toolchain (this dev machine)

Everything lives in `D:\android-toolchain` (installed 2026-11-12):

- JDK 21 (Microsoft OpenJDK): `D:\android-toolchain\jdk\jdk-21.0.13.0.2+1`
- Gradle 8.14.3: `D:\android-toolchain\gradle\gradle-8.14.3`
- Android SDK: `D:\android-toolchain\sdk` (platform 36, build-tools 36.0.0)

`local.properties` (gitignored) carries `sdk.dir=D:\\android-toolchain\\sdk`.

Pinned stack: AGP 8.13.0 · Kotlin 2.4.21 · Compose BOM 2026.06.01 (Compose
1.11.4 / material3 1.4.0 — pinned because BOM ≥2026.08 maps to Compose 1.12
whose AARs demand compileSdk 37 / AGP 9.1+) · minSdk 28 (libtorrent4j floor)
· targetSdk/compileSdk 36 · platform 36 installed (37.0 also present, unused).
AGP 9.x (built-in Kotlin) is a deliberate LATER upgrade — don't bump
casually; see the JetBrains AGP9 migration note referenced in
ANDROID_STUDY.md §11.

## Build

```bash
cd android
export JAVA_HOME="D:/android-toolchain/jdk/jdk-21.0.13.0.2+1"   # git-bash
./gradlew assembleDebug          # → app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleRelease        # → unsigned release APK (signing = later ritual)
```

Or without the wrapper: `D:\android-toolchain\gradle\gradle-8.14.3\bin\gradle.bat assembleDebug`.

## Shared key ritual (identical stance to the Windows setup exe)

```bash
# local build machine (reads the untracked repo-root _embedded_keys.py):
python packaging/gen_android_key.py
# CI (reads the EMBED_SHARED_DEEPSEEK_API_KEY secret):
python packaging/gen_android_key.py --from-env
```

Writes `android/app/embedded_key.properties` (gitignored): an obfuscated
`salt/blob` pair — the SAME XOR-sha256 scheme as the desktop
(`packaging/gen_embedded_keys.py`, imported by path so the scheme has one
source of truth). `app/build.gradle.kts` → BuildConfig fields →
`data/EmbeddedKey.kt` decodes at runtime. No plaintext constant ever
exists in the APK. No file/secret → keyless dev build (Settings then
accepts a user key).

## CI

`.github/workflows/android-build.yml` — manual dispatch on ubuntu-24.04,
JDK 21, builds debug + unsigned release, uploads both as artifacts; the
optional `upload_release` input mirrors the Linux build's draft-release
fast-download lane (`android-staging`). The `EMBED_SHARED_DEEPSEEK_API_KEY`
secret is shared with the Linux workflow (Contents: Workflows: Actions
permissions already configured).

## Design notes (why it looks like this)

- **Engine threading** mirrors the desktop's queue/facade contract
  (`engine/torrent_engine.py`): all libtorrent4j calls funnel through a
  single-threaded dispatcher (`Dispatchers.IO.limitedParallelism(1)`);
  UI and the alert thread only touch flows.
- **Polling + alerts hybrid**: a 1 s poller builds the UI snapshot (robust
  against missed alerts); the alert listener only triggers immediate
  refreshes and notifications. Keeps v0 away from alert-thread reentrancy
  traps (the desktop learned this lesson with python-mpv observers).
- **Palette** = the desktop background ladder (#05070C/#080B12/#0B1120/
  #1A2A4A/#2A7ABF/#A8EDFF) so both products read as one brand.
- **FGS dataSync idle-stop**: Android 15's ~6 h cumulative dataSync cap
  makes phone seeding a non-goal (study §5); the service stops itself
  after a 20 s idle grace and state rehydrates on next launch.
- **16 KB page size**: libtorrent4j ≥2.1.0-39 ships NDK r29-built .so
  (compliant). Any future custom native code must be too.

## Regenerating launcher icons

Icons derive from the root `DeepFlux.png` (the single logo source since
2026-11-06), same rule as every other derived asset. Recipe (Pillow, run
from the repo root; pads onto the brand-dark #05070C square):

```bash
.venv/Scripts/python.exe - <<'PYEOF'
from PIL import Image
import os
src = Image.open("DeepFlux.png").convert("RGBA")
res = "android/app/src/main/res"
BG = (5, 7, 12, 255)
side = max(src.size)
sq = Image.new("RGBA", (side, side), BG)
sq.paste(src, ((side - src.width) // 2, (side - src.height) // 2), src)
for dpi, px in [("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)]:
    d = f"{res}/mipmap-{dpi}"
    os.makedirs(d, exist_ok=True)
    out = sq.resize((px, px), Image.LANCZOS)
    out.save(f"{d}/ic_launcher.png")
    out.save(f"{d}/ic_launcher_round.png")
for dpi, px in [("mdpi", 108), ("hdpi", 162), ("xhdpi", 216), ("xxhdpi", 324), ("xxxhdpi", 432)]:
    canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    limit = int(px * 0.66)
    ratio = min(limit / src.width, limit / src.height)
    logo = src.resize((max(1, int(src.width * ratio)), max(1, int(src.height * ratio))), Image.LANCZOS)
    canvas.paste(logo, ((px - logo.width) // 2, (px - logo.height) // 2), logo)
    canvas.save(f"{res}/mipmap-{dpi}/ic_launcher_foreground.png")
print("icons written")
PYEOF
```

(The adaptive-icon XMLs in `mipmap-anydpi-v26/` reference these PNGs +
`@color/ic_launcher_background`; nothing to regenerate there.)

## Tests

None yet — the next milestone adds JVM unit tests (MagnetParser,
EmbeddedKey round-trip, state persistence) plus a Robolectric pass over
the repository gates. Pinned by absence for now; tracked in the study's
roadmap.
