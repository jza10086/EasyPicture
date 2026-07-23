"""P1 canvas placeholder item with no layer compositing."""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from easypicture.domain import Size


class CanvasItem(QGraphicsItem):
    """Draw only the transparent checkerboard and document boundary."""

    CHECKER_SIZE = 16

    def __init__(self, size: Size) -> None:
        super().__init__()
        self._size = size
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)
        self._checker = self._create_checker_pattern()

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, float(self._size.width), float(self._size.height))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        _ = option, widget
        painter.fillRect(self.boundingRect(), self._checker)
        border = QPen(QColor("#707070"))
        border.setCosmetic(True)
        border.setWidth(1)
        painter.setPen(border)
        painter.drawRect(self.boundingRect())

    @classmethod
    def _create_checker_pattern(cls) -> QPixmap:
        extent = cls.CHECKER_SIZE * 2
        pattern = QPixmap(extent, extent)
        pattern.fill(QColor("#f0f0f0"))
        painter = QPainter(pattern)
        painter.fillRect(0, 0, cls.CHECKER_SIZE, cls.CHECKER_SIZE, QColor("#cfcfcf"))
        painter.fillRect(
            cls.CHECKER_SIZE,
            cls.CHECKER_SIZE,
            cls.CHECKER_SIZE,
            cls.CHECKER_SIZE,
            QColor("#cfcfcf"),
        )
        painter.end()
        return pattern


__all__ = ["CanvasItem"]
