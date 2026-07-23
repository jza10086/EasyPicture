"""Revision-aware QImage Tile compositing and progressive scheduling."""

from __future__ import annotations

import heapq
import time
from collections import OrderedDict
from collections.abc import Iterable
from uuid import UUID

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter

from easypicture.domain import (
    Document,
    DocumentEvent,
    LayerListChanged,
    LayerPixelsChanged,
    LayerPropertiesChanged,
    RasterLayer,
)
from easypicture.domain.document import Unsubscribe
from easypicture.pixels import DEFAULT_TILE_SIZE, TileCoord
from easypicture.render.qimage_bridge import rgba_tile_to_qimage

DEFAULT_RENDER_CACHE_BYTES = 256 * 1024 * 1024
DEFAULT_RENDER_SLICE_MS = 8.0
type SourceCacheKey = tuple[UUID, TileCoord, int]


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
        self._pending_priority: dict[TileCoord, float] = {}
        self._pending_heap: list[tuple[float, int, TileCoord]] = []
        self._serial = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._process_pending)
        self._unsubscribe: Unsubscribe | None = document.subscribe(self._on_document_event)

    @property
    def cache_bytes(self) -> int:
        return sum(image.sizeInBytes() for image in self._source_cache.values()) + sum(
            image.sizeInBytes() for image in self._composite_cache.values()
        )

    @property
    def source_cache_count(self) -> int:
        return len(self._source_cache)

    @property
    def composite_cache_count(self) -> int:
        return len(self._composite_cache)

    @property
    def pending_count(self) -> int:
        return len(self._pending_priority)

    def dispose(self) -> None:
        self._timer.stop()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._pending_priority.clear()
        self._pending_heap.clear()
        self.clear_cache()

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
        if cached is not None:
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
                    if not layer.pixels.has_tile(layer_tx, layer_ty):
                        continue
                    source = self._source_image(layer, TileCoord(layer_tx, layer_ty))
                    destination_x = layer.origin.x + layer_tx * self.tile_size - document_left
                    destination_y = layer.origin.y + layer_ty * self.tile_size - document_top
                    painter.drawImage(destination_x, destination_y, source)
            painter.restore()
        painter.end()

        self._composite_cache[coord] = output
        self._composite_cache.move_to_end(coord)
        self._enforce_budget()
        return output

    def request_tiles(self, coords: Iterable[TileCoord], center: QPointF) -> None:
        center_tx = center.x() / self.tile_size
        center_ty = center.y() / self.tile_size
        for coord in coords:
            if not self._is_document_tile(coord) or coord in self._composite_cache:
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

    def drain_pending(self) -> None:
        """Synchronously drain queued work for deterministic tests and benchmarks."""
        self._timer.stop()
        while self._pending_priority:
            coord = self._take_pending()
            if coord is None:
                break
            self.render_tile(coord)
            self.tile_ready.emit(coord)

    def invalidate_tiles(self, coords: Iterable[TileCoord]) -> None:
        changed = frozenset(coords)
        for coord in changed:
            self._composite_cache.pop(coord, None)
        if changed:
            self.cache_invalidated.emit(changed)

    def invalidate_all_composites(self) -> None:
        self._composite_cache.clear()
        self.cache_invalidated.emit(None)

    def clear_cache(self) -> None:
        self._source_cache.clear()
        self._composite_cache.clear()

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

    def _on_document_event(self, event: DocumentEvent) -> None:
        if isinstance(event, LayerPixelsChanged):
            for key in tuple(self._source_cache):
                layer_id, coord, _revision = key
                if layer_id == event.layer_id and coord in event.layer_tiles:
                    del self._source_cache[key]
            self.invalidate_tiles(event.document_tiles)
            return
        if isinstance(event, LayerListChanged):
            current_ids = set(event.layer_ids)
            for key in tuple(self._source_cache):
                if key[0] not in current_ids:
                    del self._source_cache[key]
            self.invalidate_all_composites()
            return
        if isinstance(event, LayerPropertiesChanged) and event.properties & {
            "visible",
            "opacity",
            "origin",
        }:
            self.invalidate_all_composites()

    def _enforce_budget(self) -> None:
        while self.cache_bytes > self.cache_budget_bytes:
            if self._source_cache:
                self._source_cache.popitem(last=False)
            elif self._composite_cache:
                self._composite_cache.popitem(last=False)
            else:
                break

    def _is_document_tile(self, coord: TileCoord) -> bool:
        return (
            coord.x * self.tile_size < self.document.width
            and coord.y * self.tile_size < self.document.height
        )

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
