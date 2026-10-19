#!/usr/bin/env python
"""Pin the curated local-model ladder into infra/local_models.json.

Run on any machine (no large downloads — pins come from the HF API, where
every LFS file's `oid` IS its sha256 and `size` is exact):

    python packaging/pin_local_models.py

The manifest is TRACKED in git (unlike the llama.cpp zips / GGUF weights):
it is small provenance metadata — {repo, revision (full commit sha),
file, size, sha256} per quant — consumed at runtime by infra/local_model.py
for hardware-matched recommendations and verified, resumable downloads.

Model rules (owner direction, 2026-10-19):
  * the ladder is UNCENSORED only — the local tier exists so the agent
    answers without cloud-refusal behavior;
  * primary family: HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive
    (Apache-2.0, GGUF-only, tool-calling chat template, 262K context);
  * CPU/small-VRAM fallback: mradermacher/Huihui-Qwen3.5-4B-abliterated-GGUF
    (same Qwen3.5 family → same template + llama.cpp support, Apache-2.0).

Adding/swapping a model = edit CURATED below and re-run; entries without
a matching repo file fail loudly instead of pinning silently-wrong bytes.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timezone

import requests

HF = "https://huggingface.co/api"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "infra", "local_models.json")

# id, repo, GGUF filename (must exist in the repo), params (B), tier order.
# `family` groups quants of the same base model; `label` is GUI copy.
CURATED = [
    # -- Qwen3.5 9B Uncensored (Aggressive) — the primary GPU family ------
    dict(id="qwen35-9b-uncensored-q4km",
         repo="HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive",
         file="Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf",
         quant="Q4_K_M", params_b=9, family="qwen3.5-9b-uncensored",
         label="Qwen3.5 9B Uncensored"),
    dict(id="qwen35-9b-uncensored-q6k",
         repo="HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive",
         file="Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf",
         quant="Q6_K", params_b=9, family="qwen3.5-9b-uncensored",
         label="Qwen3.5 9B Uncensored"),
    dict(id="qwen35-9b-uncensored-q8",
         repo="HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive",
         file="Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf",
         quant="Q8_0", params_b=9, family="qwen3.5-9b-uncensored",
         label="Qwen3.5 9B Uncensored"),
    # -- Qwen3.5 4B abliterated — CPU / small-VRAM fallback family --------
    dict(id="huihui-qwen35-4b-abliterated-q4km",
         repo="mradermacher/Huihui-Qwen3.5-4B-abliterated-GGUF",
         file="Huihui-Qwen3.5-4B-abliterated.Q4_K_M.gguf",
         quant="Q4_K_M", params_b=4, family="qwen3.5-4b-abliterated",
         label="Qwen3.5 4B Uncensored"),
    dict(id="huihui-qwen35-4b-abliterated-q6k",
         repo="mradermacher/Huihui-Qwen3.5-4B-abliterated-GGUF",
         file="Huihui-Qwen3.5-4B-abliterated.Q6_K.gguf",
         quant="Q6_K", params_b=4, family="qwen3.5-4b-abliterated",
         label="Qwen3.5 4B Uncensored"),
    dict(id="huihui-qwen35-4b-abliterated-q8",
         repo="mradermacher/Huihui-Qwen3.5-4B-abliterated-GGUF",
         file="Huihui-Qwen3.5-4B-abliterated.Q8_0.gguf",
         quant="Q8_0", params_b=4, family="qwen3.5-4b-abliterated",
         label="Qwen3.5 4B Uncensored"),
]


def main() -> int:
    headers = {"User-Agent": "DeepFlux-model-pin/6.0"}
    revisions: dict = {}
    entries = []
    for item in CURATED:
        repo = item["repo"]
        if repo not in revisions:
            r = requests.get(f"{HF}/models/{repo}", headers=headers, timeout=30)
            r.raise_for_status()
            sha = r.json().get("sha") or ""
            if len(sha) != 40:
                print(f"{repo}: no commit sha from the API", file=sys.stderr)
                return 1
            revisions[repo] = sha
        rev = revisions[repo]
        r = requests.get(f"{HF}/models/{repo}/tree/{rev}", headers=headers, timeout=30)
        r.raise_for_status()
        files = {f["path"]: f for f in r.json()}
        meta = files.get(item["file"])
        if meta is None or not meta.get("lfs"):
            print(f"{repo}: file {item['file']!r} missing or not LFS", file=sys.stderr)
            return 1
        lfs = meta["lfs"]
        size, oid = int(lfs["size"]), lfs["oid"]
        if len(oid) != 64:
            print(f"{repo}/{item['file']}: no LFS sha256", file=sys.stderr)
            return 1
        gib = size / (1024 ** 3)
        entries.append({
            "id": item["id"],
            "repo": repo,
            "revision": rev,
            "file": item["file"],
            "size": size,
            "sha256": oid,
            "quant": item["quant"],
            "params_b": item["params_b"],
            "family": item["family"],
            "license": "apache-2.0",
            "label": f"{item['label']} — {item['quant']} ({gib:.1f} GiB)",
        })
        print(f"pinned {item['id']:36s} {gib:5.2f} GiB  {oid[:16]}…")

    manifest = {
        # NOTE: bump when the runtime contract changes (keys read by
        # infra/local_model.py), not on every re-pin.
        "manifest_version": 1,
        "generated": date.today().isoformat(),
        "models": entries,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    print(f"wrote {OUT} ({len(entries)} models, "
          f"{sum(e['size'] for e in entries) / 1024**3:.1f} GiB ladder)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
