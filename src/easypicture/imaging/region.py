"""Reusable RGBA tolerance and connected-region selection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

import cv2
import numpy as np

from easypicture.pixels import DEFAULT_TILE_SIZE, MaskArray, RGBAArray, TileCoord


class CancellationCheck(Protocol):
    def raise_if_cancelled(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RegionQuery:
    seed: tuple[int, int]
    tolerance: int = 0
    connectivity: Literal[4, 8] = 4
    contiguous: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.tolerance <= 255:
            raise ValueError("region tolerance must be between 0 and 255")
        if self.connectivity not in (4, 8):
            raise ValueError("region connectivity must be 4 or 8")


@dataclass(frozen=True, slots=True)
class RegionResult:
    tiles: Mapping[TileCoord, MaskArray]


def find_region(
    reference: RGBAArray,
    query: RegionQuery,
    cancellation: CancellationCheck | None = None,
) -> RegionResult:
    if reference.dtype != np.uint8 or reference.ndim != 3 or reference.shape[2] != 4:
        raise ValueError("region reference must be uint8 RGBA")
    seed_x, seed_y = query.seed
    height, width = reference.shape[:2]
    if not 0 <= seed_x < width or not 0 <= seed_y < height:
        raise ValueError("region seed lies outside the reference")
    if cancellation is not None:
        cancellation.raise_if_cancelled()

    target = reference[seed_y, seed_x].astype(np.int16)
    difference = np.abs(reference.astype(np.int16) - target)
    candidate = np.max(difference, axis=2) <= query.tolerance
    if cancellation is not None:
        cancellation.raise_if_cancelled()

    if query.contiguous:
        work = candidate.astype(np.uint8) * 255
        cv2.floodFill(
            work,
            None,
            (seed_x, seed_y),
            128,
            loDiff=0,
            upDiff=0,
            flags=query.connectivity | cv2.FLOODFILL_FIXED_RANGE,
        )
        selected = work == 128
    else:
        selected = candidate

    if cancellation is not None:
        cancellation.raise_if_cancelled()
    tiles: dict[TileCoord, MaskArray] = {}
    tile_size = DEFAULT_TILE_SIZE
    for ty in range((height + tile_size - 1) // tile_size):
        for tx in range((width + tile_size - 1) // tile_size):
            left = tx * tile_size
            top = ty * tile_size
            portion = selected[
                top : min(top + tile_size, height),
                left : min(left + tile_size, width),
            ]
            if not np.any(portion):
                continue
            tile = np.zeros((tile_size, tile_size), dtype=np.uint8)
            tile[: portion.shape[0], : portion.shape[1]] = portion.astype(np.uint8) * 255
            tiles[TileCoord(tx, ty)] = tile
    return RegionResult(tiles)


__all__ = ["CancellationCheck", "RegionQuery", "RegionResult", "find_region"]
