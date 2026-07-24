from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QStyleOptionGraphicsItem

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, PixelPoint, RasterLayer, Size
from easypicture.pixels import TileCoord
from easypicture.render import TileRenderer
from easypicture.ui.canvas_item import CanvasItem


def _solid(color: tuple[int, int, int, int]) -> np.ndarray:
    pixels = np.empty((256, 256, 4), dtype=np.uint8)
    pixels[:] = color
    return pixels


def _pixels(image: QImage) -> np.ndarray:
    straight = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return (
        np.frombuffer(straight.bits(), dtype=np.uint8, count=straight.sizeInBytes())
        .reshape(straight.height(), straight.width(), 4)
        .copy()
    )


def _source_over(bottom: np.ndarray, top: np.ndarray, opacity: float = 1.0) -> np.ndarray:
    bottom_f = bottom.astype(np.float64) / 255.0
    top_f = top.astype(np.float64) / 255.0
    source_alpha = top_f[3] * opacity
    destination_alpha = bottom_f[3]
    output_alpha = source_alpha + destination_alpha * (1.0 - source_alpha)
    output = np.zeros(4, dtype=np.float64)
    if output_alpha > 0:
        output[:3] = (
            top_f[:3] * source_alpha + bottom_f[:3] * destination_alpha * (1.0 - source_alpha)
        ) / output_alpha
    output[3] = output_alpha
    return np.rint(output * 255.0).astype(np.uint8)


def test_qpainter_compositor_matches_straight_rgba_reference() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    bottom = document.active_layer
    assert service.apply_pixel_tiles(
        bottom.id,
        {TileCoord(0, 0): _solid((0, 0, 255, 128))},
        "蓝色",
    )
    assert service.create_layer()
    top = document.active_layer
    assert service.apply_pixel_tiles(
        top.id,
        {TileCoord(0, 0): _solid((255, 0, 0, 128))},
        "红色",
    )
    renderer = TileRenderer(document)

    actual = _pixels(renderer.render_tile(TileCoord(0, 0)))[0, 0]
    expected = _source_over(
        np.array((0, 0, 255, 128), dtype=np.uint8),
        np.array((255, 0, 0, 128), dtype=np.uint8),
    )

    assert int(np.abs(actual.astype(np.int16) - expected.astype(np.int16)).max()) <= 1
    assert renderer.render_tile(TileCoord(0, 0)).format() == (
        QImage.Format.Format_RGBA8888_Premultiplied
    )


def test_hidden_layer_opacity_and_zero_alpha_rgb() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    bottom = document.active_layer
    assert service.apply_pixel_tiles(
        bottom.id,
        {TileCoord(0, 0): _solid((0, 0, 255, 255))},
        "蓝色",
    )
    assert service.create_layer()
    top = document.active_layer
    transparent = _solid((123, 45, 67, 0))
    transparent[0, 0] = (255, 0, 0, 255)
    assert service.apply_pixel_tiles(top.id, {TileCoord(0, 0): transparent}, "顶层")
    assert service.set_layer_opacity(top.id, 0.5)
    renderer = TileRenderer(document)

    pixels = _pixels(renderer.render_tile(TileCoord(0, 0)))
    expected = _source_over(
        np.array((0, 0, 255, 255), dtype=np.uint8),
        np.array((255, 0, 0, 255), dtype=np.uint8),
        0.5,
    )
    assert int(np.abs(pixels[0, 0].astype(np.int16) - expected.astype(np.int16)).max()) <= 1
    assert tuple(pixels[0, 1]) == (0, 0, 255, 255)

    assert service.set_layer_visible(top.id, False)
    assert tuple(_pixels(renderer.render_tile(TileCoord(0, 0)))[0, 0]) == (0, 0, 255, 255)


def test_positive_negative_and_non_aligned_layer_origins() -> None:
    document = Document.create(512, 512)
    positive = RasterLayer.create_blank(
        "正偏移",
        document.size,
        origin=PixelPoint(10, 20),
    )
    positive.pixels.replace_tile(0, 0, _solid((255, 0, 0, 255)))
    document.insert_layer(positive, 1)
    negative = RasterLayer.create_blank(
        "负偏移",
        document.size,
        origin=PixelPoint(-5, -7),
    )
    negative_pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    negative_pixels[7, 5] = (0, 255, 0, 255)
    negative.pixels.replace_tile(0, 0, negative_pixels)
    document.insert_layer(negative, 2)
    renderer = TileRenderer(document)

    pixels = _pixels(renderer.render_tile(TileCoord(0, 0)))
    assert tuple(pixels[20, 10]) == (255, 0, 0, 255)
    assert tuple(pixels[0, 0]) == (0, 255, 0, 255)


def test_layer_local_size_clips_edge_tile_pixels() -> None:
    document = Document.create(256, 256)
    layer = RasterLayer.create_blank("局部层", Size(10, 10))
    layer.pixels.replace_tile(0, 0, _solid((255, 0, 0, 255)))
    document.insert_layer(layer, 1)
    renderer = TileRenderer(document)

    pixels = _pixels(renderer.render_tile(TileCoord(0, 0)))

    assert tuple(pixels[9, 9]) == (255, 0, 0, 255)
    assert tuple(pixels[10, 10]) == (0, 0, 0, 0)


