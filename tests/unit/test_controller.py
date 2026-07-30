from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtWidgets import QWidget

from easypicture.controller import ApplicationController
from easypicture.persistence import GifExportOptions
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


def test_open_image_creates_clean_source_named_document(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    source = tmp_path / "photo.png"
    pixels = np.zeros((4, 5, 4), dtype=np.uint8)
    pixels[0, 0] = (1, 2, 3, 255)
    Image.fromarray(pixels, "RGBA").save(source)
    dialogs = FakeDialogService()
    dialogs.open_path = source
    controller = ApplicationController(
        make_settings(str(tmp_path / "settings.ini")),
        dialogs,
        recovery_dir=tmp_path / "recovery",
    )

    assert controller.request_open_document(parent)

    assert controller.document is not None
    assert controller.document.display_name == "photo.png"
    assert controller.document.project_path is None
    assert not controller.document.modified


def test_corrupt_open_preserves_current_document(qtbot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    corrupt = tmp_path / "broken.limg"
    corrupt.write_bytes(b"not a project")
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(10, 10)
    dialogs.open_path = corrupt
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
