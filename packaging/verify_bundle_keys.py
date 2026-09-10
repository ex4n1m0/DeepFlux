"""Verify no shared key is recoverable from a built bundle / installer.

Release gate (run after PyInstaller + ISCC):

    python packaging/verify_bundle_keys.py [dist/DeepFlux] [dist/DeepFlux3.5.9Setup.exe]

Two checks:
1. RAW SCAN — every byte of every file in the bundle (and the setup exe) is
   searched for each decoded shared-key value plus generic key markers
   (``sk-``, ``pplx-``, long JWTs). Catches plaintext anywhere on disk.
2. PYZ SCAN — the PyInstaller PYZ archive is opened the way pyinstxtractor
   does it (TOC walk -> zlib entries -> marshal), every code object's
   ``co_consts`` tree is walked and all string constants are matched against
   the same markers. This is the "extract the pyc and dump the constants"
   attack; with obfuscated ``_embedded_keys.py`` it must come up empty.

Prints pass/fail per check; never prints a key value. Exit code 1 on any hit.
"""
from __future__ import annotations

import base64
import hashlib
import marshal
import os
import re
import sys
import zlib

CHUNK = 8 * 1024 * 1024

# Shape-only nets for key material we might not be able to enumerate (e.g. an
# old, rotated value left behind). Deliberately STRICT: the 3-byte prefixes
# alone match Slovak locale files, yt_dlp extractors and OpenSSH key-type
# strings ("sk-ssh-ed25519@openssh.com"), so a bare "sk-" substring is noise.
KEY_SHAPE_RE = re.compile(rb"(?:sk-[A-Za-z0-9_-]{20,}|pplx-[A-Za-z0-9]{20,})")


def _decode_shared_value(raw: object) -> str:
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


def load_secret_markers() -> list[bytes]:
    """Decoded shared keys from the local _embedded_keys.py, as bytes."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "_embedded_keys.py")
    markers: list[bytes] = []
    if os.path.isfile(path):
        import importlib.util
        spec = importlib.util.spec_from_file_location("_ek_verify", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for attr in dir(mod):
            if not attr.startswith("SHARED_"):
                continue
            value = _decode_shared_value(getattr(mod, attr))
            if value:
                markers.append(value.encode("utf-8"))
    return markers


def raw_scan(paths: list[str], markers: list[bytes]) -> list[str]:
    """Exact-value scan: any full key value appearing in any file's bytes."""
    hits: list[str] = []
    for top in paths:
        if not os.path.exists(top):
            print(f"  (missing, skipped: {top})")
            continue
        files = []
        if os.path.isfile(top):
            files = [top]
        else:
            for dirpath, _dirs, names in os.walk(top):
                files.extend(os.path.join(dirpath, n) for n in names)
        for f in files:
            try:
                with open(f, "rb") as fh:
                    while True:
                        chunk = fh.read(CHUNK)
                        if not chunk:
                            break
                        for m in markers:
                            if m in chunk:
                                hits.append(f"{f}: contains a full key value ({len(m)}B)")
                                break
            except OSError:
                pass
    return hits


def _walk_code_strings(code, stack: list | None = None):
    stack = stack or [code]
    while stack:
        obj = stack.pop()
        if not hasattr(obj, "co_consts"):
            continue
        for const in obj.co_consts:
            if isinstance(const, str):
                yield const
            elif hasattr(const, "co_consts"):
                stack.append(const)


def _match_markers(name: str, s: str, markers: list[bytes]) -> tuple[str, str] | None:
    """('FAIL', msg) for a full key value; ('REVIEW', msg) for key-shaped."""
    sb = s.encode("utf-8", "replace")
    for m in markers:
        if m in sb:
            return "FAIL", f"{name}: string constant contains a FULL key value ({len(m)}B)"
    match = KEY_SHAPE_RE.search(sb)
    if match:
        digest = hashlib.sha1(match.group(0)).hexdigest()[:8]
        return "REVIEW", (f"{name}: key-shaped constant "
                          f"({len(match.group(0))}B, sha1:{digest}) — verify it is "
                          f"third-party (yt_dlp extractor tokens, SPDX data), not ours")
    return None


def iter_pyz_strings(data: bytes):
    """Yield (module_name, string) for every string constant in a PYZ archive."""
    if data[:4] != b"PYZ\x00":
        return
    toc_offset = int.from_bytes(data[8:12], "big")
    toc = marshal.loads(data[toc_offset:])
    if isinstance(toc, dict):
        toc = list(toc.items())
    for entry in toc:
        if isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[1], tuple):
            name, (_typ, pos, length) = entry
        else:
            continue
        try:
            raw = zlib.decompress(data[pos:pos + length])
            code = marshal.loads(raw)
        except Exception:
            continue
        for s in _walk_code_strings(code):
            yield name, s


