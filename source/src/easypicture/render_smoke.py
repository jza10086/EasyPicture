"""Standalone full-frame canvas renderer smoke test."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
from PySide6.QtGui import QImage
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication

from easypicture.commands import DocumentCommandService
from easypicture.domain import (
    AffineTransform,
    Document,
    RasterObject,
    Size,
    SvgPrimitiveKind,
    SvgPrimitiveStyle,
    SvgRectGeometry,
    create_svg_primitive,
)
from easypicture.imaging import TilePreviewMode, TransformInterpolation
from easypicture.imaging.ai_pixel_cleanup import (
    AiPixelCleanupParameters,
    AiPixelCleanupRequest,
    calculate_ai_pixel_cleanup,
)
from easypicture.imaging.pixel_outline import (
    PixelOutlineParameters,
    PixelOutlineRequest,
    calculate_pixel_outline,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, Rect, TileCoord
from easypicture.render import RenderBackendKind
from easypicture.runtime import package_versions
from easypicture.ui.canvas_view import CanvasView
from easypicture.ui.settings import TileWorkspaceSettings
from easypicture.ui.tile_preview import TilePreviewRequest, calculate_tile_preview


def run_render_smoke_test(
    app: QApplication,
    directory: Path,
    backend: RenderBackendKind,
) -> int:
    """Exercise preview replacement and compare the restored complete frame."""
    directory.mkdir(parents=True, exist_ok=True)
    view = CanvasView(render_backend=backend)
    reference = CanvasView(render_backend=RenderBackendKind.SOFTWARE)
    try:
        document = _create_scene()
        preview_tiles = _multi_tile_preview(
            document,
            (96, 72, 700, 470),
            (240, 32, 112, 220),
        )

        for canvas in (view, reference):
            canvas.resize(800, 600)
            canvas.set_document(document)
            canvas.show()
        _process_frames(app)
        for canvas in (view, reference):
            canvas.actual_size()
            canvas.centerOn(document.width / 2.0, document.height / 2.0)
        _process_frames(app)

        for canvas in (view, reference):
            canvas.set_pixel_preview(document.require_raster_target(), preview_tiles)
        _process_frames(app)
        preview_frame = _capture_frame(view, app).convertToFormat(QImage.Format.Format_RGBA8888)
        preview_reference = _capture_frame(reference, app).convertToFormat(
            QImage.Format.Format_RGBA8888
        )
        preview_frame.save(str(directory / "preview.png"))
        preview_reference.save(str(directory / "preview-reference.png"))
        top_left = view.mapFromScene(0.0, 0.0)
        bottom_right = view.mapFromScene(
            float(document.width),
            float(document.height),
        )
        preview_error, _preview_full_error = _channel_errors(
            preview_frame,
            preview_reference,
            (
                top_left.x(),
                top_left.y(),
                bottom_right.x(),
                bottom_right.y(),
            ),
        )
        replaced_tiles = _multi_tile_preview(
            document,
            (40, 128, 650, 430),
            (32, 210, 144, 230),
        )
        for canvas in (view, reference):
            canvas.replace_pixel_preview(document.require_raster_target(), replaced_tiles)
        replaced_frame = _capture_frame(view, app).convertToFormat(QImage.Format.Format_RGBA8888)
        replaced_reference = _capture_frame(reference, app).convertToFormat(
            QImage.Format.Format_RGBA8888
        )
        replaced_frame.save(str(directory / "preview-replaced.png"))
        replaced_reference.save(str(directory / "preview-replaced-reference.png"))
        replaced_preview_error, _replaced_full_error = _channel_errors(
            replaced_frame,
            replaced_reference,
            (
                top_left.x(),
                top_left.y(),
                bottom_right.x(),
                bottom_right.y(),
            ),
        )
        transient_vector = create_svg_primitive(
            SvgPrimitiveKind.RECT,
            SvgRectGeometry(120.0, 96.0, 260.0, 180.0),
            SvgPrimitiveStyle(fill=(225, 28, 108, 235), stroke=None),
            document.size,
        )
        for canvas in (view, reference):
            canvas.clear_pixel_preview()
            canvas.replace_transient_vector_preview((transient_vector,))
        transient_frame = _capture_frame(view, app).convertToFormat(QImage.Format.Format_RGBA8888)
        transient_reference = _capture_frame(reference, app).convertToFormat(
            QImage.Format.Format_RGBA8888
        )
        transient_frame.save(str(directory / "transient-vector-preview.png"))
        transient_reference.save(str(directory / "transient-vector-preview-reference.png"))
        transient_vector_error, _transient_full_error = _channel_errors(
            transient_frame,
            transient_reference,
            (
                top_left.x(),
                top_left.y(),
                bottom_right.x(),
                bottom_right.y(),
            ),
        )
        rotation = (
            AffineTransform.translation(-256.0, -192.0)
            .then(AffineTransform.rotation(43.0))
            .then(AffineTransform.translation(256.0, 192.0))
        )
        for canvas in (view, reference):
            canvas.clear_vector_preview()
        view.set_transform_preview(
            document.require_raster_target(),
            rotation,
            TransformInterpolation.LINEAR,
        )
        _capture_frame(view, app).save(str(directory / "rotated.png"))

        view.clear_transform_preview()
        restored = _capture_frame(view, app).convertToFormat(QImage.Format.Format_RGBA8888)
        expected = _capture_frame(reference, app).convertToFormat(QImage.Format.Format_RGBA8888)
        restored.save(str(directory / "framebuffer.png"))
        expected.save(str(directory / "reference.png"))
        top_left = view.mapFromScene(0.0, 0.0)
        bottom_right = view.mapFromScene(
            float(document.width),
            float(document.height),
        )
        error, full_error = _channel_errors(
            restored,
            expected,
            (
                top_left.x(),
                top_left.y(),
                bottom_right.x(),
                bottom_right.y(),
            ),
        )
        outline_error = _outline_preview_smoke(app, view, reference, directory)
        cleanup_error = _cleanup_preview_smoke(app, view, reference, directory)
        tile_error, tile_overlay_error = _tile_surface_smoke(app, view, reference, directory)
        diagnostics = view.render_coordinator.diagnostics
        payload = {
            "dependencies": package_versions(),
            "requested_backend": diagnostics.requested_backend.value,
            "active_backend": diagnostics.active_backend.value,
            "vendor": diagnostics.vendor,
            "renderer": diagnostics.renderer,
            "version": diagnostics.version,
            "frame_generation": diagnostics.frame_generation,
            "texture_bytes": diagnostics.texture_bytes,
            "fallback_reason": diagnostics.fallback_reason,
            "maximum_channel_error": error,
            "maximum_preview_channel_error": preview_error,
            "maximum_replaced_preview_channel_error": replaced_preview_error,
            "maximum_transient_vector_preview_channel_error": transient_vector_error,
            "maximum_tile_surface_channel_error": tile_error,
            "maximum_tile_overlay_channel_error": tile_overlay_error,
            "maximum_outline_preview_vs_committed_error": outline_error,
            "maximum_ai_cleanup_preview_vs_committed_error": cleanup_error,
            "maximum_channel_error_including_border": full_error,
            "border_comparison": "2 px document border excluded",
        }
        (directory / "diagnostics.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if backend is RenderBackendKind.OPENGL:
            if diagnostics.active_backend is not RenderBackendKind.OPENGL:
                return 2
        return (
            0
            if error <= 1
            and preview_error <= 1
            and replaced_preview_error <= 1
            and transient_vector_error <= 1
            and outline_error <= 1
            and cleanup_error <= 1
            and tile_error <= 1
            and tile_overlay_error <= 1
            else 3
        )
    except Exception as error:
        (directory / "diagnostics.json").write_text(
            json.dumps(
                {
                    "requested_backend": backend.value,
                    "active_backend": "error",
                    "error": f"{type(error).__name__}: {error}",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return 1
    finally:
        view.close()
        reference.close()
        _process_frames(app, count=1)


def _outline_preview_smoke(
    app: QApplication,
    view: CanvasView,
    reference: CanvasView,
    directory: Path,
) -> int:
    """Compare transient insertion with actual committed pixels, including occlusion."""
    project = Document.create(128, 128)
    pixels = np.zeros((128, 128, 4), dtype=np.uint8)
    pixels[30:95, 30:95] = (30, 120, 240, 255)
    project.active_layer.pixels.write_region(Rect(0, 0, 128, 128), pixels)
    source = project.active_layer
    upper = RasterObject.create_blank("遮挡", project.size)
    pixels[:] = 0
    pixels[50:110, 10:60] = (180, 30, 80, 190)
    upper.pixels.write_region(Rect(0, 0, 128, 128), pixels)
    project.insert_layer(upper, 1, activate=False)
    project.nodes[0].opacity = 0.65
    view.set_document(project)
    maximum = 0
    for width in (1, 7, 2):
        result = calculate_pixel_outline(
            PixelOutlineRequest(
                DocumentSnapshot.capture(project),
                source.id,
                PixelOutlineParameters(width=width, color=(240, 180, 30, 210)),
            ),
            CancellationToken(),
            lambda *_args: None,
        )
        committed = DocumentSnapshot.capture(project).to_document()
        commands = DocumentCommandService()
        commands.attach_document(committed)
        assert commands.apply_pixel_outline(replace(result, source_revision=committed.revision))
        reference.set_document(committed)
        view.render_coordinator.replace_raster_layer_preview(source.id, result.updates)
        for canvas in (view, reference):
            canvas.set_zoom(2)
            canvas.centerOn(64, 64)
        _process_frames(app)
        actual = _capture_frame(view, app).convertToFormat(QImage.Format.Format_RGBA8888)
        expected = _capture_frame(reference, app).convertToFormat(QImage.Format.Format_RGBA8888)
        actual.save(str(directory / f"outline-{width}.png"))
        expected.save(str(directory / f"outline-{width}-committed.png"))
        corner, bottom = view.mapFromScene(0, 0), view.mapFromScene(128, 128)
        error, _ = _channel_errors(
            actual, expected, (corner.x(), corner.y(), bottom.x(), bottom.y())
        )
        maximum = max(maximum, error)
    view.render_coordinator.clear_raster_layer_preview()
    return maximum


def _cleanup_preview_smoke(
    app: QApplication,
    view: CanvasView,
    reference: CanvasView,
    directory: Path,
) -> int:
    project = Document.create(128, 128)
    pixels = np.zeros((128, 128, 4), dtype=np.uint8)
    pixels[12:115, 20:100] = (90, 120, 170, 180)
    source = project.active_layer
    source.pixels.write_region(Rect(0, 0, 128, 128), pixels)
    source.opacity = 0.8
    upper = RasterObject.create_blank("遮挡", project.size)
    pixels[:] = 0
    pixels[40:90, :50] = (170, 40, 80, 160)
    upper.pixels.write_region(Rect(0, 0, 128, 128), pixels)
    project.insert_layer(upper, 1, activate=False)
    project.nodes[0].opacity = 0.65
    view.set_document(project)
    maximum = 0
    for number, color in enumerate(((10, 20, 30), (210, 180, 130), (20, 60, 90))):
        result = calculate_ai_pixel_cleanup(
            AiPixelCleanupRequest(
                DocumentSnapshot.capture(project),
                source.id,
                AiPixelCleanupParameters(fixed_palette=(color,)),
            ),
            CancellationToken(),
            lambda *_args: None,
        )
        committed = DocumentSnapshot.capture(project).to_document()
        commands = DocumentCommandService()
        commands.attach_document(committed)
        assert commands.apply_ai_pixel_cleanup(replace(result, source_revision=committed.revision))
        reference.set_document(committed)
        view.render_coordinator.replace_raster_layer_preview(
            source.id,
            result.updates,
            replace_source=True,
        )
        for canvas in (view, reference):
            canvas.set_zoom(2)
            canvas.centerOn(64, 64)
        _process_frames(app)
        actual = _capture_frame(view, app).convertToFormat(QImage.Format.Format_RGBA8888)
        expected = _capture_frame(reference, app).convertToFormat(QImage.Format.Format_RGBA8888)
        actual.save(str(directory / f"ai-cleanup-{number}.png"))
        expected.save(str(directory / f"ai-cleanup-{number}-committed.png"))
        corner, bottom = view.mapFromScene(0, 0), view.mapFromScene(128, 128)
        error, _ = _channel_errors(
            actual, expected, (corner.x(), corner.y(), bottom.x(), bottom.y())
        )
        maximum = max(maximum, error)
    view.render_coordinator.clear_raster_layer_preview()
    return maximum


def _tile_surface_smoke(
    app: QApplication, view: CanvasView, reference: CanvasView, directory: Path
) -> tuple[int, int]:
    project = _create_scene()
    commands = DocumentCommandService()
    commands.attach_document(project)
    commands.set_sprite_cell_size(Size(128, 128), clear_frames=False)
    commands.create_sprite_frame(Rect(128, 0, 128, 128))
    commands.create_sprite_frame(Rect(256, 0, 128, 128))
    first, second = project.sprite_workspace.frames
    maximum = 0
    overlay_maximum = 0
    for mode in (TilePreviewMode.OFFSET, TilePreviewMode.TRANSITION, TilePreviewMode.OFFSET):
        result = calculate_tile_preview(
            TilePreviewRequest(
                DocumentSnapshot.capture(project),
                first.id,
                second.id,
                TileWorkspaceSettings(preview_mode=mode),
            ),
            CancellationToken(),
            lambda _done, _total, _label: None,
        )
        for canvas in (view, reference):
            canvas.set_document(result.document, preserve_view=True)
        _process_frames(app)
        for canvas in (view, reference):
            canvas.set_zoom(1)
            # Compare on an identical integer pixel grid; the preceding 2x outline
            # fixture can leave a half-pixel translation from cursor-anchored zoom.
            canvas.resetTransform()
            canvas.centerOn(192, 192)
        _process_frames(app)
        actual = _capture_frame(view, app).convertToFormat(QImage.Format.Format_RGBA8888)
        expected = _capture_frame(reference, app).convertToFormat(QImage.Format.Format_RGBA8888)
        actual.save(str(directory / f"tile-{mode.value}.png"))
        expected.save(str(directory / f"tile-{mode.value}-reference.png"))
        corner = view.mapFromScene(0, 0)
        bottom = view.mapFromScene(result.document.width, result.document.height)
        error, _ = _channel_errors(
            actual, expected, (corner.x(), corner.y(), bottom.x(), bottom.y())
        )
        maximum = max(maximum, error)
        # Native diagnostic captures deliberately precede scene overlays. For
        # heatmaps compare completed Qt widget framebuffers on both backends.
        for canvas in (view, reference):
            canvas.set_tile_heatmap(result.segments)
        _process_frames(app)
        view.request_render_capture(include_overlays=True)
        _process_frames(app)
        completed = view.take_render_capture()
        actual = (completed if completed is not None else _grab_framebuffer(view)).convertToFormat(
            QImage.Format.Format_RGBA8888
        )
        expected = _grab_framebuffer(reference).convertToFormat(QImage.Format.Format_RGBA8888)
        actual.save(str(directory / f"tile-{mode.value}-overlay.png"))
        expected.save(str(directory / f"tile-{mode.value}-overlay-reference.png"))
        overlay_error, _ = _channel_errors(
            actual, expected, (corner.x(), corner.y(), bottom.x(), bottom.y())
        )
        overlay_maximum = max(overlay_maximum, overlay_error)
    return maximum, overlay_maximum


def _create_scene() -> Document:
    document = Document.create(768, 512)
    bottom = document.active_layer
    bottom.name = "底层"
    bottom_tile = np.zeros(
        (DEFAULT_TILE_SIZE, DEFAULT_TILE_SIZE, 4),
        dtype=np.uint8,
    )
    bottom_tile[..., :] = (38, 82, 164, 210)
    bottom.pixels.replace_tile(0, 0, bottom_tile)
    bottom.pixels.replace_tile(1, 1, np.rot90(bottom_tile).copy(order="C"))

    top = RasterObject.create_blank("上层", Size(768, 512))
    top_tile = np.zeros_like(bottom_tile)
    yy, xx = np.ogrid[:DEFAULT_TILE_SIZE, :DEFAULT_TILE_SIZE]
    circle = (xx - 128) ** 2 + (yy - 128) ** 2 <= 92**2
    top_tile[circle] = (238, 132, 42, 150)
    top.pixels.replace_tile(1, 0, top_tile)
    document.insert_layer(top, 1)
    return document


def _multi_tile_preview(
    document: Document,
    rect: tuple[int, int, int, int],
    color: tuple[int, int, int, int],
) -> dict[TileCoord, np.ndarray]:
    """Create one continuous translucent rectangle spanning four source Tiles."""

    updates: dict[TileCoord, np.ndarray] = {}
    left_bound, top_bound, right_bound, bottom_bound = rect
    for ty in range(2):
        for tx in range(3):
            coord = TileCoord(tx, ty)
            tile = document.active_layer.pixels.read_tile(tx, ty).copy(order="C")
            global_left = tx * DEFAULT_TILE_SIZE
            global_top = ty * DEFAULT_TILE_SIZE
            left = max(0, left_bound - global_left)
            top = max(0, top_bound - global_top)
            right = min(DEFAULT_TILE_SIZE, right_bound - global_left)
            bottom = min(DEFAULT_TILE_SIZE, bottom_bound - global_top)
            if right > left and bottom > top:
                tile[top:bottom, left:right] = color
            updates[coord] = tile
    return updates


def _process_frames(app: QApplication, *, count: int = 4) -> None:
    for _index in range(count):
        app.processEvents()


def _grab_framebuffer(view: CanvasView) -> QImage:
    viewport = view.viewport()
    if isinstance(viewport, QOpenGLWidget):
        return viewport.grabFramebuffer()
    return viewport.grab().toImage()


def _capture_frame(view: CanvasView, app: QApplication) -> QImage:
    view.request_render_capture()
    _process_frames(app)
    captured = view.take_render_capture()
    return captured if captured is not None else _grab_framebuffer(view)


def _channel_errors(
    first: QImage,
    second: QImage,
    document_boundary: tuple[int, int, int, int],
) -> tuple[int, int]:
    if first.size() != second.size():
        return 255, 255
    width = first.width()
    height = first.height()
    first_array = (
        np.frombuffer(first.constBits(), dtype=np.uint8)
        .reshape(
            height,
            first.bytesPerLine(),
        )[:, : width * 4]
        .reshape(height, width, 4)
    )
    second_array = (
        np.frombuffer(second.constBits(), dtype=np.uint8)
        .reshape(
            height,
            second.bytesPerLine(),
        )[:, : width * 4]
        .reshape(height, width, 4)
    )
    if first_array.size == 0:
        return 0, 0
    difference = np.abs(first_array.astype(np.int16) - second_array.astype(np.int16))
    full_result = float(difference.max())
    mask = np.ones((height, width), dtype=np.bool_)
    left, top, right, bottom = document_boundary
    vertical_top = max(0, top - 2)
    vertical_bottom = min(height, bottom + 3)
    horizontal_left = max(0, left - 2)
    horizontal_right = min(width, right + 3)
    mask[
        vertical_top:vertical_bottom,
        max(0, left - 2) : min(width, left + 3),
    ] = False
    mask[
        vertical_top:vertical_bottom,
        max(0, right - 2) : min(width, right + 3),
    ] = False
    mask[
        max(0, top - 2) : min(height, top + 3),
        horizontal_left:horizontal_right,
    ] = False
    mask[
        max(0, bottom - 2) : min(height, bottom + 3),
        horizontal_left:horizontal_right,
    ] = False
    stable_result = float(difference[mask].max()) if mask.any() else full_result
    return (
        255 if not math.isfinite(stable_result) else int(stable_result),
        255 if not math.isfinite(full_result) else int(full_result),
    )


__all__ = ["run_render_smoke_test"]
