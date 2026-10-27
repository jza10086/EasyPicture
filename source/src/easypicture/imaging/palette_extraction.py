"""Deterministic, bounded-memory palettes from authoritative active-layer pixels."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import numpy as np
from PIL import Image

from easypicture.domain.effective import iter_effective_leaves
from easypicture.imaging.color_adjustments import (
    ColorAdjustmentCancellation,
    ProgressReporter,
    RGBColor,
)
from easypicture.imaging.object_pixels import local_selection
from easypicture.pixels import DEFAULT_TILE_SIZE

MAX_PALETTE_SAMPLES = 262_144

if TYPE_CHECKING:
    from easypicture.persistence.snapshots import DocumentSnapshot, LayerSnapshot


@dataclass(frozen=True, slots=True)
class PaletteExtractionRequest:
    snapshot: DocumentSnapshot
    layer_id: UUID
    target_color_count: int = 16

    def __post_init__(self) -> None:
        if type(self.target_color_count) is not int or not 1 <= self.target_color_count <= 256:
            raise ValueError("target color count must be between 1 and 256")


@dataclass(frozen=True, slots=True)
class PaletteExtractionResult:
    document_id: UUID
    source_revision: int
    layer_id: UUID
    colors: tuple[RGBColor, ...]
    source_color_count: int


def active_raster_snapshot(snapshot: DocumentSnapshot, layer_id: UUID) -> LayerSnapshot:
    from easypicture.persistence.snapshots import LayerSnapshot

    for item in iter_effective_leaves(snapshot):
        if item.leaf.id == layer_id and isinstance(item.leaf, LayerSnapshot):
            if item.locked:
                raise PermissionError("object or ancestor collection is locked")
            return item.leaf
    raise ValueError("unknown raster object")


def _eligible_chunks(
    snapshot: DocumentSnapshot,
    layer: LayerSnapshot,
    cancellation: ColorAdjustmentCancellation,
) -> Iterator[np.ndarray]:
    # Tile-row-major order is independent of dictionary insertion order. Never materialize
    # a full image; both passes use exactly the same deterministic pixel scan order.
    for coord, tile in sorted(
        layer.pixels.iter_allocated_tiles(),
        key=lambda item: (
            item[0].y,
            item[0].x,
        ),
    ):
        cancellation.raise_if_cancelled()
        x, y = coord.x * DEFAULT_TILE_SIZE, coord.y * DEFAULT_TILE_SIZE
        width = min(DEFAULT_TILE_SIZE, layer.size.width - x)
        height = min(DEFAULT_TILE_SIZE, layer.size.height - y)
        if width <= 0 or height <= 0:
            continue
        pixels = tile[:height, :width]
        eligible = pixels[..., 3] > 0
        if snapshot.selection.has_nonzero_data:
            eligible &= local_selection(snapshot, layer, x, y, width, height) > 0
        yield pixels[..., :3][eligible]


def extract_layer_palette(
    request: PaletteExtractionRequest,
    cancellation: ColorAdjustmentCancellation,
    progress: ProgressReporter,
) -> PaletteExtractionResult:
    layer = active_raster_snapshot(request.snapshot, request.layer_id)
    # 24-bit membership bitmap: 16 MiB regardless of image size or number of colors.
    seen = np.zeros(1 << 24, dtype=np.bool_)
    eligible_count = 0
    total = max(1, layer.pixels.allocated_tile_count)
    for index, rgb in enumerate(_eligible_chunks(request.snapshot, layer, cancellation)):
        values = rgb.astype(np.uint32)
        packed = (values[:, 0] << 16) | (values[:, 1] << 8) | values[:, 2]
        seen[packed] = True
        eligible_count += len(rgb)
        progress(index + 1, total * 2 + 1, "统计有效 RGB 颜色")
    cancellation.raise_if_cancelled()
    count = int(np.count_nonzero(seen))
    colors: tuple[RGBColor, ...] = ()
    if count <= request.target_color_count:
        colors = tuple(
            (int(v >> 16), int((v >> 8) & 255), int(v & 255)) for v in np.flatnonzero(seen)
        )
    else:
        # Uniform ranks in the sequence of eligible pixels, not in the bounding rectangle.
        sample_count = min(eligible_count, MAX_PALETTE_SAMPLES)
        ranks = np.arange(sample_count, dtype=np.int64) * eligible_count // sample_count
        samples = np.empty((sample_count, 3), dtype=np.uint8)
        offset = 0
        for index, rgb in enumerate(_eligible_chunks(request.snapshot, layer, cancellation)):
            first = int(np.searchsorted(ranks, offset))
            last = int(np.searchsorted(ranks, offset + len(rgb)))
            samples[first:last] = rgb[ranks[first:last] - offset]
            offset += len(rgb)
            progress(total + index + 1, total * 2 + 1, "提取调色板样本")
        cancellation.raise_if_cancelled()
        image = Image.fromarray(samples.reshape(1, sample_count, 3))
        quantized = image.quantize(
            colors=request.target_color_count, method=Image.Quantize.MEDIANCUT
        )
        palette = quantized.getpalette()
        assert palette is not None
        indices = np.unique(np.asarray(quantized))
        colors = tuple(
            sorted(
                {
                    (palette[int(i) * 3], palette[int(i) * 3 + 1], palette[int(i) * 3 + 2])
                    for i in indices
                }
            )
        )
    cancellation.raise_if_cancelled()
    progress(total * 2 + 1, total * 2 + 1, "调色板提取完成")
    return PaletteExtractionResult(
        request.snapshot.document_id, request.snapshot.revision, request.layer_id, colors, count
    )
