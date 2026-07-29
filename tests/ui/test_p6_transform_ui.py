from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from easypicture.controller import ApplicationController
from easypicture.imaging import TransformInterpolation
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.drag_value_spinbox import (
    DragValueDoubleSpinBox,
    NumericEditState,
)
from easypicture.ui.main_window import MainWindow
from tests.fakes import FakeDialogService, make_settings


def _window(qtbot, tmp_path) -> tuple[ApplicationController, MainWindow]:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(64, 48)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.resize(800, 600)
    window.show()
    window.new_action.trigger()
    qtbot.wait(1)
    return controller, window


def test_double_drag_value_field_formats_and_evaluates_decimals(qtbot) -> None:
    field = DragValueDoubleSpinBox(-180.0, 180.0, 12.5, suffix="°")
    qtbot.addWidget(field)
    field.resize(140, 26)
    field.show()

    assert field.value() == pytest.approx(12.5)
    assert field.lineEdit().text() == "12.50°"
    assert field.valueFromText("(2.5 + 1.25) * 2") == pytest.approx(7.5)
    assert field.drag_delta_for_pixels(10) == pytest.approx(36.0)


def test_transform_options_round_trip(tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    settings.set_transform_options(
        TransformInterpolation.LANCZOS,
        True,
    )
    settings.sync()

    restored = make_settings(str(tmp_path / "settings.ini"))
    assert restored.transform_interpolation() is TransformInterpolation.LANCZOS
    assert restored.transform_irregular_edit()


def test_transform_tool_keyboard_translation_previews_and_commits_once(
    qtbot,
    tmp_path,
) -> None:
    controller, window = _window(qtbot, tmp_path)
    document = controller.document
    assert document is not None
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[5, 5] = (255, 0, 0, 255)
    document.active_layer.pixels.replace_tile(0, 0, tile)

    window.tool_actions["transform"].trigger()
    assert window.overlay_manager.count == 14
    options = window.tool_options_panel.transform
    qtbot.keyClick(
        window.canvas_view,
        Qt.Key.Key_Right,
        modifier=Qt.KeyboardModifier.ShiftModifier,
    )

    renderer = window.canvas_view.renderer
    assert renderer is not None
    preview = QImage(
        64,
        48,
        QImage.Format.Format_RGBA8888_Premultiplied,
    )
    preview.fill(Qt.GlobalColor.transparent)
    painter = QPainter(preview)
    assert renderer.paint_transform_preview(painter, QRectF(0.0, 0.0, 64.0, 48.0))
    painter.end()
    assert preview.pixelColor(15, 5).red() == 255
    assert preview.pixelColor(5, 5).alpha() == 0
    assert document.active_layer.origin.x == 0
    assert controller.commands.undo_stack.count() == 0

    qtbot.mouseClick(options.apply_button, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    qtbot.waitUntil(lambda: controller.commands.undo_stack.count() == 1, timeout=5000)
    assert document.active_layer.origin.x == 0
    assert document.active_layer.size == document.size
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[5, 15]) == (
        255,
        0,
        0,
        255,
    )

    controller.commands.undo_stack.undo()
    assert document.active_layer.origin.x == 0
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[5, 5]) == (
        255,
        0,
        0,
        255,
    )


def test_transform_preview_operations_have_local_undo_and_redo(
    qtbot,
    tmp_path,
) -> None:
    controller, window = _window(qtbot, tmp_path)
    window.tool_actions["transform"].trigger()
    options = window.tool_options_panel.transform
    original_width = options.width_spin.value()

    options.width_spin.setValue(original_width + 12.0)
    options.rotation_spin.setValue(25.0)

    assert controller.commands.undo_stack.count() == 0
    assert window.undo_action.isEnabled()
    window.undo_action.trigger()
    assert options.rotation_spin.value() == pytest.approx(0.0)
    assert options.width_spin.value() == pytest.approx(original_width + 12.0)

    window.undo_action.trigger()
    assert options.width_spin.value() == pytest.approx(original_width)
    assert not window.undo_action.isEnabled()
    assert window.redo_action.isEnabled()

    window.redo_action.trigger()
    assert options.width_spin.value() == pytest.approx(original_width + 12.0)


def test_transform_drag_right_click_restores_only_current_drag(
    qtbot,
    tmp_path,
) -> None:
    _controller, window = _window(qtbot, tmp_path)
    window.tool_actions["transform"].trigger()
    before = window.transform_properties.corners()
    center = window.canvas_view.mapFromScene(20.0, 20.0)

    qtbot.mousePress(
        window.canvas_view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=center,
    )
    qtbot.mouseMove(window.canvas_view.viewport(), center + QPoint(18, 0))
    assert window.transform_properties.corners() != pytest.approx(before)

    qtbot.mousePress(
        window.canvas_view.viewport(),
        Qt.MouseButton.RightButton,
        pos=center + QPoint(18, 0),
    )
    qtbot.mouseRelease(
        window.canvas_view.viewport(),
        Qt.MouseButton.RightButton,
        pos=center + QPoint(18, 0),
    )
    qtbot.mouseRelease(
        window.canvas_view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=center + QPoint(18, 0),
    )

    assert window.transform_properties.corners() == pytest.approx(before)
    assert not window.undo_action.isEnabled()


