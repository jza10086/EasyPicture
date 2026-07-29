from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt

from easypicture.controller import ApplicationController
from easypicture.imaging import ShapeGeometry, ShapeKind
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


def test_shape_tool_creates_preview_then_one_command(qtbot, tmp_path) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    start = window.canvas_view.mapFromScene(10.0, 10.0)
    end = window.canvas_view.mapFromScene(60.0, 45.0)
    _drag(qtbot, window, start, end)

    assert tool.has_active_session
    assert controller.commands.undo_stack.count() == 0
    assert window.overlay_manager.count > 0
    qtbot.mouseClick(
        window.tool_options_panel.shape.apply_button,
        Qt.MouseButton.LeftButton,
    )
    assert controller.commands.undo_stack.count() == 1
    assert not tool.has_active_session
    document = controller.document
    assert document is not None
    assert document.active_layer.pixels.has_tile(0, 0)


def test_shape_session_undo_redo_and_right_drag_restore(qtbot, tmp_path) -> None:
    _controller, window, tool = _window(qtbot, tmp_path)
    _drag(
        qtbot,
        window,
        window.canvas_view.mapFromScene(10.0, 10.0),
        window.canvas_view.mapFromScene(50.0, 40.0),
    )
    original = window.shape_properties._geometry
    assert isinstance(original, ShapeGeometry)
    center = window.canvas_view.mapFromScene(30.0, 25.0)
    viewport = window.canvas_view.viewport()
    assert window.canvas_view.render_coordinator.preview_tile_count > 0
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=center)
    assert window.canvas_view.render_coordinator.preview_tile_count == 0
    qtbot.mouseMove(viewport, center + QPoint(20, 10))
    qtbot.mousePress(
        viewport,
        Qt.MouseButton.RightButton,
        pos=center + QPoint(20, 10),
    )
    qtbot.mouseRelease(
        viewport,
        Qt.MouseButton.RightButton,
        pos=center + QPoint(20, 10),
    )
    qtbot.mouseRelease(
        viewport,
        Qt.MouseButton.LeftButton,
        pos=center + QPoint(20, 10),
    )
    assert window.shape_properties._geometry == original
    assert window.canvas_view.render_coordinator.preview_tile_count > 0

    moved = ShapeGeometry(
        ShapeKind.RECTANGLE,
        ((20.0, 15.0), (70.0, 50.0)),
    )
    tool.set_geometry(moved)
    assert tool.can_undo_preview
    assert tool.undo_preview()
    assert window.shape_properties._geometry == original
    assert tool.redo_preview()
    assert window.shape_properties._geometry == moved


def test_shape_properties_and_settings_are_connected(qtbot, tmp_path) -> None:
    _controller, window, tool = _window(qtbot, tmp_path)
    _drag(
        qtbot,
        window,
        window.canvas_view.mapFromScene(8.0, 8.0),
        window.canvas_view.mapFromScene(40.0, 30.0),
    )
    fields = window.shape_properties._box_fields
    fields[0].setValue(12.0)
    geometry = window.shape_properties._geometry
    assert geometry is not None
    assert geometry.bounds[0] == 12.0
    assert tool.has_active_session

    options = window.tool_options_panel.shape
    options.stroke_width_spin.setValue(7.5)
    assert window.settings.shape_settings().stroke_width == 7.5
    options.kind_combo.setCurrentIndex(
        options.kind_combo.findData(ShapeKind.ELLIPSE.value)
    )
    assert window.settings.shape_kind() is ShapeKind.ELLIPSE
    assert not tool.has_active_session


