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
