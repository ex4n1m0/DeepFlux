"""In-app update of the bundled yt-dlp (infra/ytdlp_overlay.py) and its probe
(dlmgr/ytdlp_update.py).

The import hook is exercised for real in this process against a tiny FAKE
yt_dlp package (the real one is 1,044 modules); the child-process start-up
check runs against the real venv package in a subprocess. The autouse
conftest fixture points DF_YTDLP_OVERLAY_DIR at a per-test temp dir, so the
real ~/.deeptorrent/ytdlp is never touched.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import importlib
import importlib.util
import io
import json
import marshal
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from config import DeeptorrentConfig
from dlmgr import ytdlp_update
from infra import ytdlp_overlay as ov

REPO_ROOT = Path(__file__).resolve().parents[1]
NEW, OLD = "2099.1.1", "2098.12.1"
NEW_SHA, OLD_SHA = "ab" * 32, "cd" * 32
# The real feed's release (website/deepflux/latest-ytdlp.json), used verbatim
# so the parse tests pin the production document's shape.
FEED = {
    "version": "2026.11.23",
    "file": "yt_dlp-2026.11.23-py3-none-any.whl",
    "size": 3186416,
    "sha256": "e9188e0c559905f7ee05acc1ba0b6fb3f310e7565bf6dea5f62b5300e0775e35",
    "url": ("https://files.pythonhosted.org/packages/30/1e/"
            "f4bf64ca733135e609696c6df77a6c22c6d4909b6e293e4563ae51ea335c/"
            "yt_dlp-2026.11.23-py3-none-any.whl"),
    "released": "2026-11-23",
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _process_state(monkeypatch):
    """The import decision happens once per process: reset it per test, and
    never let a fake yt_dlp leak into the other files of this pytest run.

    Other files in the same chunk build real MainWindows whose startup probe
    thread imports the REAL yt_dlp into this process — so purge it at SETUP
    too, otherwise every test here that expects the override would silently
    get the bundled copy (found by the release gate, 2026-11-26)."""
    monkeypatch.setattr(ov, "_decided", False)
    monkeypatch.setattr(ov, "_finder", None)
    saved = {n: m for n, m in sys.modules.items()
             if n == "yt_dlp" or n.startswith("yt_dlp.")}
    meta_path = list(sys.meta_path)
    for name in list(saved):
        del sys.modules[name]
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, ov._YtdlpOnlyFinder)]
    yield
    sys.meta_path[:] = meta_path
    for name in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.")]:
        del sys.modules[name]
    sys.modules.update(saved)


def _enable_overlay(monkeypatch, bundled="2000.1.1"):
    """Pretend to be a frozen Windows/Linux build with a readable bundle."""
    monkeypatch.setattr(ov, "supported", lambda: True)
    monkeypatch.setattr(ov, "bundled_version", lambda: bundled)


def _root() -> Path:
    return ov.overlay_dir()


def _under(module, release: Path) -> bool:
    return ov._is_under(getattr(module, "__file__", None), str(release))


def _fake_release(root: Path, version: str, sha: str, *, broken=False,
                  manifest=True, pointer=True) -> Path:
    """An installed release laid out the way install() leaves it."""
    release = root / "versions" / f"{version}-{sha[:8]}"
    pkg = release / "yt_dlp"
    (pkg / "sub").mkdir(parents=True)
    body = f"__version__ = {version!r}\nWHO = 'override'\n"
    if broken:
        body += "def broken(:\n"
    (pkg / "__init__.py").write_text(body, encoding="utf-8")
    (pkg / "version.py").write_text(f"__version__ = {version!r}\n", encoding="utf-8")
    (pkg / "helper.py").write_text("WHO = 'override'\n", encoding="utf-8")
    (pkg / "sub" / "__init__.py").write_text("WHO = 'override'\n", encoding="utf-8")
    if manifest:
        ov.write_json_atomic(release / "manifest.json", {
            "version": version, "sha256": sha, "tree_sha256": ov.tree_digest(pkg)})
    if pointer:
        ov.write_json_atomic(root / ov.POINTER_NAME,
                             {"version": version, "sha256": sha, "dir": release.name})
    return release


def _real_site_packages() -> str:
    spec = importlib.util.find_spec("yt_dlp")
    return os.path.dirname(list(spec.submodule_search_locations)[0])


def _package(version: str, *, broken=None) -> dict:
    files = {
        "yt_dlp/__init__.py": f"__version__ = {version!r}\nWHO = 'wheel'\n".encode(),
        "yt_dlp/version.py": f"__version__ = {version!r}\n".encode(),
        "yt_dlp/helper.py": b"WHO = 'wheel'\n",
        "yt_dlp/sub/__init__.py": b"WHO = 'wheel'\n",
        "yt_dlp/data/solver.js": b"// solver\n",
    }
    if broken:
        files[broken] = b"def broken(:\n"
    return files


def _record_row(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def _wheel_bytes(files: dict, version: str, *, record_text=None, record_count=1,
                 symlink=None) -> bytes:
    """A minimal but well-formed wheel: the package files + a RECORD whose
    last row is the digest-less self-row every real wheel carries."""
    dist = f"yt_dlp-{version}.dist-info"
    record_name = f"{dist}/RECORD"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            if name == symlink:
                info = zipfile.ZipInfo(name)
                info.external_attr = 0o120777 << 16  # symlink
                zf.writestr(info, data)
            else:
                zf.writestr(name, data)
        if record_text is None:
            rows = [_record_row(name, data) for name, data in files.items()]
            rows.append(f"{record_name},,")
            record_text = "\n".join(rows) + "\n"
        for _ in range(record_count):
            zf.writestr(record_name, record_text)
    return buffer.getvalue()


def _info(body: bytes, version="2099.1.1", *, sha=None, size=None, url=None) -> ov.YtdlpUpdateInfo:
    return ov.YtdlpUpdateInfo(
        version=version, file=f"yt_dlp-{version}-py3-none-any.whl",
        size=len(body) if size is None else size,
        sha256=hashlib.sha256(body).hexdigest() if sha is None else sha,
        url=url or f"https://files.pythonhosted.org/packages/aa/bb/{'c' * 64}/yt_dlp-{version}-py3-none-any.whl",
        released="2099-01-01")


class _FakeResponse:
    def __init__(self, body=b"", status=200, error=None, fail_at=None, url=ov.FEED_URL):
        self._body, self.status_code = body, status
        self._error, self._fail_at = error, fail_at
        self.url = url            # the effective URL after any redirects
        self.closed = False

    def raise_for_status(self):
        if self._error is not None:
            raise self._error
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for start in range(0, len(self._body), chunk_size):
            if self._fail_at is not None and start >= self._fail_at:
                raise OSError("connection reset by peer")
            yield self._body[start:start + chunk_size]

    def close(self):
        self.closed = True


def _serve(monkeypatch, body: bytes, **response_kwargs):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return _FakeResponse(body, **response_kwargs)

    monkeypatch.setattr("dlmgr.http_client.get", get)
    return calls


def _stub_selfcheck(monkeypatch, recorded=None, exc=None):
    def run(candidate):
        if recorded is not None:
            recorded.append(Path(candidate))
        if exc is not None:
            raise exc
        version = ov._read_version_file(str(Path(candidate) / "yt_dlp" / "version.py"))
        return {"ok": True, "version": version, "extractors": 1700}
    monkeypatch.setattr(ov, "_run_selfcheck", run)


def _install_happy(monkeypatch, version=NEW, **package_kwargs):
    """Drive install() end to end with a fake HTTP stream and a stubbed
    start-up check; returns (info, work root)."""
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(version, **package_kwargs), version)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)
    info = _info(body, version)
    return info, ov.install(info)


# ---------------------------------------------------------------------------
# The import hook
# ---------------------------------------------------------------------------

def test_without_an_override_the_bundled_copy_is_imported(monkeypatch):
    _enable_overlay(monkeypatch)
    module = ov.import_yt_dlp()
    assert hasattr(module, "YoutubeDL")          # the real package
    assert not _under(module, _root())
    assert not [f for f in sys.meta_path if isinstance(f, ov._YtdlpOnlyFinder)]
    assert ov.active_version() == ""
    assert ov.import_yt_dlp() is module          # idempotent


def test_newer_override_is_loaded_and_nothing_else_is_touched(monkeypatch):
    _enable_overlay(monkeypatch)
    release = _fake_release(_root(), NEW, NEW_SHA)
    path_before = list(sys.path)
    module = ov.import_yt_dlp()
    assert module.WHO == "override"
    assert Path(module.__file__).resolve() == (release / "yt_dlp" / "__init__.py").resolve()
    assert importlib.import_module("yt_dlp.helper").WHO == "override"
    assert importlib.import_module("yt_dlp.sub").WHO == "override"
    assert ov.active_version() == NEW
    assert sys.path == path_before                       # nothing added to sys.path
    assert isinstance(sys.meta_path[0], ov._YtdlpOnlyFinder)
    assert not json.__file__.startswith(str(_root()))    # unrelated names untouched
    assert ov.import_yt_dlp() is module


@pytest.mark.parametrize("bundled,version,sha", [
    (NEW, NEW, NEW_SHA),          # equal — the bundle wins
    ("2099.6.1", NEW, NEW_SHA),   # the bundle is newer (app update) — override retired
    ("", NEW, NEW_SHA),           # bundled version unreadable — fail safe
])
def test_only_a_strictly_newer_override_is_used(monkeypatch, bundled, version, sha):
    _enable_overlay(monkeypatch, bundled=bundled)
    _fake_release(_root(), version, sha)
    module = ov.import_yt_dlp()
    assert not getattr(module, "WHO", None) == "override"
    assert ov.active_version() == ""


def test_unsupported_platforms_never_use_an_override(monkeypatch):
    monkeypatch.setattr(ov, "supported", lambda: False)
    monkeypatch.setattr(ov, "bundled_version", lambda: "2000.1.1")
    _fake_release(_root(), NEW, NEW_SHA)
    module = ov.import_yt_dlp()
    assert getattr(module, "WHO", None) != "override"
    assert not [f for f in sys.meta_path if isinstance(f, ov._YtdlpOnlyFinder)]


@pytest.mark.parametrize("damage", [
    "pointer-dir-mismatch", "pointer-dir-traversal", "pointer-not-json",
    "manifest-missing", "manifest-version-mismatch",
    "version-file-mismatch", "init-missing",
])
def test_damaged_pointer_or_release_is_ignored(monkeypatch, damage):
    _enable_overlay(monkeypatch)
    root = _root()
    release = _fake_release(root, NEW, NEW_SHA)
    pointer_path = root / ov.POINTER_NAME
    if damage == "pointer-dir-mismatch":
        ov.write_json_atomic(pointer_path, {"version": NEW, "sha256": NEW_SHA,
                                            "dir": f"{NEW}-{'0' * 8}"})
    elif damage == "pointer-dir-traversal":
        ov.write_json_atomic(pointer_path, {"version": NEW, "sha256": NEW_SHA,
                                            "dir": "../../evil"})
    elif damage == "pointer-not-json":
        pointer_path.write_text("{not json", encoding="utf-8")
    elif damage == "manifest-missing":
        (release / "manifest.json").unlink()
    elif damage == "manifest-version-mismatch":
        ov.write_json_atomic(release / "manifest.json", {"version": OLD, "sha256": OLD_SHA})
    elif damage == "version-file-mismatch":
        (release / "yt_dlp" / "version.py").write_text("__version__ = '2097.1.1'\n",
                                                       encoding="utf-8")
    elif damage == "init-missing":
        (release / "yt_dlp" / "__init__.py").unlink()
    module = ov.import_yt_dlp()
    assert getattr(module, "WHO", None) != "override"
    assert ov.active_version() == ""
    assert not [f for f in sys.meta_path if isinstance(f, ov._YtdlpOnlyFinder)]


def test_broken_override_falls_back_to_the_bundle_and_is_switched_off(monkeypatch):
    _enable_overlay(monkeypatch)
    release = _fake_release(_root(), NEW, NEW_SHA, broken=True)
    module = ov.import_yt_dlp()                    # must NOT raise
    assert hasattr(module, "YoutubeDL")
    assert not _under(module, release)
    assert ov._finder is None
    assert not [f for f in sys.meta_path if isinstance(f, ov._YtdlpOnlyFinder)]
    # nothing the broken tree loaded may survive in sys.modules
    assert not [n for n, m in sys.modules.items()
                if (n == "yt_dlp" or n.startswith("yt_dlp.")) and _under(m, release)]
    skipped = ov.skipped()
    assert skipped["version"] == NEW and skipped["sha256"] == NEW_SHA
    assert skipped["stage"] == "load" and "SyntaxError" in skipped["error"]
    assert not (_root() / ov.POINTER_NAME).exists()
    assert ov.active_version() == ""
    # later imports in this process keep working on the bundle
    assert importlib.import_module("yt_dlp.version").__version__ != NEW


def test_release_damaged_after_activation_is_refused_before_its_code_runs(monkeypatch):
    """yt-dlp loads most extractors lazily, so the first-import net alone
    would not cover a release damaged AFTER it was switched on: the tree
    digest is checked before any of its code runs."""
    _enable_overlay(monkeypatch)
    release = _fake_release(_root(), NEW, NEW_SHA)
    (release / "yt_dlp" / "helper.py").write_text("WHO = 'tampered'\n", encoding="utf-8")
    module = ov.import_yt_dlp()
    assert getattr(module, "WHO", None) != "override"        # none of it ran
    assert not [n for n, m in sys.modules.items()
                if (n == "yt_dlp" or n.startswith("yt_dlp.")) and _under(m, release)]
    assert not [f for f in sys.meta_path if isinstance(f, ov._YtdlpOnlyFinder)]
    skipped = ov.skipped()
    assert skipped["stage"] == "load" and "integrity" in skipped["error"]
    assert ov.active_version() == ""


def test_release_without_a_tree_digest_is_refused(monkeypatch):
    """Fail closed: a manifest that lost its digest is not trusted."""
    _enable_overlay(monkeypatch)
    release = _fake_release(_root(), NEW, NEW_SHA)
    ov.write_json_atomic(release / "manifest.json", {"version": NEW, "sha256": NEW_SHA})
    module = ov.import_yt_dlp()
    assert getattr(module, "WHO", None) != "override"
    assert "integrity" in ov.skipped()["error"]


def test_finder_refuses_every_other_name(tmp_path):
    finder = ov._YtdlpOnlyFinder(str(tmp_path / "yt_dlp"))
    for name in ("os", "json", "yt_dlp.does_not_exist", "yt_dlp...x", "yt_dlp../os",
                 "yt_dlp.a..b", "yt_dlp_ejs", "yt_dlpx", "yt_dlpExtractor", "yt_dlp.",
                 "yt_dlp.\u0410", "yt_dlp.con"):
        assert finder.find_spec(name) is None, name


def test_finder_steps_aside_when_the_release_directory_is_gone(tmp_path):
    """Deleted out from under a running app: stop answering instead of
    failing every later yt_dlp import."""
    assert ov._YtdlpOnlyFinder(str(tmp_path / "yt_dlp")).find_spec("yt_dlp") is None


def test_finder_fails_loudly_when_the_package_itself_is_damaged(tmp_path):
    """Root present but no __init__.py: raise, so the first-import safety net
    switches the release off rather than silently loading another copy."""
    (tmp_path / "yt_dlp").mkdir()
    with pytest.raises(ModuleNotFoundError):
        ov._YtdlpOnlyFinder(str(tmp_path / "yt_dlp")).find_spec("yt_dlp")


def test_submodule_missing_from_the_override_is_not_resolved_elsewhere(monkeypatch):
    """`yt_dlp.utils` exists in the venv's real copy — if the override's tree
    is incomplete it must raise, never silently mix releases."""
    _enable_overlay(monkeypatch)
    _fake_release(_root(), NEW, NEW_SHA)
    ov.import_yt_dlp()
    assert importlib.util.find_spec("yt_dlp.nope") is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("yt_dlp.utils")


def test_retired_releases_are_removed_at_the_next_start_only(monkeypatch):
    _enable_overlay(monkeypatch)
    root = _root()
    active = _fake_release(root, NEW, NEW_SHA)
    old = _fake_release(root, OLD, OLD_SHA, pointer=False)
    notes = root / "versions" / "notes"
    notes.mkdir(parents=True)
    (notes / "keep.txt").write_text("keep", encoding="utf-8")
    loose = root / "versions" / "loose.bin"
    loose.write_bytes(b"keep")
    ov.import_yt_dlp()
    assert active.is_dir()
    assert not old.exists()                     # retired release collected
    assert (notes / "keep.txt").exists()         # anything not a release dir survives
    assert loose.exists()


def test_install_and_revert_never_delete_the_release_this_process_loaded(monkeypatch):
    _enable_overlay(monkeypatch)
    root = _root()
    loaded = _fake_release(root, NEW, NEW_SHA)
    ov.import_yt_dlp()
    assert importlib.import_module("yt_dlp.helper").WHO == "override"
    newer = _fake_release(root, "2099.6.1", "ef" * 32)   # an in-session install
    assert loaded.is_dir() and newer.is_dir()
    # lazy imports of the loaded release keep working while both exist
    assert importlib.import_module("yt_dlp.sub").WHO == "override"
    assert ov.revert() is True
    assert loaded.is_dir() and newer.is_dir()           # revert deletes nothing
    assert not (root / ov.POINTER_NAME).exists()
    # the NEXT start (a fresh decision) collects everything retired — a new
    # process has no finder installed and nothing imported, so mirror that
    monkeypatch.setattr(ov, "_decided", False)
    monkeypatch.setattr(ov, "_finder", None)
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, ov._YtdlpOnlyFinder)]
    for name in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.")]:
        del sys.modules[name]
    ov.import_yt_dlp()
    assert not loaded.exists() and not newer.exists()


def test_revert_records_the_choice_and_is_a_noop_when_unsupported(monkeypatch):
    _enable_overlay(monkeypatch)
    info_release = _fake_release(_root(), NEW, NEW_SHA)
    assert ov.revert() is True
    skipped = ov.skipped()
    assert skipped["stage"] == "user" and skipped["version"] == NEW
    info = ov.YtdlpUpdateInfo(version=NEW, file="x.whl", size=10**5,
                              sha256=NEW_SHA, url="https://files.pythonhosted.org/x")
    assert ov.is_skipped(info) is True
    monkeypatch.setattr(ov, "supported", lambda: False)
    assert ov.revert() is False


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

def _feed(**changes) -> bytes:
    return json.dumps({**FEED, **changes}).encode("utf-8")


def test_valid_feed_parses():
    info = ov.parse_feed(_feed())
    assert info is not None
    assert info.version == "2026.11.23" and info.size == 3186416
    assert info.sha256 == FEED["sha256"] and info.file == FEED["file"]
    assert info.dir_name == "2026.11.23-e9188e0c"


@pytest.mark.parametrize("changes", [
    {"version": "2026.11.23.232705.dev0"}, {"version": "not-a-version"}, {"version": ""},
    {"version": "2026"}, {"version": "2026.11.24"},          # file name no longer matches
    {"file": "../yt_dlp-2026.11.23-py3-none-any.whl"},
    {"file": "yt_dlp-2026.11.23-py3-none-any.whl.exe"},
    {"file": "yt_dlp-2026.11.23-py2-none-any.whl"},
    {"sha256": "XYZ"}, {"sha256": ""}, {"sha256": "ab" * 31},
    {"size": 0}, {"size": 10}, {"size": ov.MAX_WHEEL_BYTES + 1}, {"size": "many"},
    {"url": FEED["url"].replace("https://", "http://")},
    {"url": FEED["url"].replace("files.pythonhosted.org", "evil.example")},
    {"url": FEED["url"].replace("files.pythonhosted.org", "user:pw@files.pythonhosted.org")},
    {"url": FEED["url"].replace("files.pythonhosted.org", "files.pythonhosted.org:8443")},
    {"url": FEED["url"] + "?x=1"}, {"url": FEED["url"] + "#frag"},
    {"url": FEED["url"].replace("/packages/", "/other/")},
    {"url": FEED["url"].replace("/yt_dlp-2026.11.23", "/elsewhere/yt_dlp-2026.11.23")},
    {"url": ""},
])
def test_feed_mutations_are_rejected(changes):
    assert ov.parse_feed(_feed(**changes)) is None


def test_feed_garbage_is_rejected():
    assert ov.parse_feed(b"") is None
    assert ov.parse_feed(b"not json") is None
    assert ov.parse_feed(b"[]") is None
    assert ov.parse_feed(b'"2026.11.23"') is None
    assert ov.parse_feed(b"x" * (ov.FEED_BODY_LIMIT + 1)) is None


def test_committed_feed_is_valid_and_not_older_than_the_bundled_floor():
    path = REPO_ROOT / "website" / "deepflux" / "latest-ytdlp.json"
    info = ov.parse_feed(path.read_bytes())
    assert info is not None, "the committed feed must pass the app's own parser"
    floor = re.search(r"^yt-dlp>=(\S+)$",
                      (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8"),
                      re.M).group(1)
    assert not ov.is_newer(floor, info.version), \
        f"the pinned feed ({info.version}) lags the requirements floor ({floor})"


def test_fetch_feed_validates_the_body(monkeypatch):
    _serve(monkeypatch, _feed())
    assert ov.fetch_feed().version == "2026.11.23"
    _serve(monkeypatch, b"garbage")
    with pytest.raises(ov.OverlayError, match="malformed"):
        ov.fetch_feed()


def test_fetch_feed_refuses_an_oversized_body(monkeypatch):
    _serve(monkeypatch, b" " * (ov.FEED_BODY_LIMIT + 1))
    with pytest.raises(ov.OverlayError, match="larger"):
        ov.fetch_feed()


def test_fetch_feed_refuses_a_redirected_origin(monkeypatch):
    """The feed is the one payload with no digest of its own, so the origin we
    end up on after redirects must still be ours."""
    _serve(monkeypatch, _feed(), url="https://evil.example/latest-ytdlp.json")
    with pytest.raises(ov.OverlayError, match="unexpected origin"):
        ov.fetch_feed()


# ---------------------------------------------------------------------------
# Wheel extraction
# ---------------------------------------------------------------------------

def _extract(monkeypatch, body: bytes, tmp_path, **info_kwargs):
    wheel = tmp_path / "pkg.whl"
    wheel.write_bytes(body)
    candidate = tmp_path / "candidate"
    return ov._extract_wheel(wheel, candidate), candidate


def test_extract_writes_the_package_and_its_data_files(tmp_path):
    body = _wheel_bytes(_package(NEW), NEW)
    count, candidate = _extract(None, body, tmp_path)
    assert count == len(_package(NEW))
    assert (candidate / "yt_dlp" / "__init__.py").is_file()
    assert (candidate / "yt_dlp" / "data" / "solver.js").read_bytes() == b"// solver\n"
    assert not (candidate / f"yt_dlp-{NEW}.dist-info").exists()   # metadata is not unpacked


@pytest.mark.parametrize("name", [
    "../evil.py", "/abs.py", "yt_dlp/../../evil.py", "yt_dlp\\..\\evil.py",
    "C:/evil.py", "yt_dlp/./x.py", "yt_dlp//x.py", "yt_dlp/con.py",
    "yt_dlp/NUL.py", "yt_dlp/trailing. /x.py", "yt_dlp/ctl\x01.py",
])
def test_extract_refuses_unsafe_member_names(tmp_path, name):
    files = _package(NEW)
    files[name] = b"x = 1\n"
    body = _wheel_bytes(files, NEW)
    with pytest.raises(ov.OverlayError, match="unsafe path"):
        _extract(None, body, tmp_path)
    # nothing escaped the candidate dir
    assert not (tmp_path.parent / "evil.py").exists()


def test_extract_refuses_symlink_members(tmp_path):
    body = _wheel_bytes(_package(NEW), NEW, symlink="yt_dlp/helper.py")
    with pytest.raises(ov.OverlayError, match="symbolic link"):
        _extract(None, body, tmp_path)


def test_extract_refuses_too_many_or_too_large_wheels(tmp_path, monkeypatch):
    body = _wheel_bytes(_package(NEW), NEW)
    monkeypatch.setattr(ov, "MAX_MEMBERS", 3)
    with pytest.raises(ov.OverlayError, match="too many entries"):
        _extract(None, body, tmp_path)
    monkeypatch.setattr(ov, "MAX_MEMBERS", 5000)
    monkeypatch.setattr(ov, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(ov.OverlayError, match="unpacks larger"):
        _extract(None, body, tmp_path)


def test_extract_checks_every_file_against_the_record(tmp_path):
    files = _package(NEW)
    rows = [_record_row(n, d) for n, d in files.items() if n != "yt_dlp/helper.py"]
    rows.append(_record_row("yt_dlp/helper.py", b"TAMPERED"))     # wrong digest
    rows.append(f"yt_dlp-{NEW}.dist-info/RECORD,,")
    body = _wheel_bytes(files, NEW, record_text="\n".join(rows) + "\n")
    with pytest.raises(ov.OverlayError, match="RECORD digest"):
        _extract(None, body, tmp_path)


def test_extract_refuses_members_missing_from_the_record(tmp_path):
    files = _package(NEW)
    rows = [_record_row(n, d) for n, d in files.items() if n != "yt_dlp/helper.py"]
    rows.append(f"yt_dlp-{NEW}.dist-info/RECORD,,")
    body = _wheel_bytes(files, NEW, record_text="\n".join(rows) + "\n")
    with pytest.raises(ov.OverlayError, match="not listed"):
        _extract(None, body, tmp_path)


def test_extract_refuses_a_record_size_that_lies(tmp_path):
    files = _package(NEW)
    rows = [f"{n},sha256={base64.urlsafe_b64encode(hashlib.sha256(d).digest()).rstrip(b'=').decode()},1"
            for n, d in files.items()]
    rows.append(f"yt_dlp-{NEW}.dist-info/RECORD,,")
    body = _wheel_bytes(files, NEW, record_text="\n".join(rows) + "\n")
    with pytest.raises(ov.OverlayError, match="RECORD digest"):
        _extract(None, body, tmp_path)


def test_extract_needs_exactly_one_record(tmp_path):
    with pytest.raises(ov.OverlayError, match="no single RECORD"):
        _extract(None, _wheel_bytes(_package(NEW), NEW, record_count=2), tmp_path)
    with pytest.raises(ov.OverlayError, match="no single RECORD"):
        _extract(None, _wheel_bytes(_package(NEW), NEW, record_count=0), tmp_path)


def test_extract_refuses_a_file_that_is_not_a_wheel(tmp_path):
    with pytest.raises(ov.OverlayError, match="not a valid wheel"):
        _extract(None, b"this is not a zip file", tmp_path)


def test_extract_refuses_an_absurd_single_file_size(tmp_path, monkeypatch):
    """A RECORD that claims an enormous file is refused before it is read."""
    monkeypatch.setattr(ov, "MAX_FILE_BYTES", 4)
    with pytest.raises(ov.OverlayError, match="implausible size"):
        _extract(None, _wheel_bytes(_package(NEW), NEW), tmp_path)


def test_extract_needs_a_complete_package(tmp_path):
    body = _wheel_bytes({"yt_dlp/helper.py": b"x = 1\n"}, NEW)
    with pytest.raises(ov.OverlayError, match="does not contain the yt_dlp package"):
        _extract(None, body, tmp_path)


# ---------------------------------------------------------------------------
# install()
# ---------------------------------------------------------------------------

def test_install_happy_path_switches_on_a_verified_release(monkeypatch, tmp_path):
    checked = []
    _enable_overlay(monkeypatch)
    old_release = _fake_release(_root(), OLD, OLD_SHA, pointer=False)
    body = _wheel_bytes(_package(NEW), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch, recorded=checked)
    info = _info(body)
    result = ov.install(info)

    assert result["version"] == NEW
    release = _root() / "versions" / info.dir_name
    assert Path(result["dir"]) == release
    assert (release / "yt_dlp" / "__init__.py").is_file()
    assert (release / "yt_dlp" / "data" / "solver.js").is_file()
    assert checked and checked[0].name == "release"
    assert checked[0].parent.parent == _root() / "staging"   # checked while still in staging
    # pre-compiled, and the bytecode records the FINAL path (not staging)
    pyc = next((release / "yt_dlp" / "__pycache__").glob("__init__.*.pyc"))
    code = marshal.loads(pyc.read_bytes()[16:])
    assert code.co_filename.startswith(str(release))
    # pointer + manifest
    pointer = json.loads((_root() / ov.POINTER_NAME).read_text(encoding="utf-8"))
    assert pointer["version"] == NEW and pointer["dir"] == info.dir_name
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sha256"] == info.sha256 and manifest["extractors"] == 1700
    assert manifest["tree_sha256"] == ov.tree_digest(release / "yt_dlp")
    assert ov.active_version() == NEW
    # staging cleaned, the previously loaded release NOT deleted, no markers
    assert list((_root() / "staging").iterdir()) == []
    assert old_release.is_dir()
    assert not (_root() / ov.SKIP_NAME).exists()
    assert not (_root() / ov.TRANSIENT_NAME).exists()


def test_install_refuses_a_sha_mismatch_and_counts_it_as_local(monkeypatch, tmp_path):
    """A digest mismatch never activates anything. It is counted rather than
    recorded at once: a TLS-intercepting proxy produces it on every attempt
    and must not burn the release permanently (security review m4)."""
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(NEW), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)
    with pytest.raises(ov.TransientUpdateError, match="sha256 mismatch"):
        ov.install(_info(body, sha="0" * 64))
    assert not (_root() / ov.POINTER_NAME).exists()
    assert list((_root() / "staging").iterdir()) == []
    counter = json.loads((_root() / ov.TRANSIENT_NAME).read_text(encoding="utf-8"))
    assert counter["count"] == 1 and counter["sha256"] == "0" * 64
    assert not (_root() / ov.SKIP_NAME).exists()


def test_install_refuses_an_oversized_stream(monkeypatch):
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(NEW), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)
    with pytest.raises(ov.TransientUpdateError, match="larger than the pinned size"):
        ov.install(_info(body, size=len(body) - 1))
    assert not (_root() / ov.POINTER_NAME).exists()


def test_a_failed_install_leaves_a_healthy_active_release_alone(monkeypatch):
    """Security review M1: recording a FAILED release must not switch off the
    release that is currently working."""
    _enable_overlay(monkeypatch)
    active = _fake_release(_root(), OLD, OLD_SHA)          # healthy, in use
    body = _wheel_bytes(_package(NEW, broken="yt_dlp/helper.py"), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)
    with pytest.raises(ov.OverlayError, match="does not compile"):
        ov.install(_info(body))
    skipped = ov.skipped()
    assert skipped["version"] == NEW and skipped["stage"] == "install"
    # the healthy release is still pointed at and still active
    assert ov._pointer()["version"] == OLD
    assert ov.active_version() == OLD
    assert active.is_dir()


def test_install_refuses_a_recorded_release_unless_forced(monkeypatch):
    """Automatic mode must not re-download a release this machine rejected;
    the user's own click (force) may retry it."""
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(NEW), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)
    info = _info(body)
    ov._mark_skipped("selfcheck", "no video extractors were registered",
                     info.version, info.sha256)
    assert ov.is_skipped(info)
    with pytest.raises(ov.OverlayError, match="set aside"):
        ov.install(info)
    assert not (_root() / ov.POINTER_NAME).exists()
    result = ov.install(info, force=True)
    assert result["version"] == NEW
    assert ov.active_version() == NEW
    assert not (_root() / ov.SKIP_NAME).exists()      # success clears the record


