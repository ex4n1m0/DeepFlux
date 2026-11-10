"""Windows taskbar progress overlay (v5) — aggregate download progress on the
taskbar button, visible without focusing the window.

PySide6 ships no QWinTaskbarButton (QtWinExtras died with Qt5), so this is a
minimal ctypes COM wrapper around ITaskbarList3 — the same interface Explorer
exposes. Every failure path is a silent no-op: taskbar sugar must never be
able to crash the app, and on non-Windows builds the class is inert by design
(the Play tab guards keep POSIX boots alive).
"""
from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

_WIN32 = sys.platform == "win32"

if _WIN32:
    from ctypes import wintypes  # noqa: F401  (prototypes below)

# ITaskbarList3 vtable slots (after IUnknown 0-2): HrInit=3, AddTab=4,
# DeleteTab=5, ActivateTab=6, SetActiveAlt=7, MarkFullscreenWindow=8,
# SetProgressValue=9, SetProgressState=10, RegisterTab=11, UnregisterTab=12.
# Slot numbers are ABI-stable (the interface is frozen) — 11/12 are
# Register/UnregisterTab and were wrongly used for the progress calls
# (2026-11-09 review, M9).
_HR_INIT = 3
_SET_PROGRESS_VALUE = 9
_SET_PROGRESS_STATE = 10
_RELEASE = 2


class _GUID(ctypes.Structure):
    # Plain ctypes primitives, NOT wintypes: this module imports on every
    # platform, and wintypes only exists under the _WIN32 guard above — the
    # wintypes version crashed the Linux/macOS builds AT IMPORT ("NameError:
    # wintypes is not defined", CI run 42809567604). Layout is identical.
    _fields_ = [("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(spec: str) -> _GUID:
    """Build a real ctypes GUID from a registry-format string. The original
    code passed uuid.UUID straight to ctypes.byref() — byref demands a ctypes
    instance and raised TypeError, so CoCreateInstance was never reached and
    the whole feature silently died (2026-11-09 review, M9)."""
    from uuid import UUID
    g = _GUID()
    # UUID.bytes_le is exactly the in-memory GUID layout.
    ctypes.memmove(ctypes.byref(g), UUID(spec).bytes_le, 16)
    return g

_TBPF_NOPROGRESS = 0
_TBPF_INDETERMINATE = 1
_TBPF_NORMAL = 2


def _vtable_entry(interface_ptr: int, index: int) -> int:
    """Read function pointer `index` from the COM object's vtable."""
    vtbl_ptr = ctypes.cast(
        ctypes.c_void_p(interface_ptr), ctypes.POINTER(ctypes.c_void_p)
    ).contents.value
    vtbl = ctypes.cast(
        ctypes.c_void_p(vtbl_ptr), ctypes.POINTER(ctypes.c_void_p * 64)
    ).contents
    return vtbl[index] or 0


class TaskbarProgress:
    """Aggregate progress on the Windows taskbar button; inert everywhere else."""

    def __init__(self, hwnd: int) -> None:
        self._ptr = 0
        self._hwnd = int(hwnd or 0)
        if not _WIN32 or not hwnd:
            return
        try:
            clsid = _guid("{56FDF344-FD6D-11d0-958A-006097C9A090}")  # TaskbarList
            iid = _guid("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}")   # ITaskbarList3
            ptr = ctypes.c_void_p()
            co_create = ctypes.windll.ole32.CoCreateInstance
            co_create.argtypes = (
                ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
            )
            co_create.restype = ctypes.HRESULT
            # CLSCTX_INPROC_SERVER = 1. QApplication already initialized COM STA.
            hr = co_create(ctypes.byref(clsid), None, 1,
                           ctypes.byref(iid), ctypes.byref(ptr))
            if hr != 0 or not ptr.value:
                return
            self._ptr = ptr.value
            # HrInit(self) must run before any Set* call.
            fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(
                _vtable_entry(self._ptr, _HR_INIT)
            )
            fn(self._ptr)
        except Exception:
            self._ptr = 0
            logger.debug("taskbar progress unavailable", exc_info=True)

    # -- public API (safe on every platform / state) ----------------------
    def set_progress(self, done: float, total: float) -> None:
        if not self._ptr or total <= 0:
            return
        try:
            fn = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p, wintypes.HWND,
                ctypes.c_ulonglong, ctypes.c_ulonglong,
            )(_vtable_entry(self._ptr, _SET_PROGRESS_VALUE))
            fn(self._ptr, int(self._hwnd or 0), max(0, int(done)), max(1, int(total)))
        except Exception:
            logger.debug("SetProgressValue failed", exc_info=True)

    def set_indeterminate(self) -> None:
        self._set_state(_TBPF_INDETERMINATE)

    def clear(self) -> None:
        self._set_state(_TBPF_NOPROGRESS)

    def release(self) -> None:
        """Drop our COM reference (best-effort; the app holds one instance
        for its lifetime, so this is hygiene, not a leak fix)."""
        if not self._ptr:
            return
        try:
            fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(
                _vtable_entry(self._ptr, _RELEASE)
            )
            fn(self._ptr)
        except Exception:
            logger.debug("taskbar Release failed", exc_info=True)
        finally:
            self._ptr = 0

    # -- internals ---------------------------------------------------------
    def _set_state(self, flag: int) -> None:
        if not self._ptr:
            return
        try:
            fn = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
            )(_vtable_entry(self._ptr, _SET_PROGRESS_STATE))
            fn(self._ptr, int(self._hwnd or 0), flag)
        except Exception:
            logger.debug("SetProgressState failed", exc_info=True)
