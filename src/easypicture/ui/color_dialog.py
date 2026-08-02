"""Application-owned dark color picker inspired by Blender's HSV circle."""

from __future__ import annotations

import math
import re

from PySide6.QtCore import QPoint, QPointF, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QHideEvent,
    QImage,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from easypicture.ui.drag_value_spinbox import DragValueSpinBox, NumericEditState

_HEX_PATTERN = re.compile(r"^#?([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class _ColorWheel(QWidget):
    """HSV wheel: angle is hue and radius is saturation."""

    changed = Signal(int, int)
    valueStepRequested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hue = 0
        self._saturation = 0
        self._wheel_image = QImage()
        self.setFixedSize(220, 220)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def sizeHint(self) -> QSize:
        return QSize(220, 220)

    def set_hs(self, hue: int, saturation: int) -> None:
        resolved = (hue % 360, max(0, min(100, saturation)))
        if resolved == (self._hue, self._saturation):
            return
        self._hue, self._saturation = resolved
        self.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._wheel_image = QImage()
        super().resizeEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        _ = event
        if (
            self._wheel_image.isNull()
            or self._wheel_image.size() != self.size()
        ):
            self._wheel_image = self._make_wheel_image()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.drawImage(0, 0, self._wheel_image)

        center, radius = self._geometry()
        angle = math.radians(self._hue + 90.0)
        distance = radius * self._saturation / 100.0
        marker = QPointF(
            center.x() + math.cos(angle) * distance,
            center.y() + math.sin(angle) * distance,
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#111111"), 3.0))
        painter.drawEllipse(marker, 4.5, 4.5)
        painter.setPen(QPen(QColor("#eeeeee"), 1.0))
        painter.drawEllipse(marker, 4.5, 4.5)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._update_from_position(event.position(), event.modifiers())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._update_from_position(event.position(), event.modifiers())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta:
            self.valueStepRequested.emit(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def _geometry(self) -> tuple[QPointF, float]:
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        return center, max(1.0, min(self.width(), self.height()) / 2.0 - 3.0)

    def _make_wheel_image(self) -> QImage:
        image = QImage(
            max(1, self.width()),
            max(1, self.height()),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(Qt.GlobalColor.transparent)
        center, radius = self._geometry()
        radius_squared = radius * radius
        for y in range(image.height()):
            dy = y + 0.5 - center.y()
            for x in range(image.width()):
                dx = x + 0.5 - center.x()
                distance_squared = dx * dx + dy * dy
                if distance_squared > radius_squared:
                    continue
                saturation = min(255, round(math.sqrt(distance_squared) / radius * 255))
                angle = math.degrees(math.atan2(dy, dx))
                hue = round((angle - 90.0) % 360.0) % 360
                image.setPixelColor(x, y, QColor.fromHsv(hue, saturation, 255))
        return image

    def _update_from_position(
        self,
        position: QPointF,
        modifiers: Qt.KeyboardModifier,
    ) -> None:
        center, radius = self._geometry()
        dx = position.x() - center.x()
        dy = position.y() - center.y()
        distance = math.hypot(dx, dy)
        hue = round((math.degrees(math.atan2(dy, dx)) - 90.0) % 360.0) % 360
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            hue = round(hue / 30.0) * 30 % 360
        saturation = round(min(1.0, distance / radius) * 100)
        self._hue = hue
        self._saturation = saturation
        self.update()
        self.changed.emit(hue, saturation)


class _ValueBar(QWidget):
    """Vertical HSV value control using the wheel's hue and saturation."""

    changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hue = 0
        self._saturation = 0
        self._value = 100
        self.setFixedSize(22, 220)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def set_hsv(self, hue: int, saturation: int, value: int) -> None:
        resolved = (
            hue % 360,
            max(0, min(100, saturation)),
            max(0, min(100, value)),
        )
        if resolved == (self._hue, self._saturation, self._value):
            return
        self._hue, self._saturation, self._value = resolved
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        _ = event
        painter = QPainter(self)
        gradient = QLinearGradient(0.0, 1.0, 0.0, float(self.height() - 1))
        gradient.setColorAt(
            0.0,
            QColor.fromHsv(
                self._hue,
                round(self._saturation * 255 / 100),
                255,
            ),
        )
        gradient.setColorAt(1.0, QColor("#050505"))
        painter.fillRect(self.rect(), gradient)
        painter.setPen(QPen(QColor("#111111"), 1.0))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

        y = (1.0 - self._value / 100.0) * max(0, self.height() - 1)
        path = QPainterPath()
        path.moveTo(0.0, y)
        path.lineTo(5.0, y - 4.0)
        path.lineTo(5.0, y + 4.0)
        path.closeSubpath()
        painter.fillPath(path, QColor("#eeeeee"))
        painter.setPen(QPen(QColor("#111111"), 1.0))
        painter.drawPath(path)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._update_from_y(event.position().y())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._update_from_y(event.position().y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def _update_from_y(self, y: float) -> None:
        self._value = round(
            (
                1.0
                - min(1.0, max(0.0, y / max(1, self.height() - 1)))
            )
            * 100
        )
        self.update()
        self.changed.emit(self._value)


class _ColorPreview(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(Qt.GlobalColor.black)
        self.setFixedHeight(24)

    def set_color(self, color: QColor) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        _ = event
        painter = QPainter(self)
        cell = 6
        for y in range(0, self.height(), cell):
            for x in range(0, self.width(), cell):
                shade = 180 if (x // cell + y // cell) % 2 == 0 else 115
                painter.fillRect(x, y, cell, cell, QColor(shade, shade, shade))
        painter.fillRect(self.rect(), self._color)
        painter.setPen(QColor("#111111"))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))


class ColorPickerDialog(QDialog):
    """Compact modal HSV-circle picker used for foreground/background colors."""

    def __init__(
        self,
        color: QColor,
        title: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedWidth(292)
        self._color = QColor(color) if color.isValid() else QColor(Qt.GlobalColor.black)
        self._syncing = False
        self._eyedropper_active = False
        self._eyedropper_cursor_active = False
        self.setStyleSheet(
            """
            QDialog { background: #242424; color: #dddddd; }
            QLabel { color: #dddddd; }
            QPushButton, QToolButton {
                background: #3a3a3a; color: #dddddd;
                border: 1px solid #171717; border-radius: 3px;
                padding: 3px 7px;
            }
            QPushButton:hover, QToolButton:hover { background: #4a4a4a; }
            QPushButton:checked {
                background: #4b72a8; color: #ffffff;
                border-color: #5d83b8;
            }
            QLineEdit {
                background: #1f1f1f; color: #eeeeee;
                border: 1px solid #151515; border-radius: 2px;
                padding: 3px 5px;
            }
            """
        )

        self.color_wheel = _ColorWheel(self)
        self.value_bar = _ValueBar(self)
        self.preview = _ColorPreview(self)

        self.hue_spin = self._spin(0, 359, "°")
        self.saturation_spin = self._spin(0, 100, "%")
        self.value_spin = self._spin(0, 100, "%")
        self.red_spin = self._spin(0, 255)
        self.green_spin = self._spin(0, 255)
        self.blue_spin = self._spin(0, 255)
        self.alpha_spin = self._spin(0, 255)
        self.hex_edit = QLineEdit(self)
        self.hex_edit.setPlaceholderText("#RRGGBB 或 #RRGGBBAA")

        picker_layout = QHBoxLayout()
        picker_layout.setContentsMargins(0, 0, 0, 0)
        picker_layout.setSpacing(8)
        picker_layout.addWidget(self.color_wheel)
        picker_layout.addWidget(self.value_bar)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        mode_layout = QHBoxLayout()
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(1)
        self.mode_buttons: dict[str, QPushButton] = {}
        for index, name in enumerate(("RGB", "HSV", "Hex")):
            button = QPushButton(name, self)
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setMinimumHeight(24)
            self.mode_group.addButton(button, index)
            mode_layout.addWidget(button, 1)
            self.mode_buttons[name] = button
        self.mode_buttons["HSV"].setChecked(True)

        self.mode_stack = QStackedWidget(self)
        self.rgb_page = self._field_page(
            (("红色 R", self.red_spin), ("绿色 G", self.green_spin), ("蓝色 B", self.blue_spin))
        )
        self.hsv_page = self._field_page(
            (
                ("色相 H", self.hue_spin),
                ("饱和度 S", self.saturation_spin),
                ("明度 V", self.value_spin),
            )
        )
        self.hex_page = QWidget(self)
        hex_layout = QGridLayout(self.hex_page)
        hex_layout.setContentsMargins(0, 0, 0, 0)
        hex_layout.setHorizontalSpacing(6)
        hex_layout.addWidget(QLabel("十六进制", self.hex_page), 0, 0)
        hex_layout.addWidget(self.hex_edit, 0, 1)
        self.mode_stack.addWidget(self.rgb_page)
        self.mode_stack.addWidget(self.hsv_page)
        self.mode_stack.addWidget(self.hex_page)
        self.mode_stack.setCurrentWidget(self.hsv_page)

        alpha_layout = QGridLayout()
        alpha_layout.setContentsMargins(0, 0, 0, 0)
        alpha_layout.setHorizontalSpacing(6)
        alpha_layout.addWidget(QLabel("透明度 A", self), 0, 0)
        alpha_layout.addWidget(self.alpha_spin, 0, 1)

        utility_layout = QHBoxLayout()
        utility_layout.setContentsMargins(0, 0, 0, 0)
        utility_layout.setSpacing(6)
        utility_layout.addWidget(self.preview, 1)
        self.eyedropper_button = QToolButton(self)
        self.eyedropper_button.setText("吸管")
        self.eyedropper_button.setToolTip("从屏幕取样颜色")
        self.eyedropper_button.setAutoRaise(False)
        utility_layout.addWidget(self.eyedropper_button)

        buttons = QDialogButtonBox(self)
        accept_button = buttons.addButton("确定", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        accept_button.setDefault(True)
        cancel_button.setAutoDefault(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)
        root.addLayout(picker_layout)
        root.addLayout(mode_layout)
        root.addWidget(self.mode_stack)
        root.addLayout(alpha_layout)
        root.addLayout(utility_layout)
        root.addWidget(buttons)

        self.color_wheel.changed.connect(self._wheel_changed)
        self.color_wheel.valueStepRequested.connect(self._wheel_value_step)
        self.value_bar.changed.connect(self._value_bar_changed)
        self.mode_group.idClicked.connect(self._mode_changed)
        self.eyedropper_button.clicked.connect(self._start_eyedropper)
        for field in (self.hue_spin, self.saturation_spin, self.value_spin):
            field.valueChanged.connect(self._hsv_fields_changed)
        for field in (self.red_spin, self.green_spin, self.blue_spin):
            field.valueChanged.connect(self._rgba_fields_changed)
        self.alpha_spin.valueChanged.connect(self._alpha_changed)
        self.hex_edit.editingFinished.connect(self._hex_changed)
        self._sync_widgets()

    @property
    def selected_color(self) -> QColor:
        return QColor(self._color)

    def _spin(self, minimum: int, maximum: int, suffix: str = "") -> DragValueSpinBox:
        spin = DragValueSpinBox(
            minimum,
            maximum,
            minimum,
            suffix=suffix,
            parent=self,
        )
        spin.setMinimumWidth(145)
        return spin

    def _field_page(
        self,
        rows: tuple[tuple[str, DragValueSpinBox], ...],
    ) -> QWidget:
        page = QWidget(self)
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(3)
        for row, (label, field) in enumerate(rows):
            layout.addWidget(QLabel(label, page), row, 0)
            layout.addWidget(field, row, 1)
        return page

    def _set_color(self, color: QColor) -> None:
        if not color.isValid():
            return
        self._color = QColor(color)
        self._sync_widgets()

    def _wheel_changed(self, hue: int, saturation: int) -> None:
        if self._syncing:
            return
        self._set_color(
            QColor.fromHsv(
                hue,
                round(saturation * 255 / 100),
                self._color.value(),
                self._color.alpha(),
            )
        )

    def _wheel_value_step(self, step: int) -> None:
        value = max(0, min(100, self.value_spin.value() + step))
        self._value_bar_changed(value)

    def _value_bar_changed(self, value: int) -> None:
        if self._syncing:
            return
        self._set_color(
            QColor.fromHsv(
                max(0, self._color.hsvHue()),
                self._color.hsvSaturation(),
                round(value * 255 / 100),
                self._color.alpha(),
            )
        )

    def _hsv_fields_changed(self, _value: int) -> None:
        if self._syncing:
            return
        self._set_color(
            QColor.fromHsv(
                self.hue_spin.value(),
                round(self.saturation_spin.value() * 255 / 100),
                round(self.value_spin.value() * 255 / 100),
                self._color.alpha(),
            )
        )

    def _rgba_fields_changed(self, _value: int) -> None:
        if self._syncing:
            return
        self._set_color(
            QColor(
                self.red_spin.value(),
                self.green_spin.value(),
                self.blue_spin.value(),
                self._color.alpha(),
            )
        )

    def _alpha_changed(self, alpha: int) -> None:
        if self._syncing:
            return
        color = QColor(self._color)
        color.setAlpha(alpha)
        self._set_color(color)

    def _mode_changed(self, mode_id: int) -> None:
        self.mode_stack.setCurrentIndex(mode_id)

    def _hex_changed(self) -> None:
        match = _HEX_PATTERN.fullmatch(self.hex_edit.text().strip())
        if match is None:
            self._sync_widgets()
            return
        payload = match.group(1)
        if len(payload) in (3, 4):
            payload = "".join(character * 2 for character in payload)
        color = QColor(f"#{payload[:6]}")
        color.setAlpha(int(payload[6:8], 16) if len(payload) == 8 else self._color.alpha())
        self._set_color(color)

    def _start_eyedropper(self) -> None:
        if self._eyedropper_active:
            return
        self._eyedropper_active = True
        self.eyedropper_button.setText("取色中")
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.CrossCursor))
        self._eyedropper_cursor_active = True
        self.grabMouse()
        self.grabKeyboard()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._eyedropper_active:
            if event.button() == Qt.MouseButton.LeftButton:
                self._sample_screen_color(event.globalPosition().toPoint())
                self._finish_eyedropper()
                event.accept()
                return
            if event.button() == Qt.MouseButton.RightButton:
                self._finish_eyedropper()
                event.accept()
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            self._eyedropper_active
            and event.key() == Qt.Key.Key_Escape
        ):
            self._finish_eyedropper()
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        self._finish_eyedropper()
        super().hideEvent(event)

    def _sample_screen_color(self, position: QPoint) -> None:
        screen = QGuiApplication.screenAt(position)
        if screen is None:
            return
        local = position - screen.geometry().topLeft()
        image = screen.grabWindow(0, local.x(), local.y(), 1, 1).toImage()
        if image.isNull():
            return
        resolved = image.pixelColor(0, 0)
        resolved.setAlpha(self._color.alpha())
        self._set_color(resolved)

    def _finish_eyedropper(self) -> None:
        if not self._eyedropper_active and not self._eyedropper_cursor_active:
            return
        self._eyedropper_active = False
        self.eyedropper_button.setText("吸管")
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()
        if QWidget.keyboardGrabber() is self:
            self.releaseKeyboard()
        if self._eyedropper_cursor_active:
            QApplication.restoreOverrideCursor()
            self._eyedropper_cursor_active = False

    def _sync_widgets(self) -> None:
        hue = max(0, self._color.hsvHue())
        saturation = round(self._color.hsvSaturation() * 100 / 255)
        value = round(self._color.value() * 100 / 255)
        values = (
            (self.hue_spin, hue),
            (self.saturation_spin, saturation),
            (self.value_spin, value),
            (self.red_spin, self._color.red()),
            (self.green_spin, self._color.green()),
            (self.blue_spin, self._color.blue()),
            (self.alpha_spin, self._color.alpha()),
        )
        self._syncing = True
        try:
            idle_values = tuple(
                (field, resolved)
                for field, resolved in values
                if field.interaction_state is NumericEditState.IDLE
            )
            blockers = [QSignalBlocker(field) for field, _value in idle_values]
            for field, resolved in idle_values:
                field.setValue(resolved)
            del blockers
            if not self.hex_edit.hasFocus():
                hex_value = self._color.name(QColor.NameFormat.HexRgb)
                if self._color.alpha() != 255:
                    hex_value += f"{self._color.alpha():02x}"
                self.hex_edit.setText(hex_value)
            self.color_wheel.set_hs(hue, saturation)
            self.value_bar.set_hsv(hue, saturation, value)
            self.preview.set_color(self._color)
        finally:
            self._syncing = False


__all__ = ["ColorPickerDialog"]
