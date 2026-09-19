# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for DeepFlux on Linux (plain onedir, packaged as an
AppImage + tar.gz by CI — see .github/workflows/linux-build.yml).

Built by CI on an Ubuntu runner — PyInstaller cannot cross-compile, so this
spec only ever runs on Linux.

Same feature scope as the macOS build (app_macos.spec):
  * No Play tab: gui.iptv_tab / gui.iptv_settings_dialog are conditionally
    imported behind _PLAY_TAB_SUPPORTED (False on linux), so they never
    enter the bundle. The iptv.* pure-python modules may still be pulled in
    by agent/tools.py's lazy imports — harmless dead code.
  * No libmpv / libVLC, no bundled FFmpeg (the agent installs it via the
    distro's package manager — /usr/bin/ffmpeg is auto-detected from PATH,
    see the SYSTEM_PROMPT note in agent/loop.py), no MilkDrop visualizer
    files (player-only).
  * Keyless by design: _embedded_keys.py is not committed, so CI builds
    without shared keys (users enter their own; everything degrades).
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

# Voice input stack (agent-box mic).
fw_datas, fw_binaries, fw_hiddenimports = collect_all("faster_whisper")
ct2_datas, ct2_binaries, ct2_hiddenimports = collect_all("ctranslate2")
ort_datas, ort_binaries, ort_hiddenimports = collect_all("onnxruntime")
tok_datas, tok_binaries, tok_hiddenimports = collect_all("tokenizers")
hf_datas, hf_binaries, hf_hiddenimports = collect_all("huggingface_hub")

# LGPL/GPL notices for whatever does ship (kept for parity; mpv/ffmpeg do not).
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
    (os.path.join(project_root, "chrome_extension"), "chrome_extension"),
]
datas = [(src, dst) for src, dst in _wanted_datas if os.path.exists(src)]
if len(datas) != len(_wanted_datas):
    missing = [src for src, _ in _wanted_datas if not os.path.exists(src)]
    print("WARNING: missing data files skipped:", missing)

a = Analysis(
    [os.path.join(project_root, "main.py")],
    pathex=[project_root],
    binaries=libtorrent_binaries + yt_dlp_binaries + curl_cffi_binaries + ddgs_binaries + primp_binaries + fake_useragent_binaries + crypto_binaries + fw_binaries + ct2_binaries + ort_binaries + tok_binaries + hf_binaries,
    datas=datas + yt_dlp_datas + curl_cffi_datas + ddgs_datas + primp_datas + fake_useragent_datas + crypto_datas + license_datas + fw_datas + ct2_datas + ort_datas + tok_datas + hf_datas,
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
        "gui.room_tab",
        "gui.voice_input",
        "ircmgr",
        "ircmgr.state",
        "ircmgr.room",
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
        # Keyless by design on CI: _embedded_keys.py is untracked, so the
        # Linux build ships without shared keys (users enter their own).
        *(["_embedded_keys"] if os.path.exists(os.path.join(project_root, "_embedded_keys.py")) else []),
        "libtorrent",
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineCore",
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
        "PIL",
        "PIL.Image",
        "curl_cffi",
        "ddgs",
        "primp",
        "fake_useragent",
        "infra.config_backup",
    ] + yt_dlp_hiddenimports + curl_cffi_hiddenimports + ddgs_hiddenimports + primp_hiddenimports + fake_useragent_hiddenimports + crypto_hiddenimports + fw_hiddenimports + ct2_hiddenimports + ort_hiddenimports + tok_hiddenimports + hf_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Player backends never load on Linux (no Play tab) — and python-mpv
        # without a system libmpv would be dead weight anyway.
        "mpv",
        "vlc",
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
