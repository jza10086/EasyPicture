"""Bounded color interaction previews; exact results always use the original request."""

from dataclasses import replace

import numpy as np

from easypicture.domain import AffineTransform, Size
from easypicture.imaging.color_adjustments import (
    ColorAdjustmentCancellation,
    ColorAdjustmentRequest,
    ColorAdjustmentResult,
    PaletteMapAdjustment,
    ProgressReporter,
    calculate_color_adjustment,
)
from easypicture.imaging.geometry_ops import _rgba_tiles
from easypicture.imaging.palette_extraction import active_raster_snapshot
from easypicture.pixels import TilePixelStore


def calculate_color_preview(
    request: ColorAdjustmentRequest,
    token: ColorAdjustmentCancellation,
    progress: ProgressReporter,
) -> ColorAdjustmentResult:
    source = active_raster_snapshot(request.snapshot, request.layer_id)
    if max(source.size.width, source.size.height) <= 1024 or (
        isinstance(request.parameters, PaletteMapAdjustment) and request.parameters.dither
    ):
        return calculate_color_adjustment(request, token, progress)
    ratio = 1024 / max(source.size.width, source.size.height)
    size = Size(max(1, round(source.size.width * ratio)), max(1, round(source.size.height * ratio)))
    pixels = np.zeros((size.height, size.width, 4), np.uint8)
    xs = np.floor((np.arange(size.width) + 0.5) * source.size.width / size.width).astype(int)
    ys = np.floor((np.arange(size.height) + 0.5) * source.size.height / size.height).astype(int)
    tile_size = source.pixels.tile_size

    def axis_parts(samples: np.ndarray) -> dict[int, tuple[slice, np.ndarray]]:
        parts = {}
        tiles = samples // tile_size
        for tile_index in np.unique(tiles):
            start = int(np.searchsorted(tiles, tile_index))
            end = int(np.searchsorted(tiles, tile_index, side="right"))
            parts[int(tile_index)] = (slice(start, end), samples[start:end] % tile_size)
        return parts

    x_parts, y_parts = axis_parts(xs), axis_parts(ys)
    for coord, tile in source.pixels.iter_allocated_tiles():
        token.raise_if_cancelled()
        if coord.x in x_parts and coord.y in y_parts:
            x_slice, local_x = x_parts[coord.x]
            y_slice, local_y = y_parts[coord.y]
            pixels[y_slice, x_slice] = tile[local_y[:, None], local_x]
    store = TilePixelStore()
    for coord, tile in _rgba_tiles(pixels).items():
        store.replace_tile(coord.x, coord.y, tile)
    layer = replace(
        source,
        size=size,
        pixels=store,
        transform=AffineTransform.scale(
            source.size.width / size.width,
            source.size.height / size.height,
        ).then(source.transform),
    )
    snapshot = replace(request.snapshot, nodes=(layer,))
    result = calculate_color_adjustment(replace(request, snapshot=snapshot), token, progress)
    complete = dict(store.iter_allocated_tiles())
    for coord, update in result.updates.items():
        if update is None:
            complete.pop(coord, None)
        else:
            complete[coord] = update
    for tile in complete.values():
        tile.flags.writeable = False
    return replace(result, updates=complete, preview_size=size)
