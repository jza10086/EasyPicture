from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QMenu

from easypicture.domain import SvgRectGeometry
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.drag_value_spinbox import (
    DragValueDoubleSpinBox,
    DragValueSpinBox,
    NumericEditState,
)
from easypicture.ui.settings import TileWorkspaceSettings
from easypicture.ui.tile_workspace_panel import TileWorkspacePanel
from easypicture.ui.transform_properties import TransformPropertiesPanel
from tests.ui.test_p9d_svg_ui import _panel_for_object


def _context_menu(field):
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(10, 10), field.mapToGlobal(QPoint(10, 10)),
    )
    QApplication.sendEvent(field, event)
    return QApplication.activePopupWidget()


@pytest.mark.parametrize("floating", [False, True])
def test_default_menu_applies_one_interaction_and_blocks_feedback(qtbot, floating):
    field = (DragValueDoubleSpinBox(0, 100, 19.5, default_value=3.25) if floating
             else DragValueSpinBox(0, 100, 19, default_value=3))
    qtbot.addWidget(field)
    field.show()
    events = []
    feedback = []
    field.interactionStarted.connect(lambda: events.append("start"))
    field.valueChanged.connect(lambda value: events.append(value))
    field.valueChanged.connect(lambda _value: feedback.append(field.syncValue(99)))
    field.editingFinished.connect(lambda: events.append("finish"))
    field.interactionCanceled.connect(lambda: events.append("cancel"))
    menu = _context_menu(field)
    assert isinstance(menu, QMenu)
    assert [action.text() for action in menu.actions()] == ["设为默认值"]
    qtbot.mouseClick(menu, Qt.MouseButton.LeftButton,
                     pos=menu.actionGeometry(menu.actions()[0]).center())
    expected = 3.25 if floating else 3
    assert field.value() == expected
    assert events == ["start", expected, "finish"]
    assert feedback == [False]
    assert field.interaction_state is NumericEditState.IDLE
    assert QApplication.overrideCursor() is None
    field.resetToDefault()
    assert events == ["start", expected, "finish"]
    # A dismissed menu must not leak any interaction into the next click.
    menu = _context_menu(field)
    assert not menu.actions()[0].isEnabled()
    qtbot.keyClick(menu, Qt.Key.Key_Escape)
    target = field._field if floating else field
    qtbot.mouseClick(target, Qt.MouseButton.LeftButton)
    assert field.interaction_state is NumericEditState.TEXT_EDITING
    qtbot.keyClick(field.lineEdit(), Qt.Key.Key_Escape)


