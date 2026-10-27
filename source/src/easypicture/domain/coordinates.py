"""Immutable object-local mappings shared by pixel tools and their previews."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from easypicture.domain.geometry import AffineTransform, FloatRect, Point, Size
from easypicture.pixels import MaskArray, MaskTileStore


def validate_object_transform(matrix: AffineTransform) -> None:
    if abs(matrix.determinant) < 1e-8:
        raise ValueError("object transform is not invertible")
    if any(abs(value) > 1024 for value in matrix.values[:4]):
        raise ValueError("object transform scale exceeds the limit")
    if any(abs(value) > 1_000_000 for value in matrix.values[4:]):
        raise ValueError("object transform position exceeds the limit")
    inverse = matrix.inverse()
    if any(abs(value) > 1_000_000 for value in inverse.values[:4]):
        raise ValueError("object transform is too small to edit")


@dataclass(frozen=True, slots=True)
class RasterMapping:
    matrix: AffineTransform
    size: Size

    def __post_init__(self) -> None:
        validate_object_transform(self.matrix)

    def to_local(self, point: Point) -> Point:
        return self.matrix.inverse().map_point(point)

    def to_document(self, point: Point) -> Point:
        return self.matrix.map_point(point)

    def contains_document_point(self, point: Point) -> bool:
        local = self.to_local(point)
        return 0 <= local.x < self.size.width and 0 <= local.y < self.size.height

    @property
    def bounds(self) -> FloatRect:
        points = [
            self.to_document(Point(x, y))
            for x in (0, self.size.width)
            for y in (0, self.size.height)
        ]
        left, top = min(p.x for p in points), min(p.y for p in points)
        return FloatRect(left, top, max(p.x for p in points) - left, max(p.y for p in points) - top)

    def brush_outline(self, center: Point, diameter: float) -> tuple[Point, ...]:
        local = self.to_local(center)
        radius = diameter / 2
        return tuple(
            self.to_document(
                Point(
                    local.x + radius * math.cos(index * math.tau / 64),
                    local.y + radius * math.sin(index * math.tau / 64),
                )
            )
            for index in range(64)
        )

    def selection_coverage(
        self,
        mask: MaskTileStore,
        canvas: Size,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        smooth: bool = False,
    ) -> MaskArray:
        """Map local pixel centers onto the document selection without enlarging it."""
        yy, xx = np.indices((height, width), dtype=np.float64)
        xx += x + 0.5
        yy += y + 0.5
        matrix = self.matrix
        dx = matrix.a * xx + matrix.c * yy + matrix.tx
        dy = matrix.b * xx + matrix.d * yy + matrix.ty
        if not smooth:
            return _sample_mask(mask, canvas, np.floor(dx), np.floor(dy))
        dx -= 0.5
        dy -= 0.5
        ix, iy = np.floor(dx), np.floor(dy)
        fx, fy = dx - ix, dy - iy
        result = (
            _sample_mask(mask, canvas, ix, iy) * (1 - fx) * (1 - fy)
            + _sample_mask(mask, canvas, ix + 1, iy) * fx * (1 - fy)
            + _sample_mask(mask, canvas, ix, iy + 1) * (1 - fx) * fy
            + _sample_mask(mask, canvas, ix + 1, iy + 1) * fx * fy
        )
        return np.rint(result).astype(np.uint8)


def _sample_mask(mask: MaskTileStore, canvas: Size, x: np.ndarray, y: np.ndarray) -> MaskArray:
    x, y = x.astype(np.int64), y.astype(np.int64)
    result = np.zeros(x.shape, dtype=np.uint8)
    valid = (x >= 0) & (y >= 0) & (x < canvas.width) & (y < canvas.height)
    tile_size = mask.tile_size
    for tx, ty in np.unique(
        np.stack((x[valid] // tile_size, y[valid] // tile_size), axis=1), axis=0
    ):
        selected = valid & (x // tile_size == tx) & (y // tile_size == ty)
        tile = mask.read_tile(int(tx), int(ty))
        result[selected] = tile[y[selected] % tile_size, x[selected] % tile_size]
    return result
