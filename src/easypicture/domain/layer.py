"""Raster layer domain entity."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from easypicture.domain.geometry import PixelPoint, Size
from easypicture.pixels import DEFAULT_TILE_SIZE, TilePixelStore


@dataclass(slots=True)
class RasterLayer:
    id: UUID
    name: str
    size: Size
    origin: PixelPoint
    pixels: TilePixelStore
    visible: bool = True
    opacity: float = 1.0
    locked: bool = False
    alpha_locked: bool = False
    _revision: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("layer name must not be empty")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("layer opacity must be between 0 and 1")

    @classmethod
    def create_blank(
        cls,
        name: str,
        size: Size,
        *,
        origin: PixelPoint | None = None,
    ) -> RasterLayer:
        return cls(
            id=uuid4(),
            name=name,
            size=size,
            origin=origin if origin is not None else PixelPoint(),
            pixels=TilePixelStore(DEFAULT_TILE_SIZE),
        )

    @property
    def revision(self) -> int:
        return self._revision

    def record_pixel_change(self) -> None:
        self._revision += 1


__all__ = ["RasterLayer"]
