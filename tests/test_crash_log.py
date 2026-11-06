"""Pins the safe faulthandler configuration in main._enable_crash_log.

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

main.py imports the whole GUI/engine stack, too heavy for a unit test —
so the call shape is pinned from source (same approach as the QSS pins
in test_font_scale.py).
"""
import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parent.parent / "main.py"


def _faulthandler_enable_calls() -> list[ast.Call]:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
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


def test_crash_log_enabled_with_all_threads_false() -> None:
    calls = _faulthandler_enable_calls()
    assert calls, "main.py must call faulthandler.enable() (windowed crash log)"
    (call,) = calls
    kwargs = {kw.arg: kw.value for kw in call.keywords}
    assert "all_threads" in kwargs, (
        "faulthandler.enable() must pass all_threads explicitly — the "
        "default True walk crashed the process on libmpv's internal "
        "exceptions (2026-11-06, 11 native crashes)"
    )
    assert getattr(kwargs["all_threads"], "value", None) is False, (
        "all_threads=True makes faulthandler dump every thread from the "
        "faulting (possibly foreign, no-tstate) thread — that walk AVs "
        "and kills the process"
    )
