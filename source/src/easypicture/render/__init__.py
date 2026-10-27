"""Qt-specific derived rendering caches and bridges."""

from easypicture.domain import EffectiveLeaf, iter_effective_leaves
from easypicture.render.canvas_frame import (
    CanvasRenderCoordinator,
    CanvasRenderDiagnostics,
    GeometryPreviewState,
    RenderBackendKind,
    TransformPreviewState,
)
from easypicture.render.opengl_canvas import OpenGLCanvasBackend
from easypicture.render.qimage_bridge import qimage_to_rgba, rgba_tile_to_qimage
from easypicture.render.text_renderer import (
    paint_text_object,
    paint_vector_groups,
    paint_vector_objects,
)

__all__ = [
    "CanvasRenderCoordinator",
    "CanvasRenderDiagnostics",
    "EffectiveLeaf",
    "GeometryPreviewState",
    "OpenGLCanvasBackend",
    "RenderBackendKind",
    "TransformPreviewState",
    "rgba_tile_to_qimage",
    "qimage_to_rgba",
    "paint_vector_groups",
    "paint_text_object",
    "paint_vector_objects",
    "iter_effective_leaves",
]
