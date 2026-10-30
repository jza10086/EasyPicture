from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from easypicture.commands import DocumentCommandService
from easypicture.domain import AffineTransform, Document, FloatRect, PixelPoint, Size
from easypicture.imaging import (
    CanvasAnchor,
    CanvasResizeRequest,
    CropDocumentRequest,
    GeometryRect,
    ImageScaleRequest,
    LayerScaleRequest,
    TransformInterpolation,
    TrimTransparentRequest,
    calculate_canvas_resize,
    calculate_crop_document,
    calculate_image_scale,
    calculate_layer_scale,
    calculate_trim_transparent,
    canvas_anchor_offset,
)
from easypicture.imaging.composite import compose_document_tile
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot, load_project, save_project
from easypicture.pixels import TileCoord


def _progress(_value: int, _maximum: int, _message: str) -> None:
    return


def _document_with_pixels() -> Document:
    document = Document.create(8, 6)
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[1, 1] = (20, 40, 60, 255)
    pixels[2, 3] = (200, 100, 50, 0)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[1, 1] = 255
    document.selection.mask.replace_tile(0, 0, mask)
    return document


def test_canvas_anchor_offsets_cover_all_nine_positions() -> None:
    old = Size(10, 8)
    new = Size(16, 12)
    assert canvas_anchor_offset(old, new, CanvasAnchor.TOP_LEFT) == PixelPoint(0, 0)
    assert canvas_anchor_offset(old, new, CanvasAnchor.TOP) == PixelPoint(3, 0)
    assert canvas_anchor_offset(old, new, CanvasAnchor.TOP_RIGHT) == PixelPoint(6, 0)
    assert canvas_anchor_offset(old, new, CanvasAnchor.LEFT) == PixelPoint(0, 2)
    assert canvas_anchor_offset(old, new, CanvasAnchor.CENTER) == PixelPoint(3, 2)
    assert canvas_anchor_offset(old, new, CanvasAnchor.RIGHT) == PixelPoint(6, 2)
    assert canvas_anchor_offset(old, new, CanvasAnchor.BOTTOM_LEFT) == PixelPoint(0, 4)
    assert canvas_anchor_offset(old, new, CanvasAnchor.BOTTOM) == PixelPoint(3, 4)
    assert canvas_anchor_offset(old, new, CanvasAnchor.BOTTOM_RIGHT) == PixelPoint(6, 4)


def test_crop_retains_layer_bytes_and_translates_selection() -> None:
    document = _document_with_pixels()
    before = document.active_layer.pixels.read_tile(0, 0).copy()
    result = calculate_crop_document(
        CropDocumentRequest(
            DocumentSnapshot.capture(document),
            GeometryRect(-2, -1, 12, 9),
        ),
        CancellationToken(),
        _progress,
    )

    assert result.document_size == Size(12, 9)
    assert result.layers[0].origin == PixelPoint(2, 1)
    assert result.layers[0].size == Size(8, 6)
    assert result.layers[0].tiles is None
    assert result.selection_tiles[TileCoord(0, 0)][2, 3] == 255
    assert np.array_equal(document.active_layer.pixels.read_tile(0, 0), before)


def test_canvas_resize_is_one_atomic_undo_step_and_preserves_off_canvas_pixels(
    qtbot,
) -> None:
    document = _document_with_pixels()
    service = DocumentCommandService()
    service.attach_document(document)
    result = calculate_canvas_resize(
        CanvasResizeRequest(
            DocumentSnapshot.capture(document),
            Size(4, 4),
            CanvasAnchor.BOTTOM_RIGHT,
        ),
        CancellationToken(),
        _progress,
    )

    assert service.apply_document_geometry(result, "调整画布大小")
    assert document.size == Size(4, 4)
    assert document.active_layer.origin == PixelPoint(-4, -2)
    assert document.active_layer.size == Size(8, 6)
    assert document.active_layer.pixels.has_tile(0, 0)
    assert service.undo_stack.count() == 1

    service.undo_stack.undo()
    assert document.size == Size(8, 6)
    assert document.active_layer.origin == PixelPoint()
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[2, 3]) == (
        200,
        100,
        50,
        0,
    )
    service.undo_stack.redo()
    assert document.size == Size(4, 4)
    assert document.active_layer.origin == PixelPoint(-4, -2)
    assert document.active_layer.pixels.has_tile(0, 0)


def test_expand_after_shrink_reveals_preserved_pixels(
    qtbot,
) -> None:
    document = Document.create(8, 6)
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[5, 7] = (90, 80, 70, 255)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    service = DocumentCommandService()
    service.attach_document(document)

    shrink = calculate_canvas_resize(
        CanvasResizeRequest(
            DocumentSnapshot.capture(document),
            Size(4, 4),
            CanvasAnchor.TOP_LEFT,
        ),
        CancellationToken(),
        _progress,
    )
    assert service.apply_document_geometry(shrink, "缩小画布")
    np.testing.assert_array_equal(document.active_layer.pixels.read_tile(0, 0), pixels)
    clipped_output = compose_document_tile(DocumentSnapshot.capture(document), TileCoord(0, 0))
    assert not np.any(clipped_output)
    expand = calculate_canvas_resize(
        CanvasResizeRequest(
            DocumentSnapshot.capture(document),
            Size(8, 6),
            CanvasAnchor.TOP_LEFT,
        ),
        CancellationToken(),
        _progress,
    )
    assert service.apply_document_geometry(expand, "扩大画布")
    assert document.active_layer.pixels.has_tile(0, 0)
    expanded_output = compose_document_tile(DocumentSnapshot.capture(document), TileCoord(0, 0))
    np.testing.assert_array_equal(expanded_output, pixels)

    service.undo_stack.undo()
    service.undo_stack.undo()
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[5, 7]) == (
        90,
        80,
        70,
        255,
    )


