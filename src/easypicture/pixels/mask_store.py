"""Sparse copy-on-write Tile storage for uint8 selection masks."""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

from easypicture.pixels.types import (
    DEFAULT_TILE_SIZE,
    DirtyRegion,
    MaskArray,
    Rect,
    TileCoord,
    validate_mask_array,
)


class MaskTileStore:
    """Store a sparse uint8 mask where missing tiles mean fully unselected."""

    def __init__(self, tile_size: int = DEFAULT_TILE_SIZE) -> None:
        if tile_size <= 0:
            raise ValueError("tile_size must be positive")
        self.tile_size = tile_size
        self._tiles: dict[TileCoord, MaskArray] = {}
        self._tile_revisions: dict[TileCoord, int] = {}
        self._revision = 0
        self._empty_tile = np.zeros((tile_size, tile_size), dtype=np.uint8)
        self._empty_tile.flags.writeable = False

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def allocated_tile_count(self) -> int:
        return len(self._tiles)

    @property
    def allocated_bytes(self) -> int:
        return sum(tile.nbytes for tile in self._tiles.values())

    def tile_revision(self, tx: int, ty: int) -> int:
        coord = TileCoord(tx, ty)
        return self._tile_revisions.get(coord, 0)

    def read_tile(self, tx: int, ty: int) -> MaskArray:
        coord = TileCoord(tx, ty)
        tile = self._tiles.get(coord, self._empty_tile)
        view = tile.view()
        view.flags.writeable = False
        return view

    def writable_tile(self, tx: int, ty: int) -> MaskArray:
        coord = TileCoord(tx, ty)
        current = self._tiles.get(coord)
        replacement = (
            np.zeros((self.tile_size, self.tile_size), dtype=np.uint8)
            if current is None
            else current.copy(order="C")
        )
        self._tiles[coord] = replacement
        self._revision += 1
        self._tile_revisions[coord] = self._tile_revisions.get(coord, 0) + 1
        return replacement

    def read_region(self, rect: Rect) -> MaskArray:
        output = np.zeros((rect.height, rect.width), dtype=np.uint8)
        if rect.is_empty:
            return output

        for coord in self._coords_for_rect(rect):
            tile = self._tiles.get(coord)
            if tile is None:
                continue
            source_y, source_x, target_y, target_x = self._overlap_slices(rect, coord)
            output[target_y, target_x] = tile[source_y, source_x]
        return output

    def write_region(self, rect: Rect, pixels: MaskArray) -> DirtyRegion:
        validate_mask_array(
            pixels,
            expected_width=rect.width,
            expected_height=rect.height,
        )
        if rect.is_empty:
            return DirtyRegion(rect=rect, tiles=frozenset())

        dirty_tiles: set[TileCoord] = set()
        for coord in self._coords_for_rect(rect):
            target = self.writable_tile(coord.x, coord.y)
            target_y, target_x, source_y, source_x = self._overlap_slices(rect, coord)
            target[target_y, target_x] = pixels[source_y, source_x]
            dirty_tiles.add(coord)
        return DirtyRegion(rect=rect, tiles=frozenset(dirty_tiles))

    def iter_allocated_tiles(self) -> Iterator[tuple[TileCoord, MaskArray]]:
        for coord in sorted(self._tiles, key=lambda item: (item.y, item.x)):
            yield coord, self.read_tile(coord.x, coord.y)

    def _coords_for_rect(self, rect: Rect) -> Iterator[TileCoord]:
        if rect.is_empty:
            return
        first_tx = rect.x // self.tile_size
        first_ty = rect.y // self.tile_size
        last_tx = (rect.right - 1) // self.tile_size
        last_ty = (rect.bottom - 1) // self.tile_size
        for ty in range(first_ty, last_ty + 1):
            for tx in range(first_tx, last_tx + 1):
                yield TileCoord(tx, ty)

    def _overlap_slices(
        self,
        rect: Rect,
        coord: TileCoord,
    ) -> tuple[slice, slice, slice, slice]:
        tile_left = coord.x * self.tile_size
        tile_top = coord.y * self.tile_size
        overlap_left = max(rect.x, tile_left)
        overlap_top = max(rect.y, tile_top)
        overlap_right = min(rect.right, tile_left + self.tile_size)
        overlap_bottom = min(rect.bottom, tile_top + self.tile_size)

        tile_y = slice(overlap_top - tile_top, overlap_bottom - tile_top)
        tile_x = slice(overlap_left - tile_left, overlap_right - tile_left)
        region_y = slice(overlap_top - rect.y, overlap_bottom - rect.y)
        region_x = slice(overlap_left - rect.x, overlap_right - rect.x)
        return tile_y, tile_x, region_y, region_x


__all__ = ["MaskTileStore"]
