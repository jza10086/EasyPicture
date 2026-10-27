"""Pixel-aware object picking without compositing or reading unrelated objects."""

from math import floor

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter

from easypicture.domain import Point, RasterObject, SvgObject, TextObject
from easypicture.domain.coordinates import RasterMapping
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
