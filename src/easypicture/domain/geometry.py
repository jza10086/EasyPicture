"""Qt-independent geometry value objects."""

from __future__ import annotations

import math
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


@dataclass(frozen=True, slots=True)
class FloatRect:
    """A finite floating-point rectangle used by vector objects."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (self.x, self.y, self.width, self.height)
        ):
            raise ValueError("rectangle values must be finite")
        if self.width <= 0.0 or self.height <= 0.0:
            raise ValueError("rectangle dimensions must be positive")

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


@dataclass(frozen=True, slots=True)
class AffineTransform:
    """Qt-free 2D affine transform using the conventional 2x3 layout."""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (self.a, self.b, self.c, self.d, self.tx, self.ty)
        ):
            raise ValueError("affine transform values must be finite")

    @property
    def determinant(self) -> float:
        return self.a * self.d - self.b * self.c

    @property
    def is_identity(self) -> bool:
        return self.almost_equals(AffineTransform())

    def almost_equals(self, other: AffineTransform, *, tolerance: float = 1e-9) -> bool:
        return all(
            abs(left - right) <= tolerance
            for left, right in zip(self.values, other.values, strict=True)
        )

    @property
    def values(self) -> tuple[float, float, float, float, float, float]:
        return self.a, self.b, self.c, self.d, self.tx, self.ty

    def map_point(self, point: Point) -> Point:
        return Point(
            self.a * point.x + self.c * point.y + self.tx,
            self.b * point.x + self.d * point.y + self.ty,
        )

    def then(self, after: AffineTransform) -> AffineTransform:
        """Return a transform that applies this transform, then ``after``."""
        return AffineTransform(
            a=after.a * self.a + after.c * self.b,
            b=after.b * self.a + after.d * self.b,
            c=after.a * self.c + after.c * self.d,
            d=after.b * self.c + after.d * self.d,
            tx=after.a * self.tx + after.c * self.ty + after.tx,
            ty=after.b * self.tx + after.d * self.ty + after.ty,
        )

    def inverse(self) -> AffineTransform:
        determinant = self.determinant
        if abs(determinant) <= 1e-12:
            raise ValueError("affine transform is singular")
        return AffineTransform(
            a=self.d / determinant,
            b=-self.b / determinant,
            c=-self.c / determinant,
            d=self.a / determinant,
            tx=(self.c * self.ty - self.d * self.tx) / determinant,
            ty=(self.b * self.tx - self.a * self.ty) / determinant,
        )

    @classmethod
    def translation(cls, x: float, y: float) -> AffineTransform:
        return cls(tx=x, ty=y)

    @classmethod
    def scale(cls, x: float, y: float) -> AffineTransform:
        return cls(a=x, d=y)

    @classmethod
    def rotation(cls, degrees: float) -> AffineTransform:
        radians = math.radians(degrees)
        cosine = math.cos(radians)
        sine = math.sin(radians)
        return cls(a=cosine, b=sine, c=-sine, d=cosine)

    @classmethod
    def shear(cls, x_degrees: float, y_degrees: float) -> AffineTransform:
        return cls(
            a=1.0,
            b=math.tan(math.radians(y_degrees)),
            c=math.tan(math.radians(x_degrees)),
            d=1.0,
        )


@dataclass(frozen=True, slots=True)
class ProjectiveTransform:
    """Qt-free 3x3 projective transform using conventional row-major layout."""

    m11: float = 1.0
    m12: float = 0.0
    m13: float = 0.0
    m21: float = 0.0
    m22: float = 1.0
    m23: float = 0.0
    m31: float = 0.0
    m32: float = 0.0
    m33: float = 1.0

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in self.values):
            raise ValueError("projective transform values must be finite")

    @property
    def values(self) -> tuple[float, float, float, float, float, float, float, float, float]:
        return (
            self.m11,
            self.m12,
            self.m13,
            self.m21,
            self.m22,
            self.m23,
            self.m31,
            self.m32,
            self.m33,
        )

    @property
    def determinant(self) -> float:
        return (
            self.m11 * (self.m22 * self.m33 - self.m23 * self.m32)
            - self.m12 * (self.m21 * self.m33 - self.m23 * self.m31)
            + self.m13 * (self.m21 * self.m32 - self.m22 * self.m31)
        )

    @property
    def is_identity(self) -> bool:
        return self.almost_equals(ProjectiveTransform())

    def almost_equals(
        self,
        other: ProjectiveTransform,
        *,
        tolerance: float = 1e-9,
    ) -> bool:
        return all(
            abs(left - right) <= tolerance
            for left, right in zip(self.values, other.values, strict=True)
        )

    def map_point(self, point: Point) -> Point:
        denominator = self.m31 * point.x + self.m32 * point.y + self.m33
        if abs(denominator) <= 1e-12:
            raise ValueError("point maps to projective infinity")
        return Point(
            (self.m11 * point.x + self.m12 * point.y + self.m13) / denominator,
            (self.m21 * point.x + self.m22 * point.y + self.m23) / denominator,
        )

    def then(self, after: ProjectiveTransform) -> ProjectiveTransform:
        """Return a transform that applies this transform, then ``after``."""
        left = (
            (after.m11, after.m12, after.m13),
            (after.m21, after.m22, after.m23),
            (after.m31, after.m32, after.m33),
        )
        right = (
            (self.m11, self.m12, self.m13),
            (self.m21, self.m22, self.m23),
            (self.m31, self.m32, self.m33),
        )
        values = tuple(
            sum(left[row][index] * right[index][column] for index in range(3))
            for row in range(3)
            for column in range(3)
        )
        return ProjectiveTransform(*values)

    def inverse(self) -> ProjectiveTransform:
        determinant = self.determinant
        if abs(determinant) <= 1e-12:
            raise ValueError("projective transform is singular")
        cofactors = (
            self.m22 * self.m33 - self.m23 * self.m32,
            self.m13 * self.m32 - self.m12 * self.m33,
            self.m12 * self.m23 - self.m13 * self.m22,
            self.m23 * self.m31 - self.m21 * self.m33,
            self.m11 * self.m33 - self.m13 * self.m31,
            self.m13 * self.m21 - self.m11 * self.m23,
            self.m21 * self.m32 - self.m22 * self.m31,
            self.m12 * self.m31 - self.m11 * self.m32,
            self.m11 * self.m22 - self.m12 * self.m21,
        )
        return ProjectiveTransform(*(value / determinant for value in cofactors))

    @classmethod
    def from_affine(cls, transform: AffineTransform) -> ProjectiveTransform:
        return cls(
            transform.a,
            transform.c,
            transform.tx,
            transform.b,
            transform.d,
            transform.ty,
        )

    @classmethod
    def translation(cls, x: float, y: float) -> ProjectiveTransform:
        return cls(m13=x, m23=y)


type TransformMatrix = AffineTransform | ProjectiveTransform


def as_projective(transform: TransformMatrix) -> ProjectiveTransform:
    if isinstance(transform, ProjectiveTransform):
        return transform
    return ProjectiveTransform.from_affine(transform)


__all__ = [
    "AffineTransform",
    "FloatRect",
    "PixelPoint",
    "Point",
    "ProjectiveTransform",
    "Size",
    "TransformMatrix",
    "as_projective",
]
