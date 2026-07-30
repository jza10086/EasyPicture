"""Qt-free document, canvas, and layer geometry operations."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

import cv2
import numpy as np

from easypicture.domain import PixelPoint, Size
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION
from easypicture.imaging.transform import (
    TransformInterpolation,
    _cv_interpolation,
    _read_layer_pixels,
    _WorkingFiles,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence.snapshots import DocumentSnapshot, LayerSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, MaskArray, Rect, RGBAArray, TileCoord

type ProgressSink = Callable[[int, int, str], None]


@dataclass(frozen=True, slots=True)
class GeometryRect:
    """A half-open integer rectangle whose origin may lie outside the canvas."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("geometry rectangle dimensions must be positive")

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


class CanvasAnchor(Enum):
    TOP_LEFT = "top_left"
    TOP = "top"
    TOP_RIGHT = "top_right"
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM = "bottom"
    BOTTOM_RIGHT = "bottom_right"


@dataclass(frozen=True, slots=True)
class LayerGeometryResult:
    layer_id: UUID
    size: Size
    origin: PixelPoint
    tiles: Mapping[TileCoord, RGBAArray] | None = None


@dataclass(frozen=True, slots=True)
class DocumentGeometryResult:
    document_id: UUID
    source_revision: int
    document_size: Size
    layers: tuple[LayerGeometryResult, ...]
    selection_tiles: Mapping[TileCoord, MaskArray]


@dataclass(frozen=True, slots=True)
class CropDocumentRequest:
    snapshot: DocumentSnapshot
    rect: GeometryRect


@dataclass(frozen=True, slots=True)
class CanvasResizeRequest:
    snapshot: DocumentSnapshot
    size: Size
    anchor: CanvasAnchor = CanvasAnchor.CENTER


@dataclass(frozen=True, slots=True)
class ImageScaleRequest:
    snapshot: DocumentSnapshot
    size: Size
    interpolation: TransformInterpolation = TransformInterpolation.LINEAR


@dataclass(frozen=True, slots=True)
class LayerScaleRequest:
    snapshot: DocumentSnapshot
    layer_id: UUID
    size: Size
    interpolation: TransformInterpolation = TransformInterpolation.LINEAR


@dataclass(frozen=True, slots=True)
class LayerBoundsRequest:
    snapshot: DocumentSnapshot
    layer_id: UUID
    crop_to_content: bool


@dataclass(frozen=True, slots=True)
class TrimTransparentRequest:
    snapshot: DocumentSnapshot


