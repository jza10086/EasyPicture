"""Raster object drawing with source pixels independent of the object transform."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter, QTransform

from easypicture.domain.layer import RasterObject, RasterSampling
from easypicture.persistence.snapshots import LayerSnapshot
from easypicture.render.qimage_bridge import rgba_tile_to_qimage


def paint_raster_object(
    painter: QPainter,
    node: RasterObject | LayerSnapshot,
    *,
    opacity: float,
) -> None:
    if not node.visible or opacity <= 0:
        return
    matrix = node.world_transform
    painter.save()
    painter.setOpacity(opacity)
    painter.setWorldTransform(QTransform(*matrix.values), combine=True)
    painter.setClipRect(
        QRectF(0, 0, node.size.width, node.size.height),
        Qt.ClipOperation.IntersectClip,
    )
    painter.setRenderHint(
        QPainter.RenderHint.SmoothPixmapTransform,
        node.sampling is RasterSampling.SMOOTH,
    )
    for coord, pixels in node.pixels.iter_allocated_tiles():
        painter.drawImage(
            coord.x * node.pixels.tile_size,
            coord.y * node.pixels.tile_size,
            rgba_tile_to_qimage(pixels),
        )
    painter.restore()