def test_a_second_install_in_the_same_process_is_refused(monkeypatch):
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(NEW), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)
    assert ov._install_lock.acquire(blocking=False)
    try:
        with pytest.raises(ov.TransientUpdateError, match="already running"):
            ov.install(_info(body))
    finally:
        ov._install_lock.release()
    assert not (_root() / ov.POINTER_NAME).exists()


def test_retired_releases_survive_while_another_window_is_open(monkeypatch):
    """A second DeepFlux window may still be importing lazily from the release
    it started with (security review m3)."""
    import infra.updater as updater
    _enable_overlay(monkeypatch)
    root = _root()
    active = _fake_release(root, NEW, NEW_SHA)
    old = _fake_release(root, OLD, OLD_SHA, pointer=False)
    monkeypatch.setattr(updater, "other_instances_running", lambda: 1)
    ov.import_yt_dlp()
    assert active.is_dir() and old.is_dir()          # nothing collected
    # a fresh decision with no other instance collects the retired release
    monkeypatch.setattr(updater, "other_instances_running", lambda: 0)
    monkeypatch.setattr(ov, "_decided", False)
    monkeypatch.setattr(ov, "_finder", None)
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, ov._YtdlpOnlyFinder)]
    for name in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.")]:
        del sys.modules[name]
    ov.import_yt_dlp()
    assert active.is_dir() and not old.exists()


