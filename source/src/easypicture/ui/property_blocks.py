"""Composable paint fields and preview actions with one modal lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPushButton, QWidget

from easypicture.ui.color_dialog import ColorPickerDialog
from easypicture.ui.drag_value_spinbox import DragValueDoubleSpinBox
from easypicture.ui.property_binding import interaction_coordinator


def choose_property_color(
    parent: QWidget,
    current: QColor,
    title: str,
    *,
    preview: Callable[[QColor], None] | None = None,
) -> QColor | None:
    interaction_coordinator(parent).cancel()
    dialog = ColorPickerDialog(current, title, parent)
    if preview is not None:
        dialog.color_changed.connect(preview)
    try:
        return dialog.selected_color if dialog.exec() == QDialog.DialogCode.Accepted else None
    finally:
        interaction_coordinator(dialog).cancel()


def synchronize_color_button(button: QPushButton, color: QColor) -> None:
    foreground = "#ffffff" if color.lightness() < 128 else "#111111"
    button.setStyleSheet(
        f"QPushButton {{ background-color: {color.name(QColor.NameFormat.HexArgb)}; "
        f"color: {foreground}; }}"
    )


@dataclass
class FillStrokeFields:
    """Panels arrange the shared controls; document transactions stay with their binding."""

    fill: QPushButton
    stroke: QPushButton
    width: DragValueDoubleSpinBox

    @classmethod
    def create(cls, parent: QWidget, width: DragValueDoubleSpinBox) -> FillStrokeFields:
        return cls(QPushButton("填充颜色", parent), QPushButton("描边颜色", parent), width)

    def synchronize(self, fill: QColor, stroke: QColor) -> None:
        synchronize_color_button(self.fill, fill)
        synchronize_color_button(self.stroke, stroke)


class PreviewActions(QHBoxLayout):
    def __init__(self, parent: QWidget) -> None:
        super().__init__()
        self.apply = QPushButton("应用", parent)
        self.cancel = QPushButton("取消", parent)
        self.addStretch(1)
        self.addWidget(self.apply)
        self.addWidget(self.cancel)

    def set_waiting(self, waiting: bool) -> None:
        self.apply.setText("等待结果…" if waiting else "应用")

    def set_applying(self, applying: bool) -> None:
        self.apply.setEnabled(not applying)
        self.cancel.setEnabled(True)
