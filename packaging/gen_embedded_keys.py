"""Regenerate the obfuscated repo-root ``_embedded_keys.py``.

The shared in-box keys must never exist as plaintext constants — not in git
(they aren't) and not in the frozen bundle (PyInstaller ships the module as a
pyc inside the PYZ, whose constants anyone can dump with pyinstxtractor).
This script reads the CURRENT ``_embedded_keys.py`` (plaintext strings or an
older obfuscated form — both decode), re-encodes every ``SHARED_*`` value as
``(salt_b64, blob_b64)`` where ``blob = value XOR sha256(salt + counter)``
and rewrites the file in place.

Run it on the build machine whenever a shared key changes::

    python packaging/gen_embedded_keys.py

config.py decodes the pairs at import time (``_decode_shared_value``); a wrong
or tampered pair fails closed to "". The scheme is obfuscation, not
cryptography — it raises the bar from "run a script, read the key" to
"decompile the loader and replicate it", which is the most a client-side
embedded key can offer.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import secrets
import sys

ATTRS = [
    "SHARED_DEEPSEEK_API_KEY",
    "SHARED_PERPLEXITY_API_KEY",
    "SHARED_TMDB_API_KEY",
    "SHARED_OPENSUBTITLES_API_KEY",
    "SHARED_TPDB_API_KEY",
    "SHARED_STASHDB_API_KEY",
    "SHARED_OMDB_API_KEY",
    "SHARED_FANARTTV_API_KEY",
]

HEADER = '''"""LOCAL-ONLY build secret — gitignored, NEVER commit.

Read by config.py at import time; PyInstaller bundles this module into the
frozen app so the shipped setup.exe carries the shared keys while the GitHub
source never does. Values are OBFUSCATED (salt + XOR keystream, see
packaging/gen_embedded_keys.py and config._decode_shared_value) so no key
exists as a plaintext constant in the bundle. Delete this file and builds
simply come out keyless. Regenerate after editing with:
    python packaging/gen_embedded_keys.py
"""

# fmt: off
'''


def decode_shared_value(raw: object) -> str:
    """Mirror of config._decode_shared_value (kept dependency-free here)."""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, tuple) or len(raw) != 2:
        return ""
    try:
        salt = base64.b64decode(raw[0], validate=True)
        blob = base64.b64decode(raw[1], validate=True)
    except Exception:
        return ""
    pad = bytearray()
    counter = 0
    while len(pad) < len(blob):
        pad.extend(hashlib.sha256(salt + counter.to_bytes(4, "big")).digest())
        counter += 1
    value = bytes(b ^ p for b, p in zip(blob, bytes(pad))).decode("utf-8", "replace")
    return value if value and all(32 <= ord(c) < 127 for c in value) else ""


def encode_shared_value(value: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    pad = bytearray()
    counter = 0
    while len(pad) < len(value.encode("utf-8")):
        pad.extend(hashlib.sha256(salt + counter.to_bytes(4, "big")).digest())
        counter += 1
    blob = bytes(b ^ p for b, p in zip(value.encode("utf-8"), bytes(pad)))
    return base64.b64encode(salt).decode("ascii"), base64.b64encode(blob).decode("ascii")


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "_embedded_keys.py")
    if not os.path.isfile(path):
        print(f"_embedded_keys.py not found at {path}")
        return 1
    spec = importlib.util.spec_from_file_location("_embedded_keys_src", path)
    src = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(src)

    lines = [HEADER]
    empty = []
    for attr in ATTRS:
        value = decode_shared_value(getattr(src, attr, ""))
        if not value:
            empty.append(attr)
            lines.append(f'{attr} = ""')
            continue
        salt_b64, blob_b64 = encode_shared_value(value)
        lines.append(f'{attr} = ("{salt_b64}", "{blob_b64}")')
    lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))

    filled = [a for a in ATTRS if a not in empty]
    print(f"Re-obfuscated {len(filled)} key(s) into {path}")
    for attr in empty:
        print(f"  (empty slot left empty: {attr})")
    # Never print values or their encodings.
    return 0


if __name__ == "__main__":
    sys.exit(main())
