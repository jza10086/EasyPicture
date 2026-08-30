"""Blender-inspired combo box with a stable painted drop-down arrow."""

from __future__ import annotations

from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QComboBox, QMenu, QWidget


class BlenderComboBox(QComboBox):
    """Draw the arrow independently of the active Windows/Qt style."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(72)
        self._default_data: object = None

    def setDefaultValue(self, data: object) -> None:
        """Declare a stable item-data default; ordinary selection never changes it."""
        self._default_data = data

    def _default_index(self) -> int:
        index = self.findData(self._default_data) if self._default_data is not None else 0
        return index if 0 <= index < self.count() else (0 if self.count() else -1)

    def resetToDefault(self) -> None:
        index = self._default_index()
        if self.isEnabled() and index >= 0 and self.currentIndex() != index:
            self.setCurrentIndex(index)
            self.activated.emit(index)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        event.accept()
        if not self.isEnabled() or not self.count():
            return
        menu = QMenu(self)
        action = menu.addAction("设为默认值")
        action.setToolTip(f"恢复为 {self.itemText(self._default_index())}")
        menu.setToolTipsVisible(True)
        action.setEnabled(self.currentIndex() != self._default_index())
        action.triggered.connect(self.resetToDefault)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(event.globalPos())

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

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Leave wheel scrolling to the containing panel instead of changing values."""
        event.ignore()


__all__ = ["BlenderComboBox"]
