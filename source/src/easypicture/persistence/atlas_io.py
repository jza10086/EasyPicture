"""Authoritative animation-row sprite-atlas PNG and JSON export."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import numpy as np
from PIL import Image

from easypicture.domain import SpriteFrameContentMode
from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import ProgressSink
from easypicture.persistence.image_buffers import (
    flush_file,
    srgb_profile_bytes,
    temporary_sibling,
)
from easypicture.persistence.snapshots import DocumentSnapshot
from easypicture.persistence.sprite_io import render_snapshot_region
from easypicture.pixels import Rect


@dataclass(frozen=True, slots=True)
class SpriteAtlasExportRequest:
    snapshot: DocumentSnapshot
    path: Path
    included_animation_ids: tuple[UUID, ...] | None = None
    include_ungrouped: bool = True
    horizontal_spacing: int = 0
    vertical_spacing: int = 0
    margin: int = 0


@dataclass(frozen=True, slots=True)
class SpriteAtlasExportReceipt:
    png_path: Path
    json_path: Path
    document_id: UUID
    revision: int
    width: int
    height: int
    frame_count: int


def export_sprite_atlas(
    request: SpriteAtlasExportRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> SpriteAtlasExportReceipt:
    workspace = request.snapshot.sprite_workspace
    if workspace is None:
        raise ValueError("sprite atlas requires a project document")
    if min(request.horizontal_spacing, request.vertical_spacing, request.margin) < 0:
        raise ValueError("atlas spacing and margin cannot be negative")

    frame_by_id = {frame.id: frame for frame in workspace.frames}
    selected = (
        {animation.id for animation in workspace.animations}
        if request.included_animation_ids is None
        else set(request.included_animation_ids)
    )
    unknown = selected.difference(animation.id for animation in workspace.animations)
    if unknown:
        raise ValueError("atlas selection contains an unknown animation")
    rows: list[tuple[UUID | None, str, tuple[UUID, ...]]] = [
        (animation.id, animation.name, animation.frame_ids)
        for animation in workspace.animations
        if animation.id in selected and animation.frame_ids
    ]
    if request.include_ungrouped and workspace.ungrouped_frame_ids:
        rows.append((None, "未分组", workspace.ungrouped_frame_ids))
    if not rows:
        raise ValueError("atlas has no frames to export")

    columns = max(len(frame_ids) for _, _, frame_ids in rows)
    cell = workspace.cell_size
    width = request.margin * 2 + columns * cell.width
    width += max(columns - 1, 0) * request.horizontal_spacing
    height = request.margin * 2 + len(rows) * cell.height
    height += max(len(rows) - 1, 0) * request.vertical_spacing
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    embedded = {snapshot.document_id: snapshot for snapshot in request.snapshot.embedded_documents}
    records: list[dict[str, object]] = []
    total = sum(len(frame_ids) for _, _, frame_ids in rows)
    completed = 0
    for row_index, (animation_id, animation_name, frame_ids) in enumerate(rows):
        y = request.margin + row_index * (cell.height + request.vertical_spacing)
        for column_index, frame_id in enumerate(frame_ids):
            token.raise_if_cancelled()
            frame = frame_by_id[frame_id]
            x = request.margin + column_index * (cell.width + request.horizontal_spacing)
            if frame.content_mode is SpriteFrameContentMode.LINKED:
                frame_pixels = render_snapshot_region(
                    request.snapshot,
                    frame.region,
                    token,
                    lambda _done, _total, _label: None,
                )
            else:
                if frame.embedded_document_id is None:
                    raise ValueError("detached frame has no embedded document")
                try:
                    frame_snapshot = embedded[frame.embedded_document_id]
                except KeyError as error:
                    raise ValueError("detached frame document is missing") from error
                frame_pixels = render_snapshot_region(
                    frame_snapshot,
                    Rect(0, 0, cell.width, cell.height),
                    token,
                    lambda _done, _total, _label: None,
                )
            pixels[y : y + cell.height, x : x + cell.width] = frame_pixels
            records.append(
                {
                    "id": str(frame.id),
                    "name": frame.name,
                    "rect": [x, y, cell.width, cell.height],
                    "pivot": [frame.pivot.x, frame.pivot.y],
                    "animation_id": (str(animation_id) if animation_id is not None else None),
                    "animation": animation_name,
                    "content_mode": frame.content_mode.value,
                    "duration_ticks": frame.duration_ticks,
                }
            )
            completed += 1
            progress(completed, total, "合成精灵图")

    png_path = Path(request.path).with_suffix(".png")
    json_path = png_path.with_suffix(".json")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    png_temp = temporary_sibling(png_path)
    json_temp = temporary_sibling(json_path)
    old_png = png_path.read_bytes() if png_path.exists() else None
    old_json = json_path.read_bytes() if json_path.exists() else None
    try:
        image = Image.fromarray(pixels, "RGBA")
        image.save(
            png_temp,
            format="PNG",
            compress_level=6,
            icc_profile=srgb_profile_bytes(),
        )
        manifest = {
            "format": "easypicture-sprite-atlas",
            "schema_version": 2,
            "image": png_path.name,
            "size": [width, height],
            "cell_size": [cell.width, cell.height],
            "animations": [
                {
                    "id": str(animation.id),
                    "name": animation.name,
                    "fps": animation.fps,
                    "playback_mode": animation.playback_mode.value,
                    "frames": [
                        {
                            "frame_id": str(frame_id),
                            "duration_ticks": frame_by_id[frame_id].duration_ticks,
                        }
                        for frame_id in animation.frame_ids
                    ],
                }
                for animation in workspace.animations
                if animation.id in selected and animation.frame_ids
            ],
            "frames": records,
        }
        json_temp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        flush_file(png_temp)
        flush_file(json_temp)
        token.raise_if_cancelled()
        with Image.open(png_temp) as reopened:
            reopened.load()
            if reopened.format != "PNG" or reopened.size != (width, height):
                raise ValueError("atlas PNG validation failed")
        decoded = json.loads(json_temp.read_text(encoding="utf-8"))
        if decoded.get("schema_version") != 2 or len(decoded.get("frames", [])) != total:
            raise ValueError("atlas JSON validation failed")
        os.replace(png_temp, png_path)
        try:
            os.replace(json_temp, json_path)
        except BaseException:
            if old_png is None:
                png_path.unlink(missing_ok=True)
            else:
                png_path.write_bytes(old_png)
            raise
    except BaseException:
        if old_png is not None and not png_path.exists():
            png_path.write_bytes(old_png)
        if old_json is not None and not json_path.exists():
            json_path.write_bytes(old_json)
        raise
    finally:
        png_temp.unlink(missing_ok=True)
        json_temp.unlink(missing_ok=True)

    return SpriteAtlasExportReceipt(
        png_path=png_path,
        json_path=json_path,
        document_id=request.snapshot.document_id,
        revision=request.snapshot.revision,
        width=width,
        height=height,
        frame_count=total,
    )


__all__ = [
    "SpriteAtlasExportReceipt",
    "SpriteAtlasExportRequest",
    "export_sprite_atlas",
]
