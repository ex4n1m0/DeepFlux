"""Standalone progress window for the installer's Jackett final step.

Runs `DeepFlux --setup-jackett` (see infra/jackett_setup.py): a small dark
log window that streams the bootstrap's progress lines so the user sees
something happening between the UAC prompt and the final summary. It is a
self-contained QApplication entry point — the main window is never built.
"""
from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QPlainTextEdit,
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
    done = Signal(dict)

    def __init__(self, config_path: Optional[str], parent=None):
        super().__init__(parent)
        self._config_path = config_path

    def run(self):
        from infra.jackett_setup import run_setup
        try:
            result = run_setup(config_path=self._config_path, on_progress=self.line.emit)
        except Exception as exc:  # never leave the window stuck on a crash
            result = {"ok": False, "error": f"exception:{exc}"}
        self.done.emit(result or {})


class JackettSetupDialog(QDialog):
    def __init__(self, config_path: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DeepFlux — torrent search setup")
        self.setModal(False)
        self._result: dict = {}

        root = QWidget(objectName="setupRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)

        heading = QLabel("Setting up torrent search", objectName="heading")
        subline = QLabel(
            "Installing and linking the Jackett service, then adding its public "
            "indexers. You may see one Windows permission prompt.",
            objectName="subline",
        )
        subline.setWordWrap(True)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setMaximumBlockCount(2000)

        self.close_btn = QPushButton("Close")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)

        layout.addWidget(heading)
        layout.addWidget(subline)
        layout.addWidget(self.log, 1)
        layout.addWidget(self.close_btn, 0, Qt.AlignRight)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)
        self.setStyleSheet(_STYLE)
        self.resize(640, 460)

        self._worker = _SetupWorker(config_path, self)
        self._worker.line.connect(self._on_line)
        self._worker.done.connect(self._on_done)
        self._worker.start()

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
        if result.get("ok"):
            self.log.appendPlainText("")
            self.log.appendPlainText(
                f"✓ Done — {result.get('sources_enabled', 0)} torrent sources enabled."
            )
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


def run_setup_jackett_dialog(config_path: Optional[str] = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Deeptorrent")
    try:
        from gui.fonts import load_app_fonts
        load_app_fonts(app)
    except Exception:
        pass
    dialog = JackettSetupDialog(config_path=config_path)
    dialog.show()
    dialog.center_on_screen()
    return app.exec()
