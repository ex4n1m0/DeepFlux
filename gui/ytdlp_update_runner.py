"""Background check / install of the bundled yt-dlp, with Qt signals.

The work itself is Qt-free (infra/ytdlp_overlay.py); this class only moves
it off the GUI thread and marshals the outcome back. One instance per
surface that offers the update — the Agent-tab "Update now" link and the
Download Manager settings page. A busy runner ignores a second request, so
a double click can never start two installs."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from gui.i18n import tr
from infra import ytdlp_overlay


def ask_to_install(parent: QWidget, info) -> bool:
    """The one confirmation every install needs (ask mode's whole point).
    `info` is a YtdlpUpdateInfo from the owner-pinned feed."""
    answer = QMessageBox.question(
        parent, tr('Update the YouTube downloader'),
        tr("DeepFlux can update yt-dlp to {version} by itself — a {size} download "
           "that is checked against a version DeepFlux has approved.\n\n"
           "It replaces the bundled downloader the next time DeepFlux starts. "
           "You can go back to the bundled one at any time in Settings.\n\n"
           "Update now?").format(version=info.version, size=f"{info.size / 1e6:.1f} MB"),
        QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
    return answer == QMessageBox.Yes


def ask_to_revert(parent: QWidget) -> bool:
    answer = QMessageBox.question(
        parent, tr('Use the bundled YouTube downloader'),
        tr("Go back to the yt-dlp that came with DeepFlux? This takes effect "
           "the next time DeepFlux starts."),
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    return answer == QMessageBox.Yes


def show_installed(parent: QWidget, version: str) -> bool:
    """Report a finished install; True when the user chose to restart now."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle(tr('YouTube downloader updated'))
    box.setText(tr("yt-dlp {version} is installed. Restart DeepFlux to start "
                   "using it.").format(version=version))
    restart = box.addButton(tr("Restart now"), QMessageBox.AcceptRole)
    box.addButton(tr("Later"), QMessageBox.RejectRole)
    box.exec()
    return box.clickedButton() is restart


def show_failed(parent: QWidget, message: str) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    # Error text can carry wheel member names or HTTP exception text.
    box.setTextFormat(Qt.PlainText)
    box.setWindowTitle(tr('YouTube downloader update failed'))
    box.setText(tr("The YouTube downloader could not be updated:\n{error}\n\n"
                   "The bundled downloader stays in use.").format(error=message))
    box.exec()


class YtdlpUpdateRunner(QObject):
    found = Signal(object)      # YtdlpUpdateInfo, or None when nothing is newer
    failed = Signal(str)        # human-readable reason
    installed = Signal(str)     # the version that was installed

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._busy = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def _spawn(self, name: str, work) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True

        def _run() -> None:
            try:
                work()
            finally:
                with self._lock:
                    self._busy = False

        threading.Thread(target=_run, daemon=True, name=f"ytdlp-{name}").start()
        return True

    def check(self) -> bool:
        """Look up the owner-pinned release. Emits found(info) when it is
        newer than the one in use, else found(None); failed(reason) when the
        feed cannot be read or this build cannot update yt-dlp itself."""

        def work() -> None:
            if not ytdlp_overlay.can_override():
                self.failed.emit(
                    "The YouTube downloader can only update itself in the "
                    "installed Windows and Linux builds.")
                return
            try:
                info = ytdlp_overlay.fetch_feed()
            except Exception as exc:
                self.failed.emit(str(exc))
                return
            in_use = ytdlp_overlay.active_version() or ytdlp_overlay.bundled_version()
            self.found.emit(info if ytdlp_overlay.is_newer(info.version, in_use) else None)

        return self._spawn("check", work)

    def install(self, info) -> bool:
        """Install a release the user has agreed to. Emits installed(version)
        or failed(reason). ``force`` because this is the user's own click: it
        may retry a release the automatic path would skip."""

        def work() -> None:
            try:
                ytdlp_overlay.install(info, force=True)
            except Exception as exc:
                self.failed.emit(str(exc))
                return
            self.installed.emit(info.version)

        return self._spawn("install", work)
