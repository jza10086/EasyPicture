"""Authoritative straight-RGBA Tile compositing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from easypicture.pixels import DEFAULT_TILE_SIZE, Rect, RGBAArray, TileCoord

if TYPE_CHECKING:
    from easypicture.persistence.snapshots import DocumentSnapshot, LayerSnapshot


def compose_document_tile(
    snapshot: DocumentSnapshot,
    coord: TileCoord,
) -> RGBAArray:
    tile_size = DEFAULT_TILE_SIZE
    left = coord.x * tile_size
    top = coord.y * tile_size
    if left < 0 or top < 0 or left >= snapshot.size.width or top >= snapshot.size.height:
        raise ValueError("document Tile lies outside the canvas")
    right = min(left + tile_size, snapshot.size.width)
    bottom = min(top + tile_size, snapshot.size.height)
    output = np.zeros((tile_size, tile_size, 4), dtype=np.uint8)

    from easypicture.domain.effective import iter_effective_leaves
    from easypicture.persistence.snapshots import LayerSnapshot

    leaves = iter_effective_leaves(snapshot)
    if all(
        isinstance(item.leaf, LayerSnapshot) and item.leaf.transform.is_identity for item in leaves
    ):
        for item in leaves:
            layer = item.leaf
            assert isinstance(layer, LayerSnapshot)
            if item.visible and item.opacity > 0.0:
                _composite_layer(
                    output, layer, left, top, right, bottom, item.opacity / layer.opacity
                )
        return output
    return _compose_document_tile_with_vectors(snapshot, coord)


def _composite_layer(
    output: RGBAArray,
    layer: LayerSnapshot,
    left: int,
    top: int,
    right: int,
    bottom: int,
    group_opacity: float,
) -> None:
    if not layer.visible or layer.opacity <= 0.0:
        return
    overlap_left = max(left, layer.origin.x)
    overlap_top = max(top, layer.origin.y)
    overlap_right = min(right, layer.origin.x + layer.size.width)
    overlap_bottom = min(bottom, layer.origin.y + layer.size.height)
    if overlap_right <= overlap_left or overlap_bottom <= overlap_top:
        return
    source = layer.pixels.read_region(
        Rect(
            overlap_left - layer.origin.x,
            overlap_top - layer.origin.y,
            overlap_right - overlap_left,
            overlap_bottom - overlap_top,
        )
    )
    target_y = slice(overlap_top - top, overlap_bottom - top)
    target_x = slice(overlap_left - left, overlap_right - left)
    output[target_y, target_x] = _source_over(
        output[target_y, target_x],
        source,
        float(layer.opacity) * group_opacity,
    )


def _compose_document_tile_with_vectors(
    snapshot: DocumentSnapshot,
    coord: TileCoord,
) -> RGBAArray:
    """Compose a tile through the same QPainter path used for vector text."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter, Qt

    from easypicture.domain.effective import iter_effective_leaves
    from easypicture.persistence.snapshots import (
        LayerSnapshot,
        SvgElementSnapshot,
        TextObjectSnapshot,
    )
    from easypicture.render.qimage_bridge import qimage_to_rgba
    from easypicture.render.text_renderer import paint_vector_objects

    tile_size = DEFAULT_TILE_SIZE
    left = coord.x * tile_size
    top = coord.y * tile_size
    right = min(left + tile_size, snapshot.size.width)
    bottom = min(top + tile_size, snapshot.size.height)
    image = QImage(tile_size, tile_size, QImage.Format.Format_RGBA8888_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setClipRect(QRectF(0.0, 0.0, right - left, bottom - top))
    painter.translate(-left, -top)
    for item in iter_effective_leaves(snapshot):
        node = item.leaf
        if not item.visible or item.opacity <= 0.0:
            continue
        if isinstance(node, LayerSnapshot):
            from easypicture.render.raster_renderer import paint_raster_object

            paint_raster_object(painter, node, opacity=item.opacity)
        elif isinstance(node, (TextObjectSnapshot, SvgElementSnapshot)):
            paint_vector_objects(painter, (node,), group_opacity=item.opacity / node.opacity)
    painter.end()
    return qimage_to_rgba(image)


def _source_over(destination: RGBAArray, source: RGBAArray, opacity: float) -> RGBAArray:
    src = source.astype(np.uint32)
    dst = destination.astype(np.uint32)
    opacity_u8 = round(max(0.0, min(1.0, opacity)) * 255)
    src_alpha = (src[..., 3] * opacity_u8 + 127) // 255
    dst_alpha = dst[..., 3]
    inverse = 255 - src_alpha
    alpha_numerator = src_alpha * 255 + dst_alpha * inverse

    result = np.zeros_like(src, dtype=np.uint32)
    result[..., 3] = (alpha_numerator + 127) // 255
    nonzero = alpha_numerator > 0
    for channel in range(3):
        numerator = src[..., channel] * src_alpha * 255 + dst[..., channel] * dst_alpha * inverse
        rounded = np.zeros_like(numerator)
        rounded[nonzero] = (numerator[nonzero] + alpha_numerator[nonzero] // 2) // alpha_numerator[
            nonzero
        ]
        result[..., channel] = rounded
    return result.astype(np.uint8)


__all__ = ["compose_document_tile"]