def iter_carchive_strings(exe_path: str):
    """Walk a PyInstaller CArchive (the pyinstxtractor attack) inside an exe.

    Yields (entry_name, string) for string constants of every embedded Python
    module: direct 'm'/'M'/'s' entries plus every module inside the embedded
    PYZ ('z') archive.
    """
    with open(exe_path, "rb") as fh:
        data = fh.read()
    magic = b"MEI\x0c\x0b\x0a\x0b\x0e"
    pos = data.rfind(magic)
    if pos < 0:
        return
    # cookie: magic(8) pkgLen(u32) toc(u32) tocLen(u32) pyver(i32) pylib(64s)
    import struct
    _magic, pkg_len, toc_off, toc_len, _pyver, _pylib = struct.unpack(
        "!8sIIIi64s", data[pos:pos + 88])
    toc_pos = len(data) - pkg_len + toc_off
    toc = data[toc_pos:toc_pos + toc_len]
    p = 0
    while p < len(toc):
        (entry_len,) = struct.unpack("!i", toc[p:p + 4])
        if entry_len <= 0:
            break
        # entry: len(i32) pos(u32) csize(u32) usize(u32) flag(u8) type(1c) name
        _entry_pos, _csize, _usize, _flag, typ = struct.unpack(
            "!IIIBc", toc[p + 4:p + 18])
        name = toc[p + 18:p + entry_len].rstrip(b"\x00").decode("utf-8", "replace")
        p += entry_len
        try:
            start = len(data) - pkg_len + _entry_pos
            blob = zlib.decompress(data[start:start + _csize]) if _flag else data[start:start + _csize]
        except Exception:
            continue
        if typ in (b"m", b"M", b"s"):
            for attempt in (0, 16):  # raw marshal, or behind a pyc header
                try:
                    code = marshal.loads(blob[attempt:])
                    for s in _walk_code_strings(code):
                        yield name, s
                    break
                except Exception:
                    continue
        elif typ == b"z":
            for modname, s in iter_pyz_strings(blob):
                yield f"{name}::{modname}", s


def _scan_iter(iterable, markers: list[bytes]) -> tuple[list[str], list[str]]:
    fails: list[str] = []
    reviews: list[str] = []
    for name, s in iterable:
        hit = _match_markers(name, s, markers)
        if hit:
            kind, msg = hit
            (fails if kind == "FAIL" else reviews).append(msg)
    return fails, reviews


def exe_scan(bundle_dir: str, markers: list[bytes]) -> tuple[list[str], list[str]]:
    fails: list[str] = []
    reviews: list[str] = []
    for dirpath, _dirs, names in os.walk(bundle_dir):
        for n in names:
            if not n.lower().endswith(".exe"):
                continue
            exe = os.path.join(dirpath, n)
            count = 0
            def gen():
                nonlocal count
                for name, s in iter_carchive_strings(exe):
                    count += 1
                    yield name, s
            f, r = _scan_iter(gen(), markers)
            fails += f
            reviews += r
            print(f"  scanned {count} string constants embedded in {os.path.basename(exe)}")
    return fails, reviews


def pyz_scan(bundle_dir: str, markers: list[bytes]) -> tuple[list[str], list[str]]:
    fails: list[str] = []
    reviews: list[str] = []
    for dirpath, _dirs, names in os.walk(bundle_dir):
        for n in names:
            if not n.endswith(".pyz"):
                continue
            pyz = os.path.join(dirpath, n)
            with open(pyz, "rb") as fh:
                data = fh.read()
            count = 0
            def gen():
                nonlocal count
                for modname, s in iter_pyz_strings(data):
                    count += 1
                    yield modname, s
            f, r = _scan_iter(gen(), markers)
            fails += f
            reviews += r
            print(f"  scanned {count} string constants in {os.path.basename(pyz)}")
    return fails, reviews


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bundle = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "dist", "DeepFlux")
    installer = sys.argv[2] if len(sys.argv) > 2 else None
    markers = load_secret_markers()
    if not any(len(m) > 6 for m in markers):
        print("No decoded shared keys available (keyless checkout?) — nothing to verify.")
        return 0

    targets = [bundle] + ([installer] if installer else [])
    print(f"[1/3] raw byte scan of {targets[0]}" + (f" + installer" if installer else ""))
    raw_hits = raw_scan(targets, markers)
    print(f"[2/3] embedded-PYZ / CArchive constants scan of {bundle} exes (pyinstxtractor attack)")
    exe_fails, exe_reviews = exe_scan(bundle, markers)
    print(f"[3/3] standalone .pyz constants scan of {bundle}")
    pyz_fails, pyz_reviews = pyz_scan(bundle, markers)

    for h in raw_hits + exe_fails + pyz_fails:
        print("FAIL:", h)
    for h in exe_reviews + pyz_reviews:
        print("REVIEW:", h)
    if raw_hits or exe_fails or pyz_fails:
        print("RESULT: FAIL — key material is recoverable from the build")
        return 1
    print("RESULT: PASS — no shared key value recoverable "
          f"({len(exe_reviews) + len(pyz_reviews)} key-shaped third-party constants to review above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
