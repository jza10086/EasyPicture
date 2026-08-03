"""Central soft and hard range policy for spatial numeric UI controls."""

from __future__ import annotations

from dataclasses import dataclass

from easypicture.domain.document import MAX_DOCUMENT_DIMENSION
from easypicture.domain.vector import MAX_TEXT_FONT_SIZE


@dataclass(frozen=True, slots=True)
class UiNumericRange:
    soft_minimum: float
    soft_maximum: float
    hard_minimum: float
    hard_maximum: float


COMMON_CANVAS_SIZE = 4096.0
EXTENDED_SPATIAL_LIMIT = float(MAX_DOCUMENT_DIMENSION * 4)

DOCUMENT_DIMENSION_RANGE = UiNumericRange(
    1.0,
    COMMON_CANVAS_SIZE,
    1.0,
    float(MAX_DOCUMENT_DIMENSION),
)
TRANSFORM_DIMENSION_RANGE = DOCUMENT_DIMENSION_RANGE
OBJECT_DIMENSION_RANGE = UiNumericRange(
    0.01,
    COMMON_CANVAS_SIZE,
    0.01,
    EXTENDED_SPATIAL_LIMIT,
)
SIGNED_COORDINATE_RANGE = UiNumericRange(
    -COMMON_CANVAS_SIZE,
    COMMON_CANVAS_SIZE,
    -EXTENDED_SPATIAL_LIMIT,
    EXTENDED_SPATIAL_LIMIT,
)
ROTATION_DEGREES_RANGE = UiNumericRange(-180.0, 180.0, -36000.0, 36000.0)
OBJECT_RADIUS_RANGE = UiNumericRange(
    0.0,
    COMMON_CANVAS_SIZE / 2.0,
    0.0,
    EXTENDED_SPATIAL_LIMIT / 2.0,
)
STROKE_WIDTH_RANGE = UiNumericRange(0.0, 256.0, 0.0, 8192.0)
DASH_LENGTH_RANGE = UiNumericRange(0.0, 256.0, 0.0, 8192.0)
DASH_OFFSET_RANGE = UiNumericRange(-4096.0, 4096.0, -8192.0, 8192.0)
MITER_LIMIT_RANGE = UiNumericRange(1.0, 100.0, 1.0, 1000.0)
FONT_SIZE_RANGE = UiNumericRange(1.0, 256.0, 1.0, MAX_TEXT_FONT_SIZE)


__all__ = [
    "COMMON_CANVAS_SIZE",
    "DASH_LENGTH_RANGE",
    "DASH_OFFSET_RANGE",
    "DOCUMENT_DIMENSION_RANGE",
    "EXTENDED_SPATIAL_LIMIT",
    "FONT_SIZE_RANGE",
    "MITER_LIMIT_RANGE",
    "OBJECT_DIMENSION_RANGE",
    "OBJECT_RADIUS_RANGE",
    "ROTATION_DEGREES_RANGE",
    "SIGNED_COORDINATE_RANGE",
    "STROKE_WIDTH_RANGE",
    "TRANSFORM_DIMENSION_RANGE",
    "UiNumericRange",
]
