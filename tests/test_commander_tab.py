from __future__ import annotations

import io
import os
import stat
import tarfile
import zipfile
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDir
from PySide6.QtGui import QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QInputDialog, QLineEdit, QMessageBox,
    QPushButton,
)

import gui.commander_tab as commander
from config import DeeptorrentConfig
from gui.commander_tab import CommanderTab, _Cancelled, _OperationState


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def tab(qapp, tmp_path):
    widget = CommanderTab(SimpleNamespace(default_save_path=str(tmp_path)))
    yield widget
    operation = widget._active_operation
    if operation is not None:
        operation.cancel.set()
        widget._on_op_finished(operation, False, "Cancelled")
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def artifacts(parent):
    return [path for path in parent.iterdir() if ".deepflux-" in path.name]


def test_copy_overwrite_rolls_back_existing_destination(tab, tmp_path, monkeypatch):
    src = tmp_path / "source.txt"
    dst = tmp_path / "destination.txt"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    real_replace = os.replace

    def fail_staged_commit(source, destination):
        if str(source).endswith(".tmp") and os.path.abspath(destination) == str(dst):
            raise OSError("simulated commit failure")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_staged_commit)
    with pytest.raises(OSError, match="simulated"):
        tab._copy_transactional(
            str(src), str(dst), 0, 1, _OperationState("copy"), True)

    assert dst.read_text(encoding="utf-8") == "old"
    assert src.read_text(encoding="utf-8") == "new"
    assert artifacts(tmp_path) == []


def test_cancellation_removes_partial_staged_directory(tab, tmp_path, monkeypatch):
    src = tmp_path / "tree"
    src.mkdir()
    (src / "first.bin").write_bytes(b"a" * 32)
    (src / "second.bin").write_bytes(b"b" * 32)
    dst = tmp_path / "copied-tree"
    operation = _OperationState("copy")
    monkeypatch.setattr(commander, "_COPY_CHUNK", 4)

    def cancel_after_first_chunk(*_args):
        operation.cancel.set()

    tab.op_progress.connect(cancel_after_first_chunk)
    try:
        with pytest.raises(_Cancelled):
            tab._copy_transactional(
                str(src), str(dst), 0, 1, operation, overwrite=False)
    finally:
        tab.op_progress.disconnect(cancel_after_first_chunk)

    assert src.is_dir()
    assert not dst.exists()
    assert artifacts(tmp_path) == []


def test_operation_launches_are_serialized_and_cancellation_is_per_operation(
        tab, tmp_path, monkeypatch):
    threads = []

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon
            threads.append(self)

        def start(self):
            pass

    monkeypatch.setattr(commander.threading, "Thread", FakeThread)
    source = str(tmp_path / "source")

    assert tab._launch_worker("copy", [source], str(tmp_path), False)
    first = tab._active_operation
    assert first is not None
    assert not tab._launch_worker("delete", [source], "", False)
    assert len(threads) == 1

    first.cancel.set()
    tab._on_op_finished(first, False, "Cancelled")
    assert tab._launch_worker("copy", [source], str(tmp_path), False)
    second = tab._active_operation
    assert second is not None and second is not first
    assert not second.cancel.is_set()
    assert len(threads) == 2


def test_same_volume_move_uses_replace_without_copy(tab, tmp_path, monkeypatch):
    src = tmp_path / "move-me.txt"
    dst = tmp_path / "moved.txt"
    src.write_text("payload", encoding="utf-8")
    calls = []
    real_replace = os.replace

    def recording_replace(source, destination):
        calls.append((os.path.abspath(source), os.path.abspath(destination)))
        return real_replace(source, destination)

    def copy_must_not_run(*_args, **_kwargs):
        raise AssertionError("same-volume move fell back to copy")

    monkeypatch.setattr(os, "replace", recording_replace)
    monkeypatch.setattr(tab, "_copy_transactional", copy_must_not_run)
    tab._move_transactional(
        str(src), str(dst), 0, 1, _OperationState("move"), overwrite=False)

    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "payload"
    assert calls == [(str(src.resolve()), str(dst.resolve()))]


def test_symlink_is_a_boundary_for_size_copy_and_remove(tab, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "large.bin").write_bytes(b"x" * 4096)
    link = tmp_path / "linked-tree"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    assert tab._is_boundary(str(link))
    assert tab._size_of(str(link), _OperationState("copy")) == 0

    copied = tmp_path / "copied-link"
    tab._copy_transactional(
        str(link), str(copied), 0, 1, _OperationState("copy"), False)
    assert copied.is_symlink()

    tab._remove(str(link))
    assert not os.path.lexists(link)
    assert (target / "large.bin").exists()


def test_windows_reparse_attribute_is_detected_without_traversal(monkeypatch):
    class ReparseStat:
        st_mode = stat.S_IFDIR
        st_file_attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    monkeypatch.setattr(os.path, "islink", lambda _path: False)
    if hasattr(os.path, "isjunction"):
        monkeypatch.setattr(os.path, "isjunction", lambda _path: False)
    monkeypatch.setattr(os, "lstat", lambda _path: ReparseStat())

    assert CommanderTab._is_boundary("reparse")
    assert CommanderTab._size_of("reparse", _OperationState("copy")) == 0


def test_delete_does_not_discover_directory_size(tab, tmp_path, monkeypatch):
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    (doomed / "child.txt").write_text("data", encoding="utf-8")

    def sizing_is_a_failure(*_args, **_kwargs):
        raise AssertionError("delete must not perform a size walk")

    monkeypatch.setattr(tab, "_size_of", sizing_is_a_failure)
    result = tab._execute_operation(
        _OperationState("delete"), [str(doomed)], "", overwrite=False)

    assert result == "Permanent delete complete"
    assert not doomed.exists()


