"""Qt-free selection mask algebra and background operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

import cv2
import numpy as np

from easypicture.domain import Size
from easypicture.imaging.reference import create_region_reference, layer_by_id
from easypicture.imaging.region import RegionQuery, find_region
from easypicture.persistence.snapshots import DocumentSnapshot
from easypicture.pixels import (
    DEFAULT_TILE_SIZE,
    MaskArray,
    MaskTileStore,
    Rect,
    TileCoord,
)
from easypicture.temp_files import owned_temporary_directory

type ProgressReporter = Callable[[int, int, str], None]
BACKGROUND_SELECTION_PIXEL_THRESHOLD = 16_000_000


class SelectionCancellation(Protocol):
    def raise_if_cancelled(self) -> None: ...


class SelectionCombineMode(StrEnum):
    REPLACE = "replace"
    ADD = "add"
    SUBTRACT = "subtract"
    INTERSECT = "intersect"


class SelectionAdjustment(StrEnum):
    FEATHER = "feather"
    GROW = "grow"
    SHRINK = "shrink"
    SMOOTH = "smooth"


class SelectionBulkOperation(StrEnum):
    RECTANGLE = "rectangle"
    SELECT_ALL = "select_all"
    INVERT = "invert"


@dataclass(frozen=True, slots=True)
class MagicSelectionRequest:
    snapshot: DocumentSnapshot
    layer_id: UUID
    query: RegionQuery
    mode: SelectionCombineMode = SelectionCombineMode.REPLACE
    sample_merged: bool = False


@dataclass(frozen=True, slots=True)
class SelectionAdjustmentRequest:
    snapshot: DocumentSnapshot
    operation: SelectionAdjustment
    radius: int

    def __post_init__(self) -> None:
        maximum = 1024 if self.operation in {
            SelectionAdjustment.GROW,
            SelectionAdjustment.SHRINK,
        } else 256
        if not 1 <= self.radius <= maximum:
            raise ValueError(f"selection adjustment radius must be between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class SelectionBulkRequest:
    snapshot: DocumentSnapshot
    operation: SelectionBulkOperation
    mode: SelectionCombineMode = SelectionCombineMode.REPLACE
    rect: Rect | None = None

    def __post_init__(self) -> None:
        if self.operation is SelectionBulkOperation.RECTANGLE and self.rect is None:
            raise ValueError("rectangle selection requires a rectangle")


@dataclass(frozen=True, slots=True)
class SelectionOperationResult:
    document_id: UUID
    revision: int
    updates: Mapping[TileCoord, MaskArray | None]


def combine_selection_tiles(
    current: MaskTileStore,
    candidate: Mapping[TileCoord, MaskArray],
    size: Size,
    mode: SelectionCombineMode,
) -> dict[TileCoord, MaskArray | None]:
    """Return exact full-Tile replacements for one fuzzy-set operation."""
    current_coords = {coord for coord, _tile in current.iter_allocated_tiles()}
    candidate_coords = set(candidate)
    if mode is SelectionCombineMode.REPLACE:
        coords = current_coords | candidate_coords
    elif mode is SelectionCombineMode.ADD:
        coords = current_coords | candidate_coords
    elif mode is SelectionCombineMode.SUBTRACT:
        coords = current_coords & candidate_coords
    else:
        coords = current_coords | candidate_coords

    tile_size = current.tile_size
    updates: dict[TileCoord, MaskArray | None] = {}
    zero = np.zeros((tile_size, tile_size), dtype=np.uint8)
    for coord in coords:
        before = current.read_tile(coord.x, coord.y)
        proposed = candidate.get(coord, zero)
        if mode is SelectionCombineMode.REPLACE:
            after = proposed
        elif mode is SelectionCombineMode.ADD:
            after = np.maximum(before, proposed)
        elif mode is SelectionCombineMode.SUBTRACT:
            after = np.minimum(before, 255 - proposed)
        else:
            after = np.minimum(before, proposed)
        normalized = _clip_mask_tile(after, coord, size, tile_size)
        if np.array_equal(before, normalized):
            continue
        updates[coord] = normalized if np.any(normalized) else None
    return updates


def rectangle_candidate(size: Size, rect: Rect) -> dict[TileCoord, MaskArray]:
    clipped_left = min(size.width, rect.x)
    clipped_top = min(size.height, rect.y)
    clipped_right = min(size.width, rect.right)
    clipped_bottom = min(size.height, rect.bottom)
    if clipped_right <= clipped_left or clipped_bottom <= clipped_top:
        return {}
    tile_size = DEFAULT_TILE_SIZE
    result: dict[TileCoord, MaskArray] = {}
    first_tx = clipped_left // tile_size
    first_ty = clipped_top // tile_size
    last_tx = (clipped_right - 1) // tile_size
    last_ty = (clipped_bottom - 1) // tile_size
    for ty in range(first_ty, last_ty + 1):
        for tx in range(first_tx, last_tx + 1):
            tile_left = tx * tile_size
            tile_top = ty * tile_size
            left = max(clipped_left, tile_left) - tile_left
            top = max(clipped_top, tile_top) - tile_top
            right = min(clipped_right, tile_left + tile_size) - tile_left
            bottom = min(clipped_bottom, tile_top + tile_size) - tile_top
            tile = np.zeros((tile_size, tile_size), dtype=np.uint8)
            tile[top:bottom, left:right] = 255
            result[TileCoord(tx, ty)] = tile
    return result


def select_all_candidate(size: Size) -> dict[TileCoord, MaskArray]:
    return rectangle_candidate(size, Rect(0, 0, size.width, size.height))


def invert_selection_tiles(
    current: MaskTileStore,
    size: Size,
) -> dict[TileCoord, MaskArray | None]:
    tile_size = current.tile_size
    columns = (size.width + tile_size - 1) // tile_size
    rows = (size.height + tile_size - 1) // tile_size
    updates: dict[TileCoord, MaskArray | None] = {}
    for ty in range(rows):
        for tx in range(columns):
            coord = TileCoord(tx, ty)
            before = current.read_tile(tx, ty)
            after = _clip_mask_tile(255 - before, coord, size, tile_size)
            if np.array_equal(before, after):
                continue
            updates[coord] = after if np.any(after) else None
    return updates


def clear_selection_tiles(current: MaskTileStore) -> dict[TileCoord, None]:
    return {coord: None for coord, _tile in current.iter_allocated_tiles()}


def calculate_magic_selection(
    request: MagicSelectionRequest,
    cancellation: SelectionCancellation,
    progress: ProgressReporter,
) -> SelectionOperationResult:
    snapshot = request.snapshot
    layer = layer_by_id(snapshot, request.layer_id)
    with owned_temporary_directory(prefix="wand-") as temp_dir:
        reference = create_region_reference(
            snapshot,
            layer,
            request.sample_merged,
            Path(temp_dir),
            cancellation,
            progress,
            progress_message="分析魔棒区域",
        )
        region = find_region(reference, request.query, cancellation)
        del reference
    updates = combine_selection_tiles(
        snapshot.selection,
        region.tiles,
        snapshot.size,
        request.mode,
    )
    return SelectionOperationResult(snapshot.document_id, snapshot.revision, updates)


def calculate_bulk_selection(
    request: SelectionBulkRequest,
    cancellation: SelectionCancellation,
    progress: ProgressReporter,
) -> SelectionOperationResult:
    snapshot = request.snapshot
    cancellation.raise_if_cancelled()
    progress(0, 1, "生成选区")
    if request.operation is SelectionBulkOperation.RECTANGLE:
        if request.rect is None:
            raise ValueError("rectangle selection requires a rectangle")
        candidate = rectangle_candidate(snapshot.size, request.rect)
        updates = combine_selection_tiles(
            snapshot.selection,
            candidate,
            snapshot.size,
            request.mode,
        )
    elif request.operation is SelectionBulkOperation.SELECT_ALL:
        updates = combine_selection_tiles(
            snapshot.selection,
            select_all_candidate(snapshot.size),
            snapshot.size,
            SelectionCombineMode.REPLACE,
        )
    else:
        updates = invert_selection_tiles(snapshot.selection, snapshot.size)
    cancellation.raise_if_cancelled()
    progress(1, 1, "生成选区")
    return SelectionOperationResult(snapshot.document_id, snapshot.revision, updates)


def calculate_selection_adjustment(
    request: SelectionAdjustmentRequest,
    cancellation: SelectionCancellation,
    progress: ProgressReporter,
) -> SelectionOperationResult:
    snapshot = request.snapshot
    with owned_temporary_directory(prefix="selection-") as temp_dir:
        source = _materialize_selection(
            snapshot.selection,
            snapshot.size,
            Path(temp_dir),
            cancellation,
            progress,
        )
        cancellation.raise_if_cancelled()
        radius = request.radius
        if request.operation is SelectionAdjustment.FEATHER:
            sigma = max(radius / 3.0, 0.5)
            adjusted = cv2.GaussianBlur(
                source,
                (radius * 2 + 1, radius * 2 + 1),
                sigmaX=sigma,
                sigmaY=sigma,
                borderType=cv2.BORDER_CONSTANT,
            )
        else:
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE,
                (radius * 2 + 1, radius * 2 + 1),
            )
            if request.operation is SelectionAdjustment.GROW:
                adjusted = cv2.dilate(
                    source,
                    kernel,
                    borderType=cv2.BORDER_CONSTANT,
                    borderValue=0,
                )
            elif request.operation is SelectionAdjustment.SHRINK:
                adjusted = cv2.erode(
                    source,
                    kernel,
                    borderType=cv2.BORDER_CONSTANT,
                    borderValue=0,
                )
            else:
                closed = cv2.morphologyEx(
                    source,
                    cv2.MORPH_CLOSE,
                    kernel,
                    borderType=cv2.BORDER_CONSTANT,
                    borderValue=0,
                )
                adjusted = cv2.morphologyEx(
                    closed,
                    cv2.MORPH_OPEN,
                    kernel,
                    borderType=cv2.BORDER_CONSTANT,
                    borderValue=0,
                )
        cancellation.raise_if_cancelled()
        adjusted_mask: MaskArray = np.ascontiguousarray(adjusted, dtype=np.uint8)
        candidate = _array_to_tiles(
            adjusted_mask,
            snapshot.size,
            cancellation,
            progress,
        )
    updates = combine_selection_tiles(
        snapshot.selection,
        candidate,
        snapshot.size,
        SelectionCombineMode.REPLACE,
    )
    return SelectionOperationResult(snapshot.document_id, snapshot.revision, updates)


def _materialize_selection(
    store: MaskTileStore,
    size: Size,
    temp_dir: Path,
    cancellation: SelectionCancellation,
    progress: ProgressReporter,
) -> MaskArray:
    byte_count = size.width * size.height
    if byte_count >= 64 * 1024 * 1024:
        output: MaskArray = np.memmap(
            temp_dir / "selection.mask",
            dtype=np.uint8,
            mode="w+",
            shape=(size.height, size.width),
        )
        output[:] = 0
    else:
        output = np.zeros((size.height, size.width), dtype=np.uint8)
    allocated = tuple(store.iter_allocated_tiles())
    total = max(1, len(allocated))
    for index, (coord, tile) in enumerate(allocated, start=1):
        cancellation.raise_if_cancelled()
        left = coord.x * store.tile_size
        top = coord.y * store.tile_size
        width = min(store.tile_size, size.width - left)
        height = min(store.tile_size, size.height - top)
        output[top : top + height, left : left + width] = tile[:height, :width]
        progress(index, total * 2, "读取选区")
    return output


def _array_to_tiles(
    pixels: MaskArray,
    size: Size,
    cancellation: SelectionCancellation,
    progress: ProgressReporter,
) -> dict[TileCoord, MaskArray]:
    tile_size = DEFAULT_TILE_SIZE
    columns = (size.width + tile_size - 1) // tile_size
    rows = (size.height + tile_size - 1) // tile_size
    total = columns * rows
    tiles: dict[TileCoord, MaskArray] = {}
    for index, coord in enumerate(
        (TileCoord(tx, ty) for ty in range(rows) for tx in range(columns)),
        start=1,
    ):
        cancellation.raise_if_cancelled()
        left = coord.x * tile_size
        top = coord.y * tile_size
        width = min(tile_size, size.width - left)
        height = min(tile_size, size.height - top)
        portion = pixels[top : top + height, left : left + width]
        if np.any(portion):
            tile = np.zeros((tile_size, tile_size), dtype=np.uint8)
            tile[:height, :width] = portion
            tiles[coord] = tile
        progress(total + index, total * 2, "生成选区")
    return tiles


def _clip_mask_tile(
    pixels: MaskArray,
    coord: TileCoord,
    size: Size,
    tile_size: int,
) -> MaskArray:
    result = np.ascontiguousarray(pixels, dtype=np.uint8)
    valid_width = min(tile_size, size.width - coord.x * tile_size)
    valid_height = min(tile_size, size.height - coord.y * tile_size)
    if valid_width < tile_size:
        result[:, valid_width:] = 0
    if valid_height < tile_size:
        result[valid_height:, :] = 0
    return result


__all__ = [
    "MagicSelectionRequest",
    "BACKGROUND_SELECTION_PIXEL_THRESHOLD",
    "SelectionAdjustment",
    "SelectionAdjustmentRequest",
    "SelectionBulkOperation",
    "SelectionBulkRequest",
    "SelectionCombineMode",
    "SelectionOperationResult",
    "calculate_magic_selection",
    "calculate_bulk_selection",
    "calculate_selection_adjustment",
    "clear_selection_tiles",
    "combine_selection_tiles",
    "invert_selection_tiles",
    "rectangle_candidate",
    "select_all_candidate",
]
