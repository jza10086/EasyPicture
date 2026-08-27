"""Authoritative sprite-frame region rendering and PNG export."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import numpy as np
from PIL import Image

from easypicture.domain import (
    SpriteFrameContentMode,
    onion_neighbor_indices,
)
from easypicture.imaging import compose_document_tile
from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import ProgressSink
from easypicture.persistence.image_buffers import (
    flush_file,
    srgb_profile_bytes,
    temporary_sibling,
)
from easypicture.persistence.snapshots import DocumentSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, Rect, RGBAArray, TileCoord


@dataclass(frozen=True, slots=True)
class SpriteFrameExportRequest:
    snapshot: DocumentSnapshot
    frame_id: UUID
    path: Path


@dataclass(frozen=True, slots=True)
class SpriteFrameExportReceipt:
    path: Path
    document_id: UUID
    revision: int
    frame_id: UUID
    width: int
    height: int
    file_size: int


def render_snapshot_region(
    snapshot: DocumentSnapshot,
    region: Rect,
    token: CancellationToken,
    progress: ProgressSink,
) -> RGBAArray:
    """Render a canvas region from authoritative visible leaves."""
    if (
        region.x < 0
        or region.y < 0
        or region.right > snapshot.size.width
        or region.bottom > snapshot.size.height
    ):
        raise ValueError("sprite frame region lies outside the canvas")
    output = np.zeros((region.height, region.width, 4), dtype=np.uint8)
    first_tx = region.x // DEFAULT_TILE_SIZE
    first_ty = region.y // DEFAULT_TILE_SIZE
    last_tx = (region.right - 1) // DEFAULT_TILE_SIZE
    last_ty = (region.bottom - 1) // DEFAULT_TILE_SIZE
    total = (last_tx - first_tx + 1) * (last_ty - first_ty + 1)
    completed = 0
    for ty in range(first_ty, last_ty + 1):
        for tx in range(first_tx, last_tx + 1):
            token.raise_if_cancelled()
            tile_left = tx * DEFAULT_TILE_SIZE
            tile_top = ty * DEFAULT_TILE_SIZE
            overlap_left = max(region.x, tile_left)
            overlap_top = max(region.y, tile_top)
            overlap_right = min(region.right, tile_left + DEFAULT_TILE_SIZE)
            overlap_bottom = min(region.bottom, tile_top + DEFAULT_TILE_SIZE)
            tile = compose_document_tile(snapshot, TileCoord(tx, ty))
            output[
                overlap_top - region.y : overlap_bottom - region.y,
                overlap_left - region.x : overlap_right - region.x,
            ] = tile[
                overlap_top - tile_top : overlap_bottom - tile_top,
                overlap_left - tile_left : overlap_right - tile_left,
            ]
            completed += 1
            progress(completed, total, "合成精灵帧")
    return output


def render_sprite_frame_pixels(
    snapshot: DocumentSnapshot,
    frame_id: UUID,
    token: CancellationToken,
    progress: ProgressSink,
) -> RGBAArray:
    workspace = snapshot.sprite_workspace
    if workspace is None:
        raise ValueError("document snapshot does not contain a sprite workspace")
    frame = next((item for item in workspace.frames if item.id == frame_id), None)
    if frame is None:
        raise ValueError("sprite frame does not exist")
    if frame.content_mode is SpriteFrameContentMode.LINKED:
        return render_snapshot_region(snapshot, frame.region, token, progress)
    embedded = next(
        (
            item
            for item in snapshot.embedded_documents
            if item.document_id == frame.embedded_document_id
        ),
        None,
    )
    if embedded is None:
        raise ValueError("detached sprite frame document is missing")
    return render_snapshot_region(
        embedded,
        Rect(0, 0, frame.region.width, frame.region.height),
        token,
        progress,
    )


def _source_over(destination: RGBAArray, source: RGBAArray) -> None:
    src = source.astype(np.float32) / 255.0
    dst = destination.astype(np.float32) / 255.0
    src_alpha = src[..., 3:4]
    dst_alpha = dst[..., 3:4]
    out_alpha = src_alpha + dst_alpha * (1.0 - src_alpha)
    numerator = src[..., :3] * src_alpha + dst[..., :3] * dst_alpha * (
        1.0 - src_alpha
    )
    out_rgb = np.divide(
        numerator,
        out_alpha,
        out=np.zeros_like(numerator),
        where=out_alpha > 0,
    )
    destination[..., :3] = np.rint(np.clip(out_rgb, 0.0, 1.0) * 255.0).astype(
        np.uint8
    )
    destination[..., 3] = np.rint(
        np.clip(out_alpha[..., 0], 0.0, 1.0) * 255.0
    ).astype(np.uint8)


def _onion_layer(
    source: RGBAArray,
    *,
    current: RGBAArray,
    color: tuple[int, int, int, int],
    opacity: float,
    size: tuple[int, int],
) -> RGBAArray:
    width, height = size
    layer = np.zeros((height, width, 4), dtype=np.uint8)
    left = 0
    top = 0
    right = min(width, source.shape[1])
    bottom = min(height, source.shape[0])
    if right <= left or bottom <= top:
        return layer
    source_slice = source[:bottom, :right]
    current_slice = current[:bottom, :right]
    target = layer[top:bottom, left:right]
    target[..., :3] = color[:3]
    alpha_scale = np.clip(opacity, 0.0, 1.0) * (color[3] / 255.0)
    changed = np.any(source_slice != current_slice, axis=2)
    target[..., 3] = np.where(
        changed,
        np.rint(source_slice[..., 3] * alpha_scale),
        0,
    ).astype(np.uint8)
    return layer


def _draw_pivot_marker(
    pixels: RGBAArray,
    pivot: tuple[int, int],
    radius: int,
) -> None:
    if radius <= 0 or pixels.size == 0:
        return
    height, width = pixels.shape[:2]
    resolved_radius = min(max(int(radius), 1), min(width, height))
    pivot_x = min(max(int(pivot[0]), 0), width - 1)
    pivot_y = min(max(int(pivot[1]), 0), height - 1)
    left = max(0, pivot_x - resolved_radius)
    right = min(width, pivot_x + resolved_radius + 1)
    top = max(0, pivot_y - resolved_radius)
    bottom = min(height, pivot_y + resolved_radius + 1)
    yy, xx = np.ogrid[top:bottom, left:right]
    mask = (xx - pivot_x) ** 2 + (yy - pivot_y) ** 2 <= resolved_radius**2
    target = pixels[top:bottom, left:right]
    target[mask] = (255, 0, 0, 255)


def render_animation_preview(
    snapshot: DocumentSnapshot,
    animation_id: UUID,
    current_index: int,
    direction: int,
    token: CancellationToken,
    progress: ProgressSink,
    *,
    previous_count: int = 0,
    next_count: int = 0,
    opacity: float = 0.35,
    falloff: float = 0.60,
    previous_color: tuple[int, int, int, int] = (255, 64, 64, 255),
    next_color: tuple[int, int, int, int] = (64, 128, 255, 255),
    pivot_radius: int = 3,
) -> RGBAArray:
    workspace = snapshot.sprite_workspace
    if workspace is None:
        raise ValueError("animation preview requires a project snapshot")
    animation = next(
        (item for item in workspace.animations if item.id == animation_id),
        None,
    )
    if animation is None or not animation.frame_ids:
        raise ValueError("sprite animation is empty or missing")
    index = min(max(current_index, 0), len(animation.frame_ids) - 1)
    by_id = {frame.id: frame for frame in workspace.frames}
    current = by_id[animation.frame_ids[index]]
    output = np.zeros(
        (workspace.cell_size.height, workspace.cell_size.width, 4),
        dtype=np.uint8,
    )
    previous, following = onion_neighbor_indices(
        animation,
        index,
        direction,
        previous_count,
        next_count,
    )
    layers = [
        (neighbor, previous_color, opacity * (falloff ** distance))
        for distance, neighbor in reversed(tuple(enumerate(previous, start=0)))
    ]
    layers += [
        (neighbor, next_color, opacity * (falloff ** distance))
        for distance, neighbor in reversed(tuple(enumerate(following, start=0)))
    ]
    current_pixels = render_sprite_frame_pixels(
        snapshot,
        current.id,
        token,
        lambda _done, _total, _label: None,
    )
    _source_over(output, current_pixels)
    total = len(layers) + 1
    for completed, (neighbor_index, color, layer_opacity) in enumerate(layers, start=1):
        token.raise_if_cancelled()
        neighbor = by_id[animation.frame_ids[neighbor_index]]
        pixels = render_sprite_frame_pixels(
            snapshot,
            neighbor.id,
            token,
            lambda _done, _total, _label: None,
        )
        layer = _onion_layer(
            pixels,
            current=current_pixels,
            color=color,
            opacity=layer_opacity,
            size=(workspace.cell_size.width, workspace.cell_size.height),
        )
        _source_over(output, layer)
        progress(completed, total, "合成洋葱皮")
    _draw_pivot_marker(output, (current.pivot.x, current.pivot.y), pivot_radius)
    progress(total, total, "合成动画帧")
    return output


def render_frame_reference_preview(
    snapshot: DocumentSnapshot,
    frame_id: UUID,
    token: CancellationToken,
    progress: ProgressSink,
    *,
    pivot_radius: int = 3,
) -> RGBAArray:
    workspace = snapshot.sprite_workspace
    if workspace is None:
        raise ValueError("frame reference preview requires a project snapshot")
    frame = next((item for item in workspace.frames if item.id == frame_id), None)
    if frame is None:
        raise ValueError("frame reference preview does not exist")
    pixels = render_sprite_frame_pixels(snapshot, frame_id, token, progress)
    width = workspace.cell_size.width
    height = workspace.cell_size.height
    output = np.zeros((height, width, 4), dtype=np.uint8)
    left = 0
    top = 0
    right = min(width, pixels.shape[1])
    bottom = min(height, pixels.shape[0])
    if right <= left or bottom <= top:
        return output
    output[top:bottom, left:right] = pixels[:bottom, :right]
    _draw_pivot_marker(output, (frame.pivot.x, frame.pivot.y), pivot_radius)
    return output


def export_sprite_frame_png(
    request: SpriteFrameExportRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> SpriteFrameExportReceipt:
    workspace = request.snapshot.sprite_workspace
    if workspace is None:
        raise ValueError("document snapshot does not contain a sprite workspace")
    frame = next(
        (candidate for candidate in workspace.frames if candidate.id == request.frame_id),
        None,
    )
    if frame is None:
        raise ValueError("sprite frame does not exist")
    destination = Path(request.path).with_suffix(".png")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_sibling(destination)
    try:
        pixels = render_sprite_frame_pixels(
            request.snapshot,
            frame.id,
            token,
            progress,
        )
        token.raise_if_cancelled()
        image = Image.fromarray(pixels, "RGBA")
        image.info.clear()
        image.save(
            temporary,
            format="PNG",
            compress_level=6,
            icc_profile=srgb_profile_bytes(),
        )
        token.raise_if_cancelled()
        flush_file(temporary)
        with Image.open(temporary) as reopened:
            reopened.load()
            if reopened.format != "PNG" or reopened.size != (
                frame.region.width,
                frame.region.height,
            ):
                raise ValueError("exported sprite frame failed validation")
            if not np.array_equal(np.asarray(reopened.convert("RGBA")), pixels):
                raise ValueError("exported sprite frame pixels failed validation")
        token.raise_if_cancelled()
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return SpriteFrameExportReceipt(
        path=destination,
        document_id=request.snapshot.document_id,
        revision=request.snapshot.revision,
        frame_id=frame.id,
        width=frame.region.width,
        height=frame.region.height,
        file_size=destination.stat().st_size,
    )


__all__ = [
    "SpriteFrameExportReceipt",
    "SpriteFrameExportRequest",
    "export_sprite_frame_png",
    "render_animation_preview",
    "render_frame_reference_preview",
    "render_sprite_frame_pixels",
    "render_snapshot_region",
]