def test_native_filesystem_drag_drop_is_disabled(tab):
    for pane in (tab.left_pane, tab.right_pane):
        assert pane._view.dragDropMode() == QAbstractItemView.DragDropMode.NoDragDrop
        assert not pane._view.dragEnabled()
        assert not pane._view.acceptDrops()
        assert not pane._view.viewport().acceptDrops()


def test_default_delete_uses_recycle_and_permanent_delete_is_distinct(
        tab, tmp_path, monkeypatch):
    first = str(tmp_path / "one.txt")
    second = str(tmp_path / "folder" / "two.txt")
    paths = [first, second]
    monkeypatch.setattr(tab._active, "selected_paths", lambda: paths)
    launches = []
    prompts = []

    def question(_parent, title, text, _buttons):
        prompts.append((title, text))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", question)
    monkeypatch.setattr(
        tab, "_launch_worker",
        lambda *args, **kwargs: launches.append((args, kwargs)) or True,
    )
    tab._start_delete()

    assert launches[0][0][:4] == ("recycle", paths, "", False)
    assert prompts[0][0] == "Move to Recycle Bin"
    assert all(os.path.abspath(path) in prompts[0][1] for path in paths)

    display = tab._display_paths(paths)
    permanent_prompt = {}

    def exact_paths(_parent, title, label, initial):
        permanent_prompt.update(title=title, label=label, initial=initial)
        return display, True

    monkeypatch.setattr(QInputDialog, "getMultiLineText", exact_paths)
    tab._start_permanent_delete()

    assert launches[1][0][:4] == ("delete", paths, "", False)
    assert permanent_prompt["title"] == "Permanently Delete"
    assert permanent_prompt["initial"] == ""
    assert display in permanent_prompt["label"]
    assert "cannot be undone" in permanent_prompt["label"].lower()


def test_permanent_delete_rejects_inexact_path_confirmation(tab, tmp_path, monkeypatch):
    path = str(tmp_path / "precise.txt")
    monkeypatch.setattr(tab._active, "selected_paths", lambda: [path])
    monkeypatch.setattr(
        QInputDialog, "getMultiLineText",
        lambda *_args: (path + ".wrong", True),
    )
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda _parent, title, text: warnings.append((title, text)),
    )
    monkeypatch.setattr(
        tab, "_launch_worker",
        lambda *_args, **_kwargs: pytest.fail("inexact permanent deletion was launched"),
    )

    tab._start_permanent_delete()

    assert warnings
    assert "exactly match" in warnings[0][1]


def test_recycle_execution_never_calls_permanent_remove(tab, tmp_path, monkeypatch):
    doomed = str(tmp_path / "recycle-me.txt")
    recycled = []
    monkeypatch.setattr(tab, "_recycle", lambda path: recycled.append(path))
    monkeypatch.setattr(
        tab, "_remove",
        lambda _path: pytest.fail("recycle operation permanently removed an item"),
    )

    result = tab._execute_operation(
        _OperationState("recycle"), [doomed], "", overwrite=False)

    assert result == "Moved to Recycle Bin"
    assert recycled == [doomed]


def test_windows_recycle_routes_to_native_shell_api(tab, tmp_path, monkeypatch):
    source = tmp_path / "native-recycle.txt"
    source.write_text("payload", encoding="utf-8")
    calls = []
    monkeypatch.setattr(commander.sys, "platform", "win32")
    monkeypatch.setattr(
        CommanderTab, "_recycle_windows", staticmethod(lambda path: calls.append(path)))
    monkeypatch.setattr(
        CommanderTab, "_recycle_fallback", classmethod(
            lambda _cls, _path: pytest.fail("Windows used non-native trash fallback")))

    tab._recycle(str(source))

    assert calls == [str(source)]


