"""Document selection state."""

from __future__ import annotations

from dataclasses import dataclass

from easypicture.domain.geometry import Size
from easypicture.pixels import DEFAULT_TILE_SIZE, MaskTileStore


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
