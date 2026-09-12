"""Startup greeting (2026-09-12): ~1.5s after launch the GUI auto-sends
"Hello" through the normal _on_send path so the agent greets the user.
Guards: it must never clobber input the user already typed, and must not
fire when the agent is already busy."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit

from gui.main_window import MainWindow

app = QApplication.instance() or QApplication([])


class _Stub:
    """Minimal MainWindow stand-in: just the attributes the greeting touches."""

    def __init__(self, text: str = "", busy: bool = False) -> None:
        self.chat_input = QLineEdit()
        self.chat_input.setText(text)
        if busy:
            self._agent_thread = type("T", (), {"is_alive": lambda self: True})()
        else:
            self._agent_thread = None
        self.sent = []

    def _on_send(self) -> None:
        # The real _on_send reads the input box text — record what it'd get.
        self.sent.append(self.chat_input.text())


def test_greeting_sends_hello_when_idle():
    stub = _Stub()
    MainWindow._send_startup_greeting(stub)
    assert stub.sent == ["Hello"]


def test_greeting_skipped_when_agent_busy():
    stub = _Stub(busy=True)
    MainWindow._send_startup_greeting(stub)
    assert stub.sent == []
    assert not stub.chat_input.text()


def test_greeting_never_clobbers_user_input():
    stub = _Stub(text="search ubuntu torrents")
    MainWindow._send_startup_greeting(stub)
    assert stub.sent == []
    assert stub.chat_input.text() == "search ubuntu torrents"
