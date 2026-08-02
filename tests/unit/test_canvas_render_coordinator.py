from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from easypicture.commands import DocumentCommandService
from easypicture.domain import AffineTransform, Document, Size
from easypicture.imaging import TransformInterpolation
from easypicture.pixels import TileCoord
from easypicture.render import CanvasRenderCoordinator, RenderBackendKind


def _solid(color: tuple[int, int, int, int]) -> np.ndarray:
    tile = np.empty((256, 256, 4), dtype=np.uint8)
    tile[:] = color
    return tile


def _pixel(image: QImage, x: int, y: int) -> tuple[int, int, int, int]:
    straight = image.convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = np.frombuffer(
        straight.bits(),
        dtype=np.uint8,
        count=straight.sizeInBytes(),
    ).reshape(straight.height(), straight.width(), 4)
    return tuple(int(channel) for channel in pixels[y, x])  # type: ignore[return-value]


def _paint_frame(
    coordinator: CanvasRenderCoordinator,
    width: int,
    height: int,
) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGBA8888_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    coordinator.paint_software(
        painter,
        QRectF(0.0, 0.0, float(width), float(height)),
    )
    painter.end()
    return image


def test_coordinator_uses_generation_without_composite_or_stale_cache(qapp) -> None:
    _ = qapp
    document = Document.create(256, 256)
    coordinator = CanvasRenderCoordinator(
        requested_backend=RenderBackendKind.SOFTWARE
    )
    generations: list[int] = []
    coordinator.frame_requested.connect(lambda: generations.append(coordinator.generation))

    coordinator.set_document(document)
    coordinator.request_frame()
    coordinator.request_frame()

    assert generations == [1, 2, 3]
    assert not hasattr(coordinator, "composite_cache")
    assert not hasattr(coordinator, "stale_tiles")
    assert not hasattr(coordinator, "pending_tiles")


def test_pixel_preview_only_replaces_requested_source_tile(qapp) -> None:
    _ = qapp
    document = Document.create(512, 256)
    layer = document.active_layer
    layer.pixels.replace_tile(0, 0, _solid((0, 0, 255, 255)))
    layer.pixels.replace_tile(1, 0, _solid((0, 255, 0, 255)))
    coordinator = CanvasRenderCoordinator()
    coordinator.set_document(document)
    before_second = coordinator.display_tile_signature(
        layer,
        TileCoord(1, 0),
        "source",
    )

    coordinator.set_pixel_preview(
        layer.id,
        {TileCoord(0, 0): _solid((255, 0, 0, 255))},
    )

    assert _pixel(_paint_frame(coordinator, 512, 256), 10, 10) == (
        255,
        0,
        0,
        255,
    )
    assert coordinator.display_tile_signature(
        layer,
        TileCoord(1, 0),
        "source",
    ) == before_second


def test_preview_signature_is_monotonic_across_clear_and_same_frame_replace(
    qapp,
) -> None:
    _ = qapp
    document = Document.create(256, 256)
    layer = document.active_layer
    coordinator = CanvasRenderCoordinator()
    coordinator.set_document(document)
    coord = TileCoord(0, 0)
    coordinator.set_pixel_preview(layer.id, {coord: _solid((255, 0, 0, 255))})
    first = coordinator.display_tile_signature(layer, coord, "source")

    coordinator.clear_pixel_preview()
    coordinator.set_pixel_preview(layer.id, {coord: _solid((0, 255, 0, 255))})
    second = coordinator.display_tile_signature(layer, coord, "source")

    assert second != first
    assert second[1] > first[1]
    assert _pixel(_paint_frame(coordinator, 256, 256), 10, 10) == (0, 255, 0, 255)


