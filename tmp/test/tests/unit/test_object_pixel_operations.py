import numpy as np
import pytest

from easypicture.commands import DocumentCommandService
from easypicture.domain import AffineTransform, Document, Point, Size
from easypicture.imaging import (
    ColorAdjustmentRequest,
    ImageScaleRequest,
    InvertAdjustment,
    LayerScaleRequest,
    TransformInterpolation,
    calculate_image_scale,
    calculate_layer_scale,
)
from easypicture.imaging.color_preview import calculate_color_preview
from easypicture.imaging.geometry_ops import (
    ObjectCanvasResizeRequest,
    calculate_object_canvas_resize,
)
from easypicture.imaging.sampling import sample_document_pixel
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import TileCoord


def progress(*_):
    pass


def test_output_scale_resolution_and_bake_preserve_pixels_and_undo(qapp):
    document = Document.create(8, 8)
    source = document.active_layer
    source.pixels.writable_tile(0, 0)[1, 1] = (255, 20, 10, 255)
    original = source.pixels.read_tile(0, 0).copy()
    commands = DocumentCommandService()
    commands.attach_document(document)
    result = calculate_image_scale(
        ImageScaleRequest(DocumentSnapshot.capture(document), Size(16, 16)),
        CancellationToken(),
        progress,
    )
    assert commands.apply_document_geometry(result, "Scale output")
    assert source.size == Size(8, 8)
    np.testing.assert_array_equal(source.pixels.read_tile(0, 0), original)
    assert source.world_transform == AffineTransform.scale(2, 2)
    result = calculate_layer_scale(
        LayerScaleRequest(
            DocumentSnapshot.capture(document),
            source.id,
            Size(16, 16),
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        progress,
    )
    assert commands.apply_document_geometry(result, "Resolution")
    assert source.size == Size(16, 16)
    assert source.world_transform.is_identity
    assert sample_document_pixel(document, 2, 2, merged=True) == (255, 20, 10, 255)
    commands.undo_stack.undo()
    assert source.size == Size(8, 8)
    np.testing.assert_array_equal(source.pixels.read_tile(0, 0), original)
    assert commands.transform_objects((source.id,), AffineTransform.translation(30, 40))
    assert commands.rasterize_object(source.id)
    baked = document.layer_by_id(source.id)
    assert baked.transform.is_identity
    assert baked.origin.x == 30 and baked.origin.y == 40
    assert tuple(baked.pixels.read_tile(0, 0)[2, 2]) == (255, 20, 10, 255)
    commands.undo_stack.undo()
    restored = document.layer_by_id(source.id)
    np.testing.assert_array_equal(restored.pixels.read_tile(0, 0), original)
    assert restored.world_transform.map_point(Point(1, 1)) == Point(32, 42)


def test_large_color_preview_is_bounded_and_marked_inexact():
    document = Document.create(4096, 2048)
    layer = document.active_layer
    layer.pixels.writable_tile(0, 0)[:] = (10, 20, 30, 255)
    result = calculate_color_preview(
        ColorAdjustmentRequest(DocumentSnapshot.capture(document), layer.id, InvertAdjustment()),
        CancellationToken(),
        progress,
    )
    assert result.preview_size == Size(1024, 512)
    assert tuple(result.updates[TileCoord(0, 0)][0, 0]) == (245, 235, 225, 255)
    assert tuple(layer.pixels.read_tile(0, 0)[0, 0]) == (10, 20, 30, 255)
    assert all(not tile.flags.writeable for tile in result.updates.values())


def test_local_canvas_resize_on_detached_frame_preserves_world_position_and_undo(qapp):
    from easypicture.imaging import CanvasAnchor

    document = Document.create_sprite_frame(Size(8, 8))
    source = document.active_layer
    source.transform = AffineTransform.rotation(30).then(AffineTransform.translation(9, 13))
    source.pixels.writable_tile(0, 0)[2, 2] = (20, 40, 60, 255)
    position = source.world_transform.map_point(Point(2, 2))
    commands = DocumentCommandService()
    commands.attach_document(document)
    result = calculate_object_canvas_resize(
        ObjectCanvasResizeRequest(
            DocumentSnapshot.capture(document), source.id, Size(12, 12), CanvasAnchor.CENTER
        ),
        CancellationToken(),
        progress,
    )
    assert commands.apply_document_geometry(result, "Local canvas")
    assert document.size == Size(8, 8)
    assert source.size == Size(12, 12)
    assert source.world_transform.map_point(Point(4, 4)).x == pytest.approx(position.x)
    assert source.world_transform.map_point(Point(4, 4)).y == pytest.approx(position.y)
    assert tuple(source.pixels.read_tile(0, 0)[4, 4]) == (20, 40, 60, 255)
    with pytest.raises(ValueError, match="no sprite workspace"):
        _ = document.sprite_workspace
    commands.undo_stack.undo()
    assert source.size == Size(8, 8)
    assert tuple(source.pixels.read_tile(0, 0)[2, 2]) == (20, 40, 60, 255)


def test_outside_canvas_pixels_are_dimmed_only_in_edit_view(qapp):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter

    from easypicture.imaging.composite import compose_document_tile
    from easypicture.render import CanvasRenderCoordinator

    document = Document.create(8, 8)
    source = document.active_layer
    source.pixels.writable_tile(0, 0)[:8, :8] = (255, 0, 0, 255)
    source.transform = AffineTransform.translation(-4, 0)
    renderer = CanvasRenderCoordinator()
    renderer.set_document(document)
    image = QImage(24, 24, QImage.Format.Format_RGBA8888)
    image.fill(0)
    painter = QPainter(image)
    painter.translate(8, 8)
    renderer.paint_software(painter, QRectF(-8, -8, 24, 24))
    painter.end()
    # The editor has an opaque #363636 background. Outside content uses 25% opacity.
    outside = image.pixelColor(6, 10)
    assert outside.red() == pytest.approx(104, abs=1)
    assert outside.green() == pytest.approx(41, abs=1)
    assert outside.blue() == pytest.approx(41, abs=1)
    assert image.pixelColor(10, 10).getRgb() == (255, 0, 0, 255)
    output = compose_document_tile(DocumentSnapshot.capture(document), TileCoord(0, 0))
    assert tuple(output[2, 2]) == (255, 0, 0, 255)
    assert not output[:, 8:].any()
    assert not output[8:].any()
    renderer.dispose()
