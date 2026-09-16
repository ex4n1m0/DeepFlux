"""Guards the config.json seed embedded in packaging/installer.iss.

The installer writes this seed to ~/.deeptorrent/config.json at
ssPostInstall. History (found 2026-09-16): the seed used to duplicate the
whole default config WITH trailing commas — invalid JSON that
DeeptorrentConfig.from_file silently discarded, so fresh installs always
ran on config.py defaults while the stale copy fossilized. These tests pin
the two invariants: the seed is VALID strict JSON that loads through the
real config class, and it stays MINIMAL (only real overrides — currently
the sample Macau IPTV playlist — never a copy of defaults that can drift).
"""
from __future__ import annotations

import json
import os
import re

import pytest

ISS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "packaging", "installer.iss")

pytestmark = pytest.mark.skipif(not os.path.isfile(ISS_PATH),
                                reason="installer.iss not present (frozen/partial checkout)")


def _extract_seed() -> str:
    iss = open(ISS_PATH, encoding="utf-8").read()
    block = iss.split("Config :=")[1].split("SaveStringToFile")[0]
    # Pascal // comments may sit between concatenation lines (and may
    # contain apostrophes) — strip whole comment lines before scanning.
    block = "\n".join("" if re.match(r"^\s*//", line) else line
                      for line in block.splitlines())
    parts = re.findall(r"'((?:[^']|'')*)'", block)
    return "\r\n".join(p.replace("''", "'") for p in parts)


def test_seed_is_valid_strict_json():
    data = json.loads(_extract_seed())  # raises on trailing commas etc.
    assert isinstance(data, dict) and data


def test_seed_loads_through_real_config(tmp_path):
    from config import DeeptorrentConfig
    path = str(tmp_path / "config.json")
    open(path, "w", encoding="utf-8").write(_extract_seed())
    config = DeeptorrentConfig.from_file(path)
    # The one shipped override: the sample playlist (installer comment
    # documents it — update this assertion together with the seed).
    assert len(config.iptv.sources) == 1
    src = config.iptv.sources[0]
    assert (src.id, src.name, src.kind, src.enabled) == \
        ("macau-iptv-org", "Macau", "m3u_url", True)
    assert src.url == "https://iptv-org.github.io/iptv/countries/mo.m3u"


def test_seed_stays_minimal_no_default_duplication():
    """Every seeded key must be a real override, not a copy of a default.

    Duplicating defaults fossilizes them (the dead-seed bug): config.py is
    the single source of truth, from_file fills the rest in.
    """
    from config import DeeptorrentConfig
    from dataclasses import asdict
    seed = json.loads(_extract_seed())
    defaults = asdict(DeeptorrentConfig())

    def _walk(seed_v, default_v, path=""):
        if isinstance(seed_v, dict):
            for key, value in seed_v.items():
                assert key in default_v or isinstance(default_v, dict), \
                    f"{path}.{key}: seeded section/field unknown to config"
                _walk(value, default_v.get(key) if isinstance(default_v, dict) else None,
                      f"{path}.{key}")
        else:
            assert seed_v != default_v, \
                f"{path}: seeds the default value ({seed_v!r}) — remove it; " \
                f"only real overrides belong in the installer seed"

    _walk(seed, defaults)


def test_seeded_iptv_url_serves_extm3u(monkeypatch):
    """The playlist URL must stay a reachable #EXTM3U document (the app's
    own source validation would reject it otherwise — iptv_add_source's
    _peek_playlist contract). Network test; skipped when offline."""
    import urllib.request
    seed = json.loads(_extract_seed())
    url = seed["iptv"]["sources"][0]["url"]
    try:
        head = urllib.request.urlopen(url, timeout=15).read(64).decode("utf-8", "replace")
    except Exception:
        pytest.skip("offline")
    assert head.startswith("#EXTM3U"), head
