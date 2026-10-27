"""Tile-local pixel morphology with halos; no Qt or display-cache dependency."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

import cv2
import numpy as np

from easypicture.imaging.color_adjustments import (
    ColorAdjustmentCancellation,
    ProgressReporter,
    RGBAColor,
)
from easypicture.imaging.object_pixels import local_selection
from easypicture.imaging.palette_extraction import active_raster_snapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, Rect, RGBAArray, TileCoord

if TYPE_CHECKING:
    from easypicture.persistence.snapshots import DocumentSnapshot


class PixelOutlinePlacement(StrEnum):
    OUTER = "outer"
    INNER = "inner"


@dataclass(frozen=True, slots=True)
class PixelOutlineParameters:
    placement: PixelOutlinePlacement = PixelOutlinePlacement.OUTER
    width: int = 1
    neighbors: int = 8
    alpha_threshold: int = 128
    color: RGBAColor = (0, 0, 0, 255)

    def __post_init__(self) -> None:
        if not isinstance(self.placement, PixelOutlinePlacement):
            raise ValueError("unknown outline placement")
        if type(self.width) is not int or not 1 <= self.width <= 64:
            raise ValueError("outline width must be between 1 and 64")
        if self.neighbors not in (4, 8):
            raise ValueError("outline requires 4 or 8 neighbors")
        if type(self.alpha_threshold) is not int or not 1 <= self.alpha_threshold <= 255:
            raise ValueError("alpha threshold must be between 1 and 255")
        if len(self.color) != 4 or any(type(v) is not int or not 0 <= v <= 255 for v in self.color):
            raise ValueError("invalid outline RGBA color")


@dataclass(frozen=True, slots=True)
class PixelOutlineRequest:
    snapshot: DocumentSnapshot
    layer_id: UUID
    parameters: PixelOutlineParameters


@dataclass(frozen=True, slots=True)
class PixelOutlineResult:
    document_id: UUID
    source_revision: int
    layer_id: UUID
    updates: Mapping[TileCoord, RGBAArray]
    clipped: bool = False


def calculate_pixel_outline(
    request: PixelOutlineRequest,
    cancellation: ColorAdjustmentCancellation,
    progress: ProgressReporter,
) -> PixelOutlineResult:
    snapshot, p = request.snapshot, request.parameters
    layer = active_raster_snapshot(snapshot, request.layer_id)
    width, height = layer.size.width, layer.size.height
    tile_size = DEFAULT_TILE_SIZE
    coords: set[TileCoord] = set()
    for coord, _tile in layer.pixels.iter_allocated_tiles():
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                tx, ty = coord.x + dx, coord.y + dy
                if 0 <= tx * tile_size < width and 0 <= ty * tile_size < height:
                    coords.add(TileCoord(tx, ty))
    kernel = (
        np.ones((3, 3), dtype=np.uint8)
        if p.neighbors == 8
        else np.asarray(((0, 1, 0), (1, 1, 1), (0, 1, 0)), dtype=np.uint8)
    )
    updates: dict[TileCoord, RGBAArray] = {}
    clipped = False
    for index, coord in enumerate(sorted(coords, key=lambda c: (c.y, c.x))):
        cancellation.raise_if_cancelled()
        x, y = coord.x * tile_size, coord.y * tile_size
        w, h = min(tile_size, width - x), min(tile_size, height - y)
        halo = p.width
        mask = np.zeros((h + 2 * halo, w + 2 * halo), dtype=np.uint8)
        left, top = max(0, x - halo), max(0, y - halo)
        right, bottom = min(width, x + w + halo), min(height, y + h + halo)
        alpha = layer.pixels.read_region(Rect(left, top, right - left, bottom - top))[..., 3]
        mask[top - y + halo : bottom - y + halo, left - x + halo : right - x + halo] = (
            alpha >= p.alpha_threshold
        )
        if p.placement is PixelOutlinePlacement.OUTER:
            result = (
                cv2.dilate(
                    mask, kernel, iterations=halo, borderType=cv2.BORDER_CONSTANT, borderValue=0
                )
                - mask
            )
            # Conservative warning when dilation can extend beyond a canvas boundary.
            clipped |= bool(
                (x == 0 and np.any(mask[:, halo : 2 * halo]))
                or (y == 0 and np.any(mask[halo : 2 * halo]))
                or (x + w == width and np.any(mask[:, w : w + halo]))
                or (y + h == height and np.any(mask[h : h + halo]))
            )
        else:
            result = mask - cv2.erode(
                mask, kernel, iterations=halo, borderType=cv2.BORDER_CONSTANT, borderValue=0
            )
        selected = result[halo : halo + h, halo : halo + w] != 0
        alpha_out = selected.astype(np.uint16) * p.color[3]
        if snapshot.selection.has_nonzero_data:
            weights = local_selection(snapshot, layer, x, y, w, h).astype(np.uint16)
            alpha_out = (alpha_out * weights + 127) // 255
        if np.any(alpha_out):
            tile = np.zeros((tile_size, tile_size, 4), dtype=np.uint8)
            region = tile[:h, :w]
            region[alpha_out > 0] = p.color
            region[..., 3] = alpha_out.astype(np.uint8)
            tile.flags.writeable = False
            updates[coord] = tile
        progress(index + 1, max(1, len(coords)), "生成像素描边")
    cancellation.raise_if_cancelled()
    return PixelOutlineResult(
        snapshot.document_id, snapshot.revision, request.layer_id, updates, clipped
    )