def test_complete_preview_replacement_is_atomic_and_removes_old_coords(qapp) -> None:
    _ = qapp
    document = Document.create(512, 256)
    layer = document.active_layer
    layer.pixels.replace_tile(0, 0, _solid((10, 20, 30, 255)))
    layer.pixels.replace_tile(1, 0, _solid((40, 50, 60, 255)))
    coordinator = CanvasRenderCoordinator()
    coordinator.set_document(document)
    frames: list[int] = []
    coordinator.frame_requested.connect(lambda: frames.append(coordinator.generation))

    coordinator.replace_pixel_preview(
        layer.id,
        {
            TileCoord(0, 0): _solid((255, 0, 0, 255)),
            TileCoord(1, 0): _solid((0, 255, 0, 255)),
        },
    )
    first_generation = coordinator.generation
    coordinator.replace_pixel_preview(
        layer.id,
        {TileCoord(0, 0): None},
    )

    assert coordinator.generation == first_generation + 1
    assert len(frames) == 2
    assert coordinator.preview_tile_count == 1
    frame = _paint_frame(coordinator, 512, 256)
    cleared = _pixel(frame, 10, 10)
    assert cleared[0] == cleared[1] == cleared[2]
    assert cleared[3] == 255
    assert _pixel(frame, 266, 10) == (
        40,
        50,
        60,
        255,
    )


def test_transform_return_draws_complete_latest_frame(qapp) -> None:
    _ = qapp
    document = Document.create(64, 64)
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[8:24, 8:24] = (80, 120, 220, 255)
    document.active_layer.pixels.replace_tile(0, 0, tile)
    coordinator = CanvasRenderCoordinator()
    coordinator.set_document(document)

    coordinator.set_transform_preview(
        document.active_layer_id,
        AffineTransform.translation(80.0, 0.0),
        TransformInterpolation.NEAREST,
    )
    moved = _paint_frame(coordinator, 160, 80)
    assert _pixel(moved, 12, 12) != (80, 120, 220, 255)
    assert _pixel(moved, 92, 12) == (80, 120, 220, 255)

    coordinator.set_transform_preview(
        document.active_layer_id,
        AffineTransform(),
        TransformInterpolation.NEAREST,
    )
    restored = _paint_frame(coordinator, 160, 80)
    assert _pixel(restored, 12, 12) == (80, 120, 220, 255)
    assert _pixel(restored, 92, 12) != (80, 120, 220, 255)


def test_deleting_transform_layer_clears_preview_in_same_generation(qapp) -> None:
    _ = qapp
    document = Document.create(64, 64)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_layer()
    transformed_layer = document.active_layer
    coordinator = CanvasRenderCoordinator()
    coordinator.set_document(document)
    coordinator.set_transform_preview(
        transformed_layer.id,
        AffineTransform.translation(10.0, 10.0),
        TransformInterpolation.LINEAR,
    )

    assert commands.delete_layer(transformed_layer.id)

    assert coordinator.transform_preview is None
    assert coordinator.document is document


def test_geometry_preview_repaints_scaled_latest_frame_and_clears(qapp) -> None:
    _ = qapp
    document = Document.create(32, 24)
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[2:6, 3:7] = (230, 70, 20, 255)
    document.active_layer.pixels.replace_tile(0, 0, tile)
    coordinator = CanvasRenderCoordinator()
    coordinator.set_document(document)

    coordinator.set_geometry_preview(
        Size(64, 48),
        AffineTransform.scale(2.0, 2.0),
        interpolation=TransformInterpolation.NEAREST,
    )
    scaled = _paint_frame(coordinator, 72, 56)
    assert coordinator.document_rect() == QRectF(0.0, 0.0, 64.0, 48.0)
    assert _pixel(scaled, 7, 5) == (230, 70, 20, 255)
    assert _pixel(scaled, 3, 2) != (230, 70, 20, 255)

    coordinator.clear_geometry_preview()
    restored = _paint_frame(coordinator, 72, 56)
    assert coordinator.document_rect() == QRectF(0.0, 0.0, 32.0, 24.0)
    assert _pixel(restored, 3, 2) == (230, 70, 20, 255)
    assert _pixel(restored, 7, 5) != (230, 70, 20, 255)