def test_install_treats_network_failures_as_transient(monkeypatch):
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(NEW), NEW)
    _stub_selfcheck(monkeypatch)          # never reached; keeps a stray success off the real child
    # incomplete body
    _serve(monkeypatch, body)
    with pytest.raises(ov.TransientUpdateError, match="incomplete"):
        ov.install(_info(body, size=len(body) + 100))
    assert not (_root() / ov.SKIP_NAME).exists()
    assert not (_root() / ov.TRANSIENT_NAME).exists()
    # mid-stream reset: small chunks so the failure lands between two writes
    monkeypatch.setattr(ov, "DOWNLOAD_CHUNK", 8)
    _serve(monkeypatch, body, fail_at=8)
    with pytest.raises(ov.TransientUpdateError, match="download failed"):
        ov.install(_info(body))
    assert not (_root() / ov.SKIP_NAME).exists()
    assert not (_root() / ov.POINTER_NAME).exists()
    monkeypatch.setattr(ov, "DOWNLOAD_CHUNK", 256 * 1024)
    # HTTP error
    _serve(monkeypatch, b"", status=503)
    with pytest.raises(ov.TransientUpdateError):
        ov.install(_info(body))
    assert not (_root() / ov.POINTER_NAME).exists()


def test_install_honours_cancel(monkeypatch):
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(NEW), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ov._Cancelled):
        ov.install(_info(body), cancel=cancel)
    assert not (_root() / ov.POINTER_NAME).exists()
    assert list((_root() / "staging").iterdir()) == []


