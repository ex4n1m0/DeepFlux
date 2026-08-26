"""PySide6 main window for Deeptorrent."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from PySide6.QtCore import QTimer, Qt, QRectF, Signal, QObject, QEvent, QProcess
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QIcon, QKeySequence, QLinearGradient, QPainter, QPen, QPixmap, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineSettings
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtCore import QUrl, QByteArray

from agent.loop import AgentLoop
from agent.llm import create_llm_client
from agent.tools import ToolRegistry
from agent.rss import RSSMonitor
from config import DeeptorrentConfig
from engine import TorrentEngine
from infra.config_backup import (
    SettingsBackupError,
    decrypt_settings,
    encrypt_settings,
    validate_settings,
)
from engine.state import TorrentStateManager
from gui.rss_dialog import RSSDialog
from gui.rss_viewer import RSSViewer
from gui.settings_dialog import APIKeysDialog, IndexerSettingsDialog, DownloadsSettingsDialog, BrowserSettingsDialog
from gui.sources_dialog import SourcesDialog
from gui.help_dialog import HelpDialog, AboutDialog
from gui.downloads_tab import DownloadsTab
from gui.commander_tab import CommanderTab
from gui.browser_bridge import BrowserBridge
from gui.iptv_tab import AgentIPTVBridge, IPTVTab
from gui.iptv_settings_dialog import IPTV_SETTINGS_PAGES
from gui.irc_tab import IRCTab
from gui.voice_input import MIN_SECONDS, SAMPLE_RATE as VOICE_SAMPLE_RATE, VoiceRecorder, VoiceTranscriber, pcm_to_whisper_audio
from ircmgr.client import IRCClientCore
from dlmgr.engine import DownloadEngine
from dlmgr.control_api import ControlAPI

logger = logging.getLogger(__name__)


class _AgentSignals(QObject):
    """Qt signals used to safely cross from the agent worker thread to the GUI thread."""
    event = Signal(dict)
    finished = Signal(dict)


class _OpenSignals(QObject):
    """Marshals /api/open requests (HTTP thread) onto the GUI thread."""
    open_target = Signal(str)


class _PlaySignals(QObject):
    """Marshals /api/play requests (HTTP thread) onto the GUI thread."""
    play_stream = Signal(dict)


class _RefreshSignals(QObject):
    """Cross-thread marshalling for the torrent table refresh worker."""
    torrents = Signal(list)
    files = Signal(object)
    # (info_hash, status) for torrents waiting to stream (buffer threshold).
    stream_status = Signal(str, object)


class _ChatHistoryEdit(QTextBrowser):
    """Read-only chat view with the DeepFlux logo painted full-opacity,
    centered behind the text. The logo is scaled to at most 60% of the
    viewport (same as the Browse tab start page), aspect ratio preserved,
    never cropped. The widget background matches the logo's own background
    so the letterbox margins blend in."""

    LOGO_OPACITY = 1.0
    # Share of the viewport the logo may occupy — matches the Browse tab
    # start page (max-width:60%; max-height:60vh).
    LOGO_MAX_RATIO = 0.6
    # Solid black background — blends with the DeepFlux logo's black edges.
    BG_GRADIENT_START = "#000000"
    BG_GRADIENT_END = "#000000"

    def __init__(self, logo_path: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._logo = QPixmap(logo_path) if logo_path else QPixmap()
        self._scaled_logo = QPixmap()
        self._logo_visible = True
        self._rescale_logo()

    def set_logo_visible(self, visible: bool) -> None:
        """Show/hide the logo background (hidden once the conversation starts
        so the chat becomes plain black and text is always easy to read)."""
        if self._logo_visible != visible:
            self._logo_visible = visible
            self.viewport().update()

    def _rescale_logo(self) -> None:
        if self._logo.isNull():
            return
        vp = self.viewport().size()
        # Fit inside 60% of the viewport (same cap as the Browse tab start
        # page), aspect ratio preserved; centered drawing letterboxes the rest.
        target = self._logo.size()
        target.scale(max(1, int(vp.width() * self.LOGO_MAX_RATIO)),
                     max(1, int(vp.height() * self.LOGO_MAX_RATIO)), Qt.KeepAspectRatio)
        # SmoothTransformation is bilinear: fine at ≤2x, but it aliases on
        # large downscales (1254px source -> ~500px viewport) — the neon
        # edges go jagged. Cascade alias-free 2:1 steps until within 2x of
        # the target, then do the final smooth scale (mipmap-style).
        img = self._logo.toImage()
        while img.width() > 2 * target.width() and img.width() > 1:
            img = img.scaled(max(1, img.width() // 2), max(1, img.height() // 2),
                             Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self._scaled_logo = QPixmap.fromImage(
            img.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event) -> None:
        self._rescale_logo()
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        rect = self.viewport().rect()
        if self._logo_visible:
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0, QColor(self.BG_GRADIENT_START))
            gradient.setColorAt(1, QColor(self.BG_GRADIENT_END))
            painter.fillRect(rect, gradient)
        else:
            painter.fillRect(rect, QColor("#000000"))
        if self._logo_visible and not self._scaled_logo.isNull():
            vp = self.viewport().rect()
            x = vp.x() + (vp.width() - self._scaled_logo.width()) // 2
            y = vp.y() + (vp.height() - self._scaled_logo.height()) // 2
            painter.setOpacity(self.LOGO_OPACITY)
            painter.drawPixmap(x, y, self._scaled_logo)
        painter.end()
        super().paintEvent(event)


# ---------------------------------------------------------------------------
# Chat panel styling
# ---------------------------------------------------------------------------

CHAT_CSS = """
/* Note: text background bands are applied programmatically per block in
   MainWindow._insert_html (opaque black) because CSS backgrounds on
   container elements are unreliable for block-level children. */
