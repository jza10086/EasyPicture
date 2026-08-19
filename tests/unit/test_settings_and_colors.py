from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

from easypicture.domain import Document, Size
from easypicture.render import RenderBackendKind
from easypicture.ui.color_dialog import ColorPickerDialog
from easypicture.ui.color_panel import ColorPanel
from easypicture.ui.drag_value_spinbox import DRAG_THRESHOLD, NumericEditState
from tests.fakes import FakeDialogService, make_settings


def test_settings_round_trip_size_and_colors(tmp_path) -> None:
    path = str(tmp_path / "settings.ini")
    first = make_settings(path)
    first.set_last_document_size(Size(1234, 567))
    first.set_foreground_color(QColor("#123456"))
    first.set_background_color(QColor("#abcdef"))
    first.set_selection_outline_style(QColor("#35a7ff"), 4)
    first.set_render_backend(RenderBackendKind.SOFTWARE)
    first.set_numeric_drag_sensitivity(0.02, 0.0005)
    first.sync()

    second = make_settings(path)

    assert second.last_document_size() == Size(1234, 567)
    assert second.foreground_color().name() == "#123456"
    assert second.background_color().name() == "#abcdef"
    assert second.selection_outline_color().name() == "#35a7ff"
    assert second.selection_outline_width() == 4
    assert second.render_backend() is RenderBackendKind.SOFTWARE
    assert second.numeric_drag_sensitivity() == 0.02
    assert second.numeric_fine_drag_sensitivity() == 0.0005