def test_install_refuses_a_release_that_does_not_compile(monkeypatch):
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(NEW, broken="yt_dlp/helper.py"), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)
    with pytest.raises(ov.OverlayError, match="does not compile"):
        ov.install(_info(body))
    skipped = ov.skipped()
    assert skipped["stage"] == "install" and "SyntaxError" in skipped["error"]
    assert not (_root() / ov.POINTER_NAME).exists()


def test_install_refuses_a_release_that_fails_its_start_up_check(monkeypatch):
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(NEW), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch, exc=ov._SelfcheckError(
        "the new downloader failed its start-up check (no video extractors were registered)"))
    with pytest.raises(ov.OverlayError, match="start-up check"):
        ov.install(_info(body))
    assert ov.skipped()["stage"] == "selfcheck"
    assert not (_root() / ov.POINTER_NAME).exists()


def test_install_refuses_a_version_that_disagrees_with_the_feed(monkeypatch):
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package("2099.2.2"), NEW)      # wheel says another version
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)
    with pytest.raises(ov.OverlayError, match="different version"):
        ov.install(_info(body))


def test_local_transient_failures_are_counted_then_give_up(monkeypatch):
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(NEW), NEW)
    info = _info(body)
    for attempt in range(ov.TRANSIENT_LIMIT):
        _serve(monkeypatch, body)
        _stub_selfcheck(monkeypatch,
                        exc=ov._LocalTransientError("the start-up check ended without a result"))
        with pytest.raises(ov.TransientUpdateError):
            ov.install(info)
        if attempt + 1 < ov.TRANSIENT_LIMIT:
            counter = json.loads((_root() / ov.TRANSIENT_NAME).read_text(encoding="utf-8"))
            assert counter["count"] == attempt + 1
            assert not (_root() / ov.SKIP_NAME).exists()
    skipped = ov.skipped()
    assert skipped["stage"] == "install" and f"failed {ov.TRANSIENT_LIMIT} times" in skipped["error"]
    assert ov.is_skipped(info)
    assert not (_root() / ov.TRANSIENT_NAME).exists()


