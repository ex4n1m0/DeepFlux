"""DeepFlux GUI package.

Deliberately LAZY since 7.8 (FAST_VIDEO_OPEN_STUDY.md): importing
``gui.mini_player`` — the app's default fast-boot surface — must NOT drag
``gui.main_window``'s heavy chain (QtWebEngine, libtorrent engine, agent,
requests) through this package __init__. ``from gui import run_gui`` /
``MainWindow`` still work via the PEP 562 fallback below; ``from gui
import <submodule>`` is unaffected (the import system resolves real
submodules before consulting __getattr__).
"""

__all__ = ["MainWindow", "run_gui"]


def __getattr__(name):
    if name in ("MainWindow", "run_gui"):
        from . import main_window as _mw
        return getattr(_mw, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
