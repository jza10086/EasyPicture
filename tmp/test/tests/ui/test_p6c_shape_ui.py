from __future__ import annotations

from PySide6.QtCore import QPoint, Qt

from easypicture.controller import ApplicationController
from easypicture.imaging import ShapeKind
from easypicture.tools import ShapeTool
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from tests.fakes import FakeDialogService, make_settings


def _window(qtbot, tmp_path) -> tuple[ApplicationController, MainWindow, ShapeTool]:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(96, 72)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.resize(900, 650)
    window.show()
    window.new_action.trigger()
    window.tool_actions["shape"].trigger()
    tool = window._tools["shape"]
    assert isinstance(tool, ShapeTool)
    return controller, window, tool


def _drag(qtbot, window: MainWindow, start: QPoint, end: QPoint) -> None:
    viewport = window.canvas_view.viewport()
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, end)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)


def test_shape_release_commits_and_keeps_tool_for_continuous_creation(qtbot, tmp_path) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    start = window.canvas_view.mapFromScene(10.0, 10.0)
    end = window.canvas_view.mapFromScene(60.0, 45.0)
    _drag(qtbot, window, start, end)

    assert controller.commands.undo_stack.count() == 1
    assert not tool.has_active_session
    assert window.tool_controller.active_tool is tool
    document = controller.document
    assert document is not None
    assert document.active_layer.pixels.has_tile(0, 0)

    _drag(
        qtbot,
        window,
        window.canvas_view.mapFromScene(62.0, 8.0),
        window.canvas_view.mapFromScene(86.0, 30.0),
    )
    assert controller.commands.undo_stack.count() == 2
    assert not tool.has_active_session
    assert window.tool_controller.active_tool is tool


def test_shape_creation_right_press_cancels_current_drag(qtbot, tmp_path) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    viewport = window.canvas_view.viewport()
    start = window.canvas_view.mapFromScene(10.0, 10.0)
    end = window.canvas_view.mapFromScene(50.0, 40.0)
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, end)
    qtbot.mousePress(
        viewport,
        Qt.MouseButton.RightButton,
        pos=end,
    )
    qtbot.mouseRelease(
        viewport,
        Qt.MouseButton.RightButton,
        pos=end,
    )
    qtbot.mouseRelease(
        viewport,
        Qt.MouseButton.LeftButton,
        pos=end,
    )
    assert controller.commands.undo_stack.count() == 0
    assert not tool.has_active_session
    assert window.overlay_manager.count == 0


def test_shape_properties_and_settings_are_connected(qtbot, tmp_path) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    options = window.tool_options_panel.shape
    assert options.apply_button.isHidden()
    options.stroke_width_spin.setValue(7.5)
    assert window.settings.shape_settings().stroke_width == 7.5
    options.kind_combo.setCurrentIndex(options.kind_combo.findData(ShapeKind.ELLIPSE.value))
    assert window.settings.shape_kind() is ShapeKind.ELLIPSE
    assert not tool.has_active_session
    _drag(
        qtbot,
        window,
        window.canvas_view.mapFromScene(8.0, 8.0),
        window.canvas_view.mapFromScene(40.0, 30.0),
    )
    assert controller.commands.undo_stack.count() == 1


def test_shape_drag_uses_lightweight_preview_until_release(qtbot, tmp_path) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    coordinator = window.canvas_view.render_coordinator
    viewport = window.canvas_view.viewport()
    start = window.canvas_view.mapFromScene(8.0, 8.0)
    end = window.canvas_view.mapFromScene(40.0, 30.0)
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, end)
    assert coordinator.preview_tile_count == 0
    assert window.overlay_manager.count > 0
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    assert controller.commands.undo_stack.count() == 1
    assert coordinator.preview_tile_count == 0
    assert not tool.has_active_session


def test_shape_cancel_and_layer_change_clear_preview(qtbot, tmp_path) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    viewport = window.canvas_view.viewport()
    start = window.canvas_view.mapFromScene(4.0, 4.0)
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, window.canvas_view.mapFromScene(30.0, 24.0))
    qtbot.mouseClick(
        window.tool_options_panel.shape.cancel_button,
        Qt.MouseButton.LeftButton,
    )
    assert not tool.has_active_session
    assert window.overlay_manager.count == 0
    assert controller.commands.undo_stack.count() == 0

    assert controller.commands.create_layer()
    assert window.overlay_manager.count == 0


def test_polygon_closes_with_enter_and_commits_immediately(qtbot, tmp_path) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    options = window.tool_options_panel.shape
    options.kind_combo.setCurrentIndex(options.kind_combo.findData(ShapeKind.POLYGON.value))
    viewport = window.canvas_view.viewport()
    for point in ((10.0, 10.0), (60.0, 12.0), (30.0, 55.0)):
        qtbot.mouseClick(
            viewport,
            Qt.MouseButton.LeftButton,
            pos=window.canvas_view.mapFromScene(*point),
        )
    qtbot.keyClick(viewport, Qt.Key.Key_Return)
    assert controller.commands.undo_stack.count() == 1
    assert not tool.has_active_session
    assert window.tool_controller.active_tool is tool