def test_non_windows_recycle_fallback_moves_instead_of_unlinking(
        tab, tmp_path, monkeypatch):
    source = tmp_path / "recoverable.txt"
    source.write_text("payload", encoding="utf-8")
    data_home = tmp_path / "xdg"
    monkeypatch.setattr(commander.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    tab._recycle(str(source))

    trashed = list((data_home / "Trash" / "files").iterdir())
    assert not source.exists()
    assert len(trashed) == 1
    assert trashed[0].read_text(encoding="utf-8") == "payload"
    assert (data_home / "Trash" / "info" / (trashed[0].name + ".trashinfo")).exists()


def test_pane_back_forward_history_and_persistable_paths(tab, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    third = tmp_path / "third"
    for path in (first, second, third):
        path.mkdir()

    pane = tab.left_pane
    pane.navigate(str(first))
    pane.navigate(str(second))
    assert pane._back_btn.isEnabled()

    pane.go_back()
    assert os.path.samefile(pane.current_path(), first)
    assert pane._forward_btn.isEnabled()
    pane.go_forward()
    assert os.path.samefile(pane.current_path(), second)

    pane.go_back()
    pane.navigate(str(third))
    assert not pane._forward_history

    tab.set_pane_paths(str(first), str(second))
    left, right = tab.pane_paths()
    assert os.path.samefile(left, first)
    assert os.path.samefile(right, second)


def test_quick_filter_and_hidden_system_toggle(tab):
    pane = tab.left_pane
    pane.set_filename_filter("report")
    assert pane.filename_filter() == "report"
    assert pane._model.nameFilters() == ["*report*"]
    assert not pane._model.nameFilterDisables()

    pane.set_show_hidden_system(True)
    filters = pane._model.filter()
    assert filters & QDir.Filter.Hidden
    assert filters & QDir.Filter.System
    assert pane.shows_hidden_system()
    assert "free" in pane._pane_status.text().lower()


def test_conflicts_are_resolved_per_item_and_keep_both_is_planned(
        tab, tmp_path, monkeypatch):
    source_dir = tmp_path / "sources"
    destination = tmp_path / "destination"
    source_dir.mkdir()
    destination.mkdir()
    keep = source_dir / "keep.txt"
    skip = source_dir / "skip.txt"
    keep.write_text("new keep", encoding="utf-8")
    skip.write_text("new skip", encoding="utf-8")
    (destination / "keep.txt").write_text("old keep", encoding="utf-8")
    (destination / "skip.txt").write_text("old skip", encoding="utf-8")
    tab.set_pane_paths(str(source_dir), str(destination))
    monkeypatch.setattr(tab.left_pane, "selected_paths", lambda: [str(keep), str(skip)])
    choices = iter(("keep_both", "skip"))
    monkeypatch.setattr(tab, "_prompt_conflict", lambda *_args: next(choices))
    launched = {}

    def launch(mode, paths, dst_dir, overwrite, target_plan):
        launched.update(mode=mode, paths=paths, dst_dir=dst_dir,
                        overwrite=overwrite, target_plan=target_plan)
        return True

    monkeypatch.setattr(tab, "_launch_worker", launch)
    tab._start_transfer("copy")

    assert launched["paths"] == [str(keep)]
    planned_path, planned_overwrite = launched["target_plan"][str(keep)]
    assert os.path.normpath(planned_path) == os.path.normpath(
        str(destination / "keep (copy).txt"))
    assert not planned_overwrite
    assert str(skip) not in launched["target_plan"]


def test_keep_both_generates_human_readable_unique_names(tab, tmp_path):
    original = tmp_path / "archive.tar.gz"
    first_copy = tmp_path / "archive.tar (copy).gz"
    original.write_text("old", encoding="utf-8")
    first_copy.write_text("copy", encoding="utf-8")

    candidate = tab._keep_both_path(str(original))
    assert candidate == str(tmp_path / "archive.tar (copy 2).gz")

    reserved = {os.path.normcase(os.path.abspath(candidate))}
    assert tab._keep_both_path(str(original), reserved) == str(
        tmp_path / "archive.tar (copy 3).gz")


def test_commander_controls_have_accessible_names_and_tooltips(tab):
    assert tab.left_pane._view.accessibleName() == "Left pane files"
    assert tab.right_pane._view.accessibleName() == "Right pane files"
    assert tab._status.accessibleName()
    assert tab._status.toolTip()
    assert tab._outcome_status.accessibleName()
    assert tab._outcome_status.toolTip()
    shortcuts = [shortcut.key() for shortcut in tab.findChildren(QShortcut)]
    assert QKeySequence("Delete") in shortcuts
    assert QKeySequence("F8") in shortcuts
    assert QKeySequence("Shift+Delete") in shortcuts

    for pane in (tab.left_pane, tab.right_pane):
        widgets = pane.findChildren(QPushButton) + pane.findChildren(QLineEdit)
        for widget in widgets:
            assert widget.accessibleName(), type(widget).__name__
            assert widget.toolTip(), widget.accessibleName()


def test_operation_outcomes_are_bounded_and_summarized(tab, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: None)
    for index in range(22):
        operation = _OperationState("copy", item_count=index + 1)
        tab._active_operation = operation
        tab._on_op_finished(operation, index % 2 == 0, f"outcome {index}")

    outcomes = tab.operation_outcomes()
    assert len(outcomes) == 20
    assert outcomes[-1] == {
        "mode": "copy", "ok": False, "message": "outcome 21", "items": 22,
    }
    assert "Copy failed" in tab.operation_history_summary()
    assert "outcome 21" in tab._outcome_status.toolTip()


def test_commander_paths_round_trip_separately_from_splitter_state(qapp, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    config_path = tmp_path / "config.json"
    config = DeeptorrentConfig(default_save_path=str(tmp_path))
    config.ui_splitters["commander_v1"] = "splitter-state-only"
    widget = CommanderTab(config)
    widget.set_pane_paths(str(left), str(right))
    assert widget.persist_pane_paths() == {"left": str(left), "right": str(right)}
    config.to_file(str(config_path))
    widget.close()

    restored = DeeptorrentConfig.from_file(str(config_path))
    assert restored.ui_commander_paths == {"left": str(left), "right": str(right)}
    assert restored.ui_splitters["commander_v1"] == "splitter-state-only"
    restored_widget = CommanderTab(restored)
    try:
        restored_left, restored_right = restored_widget.pane_paths()
        assert os.path.samefile(restored_left, left)
        assert os.path.samefile(restored_right, right)
    finally:
        restored_widget.close()


def test_missing_and_malformed_persisted_paths_fall_back(qapp, tmp_path):
    config = SimpleNamespace(
        default_save_path=str(tmp_path),
        ui_commander_paths={"left": "bad\x00path", "right": str(tmp_path / "missing")},
    )
    widget = CommanderTab(config)
    try:
        left, right = widget.pane_paths()
        assert os.path.samefile(left, tmp_path)
        assert os.path.isdir(right)
    finally:
        widget.close()


def test_text_preview_obeys_strict_byte_cap_and_marks_truncation(tab, tmp_path, monkeypatch):
    source = tmp_path / "large.txt"
    source.write_bytes(b"abcdefghijklmnop")
    monkeypatch.setattr(commander, "_PREVIEW_TEXT_BYTES", 8)

    result = tab._build_preview(str(source))

    assert result["kind"] == "text"
    assert str(result["text"]).startswith("abcdefgh")
    assert "ijkl" not in str(result["text"])
    assert "truncated" in str(result["text"]).lower()


def test_image_preview_enforces_byte_and_pixel_limits(tab, tmp_path, monkeypatch):
    source = tmp_path / "image.png"
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(0xFF336699)
    assert image.save(str(source))

    monkeypatch.setattr(commander, "_PREVIEW_IMAGE_BYTES", 1)
    too_many_bytes = tab._build_preview(str(source))
    assert too_many_bytes["kind"] == "state"
    assert "too large" in str(too_many_bytes["text"]).lower()

    monkeypatch.setattr(commander, "_PREVIEW_IMAGE_BYTES", 1024 * 1024)
    monkeypatch.setattr(commander, "_PREVIEW_IMAGE_PIXELS", 1)
    too_many_pixels = tab._build_preview(str(source))
    assert too_many_pixels["kind"] == "state"
    assert "pixels" in str(too_many_pixels["text"]).lower()


def test_stale_preview_result_is_ignored(tab):
    tab._preview_token = "new-request"
    tab._preview_text.setPlainText("new pending")

    tab._apply_preview("old-request", {"kind": "text", "text": "stale payload"})

    assert tab._preview_text.toPlainText() == "new pending"


def test_binary_preview_reports_unsupported(tab, tmp_path):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"\x00\x01\x02\x03")
    result = tab._build_preview(str(source))
    assert result["kind"] == "state"
    assert "unsupported" in str(result["text"]).lower()


def test_sha256_calculation_and_cancellation(tab, tmp_path, monkeypatch):
    source = tmp_path / "checksum.bin"
    source.write_bytes(b"abc")
    assert tab._calculate_checksum(str(source), _OperationState("checksum")) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    source.write_bytes(b"x" * 64)
    operation = _OperationState("checksum")
    monkeypatch.setattr(commander, "_CHECKSUM_CHUNK", 4)

    def cancel(*_args):
        operation.cancel.set()

    tab.op_progress.connect(cancel)
    try:
        with pytest.raises(_Cancelled):
            tab._calculate_checksum(str(source), operation)
    finally:
        tab.op_progress.disconnect(cancel)


def test_sha256_verification_reports_match_and_mismatch(tab, tmp_path):
    source = tmp_path / "verify.txt"
    source.write_bytes(b"abc")
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    matched = _OperationState("verify_checksum", options={"expected": expected})
    assert "verified" in tab._execute_operation(matched, [str(source)], "", False).lower()
    assert matched.result == expected

    mismatched = _OperationState("verify_checksum", options={"expected": "0" * 64})
    with pytest.raises(OSError, match="mismatch"):
        tab._execute_operation(mismatched, [str(source)], "", False)
    assert mismatched.result == expected


def test_archive_round_trip_zip_and_tar_gz(tab, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "hello.txt").write_text("hello", encoding="utf-8")

    for suffix in (".zip", ".tar.gz"):
        archive = tmp_path / f"bundle{suffix}"
        destination = tmp_path / f"unpacked-{suffix.replace('.', '-')}"
        tab._create_archive([str(source)], str(archive), _OperationState("archive_create"))
        tab._extract_archive(str(archive), str(destination), _OperationState("archive_extract"))
        assert (destination / "source" / "nested" / "hello.txt").read_text(
            encoding="utf-8") == "hello"


def test_archive_extraction_rejects_zip_traversal_and_cleans_staging(tab, tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../outside.txt", b"owned")
    destination = tmp_path / "extract"

    with pytest.raises(OSError, match="traversal"):
        tab._extract_archive(str(archive), str(destination), _OperationState("archive_extract"))

    assert not destination.exists()
    assert not (tmp_path / "outside.txt").exists()
    assert artifacts(tmp_path) == []


def test_archive_extraction_rejects_tar_symlink(tab, tmp_path):
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as output:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../outside"
        output.addfile(link)
    destination = tmp_path / "extract-tar"

    with pytest.raises(OSError, match="link or special"):
        tab._extract_archive(str(archive), str(destination), _OperationState("archive_extract"))

    assert not destination.exists()
    assert artifacts(tmp_path) == []


def test_archive_extraction_rejects_tar_traversal_and_zip_symlink(tab, tmp_path):
    bad_tar = tmp_path / "traversal.tar"
    with tarfile.open(bad_tar, "w") as output:
        member = tarfile.TarInfo("../../outside.txt")
        member.size = 1
        output.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(OSError, match="traversal"):
        tab._extract_archive(
            str(bad_tar), str(tmp_path / "tar-output"), _OperationState("archive_extract"))

    bad_zip = tmp_path / "symlink.zip"
    with zipfile.ZipFile(bad_zip, "w") as output:
        member = zipfile.ZipInfo("link")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(member, "../outside")
    with pytest.raises(OSError, match="link or special"):
        tab._extract_archive(
            str(bad_zip), str(tmp_path / "zip-output"), _OperationState("archive_extract"))

    assert not (tmp_path / "outside.txt").exists()
    assert artifacts(tmp_path) == []


def test_archive_extraction_enforces_entry_and_size_limits(tab, tmp_path, monkeypatch):
    archive = tmp_path / "limits.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("one.txt", b"abc")
        output.writestr("two.txt", b"d")

    monkeypatch.setattr(commander, "_ARCHIVE_MAX_ENTRIES", 1)
    with pytest.raises(OSError, match="too many entries"):
        tab._extract_archive(
            str(archive), str(tmp_path / "count-output"), _OperationState("archive_extract"))

    monkeypatch.setattr(commander, "_ARCHIVE_MAX_ENTRIES", 10)
    monkeypatch.setattr(commander, "_ARCHIVE_MAX_BYTES", 2)
    with pytest.raises(OSError, match="uncompressed size"):
        tab._extract_archive(
            str(archive), str(tmp_path / "size-output"), _OperationState("archive_extract"))

    assert artifacts(tmp_path) == []


def test_archive_extraction_never_overwrites_destination(tab, tmp_path):
    archive = tmp_path / "safe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("file.txt", b"new")
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "file.txt").write_bytes(b"old")

    with pytest.raises(FileExistsError):
        tab._extract_archive(str(archive), str(destination), _OperationState("archive_extract"))

    assert (destination / "file.txt").read_bytes() == b"old"


def test_folder_comparison_summarizes_name_size_and_mtime(tab, tmp_path):
    left = tmp_path / "compare-left"
    right = tmp_path / "compare-right"
    left.mkdir()
    right.mkdir()
    (left / "same.txt").write_text("same", encoding="utf-8")
    (right / "same.txt").write_text("same", encoding="utf-8")
    same_time = 1_700_000_000
    os.utime(left / "same.txt", (same_time, same_time))
    os.utime(right / "same.txt", (same_time, same_time))
    (left / "different.txt").write_text("left", encoding="utf-8")
    (right / "different.txt").write_text("right is larger", encoding="utf-8")
    (left / "left-only.txt").write_text("left", encoding="utf-8")

    result = tab._compare_folders(str(left), str(right), _OperationState("compare"))

    assert result["same"] == 1
    assert os.path.normcase("different.txt") in result["different"]
    assert os.path.normcase("left-only.txt") in result["left_only"]
    assert "1 same" in result["summary"]


def test_archive_creation_cancellation_removes_partial_output(tab, tmp_path, monkeypatch):
    source = tmp_path / "large.bin"
    source.write_bytes(b"x" * 128)
    target = tmp_path / "cancelled.zip"
    operation = _OperationState("archive_create")
    monkeypatch.setattr(commander, "_COPY_CHUNK", 4)

    def cancel(*_args):
        operation.cancel.set()

    tab.op_progress.connect(cancel)
    try:
        with pytest.raises(_Cancelled):
            tab._create_archive([str(source)], str(target), operation)
    finally:
        tab.op_progress.disconnect(cancel)

    assert not target.exists()
    assert artifacts(tmp_path) == []


# -- tier-1 commander upgrades: selection keys, incremental search, breadcrumbs,
#    favorites, commander clipboard, new file ------------------------------------

def _populate(tab, tmp_path, names):
    for name in names:
        (tmp_path / name).write_text(name, encoding="utf-8")
    tab.left_pane.navigate(str(tmp_path), record_history=False)
    commander.QApplication.processEvents()
    return tab.left_pane


def _wait_for_rows(pane, folder, expected: int, timeout_ms: int = 5000) -> bool:
    """QFileSystemModel populates asynchronously; poll until the pane really
    shows `folder` (never the drive list — an unfetched folder races the
    model's initial scan) with at least `expected` rows visible."""
    import time
    target = os.path.normcase(os.path.abspath(str(folder)))
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        QApplication.processEvents()
        current = os.path.normcase(os.path.abspath(pane.current_path()))
        if (current == target
                and pane._model.rowCount(pane._view.rootIndex()) >= expected):
            QApplication.processEvents()
            return True
        time.sleep(0.02)
    return False


def test_select_and_deselect_group_by_wildcard(tab, tmp_path, monkeypatch):
    pane = _populate(tab, tmp_path, ["alpha.txt", "beta.log", "gamma.txt", "notes.md"])
    assert _wait_for_rows(pane, tmp_path, 4)

    monkeypatch.setattr(commander.QInputDialog, "getText", lambda *_a, **_k: ("*.txt", True))
    pane._select_group(True)
    assert len(pane.selected_paths()) == 2

    monkeypatch.setattr(commander.QInputDialog, "getText", lambda *_a, **_k: ("*.txt;*.md", True))
    pane._select_group(False)
    assert pane.selected_paths() == []

    assert pane.select_pattern("*.log") == 1
    assert len(pane.selected_paths()) == 1


def test_insert_key_toggles_row_and_advances(tab, tmp_path):
    pane = _populate(tab, tmp_path, ["one.txt", "two.txt", "three.txt"])
    assert _wait_for_rows(pane, tmp_path, 3)

    model = pane._model
    root = pane._view.rootIndex()
    pane._view.selectionModel().clear()
    pane._view.selectionModel().setCurrentIndex(
        model.index(0, 0, root),
        commander.QItemSelectionModel.SelectionFlag.NoUpdate)

    pane._view.toggle_current_and_advance()
    assert len(pane.selected_paths()) == 1
    assert pane._view.currentIndex().row() == 1

    pane._view.toggle_current_and_advance()
    pane._view.toggle_current_and_advance()
    pane._view.toggle_current_and_advance()  # row 2 stays (no row below)
    assert len(pane.selected_paths()) == 2


def test_invert_selection_and_same_extension(tab, tmp_path):
    pane = _populate(tab, tmp_path, ["a.txt", "b.log", "c.txt"])
    assert _wait_for_rows(pane, tmp_path, 3)

    pane.select_pattern("*.txt")
    assert len(pane.selected_paths()) == 2
    pane._invert_selection()
    assert len(pane.selected_paths()) == 1
    assert os.path.basename(pane.selected_paths()[0]) == "b.log"

    pane._invert_selection()
    pane.select_same_extension()
    assert len(pane.selected_paths()) == 2


def test_incremental_search_moves_cursor_not_selection(tab, tmp_path):
    pane = _populate(tab, tmp_path, ["aaa.txt", "bee.log", "cee.txt"])
    assert _wait_for_rows(pane, tmp_path, 3)

    pane.select_pattern("*.log")
    selected = pane.selected_paths()
    assert len(selected) == 1

    assert pane._view.jump_to_prefix("c")
    current = pane._view.currentIndex()
    assert os.path.basename(pane._model.filePath(current)) == "cee.txt"
    # the cursor moved but the .log selection survived
    assert pane.selected_paths() == selected

    assert not pane._view.jump_to_prefix("zz")
    # wildcard search works too
    assert pane._view.jump_to_prefix("b*")


def test_breadcrumb_segments_and_collapse(tab, tmp_path):
    pane = tab.left_pane
    deep = tmp_path / "one" / "two" / "three"
    deep.mkdir(parents=True)
    pane.navigate(str(deep), record_history=False)

    segments = pane._path_bar.segment_paths()
    assert len(segments) == len(str(deep).split(os.sep))
    assert os.path.normcase(segments[-1]) == os.path.normcase(str(deep))
    # every prefix is itself a navigable directory
    for segment in segments:
        assert os.path.isdir(segment)

    # a very narrow bar hides leading segments but never the current one
    pane._path_bar.resize(80, pane._path_bar.height())
    QApplication.processEvents()
    assert pane._path_bar.visible_segment_count() < len(segments)
    assert pane._path_bar.visible_segment_count() >= 1
    assert not pane._path_bar._ellipsis.isHidden()

    pane._path_bar.show_edit()
    assert pane._path_bar._stack.currentWidget() is pane._path_edit
    pane._path_bar.show_crumbs()
    assert pane._path_bar._stack.currentWidget() is not pane._path_edit


def test_favorites_add_remove_jump_and_persist(qapp, tmp_path):
    config = DeeptorrentConfig(default_save_path=str(tmp_path))
    config_path = tmp_path / "config.json"
    widget = CommanderTab(config)
    try:
        left = tmp_path / "fav-left"
        right = tmp_path / "fav-right"
        left.mkdir()
        right.mkdir()
        widget.set_pane_paths(str(left), str(right))

        widget.add_favorite(str(left))
        widget.add_favorite(str(right))
        assert len(widget.commander_favorites()) == 2
        # most-recently-added first, duplicates collapse
        widget.add_favorite(str(left))
        assert len(widget.commander_favorites()) == 2

        widget._jump_favorite(0)
        assert os.path.samefile(widget._active.current_path(), right)
        widget._jump_favorite(1)
        assert os.path.samefile(widget._active.current_path(), left)
        widget._jump_favorite(99)  # out of range is a safe status message

        widget.remove_favorite(str(left))
        assert len(widget.commander_favorites()) == 1
        assert widget._is_favorite(str(right))
        assert not widget._is_favorite(str(left))

        widget.add_favorite(str(left))
        config.to_file(str(config_path))
    finally:
        widget.close()

    restored = DeeptorrentConfig.from_file(str(config_path))
    assert restored.ui_commander_favorites == widget.commander_favorites()
    # malformed values are dropped, not crashed on
    bad = tmp_path / "bad.json"
    bad.write_text('{"ui_commander_favorites": ["ok", 5, null, "x\\u0000y"]}', encoding="utf-8")
    assert DeeptorrentConfig.from_file(str(bad)).ui_commander_favorites == ["ok"]


def test_favorite_star_reflects_current_folder(tab, tmp_path):
    folder = tmp_path / "starred"
    folder.mkdir()
    tab.add_favorite(str(folder))
    tab.left_pane.navigate(str(folder), record_history=False)
    assert tab.left_pane._fav_btn.text() == "★"
    tab.remove_favorite(str(folder))
    assert tab.left_pane._fav_btn.text() == "☆"


def test_copy_path_puts_selection_on_os_clipboard(tab, tmp_path):
    pane = _populate(tab, tmp_path, ["one.txt", "two.txt"])
    assert _wait_for_rows(pane, tmp_path, 2)
    pane.select_pattern("*.txt")
    pane._copy_paths_to_clipboard()
    text = QApplication.clipboard().text()
    lines = text.splitlines()
    assert len(lines) == 2
    assert all(os.path.isabs(line.replace("\\", os.sep).replace("/", os.sep)) for line in lines)


def test_commander_clipboard_copy_cut_and_paste(tab, tmp_path, monkeypatch):
    source_dir = tmp_path / "clip-source"
    target_dir = tmp_path / "clip-target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "file.txt").write_text("payload", encoding="utf-8")
    tab.set_pane_paths(str(source_dir), str(target_dir))
    assert _wait_for_rows(tab.left_pane, source_dir, 1)
    tab.left_pane.select_pattern("*.txt")
    assert len(tab.left_pane.selected_paths()) == 1

    tab._on_file_clipboard(tab.left_pane, "copy")
    state = tab.file_clipboard_state()
    assert state is not None and state[0] == "copy" and len(state[1]) == 1

    launches = []
    monkeypatch.setattr(tab, "_plan_and_launch",
                        lambda mode, paths, dst, policy="ask": launches.append((mode, paths, dst, policy)))
    tab._on_file_clipboard(tab.right_pane, "paste")
    assert launches == [("copy", state[1], str(target_dir), "ask")]

    # cut-paste must plan a move; empty clipboard must not launch anything
    tab._on_file_clipboard(tab.left_pane, "cut")
    launches.clear()
    tab._on_file_clipboard(tab.right_pane, "paste")
    assert launches == [("move", tab.file_clipboard_state()[1], str(target_dir), "ask")]

    tab._file_clipboard = None
    launches.clear()
    tab._on_file_clipboard(tab.right_pane, "paste")
    assert launches == []
    assert "empty" in tab._status.text().lower()


def test_cut_clipboard_clears_after_successful_move(tab, monkeypatch):
    operation = _OperationState("move", item_count=1)
    tab._active_operation = operation
    tab._file_clipboard = ("cut", ["/some/path.txt"])
    tab._on_op_finished(operation, True, "Move complete")
    assert tab._file_clipboard is None

    operation = _OperationState("copy", item_count=1)
    tab._active_operation = operation
    tab._file_clipboard = ("copy", ["/some/path.txt"])
    tab._on_op_finished(operation, True, "Copy complete")
    assert tab._file_clipboard is not None  # copies stay pastable


def test_paste_into_source_folder_refuses_move(tab, tmp_path, monkeypatch):
    source_dir = tmp_path / "same-dir"
    source_dir.mkdir()
    (source_dir / "file.txt").write_text("x", encoding="utf-8")
    tab._file_clipboard = ("cut", [str(source_dir / "file.txt")])
    launches = []
    monkeypatch.setattr(tab, "_launch_worker",
                        lambda *args, **kwargs: launches.append(args) or True)
    tab.left_pane.navigate(str(source_dir), record_history=False)
    tab._paste_into(tab.left_pane)
    assert launches == []
    assert "same" in tab._status.text().lower()


def test_new_text_file_creates_and_rejects_separators(tab, tmp_path, monkeypatch):
    folder = tmp_path / "newfile"
    folder.mkdir()
    tab.left_pane.navigate(str(folder), record_history=False)

    monkeypatch.setattr(commander.QInputDialog, "getText", lambda *_a, **_k: ("note.txt", True))
    tab._new_file()
    assert (folder / "note.txt").exists()

    warnings = []
    monkeypatch.setattr(commander.QMessageBox, "warning",
                        lambda _p, _t, text: warnings.append(text))
    monkeypatch.setattr(commander.QInputDialog, "getText",
                        lambda *_a, **_k: ("../escape.txt", True))
    tab._new_file()
    assert warnings and not (tmp_path / "escape.txt").exists()

    # an existing name is never overwritten
    monkeypatch.setattr(commander.QInputDialog, "getText", lambda *_a, **_k: ("note.txt", True))
    (folder / "note.txt").write_text("keep", encoding="utf-8")
    tab._new_file()
    assert (folder / "note.txt").read_text(encoding="utf-8") == "keep"


def test_headless_conflict_policy_plans_without_prompting(tab, tmp_path, monkeypatch):
    source_dir = tmp_path / "headless"
    target_dir = tmp_path / "headless-dst"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "dupe.txt").write_text("new", encoding="utf-8")
    (target_dir / "dupe.txt").write_text("old", encoding="utf-8")

    def prompt_must_not_run(*_args):
        pytest.fail("headless policy must not open a dialog")

    monkeypatch.setattr(tab, "_prompt_conflict", prompt_must_not_run)
    launches = []
    monkeypatch.setattr(tab, "_launch_worker",
                        lambda *args, **kwargs: launches.append(args) or True)
    tab._plan_and_launch("copy", [str(source_dir / "dupe.txt")],
                         str(target_dir), conflict_policy="keep_both")
    plan = launches[0][4]
    planned_dst, overwrite = plan[str(source_dir / "dupe.txt")]
    assert os.path.normpath(planned_dst) == os.path.normpath(str(target_dir / "dupe (copy).txt"))
    assert not overwrite


# -- tier-2: folder tabs ---------------------------------------------------------------

def test_folder_tabs_new_close_and_active_pane_tracking(tab, tmp_path):
    first = tmp_path / "tabs-first"
    second = tmp_path / "tabs-second"
    first.mkdir()
    second.mkdir()
    tab.set_pane_paths(str(first), str(second))
    assert tab._sides["left"].count() == 1
    assert tab.left_pane is tab._sides["left"].widget(0)

    tab._new_tab("left")
    assert tab._sides["left"].count() == 2
    # new tab duplicates the current folder and becomes current
    assert os.path.samefile(tab.left_pane.current_path(), first)
    tab.left_pane.navigate(str(second), record_history=False)
    assert os.path.samefile(tab.left_pane.current_path(), second)
    assert tab._sides["left"].tabText(1).lower().startswith("tabs-second")

    # closing the current tab falls back to the remaining one
    tab._close_current_tab()
    assert tab._sides["left"].count() == 1
    assert os.path.samefile(tab.left_pane.current_path(), first)

    # the last tab is never closable
    tab._close_tab("left", 0)
    assert tab._sides["left"].count() == 1
    assert "cannot be closed" in tab._status.text().lower()


def test_folder_tabs_persist_and_restore(qapp, tmp_path):
    left_a = tmp_path / "left-a"
    left_b = tmp_path / "left-b"
    right_a = tmp_path / "right-a"
    for folder in (left_a, left_b, right_a):
        folder.mkdir()
    config = DeeptorrentConfig(default_save_path=str(tmp_path))
    config_path = tmp_path / "config.json"
    widget = CommanderTab(config)
    try:
        widget.set_pane_paths(str(left_a), str(right_a))
        widget._new_tab("left")
        widget.left_pane.navigate(str(left_b), record_history=False)
        assert widget.persist_pane_paths() == {"left": str(left_b), "right": str(right_a)}
        config.to_file(str(config_path))
    finally:
        widget.close()

    restored = DeeptorrentConfig.from_file(str(config_path))
    assert restored.ui_commander_tabs["left"] == [str(left_a), str(left_b)]
    assert restored.ui_commander_tabs["right"] == [str(right_a)]
    widget2 = CommanderTab(restored)
    try:
        assert widget2._sides["left"].count() == 2
        # the CURRENT tab (recorded in ui_commander_paths) is the last active one
        assert os.path.samefile(widget2.left_pane.current_path(), left_b)
        assert widget2._sides["right"].count() == 1
    finally:
        widget2.close()


# -- tier-2: batch rename ---------------------------------------------------------------

def _make_names(tmp_path, names):
    for name in names:
        (tmp_path / name).write_text(name, encoding="utf-8")
    return [str(tmp_path / name) for name in names]


def test_plan_renames_find_replace_case_and_numbering(tmp_path):
    from gui.commander_rename import plan_renames
    paths = _make_names(tmp_path, ["holiday 01.jpg", "holiday 02.jpg", "notes.txt"])

    plan = plan_renames(paths, find="holiday", replace="trip")
    assert [os.path.basename(item.target) for item in plan] == [
        "trip 01.jpg", "trip 02.jpg", "notes.txt"]
    assert not any(item.error for item in plan)

    plan = plan_renames(paths[:2], find="holiday ", replace="", number_pattern="photo ###")
    assert [os.path.basename(item.target) for item in plan] == ["photo 001.jpg", "photo 002.jpg"]

    plan = plan_renames(paths[:1], case="upper")
    assert os.path.basename(plan[0].target) == "HOLIDAY 01.JPG"

    plan = plan_renames(paths[:2], find=r"holiday (\d+)", replace=r"day-\1", use_regex=True)
    assert [os.path.basename(item.target) for item in plan] == ["day-01.jpg", "day-02.jpg"]


def test_plan_renames_flags_collisions_and_invalid_names(tmp_path):
    from gui.commander_rename import plan_renames
    paths = _make_names(tmp_path, ["a.txt", "b.txt"])
    (tmp_path / "c.txt").write_text("existing", encoding="utf-8")

    # a target that already exists on disk is refused
    plan = plan_renames([paths[0]], new_names=["c.txt"])
    assert plan[0].error and "exists" in plan[0].error.lower()

    # two sources mapping to one target: the later one is flagged
    plan = plan_renames(paths, new_names=["x.txt", "x.txt"])
    assert not plan[0].error and plan[1].error

    # invalid characters / wrong list length surface as per-item errors
    plan = plan_renames([paths[0]], new_names=["bad|name.txt"])
    assert plan[0].error
    plan = plan_renames(paths, new_names=["only-one.txt"])
    assert all(item.error for item in plan)

    # invalid regex marks every item instead of raising
    plan = plan_renames(paths, find="(", replace="x", use_regex=True)
    assert all(item.error for item in plan)

    # a case-only change is legitimate (no "destination exists" error)
    plan = plan_renames([paths[0]], new_names=["A.txt"])
    assert not plan[0].error


def test_batch_rename_execution_and_rollback(tab, tmp_path, monkeypatch):
    paths = _make_names(tmp_path, ["one.txt", "two.txt", "three.txt"])
    plans = [(paths[0], str(tmp_path / "1.txt")),
             (paths[1], str(tmp_path / "2.txt")),
             (paths[2], str(tmp_path / "3.txt"))]
    result = tab._execute_operation(
        _OperationState("batch_rename", options={"plans": plans}), paths, "", False)
    assert "3 items" in result
    assert all((tmp_path / f"{i}.txt").exists() for i in (1, 2, 3))
    assert not any((tmp_path / name).exists() for name in ("one.txt", "two.txt", "three.txt"))

    # a mid-batch failure rolls every completed rename back
    paths2 = _make_names(tmp_path, ["four.txt", "five.txt", "six.txt"])
    plans2 = [(paths2[0], str(tmp_path / "4.txt")),
              (paths2[1], str(tmp_path / "5.txt")),
              (paths2[2], str(tmp_path / "6.txt"))]
    real_rename = os.rename
    calls = {"n": 0}

    def failing_rename(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # the second forward rename fails
            raise OSError("simulated mid-batch failure")
        return real_rename(src, dst)

    monkeypatch.setattr(commander.os, "rename", failing_rename)
    with pytest.raises(OSError, match="simulated"):
        tab._execute_operation(
            _OperationState("batch_rename", options={"plans": plans2}), paths2, "", False)

    assert not (tmp_path / "4.txt").exists()
    assert (tmp_path / "four.txt").exists()          # rolled back
    assert (tmp_path / "five.txt").exists()
    assert (tmp_path / "six.txt").exists()


def test_batch_rename_case_only_and_worker_launch(tab, tmp_path, monkeypatch):
    paths = _make_names(tmp_path, ["case.txt"])
    operation = _OperationState(
        "batch_rename",
        options={"plans": [(paths[0], str(tmp_path / "CASE.txt"))]})
    result = tab._execute_operation(operation, paths, "", False)
    assert "1 items" in result
    # on Windows both spellings stat the same file — the DIRECTORY LISTING
    # is the honest witness of the stored case
    listed = [entry.name for entry in tmp_path.iterdir()]
    assert "CASE.txt" in listed
    assert "case.txt" not in listed

    from gui.commander_rename import RenamePlanItem
    launches = []
    monkeypatch.setattr(tab, "_launch_worker",
                        lambda *args, **kwargs: launches.append((args, kwargs)) or True)
    tab._start_batch_rename([
        RenamePlanItem(paths[0], str(tmp_path / "CASE.txt")),
        RenamePlanItem(str(tmp_path / "CASE.txt"), str(tmp_path / "CASE.txt")),  # no-op dropped
    ])
    assert len(launches) == 1
    assert launches[0][0][0] == "batch_rename"
    assert len(launches[0][1]["options"]["plans"]) == 1  # the no-op row is filtered out


def test_batch_rename_dialog_preview_and_paste_mode(tab, tmp_path, qapp):
    from gui.commander_rename import BatchRenameDialog
    paths = _make_names(tmp_path, ["old-one.txt", "old-two.txt"])
    dialog = BatchRenameDialog(tab, paths)
    try:
        dialog._find_edit.setText("old")
        dialog._replace_edit.setText("new")
        assert [os.path.basename(item.target) for item in dialog.plans] == [
            "new-one.txt", "new-two.txt"]
        assert dialog._ok_button.isEnabled()

        # invalid target disables Apply and flags the row
        dialog._replace_edit.setText("bad|name")
        assert not dialog._ok_button.isEnabled()
        assert dialog.plans[0].error

        # paste-list mode with a mismatched count flags everything
        dialog._tabs.setCurrentIndex(1)
        dialog._names_edit.setPlainText("single.txt")
        assert all(item.error for item in dialog.plans)
        assert not dialog._ok_button.isEnabled()

        dialog._names_edit.setPlainText("first.txt\nsecond.txt")
        assert not any(item.error for item in dialog.plans)
        assert dialog._ok_button.isEnabled()
        dialog._accept_plan()
        assert [os.path.basename(item.target) for item in dialog.plans] == [
            "first.txt", "second.txt"]
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()

