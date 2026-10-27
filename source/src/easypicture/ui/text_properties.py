"""Properties editor for editable P9A text objects."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QHideEvent, QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
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
from easypicture.ui.drag_value_spinbox import DragValueDoubleSpinBox
from easypicture.ui.numeric_ranges import (
    DOCUMENT_DIMENSION_RANGE,
    FONT_SIZE_RANGE,
    ROTATION_DEGREES_RANGE,
    SIGNED_COORDINATE_RANGE,
)
from easypicture.ui.property_binding import (
    EditTarget,
    PropertyBinding,
    PropertyEditSession,
    interaction_coordinator,
)
from easypicture.ui.property_blocks import choose_property_color


class TextPropertiesPanel(QWidget):
    object_changed = Signal(object)
    preview_changed = Signal(object)
    preview_cleared = Signal()

    def __init__(
        self,
        commands: DocumentCommandService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands = commands
        self._object: TextObject | None = None
        self._syncing = False
        self._feedback_values: dict[DragValueDoubleSpinBox, float] = {}
        self._bindings: dict[DragValueDoubleSpinBox, PropertyBinding[float]] = {}
        self._edit_session = PropertyEditSession[dict[str, Any]](
            self._edit_target,
            self._publish_properties,
            self._commit_properties,
            self.preview_cleared.emit,
            self._validate_properties,
        )
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
        self.size_spin = DragValueDoubleSpinBox(
            FONT_SIZE_RANGE.soft_minimum,
            FONT_SIZE_RANGE.soft_maximum,
            32.0,
            manual_minimum=FONT_SIZE_RANGE.hard_minimum,
            manual_maximum=FONT_SIZE_RANGE.hard_maximum,
            suffix=" px",
            parent=self,
        )
        self.x_spin = DragValueDoubleSpinBox(
            SIGNED_COORDINATE_RANGE.soft_minimum,
            SIGNED_COORDINATE_RANGE.soft_maximum,
            0.0,
            manual_minimum=SIGNED_COORDINATE_RANGE.hard_minimum,
            manual_maximum=SIGNED_COORDINATE_RANGE.hard_maximum,
            decimals=2,
            parent=self,
        )
        self.y_spin = DragValueDoubleSpinBox(
            SIGNED_COORDINATE_RANGE.soft_minimum,
            SIGNED_COORDINATE_RANGE.soft_maximum,
            0.0,
            manual_minimum=SIGNED_COORDINATE_RANGE.hard_minimum,
            manual_maximum=SIGNED_COORDINATE_RANGE.hard_maximum,
            decimals=2,
            parent=self,
        )
        self.width_spin = DragValueDoubleSpinBox(
            DOCUMENT_DIMENSION_RANGE.soft_minimum,
            DOCUMENT_DIMENSION_RANGE.soft_maximum,
            256.0,
            manual_minimum=DOCUMENT_DIMENSION_RANGE.hard_minimum,
            manual_maximum=DOCUMENT_DIMENSION_RANGE.hard_maximum,
            decimals=2,
            parent=self,
        )
        self.height_spin = DragValueDoubleSpinBox(
            DOCUMENT_DIMENSION_RANGE.soft_minimum,
            DOCUMENT_DIMENSION_RANGE.soft_maximum,
            64.0,
            manual_minimum=DOCUMENT_DIMENSION_RANGE.hard_minimum,
            manual_maximum=DOCUMENT_DIMENSION_RANGE.hard_maximum,
            decimals=2,
            parent=self,
        )
        self.rotation_spin = DragValueDoubleSpinBox(
            ROTATION_DEGREES_RANGE.soft_minimum,
            ROTATION_DEGREES_RANGE.soft_maximum,
            0.0,
            manual_minimum=ROTATION_DEGREES_RANGE.hard_minimum,
            manual_maximum=ROTATION_DEGREES_RANGE.hard_maximum,
            decimals=2,
            suffix="°",
            parent=self,
        )
        self.pivot_x_spin = DragValueDoubleSpinBox(
            SIGNED_COORDINATE_RANGE.soft_minimum,
            SIGNED_COORDINATE_RANGE.soft_maximum,
            0.0,
            manual_minimum=SIGNED_COORDINATE_RANGE.hard_minimum,
            manual_maximum=SIGNED_COORDINATE_RANGE.hard_maximum,
            decimals=2,
            parent=self,
        )
        self.pivot_y_spin = DragValueDoubleSpinBox(
            SIGNED_COORDINATE_RANGE.soft_minimum,
            SIGNED_COORDINATE_RANGE.soft_maximum,
            0.0,
            manual_minimum=SIGNED_COORDINATE_RANGE.hard_minimum,
            manual_maximum=SIGNED_COORDINATE_RANGE.hard_maximum,
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
        self.validation_label = QLabel(self)
        self.validation_label.setWordWrap(True)
        self.validation_label.hide()
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
        layout.addWidget(self.validation_label)
        layout.addWidget(QLabel("矩形自由变换", self))
        layout.addLayout(transform_form)
        layout.addStretch(1)
        self.color_button.clicked.connect(self._pick_color)
        self.stroke_color_button.clicked.connect(self._pick_stroke_color)
        for edit in (self.name_edit, self.font_edit):
            edit.textChanged.connect(self.apply)
        for text_widget in (self.name_edit, self.font_edit, self.text_edit):
            text_widget.installEventFilter(self)
        self.name_edit.editingFinished.connect(self._finish_edit)
        self.font_edit.editingFinished.connect(self._finish_edit)
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
            field.interactionStarted.connect(self._begin_edit)
            field.editingFinished.connect(self._finish_edit)
            field.interactionCanceled.connect(self._cancel_edit)
        for check in (self.bold_check, self.italic_check, self.locked_check):
            check.toggled.connect(self.apply)
        self.horizontal_combo.currentIndexChanged.connect(self.apply)
        self.vertical_combo.currentIndexChanged.connect(self.apply)
        self.wrap_combo.currentIndexChanged.connect(self.apply)
        self.set_object(None)

    def set_object(self, text_object: TextObject | None) -> None:
        if self._object is not text_object:
            was_syncing = self._syncing
            self._syncing = True
            try:
                for field in self.findChildren(DragValueDoubleSpinBox):
                    field.cancelInteraction()
                if self._edit_session.active:
                    self._edit_session.cancel()
                interaction_coordinator(self).release(self)
            finally:
                self._syncing = was_syncing
        self._object = text_object
        self._syncing = True
        try:
            enabled = text_object is not None
            self.setEnabled(enabled)
            if text_object is None:
                return
            if not self.name_edit.hasFocus():
                self.name_edit.setText(text_object.name)
            if not self.text_edit.hasFocus():
                self.text_edit.setPlainText(text_object.text)
            if not self.font_edit.hasFocus():
                self.font_edit.setText(text_object.font_family)
            self.bold_check.setChecked(text_object.font_weight >= 700)
            self._set_color(QColor(*text_object.color))
            self._set_stroke_color(QColor(*text_object.stroke_color))
            self._sync_numeric(self.stroke_width_spin, text_object.stroke_width)
            self._sync_numeric(self.size_spin, text_object.font_size)
            self._sync_numeric(self.x_spin, text_object.frame.x)
            self._sync_numeric(self.y_spin, text_object.frame.y)
            self._sync_numeric(self.width_spin, text_object.frame.width)
            self._sync_numeric(self.height_spin, text_object.frame.height)
            self._sync_numeric(
                self.rotation_spin,
                math.degrees(math.atan2(text_object.transform.b, text_object.transform.a)),
            )
            pivot = self._effective_pivot(text_object)
            self._sync_numeric(self.pivot_x_spin, pivot.x)
            self._sync_numeric(self.pivot_y_spin, pivot.y)
            self._sync_numeric(self.line_spacing_spin, text_object.line_spacing)
            self._sync_numeric(self.opacity_spin, text_object.opacity * 100.0)
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
            self._sync_numeric(self.x_spin, text_object.frame.x)
            self._sync_numeric(self.y_spin, text_object.frame.y)
            self._sync_numeric(self.width_spin, text_object.frame.width)
            self._sync_numeric(self.height_spin, text_object.frame.height)
            self._sync_numeric(
                self.rotation_spin,
                math.degrees(math.atan2(text_object.transform.b, text_object.transform.a)),
            )
            pivot = self._effective_pivot(text_object)
            self._sync_numeric(self.pivot_x_spin, pivot.x)
            self._sync_numeric(self.pivot_y_spin, pivot.y)
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
            "vertical_alignment": TextVerticalAlignment(str(self.vertical_combo.currentData())),
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
            if self._edit_session.active:
                self._edit_session.preview({})
            return
        if text_object.locked and changes != {"locked": False}:
            return
        if self._edit_session.active:
            self._edit_session.preview(changes)
        elif any(edit.hasFocus() for edit in (self.name_edit, self.font_edit, self.text_edit)):
            if self._begin_text_edit():
                self._edit_session.preview(changes)
        else:
            self._edit_session.begin({})
            self._edit_session.preview(changes)
            self._edit_session.finish()

    def _validate_properties(self, changes: dict[str, Any]) -> bool:
        if self._object is None:
            return False
        try:
            replace(self._object, **changes)
        except (ValueError, TypeError) as error:
            self.validation_label.setText(f"输入无效，将恢复原值：{error}")
            self.validation_label.show()
            return False
        self.validation_label.hide()
        return True

    def _sync_numeric(self, field: DragValueDoubleSpinBox, value: float) -> None:
        self._feedback_values[field] = value
        binding = self._bindings.get(field)
        if binding is None:
            binding = PropertyBinding(lambda: self._feedback_values[field], field.syncValue)
            self._bindings[field] = binding
        binding.refresh()

    def _edit_target(self) -> EditTarget | None:
        document = self.commands.document
        if document is None or self._object is None:
            return None
        try:
            _group, current = document.text_object_by_id(self._object.id)
        except ValueError:
            return None
        if current is not self._object:
            return None
        return EditTarget(document.id, current.id, current.revision)

    def _begin_edit(self) -> None:
        if not self._syncing:
            self._edit_session.begin({})

    def _begin_text_edit(self) -> bool:
        if self._syncing:
            return False
        if not interaction_coordinator(self).acquire(
            self,
            self._cancel_edit,
            finish=self._finish_edit,
        ):
            return False
        self._begin_edit()
        return self._edit_session.active

    def _finish_edit(self) -> None:
        if self._syncing:
            return
        if self._edit_session.active:
            self._edit_session.finish()
        interaction_coordinator(self).release(self)
        self.refresh()
        self._restore_text_fields()

    def _cancel_edit(self) -> None:
        if self._syncing:
            return
        self._edit_session.cancel()
        interaction_coordinator(self).release(self)
        self.refresh()
        self._restore_text_fields()

    def _restore_text_fields(self) -> None:
        if self._object is None:
            return
        previous, self._syncing = self._syncing, True
        try:
            if self.name_edit.text() != self._object.name:
                self.name_edit.setText(self._object.name)
            if self.font_edit.text() != self._object.font_family:
                self.font_edit.setText(self._object.font_family)
            if self.text_edit.toPlainText() != self._object.text:
                self.text_edit.setPlainText(self._object.text)
        finally:
            self._syncing = previous

    def _publish_properties(self, changes: dict[str, Any]) -> None:
        if self._object is None:
            return
        try:
            candidate = replace(self._object, **changes)
        except ValueError, TypeError:
            return
        self.preview_changed.emit(candidate)

    def _commit_properties(self, changes: dict[str, Any]) -> bool:
        if self._object is None:
            return False
        changed = self.commands.update_text_object(self._object.id, **changes)
        if changed:
            self.object_changed.emit(self._object)
        return changed

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched in (self.name_edit, self.font_edit, self.text_edit):
            if event.type() == QEvent.Type.FocusIn:
                self._begin_text_edit()
            elif event.type() == QEvent.Type.FocusOut:
                self._finish_edit()
            elif isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_Escape:
                if event.type() == QEvent.Type.KeyPress:
                    self._cancel_edit()
                    if self._object is not None:
                        was_syncing = self._syncing
                        self._syncing = True
                        try:
                            self.name_edit.setText(self._object.name)
                            self.font_edit.setText(self._object.font_family)
                            self.text_edit.setPlainText(self._object.text)
                        finally:
                            self._syncing = was_syncing
                elif event.type() == QEvent.Type.KeyRelease and isinstance(watched, QWidget):
                    watched.clearFocus()
                event.accept()
                return True
            elif (
                isinstance(event, QKeyEvent)
                and event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return)
                and watched is self.text_edit
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            ):
                if event.type() == QEvent.Type.KeyPress:
                    self._finish_edit()
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def hideEvent(self, event: QHideEvent) -> None:
        self._cancel_edit()
        super().hideEvent(event)

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
        return choose_property_color(self, current, title)

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
