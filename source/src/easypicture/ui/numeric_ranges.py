"""Central soft and hard range policy for spatial numeric UI controls."""

from __future__ import annotations

from dataclasses import dataclass

from easypicture.domain.document import MAX_DOCUMENT_DIMENSION
from easypicture.domain.vector import MAX_TEXT_FONT_SIZE
from easypicture.ui.drag_value_spinbox import DragValueDoubleSpinBox


@dataclass(frozen=True, slots=True)
class UiNumericRange:
    soft_minimum: float
    soft_maximum: float
    hard_minimum: float
    hard_maximum: float
    default_value: float = 0.0


COMMON_CANVAS_SIZE = 4096.0
EXTENDED_SPATIAL_LIMIT = float(MAX_DOCUMENT_DIMENSION * 4)

DOCUMENT_DIMENSION_RANGE = UiNumericRange(
    1.0,
    COMMON_CANVAS_SIZE,
    1.0,
    float(MAX_DOCUMENT_DIMENSION),
    32.0,
)
TRANSFORM_DIMENSION_RANGE = DOCUMENT_DIMENSION_RANGE
OBJECT_DIMENSION_RANGE = UiNumericRange(
    0.01,
    COMMON_CANVAS_SIZE,
    0.01,
    EXTENDED_SPATIAL_LIMIT,
    32.0,
)
SIGNED_COORDINATE_RANGE = UiNumericRange(
    -256.0,
    256.0,
    -EXTENDED_SPATIAL_LIMIT,
    EXTENDED_SPATIAL_LIMIT,
)
ROTATION_DEGREES_RANGE = UiNumericRange(-180.0, 180.0, -36000.0, 36000.0)
OBJECT_SCALE_RANGE = UiNumericRange(0.1, 4.0, -100.0, 100.0, 1.0)
OBJECT_RADIUS_RANGE = UiNumericRange(
    0.0,
    128.0,
    0.0,
    EXTENDED_SPATIAL_LIMIT / 2.0,
    16.0,
)
STROKE_WIDTH_RANGE = UiNumericRange(0.0, 32.0, 0.0, 8192.0, 1.0)
DASH_LENGTH_RANGE = UiNumericRange(0.0, 64.0, 0.0, 8192.0)
DASH_OFFSET_RANGE = UiNumericRange(-64.0, 64.0, -8192.0, 8192.0)
MITER_LIMIT_RANGE = UiNumericRange(1.0, 100.0, 1.0, 1000.0, 4.0)
FONT_SIZE_RANGE = UiNumericRange(1.0, 256.0, 1.0, MAX_TEXT_FONT_SIZE, 32.0)
PALETTE_COLOR_COUNT_RANGE = UiNumericRange(1, 64, 1, 256, 16)
PIXEL_OUTLINE_WIDTH_RANGE = UiNumericRange(1, 16, 1, 64, 1)
AI_PIXEL_BLOCK_RANGE = UiNumericRange(1, 64, 1, 4096, 8)
AI_PALETTE_COLOR_COUNT_RANGE = UiNumericRange(1, 64, 1, 256, 32)


def sync_spatial_range(
    field: DragValueDoubleSpinBox, extent: float, *, dimension: bool = False
) -> None:
    """Canvas-aware drag range; manual entry retains the spatial hard limit."""
    field.syncPropertyRange(
        0.01 if dimension else -extent,
        extent,
        manual_minimum=0.01 if dimension else -EXTENDED_SPATIAL_LIMIT,
        manual_maximum=EXTENDED_SPATIAL_LIMIT,
    )


__all__ = [
    "AI_PIXEL_BLOCK_RANGE",
    "AI_PALETTE_COLOR_COUNT_RANGE",
    "PALETTE_COLOR_COUNT_RANGE",
    "PIXEL_OUTLINE_WIDTH_RANGE",
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
