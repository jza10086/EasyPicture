"""Explicit object rasterization, preserving all content outside the output canvas."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
from PySide6.QtGui import QImage, QPainter

from easypicture.domain import Collection, DocumentNode, PixelPoint, RasterObject, Size, walk_nodes
from easypicture.domain.spatial import object_bounds
from easypicture.imaging.geometry_ops import _rgba_tiles
from easypicture.pixels import TilePixelStore
from easypicture.render.raster_renderer import paint_raster_object
from easypicture.render.text_renderer import paint_vector_objects


def rasterize_object(node: DocumentNode) -> RasterObject:
    leaves = [leaf for leaf in walk_nodes((node,)) if not isinstance(leaf, Collection)]
    bounds = [object_bounds(leaf) for leaf in leaves]
    left = math.floor(min((rect.x for rect in bounds), default=0))
    top = math.floor(min((rect.y for rect in bounds), default=0))
    right = math.ceil(max((rect.right for rect in bounds), default=left + 1))
    bottom = math.ceil(max((rect.bottom for rect in bounds), default=top + 1))
    size = Size(max(1, right - left), max(1, bottom - top))
    if max(size.width, size.height) > 8192 or max(abs(left), abs(top)) > 32768:
        raise ValueError("栅格化结果超过局部像素范围上限")
    image = QImage(size.width, size.height, QImage.Format.Format_RGBA8888_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.translate(-left, -top)

    def paint(item: DocumentNode, opacity: float, visible: bool) -> None:
        visible = visible and item.visible
        opacity *= item.opacity
        if not visible or opacity <= 0:
            return
        if isinstance(item, Collection):
            for child in item.children:
                paint(child, opacity, visible)
        elif isinstance(item, RasterObject):
            paint_raster_object(painter, item, opacity=opacity)
        else:
            paint_vector_objects(painter, (item,), group_opacity=opacity / item.opacity)

    try:
        paint(replace(node, visible=True, opacity=1.0), 1.0, True)
    finally:
        painter.end()
    straight = image.convertToFormat(QImage.Format.Format_RGBA8888)
    rows = np.frombuffer(straight.constBits(), np.uint8).reshape(
        size.height, straight.bytesPerLine()
    )
    pixels = rows[:, : size.width * 4].reshape(size.height, size.width, 4)
    store = TilePixelStore()
    for coord, tile in _rgba_tiles(pixels).items():
        store.replace_tile(coord.x, coord.y, tile)
    return RasterObject(
        id=node.id,
        name=node.name,
        size=size,
        origin=PixelPoint(left, top),
        pixels=store,
        visible=node.visible,
        opacity=node.opacity,
        locked=node.locked,
        alpha_locked=node.alpha_locked if isinstance(node, RasterObject) else False,
    )
