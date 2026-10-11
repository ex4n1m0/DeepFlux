"""Generate the obfuscated ``_embedded_keys.py`` from environment variables.

This is the CI counterpart of ``packaging/gen_embedded_keys.py`` (which
re-encodes the file already present on a local build machine). The shared
keys must never live in the public git tree, but the Linux CI build embeds
the DeepSeek key by owner decision (2026-10-11) — the same exposure as the
public Windows setup exe. The workflow passes each key as a GitHub Actions
secret mapped to ``EMBED_<ATTR>`` (e.g. ``EMBED_SHARED_DEEPSEEK_API_KEY``);
this script re-encodes every provided value with the same salt+XOR
obfuscation the local generator uses and writes ``_embedded_keys.py`` at
the repo root, where the spec's conditional hiddenimport bundles it.

Slots without an env var are written empty, so a build without the secret
degrades to the previous keyless behaviour. Values are never printed.

Run (from the repo root, with secrets in the environment)::

    python packaging/gen_embedded_keys_from_env.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

from gen_embedded_keys import ATTRS, HEADER, encode_shared_value  # noqa: E402


def main() -> int:
    path = os.path.join(_ROOT, "_embedded_keys.py")
    if os.path.exists(path) and not os.environ.get("DF_OVERWRITE_EMBEDDED_KEYS"):
        # A local build machine's real key file lives here — regenerating
        # from partial env vars would DESTROY the other slots. CI checkouts
        # never have the file; only an explicit opt-in overwrites.
        print("_embedded_keys.py already exists; refusing to overwrite "
              "(set DF_OVERWRITE_EMBEDDED_KEYS=1 to force)")
        return 1
    lines = [HEADER]
    filled = []
    for attr in ATTRS:
        value = os.environ.get(f"EMBED_{attr}", "").strip()
        if value:
            salt_b64, blob_b64 = encode_shared_value(value)
            lines.append(f'{attr} = ("{salt_b64}", "{blob_b64}")')
            filled.append(attr)
        else:
            lines.append(f'{attr} = ""')
    lines.append("")
    if not filled:
        # No secrets provided — leave the tree exactly as it was so local
        # (keyless or locally-keyed) builds are untouched.
        print("_embedded_keys.py: no EMBED_* variables set; nothing written")
        return 0
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    print(f"_embedded_keys.py: embedded {len(filled)} key(s) from environment")
    return 0


if __name__ == "__main__":
    sys.exit(main())
