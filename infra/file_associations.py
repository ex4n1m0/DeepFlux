"""File-type and protocol association registration for DeepFlux.

Windows (per-user, HKCU, no admin rights needed):
  - ProgIDs for torrent / media / web files, all launching DeepFlux
  - The magnet: protocol handler
  - An application entry under RegisteredApplications + Capabilities so
    DeepFlux appears in Windows Settings > Default Apps, where the user can
    pick it as the default for each file type.

Linux (per-user, no root):
  - ~/.local/share/applications/DeepFlux.desktop with the torrent/media/web
    MimeType list and the magnet scheme handler
  - DeepFlux icon installed into the hicolor theme
  - xdg-mime claims the DEFAULT only for application/x-bittorrent and
    x-scheme-handler/magnet (mirroring the Windows semantics: nothing else
    wants them, and forcing video/audio defaults away from VLC & co. would
    be rude); every other type is merely *available* via "Open With".

Note: since Windows 10, programs cannot force themselves as the default —
registration makes DeepFlux *available* everywhere (Open With, Default
Apps); .torrent and magnet usually apply immediately because nothing else
claims them.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

APP_NAME = "DeepFlux"
APP_DESCRIPTION = "DeepFlux — AI torrent client, download manager and media player"

TORRENT_EXTS = [".torrent"]
VIDEO_EXTS = [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
              ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".3gp", ".ogv"]
AUDIO_EXTS = [".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"]
WEB_EXTS = [".html", ".htm", ".mhtml", ".svg"]
PROTOCOLS = ["magnet"]

# (progid, friendly name, extensions)
_PROGIDS = [
    ("DeepFlux.Torrent", "DeepFlux Torrent", TORRENT_EXTS),
    ("DeepFlux.Media", "DeepFlux Media File", VIDEO_EXTS + AUDIO_EXTS),
    ("DeepFlux.WebDoc", "DeepFlux Web Document", WEB_EXTS),
]

# Linux MimeType list for the .desktop entry (kept in sync with the
# extension groups above).
_LINUX_MIME_TYPES = [
    "application/x-bittorrent",
    "x-scheme-handler/magnet",
    "video/mp4", "video/x-matroska", "video/avi", "video/quicktime",
    "video/x-ms-wmv", "video/x-flv", "video/webm", "video/x-m4v",
    "video/mpeg", "video/mp2t", "video/3gpp", "video/ogg",
    "audio/mpeg", "audio/flac", "audio/mp4", "audio/aac", "audio/ogg",
    "audio/opus", "audio/x-wav", "audio/x-ms-wma",
    "text/html", "application/xhtml+xml", "image/svg+xml",
]


def _launch_command() -> str:
    """Command line used to open a target with DeepFlux."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return f'"{exe}" "%1"'
    # Dev mode: python main.py
    main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
    return f'"{sys.executable}" "{main_py}" "%1"'


# ---------------------------------------------------------------------------
# Linux implementation
# ---------------------------------------------------------------------------

def _linux_exec_line() -> str:
    """Exec= line for the .desktop entry (AppImage-aware)."""
    if getattr(sys, "frozen", False):
        # Inside an AppImage, sys.executable is the EPHEMERAL mount dir
        # (/tmp/.mount_xxx) — launch the durable image file instead.
        exe = os.environ.get("APPIMAGE") or sys.executable
        return f'"{exe}" %f'
    main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
    return f'"{sys.executable}" "{main_py}" %f'


def _install_linux_icon() -> str:
    """Copy the app logo into the hicolor theme; returns the icon name."""
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "DeepFlux5.png"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "DeepFlux5.png"))
    src = next((c for c in candidates if os.path.isfile(c)), "")
    dst_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "icons",
                           "hicolor", "512x512", "apps")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"{APP_NAME}.png")
    try:
        if src:
            shutil.copyfile(src, dst)
        return APP_NAME
    except OSError as exc:
        logger.warning("could not install icon: %s", exc)
        return APP_NAME


def _linux_desktop_path() -> str:
    d = os.path.join(os.path.expanduser("~"), ".local", "share", "applications")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{APP_NAME}.desktop")


def _run_quiet(*cmd: str) -> None:
    try:
        subprocess.run(cmd, capture_output=True, timeout=20)
    except Exception:
        pass  # databases refresh is cosmetic


