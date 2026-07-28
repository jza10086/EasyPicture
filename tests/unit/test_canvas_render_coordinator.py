from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from easypicture.commands import DocumentCommandService
from easypicture.domain import AffineTransform, Document
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

    assert _pixel(coordinator.render_tile(TileCoord(0, 0)), 10, 10) == (
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
