"""Qt-independent geometry value objects."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Size:
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("size dimensions must be positive")


@dataclass(frozen=True, slots=True)
class PixelPoint:
    x: int = 0
    y: int = 0


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float


__all__ = ["PixelPoint", "Point", "Size"]