def test_shape_numeric_session_defers_exact_raster_until_edit_finishes(
    qtbot,
    tmp_path,
) -> None:
    _controller, window, tool = _window(qtbot, tmp_path)
    _drag(
        qtbot,
        window,
        window.canvas_view.mapFromScene(8.0, 8.0),
        window.canvas_view.mapFromScene(40.0, 30.0),
    )
    coordinator = window.canvas_view.render_coordinator
    assert coordinator.preview_tile_count > 0
    tool.begin_property_edit()
    generation = coordinator.generation
    assert coordinator.preview_tile_count == 0

    for offset in (4.0, 8.0, 12.0):
        tool.set_geometry(
            ShapeGeometry(
                ShapeKind.RECTANGLE,
                ((8.0 + offset, 8.0), (40.0 + offset, 30.0)),
            )
        )

    assert coordinator.generation == generation
    assert coordinator.preview_tile_count == 0
    tool.finish_property_edit()
    assert coordinator.generation > generation
    assert coordinator.preview_tile_count > 0


def test_rectangle_negative_width_keeps_numeric_anchor_stable(
    qtbot,
    tmp_path,
) -> None:
    _controller, window, _tool = _window(qtbot, tmp_path)
    _drag(
        qtbot,
        window,
        window.canvas_view.mapFromScene(10.0, 10.0),
        window.canvas_view.mapFromScene(50.0, 40.0),
    )
    x_field, y_field, width_field, height_field = (
        window.shape_properties._box_fields
    )
    anchor_x = x_field.value()
    anchor_y = y_field.value()

    def enter_value(field, text: str) -> None:
        field.interactionStarted.emit()
        field.setValue(float(text))
        field.editingFinished.emit()

    enter_value(width_field, "-20")
    geometry = window.shape_properties._geometry
    assert geometry is not None
    assert x_field.value() == pytest.approx(anchor_x)
    assert y_field.value() == pytest.approx(anchor_y)
    assert width_field.value() == pytest.approx(-20.0)
    assert geometry.points[0][0] == pytest.approx(anchor_x)
    assert geometry.points[1][0] == pytest.approx(anchor_x - 20.0)

    enter_value(height_field, "-15")
    geometry = window.shape_properties._geometry
    assert geometry is not None
    assert x_field.value() == pytest.approx(anchor_x)
    assert y_field.value() == pytest.approx(anchor_y)
    assert height_field.value() == pytest.approx(-15.0)
    assert geometry.points[0][1] == pytest.approx(anchor_y)
    assert geometry.points[1][1] == pytest.approx(anchor_y - 15.0)

    enter_value(width_field, "-35")
    assert x_field.value() == pytest.approx(anchor_x)
    assert y_field.value() == pytest.approx(anchor_y)
    assert width_field.value() == pytest.approx(-35.0)


def test_shape_cancel_and_layer_change_clear_preview(qtbot, tmp_path) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    _drag(
        qtbot,
        window,
        window.canvas_view.mapFromScene(4.0, 4.0),
        window.canvas_view.mapFromScene(30.0, 24.0),
    )
    qtbot.mouseClick(
        window.tool_options_panel.shape.cancel_button,
        Qt.MouseButton.LeftButton,
    )
    assert not tool.has_active_session
    assert window.overlay_manager.count == 0
    assert controller.commands.undo_stack.count() == 0

    _drag(
        qtbot,
        window,
        window.canvas_view.mapFromScene(4.0, 4.0),
        window.canvas_view.mapFromScene(30.0, 24.0),
    )
    assert controller.commands.create_layer()
    qtbot.waitUntil(lambda: not tool.has_active_session)
    assert window.overlay_manager.count == 0


def test_polygon_closes_with_enter_and_can_commit(qtbot, tmp_path) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    options = window.tool_options_panel.shape
    options.kind_combo.setCurrentIndex(
        options.kind_combo.findData(ShapeKind.POLYGON.value)
    )
    viewport = window.canvas_view.viewport()
    for point in ((10.0, 10.0), (60.0, 12.0), (30.0, 55.0)):
        qtbot.mouseClick(
            viewport,
            Qt.MouseButton.LeftButton,
            pos=window.canvas_view.mapFromScene(*point),
        )
    qtbot.keyClick(viewport, Qt.Key.Key_Return)
    assert tool.has_active_session
    qtbot.keyClick(viewport, Qt.Key.Key_Return)
    assert controller.commands.undo_stack.count() == 1
