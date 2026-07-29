from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QValidator
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from easypicture.controller import ApplicationController
from easypicture.imaging import MAX_BRUSH_SIZE, BrushSettings, PaintBlendMode
from easypicture.tools import BucketSettings
from easypicture.ui.dialogs import NewDocumentDialog, NewDocumentSpec
from easypicture.ui.drag_value_spinbox import (
    DRAG_THRESHOLD,
    DragValueDoubleSpinBox,
    DragValueSpinBox,
    NumericEditState,
    NumericPropertySpec,
    configure_numeric_drag_sensitivity,
)
from easypicture.ui.main_window import MainWindow
from tests.fakes import FakeDialogService, make_settings


def test_tool_settings_round_trip(tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    brush = BrushSettings(
        size=73,
        hardness=0.25,
        opacity=0.6,
        spacing=1.25,
        antialias=False,
        mode=PaintBlendMode.OVERLAY,
    )
    bucket = BucketSettings(
        tolerance=17,
        connectivity=8,
        contiguous=False,
        sample_merged=True,
        mode=PaintBlendMode.MULTIPLY,
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

    assert spin.property_spec == NumericPropertySpec(0, 100, 0, 100, suffix="%")
    qtbot.mousePress(spin, Qt.MouseButton.LeftButton, pos=QPoint(20, 10))
    qtbot.mouseMove(spin, QPoint(115, 10))
    qtbot.mouseRelease(spin, Qt.MouseButton.LeftButton, pos=QPoint(115, 10))

    assert spin.value() == 100
    image = spin.grab().toImage()
    assert image.pixelColor(110, 21).name() == "#365f91"


def test_drag_value_spinbox_scales_by_range_and_shift_precision(qtbot) -> None:
    small = DragValueSpinBox(0, 10, 5)
    large = DragValueSpinBox(0, 1000, 500)
    qtbot.addWidget(small)
    qtbot.addWidget(large)

    assert small.drag_delta_for_pixels(25) == pytest.approx(2.5)
    assert large.drag_delta_for_pixels(25) == pytest.approx(250.0)
    assert small.drag_delta_for_pixels(25, fine=True) == pytest.approx(0.25)
    assert large.drag_delta_for_pixels(25, fine=True) == pytest.approx(25.0)


def test_drag_value_spinbox_uses_independent_global_sensitivities(qtbot) -> None:
    configure_numeric_drag_sensitivity(0.02, 0.0005)
    try:
        spin = DragValueSpinBox(0, 100, 50)
        qtbot.addWidget(spin)

        assert spin.drag_delta_for_pixels(10) == pytest.approx(20.0)
        assert spin.drag_delta_for_pixels(10, fine=True) == pytest.approx(0.5)
    finally:
        configure_numeric_drag_sensitivity(0.01, 0.001)


def test_drag_value_click_enters_text_mode_and_evaluates_expression(qtbot) -> None:
    spin = DragValueSpinBox(0, 1000, 41, suffix=" px")
    qtbot.addWidget(spin)
    spin.resize(160, 26)
    spin.show()

    assert spin.lineEdit().isReadOnly()
    qtbot.mousePress(spin, Qt.MouseButton.LeftButton, pos=QPoint(40, 10))
    assert spin.interaction_state is NumericEditState.WAIT_DRAG
    assert spin.lineEdit().cursor().shape() is Qt.CursorShape.SizeHorCursor
    assert not spin.lineEdit().hasFocus()
    qtbot.mouseRelease(spin, Qt.MouseButton.LeftButton, pos=QPoint(40, 10))
    qtbot.wait(1)
    assert spin.interaction_state is NumericEditState.TEXT_EDITING
    assert spin.lineEdit().selectedText() == spin.lineEdit().text()
    assert not spin.lineEdit().isReadOnly()
    assert spin.lineEdit().cursor().shape() is Qt.CursorShape.IBeamCursor
    assert spin.grab().toImage().pixelColor(150, 21).name() == "#1f1f1f"
    first_position = spin.lineEdit().cursorPosition()
    qtbot.mouseClick(
        spin.lineEdit(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(2, 10),
    )
    assert not spin.lineEdit().hasSelectedText()
    assert spin.lineEdit().cursorPosition() < first_position

    qtbot.keyClick(spin.lineEdit(), Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClicks(spin.lineEdit(), "(10 + 6) / 2 * 5")
    qtbot.keyPress(spin.lineEdit(), Qt.Key.Key_Enter)
    assert spin.interaction_state is NumericEditState.WAIT_ENTER_RELEASE
    qtbot.keyRelease(spin.lineEdit(), Qt.Key.Key_Enter)

    assert spin.value() == 40
    assert spin.interaction_state is NumericEditState.IDLE
    assert not spin.lineEdit().hasSelectedText()
    assert spin.lineEdit().isReadOnly()
    assert spin.lineEdit().cursor().shape() is Qt.CursorShape.SizeHorCursor


def test_drag_value_expression_rejects_code_and_unsupported_operators(qtbot) -> None:
    spin = DragValueSpinBox(0, 1000, 40)
    qtbot.addWidget(spin)

    code_state, _text, _position = spin.validate("__import__('os')", 0)
    power_state, _text, _position = spin.validate("2 ** 8", 0)
    zero_division_state, _text, _position = spin.validate("10 / 0", 0)

    assert code_state is QValidator.State.Invalid
    assert power_state is QValidator.State.Invalid
    assert zero_division_state is QValidator.State.Invalid
    assert spin.valueFromText("2 ** 8") == 40


def test_drag_value_outside_left_commits_and_right_cancels(qtbot) -> None:
    container = QWidget()
    spin = DragValueSpinBox(0, 100, 40)
    outside = QPushButton("outside")
    layout = QVBoxLayout(container)
    layout.addWidget(spin)
    layout.addWidget(outside)
    qtbot.addWidget(container)
    container.show()

    qtbot.mouseClick(spin, Qt.MouseButton.LeftButton)
    qtbot.wait(1)
    qtbot.keyClick(spin.lineEdit(), Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClicks(spin.lineEdit(), "20 + 5")
    qtbot.mouseClick(outside, Qt.MouseButton.LeftButton)
    assert spin.value() == 25
    assert not spin.hasFocus()

    qtbot.mouseClick(spin, Qt.MouseButton.LeftButton)
    qtbot.wait(1)
    qtbot.keyClick(spin.lineEdit(), Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClicks(spin.lineEdit(), "90")
    qtbot.mousePress(outside, Qt.MouseButton.RightButton)
    assert spin.value() == 25
    assert not spin.hasFocus()
    qtbot.mouseRelease(outside, Qt.MouseButton.RightButton)

    context_requests: list[QPoint] = []
    outside.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    outside.customContextMenuRequested.connect(context_requests.append)
    position = QPoint(3, 3)
    context_event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse,
        position,
        outside.mapToGlobal(position),
    )
    QApplication.sendEvent(outside, context_event)
    assert not context_requests
    assert spin.interaction_state is NumericEditState.IDLE


def test_drag_value_enter_does_not_accept_parent_dialog(qtbot) -> None:
    dialog = NewDocumentDialog(NewDocumentSpec(640, 480).size)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(1)
    spin = dialog.width_spin
    editor = spin.lineEdit()

    qtbot.mouseClick(spin, Qt.MouseButton.LeftButton)
    qtbot.keyClick(editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClicks(editor, "300 * 2")
    qtbot.keyPress(editor, Qt.Key.Key_Enter)
    qtbot.keyRelease(editor, Qt.Key.Key_Enter)

    assert dialog.width_spin.value() == 600
    assert dialog.result() != QDialog.DialogCode.Accepted

    qtbot.keyClick(dialog, Qt.Key.Key_Enter)
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_drag_value_fill_geometry_is_stable_at_minimum_middle_and_maximum(
    qtbot,
) -> None:
    spin = DragValueSpinBox(0, 100, 50)
    qtbot.addWidget(spin)
    spin.resize(120, 26)
    spin.show()
    size_hints = []
    fill_samples = []

    for value in (0, 50, 100):
        spin.setValue(value)
        QApplication.processEvents()
        size_hints.append(spin.sizeHint())
        image = spin.grab().toImage()
        fill_samples.append(
            (
                image.pixelColor(10, 21).name(),
                image.pixelColor(110, 21).name(),
            )
        )

    assert size_hints[0] == size_hints[1] == size_hints[2]
    assert fill_samples == [
        ("#1f1f1f", "#1f1f1f"),
        ("#365f91", "#1f1f1f"),
        ("#365f91", "#365f91"),
    ]


def test_drag_value_right_press_cancels_active_drag(qtbot) -> None:
    container = QWidget()
    spin = DragValueSpinBox(0, 100, 50)
    outside = QPushButton("outside")
    layout = QVBoxLayout(container)
    layout.addWidget(spin)
    layout.addWidget(outside)
    qtbot.addWidget(container)
    container.show()

    qtbot.mousePress(spin, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    qtbot.mouseMove(spin, QPoint(70, 10))
    assert spin.value() > 50
    override_cursor = QApplication.overrideCursor()
    assert override_cursor is not None
    assert override_cursor.shape() is Qt.CursorShape.BlankCursor

    qtbot.mousePress(outside, Qt.MouseButton.RightButton)
    assert spin.value() == 50
    assert QApplication.overrideCursor() is None
    qtbot.mouseRelease(outside, Qt.MouseButton.RightButton)
    qtbot.mouseRelease(spin, Qt.MouseButton.LeftButton, pos=QPoint(70, 10))
    context_event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse,
        QPoint(3, 3),
        outside.mapToGlobal(QPoint(3, 3)),
    )
    QApplication.sendEvent(outside, context_event)
    assert spin.interaction_state is NumericEditState.IDLE


def test_brush_size_manual_entry_can_exceed_drag_range_but_opacity_cannot(qtbot) -> None:
    container = QWidget()
    size = DragValueSpinBox(
        1,
        1024,
        32,
        manual_maximum=MAX_BRUSH_SIZE,
        suffix=" px",
    )
    opacity = DragValueSpinBox(0, 100, 50, suffix="%")
    layout = QVBoxLayout(container)
    layout.addWidget(size)
    layout.addWidget(opacity)
    qtbot.addWidget(container)
    container.show()
    qtbot.wait(1)

    qtbot.mouseClick(size, Qt.MouseButton.LeftButton)
    qtbot.keyClicks(size.lineEdit(), "2048")
    qtbot.keyPress(size.lineEdit(), Qt.Key.Key_Enter)
    qtbot.keyRelease(size.lineEdit(), Qt.Key.Key_Enter)
    assert size.value() == 2048
    size.resize(120, 26)
    size.show()
    assert size.grab().toImage().pixelColor(110, 21).name() == "#365f91"

    qtbot.mouseClick(opacity, Qt.MouseButton.LeftButton)
    qtbot.keyClicks(opacity.lineEdit(), "150")
    qtbot.keyPress(opacity.lineEdit(), Qt.Key.Key_Enter)
    qtbot.keyRelease(opacity.lineEdit(), Qt.Key.Key_Enter)
    assert opacity.value() <= 100

    qtbot.mousePress(size, Qt.MouseButton.LeftButton, pos=QPoint(80, 10))
    qtbot.mouseMove(size, QPoint(60, 10))
    qtbot.mouseRelease(size, Qt.MouseButton.LeftButton, pos=QPoint(60, 10))
    assert size.value() == 1024


def test_drag_value_text_mode_selects_once_and_never_reenters_drag(qtbot) -> None:
    spin = DragValueSpinBox(0, 100, 50)
    qtbot.addWidget(spin)
    spin.resize(160, 26)
    spin.show()

    qtbot.mouseClick(spin, Qt.MouseButton.LeftButton, pos=QPoint(60, 10))
    assert spin.interaction_state is NumericEditState.TEXT_EDITING
    assert spin.lineEdit().selectedText() == spin.lineEdit().text()

    qtbot.mouseClick(spin.lineEdit(), Qt.MouseButton.LeftButton, pos=QPoint(4, 10))
    assert not spin.lineEdit().hasSelectedText()
    first_position = spin.lineEdit().cursorPosition()
    qtbot.mousePress(spin.lineEdit(), Qt.MouseButton.LeftButton, pos=QPoint(4, 10))
    qtbot.mouseMove(spin.lineEdit(), QPoint(120, 10))
    qtbot.mouseRelease(
        spin.lineEdit(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(120, 10),
    )

    assert spin.interaction_state is NumericEditState.TEXT_EDITING
    assert spin.value() == 50
    assert spin.lineEdit().cursorPosition() != first_position


def test_double_drag_value_second_click_stays_in_normal_text_mode(qtbot) -> None:
    spin = DragValueDoubleSpinBox(-100.0, 100.0, 12.5)
    qtbot.addWidget(spin)
    spin.resize(180, 26)
    spin.show()

    qtbot.mouseClick(spin._field, Qt.MouseButton.LeftButton)  # noqa: SLF001
    assert spin.interaction_state is NumericEditState.TEXT_EDITING
    assert spin.lineEdit().hasSelectedText()
    assert spin.lineEdit().geometry() == spin._field.rect()  # noqa: SLF001
    assert spin._field.cursor().shape() is Qt.CursorShape.IBeamCursor  # noqa: SLF001

    qtbot.mousePress(spin, Qt.MouseButton.LeftButton, pos=QPoint(2, 10))
    assert spin.interaction_state is NumericEditState.TEXT_SELECTING
    qtbot.mouseRelease(spin, Qt.MouseButton.LeftButton, pos=QPoint(2, 10))
    assert spin.interaction_state is NumericEditState.TEXT_EDITING
    qtbot.mouseDClick(
        spin._field,  # noqa: SLF001
        Qt.MouseButton.LeftButton,
        pos=QPoint(2, 10),
    )
    assert spin.interaction_state is NumericEditState.TEXT_SELECTING
    qtbot.mouseRelease(
        spin.lineEdit(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(2, 10),
    )
    assert spin.interaction_state is NumericEditState.TEXT_EDITING
    qtbot.mouseClick(
        spin.lineEdit(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(8, 10),
    )
    qtbot.mousePress(
        spin.lineEdit(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(8, 10),
    )
    assert spin.interaction_state is NumericEditState.TEXT_SELECTING
    qtbot.mouseMove(spin.lineEdit(), QPoint(100, 10))
    qtbot.mouseRelease(
        spin.lineEdit(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(100, 10),
    )

    assert spin.interaction_state is NumericEditState.TEXT_EDITING
    assert spin.value() == pytest.approx(12.5)
    assert not spin.lineEdit().isReadOnly()
    assert spin.lineEdit().cursor().shape() is Qt.CursorShape.IBeamCursor

    qtbot.keyClick(
        spin.lineEdit(),
        Qt.Key.Key_A,
        Qt.KeyboardModifier.ControlModifier,
    )
    qtbot.keyClicks(spin.lineEdit(), "-20")
    assert spin.lineEdit().text() == "-20"
    qtbot.keyPress(spin.lineEdit(), Qt.Key.Key_Enter)
    qtbot.keyRelease(spin.lineEdit(), Qt.Key.Key_Enter)
    assert spin.value() == pytest.approx(-20.0)
    assert spin.interaction_state is NumericEditState.IDLE


def test_drag_value_wait_drag_threshold_and_editing_finished_signals(qtbot) -> None:
    spin = DragValueSpinBox(0, 100, 50)
    qtbot.addWidget(spin)
    spin.resize(160, 26)
    spin.show()
    finished: list[None] = []
    spin.editingFinished.connect(lambda: finished.append(None))

    qtbot.mousePress(spin, Qt.MouseButton.LeftButton, pos=QPoint(20, 10))
    qtbot.mouseMove(spin, QPoint(20 + DRAG_THRESHOLD - 1, 10))
    assert spin.interaction_state is NumericEditState.WAIT_DRAG
    qtbot.mouseRelease(
        spin,
        Qt.MouseButton.LeftButton,
        pos=QPoint(20 + DRAG_THRESHOLD - 1, 10),
    )
    assert spin.interaction_state is NumericEditState.TEXT_EDITING
    qtbot.keyPress(spin.lineEdit(), Qt.Key.Key_Escape)
    assert not finished

    qtbot.mousePress(spin, Qt.MouseButton.LeftButton, pos=QPoint(20, 10))
    qtbot.mouseMove(spin, QPoint(20 + DRAG_THRESHOLD, 10))
    assert spin.interaction_state is NumericEditState.DRAGGING
    qtbot.mouseRelease(
        spin,
        Qt.MouseButton.LeftButton,
        pos=QPoint(20 + DRAG_THRESHOLD, 10),
    )
    assert spin.interaction_state is NumericEditState.IDLE
    assert len(finished) == 1


def test_drag_value_invalid_expression_restores_value_without_commit(qtbot) -> None:
    spin = DragValueSpinBox(0, 100, 40)
    qtbot.addWidget(spin)
    spin.show()
    qtbot.wait(1)
    finished: list[None] = []
    spin.editingFinished.connect(lambda: finished.append(None))

    qtbot.mouseClick(spin, Qt.MouseButton.LeftButton)
    qtbot.keyClicks(spin.lineEdit(), "10 / 0")
    qtbot.keyPress(spin.lineEdit(), Qt.Key.Key_Enter)
    qtbot.keyRelease(spin.lineEdit(), Qt.Key.Key_Enter)

    assert spin.value() == 40
    assert spin.interaction_state is NumericEditState.IDLE
    assert not finished


def test_drag_value_programmatic_update_cleans_active_drag(qtbot) -> None:
    spin = DragValueSpinBox(0, 100, 50)
    qtbot.addWidget(spin)
    spin.resize(160, 26)
    spin.show()

    qtbot.mousePress(spin, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    qtbot.mouseMove(spin, QPoint(70, 10))
    assert spin.interaction_state is NumericEditState.DRAGGING
    assert QApplication.overrideCursor() is not None

    spin.setValue(25)

    assert spin.value() == 25
    assert spin.interaction_state is NumericEditState.IDLE
    assert QApplication.overrideCursor() is None


def test_drag_value_disable_and_hide_cleanup_active_sessions(qtbot) -> None:
    spin = DragValueSpinBox(0, 100, 50)
    qtbot.addWidget(spin)
    spin.resize(160, 26)
    spin.show()

    qtbot.mouseClick(spin, Qt.MouseButton.LeftButton)
    spin.setEnabled(False)
    assert spin.interaction_state is NumericEditState.IDLE
    assert spin.value() == 50

    spin.setEnabled(True)
    qtbot.mousePress(spin, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    qtbot.mouseMove(spin, QPoint(70, 10))
    assert QApplication.overrideCursor() is not None

    spin.hide()

    assert spin.interaction_state is NumericEditState.IDLE
    assert spin.value() == 50
    assert QApplication.overrideCursor() is None


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
