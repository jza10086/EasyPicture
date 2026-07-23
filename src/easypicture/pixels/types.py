"""Shared pixel and geometry types for the P0 prototype."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

type RGBAArray = NDArray[np.uint8]


@dataclass(frozen=True, slots=True, order=True)
class TileCoord:
    """Non-negative tile coordinates."""

    x: int
    y: int

    def __post_init__(self) -> None:
        if self.x < 0 or self.y < 0:
            raise ValueError("tile coordinates must be non-negative")


@dataclass(frozen=True, slots=True)
class Rect:
    """A non-negative half-open pixel rectangle."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.x < 0 or self.y < 0:
            raise ValueError("rectangle origin must be non-negative")
        if self.width < 0 or self.height < 0:
            raise ValueError("rectangle dimensions must be non-negative")

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def is_empty(self) -> bool:
        return self.width == 0 or self.height == 0


@dataclass(frozen=True, slots=True)
class DirtyRegion:
    """The exact rectangle and tiles dirtied by a write."""

    rect: Rect
    tiles: frozenset[TileCoord]


def validate_rgba_array(
    pixels: RGBAArray,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
) -> None:
    """Validate the authoritative uint8, C-contiguous, straight-RGBA contract."""
    if not isinstance(pixels, np.ndarray):
        raise TypeError("pixels must be a numpy.ndarray")
    if pixels.dtype != np.uint8:
        raise TypeError("pixels must use dtype uint8")
    if pixels.ndim != 3 or pixels.shape[2] != 4:
        raise ValueError("pixels must have shape (height, width, 4)")
    if expected_width is not None and pixels.shape[1] != expected_width:
        raise ValueError(f"pixel width must be {expected_width}")
    if expected_height is not None and pixels.shape[0] != expected_height:
        raise ValueError(f"pixel height must be {expected_height}")
    if not pixels.flags.c_contiguous:
        raise ValueError("pixels must be C-contiguous")


__all__ = [
    "DirtyRegion",
    "RGBAArray",
    "Rect",
    "TileCoord",
    "validate_rgba_array",
]
