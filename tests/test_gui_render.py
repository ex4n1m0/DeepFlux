"""Chat-render regression tests: long unbroken tokens (magnets, hashes, URLs)
must get zero-width-space break opportunities so result bubbles and tables
never overflow the chat viewport; HTML tags and entities must stay intact."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui.main_window import MainWindow, _break_long_tokens, _markdown_to_html


def test_long_token_gets_break_opportunities():
    magnet = "magnet:?xt=urn:btih:" + "a1" * 30
    out = _break_long_tokens(magnet)
    assert "\u200b" in out
    # The original token is recoverable by removing the break markers.
    assert out.replace("\u200b", "") == magnet


def test_entities_are_never_split():
    out = _break_long_tokens("x" * 30 + "&amp;" + "y" * 30)
    assert "&amp;" in out
    assert "&am\u200bp;" not in out


def test_tags_and_attributes_untouched():
    url = "u" * 60
    out = _break_long_tokens('<a href="' + url + '">label</a>')
    assert '<a href="' + url + '">' in out


def test_short_text_untouched():
    assert _break_long_tokens("hello world") == "hello world"


def test_format_tool_args_escapes_llm_controlled_values():
    # Regression: _format_tool_args used to reference an undefined `_html`
    # module (2026-11-06 review) — every tool_start event raised a NameError
    # that the event handler silently swallowed, killing the escape layer.
    fmt = MainWindow._format_tool_args  # stateless — safe to call unbound
    out = fmt(None, "web_search", {"query": '<img src=x onerror=alert(1)>'})
    assert "<img" not in out and "&lt;img" in out
    out = fmt(None, "search_indexers", {"query": 'magnet:?xt=urn:btih:" onclick="x'})
    assert '" onclick' not in out


def test_format_tool_args_handles_every_tool_without_raising():
    fmt = MainWindow._format_tool_args
    cases = [        ("web_search", {"query": "q"}),
        ("web_fetch", {"url": "https://example.com"}),
        ("search_indexers", {"query": "q"}),
        ("find_alt_trackers", {"torrent_name": "t"}),
        ("find_alt_release", {"torrent_name": "t"}),
        ("search_memory", {"query": "q"}),
        ("save_memory", {"content": "c"}),
        ("add_magnet", {"uri": "magnet:?xt=urn:btih:aa"}),
        ("add_torrent_file", {"path": "/p"}),
        ("diagnose_swarm", {"info_hash": "a" * 40}),
        ("get_torrent_status", {"info_hash": "a" * 40}),
        ("pause_torrent", {"info_hash": "a" * 40}),
        ("add_tracker", {"url": "u"}),
        ("list_directory", {"path": "/p"}),
        ("copy_path", {"source": "s", "destination": "d"}),
        ("rename_path", {"path": "/p", "new_name": "n"}),
        ("iptv_search", {"query": "q"}),
        ("iptv_play", {"query": "q"}),
        ("iptv_set_volume", {"level": 50}),
        ("browser_navigate", {"url": "u"}),
        ("browser_close_tab", {"index": 1}),
        ("browser_click", {"selector": "s"}),
        ("browser_fill", {"selector": "s", "value": "v"}),
        ("browser_scroll", {"direction": "down"}),
        ("browser_add_bookmark", {"url": "u"}),
        ("remove_download", {"job_id": "j"}),
        ("force_recheck", {"info_hash": "a" * 40}),
        ("set_torrent_rate_limits", {"download_kb": 10, "upload_kb": 5}),
        ("add_rss_feed", {"url": "u"}),
        ("iptv_add_source", {"name": "n"}),
        ("set_api_key", {"slot": "s", "value": "v"}),
        ("set_settings", {"path": "p", "value": "v"}),
        ("add_torrent_source", {"name": "n"}),
        ("run_shell", {"command": "echo hi"}),
        ("no_args_tool", {}),
        ("unknown_tool_xyz", {"a": "1", "b": "2"}),
    ]
    for tool, args in cases:
        s = fmt(None, tool, args)
        assert isinstance(s, str), tool


def test_markdown_table_with_magnet_is_wrappable():
    md = "| Name | Magnet |\n|---|---|\n| rel | `magnet:?xt=urn:btih:" + "b2" * 30 + "` |"
    out = _break_long_tokens(_markdown_to_html(md))
    assert "\u200b" in out
    assert "<table>" in out


def test_rss_and_sources_dialogs_construct_offscreen():
    # Regression 2026-11-06: these three dialogs had zero test references —
    # a constructor regression would only surface at runtime.
    from unittest.mock import MagicMock

    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.rss_dialog import RSSDialog
    from gui.rss_viewer import RSSViewer
    from gui.sources_dialog import SourcesDialog

    app = QApplication.instance() or QApplication([])
    cfg = DeeptorrentConfig()
    dlg = RSSDialog(cfg)
    dlg.close()
    viewer = RSSViewer(cfg, MagicMock())
    viewer.close()
    src = SourcesDialog(cfg)
    src.close()
    app.processEvents()


def test_iptv_source_edit_dialog_content_type_roundtrip():
    # Per-source content override (owner request 2026-11-22): the Content
    # dropdown loads an existing source's value and survives to_source();
    # new dialogs default to "auto" (detected).
    from PySide6.QtWidgets import QApplication
    from config import IPTVSourceConfig
    from gui.iptv_settings_dialog import _SourceEditDialog

    app = QApplication.instance() or QApplication([])
    src = IPTVSourceConfig(id="a", name="A", kind="m3u_url",
                           url="http://x/pl.m3u", content_type="vod")
    dlg = _SourceEditDialog(source=src)
    assert dlg.content_type.currentData() == "vod"
    assert dlg.to_source(existing=src).content_type == "vod"

    dlg2 = _SourceEditDialog()  # add-new: defaults to auto
    assert dlg2.content_type.currentData() == "auto"
    assert dlg2.to_source().content_type == "auto"

    dlg3 = _SourceEditDialog(source=IPTVSourceConfig(
        id="b", name="B", kind="m3u_url", url="http://y/pl.m3u"))
    assert dlg3.content_type.currentData() == "auto"
    dlg.close(); dlg2.close(); dlg3.close()
    app.processEvents()


def test_sources_dialog_save_preserves_provenance_fields():
    """Regression (2026-11-09 review, M2): _save_and_accept rebuilt every row
    from the five visible table columns, silently wiping origin/torznab_caps/
    popularity/categories — wiping origin made manual sources Jackett-queried
    and zombie-struck, and broke the sync's zombie auto-removal for jackett
    sources. Existing fields must survive an OK; brand-new rows are manual."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTableWidgetItem
    from config import DeeptorrentConfig, SourceConfig
    from gui.sources_dialog import SourcesDialog

    app = QApplication.instance() or QApplication([])
    cfg = DeeptorrentConfig()
    cfg.sources.sources = [
        SourceConfig(id="1337x", name="1337x", url="https://x.test", type="public",
                     enabled=True, origin="jackett", popularity=95,
                     torznab_caps=[2000, 2030], categories=["Movies"]),
        SourceConfig(id="manual-site", name="Manual", url="https://m.test", type="public",
                     enabled=False, origin="manual", popularity=5,
                     torznab_caps=[], categories=["TV"]),
    ]
    dlg = SourcesDialog(cfg)
    dlg._save_and_accept()
    app.processEvents()
    saved = {s.id: s for s in cfg.sources.sources}
    assert set(saved) == {"1337x", "manual-site"}
    j = saved["1337x"]
    assert j.origin == "jackett" and j.popularity == 95
    assert j.torznab_caps == [2000, 2030] and j.categories == ["Movies"]
    m = saved["manual-site"]
    assert m.origin == "manual" and not m.enabled and m.popularity == 5

    # a NEW row (id not previously known) becomes a manual source
    dlg2 = SourcesDialog(cfg)
    row = dlg2.table.rowCount()
    dlg2.table.insertRow(row)
    dlg2.table.setItem(row, 1, QTableWidgetItem("Fresh Site"))
    dlg2.table.setItem(row, 2, QTableWidgetItem("fresh-site"))
    dlg2.table.setItem(row, 3, QTableWidgetItem("public"))
    dlg2.table.setItem(row, 4, QTableWidgetItem("https://f.test"))
    dlg2._save_and_accept()
    app.processEvents()
    fresh = {s.id: s for s in cfg.sources.sources}["fresh-site"]
    assert fresh.origin == "manual"


