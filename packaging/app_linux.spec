# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for DeepFlux on Linux (plain onedir, packaged as an
AppImage + tar.gz by CI — see .github/workflows/linux-build.yml).

Built by CI on an Ubuntu runner — PyInstaller cannot cross-compile, so this
spec only ever runs on Linux.

Since the 2026-10-11 full-feature port this is a FULL build, Play tab
included (owner decision — see _PLAY_TAB_SUPPORTED in gui/main_window.py):
  * The Play tab embeds video via X11 window IDs (mpv --wid / libvlc
    set_xwindow); the app defaults QT_QPA_PLATFORM=xcb (XWayland) because
    native Wayland has no cross-client window embedding.
  * libmpv is NOT collected here — ctypes-loaded libraries are invisible to
    PyInstaller's ELF analysis. The workflow apt-installs libmpv2 and drops
    libmpv.so.2 + its non-glibc dependency closure (rpath-patched to
    $ORIGIN) into _internal/mpvlibs/, where iptv.player._bundled_so_base
    finds it.
  * libVLC is not bundled: the VLC fallback activates only on machines that
    have a system VLC installed (python-vlc ships, its import raises
    OSError without libvlc and the backend degrades to mpv).
  * No bundled FFmpeg binary (the agent installs it via the distro's
    package manager — /usr/bin/ffmpeg is auto-detected from PATH, see the
    SYSTEM_PROMPT note in agent/loop.py).
  * The DeepSeek shared key is embedded via CI secret (owner decision
    2026-10-11, same exposure as the public Windows setup exe): the
    workflow generates an obfuscated _embedded_keys.py from
    EMBED_SHARED_* secrets before running this spec (packaging/
    gen_embedded_keys_from_env.py); git never carries the file.
  * No signing of any kind — Linux has no Gatekeeper/SmartScreen analog;
    an AppImage runs as soon as it carries the exec bit.

Differences from app_macos.spec:
  * No BUNDLE() step — the AppImage wrapper is built by linuxdeploy in CI,
    which also bundles the SYSTEM library closure QtWebEngine needs
    (libnss3, libasound, libxkbcommon-x11, the libxcb set…) that a
    PyInstaller onedir alone would leave on the target's disk.
  * No .icns/icon — ELF executables carry no icon; the AppImage uses the
    tracked DeepFlux5.png directly.
