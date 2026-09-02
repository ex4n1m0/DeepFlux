"""Windows registry registration for the Chrome native messaging host.

Registers DeepFlux as a native messaging host so Chrome can launch it
via chrome.runtime.connectNative("com.deeptorrent.integration").

The host manifest JSON is written to the user's AppData directory, and
a registry key is created pointing to it.

Registry location (per-user):
    HKCU\\SOFTWARE\\Google\\Chrome\\NativeMessagingHosts\\com.deeptorrent.integration
        (Default) = "C:\\Users\\{user}\\AppData\\Roaming\\DeepTorrent\\native_messaging_host.json"

The host manifest contains:
    name:           com.deeptorrent.integration
    description:    DeepFlux Integration Module
    path:           path to Deeptorrent4.exe
    type:           stdio
    allowed_origins: ["chrome-extension://{EXTENSION_ID}/"]
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Optional

logger = logging.getLogger(__name__)

HOST_NAME = "com.deeptorrent.integration"
HOST_DESCRIPTION = "DeepFlux Integration Module"

# The extension ID will be set after publishing to the Chrome Web Store.
# Development registration also requires the exact ID shown for the unpacked
# extension; native-messaging manifests must never use a wildcard origin.
DEFAULT_EXTENSION_ID = ""


def _get_host_manifest_path() -> str:
    """Return the path where the native messaging host manifest JSON is stored."""
    app_data = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "DeepTorrent")
    os.makedirs(app_data, exist_ok=True)
    return os.path.join(app_data, "native_messaging_host.json")


def _get_exe_path() -> str:
    """Return the path Chrome should launch for the native messaging host.

    Packaged build: the Deeptorrent exe itself (it detects the
    chrome-extension:// origin in argv and enters host mode automatically).
    Development: a generated .bat wrapper that runs `python main.py
    --native-messaging` — Chrome cannot pass arguments, so pointing the
    manifest at python.exe alone would just start an interpreter and exit."""
    exe_name = os.path.basename(sys.executable).lower() if sys.executable else ""
    if exe_name.startswith("deeptorrent") or exe_name.startswith("deepflux"):
        return sys.executable

    # Development mode — generate a wrapper batch file next to the manifest.
    app_data = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "DeepTorrent")
    os.makedirs(app_data, exist_ok=True)
    wrapper = os.path.join(app_data, "native_host.bat")
    main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
    try:
        with open(wrapper, "w", encoding="ascii") as f:
            f.write(f'@echo off\r\n"{sys.executable}" "{main_py}" --native-messaging %*\r\n')
        return wrapper
    except OSError as exc:
        logger.error("Failed to write native host wrapper: %s", exc)
        return sys.executable


def write_host_manifest(extension_id: str = "") -> str:
    """Write the native messaging host manifest JSON file.

    Returns the path to the manifest file."""
    exe_path = _get_exe_path()

    # Build allowed_origins from one exact, validated Chrome extension ID.
    extension_id = (extension_id or DEFAULT_EXTENSION_ID).strip().lower()
    if not re.fullmatch(r"[a-p]{32}", extension_id):
        raise ValueError("A valid 32-character Chrome extension ID is required")
    allowed_origins = [f"chrome-extension://{extension_id}/"]

    manifest = {
        "name": HOST_NAME,
        "description": HOST_DESCRIPTION,
        "path": exe_path,
        "type": "stdio",
        "allowed_origins": allowed_origins,
    }

    manifest_path = _get_host_manifest_path()
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    logger.info("Wrote native messaging host manifest to %s", manifest_path)
    logger.info("Host manifest: %s", json.dumps(manifest, indent=2))
    return manifest_path


def register_native_host(extension_id: str = "") -> bool:
    """Register the native messaging host in the Windows registry.

    Creates the HKCU registry key pointing to the host manifest JSON.
    This allows Chrome to find and launch DeepFlux as a native messaging host.

    Returns True on success, False on failure."""
    if sys.platform != "win32":
        logger.warning("Native messaging registration is only supported on Windows")
        return False

    try:
        import winreg
    except ImportError:
        logger.error("winreg module not available (not on Windows?)")
        return False

    # Write the manifest file first.
    try:
        manifest_path = write_host_manifest(extension_id)
    except (OSError, ValueError) as exc:
        logger.error("Failed to write native messaging manifest: %s", exc)
        return False

    # Create the registry key.
    key_path = f"SOFTWARE\\Google\\Chrome\\NativeMessagingHosts\\{HOST_NAME}"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, manifest_path)
        logger.info("Registered native messaging host: %s -> %s", key_path, manifest_path)
        return True
    except Exception as exc:
        logger.error("Failed to register native messaging host: %s", exc)
        return False


def unregister_native_host() -> bool:
    """Remove the native messaging host registration from the Windows registry.

    Returns True on success, False on failure."""
    if sys.platform != "win32":
        return False

    try:
        import winreg
    except ImportError:
        return False

    key_path = f"SOFTWARE\\Google\\Chrome\\NativeMessagingHosts\\{HOST_NAME}"
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        logger.info("Unregistered native messaging host: %s", key_path)
        return True
    except FileNotFoundError:
        # Key doesn't exist — nothing to remove.
        return True
    except Exception as exc:
        logger.error("Failed to unregister native messaging host: %s", exc)
        return False
