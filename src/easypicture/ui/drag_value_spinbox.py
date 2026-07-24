"""Blender-style integer input with horizontal dragging and range fill."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QAbstractSpinBox, QSpinBox, QWidget


class DragValueSpinBox(QSpinBox):
    """Use the line edit as both a numeric editor and a horizontal drag surface."""

    def __init__(
        self,
        drag_minimum: int,
        drag_maximum: int,
        value: int,
        *,
        manual_minimum: int | None = None,
        manual_maximum: int | None = None,
        suffix: str = "",
        drag_step: float = 1.0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if drag_minimum > drag_maximum:
            raise ValueError("drag minimum must not exceed drag maximum")
        resolved_manual_minimum = (
            drag_minimum if manual_minimum is None else manual_minimum
        )
        resolved_manual_maximum = (
            drag_maximum if manual_maximum is None else manual_maximum
        )
        if (
            resolved_manual_minimum > drag_minimum
            or resolved_manual_maximum < drag_maximum
        ):
            raise ValueError("manual range must contain the drag range")
        if drag_step <= 0.0:
            raise ValueError("drag step must be positive")

        self._drag_minimum = drag_minimum
        self._drag_maximum = drag_maximum
        self._drag_step = drag_step
        self._press_position: QPoint | None = None
        self._press_value = value
        self._dragging = False

        self.setRange(resolved_manual_minimum, resolved_manual_maximum)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setSuffix(suffix)
        self.setValue(value)
        self.setToolTip("左右拖动修改；单击后可键盘输入")
        editor = self.lineEdit()
        editor.setCursor(Qt.CursorShape.SizeHorCursor)
        editor.installEventFilter(self)
        self.valueChanged.connect(self._update_fill)
        self._update_fill(self.value())

    @property
    def drag_minimum(self) -> int:
        return self._drag_minimum

    @property
    def drag_maximum(self) -> int:
        return self._drag_maximum

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self.lineEdit() or not isinstance(event, QMouseEvent):
            return super().eventFilter(watched, event)
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._press_position = event.globalPosition().toPoint()
            self._press_value = self.value()
            self._dragging = False
            return False
        if event.type() == QEvent.Type.MouseMove and self._press_position is not None:
            delta_x = event.globalPosition().toPoint().x() - self._press_position.x()
            if not self._dragging and abs(delta_x) < 3:
                return False
            self._dragging = True
            precision = (
                0.1
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                else 1.0
            )
            dragged = self._press_value + round(delta_x * self._drag_step * precision)
            self.setValue(
                max(self._drag_minimum, min(self._drag_maximum, dragged))
            )
            return True
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            was_dragging = self._dragging
            self._press_position = None
            self._dragging = False
            if was_dragging:
                self.lineEdit().deselect()
                return True
        return super().eventFilter(watched, event)

    def _update_fill(self, value: int) -> None:
        extent = self._drag_maximum - self._drag_minimum
        ratio = (
            1.0
            if extent <= 0
            else (value - self._drag_minimum) / float(extent)
        )
        ratio = max(0.0, min(1.0, ratio))
        if ratio <= 0.0:
            background = "#1f1f1f"
        elif ratio >= 1.0:
            background = "#365f91"
        else:
            edge = min(1.0, ratio + 0.001)
            background = (
                "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                f"stop:0 #365f91, stop:{ratio:.4f} #365f91, "
                f"stop:{edge:.4f} #1f1f1f, stop:1 #1f1f1f)"
            )
        self.setStyleSheet(
            f"""
            QSpinBox {{
                color: #eeeeee;
                background: {background};
                border: 1px solid #151515;
                border-radius: 2px;
                padding: 2px 4px;
                min-height: 19px;
                selection-background-color: #5d88c7;
                selection-color: #ffffff;
            }}
            QSpinBox::up-button,
            QSpinBox::down-button {{
                width: 0;
                height: 0;
                border: 0;
            }}
            QLineEdit {{
                color: #eeeeee;
                background: transparent;
                border: 0;
                padding: 0;
                selection-background-color: #5d88c7;
                selection-color: #ffffff;
            }}
            """
        )


__all__ = ["DragValueSpinBox"]
