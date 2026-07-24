"""Qt-free image processing services."""

from easypicture.imaging.brush import (
    DEFAULT_BRUSH_SIZE_LIMIT,
    MAX_BRUSH_SIZE,
    BrushSettings,
    BrushStrokeEngine,
    RGBAColor,
    blend_color,
    blend_color_behind,
    erase_alpha,
)
from easypicture.imaging.composite import compose_document_tile
from easypicture.imaging.fill import FillRequest, FillResult, calculate_fill
from easypicture.imaging.region import RegionQuery, RegionResult, find_region
from easypicture.imaging.sampling import sample_document_pixel

__all__ = [
    "BrushSettings",
    "BrushStrokeEngine",
    "DEFAULT_BRUSH_SIZE_LIMIT",
    "FillRequest",
    "FillResult",
    "MAX_BRUSH_SIZE",
    "RGBAColor",
    "RegionQuery",
    "RegionResult",
    "blend_color",
    "blend_color_behind",
    "calculate_fill",
    "compose_document_tile",
    "erase_alpha",
    "find_region",
    "sample_document_pixel",
]
