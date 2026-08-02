from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from easypicture.commands import DocumentCommandService
from easypicture.domain import (
    AffineTransform,
    Document,
    Point,
    RasterLayer,
    Size,
)
from easypicture.imaging import (
    RasterTransformRequest,
    TransformInterpolation,
    calculate_raster_transform,
    projective_from_quad,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot, load_project, save_project
from easypicture.pixels import TileCoord
from easypicture.render import CanvasRenderCoordinator


def _progress(_value: int, _maximum: int, _message: str) -> None:
    return


def _coordinator(document: Document) -> CanvasRenderCoordinator:
    coordinator = CanvasRenderCoordinator()
    coordinator.set_document(document)
    return coordinator


def _render_preview(
    renderer: CanvasRenderCoordinator,
    width: int,
    height: int,
) -> QImage:
    image = QImage(
        width,
        height,
        QImage.Format.Format_RGBA8888_Premultiplied,
    )
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.paint_software(painter, QRectF(0.0, 0.0, float(width), float(height)))
    painter.end()
    return image


def _document_with_pixels() -> Document:
    document = Document.create(8, 6)
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[1, 1] = (200, 10, 30, 255)
    document.active_layer.pixels.replace_tile(0, 0, tile)
    return document


def test_affine_transform_inverse_and_composition() -> None:
    matrix = (
        AffineTransform.translation(4.0, -2.0)
        .then(AffineTransform.rotation(30.0))
        .then(AffineTransform.shear(10.0, -5.0))
    )
    point = Point(12.5, -3.25)
    restored = matrix.inverse().map_point(matrix.map_point(point))
    assert restored.x == pytest.approx(point.x)
    assert restored.y == pytest.approx(point.y)


def test_projective_quad_maps_all_four_corners() -> None:
    source = (
        Point(0.0, 0.0),
        Point(20.0, 0.0),
        Point(20.0, 10.0),
        Point(0.0, 10.0),
    )
    destination = (
        Point(2.0, 1.0),
        Point(24.0, 3.0),
        Point(18.0, 14.0),
        Point(-1.0, 11.0),
    )
    matrix = projective_from_quad(source, destination)

    for original, expected in zip(source, destination, strict=True):
        mapped = matrix.map_point(original)
        assert mapped.x == pytest.approx(expected.x, abs=1e-5)
        assert mapped.y == pytest.approx(expected.y, abs=1e-5)
        restored = matrix.inverse().map_point(mapped)
        assert restored.x == pytest.approx(original.x, abs=1e-5)
        assert restored.y == pytest.approx(original.y, abs=1e-5)


def test_nearest_integer_translation_preserves_layer_bytes() -> None:
    document = _document_with_pixels()
    result = calculate_raster_transform(
        RasterTransformRequest(
            DocumentSnapshot.capture(document),
            document.active_layer_id,
            AffineTransform.translation(3.0, 2.0),
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        _progress,
    )
    assert result.layer_origin.x == 0
    assert result.layer_origin.y == 0
    assert result.layer_size == document.size
    target = result.layer_tiles[TileCoord(0, 0)]
    assert tuple(target[3, 4]) == (200, 10, 30, 255)
    assert tuple(target[1, 1]) == (0, 0, 0, 0)


def test_irregular_quad_is_resampled_with_projective_mapping() -> None:
    document = Document.create(16, 16)
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[4:8, 4:8] = (220, 40, 20, 255)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    source = (
        Point(0.0, 0.0),
        Point(16.0, 0.0),
        Point(16.0, 16.0),
        Point(0.0, 16.0),
    )
    destination = (
        Point(0.0, 0.0),
        Point(16.0, 0.0),
        Point(12.0, 16.0),
        Point(4.0, 16.0),
    )
    matrix = projective_from_quad(source, destination)
    result = calculate_raster_transform(
        RasterTransformRequest(
            DocumentSnapshot.capture(document),
            document.active_layer_id,
            matrix,
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        _progress,
    )
    target = result.layer_tiles[TileCoord(0, 0)]
    mapped = matrix.map_point(Point(6.0, 6.0))
    sample = target[
        max(0, round(mapped.y) - 1) : round(mapped.y) + 2,
        max(0, round(mapped.x) - 1) : round(mapped.x) + 2,
    ]
    assert np.any(sample[..., 3] == 255)
    assert np.any(sample[..., 0] == 220)


def test_transform_clips_to_canvas_and_preserves_hidden_rgb_inside_it() -> None:
    document = Document.create(10, 20)
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[2, 3] = (17, 29, 41, 0)
    document.active_layer.pixels.replace_tile(0, 0, tile)
    result = calculate_raster_transform(
        RasterTransformRequest(
            DocumentSnapshot.capture(document),
            document.active_layer_id,
            AffineTransform.translation(1.0, 1.0),
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        _progress,
    )
    assert result.layer_origin.x == 0
    assert result.layer_origin.y == 0
    assert result.layer_size == document.size
    assert tuple(result.layer_tiles[TileCoord(0, 0)][3, 4]) == (17, 29, 41, 0)


def test_selected_pixel_is_cut_moved_and_selection_follows() -> None:
    document = Document.create(8, 6)
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[1, 1] = (255, 0, 0, 255)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[1, 1] = 255
    document.selection.mask.replace_tile(0, 0, mask)
    result = calculate_raster_transform(
        RasterTransformRequest(
            DocumentSnapshot.capture(document),
            document.active_layer_id,
            AffineTransform.translation(2.0, 0.0),
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        _progress,
    )
    output = result.layer_tiles[TileCoord(0, 0)]
    output_mask = result.selection_tiles[TileCoord(0, 0)]
    assert tuple(output[1, 1]) == (0, 0, 0, 0)
    assert tuple(output[1, 3]) == (255, 0, 0, 255)
    assert output_mask[1, 1] == 0
    assert output_mask[1, 3] == 255


def test_feathered_selection_splits_source_alpha() -> None:
    document = Document.create(8, 6)
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[1, 1] = (50, 100, 150, 255)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[1, 1] = 128
    document.selection.mask.replace_tile(0, 0, mask)
    result = calculate_raster_transform(
        RasterTransformRequest(
            DocumentSnapshot.capture(document),
            document.active_layer_id,
            AffineTransform.translation(2.0, 0.0),
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        _progress,
    )
    output = result.layer_tiles[TileCoord(0, 0)]
    assert output[1, 1, 3] == 127
    assert output[1, 3, 3] == 128


def test_transform_preview_stays_at_layer_stack_position(qtbot) -> None:
    document = Document.create(32, 32)
    bottom = document.active_layer
    bottom_pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    bottom_pixels[1, 1] = (255, 0, 0, 255)
    bottom.pixels.replace_tile(0, 0, bottom_pixels)
    top = RasterLayer.create_blank("top", Size(32, 32))
    top_pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    top_pixels[1, 11] = (0, 0, 255, 255)
    top.pixels.replace_tile(0, 0, top_pixels)
    document.insert_layer(top, 1, activate=False)
    renderer = _coordinator(document)

    renderer.set_transform_preview(
        bottom.id,
        AffineTransform.translation(10.0, 0.0),
        TransformInterpolation.NEAREST,
    )
    preview = _render_preview(renderer, 32, 32)

    assert preview.pixelColor(1, 1).getRgb()[:3] != (255, 0, 0)
    assert preview.pixelColor(11, 1).blue() == 255


def test_transform_preview_replaces_old_frame_without_stale_composite(qtbot) -> None:
    document = Document.create(32, 32)
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[4, 4] = (255, 80, 10, 255)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    renderer = _coordinator(document)
    authoritative = _render_preview(renderer, 32, 32)
    assert authoritative.pixelColor(4, 4).alpha() == 255
    renderer.set_transform_preview(
        document.active_layer_id,
        AffineTransform.translation(4.0, 0.0),
        TransformInterpolation.NEAREST,
    )
    first = _render_preview(renderer, 32, 32)
    assert first.pixelColor(8, 4).alpha() == 255
    first_generation = renderer.generation

    renderer.set_transform_preview(
        document.active_layer_id,
        AffineTransform.translation(10.0, 0.0),
        TransformInterpolation.NEAREST,
    )
    assert renderer.generation == first_generation + 1
    second = _render_preview(renderer, 32, 32)
    assert second.pixelColor(8, 4).getRgb()[:3] != (255, 80, 10)
    assert second.pixelColor(14, 4).alpha() == 255


def test_transform_preview_rotation_uses_same_atomic_frame_as_outline(qtbot) -> None:
    document = Document.create(32, 32)
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[14:18, 4:9] = (80, 120, 220, 255)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    renderer = _coordinator(document)
    matrix = (
        AffineTransform.translation(-16.0, -16.0)
        .then(AffineTransform.rotation(90.0))
        .then(AffineTransform.translation(16.0, 16.0))
    )
    renderer.set_transform_preview(
        document.active_layer_id,
        matrix,
        TransformInterpolation.NEAREST,
    )

    preview = _render_preview(renderer, 32, 32)

    assert preview.pixelColor(16, 6).alpha() == 255
    assert preview.pixelColor(6, 16).getRgb()[:3] != (80, 120, 220)


def test_transform_preview_draws_projective_quad_directly(qtbot) -> None:
    document = Document.create(32, 32)
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[7:10, 7:10] = (210, 90, 30, 255)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    source = (
        Point(0.0, 0.0),
        Point(32.0, 0.0),
        Point(32.0, 32.0),
        Point(0.0, 32.0),
    )
    destination = (
        Point(0.0, 0.0),
        Point(32.0, 0.0),
        Point(24.0, 32.0),
        Point(8.0, 32.0),
    )
    matrix = projective_from_quad(source, destination)
    renderer = _coordinator(document)
    renderer.set_transform_preview(
        document.active_layer_id,
        matrix,
        TransformInterpolation.NEAREST,
    )

    preview = _render_preview(renderer, 32, 32)
    mapped = matrix.map_point(Point(8.0, 8.0))

    assert preview.pixelColor(round(mapped.x), round(mapped.y)).red() == 210
    assert preview.pixelColor(8, 8).getRgb()[:3] != (210, 90, 30)


def test_transform_preview_combines_with_existing_view_transform(qtbot) -> None:
    document = Document.create(32, 32)
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[8, 8] = (30, 200, 80, 255)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    renderer = _coordinator(document)
    renderer.set_transform_preview(
        document.active_layer_id,
        AffineTransform.translation(10.0, 0.0),
        TransformInterpolation.NEAREST,
    )
    target = QImage(64, 64, QImage.Format.Format_RGBA8888_Premultiplied)
    target.fill(Qt.GlobalColor.transparent)
    painter = QPainter(target)
    painter.scale(2.0, 2.0)

    renderer.paint_software(painter, QRectF(0.0, 0.0, 32.0, 32.0))
    painter.end()

    assert target.pixelColor(36, 16).green() == 200
    assert target.pixelColor(16, 16).getRgb()[:3] != (30, 200, 80)


def test_transform_preview_reuses_low_zoom_session_atlas(qtbot) -> None:
    document = Document.create(512, 256)
    first = np.zeros((256, 256, 4), dtype=np.uint8)
    second = np.zeros((256, 256, 4), dtype=np.uint8)
    first[40:80, 40:80] = (240, 60, 20, 255)
    second[40:80, 40:80] = (20, 90, 240, 255)
    document.active_layer.pixels.replace_tile(0, 0, first)
    document.active_layer.pixels.replace_tile(1, 0, second)
    renderer = _coordinator(document)
    renderer.set_transform_preview(
        document.active_layer_id,
        AffineTransform(),
        TransformInterpolation.LINEAR,
    )

    def paint_atlas() -> QImage:
        target = QImage(128, 64, QImage.Format.Format_RGBA8888_Premultiplied)
        target.fill(Qt.GlobalColor.transparent)
        painter = QPainter(target)
        painter.scale(0.25, 0.25)
        renderer.paint_software(painter, QRectF(0.0, 0.0, 512.0, 256.0))
        painter.end()
        return target

    first_frame = paint_atlas()
    assert first_frame.pixelColor(15, 15).red() > 200
    assert first_frame.pixelColor(79, 15).blue() > 200

    renderer.set_transform_preview(
        document.active_layer_id,
        AffineTransform.translation(4.0, 0.0),
        TransformInterpolation.LINEAR,
    )
    paint_atlas()


def test_transform_preview_keeps_latest_matrix_generation(qtbot) -> None:
    document = Document.create(64, 64)
    renderer = _coordinator(document)
    invalidations: list[object] = []
    renderer.frame_requested.connect(lambda: invalidations.append(None))

    for offset in range(1, 7):
        renderer.set_transform_preview(
            document.active_layer_id,
            AffineTransform.translation(float(offset), 0.0),
            TransformInterpolation.LINEAR,
        )

    assert len(invalidations) == 6
    frame = renderer.transform_preview
    assert frame is not None
    assert renderer.generation == 7
    assert frame.matrix.map_point(Point(0.0, 0.0)).x == pytest.approx(6.0)


def test_transform_preview_rotating_outside_and_back_invalidates_whole_view(
    qtbot,
) -> None:
    document = Document.create(64, 64)
    renderer = _coordinator(document)
    invalidations: list[object] = []
    renderer.frame_requested.connect(lambda: invalidations.append(None))
    rotated = (
        AffineTransform.translation(-32.0, -32.0)
        .then(AffineTransform.rotation(40.0))
        .then(AffineTransform.translation(32.0, 32.0))
    )

    renderer.set_transform_preview(
        document.active_layer_id,
        rotated,
        TransformInterpolation.LINEAR,
    )
    assert len(invalidations) == 1
    renderer.set_transform_preview(
        document.active_layer_id,
        AffineTransform(),
        TransformInterpolation.LINEAR,
    )
    assert len(invalidations) == 2

    assert invalidations == [None, None]


def test_transform_preview_keeps_unselected_base_and_moves_floating_pixels(
    qtbot,
) -> None:
    document = Document.create(32, 32)
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[5, 5] = (255, 0, 0, 255)
    pixels[5, 20] = (0, 0, 255, 255)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[5, 5] = 255
    document.selection.mask.replace_tile(0, 0, mask)
    renderer = _coordinator(document)
    renderer.set_transform_preview(
        document.active_layer_id,
        AffineTransform.translation(6.0, 0.0),
        TransformInterpolation.NEAREST,
    )

    preview = _render_preview(renderer, 32, 32)

    assert preview.pixelColor(5, 5).getRgb()[:3] != (255, 0, 0)
    assert preview.pixelColor(11, 5).red() == 255
    assert preview.pixelColor(20, 5).blue() == 255


def test_transform_preview_can_clear_after_preview_layer_is_deleted(qtbot) -> None:
    document = Document.create(32, 32)
    preview_layer = document.active_layer
    document.insert_layer(RasterLayer.create_blank("fallback", document.size), 1)
    renderer = _coordinator(document)
    renderer.set_transform_preview(
        preview_layer.id,
        AffineTransform.translation(5.0, 0.0),
        TransformInterpolation.NEAREST,
    )

    document.remove_layer(preview_layer.id)
    renderer.clear_transform_preview()

    assert _render_preview(renderer, 32, 32).isNull() is False


def test_transform_command_is_one_atomic_undo_step(qtbot) -> None:
    document = _document_with_pixels()
    service = DocumentCommandService()
    service.attach_document(document)
    result = calculate_raster_transform(
        RasterTransformRequest(
            DocumentSnapshot.capture(document),
            document.active_layer_id,
            AffineTransform.translation(4.0, 2.0),
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        _progress,
    )
    assert service.apply_raster_transform(result)
    assert service.undo_stack.count() == 1
    assert document.active_layer.origin.x == 0
    assert document.active_layer.origin.y == 0
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[3, 5]) == (
        200,
        10,
        30,
        255,
    )

    service.undo_stack.undo()
    assert document.active_layer.origin.x == 0
    assert document.active_layer.origin.y == 0
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[1, 1]) == (
        200,
        10,
        30,
        255,
    )

    service.undo_stack.redo()
    assert document.active_layer.origin.x == 0
    assert document.active_layer.origin.y == 0
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[3, 5]) == (
        200,
        10,
        30,
        255,
    )


def test_transform_rejects_stale_or_over_budget_result_without_mutation(qtbot) -> None:
    document = _document_with_pixels()
    snapshot = DocumentSnapshot.capture(document)
    result = calculate_raster_transform(
        RasterTransformRequest(
            snapshot,
            document.active_layer_id,
            AffineTransform.translation(4.0, 2.0),
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        _progress,
    )
    stale_service = DocumentCommandService()
    stale_service.attach_document(document)
    document.record_change()
    assert not stale_service.apply_raster_transform(result)
    assert document.active_layer.origin.x == 0

    fresh_result = calculate_raster_transform(
        RasterTransformRequest(
            DocumentSnapshot.capture(document),
            document.active_layer_id,
            AffineTransform.scale(2.0, 2.0),
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        _progress,
    )
    reports: list[tuple[int, int]] = []
    limited = DocumentCommandService(
        undo_budget_bytes=1,
        report_command_too_large=lambda required, budget: reports.append(
            (required, budget)
        ),
    )
    limited.attach_document(document)
    assert not limited.apply_raster_transform(fresh_result)
    assert reports and reports[0][1] == 1
    assert document.active_layer.origin.x == 0


def test_transformed_geometry_and_selection_round_trip_limg(
    qtbot,
    tmp_path: Path,
) -> None:
    document = _document_with_pixels()
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[1, 1] = 255
    document.selection.mask.replace_tile(0, 0, mask)
    service = DocumentCommandService()
    service.attach_document(document)
    result = calculate_raster_transform(
        RasterTransformRequest(
            DocumentSnapshot.capture(document),
            document.active_layer_id,
            AffineTransform.translation(2.0, 1.0),
            TransformInterpolation.NEAREST,
        ),
        CancellationToken(),
        _progress,
    )
    assert service.apply_raster_transform(result)
    path = tmp_path / "transformed.limg"
    save_project(DocumentSnapshot.capture(document), path, CancellationToken(), _progress)
    restored = load_project(path, CancellationToken(), _progress).snapshot.to_document()

    assert restored.active_layer.origin.x == 0
    assert restored.active_layer.origin.y == 0
    assert tuple(restored.active_layer.pixels.read_tile(0, 0)[2, 3]) == (
        200,
        10,
        30,
        255,
    )
    assert restored.selection.mask.read_tile(0, 0)[2, 3] == 255