def test_a_publish_failure_counts_as_local_transient(monkeypatch):
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(NEW), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)

    def _blocked(candidate, final, info):
        raise ov._LocalTransientError("could not store the new downloader (sharing violation)")

    monkeypatch.setattr(ov, "_publish", _blocked)
    with pytest.raises(ov.TransientUpdateError, match="sharing violation"):
        ov.install(_info(body))
    counter = json.loads((_root() / ov.TRANSIENT_NAME).read_text(encoding="utf-8"))
    assert counter["count"] == 1
    assert not (_root() / ov.SKIP_NAME).exists()


def test_install_reuses_an_identical_release_another_window_installed(monkeypatch):
    _enable_overlay(monkeypatch)
    body = _wheel_bytes(_package(NEW), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)
    info = _info(body)
    first = ov.install(info)
    second_body = body
    _serve(monkeypatch, second_body)
    second = ov.install(info)
    assert first["dir"] == second["dir"]


def test_install_requires_a_supported_build_with_a_readable_bundle(monkeypatch):
    monkeypatch.setattr(ov, "supported", lambda: False)
    body = _wheel_bytes(_package(NEW), NEW)
    with pytest.raises(ov.OverlayError, match="installed app"):
        ov.install(_info(body))
    _enable_overlay(monkeypatch, bundled="")
    with pytest.raises(ov.OverlayError, match="cannot read its bundled"):
        ov.install(_info(body))


