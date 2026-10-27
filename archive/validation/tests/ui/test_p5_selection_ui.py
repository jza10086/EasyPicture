from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QWheelEvent
from PySide6.QtWidgets import QApplication

from easypicture.controller import ApplicationController
from easypicture.domain import Document
from easypicture.imaging import SelectionCombineMode, select_all_candidate
from easypicture.pixels import TileCoord
from easypicture.render import RenderBackendKind
from easypicture.tools import MagicWandSettings, mode_from_modifiers
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.dialogs import (
    NewDocumentDialog,
    NewDocumentSpec,
    SelectionRadiusDialog,
)
from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.main_window import MainWindow
from easypicture.ui.preferences_dialog import PreferencesDialog
from easypicture.ui.selection_overlay import SelectionOverlayItem
from tests.fakes import FakeDialogService, make_settings


def _window(qtbot, tmp_path) -> tuple[ApplicationController, MainWindow]:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(300, 300)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.resize(760, 560)
    window.show()
    window.new_action.trigger()
    qtbot.wait(1)
    return controller, window


def test_rectangle_selection_commits_once_and_persists_across_tool_switch(
    qtbot,
    tmp_path,
) -> None:
    controller, window = _window(qtbot, tmp_path)
    window.tool_actions["rectangle_selection"].trigger()
    start = window.canvas_view.mapFromScene(QPointF(250.0, 250.0))
    end = window.canvas_view.mapFromScene(QPointF(290.0, 290.0))

    qtbot.mousePress(window.canvas_view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(window.canvas_view.viewport(), end)
    qtbot.mouseRelease(window.canvas_view.viewport(), Qt.MouseButton.LeftButton, pos=end)

    document = controller.document
    assert document is not None
    assert document.selection.is_active
    assert document.modified
    assert controller.commands.undo_stack.count() == 1
    assert window.canvas_view._selection_overlay is not None

    window.tool_actions["brush"].trigger()
    assert document.selection.is_active
    assert window.overlay_manager.count == 0

    controller.commands.undo_stack.undo()
    assert not document.selection.is_active
    assert not document.modified


def test_selection_overlay_uses_static_solid_cosmetic_pen() -> None:
    pen = SelectionOverlayItem._pen(QColor(242, 140, 40), 1.0)

    assert pen.style() is Qt.PenStyle.SolidLine
    assert pen.capStyle() is Qt.PenCapStyle.RoundCap
    assert pen.joinStyle() is Qt.PenJoinStyle.RoundJoin
    assert pen.isCosmetic()
    assert pen.widthF() == 1.0


def test_selection_menu_actions_and_enablement(qtbot, tmp_path) -> None:
    controller, window = _window(qtbot, tmp_path)
    assert window.select_all_action.isEnabled()
    assert not window.deselect_action.isEnabled()
    assert not window.tool_options_panel.rectangle_selection.deselect_button.isEnabled()

    window.select_all_action.trigger()

    document = controller.document
    assert document is not None
    assert document.selection.is_active
    assert window.deselect_action.isEnabled()
    assert window.invert_selection_action.isEnabled()
    assert window.tool_options_panel.rectangle_selection.deselect_button.isEnabled()
    assert window.tool_options_panel.magic_wand.deselect_button.isEnabled()

    window.tool_actions["rectangle_selection"].trigger()
    qtbot.mouseClick(
        window.tool_options_panel.rectangle_selection.deselect_button,
        Qt.MouseButton.LeftButton,
    )
    assert not document.selection.is_active
    assert not window.deselect_action.isEnabled()
    assert not window.tool_options_panel.rectangle_selection.deselect_button.isEnabled()


def test_selection_outline_style_updates_without_modifying_document(qtbot, tmp_path) -> None:
    controller, window = _window(qtbot, tmp_path)
    document = controller.document
    overlay = window.canvas_view._selection_overlay
    assert document is not None
    assert overlay is not None

    window.canvas_view.set_selection_outline_style(QColor("#35a7ff"), 5)

    assert overlay.outline_color.name() == "#35a7ff"
    assert overlay.outline_width == 5.0
    assert not document.modified
    assert document.revision == 0


def test_selection_preferences_use_drag_value_input(qtbot) -> None:
    dialog = PreferencesDialog(
        QColor("#f28c28"),
        3,
        RenderBackendKind.SOFTWARE,
        numeric_drag_sensitivity=0.02,
        numeric_fine_drag_sensitivity=0.0005,
    )
    qtbot.addWidget(dialog)

    assert isinstance(dialog.selection_width_spin, DragValueSpinBox)
    assert dialog.selection_width_spin.drag_minimum == 1
    assert dialog.selection_width_spin.drag_maximum == 8
    assert dialog.selection_width == 3
    assert dialog.render_backend is RenderBackendKind.SOFTWARE
    assert dialog.numeric_drag_sensitivity == pytest.approx(0.02)
    assert dialog.numeric_fine_drag_sensitivity == pytest.approx(0.0005)


def test_document_dimensions_and_selection_radius_use_drag_inputs(qtbot) -> None:
    new_document = NewDocumentDialog(NewDocumentSpec(640, 480).size)
    selection_radius = SelectionRadiusDialog("羽化选区", 256)
    qtbot.addWidget(new_document)
    qtbot.addWidget(selection_radius)

    assert isinstance(new_document.width_spin, DragValueSpinBox)
    assert isinstance(new_document.height_spin, DragValueSpinBox)
    assert isinstance(selection_radius.radius_spin, DragValueSpinBox)
    assert selection_radius.radius_spin.drag_maximum == 256


def test_selection_enum_options_use_blender_combo_boxes(qtbot, tmp_path) -> None:
    _controller, window = _window(qtbot, tmp_path)

    assert isinstance(
        window.tool_options_panel.rectangle_selection.mode_combo,
        BlenderComboBox,
    )
    assert isinstance(window.tool_options_panel.magic_wand.mode_combo, BlenderComboBox)
    assert isinstance(
        window.tool_options_panel.magic_wand.connectivity_combo,
        BlenderComboBox,
    )


def test_blender_combo_box_ignores_wheel_value_changes(qtbot) -> None:
    combo = BlenderComboBox()
    qtbot.addWidget(combo)
    combo.addItems(("A", "B", "C"))
    combo.setCurrentIndex(1)
    event = QWheelEvent(
        QPointF(8.0, 8.0),
        QPointF(8.0, 8.0),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )

    QApplication.sendEvent(combo, event)

    assert combo.currentIndex() == 1
    assert not event.isAccepted()


def test_selection_modifier_precedence() -> None:
    assert (
        mode_from_modifiers(SelectionCombineMode.REPLACE, Qt.KeyboardModifier.NoModifier)
        is SelectionCombineMode.REPLACE
    )
    assert (
        mode_from_modifiers(
            SelectionCombineMode.REPLACE,
            Qt.KeyboardModifier.ShiftModifier,
        )
        is SelectionCombineMode.ADD
    )
    assert (
        mode_from_modifiers(
            SelectionCombineMode.REPLACE,
            Qt.KeyboardModifier.AltModifier,
        )
        is SelectionCombineMode.SUBTRACT
    )
    assert (
        mode_from_modifiers(
            SelectionCombineMode.REPLACE,
            Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier,
        )
        is SelectionCombineMode.INTERSECT
    )


def test_magic_wand_settings_round_trip(tmp_path) -> None:
    path = str(tmp_path / "settings.ini")
    settings = make_settings(path)
    expected = MagicWandSettings(
        tolerance=31,
        connectivity=8,
        contiguous=False,
        sample_merged=True,
        include_alpha=False,
        mode=SelectionCombineMode.INTERSECT,
    )
    settings.set_magic_wand_settings(expected)
    settings.sync()

    restored = make_settings(path)
    assert restored.magic_wand_settings() == expected
    assert restored.selection_combine_mode() is SelectionCombineMode.INTERSECT


def test_full_canvas_selection_outline_has_no_internal_tile_seams(qtbot) -> None:
    _ = qtbot
    document = Document.create(600, 600)
    for coord, tile in select_all_candidate(document.size).items():
        document.selection.mask.replace_tile(coord.x, coord.y, tile)
    timer_parent = QObject()
    overlay = SelectionOverlayItem(document, timer_parent)

    interior = overlay._build_path(TileCoord(1, 1))
    left_edge = overlay._build_path(TileCoord(0, 1))
    right_edge = overlay._build_path(TileCoord(2, 1))

    assert interior.isEmpty()
    assert left_edge.elementCount() == 2
    assert {left_edge.elementAt(index).x for index in range(2)} == {0.0}
    assert right_edge.elementCount() == 2
    assert {right_edge.elementAt(index).x for index in range(2)} == {600.0}
    overlay.dispose()
