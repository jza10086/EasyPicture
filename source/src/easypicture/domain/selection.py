"""Document selection state."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from easypicture.domain.geometry import Size
from easypicture.pixels import DEFAULT_TILE_SIZE, MaskArray, MaskTileStore, Rect


def selection_coverage(
    mask: MaskTileStore,
    size: Size,
    x: int,
    y: int,
    width: int,
    height: int,
) -> MaskArray:
    """Read document-space coverage, with zero coverage outside output bounds."""
    result = np.zeros((height, width), dtype=np.uint8)
    left, top = max(0, x), max(0, y)
    right, bottom = min(size.width, x + width), min(size.height, y + height)
    if right > left and bottom > top:
        result[top - y : bottom - y, left - x : right - x] = mask.read_region(
            Rect(left, top, right - left, bottom - top)
        )
    return result


@dataclass(slots=True)
class Selection:
    size: Size
    mask: MaskTileStore

    @classmethod
    def create_empty(cls, size: Size) -> Selection:
        return cls(size=size, mask=MaskTileStore(DEFAULT_TILE_SIZE))

    @property
    def revision(self) -> int:
        return self.mask.revision

    @property
    def is_active(self) -> bool:
        """An empty sparse mask means editing is unconstrained."""
        return self.mask.has_nonzero_data


__all__ = ["Selection"]