def _register_associations_linux() -> bool:
    try:
        icon = _install_linux_icon()
        desktop = _linux_desktop_path()
        exec_line = _linux_exec_line()
        content = "\n".join([
            "[Desktop Entry]",
            "Type=Application",
            f"Name={APP_NAME}",
            f"Comment={APP_DESCRIPTION}",
            f"Exec={exec_line}",
            f"Icon={icon}",
            "Terminal=false",
            "Categories=Network;FileTransfer;AudioVideo;",
            "MimeType=" + ";".join(_LINUX_MIME_TYPES) + ";",
            "",
        ])
        with open(desktop, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        _run_quiet("update-desktop-database",
                   os.path.join(os.path.expanduser("~"), ".local", "share", "applications"))
        # Claim the DEFAULT only where nothing else wants it (mirrors the
        # Windows .torrent/magnet semantics).
        if shutil.which("xdg-mime"):
            _run_quiet("xdg-mime", "default", f"{APP_NAME}.desktop",
                       "application/x-bittorrent")
            _run_quiet("xdg-mime", "default", f"{APP_NAME}.desktop",
                       "x-scheme-handler/magnet")
        logger.info("Linux associations registered (%s)", desktop)
        return True
    except OSError as exc:
        logger.exception("Failed to register Linux file associations: %s", exc)
        return False


def _unregister_associations_linux() -> bool:
    ok = True
    for path in (_linux_desktop_path(),
                 os.path.join(os.path.expanduser("~"), ".local", "share", "icons",
                              "hicolor", "512x512", "apps", f"{APP_NAME}.png")):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            ok = False
    _run_quiet("update-desktop-database",
               os.path.join(os.path.expanduser("~"), ".local", "share", "applications"))
    # mimeapps.list: drop our Default Applications lines only (never touch
    # anything else the user configured).
    mimeapps = os.path.join(os.path.expanduser("~"), ".config", "mimeapps.list")
    try:
        if os.path.isfile(mimeapps):
            with open(mimeapps, "r", encoding="utf-8") as f:
                lines = f.readlines()
            kept = [ln for ln in lines
                    if not ln.strip().endswith(f"{APP_NAME}.desktop")]
            if kept != lines:
                with open(mimeapps, "w", encoding="utf-8", newline="\n") as f:
                    f.writelines(kept)
    except OSError:
        ok = False
    return ok


def _icon_path() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable + ",0"
    ico = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "packaging", "icon.ico"))
    return ico if os.path.isfile(ico) else ""


def register_associations() -> bool:
    """Create the per-user associations. Returns True on success."""
    if sys.platform == "linux":
        return _register_associations_linux()
    if sys.platform != "win32":
        logger.warning("File associations are only supported on Windows and Linux")
        return False
    import winreg

    cmd = _launch_command()
    icon = _icon_path()

    def set_value(key, sub: str, value: str, name: str = "") -> None:
        with winreg.CreateKey(key, sub) as k:
            winreg.SetValueEx(k, name or None, 0, winreg.REG_SZ, value)

    try:
        classes = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes",
                                 0, winreg.KEY_ALL_ACCESS)
        # 1. ProgIDs + their open commands.
        for progid, friendly, exts in _PROGIDS:
            set_value(classes, progid, friendly)
            if icon:
                set_value(classes, rf"{progid}\DefaultIcon", icon)
            set_value(classes, rf"{progid}\shell\open\command", cmd)
            for ext in exts:
                # Register under OpenWithProgids (never clobber the user's
                # current default), and claim the default only for .torrent.
                set_value(classes, rf"{ext}\OpenWithProgids", "", progid)
                with winreg.CreateKey(classes, rf"{ext}\OpenWithProgids") as k:
                    winreg.SetValueEx(k, progid, 0, winreg.REG_NONE, b"")
                if ext == ".torrent":
                    set_value(classes, ext, progid)

        # 2. magnet: protocol handler.
        set_value(classes, "magnet", "URL:Magnet Link")
        with winreg.CreateKey(classes, "magnet") as k:
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        if icon:
            set_value(classes, r"magnet\DefaultIcon", icon)
        set_value(classes, r"magnet\shell\open\command", cmd)

        # 3. Default Apps registration (Capabilities).
        caps_root = rf"Software\{APP_NAME}\Capabilities"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, caps_root) as k:
            winreg.SetValueEx(k, "ApplicationName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "ApplicationDescription", 0, winreg.REG_SZ, APP_DESCRIPTION)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, caps_root + r"\FileAssociations") as k:
            for progid, _friendly, exts in _PROGIDS:
                for ext in exts:
                    winreg.SetValueEx(k, ext, 0, winreg.REG_SZ, progid)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, caps_root + r"\URLAssociations") as k:
            for proto in PROTOCOLS:
                winreg.SetValueEx(k, proto, 0, winreg.REG_SZ, "DeepFlux.Torrent")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications") as k:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, caps_root)

        classes.Close()
        _notify_shell()
        logger.info("File associations registered (command: %s)", cmd)
        return True
    except OSError as exc:
        logger.exception("Failed to register file associations: %s", exc)
        return False


def unregister_associations() -> bool:
    """Remove the entries created by register_associations()."""
    if sys.platform == "linux":
        return _unregister_associations_linux()
    if sys.platform != "win32":
        return False
    import winreg

    def delete_tree(root, sub: str) -> None:
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_ALL_ACCESS) as k:
                while True:
                    try:
                        child = winreg.EnumKey(k, 0)
                    except OSError:
                        break
                    delete_tree(root, sub + "\\" + child)
            winreg.DeleteKey(root, sub)
        except OSError:
            pass

    try:
        for progid, _f, _e in _PROGIDS:
            delete_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{progid}")
        delete_tree(winreg.HKEY_CURRENT_USER, r"Software\Classes\magnet")
        delete_tree(winreg.HKEY_CURRENT_USER, rf"Software\{APP_NAME}")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications",
                                0, winreg.KEY_ALL_ACCESS) as k:
                winreg.DeleteValue(k, APP_NAME)
        except OSError:
            pass
        _notify_shell()
        return True
    except OSError:
        return False


def _notify_shell() -> None:
    """Tell Explorer that associations changed so icons/menus refresh."""
    try:
        import ctypes
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except Exception:
        pass