body { font-family: 'Inter', 'Segoe UI', sans-serif; font-size: 12px; color: #c8d3e0; background-color: transparent; }
p { padding: 2px 4px; }
.msg-user {
    border-left: 4px solid #2a7abf;
    padding: 4px 8px; margin: 3px 0; border-radius: 4px; color: #c8d3e0;
}
.msg-agent {
    border-left: 4px solid #00ff9d;
    padding: 4px 8px; margin: 3px 0; border-radius: 4px; color: #c8d3e0;
}
.msg-event {
    border-left: 3px solid #1a2a4a;
    padding: 4px 10px; margin: 3px 0; border-radius: 4px;
    color: #4a6a8a; font-size: 12px;
}
.msg-debug {
    border-left: 3px solid #2a2a3a;
    padding: 4px 10px; margin: 3px 0; border-radius: 4px;
    color: #5a7a6a; font-size: 11px;
    font-family: 'JetBrains Mono', 'Cascadia Mono', 'Consolas', monospace;
    white-space: pre-wrap;
}
.msg-error {
    border-left: 4px solid #ff3366;
    padding: 4px 8px; margin: 3px 0; border-radius: 4px; color: #ff8080;
}
.msg-pending {
    border-left: 4px solid #ffcc00;
    padding: 4px 8px; margin: 3px 0; border-radius: 4px; color: #c8d3e0;
}
table { border-collapse: collapse; width: 100%; margin: 8px 0; border-radius: 6px; overflow: hidden; }
th { color: #2a7abf; padding: 2px 8px; text-align: left; font-size: 12px; font-weight: 600; border-bottom: 1px solid #1a2a4a; }
td { border: none; border-bottom: 1px solid #1a2a4a; padding: 2px 8px; font-size: 12px; color: #c8d3e0; }
h2 { color: #2a7abf; font-size: 14px; margin: 12px 0 6px 0; font-weight: 700; }
h3 { color: #2a7abf; font-size: 13px; margin: 10px 0 4px 0; font-weight: 600; }
h4 { color: #8a9ab0; font-size: 12px; margin: 8px 0 4px 0; }
code { background-color: #000000; padding: 2px 6px; border-radius: 4px; font-family: 'JetBrains Mono', 'Cascadia Code', 'Fira Code', Consolas, monospace; font-size: 12px; color: #2a7abf; }
hr { border: none; border-top: 1px solid #1a2a4a; margin: 12px 0; }
a { color: #2a7abf; text-decoration: none; }
a:hover { color: #80f0ff; }
ul { margin: 4px 0 4px 20px; }
ol { margin: 4px 0 4px 20px; }
li { margin: 2px 0; color: #c8d3e0; }
b { color: #e8f0f8; }
i { color: #8a9ab0; }
blockquote { border-left: 3px solid #1a2a4a; margin: 6px 0; padding: 4px 12px; color: #4a6a8a; }
"""


def _markdown_to_html(md: str) -> str:
    """Convert a subset of Markdown to HTML for QTextEdit.

    Handles: headings, bold, italic, code, links, horizontal rules,
    unordered/ordered lists, and pipe tables.
    """
    if not md:
        return ""

    lines = md.split("\n")
    html_lines: List[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Horizontal rule
        if re.match(r"^-{3,}$", stripped) or re.match(r"^\*{3,}$", stripped):
            html_lines.append("<hr>")
            i += 1
            continue

        # Headings
        m = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if m:
            level = len(m.group(1))
            text = _inline_md(m.group(2))
            html_lines.append(f"<h{level + 1}>{text}</h{level + 1}>")
            i += 1
            continue

        # Pipe table — collect consecutive table rows.
        if "|" in stripped and stripped.startswith("|") or (stripped.count("|") >= 2 and i + 1 < len(lines) and re.match(r"^\|[\s\-:|]+\|$", lines[i + 1].strip())):
            table_lines = []
            while i < len(lines) and "|" in lines[i].strip() and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            html_lines.append(_table_to_html(table_lines))
            continue

        # Unordered list
        if re.match(r"^[-*]\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append(_inline_md(re.sub(r"^[-*]\s+", "", lines[i].strip())))
                i += 1
            html_lines.append("<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>")
            continue

        # Ordered list
        if re.match(r"^\d+\.\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i].strip()):
                items.append(_inline_md(re.sub(r"^\d+\.\s+", "", lines[i].strip())))
                i += 1
            html_lines.append("<ol>" + "".join(f"<li>{it}</li>" for it in items) + "</ol>")
            continue

        # Blockquote
        if stripped.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(_inline_md(lines[i].strip().lstrip("> ")))
                i += 1
            html_lines.append(f"<blockquote>{' '.join(quote_lines)}</blockquote>")
            continue

        # Empty line
        if not stripped:
            html_lines.append("<br>")
            i += 1
            continue

        # Regular paragraph
        html_lines.append(_inline_md(stripped))
        i += 1

    html = "\n".join(html_lines)
    # Collapse runs of blank lines — each becomes a full-height empty block
    # in QTextDocument, so consecutive ones stack into large empty bands.
    html = re.sub(r"(?:<br>\s*){2,}", "<br>", html)
    # Drop <br> adjacent to block elements (lists, tables, headings, quotes,
    # rules) — those already carry their own margins, so the extra <br> just
    # adds a big gap.
    html = re.sub(r"<br>\s*(?=<(?:ul|ol|table|h[234]|blockquote|hr)[ >])", "", html)
    html = re.sub(r"</(ul|ol|table|h[234]|blockquote)>\s*<br>", r"</\1>", html)
    # Trim leading/trailing line breaks.
    html = re.sub(r"^(?:\s*<br>)+|(?:<br>\s*)+$", "", html)
    return html


def _inline_md(text: str) -> str:
    """Convert inline markdown (bold, italic, code, links) to HTML."""
    # Escape HTML special chars first.
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Inline code: `code`
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    # Italic: *text* or _text_ (but not inside bold markers)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    # Links: [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


# Runs of this many non-space characters get zero-width spaces inserted so the
# QTextDocument line breaker can wrap them. Magnet links, info hashes and long
# URLs otherwise stay one unbreakable token and overflow the chat width.
_LONG_TOKEN_LIMIT = 28
_LONG_TOKEN_CHUNK = 20


def _break_long_tokens(html: str) -> str:
    """Insert zero-width spaces into long unbroken tokens in HTML text nodes.

    Tags are left untouched; entities (&amp;, &#123;) are never split.
    """
    def _soften(text: str) -> str:
        out = []
        # Split entities aside first so a break never lands inside one.
        for part in re.split(r"(&\w+;|&#\d+;)", text):
            if part.startswith("&") and part.endswith(";"):
                out.append(part)
                continue
            out.append(re.sub(
                rf"\S{{{_LONG_TOKEN_LIMIT},}}",
                lambda m: "\u200b".join(
                    m.group(0)[i:i + _LONG_TOKEN_CHUNK]
                    for i in range(0, len(m.group(0)), _LONG_TOKEN_CHUNK)
                ),
                part,
            ))
        return "".join(out)

    parts = re.split(r"(<[^>]+>)", html)
    for idx in range(0, len(parts), 2):  # even indices = text nodes
        if parts[idx]:
            parts[idx] = _soften(parts[idx])
    return "".join(parts)


def _table_to_html(table_lines: List[str]) -> str:
    """Convert pipe-delimited table lines to an HTML table."""
    if len(table_lines) < 2:
        return "<br>".join(table_lines)

    def parse_row(row: str) -> List[str]:
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [c.strip() for c in row.split("|")]

    header = parse_row(table_lines[0])
    # Skip separator row (table_lines[1])
    body = [parse_row(r) for r in table_lines[2:]]

    html = '<table><tr>' + "".join(f"<th>{_inline_md(h)}</th>" for h in header) + '</tr>'
    for row in body:
        html += '<tr>' + "".join(f"<td>{_inline_md(c)}</td>" for c in row) + '</tr>'
    html += '</table>'
    return html


def format_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def format_rate(num: int) -> str:
    if num < 1024:
        return f"{num} B/s"
    if num < 1024 ** 2:
        return f"{num / 1024:.1f} KB/s"
    if num < 1024 ** 3:
        return f"{num / 1024 ** 2:.1f} MB/s"
    return f"{num / 1024 ** 3:.2f} GB/s"


def _human_state(state: str) -> str:
    """Friendlier labels for raw libtorrent state names."""
    return {
        "downloading_metadata": "metadata",
        "queued_for_checking": "queued",
        "checking_files": "checking",
        "checking_resume_data": "resume check",
    }.get(state, state or "—")


def _fmt_eta(eta: Optional[int]) -> str:
    if not eta:
        return "—"
    h, r = divmod(eta, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class _BrowserPage(QWebEnginePage):
    """Custom QWebEnginePage that opens new windows/tabs in the internal
    browser instead of the external system browser.

    When a page calls window.open() or a link with target="_blank" is
    clicked, Chromium calls createWindow(). We emit a signal so the
    MainWindow can create a new internal tab with the requested URL."""

    # Emitted when a new window/tab should be created. Carries the URL.
    newTabRequested = Signal(QUrl)

    def createWindow(self, _type):  # noqa: N802
        # Return a dummy page — we'll intercept the URL via urlChanged
        # and redirect it to a new tab.
        page = _BrowserPage(self.profile(), self)
        page.urlChanged.connect(self._on_new_page_url)
        return page

    def _on_new_page_url(self, url: QUrl) -> None:
        """When the dummy page gets a URL, emit it and delete the dummy."""
        sender = self.sender()
        if sender:
            sender.deleteLater()
        if url and not url.isEmpty():
            self.newTabRequested.emit(url)


class _TabMenuBar(QMenuBar):
    """Menu bar whose tab-linked top-level titles double as tab buttons.

    Clicking a linked title while NOT on its tab switches to that tab
    instead of opening the menu; clicking it while already on the tab opens
    the menu. Linked menus with no items left (Agent, Command) act as pure
    buttons — a click never pops an empty popup. Unlinked menus (File,
    Bookmarks, Help) behave normally.

    Menus open on CLICK only: Qt's default mouse tracking lets a hover tear
    down the open popup and open the hovered one whenever a menu is active —
    that path is blocked here. A click-opened popup auto-closes once the
    cursor has left the popup/submenus and the bar for ~450ms."""

    # Active-tab box (see paintEvent) — turquoise against the blue accent.
    _ACTIVE_COLOR = QColor("#2ee6c8")
    _ACTIVE_FILL = QColor(46, 230, 200, 34)
    _ACTIVE_BG = QColor("#0d1117")  # matches the QMenuBar background

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._tabs: Any = None  # QTabWidget to read/switch
        # PySide hands back Python-OWNED QMenu wrappers from addMenu(): drop
        # the last Python reference and the C++ menu is destroyed, silently
        # removing the title from the bar. Keep them all alive here.
        self._menus: List[Any] = []
        # Auto-close for click-opened popups: poll the cursor; once it has
        # been outside the popup (+submenus) and the bar for ~450ms, close.
        self._mouse_menu: Any = None
        self._outside_ticks = 0
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(150)
        self._close_timer.timeout.connect(self._auto_close_check)
        # When a popup closed last — Qt may dismiss it as part of the very
        # click we are about to handle (see mousePressEvent).
        self._menu_closed_at = 0.0

    def addMenu(self, *args, **kwargs):  # noqa: N802
        menu = super().addMenu(*args, **kwargs)
        if menu is not None:
            self._menus.append(menu)
            menu.aboutToHide.connect(self._note_menu_closed)
        return menu

    def _note_menu_closed(self) -> None:
        self._menu_closed_at = time.monotonic()

    def _popup_active(self) -> bool:
        """True while a popup is open — or was, until this very click."""
        for action in self.actions():
            menu = action.menu()
            if menu is not None and menu.isVisible():
                return True
        return (time.monotonic() - self._menu_closed_at) < 0.15

    def _close_popups(self) -> None:
        for action in self.actions():
            menu = action.menu()
            if menu is not None and menu.isVisible():
                menu.close()
        self._close_timer.stop()
        self._mouse_menu = None

    def link_tabs(self, links: Dict[Any, int], tabs: Any) -> None:
        # Tab index is a dynamic property on each menu's QAction (the C++
        # object), NOT a Python-side dict keyed by QMenu — a fresh sip wrapper
        # for the same C++ menu would silently break dict lookups.
        self._tabs = tabs
        for menu, idx in links.items():
            menu.menuAction().setProperty("tab_index", int(idx))
        tabs.currentChanged.connect(lambda *_: self.update())

    def _active_action(self) -> Any:
        """The top-level action linked to the tab currently on screen."""
        if self._tabs is None:
            return None
        current = self._tabs.currentIndex()
        for action in self.actions():
            idx = action.property("tab_index")
            if idx is not None and int(idx) == current:
                return action
        return None

    def paintEvent(self, event) -> None:  # noqa: N802
        # The linked title of the visible tab is boxed in turquoise, so the
        # menu bar reads as a tab strip (the real tab bar stays hidden).
        super().paintEvent(event)
        action = self._active_action()
        if action is None:
            return
        rect = self.actionGeometry(action)
        if rect.isEmpty():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(rect, self._ACTIVE_BG)  # drop the hover/open highlight
        painter.setPen(QPen(self._ACTIVE_COLOR, 1))
        painter.setBrush(self._ACTIVE_FILL)
        painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 1.5, -0.5, -1.5), 4, 4)
        painter.setBrush(Qt.NoBrush)
        painter.setFont(self.font())
        painter.drawText(rect, Qt.AlignCenter, action.text().replace("&", ""))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # With a popup open, a click on the bar only dismisses it: no tab
        # switch, no second menu. The bar is inert until the menu is gone.
        if self._popup_active():
            self._close_popups()
            event.accept()
            return
        action = self.actionAt(event.position().toPoint())
        idx = action.property("tab_index") if action is not None else None
        if idx is not None and self._tabs is not None:
            idx = int(idx)
            if self._tabs.currentIndex() != idx:
                self._tabs.setCurrentIndex(idx)
                event.accept()
                return
            menu = action.menu()
            if menu is not None and menu.isEmpty():
                event.accept()
                return
        super().mousePressEvent(event)
        # Click-opened popup → arm auto-close-on-mouse-away. Keyboard-opened
        # menus are never armed, so arrow-key navigation survives with the
        # mouse parked elsewhere.
        menu = action.menu() if action is not None else None
        if menu is not None and menu.isVisible():
            self._mouse_menu = menu
            self._outside_ticks = 0
            self._close_timer.start()

    def _auto_close_check(self) -> None:
        menu = self._mouse_menu
        if menu is None or not menu.isVisible():
            self._close_timer.stop()
            self._mouse_menu = None
            return
        pos = QCursor.pos()
        # "Inside" = over the bar, the open popup, or any visible submenu
        # (submenus are separate windows beyond the parent popup's rect).
        inside = self.rect().contains(self.mapFromGlobal(pos))
        if not inside:
            popups = [menu] + [s for s in menu.findChildren(QMenu) if s.isVisible()]
            inside = any(m.geometry().contains(pos) for m in popups)
        if inside:
            self._outside_ticks = 0
        else:
            self._outside_ticks += 1
            if self._outside_ticks >= 3:  # ~450ms grace
                menu.close()
                self._close_timer.stop()
                self._mouse_menu = None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        # While any popup is open, swallow bar hover so the mouse can cross
        # other titles without switching the open menu. With no popup open,
        # hover only highlights (default behavior).
        for action in self.actions():
            menu = action.menu()
            if menu is not None and menu.isVisible():
                event.accept()
                return
        super().mouseMoveEvent(event)


class MainWindow(QMainWindow):
    # Merged Agents-tab input: Enter/Agent = conversational torrent agent,
    # Ctrl+Enter/Search = instant web sweep.
    _INPUT_PLACEHOLDER = "AI Deep Search ... (Enter = agent, Ctrl+Enter = web)"

    def __init__(self, config_path: Optional[str] = None) -> None:
        super().__init__()
        self.config_path = config_path or DeeptorrentConfig.default_config_path()
        self.config = DeeptorrentConfig.from_file(self.config_path)
        if not self.config.llm.api_key:
            self.config.llm.provider = "dummy"

        self.engine = TorrentEngine(
            download_limit_kb=self.config.torrents.download_rate_limit_kb,
            upload_limit_kb=self.config.torrents.upload_rate_limit_kb,
            listen_port=self.config.torrents.listen_port,
            max_connections=self.config.torrents.max_connections,
        )
        self.engine.start()
        self._state_manager = TorrentStateManager(
            str(Path.home() / ".deeptorrent")
        )
        self._agent_signals = _AgentSignals()
        self._agent_signals.event.connect(self._on_agent_event)
        self._agent_signals.finished.connect(self._on_agent_finished)
        self._agent_thread: Optional[threading.Thread] = None
        self._search_thread: Optional[threading.Thread] = None
        self._search_timed_out_flag = False
        # Live token-streaming state: doc position where the streamed reply
        # text begins (replaced by the rendered bubble when finished), and
        # whether a reasoning stream block is currently open.
        self._stream_content_start: Optional[int] = None
        self._stream_reasoning_started = False
        # Elapsed-time ticker shown while the agent is working.
        self._busy_started = 0.0
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(1000)
        self._busy_timer.timeout.connect(self._tick_busy)
        self._refresh_signals = _RefreshSignals()
        self._refresh_signals.torrents.connect(self._render_torrents)
        self._refresh_signals.files.connect(self._render_files)
        self._refresh_signals.stream_status.connect(self._on_stream_status)
        self._refresh_inflight = False
        self._selected_hash: Optional[str] = None
        self._files_hash: Optional[str] = None
        self._files_last_fetch = 0.0
        # Stream-while-downloading: info_hash -> wait state (buffer threshold).
        self._stream_waits: Dict[str, Dict[str, Any]] = {}
        # Active torrent stream being playback-capped at the verified
        # download frontier (see _register_stream_playback).
        self._stream_active: Optional[Dict[str, Any]] = None
        self._player_pos: tuple = (0.0, 0.0)
        self._stream_tick_timer = QTimer(self)
        self._stream_tick_timer.setInterval(1000)
        self._stream_tick_timer.timeout.connect(self._on_stream_playback_tick)
        # Tray + completion notifications.
        self._tray: Optional[QSystemTrayIcon] = None
        self._last_notify_path = ""
        self._known_complete: Optional[set] = None
        # --- Download manager (IDM-style) ---  (created before the agent's
        # tool registry: the add_download tool submits jobs to it)
        self._dl_engine = DownloadEngine(self.config.download)
        self._dl_engine.start()
        self._dl_api = ControlAPI(self._dl_engine, self.config.download.control_api_port)
        # File-association / second-instance opens arrive on the HTTP thread;
        # a queued Qt signal delivers them safely to the GUI thread.
        self._open_signals = _OpenSignals()
        self._open_signals.open_target.connect(self.open_target)
        self._dl_api.set_open_handler(self._open_signals.open_target.emit)
        self._play_signals = _PlaySignals()
        self._play_signals.play_stream.connect(self.play_web_stream)
        self._dl_api.set_play_handler(self._play_signals.play_stream.emit)
        self._dl_api.start()

        # IRC client core — shared between the IRC tab and the agent's
        # irc_* tools (same pattern as the download engine above).
        self._irc_client = IRCClientCore(self.config.irc)

        self.tools = ToolRegistry(self.engine, self.config, dl_engine=self._dl_engine,
                                  irc_client=self._irc_client)
        self.agent = AgentLoop(
            self.engine, self.config, tools=self.tools,
            on_event=lambda evt: self._agent_signals.event.emit(evt),
        )
        self._rss_monitor = RSSMonitor(self.config.rss)

        # Jackett integration: auto-start the service when needed and keep the
        # sources list synced with Jackett's configured indexers. Checked at
        # startup (background thread — Jackett may need a cold start) and then
        # hourly; the sync itself is daily-gated inside maybe_auto_sync.
        self._jackett_notice_shown = False  # unreachable notice: once per session
        self._jackett_sync_thread: Optional[threading.Thread] = None
        self._start_jackett_sync()
        self._jackett_timer = QTimer(self)
        self._jackett_timer.setInterval(3600_000)  # 1h; sync itself is daily-gated
        self._jackett_timer.timeout.connect(self._start_jackett_sync)
        self._jackett_timer.start()

        # --- Branding ---
        self.setWindowTitle("DeepFlux 3.0.3 - AI Deep Search")
        self.setGeometry(100, 100, 1200, 800)

        # Set window icon (shows in taskbar, title bar, alt-tab).
        icon_path = self._resolve_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))
            # Also set the app icon for taskbar on Windows.
            if sys.platform == "win32":
                import ctypes
                try:
                    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("deepflux.app")
                except Exception:
                    pass

        # Dark cyberpunk theme for the whole window.
        self.setStyleSheet("""
            /* === Global === */
            QMainWindow, QWidget {
                background-color: #0a0a0f;
                color: #c8d3e0;
                font-family: "Inter", "Segoe UI", "Helvetica Neue", sans-serif;
            }
            QLabel {
                color: #c8d3e0;
            }

            /* === Menu bar === */
            QMenuBar {
                background-color: #0d1117;
                color: #c8d3e0;
                border-bottom: 1px solid #1a2a4a;
                padding: 0px;
            }
            QMenuBar::item { padding: 2px 10px; }
            QMenuBar::item:selected { background-color: #1a2a4a; border-radius: 4px; }
            QMenu {
                background-color: #111827;
                color: #c8d3e0;
                border: 1px solid #1a2a4a;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item { padding: 3px 20px; border-radius: 4px; }
            QMenu::item:selected { background-color: #1a2a4a; color: #2a7abf; }

            /* === Tables === */
            QTableWidget {
                background-color: #0d1117;
                color: #c8d3e0;
                gridline-color: #1a2a4a;
                border: 1px solid #1a2a4a;
                border-radius: 6px;
                selection-background-color: #1a2a4a;
                selection-color: #2a7abf;
                alternate-background-color: #0f1520;
                font-size: 12px;
            }
            QTableWidget::item { padding: 1px 4px; }
            QTableWidget::item:selected { background-color: #1a2a4a; color: #2a7abf; }
            QHeaderView::section {
                background-color: #111827;
                color: #2a7abf;
                border: none;
                border-bottom: 1px solid #1a2a4a;
                padding: 2px 4px;
                font-weight: 600;
                font-size: 11px;
            }

            /* === Buttons === */
            QPushButton {
                background-color: #111827;
                color: #c8d3e0;
                border: 1px solid #1a2a4a;
                border-radius: 3px;
                padding: 2px 10px;
                font-size: 12px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #1a2a4a;
                border: 1px solid #2a7abf;
                color: #2a7abf;
            }
            QPushButton:pressed {
                background-color: #0a1a2a;
            }
            QPushButton:disabled {
                background-color: #0d1117;
                color: #3a4a5a;
                border-color: #1a2a4a;
            }

            /* === Primary action buttons (accent) === */
            QPushButton#btn_accent {
                background-color: #0a1a2e;
                border: 1px solid #2a7abf;
                color: #2a7abf;
            }
            QPushButton#btn_accent:hover {
                background-color: #2a7abf;
                color: #0a0a0f;
            }

            /* === Secondary buttons (subtle) === */
            QPushButton#btn_secondary {
                background-color: #111827;
                border: 1px solid #1a2a4a;
                color: #8a9ab0;
            }
            QPushButton#btn_secondary:hover {
                background-color: #1a2a4a;
                color: #c8d3e0;
            }

            /* === Inputs === */
            QLineEdit {
                background-color: #0d1117;
                color: #c8d3e0;
                border: 1px solid #1a2a4a;
                border-radius: 3px;
                padding: 2px 8px;
                font-size: 12px;
                selection-background-color: #1a2a4a;
            }
            QLineEdit:focus {
                border: 1px solid #2a7abf;
            }
            QLineEdit::placeholder {
                color: #3a4a5a;
            }
            /* Large hero inputs: Agent chat + Browser web search. */
            QLineEdit#big_input {
                font-size: 12px;
                padding: 6px 12px;
                border-radius: 6px;
            }

            /* === Chat panel === */
            QTextEdit {
                background-color: #0a0a0f;
                color: #c8d3e0;
                border: none;
                font-size: 12px;
                line-height: 1.4;
            }

            /* === Tabs === */
            QTabWidget::pane {
                border: 1px solid #1a2a4a;
                background-color: #000000;
            }
            /* Main tab bar is hidden — navigation lives in the menus. */
            QTabWidget#main_tabs::tab-bar {
                height: 0;
                border: none;
            }
            /* Browser tab bar is hidden — the overlay strip replaces it. */
            QTabWidget#browser_tabs::tab-bar {
                height: 0;
                border: none;
            }
            QTabWidget#browser_tabs::pane {
                border: none;
            }
            /* Browser overlay: bookmarks + tab name floating on web content.
               The strip itself is transparent — only the buttons/labels are opaque. */
            QWidget#browser_overlay {
                background: transparent;
            }
            /* Direct children (bookmarks bar container) stay transparent. */
            QWidget#browser_overlay > QWidget {
                background: transparent;
            }
            /* Buttons inside the overlay are opaque. */
            QWidget#browser_overlay QPushButton,
            QWidget#browser_overlay QToolButton {
                background-color: #0d1117;
                color: #c8d3e0;
                border: 1px solid #1a2a4a;
                border-radius: 3px;
                padding: 2px 8px;
            }
            QWidget#browser_overlay QPushButton:hover,
            QWidget#browser_overlay QToolButton:hover {
                background-color: #1a2a4a;
                border: 1px solid #2a7abf;
                color: #2a7abf;
            }
            QTabBar::tab {
                background-color: #0d1117;
                color: #8a9ab0;
                padding: 2px 12px;
                margin-right: 2px;
                border: 1px solid #1a2a4a;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
            }
            QTabBar::tab:selected {
                background-color: #0a0a0f;
                color: #2a7abf;
                border-color: #2a7abf;
            }
            QTabBar::tab:hover:!selected {
                color: #c8d3e0;
            }

            /* === Scroll bars === */
            QScrollBar:vertical {
                background: #0d1117;
                width: 10px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #1a2a4a;
                min-height: 30px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical:hover {
                background: #2a7abf;
            }
            QScrollBar:horizontal {
                background: #0d1117;
                height: 10px;
                border-radius: 5px;
            }
            QScrollBar::handle:horizontal {
                background: #1a2a4a;
                min-width: 30px;
                border-radius: 5px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #2a7abf;
            }
            QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }

            /* === Section labels === */
            QLabel#section_label {
                color: #2a7abf;
                font-size: 11px;
                font-weight: 600;
                padding: 0px;
            }
        """)

        self._build_ui()
        self._start_refresh_timer()
        self._setup_status_bar()
        self._setup_shortcuts()
        self._setup_input_history()
        self.setAcceptDrops(True)

        # Restore window geometry from the previous session; always start
        # on the Browse tab.
        if self.config.ui_geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(self.config.ui_geometry.encode("ascii")))
            except Exception:
                pass
        self.main_tabs.setCurrentWidget(self._browser_tab)

        # Check for incomplete torrents from a previous session.
        QTimer.singleShot(1000, self._check_incomplete_torrents)

        # System tray icon (minimize-to-tray + completion toasts).
        self._setup_tray()

    def changeEvent(self, event) -> None:  # noqa: N802
        # Single source of truth for video-fullscreen chrome: the window can
        # leave fullscreen behind the player's back (Win+Down, showNormal()
        # from the tray / open_target / agent playback), which used to leave
        # the menu bar, tab bar and status bar hidden for good.
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and not self.isFullScreen():
            iptv = getattr(self, "iptv_tab", None)
            if iptv is not None:
                try:
                    iptv.sync_fullscreen_chrome(False)
                except Exception:
                    logger.exception("failed to restore fullscreen chrome")

    def set_video_fullscreen_chrome(self, on: bool) -> None:
        """Collapse the chrome that survives fullscreen mode: the tab pane's
        1px frame + the 4px margins around the tab widget read as a faint
        bright ring at the screen edges once the video owns the screen."""
        self.main_tabs.setStyleSheet("QTabWidget::pane { border: none; }" if on else "")
        m = 0 if on else 4
        self._tabs_layout.setContentsMargins(m, m, m, m)

    def _restore_splitters(self) -> None:
        """Restore user-adjusted splitter sizes from the previous session."""
        # NOTE: versioned keys ("agent_v4", "browser_v4") — when a layout's
        # default ratio changes, bump the key so stale saved states from the
        # old layout don't override the new default.
        for name, splitter in (
            ("agent_v4", self.agent_splitter),
            ("commander_v1", self.commander_tab.splitter),
        ):
            state = self.config.ui_splitters.get(name, "")
            if state:
                try:
                    splitter.restoreState(QByteArray.fromBase64(state.encode("ascii")))
                except Exception:
                    pass

    def _save_splitters(self) -> None:
        """Persist splitter sizes so the layout survives restarts."""
        for name, splitter in (
            ("agent_v4", self.agent_splitter),
            ("commander_v1", self.commander_tab.splitter),
        ):
            try:
                self.config.ui_splitters[name] = bytes(splitter.saveState().toBase64()).decode("ascii")
            except Exception:
                pass

    def _resolve_icon_path(self) -> str:
        """Find the icon file — works in dev mode and PyInstaller bundle."""
        candidates = [
            # Dev mode: relative to project root.
            os.path.join(os.path.dirname(__file__), "..", "packaging", "icon.ico"),
            os.path.join(os.path.dirname(__file__), "..", "DeepTorrent.png"),
            # PyInstaller bundle.
            os.path.join(sys._MEIPASS, "packaging", "icon.ico") if hasattr(sys, "_MEIPASS") else "",
            os.path.join(sys._MEIPASS, "DeepTorrent.png") if hasattr(sys, "_MEIPASS") else "",
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return os.path.abspath(path)
        return ""

    def _resolve_logo_path(self) -> str:
        """Find the logo PNG for the branding header."""
        candidates = [
            os.path.join(os.path.dirname(__file__), "..", "packaging", "logo_48.png"),
            os.path.join(os.path.dirname(__file__), "..", "DeepFlux3.png"),
            os.path.join(sys._MEIPASS, "packaging", "logo_48.png") if hasattr(sys, "_MEIPASS") else "",
            os.path.join(sys._MEIPASS, "DeepFlux3.png") if hasattr(sys, "_MEIPASS") else "",
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return os.path.abspath(path)
        return ""

    def _resolve_watermark_path(self) -> str:
        """Find the full-size logo PNG for the chat watermark (prefers high-res)."""
        candidates = [
            os.path.join(os.path.dirname(__file__), "..", "DeepFlux3.png"),
            os.path.join(sys._MEIPASS, "DeepFlux3.png") if hasattr(sys, "_MEIPASS") else "",
            os.path.join(os.path.dirname(__file__), "..", "packaging", "logo_48.png"),
            os.path.join(sys._MEIPASS, "packaging", "logo_48.png") if hasattr(sys, "_MEIPASS") else "",
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return os.path.abspath(path)
        return ""

    def _load_browser_icon(self, name: str) -> QIcon:
        """Load an SVG or PNG icon for the browser nav bar."""
        # Try SVG first (crisp at any size), then PNG fallback.
        for ext in ("svg", "png"):
            candidates = [
                os.path.join(os.path.dirname(__file__), "..", "packaging", "icons", f"{name}.{ext}"),
                os.path.join(sys._MEIPASS, "packaging", "icons", f"{name}.{ext}") if hasattr(sys, "_MEIPASS") else "",
            ]
            for path in candidates:
                if path and os.path.isfile(path):
                    if ext == "svg":
                        with open(path, "rb") as f:
                            data = QByteArray(f.read())
                        renderer = QSvgRenderer(data)
                        if renderer.isValid():
                            pixmap = QPixmap(20, 20)
                            pixmap.fill(Qt.GlobalColor.transparent)
                            from PySide6.QtGui import QPainter
                            painter = QPainter(pixmap)
                            renderer.render(painter)
                            painter.end()
                            return QIcon(pixmap)
                    else:
                        return QIcon(path)
        return QIcon()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # --- Main content: a single tabbed interface ---
        layout = QHBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        outer_layout.addLayout(layout)
        self._tabs_layout = layout  # margins collapse during fullscreen video

        # Top-level tabs — tab bar hidden; navigation lives in the menus
        # via "Go to" items. QSS collapses the bar so the layout can't
        # re-expand it (plain .hide() doesn't stick on Qt).
        self.main_tabs = QTabWidget()
        self.main_tabs.setObjectName("main_tabs")
        self.main_tabs.tabBar().hide()
        layout.addWidget(self.main_tabs)

        # --- Agents tab: one merged deep-search window ---------------------
        # A single history + input serving both pipelines:
        #   Enter / [Agent]         -> the conversational torrent agent
        #   Ctrl+Enter / [Search]   -> instant web sweep (no agent turn)
        # The DeepFlux logo shows centered until the first message is sent.
        agents_tab = QWidget()
        agents_tab.setStyleSheet("background-color: #000000;")
        agents_tab_layout = QVBoxLayout(agents_tab)
        agents_tab_layout.setContentsMargins(0, 0, 0, 0)

        self.chat_history = _ChatHistoryEdit(self._resolve_watermark_path())
        self.chat_history.setReadOnly(True)
        self.chat_history.setOpenLinks(False)
        # Bottom dead-zone (~2 rows): at max scroll the viewport fold lands in
        # this blank strip instead of clipping the last content row in half
        # (most visible on the 20-row web-sweep results table).
        self.chat_history.setViewportMargins(0, 0, 0, 28)
        self.chat_history.anchorClicked.connect(self._on_chat_link_clicked)
        self.chat_history.document().setDefaultStyleSheet(CHAT_CSS)
        self.chat_history.setStyleSheet("QTextBrowser { background-color: transparent; color: #c8d3e0; border: 1px solid #1a2a4a; border-radius: 3px; padding: 2px; }")
        agents_tab_layout.addWidget(self.chat_history)

        agent_input_layout = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setObjectName("big_input")
        self.chat_input.setPlaceholderText(self._INPUT_PLACEHOLDER)
        self.chat_input.returnPressed.connect(self._on_send)
        agent_input_layout.addWidget(self.chat_input)

        voice_btn = QPushButton("🎤")
        voice_btn.setObjectName("btn_secondary")
        voice_btn.setToolTip("Dictate to the agent (local whisper, offline) — click to start, click again to stop & send")
        voice_btn.setFixedWidth(44)
        voice_btn.clicked.connect(self._on_voice_toggle)
        self.voice_btn = voice_btn
        agent_input_layout.addWidget(voice_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setObjectName("btn_secondary")
        clear_btn.setToolTip("Clear the history (also resets the agent's memory of this conversation)")
        clear_btn.clicked.connect(self._clear_chat)
        self.clear_btn = clear_btn
        agent_input_layout.addWidget(clear_btn)

        search_btn = QPushButton("Search")
        search_btn.setObjectName("btn_accent")
        search_btn.setToolTip("Instant web sweep — no agent involved (Ctrl+Enter)")
        search_btn.clicked.connect(self._on_search_send)
        self.search_btn = search_btn
        agent_input_layout.addWidget(search_btn)

        send_btn = QPushButton("Agent")
        send_btn.setObjectName("btn_accent")
        send_btn.setToolTip("Ask the conversational torrent agent (Enter)")
        send_btn.clicked.connect(self._on_send)
        self.send_btn = send_btn
        agent_input_layout.addWidget(send_btn)
        agents_tab_layout.addLayout(agent_input_layout)

        # --- Torrents panel (top-right of the Agent tab) ---
        torrents_tab = QWidget()
        torrents_tab_layout = QVBoxLayout(torrents_tab)
        torrents_tab_layout.setContentsMargins(0, 0, 0, 0)

        # Quick-action buttons above the torrent list
        btn_layout = QHBoxLayout()
        add_magnet_btn = QPushButton("+ Magnet")
        add_magnet_btn.setObjectName("btn_accent")
        add_magnet_btn.clicked.connect(self._add_magnet_dialog)
        btn_layout.addWidget(add_magnet_btn)
        add_file_btn = QPushButton("+ Torrent File")
        add_file_btn.setObjectName("btn_accent")
        add_file_btn.clicked.connect(self._add_torrent_file_dialog)
        btn_layout.addWidget(add_file_btn)
        rss_btn = QPushButton("RSS")
        rss_btn.clicked.connect(self._open_rss_dialog)
        btn_layout.addWidget(rss_btn)
        btn_layout.addStretch()
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._remove_selected_torrent)
        btn_layout.addWidget(remove_btn)
        clear_completed_btn = QPushButton("Clear Completed")
        clear_completed_btn.clicked.connect(self._clear_completed_torrents)
        btn_layout.addWidget(clear_completed_btn)
        torrents_tab_layout.addLayout(btn_layout)

        torrents_label = QLabel("◈ Torrents")
        torrents_label.setObjectName("section_label")
        torrents_tab_layout.addWidget(torrents_label)
        self.torrent_table = QTableWidget()
        self.torrent_table.setColumnCount(10)
        self.torrent_table.setHorizontalHeaderLabels(
            ["Name", "State", "Progress", "Down", "Up", "ETA", "Seeds", "Peers", "Size", "Health"]
        )
        self.torrent_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.torrent_table.setAlternatingRowColors(True)
        self.torrent_table.setSortingEnabled(True)
        self.torrent_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.torrent_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.torrent_table.itemSelectionChanged.connect(self._on_torrent_selection_changed)
        self.torrent_table.itemDoubleClicked.connect(self._on_torrent_double_clicked)
        self.torrent_table.verticalHeader().setDefaultSectionSize(20)
        torrents_tab_layout.addWidget(self.torrent_table)

        # Details / file priorities
        files_label = QLabel("◈ Files")
        files_label.setObjectName("section_label")
        torrents_tab_layout.addWidget(files_label)
        self.file_table = QTableWidget()
        self.file_table.setColumnCount(4)
        self.file_table.setHorizontalHeaderLabels(["#", "Path", "Size", "Priority"])
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_table.customContextMenuRequested.connect(self._file_context_menu)
        self.file_table.verticalHeader().setDefaultSectionSize(20)
        torrents_tab_layout.addWidget(self.file_table)

        # --- Downloads panel (bottom-right of the Agent tab) ---
        downloads_panel = QWidget()
        downloads_panel_layout = QVBoxLayout(downloads_panel)
        downloads_panel_layout.setContentsMargins(0, 0, 0, 0)
        downloads_panel_layout.setSpacing(2)
        downloads_label = QLabel("◈ Downloads")
        downloads_label.setObjectName("section_label")
        downloads_panel_layout.addWidget(downloads_label)
        self.downloads_tab = DownloadsTab(self._dl_engine, self.config, self)
        self.downloads_tab.set_play_callback(self.play_file_in_player)
        self.downloads_tab.set_torrent_callback(self._on_downloaded_torrent_file)
        self.downloads_tab.set_notify_callback(
            lambda job: self._notify("Download complete", job.filename, path=job.save_path))
        downloads_panel_layout.addWidget(self.downloads_tab)

        # Downloads tab: torrents on top / downloads below, full height —
        # the merged deep-search window lives on the Agents tab.
        self.agent_splitter = QSplitter(Qt.Vertical)
        self.agent_splitter.addWidget(torrents_tab)
        self.agent_splitter.addWidget(downloads_panel)
        self.agent_splitter.setStretchFactor(0, 1)
        self.agent_splitter.setStretchFactor(1, 1)
        self.agent_splitter.setChildrenCollapsible(True)
        # setSizes() before the window is shown gets overridden by widget size
        # hints; enforce the 50/50 split once the real height is known.
        self._agent_ratio_applied = False
        self.main_tabs.addTab(self.agent_splitter, "Download")
        # Drag & drop and other flows surface this tab to show the torrent list.
        self._torrents_tab = self.agent_splitter

        # --- Browser panel (the whole Browser tab) ---
        browser_tab = QWidget()
        browser_tab_layout = QVBoxLayout(browser_tab)
        browser_tab_layout.setContentsMargins(0, 0, 0, 0)
        browser_tab_layout.setSpacing(2)

        # Shared profile for all browser tabs — named + on-disk so cookies,
        # logins, and cache survive app restarts (a default-constructed
        # profile is off-the-record and forgets everything).
        browser_storage = str(Path.home() / ".deeptorrent" / "browser")
        os.makedirs(browser_storage, exist_ok=True)
        self.browser_profile = QWebEngineProfile("deeptorrent", self)
        self.browser_profile.setPersistentStoragePath(browser_storage)
        self.browser_profile.setCachePath(os.path.join(browser_storage, "cache"))
        from dlmgr.browser_extension import inject_into_profile
        inject_into_profile(self.browser_profile)
        from dlmgr.adblock import AdBlockInterceptor
        self.adblock_interceptor = AdBlockInterceptor()
        self.adblock_interceptor.set_enabled(self.config.browser.adblock_enabled)
        self.browser_profile.setUrlRequestInterceptor(self.adblock_interceptor)
        # Keep a live cache of the browser session's cookies so file
        # downloads handed to the internal download manager carry the same
        # login session the browser has (private sites gate files on it).
        self._browser_cookies: Dict[str, Dict[str, str]] = {}
        self.browser_profile.cookieStore().cookieAdded.connect(self._on_browser_cookie_added)
        # Handle file downloads triggered inside the browser: .torrent files
        # are added to the torrent engine automatically; everything else is
        # routed to the internal segmented download manager (Downloads panel).
        self.browser_profile.downloadRequested.connect(self._on_browser_download_requested)

        # Navigation bar
        nav_layout = QHBoxLayout()
        self.browser_back_btn = QPushButton()
        self.browser_back_btn.setObjectName("btn_secondary")
        self.browser_back_btn.setFixedWidth(34)
        self.browser_back_btn.setIcon(self._load_browser_icon("back"))
        self.browser_back_btn.setToolTip("Back")
        self.browser_back_btn.clicked.connect(lambda: self._current_browser_view().back())
        nav_layout.addWidget(self.browser_back_btn)

        self.browser_fwd_btn = QPushButton()
        self.browser_fwd_btn.setObjectName("btn_secondary")
        self.browser_fwd_btn.setFixedWidth(34)
        self.browser_fwd_btn.setIcon(self._load_browser_icon("forward"))
        self.browser_fwd_btn.setToolTip("Forward")
        self.browser_fwd_btn.clicked.connect(lambda: self._current_browser_view().forward())
        nav_layout.addWidget(self.browser_fwd_btn)

        self.browser_reload_btn = QPushButton()
        self.browser_reload_btn.setObjectName("btn_secondary")
        self.browser_reload_btn.setFixedWidth(34)
        self.browser_reload_btn.setIcon(self._load_browser_icon("reload"))
        self.browser_reload_btn.setToolTip("Reload")
        self.browser_reload_btn.clicked.connect(lambda: self._current_browser_view().reload())
        nav_layout.addWidget(self.browser_reload_btn)

        self.browser_home_btn = QPushButton()
        self.browser_home_btn.setObjectName("btn_secondary")
        self.browser_home_btn.setFixedWidth(34)
        self.browser_home_btn.setIcon(self._load_browser_icon("home"))
        self.browser_home_btn.setToolTip("Home")
        self.browser_home_btn.clicked.connect(self._browser_go_home)
        nav_layout.addWidget(self.browser_home_btn)

        # New tab button
        self.browser_new_tab_btn = QPushButton()
        self.browser_new_tab_btn.setObjectName("btn_secondary")
        self.browser_new_tab_btn.setFixedWidth(34)
        self.browser_new_tab_btn.setIcon(self._load_browser_icon("newtab"))
        self.browser_new_tab_btn.setToolTip("New Tab")
        self.browser_new_tab_btn.clicked.connect(lambda: self._browser_new_tab())
        nav_layout.addWidget(self.browser_new_tab_btn)

        self.browser_url_bar = QLineEdit()
        self.browser_url_bar.setPlaceholderText("Enter URL or search...")
        self.browser_url_bar.returnPressed.connect(self._browser_navigate)
        nav_layout.addWidget(self.browser_url_bar)

        self.browser_go_btn = QPushButton("Go")
        self.browser_go_btn.setObjectName("btn_accent")
        self.browser_go_btn.clicked.connect(self._browser_navigate)
        nav_layout.addWidget(self.browser_go_btn)

        self.browser_bookmark_btn = QPushButton()
        self.browser_bookmark_btn.setObjectName("btn_secondary")
        self.browser_bookmark_btn.setFixedWidth(34)
        self.browser_bookmark_btn.setIcon(self._load_browser_icon("bookmark"))
        self.browser_bookmark_btn.setToolTip("Bookmark this page")
        self.browser_bookmark_btn.clicked.connect(self._browser_add_bookmark)
        nav_layout.addWidget(self.browser_bookmark_btn)

        # Ad-block toggle button
        self.browser_adblock_btn = QPushButton()
        self.browser_adblock_btn.setObjectName("btn_secondary")
        self.browser_adblock_btn.setCheckable(True)
        self.browser_adblock_btn.setChecked(self.config.browser.adblock_enabled)
        self.browser_adblock_btn.setIcon(self._load_browser_icon("adblock"))
        self.browser_adblock_btn.setToolTip("Toggle ad blocking (off by default)")
        self.browser_adblock_btn.setFixedWidth(34)
        self._update_adblock_button_style()
        self.browser_adblock_btn.clicked.connect(self._toggle_adblock)
        nav_layout.addWidget(self.browser_adblock_btn)

        browser_tab_layout.addLayout(nav_layout)

        # Tabbed browser — each tab holds a QWebEngineView sharing the profile.
        self.browser_tabs = QTabWidget()
        self.browser_tabs.setObjectName("browser_tabs")
        self.browser_tabs.setTabsClosable(True)
        self.browser_tabs.tabCloseRequested.connect(self._browser_close_tab)
        self.browser_tabs.currentChanged.connect(self._browser_tab_changed)
        # Hide the built-in tab bar — the overlay strip replaces it.
        self.browser_tabs.tabBar().hide()
        # DOM fullscreen state: (view, tab index, title, tooltip) while a web
        # view owns the screen, plus its ESC shortcut.
        self._browser_fs_state = None
        self._browser_fs_esc = None

        # Content container: web view fills it, overlay floats on top.
        self._browser_content = QWidget()
        _bc_layout = QVBoxLayout(self._browser_content)
        _bc_layout.setContentsMargins(0, 0, 0, 0)
        _bc_layout.setSpacing(0)
        _bc_layout.addWidget(self.browser_tabs)

        # Overlay strip: bookmarks + tab name, semi-transparent, on top.
        self._browser_overlay = QWidget(self._browser_content)
        self._browser_overlay.setObjectName("browser_overlay")
        self._browser_overlay.setAttribute(Qt.WA_TranslucentBackground)
        self._browser_overlay.setAttribute(Qt.WA_AlwaysStackOnTop)
        _ov = QVBoxLayout(self._browser_overlay)
        _ov.setContentsMargins(0, 0, 0, 0)
        _ov.setSpacing(0)

        # Tab strip: one chip per open tab, floats over web content.
        self._tab_strip_container = QWidget()
        self._tab_strip_container.setAttribute(Qt.WA_TranslucentBackground)
        self._tab_strip_layout = QHBoxLayout(self._tab_strip_container)
        self._tab_strip_layout.setContentsMargins(4, 0, 0, 2)
        self._tab_strip_layout.setSpacing(2)
        self._tab_strip_layout.addStretch()
        _ov.addWidget(self._tab_strip_container)

        self._browser_overlay.raise_()
        browser_tab_layout.addWidget(self._browser_content)
        # Reposition the overlay whenever the content area resizes.
        self._browser_content.installEventFilter(self)

        # Auto-hide: poll mouse position — show the overlay when the cursor
        # is near the top edge, hide it when the cursor moves into the page.
        # This avoids fighting with fixed-position page elements (YouTube's
        # search bar etc.) that ignore document padding.
        self._overlay_hide_timer = QTimer(self)
        self._overlay_hide_timer.setSingleShot(True)
        self._overlay_hide_timer.timeout.connect(self._browser_overlay.hide)
        self._overlay_poll = QTimer(self)
        self._overlay_poll.timeout.connect(self._poll_overlay_hover)
        self._overlay_poll.start(80)

        # Create the first tab (after the overlay so tab_changed can
        # update the label).
        self._browser_new_tab(url=QUrl(self.config.browser.homepage))

        # Keep a reference to the first view for backward compatibility.
        self.browser_view = self._current_browser_view()

        # Let the agent's browser_* tools drive the browser (every action is
        # queued onto the GUI thread; the calling worker waits for the result).
        self._browser_bridge = BrowserBridge(self)
        self.tools.set_browser_bridge(self._browser_bridge)

        # Agents tab (leftmost): the merged deep-search window.
        self.main_tabs.insertTab(0, agents_tab, "Agent")

        # Browser tab: the browser alone takes the whole tab.
        self.main_tabs.insertTab(0, browser_tab, "Browse")
        self._browser_tab = browser_tab
        self._agents_tab = agents_tab

        # --- IPTV tab (M3U/Xtream player + metadata) ---
        self.iptv_tab = IPTVTab(self.config, self)
        self.iptv_tab.set_settings_callback(self._open_iptv_settings)
        self.iptv_tab.set_engine(self.engine)
        self.main_tabs.addTab(self.iptv_tab, "Play")
        # Let the agent's iptv_* tools drive playback (queued onto the GUI
        # thread) and read the playlist/player state.
        self._iptv_bridge = AgentIPTVBridge(self.iptv_tab)
        self.tools.set_iptv_bridge(self._iptv_bridge)
        # Player position feeds the torrent-stream frontier cap.
        self.iptv_tab._player.sig_position.connect(self._on_player_position)

        # --- Commander tab (dual-pane file manager) ---
        self.commander_tab = CommanderTab(self.config, self)
        self.main_tabs.addTab(self.commander_tab, "Command")

        # --- IRC tab (embedded IRC client; shares its core with the agent) ---
        self.irc_tab = IRCTab(self.config, self._irc_client, self)
        self.main_tabs.addTab(self.irc_tab, "IRC")

        # Restore saved splitter positions (user-adjusted sizes persist).
        self._restore_splitters()

        # Default to the Browse tab
        self.main_tabs.setCurrentWidget(self._browser_tab)

        # Menu bar — tab-linked titles double as tab buttons (see _TabMenuBar).
        menubar = _TabMenuBar(self)
        self.setMenuBar(menubar)
        file_menu = menubar.addMenu("File")

        api_keys_action = QAction("API Keys...", self)
        api_keys_action.triggered.connect(self._open_api_keys)
        file_menu.addAction(api_keys_action)

        export_action = QAction("Export Settings...", self)
        export_action.triggered.connect(self._export_settings)
        file_menu.addAction(export_action)

        import_action = QAction("Import Settings...", self)
        import_action.triggered.connect(self._import_settings)
        file_menu.addAction(import_action)

        file_menu.addSeparator()

        assoc_action = QAction("Set as Default App (File Associations)...", self)
        assoc_action.triggered.connect(self._register_file_associations)
        file_menu.addAction(assoc_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self._tray_quit)
        file_menu.addAction(exit_action)

        # Browse menu — title jumps to the tab; items below once there.
        browse_menu = menubar.addMenu("Browse")

        browser_settings_action = QAction("Browser Settings...", self)
        browser_settings_action.triggered.connect(self._open_browser_settings)
        browse_menu.addAction(browser_settings_action)

        browse_menu.addSeparator()

        import_bookmarks_action = QAction("Import Bookmarks...", self)
        import_bookmarks_action.triggered.connect(self._import_bookmarks)
        browse_menu.addAction(import_bookmarks_action)

        # Agent menu — no items left; the title is a pure tab button.
        agent_menu = menubar.addMenu("Agent")

        # Download menu — title jumps to the tab; items below once there.
        download_menu = menubar.addMenu("Download")

        add_magnet_action = QAction("Add Magnet...", self)
        add_magnet_action.triggered.connect(self._add_magnet_dialog)
        download_menu.addAction(add_magnet_action)

        add_torrent_action = QAction("Add Torrent File...", self)
        add_torrent_action.triggered.connect(self._add_torrent_file_dialog)
        download_menu.addAction(add_torrent_action)

        download_menu.addSeparator()

        indexer_settings_action = QAction("Jackett Settings...", self)
        indexer_settings_action.triggered.connect(self._open_indexer_settings)
        download_menu.addAction(indexer_settings_action)

        downloads_settings_action = QAction("Downloads Settings...", self)
        downloads_settings_action.triggered.connect(self._open_downloads_settings)
        download_menu.addAction(downloads_settings_action)

        sources_action = QAction("Sources...", self)
        sources_action.triggered.connect(self._open_sources)
        download_menu.addAction(sources_action)

        rss_action2 = QAction("RSS Feeds...", self)
        rss_action2.triggered.connect(self._open_rss_dialog)
        download_menu.addAction(rss_action2)

        # Play menu — title jumps to the tab; items below once there.
        play_menu = menubar.addMenu("Play")

        for label, _cls in IPTV_SETTINGS_PAGES:
            action = QAction(label, self)
            action.triggered.connect(lambda _c=False, page_cls=_cls: self._open_iptv_page(page_cls))
            play_menu.addAction(action)

        # Command menu — no items; the title is a pure tab button.
        command_menu = menubar.addMenu("Command")

        # IRC menu — title jumps to the tab; items below once there.
        irc_menu = menubar.addMenu("IRC")

        irc_networks_action = QAction("Networks...", self)
        irc_networks_action.triggered.connect(lambda: self.irc_tab._on_manage_networks())
        irc_menu.addAction(irc_networks_action)

        # Help dropdown menu
        help_menu = menubar.addMenu("Help")

        guide_action = QAction("User Guide", self)
        guide_action.triggered.connect(self._open_help)
        help_menu.addAction(guide_action)

        about_action = QAction("About", self)
        about_action.triggered.connect(self._open_about)
        help_menu.addAction(about_action)

        # Bookmarks menu — right of Help, only visible on the Browse tab.
        self._bookmarks_menu = menubar.addMenu("Bookmarks")
        self._rebuild_bookmarks_bar()
        self.main_tabs.currentChanged.connect(self._update_bookmarks_menu_visibility)
        self._update_bookmarks_menu_visibility(self.main_tabs.currentIndex())

        # Tab-linked menu titles act as buttons: a click from another tab
        # switches tabs; a click while on the tab opens the menu.
        menubar.link_tabs({
            browse_menu: 0,
            agent_menu: 1,
            download_menu: 2,
            play_menu: 3,
            command_menu: 4,
            irc_menu: 5,
        }, self.main_tabs)

        # Right-click context menu on the torrent table
        self.torrent_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.torrent_table.customContextMenuRequested.connect(self._torrent_context_menu)

    def _start_refresh_timer(self) -> None:
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(1000)

        # RSS monitor timer — checks auto-download feeds periodically.
        self._rss_timer = QTimer(self)
        self._rss_timer.timeout.connect(self._check_rss_feeds)
        interval = max(60, self.config.rss.check_interval_seconds) * 1000
        self._rss_timer.start(interval)

    # ------------------------------------------------------------------
    # Status bar / shortcuts / input history / drag & drop
    # ------------------------------------------------------------------

    def _setup_status_bar(self) -> None:
        """Status strip: hover URL on the left, live transfer summary right."""
        self.statusBar().setStyleSheet("QStatusBar { background: #0d1117; border-top: 1px solid #1a2a4a; }")
        # Left: hovered link URL (browser linkHovered).
        self._hover_label = QLabel("")
        self._hover_label.setStyleSheet("color: #8a9ab0; font-size: 11px; padding: 2px 8px;")
        self.statusBar().addWidget(self._hover_label, 1)
        # Right: permanent transfer summary.
        self._status_label = QLabel("Ready")
        self._status_label.setStyleSheet("color: #8a9ab0; font-size: 11px; padding: 2px 8px;")
        self._status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.statusBar().addPermanentWidget(self._status_label)

    def _update_status_bar(self, torrents: List[Dict[str, Any]]) -> None:
        """Refresh the status bar summary (called from _render_torrents)."""
        t_down = sum(t.get("download_rate", 0) for t in torrents)
        t_up = sum(t.get("upload_rate", 0) for t in torrents)
        t_active = sum(1 for t in torrents if t.get("state") == "downloading" and not t.get("paused"))
        try:
            jobs = self._dl_engine.list_jobs()
        except Exception:
            jobs = []
        d_active = sum(1 for j in jobs if j.status.value in ("downloading", "queued"))
        d_speed = sum(j.speed_bps for j in jobs if j.status.value == "downloading")

        parts = []
        if t_active:
            parts.append(f"Torrents: {t_active} active  ↓ {format_rate(t_down)}  ↑ {format_rate(t_up)}")
        elif torrents:
            parts.append(f"Torrents: {len(torrents)} idle")
        if d_active:
            parts.append(f"Downloads: {d_active} active  ↓ {format_rate(d_speed)}")
        self._status_label.setText("   •   ".join(parts) if parts else "Ready")

    def _setup_shortcuts(self) -> None:
        """Global + browser-scoped keyboard shortcuts."""
        def sc(seq: str, fn, parent=None, context=Qt.ApplicationShortcut) -> None:
            s = QShortcut(QKeySequence(seq), parent or self)
            s.setContext(context)
            s.activated.connect(fn)

        for i in range(self.main_tabs.count()):
            sc(f"Ctrl+{i + 1}", lambda idx=i: self.main_tabs.setCurrentIndex(idx))
        sc("Ctrl+M", self._add_magnet_dialog)
        sc("Ctrl+O", self._add_torrent_file_dialog)
        sc("Ctrl+F", lambda: (self.main_tabs.setCurrentWidget(self._agents_tab), self.chat_input.setFocus()))
        sc("Ctrl+Q", self.close)

        # Browser-scoped: only fire while the Browser tab is shown.
        bt = self._browser_tab
        sc("Ctrl+T", lambda: self._browser_new_tab(), parent=bt, context=Qt.WidgetWithChildrenShortcut)
        sc("Ctrl+W", lambda: self._browser_close_tab(self.browser_tabs.currentIndex()), parent=bt, context=Qt.WidgetWithChildrenShortcut)
        sc("F5", lambda: self._current_browser_view().reload(), parent=bt, context=Qt.WidgetWithChildrenShortcut)
        sc("Ctrl+L", self._browser_focus_url)
        # Zoom controls (browser-scoped).
        sc("Ctrl+=", lambda: self._browser_zoom(0.1), parent=bt, context=Qt.WidgetWithChildrenShortcut)
        sc("Ctrl++", lambda: self._browser_zoom(0.1), parent=bt, context=Qt.WidgetWithChildrenShortcut)
        sc("Ctrl+-", lambda: self._browser_zoom(-0.1), parent=bt, context=Qt.WidgetWithChildrenShortcut)
        sc("Ctrl+0", lambda: self._current_browser_view().setZoomFactor(1.0), parent=bt, context=Qt.WidgetWithChildrenShortcut)

    def _browser_focus_url(self) -> None:
        self.main_tabs.setCurrentWidget(self._browser_tab)
        self.browser_url_bar.setFocus()
        self.browser_url_bar.selectAll()

    def _browser_zoom(self, delta: float) -> None:
        view = self._current_browser_view()
        view.setZoomFactor(max(0.5, min(3.0, view.zoomFactor() + delta)))

    def _poll_overlay_hover(self) -> None:
        """Show the browser overlay when the cursor is near the top edge,
        hide it (with a short delay) when the cursor moves into the page."""
        from PySide6.QtGui import QCursor
        pos = self._browser_content.mapFromGlobal(QCursor.pos())
        overlay_h = self._browser_overlay.sizeHint().height()
        in_zone = (0 <= pos.x() <= self._browser_content.width()
                   and pos.y() < overlay_h + 6)
        if in_zone:
            self._overlay_hide_timer.stop()
            if not self._browser_overlay.isVisible():
                self._browser_overlay.show()
                self._browser_overlay.raise_()
        elif self._browser_overlay.isVisible() and not self._browser_overlay.underMouse():
            if not self._overlay_hide_timer.isActive():
                self._overlay_hide_timer.start(400)

    def _setup_input_history(self) -> None:
        """Terminal-style Up/Down recall for the merged Agents input."""
        self._input_history: List[str] = []
        self._input_history_idx = -1
        self._agent_busy = False
        self._search_busy = False
        self.chat_input.installEventFilter(self)
        self.browser_url_bar.installEventFilter(self)  # select-all on focus

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # A fullscreened web view getting Alt+F4 / window-close: exit DOM
        # fullscreen instead of closing (it has no tab to return to).
        if (self._browser_fs_state is not None
                and obj is self._browser_fs_state[0]
                and event.type() == QEvent.Close):
            event.ignore()
            obj.page().triggerAction(QWebEnginePage.ExitFullScreen)
            return True
        # Browser overlay: keep it pinned to the top of the content area
        # and re-raise it above the native web-view window.
        if obj is self._browser_content and event.type() == QEvent.Resize:
            self._browser_overlay.setGeometry(
                0, 0, self._browser_content.width(),
                self._browser_overlay.sizeHint().height())
            self._browser_overlay.raise_()
        if obj is self.chat_input and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Up, Qt.Key_Down):
                self._input_history_nav(-1 if event.key() == Qt.Key_Up else 1)
                return True
            # Ctrl+Enter routes to the web sweep; swallow it so QLineEdit
            # doesn't also emit returnPressed (which is wired to the agent).
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier:
                self._on_search_send()
                return True
        if obj is self.browser_url_bar and event.type() == QEvent.FocusIn:
            # Select-all like a real browser (deferred so the click doesn't
            # collapse the selection).
            QTimer.singleShot(0, self.browser_url_bar.selectAll)
        return super().eventFilter(obj, event)

    def _input_history_nav(self, delta: int) -> None:
        if not self._input_history:
            return
        if self._input_history_idx == -1:
            self._input_history_idx = len(self._input_history)
        self._input_history_idx = max(0, min(len(self._input_history), self._input_history_idx + delta))
        if self._input_history_idx == len(self._input_history):
            self.chat_input.clear()
        else:
            self.chat_input.setText(self._input_history[self._input_history_idx])
            self.chat_input.end()

    # -- drag & drop ---------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        md = event.mimeData()
        if md.hasUrls() or (md.hasText() and md.text().strip().startswith("magnet:")):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        md = event.mimeData()
        added = 0
        errors = 0
        candidates: List[str] = []
        for u in md.urls():
            candidates.append(u.toLocalFile() or u.toString())
        if not candidates and md.hasText():
            candidates.append(md.text().strip())

        ignored = 0
        for s in candidates:
            s = s.strip()
            try:
                if (s.lower().endswith(".torrent") and os.path.isfile(s)) or (
                    s.lower().startswith(("http://", "https://")) and ".torrent" in s.lower()
                ):
                    self.tools.call("add_torrent_file", {
                        "path": s,
                        "save_path": self.config.default_save_path,
                        "category": "Other",
                    })
                    added += 1
                elif s.startswith("magnet:"):
                    self.tools.call("add_magnet", {
                        "uri": s,
                        "save_path": self.config.default_save_path,
                        "category": "Other",
                    })
                    added += 1
                else:
                    ignored += 1
            except Exception as exc:
                errors += 1
                logger.warning("drag&drop add failed for %s: %s", s[:60], exc)
        if added:
            self._append_agent(f"**Added {added} torrent(s) via drag & drop.**")
            # Surface the Torrents tab so the user sees the result.
            self.main_tabs.setCurrentWidget(self._torrents_tab)
        if errors:
            self._append_error(f"Drag & drop: {errors} item(s) could not be added.")
        if ignored:
            self._append_error(
                f"Drag & drop: {ignored} item(s) not recognized — drop a .torrent file, "
                "a magnet link, or a .torrent download URL."
            )
        if added or errors:
            self._refresh()

    def _on_send(self) -> None:
        """Send a message to the AI agent (Agent button / Enter)."""
        text = self.chat_input.text().strip()
        if not text:
            return
        # Don't allow a new message while the agent is still working.
        if self._agent_thread and self._agent_thread.is_alive():
            self._append_search_error("The agent is still working — please wait.")
            return
        self.chat_input.clear()
        self._append_user(text)
        # Record for Up/Down recall (skip consecutive duplicates).
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
        self._input_history_idx = -1
        self._set_busy(True)

        # Run agent.chat() in a background thread so the GUI stays responsive.
        def _worker():
            try:
                response = self.agent.chat(text)
            except Exception as exc:
                import traceback
                tb = traceback.format_exc()
                logger.error("Agent worker thread crashed:\n%s", tb)
                response = {"role": "assistant", "content": f"Error: {exc}", "tool_calls": []}
            self._agent_signals.finished.emit(response)

        self._agent_thread = threading.Thread(target=_worker, daemon=True)
        self._agent_thread.start()

    # ------------------------------------------------------------------
    # Voice input (mic button next to the agent input)
    # ------------------------------------------------------------------

    def _on_voice_toggle(self) -> None:
        """Mic button: click to start recording, click again to stop & transcribe."""
        if not self.config.voice.enabled:
            self._append_event("🎤 Voice input is disabled (config → voice.enabled).")
            return
        recorder = self._voice_recorder()
        if recorder.recording:
            rate, channels, sample_format = recorder.format_info()
            pcm = recorder.stop()
            self._reset_voice_button()
            audio = pcm_to_whisper_audio(pcm, rate, channels, sample_format)
            if len(audio) / VOICE_SAMPLE_RATE < MIN_SECONDS:
                self._append_event("🎤 Too short — hold a thought, then click again.")
                return
            self.voice_btn.setEnabled(False)
            self.voice_btn.setText("…")
            self._voice_transcriber().transcribe_async(audio)
            return
        if recorder.start():
            self.voice_btn.setText("⏺")
            self.voice_btn.setStyleSheet("color: #ff5555;")
            self.voice_btn.setToolTip("Recording — click again to stop & transcribe")

    def _voice_recorder(self) -> VoiceRecorder:
        if getattr(self, "_voice_rec", None) is None:
            self._voice_rec = VoiceRecorder(self)
            self._voice_rec.failed.connect(self._on_voice_failed)
        return self._voice_rec

    def _voice_transcriber(self) -> VoiceTranscriber:
        if getattr(self, "_voice_tr", None) is None:
            v = self.config.voice
            self._voice_tr = VoiceTranscriber(v.model, v.language, v.device, self)
            self._voice_tr.status.connect(lambda msg: self._append_event(f"🎤 {msg}"))
            self._voice_tr.done.connect(self._on_voice_done)
            self._voice_tr.failed.connect(self._on_voice_failed)
        return self._voice_tr

    def _reset_voice_button(self) -> None:
        self.voice_btn.setEnabled(True)
        self.voice_btn.setText("🎤")
        self.voice_btn.setStyleSheet("")
        self.voice_btn.setToolTip("Dictate to the agent (local whisper, offline) — click to start, click again to stop & send")

    def _on_voice_done(self, text: str) -> None:
        self._reset_voice_button()
        if not text:
            self._append_event("🎤 Nothing recognized — try again.")
            return
        self.chat_input.setText(text)
        agent_busy = self._agent_thread and self._agent_thread.is_alive()
        if self.config.voice.auto_send and not agent_busy:
            self._on_send()
        else:
            self._append_event("🎤 Transcript left in the input box for review.")

    def _on_voice_failed(self, message: str) -> None:
        self._reset_voice_button()
        self._append_event(f"🎤 Voice input failed: {message}")

    def _on_search_send(self) -> None:
        """Instant web sweep (Ctrl+Enter / Search button) — no agent involved.

        Web results only; torrent results come from the Agent button, which
        runs the full conversational pipeline (its tools include the indexer
        cascade)."""
        text = self.chat_input.text().strip()
        if not text:
            return
        # Search has its own worker thread — it no longer blocks (or is
        # blocked by) the agent. A search already in flight gives feedback
        # instead of silently doing nothing.
        if getattr(self, "_search_thread", None) and self._search_thread.is_alive():
            self._append_search_error("A search is already in progress — please wait.")
            return
        self._search_timed_out_flag = False
        self.chat_input.clear()
        self._append_search_user(text)
        # Record for Up/Down recall (skip consecutive duplicates).
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
        self._input_history_idx = -1
        self._set_search_busy(True)

        # Watchdog: unblock the UI after 60s. Late results are still rendered
        # when they arrive (marked as late), never discarded.
        self._search_timeout_timer = QTimer(self)
        self._search_timeout_timer.setSingleShot(True)
        self._search_timeout_timer.setInterval(60_000)
        self._search_timeout_timer.timeout.connect(self._search_timed_out)
        self._search_timeout_timer.start()

        def _worker():
            try:
                web_results: List[Dict[str, Any]] = []
                answer = ""
                try:
                    dr = self.tools._web_search.search(text, limit=20)
                    answer = dr.get("answer", "")
                    seen_urls = set()
                    for r in dr.get("results", []):
                        u = r.get("url", "")
                        if u and u in seen_urls:
                            continue  # dedupe by URL
                        if u:
                            seen_urls.add(u)
                        if not answer and r.get("answer"):
                            answer = r["answer"]
                        r["_source"] = r.get("source") or "web"
                        web_results.append(r)
                except Exception as exc:
                    logger.warning("Web search failed: %s", exc)

                self._agent_signals.finished.emit({
                    "role": "assistant",
                    "content": "",
                    "_search_results": web_results,
                    "_search_answer": answer,
                })
            except Exception as exc:
                logger.error("Search worker crashed: %s", exc, exc_info=True)
                self._agent_signals.finished.emit({
                    "role": "assistant",
                    "content": f"Search failed: {exc}",
                    "tool_calls": [],
                })

        self._search_thread = threading.Thread(target=_worker, daemon=True)
        self._search_thread.start()

    def _set_busy(self, busy: bool) -> None:
        """Agent working: disable the Agent button and show elapsed time."""
        self._agent_busy = busy
        self.send_btn.setEnabled(not busy)
        if busy:
            self._busy_started = time.time()
            self._busy_timer.start()
        else:
            self._busy_timer.stop()
        self._refresh_input_placeholder()

    def _tick_busy(self) -> None:
        """Update the elapsed-time indicator while the agent is working."""
        self._refresh_input_placeholder()

    def _set_search_busy(self, busy: bool) -> None:
        """Web sweep running: disable the Search button."""
        self._search_busy = busy
        self.search_btn.setEnabled(not busy)
        self._refresh_input_placeholder()

    def _refresh_input_placeholder(self) -> None:
        """Shared input placeholder: agent status wins over search status.

        The input itself stays enabled — the two pipelines run independently,
        only their own send buttons lock."""
        if self._agent_busy:
            elapsed = int(time.time() - self._busy_started)
            self.chat_input.setPlaceholderText(f"Agent is working... ({elapsed}s)")
        elif self._search_busy:
            self.chat_input.setPlaceholderText("Searching...")
        else:
            self.chat_input.setPlaceholderText(self._INPUT_PLACEHOLDER)
            # Don't yank focus while the user is on another tab.
            if self.main_tabs.currentWidget() is self._agents_tab:
                self.chat_input.setFocus()

    # ------------------------------------------------------------------
    # Browser tab
    # ------------------------------------------------------------------

    def _current_browser_view(self) -> QWebEngineView:
        """Return the QWebEngineView of the currently active browser tab."""
        idx = self.browser_tabs.currentIndex()
        if idx < 0:
            # No tabs — create one.
            self._browser_new_tab()
            idx = self.browser_tabs.currentIndex()
        w = self.browser_tabs.widget(idx)
        return w if isinstance(w, QWebEngineView) else QWebEngineView()

    def _browser_new_tab(self, url: Optional[QUrl] = None) -> QWebEngineView:
        """Create a new browser tab and load the given URL (or homepage)."""
        view = QWebEngineView()
        page = _BrowserPage(self.browser_profile, view)
        view.setPage(page)
        view.urlChanged.connect(self._browser_url_changed)
        view.titleChanged.connect(self._browser_title_changed)
        # Loading feedback in the tab title.
        view.loadStarted.connect(lambda v=view: self._browser_set_tab_loading(v, True))
        view.loadFinished.connect(lambda _ok, v=view: self._browser_set_tab_loading(v, False))
        # Add scrollable space at the top so page content can be scrolled
        # below the overlay strip (bookmarks + tabs float on top).
        view.loadFinished.connect(lambda _ok, v=view: v.page().runJavaScript(
            "document.documentElement.style.scrollPaddingTop='70px';"
            "document.documentElement.style.paddingTop='70px';"
        ))
        # Show hovered link URL in the status bar (like a real browser).
        page.linkHovered.connect(
            lambda url: self._hover_label.setText(url if url else ""))
        # When the page requests a new window/tab, create one internally.
        page.newTabRequested.connect(lambda u: self._browser_new_tab(url=u))
        # DOM fullscreen (YouTube ⛶): Chromium only emits the request — the
        # app must accept it and give the view the screen, otherwise the
        # player's fullscreen button silently does nothing.
        page.settings().setAttribute(QWebEngineSettings.FullScreenSupportEnabled, True)
        page.fullScreenRequested.connect(
            lambda req, v=view: self._browser_fullscreen_requested(v, req))
        load_url = url if url and not url.isEmpty() else None
        if load_url is None and self.config.browser.homepage:
            load_url = QUrl(self.config.browser.homepage)
        idx = self.browser_tabs.addTab(view, "New Tab")
        if load_url is not None:
            view.load(load_url)
        else:
            self._browser_show_start_page(view)
        self.browser_tabs.setCurrentIndex(idx)
        self.browser_view = view  # backward-compat reference
        return view

    def _browser_fullscreen_requested(self, view: QWebEngineView, request) -> None:
        """Hand the whole screen to a web view (or take it back).

        The view leaves the tab widget and becomes its own fullscreen
        top-level window; tab bookkeeping handlers all tolerate
        indexOf(view) == -1 while it's detached."""
        request.accept()
        if request.toggleOn():
            idx = self.browser_tabs.indexOf(view)
            if idx < 0 or self._browser_fs_state is not None:
                return  # one fullscreen view at a time
            self._browser_fs_state = (view, idx,
                                      self.browser_tabs.tabText(idx),
                                      self.browser_tabs.tabToolTip(idx))
            view.setParent(None)  # drop out of the tab widget → own window
            view.installEventFilter(self)  # reroute window-close → exit fullscreen
            view.showFullScreen()
            # Chromium doesn't map ESC to exit-fullscreen here; do it ourselves.
            esc = QShortcut(QKeySequence("Esc"), view)
            esc.setContext(Qt.WidgetShortcut)
            esc.activated.connect(
                lambda v=view: v.page().triggerAction(QWebEnginePage.ExitFullScreen))
            self._browser_fs_esc = esc
        else:
            state, self._browser_fs_state = self._browser_fs_state, None
            view.removeEventFilter(self)
            if self._browser_fs_esc is not None:
                self._browser_fs_esc.deleteLater()
                self._browser_fs_esc = None
            if state is not None and state[0] is view:
                _v, idx, text, tip = state
                ins = min(idx, self.browser_tabs.count())
                self.browser_tabs.insertTab(ins, view, text)
                self.browser_tabs.setTabToolTip(ins, tip)
                self.browser_tabs.setCurrentIndex(ins)
                self._rebuild_browser_tab_strip()

    def _browser_close_tab(self, idx: int) -> None:
        """Close a browser tab. Keep at least one tab open."""
        if self.browser_tabs.count() <= 1:
            # Don't close the last tab — reload homepage instead.
            self._browser_go_home()
            return
        w = self.browser_tabs.widget(idx)
        self.browser_tabs.removeTab(idx)
        if w:
            w.deleteLater()
        self._rebuild_browser_tab_strip()

    def _rebuild_browser_tab_strip(self) -> None:
        """Rebuild the floating tab chips — one per open browser tab."""
        layout = self._tab_strip_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        current = self.browser_tabs.currentIndex()
        for i in range(self.browser_tabs.count()):
            chip = QWidget()
            chip.setAttribute(Qt.WA_StyledBackground)
            is_active = i == current
            chip.setStyleSheet(
                f"background: {'#1a2a4a' if is_active else '#0d1117'};"
                f" border: 1px solid {'#2a7abf' if is_active else '#1a2a4a'};"
                " border-radius: 3px;")
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(6, 1, 2, 1)
            cl.setSpacing(2)
            lbl = QLabel(self.browser_tabs.tabText(i))
            lbl.setStyleSheet(
                f"background: transparent; color: {'#2a7abf' if is_active else '#8a9ab0'}; font-size: 11px;")
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.mousePressEvent = lambda _e, idx=i: self.browser_tabs.setCurrentIndex(idx)
            cl.addWidget(lbl)
            if self.browser_tabs.count() > 1:
                x = QPushButton("×")
                x.setFixedSize(14, 14)
                x.setAttribute(Qt.WA_StyledBackground)
                x.setStyleSheet(
                    "QPushButton { background: transparent; color: #ff6666; border: none; font-size: 12px; font-weight: 700; }"
                    "QPushButton:hover { color: #ff3366; background: #3a1a2a; border-radius: 3px; }")
                x.clicked.connect(lambda _c, idx=i: self._browser_close_tab(idx))
                cl.addWidget(x)
            layout.addWidget(chip)
        layout.addStretch()
        self._browser_overlay.raise_()

    def _browser_tab_changed(self, idx: int) -> None:
        """When the active tab changes, update the URL bar and back-compat ref."""
        if idx < 0:
            return
        w = self.browser_tabs.widget(idx)
        if isinstance(w, QWebEngineView):
            self.browser_view = w
            self.browser_url_bar.setText(self._display_url(w.url().toString()))
            self._rebuild_browser_tab_strip()

    def _browser_title_changed(self, title: str) -> None:
        """Update the tab title when the page title changes."""
        view = self.sender()
        if not isinstance(view, QWebEngineView):
            return
        idx = self.browser_tabs.indexOf(view)
        if idx >= 0:
            self.browser_tabs.setTabText(idx, title[:25] if title else "New Tab")
            self.browser_tabs.setTabToolTip(idx, title)
            self._rebuild_browser_tab_strip()

    def _browser_set_tab_loading(self, view: QWebEngineView, loading: bool) -> None:
        idx = self.browser_tabs.indexOf(view)
        if idx < 0:
            return
        if loading:
            self.browser_tabs.setTabText(idx, "Loading…")
        elif self.browser_tabs.tabText(idx) == "Loading…":
            title = view.title() or "New Tab"
            self.browser_tabs.setTabText(idx, title[:25])
            self.browser_tabs.setTabToolTip(idx, title)
        self._rebuild_browser_tab_strip()

    def _browser_navigate(self) -> None:
        """Navigate to the URL in the browser address bar."""
        text = self.browser_url_bar.text().strip()
        if not text:
            return
        # If it looks like a URL, load it directly; otherwise search Google.
        if "." in text and " " not in text:
            if not text.startswith(("http://", "https://")):
                text = "https://" + text
            self._current_browser_view().load(QUrl(text))
        else:
            self._current_browser_view().load(QUrl(f"https://www.google.com/search?q={text}"))

    @staticmethod
    def _display_url(url_str: str) -> str:
        """Address-bar text for a page URL — blank for internal pages
        (about:blank, or the data: URL that setHtml generates)."""
        return "" if url_str in ("about:blank", "") or url_str.startswith("data:") else url_str

    def _browser_url_changed(self, url) -> None:
        """Update the address bar when the page URL changes (current tab only)."""
        # Only update if the signal came from the active tab.
        view = self.sender()
        if isinstance(view, QWebEngineView):
            idx = self.browser_tabs.indexOf(view)
            if idx == self.browser_tabs.currentIndex():
                self.browser_url_bar.setText(self._display_url(url.toString()))

    def _browser_go_home(self) -> None:
        """Navigate to the configured homepage (or the built-in start page)."""
        if self.config.browser.homepage:
            self._current_browser_view().load(QUrl(self.config.browser.homepage))
        else:
            self._browser_show_start_page(self._current_browser_view())

    def _browser_show_start_page(self, view) -> None:
        """Render the built-in start page: DeepFlux logo centered on black,
        no network load. The about:blank base URL keeps the giant data: URL
        out of the page's address (and thus the address bar)."""
        view.setHtml(self._start_page_html(), QUrl("about:blank"))

    def _start_page_html(self) -> str:
        """Start page HTML with the logo embedded as a data URI (cached)."""
        cached = getattr(self, "_start_page_cache", None)
        if cached:
            return cached
        img_tag = ""
        logo_path = self._resolve_watermark_path()
        if logo_path:
            import base64
            try:
                with open(logo_path, "rb") as f:
                    data = base64.b64encode(f.read()).decode("ascii")
                img_tag = (f'<img id="df-logo" src="data:image/png;base64,{data}" alt="DeepFlux" '
                           f'style="max-width:60%; max-height:60vh;">')
            except OSError:
                pass
        # Static logo (rotation removed). Clock badge mirrors the extension's
        # status badge styling (dlmgr/browser_extension.py createStatusBadge).
        html = (
            "<html><head><title>DeepFlux</title><style>"
            "#df-clock { position:fixed; top:10px; left:10px; "
            "background:linear-gradient(135deg, #011d3e, #001431); color:#2a7abf; "
            "border:1px solid #2a7abf; border-radius:5px; padding:4px 7px; "
            "font-family:'Segoe UI', Arial, sans-serif; font-size:7px; font-weight:600; "
            "box-shadow:0 2px 6px rgba(42, 122, 191, 0.3); user-select:none; "
            "display:flex; align-items:center; gap:4px; }"
            "#df-clock .dot { display:inline-block; width:5px; height:5px; "
            "border-radius:50%; flex-shrink:0; background:#2a7abf; "
            "box-shadow:0 0 3px #2a7abf; }"
            "</style></head>"
            "<body style='background:#000; margin:0; height:100vh; display:flex; "
            "align-items:center; justify-content:center; overflow:hidden;'>"
            "<div id='df-clock'><span class='dot'></span><span id='df-clock-text'></span></div>"
            f"{img_tag}"
            "<script>"
            "(function(){"
            "var el=document.getElementById('df-clock-text');"
            "function tick(){el.textContent=new Date().toLocaleTimeString();}"
            "tick();setInterval(tick,1000);"
            "})();"
            "</script></body></html>"
        )
        self._start_page_cache = html
        return html

    def _browser_add_bookmark(self) -> None:
        """Bookmark the current page."""
        url = self.browser_url_bar.text().strip()
        if not url:
            return
        # Derive a title from the current page title or the URL.
        title = self._current_browser_view().title() or url
        # Check if already bookmarked.
        for b in self.config.browser.bookmarks:
            if b.url == url:
                QMessageBox.information(self, "Bookmark", "This page is already bookmarked.")
                return
        from config import Bookmark
        self.config.browser.bookmarks.append(Bookmark(title=title, url=url))
        self._save_config()
        self._rebuild_bookmarks_bar()

    def _browser_open_bookmark(self, url: str) -> None:
        """Open a bookmarked URL in the current tab."""
        self._current_browser_view().load(QUrl(url))

    def _browser_open_internal(self, url_str: str) -> None:
        """Open a URL in the internal browser (new tab) and switch to it.

        Used by agent chat links and search result links."""
        if not url_str:
            return
        # Switch to the Web tab.
        self.main_tabs.setCurrentWidget(self._browser_tab)
        self._browser_new_tab(url=QUrl(url_str))

    def _toggle_adblock(self) -> None:
        """Toggle ad-blocking on/off."""
        enabled = self.browser_adblock_btn.isChecked()
        self.adblock_interceptor.set_enabled(enabled)
        self.config.browser.adblock_enabled = enabled
        self._save_config()
        self._update_adblock_button_style()

    def _update_adblock_button_style(self) -> None:
        """Update the AdBlock button appearance based on its state."""
        if self.browser_adblock_btn.isChecked():
            self.browser_adblock_btn.setStyleSheet(
                "QPushButton { background: #00ff9d; color: #001431; font-weight: 600; }"
            )
        else:
            self.browser_adblock_btn.setStyleSheet("")

    def _browser_remove_bookmark(self, url: str) -> None:
        """Remove a bookmark."""
        self.config.browser.bookmarks = [b for b in self.config.browser.bookmarks if b.url != url]
        self._save_config()
        self._rebuild_bookmarks_bar()

    def _import_bookmarks(self) -> None:
        """Import bookmarks from another browser (File menu + Browser Settings)."""
        from gui.settings_dialog import BookmarkImportDialog
        from dlmgr.bookmarks_import import merge_bookmarks

        dialog = BookmarkImportDialog(self)
        if dialog.exec() == QDialog.Accepted and dialog.imported:
            added, updated = merge_bookmarks(self.config.browser.bookmarks, dialog.imported)
            self._save_config()
            self._rebuild_bookmarks_bar()
            QMessageBox.information(
                self,
                "Import Bookmarks",
                f"Imported {added} new bookmark(s); organized {updated} existing one(s) into folders.",
            )

    def _update_bookmarks_menu_visibility(self, idx: int) -> None:
        """Show the Bookmarks menu only when the Browse tab is active."""
        self._bookmarks_menu.menuAction().setVisible(idx == 0)

    def _rebuild_bookmarks_bar(self) -> None:
        """Rebuild the Bookmarks menu from config.

        Folders become submenus; plain bookmarks are actions that open the URL.
        """
        menu = self._bookmarks_menu
        menu.clear()

        # Build the folder tree: node = {"folders": {name: node}, "items": [Bookmark]}.
        tree: dict = {"folders": {}, "items": []}
        entries: list = []
        seen_folders: set = set()
        for b in self.config.browser.bookmarks:
            if not b.folder:
                entries.append(("bm", b))
                continue
            node = tree
            for part in b.folder.split("/"):
                node = node["folders"].setdefault(part, {"folders": {}, "items": []})
            node["items"].append(b)
            top = b.folder.split("/")[0]
            if top not in seen_folders:
                seen_folders.add(top)
                entries.append(("folder", top, tree["folders"][top]))

        for entry in entries:
            if entry[0] == "bm":
                b = entry[1]
                action = menu.addAction(b.title[:40] or b.url)
                action.setToolTip(b.url)
                action.triggered.connect(lambda _c, u=b.url: self._browser_open_bookmark(u))
            else:
                name, node = entry[1], entry[2]
                submenu = menu.addMenu(name)
                self._fill_bookmark_menu(submenu, node)

        menu.setEnabled(bool(self.config.browser.bookmarks))

    def _fill_bookmark_menu(self, menu: QMenu, node: dict) -> None:
        """Fill a folder menu with subfolder submenus and bookmark rows."""
        for name, sub in node["folders"].items():
            submenu = menu.addMenu(name)
            self._fill_bookmark_menu(submenu, sub)
        for b in node["items"]:
            self._add_bookmark_menu_item(menu, b)

    def _add_bookmark_menu_item(self, menu: QMenu, b) -> None:
        """Add a bookmark row (open + remove) to a folder menu.

        QMenu swallows right-clicks on plain actions, so each row is a
        QWidgetAction: a title button that opens the URL and a small x
        button that removes the bookmark — same capabilities as the bar.
        """
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(6, 0, 4, 0)
        lay.setSpacing(2)

        title = b.title[:40] or b.url
        open_btn = QPushButton(title)
        open_btn.setFlat(True)
        open_btn.setToolTip(b.url)
        open_btn.setStyleSheet("text-align: left; border: none; padding: 4px;")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(
            lambda checked, u=b.url, m=menu: (m.close(), self._browser_open_bookmark(u))
        )
        lay.addWidget(open_btn, 1)

        remove_btn = QToolButton()
        remove_btn.setText("✕")
        remove_btn.setToolTip("Remove bookmark")
        remove_btn.setFixedSize(20, 20)
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.clicked.connect(
            lambda checked, u=b.url, m=menu: (m.close(), self._browser_remove_bookmark(u))
        )
        lay.addWidget(remove_btn)

        action = QWidgetAction(menu)
        action.setDefaultWidget(row)
        menu.addAction(action)

    def _bookmark_folder_context_menu(self, folder: str, btn) -> None:
        """Right-click menu for a folder button: remove the whole folder."""
        menu = QMenu(self)
        remove_action = menu.addAction(f"Remove folder '{folder}' and its bookmarks")
        action = menu.exec(btn.mapToGlobal(btn.rect().center()))
        if action == remove_action:
            prefix = folder + "/"
            self.config.browser.bookmarks = [
                b for b in self.config.browser.bookmarks
                if b.folder != folder and not b.folder.startswith(prefix)
            ]
            self._save_config()
            self._rebuild_bookmarks_bar()

    def _bookmark_context_menu(self, url: str, btn) -> None:
        """Show a context menu to remove a bookmark."""
        menu = QMenu(self)
        remove_action = menu.addAction("Remove bookmark")
        action = menu.exec(btn.mapToGlobal(btn.rect().center()))
        if action == remove_action:
            self._browser_remove_bookmark(url)

    def _save_config(self) -> None:
        """Save the current config to disk."""
        try:
            self.config.to_file(self.config_path)
        except Exception as exc:
            logger.warning("Failed to save config: %s", exc)

    # ------------------------------------------------------------------
    # Jackett auto-start + source sync (startup, then hourly)
    # ------------------------------------------------------------------

    def _start_jackett_sync(self, force: bool = False) -> None:
        """Kick off the background Jackett check (no-op while one is running).

        force=True bypasses the daily gate — used when the user just saved
        Jackett settings and expects the sources list to refresh now."""
        if self._jackett_sync_thread and self._jackett_sync_thread.is_alive():
            return
        self._jackett_sync_thread = threading.Thread(
            target=self._jackett_sync_worker, args=(force,), daemon=True)
        self._jackett_sync_thread.start()

    def _jackett_sync_worker(self, force: bool = False) -> None:
        from infra import jackett
        try:
            result = jackett.maybe_auto_sync(self.config, self.config_path, force=force)
        except Exception as exc:
            logger.debug("Jackett auto-sync failed: %s", exc, exc_info=True)
            return
        if result:
            self._agent_signals.event.emit({"type": "jackett_sync", **result})

    # ------------------------------------------------------------------
    # Tool event formatting helpers
    # ------------------------------------------------------------------

    # Friendly labels and icons for each tool name.
    _TOOL_LABELS = {
        "web_search": ("🔍", "Searching the web"),
        "web_fetch": ("🌐", "Fetching web page"),
        "search_indexers": ("🔍", "Searching indexers"),
        "find_alt_trackers": ("🔎", "Finding alternative trackers"),
        "find_alt_release": ("🔎", "Finding alternative releases"),
        "refresh_tracker_list": ("📋", "Refreshing tracker list"),
        "list_torrents": ("📋", "Listing torrents"),
        "get_torrent_status": ("📊", "Getting torrent status"),
        "diagnose_swarm": ("🩺", "Diagnosing swarm"),
        "get_swarm_stats": ("📊", "Getting swarm stats"),
        "add_magnet": ("🧲", "Adding magnet"),
        "add_torrent_file": ("📁", "Adding torrent file"),
        "add_download": ("⬇", "Starting download"),
        "list_downloads": ("📋", "Listing downloads"),
        "pause_download": ("⏸", "Pausing download"),
        "resume_download": ("▶", "Resuming download"),
        "retry_download": ("↻", "Retrying download"),
        "cancel_download": ("✖", "Cancelling download"),
        "remove_download": ("🗑", "Removing download entry"),
        "set_torrent_rate_limits": ("🚦", "Setting torrent rate limits"),
        "set_sequential_download": ("⏩", "Toggling sequential download"),
        "force_recheck": ("🔍", "Re-checking torrent files"),
        "force_reannounce": ("📡", "Reannouncing to trackers"),
        "add_rss_feed": ("📡", "Adding RSS feed"),
        "remove_rss_feed": ("🗑", "Removing RSS feed"),
        "pause_torrent": ("⏸", "Pausing torrent"),
        "resume_torrent": ("▶", "Resuming torrent"),
        "remove_torrent": ("🗑", "Removing torrent"),
        "set_file_priority": ("⚙", "Setting file priority"),
        "add_tracker": ("➕", "Adding tracker"),
        "propose_rename_and_category": ("✏", "Proposing rename"),
        "save_memory": ("🧠", "Saving memory"),
        "search_memory": ("🧠", "Searching memory"),
        "irc_status": ("📡", "Checking IRC status"),
        "irc_list_messages": ("💬", "Reading IRC channel"),
        "irc_search_messages": ("🔎", "Searching IRC buffers"),
        "irc_send_message": ("📨", "Sending IRC message"),
        "irc_join": ("➡", "Joining IRC channel"),
        "irc_part": ("⬅", "Leaving IRC channel"),
        "irc_connect": ("🔌", "Connecting to IRC"),
        "irc_disconnect": ("🔌", "Disconnecting from IRC"),
        "irc_send_action": ("📨", "Sending IRC action"),
        "irc_send_notice": ("📨", "Sending IRC notice"),
        "irc_set_nick": ("✏", "Changing IRC nick"),
        "irc_send_raw": ("⌨", "Sending raw IRC line"),
        "irc_list_channels": ("📋", "Listing IRC channels"),
        "irc_list_nicks": ("👥", "Listing channel users"),
        "browser_list_tabs": ("🗂", "Listing browser tabs"),
        "browser_navigate": ("🌐", "Navigating browser"),
        "browser_close_tab": ("✖", "Closing browser tab"),
        "browser_switch_tab": ("↹", "Switching browser tab"),
        "browser_go": ("↶", "Browser navigation"),
        "browser_get_content": ("📄", "Reading browser page"),
        "browser_click": ("🖱", "Clicking page element"),
        "browser_fill": ("⌨", "Filling form field"),
        "browser_scroll": ("↕", "Scrolling page"),
        "browser_add_bookmark": ("🔖", "Adding bookmark"),
        "browser_remove_bookmark": ("🔖", "Removing bookmark"),
        "browser_list_bookmarks": ("🔖", "Listing bookmarks"),
        "list_directory": ("📂", "Listing directory"),
        "create_folder": ("📁", "Creating folder"),
        "copy_path": ("📄", "Copying"),
        "move_path": ("📦", "Moving"),
        "rename_path": ("✏", "Renaming"),
        "delete_path": ("🗑", "Deleting"),
        "iptv_search": ("📺", "Searching IPTV playlist"),
        "iptv_list": ("📺", "Listing IPTV content"),
        "iptv_epg": ("🗓", "Checking TV guide"),
        "iptv_now_playing": ("🎬", "Checking player state"),
        "iptv_find_subtitles": ("💬", "Searching OpenSubtitles"),
        "iptv_load_subtitle": ("💬", "Loading subtitle"),
        "iptv_play": ("▶", "Starting playback"),
        "iptv_pause": ("⏸", "Toggling pause"),
        "iptv_stop": ("⏹", "Stopping playback"),
        "iptv_set_volume": ("🔊", "Setting volume"),
    }

    def _format_tool_args(self, tool: str, args: dict) -> str:
        """Human-readable summary of what a tool is doing, not raw JSON."""
        if tool == "web_search":
            return f"for \"{args.get('query', '')}\""
        if tool == "web_fetch":
            return f"<a href=\"{args.get('url', '')}\">{args.get('url', '')}</a>"
        if tool in ("search_indexers",):
            return f"for \"{args.get('query', '')}\""
        if tool in ("find_alt_trackers", "find_alt_release"):
            return f"for \"{args.get('torrent_name', '')}\""
        if tool == "search_memory":
            return f"for \"{args.get('query', '')}\""
        if tool == "save_memory":
            return f"\"{str(args.get('content', ''))[:60]}\""
        if tool in ("add_magnet",):
            uri = args.get("uri", "")
            short = uri[:60] + "..." if len(uri) > 60 else uri
            return f"magnet: {short}"
        if tool in ("add_torrent_file",):
            return f"file: {args.get('path', '')}"
        if tool in ("diagnose_swarm", "get_torrent_status", "get_swarm_stats",
                     "pause_torrent", "resume_torrent", "remove_torrent"):
            return f"hash: {args.get('info_hash', '')[:12]}..."
        if tool == "add_tracker":
            return f"tracker: {args.get('url', '')}"
        if tool in ("list_directory", "create_folder", "delete_path"):
            return args.get("path", "")
        if tool in ("copy_path", "move_path"):
            return f"{args.get('source', '')} → {args.get('destination', '')}"
        if tool == "rename_path":
            return f"{args.get('path', '')} → {args.get('new_name', '')}"
        if tool in ("iptv_search", "iptv_epg"):
            return f"for \"{args.get('query', '') or args.get('channel', '')}\""
        if tool == "iptv_play":
            return f"\"{args.get('query', '') or args.get('title', '') or args.get('url', '') or args.get('file', '') or args.get('item_id', '')}\""
        if tool == "iptv_set_volume":
            return f"to {args.get('level', '')}"
        if tool in ("irc_send_action", "irc_send_notice"):
            return f"to {args.get('target', '')}: \"{str(args.get('text', ''))[:60]}\""
        if tool in ("irc_connect", "irc_disconnect"):
            return args.get("network", "") or "(auto)"
        if tool == "irc_set_nick":
            return f"to {args.get('new_nick', '')}"
        if tool == "irc_send_raw":
            return f"\"{str(args.get('line', ''))[:60]}\""
        if tool == "irc_list_channels":
            return args.get("filter", "")
        if tool == "irc_list_nicks":
            return args.get("channel", "")
        if tool == "browser_navigate":
            return f"to {args.get('url', '')}"
        if tool in ("browser_close_tab", "browser_switch_tab"):
            return f"tab {args.get('index', '')}"
        if tool == "browser_go":
            return args.get("action", "")
        if tool == "browser_click":
            return args.get("selector", "") or f"\"{args.get('text', '')}\""
        if tool == "browser_fill":
            return f"{args.get('selector', '')} = \"{str(args.get('value', ''))[:40]}\""
        if tool == "browser_scroll":
            return args.get("direction", "down")
        if tool in ("browser_add_bookmark", "browser_remove_bookmark"):
            return args.get("url", "") or "(current page)"
        if tool in ("pause_download", "resume_download", "retry_download",
                    "cancel_download", "remove_download"):
            return f"job {args.get('job_id', '')}"
        if tool in ("force_recheck", "force_reannounce", "set_sequential_download"):
            return f"hash: {args.get('info_hash', '')[:12]}..."
        if tool == "set_torrent_rate_limits":
            return f"↓{args.get('download_kb', 0)} ↑{args.get('upload_kb', 0)} KB/s"
        if tool in ("add_rss_feed", "remove_rss_feed"):
            return args.get("url", "")
        # Generic fallback.
        parts = [f"{k}={v}" for k, v in args.items() if k not in ("save_path",)]
        return ", ".join(parts[:3]) if parts else ""

    def _on_agent_event(self, event: dict) -> None:
        """Handle a live progress event from the agent (called on GUI thread)."""
        try:
            etype = event.get("type", "")
            if etype == "thinking":
                turn = event.get("turn", "?")
                self._append_event(f"🧠 Thinking... (step {turn})")
            elif etype == "stream_delta":
                self._on_stream_delta(event.get("kind", "content"), event.get("text", ""))
            elif etype == "model_reasoning":
                # Complete thinking trace (non-streamed); capped unless debug is on.
                content = (event.get("content") or "").strip()
                if content:
                    if not self.config.ui_agent_debug and len(content) > 600:
                        content = content[:600] + "…"
                    safe = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
                    self._append_event(f"🧠 {safe}")
            elif etype == "reasoning":
                content = event.get("content", "")
                if content:
                    self._append_event(f"💭 {content}")
            elif etype == "tool_start":
                # A new tool turn begins — seal any streamed text so it isn't
                # replaced when the final answer arrives.
                self._seal_stream()
                tool = event.get("tool", "?")
                args = event.get("args", {})
                icon, label = self._TOOL_LABELS.get(tool, ("🔧", tool))
                arg_str = self._format_tool_args(tool, args)
                self._append_event(f"{icon} {label} — {arg_str}" if arg_str else f"{icon} {label}")
                if self.config.ui_agent_debug and args:
                    self._append_debug(args)
            elif etype == "tool_progress":
                message = event.get("message", "")
                if message:
                    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    self._append_event(f"&nbsp;&nbsp;· {safe}")
            elif etype == "tool_end":
                tool = event.get("tool", "?")
                icon, label = self._TOOL_LABELS.get(tool, ("🔧", tool))
                summary = event.get("summary", "")
                if summary:
                    safe = summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    self._append_event(f"  ✅ {label} done — {safe}")
                else:
                    self._append_event(f"  ✅ {label} done")
                if self.config.ui_agent_debug and event.get("result") is not None:
                    self._append_debug(event["result"])
                # Surface the Download tab when the agent queued a transfer,
                # so the user sees where it went.
                result = event.get("result")
                if (tool in ("add_download", "add_magnet", "add_torrent_file")
                        and isinstance(result, dict) and result.get("success")):
                    self.main_tabs.setCurrentWidget(self._torrents_tab)
            elif etype == "watchdog":
                message = event.get("message", "")
                if message:
                    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    self._append_event(f"🩺 {safe}")
            elif etype == "error":
                self._append_error(event.get("message", "Unknown error"))
            elif etype == "pending_confirmation":
                tools = event.get("tools", [])
                reasoning = event.get("reasoning", "")
                self._append_pending(f"⏳ Waiting for confirmation: {', '.join(tools)}")
                if reasoning:
                    self._append_event(f"💭 {reasoning}")
            elif etype == "rss_auto_download":
                feed = event.get("feed", "")
                count = event.get("count", 0)
                self._append_agent(f"📡 **RSS auto-download:** {count} new item(s) from '{feed}' added to downloads.")
                self._refresh()
            elif etype == "rss_monitor":
                feed = event.get("feed", "")
                new_items = event.get("new_items", 0)
                self._append_event(f"📡 RSS monitor: {new_items} new item(s) in '{feed}' (monitor mode, not downloaded)")
            elif etype == "jackett_sync":
                if not event.get("reachable", True):
                    # The hourly timer re-checks — show the failure once per session.
                    if self._jackett_notice_shown:
                        return
                    self._jackett_notice_shown = True
                    self._append_event("🧩 Jackett is configured but not reachable — auto-start failed. "
                                       "Searches fall back to web sources.")
                    return
                if event.get("sync_failed") and not event.get("changed"):
                    self._append_event("🧩 Jackett answered but no sources were synced — check the API key, "
                                       "or that indexers are configured in Jackett.")
                    return
                parts = []
                if event.get("started"):
                    parts.append("Jackett was not running — started it")
                if event.get("changed"):
                    total, enabled = event.get("total", 0), event.get("enabled", 0)
                    if event.get("bootstrap"):
                        parts.append(f"fetched {total} indexer(s) from Jackett and enabled them")
                    else:
                        parts.append(f"fetched {total} indexer(s) from Jackett ({enabled} enabled)")
                if parts:
                    self._append_event("🧩 " + "; ".join(parts) + ".")
        except Exception:
            logger.debug("event handler failed", exc_info=True)

    def _on_stream_delta(self, kind: str, text: str) -> None:
        """Render one streamed token chunk (reasoning dimmed, content plain)."""
        if not text:
            return
        safe = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>"))
        # insertHtml parses each fragment as standalone HTML and strips its
        # leading whitespace — streamed tokens like " user" would glue onto the
        # previous word ("Theuser..."). Route edge spaces through insertText,
        # which is literal. (Trailing whitespace is preserved by insertHtml.)
        lead_m = re.match(r" +", safe)
        lead = lead_m.group(0) if lead_m else ""
        if lead_m:
            safe = safe[lead_m.end():]
        if kind == "reasoning":
            if not self._stream_reasoning_started:
                self._stream_append('<span style="color:#8a7aaa; font-size:12px">🧠 ')
                self._stream_reasoning_started = True
            if lead:
                self._stream_insert_text(lead)
            if safe:
                self._stream_append(f'<span style="color:#8a7aaa; font-size:12px">{safe}</span>')
            return
        if self._stream_reasoning_started and self._stream_content_start is None:
            self._stream_append("<br><br>")  # separate thinking from the answer
        if self._stream_content_start is None:
            cursor = self.chat_history.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self._stream_content_start = cursor.position()
        if lead:
            self._stream_insert_text(lead)
        if safe:
            self._stream_append(f"<span>{safe}</span>")

    def _stream_insert_text(self, text: str) -> None:
        """Insert literal (non-HTML) text at the document end — whitespace is
        preserved exactly, unlike insertHtml fragments."""
        cursor = self.chat_history.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.setCharFormat(QTextCharFormat())
        cursor.insertText(text)
        self.chat_history.setTextCursor(cursor)

    def _stream_append(self, html_fragment: str) -> None:
        """Append a raw HTML fragment at the document end (no band painting —
        the logo is already hidden once a conversation is underway)."""
        html_fragment = _break_long_tokens(html_fragment)
        cursor = self.chat_history.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(html_fragment)
        self.chat_history.setTextCursor(cursor)
        self.chat_history.ensureCursorVisible()

    def _seal_stream(self) -> None:
        """Keep the currently streamed text as-is (a new tool turn follows)."""
        if self._stream_content_start is not None:
            self._stream_append("<br>")
            self._stream_content_start = None
            # Next turn's reasoning re-earns its 🧠 prefix after a seal.
            self._stream_reasoning_started = False

    def _finalize_stream(self) -> bool:
        """Drop the raw streamed reply text; returns True if a stream was live."""
        if self._stream_content_start is None:
            self._stream_reasoning_started = False
            return False
        cursor = self.chat_history.textCursor()
        cursor.setPosition(self._stream_content_start)
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self._stream_content_start = None
        self._stream_reasoning_started = False
        return True

    def _search_timed_out(self) -> None:
        """Hard timeout for direct searches — unblock the Search tab UI.

        Results arriving later are still rendered (marked as late)."""
        logger.warning("Search timed out (30s)")
        self._search_timed_out_flag = True
        self._append_search_error("Search is taking longer than 30s — the network may be slow. Results will appear here if they arrive.")
        self._set_search_busy(False)

    def _on_chat_link_clicked(self, url) -> None:
        """Handle clicks on links in the chat — magnet links are added directly,
        other URLs open in the internal browser."""
        url_str = url.toString() if hasattr(url, "toString") else str(url)
        if url_str.startswith("magnet:"):
            try:
                result = self.tools.call("add_magnet", {
                    "uri": url_str,
                    "save_path": self.config.default_save_path,
                    "category": "Other",
                })
                self._append_agent(f"**Added from search result.**\n\n- **Info hash:** `{result.get('info_hash', '')}`\n- **Save path:** `{result.get('save_path', '')}`")
                self.main_tabs.setCurrentWidget(self._torrents_tab)
            except Exception as exc:
                self._append_error(f"Failed to add magnet: {exc}")
            self._refresh()
        elif self._is_torrent_download_url(url_str):
            # .torrent download links (e.g. Jackett-proxied private tracker URLs).
            try:
                result = self.tools._add_torrent_from_url(url_str, self.config.default_save_path, "Other")
                self._append_agent(f"**Added from search result.**\n\n- **Info hash:** `{result.get('info_hash', '')}`\n- **Save path:** `{result.get('save_path', '')}`")
                self.main_tabs.setCurrentWidget(self._torrents_tab)
            except Exception as exc:
                self._append_error(f"Failed to add torrent: {exc}")
            self._refresh()
        else:
            # Open regular URLs in the internal browser (new tab).
            self._browser_open_internal(url_str)

    # -- browser downloads ----------------------------------------------------
    def _on_browser_cookie_added(self, cookie) -> None:
        """Cache browser cookies (domain -> {name: value}) for dlmgr jobs."""
        try:
            domain = cookie.domain().lstrip(".")
            if domain:
                jar = self._browser_cookies.setdefault(domain, {})
                jar[bytes(cookie.name()).decode("utf-8", "ignore")] = bytes(cookie.value()).decode("utf-8", "ignore")
        except Exception:
            pass

    def _browser_cookies_for(self, url: str) -> str:
        """Build a Cookie header for url from the browser session cache."""
        from urllib.parse import urlparse
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            return ""
        parts: List[str] = []
        for domain, jar in self._browser_cookies.items():
            if host == domain or host.endswith("." + domain):
                parts.extend(f"{k}={v}" for k, v in jar.items())
        return "; ".join(parts)

    def _on_browser_download_requested(self, item) -> None:
        """Route browser downloads: .torrent files keep QtWebEngine's own
        downloader (small files, and the profile's session cookies apply —
        needed on private trackers); everything else is handed to the
        internal segmented download manager with a notification."""
        try:
            url = item.url().toString()
            name = item.downloadFileName() or "download"
        except Exception:
            return
        is_torrent = name.lower().endswith(".torrent") or url.split("?")[0].lower().endswith(".torrent")
        if not is_torrent:
            self._route_browser_download(item, url, name)
            return
        # Stash .torrent files in a temp folder; they're added to the
        # engine (and persisted) on completion, not kept as user files.
        import tempfile
        dl_dir = os.path.join(tempfile.gettempdir(), "deepflux_torrents")
        try:
            os.makedirs(dl_dir, exist_ok=True)
            item.setDownloadDirectory(dl_dir)
            item.stateChanged.connect(lambda _st, it=item: self._on_browser_download_state(it, True))
            item.accept()
            self._append_event(f"⬇ Browser download started: {name}")
        except Exception as exc:
            logger.warning("Browser download failed to start: %s", exc)

    def _route_browser_download(self, item, url: str, name: str) -> None:
        """Hand a browser file download to the internal segmented download
        manager (dlmgr) instead of QtWebEngine's silent downloader, with the
        browser session's cookies/referrer, and notify the user."""
        try:
            item.cancel()  # dismiss QtWebEngine's own download
        except Exception:
            pass
        try:
            referrer = ""
            try:
                page = item.page()
                if page is not None:
                    referrer = page.url().toString()
            except Exception:
                pass
            low = url.lower().split("?", 1)[0]
            cookies = self._browser_cookies_for(url)
            if low.endswith((".m3u8", ".mpd")):
                job = self._dl_engine.add_stream_job(
                    url=url, filename=name,
                    cookies=cookies, referrer=referrer, source_url=referrer,
                )
            else:
                job = self._dl_engine.add_job(
                    url=url, filename=name,
                    cookies=cookies, referrer=referrer, source_url=referrer,
                )
            self._append_event(f"⬇ Download started: {name} → Download Manager")
            self._notify("Download started", name, path=job.save_path)
            # Surface the Download tab so the user sees where the file went.
            self.main_tabs.setCurrentWidget(self._torrents_tab)
        except Exception as exc:
            logger.warning("Browser download failed to start: %s", exc)
            self._append_error(f"Download failed to start: {name} ({exc})")

    def _on_browser_download_state(self, item, is_torrent: bool) -> None:
        """React to a browser download finishing (QtWebEngine downloads are
        .torrent files only; everything else goes through the dlmgr)."""
        try:
            state = item.state()
            done = state == item.DownloadState.DownloadCompleted
            failed = state in (item.DownloadState.DownloadCancelled, item.DownloadState.DownloadInterrupted)
        except Exception:
            return
        if failed:
            self._append_error(f"Browser download failed: {item.downloadFileName()}")
            return
        if not done:
            return
        path = os.path.join(item.downloadDirectory(), item.downloadFileName())
        # .torrent downloaded — add it to the torrent engine automatically.
        try:
            result = self.tools.call("add_torrent_file", {
                "path": path,
                "save_path": self.config.default_save_path,
                "category": "Other",
            })
            info_hash = result.get("info_hash", "")
            if info_hash:
                self._state_manager.store_torrent_file(path, info_hash)
            self._append_agent(
                f"**Torrent added from browser download.**\n\n"
                f"- **File:** `{item.downloadFileName()}`\n"
                f"- **Info hash:** `{info_hash}`\n"
                f"- **Save path:** `{self.config.default_save_path}`"
            )
            # Surface the Agent tab so the new torrent is visible.
            self.main_tabs.setCurrentWidget(self._torrents_tab)
            self._refresh()
        except Exception as exc:
            self._append_error(f"Downloaded torrent could not be added: {exc}")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _register_file_associations(self) -> None:
        """Register DeepFlux for torrent/magnet/media/html files (per-user)."""
        try:
            from infra.file_associations import register_associations
            ok = register_associations()
        except Exception as exc:
            ok = False
            logger.exception("association registration failed: %s", exc)
        if ok:
            QMessageBox.information(
                self, "Default App",
                "DeepFlux registered for:\n\n"
                "  •  .torrent files and magnet: links (set as default)\n"
                "  •  Video & audio files (available in 'Open with' / Default Apps)\n"
                "  •  .html / web documents (available in 'Open with' / Default Apps)\n\n"
                "Windows only lets you choose defaults for media/web files yourself:\n"
                "Settings → Apps → Default apps → DeepFlux.",
            )
        else:
            QMessageBox.warning(self, "Default App", "Registration failed — see the log for details.")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Enforce default split ratios once the splitters have real
        # dimensions — pre-show setSizes() gets overridden by content size hints.
        if not self._agent_ratio_applied:
            self._agent_ratio_applied = True
            # Downloads tab: 50% torrents / 50% downloads.
            h = self.agent_splitter.height()
            if h > 0:
                self.agent_splitter.setSizes([h // 2, h - h // 2])

    def open_target(self, target: str) -> None:
        """Open a file or URI passed via file association / second instance.

        Dispatch: magnet → torrent engine; .torrent file → torrent engine;
        video/audio file → Player tab; html/http(s) → Browser tab."""
        target = target.strip().strip('"')
        if not target:
            return
        # Bring the window to the front — the user just "opened" something.
        self.showNormal()
        self.raise_()
        self.activateWindow()
        try:
            if target.startswith("magnet:"):
                result = self.tools.call("add_magnet", {
                    "uri": target, "save_path": self.config.default_save_path, "category": "Other",
                })
                self._append_agent(f"**Magnet added.**\n\n- **Info hash:** `{result.get('info_hash', '')}`")
                self.main_tabs.setCurrentWidget(self._torrents_tab)
                self._refresh()
            elif target.lower().startswith(("http://", "https://")):
                self._browser_open_internal(target)
            elif os.path.isfile(target):
                low = target.lower()
                if low.endswith(".torrent"):
                    result = self.tools.call("add_torrent_file", {
                        "path": target, "save_path": self.config.default_save_path, "category": "Other",
                    })
                    info_hash = result.get("info_hash", "")
                    if info_hash:
                        self._state_manager.store_torrent_file(target, info_hash)
                    self._append_agent(
                        f"**Torrent added.**\n\n- **File:** `{os.path.basename(target)}`\n"
                        f"- **Info hash:** `{info_hash}`\n- **Save path:** `{self.config.default_save_path}`"
                    )
                    self.main_tabs.setCurrentWidget(self._torrents_tab)
                    self._refresh()
                elif self._is_video_file(low) or low.endswith(
                        (".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma")):
                    self.play_file_in_player(target)
                elif low.endswith((".html", ".htm", ".mhtml", ".svg")):
                    self._browser_open_internal(QUrl.fromLocalFile(target).toString())
                else:
                    self._append_error(f"Don't know how to open: {os.path.basename(target)}")
            else:
                self._append_error(f"Cannot open (not found): {target[:100]}")
        except Exception as exc:
            self._append_error(f"Failed to open {os.path.basename(target)[:60]}: {exc}")

    def _on_downloaded_torrent_file(self, path: str, job_id: str) -> None:
        """A completed download-manager job turned out to be a .torrent file —
        add it to the torrent engine and remove the job from the list."""
        try:
            result = self.tools.call("add_torrent_file", {
                "path": path,
                "save_path": self.config.default_save_path,
                "category": "Other",
            })
            info_hash = result.get("info_hash", "")
            if info_hash:
                self._state_manager.store_torrent_file(path, info_hash)
            self._append_agent(
                f"**Torrent added from download.**\n\n"
                f"- **File:** `{os.path.basename(path)}`\n"
                f"- **Info hash:** `{info_hash}`\n"
                f"- **Save path:** `{self.config.default_save_path}`"
            )
            # The .torrent job itself is no longer useful — drop it and the file.
            try:
                self._dl_engine.remove_job(job_id)
                os.unlink(path)
            except OSError:
                pass
            self.main_tabs.setCurrentWidget(self._torrents_tab)
            self._refresh()
        except Exception as exc:
            self._append_error(f"Downloaded torrent could not be added: {exc}")

    def _is_torrent_download_url(self, url: str) -> bool:
        """True for direct .torrent files and URLs served by the configured indexer."""
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.path.lower().endswith(".torrent"):
            return True
        indexer_host = urlparse(self.config.indexer.url).netloc
        return bool(indexer_host) and parsed.netloc == indexer_host

    def _on_agent_finished(self, response: dict) -> None:
        """Handle the final agent/search response (called on GUI thread)."""
        try:
            search_results = response.get("_search_results")
            if search_results is not None:
                # Stop the search watchdog timer if it's running.
                if hasattr(self, "_search_timeout_timer") and self._search_timeout_timer.isActive():
                    self._search_timeout_timer.stop()
                # Late results (after the watchdog fired) are still rendered,
                # with a note — never silently discarded.
                late = getattr(self, "_search_timed_out_flag", False)
                self._search_timed_out_flag = False
                self._render_search_results(
                    search_results,
                    answer=response.get("_search_answer", ""),
                    late=late,
                )
                self._set_search_busy(False)
            else:
                content = response.get("content", "")
                # If the reply was streamed live, replace the raw streamed text
                # with the fully markdown-rendered bubble.
                self._finalize_stream()
                if content:
                    self._append_agent(content)
                if response.get("pending_confirmation"):
                    self._append_pending("Type 'yes' to confirm the pending action(s).")
                self._set_busy(False)
        except Exception:
            logger.error("failed to render response", exc_info=True)
            # Make sure both UIs are re-enabled on error.
            self._set_busy(False)
            self._set_search_busy(False)
        finally:
            self._refresh()

    def _render_search_results(self, results: list, answer: str = "", late: bool = False) -> None:
        """Render web sweep results in the merged Agents history."""
        import html as _html

        def esc(s: str) -> str:
            return _html.escape(s or "", quote=True)

        parts = []
        if late:
            parts.append('<i>(results arrived after the timeout)</i><br><br>')

        if answer:
            parts.append(f'<b>Summary</b><br>{esc(answer)}<br><br>')
        if not results:
            parts.append("<b>No results found.</b> Try a different search term.")
        else:
            rows_html = ""
            for i, r in enumerate(results, 1):
                url = r.get("url", "") or r.get("download_url", "")
                title = r.get("name", "") or r.get("title", "")
                title = esc(title)
                if url:
                    name = f'<a href="{esc(url)}">{title}</a>'
                else:
                    name = title
                rows_html += f"<tr><td>{i}</td><td>{name}</td></tr>"
            parts.append(
                f"<b>Web results</b> ({len(results)} found):<br><br>"
                f"<table><tr><th>#</th><th>Title</th></tr>"
                f"{rows_html}</table>"
                # No <br> here: a bare break right after a table makes Qt
                # under-compute the last row's height (clips it in half).
            )

        # Seal any live agent stream first: inserting at the document end must
        # not be swallowed when the stream's final answer replaces the raw
        # streamed text. Then scroll back to the TOP of the new results — a
        # results table is read from row 1 down, and landing at the document
        # bottom hides leading rows.
        self._seal_stream()
        self.chat_history.set_logo_visible(False)
        start_pos = self._insert_html_into(self.chat_history, f'<div class="msg-agent">{"".join(parts)}</div>')
        self._scroll_to_char_pos(self.chat_history, start_pos)

    @staticmethod
    def _scroll_to_char_pos(widget: QTextBrowser, char_pos: int) -> None:
        """Scroll so the block containing `char_pos` sits at the viewport top.

        Character positions + cursorRect do the document→viewport mapping for
        us (no manual margin math, no stale document().size() height — the old
        approach could overshoot and land mid-table with a half-clipped row).
        Deferred one tick so the scrollbar range reflects the new content."""
        def _go() -> None:
            doc = widget.document()
            cursor = QTextCursor(doc)
            cursor.setPosition(min(char_pos, max(0, doc.characterCount() - 1)))
            rect = widget.cursorRect(cursor)  # viewport coords; forces layout
            sb = widget.verticalScrollBar()
            sb.setValue(max(0, min(sb.maximum(), sb.value() + rect.top())))

        QTimer.singleShot(0, _go)

    def _append_search_user(self, text: str) -> None:
        """Render a web-sweep query in the merged Agents history."""
        self.chat_history.set_logo_visible(False)
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._insert_html(f'<div class="msg-user"><b>Search:</b> {safe}</div>')

    def _append_search_error(self, text: str) -> None:
        """Render a web-sweep error in the merged Agents history."""
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._insert_html(f'<div class="msg-error">{safe}</div>')

    # ------------------------------------------------------------------
    # Chat panel message rendering
    # ------------------------------------------------------------------

    def _append_user(self, text: str) -> None:
        """Render a user message as a styled bubble."""
        # First user message: hide the logo so the chat turns plain black.
        self.chat_history.set_logo_visible(False)
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._insert_html(f'<div class="msg-user"><b>You:</b> {safe}</div>')

    def _clear_chat(self) -> None:
        """Clear the merged Agents history and restore the logo background."""
        self.chat_history.clear()
        self.chat_history.set_logo_visible(True)
        self._stream_content_start = None
        self._stream_reasoning_started = False
        # Reset the agent's conversation history so old context doesn't leak back in.
        self.agent.history.clear()
        self.agent.pending.clear()

    def _append_agent(self, text: str) -> None:
        """Render an agent message as a styled bubble with markdown rendering."""
        body = _markdown_to_html(text)
        self._insert_html(f'<div class="msg-agent"><b>Agent:</b><br>{body}</div>')

    def _append_event(self, text: str) -> None:
        """Render a lightweight progress event (tool call, thinking, etc.)."""
        self._insert_html(f'<div class="msg-event">{text}</div>')

    def _append_debug(self, data) -> None:
        """Render raw tool data in debug mode (preformatted, capped)."""
        if isinstance(data, (dict, list)):
            text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        else:
            text = str(data)
        if len(text) > 4000:
            text = text[:4000] + "\n… (truncated)"
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._insert_html(f'<div class="msg-debug">{safe}</div>')

    def _append_error(self, text: str) -> None:
        """Render an error message."""
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._insert_html(f'<div class="msg-error"><b>Error:</b> {safe}</div>')

    def _append_pending(self, text: str) -> None:
        """Render a pending-confirmation prompt."""
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._insert_html(f'<div class="msg-pending"><b>⚠ Action needed:</b> {safe}</div>')

    def _insert_html(self, html: str) -> None:
        """Insert HTML at the end of the chat document and scroll to bottom."""
        # Funnel invariant: any insert seals a live agent stream first, so a
        # block arriving mid-stream (RSS event, error, search result) can't be
        # erased when _finalize_stream later trims the raw streamed text.
        self._seal_stream()
        html = _break_long_tokens(html)
        doc = self.chat_history.document()
        cursor = self.chat_history.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        start = cursor.position()
        cursor.insertHtml(html + "<br>")
        end = cursor.position()
        # Paint a translucent-black band behind every inserted block so ALL
        # text stays readable over the logo. CSS backgrounds on container
        # divs are unreliable when the div contains block-level children,
        # so this is applied programmatically per block instead.
        band = QColor(0, 0, 0, 255)  # 100% opaque black
        block = doc.findBlock(start)
        while block.isValid() and block.position() <= end:
            block_cursor = QTextCursor(block)
            fmt = block_cursor.blockFormat()
            fmt.setBackground(band)
            block_cursor.setBlockFormat(fmt)
            block = block.next()
        self.chat_history.setTextCursor(cursor)
        self.chat_history.ensureCursorVisible()

    def _insert_html_into(self, widget: QTextBrowser, html: str) -> int:
        """Insert HTML at the end of a QTextBrowser and scroll to bottom.

        Returns the document character position where the new content starts,
        so callers can scroll back to it (see _scroll_to_char_pos)."""
        html = _break_long_tokens(html)
        doc = widget.document()
        cursor = widget.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        start = cursor.position()
        cursor.insertHtml(html + "<br>")
        end = cursor.position()
        band = QColor(0, 0, 0, 255)
        block = doc.findBlock(start)
        while block.isValid() and block.position() <= end:
            block_cursor = QTextCursor(block)
            fmt = block_cursor.blockFormat()
            fmt.setBackground(band)
            block_cursor.setBlockFormat(fmt)
            block = block.next()
        widget.setTextCursor(cursor)
        widget.ensureCursorVisible()
        return start

    def _refresh(self) -> None:
        """Fetch torrent state off the GUI thread; render via signals.

        The engine's command queue blocks until the engine thread answers —
        doing that on the Qt thread stutters the whole UI while the engine is
        busy (hash checks, large adds). Skip the tick if the previous fetch
        is still running."""
        if self._refresh_inflight:
            return
        self._refresh_inflight = True
        selected = self._selected_hash
        # Only re-fetch the files list when the selection changed, or every
        # few seconds while it stays selected — rebuilding that table every
        # second resets scroll/selection constantly.
        fetch_files = False
        if selected:
            if selected != self._files_hash or time.time() - self._files_last_fetch > 5:
                fetch_files = True

        def _worker() -> None:
            try:
                torrents = self.engine.list_torrents()
            except Exception:
                self._refresh_inflight = False
                return
            self._refresh_signals.torrents.emit(torrents)
            if fetch_files and selected:
                try:
                    status = self.engine.get_torrent_status(selected)
                    self._refresh_signals.files.emit(status)
                except Exception:
                    pass
            # Stream waits need per-file progress + the verified contiguous
            # prefix to know when the buffer threshold is reached.
            for h in list(self._stream_waits):
                try:
                    st = self.engine.get_torrent_status(h)
                    w = self._stream_waits.get(h)
                    if w is not None:
                        try:
                            st["_stream_prefix"] = self.engine.get_file_prefix(
                                h, w.get("file_index", 0))[0]
                        except Exception:
                            pass
                    self._refresh_signals.stream_status.emit(h, st)
                except Exception:
                    pass
            self._refresh_inflight = False

        threading.Thread(target=_worker, daemon=True).start()

    def _on_torrent_selection_changed(self) -> None:
        selected = self.torrent_table.selectedItems()
        self._selected_hash = selected[0].data(Qt.UserRole) if selected else None
        if not self._selected_hash:
            # Selection lost (e.g. the torrent was removed) — clear the files
            # panel so it doesn't keep showing a deleted torrent's contents.
            self.file_table.setRowCount(0)
            self._files_hash = None
            return
        if self._selected_hash != self._files_hash:
            # Fetch off the GUI thread — the engine's command queue can block
            # for seconds while libtorrent is busy, freezing the whole UI.
            info_hash = self._selected_hash

            def _worker() -> None:
                try:
                    self._refresh_signals.files.emit(self.engine.get_torrent_status(info_hash))
                except Exception:
                    pass

            threading.Thread(target=_worker, daemon=True).start()

    VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
                  ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".3gp", ".ogv")

    def _is_video_file(self, path: str) -> bool:
        return path.lower().endswith(self.VIDEO_EXTS)

    def _render_torrents(self, torrents: List[Dict[str, Any]]) -> None:
        """Paint the torrent table (GUI thread, via _RefreshSignals)."""
        self.torrent_table.setSortingEnabled(False)  # don't re-sort mid-rebuild
        self.torrent_table.setRowCount(len(torrents))
        for i, t in enumerate(torrents):
            health = "OK" if t.get("num_peers", 0) > 0 or t.get("progress", 0) > 0 else "stalled"
            paused = " (paused)" if t.get("paused") else ""
            # Completed video: show a play indicator (double-click plays it).
            playable = t.get("progress", 0) >= 1.0 and self._is_video_file(t.get("name", ""))
            play_mark = "▶ " if playable else ""
            items = [
                play_mark + t.get("name", "") + paused,
                _human_state(t.get("state", "")),
                f"{t.get('progress', 0) * 100:.1f}%",
                format_rate(t.get("download_rate", 0)),
                format_rate(t.get("upload_rate", 0)),
                _fmt_eta(t.get("eta")),
                str(t.get("num_seeds", 0)),
                str(t.get("num_peers", 0)),
                format_size(t.get("total_size", 0)),
                health,
            ]
            for j, val in enumerate(items):
                self.torrent_table.setItem(i, j, QTableWidgetItem(val))
            self.torrent_table.item(i, 0).setData(Qt.UserRole, t.get("info_hash"))
        self.torrent_table.setSortingEnabled(True)
        # If the selected torrent disappeared (removed/completed-and-cleared),
        # drop the stale files panel — itemSelectionChanged doesn't always
        # fire when the rebuild wipes the selection.
        if self._selected_hash and not any(
                t.get("info_hash") == self._selected_hash for t in torrents):
            self._selected_hash = None
            self._files_hash = None
            self.file_table.setRowCount(0)
        self._update_status_bar(torrents)
        self._notify_completed_torrents(torrents)

    def _notify_completed_torrents(self, torrents: List[Dict[str, Any]]) -> None:
        """Toast when a torrent reaches 100% (baseline on first paint so
        torrents that were already complete at startup don't spam)."""
        if self._known_complete is None:
            self._known_complete = {
                t.get("info_hash") for t in torrents if t.get("progress", 0) >= 1.0}
            return
        for t in torrents:
            h = t.get("info_hash")
            if h and t.get("progress", 0) >= 1.0 and h not in self._known_complete:
                self._known_complete.add(h)
                self._notify("Torrent complete", t.get("name", h),
                             path=self._torrent_primary_file(h) or "")

    def _render_files(self, status: Dict[str, Any]) -> None:
        """Paint the files panel for a torrent status dict (GUI thread)."""
        self._files_hash = status.get("info_hash")
        self._files_last_fetch = time.time()
        files = status.get("files", [])
        self.file_table.setRowCount(len(files))
        for i, f in enumerate(files):
            self.file_table.setItem(i, 0, QTableWidgetItem(str(f["file_id"])))
            self.file_table.setItem(i, 1, QTableWidgetItem(f["path"]))
            self.file_table.setItem(i, 2, QTableWidgetItem(format_size(f["size"])))
            self.file_table.setItem(i, 3, QTableWidgetItem(f["priority_name"]))

    def _show_files(self, info_hash: str) -> None:
        try:
            self._render_files(self.engine.get_torrent_status(info_hash))
        except Exception:
            return

    def _file_context_menu(self, position) -> None:
        """Right-click context menu on the file table — open a single file."""
        info_hash = self._get_selected_info_hash()
        if not info_hash:
            return
        row = self.file_table.rowAt(position.y())
        if row < 0:
            return
        path_item = self.file_table.item(row, 1)
        if not path_item:
            return
        rel_path = path_item.text()
        try:
            status = self.engine.get_torrent_status(info_hash)
        except Exception as exc:
            self._append_error(f"Error locating torrent: {exc}")
            return
        save_path = status.get("save_path", "") or self.config.default_save_path
        full_path = os.path.join(save_path, rel_path)
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background-color: #111827; color: #c8d3e0; border: 1px solid #1a2a4a; border-radius: 6px; padding: 4px; } QMenu::item { padding: 3px 20px; border-radius: 4px; } QMenu::item:selected { background-color: #1a2a4a; color: #2a7abf; }")
        act_open = menu.addAction("Open File")
        act_reveal = menu.addAction("Reveal in Folder")
        action = menu.exec(self.file_table.mapToGlobal(position))
        if action == act_open:
            if os.path.isfile(full_path):
                self._reveal_path(full_path)
            else:
                QMessageBox.warning(self, "Open File", f"File not found:\n{full_path}\n\nIt may not be fully downloaded yet.")
        elif action == act_reveal:
            folder = os.path.dirname(full_path) or save_path
            if os.path.isdir(folder):
                self._reveal_path(folder)
            else:
                QMessageBox.warning(self, "Reveal in Folder", f"Folder not found:\n{folder}")

    def _add_magnet_dialog(self) -> None:
        uri, ok = QInputDialog.getText(self, "Add Magnet", "Paste a magnet link:")
        if ok and uri:
            try:
                result = self.tools.call("add_magnet", {"uri": uri, "save_path": self.config.default_save_path, "category": "Other"})
                self._append_agent(f"**Magnet added successfully.**\n\n- **Info hash:** `{result.get('info_hash', '')}`\n- **Category:** {result.get('category', '')}\n- **Save path:** `{result.get('save_path', '')}`")
                self.main_tabs.setCurrentWidget(self._torrents_tab)
            except Exception as exc:
                self._append_error(f"Error adding magnet: {exc}")
            self._refresh()

    def _add_torrent_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open Torrent File", "", "Torrent Files (*.torrent);;All Files (*)")
        if path:
            try:
                result = self.tools.call("add_torrent_file", {"path": path, "save_path": self.config.default_save_path, "category": "Other"})
                info_hash = result.get("info_hash", "")
                # Store the .torrent file persistently so we can resume after restart.
                if info_hash:
                    self._state_manager.store_torrent_file(path, info_hash)
                self._append_agent(f"**Torrent file added successfully.**\n\n- **Info hash:** `{info_hash}`\n- **Category:** {result.get('category', '')}\n- **Save path:** `{result.get('save_path', '')}`")
                self.main_tabs.setCurrentWidget(self._torrents_tab)
            except Exception as exc:
                self._append_error(f"Error adding torrent file: {exc}")
            self._refresh()

    def _on_torrent_double_clicked(self, _item) -> None:
        """Double-click: play completed videos; stream in-progress ones; else open the folder."""
        info_hash = self._get_selected_info_hash()
        if not info_hash:
            return
        try:
            status = self.engine.get_torrent_status(info_hash)
        except Exception:
            status = {}
        done = status.get("progress", 0) >= 1.0
        target = self._torrent_primary_file(info_hash) if done else None
        if target and self._is_video_file(target):
            self.play_file_in_player(target)
        elif not done and (
            any(self._is_video_file(f.get("path", "")) for f in status.get("files", []))
            or self._is_video_file(status.get("name", ""))
        ):
            self._stream_torrent(info_hash)
        else:
            self._open_torrent_folder(info_hash)

    def _get_selected_info_hashes(self) -> List[str]:
        """All selected torrent info-hashes (multi-select aware)."""
        seen = []
        for item in self.torrent_table.selectedItems():
            if item.column() != 0:
                continue
            h = item.data(Qt.UserRole)
            if h and h not in seen:
                seen.append(h)
        return seen

    def _get_selected_info_hash(self) -> Optional[str]:
        """Return the info_hash of the currently selected torrent row, or None."""
        hashes = self._get_selected_info_hashes()
        return hashes[0] if hashes else None

    def _remove_selected_torrent(self) -> None:
        """Remove the selected torrent (asks whether to delete files too)."""
        info_hash = self._get_selected_info_hash()
        if not info_hash:
            self._append_error("No torrent selected. Click a row in the torrent list first.")
            return
        # Find the torrent name for a nicer message.
        name = ""
        try:
            status = self.engine.get_torrent_status(info_hash)
            name = status.get("name", info_hash[:12])
        except Exception:
            name = info_hash[:12]

        reply = QMessageBox.question(
            self, "Remove Torrent",
            f"Remove '{name}' from the torrent list?\n\n"
            f"This removes it from the engine. The downloaded files stay on disk.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            self.tools.call("remove_torrent", {"info_hash": info_hash, "delete_files": False})
            self._state_manager.remove_torrent_file(info_hash)
            self._state_manager.remove_resume_data(info_hash)
            self._append_agent(f"**Torrent removed:** {name}\n\nThe downloaded files were kept on disk.")
        except Exception as exc:
            self._append_error(f"Error removing torrent: {exc}")
        self._refresh()

    def _clear_completed_torrents(self) -> None:
        """Remove all finished/seeding torrents from the list (files kept)."""
        try:
            torrents = self.engine.list_torrents()
        except Exception as exc:
            self._append_error(f"Error listing torrents: {exc}")
            return

        completed = [t for t in torrents if t.get("state") in ("finished", "seeding")]
        if not completed:
            self._append_agent("**No completed torrents to clear.**\n\nCompleted torrents (state: finished or seeding) will appear here.")
            return

        names = [t.get("name", t.get("info_hash", "")[:12]) for t in completed]
        reply = QMessageBox.question(
            self, "Clear Completed",
            f"Remove {len(completed)} completed torrent(s) from the list?\n\n"
            f"Files: " + ", ".join(names[:3]) + ("..." if len(names) > 3 else "") + "\n\n"
            f"Downloaded files will be kept on disk.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        removed = 0
        for t in completed:
            try:
                self.tools.call("remove_torrent", {"info_hash": t["info_hash"], "delete_files": False})
                self._state_manager.remove_torrent_file(t["info_hash"])
                self._state_manager.remove_resume_data(t["info_hash"])
                removed += 1
            except Exception:
                pass
        self._append_agent(f"**Cleared {removed} completed torrent(s).**\n\nFiles kept on disk:\n" + "\n".join(f"- {n}" for n in names))
        self._refresh()

    def _torrent_context_menu(self, position) -> None:
        """Right-click context menu on the torrent table."""
        info_hash = self._get_selected_info_hash()
        if not info_hash:
            return
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background-color: #111827; color: #c8d3e0; border: 1px solid #1a2a4a; border-radius: 6px; padding: 4px; } QMenu::item { padding: 3px 20px; border-radius: 4px; } QMenu::item:selected { background-color: #1a2a4a; color: #2a7abf; }")

        act_remove = menu.addAction("Remove from list (keep files)")
        act_delete = menu.addAction("Remove and delete files")
        menu.addSeparator()
        act_pause = menu.addAction("Pause")
        act_resume = menu.addAction("Resume")
        menu.addSeparator()
        act_open_folder = menu.addAction("Open Folder")
        act_open_file = menu.addAction("Open File")
        act_play = menu.addAction("Play in Player")
        act_stream = menu.addAction("Stream while downloading")

        action = menu.exec(self.torrent_table.mapToGlobal(position))
        selected_hashes = self._get_selected_info_hashes() or [info_hash]
        if action == act_remove:
            self._remove_selected_torrent()
        elif action == act_delete:
            self._delete_selected_torrent_with_files()
        elif action == act_pause:
            paused = 0
            for h in selected_hashes:
                try:
                    self.tools.call("pause_torrent", {"info_hash": h})
                    paused += 1
                except Exception as exc:
                    self._append_error(f"Error pausing: {exc}")
            if paused:
                self._append_event(f"⏸ Paused {paused} torrent(s)")
            self._refresh()
        elif action == act_resume:
            resumed = 0
            for h in selected_hashes:
                try:
                    self.tools.call("resume_torrent", {"info_hash": h})
                    resumed += 1
                except Exception as exc:
                    self._append_error(f"Error resuming: {exc}")
            if resumed:
                self._append_event(f"▶ Resumed {resumed} torrent(s)")
            self._refresh()
        elif action == act_open_folder:
            self._open_torrent_folder(info_hash)
        elif action == act_open_file:
            self._open_torrent_file(info_hash)
        elif action == act_play:
            target = self._torrent_primary_file(info_hash)
            if target:
                self.play_file_in_player(target)
            else:
                QMessageBox.warning(self, "Play", "File not found on disk. The torrent may not be fully downloaded.")
        elif action == act_stream:
            self._stream_torrent(info_hash)

    def _delete_selected_torrent_with_files(self) -> None:
        """Remove the selected torrent AND delete the downloaded files."""
        info_hash = self._get_selected_info_hash()
        if not info_hash:
            return
        name = ""
        try:
            status = self.engine.get_torrent_status(info_hash)
            name = status.get("name", info_hash[:12])
        except Exception:
            name = info_hash[:12]

        reply = QMessageBox.question(
            self, "Delete Torrent + Files",
            f"Permanently delete '{name}' and all downloaded files?\n\n"
            f"⚠ This cannot be undone. The files will be deleted from disk.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            self.tools.call("remove_torrent", {"info_hash": info_hash, "delete_files": True})
            self._state_manager.remove_torrent_file(info_hash)
            self._state_manager.remove_resume_data(info_hash)
            self._append_agent(f"**Torrent deleted:** {name}\n\nThe downloaded files were permanently deleted from disk.")
        except Exception as exc:
            self._append_error(f"Error deleting torrent: {exc}")
        self._refresh()

    def _open_torrent_folder(self, info_hash: str) -> None:
        """Open the torrent's save folder in Windows Explorer."""
        try:
            status = self.engine.get_torrent_status(info_hash)
        except Exception as exc:
            self._append_error(f"Error locating torrent: {exc}")
            return
        save_path = status.get("save_path", "") or self.config.default_save_path
        name = status.get("name", "")
        # libtorrent saves into save_path; single-file torrents sit directly in
        # save_path, multi-file torrents create a subfolder named after the torrent.
        folder = os.path.join(save_path, name) if name and os.path.isdir(os.path.join(save_path, name)) else save_path
        if not os.path.isdir(folder):
            folder = save_path
        if not os.path.isdir(folder):
            QMessageBox.warning(self, "Open Folder", f"Folder not found:\n{folder}")
            return
        self._reveal_path(folder)

    def _torrent_primary_file(self, info_hash: str) -> Optional[str]:
        """Locate the torrent's primary (largest) file on disk, or None."""
        try:
            status = self.engine.get_torrent_status(info_hash)
        except Exception as exc:
            self._append_error(f"Error locating torrent: {exc}")
            return None
        save_path = status.get("save_path", "") or self.config.default_save_path
        name = status.get("name", "")
        files = status.get("files", [])
        if files:
            biggest = max(files, key=lambda f: f.get("size", 0))
            candidate = os.path.join(save_path, biggest.get("path", ""))
            if os.path.isfile(candidate):
                return candidate
        if name:
            # Single-file torrent: the file sits directly in save_path.
            candidate = os.path.join(save_path, name)
            if os.path.isfile(candidate):
                return candidate
        return None

    def _open_torrent_file(self, info_hash: str) -> None:
        """Open the torrent's primary file directly with its default app."""
        target = self._torrent_primary_file(info_hash)
        if not target:
            QMessageBox.warning(self, "Open File", "File not found on disk. The torrent may not be fully downloaded.")
            return
        self._reveal_path(target)

    def play_file_in_player(self, path: str) -> None:
        """Switch to the Player tab and play a local media file."""
        self.main_tabs.setCurrentWidget(self.iptv_tab)
        self.iptv_tab.play_file(path)

    # -- stream while downloading ------------------------------------------
    def _stream_torrent(self, info_hash: str) -> None:
        """Enable sequential download and start playback once enough of the
        video is buffered (mpv reads the growing file from disk)."""
        try:
            status = self.engine.get_torrent_status(info_hash)
        except Exception as exc:
            self._append_error(f"Error reading torrent: {exc}")
            return
        videos = [f for f in status.get("files", [])
                  if self._is_video_file(f.get("path", ""))]
        save_path = status.get("save_path", "") or self.config.default_save_path
        if not videos:
            # Single-file torrents have no file list entry; use the name.
            name = status.get("name", "")
            if self._is_video_file(name):
                videos = [{"path": name,
                           "size": status.get("total_size", 0),
                           "downloaded": status.get("total_done", 0)}]
            else:
                QMessageBox.information(
                    self, "Stream",
                    "No video file found in this torrent.\n"
                    "(Metadata may still be downloading.)")
                return
        target = max(videos, key=lambda f: f.get("size", 0))
        file_index = int(target.get("file_id", 0))
        disk_path = os.path.join(save_path, target.get("path", ""))
        # Sequential pieces + make sure the torrent is actually running.
        try:
            self.engine.set_sequential_download(info_hash, True)
            if status.get("paused"):
                self.tools.call("resume_torrent", {"info_hash": info_hash})
        except Exception as exc:
            self._append_error(f"Error enabling stream mode: {exc}")
        size = int(target.get("size", 0))
        done = int(target.get("downloaded", 0))
        # The playable amount is the gapless VERIFIED prefix — file progress
        # counts pieces anywhere in the file (gaps decode as artifacts).
        prefix = done
        try:
            prefix, _sz = self.engine.get_file_prefix(info_hash, file_index)
        except Exception:
            pass
        threshold = (min(32 * 1024 * 1024, max(4 * 1024 * 1024, size // 100))
                     if size else 4 * 1024 * 1024)
        if (size and prefix >= size) or (prefix >= threshold and os.path.isfile(disk_path)):
            if size and prefix < size:
                self._register_stream_playback(info_hash, file_index, disk_path, size)
            self.play_file_in_player(disk_path)
            return
        self._stream_waits[info_hash] = {
            "disk_path": disk_path,
            "file": target.get("path", ""),
            "file_index": file_index,
            "size": size,
            "threshold": threshold,
            "duration": 0.0,          # probed via ffprobe; -1 = unavailable
            "probing": False,
            "last_probe_done": 0,
            "rate": 0.0,              # EMA of the per-file download rate (B/s)
            "last_done": done,
            "last_t": time.time(),
            "deadline": time.time() + 120,
            "announced": False,
        }
        self._append_event(
            f"⏳ Buffering `{os.path.basename(disk_path)}` — playback starts "
            f"at ~{threshold // (1024 * 1024)} MB downloaded…")
        self._refresh()  # kick an immediate status fetch

    def _probe_stream_duration(self, info_hash: str) -> None:
        """ffprobe the partial file for its duration (background thread — the
        wait dict is polled on the GUI thread, never block it)."""
        wait = self._stream_waits.get(info_hash)
        if not wait:
            return
        path = wait["disk_path"]

        def _run() -> None:
            from dlmgr.ffmpeg import find_ffmpeg, find_ffprobe, probe_duration
            ffmpeg = find_ffmpeg(self.config.download.ffmpeg_path)
            if ffmpeg:
                dur = probe_duration(path, find_ffprobe(ffmpeg))
            else:
                dur = -1.0  # no ffmpeg/ffprobe at all — stop retrying
            w = self._stream_waits.get(info_hash)
            if w is not None and w.get("disk_path") == path:
                w["duration"] = dur
                w["probing"] = False

        threading.Thread(target=_run, daemon=True).start()

    def _on_stream_status(self, info_hash: str, status: Dict[str, Any]) -> None:
        """Buffer-threshold check for pending streams (GUI thread).

        The threshold is adaptive: the real per-file download rate (EMA) and
        the video duration (ffprobe on the partial file) feed the classic
        stall-free condition — buffer >= (bitrate - rate) * duration — so a
        connection slower than the video bitrate accumulates enough headroom
        for playback to never catch the download. Fast connections keep the
        small default buffer."""
        wait = self._stream_waits.get(info_hash)
        if not wait:
            return
        now = time.time()
        files = status.get("files", [])
        done = 0
        for f in files:
            if f.get("path") == wait["file"]:
                done = int(f.get("downloaded", 0))
                break
        if not files:
            # Single-file torrents have no file list entry.
            done = int(status.get("total_done", 0))
        # The playable amount is the verified gapless prefix — never let raw
        # per-file progress (counts pieces with gaps) start playback early.
        prefix = status.get("_stream_prefix")
        avail = int(prefix) if prefix is not None else done

        # Per-file download rate, EMA-smoothed (measured on the playable
        # prefix — that's the rate playback actually experiences).
        dt = now - wait["last_t"]
        if dt > 0.2:
            inst = max(0, avail - wait["last_done"]) / dt
            wait["rate"] = inst if not wait["rate"] else 0.7 * wait["rate"] + 0.3 * inst
            wait["last_done"], wait["last_t"] = avail, now

        # Probe the duration once enough of the file head exists; retry as
        # data grows (non-faststart MP4 reveals duration late). -1 = no
        # ffprobe — keep the fixed threshold.
        size = wait["size"]
        if (size and not wait["duration"] and not wait["probing"]
                and avail >= 4 * 1024 * 1024 and avail >= wait["last_probe_done"] * 2):
            wait["probing"] = True
            wait["last_probe_done"] = avail
            self._probe_stream_duration(info_hash)

        # Grow the buffer when the download can't outrun playback.
        if size and wait["duration"] > 0 and wait["rate"] > 0:
            bitrate = size / wait["duration"]
            safe_rate = wait["rate"] / 1.25  # margin for rate dips
            if safe_rate < bitrate:
                needed = int((bitrate - safe_rate) * wait["duration"])
                wait["threshold"] = max(4 * 1024 * 1024, min(needed, size))
                if not wait["announced"]:
                    wait["announced"] = True
                    self._append_event(
                        f"🐢 Download {format_rate(int(wait['rate']))} is below the "
                        f"video bitrate ~{format_rate(int(bitrate))} — buffering "
                        f"{wait['threshold'] // (1024 * 1024)} MB before playback…")

        # The deadline follows the buffer goal instead of a fixed 120s —
        # slow connections get the time they actually need; a stalled
        # download still times out.
        remaining = wait["threshold"] - avail
        if wait["rate"] > 1024 and remaining > 0:
            wait["deadline"] = now + max(60.0, remaining / wait["rate"] * 1.5 + 30)
        if now > wait["deadline"]:
            del self._stream_waits[info_hash]
            self._append_error(f"Streaming timed out waiting for data: {wait['file']}")
            return
        if status.get("progress", 0) >= 1.0 or avail >= wait["threshold"]:
            del self._stream_waits[info_hash]
            if os.path.isfile(wait["disk_path"]):
                # Partial file: cap playback at the verified frontier and
                # keep read-ahead piece deadlines active while it grows.
                if wait["size"] and avail < wait["size"]:
                    self._register_stream_playback(
                        info_hash, wait.get("file_index", 0),
                        wait["disk_path"], wait["size"])
                self.play_file_in_player(wait["disk_path"])
            else:
                self._append_error(f"File not on disk yet: {wait['file']}")

    # -- torrent stream frontier capping (anti-artifact) ----------------------
    def _on_player_position(self, pos: float, dur: float) -> None:
        self._player_pos = (pos, dur)

    def _register_stream_playback(self, info_hash: str, file_index: int,
                                  path: str, size: int) -> None:
        """Watch a still-downloading file during playback: cap the player's
        end at the verified contiguous prefix and keep pieces ahead of the
        playhead on deadline, so mpv never reads zeroed/unverified regions
        (they decode as macroblock artifacts)."""
        self._stream_active = {
            "info_hash": info_hash, "file_index": file_index,
            "path": path, "size": size, "last_end": None,
        }
        self._stream_tick_timer.start()

    def _on_stream_playback_tick(self) -> None:
        active = self._stream_active
        if active is None:
            self._stream_tick_timer.stop()
            return
        player = self.iptv_tab._player
        item = player._current_item
        if item is None or getattr(item, "url", "") != active["path"]:
            # User stopped or switched content — lift the cap and stop.
            if player._backend is not None:
                player._backend.set_playback_end(None)
            self._stream_active = None
            self._stream_tick_timer.stop()
            return
        try:
            prefix, size = self.engine.get_file_prefix(
                active["info_hash"], active["file_index"])
        except Exception:
            return
        size = size or active["size"]
        pos, dur = self._player_pos
        # Read-ahead: pieces covering the 64 MB after the playhead get top
        # priority + a deadline so the frontier stays ahead of playback.
        if dur > 0 and size:
            playhead = int(pos / dur * size)
            try:
                self.engine.set_stream_window(active["info_hash"], playhead,
                                              playhead + 64 * 1024 * 1024)
            except Exception:
                pass
        if not size or prefix >= size:
            # Fully downloaded — uncap and stop watching.
            if player._backend is not None:
                player._backend.set_playback_end(None)
            self._stream_active = None
            self._stream_tick_timer.stop()
            return
        if dur <= 0:
            return
        # Cap 2 MB before the frontier — the demuxer reads a little ahead.
        end_time = max(1.0, (prefix - 2 * 1024 * 1024) / size * dur)
        last = active["last_end"]
        if last is None or abs(end_time - last) > 1.0:
            # If playback stalled exactly at the previous cap, resume it now
            # that the frontier moved (without fighting a deliberate pause).
            if (last is not None and player._backend is not None
                    and not player._backend.is_playing and abs(pos - last) < 2.0):
                player._backend.resume()
            if player._backend is not None:
                player._backend.set_playback_end(end_time)
            active["last_end"] = end_time

    # -- system tray + notifications -----------------------------------------
    def _setup_tray(self) -> None:
        """Tray icon with Show/Quit + completion toasts. Closing the window
        quits the app; the tray icon disappears with it."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon_path = self._resolve_icon_path()
        icon = QIcon(icon_path) if icon_path else self.windowIcon()
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("DeepFlux")
        menu = QMenu()
        menu.setStyleSheet("QMenu { background-color: #111827; color: #c8d3e0; border: 1px solid #1a2a4a; border-radius: 6px; padding: 4px; } QMenu::item { padding: 3px 20px; border-radius: 4px; } QMenu::item:selected { background-color: #1a2a4a; color: #2a7abf; }")
        menu.addAction("Show DeepFlux", self._tray_restore)
        menu.addAction("Quit", self._tray_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.messageClicked.connect(self._on_tray_message_clicked)
        self._tray.show()

    def _tray_restore(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_quit(self) -> None:
        self.close()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible():
                self.hide()
            else:
                self._tray_restore()

    def _notify(self, title: str, text: str, path: str = "") -> None:
        """Windows toast/balloon via the tray icon. path powers click-through."""
        if self._tray is None or not self.config.ui_notifications:
            return
        self._last_notify_path = path
        self._tray.showMessage(title, text, QSystemTrayIcon.Information, 6000)

    def _on_tray_message_clicked(self) -> None:
        """Click on a completion toast: show the app and open the result —
        videos play in the Player, anything else reveals its folder."""
        self._tray_restore()
        path = self._last_notify_path
        if path and os.path.isfile(path):
            if self._is_video_file(path):
                self.play_file_in_player(path)
            else:
                self._reveal_path(os.path.dirname(path))

    def play_web_stream(self, payload: dict) -> None:
        """Play a web stream captured by the browser extension (POST /api/play).

        Uses the mpv-backed Player tab — mpv bundles FFmpeg with proprietary
        codecs (H.264/AAC), which the built-in QtWebEngine browser lacks."""
        url = (payload.get("url") or "").strip()
        if not url:
            return
        headers = dict(payload.get("headers") or {})
        if payload.get("referrer"):
            headers.setdefault("Referer", payload["referrer"])
        if payload.get("cookies"):
            headers.setdefault("Cookie", payload["cookies"])
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.main_tabs.setCurrentWidget(self.iptv_tab)
        self.iptv_tab.play_web_stream(url, title=payload.get("title", ""), headers=headers)

    @staticmethod
    def _reveal_path(path: str) -> None:
        """Open a folder in Explorer or launch a file with its default app."""
        if sys.platform == "win32":
            import os as _os
            _os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])

    def _open_api_keys(self) -> None:
        """Open the unified API Keys dialog."""
        dialog = APIKeysDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            self.config.to_file(self.config_path)
            self._reload_agent()
            self.iptv_tab.reload_config(self.config)
            self._append_agent("**API Keys saved.** All keys updated.")

    # ------------------------------------------------------------------
    # Settings backup (File → Export/Import Settings)
    # ------------------------------------------------------------------

    def _ask_passphrase(self, title: str, confirm: bool) -> Optional[str]:
        pw, ok = QInputDialog.getText(self, title, "Passphrase:", QLineEdit.Password)
        if not ok or not pw:
            return None
        if confirm:
            pw2, ok = QInputDialog.getText(
                self, title, "Confirm passphrase:", QLineEdit.Password)
            if not ok:
                return None
            if pw != pw2:
                QMessageBox.warning(self, title, "Passphrases do not match.")
                return None
        return pw

    def _export_settings(self) -> None:
        """Write the whole config to a passphrase-encrypted .dfc file."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Settings",
            os.path.join(os.path.expanduser("~"), "DeepFluxSettings.dfc"),
            "DeepFlux Settings (*.dfc)")
        if not path:
            return
        pw = self._ask_passphrase("Export Settings", confirm=True)
        if pw is None:
            return
        try:
            import dataclasses
            payload = json.dumps(
                dataclasses.asdict(self.config), ensure_ascii=False).encode("utf-8")
            Path(path).write_bytes(encrypt_settings(payload, pw))
        except OSError as exc:
            QMessageBox.warning(self, "Export Settings",
                                f"Could not write the file:\n{exc}")
            return
        self._append_agent(
            f"**Settings exported** to `{path}` — passphrase-protected. "
            "Keep the file safe; it contains your API keys.")

    def _import_settings(self) -> None:
        """Replace the config from a .dfc backup; applies after a restart."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Settings", "",
            "DeepFlux Settings (*.dfc);;All Files (*)")
        if not path:
            return
        try:
            blob = Path(path).read_bytes()
        except OSError as exc:
            QMessageBox.warning(self, "Import Settings",
                                f"Could not read the file:\n{exc}")
            return
        pw = self._ask_passphrase("Import Settings", confirm=False)
        if pw is None:
            return
        try:
            payload = decrypt_settings(blob, pw)
            validate_settings(payload)
        except SettingsBackupError as exc:
            QMessageBox.warning(self, "Import Settings", str(exc))
            return
        # The running app keeps its in-memory config; the imported file takes
        # effect on the next launch. Back up the current one first.
        try:
            if os.path.exists(self.config_path):
                import shutil
                shutil.copy2(self.config_path, self.config_path + ".bak")
            Path(self.config_path).write_bytes(payload)
        except OSError as exc:
            QMessageBox.warning(self, "Import Settings",
                                f"Could not replace the current settings:\n{exc}")
            return
        box = QMessageBox(self)
        box.setWindowTitle("Import Settings")
        box.setText(
            "Settings imported successfully.\n\n"
            "They take effect after a restart. Your previous settings were "
            "backed up next to the config file (config.json.bak).")
        restart_btn = box.addButton("Restart now", QMessageBox.AcceptRole)
        box.addButton("Later", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is restart_btn:
            self._restart_app()
        else:
            self._append_agent("**Settings imported** — restart DeepFlux to apply them.")

    def _restart_app(self) -> None:
        """Restart into newly imported settings.

        closeEvent must NOT re-save the old in-memory config over the
        imported file, hence the skip flag."""
        self._skip_config_save = True
        if getattr(sys, "frozen", False):
            QProcess.startDetached(sys.executable, sys.argv[1:])
        else:
            QProcess.startDetached(
                sys.executable, [os.path.abspath(sys.argv[0]), *sys.argv[1:]])
        self.close()

    def _open_indexer_settings(self) -> None:
        """Open Indexer settings (Jackett)."""
        dialog = IndexerSettingsDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            self.config.to_file(self.config_path)
            self._reload_agent()
            status = "configured" if self.config.indexer.api_key else "not configured (per-source web search)"
            self._append_agent(f"**Jackett Settings saved.** Jackett: `{status}`.")
            # The user just (re)entered the key — sync the sources list now,
            # starting Jackett first if needed.
            self._start_jackett_sync(force=True)

    def _open_downloads_settings(self) -> None:
        """Open Downloads settings (save path + download manager)."""
        dialog = DownloadsSettingsDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            self.config.to_file(self.config_path)
            # Torrent rate limits apply live; listen port/connections need a restart.
            try:
                self.engine.set_rate_limits(
                    self.config.torrents.download_rate_limit_kb,
                    self.config.torrents.upload_rate_limit_kb,
                )
            except Exception:
                pass
            # Download-manager bandwidth limit applies live too.
            try:
                self._dl_engine.set_bandwidth_limit(self.config.download.bandwidth_limit_bps)
            except Exception:
                pass
            self._append_agent(
                f"**Downloads Settings saved.** Save path: `{self.config.default_save_path}`, "
                f"Max concurrent: `{self.config.download.max_concurrent}`, "
                f"Max connections: `{self.config.download.max_connections_per_download}`. "
                f"Torrent listen port / connection limit changes apply after restart."
            )

    def _open_browser_settings(self) -> None:
        """Open Browser settings (homepage)."""
        dialog = BrowserSettingsDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            self.config.to_file(self.config_path)
            self._append_agent(f"**Browser Settings saved.** Homepage: `{self.config.browser.homepage}`.")

    def _open_iptv_settings(self) -> None:
        """Play-tab gear button: IPTV settings are split into focused pages —
        show a picker menu at the cursor."""
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background-color: #111827; color: #c8d3e0; border: 1px solid #1a2a4a; border-radius: 6px; padding: 4px; } QMenu::item { padding: 3px 20px; border-radius: 4px; } QMenu::item:selected { background-color: #1a2a4a; color: #2a7abf; }")
        chosen: Dict[str, Any] = {}
        for label, cls in IPTV_SETTINGS_PAGES:
            menu.addAction(label, lambda _c=False, page_cls=cls: chosen.update(cls=page_cls))
        from PySide6.QtGui import QCursor
        menu.exec(QCursor.pos())
        cls = chosen.get("cls")
        if cls is not None:
            self._open_iptv_page(cls)

    def _open_iptv_page(self, dialog_cls) -> None:
        """Open one IPTV settings page and persist on OK."""
        dialog = dialog_cls(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            self.config.to_file(self.config_path)
            # Re-apply config to the running IPTV tab (sources, TMDb key, etc.).
            self.iptv_tab.reload_config(self.config)
            self._append_agent(
                f"**IPTV Settings saved** ({dialog.windowTitle()}). "
                f"Sources: `{len(self.config.iptv.sources)}`, "
                f"Player: `{self.config.iptv.preferred_player}`, "
                f"TMDb: {'configured' if self.config.iptv.tmdb_api_key else 'not set (TVmaze fallback)'}."
            )

    def _open_sources(self) -> None:
        """Open the Sources management dialog."""
        dialog = SourcesDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            self.config.to_file(self.config_path)
            self._reload_agent()
            count = len(self.config.sources.sources)
            enabled = sum(1 for s in self.config.sources.sources if s.enabled)
            self._append_agent(
                f"**Sources updated.** {enabled}/{count} sources enabled. "
                f"Jackett integration: {'on' if self.config.sources.use_jackett else 'off'}."
            )

    def _open_help(self) -> None:
        """Open the comprehensive user guide."""
        dialog = HelpDialog(self)
        dialog.exec()

    def _open_about(self) -> None:
        """Open the About dialog."""
        dialog = AboutDialog(self)
        dialog.exec()

    def _reload_agent(self) -> None:
        """Recreate the LLM client and agent loop with the current config."""
        try:
            self.agent.stop_watchdog()
        except Exception:
            pass
        self.tools = ToolRegistry(self.engine, self.config, dl_engine=self._dl_engine,
                                  irc_client=self._irc_client)
        if getattr(self, "_iptv_bridge", None) is not None:
            self.tools.set_iptv_bridge(self._iptv_bridge)
        if getattr(self, "_browser_bridge", None) is not None:
            self.tools.set_browser_bridge(self._browser_bridge)
        self.agent = AgentLoop(
            self.engine, self.config, tools=self.tools,
            on_event=lambda evt: self._agent_signals.event.emit(evt),
        )
        self._rss_monitor = RSSMonitor(self.config.rss)
        logger.info("Agent reloaded with provider=%s", self.config.llm.provider)

    # ------------------------------------------------------------------
    # RSS feeds
    # ------------------------------------------------------------------

    def _open_rss_dialog(self) -> None:
        """Open the RSS feed management dialog."""
        dialog = RSSDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            # Save config to disk.
            self.config.to_file(self.config_path)
            # Update the RSS monitor with the new config.
            self._rss_monitor = RSSMonitor(self.config.rss)
            feed_count = len(self.config.rss.feeds)
            self._append_agent(
                f"**RSS feeds updated.**\n\n"
                f"| # | Name | Mode | Category |\n"
                f"|---|------|------|----------|\n" +
                "\n".join(
                    f"| {i+1} | {f.name or f.url} | {'Auto-download' if f.mode == 'auto_download' else 'Monitor'} | {f.category} |"
                    for i, f in enumerate(self.config.rss.feeds)
                ) +
                f"\n\n**Total feeds:** {feed_count}"
            )
            # Check if "Check Now" was requested — open the viewer window.
            if getattr(dialog, "_check_requested", False):
                self._open_rss_viewer()

    def _check_rss_feeds(self) -> None:
        """Background RSS check. Auto-downloads from auto_download feeds.
        For monitor feeds, just emits a notification — does NOT mark items as seen
        (so the agent can still show them when the user asks)."""
        if not self.config.rss.feeds:
            return

        def _worker():
            for feed in self.config.rss.feeds:
                try:
                    result = self._rss_monitor.check_feed(feed)
                    if result.get("error"):
                        logger.warning("RSS check failed for %s: %s", feed.url, result["error"])
                        continue

                    new_items = result.get("items", [])
                    if not new_items:
                        continue

                    feed_name = feed.name or feed.url

                    if feed.mode == "auto_download":
                        # Auto-download all new items with magnet/torrent links.
                        downloaded = 0
                        for item in new_items:
                            magnet = item.get("magnet_uri", "")
                            torrent_url = item.get("torrent_url", "")
                            try:
                                if magnet:
                                    self.tools.call("add_magnet", {
                                        "uri": magnet,
                                        "save_path": self.config.default_save_path,
                                        "category": feed.category,
                                    })
                                    downloaded += 1
                                elif torrent_url:
                                    # Download the .torrent file from URL.
                                    import tempfile
                                    resp = requests.get(torrent_url, timeout=30, headers={"User-Agent": "Deeptorrent/0.1"})
                                    resp.raise_for_status()
                                    with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as tmp:
                                        tmp.write(resp.content)
                                        tmp_path = tmp.name
                                    self.tools.call("add_torrent_file", {
                                        "path": tmp_path,
                                        "save_path": self.config.default_save_path,
                                        "category": feed.category,
                                    })
                                    downloaded += 1
                            except Exception as exc:
                                logger.warning("Auto-download failed for %s: %s", item.get("title", ""), exc)

                        # Mark items as seen only for auto-download mode.
                        self._rss_monitor.mark_seen(feed, new_items)

                        if downloaded > 0:
                            self._agent_signals.event.emit({
                                "type": "rss_auto_download",
                                "feed": feed_name,
                                "count": downloaded,
                            })
                    else:
                        # Monitor mode: do NOT mark items as seen — leave them
                        # for the agent/user to view later. Just notify.
                        self._agent_signals.event.emit({
                            "type": "rss_monitor",
                            "feed": feed_name,
                            "new_items": len(new_items),
                        })

                except Exception as exc:
                    logger.error("RSS check error for %s: %s", feed.url, exc)

            # Save config (updates seen_items for auto-download feeds).
            try:
                self.config.to_file(self.config_path)
            except Exception:
                pass

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def _open_rss_viewer(self) -> None:
        """Open the RSS viewer window with all feed items in a browsable table."""
        viewer = RSSViewer(self.config, self.tools, self)
        viewer.exec()

    def _check_incomplete_torrents(self) -> None:
        """On startup, offer to restore torrents from the previous session."""
        entries = self._state_manager.load_state()
        if not entries:
            return
        incomplete = [e for e in entries
                      if e.get("state") not in ("finished", "seeding") and e.get("progress", 0) < 1.0]
        # Completed torrents are restored too (paused) when enabled in settings.
        if self.config.torrents.restore_completed:
            completed = [e for e in entries if e not in incomplete]
        else:
            completed = []
        to_restore = incomplete + completed
        if not to_restore:
            self._state_manager.clear_state()
            return

        lines = []
        for t in incomplete:
            name = t.get("name", t.get("info_hash", "")[:12])
            progress = t.get("progress", 0) * 100
            lines.append(f"  • {name} — {progress:.1f}%")

        msg = f"You have {len(incomplete)} incomplete download(s) from your last session:\n\n" if incomplete else ""
        msg += "\n".join(lines[:10])
        if len(incomplete) > 10:
            msg += f"\n  ...and {len(incomplete) - 10} more."
        if completed:
            msg += f"\n\nPlus {len(completed)} completed torrent(s) (restored paused, kept in the list)."
        msg += "\n\nWould you like to restore them?"

        reply = QMessageBox.question(
            self, "Resume Downloads", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )

        if reply == QMessageBox.Yes:
            self._restore_torrents(to_restore)
        else:
            # Clear the state so it doesn't ask again.
            self._state_manager.clear_state()

    def _restore_torrents(self, entries: List[Dict[str, Any]]) -> None:
        """Re-add torrents from saved state, using fast-resume data when present."""
        restored = 0
        failed = 0
        for entry in entries:
            info_hash = entry.get("info_hash", "")
            save_path = entry.get("save_path", self.config.default_save_path)
            category = entry.get("category", "Other")
            magnet_uri = entry.get("magnet_uri", "")
            torrent_file = entry.get("torrent_file", "")
            # Completed torrents come back paused; interrupted ones keep their
            # previous paused state too.
            was_complete = entry.get("state") in ("finished", "seeding") or entry.get("progress", 0) >= 1.0
            paused = bool(entry.get("paused")) or was_complete
            resume = self._state_manager.load_resume_data(info_hash)

            try:
                # Prefer the stored .torrent file (more reliable than magnet for resume).
                stored_path = self._state_manager.get_stored_torrent_path(info_hash)
                if stored_path:
                    self.engine.add_torrent_file(stored_path, save_path, category, paused=paused, resume=resume)
                    restored += 1
                elif magnet_uri:
                    self.engine.add_magnet(magnet_uri, save_path, category, paused=paused, resume=resume)
                    restored += 1
                elif torrent_file and os.path.isfile(torrent_file):
                    self.engine.add_torrent_file(torrent_file, save_path, category, paused=paused, resume=resume)
                    restored += 1
                else:
                    failed += 1
                    logger.warning("Cannot restore torrent %s: no magnet or torrent file", info_hash)
            except Exception as exc:
                failed += 1
                logger.warning("Failed to restore torrent %s: %s", info_hash, exc)

        if restored > 0:
            self._append_agent(
                f"**Resumed {restored} download(s) from your last session.**\n\n" +
                (f"Failed to restore {failed}." if failed else "")
            )
        # Clear the state — it will be re-saved on next close.
        self._state_manager.clear_state()
        self._refresh()

    def _save_torrent_state(self) -> None:
        """Save current torrent state to disk for next session."""
        try:
            torrents = self.engine.list_torrents()
            # Store .torrent files for torrents added via file (so we can restore them).
            for t in torrents:
                torrent_file = t.get("torrent_file", "")
                info_hash = t.get("info_hash", "")
                if torrent_file and info_hash and os.path.isfile(torrent_file):
                    self._state_manager.store_torrent_file(torrent_file, info_hash)
            self._state_manager.save_state(torrents)
        except Exception as exc:
            logger.warning("Failed to save torrent state: %s", exc)

    def closeEvent(self, event) -> None:
        # Closing the window (X) quits the app — no background instance is
        # left running in the tray.

        # Confirm before quitting with active transfers.
        try:
            active_t = sum(
                1 for t in self.engine.list_torrents()
                if t.get("state") == "downloading" and not t.get("paused")
            )
            active_d = sum(
                1 for j in self._dl_engine.list_jobs()
                if j.status.value in ("downloading", "queued")
            )
        except Exception:
            active_t = active_d = 0
        if active_t or active_d:
            reply = QMessageBox.question(
                self, "Quit DeepFlux",
                f"{active_t + active_d} transfer(s) still in progress "
                f"({active_t} torrents, {active_d} downloads).\n\n"
                "They will resume next time you launch the app. Quit anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return

        # Stop an in-progress voice recording so the mic handle is released.
        if getattr(self, "_voice_rec", None) is not None and self._voice_rec.recording:
            self._voice_rec.stop()

        # Persist window geometry + active tab. Skipped when an imported
        # settings file is pending — the old in-memory config must not
        # overwrite it on the way out.
        if not getattr(self, "_skip_config_save", False):
            try:
                self.config.ui_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
                self.config.ui_last_tab = self.main_tabs.currentIndex()
                self._save_splitters()
                self.config.to_file(self.config_path)
            except Exception as exc:
                logger.warning("Failed to persist UI state: %s", exc)

        # Export fast-resume data first so the next launch skips hash rechecks.
        try:
            self._state_manager.save_resume_data(self.engine.export_resume_data())
        except Exception as exc:
            logger.warning("Failed to export resume data: %s", exc)
        # Save torrent state before closing.
        self._save_torrent_state()
        self.timer.stop()
        try:
            self._rss_timer.stop()
        except Exception:
            pass
        self.agent.stop_watchdog()
        self.engine.stop()
        # Stop download manager.
        try:
            self._dl_api.stop()
            self._dl_engine.stop()
        except Exception:
            pass
        # Stop IPTV subsystem (player backend + background threads + cache).
        try:
            self.iptv_tab.shutdown()
        except Exception:
            pass
        # Stop IRC subsystem (persist joined channels, QUIT networks).
        try:
            self.irc_tab.shutdown()
            self._irc_client.shutdown()
        except Exception:
            pass
        # A fullscreened browser view is a parentless top-level window —
        # destroy it so it can't outlive the main window and block the quit.
        if self._browser_fs_state is not None:
            self._browser_fs_state[0].hide()
            self._browser_fs_state[0].deleteLater()
            self._browser_fs_state = None
        event.accept()


def run_gui(config_path: Optional[str] = None, open_targets: Optional[List[str]] = None) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Deeptorrent")
    from gui.fonts import load_app_fonts
    load_app_fonts(app)  # bundled fonts → identical text metrics on every machine
    window = MainWindow(config_path=config_path)
    window.show()
    # Targets passed on the command line (file associations / "Open with").
    for t in (open_targets or []):
        QTimer.singleShot(500, lambda t=t: window.open_target(t))
    return app.exec()