"""
import os
import re
import sys

from PyInstaller.building.api import PYZ, EXE, COLLECT
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

project_root = os.getcwd()
sys.path.insert(0, project_root)

import libtorrent  # noqa: E402

block_cipher = None

libtorrent_binaries = collect_dynamic_libs("libtorrent")

# yt-dlp: many lazy-loaded extractors — grab everything (same as Windows).
yt_dlp_datas, yt_dlp_binaries, yt_dlp_hiddenimports = collect_all("yt_dlp")

# curl_cffi ships a bundled libcurl + certs.
curl_cffi_datas, curl_cffi_binaries, curl_cffi_hiddenimports = collect_all("curl_cffi")

# ddgs -> primp (Rust HTTP client) + fake_useragent (browsers.jsonl data).
ddgs_datas, ddgs_binaries, ddgs_hiddenimports = collect_all("ddgs")
primp_datas, primp_binaries, primp_hiddenimports = collect_all("primp")
fake_useragent_datas, fake_useragent_binaries, fake_useragent_hiddenimports = collect_all("fake_useragent")

# cryptography — settings backup (.dfc), Rust native extension.
crypto_datas, crypto_binaries, crypto_hiddenimports = collect_all("cryptography")

# OnlyHumans room stack (ircmgr/oh_room.py): PyNaCl bundles libsodium
# (XChaCha20-Poly1305 AEAD) and argon2-cffi the Argon2id word stretch —
# both native extensions (same as the Windows spec; previously only pulled
# in transitively here).
nacl_datas, nacl_binaries, nacl_hiddenimports = collect_all("nacl")
argon2_datas, argon2_binaries, argon2_hiddenimports = collect_all("argon2")

# Voice input stack (agent-box mic).
fw_datas, fw_binaries, fw_hiddenimports = collect_all("faster_whisper")
ct2_datas, ct2_binaries, ct2_hiddenimports = collect_all("ctranslate2")
ort_datas, ort_binaries, ort_hiddenimports = collect_all("onnxruntime")
tok_datas, tok_binaries, tok_hiddenimports = collect_all("tokenizers")
hf_datas, hf_binaries, hf_hiddenimports = collect_all("huggingface_hub")

# LGPL/GPL notices for whatever does ship (mpv's libmpv ships in
# _internal/mpvlibs via the workflow's closure step — its notice belongs here).
license_datas = []
licenses_dir = os.path.join(project_root, "packaging", "licenses")
if os.path.isdir(licenses_dir):
    for fname in os.listdir(licenses_dir):
        if fname.lower().endswith((".txt", ".md", ".html")):
            license_datas.append((os.path.join(licenses_dir, fname), "licenses"))

# App version mirrors installer.iss MyAppVersion (single source of truth).
app_version = "0.0.0"
iss_path = os.path.join(project_root, "packaging", "installer.iss")
if os.path.isfile(iss_path):
    m = re.search(r'^#define\s+MyAppVersion\s+"([^"]+)"',
                  open(iss_path, encoding="utf-8", errors="replace").read(), re.M)
    if m:
        app_version = m.group(1)

_wanted_datas = [
    (os.path.join(project_root, "packaging", "logo_48.png"), "packaging"),
    (os.path.join(project_root, "packaging", "fonts", "Inter.ttf"), "packaging/fonts"),
    (os.path.join(project_root, "packaging", "fonts", "JetBrainsMono.ttf"), "packaging/fonts"),
    (os.path.join(project_root, "packaging", "fonts", "Inter-OFL.txt"), "packaging/fonts"),
    (os.path.join(project_root, "packaging", "fonts", "JetBrainsMono-OFL.txt"), "packaging/fonts"),
    (os.path.join(project_root, "DeepFlux5.png"), "."),
    # Agent-tab watermark: the website hero banner (same file as deepflux.space).
    (os.path.join(project_root, "DeepFluxNeonBanner.webp"), "."),
(os.path.join(project_root, "website", "deepflux", "DeepFluxBanner.webp"), "."),
    (os.path.join(project_root, "packaging", "icons", "back.svg"), "packaging/icons"),
    (os.path.join(project_root, "packaging", "icons", "forward.svg"), "packaging/icons"),
    (os.path.join(project_root, "packaging", "icons", "reload.svg"), "packaging/icons"),
    (os.path.join(project_root, "packaging", "icons", "home.svg"), "packaging/icons"),
    (os.path.join(project_root, "packaging", "icons", "bookmark.svg"), "packaging/icons"),
    (os.path.join(project_root, "packaging", "icons", "newtab.svg"), "packaging/icons"),
    (os.path.join(project_root, "packaging", "icons", "newtab.png"), "packaging/icons"),
    (os.path.join(project_root, "packaging", "icons", "adblock.svg"), "packaging/icons"),
    (os.path.join(project_root, "packaging", "icons", "adblock.png"), "packaging/icons"),
    (os.path.join(project_root, "packaging", "icons", "grabber.svg"), "packaging/icons"),
    (os.path.join(project_root, "packaging", "icons", "video.svg"), "packaging/icons"),
    # MilkDrop audio visualizer: Butterchurn renderer + .milk converter run
    # in a QWebEngineView (gui/milkdrop.py); presets live in MilkDrop/.
    # Presets ship INSIDE the assets dir (top-level "MilkDrop" would collide
    # with "milkdrop" on case-insensitive filesystems) — same as Windows.
    (os.path.join(project_root, "packaging", "milkdrop"), "milkdrop"),
    (os.path.join(project_root, "MilkDrop"), "milkdrop/presets"),
    (os.path.join(project_root, "chrome_extension"), "chrome_extension"),
]
datas = [(src, dst) for src, dst in _wanted_datas if os.path.exists(src)]
if len(datas) != len(_wanted_datas):
    missing = [src for src, _ in _wanted_datas if not os.path.exists(src)]
    print("WARNING: missing data files skipped:", missing)

a = Analysis(
    [os.path.join(project_root, "main.py")],
    pathex=[project_root],
    binaries=libtorrent_binaries + yt_dlp_binaries + curl_cffi_binaries + ddgs_binaries + primp_binaries + fake_useragent_binaries + crypto_binaries + nacl_binaries + argon2_binaries + fw_binaries + ct2_binaries + ort_binaries + tok_binaries + hf_binaries,
    datas=datas + yt_dlp_datas + curl_cffi_datas + ddgs_datas + primp_datas + fake_useragent_datas + crypto_datas + nacl_datas + argon2_datas + license_datas + fw_datas + ct2_datas + ort_datas + tok_datas + hf_datas,
    hiddenimports=[
        "engine",
        "engine.torrent_engine",
        "engine.state",
        "agent",
        "agent.tools",
        "agent.loop",
        "agent.llm",
        "agent.memory",
        "agent.organizer",
        "agent.rss",
        "gui",
        "gui.main_window",
        "gui.rss_dialog",
        "gui.rss_viewer",
        "gui.settings_dialog",
        "gui.sources_dialog",
        "gui.help_dialog",
        "gui.downloads_tab",
        "gui.commander_tab",
        "gui.iptv_tab",
        "gui.iptv_settings_dialog",
        "gui.room_tab",
        "gui.voice_input",
        "ircmgr",
        "ircmgr.state",
        "ircmgr.oh_room",
        "iptv",
        "iptv.models",
        "iptv.m3u_parser",
        "iptv.classify",
        "iptv.xtream",
        "iptv.cache",
        "iptv.artwork",
        "iptv.metadata",
        "iptv.epg",
        "iptv.player",
        "iptv.manager",
        "iptv.svp",
        "iptv.mpv_process",
        "iptv.local_folder",
        "iptv.framegrab",
        "iptv.opensubtitles",
        "dlmgr",
        "dlmgr.engine",
        "dlmgr.job",
        "dlmgr.segment",
        "dlmgr.scheduler",
        "dlmgr.resume_state",
        "dlmgr.control_api",
        "dlmgr.hls_dash",
        "dlmgr.ffmpeg",
        "dlmgr.http_client",
        "dlmgr.browser_extension",
        "dlmgr.bookmarks_import",
        "dlmgr.adblock",
        "native_messaging",
        "native_messaging.host",
        "native_messaging.register",
        "infra",
        "infra.file_associations",
        "config",
        # CI generates an obfuscated _embedded_keys.py from EMBED_SHARED_*
        # secrets (DeepSeek, owner decision 2026-10-11) before PyInstaller
        # runs; the git tree never carries the file, so source builds stay
        # keyless automatically.
        *(["_embedded_keys"] if os.path.exists(os.path.join(project_root, "_embedded_keys.py")) else []),
        "libtorrent",
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtSvg",
        "PySide6.QtMultimedia",
        "faster_whisper",
        "requests",
        "httpx",
        "pydantic",
        "dotenv",
        "xml.etree.ElementTree",
        "bs4",
        "html.parser",
        "m3u8",
        "Crypto",
        "Crypto.Cipher",
        "Crypto.Cipher.AES",
        "yt_dlp",
        "mpv",
        "vlc",
        "PIL",
        "PIL.Image",
        "curl_cffi",
        "ddgs",
        "primp",
        "fake_useragent",
        "infra.config_backup",
    ] + yt_dlp_hiddenimports + curl_cffi_hiddenimports + ddgs_hiddenimports + primp_hiddenimports + fake_useragent_hiddenimports + crypto_hiddenimports + nacl_hiddenimports + argon2_hiddenimports + fw_hiddenimports + ct2_hiddenimports + ort_hiddenimports + tok_hiddenimports + hf_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DeepFlux",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DeepFlux",
)