def test_atomic_config_write_leaves_no_temp_and_replaces(tmp_path):
    from gui.main_window import _write_bytes_atomic
    p = str(tmp_path / "config.json")
    _write_bytes_atomic(p, b'{"a": 1}')
    assert (tmp_path / "config.json").read_bytes() == b'{"a": 1}'
    assert not (tmp_path / "config.json.importing").exists()
    _write_bytes_atomic(p, b'{"b": 2}')
    assert (tmp_path / "config.json").read_bytes() == b'{"b": 2}'
    assert not (tmp_path / "config.json.importing").exists()


def test_reload_config_in_place_keeps_identity(tmp_path):
    from config import DeeptorrentConfig
    from gui.main_window import _reload_config_in_place
    cfg = DeeptorrentConfig()
    ident = id(cfg)
    p = str(tmp_path / "config.json")
    cfg.download.default_folder = "C:\\Imported"
    cfg.to_file(p)
    cfg.download.default_folder = "C:\\Stale"  # stale in-memory value
    _reload_config_in_place(cfg, p)
    assert id(cfg) == ident  # holders of the reference stay valid
    assert cfg.download.default_folder == "C:\\Imported"


def test_settings_menu_pages_fit_low_resolution():
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.settings_dialog import (
        API_KEY_PAGES,
        BROWSER_SETTINGS_PAGES,
        DOWNLOAD_SETTINGS_PAGES,
        APIKeysDialog,
        BrowserSettingsDialog,
        DownloadsSettingsDialog,
    )
    from gui.window_sizing import roomy

    app = QApplication.instance() or QApplication([])
    config = DeeptorrentConfig()
    groups = (
        (DownloadsSettingsDialog, DOWNLOAD_SETTINGS_PAGES),
        (BrowserSettingsDialog, BROWSER_SETTINGS_PAGES),
        (APIKeysDialog, API_KEY_PAGES),
    )
    for dialog_class, pages in groups:
        for _label, page in pages:
            dialog = dialog_class(config, page=page)
            dialog.show()
            app.processEvents()
            # Policy (2026-09-10): dialogs open roomy — content sizeHint +
            # 15% headroom, never above 90% of the available screen.
            screen = dialog.screen() or app.primaryScreen()
            avail = screen.availableGeometry()
            assert dialog.width() <= round(avail.width() * 0.9) + 1
            assert dialog.height() <= round(avail.height() * 0.9) + 1
            assert dialog.width() >= dialog.minimumSizeHint().width()
            dialog.close()


