"""Update UI: the "new version" prompt and the download progress dialog.

The check/apply logic is Qt-free in infra/updater.py; this module only
presents it. Background results reach the GUI thread through _UpdateSignals
(queued connections), mirroring the other bridge-signal patterns in
main_window.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from infra.updater import UpdateInfo
from gui.i18n import tr

# Dialog result codes for the prompt (exec() return values are ints).
RESULT_UPDATE_NOW = 1
RESULT_LATER = 0
RESULT_SKIP = 2


class _UpdateSignals(QObject):
    """Queued bridge between updater worker threads and the GUI thread."""

    found = Signal(object)          # UpdateInfo — a newer version exists
    status = Signal(str)            # manual-check outcome (up to date / error)
    progress = Signal(int, int)     # bytes done, bytes total
    download_done = Signal(object)  # verified setup path
    download_failed = Signal(str)


class UpdateDialog(QDialog):
    """'Version X is available' — Update now / Remind me later / Skip."""

    def __init__(self, parent, info: UpdateInfo, current_version: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"DeepFlux {info.version} is available")
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        heading = QLabel(f"DeepFlux {info.version} is available "
                         f"(you have {current_version}).")
        heading.setWordWrap(True)
        # Feed strings render as plain text — QLabel's rich-text
        # auto-detection must not turn a crafted feed into markup.
        heading.setTextFormat(Qt.PlainText)
        layout.addWidget(heading)

        if info.notes:
            notes = QLabel(info.notes)
            notes.setWordWrap(True)
            notes.setTextFormat(Qt.PlainText)
            notes.setStyleSheet("color: rgba(238, 244, 255, 0.7);")
            layout.addWidget(notes)

        size_lbl = QLabel(
            f"Download size: ~{info.size_mb} MB. Your sources, API keys and "
            f"settings are kept — the update reinstalls in place and "
            f"restarts DeepFlux.")
        size_lbl.setWordWrap(True)
        size_lbl.setStyleSheet("color: rgba(238, 244, 255, 0.55);")
        layout.addWidget(size_lbl)

        buttons = QHBoxLayout()
        update_btn = QPushButton(tr('Update now'))
        update_btn.setObjectName("btn_accent")
        update_btn.setDefault(True)
        update_btn.clicked.connect(lambda: self.done(RESULT_UPDATE_NOW))
        later_btn = QPushButton(tr('Remind me later'))
        later_btn.clicked.connect(lambda: self.done(RESULT_LATER))
        skip_btn = QPushButton(tr('Skip this version'))
        skip_btn.clicked.connect(lambda: self.done(RESULT_SKIP))
        buttons.addWidget(update_btn)
        buttons.addWidget(later_btn)
        buttons.addWidget(skip_btn)
        layout.addLayout(buttons)


class UpdateProgressDialog(QDialog):
    """Streams the setup exe (Range-resumable). Closing keeps the partial
    file, so a retry continues instead of restarting the ~370 MB download."""

    def __init__(self, parent, info: UpdateInfo, signals: _UpdateSignals) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Downloading DeepFlux {info.version}")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._info = info
        self._signals = signals
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

        layout = QVBoxLayout(self)
        self._label = QLabel(f"Downloading the update (~{info.size_mb} MB)…")
        self._label.setWordWrap(True)
        layout.addWidget(self._label)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        layout.addWidget(self._bar)
        cancel_btn = QPushButton(tr('Cancel'))
        cancel_btn.clicked.connect(self._on_cancel)
        layout.addWidget(cancel_btn, alignment=Qt.AlignRight)

        self._signals.progress.connect(self._on_progress)
        self._signals.download_done.connect(self._on_done)
        self._signals.download_failed.connect(self._on_failed)

        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="update-download")
        self._thread.start()

    def _worker(self) -> None:
        from infra import updater
        try:
            path = updater.download_update(
                self._info,
                progress=lambda done, total: self._signals.progress.emit(done, total),
                cancel=self._cancel,
            )
        except Exception as exc:
            self._signals.download_failed.emit(str(exc))
            return
        self._signals.download_done.emit(path)

    def _on_progress(self, done: int, total: int) -> None:
        self._bar.setValue(int(done * 100 / max(1, total)))
        self._label.setText(f"Downloading the update — {done // (1024 * 1024)} "
                             f"of {total // (1024 * 1024)} MB")

    def _on_done(self, path) -> None:
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._error = message
        self.reject()

    def _on_cancel(self) -> None:
        self._cancel.set()
        self._label.setText(tr('Cancelling…'))

    def error_message(self) -> str:
        return getattr(self, "_error", "")