def test_successful_install_clears_previous_markers(monkeypatch):
    _enable_overlay(monkeypatch)
    root = _root()
    ov._mark_skipped("user", "", NEW, "0" * 64)
    body = _wheel_bytes(_package(NEW), NEW)
    _serve(monkeypatch, body)
    _stub_selfcheck(monkeypatch)
    ov.install(_info(body), force=True)      # the user's own click retries
    assert not (root / ov.SKIP_NAME).exists()


# ---------------------------------------------------------------------------
# The child-process start-up check
# ---------------------------------------------------------------------------

def test_start_up_check_passes_for_the_real_package():
    site = Path(_real_site_packages())
    result_path = Path(str(site) + ".selfcheck.json")
    try:
        result = ov._run_selfcheck(site)
    finally:
        result_path.unlink(missing_ok=True)
    assert result["ok"] is True
    assert result["version"] == ov._read_version_file(str(site / "yt_dlp" / "version.py"))
    assert result["extractors"] > 100
    assert not result_path.exists()      # the parent cleans the result file up


def test_start_up_check_refuses_a_broken_candidate(tmp_path):
    bad = tmp_path / "bad"
    (bad / "yt_dlp").mkdir(parents=True)
    (bad / "yt_dlp" / "__init__.py").write_text("def broken(:\n", encoding="utf-8")
    (bad / "yt_dlp" / "version.py").write_text("__version__ = '2099.1.1'\n", encoding="utf-8")
    with pytest.raises(ov._SelfcheckError, match="SyntaxError"):
        ov._run_selfcheck(bad)


def test_start_up_check_refuses_a_tree_that_leaks_to_another_copy(tmp_path):
    """The candidate's __init__ imports a module the candidate lacks: it must
    fail LOUDLY instead of resolving from the venv's real yt_dlp."""
    incomplete = tmp_path / "incomplete"
    (incomplete / "yt_dlp").mkdir(parents=True)
    (incomplete / "yt_dlp" / "__init__.py").write_text(
        "import yt_dlp.utils\n__version__ = '2099.1.1'\n", encoding="utf-8")
    (incomplete / "yt_dlp" / "version.py").write_text(
        "__version__ = '2099.1.1'\n", encoding="utf-8")
    with pytest.raises(ov._SelfcheckError, match="No module named"):
        ov._run_selfcheck(incomplete)


def test_start_up_check_timeout_is_transient(monkeypatch):
    monkeypatch.setattr(ov, "SELFCHECK_TIMEOUT", 0.001)
    site = Path(_real_site_packages())
    try:
        with pytest.raises(ov._LocalTransientError, match="in time"):
            ov._run_selfcheck(site)
    finally:
        Path(str(site) + ".selfcheck.json").unlink(missing_ok=True)


