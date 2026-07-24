"""Authoritative single-pixel sampling helpers."""

from __future__ import annotations

from easypicture.domain import Document
from easypicture.imaging.composite import compose_document_tile
from easypicture.persistence.snapshots import DocumentSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, TileCoord

type SampledColor = tuple[int, int, int, int]


def sample_document_pixel(
    document: Document,
    x: int,
    y: int,
    *,
    merged: bool = True,
) -> SampledColor:
    if not 0 <= x < document.width or not 0 <= y < document.height:
        raise ValueError("sample point lies outside the document")
    if merged:
        coord = TileCoord(x // DEFAULT_TILE_SIZE, y // DEFAULT_TILE_SIZE)
        tile = compose_document_tile(DocumentSnapshot.capture(document), coord)
        pixel = tile[y % DEFAULT_TILE_SIZE, x % DEFAULT_TILE_SIZE]
    else:
        layer = document.active_layer
        layer_x = x - layer.origin.x
        layer_y = y - layer.origin.y
        if not 0 <= layer_x < layer.size.width or not 0 <= layer_y < layer.size.height:
            return (0, 0, 0, 0)
        tile = layer.pixels.read_tile(
            layer_x // DEFAULT_TILE_SIZE,
            layer_y // DEFAULT_TILE_SIZE,
        )
        pixel = tile[layer_y % DEFAULT_TILE_SIZE, layer_x % DEFAULT_TILE_SIZE]
    return tuple(int(channel) for channel in pixel)  # type: ignore[return-value]


__all__ = ["SampledColor", "sample_document_pixel"]
