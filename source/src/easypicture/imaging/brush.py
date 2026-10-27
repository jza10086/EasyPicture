"""Qt-free brush dab generation and straight-RGBA pixel blending."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Protocol, cast

import numpy as np

from easypicture.pixels import DEFAULT_TILE_SIZE, MaskArray, RGBAArray, TileCoord

type RGBAColor = tuple[int, int, int, int]
DEFAULT_BRUSH_SIZE_LIMIT = 1024
MAX_BRUSH_SIZE = 8192


class PaintBlendMode(Enum):
    NORMAL = "normal"
    REPLACE = "replace"
    MULTIPLY = "multiply"
    SCREEN = "screen"
    OVERLAY = "overlay"


@dataclass(frozen=True, slots=True)
class BrushSettings:
    size: int = 32
    hardness: float = 0.8
    opacity: float = 1.0
    spacing: float = 0.15
    antialias: bool = True
    mode: PaintBlendMode = PaintBlendMode.NORMAL

    def __post_init__(self) -> None:
        if not 1 <= self.size <= MAX_BRUSH_SIZE:
            raise ValueError(f"brush size must be between 1 and {MAX_BRUSH_SIZE}")
        if not 0.0 <= self.hardness <= 1.0:
            raise ValueError("brush hardness must be between 0 and 1")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("brush opacity must be between 0 and 1")
        if not 0.01 <= self.spacing <= 2.0:
            raise ValueError("brush spacing must be between 0.01 and 2.0")


class BrushSurface(Protocol):
    width: int
    height: int
    origin_x: int
    origin_y: int
    document_width: int
    document_height: int

    def writable_tile(self, coord: TileCoord) -> RGBAArray: ...

    def selection_region(self, x: int, y: int, width: int, height: int) -> MaskArray | None: ...


def blend_color(
    destination: RGBAArray,
    color: RGBAColor,
    coverage: np.ndarray,
    mode: PaintBlendMode = PaintBlendMode.NORMAL,
) -> RGBAArray:
    """Blend a constant straight-RGBA color over a destination array."""
    if destination.shape[:2] != coverage.shape:
        raise ValueError("coverage shape must match destination")
    if not all(0 <= channel <= 255 for channel in color):
        raise ValueError("RGBA channels must be between 0 and 255")
    strength = np.clip(coverage.astype(np.float32), 0.0, 1.0)
    destination_f = destination.astype(np.float32) / 255.0
    source_rgb = np.asarray(color[:3], dtype=np.float32) / 255.0
    if mode is PaintBlendMode.REPLACE:
        output = destination_f * (1.0 - strength[..., None])
        output += np.asarray(color, dtype=np.float32) / 255.0 * strength[..., None]
        return np.clip(np.rint(output * 255.0), 0, 255).astype(np.uint8)

    source_alpha = strength * (color[3] / 255.0)
    destination_alpha = destination_f[..., 3]
    output_alpha = source_alpha + destination_alpha * (1.0 - source_alpha)

    output = destination_f.copy()
    nonzero = output_alpha > 0.0
    destination_rgb = destination_f[..., :3]
    blend_rgb = _blend_rgb(destination_rgb, source_rgb, mode)
    numerator = (
        source_alpha[..., None] * (1.0 - destination_alpha[..., None]) * source_rgb
        + source_alpha[..., None] * destination_alpha[..., None] * blend_rgb
        + (1.0 - source_alpha[..., None]) * destination_alpha[..., None] * destination_rgb
    )
    output[..., :3] = np.divide(
        numerator,
        output_alpha[..., None],
        out=output[..., :3],
        where=nonzero[..., None],
    )
    output[..., 3] = output_alpha
    return np.clip(np.rint(output * 255.0), 0, 255).astype(np.uint8)


def _blend_rgb(
    destination: np.ndarray,
    source: np.ndarray,
    mode: PaintBlendMode,
) -> np.ndarray:
    if mode is PaintBlendMode.MULTIPLY:
        return np.asarray(destination * source, dtype=np.float32)
    if mode is PaintBlendMode.SCREEN:
        return np.asarray(
            destination + source - destination * source,
            dtype=np.float32,
        )
    if mode is PaintBlendMode.OVERLAY:
        return np.asarray(
            np.where(
                destination <= 0.5,
                2.0 * destination * source,
                1.0 - 2.0 * (1.0 - destination) * (1.0 - source),
            ),
            dtype=np.float32,
        )
    return np.asarray(np.broadcast_to(source, destination.shape), dtype=np.float32)


def erase_alpha(destination: RGBAArray, coverage: np.ndarray) -> RGBAArray:
    """Reduce alpha while preserving all hidden RGB bytes."""
    if destination.shape[:2] != coverage.shape:
        raise ValueError("coverage shape must match destination")
    output = destination.copy()
    alpha = destination[..., 3].astype(np.float32)
    output[..., 3] = np.clip(
        np.rint(alpha * (1.0 - np.clip(coverage, 0.0, 1.0))),
        0,
        255,
    ).astype(np.uint8)
    return output


def blend_color_behind(
    foreground: RGBAArray,
    color: RGBAColor,
    coverage: np.ndarray,
) -> RGBAArray:
    """Place a constant color behind existing straight-RGBA pixels."""
    if foreground.shape[:2] != coverage.shape:
        raise ValueError("coverage shape must match foreground")
    if not all(0 <= channel <= 255 for channel in color):
        raise ValueError("RGBA channels must be between 0 and 255")
    strength = np.clip(coverage.astype(np.float32), 0.0, 1.0)
    background_alpha = strength * (color[3] / 255.0)
    foreground_f = foreground.astype(np.float32) / 255.0
    foreground_alpha = foreground_f[..., 3]
    output_alpha = foreground_alpha + background_alpha * (1.0 - foreground_alpha)

    output = foreground_f.copy()
    nonzero = output_alpha > 0.0
    for channel in range(3):
        numerator = foreground_f[..., channel] * foreground_alpha + (
            color[channel] / 255.0
        ) * background_alpha * (1.0 - foreground_alpha)
        output[..., channel] = np.divide(
            numerator,
            output_alpha,
            out=output[..., channel],
            where=nonzero,
        )
    output[..., 3] = output_alpha
    return np.clip(np.rint(output * 255.0), 0, 255).astype(np.uint8)


class BrushStrokeEngine:
    """Interpolate pointer samples and apply each generated circular dab."""

    def __init__(
        self,
        surface: BrushSurface,
        settings: BrushSettings,
        color: RGBAColor,
        *,
        eraser: bool = False,
        changed: Callable[[frozenset[TileCoord]], None] | None = None,
    ) -> None:
        self.surface = surface
        self.settings = settings
        self.color = color
        self.eraser = eraser
        self.changed = changed
        self._last_input: tuple[float, float] | None = None
        self._distance_to_next = self._spacing_distance

    @property
    def _spacing_distance(self) -> float:
        return max(1.0, self.settings.size * self.settings.spacing)

    def begin(self, x: float, y: float) -> frozenset[TileCoord]:
        self._last_input = (x, y)
        self._distance_to_next = self._spacing_distance
        return self._apply_and_publish(x, y)

    def extend(self, x: float, y: float) -> frozenset[TileCoord]:
        if self._last_input is None:
            return self.begin(x, y)
        start_x, start_y = self._last_input
        delta_x = x - start_x
        delta_y = y - start_y
        length = math.hypot(delta_x, delta_y)
        if length <= 1e-9:
            return frozenset()

        unit_x = delta_x / length
        unit_y = delta_y / length
        travelled = 0.0
        dirty: set[TileCoord] = set()
        distance_to_next = self._distance_to_next
        while travelled + distance_to_next <= length + 1e-9:
            travelled += distance_to_next
            dirty.update(
                self._apply_dab(
                    start_x + unit_x * travelled,
                    start_y + unit_y * travelled,
                )
            )
            distance_to_next = self._spacing_distance
        distance_to_next -= length - travelled
        self._distance_to_next = max(1e-6, distance_to_next)
        self._last_input = (x, y)
        result = frozenset(dirty)
        if result and self.changed is not None:
            self.changed(result)
        return result

    def _apply_and_publish(self, x: float, y: float) -> frozenset[TileCoord]:
        result = self._apply_dab(x, y)
        if result and self.changed is not None:
            self.changed(result)
        return result

    def _apply_dab(self, center_x: float, center_y: float) -> frozenset[TileCoord]:
        radius = self.settings.size / 2.0
        padding = 0.5 if self.settings.antialias else 0.0
        raw_left = math.floor(center_x - radius - padding)
        raw_top = math.floor(center_y - radius - padding)
        raw_right = math.ceil(center_x + radius + padding)
        raw_bottom = math.ceil(center_y + radius + padding)

        left = max(0, raw_left)
        top = max(0, raw_top)
        right = min(
            self.surface.width,
            raw_right,
        )
        bottom = min(
            self.surface.height,
            raw_bottom,
        )
        if right <= left or bottom <= top:
            return frozenset()

        coverage = _dab_coverage(
            left,
            top,
            right,
            bottom,
            center_x,
            center_y,
            self.settings,
        )
        document_x = left + self.surface.origin_x
        document_y = top + self.surface.origin_y
        selection = self.surface.selection_region(
            document_x,
            document_y,
            right - left,
            bottom - top,
        )
        if selection is not None:
            coverage *= selection.astype(np.float32) / 255.0
        coverage *= self.settings.opacity
        if not np.any(coverage > 0.0):
            return frozenset()

        tile_size = DEFAULT_TILE_SIZE
        dirty: set[TileCoord] = set()
        for ty in range(top // tile_size, (bottom - 1) // tile_size + 1):
            for tx in range(left // tile_size, (right - 1) // tile_size + 1):
                coord = TileCoord(tx, ty)
                tile_left = tx * tile_size
                tile_top = ty * tile_size
                overlap_left = max(left, tile_left)
                overlap_top = max(top, tile_top)
                overlap_right = min(right, tile_left + tile_size)
                overlap_bottom = min(bottom, tile_top + tile_size)
                target_y = slice(overlap_top - tile_top, overlap_bottom - tile_top)
                target_x = slice(overlap_left - tile_left, overlap_right - tile_left)
                mask_y = slice(overlap_top - top, overlap_bottom - top)
                mask_x = slice(overlap_left - left, overlap_right - left)

                tile = self.surface.writable_tile(coord)
                before = tile[target_y, target_x]
                local_coverage = coverage[mask_y, mask_x]
                after = (
                    erase_alpha(before, local_coverage)
                    if self.eraser
                    else blend_color(
                        before,
                        self.color,
                        local_coverage,
                        self.settings.mode,
                    )
                )
                if np.array_equal(before, after):
                    continue
                tile[target_y, target_x] = after
                dirty.add(coord)
        return frozenset(dirty)


def _dab_coverage(
    left: int,
    top: int,
    right: int,
    bottom: int,
    center_x: float,
    center_y: float,
    settings: BrushSettings,
) -> np.ndarray:
    floor_x = math.floor(center_x)
    floor_y = math.floor(center_y)
    fraction_x = int(round((center_x - floor_x) * 4.0))
    fraction_y = int(round((center_y - floor_y) * 4.0))
    if fraction_x == 4:
        floor_x += 1
        fraction_x = 0
    if fraction_y == 4:
        floor_y += 1
        fraction_y = 0
    return _cached_coverage(
        left - floor_x,
        top - floor_y,
        right - left,
        bottom - top,
        settings.size,
        int(round(settings.hardness * 1000)),
        settings.antialias,
        fraction_x,
        fraction_y,
    ).copy()


@lru_cache(maxsize=256)
def _cached_coverage(
    relative_left: int,
    relative_top: int,
    width: int,
    height: int,
    size: int,
    hardness_milli: int,
    antialias: bool,
    fraction_x: int,
    fraction_y: int,
) -> np.ndarray:
    center_fraction_x = fraction_x / 4.0
    center_fraction_y = fraction_y / 4.0
    pixel_x = np.arange(relative_left, relative_left + width, dtype=np.float32) + 0.5
    pixel_y = np.arange(relative_top, relative_top + height, dtype=np.float32) + 0.5
    distance = np.hypot(
        pixel_x[np.newaxis, :] - center_fraction_x,
        pixel_y[:, np.newaxis] - center_fraction_y,
    )
    radius = size / 2.0
    hardness_radius = radius * (hardness_milli / 1000.0)
    if not antialias and hardness_milli == 1000:
        return cast(np.ndarray, (distance <= radius).astype(np.float32))
    outer_radius = radius + (0.5 if antialias else 0.0)
    feather = max(outer_radius - hardness_radius, 1e-6)
    return cast(
        np.ndarray,
        np.clip((outer_radius - distance) / feather, 0.0, 1.0).astype(np.float32),
    )


__all__ = [
    "DEFAULT_BRUSH_SIZE_LIMIT",
    "MAX_BRUSH_SIZE",
    "PaintBlendMode",
    "BrushSettings",
    "BrushStrokeEngine",
    "BrushSurface",
    "RGBAColor",
    "blend_color",
    "blend_color_behind",
    "erase_alpha",
]
