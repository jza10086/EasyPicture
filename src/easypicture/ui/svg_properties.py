"""Live properties editor for imported static SVG child objects."""

from __future__ import annotations

import math

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from easypicture.commands import DocumentCommandService
from easypicture.domain import AffineTransform, Point, SvgElementObject
from easypicture.ui.color_dialog import ColorPickerDialog
from easypicture.ui.drag_value_spinbox import DragValueDoubleSpinBox


class SvgPropertiesPanel(QWidget):
    object_changed = Signal(object)

    def __init__(
        self,
        commands: DocumentCommandService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands = commands
        self._object: SvgElementObject | None = None
        self._syncing = False
        self.name_edit = QLineEdit(self)
        self.kind_label = QLabel(self)
        self.x_spin = self._field(-32768.0, 32768.0)
        self.y_spin = self._field(-32768.0, 32768.0)
        self.width_spin = self._field(0.01, 32768.0)
        self.height_spin = self._field(0.01, 32768.0)
        self.rotation_spin = self._field(-36000.0, 36000.0, suffix="°")
        self.pivot_x_spin = self._field(-32768.0, 32768.0)
        self.pivot_y_spin = self._field(-32768.0, 32768.0)
        self.opacity_spin = self._field(0.0, 100.0, suffix="%")
        self.fill_button = QPushButton("覆盖填充色", self)
        self.stroke_button = QPushButton("覆盖描边色", self)
        self.stroke_width_spin = self._field(0.0, 8192.0, suffix=" px")
        self.visible_check = QCheckBox("可见", self)
        self.locked_check = QCheckBox("锁定对象", self)
        form = QFormLayout()
        form.addRow("名称", self.name_edit)
        form.addRow("SVG 元素", self.kind_label)
        form.addRow("X", self.x_spin)
        form.addRow("Y", self.y_spin)
        form.addRow("宽度", self.width_spin)
        form.addRow("高度", self.height_spin)
        form.addRow("旋转", self.rotation_spin)
        form.addRow("中心 X", self.pivot_x_spin)
        form.addRow("中心 Y", self.pivot_y_spin)
        form.addRow("不透明度", self.opacity_spin)
        form.addRow("填充", self.fill_button)
        form.addRow("描边", self.stroke_button)
        form.addRow("描边宽度", self.stroke_width_spin)
        form.addRow(self.visible_check)
        form.addRow(self.locked_check)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(QLabel("SVG 矢量属性", self))
        layout.addLayout(form)
        layout.addStretch(1)
        self.name_edit.textChanged.connect(self.apply)
        for field in (
            self.x_spin,
            self.y_spin,
            self.width_spin,
            self.height_spin,
            self.rotation_spin,
            self.pivot_x_spin,
            self.pivot_y_spin,
            self.opacity_spin,
            self.stroke_width_spin,
        ):
            field.valueChanged.connect(self.apply)
        self.visible_check.toggled.connect(self.apply)
        self.locked_check.toggled.connect(self.apply)
        self.fill_button.clicked.connect(lambda: self._pick_color("fill_override"))
        self.stroke_button.clicked.connect(lambda: self._pick_color("stroke_override"))
        self.set_object(None)

    def _field(
        self,
        minimum: float,
        maximum: float,
        *,
        suffix: str = "",
    ) -> DragValueDoubleSpinBox:
        return DragValueDoubleSpinBox(
            minimum,
            maximum,
            0.0,
            decimals=2,
            suffix=suffix,
            parent=self,
        )

    def set_object(self, vector_object: SvgElementObject | None) -> None:
        self._object = vector_object
        self._syncing = True
        try:
            self.setEnabled(vector_object is not None)
            if vector_object is None:
                return
            bounds = vector_object.intrinsic_bounds
            matrix = vector_object.transform
            top_left = matrix.map_point(Point(bounds.x, bounds.y))
            width = bounds.width * math.hypot(matrix.a, matrix.b)
            height = bounds.height * math.hypot(matrix.c, matrix.d)
            pivot = vector_object.transform_pivot or matrix.map_point(
                Point(bounds.x + bounds.width / 2.0, bounds.y + bounds.height / 2.0)
            )
            self.name_edit.setText(vector_object.name)
            self.kind_label.setText(vector_object.element_kind)
            self.x_spin.setValue(top_left.x)
            self.y_spin.setValue(top_left.y)
            self.width_spin.setValue(width)
            self.height_spin.setValue(height)
            self.rotation_spin.setValue(math.degrees(math.atan2(matrix.b, matrix.a)))
            self.pivot_x_spin.setValue(pivot.x)
            self.pivot_y_spin.setValue(pivot.y)
            self.opacity_spin.setValue(vector_object.opacity * 100.0)
            self.stroke_width_spin.setValue(vector_object.stroke_width_override or 0.0)
            self.visible_check.setChecked(vector_object.visible)
            self.locked_check.setChecked(vector_object.locked)
            self._update_color_button(self.fill_button, vector_object.fill_override)
            self._update_color_button(self.stroke_button, vector_object.stroke_override)
        finally:
            self._syncing = False

    def apply(self) -> None:
        vector_object = self._object
        if self._syncing or vector_object is None:
            return
        bounds = vector_object.intrinsic_bounds
        old = vector_object.transform
        current_width = max(1e-9, bounds.width * math.hypot(old.a, old.b))
        current_height = max(1e-9, bounds.height * math.hypot(old.c, old.d))
        current_rotation = math.degrees(math.atan2(old.b, old.a))
        top_left = old.map_point(Point(bounds.x, bounds.y))
        pivot = Point(self.pivot_x_spin.value(), self.pivot_y_spin.value())
        delta = (
            AffineTransform.translation(-pivot.x, -pivot.y)
            .then(AffineTransform.scale(
                self.width_spin.value() / current_width,
                self.height_spin.value() / current_height,
            ))
            .then(AffineTransform.rotation(self.rotation_spin.value() - current_rotation))
            .then(AffineTransform.translation(pivot.x, pivot.y))
            .then(
                AffineTransform.translation(
                    self.x_spin.value() - top_left.x,
                    self.y_spin.value() - top_left.y,
                )
            )
        )
        values: dict[str, object] = {
            "name": self.name_edit.text(),
            "transform": old.then(delta),
            "transform_pivot": pivot,
            "opacity": self.opacity_spin.value() / 100.0,
            "visible": self.visible_check.isChecked(),
            "locked": self.locked_check.isChecked(),
            "stroke_width_override": self.stroke_width_spin.value(),
        }
        changes = {
            key: value
            for key, value in values.items()
            if getattr(vector_object, key) != value
        }
        if changes and self.commands.update_vector_object(vector_object.id, **changes):
            self.object_changed.emit(vector_object)

    def _pick_color(self, field: str) -> None:
        vector_object = self._object
        if vector_object is None:
            return
        current = getattr(vector_object, field) or (255, 255, 255, 255)
        dialog = ColorPickerDialog(QColor(*current), "SVG 颜色", self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        color = dialog.selected_color
        value = (color.red(), color.green(), color.blue(), color.alpha())
        if self.commands.update_vector_object(vector_object.id, **{field: value}):
            self._update_color_button(
                self.fill_button if field == "fill_override" else self.stroke_button,
                value,
            )
            self.object_changed.emit(vector_object)

    @staticmethod
    def _update_color_button(
        button: QPushButton,
        color: tuple[int, int, int, int] | None,
    ) -> None:
        if color is None:
            button.setStyleSheet("")
            return
        value = QColor(*color)
        button.setStyleSheet(f"background-color: {value.name(QColor.NameFormat.HexArgb)}")


__all__ = ["SvgPropertiesPanel"]
