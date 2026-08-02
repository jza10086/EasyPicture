"""Prepare external document content for atomic append into another document."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import numpy as np

from easypicture.domain import (
    Document,
    PixelPoint,
    RasterGroupLayer,
    RasterLayer,
    VectorGroupLayer,
)
from easypicture.pixels import DEFAULT_TILE_SIZE, TilePixelStore


@dataclass(frozen=True, slots=True)
class DocumentAppendPayload:
    groups: tuple[RasterGroupLayer | VectorGroupLayer, ...]
    warnings: tuple[str, ...] = ()


def prepare_document_append(
    source: Document,
    target: Document,
    *,
    include_empty_raster_groups: bool = True,
) -> DocumentAppendPayload:
    """Clone source groups with new identities and target-canvas raster geometry."""
    used_names = {
        group.name for group in target.nodes
    } | {
        child.name for group in target.nodes for child in group.children
    }
    groups: list[RasterGroupLayer | VectorGroupLayer] = []
    for source_group in source.nodes:
        if (
            not include_empty_raster_groups
            and isinstance(source_group, RasterGroupLayer)
            and not source_group.children
        ):
            continue
        group_name = _unique_name(source_group.name, used_names)
        used_names.add(group_name)
        if isinstance(source_group, RasterGroupLayer):
            children: list[RasterLayer] = []
            for source_layer in source_group.children:
                child_name = _unique_name(source_layer.name, used_names)
                used_names.add(child_name)
                children.append(
                    _clone_raster_layer_for_canvas(source_layer, target, child_name)
                )
            groups.append(
                RasterGroupLayer(
                    id=uuid4(),
                    name=group_name,
                    children=children,
                    visible=source_group.visible,
                    opacity=source_group.opacity,
                    locked=source_group.locked,
                )
            )
        else:
            vector_children = []
            for source_child in source_group.children:
                child_name = _unique_name(source_child.name, used_names)
                used_names.add(child_name)
                vector_children.append(source_child.duplicate(child_name))
            groups.append(
                VectorGroupLayer(
                    id=uuid4(),
                    name=group_name,
                    children=vector_children,
                    visible=source_group.visible,
                    opacity=source_group.opacity,
                    locked=source_group.locked,
                )
            )

    warnings: tuple[str, ...] = ()
    if source.size != target.size:
        warnings = (
            "追加内容的画布尺寸为 "
            f"{source.width} × {source.height}，已按左上角对齐到当前 "
            f"{target.width} × {target.height} 画布；超出部分已裁切。",
        )
    return DocumentAppendPayload(tuple(groups), warnings)


def _clone_raster_layer_for_canvas(
    source: RasterLayer,
    target: Document,
    name: str,
) -> RasterLayer:
    if source.pixels.tile_size != DEFAULT_TILE_SIZE:
        raise ValueError("appended raster layer uses an unsupported tile size")
    if source.size == target.size and source.origin == PixelPoint():
        duplicate = source.duplicate(name)
        duplicate.size = target.size
        duplicate.origin = PixelPoint()
        return duplicate

    overlap_width = min(source.size.width, target.width)
    overlap_height = min(source.size.height, target.height)
    store = TilePixelStore(DEFAULT_TILE_SIZE)
    for coord, source_tile in source.pixels.iter_allocated_tiles():
        left = coord.x * DEFAULT_TILE_SIZE
        top = coord.y * DEFAULT_TILE_SIZE
        if left >= overlap_width or top >= overlap_height:
            continue
        valid_width = min(DEFAULT_TILE_SIZE, overlap_width - left)
        valid_height = min(DEFAULT_TILE_SIZE, overlap_height - top)
        tile = np.zeros_like(source_tile, order="C")
        tile[:valid_height, :valid_width] = source_tile[:valid_height, :valid_width]
        if np.any(tile):
            store.replace_tile(coord.x, coord.y, tile)
    return RasterLayer(
        id=uuid4(),
        name=name,
        size=target.size,
        origin=PixelPoint(),
        pixels=store,
        visible=source.visible,
        opacity=source.opacity,
        locked=source.locked,
        alpha_locked=source.alpha_locked,
    )


def _unique_name(requested: str, existing: set[str]) -> str:
    normalized = requested.strip()
    if normalized not in existing:
        return normalized
    suffix = 1
    while f"{normalized}.{suffix}" in existing:
        suffix += 1
    return f"{normalized}.{suffix}"


__all__ = ["DocumentAppendPayload", "prepare_document_append"]
