"""Fixed document bounds item used only for scene geometry and navigation."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QPen
from PySide6.QtWidgets import QGraphicsRectItem

from easypicture.domain import Size


class CanvasBoundsItem(QGraphicsRectItem):
    """Represent the document rectangle without owning any raster painting."""

    def __init__(self, size: Size) -> None:
        super().__init__(0.0, 0.0, float(size.width), float(size.height))
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)


__all__ = ["CanvasBoundsItem"]
