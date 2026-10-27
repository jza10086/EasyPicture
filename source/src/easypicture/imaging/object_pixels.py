"""Selection and reference sampling on a raster object's fixed local pixel grid."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from easypicture.domain.coordinates import RasterMapping
from easypicture.domain.layer import RasterSampling
from easypicture.imaging.reference import (
    ProgressReporter,
    ReferenceCancellation,
    create_region_reference,
)
from easypicture.persistence.snapshots import DocumentSnapshot, LayerSnapshot
from easypicture.pixels import MaskArray, Rect, RGBAArray


def local_selection(
    snapshot: DocumentSnapshot,
    layer: LayerSnapshot,
    x: int,
    y: int,
    width: int,
    height: int,
) -> MaskArray:
    return RasterMapping(layer.world_transform, layer.size).selection_coverage(
        snapshot.selection,
        snapshot.size,
        x,
        y,
        width,
        height,
        smooth=layer.sampling is RasterSampling.SMOOTH,
    )


def local_reference(
    snapshot: DocumentSnapshot,
    layer: LayerSnapshot,
    merged: bool,
    temp_dir: Path,
    cancellation: ReferenceCancellation,
    progress: ProgressReporter,
) -> RGBAArray:
    if not merged:
        cancellation.raise_if_cancelled()
        return layer.pixels.read_region(Rect(0, 0, layer.size.width, layer.size.height))
    document = create_region_reference(
        snapshot,
        layer,
        True,
        temp_dir,
        cancellation,
        progress,
    )
    matrix = layer.world_transform
    coefficients = np.array(
        [
            [matrix.a, matrix.c, matrix.tx + (matrix.a + matrix.c - 1) / 2],
            [matrix.b, matrix.d, matrix.ty + (matrix.b + matrix.d - 1) / 2],
        ],
        dtype=np.float64,
    )
    result = cv2.warpAffine(
        document,
        coefficients,
        (layer.size.width, layer.size.height),
        flags=cv2.WARP_INVERSE_MAP
        | (cv2.INTER_NEAREST if layer.sampling is RasterSampling.NEAREST else cv2.INTER_LINEAR),
        borderMode=cv2.BORDER_CONSTANT,
    )
    # A mapped reference is independent of its temporary source file.
    if isinstance(document, np.memmap):
        mapping = getattr(document, "_mmap", None)
        if mapping is not None:
            mapping.close()
    return np.asarray(result, dtype=np.uint8)