def test_roomy_sizes_content_with_headroom_and_caps_at_90pct():
    """roomy(): content + 15% headroom on roomy screens; capped at 90% of
    the available geometry on small ones; refit also on first show so
    subclass content added after super().__init__() is accounted for."""
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import (QApplication, QDialog, QLabel, QListWidget,
                                   QVBoxLayout)

    app = QApplication.instance() or QApplication([])
    from gui.window_sizing import roomy

    # Roomy screen: window grows past the raw content size (headroom) and
    # never exceeds 90% of the screen — and sits top-center.
    dlg = QDialog()
    QVBoxLayout(dlg).addWidget(QLabel("short content"))
    roomy(dlg, avail=QRect(0, 0, 1920, 1080))
    assert dlg.width() >= dlg.sizeHint().width()
    assert dlg.width() <= round(1920 * 0.9)
    assert dlg.height() >= dlg.sizeHint().height()
    assert dlg.x() == (1920 - dlg.width()) // 2
    assert dlg.y() == round(1080 * 0.05)
    dlg.deleteLater()

    # Deceptively small content hint (a bare QListWidget hints ~280px no
    # matter how many rows): the floor keeps the window substantial.
    dlg = QDialog()
    QVBoxLayout(dlg).addWidget(QListWidget())
    roomy(dlg, avail=QRect(0, 0, 1920, 1080))
    assert dlg.width() >= round(1920 * 0.5)
    assert dlg.height() >= round(1080 * 0.5)
    dlg.deleteLater()

    # Small screen: hard cap at 90% even when content wants more; still
    # top-centered within the 90% region.
    dlg = QDialog()
    QVBoxLayout(dlg).addWidget(QLabel("x" * 400))
    roomy(dlg, avail=QRect(0, 0, 800, 600))
    assert dlg.width() == round(800 * 0.9)
    assert dlg.height() <= round(600 * 0.9)
    assert dlg.x() == (800 - dlg.width()) // 2
    assert dlg.y() == round(600 * 0.05)
    dlg.deleteLater()

    # First-show refit: content added AFTER roomy() was called is fitted.
    dlg = QDialog()
    roomy(dlg, avail=QRect(0, 0, 1920, 1080))
    small = dlg.width()
    QVBoxLayout(dlg).addWidget(QLabel("y" * 400))
    dlg.show()
    app.processEvents()
    assert dlg.width() > small
    dlg.close()
    dlg.deleteLater()


def test_download_settings_pages_save_only_their_section():
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.settings_dialog import DownloadsSettingsDialog

    QApplication.instance() or QApplication([])
    config = DeeptorrentConfig()
    original_path = config.default_save_path
    original_concurrent = config.download.max_concurrent
    dialog = DownloadsSettingsDialog(config, page="queue")
    dialog.tor_max_downloading.setValue(17)
    dialog.save_path.setText("must-not-save")
    dialog.dm_max_concurrent.setValue(original_concurrent + 1)
    dialog._save_and_accept()
    assert config.torrents.max_downloading_torrents == 17
    assert config.default_save_path == original_path
    assert config.download.max_concurrent == original_concurrent


def test_api_and_browser_pages_preserve_hidden_settings():
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.settings_dialog import APIKeysDialog, BrowserSettingsDialog

    QApplication.instance() or QApplication([])
    config = DeeptorrentConfig()
    config.llm.api_key = "original-llm-key"
    api_dialog = APIKeysDialog(config, page="adult")
    api_dialog.llm_key.setText("must-not-save")
    api_dialog.tpdb_key.setText("new-tpdb-key")
    api_dialog._save_and_accept()
    assert config.llm.api_key == "original-llm-key"
    assert config.iptv.tpdb_api_key == "new-tpdb-key"

    config.browser.homepage = "https://example.com/"
    browser_dialog = BrowserSettingsDialog(config, page="privacy")
    browser_dialog.browser_homepage.setText("invalid hidden URL")
    browser_dialog.browser_history_days.setValue(45)
    browser_dialog._save_and_accept()
    assert config.browser.homepage == "https://example.com/"
    assert config.browser.history_retention_days == 45


def test_effort_dropdown_roundtrip_offscreen():
    """Reasoning-effort dropdown (2026-11-21): loads the config value, saves
    the pick, stays honest about values outside the four offered levels
    (agent-set "medium"), and is gated by provider — cloud presets always,
    custom only with its checkbox, mirroring DeepSeekClient's
    send_reasoning_effort."""
    from PySide6.QtWidgets import QApplication
    from config import DeeptorrentConfig
    from gui.settings_dialog import APIKeysDialog

    app = QApplication.instance() or QApplication([])
    cfg = DeeptorrentConfig()

    dlg = APIKeysDialog(cfg, page="ai")
    # Default provider (venice) sends an effort → dropdown live, showing low.
    assert dlg.llm_effort.isEnabled()
    assert dlg.llm_effort.currentData() == "low"

    dlg.llm_effort.setCurrentIndex(dlg.llm_effort.findData("high"))
    dlg._save_and_accept()
    assert cfg.llm.reasoning_effort == "high"
    dlg.close()
    dlg.deleteLater()
    app.processEvents()

    # Reopening reflects the saved pick; an out-of-list value (agent-set
    # "medium") appears as its own entry instead of lying "Low".
    dlg2 = APIKeysDialog(cfg, page="ai")
    assert dlg2.llm_effort.currentData() == "high"
    dlg2.close()
    dlg2.deleteLater()
    app.processEvents()

    cfg.llm.reasoning_effort = "medium"
    dlg3 = APIKeysDialog(cfg, page="ai")
    assert dlg3.llm_effort.currentData() == "medium"

    # Custom endpoint: no effort is sent until the checkbox is ticked.
    dlg3.llm_endpoint.setCurrentIndex(dlg3.llm_endpoint.findData("custom"))
    assert not dlg3.llm_effort.isEnabled()
    dlg3.llm_reasoning_effort.setChecked(True)
    assert dlg3.llm_effort.isEnabled()
    dlg3.llm_reasoning_effort.setChecked(False)
    assert not dlg3.llm_effort.isEnabled()
    dlg3.close()
    dlg3.deleteLater()
    app.processEvents()


