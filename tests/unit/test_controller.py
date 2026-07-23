from __future__ import annotations

from PySide6.QtWidgets import QWidget

from easypicture.controller import ApplicationController
from easypicture.ui.dialogs import NewDocumentSpec
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
