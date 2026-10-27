from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QVBoxLayout

from easypicture.controller import ApplicationController
from easypicture.pixels import Rect
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
    assert window.sprite_count_label.text() == "帧 0"
    assert "画笔" in window.shortcut_hint_label.text()
    assert window.windowTitle() == "Untitled* — EasyPicture"
    assert window.close_action.isEnabled()
    assert window.append_action.isEnabled()

    controller.document.record_change()
    assert window.windowTitle() == "Untitled* — EasyPicture"


def test_status_bar_separates_shortcuts_operations_and_document_statistics(
    qtbot,
    tmp_path,
) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(96, 48)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    window.new_action.trigger()

    window.tool_actions["sprite_slice"].trigger()
    qtbot.wait(0)
    window._show_operation_status("已完成测试操作")
    qtbot.wait(0)

    assert "Enter 确定" in window.shortcut_hint_label.text()
    assert window.operation_status_label.text() == "已完成测试操作"
    assert window.document_size_label.text() == "96 × 48 px"
    assert window.sprite_count_label.text() == "帧 0"
    assert (
        abs(
            window.operation_status_label.geometry().center().x()
            - window.statusBar().rect().center().x()
        )
        <= 1
    )
    assert (
        window.dockWidgetArea(window.sprite_frames_dock) is Qt.DockWidgetArea.BottomDockWidgetArea
    )
    assert window.corner(Qt.Corner.BottomRightCorner) is Qt.DockWidgetArea.RightDockWidgetArea
    assert window.sprite_frames_dock.minimumHeight() == (
        window.sprite_frame_panel.preferred_dock_height()
    )
    assert window.sprite_frames_dock.maximumHeight() > (
        window.sprite_frame_panel.preferred_dock_height()
    )
    assert window.sprite_frames_dock.geometry().right() < window.properties_dock.geometry().left()


def test_saved_bottom_corner_layout_is_rebuilt_for_right_sidebar_priority(
    qtbot,
    tmp_path,
) -> None:
    settings_path = str(tmp_path / "settings.ini")
    first_settings = make_settings(settings_path)
    first_controller = ApplicationController(first_settings, FakeDialogService())
    first = MainWindow(first_controller, first_settings)
    qtbot.addWidget(first)
    first.setCorner(
        Qt.Corner.BottomRightCorner,
        Qt.DockWidgetArea.BottomDockWidgetArea,
    )
    first.sprite_frames_dock.show()
    first_settings.save_window(first)
    first_settings.sync()

    settings = make_settings(settings_path)
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(96, 48)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    window.new_action.trigger()
    window.tool_actions["sprite_slice"].trigger()
    qtbot.wait(0)

    assert window.corner(Qt.Corner.BottomRightCorner) is Qt.DockWidgetArea.RightDockWidgetArea
    assert window.sprite_frames_dock.geometry().right() < window.properties_dock.geometry().left()


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


def test_sprite_slice_creates_metadata_without_touching_pixels(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(64, 64)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.new_action.trigger()
    document = controller.document
    assert document is not None
    layer_revision = document.active_layer.revision

    window.tool_actions["sprite_slice"].trigger()
    tool = window._tools["sprite_slice"]
    assert tool.region == Rect(0, 0, 32, 32)
    window._confirm_sprite_region(Rect(0, 0, 32, 32))

    assert len(document.sprite_workspace.frames) == 1
    assert document.sprite_workspace.frames[0].region == Rect(0, 0, 32, 32)
    assert document.active_layer.revision == layer_revision
    assert not document.active_layer.pixels.has_tile(0, 0)
    assert tool.region == Rect(0, 0, 32, 32)


def test_sprite_view_settings_round_trip_and_update_canvas(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)

    window.pixel_exact_action.setChecked(True)
    window.pixel_grid_action.setChecked(True)
    window.sprite_grid_action.setChecked(False)
    window.sprite_snap_action.setChecked(True)

    assert settings.pixel_exact_coordinates()
    assert settings.pixel_grid_visible()
    assert not settings.sprite_grid_visible()
    assert settings.sprite_grid_snap_enabled()


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

    assert not any("second.png" in text for text in recent_texts)
    assert any("first.limg" in text for text in recent_texts)
    assert window.about_action.text() == "关于 EasyPicture…"
    assert window.open_log_folder_action.text() == "打开日志目录"
    assert window.open_action.text() == "打开(&O)"
    assert window.import_action.text() == "导入…"
    assert window.append_action.text() == "追加…"
