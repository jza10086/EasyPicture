"""Qt-free Tile inspection, layout, and deterministic transition helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from easypicture.pixels import RGBAArray


class TilePreviewMode(StrEnum):
    OFFSET = "offset"
    PAIR = "pair"
    TRANSITION = "transition"


class TileTransitionMode(StrEnum):
    HARD_CUT = "hard_cut"
    ORDERED_DITHER = "ordered_dither"


class TileDirection(StrEnum):
    LEFT_TO_RIGHT = "left_to_right"
    RIGHT_TO_LEFT = "right_to_left"
    TOP_TO_BOTTOM = "top_to_bottom"
    BOTTOM_TO_TOP = "bottom_to_top"


@dataclass(frozen=True, slots=True)
class TileSeamMetrics:
    matching_percent: float
    mean_difference: float
    maximum_difference: int
    mismatched_positions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TileTransitionRequest:
    first: RGBAArray
    second: RGBAArray
    direction: TileDirection
    mode: TileTransitionMode
    boundary_percent: float
    transition_width: int


_BAYER_4X4 = np.array(
    (
        (0, 8, 2, 10),
        (12, 4, 14, 6),
        (3, 11, 1, 9),
        (15, 7, 13, 5),
    ),
    dtype=np.float32,
)


def _validate_tile(tile: RGBAArray, *, name: str = "tile") -> RGBAArray:
    if tile.dtype != np.uint8:
        raise ValueError(f"{name} must use uint8 pixels")
    if tile.ndim != 3 or tile.shape[2] != 4:
        raise ValueError(f"{name} must have shape (height, width, 4)")
    if tile.shape[0] <= 0 or tile.shape[1] <= 0:
        raise ValueError(f"{name} cannot be empty")
    if not tile.flags.c_contiguous:
        raise ValueError(f"{name} must be C-contiguous")
    return tile


def _validate_pair(first: RGBAArray, second: RGBAArray) -> None:
    _validate_tile(first, name="first")
    _validate_tile(second, name="second")
    if first.shape != second.shape:
        raise ValueError("Tile inputs must have identical dimensions")


def _seam_edges(
    first: RGBAArray,
    second: RGBAArray,
    direction: TileDirection,
) -> tuple[RGBAArray, RGBAArray]:
    if direction is TileDirection.LEFT_TO_RIGHT:
        return first[:, -1, :], second[:, 0, :]
    if direction is TileDirection.RIGHT_TO_LEFT:
        return first[:, 0, :], second[:, -1, :]
    if direction is TileDirection.TOP_TO_BOTTOM:
        return first[-1, :, :], second[0, :, :]
    return first[0, :, :], second[-1, :, :]


def tile_seam_differences(
    first: RGBAArray,
    second: RGBAArray,
    direction: TileDirection,
    *,
    include_alpha: bool = True,
) -> np.ndarray:
    """Return one maximum-channel byte difference for every seam position."""
    _validate_pair(first, second)
    before, after = _seam_edges(first, second, direction)
    channel_count = 4 if include_alpha else 3
    delta = np.abs(
        before[:, :channel_count].astype(np.int16) - after[:, :channel_count].astype(np.int16)
    )
    return np.ascontiguousarray(np.max(delta, axis=1).astype(np.uint8))


def analyze_tile_seam(
    first: RGBAArray,
    second: RGBAArray,
    direction: TileDirection,
    *,
    tolerance: int = 0,
    include_alpha: bool = True,
) -> TileSeamMetrics:
    """Compare two adjacent Tile edges using RGBA maximum-channel distance."""
    if not 0 <= tolerance <= 255:
        raise ValueError("tolerance must be in range 0..255")
    differences = tile_seam_differences(
        first,
        second,
        direction,
        include_alpha=include_alpha,
    )
    mismatched = np.flatnonzero(differences > tolerance)
    matching_percent = 100.0 * (differences.size - mismatched.size) / differences.size
    return TileSeamMetrics(
        matching_percent=matching_percent,
        mean_difference=float(np.mean(differences, dtype=np.float64)),
        maximum_difference=int(np.max(differences)),
        mismatched_positions=tuple(int(position) for position in mismatched),
    )


def analyze_self_seams(
    tile: RGBAArray,
    *,
    tolerance: int = 0,
    include_alpha: bool = True,
) -> tuple[TileSeamMetrics, TileSeamMetrics]:
    """Return horizontal (left/right) and vertical (top/bottom) wrap seams."""
    return (
        analyze_tile_seam(
            tile,
            tile,
            TileDirection.LEFT_TO_RIGHT,
            tolerance=tolerance,
            include_alpha=include_alpha,
        ),
        analyze_tile_seam(
            tile,
            tile,
            TileDirection.TOP_TO_BOTTOM,
            tolerance=tolerance,
            include_alpha=include_alpha,
        ),
    )


def offset_tile(tile: RGBAArray, offset_x: int, offset_y: int) -> RGBAArray:
    """Wrap an RGBA Tile by an integer offset without interpolation."""
    _validate_tile(tile)
    return np.ascontiguousarray(np.roll(tile, shift=(int(offset_y), int(offset_x)), axis=(0, 1)))


def repeat_tile(tile: RGBAArray, scale: int) -> RGBAArray:
    """Repeat a Tile into a 3x3 or 5x5 inspection surface."""
    _validate_tile(tile)
    if scale not in (3, 5):
        raise ValueError("repeat scale must be 3 or 5")
    return np.ascontiguousarray(np.tile(tile, (scale, scale, 1)))


def compose_tile_pair(
    first: RGBAArray,
    second: RGBAArray,
    direction: TileDirection,
) -> RGBAArray:
    """Place A and B in the requested adjacency direction."""
    _validate_pair(first, second)
    if direction is TileDirection.LEFT_TO_RIGHT:
        return np.ascontiguousarray(np.concatenate((first, second), axis=1))
    if direction is TileDirection.RIGHT_TO_LEFT:
        return np.ascontiguousarray(np.concatenate((second, first), axis=1))
    if direction is TileDirection.TOP_TO_BOTTOM:
        return np.ascontiguousarray(np.concatenate((first, second), axis=0))
    return np.ascontiguousarray(np.concatenate((second, first), axis=0))


def generate_tile_transition(
    request: TileTransitionRequest, *, check_cancelled: Callable[[], None] | None = None
) -> RGBAArray:
    """Generate a whole-pixel hard-cut or Bayer transition between two Tiles."""
    _validate_pair(request.first, request.second)
    if not 0.0 <= request.boundary_percent <= 100.0:
        raise ValueError("boundary percent must be in range 0..100")
    horizontal = request.direction in (
        TileDirection.LEFT_TO_RIGHT,
        TileDirection.RIGHT_TO_LEFT,
    )
    extent = request.first.shape[1] if horizontal else request.first.shape[0]
    if not 1 <= request.transition_width <= extent:
        raise ValueError("transition width exceeds the directional Tile extent")

    coordinate = (
        np.arange(request.first.shape[1], dtype=np.float32)[None, :]
        if horizontal
        else np.arange(request.first.shape[0], dtype=np.float32)[:, None]
    )
    boundary = request.boundary_percent / 100.0 * extent
    forward = request.direction in (
        TileDirection.LEFT_TO_RIGHT,
        TileDirection.TOP_TO_BOTTOM,
    )
    if request.mode is TileTransitionMode.HARD_CUT:
        choose_second = coordinate >= boundary if forward else coordinate < boundary
    else:
        width = float(request.transition_width)
        start = boundary - width / 2.0
        blend = np.clip((coordinate + 0.5 - start) / width, 0.0, 1.0)
        if not forward:
            blend = 1.0 - blend
        height, pixel_width = request.first.shape[:2]
        thresholds = (_BAYER_4X4 + 0.5) / 16.0
    result = np.empty_like(request.first)
    # Bound working memory and cancellation latency, keeping global Bayer phase.
    for start_row in range(0, request.first.shape[0], 64):
        if check_cancelled is not None:
            check_cancelled()
        end_row = min(start_row + 64, request.first.shape[0])
        if request.mode is TileTransitionMode.HARD_CUT:
            mask = choose_second if horizontal else choose_second[start_row:end_row]
        else:
            tiled_thresholds = thresholds[
                np.arange(start_row, end_row)[:, None] % 4,
                np.arange(pixel_width)[None, :] % 4,
            ]
            weights = blend if horizontal else blend[start_row:end_row]
            mask = weights > tiled_thresholds
        result[start_row:end_row] = np.where(
            mask[..., None], request.second[start_row:end_row], request.first[start_row:end_row]
        )
    return result


def compose_transition_preview(
    first: RGBAArray,
    transition: RGBAArray,
    second: RGBAArray,
    direction: TileDirection,
) -> RGBAArray:
    """Place A, generated transition, and B along the transition direction."""
    _validate_pair(first, second)
    _validate_pair(first, transition)
    if direction is TileDirection.LEFT_TO_RIGHT:
        parts = (first, transition, second)
        axis = 1
    elif direction is TileDirection.RIGHT_TO_LEFT:
        parts = (second, transition, first)
        axis = 1
    elif direction is TileDirection.TOP_TO_BOTTOM:
        parts = (first, transition, second)
        axis = 0
    else:
        parts = (second, transition, first)
        axis = 0
    return np.ascontiguousarray(np.concatenate(parts, axis=axis))


__all__ = [
    "TileDirection",
    "TilePreviewMode",
    "TileSeamMetrics",
    "TileTransitionMode",
    "TileTransitionRequest",
    "analyze_self_seams",
    "analyze_tile_seam",
    "compose_tile_pair",
    "compose_transition_preview",
    "generate_tile_transition",
    "offset_tile",
    "repeat_tile",
    "tile_seam_differences",
]