# ---------------------------------------------------------------------------
# Agent task sessions sidebar (2026-11-22): auto-saved local conversations
# ---------------------------------------------------------------------------
# These build the REAL MainWindow (same precedent as tests/test_mini_player)
# with agent.chat faked at the instance seam — no LLM, no network. The
# autouse conftest fixtures redirect both the config path and
# DF_AGENT_SESSIONS, so nothing here can touch the user's real files.

def _qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _build_main(tmp_path):
    from config import DeeptorrentConfig
    cfg = DeeptorrentConfig()
    cfg.stats.ping_enabled = False
    # Keep the startup ytdlp probe out of the test process — its worker
    # thread imports yt_dlp (heavy) concurrently with test collection, and
    # that import raced the d–g chunk's heap layout into native AVs on
    # 2026-11-22 (innocent _compile_bytecode faulting frame). Same
    # no-side-effect rule as the telemetry switch above.
    cfg.download.youtube_update_check = False
    cfg_path = str(tmp_path / "config.json")
    cfg.to_file(cfg_path)
    mw = MainWindow(cfg_path)
    return mw


def _fake_agent(mw):
    """Instance-level chat seam: appends like the real loop and answers."""
    def chat(text):
        mw.agent.history.append({"role": "user", "content": text})
        reply = f"done: {text}"
        mw.agent.history.append({"role": "assistant", "content": reply})
        return {"role": "assistant", "content": reply, "tool_calls": []}
    mw.agent.chat = chat
    return chat


def _wait_quiet(app, mw, timeout=15.0):
    """Pump events until the worker thread is done AND its queued finished
    signal has been delivered (busy flag back down)."""
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        app.processEvents()
        thread = getattr(mw, "_agent_thread", None)
        if (thread is None or not thread.is_alive()) and not getattr(mw, "_agent_busy", False) \
                and not getattr(mw, "_search_busy", False):
            break
        _time.sleep(0.01)
    for _ in range(10):
        app.processEvents()


def _send(app, mw, text):
    mw.chat_input.setText(text)
    mw._on_send()
    _wait_quiet(app, mw)


def test_send_autosaves_task_with_history_and_transcript(tmp_path):
    from agent.sessions import auto_name
    app = _qapp()
    mw = _build_main(tmp_path)
    _fake_agent(mw)
    _send(app, mw, "grab ubuntu iso")
    app.processEvents()

    metas = mw._sessions.list_sessions()
    assert len(metas) == 1
    assert metas[0].name == auto_name("grab ubuntu iso")
    data = mw._sessions.load(metas[0].id)
    roles = [m["role"] for m in data.messages]
    assert roles == ["user", "assistant"]
    kinds = [e["k"] for e in data.transcript]
    assert "user" in kinds and "agent" in kinds
    # The sidebar lists it and highlights it as the open task.
    assert mw._sessions_bar.current_session_id() == metas[0].id == mw._session_id


def test_send_persists_user_message_mid_turn(tmp_path):
    """Crash-safety: the save issued right after _append_user carries the
    outgoing message even though agent.chat logs it on the worker thread."""
    import threading
    app = _qapp()
    mw = _build_main(tmp_path)
    release = threading.Event()

    def chat(text):
        release.wait(10)
        mw.agent.history.append({"role": "user", "content": text})
        mw.agent.history.append({"role": "assistant", "content": "late"})
        return {"role": "assistant", "content": "late", "tool_calls": []}
    mw.agent.chat = chat

    mw.chat_input.setText("mid-turn question")
    mw._on_send()
    # Wait for the pre-turn save (thread still blocked).
    import time as _time
    deadline = _time.time() + 10
    while _time.time() < deadline and not mw._sessions.list_sessions():
        app.processEvents()
        _time.sleep(0.01)
    metas = mw._sessions.list_sessions()
    assert len(metas) == 1
    data = mw._sessions.load(metas[0].id)
    assert [m["role"] for m in data.messages] == ["user"]
    assert [e["k"] for e in data.transcript] == ["user"]

    release.set()
    _wait_quiet(app, mw)
    data = mw._sessions.load(metas[0].id)
    assert [m["role"] for m in data.messages] == ["user", "assistant"]


def test_boot_is_blank_but_sidebar_lists_saved_tasks(tmp_path):
    """Owner decision 2026-11-22: launch starts a BLANK chat; saved tasks
    open only on click. Seeds the store BEFORE the window exists (the
    previous app run's tasks), so this one builds its own MainWindow."""
    from agent.sessions import SessionStore, new_session_id
    app = _qapp()
    store = SessionStore()  # DF_AGENT_SESSIONS (conftest) → per-test tmp
    sid = new_session_id()
    store.save(sid, "earlier task",
               [{"role": "user", "content": "hi"}],
               [{"k": "user", "t": "hi"}])
    mw = _build_main(tmp_path)
    assert mw._session_id == ""
    metas = mw._sessions.list_sessions()
    assert [m.id for m in metas] == [sid]
    assert mw._sessions_bar.current_session_id() == ""
    # Blank look: onboarding visible, logo behind the chat.
    assert mw._onboarding.isVisibleTo(mw._agents_tab)
    assert mw.chat_history.toPlainText().strip() == ""


