from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtWidgets import QAbstractSpinBox

from easypicture.controller import ApplicationController
from easypicture.imaging import MAX_BRUSH_SIZE, BrushSettings
from easypicture.tools import BucketSettings
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.main_window import MainWindow
from tests.fakes import FakeDialogService, make_settings


def test_tool_settings_round_trip(tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    brush = BrushSettings(size=73, hardness=0.25, opacity=0.6, spacing=1.25, antialias=False)
    bucket = BucketSettings(
        tolerance=17,
        connectivity=8,
        contiguous=False,
        sample_merged=True,
    )

    settings.set_brush_settings(brush)
    settings.set_bucket_settings(bucket)
    settings.set_eyedropper_sample_merged(False)
    settings.sync()

    restored = make_settings(str(tmp_path / "settings.ini"))
    assert restored.brush_settings() == brush
    assert restored.bucket_settings() == bucket
    assert not restored.eyedropper_sample_merged()


def test_brush_size_supports_manual_values_above_drag_range(tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    brush = BrushSettings(size=2048)

    settings.set_brush_settings(brush)
    settings.sync()

    assert make_settings(str(tmp_path / "settings.ini")).brush_settings().size == 2048
    with pytest.raises(ValueError):
        BrushSettings(size=MAX_BRUSH_SIZE + 1)


def test_drag_value_spinbox_uses_fill_and_clamps_drag(qtbot) -> None:
    spin = DragValueSpinBox(
        0,
        100,
        50,
        suffix="%",
    )
    qtbot.addWidget(spin)
    spin.resize(120, 26)
    spin.show()

    assert spin.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
    assert "qlineargradient" in spin.styleSheet()
    assert "0.5000" in spin.styleSheet()

    editor = spin.lineEdit()
    qtbot.mousePress(editor, Qt.MouseButton.LeftButton, pos=QPoint(20, 10))
    qtbot.mouseMove(editor, QPoint(115, 10))
    qtbot.mouseRelease(editor, Qt.MouseButton.LeftButton, pos=QPoint(115, 10))

    assert spin.value() == 100
    assert "background: #365f91" in spin.styleSheet()


def test_brush_size_manual_entry_can_exceed_drag_range_but_opacity_cannot(qtbot) -> None:
    size = DragValueSpinBox(
        1,
        1024,
        32,
        manual_maximum=MAX_BRUSH_SIZE,
        suffix=" px",
    )
    opacity = DragValueSpinBox(0, 100, 50, suffix="%")
    qtbot.addWidget(size)
    qtbot.addWidget(opacity)

    size.lineEdit().selectAll()
    qtbot.keyClicks(size.lineEdit(), "2048")
    qtbot.keyPress(size.lineEdit(), Qt.Key.Key_Enter)
    assert size.value() == 2048
    assert "background: #365f91" in size.styleSheet()

    opacity.lineEdit().selectAll()
    qtbot.keyClicks(opacity.lineEdit(), "150")
    qtbot.keyPress(opacity.lineEdit(), Qt.Key.Key_Enter)
    assert opacity.value() <= 100

    size.resize(120, 26)
    size.show()
    qtbot.mousePress(size.lineEdit(), Qt.MouseButton.LeftButton, pos=QPoint(80, 10))
    qtbot.mouseMove(size.lineEdit(), QPoint(60, 10))
    qtbot.mouseRelease(size.lineEdit(), Qt.MouseButton.LeftButton, pos=QPoint(60, 10))
    assert size.value() == 1024


def test_main_window_enables_p4_tools_only_with_document(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(128, 128)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)

    assert all(not action.isEnabled() for action in window.tool_actions.values())

    window.new_action.trigger()

    assert all(action.isEnabled() for action in window.tool_actions.values())
    assert window.tool_actions["brush"].isChecked()
    assert window.overlay_manager.count == 0
    window.tool_actions["bucket"].trigger()
    assert window.tool_options_panel.currentWidget() is window.tool_options_panel.bucket


def test_brush_gesture_commits_one_undo_command(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(128, 128)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.resize(700, 500)
    window.show()
    window.new_action.trigger()
    qtbot.wait(1)
    start = window.canvas_view.mapFromScene(QPointF(30.0, 64.0))
    end = window.canvas_view.mapFromScene(QPointF(100.0, 64.0))

    qtbot.mousePress(window.canvas_view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(window.canvas_view.viewport(), end)
    qtbot.mouseRelease(window.canvas_view.viewport(), Qt.MouseButton.LeftButton, pos=end)

    assert controller.document is not None
    assert controller.document.modified
    assert controller.commands.undo_stack.count() == 1
    alpha = controller.document.active_layer.pixels.read_tile(0, 0)[64, 30:101, 3]
    assert alpha.max() > 0


def test_brush_outline_stays_visible_outside_document(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(128, 128)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.resize(700, 500)
    window.show()
    window.new_action.trigger()
    qtbot.wait(1)

    outside_document = QPoint(10, 10)
    document_point = window.canvas_view.viewport_to_document(outside_document)
    assert controller.document is not None
    assert not (
        0 <= document_point.x < controller.document.width
        and 0 <= document_point.y < controller.document.height
    )

    qtbot.mouseMove(window.canvas_view.viewport(), outside_document)

    assert not window.canvas_view._brush_cursor_rect().isEmpty()
