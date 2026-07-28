from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPoint, Qt

from easypicture.controller import ApplicationController
from easypicture.domain import Size
from easypicture.imaging import CanvasAnchor, GeometryRect, TransformInterpolation
from easypicture.tools import CropTool
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.geometry_dialogs import CanvasSizeDialog, ScaleGeometryDialog
from easypicture.ui.main_window import MainWindow
from tests.fakes import FakeDialogService, make_settings


def _window(qtbot, tmp_path) -> tuple[ApplicationController, MainWindow]:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(64, 48)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.resize(900, 650)
    window.show()
    window.new_action.trigger()
    qtbot.wait(1)
    return controller, window


def test_crop_tool_uses_numeric_rect_and_can_extend_outside_canvas(
    qtbot,
    tmp_path,
) -> None:
    _controller, window = _window(qtbot, tmp_path)
    window.tool_actions["crop"].trigger()
    tool = window._tools["crop"]
    assert isinstance(tool, CropTool)
    assert tool.crop_rect == GeometryRect(0, 0, 64, 48)
    assert window.tool_options_panel.currentWidget() is window.tool_options_panel.crop

    options = window.tool_options_panel.crop
    options.x_spin.setValue(-12)
    options.y_spin.setValue(-8)
    options.width_spin.setValue(96)
    options.height_spin.setValue(72)
    assert tool.crop_rect == GeometryRect(-12, -8, 96, 72)
    assert window.canvas_view.sceneRect().contains(-12.0, -8.0)

    qtbot.mouseClick(options.cancel_button, Qt.MouseButton.LeftButton)
    assert window.tool_actions["brush"].isChecked()
    assert window.overlay_manager.count == 0


def test_crop_drag_updates_rectangle_and_right_press_restores_gesture(
    qtbot,
    tmp_path,
) -> None:
    _controller, window = _window(qtbot, tmp_path)
    window.tool_actions["crop"].trigger()
    tool = window._tools["crop"]
    assert isinstance(tool, CropTool)
    before = tool.crop_rect
    center = window.canvas_view.mapFromScene(32.0, 24.0)

    qtbot.mousePress(
        window.canvas_view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=center,
    )
    qtbot.mouseMove(window.canvas_view.viewport(), center + QPoint(20, 10))
    assert tool.crop_rect != before
    qtbot.mousePress(
        window.canvas_view.viewport(),
        Qt.MouseButton.RightButton,
        pos=center + QPoint(20, 10),
    )
    qtbot.mouseRelease(
        window.canvas_view.viewport(),
        Qt.MouseButton.RightButton,
        pos=center + QPoint(20, 10),
    )
    qtbot.mouseRelease(
        window.canvas_view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=center + QPoint(20, 10),
    )
    assert tool.crop_rect == before


def test_canvas_size_dialog_emits_all_anchor_and_size_values(qtbot) -> None:
    dialog = CanvasSizeDialog(Size(64, 48))
    qtbot.addWidget(dialog)
    values: list[tuple[Size, CanvasAnchor]] = []
    dialog.preview_changed.connect(
        lambda size, anchor: values.append((size, anchor))
    )

    dialog.width_spin.setValue(80)
    dialog.height_spin.setValue(60)
    dialog.anchor_combo.setCurrentIndex(
        dialog.anchor_combo.findData(CanvasAnchor.BOTTOM_RIGHT.value)
    )

    assert dialog.size_value == Size(80, 60)
    assert dialog.anchor is CanvasAnchor.BOTTOM_RIGHT
    assert values[-1] == (Size(80, 60), CanvasAnchor.BOTTOM_RIGHT)


def test_scale_dialog_links_aspect_and_exposes_interpolation(qtbot) -> None:
    dialog = ScaleGeometryDialog(
        "缩放图像",
        Size(80, 40),
        TransformInterpolation.LINEAR,
    )
    qtbot.addWidget(dialog)
    dialog.width_spin.setValue(160)
    assert dialog.size_value == Size(160, 80)
    dialog.interpolation_combo.setCurrentIndex(
        dialog.interpolation_combo.findData(TransformInterpolation.NEAREST.value)
    )
    assert dialog.interpolation is TransformInterpolation.NEAREST


def test_crop_commit_is_one_undoable_document_geometry_command(
    qtbot,
    tmp_path,
) -> None:
    controller, window = _window(qtbot, tmp_path)
    window.tool_actions["crop"].trigger()
    options = window.tool_options_panel.crop
    options.x_spin.setValue(4)
    options.y_spin.setValue(3)
    options.width_spin.setValue(40)
    options.height_spin.setValue(30)
    qtbot.mouseClick(options.apply_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(
        lambda: controller.commands.undo_stack.count() == 1,
        timeout=5000,
    )
    document = controller.document
    assert document is not None
    assert document.size == Size(40, 30)
    assert document.active_layer.origin.x == 0
    assert document.active_layer.origin.y == 0
    assert document.active_layer.size == Size(40, 30)
    controller.commands.undo_stack.undo()
    assert document.size == Size(64, 48)


def test_trim_transparent_action_crops_to_alpha_bounds(qtbot, tmp_path) -> None:
    controller, window = _window(qtbot, tmp_path)
    document = controller.document
    assert document is not None
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[8, 10] = (10, 20, 30, 255)
    pixels[15, 20] = (40, 50, 60, 255)
    document.active_layer.pixels.replace_tile(0, 0, pixels)

    window.trim_transparent_action.trigger()
    qtbot.waitUntil(
        lambda: document.size == Size(11, 8),
        timeout=5000,
    )

    assert document.active_layer.size == document.size
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[0, 0]) == (
        10,
        20,
        30,
        255,
    )
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[7, 10]) == (
        40,
        50,
        60,
        255,
    )


def test_delete_shortcut_action_clears_active_selection(qtbot, tmp_path) -> None:
    controller, window = _window(qtbot, tmp_path)
    document = controller.document
    assert document is not None
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[4, 5] = (12, 34, 56, 255)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[4, 5] = 255
    document.selection.mask.replace_tile(0, 0, mask)
    window._update_selection_actions()

    window.clear_selected_pixels_action.trigger()

    assert tuple(document.active_layer.pixels.read_tile(0, 0)[4, 5]) == (
        12,
        34,
        56,
        0,
    )
    assert document.active_layer.pixels.has_tile(0, 0)
