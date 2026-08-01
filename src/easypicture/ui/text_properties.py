"""Properties editor for editable P9A text objects."""

from __future__ import annotations

import math

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from easypicture.commands import DocumentCommandService
from easypicture.domain import AffineTransform, FloatRect, Point, TextObject
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.color_dialog import ColorPickerDialog
from easypicture.ui.drag_value_spinbox import DragValueDoubleSpinBox


class TextPropertiesPanel(QWidget):
    object_changed = Signal(object)

    def __init__(
        self,
        commands: DocumentCommandService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands = commands
        self._object: TextObject | None = None
        self._syncing = False
        self.name_edit = QLineEdit(self)
        self.text_edit = QTextEdit(self)
        self.text_edit.setMinimumHeight(52)
        self.font_edit = QLineEdit(self)
        self.bold_check = QCheckBox("粗体", self)
        self.color_button = QPushButton("颜色", self)
        self._color = QColor(255, 255, 255, 255)
        self.stroke_color_button = QPushButton("描边颜色", self)
        self._stroke_color = QColor(0, 0, 0, 255)
        self.stroke_width_spin = DragValueDoubleSpinBox(
            0.0,
            128.0,
            0.0,
            decimals=2,
            suffix=" px",
            parent=self,
        )
        self.size_spin = DragValueDoubleSpinBox(1.0, 1024.0, 32.0, suffix=" px", parent=self)
        self.x_spin = DragValueDoubleSpinBox(-8192.0, 8192.0, 0.0, decimals=2, parent=self)
        self.y_spin = DragValueDoubleSpinBox(-8192.0, 8192.0, 0.0, decimals=2, parent=self)
        self.width_spin = DragValueDoubleSpinBox(1.0, 8192.0, 256.0, decimals=2, parent=self)
        self.height_spin = DragValueDoubleSpinBox(1.0, 8192.0, 64.0, decimals=2, parent=self)
        self.rotation_spin = DragValueDoubleSpinBox(
            -36000.0,
            36000.0,
            0.0,
            decimals=2,
            suffix="°",
            parent=self,
        )
        self.pivot_x_spin = DragValueDoubleSpinBox(
            -8192.0,
            8192.0,
            0.0,
            decimals=2,
            parent=self,
        )
        self.pivot_y_spin = DragValueDoubleSpinBox(
            -8192.0,
            8192.0,
            0.0,
            decimals=2,
            parent=self,
        )
        self.line_spacing_spin = DragValueDoubleSpinBox(0.25, 8.0, 1.0, decimals=2, parent=self)
        self.opacity_spin = DragValueDoubleSpinBox(
            0.0,
            100.0,
            100.0,
            decimals=1,
            suffix="%",
            parent=self,
        )
        self.locked_check = QCheckBox("锁定对象", self)
        self.horizontal_combo = BlenderComboBox(self)
        for label, value in (("左对齐", "left"), ("居中", "center"), ("右对齐", "right")):
            self.horizontal_combo.addItem(label, value)
        self.vertical_combo = BlenderComboBox(self)
        for label, value in (("顶部", "top"), ("居中", "center"), ("底部", "bottom")):
            self.vertical_combo.addItem(label, value)
        self.wrap_combo = BlenderComboBox(self)
        self.wrap_combo.addItem("自动换行", "auto")
        self.wrap_combo.addItem("手动换行", "manual")
        self.italic_check = QCheckBox("斜体", self)
        self.apply_button = QPushButton("应用", self)
        self.cancel_button = QPushButton("取消", self)
        text_form = QFormLayout()
        text_form.addRow("名称", self.name_edit)
        text_form.addRow("文字", self.text_edit)
        text_form.addRow("字体", self.font_edit)
        text_form.addRow("字号", self.size_spin)
        text_form.addRow(self.bold_check)
        text_form.addRow(self.italic_check)
        text_form.addRow("文字颜色", self.color_button)
        text_form.addRow("描边颜色", self.stroke_color_button)
        text_form.addRow("描边宽度", self.stroke_width_spin)
        text_form.addRow("水平对齐", self.horizontal_combo)
        text_form.addRow("垂直对齐", self.vertical_combo)
        text_form.addRow("换行方式", self.wrap_combo)
        text_form.addRow("行距", self.line_spacing_spin)
        text_form.addRow("不透明度", self.opacity_spin)
        text_form.addRow(self.locked_check)
        transform_form = QFormLayout()
        transform_form.addRow("X", self.x_spin)
        transform_form.addRow("Y", self.y_spin)
        transform_form.addRow("宽度", self.width_spin)
        transform_form.addRow("高度", self.height_spin)
        transform_form.addRow("旋转", self.rotation_spin)
        transform_form.addRow("中心 X", self.pivot_x_spin)
        transform_form.addRow("中心 Y", self.pivot_y_spin)
        self.apply_button.hide()
        self.cancel_button.hide()
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(QLabel("文本属性", self))
        layout.addLayout(text_form)
        layout.addWidget(QLabel("矩形自由变换", self))
        layout.addLayout(transform_form)
        layout.addStretch(1)
        self.color_button.clicked.connect(self._pick_color)
        self.stroke_color_button.clicked.connect(self._pick_stroke_color)
        for edit in (self.name_edit, self.font_edit):
            edit.textChanged.connect(self.apply)
        self.name_edit.editingFinished.connect(self.refresh)
        self.text_edit.textChanged.connect(self.apply)
        for field in (
            self.stroke_width_spin,
            self.size_spin,
            self.x_spin,
            self.y_spin,
            self.width_spin,
            self.height_spin,
            self.rotation_spin,
            self.pivot_x_spin,
            self.pivot_y_spin,
            self.line_spacing_spin,
            self.opacity_spin,
        ):
            field.valueChanged.connect(self.apply)
        for check in (self.bold_check, self.italic_check, self.locked_check):
            check.toggled.connect(self.apply)
        self.horizontal_combo.currentIndexChanged.connect(self.apply)
        self.vertical_combo.currentIndexChanged.connect(self.apply)
        self.wrap_combo.currentIndexChanged.connect(self.apply)
        self.set_object(None)

    def set_object(self, text_object: TextObject | None) -> None:
        self._object = text_object
        self._syncing = True
        try:
            enabled = text_object is not None
            self.setEnabled(enabled)
            if text_object is None:
                return
            self.name_edit.setText(text_object.name)
            self.text_edit.setPlainText(text_object.text)
            self.font_edit.setText(text_object.font_family)
            self.bold_check.setChecked(text_object.font_weight >= 700)
            self._set_color(QColor(*text_object.color))
            self._set_stroke_color(QColor(*text_object.stroke_color))
            self.stroke_width_spin.setValue(text_object.stroke_width)
            self.size_spin.setValue(text_object.font_size)
            self.x_spin.setValue(text_object.frame.x)
            self.y_spin.setValue(text_object.frame.y)
            self.width_spin.setValue(text_object.frame.width)
            self.height_spin.setValue(text_object.frame.height)
            self.rotation_spin.setValue(
                math.degrees(math.atan2(text_object.transform.b, text_object.transform.a))
            )
            pivot = self._effective_pivot(text_object)
            self.pivot_x_spin.setValue(pivot.x)
            self.pivot_y_spin.setValue(pivot.y)
            self.line_spacing_spin.setValue(text_object.line_spacing)
            self.opacity_spin.setValue(text_object.opacity * 100.0)
            self.locked_check.setChecked(text_object.locked)
            self.italic_check.setChecked(text_object.italic)
            self.horizontal_combo.setCurrentIndex(
                max(0, self.horizontal_combo.findData(text_object.horizontal_alignment.value))
            )
            self.vertical_combo.setCurrentIndex(
                max(0, self.vertical_combo.findData(text_object.vertical_alignment.value))
            )
            self.wrap_combo.setCurrentIndex(
                max(0, self.wrap_combo.findData(text_object.wrap_mode.value))
            )
        finally:
            self._syncing = False

    def refresh(self) -> None:
        self.set_object(self._object)

    def set_preview_object(self, text_object: TextObject) -> None:
        """Synchronize transform fields without replacing the live selection."""
        if self._object is None or self._object.id != text_object.id:
            return
        self._syncing = True
        try:
            self.x_spin.setValue(text_object.frame.x)
            self.y_spin.setValue(text_object.frame.y)
            self.width_spin.setValue(text_object.frame.width)
            self.height_spin.setValue(text_object.frame.height)
            self.rotation_spin.setValue(
                math.degrees(math.atan2(text_object.transform.b, text_object.transform.a))
            )
            pivot = self._effective_pivot(text_object)
            self.pivot_x_spin.setValue(pivot.x)
            self.pivot_y_spin.setValue(pivot.y)
        finally:
            self._syncing = False

    def apply(self) -> None:
        text_object = self._object
        if self._syncing or text_object is None:
            return
        from easypicture.domain import (
            TextHorizontalAlignment,
            TextVerticalAlignment,
            TextWrapMode,
        )

        pivot = Point(self.pivot_x_spin.value(), self.pivot_y_spin.value())
        values = {
            "name": self.name_edit.text(),
            "text": self.text_edit.toPlainText(),
            "font_family": self.font_edit.text(),
            "font_weight": 700 if self.bold_check.isChecked() else 400,
            "font_size": self.size_spin.value(),
            "frame": FloatRect(
                self.x_spin.value(),
                self.y_spin.value(),
                self.width_spin.value(),
                self.height_spin.value(),
            ),
            "transform": self._transform_with_rotation(
                text_object.transform,
                self.rotation_spin.value(),
                self._effective_pivot(text_object),
            ),
            "transform_pivot": pivot,
            "horizontal_alignment": TextHorizontalAlignment(
                str(self.horizontal_combo.currentData())
            ),
            "vertical_alignment": TextVerticalAlignment(
                str(self.vertical_combo.currentData())
            ),
            "wrap_mode": TextWrapMode(str(self.wrap_combo.currentData())),
            "line_spacing": self.line_spacing_spin.value(),
            "italic": self.italic_check.isChecked(),
            "opacity": self.opacity_spin.value() / 100.0,
            "locked": self.locked_check.isChecked(),
            "color": (
                self._color.red(),
                self._color.green(),
                self._color.blue(),
                self._color.alpha(),
            ),
            "stroke_color": (
                self._stroke_color.red(),
                self._stroke_color.green(),
                self._stroke_color.blue(),
                self._stroke_color.alpha(),
            ),
            "stroke_width": self.stroke_width_spin.value(),
        }
        changes = {
            name: value
            for name, value in values.items()
            if (
                not text_object.transform.almost_equals(value)
                if name == "transform" and isinstance(value, AffineTransform)
                else getattr(text_object, name) != value
            )
        }
        if not changes:
            return
        if text_object.locked and changes != {"locked": False}:
            return
        if self.commands.update_text_object(text_object.id, **changes):
            self.object_changed.emit(text_object)

    @staticmethod
    def _transform_with_rotation(
        transform: AffineTransform,
        degrees: float,
        pivot: Point,
    ) -> AffineTransform:
        current = math.degrees(math.atan2(transform.b, transform.a))
        delta = degrees - current
        if math.isclose(delta, 0.0, abs_tol=1e-9):
            return transform
        around_pivot = (
            AffineTransform.translation(-pivot.x, -pivot.y)
            .then(AffineTransform.rotation(delta))
            .then(AffineTransform.translation(pivot.x, pivot.y))
        )
        return transform.then(around_pivot)

    @staticmethod
    def _effective_pivot(text_object: TextObject) -> Point:
        if text_object.transform_pivot is not None:
            return text_object.transform_pivot
        frame = text_object.frame
        return text_object.transform.map_point(
            Point(frame.x + frame.width / 2.0, frame.y + frame.height / 2.0)
        )

    def _pick_color(self) -> None:
        color = self._choose_color(self._color, "文字颜色")
        if color is not None:
            self._set_color(color)
            self.apply()

    def _pick_stroke_color(self) -> None:
        color = self._choose_color(self._stroke_color, "描边颜色")
        if color is not None:
            self._set_stroke_color(color)
            self.apply()

    def _choose_color(self, current: QColor, title: str) -> QColor | None:
        dialog = ColorPickerDialog(current, title, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected_color

    def _set_color(self, color: QColor) -> None:
        self._color = QColor(color)
        self.color_button.setStyleSheet(
            "QPushButton {{ background-color: {}; color: {}; }}".format(
                self._color.name(QColor.NameFormat.HexArgb),
                "#111111" if self._color.lightness() > 128 else "#ffffff",
            )
        )

    def _set_stroke_color(self, color: QColor) -> None:
        self._stroke_color = QColor(color)
        self.stroke_color_button.setStyleSheet(
            "QPushButton {{ background-color: {}; color: {}; }}".format(
                self._stroke_color.name(QColor.NameFormat.HexArgb),
                "#111111" if self._stroke_color.lightness() > 128 else "#ffffff",
            )
        )


__all__ = ["TextPropertiesPanel"]
