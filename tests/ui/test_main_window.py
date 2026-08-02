from __future__ import annotations

from PySide6.QtWidgets import QFrame, QVBoxLayout

from easypicture.controller import ApplicationController
from easypicture.runtime import RuntimePaths
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from easypicture.ui.theme import DARK_STYLESHEET, dark_palette
from tests.fakes import FakeDialogService, make_settings


def _runtime_paths(tmp_path) -> RuntimePaths:
    return RuntimePaths(
        tmp_path / "data",
        tmp_path / "cache",
        tmp_path / "data" / "logs",
        tmp_path / "cache" / "temp",
    )


def test_main_window_starts_without_document(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    controller = ApplicationController(settings, FakeDialogService())
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)

    assert window.central_stack.currentWidget() is window.empty_state
    assert not window.close_action.isEnabled()
    assert not window.save_action.isEnabled()
    assert window.open_action.isEnabled()
    assert window.import_action.isEnabled()
    assert not window.append_action.isEnabled()
    assert window.windowTitle().startswith("EasyPicture")


def test_right_docks_have_sidebar_background_spacing_and_cards(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    controller = ApplicationController(settings, FakeDialogService())
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)

    for dock in (window.layers_dock, window.color_dock, window.properties_dock):
        assert dock.property("panelRole") == "right"
        assert "background-color: #292929" in dock.styleSheet()
        assert "border: 1px solid #151515" in dock.styleSheet()
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
        ) == (4, 3, 4, 4)

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
        ) == (4, 4, 4, 4)

    assert "QMainWindow#MainWindow::separator" in window.styleSheet()
    assert "height: 4px" in window.styleSheet()


def test_dark_theme_uses_neutral_surfaces_and_blue_selection() -> None:
    palette = dark_palette()

    assert palette.window().color().name() == "#292929"
    assert palette.base().color().name() == "#1f1f1f"
    assert palette.highlight().color().name() == "#4772b3"
    assert "QMenuBar" in DARK_STYLESHEET
    assert "QToolBar#ToolsToolbar" in DARK_STYLESHEET
    assert "QAbstractItemView::item:selected" in DARK_STYLESHEET


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
    assert window.append_action.isEnabled()

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


def test_recent_files_and_help_actions_are_available(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    first = tmp_path / "first.limg"
    second = tmp_path / "second.png"
    settings.add_recent_file(first)
    settings.add_recent_file(second)
    controller = ApplicationController(settings, FakeDialogService())
    window = MainWindow(
        controller,
        settings,
        runtime_paths=_runtime_paths(tmp_path),
    )
    qtbot.addWidget(window)

    window._rebuild_recent_files_menu()  # noqa: SLF001
    recent_texts = [action.text() for action in window.recent_files_menu.actions()]

    assert any("second.png" in text for text in recent_texts)
    assert any("first.limg" in text for text in recent_texts)
    assert window.about_action.text() == "关于 EasyPicture…"
    assert window.open_log_folder_action.text() == "打开日志目录"
    assert window.open_action.text() == "打开(&O)"
    assert window.import_action.text() == "导入…"
    assert window.append_action.text() == "追加…"
