"""Persistent zoom-independent solid selection outline."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import numpy as np
from PySide6.QtCore import QObject, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsRectItem, QStyleOptionGraphicsItem, QWidget

from easypicture.domain import Document, DocumentEvent, SelectionChanged
from easypicture.pixels import Rect, TileCoord
from easypicture.ui.selection_style import (
    DEFAULT_SELECTION_OUTLINE_COLOR,
    DEFAULT_SELECTION_OUTLINE_WIDTH,
    MAX_SELECTION_OUTLINE_WIDTH,
)


class SelectionOverlayItem(QGraphicsRectItem):
    """Draw selection contours without becoming part of document pixels."""

    def __init__(
        self,
        document: Document,
        timer_parent: QObject,
        color: QColor | None = None,
        width: float = DEFAULT_SELECTION_OUTLINE_WIDTH,
    ) -> None:
        super().__init__(
            0.0,
            0.0,
            float(document.width),
            float(document.height),
        )
        _ = timer_parent
        self.document = document
        self._color = QColor(color or DEFAULT_SELECTION_OUTLINE_COLOR)
        self._color.setAlpha(255)
        self._width = min(float(MAX_SELECTION_OUTLINE_WIDTH), max(1.0, width))
        self.setZValue(9_000.0)
        self.setFlag(
            QGraphicsRectItem.GraphicsItemFlag.ItemUsesExtendedStyleOption,
            True,
        )
        self._paths: dict[TileCoord, QPainterPath] = {}
        self._unsubscribe: Callable[[], None] = document.subscribe(self._on_document_event)

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
        outline = self._pen(self._color, self._width)
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
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(outline)
            painter.drawPath(path)
            painter.restore()

    def dispose(self) -> None:
        self._unsubscribe()
        self._paths.clear()

    @property
    def outline_color(self) -> QColor:
        return QColor(self._color)

    @property
    def outline_width(self) -> float:
        return self._width

    def set_style(self, color: QColor, width: float) -> None:
        resolved_color = QColor(color)
        resolved_color.setAlpha(255)
        resolved_width = min(float(MAX_SELECTION_OUTLINE_WIDTH), max(1.0, width))
        if resolved_color == self._color and resolved_width == self._width:
            return
        self._color = resolved_color
        self._width = resolved_width
        self.update()

    @staticmethod
    def _pen(color: QColor, width: float) -> QPen:
        pen = QPen(color)
        pen.setCosmetic(True)
        pen.setWidthF(width)
        pen.setStyle(Qt.PenStyle.SolidLine)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen

    def _build_path(self, coord: TileCoord) -> QPainterPath:
        store = self.document.selection.mask
        tile_size = store.tile_size
        left = coord.x * tile_size
        top = coord.y * tile_size
        width = min(tile_size, self.document.width - left)
        height = min(tile_size, self.document.height - top)
        if width <= 0 or height <= 0:
            return QPainterPath()
        halo_left = max(0, left - 1)
        halo_top = max(0, top - 1)
        halo_right = min(self.document.width, left + width + 1)
        halo_bottom = min(self.document.height, top + height + 1)
        mask = store.read_region(
            Rect(
                halo_left,
                halo_top,
                halo_right - halo_left,
                halo_bottom - halo_top,
            )
        )
        padded = np.zeros((height + 2, width + 2), dtype=np.bool_)
        target_left = halo_left - left + 1
        target_top = halo_top - top + 1
        padded[
            target_top : target_top + mask.shape[0],
            target_left : target_left + mask.shape[1],
        ] = mask >= 128
        core = padded[1 : height + 1, 1 : width + 1]
        if not np.any(core):
            return QPainterPath()

        top_edges = core & ~padded[0:height, 1 : width + 1]
        bottom_edges = core & ~padded[2 : height + 2, 1 : width + 1]
        left_edges = core & ~padded[1 : height + 1, 0:width]
        right_edges = core & ~padded[1 : height + 1, 2 : width + 2]
        path = QPainterPath()
        self._append_horizontal_edges(path, top_edges, left, top, offset_y=0)
        self._append_horizontal_edges(path, bottom_edges, left, top, offset_y=1)
        self._append_vertical_edges(path, left_edges, left, top, offset_x=0)
        self._append_vertical_edges(path, right_edges, left, top, offset_x=1)
        return path

    @classmethod
    def _append_horizontal_edges(
        cls,
        path: QPainterPath,
        edges: np.ndarray,
        left: int,
        top: int,
        *,
        offset_y: int,
    ) -> None:
        for row_index, row in enumerate(edges):
            y = float(top + row_index + offset_y)
            for start, end in cls._true_runs(row):
                path.moveTo(float(left + start), y)
                path.lineTo(float(left + end), y)

    @classmethod
    def _append_vertical_edges(
        cls,
        path: QPainterPath,
        edges: np.ndarray,
        left: int,
        top: int,
        *,
        offset_x: int,
    ) -> None:
        for column_index, column in enumerate(edges.T):
            x = float(left + column_index + offset_x)
            for start, end in cls._true_runs(column):
                path.moveTo(x, float(top + start))
                path.lineTo(x, float(top + end))

    @staticmethod
    def _true_runs(flags: np.ndarray) -> Iterator[tuple[int, int]]:
        bordered = np.zeros(flags.size + 2, dtype=np.bool_)
        bordered[1:-1] = flags
        transitions = np.flatnonzero(bordered[1:] != bordered[:-1])
        for start, end in zip(transitions[0::2], transitions[1::2], strict=True):
            yield int(start), int(end)

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
        if not self.document.selection.is_active:
            self._paths.clear()
            self.update()


__all__ = ["SelectionOverlayItem"]
