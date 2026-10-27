"""Pure, cancellable preparation of transient Tile surfaces (no widget access)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import numpy as np

from easypicture.domain import Document, RasterObject, Size, SpriteFrameContentMode
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION
from easypicture.imaging import (
    TileDirection,
    TilePreviewMode,
    TileSeamMetrics,
    TileTransitionRequest,
    analyze_tile_seam,
    generate_tile_transition,
    offset_tile,
    tile_seam_differences,
)
from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import ProgressSink
from easypicture.persistence import DocumentSnapshot, render_sprite_frame_pixels
from easypicture.pixels import DEFAULT_TILE_SIZE, Rect, RGBAArray, TileCoord, TilePixelStore
from easypicture.ui.settings import TileWorkspaceSettings

type HeatmapSegment = tuple[float, float, float, float, int]


class _TilePreviewDocument(Document):
    """Runtime-only presentation; real projects/embedded frames retain 8192 limits."""

    _maximum_dimension = MAX_DOCUMENT_DIMENSION * 5


@dataclass(frozen=True, slots=True)
class TilePreviewRequest:
    snapshot: DocumentSnapshot
    first_id: UUID
    second_id: UUID
    settings: TileWorkspaceSettings
    sources: tuple[RGBAArray, RGBAArray] | None = None


@dataclass(frozen=True, slots=True)
class TilePreviewResult:
    document: Document
    transition: RGBAArray | None
    metrics: TileSeamMetrics
    segments: tuple[HeatmapSegment, ...]
    sources: tuple[RGBAArray, RGBAArray]


def tile_source_signature(document: Document, frame_ids: tuple[UUID, UUID]) -> tuple[object, ...]:
    """Only selected frames' authoritative content invalidates cached pixels."""
    result: list[object] = [document.id]
    for frame_id in frame_ids:
        frame = document.sprite_workspace.frame_by_id(frame_id)
        source = document
        region = frame.region
        if frame.content_mode is SpriteFrameContentMode.DETACHED:
            source = document.embedded_document_for_frame(frame_id)
            region = Rect(0, 0, source.width, source.height)
        result.extend((frame_id, source.id, region))
        from easypicture.domain import Collection, walk_nodes

        for group in walk_nodes(source.nodes):
            if not isinstance(group, Collection):
                continue
            result.extend((group.id, group.visible, group.opacity))
            if not group.visible or group.opacity == 0:
                continue
            for leaf in group.children:
                if isinstance(leaf, Collection):
                    continue
                result.extend((leaf.id, leaf.visible, leaf.opacity))
                if not leaf.visible or leaf.opacity == 0:
                    continue
                if isinstance(leaf, RasterObject):
                    store = leaf.pixels
                    result.append(id(store))
                    result.extend(
                        store.tile_revision(x, y)
                        for y in range(
                            region.y // store.tile_size, (region.bottom - 1) // store.tile_size + 1
                        )
                        for x in range(
                            region.x // store.tile_size, (region.right - 1) // store.tile_size + 1
                        )
                    )
                else:
                    result.extend((id(leaf), leaf.revision))
    return tuple(result)


