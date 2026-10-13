#!/usr/bin/env python
"""Fetch and pin the official FlareSolverr Windows build for bundling.

Run on the BUILD machine before compiling installer.iss (same ritual as
fetch_jackett.py — the output is build input, not a tracked artifact):

    python packaging/fetch_flaresolverr.py

Downloads ``flaresolverr_windows_x64.zip`` from the official GitHub releases
into ``packaging/flaresolverr/`` and records version, size and SHA-256 in
``packaging/flaresolverr/PINNED.json`` (tracked in git so a build is
reviewable against what it actually shipped). Also writes the MIT license
notice next to it, which installer.iss ships into ``_internal\\licenses\\``
— FlareSolverr is MIT and we redistribute its unmodified zip.

The zip (~378 MB) is gitignored; a fresh clone re-runs this script. When a
matching file already exists locally (same size + sha256 as the latest
release asset, per the GitHub asset digest), the download is skipped —
rebuilds don't re-fetch 378 MB. installer.iss uses skipifsourcedoesntexist
so builds without it still succeed (they just don't offer the FlareSolverr
step).

VERIFIED 2026-10-13 (live, on this machine): the official v3.5.2 zip is a
self-contained PyInstaller onedir (flaresolverr.exe + _internal/ incl. its
OWN Chromium at _internal/chrome/chrome.exe — no system Chrome needed),
configures via ENV (PORT/HOST/HEADLESS/LOG_LEVEL — no CLI flags), serves
GET /health -> {"status":"ok"} on the default port 8191, and linking it
into Jackett unblocked 15 of 19 Cloudflare-walled indexers (1337x's current
challenge is NOT solvable by it; solve times 40-90s on first hit, cached
afterwards).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date

import requests

REPO = "FlareSolverr/FlareSolverr"
ASSET_NAME = "flaresolverr_windows_x64.zip"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "flaresolverr")
MAX_BYTES = 600 * 1024 * 1024  # sanity cap; the zip is ~378 MB


def _sha256_of(path: str) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


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

    size = int(asset.get("size") or 0)
    digest = (asset.get("digest") or "").removeprefix("sha256:").lower()
    zip_path = os.path.join(OUT_DIR, ASSET_NAME)

    if os.path.isfile(zip_path) and size and os.path.getsize(zip_path) == size:
        local_sha = _sha256_of(zip_path)
        if not digest or local_sha == digest:
            print(f"{ASSET_NAME} {tag} already present with matching digest — "
                  "skipping the download.")
            sha_hex = local_sha
        else:
            print("Local zip matches the size but not the digest — re-downloading.")
            sha_hex = None
    else:
        sha_hex = None

    if sha_hex is None:
        print(f"Downloading {ASSET_NAME} {tag} ({size / 1e6:.1f} MB)…")
        with requests.get(asset["browser_download_url"], stream=True, timeout=300) as dl:
            dl.raise_for_status()
            sha = hashlib.sha256()
            got = 0
            with open(zip_path, "wb") as fh:
                for chunk in dl.iter_content(chunk_size=1024 * 512):
                    if not chunk:
                        continue
                    got += len(chunk)
                    if got > MAX_BYTES:
                        raise SystemExit("Download exceeded the sanity cap — aborting.")
                    sha.update(chunk)
                    fh.write(chunk)
        sha_hex = sha.hexdigest()

    # License notice: the MIT text from the exact tag we just pinned.
    license_url = f"https://raw.githubusercontent.com/{REPO}/{tag}/LICENSE"
    lic_resp = requests.get(license_url, timeout=30)
    lic_path = os.path.join(OUT_DIR, "flaresolverr-mit.txt")
    with open(lic_path, "w", encoding="utf-8") as fh:
        fh.write(
            f"FlareSolverr {tag} — https://github.com/FlareSolverr/FlareSolverr\n"
            f"Bundled unmodified as {ASSET_NAME} (sha256 {sha_hex}).\n"
            f"FlareSolverr is licensed under the MIT License; source for this\n"
            f"exact release: {license_url}\n"
            f"Retrieved {date.today().isoformat()}.\n"
            f"{'-' * 70}\n\n"
        )
        fh.write(lic_resp.text if lic_resp.ok else
                 "Full license text: https://opensource.org/licenses/MIT\n")

    pinned = {
        "version": tag,
        "asset": ASSET_NAME,
        "size": os.path.getsize(zip_path),
        "sha256": sha_hex,
        "downloaded": date.today().isoformat(),
    }
    with open(os.path.join(OUT_DIR, "PINNED.json"), "w", encoding="utf-8") as fh:
        json.dump(pinned, fh, indent=2)
        fh.write("\n")
    print(f"Pinned {tag}: {pinned['size'] / 1e6:.1f} MB, sha256 {sha_hex[:16]}…")
    print(f"Files: {zip_path}\n       {lic_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
