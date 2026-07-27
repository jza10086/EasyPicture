"""Blender-inspired combo box with a stable painted drop-down arrow."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPainter, QPainterPath, QPaintEvent
from PySide6.QtWidgets import QComboBox, QWidget


class BlenderComboBox(QComboBox):
    """Draw the arrow independently of the active Windows/Qt style."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(72)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QColor(0, 0, 0, 0))
        painter.setBrush(QColor("#b8b8b8") if self.isEnabled() else QColor("#6d6d6d"))
        center_x = float(self.width() - 10)
        center_y = float(self.height()) / 2.0 + 0.5
        arrow = QPainterPath()
        arrow.moveTo(center_x - 3.5, center_y - 2.0)
        arrow.lineTo(center_x + 3.5, center_y - 2.0)
        arrow.lineTo(center_x, center_y + 2.0)
        arrow.closeSubpath()
        painter.drawPath(arrow)


__all__ = ["BlenderComboBox"]
