"""Background-safe paint-bucket computation over immutable snapshots."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

import cv2
import numpy as np

from easypicture.imaging.brush import (
    PaintBlendMode,
    RGBAColor,
    blend_color,
    blend_color_behind,
)
from easypicture.imaging.reference import create_region_reference, layer_by_id
from easypicture.imaging.region import RegionQuery, RegionResult, find_region
from easypicture.persistence.snapshots import DocumentSnapshot, LayerSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, RGBAArray, TileCoord

type ProgressReporter = Callable[[int, int, str], None]


class FillCancellation(Protocol):
    def raise_if_cancelled(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FillRequest:
    snapshot: DocumentSnapshot
    layer_id: UUID
    query: RegionQuery
    color: RGBAColor
    sample_merged: bool = False
    edge_expansion: int = 0
    mode: PaintBlendMode = PaintBlendMode.NORMAL

    def __post_init__(self) -> None:
        if not 0 <= self.edge_expansion <= 8:
            raise ValueError("fill edge expansion must be between 0 and 8")


@dataclass(frozen=True, slots=True)
class FillResult:
    document_id: UUID
    revision: int
    layer_id: UUID
    updates: Mapping[TileCoord, RGBAArray]


def calculate_fill(
    request: FillRequest,
    cancellation: FillCancellation,
    progress: ProgressReporter,
) -> FillResult:
    """Find a region and return full active-layer Tile replacements."""
    snapshot = request.snapshot
    layer = _layer_by_id(snapshot, request.layer_id)
    seed_x, seed_y = request.query.seed
    if not 0 <= seed_x < snapshot.size.width or not 0 <= seed_y < snapshot.size.height:
        raise ValueError("fill seed lies outside the document")

    with tempfile.TemporaryDirectory(prefix="easypicture-fill-") as temp_dir:
        reference = create_region_reference(
            snapshot,
            layer,
            request.sample_merged,
            Path(temp_dir),
            cancellation,
            progress,
            progress_message="分析填充区域",
        )
        region = find_region(reference, request.query, cancellation)
        expanded_region = _expand_region_edges(
            region,
            reference,
            request.edge_expansion,
            cancellation,
        )
        del reference
    updates = _build_updates(
        snapshot,
        layer,
        region,
        expanded_region,
        request.color,
        request.mode,
        cancellation,
        progress,
    )
    return FillResult(
        document_id=snapshot.document_id,
        revision=snapshot.revision,
        layer_id=layer.id,
        updates=updates,
    )


def _build_updates(
    snapshot: DocumentSnapshot,
    layer: LayerSnapshot,
    region: RegionResult,
    expanded_region: RegionResult,
    color: RGBAColor,
    mode: PaintBlendMode,
    cancellation: FillCancellation,
    progress: ProgressReporter,
) -> dict[TileCoord, RGBAArray]:
    tile_size = DEFAULT_TILE_SIZE
    first_layer_x = max(0, -layer.origin.x)
    first_layer_y = max(0, -layer.origin.y)
    last_layer_x = min(layer.size.width, snapshot.size.width - layer.origin.x)
    last_layer_y = min(layer.size.height, snapshot.size.height - layer.origin.y)
    if last_layer_x <= first_layer_x or last_layer_y <= first_layer_y:
        return {}

    first_tx = first_layer_x // tile_size
    first_ty = first_layer_y // tile_size
    last_tx = (last_layer_x - 1) // tile_size
    last_ty = (last_layer_y - 1) // tile_size
    coords = [
        TileCoord(tx, ty)
        for ty in range(first_ty, last_ty + 1)
        for tx in range(first_tx, last_tx + 1)
    ]
    updates: dict[TileCoord, RGBAArray] = {}
    selection_active = snapshot.selection.has_nonzero_data
    for index, coord in enumerate(coords, start=1):
        cancellation.raise_if_cancelled()
        layer_left = coord.x * tile_size
        layer_top = coord.y * tile_size
        document_left = max(0, layer.origin.x + layer_left)
        document_top = max(0, layer.origin.y + layer_top)
        document_right = min(
            snapshot.size.width,
            layer.origin.x + min(layer.size.width, layer_left + tile_size),
        )
        document_bottom = min(
            snapshot.size.height,
            layer.origin.y + min(layer.size.height, layer_top + tile_size),
        )
        if document_right <= document_left or document_bottom <= document_top:
            continue

        core_coverage = _region_rect(
            region,
            document_left,
            document_top,
            document_right - document_left,
            document_bottom - document_top,
        ).astype(np.float32) / 255.0
        expanded_coverage = (
            core_coverage.copy()
            if expanded_region is region
            else _region_rect(
                expanded_region,
                document_left,
                document_top,
                document_right - document_left,
                document_bottom - document_top,
            ).astype(np.float32)
            / 255.0
        )
        if selection_active:
            from easypicture.pixels import Rect

            selection = snapshot.selection.read_region(
                Rect(
                    document_left,
                    document_top,
                    document_right - document_left,
                    document_bottom - document_top,
                )
            )
            selection_weight = selection.astype(np.float32) / 255.0
            core_coverage *= selection_weight
            expanded_coverage *= selection_weight
        if not np.any(expanded_coverage > 0):
            continue

        before = layer.pixels.read_tile(coord.x, coord.y)
        after = before.copy(order="C")
        target_left = document_left - (layer.origin.x + layer_left)
        target_top = document_top - (layer.origin.y + layer_top)
        target_right = target_left + core_coverage.shape[1]
        target_bottom = target_top + core_coverage.shape[0]
        target = after[target_top:target_bottom, target_left:target_right]
        target_after = blend_color(target, color, core_coverage, mode)
        fringe_coverage = np.clip(expanded_coverage - core_coverage, 0.0, 1.0)
        if np.any(fringe_coverage > 0):
            target_after = (
                blend_color_behind(target_after, color, fringe_coverage)
                if mode is PaintBlendMode.NORMAL
                else blend_color(target_after, color, fringe_coverage, mode)
            )
        if np.array_equal(target, target_after):
            continue
        after[target_top:target_bottom, target_left:target_right] = target_after
        updates[coord] = after
        progress(len(coords) + index, len(coords) * 2, "生成像素补丁")
    return updates


def _expand_region_edges(
    result: RegionResult,
    reference: RGBAArray,
    pixels: int,
    cancellation: FillCancellation,
) -> RegionResult:
    if pixels == 0:
        return result
    height, width = reference.shape[:2]
    selected = np.zeros((height, width), dtype=np.uint8)
    tile_size = DEFAULT_TILE_SIZE
    for coord, tile in result.tiles.items():
        left = coord.x * tile_size
        top = coord.y * tile_size
        valid_width = min(tile_size, width - left)
        valid_height = min(tile_size, height - top)
        selected[top : top + valid_height, left : left + valid_width] = tile[
            :valid_height,
            :valid_width,
        ]
    if np.all(selected):
        return result
    passable = reference[..., 3] < 255
    kernel = np.ones((3, 3), dtype=np.uint8)
    for _iteration in range(pixels):
        cancellation.raise_if_cancelled()
        grown = cv2.dilate(selected, kernel, iterations=1)
        next_selected = np.where(passable, grown, selected).astype(np.uint8)
        if np.array_equal(next_selected, selected):
            break
        selected = next_selected

    tiles: dict[TileCoord, np.ndarray] = {}
    for ty in range((height + tile_size - 1) // tile_size):
        for tx in range((width + tile_size - 1) // tile_size):
            left = tx * tile_size
            top = ty * tile_size
            portion = selected[
                top : min(top + tile_size, height),
                left : min(left + tile_size, width),
            ]
            if not np.any(portion):
                continue
            tile = np.zeros((tile_size, tile_size), dtype=np.uint8)
            tile[: portion.shape[0], : portion.shape[1]] = portion
            tiles[TileCoord(tx, ty)] = tile
    return RegionResult(tiles)


def _region_rect(
    result: RegionResult,
    x: int,
    y: int,
    width: int,
    height: int,
) -> np.ndarray:
    output = np.zeros((height, width), dtype=np.uint8)
    tile_size = DEFAULT_TILE_SIZE
    first_tx = x // tile_size
    first_ty = y // tile_size
    last_tx = (x + width - 1) // tile_size
    last_ty = (y + height - 1) // tile_size
    for ty in range(first_ty, last_ty + 1):
        for tx in range(first_tx, last_tx + 1):
            tile = result.tiles.get(TileCoord(tx, ty))
            if tile is None:
                continue
            tile_left = tx * tile_size
            tile_top = ty * tile_size
            overlap_left = max(x, tile_left)
            overlap_top = max(y, tile_top)
            overlap_right = min(x + width, tile_left + tile_size)
            overlap_bottom = min(y + height, tile_top + tile_size)
            output[
                overlap_top - y : overlap_bottom - y,
                overlap_left - x : overlap_right - x,
            ] = tile[
                overlap_top - tile_top : overlap_bottom - tile_top,
                overlap_left - tile_left : overlap_right - tile_left,
            ]
    return output


def _layer_by_id(snapshot: DocumentSnapshot, layer_id: UUID) -> LayerSnapshot:
    return layer_by_id(snapshot, layer_id)


__all__ = ["FillRequest", "FillResult", "calculate_fill"]
