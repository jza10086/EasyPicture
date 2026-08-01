"""Unified full-frame canvas rendering state and software reference backend."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import numpy as np
from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QTransform

from easypicture.domain import (
    AffineTransform,
    Document,
    DocumentEvent,
    LayerListChanged,
    LayerPixelsChanged,
    RasterGroupLayer,
    RasterLayer,
    Size,
    TextObject,
    TransformMatrix,
    VectorGroupLayer,
    as_projective,
)
from easypicture.domain.document import Unsubscribe
from easypicture.imaging import TransformInterpolation
from easypicture.pixels import Rect, RGBAArray, TileCoord
from easypicture.pixels.types import validate_rgba_array
from easypicture.render.qimage_bridge import rgba_tile_to_qimage
from easypicture.render.text_renderer import paint_text_objects

DEFAULT_SOURCE_CACHE_BYTES = 256 * 1024 * 1024


class RenderBackendKind(StrEnum):
    AUTO = "auto"
    OPENGL = "opengl"
    SOFTWARE = "software"


@dataclass(frozen=True, slots=True)
class CanvasRenderDiagnostics:
    requested_backend: RenderBackendKind
    active_backend: RenderBackendKind
    vendor: str = ""
    renderer: str = ""
    version: str = ""
    frame_generation: int = 0
    texture_bytes: int = 0
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TransformPreviewState:
    layer_id: UUID
    matrix: TransformMatrix
    interpolation: TransformInterpolation


@dataclass(frozen=True, slots=True)
class GeometryPreviewState:
    """Temporary canvas or layer geometry rendered without changing the document."""

    document_size: Size
    matrix: TransformMatrix = AffineTransform()
    layer_id: UUID | None = None
    interpolation: TransformInterpolation = TransformInterpolation.LINEAR


@dataclass(frozen=True, slots=True)
class VectorTextPreviewState:
    group_id: UUID
    objects: tuple[TextObject, ...]


type SourceCacheKey = tuple[UUID, TileCoord, int, int, str]


class CanvasRenderCoordinator(QObject):
    """Own transient display state and request complete viewport frames."""

    frame_requested = Signal()
    diagnostics_changed = Signal(object)

    CHECKER_SIZE = 16

    def __init__(
        self,
        *,
        requested_backend: RenderBackendKind = RenderBackendKind.SOFTWARE,
        cache_budget_bytes: int = DEFAULT_SOURCE_CACHE_BYTES,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.requested_backend = requested_backend
        self.cache_budget_bytes = cache_budget_bytes
        self.document: Document | None = None
        self._unsubscribe: Unsubscribe | None = None
        self._pixel_preview_layer_id: UUID | None = None
        self._pixel_preview_tiles: dict[TileCoord, RGBAArray] = {}
        self._pixel_preview_revisions: dict[TileCoord, int] = {}
        self._pixel_preview_epoch = 0
        self._transform_preview: TransformPreviewState | None = None
        self._geometry_preview: GeometryPreviewState | None = None
        self._vector_text_preview: VectorTextPreviewState | None = None
        self._source_cache: OrderedDict[SourceCacheKey, QImage] = OrderedDict()
        self._generation = 0
        self._active_backend = RenderBackendKind.SOFTWARE
        self._vendor = ""
        self._renderer = "Qt QPainter"
        self._version = ""
        self._texture_bytes = 0
        self._fallback_reason: str | None = None
        self._checker = self._create_checker_pattern()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def transform_preview(self) -> TransformPreviewState | None:
        return self._transform_preview

    @property
    def geometry_preview(self) -> GeometryPreviewState | None:
        return self._geometry_preview

    @property
    def preview_tile_count(self) -> int:
        return len(self._pixel_preview_tiles)

    @property
    def cache_bytes(self) -> int:
        return sum(image.sizeInBytes() for image in self._source_cache.values())

    @property
    def diagnostics(self) -> CanvasRenderDiagnostics:
        return CanvasRenderDiagnostics(
            requested_backend=self.requested_backend,
            active_backend=self._active_backend,
            vendor=self._vendor,
            renderer=self._renderer,
            version=self._version,
            frame_generation=self._generation,
            texture_bytes=self._texture_bytes,
            fallback_reason=self._fallback_reason,
        )

    def set_backend_diagnostics(
        self,
        active_backend: RenderBackendKind,
        *,
        vendor: str = "",
        renderer: str = "",
        version: str = "",
        fallback_reason: str | None = None,
        texture_bytes: int | None = None,
    ) -> None:
        self._active_backend = active_backend
        self._vendor = vendor
        self._renderer = renderer
        self._version = version
        self._fallback_reason = fallback_reason
        if texture_bytes is not None:
            self._texture_bytes = texture_bytes
        diagnostics = self.diagnostics
        self.diagnostics_changed.emit(diagnostics)

    def set_document(self, document: Document | None) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self.document = document
        self._pixel_preview_layer_id = None
        self._pixel_preview_tiles.clear()
        self._pixel_preview_revisions.clear()
        self._transform_preview = None
        self._geometry_preview = None
        self._vector_text_preview = None
        self._source_cache.clear()
        if document is not None:
            self._unsubscribe = document.subscribe(self._on_document_event)
        self.request_frame()

    def dispose(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self.document = None
        self._source_cache.clear()
        self._pixel_preview_tiles.clear()
        self._pixel_preview_revisions.clear()
        self._transform_preview = None
        self._geometry_preview = None
        self._vector_text_preview = None

    def set_pixel_preview(
        self,
        layer_id: UUID,
        updates: Mapping[TileCoord, RGBAArray],
    ) -> None:
        document = self._require_document()
        layer = document.layer_by_id(layer_id)
        if (
            self._pixel_preview_layer_id is not None
            and self._pixel_preview_layer_id != layer_id
        ):
            self.clear_pixel_preview()
        for coord, pixels in updates.items():
            validate_rgba_array(
                pixels,
                expected_width=layer.pixels.tile_size,
                expected_height=layer.pixels.tile_size,
            )
            self._pixel_preview_tiles[coord] = pixels
        self._pixel_preview_epoch += 1
        for coord in updates:
            self._pixel_preview_revisions[coord] = self._pixel_preview_epoch
        self._pixel_preview_layer_id = layer_id
        self._discard_tile_cache(layer_id, frozenset(updates))
        self.request_frame()

    def replace_pixel_preview(
        self,
        layer_id: UUID,
        updates: Mapping[TileCoord, RGBAArray | None],
    ) -> None:
        """Replace the complete preview mapping in one frame generation."""

        document = self._require_document()
        layer = document.layer_by_id(layer_id)
        normalized: dict[TileCoord, RGBAArray] = {}
        for coord, pixels in updates.items():
            if pixels is None:
                pixels = np.zeros(
                    (layer.pixels.tile_size, layer.pixels.tile_size, 4),
                    dtype=np.uint8,
                )
            validate_rgba_array(
                pixels,
                expected_width=layer.pixels.tile_size,
                expected_height=layer.pixels.tile_size,
            )
            normalized[coord] = pixels
        old_layer_id = self._pixel_preview_layer_id
        old_coords = frozenset(self._pixel_preview_tiles)
        self._pixel_preview_epoch += 1
        self._pixel_preview_layer_id = layer_id if normalized else None
        self._pixel_preview_tiles = normalized
        self._pixel_preview_revisions = {
            coord: self._pixel_preview_epoch for coord in normalized
        }
        if old_layer_id is not None:
            self._discard_tile_cache(old_layer_id, old_coords)
        self._discard_tile_cache(layer_id, frozenset(normalized))
        self.request_frame()

    def clear_pixel_preview(self) -> None:
        layer_id = self._pixel_preview_layer_id
        if layer_id is None:
            return
        preview_coords = frozenset(self._pixel_preview_tiles)
        self._pixel_preview_layer_id = None
        self._pixel_preview_tiles.clear()
        self._pixel_preview_revisions.clear()
        self._discard_tile_cache(layer_id, preview_coords)
        self.request_frame()

    def set_transform_preview(
        self,
        layer_id: UUID,
        matrix: TransformMatrix,
        interpolation: TransformInterpolation,
    ) -> None:
        self._require_document().layer_by_id(layer_id)
        if self._pixel_preview_layer_id is not None:
            self.clear_pixel_preview()
        self._transform_preview = TransformPreviewState(layer_id, matrix, interpolation)
        self._geometry_preview = None
        self.request_frame()

    def clear_transform_preview(self) -> None:
        if self._transform_preview is None:
            return
        self._transform_preview = None
        self.request_frame()

    def set_geometry_preview(
        self,
        document_size: Size,
        matrix: TransformMatrix | None = None,
        *,
        layer_id: UUID | None = None,
        interpolation: TransformInterpolation = TransformInterpolation.LINEAR,
    ) -> None:
        document = self._require_document()
        if layer_id is not None:
            document.layer_by_id(layer_id)
        self._transform_preview = None
        self._geometry_preview = GeometryPreviewState(
            document_size,
            matrix if matrix is not None else AffineTransform(),
            layer_id,
            interpolation,
        )
        self.request_frame()

    def clear_geometry_preview(self) -> None:
        if self._geometry_preview is None:
            return
        self._geometry_preview = None
        self.request_frame()

    def replace_vector_preview(
        self,
        group_id: UUID,
        objects: tuple[TextObject, ...],
    ) -> None:
        """Atomically replace a transient text-object preview mapping."""
        self._require_document().group_by_id(group_id)
        self._vector_text_preview = VectorTextPreviewState(group_id, tuple(objects))
        self.request_frame()

    def clear_vector_preview(self) -> None:
        if self._vector_text_preview is None:
            return
        self._vector_text_preview = None
        self.request_frame()

    def request_frame(self) -> None:
        self._generation += 1
        self.frame_requested.emit()
        self.diagnostics_changed.emit(self.diagnostics)

    def paint_software(self, painter: QPainter, exposed_scene_rect: QRectF) -> None:
        """Paint a complete scene frame without any final-composite cache."""
        self.paint_background(painter, exposed_scene_rect)
        if self.document is None:
            return
        self._paint_layers(painter, self.document_rect())
        self.paint_border(painter)

    def paint_background(self, painter: QPainter, exposed_scene_rect: QRectF) -> None:
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(exposed_scene_rect, QColor("#363636"))
        painter.restore()
        if self.document is None:
            return
        painter.fillRect(self.document_rect(), self._checker)

    def paint_border(self, painter: QPainter) -> None:
        if self.document is None:
            return
        painter.save()
        painter.setPen(QColor("#707070"))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.document_rect())
        painter.restore()

    def paint_vector_text_overlay(self, painter: QPainter, document_rect: QRectF) -> None:
        """Draw vector text after a native backend frame when needed.

        The software compositor remains the reference for ordering; this small
        overlay lets the OpenGL backend show editable text without uploading
        font rasterizations into its texture atlas.
        """
        if self.document is None or not self.document.text_groups:
            return
        painter.save()
        painter.setClipRect(document_rect)
        for group in self.document.text_groups:
            self._paint_text_group(painter, group)
        painter.restore()

    def paint_vector_group(
        self,
        painter: QPainter,
        group: VectorGroupLayer,
        document_rect: QRectF | None = None,
    ) -> None:
        """Paint one vector group at its stored position in the frame order.

        The OpenGL backend uses this narrow bridge between native raster
        passes.  Keeping the operation on the same QPainter preserves the
        QGraphicsView scene transform while allowing vector groups to remain
        correctly interleaved with raster groups.
        """
        if self.document is None or not group.visible or group.opacity <= 0.0:
            return
        rect = self.document_rect() if document_rect is None else document_rect
        painter.save()
        painter.setClipRect(rect)
        self._paint_text_group(painter, group)
        painter.restore()

    def document_rect(self) -> QRectF:
        document = self._require_document()
        size = (
            self._geometry_preview.document_size
            if self._geometry_preview is not None
            else document.size
        )
        return QRectF(
            0.0,
            0.0,
            float(size.width),
            float(size.height),
        )

    def display_tile_coords(self, layer: RasterLayer) -> frozenset[TileCoord]:
        coords = {coord for coord, _pixels in layer.pixels.iter_allocated_tiles()}
        if layer.id == self._pixel_preview_layer_id:
            coords.update(self._pixel_preview_tiles)
        return frozenset(coords)

    def display_tile_signature(
        self,
        layer: RasterLayer,
        coord: TileCoord,
        kind: str,
    ) -> tuple[int, int, int, str]:
        document = self._require_document()
        preview_generation = (
            self._pixel_preview_revisions.get(coord, 0)
            if layer.id == self._pixel_preview_layer_id
            and coord in self._pixel_preview_tiles
            else 0
        )
        selection_revision = document.selection.revision if kind != "source" else 0
        return (
            layer.pixels.tile_revision(coord.x, coord.y),
            preview_generation,
            selection_revision,
            kind,
        )

    def display_tile_image(
        self,
        layer: RasterLayer,
        coord: TileCoord,
        kind: str = "source",
    ) -> QImage:
        return self._source_image(layer, coord, kind)

    def render_tile(self, coord: TileCoord) -> QImage:
        """Render one uncached transparent document Tile for compatibility/tests."""
        document = self._require_document()
        tile_size = document.active_layer.pixels.tile_size
        image = QImage(
            tile_size,
            tile_size,
            QImage.Format.Format_RGBA8888_Premultiplied,
        )
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.translate(-coord.x * tile_size, -coord.y * tile_size)
        self._paint_layers(painter, self.document_rect())
        painter.end()
        return image

    def has_pixel_preview(self, layer_id: UUID, coord: TileCoord) -> bool:
        return (
            layer_id == self._pixel_preview_layer_id
            and coord in self._pixel_preview_tiles
        )

    def pixel_preview_coords(self, layer_id: UUID) -> frozenset[TileCoord]:
        if layer_id != self._pixel_preview_layer_id:
            return frozenset()
        return frozenset(self._pixel_preview_tiles)

    def _legacy_document_rect(self) -> QRectF:
        """Kept separate so compatibility tests cannot affect frame geometry."""
        document = self._require_document()
        document_rect = QRectF(
            0.0,
            0.0,
            float(document.width),
            float(document.height),
        )
        return document_rect

    def paint_transform_preview(self, painter: QPainter, exposed: QRectF) -> bool:
        """Compatibility helper for render tests during the P6R migration."""
        document = self.document
        if document is None or self._transform_preview is None:
            return False
        document_rect = self._legacy_document_rect()
        painter.save()
        painter.setClipRect(exposed)
        self._paint_layers(painter, document_rect)
        painter.restore()
        return True

    def _paint_layers(self, painter: QPainter, document_rect: QRectF) -> None:
        geometry = self._geometry_preview
        if geometry is not None:
            if geometry.layer_id is None:
                transform = self._qt_transform(geometry.matrix)
                inverse, invertible = transform.inverted()
                painter.save()
                painter.setClipRect(document_rect)
                painter.setWorldTransform(transform, combine=True)
                self._paint_document_layers(
                    painter,
                    inverse.mapRect(document_rect) if invertible else document_rect,
                )
                painter.restore()
                return
            document = self._require_document()
            painter.save()
            painter.setClipRect(document_rect)
            transform_preview = TransformPreviewState(
                geometry.layer_id,
                geometry.matrix,
                geometry.interpolation,
            )
            for group in document.nodes:
                self._paint_group(
                    painter,
                    group,
                    document_rect,
                    transform_preview=transform_preview,
                )
            painter.restore()
            return
        self._paint_document_layers(painter, document_rect)

    def _paint_document_layers(
        self,
        painter: QPainter,
        document_rect: QRectF,
    ) -> None:
        document = self._require_document()
        preview = self._transform_preview
        if preview is None:
            painter.save()
            painter.setClipRect(document_rect)
            for group in document.nodes:
                self._paint_group(painter, group, document_rect)
            painter.restore()
        else:
            try:
                document.layer_by_id(preview.layer_id)
            except ValueError:
                self._transform_preview = None
                painter.save()
                painter.setClipRect(document_rect)
                for group in document.nodes:
                    self._paint_group(painter, group, document_rect)
                painter.restore()
            else:
                painter.save()
                for group in document.nodes:
                    self._paint_group(
                        painter,
                        group,
                        document_rect,
                        transform_preview=preview,
                    )
                painter.restore()

    def _paint_group(
        self,
        painter: QPainter,
        group: RasterGroupLayer | VectorGroupLayer,
        document_rect: QRectF,
        *,
        transform_preview: TransformPreviewState | None = None,
    ) -> None:
        """Paint one top-level group with inherited group properties.

        P9C deliberately keeps groups as transparent containers: children are
        painted in their stored bottom-to-top order and inherit visibility and
        opacity without creating an intermediate surface.
        """
        if not group.visible or group.opacity <= 0.0:
            return
        if isinstance(group, RasterGroupLayer):
            for layer in group.children:
                if transform_preview is not None and layer.id == transform_preview.layer_id:
                    if self._require_document().selection.is_active:
                        self._draw_normal_layer(
                            painter,
                            layer,
                            document_rect,
                            source_kind="base",
                            opacity_multiplier=group.opacity,
                        )
                    self._draw_transformed_layer(
                        painter,
                        layer,
                        transform_preview,
                        opacity_multiplier=group.opacity,
                    )
                else:
                    self._draw_normal_layer(
                        painter,
                        layer,
                        document_rect,
                        opacity_multiplier=group.opacity,
                    )
            return
        self._paint_text_group(painter, group, opacity_multiplier=group.opacity)

    def _paint_text_group(
        self,
        painter: QPainter,
        group: VectorGroupLayer,
        *,
        opacity_multiplier: float = 1.0,
    ) -> None:
        preview = self._vector_text_preview
        if preview is not None and getattr(group, "id", None) == preview.group_id:
            paint_text_objects(
                painter,
                preview.objects,
                group_opacity=float(getattr(group, "opacity", 1.0))
                * opacity_multiplier,
            )
            return
        paint_text_objects(
            painter,
            group.children,
            group_opacity=float(getattr(group, "opacity", 1.0))
            * opacity_multiplier,
        )

    def _draw_normal_layer(
        self,
        painter: QPainter,
        layer: RasterLayer,
        document_rect: QRectF,
        *,
        source_kind: str = "source",
        opacity_multiplier: float = 1.0,
    ) -> None:
        if not layer.visible or layer.opacity <= 0.0:
            return
        painter.save()
        painter.setClipRect(document_rect)
        painter.setOpacity(layer.opacity * opacity_multiplier)
        for coord, _pixels in layer.pixels.iter_allocated_tiles():
            source = self._source_image(layer, coord, source_kind)
            painter.drawImage(
                layer.origin.x + coord.x * layer.pixels.tile_size,
                layer.origin.y + coord.y * layer.pixels.tile_size,
                source,
            )
        if layer.id == self._pixel_preview_layer_id:
            for coord in self._pixel_preview_tiles:
                if layer.pixels.has_tile(coord.x, coord.y):
                    continue
                source = self._source_image(layer, coord, source_kind)
                painter.drawImage(
                    layer.origin.x + coord.x * layer.pixels.tile_size,
                    layer.origin.y + coord.y * layer.pixels.tile_size,
                    source,
                )
        painter.restore()

    def _draw_transformed_layer(
        self,
        painter: QPainter,
        layer: RasterLayer,
        preview: TransformPreviewState,
        *,
        opacity_multiplier: float = 1.0,
    ) -> None:
        if not layer.visible or layer.opacity <= 0.0:
            return
        transform = self._qt_transform(preview.matrix)
        painter.save()
        painter.setOpacity(layer.opacity * opacity_multiplier)
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform,
            preview.interpolation is not TransformInterpolation.NEAREST,
        )
        painter.setWorldTransform(transform, combine=True)
        kind = "floating" if self._require_document().selection.is_active else "source"
        for coord, _pixels in layer.pixels.iter_allocated_tiles():
            painter.drawImage(
                layer.origin.x + coord.x * layer.pixels.tile_size,
                layer.origin.y + coord.y * layer.pixels.tile_size,
                self._source_image(layer, coord, kind),
            )
        painter.restore()

    @staticmethod
    def _qt_transform(matrix: TransformMatrix) -> QTransform:
        projective = as_projective(matrix)
        return QTransform(
            projective.m11,
            projective.m21,
            projective.m31,
            projective.m12,
            projective.m22,
            projective.m32,
            projective.m13,
            projective.m23,
            projective.m33,
        )

    def _source_image(
        self,
        layer: RasterLayer,
        coord: TileCoord,
        kind: str,
    ) -> QImage:
        document = self._require_document()
        preview_generation = (
            self._pixel_preview_revisions.get(coord, 0)
            if layer.id == self._pixel_preview_layer_id
            and coord in self._pixel_preview_tiles
            else 0
        )
        revision = layer.pixels.tile_revision(coord.x, coord.y)
        selection_revision = document.selection.revision if kind != "source" else 0
        key = (
            layer.id,
            coord,
            revision,
            preview_generation + selection_revision,
            kind,
        )
        cached = self._source_cache.get(key)
        if cached is not None:
            self._source_cache.move_to_end(key)
            return cached
        preview = (
            self._pixel_preview_tiles.get(coord)
            if layer.id == self._pixel_preview_layer_id
            else None
        )
        pixels = (
            preview.copy(order="C")
            if preview is not None
            else layer.pixels.read_tile(coord.x, coord.y).copy(order="C")
        )
        if kind != "source":
            mask = self._selection_mask_for_layer_tile(layer, coord)
            selected_alpha = np.rint(
                pixels[..., 3].astype(np.float32)
                * mask.astype(np.float32)
                / 255.0
            ).astype(np.uint8)
            if kind == "base":
                pixels[..., 3] -= selected_alpha
                pixels[mask == 255] = 0
            elif kind == "floating":
                pixels[..., 3] = selected_alpha
                pixels[mask == 0] = 0
            else:
                raise ValueError("unknown source image kind")
        image = rgba_tile_to_qimage(pixels)
        self._source_cache[key] = image
        self._source_cache.move_to_end(key)
        self._enforce_cache_budget()
        return image

    def _selection_mask_for_layer_tile(
        self,
        layer: RasterLayer,
        coord: TileCoord,
    ) -> np.ndarray:
        document = self._require_document()
        tile_size = layer.pixels.tile_size
        mask = np.zeros((tile_size, tile_size), dtype=np.uint8)
        tile_left = layer.origin.x + coord.x * tile_size
        tile_top = layer.origin.y + coord.y * tile_size
        left = max(0, tile_left)
        top = max(0, tile_top)
        right = min(
            document.width,
            tile_left + min(tile_size, layer.size.width - coord.x * tile_size),
        )
        bottom = min(
            document.height,
            tile_top + min(tile_size, layer.size.height - coord.y * tile_size),
        )
        if right <= left or bottom <= top:
            return mask
        region = document.selection.mask.read_region(
            Rect(left, top, right - left, bottom - top)
        )
        mask[
            top - tile_top : bottom - tile_top,
            left - tile_left : right - tile_left,
        ] = region
        return mask

    def _on_document_event(self, event: DocumentEvent) -> None:
        document = self.document
        if document is None:
            return
        if isinstance(event, LayerPixelsChanged):
            self._discard_layer_cache(event.layer_id)
        elif isinstance(event, LayerListChanged):
            current_ids = set(event.layer_ids)
            for key in tuple(self._source_cache):
                if key[0] not in current_ids:
                    del self._source_cache[key]
            preview = self._transform_preview
            if preview is not None and preview.layer_id not in current_ids:
                self._transform_preview = None
            if (
                self._pixel_preview_layer_id is not None
                and self._pixel_preview_layer_id not in current_ids
            ):
                self._pixel_preview_layer_id = None
                self._pixel_preview_tiles.clear()
                self._pixel_preview_revisions.clear()
            if self._vector_text_preview is not None:
                try:
                    document.group_by_id(self._vector_text_preview.group_id)
                except ValueError:
                    self._vector_text_preview = None
        else:
            self._source_cache.clear()
        if self._vector_text_preview is not None:
            try:
                document.group_by_id(self._vector_text_preview.group_id)
            except ValueError:
                self._vector_text_preview = None
        self.request_frame()

    def _discard_layer_cache(self, layer_id: UUID) -> None:
        for key in tuple(self._source_cache):
            if key[0] == layer_id:
                del self._source_cache[key]

    def _discard_tile_cache(
        self,
        layer_id: UUID,
        coords: frozenset[TileCoord],
    ) -> None:
        for key in tuple(self._source_cache):
            if key[0] == layer_id and key[1] in coords:
                del self._source_cache[key]

    def _enforce_cache_budget(self) -> None:
        while self.cache_bytes > self.cache_budget_bytes and self._source_cache:
            self._source_cache.popitem(last=False)

    def _require_document(self) -> Document:
        if self.document is None:
            raise RuntimeError("canvas renderer has no document")
        return self.document

    @classmethod
    def _create_checker_pattern(cls) -> QPixmap:
        extent = cls.CHECKER_SIZE * 2
        pattern = QPixmap(extent, extent)
        pattern.fill(QColor("#f0f0f0"))
        painter = QPainter(pattern)
        painter.fillRect(0, 0, cls.CHECKER_SIZE, cls.CHECKER_SIZE, QColor("#cfcfcf"))
        painter.fillRect(
            cls.CHECKER_SIZE,
            cls.CHECKER_SIZE,
            cls.CHECKER_SIZE,
            cls.CHECKER_SIZE,
            QColor("#cfcfcf"),
        )
        painter.end()
        return pattern


__all__ = [
    "CanvasRenderCoordinator",
    "CanvasRenderDiagnostics",
    "DEFAULT_SOURCE_CACHE_BYTES",
    "GeometryPreviewState",
    "RenderBackendKind",
    "TransformPreviewState",
    "VectorTextPreviewState",
]
