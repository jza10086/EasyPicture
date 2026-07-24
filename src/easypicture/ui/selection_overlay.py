"""Persistent zoom-independent marching-ants selection outline."""

from __future__ import annotations

from collections.abc import Callable

import cv2
import numpy as np
from PySide6.QtCore import QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from easypicture.domain import Document, DocumentEvent, SelectionChanged
from easypicture.pixels import Rect, TileCoord


class SelectionOverlayItem(QGraphicsItem):
    """Draw selection contours without becoming part of document pixels."""

    def __init__(self, document: Document, timer_parent: QObject) -> None:
        super().__init__()
        self.document = document
        self.setZValue(9_000.0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)
        self._paths: dict[TileCoord, QPainterPath] = {}
        self._phase = 0.0
        self._unsubscribe: Callable[[], None] = document.subscribe(self._on_document_event)
        self._timer = QTimer(timer_parent)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._advance)
        self._sync_timer()

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, float(self.document.width), float(self.document.height))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        _ = widget
        if not self.document.selection.is_active:
            return
        exposed = option.exposedRect.intersected(self.boundingRect())
        if exposed.isEmpty():
            return
        black = self._pen(QColor(0, 0, 0, 230), self._phase)
        white = self._pen(QColor(255, 255, 255, 245), self._phase + 4.0)
        tile_size = self.document.selection.mask.tile_size
        for coord, _tile in self.document.selection.mask.iter_allocated_tiles():
            tile_rect = QRectF(
                coord.x * tile_size,
                coord.y * tile_size,
                min(tile_size, self.document.width - coord.x * tile_size),
                min(tile_size, self.document.height - coord.y * tile_size),
            )
            if not tile_rect.intersects(exposed):
                continue
            path = self._paths.get(coord)
            if path is None:
                path = self._build_path(coord)
                self._paths[coord] = path
            if path.isEmpty():
                continue
            painter.save()
            painter.setClipRect(tile_rect)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(black)
            painter.drawPath(path)
            painter.setPen(white)
            painter.drawPath(path)
            painter.restore()

    def dispose(self) -> None:
        self._timer.stop()
        self._timer.timeout.disconnect(self._advance)
        self._unsubscribe()
        self._paths.clear()

    @staticmethod
    def _pen(color: QColor, offset: float) -> QPen:
        pen = QPen(color)
        pen.setCosmetic(True)
        pen.setWidth(1)
        pen.setDashPattern([4.0, 4.0])
        pen.setDashOffset(offset)
        return pen

    def _build_path(self, coord: TileCoord) -> QPainterPath:
        store = self.document.selection.mask
        tile_size = store.tile_size
        left = coord.x * tile_size
        top = coord.y * tile_size
        halo_left = max(0, left - 1)
        halo_top = max(0, top - 1)
        halo_right = min(self.document.width, left + tile_size + 1)
        halo_bottom = min(self.document.height, top + tile_size + 1)
        mask = store.read_region(
            Rect(
                halo_left,
                halo_top,
                halo_right - halo_left,
                halo_bottom - halo_top,
            )
        )
        binary = np.ascontiguousarray(mask >= 128, dtype=np.uint8)
        if not np.any(binary):
            return QPainterPath()
        touches_document_edge = (
            halo_left == 0
            or halo_top == 0
            or halo_right == self.document.width
            or halo_bottom == self.document.height
        )
        if np.all(binary) and not touches_document_edge:
            return QPainterPath()
        contours, _hierarchy = cv2.findContours(
            binary,
            cv2.RETR_LIST,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        path = QPainterPath()
        for contour in contours:
            if len(contour) == 0:
                continue
            first = contour[0, 0]
            path.moveTo(float(first[0] + halo_left), float(first[1] + halo_top))
            for point in contour[1:, 0]:
                path.lineTo(float(point[0] + halo_left), float(point[1] + halo_top))
            path.closeSubpath()
        return path

    def _on_document_event(self, event: DocumentEvent) -> None:
        if not isinstance(event, SelectionChanged):
            return
        changed = set(event.tiles)
        invalidated = set(changed)
        for coord in changed:
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    tx = coord.x + dx
                    ty = coord.y + dy
                    if tx >= 0 and ty >= 0:
                        invalidated.add(TileCoord(tx, ty))
        for coord in invalidated:
            self._paths.pop(coord, None)
        tile_size = self.document.selection.mask.tile_size
        for coord in invalidated:
            self.update(
                QRectF(
                    coord.x * tile_size,
                    coord.y * tile_size,
                    tile_size,
                    tile_size,
                ).intersected(self.boundingRect())
            )
        self._sync_timer()

    def _sync_timer(self) -> None:
        if self.document.selection.is_active:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._paths.clear()
            self.update()

    def _advance(self) -> None:
        self._phase = (self._phase + 1.0) % 8.0
        self.update()


__all__ = ["SelectionOverlayItem"]