def test_switch_task_restores_transcript_and_llm_history(tmp_path):
    app = _qapp()
    mw = _build_main(tmp_path)
    _fake_agent(mw)
    _send(app, mw, "first question")
    first_id = mw._session_id
    mw._clear_chat()  # detach; first task stays on disk
    _send(app, mw, "second question")
    second_id = mw._session_id
    assert first_id != second_id

    # Reopen the first task through the sidebar's selected signal.
    mw._sessions_bar.selected.emit(first_id)
    app.processEvents()
    assert mw._session_id == first_id
    assert [m["content"] for m in mw.agent.history] == ["first question", "done: first question"]
    assert "first question" in mw.chat_history.toPlainText()
    assert "second question" not in mw.chat_history.toPlainText()
    assert not mw._onboarding.isVisibleTo(mw._agents_tab)
    assert mw._sessions_bar.current_session_id() == first_id
    # The agent panel mirror shows only the NEW conversation.
    assert "second question" not in mw.agent_panel._view.toPlainText()


def test_new_task_keeps_old_one_listed_and_starts_fresh_id(tmp_path):
    app = _qapp()
    mw = _build_main(tmp_path)
    _fake_agent(mw)
    _send(app, mw, "original task")
    old_id = mw._session_id

    mw._on_task_new()
    app.processEvents()
    assert mw._session_id == ""
    assert mw.chat_history.toPlainText().strip() == ""
    ids = [m.id for m in mw._sessions.list_sessions()]
    assert ids == [old_id]  # still saved, nothing deleted

    _send(app, mw, "fresh task")
    assert mw._session_id and mw._session_id != old_id
    assert {m.id for m in mw._sessions.list_sessions()} == {old_id, mw._session_id}


def test_rename_task_updates_store_and_open_task(tmp_path):
    app = _qapp()
    mw = _build_main(tmp_path)
    _fake_agent(mw)
    _send(app, mw, "name me")
    sid = mw._session_id

    from PySide6.QtWidgets import QInputDialog
    orig = QInputDialog.getText
    QInputDialog.getText = staticmethod(
        lambda parent, title, label, text="": ("My custom name", True))
    try:
        mw._on_task_rename(sid)
    finally:
        QInputDialog.getText = orig
    app.processEvents()
    assert mw._sessions.load(sid).name == "My custom name"
    assert mw._session_name == "My custom name"
    assert mw._sessions_bar.current_session_id() == sid


def test_delete_confirmed_removes_task_and_blanks_open_chat(tmp_path):
    from PySide6.QtWidgets import QMessageBox
    app = _qapp()
    mw = _build_main(tmp_path)
    _fake_agent(mw)
    _send(app, mw, "delete me")
    sid = mw._session_id
    assert sid

    orig = QMessageBox.question
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.Yes)
    try:
        mw._on_task_delete(sid)
    finally:
        QMessageBox.question = orig
    app.processEvents()
    assert mw._sessions.load(sid) is None
    assert [m.id for m in mw._sessions.list_sessions()] == []
    assert mw._session_id == ""
    assert mw.chat_history.toPlainText().strip() == ""


def test_delete_declined_keeps_task(tmp_path):
    from PySide6.QtWidgets import QMessageBox
    app = _qapp()
    mw = _build_main(tmp_path)
    _fake_agent(mw)
    _send(app, mw, "keep me")
    sid = mw._session_id

    orig = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    try:
        mw._on_task_delete(sid)
    finally:
        QMessageBox.question = orig
    assert mw._sessions.load(sid) is not None
    assert mw._session_id == sid


def test_busy_agent_disables_sidebar_interactions(tmp_path):
    app = _qapp()
    mw = _build_main(tmp_path)
    assert mw._sessions_bar._new_btn.isEnabled()

    mw._set_busy(True)
    app.processEvents()
    assert not mw._sessions_bar._new_btn.isEnabled()
    assert not mw._sessions_bar._list.isEnabled()
    # Guarded actions are no-ops while busy (no save/switch side effects).
    mw._on_task_new()
    assert mw._session_id == ""

    mw._set_busy(False)
    app.processEvents()
    assert mw._sessions_bar._new_btn.isEnabled()


def test_search_sweep_creates_task_with_search_transcript(tmp_path):
    app = _qapp()
    mw = _build_main(tmp_path)
    mw.tools._web_search.search = lambda q, limit=20: {
        "answer": "", "results": [{"url": "http://x/1", "name": "Result One"}]}
    mw.chat_input.setText("sweep query")
    mw._on_search_send()
    _wait_quiet(app, mw)
    app.processEvents()

    metas = mw._sessions.list_sessions()
    assert len(metas) == 1
    assert metas[0].name == "sweep query"
    data = mw._sessions.load(metas[0].id)
    kinds = [e["k"] for e in data.transcript]
    assert kinds[0] == "search_user"
    assert "search_html" in kinds
    # A web sweep carries no LLM context — messages stay empty.
    assert data.messages == []
    assert "Result One" in mw.chat_history.toPlainText()


