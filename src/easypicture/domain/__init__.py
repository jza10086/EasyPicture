"""Qt-free EasyPicture document domain."""

from easypicture.domain.document import Document
from easypicture.domain.events import (
    ActiveLayerChanged,
    DocumentEvent,
    DocumentModifiedChanged,
    DocumentPathChanged,
    DocumentRevisionChanged,
    LayerListChanged,
    LayerPixelsChanged,
    LayerPropertiesChanged,
    SelectionChanged,
)
from easypicture.domain.geometry import (
    AffineTransform,
    PixelPoint,
    Point,
    ProjectiveTransform,
    Size,
    TransformMatrix,
    as_projective,
)
from easypicture.domain.layer import RasterLayer
from easypicture.domain.selection import Selection

__all__ = [
    "ActiveLayerChanged",
    "AffineTransform",
    "Document",
    "DocumentEvent",
    "DocumentModifiedChanged",
    "DocumentPathChanged",
    "DocumentRevisionChanged",
    "LayerListChanged",
    "LayerPixelsChanged",
    "LayerPropertiesChanged",
    "SelectionChanged",
    "PixelPoint",
    "Point",
    "ProjectiveTransform",
    "RasterLayer",
    "Selection",
    "Size",
    "TransformMatrix",
    "as_projective",
]