def test_transform_pivot_coordinates_are_in_properties_panel(
    qtbot,
    tmp_path,
) -> None:
    _controller, window = _window(qtbot, tmp_path)
    window.tool_actions["transform"].trigger()
    options = window.tool_options_panel.transform
    properties = window.transform_properties
    before_corners = properties.corners()
    before_pivot = properties.pivot()

    assert not hasattr(options, "x_spin")
    assert not hasattr(options, "y_spin")
    properties._pivot_fields[0].setValue(before_pivot[0] + 5.0)  # noqa: SLF001

    assert properties.pivot()[0] == pytest.approx(before_pivot[0] + 5.0)
    assert properties.corners() == pytest.approx(before_corners)
    window.undo_action.trigger()
    assert properties.pivot() == pytest.approx(before_pivot)


def test_transform_cancel_and_revision_change_leave_pixels_untouched(
    qtbot,
    tmp_path,
) -> None:
    controller, window = _window(qtbot, tmp_path)
    document = controller.document
    assert document is not None
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[4, 4] = (10, 20, 30, 128)
    document.active_layer.pixels.replace_tile(0, 0, tile)
    before = document.active_layer.pixels.read_tile(0, 0).copy()

    window.tool_actions["transform"].trigger()
    options = window.tool_options_panel.transform
    options.rotation_spin.setValue(35.0)
    assert window.overlay_manager.count == 14
    qtbot.keyClick(window.canvas_view, Qt.Key.Key_Escape)
    assert window.overlay_manager.count == 0
    assert np.array_equal(document.active_layer.pixels.read_tile(0, 0), before)
    assert controller.commands.undo_stack.count() == 0

    window.transform_action.trigger()
    assert window.overlay_manager.count == 14
    document.record_change()
    assert window.overlay_manager.count == 0
    assert np.array_equal(document.active_layer.pixels.read_tile(0, 0), before)


def test_transform_refuses_locked_and_alpha_locked_layers(qtbot, tmp_path) -> None:
    controller, window = _window(qtbot, tmp_path)
    document = controller.document
    assert document is not None
    document.active_layer.locked = True
    window.tool_actions["transform"].trigger()
    assert window.overlay_manager.count == 0

    document.active_layer.locked = False
    document.active_layer.alpha_locked = True
    window.transform_action.trigger()
    assert window.overlay_manager.count == 0


def test_transform_parameter_feedback_does_not_cancel_active_drag(
    qtbot,
    tmp_path,
) -> None:
    _controller, window = _window(qtbot, tmp_path)
    window.tool_actions["transform"].trigger()
    field = window.tool_options_panel.transform.rotation_spin
    original = field.value()
    inner = field._field  # noqa: SLF001 - exercise the shared interaction core
    qtbot.mousePress(inner, Qt.MouseButton.LeftButton, pos=QPoint(20, 12))
    qtbot.mouseMove(inner, QPoint(50, 12))
    assert field.interaction_state is NumericEditState.DRAGGING

    values = window.tool_options_panel.transform.parameters()
    window.tool_options_panel.transform.set_parameters(values)
    assert field.interaction_state is NumericEditState.DRAGGING
    qtbot.mouseRelease(inner, Qt.MouseButton.LeftButton, pos=QPoint(50, 12))
    assert window.undo_action.isEnabled()

    window.undo_action.trigger()
    assert field.value() == pytest.approx(original)
    assert not window.undo_action.isEnabled()


def test_deleting_transformed_layer_exits_session_and_can_restart(
    qtbot,
    tmp_path,
) -> None:
    controller, window = _window(qtbot, tmp_path)
    document = controller.document
    assert document is not None
    assert controller.commands.create_layer()
    transformed_id = document.active_layer_id
    window.tool_actions["transform"].trigger()
    assert window.overlay_manager.count == 14

    assert controller.commands.delete_layer(transformed_id)
    assert window.overlay_manager.count == 0
    assert window.canvas_view.renderer is not None

    window.transform_action.trigger()
    assert window.overlay_manager.count == 14


def test_irregular_corner_coordinates_update_projective_preview(
    qtbot,
    tmp_path,
) -> None:
    _controller, window = _window(qtbot, tmp_path)
    window.tool_actions["transform"].trigger()
    options = window.tool_options_panel.transform
    options.irregular_check.setChecked(True)
    before = window.transform_properties.corners()
    window.transform_properties._fields[0].setValue(before[0] + 7.0)  # noqa: SLF001
    after = window.transform_properties.corners()

    assert after[0] == pytest.approx(before[0] + 7.0)
    assert after[2:] == pytest.approx(before[2:])


def test_regular_corner_numeric_updates_keep_related_fields_synchronized(
    qtbot,
    tmp_path,
) -> None:
    _controller, window = _window(qtbot, tmp_path)
    window.tool_actions["transform"].trigger()
    properties = window.transform_properties
    before = properties.corners()
    properties.edit_started.emit("左上 X")

    properties._fields[0].setValue(before[0] + 6.0)  # noqa: SLF001
    first = properties.corners()
    properties._fields[0].setValue(before[0] + 12.0)  # noqa: SLF001
    second = properties.corners()
    properties.edit_finished.emit()

    assert first[0] == pytest.approx(before[0] + 6.0)
    assert second[0] == pytest.approx(before[0] + 12.0)
    assert second[2] == pytest.approx(before[2])
    assert second[6] == pytest.approx(second[0])
    assert second[6] != pytest.approx(before[6])
