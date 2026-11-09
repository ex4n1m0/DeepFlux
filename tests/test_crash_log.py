"""Pins the safe faulthandler configuration across the repo.

faulthandler.enable() installs a Windows VECTORED exception handler that
fires on EVERY raised exception on ANY thread — including ones the raiser
catches itself. libmpv's native threads raise-and-catch internal
exceptions (code 0xe24c4a02) during normal IPTV playback; with
all_threads=True (the default), faulthandler then walked EVERY thread's
Python stack from the foreign faulting thread and itself AV'd,
process-killing the app 11 times on 2026-11-06 (two proven via WER dumps:
AV inside Py_DumpTracebackThreads under RtlpCallVectoredHandlers on a
libmpv thread). all_threads=False keeps the faulting-thread traceback
(real crashes stay diagnosable) and removes the killer walk; a true
no-tstate foreign thread is verified safe on that path.

7.5 fixed main.py; the 2026-11-07 stability audit found the SAME bug
still live in packaging/verify_boot.py (its boot smoke segfaulted on
Windows; one WER dump died inside Py_DumpTraceback itself). The pin is
therefore repo-wide: EVERY faulthandler.enable() call, in any first-party
.py file, must pass all_threads=False.

main.py imports the whole GUI/engine stack, too heavy for a unit test —
so the call shape is pinned from source (same approach as the QSS pins
in test_font_scale.py).
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _iter_py_files():
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).parts
        # tests/website excluded; packaging INCLUDED — verify_boot.py is
        # exactly the site the 2026-11-07 audit caught. Dot-dirs (`.venv`,
        # `.git`, …) excluded: the pin targets REPO code — site-packages
        # carry third-party enable() calls (pytest's own plugin,
        # win32ctypes' tests) that are none of our business.
        if parts[0] in ("tests", "website", "_site_shots_out"):
            continue
        if parts[0].startswith("."):
            continue
        yield path


def _faulthandler_enable_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "enable"
            and isinstance(func.value, ast.Name)
            and func.value.id == "faulthandler"
        ):
            calls.append(node)
    return calls


def _all_enable_calls() -> list[tuple[Path, ast.Call]]:
    found = []
    for path in _iter_py_files():
        found.extend((path, call) for call in _faulthandler_enable_calls(path))
    return found


def test_crash_log_enabled_with_all_threads_false() -> None:
    calls = [(p, c) for p, c in _all_enable_calls() if p.name == "main.py"]
    assert calls, "main.py must call faulthandler.enable() (windowed crash log)"
    for path, call in calls:
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        assert "all_threads" in kwargs, (
            f"{path} must pass all_threads explicitly to "
            "faulthandler.enable() — the default True walk crashed the "
            "process on libmpv's internal exceptions (2026-11-06, 11 "
            "native crashes)"
        )
        assert getattr(kwargs["all_threads"], "value", None) is False, (
            "all_threads=True makes faulthandler dump every thread from the "
            "faulting (possibly foreign, no-tstate) thread — that walk AVs "
            "and kills the process"
        )


def test_every_faulthandler_enable_site_is_single_thread() -> None:
    # The 2026-11-07 audit caught packaging/verify_boot.py still carrying
    # the default all-threads walk that 7.5 fixed in main.py — the boot
    # smoke was process-killing itself on benign native exceptions. Pin
    # EVERY site so the next one cannot ship.
    sites = _all_enable_calls()
    assert sites, "expected at least main.py + packaging/verify_boot.py"
    offenders = []
    for path, call in sites:
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        if getattr(kwargs.get("all_threads"), "value", None) is not False:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        "faulthandler.enable() without all_threads=False: "
        + ", ".join(offenders)
    )
