from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, PixelPoint, Size
from easypicture.imaging import (
    CanvasAnchor,
    CanvasResizeRequest,
    CropDocumentRequest,
    GeometryRect,
    ImageScaleRequest,
    LayerBoundsRequest,
    LayerScaleRequest,
    TransformInterpolation,
    TrimTransparentRequest,
    calculate_canvas_resize,
    calculate_crop_document,
    calculate_image_scale,
    calculate_layer_bounds,
    calculate_layer_scale,
    calculate_trim_transparent,
    canvas_anchor_offset,
)
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
    assert result.layers[0].origin == PixelPoint()
    assert result.layers[0].size == Size(12, 9)
    assert tuple(result.layers[0].tiles[TileCoord(0, 0)][2, 3]) == (
        20,
        40,
        60,
        255,
    )
    assert result.selection_tiles[TileCoord(0, 0)][2, 3] == 255
    assert np.array_equal(document.active_layer.pixels.read_tile(0, 0), before)


def test_canvas_resize_is_one_atomic_undo_step_and_discards_off_canvas_pixels(
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
    assert document.active_layer.origin == PixelPoint()
    assert document.active_layer.size == Size(4, 4)
    assert not document.active_layer.pixels.has_tile(0, 0)
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
    assert document.active_layer.origin == PixelPoint()
    assert not document.active_layer.pixels.has_tile(0, 0)


def test_expand_after_shrink_does_not_restore_clipped_pixels_but_undo_does(
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
    assert not document.active_layer.pixels.has_tile(0, 0)

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
    assert layer.size == Size(16, 12)
    assert layer.origin == PixelPoint()
    assert tuple(layer.tiles[TileCoord(0, 0)][4, 6]) == (20, 40, 60, 255)
    assert tuple(layer.tiles[TileCoord(0, 0)][6, 10]) == (200, 100, 50, 0)
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
    assert result.layers[0].size == document.size
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


def test_legacy_layer_bounds_entry_normalizes_to_canvas() -> None:
    document = _document_with_pixels()
    document.active_layer.origin = PixelPoint(-1, 1)
    snapshot = DocumentSnapshot.capture(document)

    fitted = calculate_layer_bounds(
        LayerBoundsRequest(snapshot, document.active_layer_id, crop_to_content=False),
        CancellationToken(),
        _progress,
    )
    fitted_layer = fitted.layers[0]
    assert fitted_layer.size == document.size
    assert fitted_layer.origin == PixelPoint()
    assert tuple(fitted_layer.tiles[TileCoord(0, 0)][2, 0]) == (20, 40, 60, 255)

    cropped = calculate_layer_bounds(
        LayerBoundsRequest(snapshot, document.active_layer_id, crop_to_content=True),
        CancellationToken(),
        _progress,
    )
    cropped_layer = cropped.layers[0]
    assert cropped_layer.size == document.size
    assert cropped_layer.origin == PixelPoint()
    assert tuple(cropped_layer.tiles[TileCoord(0, 0)][3, 2]) == (
        200,
        100,
        50,
        0,
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
    assert layer.size == Size(1, 1)
    assert layer.origin == PixelPoint()
    assert tuple(layer.tiles[TileCoord(0, 0)][0, 0]) == (20, 40, 60, 255)


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
    assert restored.active_layer.size == Size(5, 4)
    assert restored.active_layer.origin == PixelPoint()
    assert tuple(restored.active_layer.pixels.read_tile(0, 0)[1, 1]) == (
        200,
        100,
        50,
        0,
    )