@pytest.mark.parametrize("text_editing", [False, True])
def test_right_cancel_never_opens_default_menu(qtbot, text_editing):
    field = DragValueSpinBox(0, 100, 40, default_value=0)
    qtbot.addWidget(field)
    field.resize(180, 26)
    field.show()
    canceled, finished = [], []
    field.interactionCanceled.connect(lambda: canceled.append(True))
    field.editingFinished.connect(lambda: finished.append(True))
    if text_editing:
        qtbot.mouseClick(field, Qt.MouseButton.LeftButton)
        qtbot.keyClicks(field.lineEdit(), "70")
    else:
        qtbot.mousePress(field, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        qtbot.mouseMove(field, QPoint(30, 10))
        assert field.value() != 40
    qtbot.mousePress(field, Qt.MouseButton.RightButton)
    qtbot.mouseRelease(field, Qt.MouseButton.RightButton)
    if not text_editing:
        qtbot.mouseRelease(field, Qt.MouseButton.LeftButton)
    assert _context_menu(field) is None
    assert field.value() == 40
    assert canceled == [True]
    assert finished == []
    assert field.interaction_state is NumericEditState.IDLE
    menu = _context_menu(field)
    assert isinstance(menu, QMenu)
    qtbot.keyClick(menu, Qt.Key.Key_Escape)


def test_default_tracks_dynamic_hard_range_without_losing_preset(qtbot):
    field = DragValueDoubleSpinBox(0, 100, 9.25, default_value=12.5)
    qtbot.addWidget(field)
    field.setPropertyRange(0, 4)
    assert field.defaultValue() == 4
    field.setValue(1)
    field.resetToDefault()
    assert field.value() == 4
    field.setPropertyRange(0, 100)
    field.syncValue(32)
    assert field.defaultValue() == 12.5
    field.resetToDefault()
    assert field.value() == 12.5
    field.setEnabled(False)
    field.setValue(20)
    field.resetToDefault()
    assert field.value() == 20


def test_tile_defaults_are_presets_not_last_saved_settings(qtbot):
    panel = TileWorkspacePanel(TileWorkspaceSettings(
        tolerance=44, offset_x=21, boundary_percent=78, transition_width=12,
    ))
    qtbot.addWidget(panel)
    changed = []
    panel.parameters_changed.connect(lambda: changed.append(True))
    panel.tolerance.resetToDefault()
    panel.offset_x.resetToDefault()
    assert panel.tolerance.value() == 0
    assert panel.offset_x.value() == 0
    assert len(changed) == 2
    assert panel.boundary.defaultValue() == 50
    panel.set_tile_size(4, 4)
    assert panel.transition_width.defaultValue() == 4
    panel.set_tile_size(32, 32)
    assert panel.transition_width.defaultValue() == 8


def test_svg_default_change_is_exactly_one_undoable_edit(qtbot):
    document, commands, panel, live = _panel_for_object(
        qtbot, SvgRectGeometry(8, 5, 20, 10),
    )
    initial = live.transform
    panel.x_spin.resetToDefault()
    assert commands.undo_stack.count() == 1
    assert panel.x_spin.value() == 0
    commands.undo_stack.undo()
    assert document.vector_object_by_id(live.id)[1].transform == initial


def test_enum_default_uses_item_data_and_keeps_single_menu_action(qtbot):
    field = BlenderComboBox()
    qtbot.addWidget(field)
    field.addItem("单次", "once")
    field.addItem("循环", "loop")
    field.addItem("往返", "ping_pong")
    field.setDefaultValue("loop")
    field.setCurrentIndex(2)
    field.show()
    changed = []
    field.currentIndexChanged.connect(changed.append)
    menu = _context_menu(field)
    assert isinstance(menu, QMenu)
    assert [action.text() for action in menu.actions()] == ["设为默认值"]
    qtbot.mouseClick(menu, Qt.MouseButton.LeftButton,
                     pos=menu.actionGeometry(menu.actions()[0]).center())
    assert field.currentData() == "loop"
    assert changed == [1]
    field.insertItem(0, "new", "new")
    field.setCurrentIndex(0)
    field.resetToDefault()
    assert field.currentData() == "loop"
    field.clear()
    field.resetToDefault()
    assert field.currentIndex() == -1


def test_transform_defaults_stay_at_session_start_until_new_session(qtbot):
    panel = TransformPropertiesPanel()
    qtbot.addWidget(panel)
    original = (10., 20., 30., 20., 30., 40., 10., 40.)
    panel.set_corners(original)
    panel.set_pivot((20., 30.))
    panel.set_corners(tuple(value + 15 for value in original))
    panel.set_pivot((35., 45.))
    panel._fields[0].resetToDefault()
    panel._pivot_fields[0].resetToDefault()
    assert panel._fields[0].value() == 10
    assert panel._pivot_fields[0].value() == 20
    panel.set_session_active(False)
    panel.set_corners(tuple(value + 100 for value in original))
    assert panel._fields[0].defaultValue() == 110


def test_default_reset_rejected_by_owner_does_not_change_value(qtbot):
    field = DragValueSpinBox(0, 100, 42, default_value=0)
    qtbot.addWidget(field)
    field.interactionStarted.connect(lambda: field.setEnabled(False))
    field.resetToDefault()
    assert field.value() == 42
    assert field.interaction_state is NumericEditState.IDLE
