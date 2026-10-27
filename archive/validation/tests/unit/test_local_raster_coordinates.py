import numpy as np

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document
from easypicture.imaging import (
    BrushSettings,
    BrushStrokeEngine,
    CropDocumentRequest,
    GeometryRect,
    ShapeGeometry,
    ShapeKind,
    ShapePaintStyle,
    ShapeRasterRequest,
    ShapeSettings,
    calculate_crop_document,
    calculate_shape_raster,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import TileCoord
from easypicture.tools.edit_session import PixelEditSession
from easypicture.ui.canvas_view import CanvasView


def _cropped():
    document = Document.create(8, 6)
    commands = DocumentCommandService()
    commands.attach_document(document)
    result = calculate_crop_document(
        CropDocumentRequest(DocumentSnapshot.capture(document), GeometryRect(2, 1, 4, 3)),
        CancellationToken(),
        lambda *_: None,
    )
    assert commands.apply_document_geometry(result, "Crop")
    commands.undo_stack.clear()
    return document, commands


def test_unselected_brush_can_edit_retained_pixels_outside_output(qtbot):
    document, commands = _cropped()
    canvas = CanvasView()
    qtbot.addWidget(canvas)
    canvas.set_document(document)
    session = PixelEditSession(document, document.active_layer_id, commands, canvas)
    stroke = BrushStrokeEngine(
        session,
        BrushSettings(size=1, antialias=False),
        (255, 0, 0, 255),
        changed=session.publish,
    )
    stroke.begin(0.5, 0.5)  # document (-1.5, -0.5), inside the retained local pixels
    assert session.commit("Paint outside output")
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[0, 0]) == (255, 0, 0, 255)
    assert commands.undo_stack.count() == 1
    commands.undo_stack.undo()
    assert not document.active_layer.pixels.has_tile(0, 0)


def test_document_selection_blocks_outside_brush_and_maps_to_local_pixels(qtbot):
    document, commands = _cropped()
    mask = np.zeros((256, 256), np.uint8)
    mask[0, 0] = 255
    document.replace_selection_tiles({TileCoord(0, 0): mask})
    commands.undo_stack.clear()
    canvas = CanvasView()
    qtbot.addWidget(canvas)
    canvas.set_document(document)
    session = PixelEditSession(document, document.active_layer_id, commands, canvas)
    stroke = BrushStrokeEngine(
        session,
        BrushSettings(size=1, antialias=False),
        (255, 0, 0, 255),
        changed=session.publish,
    )
    stroke.begin(0.5, 0.5)
    assert not session.changed_tiles
    stroke.extend(2.5, 1.5)
    assert session.commit("Paint selection")
    pixels = document.active_layer.pixels.read_tile(0, 0)
    assert pixels[1, 2, 3] == 255
    assert np.count_nonzero(pixels[..., 3]) == 1


def test_shape_uses_local_tile_coordinates_and_document_selection(qapp):
    document, _commands = _cropped()
    mask = np.zeros((256, 256), np.uint8)
    mask[0, 0] = 255
    document.replace_selection_tiles({TileCoord(0, 0): mask})
    request = ShapeRasterRequest(
        DocumentSnapshot.capture(document),
        document.active_layer_id,
        ShapeGeometry(ShapeKind.RECTANGLE, ((0.0, 0.0), (2.0, 2.0))),
        ShapeSettings(style=ShapePaintStyle.FILL, fill_color=(0, 255, 0, 255), antialias=False),
    )
    result = calculate_shape_raster(request, CancellationToken(), lambda *_: None)
    pixels = result.updates[TileCoord(0, 0)]
    assert tuple(pixels[1, 2]) == (0, 255, 0, 255)
    assert np.count_nonzero(pixels[..., 3]) == 1