def test_pixel_invalidation_keeps_unrelated_composite_and_property_clears_all() -> None:
    document = Document.create(512, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    layer = document.active_layer
    assert service.apply_pixel_tiles(
        layer.id,
        {
            TileCoord(0, 0): _solid((10, 20, 30, 255)),
            TileCoord(1, 0): _solid((40, 50, 60, 255)),
        },
        "初始",
    )
    renderer = TileRenderer(document)
    first = renderer.render_tile(TileCoord(0, 0))
    second = renderer.render_tile(TileCoord(1, 0))
    source_count = renderer.source_cache_count

    assert service.apply_pixel_tiles(
        layer.id,
        {TileCoord(0, 0): _solid((70, 80, 90, 255))},
        "局部修改",
    )
    assert renderer.cached_tile(TileCoord(0, 0)) is first
    assert renderer.is_tile_stale(TileCoord(0, 0))
    assert renderer.cached_tile(TileCoord(1, 0)) is second
    assert renderer.render_tile(TileCoord(0, 0)) is not first
    assert not renderer.is_tile_stale(TileCoord(0, 0))

    assert service.set_layer_opacity(layer.id, 0.75)
    assert renderer.composite_cache_count == 0
    assert renderer.source_cache_count == source_count


def test_preview_keeps_previous_frame_until_replacement_is_ready() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    layer = document.active_layer
    assert service.apply_pixel_tiles(
        layer.id,
        {TileCoord(0, 0): _solid((0, 0, 255, 255))},
        "底图",
    )
    renderer = TileRenderer(document)
    previous = renderer.render_tile(TileCoord(0, 0))
    preview = _solid((255, 0, 0, 255))

    renderer.set_preview_tiles(layer.id, {TileCoord(0, 0): preview})

    assert renderer.cached_tile(TileCoord(0, 0)) is previous
    assert renderer.is_tile_stale(TileCoord(0, 0))
    renderer.request_tiles([TileCoord(0, 0)], QPointF(128.0, 128.0))
    assert renderer.pending_count == 1
    renderer.drain_pending()

    replacement = renderer.cached_tile(TileCoord(0, 0))
    assert replacement is not None
    assert replacement is not previous
    assert tuple(_pixels(replacement)[0, 0]) == (255, 0, 0, 255)
    assert not renderer.is_tile_stale(TileCoord(0, 0))


def test_canvas_paints_stale_frame_instead_of_checkerboard(qtbot) -> None:
    _ = qtbot
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    layer = document.active_layer
    assert service.apply_pixel_tiles(
        layer.id,
        {TileCoord(0, 0): _solid((0, 0, 255, 255))},
        "底图",
    )
    renderer = TileRenderer(document)
    renderer.render_tile(TileCoord(0, 0))
    item = CanvasItem(document.size, renderer)
    renderer.set_preview_tiles(
        layer.id,
        {TileCoord(0, 0): _solid((255, 0, 0, 255))},
    )
    target = QImage(256, 256, QImage.Format.Format_RGBA8888_Premultiplied)
    target.fill(0)
    option = QStyleOptionGraphicsItem()
    option.exposedRect = QRectF(0.0, 0.0, 256.0, 256.0)
    painter = QPainter(target)

    item.paint(painter, option)
    painter.end()

    assert tuple(_pixels(target)[100, 100]) == (0, 0, 255, 255)
    assert renderer.pending_count == 1
    item.dispose()


def test_progressive_scheduler_yields_between_tiles(qtbot) -> None:
    document = Document.create(1024, 1024)
    service = DocumentCommandService()
    service.attach_document(document)
    layer = document.active_layer
    updates = {
        TileCoord(tx, ty): _solid((tx * 20, ty * 20, 100, 255))
        for ty in range(4)
        for tx in range(4)
    }
    assert service.apply_pixel_tiles(layer.id, updates, "测试")
    renderer = TileRenderer(document, time_slice_ms=0.001)
    heartbeat_count = 0
    heartbeat = QTimer()

    def tick() -> None:
        nonlocal heartbeat_count
        heartbeat_count += 1

    heartbeat.timeout.connect(tick)
    heartbeat.start(0)
    renderer.request_tiles(updates, QPointF(512.0, 512.0))
    qtbot.waitUntil(lambda: renderer.pending_count == 0)
    heartbeat.stop()

    assert renderer.composite_cache_count == 16
    assert heartbeat_count >= 2


def test_render_cache_respects_byte_budget() -> None:
    document = Document.create(768, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    layer = document.active_layer
    assert service.apply_pixel_tiles(
        layer.id,
        {TileCoord(tx, 0): _solid((tx * 30, 10, 20, 255)) for tx in range(3)},
        "测试",
    )
    renderer = TileRenderer(document, cache_budget_bytes=2 * 256 * 256 * 4)

    for tx in range(3):
        renderer.render_tile(TileCoord(tx, 0))

    assert renderer.cache_bytes <= renderer.cache_budget_bytes