def test_task_delegate_sizehint_is_native_safe():
    """Regression (2026-11-22): QSize.expandedTo has no (int, int) overload
    in PySide6 — the TypeError escaping the delegate's sizeHint SEGFAULTED
    the process natively during item-view layout (bare-list bisect; no
    Python traceback ever printed). Showing a delegated list with two-line
    items must stay native-clean."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidget, QListWidgetItem
    from gui.agent_sessions_bar import AgentSessionsBar, _TaskDelegate, _PREVIEW_ROLE
    app = _qapp()
    lw = QListWidget()
    lw.setItemDelegate(_TaskDelegate(lw))
    item = QListWidgetItem("task name")
    item.setData(_PREVIEW_ROLE, "preview line")
    lw.addItem(item)
    lw.show()
    app.processEvents()
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtWidgets import QStyleOptionViewItem
    delegate = lw.itemDelegate()
    hint = delegate.sizeHint(QStyleOptionViewItem(), lw.model().index(0, 0))
    assert hint.height() >= 40
    # Metrics-driven two-line layout (owner screenshot 2026-11-23: the old
    # fixed half/half rects collided at larger scales): the row must be at
    # least as tall as BOTH real font lines plus padding.
    opt = QStyleOptionViewItem()
    name_font, preview_font = delegate._row_fonts(opt)
    expected = (QFontMetrics(name_font).height() + delegate._GAP
                + QFontMetrics(preview_font).height() + 2 * delegate._PAD)
    assert hint.height() >= expected
    # Paint smoke: a real render pass must complete natively (the
    # double-print fix blanks opt.text before drawControl).
    pix = lw.grab()
    assert not pix.isNull()
    bar = AgentSessionsBar()
    bar.show()
    app.processEvents()


def test_live_search_thread_locks_task_switching(tmp_path):
    """Review MED 2026-11-22: a sweep past its 60s watchdog clears
    _search_busy while its worker is still alive — task switching must
    stay locked or the late results would land in (and be saved into)
    the WRONG task's file."""
    import threading
    import time as _time
    app = _qapp()
    mw = _build_main(tmp_path)
    _fake_agent(mw)
    _send(app, mw, "task A message")

    done = threading.Event()
    mw._search_thread = threading.Thread(target=lambda: done.wait(5), daemon=True)
    mw._search_thread.start()
    mw._search_busy = False  # simulate the watchdog having fired
    try:
        assert mw._tasks_locked() is True  # the live thread still locks
        mw._on_task_new()                  # guarded → no detach
        assert mw._session_id != ""
    finally:
        done.set()
        mw._search_thread.join(5)
    assert mw._tasks_locked() is False


def test_busy_flags_combine_for_sidebar_gating(tmp_path):
    """A sweep ending must not re-enable the sidebar mid-agent-turn (and
    vice versa) — both flags are evaluated on every gate flip."""
    app = _qapp()
    mw = _build_main(tmp_path)
    mw._set_busy(True)
    mw._set_search_busy(True)
    app.processEvents()
    assert not mw._sessions_bar._new_btn.isEnabled()
    mw._set_search_busy(False)  # sweep done, agent still working
    app.processEvents()
    assert not mw._sessions_bar._new_btn.isEnabled()
    mw._set_busy(False)
    app.processEvents()
    assert mw._sessions_bar._new_btn.isEnabled()


