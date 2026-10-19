"""Standalone progress window for the installer's Jackett final step.

Runs `DeepFlux --setup-jackett` (see infra/jackett_setup.py): a small dark
log window that streams the bootstrap's progress lines so the user sees
something happening between the UAC prompt and the final summary. It is a
self-contained QApplication entry point — the main window is never built.

The same dialog serves the FlareSolverr and local-model steps (mode=
"flaresolverr" / "localmodel"). While the worker thread runs, closing the
window is intercepted: destroying a running QThread is a Qt fatal error,
and the local-model download can run for a long time — the user gets a
cancel offer instead (partial downloads resume on the next run).
"""
from __future__ import annotations
from gui.i18n import tr

import sys
import threading
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_STYLE = """
QWidget#setupRoot {
    background-color: #0a0a0f;
    color: #ffffff;
    font-family: "Inter", "Segoe UI", "Helvetica Neue", sans-serif;
}
QLabel#heading {
    color: #ffffff;
    font-size: 15pt;
    font-weight: 600;
}
QLabel#subline { color: #8a9ab0; font-size: 13px; }
QPlainTextEdit {
    background-color: #0d1117;
    color: #d7e3f4;
    border: 3px solid #1a2a4a;
    border-radius: 6px;
    font-size: 13px;
}
QPushButton {
    background-color: #111827;
    color: #ffffff;
    border: 3px solid #1a2a4a;
    border-radius: 3px;
    padding: 6px 22px;
    font-size: 14px;
}
QPushButton:hover:enabled { border-color: #a8edff; }
QPushButton:disabled { color: #5a6a80; }
"""


class _SetupWorker(QThread):
    line = Signal(str)
    # Local-model step: byte progress (done, total) for the multi-GB
    # download — text lines alone can't show that honestly.
    byte_progress = Signal(int, int)
    done = Signal(dict)

    def __init__(self, config_path: Optional[str], mode: str = "jackett", parent=None):
        super().__init__(parent)
        self._config_path = config_path
        self._mode = mode
        self.cancel = threading.Event()

    def run(self):
        try:
            if self._mode == "flaresolverr":
                from infra.flaresolverr import run_setup
            elif self._mode == "localmodel":
                from infra.local_model_setup import run_setup
            else:
                from infra.jackett_setup import run_setup
            kwargs = {}
            if self._mode == "localmodel":
                kwargs["on_bytes"] = lambda done, total: (
                    self.byte_progress.emit(done, total))
                kwargs["cancel"] = self.cancel
            result = run_setup(config_path=self._config_path,
                               on_progress=self.line.emit, **kwargs)
        except Exception as exc:  # never leave the window stuck on a crash
            result = {"ok": False, "error": f"exception:{exc}"}
        self.done.emit(result or {})


