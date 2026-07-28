"""Legacy P2 compositor retained only as a CPU regression-test oracle.

The application runtime no longer imports this module; P6R rendering is owned by
CanvasRenderCoordinator and the OpenGL/software full-frame backends.
"""

from __future__ import annotations

import heapq
import math
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter, QTransform

from easypicture.domain import (
    AffineTransform,
    Document,
    DocumentEvent,
    LayerListChanged,
    LayerPixelsChanged,
    LayerPropertiesChanged,
    Point,
    RasterLayer,
    SelectionChanged,
    TransformMatrix,
    as_projective,
)
from easypicture.domain.document import Unsubscribe
from easypicture.imaging import TransformInterpolation
from easypicture.pixels import DEFAULT_TILE_SIZE, Rect, TileCoord
from easypicture.pixels.types import RGBAArray, validate_rgba_array
from easypicture.render.qimage_bridge import rgba_tile_to_qimage

DEFAULT_RENDER_CACHE_BYTES = 256 * 1024 * 1024
DEFAULT_RENDER_SLICE_MS = 8.0
type SourceCacheKey = tuple[UUID, TileCoord, int]
type TransformSourceCacheKey = tuple[UUID, TileCoord, int, int, str]
type PreviewStaticCacheKey = tuple[str, TileCoord]
type PreviewAtlasCacheKey = tuple[UUID, int, int, str, int]


@dataclass(frozen=True, slots=True)
class TransformPreviewFrame:
    layer_id: UUID
    matrix: TransformMatrix
    interpolation: TransformInterpolation
    generation: int


