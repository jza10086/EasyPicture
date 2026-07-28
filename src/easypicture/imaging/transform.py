"""Qt-free affine raster transforms for layers and floating selections."""

from __future__ import annotations

import math
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from uuid import UUID

import cv2
import numpy as np

from easypicture.domain import (
    PixelPoint,
    Point,
    ProjectiveTransform,
    Size,
    TransformMatrix,
    as_projective,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence.snapshots import DocumentSnapshot, LayerSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, MaskArray, Rect, RGBAArray, TileCoord

type ProgressSink = Callable[[int, int, str], None]
MEMMAP_THRESHOLD_BYTES = 64 * 1024 * 1024


class TransformInterpolation(Enum):
    NEAREST = "nearest"
    LINEAR = "linear"
    CUBIC = "cubic"
    LANCZOS = "lanczos"


@dataclass(frozen=True, slots=True)
class RasterTransformRequest:
    snapshot: DocumentSnapshot
    layer_id: UUID
    matrix: TransformMatrix
    interpolation: TransformInterpolation = TransformInterpolation.LINEAR


@dataclass(frozen=True, slots=True)
class RasterTransformResult:
    document_id: UUID
    source_revision: int
    layer_id: UUID
    layer_size: Size
    layer_origin: PixelPoint
    layer_tiles: Mapping[TileCoord, RGBAArray]
    selection_tiles: Mapping[TileCoord, MaskArray]


@dataclass(frozen=True, slots=True)
class _PreparedRGBA:
    straight: RGBAArray | None
    premultiplied: np.ndarray | None
    hidden_rgb: np.ndarray | None


class _WorkingFiles:
    def __init__(self) -> None:
        self._directory: tempfile.TemporaryDirectory[str] | None = None
        self._serial = 0

    def zeros(
        self,
        shape: tuple[int, ...],
        dtype: np.dtype[np.uint8] | np.dtype[np.float32],
    ) -> np.ndarray:
        byte_count = math.prod(shape) * dtype.itemsize
        if byte_count <= MEMMAP_THRESHOLD_BYTES:
            return np.zeros(shape, dtype=dtype)
        if self._directory is None:
            self._directory = tempfile.TemporaryDirectory(prefix="easypicture-transform-")
        self._serial += 1
        path = Path(self._directory.name) / f"buffer-{self._serial}.bin"
        result = np.memmap(path, mode="w+", dtype=dtype, shape=shape)
        result[:] = 0
        return result

    def cleanup(self) -> None:
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


def calculate_raster_transform(
    request: RasterTransformRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> RasterTransformResult:
    """Resample one authoritative layer and its active selection exactly once."""
    working_files = _WorkingFiles()
    try:
        return _calculate_raster_transform(request, token, progress, working_files)
    finally:
        working_files.cleanup()


def _calculate_raster_transform(
    request: RasterTransformRequest,
    token: CancellationToken,
    progress: ProgressSink,
    working_files: _WorkingFiles,
) -> RasterTransformResult:
    if abs(request.matrix.determinant) <= 1e-12:
        raise ValueError("transform matrix is singular")
    layer = _layer_by_id(request.snapshot, request.layer_id)
    source = _read_layer_pixels(layer, token, working_files)
    selection_active = request.snapshot.selection.has_nonzero_data
    local_mask = _selection_for_layer(request.snapshot, layer) if selection_active else None

    if local_mask is None:
        bounds = _mapped_bounds(
            request.matrix,
            layer.origin.x,
            layer.origin.y,
            layer.size.width,
            layer.size.height,
        )
        base = None
        floating = source
    else:
        base, floating = _split_floating_pixels(source, local_mask, working_files)
        nonzero_bounds = _nonzero_bounds(local_mask)
        bounds = (
            _union_bounds(
                (
                    layer.origin.x,
                    layer.origin.y,
                    layer.origin.x + layer.size.width,
                    layer.origin.y + layer.size.height,
                ),
                _mapped_bounds(
                    request.matrix,
                    layer.origin.x + nonzero_bounds[0],
                    layer.origin.y + nonzero_bounds[1],
                    nonzero_bounds[2] - nonzero_bounds[0],
                    nonzero_bounds[3] - nonzero_bounds[1],
                ),
            )
            if nonzero_bounds is not None
            else (
                layer.origin.x,
                layer.origin.y,
                layer.origin.x + layer.size.width,
                layer.origin.y + layer.size.height,
            )
        )

    left, top, right, bottom = bounds
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        raise ValueError("transform produced an empty layer")
    if width > 8192 or height > 8192:
        raise ValueError("transformed layer exceeds the 8192 pixel dimension limit")
    if abs(left) > 32768 or abs(top) > 32768:
        raise ValueError("transformed layer origin exceeds the project limit")

    layer_tile_count = math.ceil(width / DEFAULT_TILE_SIZE) * math.ceil(
        height / DEFAULT_TILE_SIZE
    )
    selection_tile_count = (
        math.ceil(request.snapshot.size.width / DEFAULT_TILE_SIZE)
        * math.ceil(request.snapshot.size.height / DEFAULT_TILE_SIZE)
        if selection_active
        else 0
    )
    maximum = layer_tile_count + selection_tile_count
    completed = 0
    output_tiles: dict[TileCoord, RGBAArray] = {}
    prepared_floating = _prepare_rgba(
        floating,
        request.interpolation,
        working_files,
    )
    local_matrix = (
        ProjectiveTransform.translation(layer.origin.x, layer.origin.y)
        .then(as_projective(request.matrix))
        .then(ProjectiveTransform.translation(-left, -top))
    )
    for ty in range(math.ceil(height / DEFAULT_TILE_SIZE)):
        for tx in range(math.ceil(width / DEFAULT_TILE_SIZE)):
            token.raise_if_cancelled()
            tile_width = min(DEFAULT_TILE_SIZE, width - tx * DEFAULT_TILE_SIZE)
            tile_height = min(DEFAULT_TILE_SIZE, height - ty * DEFAULT_TILE_SIZE)
            transformed = _warp_rgba_tile(
                prepared_floating,
                local_matrix,
                tx * DEFAULT_TILE_SIZE,
                ty * DEFAULT_TILE_SIZE,
                tile_width,
                tile_height,
                request.interpolation,
            )
            if base is not None:
                base_tile = _copy_base_tile(
                    base,
                    layer,
                    left,
                    top,
                    tx * DEFAULT_TILE_SIZE,
                    ty * DEFAULT_TILE_SIZE,
                    tile_width,
                    tile_height,
                )
                transformed = _source_over(base_tile, transformed)
            full_tile = np.zeros(
                (DEFAULT_TILE_SIZE, DEFAULT_TILE_SIZE, 4),
                dtype=np.uint8,
            )
            full_tile[:tile_height, :tile_width] = transformed
            if np.any(full_tile):
                output_tiles[TileCoord(tx, ty)] = full_tile
            completed += 1
            progress(completed, maximum, "正在重采样图层")

    selection_tiles: dict[TileCoord, MaskArray] = {}
    if selection_active:
        selection_source = request.snapshot.selection.read_region(
            Rect(0, 0, request.snapshot.size.width, request.snapshot.size.height)
        )
        for ty in range(math.ceil(request.snapshot.size.height / DEFAULT_TILE_SIZE)):
            for tx in range(math.ceil(request.snapshot.size.width / DEFAULT_TILE_SIZE)):
                token.raise_if_cancelled()
                tile_width = min(
                    DEFAULT_TILE_SIZE,
                    request.snapshot.size.width - tx * DEFAULT_TILE_SIZE,
                )
                tile_height = min(
                    DEFAULT_TILE_SIZE,
                    request.snapshot.size.height - ty * DEFAULT_TILE_SIZE,
                )
                transformed_mask = _warp_mask_tile(
                    selection_source,
                    request.matrix,
                    tx * DEFAULT_TILE_SIZE,
                    ty * DEFAULT_TILE_SIZE,
                    tile_width,
                    tile_height,
                    request.interpolation,
                )
                full_mask = np.zeros(
                    (DEFAULT_TILE_SIZE, DEFAULT_TILE_SIZE),
                    dtype=np.uint8,
                )
                full_mask[:tile_height, :tile_width] = transformed_mask
                if np.any(full_mask):
                    selection_tiles[TileCoord(tx, ty)] = full_mask
                completed += 1
                progress(completed, maximum, "正在重采样选区")

    return RasterTransformResult(
        document_id=request.snapshot.document_id,
        source_revision=request.snapshot.revision,
        layer_id=request.layer_id,
        layer_size=Size(width, height),
        layer_origin=PixelPoint(left, top),
        layer_tiles=output_tiles,
        selection_tiles=selection_tiles,
    )


def _layer_by_id(snapshot: DocumentSnapshot, layer_id: UUID) -> LayerSnapshot:
    for layer in snapshot.layers:
        if layer.id == layer_id:
            return layer
    raise ValueError("layer must belong to the snapshot")


def _selection_for_layer(
    snapshot: DocumentSnapshot,
    layer: LayerSnapshot,
) -> MaskArray:
    mask = np.zeros((layer.size.height, layer.size.width), dtype=np.uint8)
    left = max(0, layer.origin.x)
    top = max(0, layer.origin.y)
    right = min(snapshot.size.width, layer.origin.x + layer.size.width)
    bottom = min(snapshot.size.height, layer.origin.y + layer.size.height)
    if right <= left or bottom <= top:
        return mask
    document_mask = snapshot.selection.read_region(
        Rect(left, top, right - left, bottom - top)
    )
    target_left = left - layer.origin.x
    target_top = top - layer.origin.y
    mask[
        target_top : target_top + document_mask.shape[0],
        target_left : target_left + document_mask.shape[1],
    ] = document_mask
    return mask


def _read_layer_pixels(
    layer: LayerSnapshot,
    token: CancellationToken,
    working_files: _WorkingFiles,
) -> RGBAArray:
    output = working_files.zeros(
        (layer.size.height, layer.size.width, 4),
        np.dtype(np.uint8),
    )
    for coord, tile in layer.pixels.iter_allocated_tiles():
        token.raise_if_cancelled()
        left = coord.x * DEFAULT_TILE_SIZE
        top = coord.y * DEFAULT_TILE_SIZE
        right = min(layer.size.width, left + DEFAULT_TILE_SIZE)
        bottom = min(layer.size.height, top + DEFAULT_TILE_SIZE)
        if right > left and bottom > top:
            output[top:bottom, left:right] = tile[: bottom - top, : right - left]
    return output


def _split_floating_pixels(
    source: RGBAArray,
    mask: MaskArray,
    working_files: _WorkingFiles,
) -> tuple[RGBAArray, RGBAArray]:
    selected_alpha = np.rint(
        source[..., 3].astype(np.float32) * (mask.astype(np.float32) / 255.0)
    ).astype(np.uint8)
    base = working_files.zeros(tuple(source.shape), np.dtype(np.uint8))
    floating = working_files.zeros(tuple(source.shape), np.dtype(np.uint8))
    base[:] = source
    floating[:] = source
    base[..., 3] = source[..., 3] - selected_alpha
    base[mask == 255] = 0
    floating[..., 3] = selected_alpha
    floating[mask == 0] = 0
    return base, floating


def _nonzero_bounds(mask: MaskArray) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(mask)
    if not xs.size:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _mapped_bounds(
    matrix: TransformMatrix,
    x: int,
    y: int,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    corners = (
        matrix.map_point(Point(x, y)),
        matrix.map_point(Point(x + width, y)),
        matrix.map_point(Point(x, y + height)),
        matrix.map_point(Point(x + width, y + height)),
    )
    left = min(point.x for point in corners)
    top = min(point.y for point in corners)
    right = max(point.x for point in corners)
    bottom = max(point.y for point in corners)
    return (
        math.floor(_snap_integer(left)),
        math.floor(_snap_integer(top)),
        math.ceil(_snap_integer(right)),
        math.ceil(_snap_integer(bottom)),
    )


def _snap_integer(value: float) -> float:
    nearest = round(value)
    return float(nearest) if abs(value - nearest) <= 1e-9 else value


def _union_bounds(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _cv_matrix(
    matrix: TransformMatrix,
    offset_x: int,
    offset_y: int,
) -> np.ndarray:
    adjusted = as_projective(matrix).then(
        ProjectiveTransform.translation(-offset_x, -offset_y)
    )
    return np.array(
        [
            [adjusted.m11, adjusted.m12, adjusted.m13],
            [adjusted.m21, adjusted.m22, adjusted.m23],
            [adjusted.m31, adjusted.m32, adjusted.m33],
        ],
        dtype=np.float64,
    )


def projective_from_quad(
    source: tuple[Point, Point, Point, Point],
    destination: tuple[Point, Point, Point, Point],
) -> ProjectiveTransform:
    """Map four source corners to four destination corners."""
    source_array = np.array([(point.x, point.y) for point in source], dtype=np.float32)
    destination_array = np.array(
        [(point.x, point.y) for point in destination],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(source_array, destination_array)
    result = ProjectiveTransform(*(float(value) for value in matrix.flat))
    if abs(result.determinant) <= 1e-12:
        raise ValueError("quadrilateral transform is singular")
    return result


def _cv_interpolation(interpolation: TransformInterpolation) -> int:
    return {
        TransformInterpolation.NEAREST: cv2.INTER_NEAREST,
        TransformInterpolation.LINEAR: cv2.INTER_LINEAR,
        TransformInterpolation.CUBIC: cv2.INTER_CUBIC,
        TransformInterpolation.LANCZOS: cv2.INTER_LANCZOS4,
    }[interpolation]


def _warp_rgba_tile(
    source: _PreparedRGBA,
    matrix: TransformMatrix,
    offset_x: int,
    offset_y: int,
    width: int,
    height: int,
    interpolation: TransformInterpolation,
) -> RGBAArray:
    cv_matrix = _cv_matrix(matrix, offset_x, offset_y)
    flags = _cv_interpolation(interpolation)
    if interpolation is TransformInterpolation.NEAREST:
        if source.straight is None:
            raise RuntimeError("nearest transform source is unavailable")
        return np.ascontiguousarray(
            cv2.warpPerspective(
                source.straight,
                cv_matrix,
                (width, height),
                flags=flags,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=(0, 0, 0, 0),
            )
        )

    if source.premultiplied is None or source.hidden_rgb is None:
        raise RuntimeError("interpolated transform source is unavailable")
    warped = cv2.warpPerspective(
        source.premultiplied,
        cv_matrix,
        (width, height),
        flags=flags,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )
    hidden_rgb = cv2.warpPerspective(
        source.hidden_rgb,
        cv_matrix,
        (width, height),
        flags=flags,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    warped = np.asarray(warped, dtype=np.float32)
    if warped.ndim == 2:
        warped = warped[..., None]
    output_alpha = np.clip(warped[..., 3:4], 0.0, 1.0)
    output_rgb = np.where(
        output_alpha > 1e-7,
        warped[..., :3] / np.maximum(output_alpha, 1e-7),
        hidden_rgb,
    )
    output = np.concatenate(
        (np.clip(output_rgb, 0.0, 1.0), output_alpha),
        axis=2,
    )
    return np.ascontiguousarray(np.rint(output * 255.0).astype(np.uint8))


def _prepare_rgba(
    source: RGBAArray,
    interpolation: TransformInterpolation,
    working_files: _WorkingFiles,
) -> _PreparedRGBA:
    if interpolation is TransformInterpolation.NEAREST:
        return _PreparedRGBA(source, None, None)
    straight = working_files.zeros(tuple(source.shape), np.dtype(np.float32))
    np.divide(source, 255.0, out=straight, casting="unsafe")
    alpha = straight[..., 3:4]
    premultiplied = working_files.zeros(tuple(source.shape), np.dtype(np.float32))
    premultiplied[..., :3] = straight[..., :3] * alpha
    premultiplied[..., 3:4] = alpha
    return _PreparedRGBA(None, premultiplied, straight[..., :3])


def _warp_mask_tile(
    source: MaskArray,
    matrix: TransformMatrix,
    offset_x: int,
    offset_y: int,
    width: int,
    height: int,
    interpolation: TransformInterpolation,
) -> MaskArray:
    result = cv2.warpPerspective(
        source,
        _cv_matrix(matrix, offset_x, offset_y),
        (width, height),
        flags=_cv_interpolation(interpolation),
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return np.ascontiguousarray(np.clip(result, 0, 255).astype(np.uint8))


def _copy_base_tile(
    base: RGBAArray,
    layer: LayerSnapshot,
    result_left: int,
    result_top: int,
    tile_x: int,
    tile_y: int,
    width: int,
    height: int,
) -> RGBAArray:
    output = np.zeros((height, width, 4), dtype=np.uint8)
    document_left = result_left + tile_x
    document_top = result_top + tile_y
    overlap_left = max(document_left, layer.origin.x)
    overlap_top = max(document_top, layer.origin.y)
    overlap_right = min(document_left + width, layer.origin.x + layer.size.width)
    overlap_bottom = min(document_top + height, layer.origin.y + layer.size.height)
    if overlap_right <= overlap_left or overlap_bottom <= overlap_top:
        return output
    output[
        overlap_top - document_top : overlap_bottom - document_top,
        overlap_left - document_left : overlap_right - document_left,
    ] = base[
        overlap_top - layer.origin.y : overlap_bottom - layer.origin.y,
        overlap_left - layer.origin.x : overlap_right - layer.origin.x,
    ]
    return output


def _source_over(destination: RGBAArray, source: RGBAArray) -> RGBAArray:
    destination_float = destination.astype(np.float32) / 255.0
    source_float = source.astype(np.float32) / 255.0
    source_alpha = source_float[..., 3:4]
    destination_alpha = destination_float[..., 3:4]
    output_alpha = source_alpha + destination_alpha * (1.0 - source_alpha)
    premultiplied = (
        source_float[..., :3] * source_alpha
        + destination_float[..., :3] * destination_alpha * (1.0 - source_alpha)
    )
    output_rgb = np.where(
        output_alpha > 1e-7,
        premultiplied / np.maximum(output_alpha, 1e-7),
        destination_float[..., :3],
    )
    return np.ascontiguousarray(
        np.rint(
            np.concatenate(
                (np.clip(output_rgb, 0.0, 1.0), np.clip(output_alpha, 0.0, 1.0)),
                axis=2,
            )
            * 255.0
        ).astype(np.uint8)
    )


__all__ = [
    "RasterTransformRequest",
    "RasterTransformResult",
    "TransformInterpolation",
    "calculate_raster_transform",
    "projective_from_quad",
]
