"""Shared background-safe RGBA reference-surface construction."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np

from easypicture.imaging.composite import compose_document_tile
from easypicture.persistence.snapshots import DocumentSnapshot, LayerSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, Rect, RGBAArray, TileCoord

type ProgressReporter = Callable[[int, int, str], None]


class ReferenceCancellation(Protocol):
    def raise_if_cancelled(self) -> None: ...


def create_region_reference(
    snapshot: DocumentSnapshot,
    layer: LayerSnapshot,
    sample_merged: bool,
    temp_dir: Path,
    cancellation: ReferenceCancellation,
    progress: ProgressReporter,
    *,
    progress_message: str = "分析区域",
) -> RGBAArray:
    """Materialize the document-space surface used by tolerance queries."""
    shape = (snapshot.size.height, snapshot.size.width, 4)
    byte_count = snapshot.size.width * snapshot.size.height * 4
    if byte_count >= 64 * 1024 * 1024:
        reference: RGBAArray = np.memmap(
            temp_dir / "reference.rgba",
            dtype=np.uint8,
            mode="w+",
            shape=shape,
        )
        reference[:] = 0
    else:
        reference = np.zeros(shape, dtype=np.uint8)

    tile_size = DEFAULT_TILE_SIZE
    columns = (snapshot.size.width + tile_size - 1) // tile_size
    rows = (snapshot.size.height + tile_size - 1) // tile_size
    total = columns * rows
    completed = 0
    for ty in range(rows):
        for tx in range(columns):
            cancellation.raise_if_cancelled()
            document_left = tx * tile_size
            document_top = ty * tile_size
            valid_width = min(tile_size, snapshot.size.width - document_left)
            valid_height = min(tile_size, snapshot.size.height - document_top)
            if sample_merged:
                source = compose_document_tile(snapshot, TileCoord(tx, ty))
                reference[
                    document_top : document_top + valid_height,
                    document_left : document_left + valid_width,
                ] = source[:valid_height, :valid_width]
            else:
                _copy_layer_to_document_tile(
                    reference,
                    layer,
                    document_left,
                    document_top,
                    valid_width,
                    valid_height,
                )
            completed += 1
            progress(completed, total, progress_message)
    return reference


def layer_by_id(snapshot: DocumentSnapshot, layer_id: object) -> LayerSnapshot:
    for layer in snapshot.raster_layers:
        if layer.id == layer_id:
            return layer
    raise ValueError("layer does not belong to the document")


def _copy_layer_to_document_tile(
    reference: RGBAArray,
    layer: LayerSnapshot,
    document_left: int,
    document_top: int,
    width: int,
    height: int,
) -> None:
    document_right = document_left + width
    document_bottom = document_top + height
    overlap_left = max(document_left, layer.origin.x)
    overlap_top = max(document_top, layer.origin.y)
    overlap_right = min(document_right, layer.origin.x + layer.size.width)
    overlap_bottom = min(document_bottom, layer.origin.y + layer.size.height)
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
    reference[overlap_top:overlap_bottom, overlap_left:overlap_right] = source


__all__ = [
    "ProgressReporter",
    "ReferenceCancellation",
    "create_region_reference",
    "layer_by_id",
]
