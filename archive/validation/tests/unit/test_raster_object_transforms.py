import numpy as np
import pytest

from easypicture.commands import DocumentCommandService
from easypicture.domain import AffineTransform, Document, Point, Size
from easypicture.domain.coordinates import RasterMapping
from easypicture.imaging import BrushSettings, BrushStrokeEngine
from easypicture.imaging.composite import compose_document_tile
from easypicture.imaging.fill import FillRequest, calculate_fill
from easypicture.imaging.region import RegionQuery
from easypicture.imaging.sampling import sample_document_pixel
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot
from easypicture.persistence.project_io import load_project, save_project
from easypicture.pixels import TileCoord
from easypicture.tools.edit_session import PixelEditSession
from easypicture.ui.canvas_view import CanvasView

MATRICES = [
    AffineTransform.translation(5, 7),
    AffineTransform.rotation(90).then(AffineTransform.translation(24, 4)),
    AffineTransform.scale(2, 3).then(AffineTransform.translation(2, 1)),
    AffineTransform.scale(-1, 1).then(AffineTransform.translation(24, 2)),
]


@pytest.mark.parametrize("matrix", MATRICES)
def test_transform_preserves_source_and_brush_selection_maps_back(qtbot, matrix):
    document = Document.create(64, 64)
    layer = document.active_layer
    layer.size = Size(8, 8)
    layer.pixels.writable_tile(0, 0)[1, 1] = (10, 20, 30, 255)
    before = layer.pixels.read_tile(0, 0).copy()
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.transform_objects((layer.id,), matrix)
    np.testing.assert_array_equal(layer.pixels.read_tile(0, 0), before)
    mapping = RasterMapping(layer.world_transform, layer.size)
    point = mapping.to_document(Point(3.5, 2.5))
    mask = np.zeros((256, 256), np.uint8)
    mask[int(point.y), int(point.x)] = 255
    document.replace_selection_tiles({TileCoord(0, 0): mask})
    canvas = CanvasView()
    qtbot.addWidget(canvas)
    canvas.set_document(document)
    session = PixelEditSession(document, layer.id, commands, canvas)
    local = session.mapping.to_local(point)
    stroke = BrushStrokeEngine(
        session,
        BrushSettings(size=1, antialias=False),
        (255, 0, 0, 255),
        changed=session.publish,
    )
    stroke.begin(local.x, local.y)
    assert session.commit("Mapped stroke")
    assert tuple(layer.pixels.read_tile(0, 0)[2, 3]) == (255, 0, 0, 255)
    assert commands.undo_stack.count() == 2
    assert sample_document_pixel(document, int(point.x), int(point.y), merged=False) == (
        255,
        0,
        0,
        255,
    )
    composite = compose_document_tile(DocumentSnapshot.capture(document), TileCoord(0, 0))
    assert tuple(composite[int(point.y), int(point.x)]) == (255, 0, 0, 255)
    commands.undo_stack.undo()
    np.testing.assert_array_equal(layer.pixels.read_tile(0, 0), before)
    commands.undo_stack.undo()
    assert layer.transform.is_identity


def test_batch_transform_rejects_locked_and_singular_targets_atomically(qapp):
    document = Document.create(8, 8)
    commands = DocumentCommandService()
    commands.attach_document(document)
    commands.create_layer()
    first, second = document.layers
    second.locked = True
    count = commands.undo_stack.count()
    assert not commands.transform_objects(
        (first.id, second.id),
        AffineTransform.translation(10, 20),
    )
    assert first.transform.is_identity
    second.locked = False
    assert not commands.transform_objects((first.id,), AffineTransform.scale(0, 1))
    assert commands.undo_stack.count() == count


def test_transformed_fill_and_persistence_use_local_pixels(tmp_path, qapp):
    document = Document.create(32, 32)
    layer = document.active_layer
    layer.size = Size(4, 3)
    commands = DocumentCommandService()
    commands.attach_document(document)
    matrix = AffineTransform.rotation(90).then(AffineTransform.translation(20, 4))
    assert commands.transform_objects((layer.id,), matrix)
    point = RasterMapping(layer.world_transform, layer.size).to_document(Point(1.5, 1.5))
    request = FillRequest(
        DocumentSnapshot.capture(document),
        layer.id,
        RegionQuery((int(point.x), int(point.y))),
        (40, 80, 120, 255),
    )
    result = calculate_fill(request, CancellationToken(), lambda *_: None)
    assert commands.apply_pixel_tiles(layer.id, result.updates, "Fill")
    pixels = layer.pixels.read_tile(0, 0)
    assert np.count_nonzero(pixels[..., 3]) == 12
    path = tmp_path / "transformed.limg"
    save_project(DocumentSnapshot.capture(document), path, CancellationToken(), lambda *_: None)
    restored = load_project(path, CancellationToken(), lambda *_: None).snapshot.to_document()
    assert restored.active_layer.world_transform == layer.world_transform
    np.testing.assert_array_equal(restored.active_layer.pixels.read_tile(0, 0), pixels)
