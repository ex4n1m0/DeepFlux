#!/usr/bin/env python
"""Fetch and pin the official Jackett Windows installer for bundling.

Run on the BUILD machine before compiling installer.iss (same ritual as
gen_logo_assets.py — the output is build input, not a tracked artifact):

    python packaging/fetch_jackett.py

Downloads the latest ``Jackett.Installer.Windows.exe`` from the official
GitHub releases into ``packaging/jackett/`` and records the exact version,
size and SHA-256 in ``packaging/jackett/PINNED.json`` (tracked in git, so
a build is reviewable against what it actually shipped). Also writes the
GPL-2.0 license notice next to it, which installer.iss ships into
``_internal\\licenses\\`` alongside the mpv/ffmpeg LGPL notices — Jackett
is GPL-2.0 and we redistribute its unmodified installer.

The .exe itself is gitignored (42 MB binary); a fresh clone simply re-runs
this script. installer.iss uses skipifsourcedoesntexist so builds without
it still succeed (they just don't offer the Jackett setup step).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date

import requests

REPO = "Jackett/Jackett"
ASSET_NAME = "Jackett.Installer.Windows.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "jackett")
MAX_BYTES = 250 * 1024 * 1024  # sanity cap; the installer is ~42 MB


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest",
                        headers=headers, timeout=30)
    resp.raise_for_status()
    release = resp.json()
    tag = release.get("tag_name") or "unknown"
    asset = next((a for a in release.get("assets", [])
                  if a.get("name") == ASSET_NAME), None)
    if asset is None:
        print(f"No {ASSET_NAME} asset on release {tag}", file=sys.stderr)
        return 1

    exe_path = os.path.join(OUT_DIR, ASSET_NAME)
    print(f"Downloading {ASSET_NAME} {tag} "
          f"({asset.get('size', 0) / 1e6:.1f} MB)…")
    with requests.get(asset["browser_download_url"], stream=True, timeout=120) as dl:
        dl.raise_for_status()
        sha = hashlib.sha256()
        size = 0
        with open(exe_path, "wb") as fh:
            for chunk in dl.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_BYTES:
                    raise SystemExit("Download exceeded the sanity cap — aborting.")
                sha.update(chunk)
                fh.write(chunk)

    # License notice: the GPL-2.0 text from the exact tag we just pinned.
    license_url = (f"https://raw.githubusercontent.com/{REPO}/{tag}/LICENSE")
    lic_resp = requests.get(license_url, timeout=30)
    lic_path = os.path.join(OUT_DIR, "jackett-gpl2.txt")
    with open(lic_path, "w", encoding="utf-8") as fh:
        fh.write(
            f"Jackett {tag} — https://github.com/Jackett/Jackett\n"
            f"Bundled unmodified as {ASSET_NAME} (sha256 {sha.hexdigest()}).\n"
            f"Jackett is licensed under GPL-2.0; source for this exact release:\n"
            f"{license_url}\n"
            f"Retrieved {date.today().isoformat()}.\n"
            f"{'-' * 70}\n\n"
        )
        fh.write(lic_resp.text if lic_resp.ok else
                 "Full license text: https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt\n")

    pinned = {
        "version": tag,
        "asset": ASSET_NAME,
        "size": size,
        "sha256": sha.hexdigest(),
        "downloaded": date.today().isoformat(),
    }
    with open(os.path.join(OUT_DIR, "PINNED.json"), "w", encoding="utf-8") as fh:
        json.dump(pinned, fh, indent=2)
        fh.write("\n")
    print(f"Pinned {tag}: {size / 1e6:.1f} MB, sha256 {sha.hexdigest()[:16]}…")
    print(f"Files: {exe_path}\n       {lic_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