class TileRenderer(QObject):
    """Compose visible document Tiles and retain only derived QImage caches."""

    tile_ready = Signal(object)
    cache_invalidated = Signal(object)

    def __init__(
        self,
        document: Document,
        *,
        cache_budget_bytes: int = DEFAULT_RENDER_CACHE_BYTES,
        time_slice_ms: float = DEFAULT_RENDER_SLICE_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if cache_budget_bytes <= 0:
            raise ValueError("render cache budget must be positive")
        if time_slice_ms <= 0:
            raise ValueError("render time slice must be positive")
        self.document = document
        self.cache_budget_bytes = cache_budget_bytes
        self.time_slice_ms = time_slice_ms
        self.tile_size = DEFAULT_TILE_SIZE
        self._source_cache: OrderedDict[SourceCacheKey, QImage] = OrderedDict()
        self._composite_cache: OrderedDict[TileCoord, QImage] = OrderedDict()
        self._stale_composites: set[TileCoord] = set()
        self._pending_priority: dict[TileCoord, float] = {}
        self._pending_heap: list[tuple[float, int, TileCoord]] = []
        self._preview_layer_id: UUID | None = None
        self._preview_tiles: dict[TileCoord, RGBAArray] = {}
        self._transform_preview_layer_id: UUID | None = None
        self._transform_matrix: TransformMatrix = AffineTransform()
        self._transform_interpolation = TransformInterpolation.LINEAR
        self._transform_source_cache: OrderedDict[TransformSourceCacheKey, QImage] = (
            OrderedDict()
        )
        self._preview_static_cache: OrderedDict[PreviewStaticCacheKey, QImage] = (
            OrderedDict()
        )
        self._preview_atlas_cache: OrderedDict[PreviewAtlasCacheKey, QImage] = (
            OrderedDict()
        )
        self._transform_preview_generation = 0
        self._preview_update_pending = False
        self._serial = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._process_pending)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(16)
        self._preview_timer.timeout.connect(self._flush_preview_update)
        self._unsubscribe: Unsubscribe | None = document.subscribe(self._on_document_event)

    @property
    def cache_bytes(self) -> int:
        return (
            sum(image.sizeInBytes() for image in self._source_cache.values())
            + sum(image.sizeInBytes() for image in self._transform_source_cache.values())
            + sum(image.sizeInBytes() for image in self._preview_static_cache.values())
            + sum(image.sizeInBytes() for image in self._preview_atlas_cache.values())
            + sum(
                image.sizeInBytes() for image in self._composite_cache.values()
            )
        )

    @property
    def source_cache_count(self) -> int:
        return len(self._source_cache)

    @property
    def composite_cache_count(self) -> int:
        return len(self._composite_cache)

    @property
    def preview_static_cache_count(self) -> int:
        return len(self._preview_static_cache)

    @property
    def preview_atlas_cache_count(self) -> int:
        return len(self._preview_atlas_cache)

    @property
    def pending_count(self) -> int:
        return len(self._pending_priority)

    def dispose(self) -> None:
        self._timer.stop()
        self._preview_timer.stop()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._pending_priority.clear()
        self._pending_heap.clear()
        self.clear_cache()

    @property
    def preview_tile_count(self) -> int:
        return len(self._preview_tiles)

    def is_tile_stale(self, coord: TileCoord) -> bool:
        return coord in self._stale_composites

    @property
    def has_transform_preview(self) -> bool:
        return self._transform_preview_layer_id is not None

    @property
    def transform_preview_generation(self) -> int:
        return self._transform_preview_generation

    def transform_preview_frame(self) -> TransformPreviewFrame | None:
        layer_id = self._transform_preview_layer_id
        if layer_id is None:
            return None
        return TransformPreviewFrame(
            layer_id,
            self._transform_matrix,
            self._transform_interpolation,
            self._transform_preview_generation,
        )

    def set_preview_tiles(
        self,
        layer_id: UUID,
        updates: dict[TileCoord, RGBAArray],
    ) -> None:
        """Install non-authoritative working Tiles for an in-progress edit."""
        layer = self.document.layer_by_id(layer_id)
        if self._preview_layer_id is not None and self._preview_layer_id != layer_id:
            self.clear_preview()
        changed: set[TileCoord] = set()
        for coord, pixels in updates.items():
            validate_rgba_array(
                pixels,
                expected_width=self.tile_size,
                expected_height=self.tile_size,
            )
            self._preview_tiles[coord] = pixels
            changed.add(coord)
        self._preview_layer_id = layer_id
        self.invalidate_tiles(
            self._document_tiles_for_layer_tiles(layer, changed),
            preserve_previous=True,
        )

    def set_transform_preview(
        self,
        layer_id: UUID,
        matrix: TransformMatrix,
        interpolation: TransformInterpolation,
    ) -> None:
        """Publish the newest direct-manipulation frame without recompositing Tiles."""
        self.document.layer_by_id(layer_id)
        same_session = self._transform_preview_layer_id == layer_id
        if not same_session and self._transform_preview_layer_id is not None:
            self.clear_transform_preview()
        if self._preview_layer_id is not None:
            self.clear_preview()
        if not same_session:
            self._timer.stop()
            self._pending_priority.clear()
            self._pending_heap.clear()
            self._preview_static_cache.clear()
            self._preview_atlas_cache.clear()
        self._transform_preview_layer_id = layer_id
        self._transform_matrix = matrix
        self._transform_interpolation = interpolation
        self._transform_preview_generation += 1
        self._schedule_preview_update()

    def clear_transform_preview(self) -> None:
        if self._transform_preview_layer_id is None:
            return
        self._transform_preview_layer_id = None
        self._transform_matrix = AffineTransform()
        self._transform_source_cache.clear()
        self._preview_static_cache.clear()
        self._preview_atlas_cache.clear()
        self._transform_preview_generation += 1
        self._schedule_preview_update(immediate=True)

    def clear_preview(self) -> None:
        if self._preview_layer_id is None:
            return
        layer = self.document.layer_by_id(self._preview_layer_id)
        affected = self._document_tiles_for_layer_tiles(layer, set(self._preview_tiles))
        self._preview_layer_id = None
        self._preview_tiles.clear()
        self.invalidate_tiles(affected, preserve_previous=True)

    def cached_tile(self, coord: TileCoord) -> QImage | None:
        image = self._composite_cache.get(coord)
        if image is not None:
            self._composite_cache.move_to_end(coord)
        return image

    def render_tile(self, coord: TileCoord) -> QImage:
        """Synchronously compose one document Tile, using revisioned source cache."""
        if not self._is_document_tile(coord):
            raise ValueError("document Tile lies outside the canvas")
        cached = self.cached_tile(coord)
        if cached is not None and coord not in self._stale_composites:
            return cached

        output = QImage(
            self.tile_size,
            self.tile_size,
            QImage.Format.Format_RGBA8888_Premultiplied,
        )
        output.fill(Qt.GlobalColor.transparent)
        painter = QPainter(output)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        document_left = coord.x * self.tile_size
        document_top = coord.y * self.tile_size
        document_right = min(document_left + self.tile_size, self.document.width)
        document_bottom = min(document_top + self.tile_size, self.document.height)
        for layer in self.document.layers:
            if not layer.visible or layer.opacity <= 0.0:
                continue
            overlap = self._layer_overlap(
                layer,
                document_left,
                document_top,
                document_right,
                document_bottom,
            )
            if overlap is None:
                continue
            left, top, right, bottom = overlap
            first_tx = (left - layer.origin.x) // self.tile_size
            first_ty = (top - layer.origin.y) // self.tile_size
            last_tx = (right - 1 - layer.origin.x) // self.tile_size
            last_ty = (bottom - 1 - layer.origin.y) // self.tile_size
            painter.save()
            painter.setClipRect(
                left - document_left,
                top - document_top,
                right - left,
                bottom - top,
            )
            painter.setOpacity(layer.opacity)
            for layer_ty in range(first_ty, last_ty + 1):
                for layer_tx in range(first_tx, last_tx + 1):
                    layer_coord = TileCoord(layer_tx, layer_ty)
                    has_preview = (
                        layer.id == self._preview_layer_id
                        and layer_coord in self._preview_tiles
                    )
                    if not has_preview and not layer.pixels.has_tile(layer_tx, layer_ty):
                        continue
                    source = self._source_image(layer, layer_coord)
                    destination_x = layer.origin.x + layer_tx * self.tile_size - document_left
                    destination_y = layer.origin.y + layer_ty * self.tile_size - document_top
                    painter.drawImage(destination_x, destination_y, source)
            painter.restore()
        painter.end()

        self._composite_cache[coord] = output
        self._composite_cache.move_to_end(coord)
        self._stale_composites.discard(coord)
        self._enforce_budget()
        return output

    def request_tiles(self, coords: Iterable[TileCoord], center: QPointF) -> None:
        center_tx = center.x() / self.tile_size
        center_ty = center.y() / self.tile_size
        for coord in coords:
            if not self._is_document_tile(coord) or (
                coord in self._composite_cache and coord not in self._stale_composites
            ):
                continue
            priority = (coord.x + 0.5 - center_tx) ** 2 + (coord.y + 0.5 - center_ty) ** 2
            previous = self._pending_priority.get(coord)
            if previous is not None and previous <= priority:
                continue
            self._pending_priority[coord] = priority
            self._serial += 1
            heapq.heappush(self._pending_heap, (priority, self._serial, coord))
        if self._pending_priority and not self._timer.isActive():
            self._timer.start(0)

    def paint_transform_preview(self, painter: QPainter, exposed: QRectF) -> bool:
        """Paint one atomic preview frame directly from immutable source caches."""
        frame = self.transform_preview_frame()
        if frame is None:
            return False
        try:
            layer = self.document.layer_by_id(frame.layer_id)
            active_index = self.document.layer_index(frame.layer_id)
        except ValueError:
            return False
        visible = exposed.intersected(
            QRectF(0.0, 0.0, float(self.document.width), float(self.document.height))
        )
        if visible.isEmpty():
            return True
        coords = self._document_tiles_for_rect(visible)
        paint_below = active_index > 0 or self.document.selection.is_active
        paint_above = active_index + 1 < len(self.document.layers)
        if paint_below:
            for coord in coords:
                painter.drawImage(
                    coord.x * self.tile_size,
                    coord.y * self.tile_size,
                    self._preview_static_tile(frame, coord, "below"),
                )
        self._paint_transformed_layer(painter, frame, layer, visible)
        if paint_above:
            for coord in coords:
                painter.drawImage(
                    coord.x * self.tile_size,
                    coord.y * self.tile_size,
                    self._preview_static_tile(frame, coord, "above"),
                )
        return True

    def _schedule_preview_update(
        self,
        *,
        immediate: bool = False,
    ) -> None:
        self._preview_update_pending = True
        if immediate:
            self._preview_timer.stop()
            self._flush_preview_update()
        elif not self._preview_timer.isActive():
            self._preview_timer.start()

    def _flush_preview_update(self) -> None:
        if not self._preview_update_pending:
            return
        self._preview_update_pending = False
        self.cache_invalidated.emit(None)

    def drain_pending(self) -> None:
        """Synchronously drain queued work for deterministic tests and benchmarks."""
        self._timer.stop()
        while self._pending_priority:
            coord = self._take_pending()
            if coord is None:
                break
            self.render_tile(coord)
            self.tile_ready.emit(coord)

    def invalidate_tiles(
        self,
        coords: Iterable[TileCoord],
        *,
        preserve_previous: bool = False,
    ) -> None:
        changed = frozenset(coords)
        if preserve_previous:
            self._stale_composites.update(changed)
        else:
            for coord in changed:
                self._composite_cache.pop(coord, None)
                self._stale_composites.discard(coord)
        if changed:
            self.cache_invalidated.emit(changed)

    def invalidate_all_composites(self, *, preserve_previous: bool = False) -> None:
        if preserve_previous:
            self._stale_composites.update(self._composite_cache)
        else:
            self._composite_cache.clear()
            self._stale_composites.clear()
        self.cache_invalidated.emit(None)

    def clear_cache(self) -> None:
        self._source_cache.clear()
        self._transform_source_cache.clear()
        self._preview_static_cache.clear()
        self._preview_atlas_cache.clear()
        self._composite_cache.clear()
        self._stale_composites.clear()

    def _process_pending(self) -> None:
        started = time.perf_counter_ns()
        rendered = False
        while self._pending_priority:
            coord = self._take_pending()
            if coord is None:
                break
            self.render_tile(coord)
            self.tile_ready.emit(coord)
            rendered = True
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            if rendered and elapsed_ms >= self.time_slice_ms:
                break
        if self._pending_priority:
            self._timer.start(0)

    def _take_pending(self) -> TileCoord | None:
        while self._pending_heap:
            priority, _serial, coord = heapq.heappop(self._pending_heap)
            if self._pending_priority.get(coord) != priority:
                continue
            del self._pending_priority[coord]
            return coord
        self._pending_priority.clear()
        return None

    def _source_image(self, layer: RasterLayer, coord: TileCoord) -> QImage:
        if layer.id == self._preview_layer_id:
            preview = self._preview_tiles.get(coord)
            if preview is not None:
                return rgba_tile_to_qimage(preview)
        revision = layer.pixels.tile_revision(coord.x, coord.y)
        key = (layer.id, coord, revision)
        cached = self._source_cache.get(key)
        if cached is not None:
            self._source_cache.move_to_end(key)
            return cached
        image = rgba_tile_to_qimage(layer.pixels.read_tile(coord.x, coord.y))
        self._source_cache[key] = image
        self._source_cache.move_to_end(key)
        self._enforce_budget()
        return image

    def _preview_static_tile(
        self,
        frame: TransformPreviewFrame,
        coord: TileCoord,
        segment: str,
    ) -> QImage:
        key = (segment, coord)
        cached = self._preview_static_cache.get(key)
        if cached is not None:
            self._preview_static_cache.move_to_end(key)
            return cached
        output = QImage(
            self.tile_size,
            self.tile_size,
            QImage.Format.Format_RGBA8888_Premultiplied,
        )
        output.fill(Qt.GlobalColor.transparent)
        painter = QPainter(output)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        document_left = coord.x * self.tile_size
        document_top = coord.y * self.tile_size
        document_right = min(document_left + self.tile_size, self.document.width)
        document_bottom = min(document_top + self.tile_size, self.document.height)
        try:
            active_index = self.document.layer_index(frame.layer_id)
            active_layer = self.document.layer_by_id(frame.layer_id)
        except ValueError:
            painter.end()
            return output
        if segment == "below":
            layers = self.document.layers[:active_index]
        elif segment == "above":
            layers = self.document.layers[active_index + 1 :]
        else:
            painter.end()
            raise ValueError("preview segment must be below or above")
        for layer in layers:
            self._draw_normal_layer(
                painter,
                layer,
                document_left,
                document_top,
                document_right,
                document_bottom,
            )
        if (
            segment == "below"
            and self.document.selection.is_active
            and active_layer.visible
            and active_layer.opacity > 0.0
        ):
            self._draw_normal_layer(
                painter,
                active_layer,
                document_left,
                document_top,
                document_right,
                document_bottom,
                source_kind="base",
            )
        painter.end()
        self._preview_static_cache[key] = output
        self._preview_static_cache.move_to_end(key)
        self._enforce_budget()
        return output

    def _draw_normal_layer(
        self,
        painter: QPainter,
        layer: RasterLayer,
        document_left: int,
        document_top: int,
        document_right: int,
        document_bottom: int,
        *,
        source_kind: str | None = None,
    ) -> None:
        if not layer.visible or layer.opacity <= 0.0:
            return
        overlap = self._layer_overlap(
            layer,
            document_left,
            document_top,
            document_right,
            document_bottom,
        )
        if overlap is None:
            return
        left, top, right, bottom = overlap
        first_tx = (left - layer.origin.x) // self.tile_size
        first_ty = (top - layer.origin.y) // self.tile_size
        last_tx = (right - 1 - layer.origin.x) // self.tile_size
        last_ty = (bottom - 1 - layer.origin.y) // self.tile_size
        painter.save()
        painter.setClipRect(
            left - document_left,
            top - document_top,
            right - left,
            bottom - top,
        )
        painter.setOpacity(layer.opacity)
        for layer_ty in range(first_ty, last_ty + 1):
            for layer_tx in range(first_tx, last_tx + 1):
                coord = TileCoord(layer_tx, layer_ty)
                if not layer.pixels.has_tile(layer_tx, layer_ty):
                    continue
                source = (
                    self._source_image(layer, coord)
                    if source_kind is None
                    else self._transform_source_image(layer, coord, source_kind)
                )
                painter.drawImage(
                    layer.origin.x + layer_tx * self.tile_size - document_left,
                    layer.origin.y + layer_ty * self.tile_size - document_top,
                    source,
                )
        painter.restore()

    def _paint_transformed_layer(
        self,
        painter: QPainter,
        frame: TransformPreviewFrame,
        layer: RasterLayer,
        exposed: QRectF,
    ) -> None:
        if not layer.visible or layer.opacity <= 0.0:
            return
        lod_level = self._preview_lod_level(painter)
        try:
            inverse = frame.matrix.inverse()
            source_points = (
                inverse.map_point(Point(exposed.left(), exposed.top())),
                inverse.map_point(Point(exposed.right(), exposed.top())),
                inverse.map_point(Point(exposed.right(), exposed.bottom())),
                inverse.map_point(Point(exposed.left(), exposed.bottom())),
            )
        except ValueError:
            return
        source_left = max(layer.origin.x, math.floor(min(p.x for p in source_points)))
        source_top = max(layer.origin.y, math.floor(min(p.y for p in source_points)))
        source_right = min(
            layer.origin.x + layer.size.width,
            math.ceil(max(p.x for p in source_points)),
        )
        source_bottom = min(
            layer.origin.y + layer.size.height,
            math.ceil(max(p.y for p in source_points)),
        )
        if source_right <= source_left or source_bottom <= source_top:
            return
        first_tx = (source_left - layer.origin.x) // self.tile_size
        first_ty = (source_top - layer.origin.y) // self.tile_size
        last_tx = (source_right - 1 - layer.origin.x) // self.tile_size
        last_ty = (source_bottom - 1 - layer.origin.y) // self.tile_size
        matrix = as_projective(frame.matrix)
        transform = QTransform(
            matrix.m11,
            matrix.m21,
            matrix.m31,
            matrix.m12,
            matrix.m22,
            matrix.m32,
            matrix.m13,
            matrix.m23,
            matrix.m33,
        )
        painter.save()
        painter.setOpacity(layer.opacity)
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform,
            frame.interpolation is not TransformInterpolation.NEAREST,
        )
        painter.setWorldTransform(transform, combine=True)
        kind = "floating" if self.document.selection.is_active else None
        if lod_level > 0:
            atlas = self._preview_atlas(
                layer,
                "source" if kind is None else kind,
                lod_level,
            )
            painter.drawImage(
                QRectF(
                    float(layer.origin.x),
                    float(layer.origin.y),
                    float(layer.size.width),
                    float(layer.size.height),
                ),
                atlas,
            )
            painter.restore()
            return
        for layer_ty in range(first_ty, last_ty + 1):
            for layer_tx in range(first_tx, last_tx + 1):
                if not layer.pixels.has_tile(layer_tx, layer_ty):
                    continue
                coord = TileCoord(layer_tx, layer_ty)
                source = (
                    self._source_image(layer, coord)
                    if kind is None
                    else self._transform_source_image(layer, coord, kind)
                )
                painter.drawImage(
                    layer.origin.x + layer_tx * self.tile_size,
                    layer.origin.y + layer_ty * self.tile_size,
                    source,
                )
        painter.restore()

    @staticmethod
    def _preview_lod_level(painter: QPainter) -> int:
        transform = painter.worldTransform()
        scale_x = math.hypot(transform.m11(), transform.m12())
        scale_y = math.hypot(transform.m21(), transform.m22())
        scale = max(scale_x, scale_y, 1e-9)
        if scale >= 0.5:
            return 0
        return max(1, int(math.floor(math.log2(1.0 / scale))))

    def _preview_atlas(
        self,
        layer: RasterLayer,
        kind: str,
        level: int,
    ) -> QImage:
        while max(layer.size.width, layer.size.height) / (2**level) > 2048:
            level += 1
        key = (
            layer.id,
            layer.pixels.revision,
            self.document.selection.revision,
            kind,
            level,
        )
        cached = self._preview_atlas_cache.get(key)
        if cached is not None:
            self._preview_atlas_cache.move_to_end(key)
            return cached
        divisor = 2**level
        width = max(1, math.ceil(layer.size.width / divisor))
        height = max(1, math.ceil(layer.size.height / divisor))
        atlas = QImage(
            width,
            height,
            QImage.Format.Format_RGBA8888_Premultiplied,
        )
        atlas.fill(Qt.GlobalColor.transparent)
        atlas_painter = QPainter(atlas)
        atlas_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        atlas_painter.scale(1.0 / divisor, 1.0 / divisor)
        for coord, _pixels in layer.pixels.iter_allocated_tiles():
            source = (
                self._source_image(layer, coord)
                if kind == "source"
                else self._transform_source_image(layer, coord, kind)
            )
            atlas_painter.drawImage(
                coord.x * self.tile_size,
                coord.y * self.tile_size,
                source,
            )
        atlas_painter.end()
        self._preview_atlas_cache[key] = atlas
        self._preview_atlas_cache.move_to_end(key)
        self._enforce_budget()
        return atlas

    def _transform_source_image(
        self,
        layer: RasterLayer,
        coord: TileCoord,
        kind: str,
    ) -> QImage:
        key = (
            layer.id,
            coord,
            layer.pixels.tile_revision(coord.x, coord.y),
            self.document.selection.revision,
            kind,
        )
        cached = self._transform_source_cache.get(key)
        if cached is not None:
            self._transform_source_cache.move_to_end(key)
            return cached
        pixels = layer.pixels.read_tile(coord.x, coord.y).copy(order="C")
        mask = self._selection_mask_for_layer_tile(layer, coord)
        selected_alpha = np.rint(
            pixels[..., 3].astype(np.float32) * mask.astype(np.float32) / 255.0
        ).astype(np.uint8)
        if kind == "base":
            pixels[..., 3] -= selected_alpha
            pixels[mask == 255] = 0
        else:
            pixels[..., 3] = selected_alpha
            pixels[mask == 0] = 0
        image = rgba_tile_to_qimage(pixels)
        self._transform_source_cache[key] = image
        self._transform_source_cache.move_to_end(key)
        self._enforce_budget()
        return image

    def _selection_mask_for_layer_tile(
        self,
        layer: RasterLayer,
        coord: TileCoord,
    ) -> np.ndarray:
        mask = np.zeros((self.tile_size, self.tile_size), dtype=np.uint8)
        tile_left = layer.origin.x + coord.x * self.tile_size
        tile_top = layer.origin.y + coord.y * self.tile_size
        left = max(0, tile_left)
        top = max(0, tile_top)
        right = min(
            self.document.width,
            tile_left + min(self.tile_size, layer.size.width - coord.x * self.tile_size),
        )
        bottom = min(
            self.document.height,
            tile_top + min(self.tile_size, layer.size.height - coord.y * self.tile_size),
        )
        if right <= left or bottom <= top:
            return mask
        region = self.document.selection.mask.read_region(
            Rect(left, top, right - left, bottom - top)
        )
        mask[
            top - tile_top : bottom - tile_top,
            left - tile_left : right - tile_left,
        ] = region
        return mask

    def _on_document_event(self, event: DocumentEvent) -> None:
        if self.has_transform_preview:
            self._preview_static_cache.clear()
            self._preview_atlas_cache.clear()
        if isinstance(event, LayerPixelsChanged):
            self._transform_source_cache.clear()
            for key in tuple(self._source_cache):
                layer_id, coord, _revision = key
                if layer_id == event.layer_id and coord in event.layer_tiles:
                    del self._source_cache[key]
            self.invalidate_tiles(event.document_tiles, preserve_previous=True)
            return
        if isinstance(event, SelectionChanged):
            self._transform_source_cache.clear()
            self.invalidate_all_composites(preserve_previous=True)
            return
        if isinstance(event, LayerListChanged):
            current_ids = set(event.layer_ids)
            for key in tuple(self._source_cache):
                if key[0] not in current_ids:
                    del self._source_cache[key]
            self.invalidate_all_composites(preserve_previous=True)
            return
        if isinstance(event, LayerPropertiesChanged) and event.properties & {
            "visible",
            "opacity",
            "origin",
            "size",
        }:
            self.invalidate_all_composites(preserve_previous=True)

    def _enforce_budget(self) -> None:
        while self.cache_bytes > self.cache_budget_bytes:
            if self.has_transform_preview and self._composite_cache:
                self._composite_cache.popitem(last=False)
            elif self._source_cache:
                self._source_cache.popitem(last=False)
            elif self._transform_source_cache:
                self._transform_source_cache.popitem(last=False)
            elif self._preview_static_cache:
                self._preview_static_cache.popitem(last=False)
            elif self._preview_atlas_cache:
                self._preview_atlas_cache.popitem(last=False)
            elif self._composite_cache:
                self._composite_cache.popitem(last=False)
            else:
                break

    def _is_document_tile(self, coord: TileCoord) -> bool:
        return (
            coord.x * self.tile_size < self.document.width
            and coord.y * self.tile_size < self.document.height
        )

    def _document_tiles_for_layer_tiles(
        self,
        layer: RasterLayer,
        layer_tiles: set[TileCoord],
    ) -> frozenset[TileCoord]:
        result: set[TileCoord] = set()
        for coord in layer_tiles:
            left = max(0, layer.origin.x + coord.x * self.tile_size)
            top = max(0, layer.origin.y + coord.y * self.tile_size)
            right = min(
                self.document.width,
                layer.origin.x + min(layer.size.width, (coord.x + 1) * self.tile_size),
            )
            bottom = min(
                self.document.height,
                layer.origin.y + min(layer.size.height, (coord.y + 1) * self.tile_size),
            )
            if right <= left or bottom <= top:
                continue
            for ty in range(top // self.tile_size, (bottom - 1) // self.tile_size + 1):
                for tx in range(left // self.tile_size, (right - 1) // self.tile_size + 1):
                    result.add(TileCoord(tx, ty))
        return frozenset(result)

    def _document_tiles_for_rect(self, rect: QRectF) -> list[TileCoord]:
        if rect.isEmpty():
            return []
        left = max(0, math.floor(rect.left()))
        top = max(0, math.floor(rect.top()))
        right = min(self.document.width, math.ceil(rect.right()))
        bottom = min(self.document.height, math.ceil(rect.bottom()))
        if right <= left or bottom <= top:
            return []
        return [
            TileCoord(tx, ty)
            for ty in range(top // self.tile_size, (bottom - 1) // self.tile_size + 1)
            for tx in range(left // self.tile_size, (right - 1) // self.tile_size + 1)
        ]

    @staticmethod
    def _layer_overlap(
        layer: RasterLayer,
        document_left: int,
        document_top: int,
        document_right: int,
        document_bottom: int,
    ) -> tuple[int, int, int, int] | None:
        left = max(document_left, layer.origin.x)
        top = max(document_top, layer.origin.y)
        right = min(document_right, layer.origin.x + layer.size.width)
        bottom = min(document_bottom, layer.origin.y + layer.size.height)
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom


__all__ = [
    "DEFAULT_RENDER_CACHE_BYTES",
    "DEFAULT_RENDER_SLICE_MS",
    "TileRenderer",
]
