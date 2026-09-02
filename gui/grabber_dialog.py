"""Site Grabber dialog: keyword search → video list → download queue.

Searches a video site (MissAV) by keywords, lists the results with
thumbnails, and queues selected videos as HLS downloads in the download
manager. All network work (search, thumbnails, resolve + manifest parse)
runs on daemon threads; engine calls stay serialized on a single worker
so DownloadEngine state is never mutated concurrently.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from dlmgr.site_grabber import GrabberError, GrabberVideo, SITES

logger = logging.getLogger(__name__)

_THUMB_W, _THUMB_H = 96, 54

_STYLE = """
    QDialog { background-color: #0a0a0f; color: #c8d3e0; }
    QLabel { color: #c8d3e0; }
    QListWidget { background-color: #0d1117; color: #c8d3e0; border: 1px solid #1a2a4a;
                  border-radius: 6px; selection-background-color: #1a2a4a; }
    QLineEdit, QComboBox { background-color: #0d1117; color: #c8d3e0; border: 1px solid #1a2a4a;
                           padding: 4px 8px; border-radius: 3px; }
    QLineEdit:focus, QComboBox:focus { border: 1px solid #2a7abf; }
    QPushButton { background-color: #111827; color: #c8d3e0; border: 1px solid #1a2a4a;
                  padding: 4px 12px; border-radius: 3px; }
    QPushButton:hover { background-color: #1a2a4a; border: 1px solid #2a7abf; color: #2a7abf; }
    QPushButton:disabled { color: #4a5568; }
    QPushButton#btn_accent { color: #2a7abf; font-weight: 600; }
    QCheckBox { color: #c8d3e0; }
    QCheckBox::indicator { width: 14px; height: 14px; }
    QSpinBox { background-color: #0d1117; color: #c8d3e0; border: 1px solid #1a2a4a;
               padding: 2px 6px; border-radius: 3px; }
    QLabel#status { color: #7a8aa0; }
    QLabel#status_error { color: #ff3366; }
"""


class SiteGrabberDialog(QDialog):
    """Keyword search on video sites; queue results as downloads."""

    _search_done = Signal(object)          # List[GrabberVideo] | Exception
    _thumb_loaded = Signal(str, bytes)     # watch url, image bytes
    _queue_progress = Signal(int, int, str)  # done, total, current code
    _queue_done = Signal(object)           # list[(GrabberVideo, job|Exception)]

    def __init__(self, config: Any, engine: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._engine = engine
        self._site = next(iter(SITES.values()))
        self._videos: List[GrabberVideo] = []
        self._query = ""
        self._page = 1
        self._busy = False
        self.setWindowTitle("Site Grabber")
        self.resize(780, 560)
        self.setStyleSheet(_STYLE)
        self._build_ui()
        self._search_done.connect(self._on_search_done)
        self._thumb_loaded.connect(self._on_thumb_loaded)
        self._queue_progress.connect(self._on_queue_progress)
        self._queue_done.connect(self._on_queue_done)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        search_row = QHBoxLayout()
        self.site_combo = QComboBox()
        for site in SITES.values():
            self.site_combo.addItem(site.label, site.key)
        self.site_combo.setToolTip("Site to search (more adapters can be added)")
        search_row.addWidget(self.site_combo)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search keywords, e.g. an id or actress…")
        self.search_input.returnPressed.connect(self._search)
        search_row.addWidget(self.search_input, 1)

        self.search_btn = QPushButton("Search")
        self.search_btn.setObjectName("btn_accent")
        self.search_btn.clicked.connect(self._search)
        search_row.addWidget(self.search_btn)

        # Auto-queue option: search results are resolved and queued without
        # a Download click (keywords in → downloads out). Opt-in, persisted.
        self.auto_queue_cb = QCheckBox("Auto-queue")
        self.auto_queue_cb.setToolTip(
            "Queue results automatically after every search — no Download "
            "click needed (capped by the limit on the right)")
        self.auto_queue_cb.toggled.connect(self._on_auto_toggled)
        search_row.addWidget(self.auto_queue_cb)

        self.auto_limit_spin = QSpinBox()
        self.auto_limit_spin.setRange(1, 50)
        self.auto_limit_spin.setSuffix(" max")
        self.auto_limit_spin.setToolTip("Max videos auto-queued per search/page")
        self.auto_limit_spin.valueChanged.connect(self._on_limit_changed)
        search_row.addWidget(self.auto_limit_spin)

        browser = getattr(self._config, "browser", None)
        # blockSignals: the initial state must not fire the toggled handlers
        # (results_list doesn't exist yet) nor rewrite the config.
        for widget, value in (
            (self.auto_queue_cb, bool(getattr(browser, "grabber_auto_queue", False))),
            (self.auto_limit_spin, int(getattr(browser, "grabber_auto_limit", 5) or 5)),
        ):
            widget.blockSignals(True)
            if widget is self.auto_queue_cb:
                widget.setChecked(value)
            else:
                widget.setValue(value)
            widget.blockSignals(False)

        layout.addLayout(search_row)

        self.status_label = QLabel("Enter keywords and press Search.")
        self.status_label.setObjectName("status")
        layout.addWidget(self.status_label)

        self.results_list = QListWidget()
        self.results_list.setUniformItemSizes(True)
        self.results_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.results_list.itemDoubleClicked.connect(
            lambda item: item.setCheckState(
                Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked))
        layout.addWidget(self.results_list, 1)

        list_row = QHBoxLayout()
        self.select_all_btn = QPushButton("Select all")
        self.select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        list_row.addWidget(self.select_all_btn)
        self.clear_sel_btn = QPushButton("Clear")
        self.clear_sel_btn.clicked.connect(lambda: self._set_all_checked(False))
        list_row.addWidget(self.clear_sel_btn)
        list_row.addStretch()
        self.prev_btn = QPushButton("← Prev page")
        self.prev_btn.clicked.connect(lambda: self._page_step(-1))
        self.prev_btn.setEnabled(False)
        list_row.addWidget(self.prev_btn)
        self.page_label = QLabel("page 1")
        list_row.addWidget(self.page_label)
        self.next_btn = QPushButton("Next page →")
        self.next_btn.clicked.connect(lambda: self._page_step(1))
        self.next_btn.setEnabled(False)
        list_row.addWidget(self.next_btn)
        layout.addLayout(list_row)

        bottom_row = QHBoxLayout()
        self.download_btn = QPushButton("Download selected")
        self.download_btn.setObjectName("btn_accent")
        self.download_btn.clicked.connect(
            lambda: self._download(self._selected_videos()))
        bottom_row.addWidget(self.download_btn)
        self.download_all_btn = QPushButton("Download this page")
        self.download_all_btn.clicked.connect(
            lambda: self._download(list(self._videos)))
        bottom_row.addWidget(self.download_all_btn)
        bottom_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        bottom_row.addWidget(close_btn)
        layout.addLayout(bottom_row)

    # ------------------------------------------------------------ searching
    def _search(self) -> None:
        query = self.search_input.text().strip()
        if not query or self._busy:
            return
        self._query = query
        self._page = 1
        self._run_search()

    def _page_step(self, delta: int) -> None:
        if self._busy or not self._query:
            return
        self._page = max(1, self._page + delta)
        self._run_search()

    def _run_search(self) -> None:
        site_key = self.site_combo.currentData()
        self._site = SITES.get(site_key, self._site)
        self._set_busy(True, f"Searching “{self._query}” on {self._site.label}…")
        self.results_list.clear()
        query, page, site = self._query, self._page, self._site

        def worker() -> None:
            try:
                videos = site.search(query, page)
            except Exception as exc:  # GrabberError or anything network-ish
                videos = exc
            try:
                self._search_done.emit(videos)
            except RuntimeError:
                pass  # dialog closed mid-search

        threading.Thread(target=worker, daemon=True).start()

    def _on_search_done(self, outcome: object) -> None:
        self._set_busy(False, "")
        if isinstance(outcome, Exception):
            self._status(str(outcome), error=True)
            return
        videos = list(outcome or [])
        self._videos = videos
        self._populate(videos)
        self._status(f"{len(videos)} results — page {self._page}")
        self.prev_btn.setEnabled(self._page > 1)
        self.next_btn.setEnabled(bool(videos))
        self.page_label.setText(f"page {self._page}")
        self._load_thumbnails(videos)
        self._maybe_auto_queue()

    # ------------------------------------------------------------ auto-queue
    def _on_auto_toggled(self, checked: bool) -> None:
        self._persist_auto_options()
        if checked:
            # Toggling on with results on screen queues them right away.
            self._maybe_auto_queue()

    def _on_limit_changed(self, _value: int) -> None:
        self._persist_auto_options()

    def _persist_auto_options(self) -> None:
        browser = getattr(self._config, "browser", None)
        if browser is not None:
            try:
                browser.grabber_auto_queue = self.auto_queue_cb.isChecked()
                browser.grabber_auto_limit = self.auto_limit_spin.value()
            except Exception:
                pass
        save = getattr(self.parent(), "_save_config", None)
        if callable(save):
            try:
                save()
            except Exception:
                pass

    def _auto_queue_candidates(self) -> List[GrabberVideo]:
        """Checked videos to auto-queue, capped by the limit.

        Only still-checked items are considered, so a re-trigger (toggle or
        re-search of the same page) picks up the next batch instead of
        duplicating already-queued videos."""
        if not self.auto_queue_cb.isChecked() or not self._videos:
            return []
        limit = max(1, self.auto_limit_spin.value())
        candidates: List[GrabberVideo] = []
        for row, video in enumerate(self._videos):
            if row >= self.results_list.count():
                break
            if self.results_list.item(row).checkState() == Qt.Checked:
                candidates.append(video)
                if len(candidates) >= limit:
                    break
        return candidates

    def _maybe_auto_queue(self) -> None:
        if self._busy:
            return
        candidates = self._auto_queue_candidates()
        if candidates:
            self._download(candidates)

    def _populate(self, videos: List[GrabberVideo]) -> None:
        self.results_list.clear()
        for video in videos:
            label = f"{video.code.upper()}  ·  {video.duration or '—'}  ·  {video.title}"
            item = QListWidgetItem(label)
            item.setCheckState(Qt.Checked)
            item.setToolTip(f"{video.title}\n{video.url}")
            item.setData(Qt.UserRole, video.url)
            self.results_list.addItem(item)

    def _load_thumbnails(self, videos: List[GrabberVideo]) -> None:
        targets = [(v.url, v.thumbnail) for v in videos if v.thumbnail]

        def worker() -> None:
            for url, thumb in targets:  # sequential — CDN friendly
                try:
                    from dlmgr import http_client
                    from dlmgr.extractors.missav import BROWSER_HEADERS
                    resp = http_client.get(
                        thumb, headers={"User-Agent": BROWSER_HEADERS["User-Agent"],
                                        "Referer": url},
                        timeout=15)
                    if resp.status_code == 200 and resp.content:
                        self._thumb_loaded.emit(url, resp.content)
                except RuntimeError:
                    return  # dialog closed — stop loading
                except Exception:
                    pass  # thumbnails are decorative — ignore failures

        threading.Thread(target=worker, daemon=True).start()

    def _on_thumb_loaded(self, url: str, data: bytes) -> None:
        for row in range(self.results_list.count()):
            item = self.results_list.item(row)
            if item.data(Qt.UserRole) == url:
                pixmap = QPixmap()
                if pixmap.loadFromData(data):
                    item.setIcon(pixmap.scaled(
                        _THUMB_W, _THUMB_H, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return

    # ------------------------------------------------------------ queueing
    def _selected_videos(self) -> List[GrabberVideo]:
        checked = []
        for row in range(self.results_list.count()):
            item = self.results_list.item(row)
            if item.checkState() == Qt.Checked:
                checked.append(self._videos[row])
        return checked

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.results_list.count()):
            self.results_list.item(row).setCheckState(state)

    def _download(self, videos: List[GrabberVideo]) -> None:
        if self._busy or not videos:
            return
        if self._engine is None:
            self._status("Download engine is not available.", error=True)
            return
        self._set_busy(True, "")
        engine, site = self._engine, self._site

        def worker() -> None:
            results = []
            for index, video in enumerate(videos, 1):
                try:
                    self._queue_progress.emit(index, len(videos), video.code)
                except RuntimeError:
                    return  # dialog closed — stop queueing
                try:
                    info = site.resolve(video.url)
                    job = engine.add_stream_job(
                        url=info["manifest_url"],
                        filename=f"{video.code}.mp4",
                        headers=info.get("headers") or {},
                        referrer=video.url,
                        source_url=video.url,
                    )
                    results.append((video, job))
                except Exception as exc:
                    logger.warning("Site grabber: resolve failed for %s: %s",
                                   video.url, exc)
                    results.append((video, exc))
            try:
                self._queue_done.emit(results)
            except RuntimeError:
                pass  # dialog closed mid-queue

        threading.Thread(target=worker, daemon=True).start()

    def _on_queue_progress(self, done: int, total: int, code: str) -> None:
        self._status(f"Resolving {done}/{total}: {code}…")

    def _on_queue_done(self, results: list) -> None:
        self._set_busy(False, "")
        queued = [pair for pair in results if not isinstance(pair[1], Exception)]
        failed = [pair for pair in results if isinstance(pair[1], Exception)]
        # Uncheck the queued ones; keep failures checked for a retry pass.
        queued_urls = {video.url for video, _ in queued}
        for row in range(self.results_list.count()):
            item = self.results_list.item(row)
            if item.data(Qt.UserRole) in queued_urls:
                item.setCheckState(Qt.Unchecked)
        message = f"Queued {len(queued)} download(s)"
        if failed:
            codes = ", ".join(video.code for video, _ in failed)
            message += f" — failed: {codes} ({failed[0][1]})"
        self._status(message, error=bool(failed))
        for video, job in queued:
            self._notify_window(f"🧲 Site Grabber queued {video.code}: {job.save_path}")
        if queued and not failed:
            self._notify_window_toast("Site Grabber", f"Queued {len(queued)} video(s)")

    # --------------------------------------------------------------- helpers
    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        for button in (self.search_btn, self.download_btn, self.download_all_btn,
                       self.prev_btn, self.next_btn):
            button.setEnabled(not busy)
        if message:
            self._status(message)

    def _status(self, text: str, error: bool = False) -> None:
        self.status_label.setObjectName("status_error" if error else "status")
        self.status_label.setText(text)
        # Re-apply the stylesheet so the objectName-driven color takes effect.
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _notify_window(self, text: str) -> None:
        append = getattr(self.parent(), "_append_event", None)
        if callable(append):
            try:
                append(text)
            except Exception:
                pass

    def _notify_window_toast(self, title: str, body: str) -> None:
        notify = getattr(self.parent(), "_notify", None)
        if callable(notify):
            try:
                notify(title, body)
            except Exception:
                pass