def test_task_bar_opens_on_enter_key(tmp_path):
    """Keyboard activation (found live 2026-11-22): a selected row must
    open on Enter/double-click too — itemClicked alone is mouse-only and
    strands keyboard users and accessibility Invoke."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from gui.agent_sessions_bar import AgentSessionsBar
    from agent.sessions import SessionStore, new_session_id
    app = _qapp()
    store = SessionStore(str(tmp_path / "s"))
    sid = new_session_id()
    store.save(sid, "t", [], [{"k": "user", "t": "hi"}])
    bar = AgentSessionsBar()
    bar.set_sessions(store.list_sessions())
    bar._list.setCurrentRow(0)
    opened = []
    bar.selected.connect(opened.append)
    QTest.keyClick(bar._list, Qt.Key.Key_Return)
    assert opened == [sid]


# ---------------------------------------------------------------------------
# In-app yt-dlp update (2026-11-25): the Agent-tab notice link, its click
# path, and the Download Manager settings row. Build-and-forget like the
# tests above — no MainWindow close teardown here (see AGENTS.md).
# ---------------------------------------------------------------------------

def _ytdlp_info(version="2099.1.1"):
    from infra import ytdlp_overlay as ov
    return ov.YtdlpUpdateInfo(
        version=version, file=f"yt_dlp-{version}-py3-none-any.whl", size=3_000_000,
        sha256="ab" * 32,
        url=f"https://files.pythonhosted.org/packages/aa/bb/{'c' * 64}/"
            f"yt_dlp-{version}-py3-none-any.whl")


def _fake_installed_release(version="2099.1.1", sha="ab" * 32):
    """Lay out an installed release the way infra/ytdlp_overlay.install does."""
    from infra import ytdlp_overlay as ov
    root = ov.overlay_dir()
    release = root / "versions" / f"{version}-{sha[:8]}"
    (release / "yt_dlp").mkdir(parents=True, exist_ok=True)
    (release / "yt_dlp" / "__init__.py").write_text(
        f"__version__ = {version!r}\n", encoding="utf-8")
    (release / "yt_dlp" / "version.py").write_text(
        f"__version__ = {version!r}\n", encoding="utf-8")
    ov.write_json_atomic(release / "manifest.json", {"version": version, "sha256": sha})
    ov.write_json_atomic(root / ov.POINTER_NAME,
                         {"version": version, "sha256": sha, "dir": release.name})
    return release


def test_ytdlp_notice_carries_an_escaped_one_click_link(tmp_path, monkeypatch):
    mw = _build_main(tmp_path)
    inserted = []
    real_insert = mw._insert_html
    monkeypatch.setattr(mw, "_insert_html",
                        lambda html: inserted.append(html) or real_insert(html))
    mw._on_ytdlp_update_event({"installed": "2026.10.26", "latest": "2026.11.23",
                               "outdated": True, "frozen": True, "can_update": True,
                               "size": 3186416, "notice": True})
    notice = next(h for h in inserted if "deepflux-action" in h)
    assert 'href="deepflux-action:ytdlp-update"' in notice
    assert "<u>" in notice                       # chat anchors are styled as plain text
    assert "2026.11.23" in notice
    # the rendered document carries the link as well (Qt re-serialises <u>
    # into a span, so the underline itself is only pinned on the source HTML)
    doc = mw.chat_history.toHtml()
    assert "deepflux-action:ytdlp-update" in doc and "2026.11.23" in doc
    # A hostile version string must never become markup in the chat document.
    mw._ytdlp_notice_shown = False
    inserted.clear()
    mw._on_ytdlp_update_event({"installed": "1.0", "latest": "<script>x</script>",
                               "can_update": True, "notice": True})
    hostile = "".join(inserted)
    assert "<script>" not in hostile and "&lt;script&gt;" in hostile
    assert "<script>" not in mw.chat_history.toHtml()


def test_ytdlp_notice_is_once_per_session_and_honours_the_notice_flag(tmp_path):
    mw = _build_main(tmp_path)
    event = {"installed": "1.0", "latest": "2.0", "can_update": True, "notice": True}
    mw._on_ytdlp_update_event(event)
    first = mw.chat_history.toHtml()
    mw._on_ytdlp_update_event(event)
    assert mw.chat_history.toHtml() == first            # one notice per session
    mw2 = _build_main(tmp_path)
    mw2._on_ytdlp_update_event({**event, "notice": False})
    assert "deepflux-action" not in mw2.chat_history.toHtml()
    # automatic-mode outcomes are reported even with the warning switched off
    mw2._on_ytdlp_update_event({"installed": "1.0", "latest": "2.0", "updated": "2.0",
                                "notice": False})
    assert "updated to yt-dlp 2.0" in mw2.chat_history.toPlainText()


def test_ytdlp_link_click_checks_and_leaves_other_links_alone(tmp_path, monkeypatch):
    from PySide6.QtCore import QUrl
    mw = _build_main(tmp_path)
    calls = []
    monkeypatch.setattr(mw._ytdlp_runner, "check", lambda: calls.append("check") or True)
    monkeypatch.setattr(mw, "_browser_open_internal", lambda url: calls.append(("open", url)))
    mw._on_chat_link_clicked(QUrl("deepflux-action:ytdlp-update"))
    assert calls == ["check"]
    mw._on_chat_link_clicked(QUrl("https://example.com/x"))
    assert calls[-1] == ("open", "https://example.com/x")
    # exact match only: a lookalike is not an action link
    mw._on_chat_link_clicked(QUrl("deepflux-action:ytdlp-update-evil"))
    assert calls[-1] == ("open", "deepflux-action:ytdlp-update-evil")


def test_ytdlp_found_asks_before_installing(tmp_path, monkeypatch):
    import gui.main_window as mw_module
    mw = _build_main(tmp_path)
    info = _ytdlp_info()
    installed = []
    monkeypatch.setattr(mw._ytdlp_runner, "install", lambda i: installed.append(i) or True)
    monkeypatch.setattr(mw_module, "ask_to_install", lambda parent, i: False)
    mw._on_ytdlp_found(info)
    assert installed == []                       # declined: nothing is downloaded
    monkeypatch.setattr(mw_module, "ask_to_install", lambda parent, i: True)
    mw._on_ytdlp_found(info)
    assert installed == [info]
    assert mw._ytdlp_wait_cursor is True
    mw._ytdlp_clear_wait_cursor()
    assert mw._ytdlp_wait_cursor is False


def test_ytdlp_found_without_a_release_only_informs(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    mw = _build_main(tmp_path)
    shown = []
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(a[1:]) or 0))
    mw._on_ytdlp_found(None)
    assert len(shown) == 1


def test_ytdlp_installed_offers_a_restart_that_keeps_the_config(tmp_path, monkeypatch):
    import gui.main_window as mw_module
    mw = _build_main(tmp_path)
    monkeypatch.setattr(mw_module, "show_installed", lambda parent, version: True)
    restarted = []
    monkeypatch.setattr(mw, "_restart_app",
                        lambda skip_config_save=True: restarted.append(skip_config_save))
    mw._on_ytdlp_update_installed("2099.1.1")
    assert restarted == [False]


def test_restart_app_never_launches_when_the_close_is_declined(tmp_path, monkeypatch):
    """A declined active-transfers prompt must not leave two DeepFlux windows."""
    from PySide6.QtCore import QProcess
    mw = _build_main(tmp_path)
    launched = []
    monkeypatch.setattr(QProcess, "startDetached",
                        staticmethod(lambda *a, **k: launched.append(a) or True))
    monkeypatch.setattr(mw, "close", lambda: False)
    mw._restart_app()
    assert launched == []
    monkeypatch.setattr(mw, "close", lambda: True)
    mw._restart_app(skip_config_save=False)
    assert len(launched) == 1
    assert mw._skip_config_save is False


def test_settings_ytdlp_controls_round_trip(tmp_path):
    from config import DeeptorrentConfig
    from gui.settings_dialog import DownloadsSettingsDialog
    _qapp()
    config = DeeptorrentConfig()
    config.download.ytdlp_auto_update = True
    dialog = DownloadsSettingsDialog(config, page="manager")
    assert dialog.dm_ytdlp_auto.isChecked() is True
    # a source checkout cannot self-update: explanatory text, no buttons
    assert dialog.dm_ytdlp_check.isHidden() and dialog.dm_ytdlp_revert.isHidden()
    assert "installed Windows and Linux builds" in dialog.dm_ytdlp_status.text()
    dialog.dm_ytdlp_auto.setChecked(False)
    dialog._save_and_accept()
    assert config.download.ytdlp_auto_update is False


def test_settings_ytdlp_status_and_revert(tmp_path, monkeypatch):
    from config import DeeptorrentConfig
    from gui.settings_dialog import DownloadsSettingsDialog
    import gui.settings_dialog as sd_module
    from infra import ytdlp_overlay as ov
    _qapp()
    monkeypatch.setattr(ov, "supported", lambda: True)
    monkeypatch.setattr(ov, "bundled_version", lambda: "2000.1.1")
    _fake_installed_release()
    dialog = DownloadsSettingsDialog(DeeptorrentConfig(), page="manager")
    assert "2099.1.1" in dialog.dm_ytdlp_status.text()
    assert "updated by DeepFlux" in dialog.dm_ytdlp_status.text()
    assert dialog.dm_ytdlp_revert.isHidden() is False
    monkeypatch.setattr(sd_module, "ask_to_revert", lambda parent: True)
    dialog._ytdlp_revert()
    assert not (ov.overlay_dir() / ov.POINTER_NAME).exists()
    assert ov.skipped()["stage"] == "user"
    assert "bundled downloader is used from the next start" in dialog.dm_ytdlp_status.text()


def test_settings_ytdlp_check_enables_update_only_when_a_release_is_found(tmp_path, monkeypatch):
    from config import DeeptorrentConfig
    from gui.settings_dialog import DownloadsSettingsDialog
    _qapp()
    dialog = DownloadsSettingsDialog(DeeptorrentConfig(), page="manager")
    assert dialog.dm_ytdlp_update.isEnabled() is False
    dialog._on_ytdlp_found(_ytdlp_info())
    assert dialog.dm_ytdlp_update.isEnabled() is True
    assert "2099.1.1" in dialog.dm_ytdlp_status.text()
    dialog._on_ytdlp_found(None)
    assert dialog.dm_ytdlp_update.isEnabled() is False
    assert "up to date" in dialog.dm_ytdlp_status.text()


def test_settings_ytdlp_reports_failure_and_success(tmp_path, monkeypatch):
    """The dialog's worker outcomes: a failed CHECK only updates the status
    line, a failed INSTALL also raises the dialog; both re-enable the buttons."""
    from config import DeeptorrentConfig
    from gui.settings_dialog import DownloadsSettingsDialog
    import gui.settings_dialog as sd_module
    _qapp()
    dialog = DownloadsSettingsDialog(DeeptorrentConfig(), page="manager")
    dialog._ytdlp_pending = "check"
    dialog.dm_ytdlp_check.setEnabled(False)
    dialog._on_ytdlp_failed("feed unreachable")
    assert "The update failed: feed unreachable" in dialog.dm_ytdlp_status.text()
    assert dialog.dm_ytdlp_check.isEnabled() is True
    shown = []
    monkeypatch.setattr(sd_module, "show_failed", lambda parent, message: shown.append(message))
    dialog._ytdlp_pending = "install"
    dialog._on_ytdlp_failed("integrity check failed")
    assert shown == ["integrity check failed"]
    dialog._on_ytdlp_installed("2099.1.1")
    assert "2099.1.1 is installed" in dialog.dm_ytdlp_status.text()
    assert "restart DeepFlux" in dialog.dm_ytdlp_status.text()
    assert dialog.dm_ytdlp_update.isEnabled() is False


def test_runner_install_forces_a_recorded_release(tmp_path, monkeypatch):
    """The user's own click must be able to retry a release the automatic
    path refuses (install() rejects a recorded release unless force=True)."""
    import time
    from gui.ytdlp_update_runner import YtdlpUpdateRunner
    from infra import ytdlp_overlay as ov
    app = _qapp()
    calls = []
    monkeypatch.setattr(ov, "install",
                        lambda info, **kw: calls.append(kw) or {"version": info.version})
    runner = YtdlpUpdateRunner()
    done = []
    runner.installed.connect(done.append)
    assert runner.install(_ytdlp_info()) is True
    for _ in range(300):
        if calls:
            break
        time.sleep(0.01)
    assert calls and calls[0].get("force") is True
    app.processEvents()
    assert done == ["2099.1.1"]
    assert runner.busy is False


def test_main_window_update_failure_clears_the_wait_cursor(tmp_path, monkeypatch):
    import gui.main_window as mw_module
    from PySide6.QtWidgets import QApplication
    mw = _build_main(tmp_path)
    shown = []
    monkeypatch.setattr(mw_module, "show_failed", lambda parent, message: shown.append(message))
    cleared = []
    monkeypatch.setattr(QApplication, "restoreOverrideCursor",
                        staticmethod(lambda: cleared.append(True)))
    mw._ytdlp_wait_cursor = True
    mw._on_ytdlp_update_failed("sha256 mismatch")
    assert shown == ["sha256 mismatch"]
    assert mw._ytdlp_wait_cursor is False and cleared == [True]
