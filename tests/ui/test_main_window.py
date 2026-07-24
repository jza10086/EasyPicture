from __future__ import annotations

from PySide6.QtWidgets import QFrame, QVBoxLayout

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


def test_right_docks_have_sidebar_background_spacing_and_cards(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    controller = ApplicationController(settings, FakeDialogService())
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)

    for dock in (window.layers_dock, window.color_dock, window.properties_dock):
        assert dock.property("panelRole") == "right"
        assert "background-color: palette(midlight)" in dock.styleSheet()
        assert "border: 1px solid palette(mid)" in dock.styleSheet()
        sidebar = dock.widget()
        assert sidebar is not None
        assert sidebar.property("panelRole") == "rightSidebar"
        sidebar_layout = sidebar.layout()
        assert isinstance(sidebar_layout, QVBoxLayout)
        sidebar_margins = sidebar_layout.contentsMargins()
        assert (
            sidebar_margins.left(),
            sidebar_margins.top(),
            sidebar_margins.right(),
            sidebar_margins.bottom(),
        ) == (8, 6, 8, 8)

        card = sidebar.findChild(QFrame, f"{dock.objectName()}Card")
        assert card is not None
        assert card.property("panelRole") == "dockCard"
        card_layout = card.layout()
        assert isinstance(card_layout, QVBoxLayout)
        card_margins = card_layout.contentsMargins()
        assert (
            card_margins.left(),
            card_margins.top(),
            card_margins.right(),
            card_margins.bottom(),
        ) == (6, 6, 6, 6)

    assert "QMainWindow#MainWindow::separator" in window.styleSheet()
    assert "height: 8px" in window.styleSheet()


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


def test_save_updates_window_title_and_actions(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(20, 10)
    dialogs.project_save_path = tmp_path / "named.limg"
    controller = ApplicationController(
        settings,
        dialogs,
        recovery_dir=tmp_path / "recovery",
    )
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.new_action.trigger()
    assert controller.document is not None
    controller.document.record_change()

    window.save_action.trigger()

    assert window.windowTitle() == "named.limg — EasyPicture"
    assert window.save_action.isEnabled()
    assert window.save_as_action.isEnabled()
    assert window.export_action.isEnabled()
