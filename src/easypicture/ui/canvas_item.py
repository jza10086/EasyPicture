"""Checkerboard canvas item backed by a progressive Tile renderer."""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from easypicture.domain import Size
from easypicture.pixels import TileCoord
from easypicture.render import TileRenderer


class CanvasItem(QGraphicsItem):
    """Draw only the transparent checkerboard and document boundary."""

    CHECKER_SIZE = 16

    def __init__(self, size: Size, renderer: TileRenderer) -> None:
        super().__init__()
        self._size = size
        self.renderer = renderer
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)
        self._checker = self._create_checker_pattern()
        self.renderer.tile_ready.connect(self._on_tile_ready)
        self.renderer.cache_invalidated.connect(self._on_cache_invalidated)

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, float(self._size.width), float(self._size.height))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        _ = widget
        painter.fillRect(self.boundingRect(), self._checker)
        exposed = option.exposedRect.intersected(self.boundingRect())
        needs_render: list[TileCoord] = []
        for coord in self._coords_for_rect(exposed):
            image = self.renderer.cached_tile(coord)
            if image is None:
                needs_render.append(coord)
                continue
            painter.drawImage(
                coord.x * self.renderer.tile_size,
                coord.y * self.renderer.tile_size,
                image,
            )
            if self.renderer.is_tile_stale(coord):
                needs_render.append(coord)
        if needs_render:
            self.renderer.request_tiles(needs_render, exposed.center())
        border = QPen(QColor("#707070"))
        border.setCosmetic(True)
        border.setWidth(1)
        painter.setPen(border)
        painter.drawRect(self.boundingRect())

    def dispose(self) -> None:
        self.renderer.tile_ready.disconnect(self._on_tile_ready)
        self.renderer.cache_invalidated.disconnect(self._on_cache_invalidated)
        self.renderer.dispose()

    def _coords_for_rect(self, rect: QRectF) -> list[TileCoord]:
        if rect.isEmpty():
            return []
        tile_size = self.renderer.tile_size
        first_tx = max(0, int(rect.left()) // tile_size)
        first_ty = max(0, int(rect.top()) // tile_size)
        last_tx = max(0, int(max(rect.left(), rect.right() - 0.0001)) // tile_size)
        last_ty = max(0, int(max(rect.top(), rect.bottom() - 0.0001)) // tile_size)
        return [
            TileCoord(tx, ty)
            for ty in range(first_ty, last_ty + 1)
            for tx in range(first_tx, last_tx + 1)
            if tx * tile_size < self._size.width and ty * tile_size < self._size.height
        ]

    def _on_tile_ready(self, coord: object) -> None:
        if not isinstance(coord, TileCoord):
            return
        tile_size = self.renderer.tile_size
        self.update(
            QRectF(
                coord.x * tile_size,
                coord.y * tile_size,
                tile_size,
                tile_size,
            ).intersected(self.boundingRect())
        )

    def _on_cache_invalidated(self, coords: object) -> None:
        if coords is None:
            self.update()
            return
        if not isinstance(coords, frozenset):
            return
        for coord in coords:
            self._on_tile_ready(coord)

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
