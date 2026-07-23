from __future__ import annotations

from easypicture.controller import ApplicationController
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from tests.fakes import FakeDialogService, make_settings


def test_main_window_starts_without_document(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    controller = ApplicationController(settings, FakeDialogService())
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)

    assert window.central_stack.currentWidget() is window.empty_state
    assert not window.close_action.isEnabled()
    assert not window.save_action.isEnabled()
    assert window.windowTitle().startswith("EasyPicture")


def test_new_document_updates_canvas_status_and_title(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(800, 600)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)

    window.new_action.trigger()

    assert controller.document is not None
    assert window.central_stack.currentWidget() is window.canvas_view
    assert window.document_size_label.text() == "800 × 600 px"
    assert window.windowTitle() == "Untitled — EasyPicture"
    assert window.close_action.isEnabled()

    controller.document.record_change()
    assert window.windowTitle() == "Untitled* — EasyPicture"


def test_close_clean_document_returns_to_empty_state(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(200, 100)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    controller.request_new_document(window)

    window.close_action.trigger()

    assert controller.document is None
    assert window.central_stack.currentWidget() is window.empty_state
    assert dialogs.discard_calls == 0
