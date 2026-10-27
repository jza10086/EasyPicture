"""Qt-free document, canvas, and layer geometry operations."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from uuid import UUID

import cv2
import numpy as np

from easypicture.domain import AffineTransform, PixelPoint, Size
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION
from easypicture.imaging.transform import (
    TransformInterpolation,
    _cv_interpolation,
    _read_layer_pixels,
    _WorkingFiles,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence.snapshots import (
    CollectionSnapshot,
    DocumentSnapshot,
    LayerSnapshot,
    walk_snapshot_nodes,
)
from easypicture.pixels import (
    DEFAULT_TILE_SIZE,
    MaskArray,
    Rect,
    RGBAArray,
    TileCoord,
    TilePixelStore,
)

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
    vector_offset: PixelPoint = PixelPoint()
    object_transforms: Mapping[UUID, AffineTransform] | None = None


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
class ObjectCanvasResizeRequest:
    snapshot: DocumentSnapshot
    layer_id: UUID
    size: Size
    anchor: CanvasAnchor = CanvasAnchor.TOP_LEFT


def calculate_object_canvas_resize(
    request: ObjectCanvasResizeRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    _validate_size(request.size)
    layer = _layer(request.snapshot, request.layer_id)
    _require_editable_layer(layer)
    offset = canvas_anchor_offset(layer.size, request.size, request.anchor)
    store = TilePixelStore()
    total = max(1, layer.pixels.allocated_tile_count)
    for index, (coord, tile) in enumerate(layer.pixels.iter_allocated_tiles()):
        token.raise_if_cancelled()
        x, y = coord.x * DEFAULT_TILE_SIZE + offset.x, coord.y * DEFAULT_TILE_SIZE + offset.y
        left, top = max(0, x), max(0, y)
        right = min(
            request.size.width,
            x + min(DEFAULT_TILE_SIZE, layer.size.width - coord.x * DEFAULT_TILE_SIZE),
        )
        bottom = min(
            request.size.height,
            y + min(DEFAULT_TILE_SIZE, layer.size.height - coord.y * DEFAULT_TILE_SIZE),
        )
        if right > left and bottom > top:
            store.write_region(
                Rect(left, top, right - left, bottom - top),
                np.ascontiguousarray(tile[top - y : bottom - y, left - x : right - x]),
            )
        progress(index + 1, total, "调整对象局部像素画布")
    replacement = LayerGeometryResult(
        layer.id, request.size, layer.origin, dict(store.iter_allocated_tiles())
    )
    return replace(
        _single_layer_result(request.snapshot, replacement),
        object_transforms={
            layer.id: AffineTransform.translation(-offset.x, -offset.y).then(layer.transform)
        },
    )


@dataclass(frozen=True, slots=True)
class TrimTransparentRequest:
    snapshot: DocumentSnapshot


def calculate_crop_document(
    request: CropDocumentRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    rect = request.rect
    return _resize_output_bounds(
        request.snapshot,
        Size(rect.width, rect.height),
        PixelPoint(-rect.x, -rect.y),
        token,
        progress,
    )


def calculate_canvas_resize(
    request: CanvasResizeRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    return _resize_output_bounds(
        request.snapshot,
        request.size,
        canvas_anchor_offset(request.snapshot.size, request.size, request.anchor),
        token,
        progress,
    )


def _resize_output_bounds(
    snapshot: DocumentSnapshot,
    size: Size,
    offset: PixelPoint,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    _validate_size(size)
    token.raise_if_cancelled()
    layers = tuple(
        LayerGeometryResult(
            layer.id,
            layer.size,
            PixelPoint(layer.origin.x + offset.x, layer.origin.y + offset.y),
        )
        for layer in snapshot.raster_layers
    )
    selection = _translate_selection(snapshot, size, offset.x, offset.y)
    token.raise_if_cancelled()
    progress(1, 1, "已调整输出范围并保留对象内容")
    return DocumentGeometryResult(
        snapshot.document_id,
        snapshot.revision,
        size,
        layers,
        selection,
        offset,
    )


def calculate_image_scale(
    request: ImageScaleRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    _validate_size(request.size)
    snapshot = request.snapshot
    token.raise_if_cancelled()
    delta = AffineTransform.scale(
        request.size.width / snapshot.size.width, request.size.height / snapshot.size.height
    )
    matrices: dict[UUID, AffineTransform] = {}
    for node in walk_snapshot_nodes(snapshot.nodes):
        if isinstance(node, CollectionSnapshot):
            continue
        matrix = node.transform.then(delta)
        if isinstance(node, LayerSnapshot):
            matrix = node.world_transform.then(delta).then(
                AffineTransform.translation(-node.origin.x, -node.origin.y)
            )
        matrices[node.id] = matrix
    selection_source = snapshot.selection.read_region(
        Rect(0, 0, snapshot.size.width, snapshot.size.height)
    )
    selection = _mask_tiles(_resize_mask(selection_source, request.size, request.interpolation))
    token.raise_if_cancelled()
    progress(1, 1, "已缩放画布和对象矩阵，保留源分辨率")
    return DocumentGeometryResult(
        snapshot.document_id,
        snapshot.revision,
        request.size,
        tuple(
            LayerGeometryResult(layer.id, layer.size, layer.origin)
            for layer in snapshot.raster_layers
        ),
        selection,
        object_transforms=matrices,
    )


def calculate_layer_scale(
    request: LayerScaleRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    _validate_size(request.size)
    snapshot = request.snapshot
    layer = _layer(snapshot, request.layer_id)
    _require_editable_layer(layer)
    token.raise_if_cancelled()
    source = layer.pixels.read_region(Rect(0, 0, layer.size.width, layer.size.height))
    resized = _resize_rgba(source, request.size, request.interpolation)
    replacement = LayerGeometryResult(layer.id, request.size, layer.origin, _rgba_tiles(resized))
    compensation = AffineTransform.scale(
        layer.size.width / request.size.width, layer.size.height / request.size.height
    )
    matrix = compensation.then(layer.transform)
    token.raise_if_cancelled()
    progress(1, 1, "已调整对象像素分辨率，保持显示尺寸")
    return replace(
        _single_layer_result(snapshot, replacement), object_transforms={layer.id: matrix}
    )


def calculate_trim_transparent(
    request: TrimTransparentRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentGeometryResult:
    """Crop the document to the union of non-transparent pixels in all layers."""

    snapshot = request.snapshot
    from easypicture.imaging.composite import compose_document_tile

    bounds: tuple[int, int, int, int] | None = None
    for ty in range((snapshot.size.height + DEFAULT_TILE_SIZE - 1) // DEFAULT_TILE_SIZE):
        for tx in range((snapshot.size.width + DEFAULT_TILE_SIZE - 1) // DEFAULT_TILE_SIZE):
            token.raise_if_cancelled()
            pixels = compose_document_tile(snapshot, TileCoord(tx, ty))
            ys, xs = np.nonzero(pixels[..., 3])
            if xs.size:
                current = (
                    int(xs.min()) + tx * DEFAULT_TILE_SIZE,
                    int(ys.min()) + ty * DEFAULT_TILE_SIZE,
                    int(xs.max()) + tx * DEFAULT_TILE_SIZE + 1,
                    int(ys.max()) + ty * DEFAULT_TILE_SIZE + 1,
                )
                bounds = (
                    current
                    if bounds is None
                    else (
                        min(bounds[0], current[0]),
                        min(bounds[1], current[1]),
                        max(bounds[2], current[2]),
                        max(bounds[3], current[3]),
                    )
                )
        progress(
            ty + 1,
            (snapshot.size.height + DEFAULT_TILE_SIZE - 1) // DEFAULT_TILE_SIZE,
            "正在查找合成输出的透明边界",
        )
    if bounds is None:
        return _document_result(
            snapshot,
            snapshot.size,
            tuple(
                LayerGeometryResult(layer.id, layer.size, layer.origin)
                for layer in snapshot.raster_layers
            ),
            {coord: tile for coord, tile in snapshot.selection.iter_allocated_tiles()},
        )
    left, top, right, bottom = bounds
    if left == 0 and top == 0 and right == snapshot.size.width and bottom == snapshot.size.height:
        return _document_result(
            snapshot,
            snapshot.size,
            tuple(
                LayerGeometryResult(layer.id, layer.size, layer.origin)
                for layer in snapshot.raster_layers
            ),
            {coord: tile for coord, tile in snapshot.selection.iter_allocated_tiles()},
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
    mask = snapshot.selection.read_region(Rect(0, 0, snapshot.size.width, snapshot.size.height))
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
        else LayerGeometryResult(layer.id, layer.size, layer.origin)
        for layer in snapshot.raster_layers
    )
    selection = {coord: tile for coord, tile in snapshot.selection.iter_allocated_tiles()}
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
    source = snapshot.selection.read_region(Rect(0, 0, snapshot.size.width, snapshot.size.height))
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
    for layer in snapshot.raster_layers:
        if layer.id == layer_id:
            return layer
    raise ValueError("layer must belong to the snapshot")


def _require_editable_layer(layer: LayerSnapshot) -> None:
    if layer.locked:
        raise ValueError("locked layers cannot be resized")
    if layer.alpha_locked:
        raise ValueError("alpha-locked layers cannot be resized")


def _validate_size(size: Size) -> None:
    if size.width > MAX_DOCUMENT_DIMENSION or size.height > MAX_DOCUMENT_DIMENSION:
        raise ValueError("geometry dimensions exceed the project limit")


__all__ = [
    "CanvasAnchor",
    "CanvasResizeRequest",
    "CropDocumentRequest",
    "DocumentGeometryResult",
    "GeometryRect",
    "ImageScaleRequest",
    "LayerGeometryResult",
    "LayerScaleRequest",
    "TrimTransparentRequest",
    "calculate_canvas_resize",
    "calculate_crop_document",
    "calculate_image_scale",
    "calculate_layer_scale",
    "calculate_trim_transparent",
    "canvas_anchor_offset",
    "selection_bounds",
]
