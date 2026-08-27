from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtWidgets import QWidget

from easypicture.controller import ApplicationController
from easypicture.domain import Document
from easypicture.jobs import CancellationToken
from easypicture.persistence import (
    DocumentSnapshot,
    GifExportOptions,
    save_project,
)
from easypicture.pixels import TileCoord
from easypicture.ui.dialogs import (
    NewDocumentSpec,
    RecoveryDecision,
    UnsavedDecision,
)
from easypicture.ui.export_dialog import ImageExportSpec
from tests.fakes import FakeDialogService, make_settings


def test_new_document_uses_spec_and_updates_last_size(qtbot, tmp_path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(800, 600)
    settings = make_settings(str(tmp_path / "settings.ini"))
    controller = ApplicationController(settings, dialogs)

    assert controller.request_new_document(parent)

    assert controller.document is not None
    assert (controller.document.width, controller.document.height) == (800, 600)
    assert settings.last_document_size().width == 800
    assert settings.last_document_size().height == 600


def test_cancel_new_document_preserves_current_document(qtbot, tmp_path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    dialogs = FakeDialogService()
    settings = make_settings(str(tmp_path / "settings.ini"))
    controller = ApplicationController(settings, dialogs)
    dialogs.next_document = NewDocumentSpec(100, 100)
    assert controller.request_new_document(parent)
    original = controller.document
    dialogs.next_document = None

    assert not controller.request_new_document(parent)
    assert controller.document is original


def test_clean_document_closes_without_prompt(qtbot, tmp_path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(100, 100)
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
    )
    controller.request_new_document(parent)

    assert controller.close_document(parent)
    assert controller.document is None
    assert dialogs.discard_calls == 0


def test_modified_document_cancel_preserves_document(qtbot, tmp_path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(100, 100)
    dialogs.discard_result = False
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
    )
    controller.request_new_document(parent)
    assert controller.document is not None
    original = controller.document
    original.record_change()

    assert not controller.close_document(parent)
    assert controller.document is original
    assert dialogs.discard_calls == 1


def test_modified_document_discard_closes_document(qtbot, tmp_path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(100, 100)
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
    )
    controller.request_new_document(parent)
    assert controller.document is not None
    controller.document.record_change()

    assert controller.close_document(parent)
    assert controller.document is None
    assert dialogs.discard_calls == 1


def test_new_document_does_not_replace_dirty_document_when_discard_cancelled(
    qtbot,
    tmp_path,
) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(100, 100)
    dialogs.discard_result = False
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
    )
    controller.request_new_document(parent)
    assert controller.document is not None
    original = controller.document
    original.record_change()
    dialogs.next_document = NewDocumentSpec(200, 200)

    assert not controller.request_new_document(parent)
    assert controller.document is original


def test_save_as_sets_project_path_and_clean_state(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(16, 12)
    dialogs.project_save_path = tmp_path / "saved.limg"
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=tmp_path / "recovery",
    )
    assert controller.request_new_document(parent)
    assert controller.document is not None
    controller.document.record_change()

    assert controller.request_save_as(parent)

    assert controller.document.project_path == dialogs.project_save_path
    assert controller.document.display_name == "saved.limg"
    assert not controller.document.modified
    assert dialogs.project_save_path.exists()


def test_png_export_does_not_clear_modified(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(8, 8)
    dialogs.png_export_path = tmp_path / "export.png"
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=tmp_path / "recovery",
    )
    assert controller.request_new_document(parent)
    assert controller.document is not None
    controller.document.record_change()
    revision = controller.document.revision

    assert controller.request_export_png(parent)

    assert controller.document.modified
    assert controller.document.revision == revision
    assert dialogs.png_export_path.exists()


def test_unified_export_does_not_clear_modified(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(8, 8)
    output = tmp_path / "export.gif"
    dialogs.image_export_spec = ImageExportSpec(
        output,
        GifExportOptions(colors=16, alpha_threshold=100, dither=False),
    )
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=tmp_path / "recovery",
    )
    assert controller.request_new_document(parent)
    assert controller.document is not None
    controller.document.record_change()
    revision = controller.document.revision

    assert controller.request_export_image(parent)

    assert output.exists()
    assert controller.document.modified
    assert controller.document.revision == revision
    with Image.open(output) as image:
        assert image.format == "GIF"
        assert image.n_frames == 1


def _progress(_completed: int, _total: int, _message: str) -> None:
    return None


def test_import_image_creates_saved_limg_project(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    source = tmp_path / "photo.png"
    pixels = np.zeros((4, 5, 4), dtype=np.uint8)
    pixels[0, 0] = (1, 2, 3, 255)
    Image.fromarray(pixels, "RGBA").save(source)
    dialogs = FakeDialogService()
    dialogs.image_import_path = source
    dialogs.project_save_path = tmp_path / "photo.limg"
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=tmp_path / "recovery",
    )

    assert controller.request_import_document(parent)

    assert controller.document is not None
    assert controller.document.display_name == "photo.limg"
    assert controller.document.project_path == dialogs.project_save_path
    assert not controller.document.modified
    assert dialogs.project_save_path.is_file()
    assert controller.recent_files == (dialogs.project_save_path,)


def test_import_cancelled_at_project_path_keeps_current_document(
    qtbot,
    tmp_path: Path,
) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    source = tmp_path / "photo.png"
    Image.new("RGBA", (2, 2), (1, 2, 3, 255)).save(source)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(3, 3)
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=tmp_path / "recovery",
    )
    assert controller.request_new_document(parent)
    original = controller.document
    dialogs.image_import_path = source
    dialogs.project_save_path = None

    assert not controller.request_import_document(parent)
    assert controller.document is original
    assert tuple(tmp_path.glob("*.limg")) == ()