def test_invalid_settings_fall_back_to_defaults(tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    settings._settings.setValue("new_document/width", 0)  # noqa: SLF001
    settings._settings.setValue("new_document/height", 9000)  # noqa: SLF001
    settings._settings.setValue("colors/foreground", "invalid")  # noqa: SLF001
    settings._settings.setValue("selection/outline_color", "invalid")  # noqa: SLF001
    settings._settings.setValue("selection/outline_width", 99)  # noqa: SLF001
    settings._settings.setValue("render/backend", "unknown")  # noqa: SLF001
    settings._settings.setValue("ui/numeric_drag_sensitivity", 99)  # noqa: SLF001
    settings._settings.setValue(  # noqa: SLF001
        "ui/numeric_fine_drag_sensitivity",
        -1,
    )

    assert settings.last_document_size() == Size(1920, 1080)
    assert settings.foreground_color().name() == "#000000"
    assert settings.selection_outline_color().name() == "#f28c28"
    assert settings.selection_outline_width() == 8
    assert settings.render_backend() is RenderBackendKind.AUTO
    assert settings.numeric_drag_sensitivity() == 0.1
    assert settings.numeric_fine_drag_sensitivity() == 0.0001


def test_color_panel_persists_without_modifying_document(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    panel = ColorPanel(settings, dialogs)
    qtbot.addWidget(panel)
    document = Document.create(10, 10)

    panel.set_foreground(QColor("#ff0000"))
    panel.set_background(QColor("#0000ff"))
    panel.swap_colors()

    assert panel.foreground.name() == "#0000ff"
    assert panel.background.name() == "#ff0000"
    assert settings.foreground_color().name() == "#0000ff"
    assert settings.background_color().name() == "#ff0000"
    assert not document.modified
    assert document.revision == 0


def test_color_picker_uses_chinese_drag_fields_and_hex_input(qtbot) -> None:
    dialog = ColorPickerDialog(QColor(10, 20, 30, 40), "选择前景色")
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == "选择前景色"
    assert dialog.red_spin.value() == 10
    assert dialog.alpha_spin.value() == 40
    dialog.red_spin.setValue(123)
    dialog.alpha_spin.setValue(77)
    assert dialog.selected_color.red() == 123
    assert dialog.selected_color.alpha() == 77

    dialog.hex_edit.setText("#00ff0080")
    dialog.hex_edit.editingFinished.emit()
    assert dialog.selected_color == QColor(0, 255, 0, 128)
    assert dialog.saturation_spin.value() == 100
    assert dialog.value_spin.value() == 100


def test_color_picker_hsv_wheel_and_modes_keep_alpha(qtbot) -> None:
    dialog = ColorPickerDialog(QColor(0, 0, 0, 91), "选择背景色")
    qtbot.addWidget(dialog)

    assert dialog.mode_stack.currentWidget() is dialog.hsv_page
    qtbot.mouseClick(dialog.mode_buttons["RGB"], Qt.MouseButton.LeftButton)
    assert dialog.mode_stack.currentWidget() is dialog.rgb_page
    qtbot.mouseClick(dialog.mode_buttons["Hex"], Qt.MouseButton.LeftButton)
    assert dialog.mode_stack.currentWidget() is dialog.hex_page

    dialog.hue_spin.setValue(240)
    dialog.saturation_spin.setValue(100)
    dialog.value_spin.setValue(100)
    assert dialog.selected_color.blue() == 255
    assert dialog.selected_color.alpha() == 91

    dialog.color_wheel.changed.emit(120, 100)
    dialog.value_bar.changed.emit(50)
    assert dialog.selected_color.green() in (127, 128)
    assert dialog.selected_color.red() == 0
    assert dialog.selected_color.blue() == 0
    assert dialog.selected_color.alpha() == 91


def test_color_picker_numeric_drag_is_not_interrupted_by_sync(qtbot) -> None:
    dialog = ColorPickerDialog(QColor(64, 96, 128, 255), "选择前景色")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(1)
    field = dialog.red_spin
    field.resize(150, 25)
    start = field.value()

    qtbot.mousePress(field, Qt.MouseButton.LeftButton, pos=QPoint(20, 10))
    qtbot.mouseMove(field, QPoint(20 + DRAG_THRESHOLD + 8, 10))

    assert field.interaction_state is NumericEditState.DRAGGING
    assert field.value() != start
    assert dialog.selected_color.red() == field.value()

    first = field.value()
    qtbot.mouseMove(field, QPoint(20 + DRAG_THRESHOLD + 18, 10))
    assert field.interaction_state is NumericEditState.DRAGGING
    assert field.value() != first
    qtbot.mouseRelease(field, Qt.MouseButton.LeftButton)
    assert field.interaction_state is NumericEditState.IDLE


def test_color_picker_eyedropper_uses_modal_dialog_grab_and_releases(qtbot) -> None:
    dialog = ColorPickerDialog(QColor(10, 20, 30, 255), "选择前景色")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(1)

    qtbot.mouseClick(dialog.eyedropper_button, Qt.MouseButton.LeftButton)

    assert dialog._eyedropper_active  # noqa: SLF001
    assert QWidget.mouseGrabber() is dialog
    assert QWidget.keyboardGrabber() is dialog
    assert QApplication.overrideCursor() is not None

    qtbot.mousePress(dialog, Qt.MouseButton.RightButton)

    assert not dialog._eyedropper_active  # noqa: SLF001
    assert QWidget.mouseGrabber() is not dialog
    assert QWidget.keyboardGrabber() is not dialog
    assert QApplication.overrideCursor() is None
    assert dialog.eyedropper_button.text() == "吸管"


def test_window_geometry_round_trip(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    first = QMainWindow()
    qtbot.addWidget(first)
    first.resize(777, 555)
    settings.save_window(first)

    second = QMainWindow()
    qtbot.addWidget(second)
    settings.restore_window(second)

    assert second.size() == first.size()


def test_window_restore_clamps_tiny_offscreen_geometry(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    first = QMainWindow()
    qtbot.addWidget(first)
    first.resize(50, 50)
    first.move(100_000, 100_000)
    settings.save_window(first)

    second = QMainWindow()
    qtbot.addWidget(second)
    second.setMinimumSize(800, 600)
    settings.restore_window(second)

    available = QApplication.primaryScreen().availableGeometry()
    assert second.width() >= 800
    assert second.height() >= 600
    assert available.contains(second.frameGeometry().center())
