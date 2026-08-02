"""Rasterize vector children through the same Qt adapter used by the canvas."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from PySide6.QtGui import QImage, QPainter

from easypicture.domain import Size, VectorObject
from easypicture.pixels import DEFAULT_TILE_SIZE, TilePixelStore
from easypicture.render.text_renderer import paint_vector_objects


def rasterize_vector_objects(
    objects: tuple[VectorObject, ...],
    size: Size,
    *,
    opacity: float = 1.0,
    include_hidden: bool = False,
) -> TilePixelStore:
    image = QImage(
        size.width,
        size.height,
        QImage.Format.Format_RGBA8888_Premultiplied,
    )
    image.fill(0)
    painter = QPainter(image)
    source = (
        tuple(replace(item, visible=True) for item in objects)
        if include_hidden
        else objects
    )
    paint_vector_objects(painter, source, group_opacity=opacity)
    painter.end()
    straight = image.convertToFormat(QImage.Format.Format_RGBA8888)
    rows = np.frombuffer(
        straight.constBits(),
        dtype=np.uint8,
        count=straight.height() * straight.bytesPerLine(),
    ).reshape(straight.height(), straight.bytesPerLine())
    pixels = rows[:, : straight.width() * 4].reshape(size.height, size.width, 4)
    store = TilePixelStore(DEFAULT_TILE_SIZE)
    for top in range(0, size.height, DEFAULT_TILE_SIZE):
        for left in range(0, size.width, DEFAULT_TILE_SIZE):
            bottom = min(size.height, top + DEFAULT_TILE_SIZE)
            right = min(size.width, left + DEFAULT_TILE_SIZE)
            tile = np.zeros(
                (DEFAULT_TILE_SIZE, DEFAULT_TILE_SIZE, 4),
                dtype=np.uint8,
            )
            tile[: bottom - top, : right - left] = pixels[top:bottom, left:right]
            if np.any(tile):
                store.replace_tile(left // DEFAULT_TILE_SIZE, top // DEFAULT_TILE_SIZE, tile)
    return store


__all__ = ["rasterize_vector_objects"]