class JackettSetupDialog(QDialog):
    _MODES = {
        "jackett": {
            "title": "DeepFlux — torrent search setup",
            "heading": "Setting up torrent search",
            "subline": "Installing and linking the Jackett service, then adding its public "
                       "indexers. You may see one Windows permission prompt.",
        },
        "flaresolverr": {
            "title": "DeepFlux — Cloudflare unlock setup",
            "heading": "Setting up FlareSolverr",
            "subline": "Extracting the bundled FlareSolverr (about 700 MB) and linking it "
                       "into Jackett — this unlocks Cloudflare-protected torrent sites. "
                       "No permission prompt needed.",
        },
        "localmodel": {
            "title": "DeepFlux — local AI setup",
            "heading": "Setting up local AI",
            "subline": "Downloading an uncensored AI model matched to this PC's "
                       "hardware (2-9 GB depending on GPU/RAM). It runs entirely "
                       "on this machine — no account, no permission prompt, and "
                       "an interrupted download resumes on the next run.",
        },
    }

    def __init__(self, config_path: Optional[str] = None, mode: str = "jackett",
                 parent=None):
        super().__init__(parent)
        texts = self._MODES.get(mode, self._MODES["jackett"])
        self.setWindowTitle(tr(texts["title"]))
        self.setModal(False)
        self._result: dict = {}
        self._mode = mode

        root = QWidget(objectName="setupRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)

        heading = QLabel(tr(texts["heading"]), objectName="heading")
        subline = QLabel(tr(texts["subline"]), objectName="subline")
        subline.setWordWrap(True)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setMaximumBlockCount(2000)

        # Byte-accurate bar for the local-model download (hidden otherwise).
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(mode == "localmodel")

        self.close_btn = QPushButton(tr('Close'))
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)

        # Local-model step only: cancel the multi-GB download (partial
        # progress is kept and resumes on the next run).
        self.cancel_btn = QPushButton(tr('Cancel download'))
        self.cancel_btn.setVisible(mode == "localmodel")
        self.cancel_btn.clicked.connect(self._cancel_download)

        layout.addWidget(heading)
        layout.addWidget(subline)
        layout.addWidget(self.log, 1)
        layout.addWidget(self.progress)
        layout.addWidget(self.close_btn, 0, Qt.AlignRight)
        layout.addWidget(self.cancel_btn, 0, Qt.AlignRight)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)
        self.setStyleSheet(_STYLE)
        self.resize(640, 460)

        self._worker = _SetupWorker(config_path, mode=self._mode, parent=self)
        self._worker.line.connect(self._on_line)
        self._worker.byte_progress.connect(self._on_byte_progress)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _worker_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _cancel_download(self) -> None:
        if self._worker_running() and not self._worker.cancel.is_set():
            self._worker.cancel.set()
            self.log.appendPlainText(
                "Cancelling after the current chunk — the partial download "
                "resumes on the next run.")

    def closeEvent(self, event) -> None:
        # Destroying a running QThread is a Qt FATAL (the dialog owns the
        # worker). While the step runs, closing means cancel-then-wait (the
        # download mode) or wait (the service modes).
        if not self._worker_running():
            event.accept()
            return
        if self._mode == "localmodel" and not self._worker.cancel.is_set():
            reply = QMessageBox.question(
                self, tr('Setup is running'),
                tr('The download is still in progress. Cancel it and close? '
                   '(Partial progress is kept and resumes on the next run.)'),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self._worker.cancel.set()
                self.close_btn.setEnabled(False)
                self.log.appendPlainText("Cancelling…")
            event.ignore()
            return
        QMessageBox.information(
            self, tr('Setup is running'),
            tr('Please wait for this step to finish (you can watch the '
               'progress above).'))
        event.ignore()

    def reject(self) -> None:
        # Esc goes through reject() — same running-worker guard as the X.
        if self._worker_running():
            QMessageBox.information(
                self, tr('Setup is running'),
                tr('Please wait for this step to finish (you can watch the '
                   'progress above).'))
            return
        super().reject()

    def _on_byte_progress(self, done: int, total: int):
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(done * 100 / total))

    def _on_line(self, text: str):
        # Also mirror to stdout: visible when launched from a console,
        # harmless (and invisible) in the windowed build.
        try:
            print(text, flush=True)
        except Exception:
            pass
        self.log.appendPlainText(text)

    def _on_done(self, result: dict):
        self._result = result
        self.cancel_btn.setVisible(False)
        if result.get("ok"):
            self.log.appendPlainText("")
            if result.get("summary"):
                self.log.appendPlainText(f"✓ Done — {result['summary']}")
            elif self._mode == "flaresolverr":
                self.log.appendPlainText(
                    "✓ Done — Cloudflare-protected sites are being unblocked. "
                    "The first search through one may take up to a minute "
                    "while the challenge is solved.")
            else:
                self.log.appendPlainText(
                    f"✓ Done — {result.get('sources_enabled', 0)} torrent sources enabled.")
        else:
            self.log.appendPlainText("")
            self.log.appendPlainText(
                "Setup did not finish — DeepFlux still works; "
                "you can retry from Help → User Guide."
            )
        self.close_btn.setEnabled(True)
        self.close_btn.setFocus()

    def center_on_screen(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(geo.center())
        self.move(frame.topLeft())


def run_setup_jackett_dialog(config_path: Optional[str] = None,
                             mode: str = "jackett") -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Deeptorrent")
    try:
        from gui.fonts import load_app_fonts
        load_app_fonts(app)
    except Exception:
        pass
    dialog = JackettSetupDialog(config_path=config_path, mode=mode)
    dialog.show()
    dialog.center_on_screen()
    return app.exec()
