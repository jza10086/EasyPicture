"""Raster layer domain entity."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4

from easypicture.domain.geometry import AffineTransform, PixelPoint, Point, Size
from easypicture.pixels import DEFAULT_TILE_SIZE, TilePixelStore


class RasterSampling(StrEnum):
    NEAREST = "nearest"
    SMOOTH = "smooth"


@dataclass(slots=True)
class RasterObject:
    id: UUID
    name: str
    size: Size
    origin: PixelPoint
    pixels: TilePixelStore
    visible: bool = True
    opacity: float = 1.0
    locked: bool = False
    alpha_locked: bool = False
    transform: AffineTransform = AffineTransform()
    transform_pivot: Point | None = None
    sampling: RasterSampling = RasterSampling.NEAREST
    _revision: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("layer name must not be empty")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("layer opacity must be between 0 and 1")
        from easypicture.domain.coordinates import validate_object_transform

        validate_object_transform(self.world_transform)
        if not isinstance(self.sampling, RasterSampling):
            raise ValueError("invalid raster sampling mode")

    @property
    def world_transform(self) -> AffineTransform:
        return self.transform.then(AffineTransform.translation(self.origin.x, self.origin.y))

    @classmethod
    def create_blank(
        cls,
        name: str,
        size: Size,
        *,
        origin: PixelPoint | None = None,
    ) -> RasterObject:
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

    def duplicate(self, name: str | None = None) -> RasterObject:
        return RasterObject(
            id=uuid4(),
            name=name if name is not None else f"{self.name} 副本",
            size=self.size,
            origin=self.origin,
            pixels=self.pixels.clone_shared(),
            visible=self.visible,
            opacity=self.opacity,
            locked=self.locked,
            alpha_locked=self.alpha_locked,
            transform=self.transform,
            transform_pivot=self.transform_pivot,
            sampling=self.sampling,
        )


__all__ = ["RasterObject", "RasterSampling"]
