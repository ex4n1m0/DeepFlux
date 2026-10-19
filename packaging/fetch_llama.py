#!/usr/bin/env python
"""Fetch and pin the official llama.cpp Windows runtimes for bundling.

Run on the BUILD machine before ISCC (same ritual as fetch_jackett.py /
fetch_flaresolverr.py — the zips are build input, not tracked artifacts):

    python packaging/fetch_llama.py

Downloads the CPU and Vulkan Windows-x64 zips from the latest stable
ggml-org/llama.cpp release into ``packaging/llama/`` and records version,
size and SHA-256 per asset in ``packaging/llama/PINNED.json`` (tracked in
git so a build is reviewable against what it actually shipped). Also
writes the MIT license notice, which installer.iss ships into
``_internal\\licenses\\`` — llama.cpp is MIT and we redistribute the
official, unmodified archives.

The zips (~52 MB combined) are gitignored; a fresh clone re-runs this
script. When a matching file already exists locally (same size + sha256 as
the release asset, per the GitHub asset digest), the download is skipped.
installer.iss uses skipifsourcedoesntexist, so builds without the zips
still compile (they just don't offer the local-model step).

Why these two variants: the VULKAN build offloads to ANY modern GPU
(NVIDIA/AMD/Intel — no CUDA toolkit or vendor dependency), the CPU build
serves machines without one. ``--setup-localmodel`` extracts both next to
the user's data (~/.deeptorrent/llama/) and picks at runtime.

Pinned-release floor: b11407 — the first tag verified (ModelSwarm project,
2026-10) to run the Qwen3.5 hybrid-architecture models this feature ships.
The script REFUSES to pin anything older, because older llama-server
builds fail to load the Qwen3.5 GGUFs at all.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import date

import requests

REPO = "ggml-org/llama.cpp"
# Qwen3.5 (hybrid Gated DeltaNet) needs at least this build.
MIN_BUILD = 11407
ASSETS = {
    "cpu": "llama-b{build}-bin-win-cpu-x64.zip",
    "vulkan": "llama-b{build}-bin-win-vulkan-x64.zip",
}
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "llama")
MAX_BYTES = 200 * 1024 * 1024  # sanity cap; the two zips are ~52 MB total


def _sha256_of(path: str) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _pick_release(releases: list) -> dict:
    """First release that carries BOTH pinned assets and is new enough for
    Qwen3.5. (llama.cpp marks EVERY release prerelease — that is just their
    tagging convention; the GitHub release channel is the official one.)"""
    for rel in releases:
        tag = rel.get("tag_name") or ""
        m = re.fullmatch(r"b(\d+)", tag)
        if not m or int(m.group(1)) < MIN_BUILD:
            continue
        names = {a.get("name") for a in rel.get("assets", [])}
        if all(tag and fmt.format(build=m.group(1)) in names
               for fmt in ASSETS.values()):
            return rel
    return {}


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(f"https://api.github.com/repos/{REPO}/releases?per_page=20",
                        headers=headers, timeout=30)
    resp.raise_for_status()
    release = _pick_release(resp.json())
    if not release:
        print(f"No stable release >= b{MIN_BUILD} with both Windows x64 assets found",
              file=sys.stderr)
        return 1
    tag = release["tag_name"]
    build = re.fullmatch(r"b(\d+)", tag).group(1)
    by_name = {a.get("name"): a for a in release.get("assets", [])}

    pinned_assets = {}
    for variant, fmt in ASSETS.items():
        asset_name = fmt.format(build=build)
        asset = by_name.get(asset_name)
        if asset is None:
            print(f"Release {tag} has no {asset_name}", file=sys.stderr)
            return 1
        size = int(asset.get("size") or 0)
        digest = (asset.get("digest") or "").removeprefix("sha256:").lower()
        zip_path = os.path.join(OUT_DIR, asset_name)

        sha_hex = None
        if os.path.isfile(zip_path) and size and os.path.getsize(zip_path) == size:
            local_sha = _sha256_of(zip_path)
            if not digest or local_sha == digest:
                print(f"{asset_name} {tag} already present with matching digest — "
                      "skipping the download.")
                sha_hex = local_sha
            else:
                print(f"Local {asset_name} matches the size but not the digest — "
                      "re-downloading.")
        if sha_hex is None:
            print(f"Downloading {asset_name} ({size / 1e6:.1f} MB)…")
            with requests.get(asset["browser_download_url"], stream=True,
                              timeout=300) as dl:
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
        pinned_assets[variant] = {
            "asset": asset_name,
            "size": os.path.getsize(zip_path),
            "sha256": sha_hex,
        }

    # License notice: llama.cpp is MIT; source for the exact pinned tag.
    lic_resp = requests.get(
        f"https://raw.githubusercontent.com/{REPO}/{tag}/LICENSE", timeout=30)
    lic_path = os.path.join(OUT_DIR, "llama-mit.txt")
    with open(lic_path, "w", encoding="utf-8") as fh:
        fh.write(
            f"llama.cpp {tag} — https://github.com/ggml-org/llama.cpp\n"
            f"Bundled unmodified (official release archives, sha256 per variant in\n"
            f"PINNED.json). llama.cpp is licensed under the MIT License; source for\n"
            f"this exact release: https://github.com/{REPO}/tree/{tag}\n"
            f"Retrieved {date.today().isoformat()}.\n"
            f"{'-' * 70}\n\n"
        )
        fh.write(lic_resp.text if lic_resp.ok else
                 "Full license text: https://opensource.org/licenses/MIT\n")

    pinned = {
        "version": tag,
        "assets": pinned_assets,
        "downloaded": date.today().isoformat(),
    }
    with open(os.path.join(OUT_DIR, "PINNED.json"), "w", encoding="utf-8") as fh:
        json.dump(pinned, fh, indent=2)
        fh.write("\n")
    total = sum(a["size"] for a in pinned_assets.values())
    print(f"Pinned {tag}: {total / 1e6:.1f} MB across {len(pinned_assets)} variants")
    print(f"Files: {OUT_DIR}\\*.zip\n       {lic_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
