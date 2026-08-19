"""Authoritative sprite-frame region rendering and PNG export."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import numpy as np
from PIL import Image

from easypicture.domain import SpriteFrameContentMode
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
        if frame.content_mode is SpriteFrameContentMode.LINKED:
            pixels = render_snapshot_region(
                request.snapshot,
                frame.region,
                token,
                progress,
            )
        else:
            embedded = next(
                (
                    item
                    for item in request.snapshot.embedded_documents
                    if item.document_id == frame.embedded_document_id
                ),
                None,
            )
            if embedded is None:
                raise ValueError("detached sprite frame document is missing")
            pixels = render_snapshot_region(
                embedded,
                Rect(0, 0, frame.region.width, frame.region.height),
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
    "render_snapshot_region",
]
