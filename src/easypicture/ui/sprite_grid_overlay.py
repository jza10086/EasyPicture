"""Non-exported pixel and sprite-cell grid overlay."""

from __future__ import annotations

import math
from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from easypicture.domain import Document


class SpriteGridOverlayItem(QGraphicsItem):
    """Draw document pixel and cell grids using cosmetic screen-space pens."""

    def __init__(self, document: Document, zoom_provider: Callable[[], float]) -> None:
        super().__init__()
        self._document = document
        self._zoom_provider = zoom_provider
        self.pixel_grid_visible = False
        self.cell_grid_visible = True
        self.setZValue(500.0)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, float(self._document.width), float(self._document.height))

    def set_visibility(self, *, pixel_grid: bool, cell_grid: bool) -> None:
        self.pixel_grid_visible = pixel_grid
        self.cell_grid_visible = cell_grid
        self.update()

    def document_size_changed(self) -> None:
        self.prepareGeometryChange()
        self.update()

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        _ = widget
        exposed = option.exposedRect.intersected(self.boundingRect())
        if exposed.isEmpty():
            return
        if self.pixel_grid_visible and self._zoom_provider() >= 8.0:
            self._draw_grid(
                painter,
                exposed,
                1,
                1,
                0,
                0,
                QColor(30, 30, 30, 100),
            )
        if self.cell_grid_visible:
            workspace = self._document.sprite_workspace
            self._draw_grid(
                painter,
                exposed,
                workspace.cell_size.width,
                workspace.cell_size.height,
                0,
                0,
                QColor(255, 138, 0, 180),
            )

    @staticmethod
    def _draw_grid(
        painter: QPainter,
        exposed: QRectF,
        step_x: int,
        step_y: int,
        offset_x: int,
        offset_y: int,
        color: QColor,
    ) -> None:
        pen = QPen(color)
        pen.setCosmetic(True)
        pen.setWidth(1)
        painter.save()
        painter.setPen(pen)
        first_x = offset_x + math.ceil((exposed.left() - offset_x) / step_x) * step_x
        first_y = offset_y + math.ceil((exposed.top() - offset_y) / step_y) * step_y
        x = first_x
        while x <= exposed.right():
            painter.drawLine(QPointF(x, exposed.top()), QPointF(x, exposed.bottom()))
            x += step_x
        y = first_y
        while y <= exposed.bottom():
            painter.drawLine(QPointF(exposed.left(), y), QPointF(exposed.right(), y))
            y += step_y
        painter.restore()


__all__ = ["SpriteGridOverlayItem"]
