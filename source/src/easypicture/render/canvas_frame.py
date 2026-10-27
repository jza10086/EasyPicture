"""Unified full-frame canvas rendering state and software reference backend."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID

import numpy as np
from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap, QTransform

from easypicture.diagnostics import performance_counters
from easypicture.domain import (
    AffineTransform,
    Collection,
    Document,
    DocumentEvent,
    DocumentNode,
    FloatRect,
    GroupListChanged,
    GroupMembersChanged,
    LayerPixelsChanged,
    RasterObject,
    Size,
    SvgObject,
    TextObject,
    TransformMatrix,
    VectorObject,
    as_projective,
)
from easypicture.domain.document import Unsubscribe
from easypicture.domain.events import (
    ActiveLayerChanged,
    DocumentChangeSet,
    DocumentModifiedChanged,
    DocumentPathChanged,
    DocumentRevisionChanged,
    DocumentSizeChanged,
    GroupPropertiesChanged,
    LayerPropertiesChanged,
    SelectionChanged,
    SpriteFrameContentChanged,
    SpriteWorkspaceChanged,
    VectorObjectChanged,
)
from easypicture.domain.layer import RasterSampling
from easypicture.imaging import TransformInterpolation
from easypicture.pixels import RGBAArray, TileCoord, TilePixelStore
from easypicture.pixels.types import validate_rgba_array
from easypicture.render.qimage_bridge import rgba_tile_to_qimage
from easypicture.render.text_renderer import paint_vector_objects

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
class VectorPreviewState:
    group_id: UUID | None
    objects: tuple[VectorObject, ...]


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
        self._vector_preview: VectorPreviewState | None = None
        self._raster_layer_preview: tuple[UUID, RasterObject] | None = None
        self._raster_preview_replaces_source = False
        self._source_cache: OrderedDict[SourceCacheKey, QImage] = OrderedDict()
        self._visible_object_ids: frozenset[UUID] | None = None
        self._opacity_preview: tuple[UUID, float] | None = None
        self._object_transform_preview: dict[UUID, AffineTransform] = {}
        self._outside_opacity = 1.0
        self._display_passes_key: tuple[object, ...] | None = None
        self._display_passes: tuple[Collection, ...] = ()
        self._batching_events = False
        self._deferred_frame = False
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
    def vector_preview_object_count(self) -> int:
        preview = self._vector_preview
        return 0 if preview is None else len(preview.objects)

    @property
    def has_transient_vector_preview(self) -> bool:
        preview = self._vector_preview
        return preview is not None and preview.group_id is None

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
        self._display_passes_key = None
        self._display_passes = ()
        self._object_transform_preview.clear()
        if self.document is not document:
            self._release_vector_content()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self.document = document
        self._opacity_preview = None
        self._raster_layer_preview = None
        self._pixel_preview_layer_id = None
        self._pixel_preview_tiles.clear()
        self._pixel_preview_revisions.clear()
        self._transform_preview = None
        self._geometry_preview = None
        self._vector_preview = None
        self._source_cache.clear()
        if document is not None:
            self._unsubscribe = document.subscribe_changes(self._on_changeset)
        self.request_frame()

    def dispose(self) -> None:
        self._display_passes_key = None
        self._display_passes = ()
        self._release_vector_content()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self.document = None
        self._opacity_preview = None
        self._raster_layer_preview = None
        self._source_cache.clear()
        self._pixel_preview_tiles.clear()
        self._pixel_preview_revisions.clear()
        self._transform_preview = None
        self._geometry_preview = None
        self._vector_preview = None

    def _release_vector_content(self) -> None:
        from easypicture.render.svg_renderer import svg_content_cache
        from easypicture.render.text_renderer import text_content_cache

        if self.document is None:
            return
        object_ids = tuple(
            child.id for group in self.document.vector_groups for child in group.children
        )
        svg_content_cache().discard(object_ids)
        text_content_cache().discard(object_ids)

    def replace_raster_layer_preview(
        self,
        source_layer_id: UUID,
        updates: Mapping[TileCoord, RGBAArray],
        *,
        replace_source: bool = False,
        preview_size: Size | None = None,
    ) -> None:
        """Atomically insert a display-only layer above its source, within its group."""
        document = self._require_document()
        source = document.layer_by_id(source_layer_id)
        layer = source.duplicate("临时像素预览")
        if preview_size is not None:
            layer.size = preview_size
            layer.transform = AffineTransform.scale(
                source.size.width / preview_size.width,
                source.size.height / preview_size.height,
            ).then(source.transform)
        if replace_source:
            layer.opacity, layer.visible = source.opacity, source.visible
        # Worker-owned immutable buffers transfer without a full image copy on the UI thread.
        layer.pixels = TilePixelStore.from_readonly_tiles(updates)
        previous = self._raster_layer_preview
        self._raster_layer_preview = (source_layer_id, layer)
        self._raster_preview_replaces_source = replace_source
        if previous is not None:
            self._discard_layer_cache(previous[1].id)
        self.request_frame()

    def clear_raster_layer_preview(self) -> None:
        if self._raster_layer_preview is not None:
            self._discard_layer_cache(self._raster_layer_preview[1].id)
            self._raster_layer_preview = None
            self.request_frame()

    def display_raster_children(self, group: Collection) -> tuple[RasterObject, ...]:
        preview = self._raster_layer_preview
        children: list[RasterObject] = []
        for layer in group.children:
            if not isinstance(layer, RasterObject):
                continue
            if not (
                preview is not None
                and layer.id == preview[0]
                and self._raster_preview_replaces_source
            ):
                children.append(self._with_opacity_preview(layer))
            if preview is not None and layer.id == preview[0]:
                children.append(preview[1])
        return tuple(children)

    def set_opacity_preview(self, object_id: UUID, opacity: float) -> None:
        self._require_document().node_by_id(object_id)
        if not 0.0 <= opacity <= 1.0:
            raise ValueError("preview opacity must be between zero and one")
        self._opacity_preview = object_id, opacity
        self.request_frame()

    def clear_opacity_preview(self) -> None:
        if self._opacity_preview is not None:
            self._opacity_preview = None
            self.request_frame()

    def set_object_transform_preview(self, matrices: Mapping[UUID, AffineTransform]) -> None:
        self._object_transform_preview = dict(matrices)
        self.request_frame()

    def clear_object_transform_preview(self) -> None:
        if self._object_transform_preview:
            self._object_transform_preview.clear()
            self.request_frame()

    def selection_bounds(self, ids: tuple[UUID, ...]) -> tuple[FloatRect, ...]:
        from easypicture.domain import walk_nodes
        from easypicture.domain.spatial import object_bounds

        document = self.document
        if document is None:
            return ()
        bounds: dict[UUID, FloatRect] = {}
        for key in ids:
            try:
                root = document.node_by_id(key)
            except ValueError:
                continue
            for node in walk_nodes((root,)):
                if isinstance(node, Collection):
                    continue
                visible = node.visible
                parent = document.parent_of(node.id)
                while parent is not None:
                    visible &= parent.visible
                    parent = document.parent_of(parent.id)
                if visible:
                    bounds[node.id] = object_bounds(self._with_opacity_preview(node))
        return tuple(bounds.values())

    def _with_opacity_preview[T: DocumentNode](
        self,
        node: T,
    ) -> T:
        preview = self._opacity_preview
        if preview is not None and node.id == preview[0]:
            node = replace(node, opacity=preview[1])
        matrix = self._object_transform_preview.get(node.id)
        if matrix is not None and not isinstance(node, Collection):
            node = replace(node, transform=matrix)
        return node

    def raster_display_transform(self, node: RasterObject) -> AffineTransform:
        """Keep raster boundary overlays aligned with the current object preview."""
        return self._with_opacity_preview(node).world_transform

    def display_groups(self) -> tuple[Collection, ...]:
        """Derived ordered render passes; the document tree remains the sole authority."""
        document = self._require_document()
        key = (
            document.versions.rendering,
            self._vector_preview,
            self._opacity_preview,
            tuple(self._object_transform_preview.items()),
        )
        if key == self._display_passes_key:
            return self._display_passes
        passes: list[Collection] = []
        preview = self._vector_preview

        def visit(node: DocumentNode, opacity: float, visible: bool, parent: UUID) -> None:
            node = self._with_opacity_preview(node)
            visible = visible and node.visible
            if isinstance(node, Collection):
                children = list(node.children)
                if preview is not None and preview.group_id == node.id:
                    replacements = {item.id: item for item in preview.objects}
                    rewritten: list[DocumentNode] = []
                    for child in children:
                        if isinstance(child, (TextObject, SvgObject)):
                            replacement = replacements.pop(child.id, None)
                            if replacement is not None:
                                rewritten.append(replacement)
                        else:
                            rewritten.append(child)
                    rewritten.extend(replacements.values())
                    children = rewritten
                for child in children:
                    visit(child, opacity * node.opacity, visible, node.id)
            else:
                passes.append(Collection(parent, "render pass", [node], visible, opacity))

        visit(document._root_group, 1.0, True, document.id)
        self._display_passes_key = key
        self._display_passes = tuple(passes)
        return self._display_passes

    def set_pixel_preview(
        self,
        layer_id: UUID,
        updates: Mapping[TileCoord, RGBAArray],
    ) -> None:
        document = self._require_document()
        layer = document.layer_by_id(layer_id)
        if self._pixel_preview_layer_id is not None and self._pixel_preview_layer_id != layer_id:
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
        self._pixel_preview_revisions = {coord: self._pixel_preview_epoch for coord in normalized}
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
        objects: tuple[VectorObject, ...],
    ) -> None:
        """Atomically replace a transient vector-object preview mapping."""
        self._require_document().vector_group_by_id(group_id)
        self._vector_preview = VectorPreviewState(group_id, tuple(objects))
        self.request_frame()

    def replace_transient_vector_preview(
        self,
        objects: tuple[VectorObject, ...],
    ) -> None:
        """Preview a future top-level vector group without mutating the document."""
        self._require_document()
        self._vector_preview = VectorPreviewState(None, tuple(objects))
        self.request_frame()

    def clear_vector_preview(self) -> None:
        if self._vector_preview is None:
            return
        self._vector_preview = None
        self.request_frame()

    def request_frame(self) -> None:
        if self._batching_events:
            self._deferred_frame = True
            return
        self._generation += 1
        performance_counters.frame_requests += 1
        self.frame_requested.emit()
        self.diagnostics_changed.emit(self.diagnostics)

    def paint_software(self, painter: QPainter, exposed_scene_rect: QRectF) -> None:
        """Paint a complete scene frame without any final-composite cache."""
        performance_counters.software_frames += 1
        self.paint_background(painter, exposed_scene_rect)
        if self.document is None:
            return
        if self._geometry_preview is None and not exposed_scene_rect.isEmpty():
            self._visible_object_ids = self.document.spatial_index.query(
                FloatRect(
                    exposed_scene_rect.x(),
                    exposed_scene_rect.y(),
                    exposed_scene_rect.width(),
                    exposed_scene_rect.height(),
                )
            )
            self._visible_object_ids |= self._object_transform_preview.keys()
        try:
            if not self.document_rect().contains(exposed_scene_rect):
                outside = QPainterPath()
                outside.addRect(exposed_scene_rect)
                inside = QPainterPath()
                inside.addRect(self.document_rect())
                painter.save()
                painter.setClipPath(outside.subtracted(inside), Qt.ClipOperation.IntersectClip)
                self._outside_opacity = 0.25
                self._paint_layers(painter, exposed_scene_rect)
                self._outside_opacity = 1.0
                painter.restore()
            self._paint_layers(painter, self.document_rect())
        finally:
            self._outside_opacity = 1.0
            self._visible_object_ids = None
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

    def paint_vector_group(
        self,
        painter: QPainter,
        group: Collection,
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
        outer = QPainterPath()
        outer.addRect(QRectF(-1e6, -1e6, 2e6, 2e6))
        inner = QPainterPath()
        inner.addRect(rect)
        painter.save()
        painter.setClipPath(outer.subtracted(inner), Qt.ClipOperation.IntersectClip)
        self._paint_vector_group(painter, group, opacity_multiplier=0.25)
        painter.restore()
        painter.save()
        painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        self._paint_vector_group(painter, group)
        painter.restore()

    def paint_transient_vector_preview(
        self,
        painter: QPainter,
        document_rect: QRectF | None = None,
    ) -> None:
        """Paint objects that will belong to a group created only on commit."""
        preview = self._vector_preview
        if self.document is None or preview is None or preview.group_id is not None:
            return
        rect = self.document_rect() if document_rect is None else document_rect
        painter.save()
        painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        paint_vector_objects(painter, preview.objects, group_opacity=1.0)
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

    def display_tile_coords(self, layer: RasterObject) -> frozenset[TileCoord]:
        coords = {coord for coord, _pixels in layer.pixels.iter_allocated_tiles()}
        if layer.id == self._pixel_preview_layer_id:
            coords.update(self._pixel_preview_tiles)
        return frozenset(coords)

    def display_tile_signature(
        self,
        layer: RasterObject,
        coord: TileCoord,
        kind: str,
    ) -> tuple[int, int, int, str]:
        document = self._require_document()
        preview_generation = (
            self._pixel_preview_revisions.get(coord, 0)
            if layer.id == self._pixel_preview_layer_id and coord in self._pixel_preview_tiles
            else 0
        )
        selection_revision = document.selection.revision if kind != "source" else 0
        return (
            layer.pixels.tile_generation(coord.x, coord.y),
            preview_generation,
            selection_revision,
            kind,
        )

    def display_tile_image(
        self,
        layer: RasterObject,
        coord: TileCoord,
        kind: str = "source",
    ) -> QImage:
        return self._source_image(layer, coord, kind)

    def has_pixel_preview(self, layer_id: UUID, coord: TileCoord) -> bool:
        return layer_id == self._pixel_preview_layer_id and coord in self._pixel_preview_tiles

    def pixel_preview_coords(self, layer_id: UUID) -> frozenset[TileCoord]:
        if layer_id != self._pixel_preview_layer_id:
            return frozenset()
        return frozenset(self._pixel_preview_tiles)

    def _paint_layers(self, painter: QPainter, document_rect: QRectF) -> None:
        geometry = self._geometry_preview
        if geometry is not None:
            if geometry.layer_id is None:
                transform = self._qt_transform(geometry.matrix)
                inverse, invertible = transform.inverted()
                painter.save()
                painter.setClipRect(document_rect, Qt.ClipOperation.IntersectClip)
                painter.setWorldTransform(transform, combine=True)
                self._paint_document_layers(
                    painter,
                    inverse.mapRect(document_rect) if invertible else document_rect,
                )
                painter.restore()
                return
            painter.save()
            painter.setClipRect(document_rect, Qt.ClipOperation.IntersectClip)
            transform_preview = TransformPreviewState(
                geometry.layer_id,
                geometry.matrix,
                geometry.interpolation,
            )
            for group in self.display_groups():
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
            painter.setClipRect(document_rect, Qt.ClipOperation.IntersectClip)
            for group in self.display_groups():
                self._paint_group(painter, group, document_rect)
            self.paint_transient_vector_preview(painter, document_rect)
            painter.restore()
        else:
            try:
                document.layer_by_id(preview.layer_id)
            except ValueError:
                self._transform_preview = None
                painter.save()
                painter.setClipRect(document_rect, Qt.ClipOperation.IntersectClip)
                for group in self.display_groups():
                    self._paint_group(painter, group, document_rect)
                self.paint_transient_vector_preview(painter, document_rect)
                painter.restore()
            else:
                painter.save()
                for group in self.display_groups():
                    self._paint_group(
                        painter,
                        group,
                        document_rect,
                        transform_preview=preview,
                    )
                self.paint_transient_vector_preview(painter, document_rect)
                painter.restore()

    def _paint_group(
        self,
        painter: QPainter,
        group: Collection,
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
        if self._outside_opacity != 1:
            group = replace(group, opacity=group.opacity * self._outside_opacity)
        if any(isinstance(child, RasterObject) for child in group.children):
            for layer in self.display_raster_children(group):
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
        self._paint_vector_group(painter, group)

    def _paint_vector_group(
        self,
        painter: QPainter,
        group: Collection,
        *,
        opacity_multiplier: float = 1.0,
    ) -> None:
        paint_vector_objects(
            painter,
            (
                self._with_opacity_preview(child)
                for child in group.children
                if isinstance(child, (TextObject, SvgObject))
                and (self._visible_object_ids is None or child.id in self._visible_object_ids)
            ),
            group_opacity=float(getattr(group, "opacity", 1.0)) * opacity_multiplier,
        )

    def _draw_normal_layer(
        self,
        painter: QPainter,
        layer: RasterObject,
        document_rect: QRectF,
        *,
        source_kind: str = "source",
        opacity_multiplier: float = 1.0,
    ) -> None:
        if not layer.visible or layer.opacity <= 0.0:
            return
        painter.save()
        painter.setClipRect(document_rect, Qt.ClipOperation.IntersectClip)
        painter.setOpacity(layer.opacity * opacity_multiplier)
        painter.setWorldTransform(self._qt_transform(layer.world_transform), combine=True)
        painter.setClipRect(
            QRectF(0, 0, layer.size.width, layer.size.height), Qt.ClipOperation.IntersectClip
        )
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, layer.sampling is RasterSampling.SMOOTH
        )
        for coord, _pixels in layer.pixels.iter_allocated_tiles():
            source = self._source_image(layer, coord, source_kind)
            painter.drawImage(
                coord.x * layer.pixels.tile_size,
                coord.y * layer.pixels.tile_size,
                source,
            )
        if layer.id == self._pixel_preview_layer_id:
            for coord in self._pixel_preview_tiles:
                if layer.pixels.has_tile(coord.x, coord.y):
                    continue
                source = self._source_image(layer, coord, source_kind)
                painter.drawImage(
                    coord.x * layer.pixels.tile_size,
                    coord.y * layer.pixels.tile_size,
                    source,
                )
        painter.restore()

    def _draw_transformed_layer(
        self,
        painter: QPainter,
        layer: RasterObject,
        preview: TransformPreviewState,
        *,
        opacity_multiplier: float = 1.0,
    ) -> None:
        if not layer.visible or layer.opacity <= 0.0:
            return
        matrix = as_projective(layer.world_transform).then(as_projective(preview.matrix))
        transform = self._qt_transform(matrix)
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
                coord.x * layer.pixels.tile_size,
                coord.y * layer.pixels.tile_size,
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
        layer: RasterObject,
        coord: TileCoord,
        kind: str,
    ) -> QImage:
        document = self._require_document()
        preview_generation = (
            self._pixel_preview_revisions.get(coord, 0)
            if layer.id == self._pixel_preview_layer_id and coord in self._pixel_preview_tiles
            else 0
        )
        revision = layer.pixels.tile_generation(coord.x, coord.y)
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
            performance_counters.source_cache_hits += 1
            self._source_cache.move_to_end(key)
            return cached
        performance_counters.source_cache_misses += 1
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
                pixels[..., 3].astype(np.float32) * mask.astype(np.float32) / 255.0
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
        layer: RasterObject,
        coord: TileCoord,
    ) -> np.ndarray:
        document = self._require_document()
        tile_size = layer.pixels.tile_size
        from easypicture.domain.coordinates import RasterMapping

        return RasterMapping(layer.world_transform, layer.size).selection_coverage(
            document.selection.mask,
            document.size,
            coord.x * tile_size,
            coord.y * tile_size,
            tile_size,
            tile_size,
            smooth=layer.sampling is RasterSampling.SMOOTH,
        )

    def _on_changeset(self, changes: DocumentChangeSet) -> None:
        removed = tuple(delta.object_id for delta in changes.bounds_changes if delta.new is None)
        if removed:
            from easypicture.render.svg_renderer import svg_content_cache
            from easypicture.render.text_renderer import text_content_cache

            svg_content_cache().discard(removed)
            text_content_cache().discard(removed)
            if self._opacity_preview is not None and self._opacity_preview[0] in removed:
                self._opacity_preview = None
        self._batching_events = True
        self._deferred_frame = False
        try:
            if not changes.events:
                # record_change() is the explicit unknown-content-change fallback.
                self._source_cache.clear()
                self.request_frame()
            for event in changes.events:
                self._on_document_event(event)
        finally:
            self._batching_events = False
        if self._deferred_frame:
            self._deferred_frame = False
            self.request_frame()

    def _on_document_event(self, event: DocumentEvent) -> None:
        document = self.document
        if document is None:
            return
        if isinstance(
            event,
            (
                DocumentModifiedChanged,
                DocumentPathChanged,
                DocumentRevisionChanged,
                ActiveLayerChanged,
                SpriteWorkspaceChanged,
                SpriteFrameContentChanged,
            ),
        ):
            return
        if isinstance(event, (LayerPropertiesChanged, GroupPropertiesChanged, VectorObjectChanged)):
            if event.properties <= {"name", "locked", "alpha_locked"}:
                return
        if isinstance(
            event,
            (LayerPixelsChanged, GroupListChanged, GroupMembersChanged, DocumentSizeChanged),
        ):
            self._raster_layer_preview = None
        if isinstance(event, LayerPixelsChanged):
            self._discard_tile_cache(event.layer_id, event.layer_tiles)
        elif isinstance(event, (GroupListChanged, GroupMembersChanged)):
            current_ids = {layer.id for layer in document.layers}
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
            if self._vector_preview is not None and self._vector_preview.group_id is not None:
                try:
                    document.vector_group_by_id(self._vector_preview.group_id)
                except ValueError:
                    self._vector_preview = None
        elif isinstance(event, DocumentSizeChanged):
            # Output bounds do not change the object's source pixels.
            for key in tuple(self._source_cache):
                if key[4] != "source":
                    del self._source_cache[key]
        elif isinstance(event, SelectionChanged):
            for key in tuple(self._source_cache):
                if key[4] != "source":
                    del self._source_cache[key]
        if self._vector_preview is not None and self._vector_preview.group_id is not None:
            try:
                document.vector_group_by_id(self._vector_preview.group_id)
            except ValueError:
                self._vector_preview = None
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
    "VectorPreviewState",
]