def test_start_up_check_crash_without_a_result_is_transient(monkeypatch, tmp_path):
    candidate = tmp_path / "cand"
    (candidate / "yt_dlp").mkdir(parents=True)
    (candidate / "yt_dlp" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(ov, "_selfcheck_command",
                        lambda path: [sys.executable, "-c", "import sys; sys.exit(3)"])
    with pytest.raises(ov._LocalTransientError, match="without a result"):
        ov._run_selfcheck(candidate)


def test_main_routes_the_selfcheck_flag_before_any_logging(tmp_path, monkeypatch):
    import main
    bad = tmp_path / "bad"
    (bad / "yt_dlp").mkdir(parents=True)
    (bad / "yt_dlp" / "__init__.py").write_text("def broken(:\n", encoding="utf-8")
    (bad / "yt_dlp" / "version.py").write_text("__version__ = '2099.1.1'\n", encoding="utf-8")
    monkeypatch.setattr(main, "_enable_crash_log", lambda: None)
    monkeypatch.setattr(main, "_configure_logging",
                        lambda level: pytest.fail("the child must not set up logging"))
    monkeypatch.setattr(sys, "argv", ["DeepFlux.exe", "--ytdlp-selfcheck", str(bad)])
    with pytest.raises(SystemExit) as stop:
        main.main()
    assert stop.value.code == 1
    result = json.loads(Path(str(bad) + ".selfcheck.json").read_text(encoding="utf-8"))
    assert result["ok"] is False and "SyntaxError" in result["error"]


def test_selfcheck_main_never_raises_and_reports_exit_codes(tmp_path):
    missing = tmp_path / "nothing-here"
    assert ov.selfcheck_main([str(missing)]) == 1
    result = json.loads(Path(str(missing) + ".selfcheck.json").read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert ov.selfcheck_main([]) == 1        # no argument at all


# ---------------------------------------------------------------------------
# Module hygiene
# ---------------------------------------------------------------------------

def test_overlay_module_imports_nothing_heavy_at_module_level():
    """The module is imported by the start-up-check child, so it must stay
    stdlib-only. Compared against a bare interpreter: this venv's
    sitecustomize imports PySide6.QtCore on purpose (the Qt-first guard), so
    the baseline — not an empty list — is the reference."""
    forbidden = ("dlmgr", "config", "requests", "PySide6", "yt_dlp", "curl_cffi",
                 "libtorrent", "agent", "gui")
    code = ("import sys; {imp} "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in {names!r}))")

    def _modules(imp):
        proc = subprocess.run(
            [sys.executable, "-c", code.format(imp=imp, names=forbidden)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, proc.stderr[-500:]
        return ast.literal_eval(proc.stdout.strip().splitlines()[-1])

    assert _modules("") == _modules("import infra.ytdlp_overlay;")


def test_app_code_imports_yt_dlp_only_through_the_overlay_helper():
    offenders = []
    roots = [REPO_ROOT / name for name in
             ("agent", "dlmgr", "engine", "gui", "infra", "ircmgr", "iptv",
              "native_messaging", "packaging")]
    files = [p for root in roots if root.is_dir() for p in root.rglob("*.py")]
    files.append(REPO_ROOT / "main.py")
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if name == "yt_dlp" or name.startswith("yt_dlp."):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []


# ---------------------------------------------------------------------------
# The probe (dlmgr/ytdlp_update.py)
# ---------------------------------------------------------------------------

def _cfg(tmp_path, **download):
    config = DeeptorrentConfig()
    for key, value in download.items():
        setattr(config.download, key, value)
    return config, str(tmp_path / "config.json")


@pytest.fixture
def quiet_import(monkeypatch):
    monkeypatch.setattr(ov, "import_yt_dlp", lambda: None)


def test_probe_uses_the_pinned_feed_when_the_app_can_update(monkeypatch, tmp_path, quiet_import):
    config, path = _cfg(tmp_path)
    info = ov.YtdlpUpdateInfo(**{k: v for k, v in FEED.items() if k != "released"},
                              released=FEED["released"])
    monkeypatch.setattr(ov, "can_override", lambda: True)
    monkeypatch.setattr(ov, "fetch_feed", lambda timeout=10.0: info)
    monkeypatch.setattr(ytdlp_update, "installed_version", lambda: "2000.1.1")
    result = ytdlp_update.maybe_check_update(config, path)
    assert result["can_update"] is True and result["latest"] == "2026.11.23"
    assert result["notice"] is True and result["info"] is info
    assert config.download.ytdlp_last_check > 0


def test_probe_is_silent_when_the_pinned_release_is_not_newer(monkeypatch, tmp_path, quiet_import):
    config, path = _cfg(tmp_path)
    info = ov.parse_feed(_feed())
    monkeypatch.setattr(ov, "can_override", lambda: True)
    monkeypatch.setattr(ov, "fetch_feed", lambda timeout=10.0: info)
    monkeypatch.setattr(ytdlp_update, "installed_version", lambda: "2026.11.23")
    assert ytdlp_update.maybe_check_update(config, path) is None
    assert config.download.ytdlp_last_check > 0        # the gate still ticks


def test_probe_is_silent_for_a_skipped_release(monkeypatch, tmp_path, quiet_import):
    config, path = _cfg(tmp_path)
    info = ov.parse_feed(_feed())
    ov._mark_skipped("user", "", info.version, info.sha256)
    monkeypatch.setattr(ov, "can_override", lambda: True)
    monkeypatch.setattr(ov, "fetch_feed", lambda timeout=10.0: info)
    monkeypatch.setattr(ytdlp_update, "installed_version", lambda: "2000.1.1")
    assert ytdlp_update.maybe_check_update(config, path) is None


def test_a_release_that_failed_still_reports_but_is_not_auto_installed(
        monkeypatch, tmp_path, quiet_import):
    """Security review m4: a release this machine could not use must not go
    quiet — the user still hears about it and can retry by hand, while
    automatic mode leaves it alone."""
    config, path = _cfg(tmp_path, ytdlp_auto_update=True)
    info = ov.parse_feed(_feed())
    ov._mark_skipped("selfcheck", "no video extractors were registered",
                     info.version, info.sha256)
    monkeypatch.setattr(ov, "can_override", lambda: True)
    monkeypatch.setattr(ov, "fetch_feed", lambda timeout=10.0: info)
    monkeypatch.setattr(ytdlp_update, "installed_version", lambda: "2000.1.1")
    installs = []
    monkeypatch.setattr(ov, "install",
                        lambda i, **kw: installs.append((i, kw)) or {"version": i.version})
    result = ytdlp_update.check_and_maybe_update(config, path)
    assert result["can_update"] is True and result["notice"] is True
    assert installs == [] and "updated" not in result


def test_forced_check_installs_in_automatic_mode(monkeypatch, tmp_path, quiet_import):
    """`force` bypasses the daily gate only — with automatic mode on it
    installs, which is what "automatic" means (architecture review F16)."""
    config, path = _cfg(tmp_path, ytdlp_auto_update=True, ytdlp_last_check=time.time())
    info = ov.parse_feed(_feed())
    monkeypatch.setattr(ov, "can_override", lambda: True)
    monkeypatch.setattr(ov, "fetch_feed", lambda timeout=10.0: info)
    monkeypatch.setattr(ytdlp_update, "installed_version", lambda: "2000.1.1")
    installs = []
    monkeypatch.setattr(ov, "install", lambda i, **kw: installs.append(i) or {"version": i.version})
    assert ytdlp_update.maybe_check_update(config, path) is None      # daily gate
    result = ytdlp_update.check_and_maybe_update(config, path, force=True)
    assert installs == [info] and result["updated"] == info.version


def test_probe_falls_back_to_pypi_when_the_feed_is_unavailable(monkeypatch, tmp_path, quiet_import):
    config, path = _cfg(tmp_path)
    monkeypatch.setattr(ov, "can_override", lambda: True)

    def _boom(timeout=10.0):
        raise ov.OverlayError("yt-dlp feed is malformed")

    monkeypatch.setattr(ov, "fetch_feed", _boom)
    monkeypatch.setattr(ytdlp_update, "fetch_latest_version", lambda timeout=10.0: "2099.9.9")
    monkeypatch.setattr(ytdlp_update, "installed_version", lambda: "2000.1.1")
    result = ytdlp_update.maybe_check_update(config, path)
    assert result["can_update"] is False and result["latest"] == "2099.9.9"
    assert result["frozen"] is False                    # dev build
    assert config.download.ytdlp_last_check > 0


def test_probe_runs_for_automatic_mode_without_the_warning(monkeypatch, tmp_path, quiet_import):
    config, path = _cfg(tmp_path, youtube_update_check=False, ytdlp_auto_update=True)
    info = ov.parse_feed(_feed())
    monkeypatch.setattr(ov, "can_override", lambda: True)
    monkeypatch.setattr(ov, "fetch_feed", lambda timeout=10.0: info)
    monkeypatch.setattr(ytdlp_update, "installed_version", lambda: "2000.1.1")
    result = ytdlp_update.maybe_check_update(config, path)
    assert result["notice"] is False and result["can_update"] is True


def test_probe_needs_at_least_one_switch_on(monkeypatch, tmp_path):
    config, path = _cfg(tmp_path, youtube_update_check=False, ytdlp_auto_update=False)
    fetch = mock.Mock(side_effect=AssertionError("must not probe"))
    monkeypatch.setattr(ov, "fetch_feed", fetch)
    monkeypatch.setattr(ytdlp_update, "fetch_latest_version", fetch)
    assert ytdlp_update.maybe_check_update(config, path) is None
    fetch.assert_not_called()


def test_automatic_mode_installs_only_with_the_switch_on(monkeypatch, tmp_path, quiet_import):
    config, path = _cfg(tmp_path)
    info = ov.parse_feed(_feed())
    monkeypatch.setattr(ov, "can_override", lambda: True)
    monkeypatch.setattr(ov, "fetch_feed", lambda timeout=10.0: info)
    monkeypatch.setattr(ytdlp_update, "installed_version", lambda: "2000.1.1")
    installs = []
    monkeypatch.setattr(ov, "install", lambda i, **kw: installs.append(i) or {"version": i.version})

    result = ytdlp_update.check_and_maybe_update(config, path)
    assert installs == [] and "updated" not in result          # ask mode is the default

    config.download.ytdlp_auto_update = True
    config.download.ytdlp_last_check = 0.0
    result = ytdlp_update.check_and_maybe_update(config, path)
    assert installs == [info] and result["updated"] == info.version

    # a failed install is reported, never raised into the startup thread
    config.download.ytdlp_last_check = 0.0
    monkeypatch.setattr(ov, "install",
                        mock.Mock(side_effect=ov.OverlayError("integrity check failed")))
    result = ytdlp_update.check_and_maybe_update(config, path)
    assert "integrity check failed" in result["update_error"] and "updated" not in result


def test_installed_version_reads_the_disk_without_importing_yt_dlp():
    code = ("import sys; from dlmgr import ytdlp_update; "
            "print(ytdlp_update.installed_version(), 'yt_dlp' in sys.modules)")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    version, imported = proc.stdout.strip().rsplit(" ", 1)
    assert imported == "False"
    assert ov._VERSION_RE.match(version)


# ---------------------------------------------------------------------------
# Config + agent guard
# ---------------------------------------------------------------------------

def test_auto_update_flag_defaults_off_and_round_trips(tmp_path):
    config = DeeptorrentConfig()
    assert config.download.ytdlp_auto_update is False
    path = str(tmp_path / "config.json")
    config.download.ytdlp_auto_update = True
    config.to_file(path)
    assert DeeptorrentConfig.from_file(path).download.ytdlp_auto_update is True


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("false", False), ('"yes"', False), ('"false"', False),
    ("0", False), ("1", False), ("null", False), ("[]", False),
])
def test_auto_update_flag_parses_strictly(tmp_path, raw, expected):
    """Only a real JSON true may switch on silent code installation."""
    path = tmp_path / "config.json"
    path.write_text('{"download": {"ytdlp_auto_update": %s}}' % raw, encoding="utf-8")
    assert DeeptorrentConfig.from_file(str(path)).download.ytdlp_auto_update is expected


def test_agent_cannot_switch_automatic_yt_dlp_updates():
    from agent.tools import ToolError, ToolRegistry
    tools = ToolRegistry(mock.MagicMock(), DeeptorrentConfig())
    with pytest.raises(ToolError, match="non-settable"):
        tools.call("set_settings",
                   {"path": "download.ytdlp_auto_update", "value": True})


# ---------------------------------------------------------------------------
# The pin script
# ---------------------------------------------------------------------------

def _pin_module():
    spec = importlib.util.spec_from_file_location(
        "pin_ytdlp", REPO_ROOT / "packaging" / "pin_ytdlp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pin_script_flags_unknown_dependencies(tmp_path):
    pin = _pin_module()
    metadata = ("Metadata-Version: 2.1\nName: yt-dlp\nVersion: 2099.1.1\n"
                "Requires-Python: >=3.11\n"
                "Requires-Dist: yt-dlp-ejs==0.8.0; extra == 'default'\n"
                "Requires-Dist: brandnew-dep>=1; extra == 'default'\n"
                "Requires-Dist: deno>=2; extra == 'deno'\n")
    wheel = tmp_path / "w.whl"
    body = _wheel_bytes({**_package(NEW), "yt_dlp-2099.1.1.dist-info/METADATA": metadata.encode()},
                        NEW)
    wheel.write_bytes(body)
    assert pin.unknown_dependencies(wheel) == {"brandnew-dep"}
    # without the unknown one it is clean
    metadata_ok = metadata.replace("Requires-Dist: brandnew-dep>=1; extra == 'default'\n", "")
    wheel.write_bytes(_wheel_bytes(
        {**_package(NEW), "yt_dlp-2099.1.1.dist-info/METADATA": metadata_ok.encode()}, NEW))
    assert pin.unknown_dependencies(wheel) == set()


def test_pin_script_feed_round_trips_through_the_app_parser():
    pin = _pin_module()
    record = {k: FEED[k] for k in ("version", "file", "size", "sha256", "url", "released")}
    feed = pin.build_feed(record)
    assert ov.parse_feed(json.dumps(feed).encode("utf-8")) is not None
    bad = dict(record, url="https://example.com/yt_dlp-2026.11.23-py3-none-any.whl")
    with pytest.raises(SystemExit):
        pin.build_feed(bad)


def test_pin_script_download_verifies_the_digest(monkeypatch, tmp_path):
    pin = _pin_module()
    body = b"wheel-bytes" * 1000
    record = {"url": "https://files.pythonhosted.org/x", "size": len(body),
              "sha256": "0" * 64}

    class _Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield body

    monkeypatch.setattr(pin.requests, "get", lambda url, **kw: _Resp())
    dest = tmp_path / "pkg.whl"
    with pytest.raises(SystemExit, match="does not match"):
        pin.download(record, dest)
    assert not dest.exists()
    record["sha256"] = hashlib.sha256(body).hexdigest()
    pin.download(record, dest)
    assert dest.read_bytes() == body
