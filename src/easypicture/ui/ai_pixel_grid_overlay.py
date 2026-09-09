"""Display-only manual AI sampling grid and actual rounded selection coverage."""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from easypicture.domain import Size
from easypicture.imaging.ai_pixel_cleanup import AiPixelCleanupParameters
from easypicture.pixels import TileCoord
from easypicture.render.qimage_bridge import rgba_tile_to_qimage
from easypicture.ui.sprite_grid_overlay import SpriteGridOverlayItem


class AiPixelGridOverlay(QGraphicsItem):
    def __init__(self, size: Size, parameters: AiPixelCleanupParameters) -> None:
        super().__init__()
        self.size = size
        self.parameters = parameters
        self.masks: Mapping[TileCoord, np.ndarray] = {}
        self._images: dict[TileCoord, QImage] = {}
        self.setZValue(550)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.size.width, self.size.height)

    def set_parameters(self, parameters: AiPixelCleanupParameters) -> None:
        self.parameters = parameters
        self.masks = {}
        self._images.clear()
        self.update()

    def set_masks(self, masks: Mapping[TileCoord, np.ndarray]) -> None:
        self.masks = masks
        self._images.clear()
        self.update()

    def paint(
        self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None
    ) -> None:
        _ = widget
        params = self.parameters
        if not params.grid_enabled:
            return
        rect = option.exposedRect.intersected(self.boundingRect())
        bw, bh, ox, oy = params.block_width, params.block_height, params.offset_x, params.offset_y
        painter.save()
        painter.setClipRect(self.boundingRect())
        left = max(0, math.floor((rect.left() + ox) / (256 * bw)))
        top = max(0, math.floor((rect.top() + oy) / (256 * bh)))
        for ty in range(top, math.ceil((rect.bottom() + oy) / (256 * bh))):
            for tx in range(left, math.ceil((rect.right() + ox) / (256 * bw))):
                coord = TileCoord(tx, ty)
                mask = self.masks.get(coord)
                if mask is None:
                    continue
                if coord not in self._images:
                    rgba = np.zeros((256, 256, 4), dtype=np.uint8)
                    rgba[..., 3] = ((255 - mask.astype(np.uint16)) * 90 // 255).astype(np.uint8)
                    self._images[coord] = rgba_tile_to_qimage(rgba)
                painter.drawImage(
                    QRectF(tx * 256 * bw - ox, ty * 256 * bh - oy, 256 * bw, 256 * bh),
                    self._images[coord],
                )
        scale = abs(painter.transform().m11())
        # At overview scales, dense lines obscure the image and waste paint time.
        if min(bw, bh) * scale >= 4:
            SpriteGridOverlayItem._draw_grid(
                painter, rect, bw, bh, -ox, -oy, QColor(255, 138, 0, 160)
            )
        painter.restore()
