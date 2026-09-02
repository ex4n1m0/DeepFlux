# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Deeptorrent (onedir mode).

Build with:
    pyinstaller packaging/app.spec --clean
"""
import os
import sys

from PyInstaller.building.api import PYZ, EXE, COLLECT
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

project_root = os.getcwd()
sys.path.insert(0, project_root)

import libtorrent  # noqa: E402

block_cipher = None

libtorrent_binaries = collect_dynamic_libs("libtorrent")

# yt-dlp has many lazy-loaded extractors and submodules that PyInstaller
# can't discover statically. collect_all grabs everything.
yt_dlp_datas, yt_dlp_binaries, yt_dlp_hiddenimports = collect_all("yt_dlp")

# curl_cffi (Chrome TLS impersonation for Cloudflare-fronted CDNs) ships a
# bundled libcurl DLL + certs that PyInstaller can't discover statically.
curl_cffi_datas, curl_cffi_binaries, curl_cffi_hiddenimports = collect_all("curl_cffi")

# ddgs (DuckDuckGo web search) depends on primp, a Rust HTTP client with a
# native extension — collect both or the frozen app silently loses DDG search.
ddgs_datas, ddgs_binaries, ddgs_hiddenimports = collect_all("ddgs")
primp_datas, primp_binaries, primp_hiddenimports = collect_all("primp")

# fake_useragent (used by ddgs' DuckDuckGo engine) loads browsers.jsonl via
# importlib.resources — without its data files the frozen app raises
# "Failed to load or parse browsers.json" and DDG search silently dies.
fake_useragent_datas, fake_useragent_binaries, fake_useragent_hiddenimports = collect_all("fake_useragent")

# irc (jaraco) — the IRC protocol library. It loads irc/codes.txt at runtime
# (numeric reply names) via importlib.resources, which PyInstaller misses.
irc_datas, irc_binaries, irc_hiddenimports = collect_all("irc")

# cryptography — encrypts the settings backup (infra/config_backup.py). Ships
# a Rust native extension that PyInstaller can't discover statically.
crypto_datas, crypto_binaries, crypto_hiddenimports = collect_all("cryptography")

# Voice input stack (agent-box mic): faster-whisper runs on ctranslate2 with
# native DLLs; its VAD filter runs on onnxruntime (native + model assets);
# tokenizers ships a Rust extension; huggingface_hub drives the one-time
# model download into ~/.deeptorrent/models/.
fw_datas, fw_binaries, fw_hiddenimports = collect_all("faster_whisper")
ct2_datas, ct2_binaries, ct2_hiddenimports = collect_all("ctranslate2")
ort_datas, ort_binaries, ort_hiddenimports = collect_all("onnxruntime")
tok_datas, tok_binaries, tok_hiddenimports = collect_all("tokenizers")
hf_datas, hf_binaries, hf_hiddenimports = collect_all("huggingface_hub")

# --- IPTV: bundled libmpv + FFmpeg DLLs -------------------------------------
# The mpv DLL (mpv-2.dll / libmpv-2.dll) and the FFmpeg libraries it depends on
# are shipped in packaging/mpv/. At runtime, iptv.player.ensure_mpv_dll_on_path
# prepends this directory to PATH before importing `mpv`, so playback works on
# a clean Windows machine with no external installs.
# See packaging/mpv/README.txt for where to obtain the DLLs.
mpv_dir = os.path.join(project_root, "packaging", "mpv")
mpv_binaries = []
if os.path.isdir(mpv_dir):
    for fname in os.listdir(mpv_dir):
        if fname.lower().endswith((".dll",)):
            mpv_binaries.append((os.path.join(mpv_dir, fname), "mpv"))

# libVLC fallback backend (optional; python-vlc + libvlc.dlls).
vlc_binaries = []
try:
    vlc_binaries = collect_dynamic_libs("vlc")
except Exception:
    pass

# License notices for LGPL components (mpv / FFmpeg / libVLC).
license_datas = []
licenses_dir = os.path.join(project_root, "packaging", "licenses")
if os.path.isdir(licenses_dir):
    for fname in os.listdir(licenses_dir):
        if fname.lower().endswith((".txt", ".md", ".html")):
            license_datas.append((os.path.join(licenses_dir, fname), "licenses"))

# Data files — only include those that exist so a missing asset fails softly
# instead of aborting the whole PyInstaller build with a cryptic error.
_wanted_datas = [
    (os.path.join(project_root, "packaging", "icon.ico"), "packaging"),
    (os.path.join(project_root, "packaging", "logo_48.png"), "packaging"),
    (os.path.join(project_root, "packaging", "fonts", "Inter.ttf"), "packaging/fonts"),
    (os.path.join(project_root, "packaging", "fonts", "JetBrainsMono.ttf"), "packaging/fonts"),
    (os.path.join(project_root, "packaging", "fonts", "Inter-OFL.txt"), "packaging/fonts"),
    (os.path.join(project_root, "packaging", "fonts", "JetBrainsMono-OFL.txt"), "packaging/fonts"),
    (os.path.join(project_root, "DeepFlux4.png"), "."),
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
    (os.path.join(project_root, "packaging", "ffmpeg", "ffmpeg.exe"), "ffmpeg"),
    (os.path.join(project_root, "chrome_extension"), "chrome_extension"),
    # MilkDrop audio visualizer: the Butterchurn renderer + .milk converter run
    # in a QWebEngineView (gui/milkdrop.py), and the shipped presets live in
    # MilkDrop/. Both directories are resolved via sys._MEIPASS when frozen.
    # NB: the presets go *inside* the assets dir — shipping them as a
    # top-level "MilkDrop" would collide with "milkdrop" on case-insensitive
    # Windows and PyInstaller would merge the two into one directory.
    (os.path.join(project_root, "packaging", "milkdrop"), "milkdrop"),
    (os.path.join(project_root, "MilkDrop"), "milkdrop/presets"),
]
datas = [(src, dst) for src, dst in _wanted_datas if os.path.exists(src)]
if len(datas) != len(_wanted_datas):
    missing = [src for src, _ in _wanted_datas if not os.path.exists(src)]
    print("WARNING: missing data files skipped:", missing)

a = Analysis(
    [os.path.join(project_root, "main.py")],
    pathex=[project_root],
    binaries=libtorrent_binaries + yt_dlp_binaries + mpv_binaries + vlc_binaries + curl_cffi_binaries + ddgs_binaries + primp_binaries + irc_binaries + fake_useragent_binaries + crypto_binaries + fw_binaries + ct2_binaries + ort_binaries + tok_binaries + hf_binaries,
    datas=datas + yt_dlp_datas + curl_cffi_datas + ddgs_datas + primp_datas + irc_datas + fake_useragent_datas + crypto_datas + license_datas + fw_datas + ct2_datas + ort_datas + tok_datas + hf_datas,
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
        "gui.irc_tab",
        "gui.voice_input",
        "ircmgr",
        "ircmgr.client",
        "ircmgr.state",
        "ircmgr.history",
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
        "irc",
        "infra.config_backup",
    ] + yt_dlp_hiddenimports + curl_cffi_hiddenimports + ddgs_hiddenimports + primp_hiddenimports + irc_hiddenimports + fake_useragent_hiddenimports + crypto_hiddenimports + fw_hiddenimports + ct2_hiddenimports + ort_hiddenimports + tok_hiddenimports + hf_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
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
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_root, "packaging", "icon.ico"),
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
