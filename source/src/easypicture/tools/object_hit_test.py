"""Pixel-aware object picking without compositing or reading unrelated objects."""

from math import ceil, floor

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from easypicture.domain import FloatRect, Point, RasterObject, SvgObject, TextObject
from easypicture.domain.coordinates import RasterMapping
from easypicture.domain.spatial import object_bounds
from easypicture.pixels import Rect
from easypicture.render import paint_vector_objects


def opaque_at(node: RasterObject | TextObject | SvgObject, point: Point) -> bool:
    if isinstance(node, RasterObject):
        mapping = RasterMapping(node.world_transform, node.size)
        if not mapping.contains_document_point(point):
            return False
        local = mapping.to_local(point)
        rgba = node.pixels.read_region(Rect(floor(local.x), floor(local.y), 1, 1))
        return bool(rgba[0, 0, 3])
    pixel = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
    pixel.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixel)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(0.5 - point.x, 0.5 - point.y)
    paint_vector_objects(painter, (node,))
    painter.end()
    return pixel.pixelColor(0, 0).alpha() > 0


def opaque_in_rect(node: RasterObject | TextObject | SvgObject, rect: FloatRect) -> bool:
    """Test coverage inside the box, with bounded scratch memory and no downsampling."""
    if isinstance(node, RasterObject):
        inverse = node.world_transform.inverse()
        corners = tuple(
            inverse.map_point(Point(x, y))
            for x, y in (
                (rect.x, rect.y),
                (rect.right, rect.y),
                (rect.right, rect.bottom),
                (rect.x, rect.bottom),
            )
        )
        left = max(0, floor(min(p.x for p in corners)))
        top = max(0, floor(min(p.y for p in corners)))
        right = min(node.size.width, ceil(max(p.x for p in corners)))
        bottom = min(node.size.height, ceil(max(p.y for p in corners)))
        # Separating axes of a local pixel square and inverse-mapped selection box.
        axes = [(1.0, 0.0), (0.0, 1.0)]
        for first, second in zip(corners[:2], corners[1:3], strict=True):
            axes.append((first.y - second.y, second.x - first.x))
        for y in range(top, bottom, 256):
            for x in range(left, right, 256):
                rgba = node.pixels.read_region(
                    Rect(x, y, min(256, right - x), min(256, bottom - y))
                )
                py, px = np.nonzero(rgba[:, :, 3])
                cx, cy = px + x + 0.5, py + y + 0.5
                hit = np.ones(len(px), dtype=bool)
                for ax, ay in axes:
                    projected = [p.x * ax + p.y * ay for p in corners]
                    center = cx * ax + cy * ay
                    radius = (abs(ax) + abs(ay)) / 2
                    hit &= (center + radius > min(projected) + 1e-9) & (
                        center - radius < max(projected) - 1e-9
                    )
                if np.any(hit):
                    return True
        return False
    bounds = object_bounds(node)
    clip = QRectF(rect.x, rect.y, rect.width, rect.height).intersected(
        QRectF(bounds.x, bounds.y, bounds.width, bounds.height)
    )
    for y in range(floor(clip.top()), ceil(clip.bottom()), 256):
        for x in range(floor(clip.left()), ceil(clip.right()), 256):
            image = QImage(256, 256, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.translate(-x, -y)
                painter.setClipRect(clip)
                paint_vector_objects(painter, (node,))
            finally:
                painter.end()
            if np.any(np.frombuffer(image.constBits(), np.uint32) >> 24):
                return True
    return False