def test_image_scale_resizes_layers_origins_and_selection() -> None:
    document = _document_with_pixels()
    document.active_layer.origin = PixelPoint(2, 1)
    result = calculate_image_scale(
        ImageScaleRequest(
            DocumentSnapshot.capture(document),
            Size(16, 12),
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        _progress,
    )

    layer = result.layers[0]
    assert result.document_size == Size(16, 12)
    assert layer.size == Size(8, 6)
    assert layer.origin == PixelPoint(2, 1)
    assert layer.tiles is None
    assert result.object_transforms[layer.layer_id] == AffineTransform(a=2, d=2, tx=2, ty=1)
    assert result.selection_tiles[TileCoord(0, 0)][2, 2] == 255


def test_layer_scale_preserves_selection_and_rejects_locked_layer() -> None:
    document = _document_with_pixels()
    snapshot = DocumentSnapshot.capture(document)
    result = calculate_layer_scale(
        LayerScaleRequest(
            snapshot,
            document.active_layer_id,
            Size(4, 3),
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        _progress,
    )
    assert result.document_size == document.size
    assert result.layers[0].size == Size(4, 3)
    assert result.object_transforms[document.active_layer_id] == AffineTransform.scale(2, 2)
    assert result.layers[0].origin == PixelPoint()
    assert result.selection_tiles[TileCoord(0, 0)][1, 1] == 255

    document.active_layer.locked = True
    with pytest.raises(ValueError, match="locked"):
        calculate_layer_scale(
            LayerScaleRequest(
                DocumentSnapshot.capture(document),
                document.active_layer_id,
                Size(4, 3),
            ),
            CancellationToken(),
            _progress,
        )


def test_trim_transparent_uses_alpha_union_and_ignores_hidden_rgb() -> None:
    document = _document_with_pixels()
    result = calculate_trim_transparent(
        TrimTransparentRequest(DocumentSnapshot.capture(document)),
        CancellationToken(),
        _progress,
    )

    assert result.document_size == Size(1, 1)
    layer = result.layers[0]
    assert layer.size == Size(8, 6)
    assert layer.origin == PixelPoint(-1, -1)
    assert layer.tiles is None
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[1, 1]) == (20, 40, 60, 255)


def test_geometry_service_rejects_stale_result(qtbot) -> None:
    document = _document_with_pixels()
    result = calculate_canvas_resize(
        CanvasResizeRequest(
            DocumentSnapshot.capture(document),
            Size(10, 10),
        ),
        CancellationToken(),
        _progress,
    )
    service = DocumentCommandService()
    service.attach_document(document)
    document.record_change()
    assert not service.apply_document_geometry(result, "调整画布大小")
    assert document.size == Size(8, 6)


def test_geometry_result_round_trips_limg(
    qtbot,
    tmp_path: Path,
) -> None:
    document = _document_with_pixels()
    service = DocumentCommandService()
    service.attach_document(document)
    result = calculate_crop_document(
        CropDocumentRequest(
            DocumentSnapshot.capture(document),
            GeometryRect(2, 1, 5, 4),
        ),
        CancellationToken(),
        _progress,
    )
    assert service.apply_document_geometry(result, "裁切画布")

    path = tmp_path / "geometry.limg"
    save_project(
        DocumentSnapshot.capture(document),
        path,
        CancellationToken(),
        _progress,
    )
    restored = load_project(path, CancellationToken(), _progress).snapshot.to_document()
    assert restored.size == Size(5, 4)
    assert restored.active_layer.size == Size(8, 6)
    assert restored.active_layer.origin == PixelPoint(-2, -1)
    assert tuple(restored.active_layer.pixels.read_tile(0, 0)[2, 3]) == (
        200,
        100,
        50,
        0,
    )


def test_crop_translates_vector_and_raster_together_in_one_undo(qapp):
    document = _document_with_pixels()
    commands = DocumentCommandService()
    commands.attach_document(document)
    commands.create_vector_group()
    group = document.vector_groups[0]
    commands.create_text_object(group.id, frame=FloatRect(1, 1, 4, 3))
    vector = group.children[0]
    before = document.active_layer.pixels.read_tile(0, 0).copy()
    commands.undo_stack.clear()
    result = calculate_crop_document(
        CropDocumentRequest(DocumentSnapshot.capture(document), GeometryRect(1, 1, 4, 3)),
        CancellationToken(),
        _progress,
    )
    assert commands.apply_document_geometry(result, "Crop")
    assert vector.transform == AffineTransform.translation(-1, -1)
    assert document.active_layer.origin == PixelPoint(-1, -1)
    np.testing.assert_array_equal(document.active_layer.pixels.read_tile(0, 0), before)
    assert commands.undo_stack.count() == 1
    commands.undo_stack.undo()
    assert vector.transform == AffineTransform()
    assert document.active_layer.origin == PixelPoint()
    assert document.size == Size(8, 6)
