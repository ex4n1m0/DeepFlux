#!/usr/bin/env python
"""Pin a yt-dlp release for the in-app updater and write its feed.

The frozen Windows and Linux builds update yt-dlp themselves
(infra/ytdlp_overlay.py): at startup they read the owner-pinned feed
``website/deepflux/latest-ytdlp.json`` and offer — or, by the user's
opt-in, install — the release it names. This script is how that feed is
written:

    python packaging/pin_ytdlp.py              # newest stable release on PyPI
    python packaging/pin_ytdlp.py 2026.11.23   # a specific one

It resolves the release through PyPI's JSON API, downloads the wheel into
a temporary folder, checks size and SHA-256 against PyPI's own record,
refuses a release that depends on something the bundle does not know, and
runs the SAME start-up check the app runs before it switches a release on
— so a feed is only written for a release that actually loads here.

Publishing is a separate, manual step (``vercel --prod --yes --scope
timedivision`` from ``website/``): nothing here deploys. Until the feed is
live the app keeps its first behaviour (PyPI probe + "update DeepFlux").
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from infra import ytdlp_overlay  # noqa: E402  (needs ROOT on sys.path)

PYPI_JSON_URL = "https://pypi.org/pypi/yt-dlp/json"
FEED_PATH = os.path.join(ROOT, "website", "deepflux", "latest-ytdlp.json")
CHUNK = 256 * 1024

# Distribution names a pinned release may depend on (its "default" extra —
# yt-dlp 2026.11.23). DeepFlux ships yt_dlp ALONE: these are optional at
# run time and the bundle is known to cope without them. A release that
# needs anything else must ship with a DeepFlux build first.
KNOWN_DEPENDENCIES = frozenset({
    "brotli", "brotlicffi", "certifi", "mutagen", "pycryptodomex",
    "requests", "urllib3", "websockets", "yt-dlp-ejs"})


def resolve(version: Optional[str]) -> dict:
    """PyPI's record of the release's un-yanked universal wheel."""
    with requests.get(PYPI_JSON_URL, timeout=30, stream=True) as response:
        response.raise_for_status()
        data = json.loads(response.content)
    if version is None:
        # PyPI's info.version is the newest STABLE release; the nightly
        # channel (…232653.dev0) never lands there.
        version = str(data["info"]["version"])
    if not ytdlp_overlay._VERSION_RE.match(version):
        raise SystemExit(f"{version!r} is not a stable CalVer yt-dlp version")
    file_name = f"yt_dlp-{version}-py3-none-any.whl"
    wheel = next((item for item in (data.get("releases", {}).get(version) or [])
                  if item.get("filename") == file_name and not item.get("yanked")), None)
    if wheel is None:
        raise SystemExit(f"yt-dlp {version} has no un-yanked universal wheel on PyPI")
    return {"version": version, "file": file_name, "size": int(wheel["size"]),
            "sha256": str(wheel["digests"]["sha256"]).lower(), "url": str(wheel["url"]),
            "released": str(wheel.get("upload_time_iso_8601") or "")[:10]}


def download(record: dict, dest: Path) -> None:
    """Fetch the wheel and check it against PyPI's own size + digest record."""
    digest = hashlib.sha256()
    written = 0
    with requests.get(record["url"], stream=True, timeout=(15, 60)) as response:
        response.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in response.iter_content(CHUNK):
                if not chunk:
                    continue
                written += len(chunk)
                if written > record["size"]:
                    break
                digest.update(chunk)
                fh.write(chunk)
    if written != record["size"] or digest.hexdigest() != record["sha256"]:
        dest.unlink(missing_ok=True)
        raise SystemExit("the downloaded wheel does not match PyPI's size/digest record")


def unknown_dependencies(wheel: Path) -> set:
    """Dependency names of the release's default extra that DeepFlux's
    bundle does not know (empty = fine)."""
    with zipfile.ZipFile(wheel) as zf:
        names = [name for name in zf.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise SystemExit("the wheel has no single METADATA file")
        metadata = zf.read(names[0]).decode("utf-8")
    found = set()
    for line in metadata.splitlines():
        if not line.startswith("Requires-Dist:"):
            continue
        requirement, _, marker = line.split(":", 1)[1].strip().partition(";")
        if "extra ==" in marker and "extra == 'default'" not in marker:
            continue  # another extra (curl-cffi, deno, pin, …): not installed by default
        found.add(re.split(r"[\s;(\[<>=!~]", requirement, 1)[0].strip().lower().replace("_", "-"))
    return found - KNOWN_DEPENDENCIES


def build_feed(record: dict) -> dict:
    """The feed document — validated with the app's own parser, so a PyPI
    URL-layout change stops here instead of shipping a feed nobody can use."""
    feed = {"version": record["version"], "file": record["file"], "size": record["size"],
            "sha256": record["sha256"], "url": record["url"], "released": record["released"]}
    if ytdlp_overlay.parse_feed(json.dumps(feed).encode("utf-8")) is None:
        raise SystemExit("PyPI's record does not fit the feed format the app accepts "
                         "(URL layout changed?)")
    return feed


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print(__doc__)
        return 2
    record = resolve(argv[0] if argv else None)
    print(f"yt-dlp {record['version']}: {record['file']} "
          f"({record['size'] / 1e6:.2f} MB, released {record['released'] or '?'})")
    with tempfile.TemporaryDirectory(prefix="df-pin-ytdlp-") as tmp:
        work = Path(tmp)
        wheel = work / record["file"]
        download(record, wheel)
        print("download matches PyPI's size and SHA-256 record")
        unknown = unknown_dependencies(wheel)
        if unknown:
            raise SystemExit(
                "the release depends on " + ", ".join(sorted(unknown)) + ", which DeepFlux's "
                "bundle does not know — ship those with a DeepFlux build before pinning it")
        info = ytdlp_overlay.YtdlpUpdateInfo(
            version=record["version"], file=record["file"], size=record["size"],
            sha256=record["sha256"], url=record["url"], released=record["released"])
        check = ytdlp_overlay.prepare_release(wheel, info, work)
        print(f"start-up check passed: {check.get('extractors')} extractors registered")
    feed = build_feed(record)
    ytdlp_overlay.write_json_atomic(Path(FEED_PATH), feed)
    print(f"wrote {os.path.relpath(FEED_PATH, ROOT)} — publish it with:\n"
          f"    vercel --prod --yes --scope timedivision      (from website/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