def calculate_crop_document(
    request: CropDocumentRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    rect = request.rect
    _validate_size(Size(rect.width, rect.height))
    target_size = Size(rect.width, rect.height)
    working = _WorkingFiles()
    layers: list[LayerGeometryResult] = []
    maximum = len(request.snapshot.layers) + 1
    try:
        for index, layer in enumerate(request.snapshot.layers, start=1):
            token.raise_if_cancelled()
            source = _layer_canvas_pixels(
                layer,
                request.snapshot.size,
                token,
                working,
            )
            target = _translated_canvas(
                source,
                target_size,
                -rect.x,
                -rect.y,
                working,
            )
            layers.append(
                LayerGeometryResult(
                    layer.id,
                    target_size,
                    PixelPoint(),
                    _rgba_tiles(target),
                )
            )
            progress(index, maximum, "正在裁切图层")
        selection = _translate_selection(
            request.snapshot,
            target_size,
            -rect.x,
            -rect.y,
        )
        progress(maximum, maximum, "正在裁切选区")
        return _document_result(
            request.snapshot,
            target_size,
            tuple(layers),
            selection,
        )
    finally:
        working.cleanup()


def calculate_canvas_resize(
    request: CanvasResizeRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    _validate_size(request.size)
    offset = canvas_anchor_offset(request.snapshot.size, request.size, request.anchor)
    working = _WorkingFiles()
    layers: list[LayerGeometryResult] = []
    maximum = len(request.snapshot.layers) + 1
    try:
        for index, layer in enumerate(request.snapshot.layers, start=1):
            token.raise_if_cancelled()
            source = _layer_canvas_pixels(
                layer,
                request.snapshot.size,
                token,
                working,
            )
            target = _translated_canvas(
                source,
                request.size,
                offset.x,
                offset.y,
                working,
            )
            layers.append(
                LayerGeometryResult(
                    layer.id,
                    request.size,
                    PixelPoint(),
                    _rgba_tiles(target),
                )
            )
            progress(index, maximum, "正在调整图层大小")
        selection = _translate_selection(
            request.snapshot,
            request.size,
            offset.x,
            offset.y,
        )
        progress(maximum, maximum, "正在调整选区")
        return _document_result(
            request.snapshot,
            request.size,
            tuple(layers),
            selection,
        )
    finally:
        working.cleanup()


def calculate_image_scale(
    request: ImageScaleRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    _validate_size(request.size)
    snapshot = request.snapshot
    working = _WorkingFiles()
    results: list[LayerGeometryResult] = []
    maximum = len(snapshot.layers) + 1
    try:
        for index, layer in enumerate(snapshot.layers, start=1):
            token.raise_if_cancelled()
            source = _layer_canvas_pixels(layer, snapshot.size, token, working)
            resized = _resize_rgba(source, request.size, request.interpolation)
            results.append(
                LayerGeometryResult(
                    layer.id,
                    request.size,
                    PixelPoint(),
                    _rgba_tiles(resized),
                )
            )
            progress(index, maximum, f"正在缩放图层 {index}/{len(snapshot.layers)}")
        selection_source = snapshot.selection.read_region(
            Rect(0, 0, snapshot.size.width, snapshot.size.height)
        )
        selection = _mask_tiles(
            _resize_mask(selection_source, request.size, request.interpolation)
        )
        progress(maximum, maximum, "正在缩放选区")
        return _document_result(snapshot, request.size, tuple(results), selection)
    finally:
        working.cleanup()


def calculate_layer_scale(
    request: LayerScaleRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    _validate_size(request.size)
    snapshot = request.snapshot
    layer = _layer(snapshot, request.layer_id)
    _require_editable_layer(layer)
    working = _WorkingFiles()
    try:
        source = _layer_canvas_pixels(layer, snapshot.size, token, working)
        resized = _resize_rgba(source, request.size, request.interpolation)
        target = working.zeros(
            (snapshot.size.height, snapshot.size.width, 4),
            np.dtype(np.uint8),
        )
        width = min(snapshot.size.width, request.size.width)
        height = min(snapshot.size.height, request.size.height)
        target[:height, :width] = resized[:height, :width]
        progress(1, 1, "正在缩放图层")
        replacement = LayerGeometryResult(
            layer.id,
            snapshot.size,
            PixelPoint(),
            _rgba_tiles(target),
        )
        return _single_layer_result(snapshot, replacement)
    finally:
        working.cleanup()


def calculate_layer_bounds(
    request: LayerBoundsRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    """Normalize a legacy layer to the canvas.

    Layers no longer have independent bounds.  The operation remains as a
    compatibility entry point for older callers and becomes a no-op for
    already-normalized documents.
    """
    snapshot = request.snapshot
    layer = _layer(snapshot, request.layer_id)
    _require_editable_layer(layer)
    working = _WorkingFiles()
    try:
        source = _layer_canvas_pixels(layer, snapshot.size, token, working)
        token.raise_if_cancelled()
        replacement = LayerGeometryResult(
            layer.id,
            snapshot.size,
            PixelPoint(),
            _rgba_tiles(source),
        )
        progress(1, 1, "正在规范化图层")
        return _single_layer_result(snapshot, replacement)
    finally:
        working.cleanup()


def calculate_trim_transparent(
    request: TrimTransparentRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    """Crop the document to the union of non-transparent pixels in all layers."""

    snapshot = request.snapshot
    working = _WorkingFiles()
    bounds: tuple[int, int, int, int] | None = None
    try:
        maximum = max(1, len(snapshot.layers))
        for index, layer in enumerate(snapshot.layers, start=1):
            token.raise_if_cancelled()
            source = _layer_canvas_pixels(layer, snapshot.size, token, working)
            ys, xs = np.nonzero(source[..., 3])
            if xs.size:
                current = (
                    int(xs.min()),
                    int(ys.min()),
                    int(xs.max()) + 1,
                    int(ys.max()) + 1,
                )
                if bounds is None:
                    bounds = current
                else:
                    bounds = (
                        min(bounds[0], current[0]),
                        min(bounds[1], current[1]),
                        max(bounds[2], current[2]),
                        max(bounds[3], current[3]),
                    )
            progress(index, maximum, "正在查找透明边界")
    finally:
        working.cleanup()
    if bounds is None:
        return _document_result(
            snapshot,
            snapshot.size,
            tuple(
                LayerGeometryResult(layer.id, snapshot.size, PixelPoint())
                for layer in snapshot.layers
            ),
            {
                coord: tile
                for coord, tile in snapshot.selection.iter_allocated_tiles()
            },
        )
    left, top, right, bottom = bounds
    if left == 0 and top == 0 and right == snapshot.size.width and bottom == snapshot.size.height:
        return _document_result(
            snapshot,
            snapshot.size,
            tuple(
                LayerGeometryResult(layer.id, snapshot.size, PixelPoint())
                for layer in snapshot.layers
            ),
            {
                coord: tile
                for coord, tile in snapshot.selection.iter_allocated_tiles()
            },
        )
    return calculate_crop_document(
        CropDocumentRequest(
            snapshot,
            GeometryRect(left, top, right - left, bottom - top),
        ),
        token,
        progress,
    )


def canvas_anchor_offset(old: Size, new: Size, anchor: CanvasAnchor) -> PixelPoint:
    delta_x = new.width - old.width
    delta_y = new.height - old.height
    horizontal = {
        CanvasAnchor.TOP_LEFT: 0,
        CanvasAnchor.LEFT: 0,
        CanvasAnchor.BOTTOM_LEFT: 0,
        CanvasAnchor.TOP: math.floor(delta_x / 2),
        CanvasAnchor.CENTER: math.floor(delta_x / 2),
        CanvasAnchor.BOTTOM: math.floor(delta_x / 2),
        CanvasAnchor.TOP_RIGHT: delta_x,
        CanvasAnchor.RIGHT: delta_x,
        CanvasAnchor.BOTTOM_RIGHT: delta_x,
    }[anchor]
    vertical = {
        CanvasAnchor.TOP_LEFT: 0,
        CanvasAnchor.TOP: 0,
        CanvasAnchor.TOP_RIGHT: 0,
        CanvasAnchor.LEFT: math.floor(delta_y / 2),
        CanvasAnchor.CENTER: math.floor(delta_y / 2),
        CanvasAnchor.RIGHT: math.floor(delta_y / 2),
        CanvasAnchor.BOTTOM_LEFT: delta_y,
        CanvasAnchor.BOTTOM: delta_y,
        CanvasAnchor.BOTTOM_RIGHT: delta_y,
    }[anchor]
    return PixelPoint(horizontal, vertical)


def selection_bounds(snapshot: DocumentSnapshot) -> GeometryRect | None:
    """Return the nonzero selection bounds in document coordinates."""

    if not snapshot.selection.has_nonzero_data:
        return None
    mask = snapshot.selection.read_region(
        Rect(0, 0, snapshot.size.width, snapshot.size.height)
    )
    ys, xs = np.nonzero(mask)
    if not xs.size:
        return None
    left = int(xs.min())
    top = int(ys.min())
    return GeometryRect(
        left,
        top,
        int(xs.max()) + 1 - left,
        int(ys.max()) + 1 - top,
    )


def _document_result(
    snapshot: DocumentSnapshot,
    size: Size,
    layers: tuple[LayerGeometryResult, ...],
    selection: Mapping[TileCoord, MaskArray],
) -> DocumentGeometryResult:
    return DocumentGeometryResult(
        snapshot.document_id,
        snapshot.revision,
        size,
        layers,
        selection,
    )


def _single_layer_result(
    snapshot: DocumentSnapshot,
    replacement: LayerGeometryResult,
) -> DocumentGeometryResult:
    layers = tuple(
        replacement
        if layer.id == replacement.layer_id
        else LayerGeometryResult(layer.id, snapshot.size, PixelPoint())
        for layer in snapshot.layers
    )
    selection = {
        coord: tile
        for coord, tile in snapshot.selection.iter_allocated_tiles()
    }
    return _document_result(snapshot, snapshot.size, layers, selection)


def _layer_canvas_pixels(
    layer: LayerSnapshot,
    canvas_size: Size,
    token: CancellationToken,
    working: _WorkingFiles,
) -> RGBAArray:
    source = _read_layer_pixels(layer, token, working)
    if layer.size == canvas_size and layer.origin == PixelPoint():
        return source
    target = working.zeros(
        (canvas_size.height, canvas_size.width, 4),
        np.dtype(np.uint8),
    )
    source_left = max(0, -layer.origin.x)
    source_top = max(0, -layer.origin.y)
    target_left = max(0, layer.origin.x)
    target_top = max(0, layer.origin.y)
    width = min(layer.size.width - source_left, canvas_size.width - target_left)
    height = min(layer.size.height - source_top, canvas_size.height - target_top)
    if width > 0 and height > 0:
        target[
            target_top : target_top + height,
            target_left : target_left + width,
        ] = source[
            source_top : source_top + height,
            source_left : source_left + width,
        ]
    return target


def _translated_canvas(
    source: RGBAArray,
    size: Size,
    offset_x: int,
    offset_y: int,
    working: _WorkingFiles,
) -> RGBAArray:
    target = working.zeros(
        (size.height, size.width, 4),
        np.dtype(np.uint8),
    )
    source_left = max(0, -offset_x)
    source_top = max(0, -offset_y)
    target_left = max(0, offset_x)
    target_top = max(0, offset_y)
    width = min(source.shape[1] - source_left, size.width - target_left)
    height = min(source.shape[0] - source_top, size.height - target_top)
    if width > 0 and height > 0:
        target[
            target_top : target_top + height,
            target_left : target_left + width,
        ] = source[
            source_top : source_top + height,
            source_left : source_left + width,
        ]
    return target


def _translate_selection(
    snapshot: DocumentSnapshot,
    size: Size,
    offset_x: int,
    offset_y: int,
) -> Mapping[TileCoord, MaskArray]:
    source = snapshot.selection.read_region(
        Rect(0, 0, snapshot.size.width, snapshot.size.height)
    )
    target = np.zeros((size.height, size.width), dtype=np.uint8)
    source_left = max(0, -offset_x)
    source_top = max(0, -offset_y)
    target_left = max(0, offset_x)
    target_top = max(0, offset_y)
    width = min(source.shape[1] - source_left, size.width - target_left)
    height = min(source.shape[0] - source_top, size.height - target_top)
    if width > 0 and height > 0:
        target[
            target_top : target_top + height,
            target_left : target_left + width,
        ] = source[
            source_top : source_top + height,
            source_left : source_left + width,
        ]
    return _mask_tiles(target)


def _resize_rgba(
    source: RGBAArray,
    size: Size,
    interpolation: TransformInterpolation,
) -> RGBAArray:
    flags = _cv_interpolation(interpolation)
    if interpolation is TransformInterpolation.NEAREST:
        return np.ascontiguousarray(
            cv2.resize(source, (size.width, size.height), interpolation=flags)
        )
    straight = source.astype(np.float32) / 255.0
    alpha = straight[..., 3:4]
    premultiplied = np.concatenate((straight[..., :3] * alpha, alpha), axis=2)
    resized = cv2.resize(
        premultiplied,
        (size.width, size.height),
        interpolation=flags,
    )
    hidden = cv2.resize(
        straight[..., :3],
        (size.width, size.height),
        interpolation=flags,
    )
    if resized.ndim == 2:
        resized = resized[..., None]
    if hidden.ndim == 2:
        hidden = hidden[..., None]
    output_alpha = np.clip(resized[..., 3:4], 0.0, 1.0)
    output_rgb = np.where(
        output_alpha > 1e-7,
        resized[..., :3] / np.maximum(output_alpha, 1e-7),
        hidden,
    )
    return np.ascontiguousarray(
        np.rint(
            np.concatenate(
                (np.clip(output_rgb, 0.0, 1.0), output_alpha),
                axis=2,
            )
            * 255.0
        ).astype(np.uint8)
    )


def _resize_mask(
    source: MaskArray,
    size: Size,
    interpolation: TransformInterpolation,
) -> MaskArray:
    return np.ascontiguousarray(
        cv2.resize(
            source,
            (size.width, size.height),
            interpolation=_cv_interpolation(interpolation),
        ).astype(np.uint8)
    )


def _rgba_tiles(source: RGBAArray) -> Mapping[TileCoord, RGBAArray]:
    result: dict[TileCoord, RGBAArray] = {}
    for top in range(0, source.shape[0], DEFAULT_TILE_SIZE):
        for left in range(0, source.shape[1], DEFAULT_TILE_SIZE):
            height = min(DEFAULT_TILE_SIZE, source.shape[0] - top)
            width = min(DEFAULT_TILE_SIZE, source.shape[1] - left)
            tile = np.zeros(
                (DEFAULT_TILE_SIZE, DEFAULT_TILE_SIZE, 4),
                dtype=np.uint8,
            )
            tile[:height, :width] = source[top : top + height, left : left + width]
            if np.any(tile):
                result[TileCoord(left // DEFAULT_TILE_SIZE, top // DEFAULT_TILE_SIZE)] = tile
    return result


def _mask_tiles(source: MaskArray) -> Mapping[TileCoord, MaskArray]:
    result: dict[TileCoord, MaskArray] = {}
    for top in range(0, source.shape[0], DEFAULT_TILE_SIZE):
        for left in range(0, source.shape[1], DEFAULT_TILE_SIZE):
            height = min(DEFAULT_TILE_SIZE, source.shape[0] - top)
            width = min(DEFAULT_TILE_SIZE, source.shape[1] - left)
            tile = np.zeros((DEFAULT_TILE_SIZE, DEFAULT_TILE_SIZE), dtype=np.uint8)
            tile[:height, :width] = source[top : top + height, left : left + width]
            if np.any(tile):
                result[TileCoord(left // DEFAULT_TILE_SIZE, top // DEFAULT_TILE_SIZE)] = tile
    return result


def _layer(snapshot: DocumentSnapshot, layer_id: UUID) -> LayerSnapshot:
    for layer in snapshot.layers:
        if layer.id == layer_id:
            return layer
    raise ValueError("layer must belong to the snapshot")


def _require_editable_layer(layer: LayerSnapshot) -> None:
    if layer.locked:
        raise ValueError("locked layers cannot be resized")
    if layer.alpha_locked:
        raise ValueError("alpha-locked layers cannot be resized")


def _validate_size(size: Size) -> None:
    if (
        size.width > MAX_DOCUMENT_DIMENSION
        or size.height > MAX_DOCUMENT_DIMENSION
    ):
        raise ValueError("geometry dimensions exceed the project limit")


__all__ = [
    "CanvasAnchor",
    "CanvasResizeRequest",
    "CropDocumentRequest",
    "DocumentGeometryResult",
    "GeometryRect",
    "ImageScaleRequest",
    "LayerBoundsRequest",
    "LayerGeometryResult",
    "LayerScaleRequest",
    "TrimTransparentRequest",
    "calculate_canvas_resize",
    "calculate_crop_document",
    "calculate_image_scale",
    "calculate_layer_bounds",
    "calculate_layer_scale",
    "calculate_trim_transparent",
    "canvas_anchor_offset",
    "selection_bounds",
]