def _preview_document(
    parts: tuple[RGBAArray, ...],
    cells: tuple[tuple[int, ...], ...],
    cancellation: CancellationToken,
    progress: ProgressSink,
) -> Document:
    """Build tiled display data directly; repeated blocks share immutable storage."""
    height, width = parts[0].shape[:2]
    size = Size(width * len(cells[0]), height * len(cells))
    document = _TilePreviewDocument.create_sprite_frame(size)
    tile_size = DEFAULT_TILE_SIZE
    tiles: dict[TileCoord, RGBAArray] = {}
    # Key describes source/target slices, not content hashes or duplicated images.
    interned: dict[tuple[tuple[int, ...], ...], RGBAArray] = {}
    rows = (size.height + tile_size - 1) // tile_size
    for ty in range(rows):
        cancellation.raise_if_cancelled()
        top = ty * tile_size
        bottom = min(top + tile_size, size.height)
        for tx in range((size.width + tile_size - 1) // tile_size):
            cancellation.raise_if_cancelled()
            left = tx * tile_size
            right = min(left + tile_size, size.width)
            slices: list[tuple[int, ...]] = []
            for row in range(top // height, (bottom - 1) // height + 1):
                y0, y1 = max(top, row * height), min(bottom, (row + 1) * height)
                for column in range(left // width, (right - 1) // width + 1):
                    x0, x1 = max(left, column * width), min(right, (column + 1) * width)
                    slices.append(
                        (
                            cells[row][column],
                            y0 - row * height,
                            y1 - row * height,
                            x0 - column * width,
                            x1 - column * width,
                            y0 - top,
                            y1 - top,
                            x0 - left,
                            x1 - left,
                        )
                    )
            key = tuple(slices)
            pixels = interned.get(key)
            if pixels is None:
                pixels = np.zeros((tile_size, tile_size, 4), dtype=np.uint8)
                for part, sy0, sy1, sx0, sx1, dy0, dy1, dx0, dx1 in slices:
                    pixels[dy0:dy1, dx0:dx1] = parts[part][sy0:sy1, sx0:sx1]
                pixels.flags.writeable = False
                interned[key] = pixels
            if np.any(pixels):
                tiles[TileCoord(tx, ty)] = pixels
        progress(ty + 1, rows, "准备 Tile 预览")
    document.active_layer.pixels = TilePixelStore.from_readonly_tiles(tiles)
    document.active_layer.locked = True
    document.active_layer.record_pixel_change()
    document.mark_clean()
    return document


def _heatmap(
    first: RGBAArray,
    second: RGBAArray,
    direction: TileDirection,
    settings: TileWorkspaceSettings,
    x: int,
    y: int,
) -> tuple[HeatmapSegment, ...]:
    differences = tile_seam_differences(
        first, second, direction, include_alpha=settings.include_alpha
    )
    horizontal = direction in {TileDirection.LEFT_TO_RIGHT, TileDirection.RIGHT_TO_LEFT}
    return tuple(
        (x, y + i, x, y + i + 1, int(delta)) if horizontal else (x + i, y, x + i + 1, y, int(delta))
        for i, delta in enumerate(differences)
        if delta > settings.tolerance
    )


def calculate_tile_preview(
    request: TilePreviewRequest, cancellation: CancellationToken, progress: ProgressSink
) -> TilePreviewResult:
    settings = request.settings
    if request.sources is None:
        first = render_sprite_frame_pixels(
            request.snapshot, request.first_id, cancellation, progress
        )
        second = (
            first
            if request.second_id == request.first_id
            else render_sprite_frame_pixels(
                request.snapshot, request.second_id, cancellation, progress
            )
        )
        first.flags.writeable = False
        second.flags.writeable = False
    else:
        first, second = request.sources
    sources = (first, second)
    cancellation.raise_if_cancelled()
    height, width = first.shape[:2]
    horizontal = settings.direction in {TileDirection.LEFT_TO_RIGHT, TileDirection.RIGHT_TO_LEFT}
    reverse = settings.direction in {TileDirection.RIGHT_TO_LEFT, TileDirection.BOTTOM_TO_TOP}
    mode = settings.preview_mode
    transition = None
    parts: tuple[RGBAArray, ...]
    segments: list[HeatmapSegment] = []
    if mode is TilePreviewMode.OFFSET:
        first = offset_tile(first, settings.offset_x, settings.offset_y)
        second = first
        parts = (first,)
        cells = tuple((0,) * settings.repeat_scale for _ in range(settings.repeat_scale))
        if settings.heatmap_visible:
            for row in range(settings.repeat_scale):
                cancellation.raise_if_cancelled()
                for column in range(settings.repeat_scale):
                    if column:
                        segments.extend(
                            _heatmap(
                                first,
                                first,
                                TileDirection.LEFT_TO_RIGHT,
                                settings,
                                column * width,
                                row * height,
                            )
                        )
                    if row:
                        segments.extend(
                            _heatmap(
                                first,
                                first,
                                TileDirection.TOP_TO_BOTTOM,
                                settings,
                                column * width,
                                row * height,
                            )
                        )
    else:
        if mode is TilePreviewMode.TRANSITION:
            transition = generate_tile_transition(
                TileTransitionRequest(
                    first,
                    second,
                    settings.direction,
                    settings.transition_mode,
                    float(settings.boundary_percent),
                    min(settings.transition_width, width if horizontal else height),
                ),
                check_cancelled=cancellation.raise_if_cancelled,
            )
            parts = (second, transition, first) if reverse else (first, transition, second)
        else:
            parts = (second, first) if reverse else (first, second)
        cells = (
            (tuple(range(len(parts))),) if horizontal else tuple((i,) for i in range(len(parts)))
        )
        if settings.heatmap_visible:
            for index in range(1, len(parts)):
                segments.extend(
                    _heatmap(
                        parts[index - 1],
                        parts[index],
                        TileDirection.LEFT_TO_RIGHT if horizontal else TileDirection.TOP_TO_BOTTOM,
                        settings,
                        index * width if horizontal else 0,
                        0 if horizontal else index * height,
                    )
                )
    metrics = analyze_tile_seam(
        first,
        second,
        settings.direction,
        tolerance=settings.tolerance,
        include_alpha=settings.include_alpha,
    )
    cancellation.raise_if_cancelled()
    preview = _preview_document(parts, cells, cancellation, progress)
    return TilePreviewResult(preview, transition, metrics, tuple(segments), sources)