def test_open_rejects_images_without_replacing_document(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    source = tmp_path / "photo.png"
    Image.new("RGBA", (2, 2), (1, 2, 3, 255)).save(source)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(3, 3)
    controller = ApplicationController(make_settings(str(tmp_path / "settings.ini")), dialogs)
    assert controller.request_new_document(parent)
    original = controller.document

    assert not controller.open_path(parent, source)

    assert controller.document is original
    assert dialogs.errors[-1][0] == "打开项目失败"


def test_append_image_is_one_undoable_top_level_operation(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    source = tmp_path / "large.png"
    pixels = np.zeros((4, 5, 4), dtype=np.uint8)
    pixels[0, 0] = (10, 20, 30, 255)
    pixels[2, 2] = (40, 50, 60, 255)
    pixels[3, 4] = (70, 80, 90, 255)
    Image.fromarray(pixels, "RGBA").save(source)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(3, 3)
    dialogs.append_path = source
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=tmp_path / "recovery",
    )
    assert controller.request_new_document(parent)
    assert controller.document is not None
    document = controller.document
    document_id = document.id

    assert controller.request_append_document(parent)

    assert controller.document is document
    assert document.id == document_id
    assert document.project_path is None
    assert len(document.nodes) == 2
    assert len(document.layers) == 2
    appended = document.layers[-1]
    assert appended.size == document.size
    assert tuple(appended.pixels.read_tile(0, 0)[0, 0]) == (10, 20, 30, 255)
    assert tuple(appended.pixels.read_tile(0, 0)[2, 2]) == (40, 50, 60, 255)
    assert document.nodes[-1].name == "栅格组.1"
    assert controller.commands.undo_stack.count() == 1
    assert dialogs.warnings[-1][0] == "追加提示"

    controller.commands.undo_stack.undo()
    assert len(document.nodes) == 1
    assert len(document.layers) == 1
    assert not document.modified

    controller.commands.undo_stack.redo()
    assert len(document.nodes) == 2
    assert len(document.layers) == 2


def test_append_limg_uses_fresh_ids_and_keeps_current_identity(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    source_document = Document.create(4, 4)
    source_tile = source_document.active_layer.pixels.writable_tile(0, 0)
    source_tile[1, 1] = (9, 8, 7, 255)
    project = tmp_path / "source.limg"
    save_project(
        DocumentSnapshot.capture(source_document),
        project,
        CancellationToken(),
        _progress,
    )
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(4, 4)
    dialogs.append_path = project
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=tmp_path / "recovery",
    )
    assert controller.request_new_document(parent)
    assert controller.document is not None
    target = controller.document
    existing_ids = {
        node.id for node in target.nodes
    } | {
        child.id for node in target.nodes for child in node.children
    }

    assert controller.request_append_document(parent)

    appended_ids = {
        node.id for node in target.nodes[1:]
    } | {
        child.id for node in target.nodes[1:] for child in node.children
    }
    assert existing_ids.isdisjoint(appended_ids)
    assert target.project_path is None
    assert tuple(target.layers[-1].pixels.read_tile(0, 0)[1, 1]) == (9, 8, 7, 255)


def test_missing_recent_file_is_removed_without_replacing_document(
    qtbot,
    tmp_path: Path,
) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    missing = tmp_path / "missing.limg"
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(12, 10)
    settings = make_settings(str(tmp_path / "settings.ini"))
    settings.add_recent_file(missing)
    controller = ApplicationController(settings, dialogs)
    assert controller.request_new_document(parent)
    original = controller.document
    notices: list[object] = []
    controller.notifications.notice.connect(notices.append)

    assert not controller.open_recent_path(parent, missing)

    assert controller.document is original
    assert controller.recent_files == ()
    assert len(notices) == 1


def test_corrupt_open_preserves_current_document(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    corrupt = tmp_path / "broken.limg"
    corrupt.write_bytes(b"not a project")
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(10, 10)
    dialogs.project_open_path = corrupt
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=tmp_path / "recovery",
    )
    assert controller.request_new_document(parent)
    original = controller.document

    assert not controller.request_open_document(parent)

    assert controller.document is original
    assert dialogs.errors


def test_unsaved_save_then_close_uses_project_save(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(10, 10)
    dialogs.project_save_path = tmp_path / "closed.limg"
    dialogs.unsaved_decision = UnsavedDecision.SAVE
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=tmp_path / "recovery",
    )
    assert controller.request_new_document(parent)
    assert controller.document is not None
    controller.document.record_change()

    assert controller.close_document(parent)

    assert controller.document is None
    assert dialogs.project_save_path.exists()


def test_recovery_draft_round_trip(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    recovery_dir = tmp_path / "recovery"
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(10, 10)
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=recovery_dir,
        recovery_interval_ms=10,
    )
    assert controller.request_new_document(parent)
    assert controller.document is not None
    controller.document.record_change()
    qtbot.waitUntil(controller.recovery_path.exists, timeout=5000)

    restore_dialogs = FakeDialogService()
    restore_dialogs.recovery_decision = RecoveryDecision.RESTORE
    restored = ApplicationController(
        make_settings(str(tmp_path / "restored-settings.ini")),
        restore_dialogs,
        recovery_dir=recovery_dir,
    )

    assert restored.offer_recovery(parent)
    assert restored.document is not None
    assert restored.document.modified
    assert restored.document.display_name == "Untitled"


def test_selection_change_schedules_recovery_and_restores_mask(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    recovery_dir = tmp_path / "selection-recovery"
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(32, 32)
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=recovery_dir,
        recovery_interval_ms=10,
    )
    assert controller.request_new_document(parent)
    assert controller.document is not None
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[4, 5] = 127

    assert controller.commands.apply_selection_tiles(
        {TileCoord(0, 0): mask},
        "测试选区",
    )
    qtbot.waitUntil(controller.recovery_path.exists, timeout=5000)

    restore_dialogs = FakeDialogService()
    restore_dialogs.recovery_decision = RecoveryDecision.RESTORE
    restored = ApplicationController(
        make_settings(str(tmp_path / "restored-settings.ini")),
        restore_dialogs,
        recovery_dir=recovery_dir,
    )
    assert restored.offer_recovery(parent)
    assert restored.document is not None
    assert restored.document.selection.mask.read_tile(0, 0)[4, 5] == 127
    assert restored.document.modified
