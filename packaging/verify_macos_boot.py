"""Offscreen boot smoke test for the macOS build (CI).

Builds the real MainWindow with a throwaway config and lets the event loop
run for a few seconds — catches import-time platform bugs, missing data
files, and constructor crashes before PyInstaller wastes a build.

Must run BEFORE QApplication is created (env vars):
    QT_QPA_PLATFORM=offscreen
    QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu --no-sandbox"
(headless runner: no window server, Chromium needs GPU+sandbox off)

Exit code 0 = boot OK. Any exception = non-zero.
"""
import faulthandler
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --no-sandbox --disable-dev-shm-usage")

# In-process hang watchdog: if the Qt event loop never reaches quit(),
# dump all thread stacks and die non-zero. (Shell `timeout` proved
# unreliable on the macOS runner — this keeps the CI step bounded.)
faulthandler.enable()


def _watchdog() -> None:
    time.sleep(120)
    faulthandler.dump_traceback()
    os._exit(3)


threading.Thread(target=_watchdog, daemon=True).start()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from config import DeeptorrentConfig  # noqa: E402
from gui.main_window import MainWindow, _PLAY_TAB_SUPPORTED  # noqa: E402


def main() -> int:
    # Throwaway config: never touch a real one, and keep the CI run off the
    # telemetry heartbeat (it would phone deepflux.space from the runner).
    with tempfile.TemporaryDirectory() as td:
        cfg_path = str(Path(td) / "config.json")
        cfg = DeeptorrentConfig()
        cfg.stats.ping_enabled = False
        cfg.to_file(cfg_path)

        app = QApplication([])
        window = MainWindow(cfg_path)

        names = [window.main_tabs.tabText(i)
                 for i in range(window.main_tabs.count())]
        print("tabs:", names)
        if _PLAY_TAB_SUPPORTED:
            assert "Play" in names, f"Play tab missing on a supported platform: {names}"
        else:
            assert "Play" not in names, f"Play tab must not exist on macOS: {names}"
            assert not hasattr(window, "iptv_tab"), "iptv_tab attribute leaked"
        assert "Browse" in names and "Agent" in names and "Download" in names, names

        # The iptv_* tools must be absent from the agent's registry on macOS.
        tool_names = set(window.tools._tools.keys())
        if not _PLAY_TAB_SUPPORTED:
            leaked = [t for t in tool_names if t.startswith("iptv_")]
            assert not leaked, f"iptv tools leaked into the macOS registry: {leaked}"

        QTimer.singleShot(4000, app.quit)
        app.exec()
        window.close()
        app.processEvents()
    print("BOOT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
