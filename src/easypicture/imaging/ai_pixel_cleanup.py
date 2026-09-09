"""AI pixel-art cleanup: private, full-resolution, deterministic CPU results."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import UUID

import numpy as np

from easypicture.domain import Size
from easypicture.imaging.ai_palette import (
    PaletteAnalysis,
    analyze_palette,
    generate_palette,
    nearest_indices,
    srgb_to_oklab,
)
from easypicture.imaging.color_adjustments import (
    ColorAdjustmentCancellation,
    PaletteMapAdjustment,
    ProgressReporter,
    RGBColor,
    _rgba_surface,
)
from easypicture.imaging.palette_extraction import active_raster_snapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, RGBAArray, TileCoord

if TYPE_CHECKING:
    from easypicture.persistence.snapshots import DocumentSnapshot


class PixelGridSampling(StrEnum):
    DOMINANT = "dominant"
    CENTER = "center"


class PixelCleanupAlpha(StrEnum):
    PRESERVE = "preserve"
    COVERAGE = "coverage"
    HARD = "hard"


class PixelCleanupSelection(StrEnum):
    STRICT = "strict"
    SOFT = "soft"


class PixelCleanupDither(StrEnum):
    NONE = "none"
    BAYER2 = "bayer2"
    BAYER4 = "bayer4"
    FLOYD_STEINBERG = "floyd_steinberg"


class PixelCleanupOutput(StrEnum):
    LAYER = "layer"
    PROJECT = "project"


@dataclass(frozen=True, slots=True)
class AiPixelCleanupParameters:
    target_colors: int = 32
    protect_details: bool = True
    locked_colors: tuple[RGBColor, ...] = ()
    fixed_palette: tuple[RGBColor, ...] | None = None
    grid_enabled: bool = False
    block_width: int = 8
    block_height: int = 8
    offset_x: int = 0
    offset_y: int = 0
    sampling: PixelGridSampling = PixelGridSampling.DOMINANT
    alpha: PixelCleanupAlpha = PixelCleanupAlpha.PRESERVE
    alpha_threshold: int = 128
    selection: PixelCleanupSelection = PixelCleanupSelection.STRICT
    dither: PixelCleanupDither = PixelCleanupDither.NONE
    dither_strength: int = 35
    output: PixelCleanupOutput = PixelCleanupOutput.LAYER

    def __post_init__(self) -> None:
        for value, lower, upper in (
            (self.target_colors, 1, 256),
            (self.block_width, 1, 4096),
            (self.block_height, 1, 4096),
            (self.alpha_threshold, 1, 255),
            (self.dither_strength, 0, 100),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("AI 像素参数超出允许范围")
        if not (
            type(self.offset_x) is int
            and 0 <= self.offset_x < self.block_width
            and type(self.offset_y) is int
            and 0 <= self.offset_y < self.block_height
        ):
            raise ValueError("对齐偏移必须位于一个像素块内")
        if self.locked_colors:
            PaletteMapAdjustment(self.locked_colors)
        if self.fixed_palette is None and len(self.locked_colors) > self.target_colors:
            raise ValueError("锁定颜色数超过目标色数，请增加目标或解锁颜色")
        if self.fixed_palette is not None:
            PaletteMapAdjustment(self.fixed_palette)
        if self.output is PixelCleanupOutput.PROJECT and (
            not self.grid_enabled or self.alpha is PixelCleanupAlpha.PRESERVE
        ):
            raise ValueError("低分辨率工程需要启用网格，并选择覆盖率或阈值 Alpha")

    def logical_size(self, source_size: Size) -> Size:
        if not self.grid_enabled:
            return source_size
        return Size(
            (source_size.width + self.offset_x + self.block_width - 1) // self.block_width,
            (source_size.height + self.offset_y + self.block_height - 1) // self.block_height,
        )


@dataclass(frozen=True, slots=True)
class AiPixelCleanupRequest:
    snapshot: DocumentSnapshot
    layer_id: UUID
    parameters: AiPixelCleanupParameters


@dataclass(frozen=True, slots=True)
class AiPixelCleanupResult:
    document_id: UUID
    source_revision: int
    layer_id: UUID
    parameters: AiPixelCleanupParameters
    palette: tuple[RGBColor, ...]
    source_color_count: int
    result_color_count: int
    updates: Mapping[TileCoord, RGBAArray]
    logical_size: Size
    logical_tiles: Mapping[TileCoord, RGBAArray]
    changed: bool
    grid_selection: Mapping[TileCoord, np.ndarray] = field(default_factory=dict)


@dataclass(slots=True)
class AiPixelCleanupCache:
    """One worker/session only. Publish a stage only after complete calculation."""

    identity: tuple[UUID, int, UUID] | None = None
    analysis_key: bool | None = None
    analysis: PaletteAnalysis | None = None
    palette_key: tuple[int, tuple[RGBColor, ...]] | None = None
    palette: tuple[RGBColor, ...] = ()
    grid_key: tuple[object, ...] | None = None
    grid: tuple[np.ndarray, np.ndarray] | None = None


def _selected(coverage: np.ndarray, parameters: AiPixelCleanupParameters) -> np.ndarray:
    return coverage >= 128 if parameters.selection is PixelCleanupSelection.STRICT else coverage > 0


def calculate_ai_pixel_cleanup(
    request: AiPixelCleanupRequest,
    cancellation: ColorAdjustmentCancellation,
    progress: ProgressReporter,
    cache: AiPixelCleanupCache | None = None,
) -> AiPixelCleanupResult:
    snapshot, params = request.snapshot, request.parameters
    layer = active_raster_snapshot(snapshot, request.layer_id)
    identity = (snapshot.document_id, snapshot.revision, request.layer_id)
    if cache is None:
        cache = AiPixelCleanupCache(identity=identity)
    elif cache.identity != identity:
        cache.identity = identity
        cache.analysis = None
        cache.palette_key = None
        cache.grid_key, cache.grid = None, None
    if cache.analysis is None or cache.analysis_key != params.protect_details:
        analysis = analyze_palette(snapshot, layer, params.protect_details, cancellation, progress)
        cancellation.raise_if_cancelled()
        cache.analysis, cache.analysis_key = analysis, params.protect_details
        cache.palette_key = None
    analysis = cache.analysis
    palette_key = (params.target_colors, params.locked_colors)
    if params.fixed_palette is not None:
        palette = params.fixed_palette if analysis.source_color_count else ()
    elif cache.palette_key == palette_key:
        palette = cache.palette
    else:
        palette = generate_palette(
            analysis, params.target_colors, params.locked_colors, cancellation, progress
        )
        cancellation.raise_if_cancelled()
        cache.palette_key, cache.palette = palette_key, palette
    size = params.logical_size(snapshot.size)
    if not palette:
        return AiPixelCleanupResult(
            *identity, params, (), 0, 0, MappingProxyType({}), size, MappingProxyType({}), False
        )
    width, height = snapshot.size.width, snapshot.size.height
    with ExitStack() as stack:
        source = stack.enter_context(_rgba_surface(width, height))
        selection = stack.enter_context(_rgba_surface(width, height))
        selection[..., 0] = 255 if not snapshot.selection.has_nonzero_data else 0
        for coord, tile in layer.pixels.iter_allocated_tiles():
            cancellation.raise_if_cancelled()
            x, y = coord.x * DEFAULT_TILE_SIZE, coord.y * DEFAULT_TILE_SIZE
            w, h = min(DEFAULT_TILE_SIZE, width - x), min(DEFAULT_TILE_SIZE, height - y)
            source[y : y + h, x : x + w] = tile[:h, :w]
        if snapshot.selection.has_nonzero_data:
            for coord, tile in snapshot.selection.iter_allocated_tiles():
                cancellation.raise_if_cancelled()
                x, y = coord.x * DEFAULT_TILE_SIZE, coord.y * DEFAULT_TILE_SIZE
                w, h = min(DEFAULT_TILE_SIZE, width - x), min(DEFAULT_TILE_SIZE, height - y)
                selection[y : y + h, x : x + w, 0] = tile[:h, :w]
        if params.grid_enabled:
            samples = stack.enter_context(_rgba_surface(size.width, size.height))
            coverage = stack.enter_context(_rgba_surface(size.width, size.height))
            grid_key = (
                palette,
                params.block_width,
                params.block_height,
                params.offset_x,
                params.offset_y,
                params.sampling,
            )
            if cache.grid_key == grid_key and cache.grid is not None:
                samples[:], coverage[:] = cache.grid
            else:
                _sample_grid(
                    source,
                    selection[..., 0],
                    samples,
                    coverage[..., 0],
                    params,
                    palette,
                    snapshot.selection.has_nonzero_data,
                    cancellation,
                    progress,
                )
                cancellation.raise_if_cancelled()
                cache.grid_key, cache.grid = grid_key, None
                if samples.nbytes + coverage.nbytes <= 16 * 1024 * 1024:
                    cache.grid = (samples.copy(), coverage.copy())
        else:
            samples, coverage = source, selection
        adjusted = stack.enter_context(_rgba_surface(size.width, size.height))
        _map_colors(samples, coverage[..., 0], adjusted, palette, params, cancellation, progress)
        if params.alpha is PixelCleanupAlpha.HARD:
            for y in range(0, size.height, 64):
                cancellation.raise_if_cancelled()
                adjusted[y : y + 64, :, 3] = (
                    samples[y : y + 64, :, 3] >= params.alpha_threshold
                ).astype(np.uint8) * 255
        updates: dict[TileCoord, RGBAArray] = {}
        seen = np.zeros(1 << 24, dtype=np.bool_)
        changed = False
        for ty, y in enumerate(range(0, height, DEFAULT_TILE_SIZE)):
            cancellation.raise_if_cancelled()
            h = min(DEFAULT_TILE_SIZE, height - y)
            iy = (
                (np.arange(y, y + h) + params.offset_y) // params.block_height
                if params.grid_enabled
                else np.arange(y, y + h)
            )
            for tx, x in enumerate(range(0, width, DEFAULT_TILE_SIZE)):
                cancellation.raise_if_cancelled()
                w = min(DEFAULT_TILE_SIZE, width - x)
                ix = (
                    (np.arange(x, x + w) + params.offset_x) // params.block_width
                    if params.grid_enabled
                    else np.arange(x, x + w)
                )
                before = source[y : y + h, x : x + w]
                after = adjusted[iy[:, None], ix].copy()
                weights = coverage[iy[:, None], ix, 0]
                selected = _selected(weights, params)
                if params.alpha is PixelCleanupAlpha.PRESERVE:
                    after[..., 3] = before[..., 3]
                    after[before[..., 3] == 0] = before[before[..., 3] == 0]
                if params.selection is PixelCleanupSelection.SOFT:
                    factor = weights.astype(np.float64)[..., None] / 255
                    after = np.rint(before * (1 - factor) + after * factor).astype(np.uint8)
                else:
                    after[~selected] = before[~selected]
                if not np.array_equal(before, after):
                    changed = True
                _count_colors(seen, after, selected)
                tile = np.zeros((DEFAULT_TILE_SIZE, DEFAULT_TILE_SIZE, 4), dtype=np.uint8)
                tile[:h, :w] = after
                if np.any(tile):
                    tile.flags.writeable = False
                    updates[TileCoord(tx, ty)] = tile
            progress(ty + 1, (height + 255) // 256, "生成完整像素整理结果")
        logical_tiles: dict[TileCoord, RGBAArray] = {}
        if params.output is PixelCleanupOutput.PROJECT:
            seen.fill(False)
            for ty, y in enumerate(range(0, size.height, DEFAULT_TILE_SIZE)):
                for tx, x in enumerate(range(0, size.width, DEFAULT_TILE_SIZE)):
                    cancellation.raise_if_cancelled()
                    w, h = min(256, size.width - x), min(256, size.height - y)
                    out = adjusted[y : y + h, x : x + w].copy()
                    weights = coverage[y : y + h, x : x + w, 0]
                    selected = _selected(weights, params)
                    if params.selection is PixelCleanupSelection.SOFT:
                        factor = weights.astype(np.float64)[..., None] / 255
                        original = samples[y : y + h, x : x + w]
                        out[..., :3] = np.rint(
                            original[..., :3] * (1 - factor) + out[..., :3] * factor
                        ).astype(np.uint8)
                        out[..., 3] = np.rint(out[..., 3] * factor[..., 0]).astype(np.uint8)
                    out[~selected] = 0
                    _count_colors(seen, out, selected)
                    tile = np.zeros((256, 256, 4), dtype=np.uint8)
                    tile[:h, :w] = out
                    if np.any(tile):
                        tile.flags.writeable = False
                        logical_tiles[TileCoord(tx, ty)] = tile
        grid_selection: dict[TileCoord, np.ndarray] = {}
        if params.grid_enabled and snapshot.selection.has_nonzero_data:
            for ty, y in enumerate(range(0, size.height, 256)):
                for tx, x in enumerate(range(0, size.width, 256)):
                    cancellation.raise_if_cancelled()
                    w, h = min(256, size.width - x), min(256, size.height - y)
                    mask = np.zeros((256, 256), dtype=np.uint8)
                    covered = coverage[y : y + h, x : x + w, 0]
                    mask[:h, :w] = (
                        _selected(covered, params) * 255
                        if params.selection is PixelCleanupSelection.STRICT
                        else covered
                    )
                    mask.flags.writeable = False
                    grid_selection[TileCoord(tx, ty)] = mask
        cancellation.raise_if_cancelled()
        return AiPixelCleanupResult(
            *identity,
            params,
            palette,
            analysis.source_color_count,
            int(np.count_nonzero(seen)),
            MappingProxyType(updates),
            size,
            MappingProxyType(logical_tiles),
            changed,
            MappingProxyType(grid_selection),
        )


def _count_colors(seen: np.ndarray, pixels: np.ndarray, selected: np.ndarray) -> None:
    rgb = pixels[..., :3][selected & (pixels[..., 3] > 0)].astype(np.uint32)
    seen[rgb[:, 0] << 16 | rgb[:, 1] << 8 | rgb[:, 2]] = True


def _sample_grid(
    source: np.ndarray,
    selection: np.ndarray,
    samples: np.ndarray,
    coverage: np.ndarray,
    params: AiPixelCleanupParameters,
    palette: tuple[RGBColor, ...],
    has_selection: bool,
    cancellation: ColorAdjustmentCancellation,
    progress: ProgressReporter,
) -> None:
    bw, bh = params.block_width, params.block_height
    height, width = source.shape[:2]
    centers = srgb_to_oklab(np.asarray(palette))
    area = bw * bh
    if bw == bh == 1:
        for y in range(source.shape[0]):
            cancellation.raise_if_cancelled()
            samples[y] = source[y]
            coverage[y] = selection[y]
        return
    for gy in range(samples.shape[0]):
        cancellation.raise_if_cancelled()
        top = gy * bh - params.offset_y
        y0, y1 = max(0, top), min(height, top + bh)
        for gx in range(samples.shape[1]):
            if gx % 64 == 0:
                cancellation.raise_if_cancelled()
            left = gx * bw - params.offset_x
            x0, x1 = max(0, left), min(width, left + bw)
            pixels = source[y0:y1, x0:x1]
            mask = selection[y0:y1, x0:x1]
            coverage[gy, gx] = (
                int(np.rint(mask.sum(dtype=np.float64) / area)) if has_selection else 255
            )
            if params.sampling is PixelGridSampling.CENTER:
                cx, cy = left + bw // 2, top + bh // 2
                if 0 <= cx < width and 0 <= cy < height and selection[cy, cx] > 0:
                    samples[gy, gx] = source[cy, cx]
                continue
            if pixels.shape[0] * pixels.shape[1] > 32768:
                samples[gy, gx] = _large_cell_sample(
                    pixels,
                    mask,
                    centers,
                    area,
                    (left + (bw - 1) / 2 - x0, top + (bh - 1) / 2 - y0),
                    cancellation,
                )
                continue
            weight = pixels[..., 3].astype(np.float64) / 255 * mask / 255
            eligible = weight > 0
            if not np.any(eligible):
                continue
            rgb, weights = pixels[..., :3][eligible], weight[eligible]
            labs = srgb_to_oklab(rgb)
            labels, _ = nearest_indices(labs, centers, cancellation)
            totals = np.bincount(labels, weights=weights, minlength=len(palette))
            candidates = np.flatnonzero(np.isclose(totals, totals.max(), rtol=0, atol=1e-12))
            yy, xx = np.nonzero(eligible)
            distances = (xx + x0 - (left + (bw - 1) / 2)) ** 2 + (
                yy + y0 - (top + (bh - 1) / 2)
            ) ** 2
            winner = min(candidates, key=lambda c: (float(distances[labels == c].min()), int(c)))
            chosen = labels == winner
            mean = np.average(labs[chosen], axis=0, weights=weights[chosen])
            errors = np.sum((labs[chosen] - mean) ** 2, axis=1)
            choices = np.flatnonzero(chosen)
            best = min(
                range(len(choices)),
                key=lambda i: (
                    float(errors[i]),
                    float(distances[choices[i]]),
                    tuple(rgb[choices[i]]),
                ),
            )
            samples[gy, gx, :3] = rgb[choices[best]]
            # Selection controls the write mask, not the source's physical coverage.
            samples[gy, gx, 3] = int(np.rint(pixels[..., 3].sum(dtype=np.float64) / area))
        progress(gy + 1, samples.shape[0], "按网格选择代表像素")


def _large_cell_sample(
    pixels: np.ndarray,
    mask: np.ndarray,
    centers: np.ndarray,
    area: int,
    center: tuple[float, float],
    cancellation: ColorAdjustmentCancellation,
) -> np.ndarray:
    """Two bounded passes, identical cluster/tie rules without full-cell Lab tables."""
    totals = np.zeros(len(centers))
    sums = np.zeros((len(centers), 3))
    nearest = np.full(len(centers), np.inf)
    rows = max(1, 32768 // pixels.shape[1])

    def chunks() -> Iterator[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
        for y in range(0, len(pixels), rows):
            cancellation.raise_if_cancelled()
            block = pixels[y : y + rows]
            weights = block[..., 3].astype(np.float64) / 255 * mask[y : y + rows] / 255
            visible = weights > 0
            if not np.any(visible):
                continue
            yy, xx = np.nonzero(visible)
            rgb = block[..., :3][visible]
            labs = srgb_to_oklab(rgb)
            labels, _ = nearest_indices(labs, centers, cancellation)
            distance = (xx - center[0]) ** 2 + (yy + y - center[1]) ** 2
            yield rgb, labs, weights[visible], labels, distance

    for _rgb, labs, weights, labels, distance in chunks():
        totals += np.bincount(labels, weights=weights, minlength=len(centers))
        for channel in range(3):
            sums[:, channel] += np.bincount(
                labels, weights=weights * labs[:, channel], minlength=len(centers)
            )
        np.minimum.at(nearest, labels, distance)
    result = np.zeros(4, dtype=np.uint8)
    if not np.any(totals):
        return result
    candidates = np.flatnonzero(np.isclose(totals, totals.max(), rtol=0, atol=1e-12))
    winner = min(candidates, key=lambda c: (float(nearest[c]), int(c)))
    mean = sums[winner] / totals[winner]
    best = (float("inf"), float("inf"), (256, 256, 256))
    for rgb, labs, _weights, labels, distance in chunks():
        chosen = labels == winner
        if not np.any(chosen):
            continue
        colors, distances = rgb[chosen], distance[chosen]
        errors = np.sum((labs[chosen] - mean) ** 2, axis=1)
        packed = colors.astype(np.uint32)
        keys = packed[:, 0] << 16 | packed[:, 1] << 8 | packed[:, 2]
        index = np.lexsort((keys, distances, errors))[0]
        candidate = (
            float(errors[index]),
            float(distances[index]),
            (int(colors[index, 0]), int(colors[index, 1]), int(colors[index, 2])),
        )
        if candidate < best:
            best = candidate
            result[:3] = colors[index]
    result[3] = int(np.rint(pixels[..., 3].sum(dtype=np.float64) / area))
    return result


def _map_colors(
    source: np.ndarray,
    coverage: np.ndarray,
    output: np.ndarray,
    colors: tuple[RGBColor, ...],
    params: AiPixelCleanupParameters,
    cancellation: ColorAdjustmentCancellation,
    progress: ProgressReporter,
) -> None:
    for y in range(0, source.shape[0], 64):
        cancellation.raise_if_cancelled()
        output[y : y + 64] = source[y : y + 64]
    palette = np.asarray(colors, dtype=np.uint8)
    centers = srgb_to_oklab(palette)
    strength = params.dither_strength / 100
    dither = params.dither if strength > 0 else PixelCleanupDither.NONE
    if dither is PixelCleanupDither.FLOYD_STEINBERG:
        _diffuse(
            source, output, coverage, params, palette, centers, strength, cancellation, progress
        )
        return
    matrix = (
        np.asarray([[0, 2], [3, 1]])
        if dither is PixelCleanupDither.BAYER2
        else np.asarray([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]])
    )
    cache: dict[int, int] = {}
    for y in range(source.shape[0]):
        cancellation.raise_if_cancelled()
        active = _selected(coverage[y], params) & (source[y, :, 3] > 0)
        if not np.any(active):
            continue
        rgb = source[y, active, :3]
        if dither is PixelCleanupDither.NONE:
            packed = rgb.astype(np.uint32)
            keys = packed[:, 0] << 16 | packed[:, 1] << 8 | packed[:, 2]
            unique, inverse = np.unique(keys, return_inverse=True)
            missing = [int(k) for k in unique if int(k) not in cache]
            if missing:
                missing_rgb = np.array([(k >> 16, (k >> 8) & 255, k & 255) for k in missing])
                indices, _ = nearest_indices(srgb_to_oklab(missing_rgb), centers, cancellation)
                if len(cache) + len(missing) > 65536:
                    cache.clear()
                cache.update(zip(missing, (int(i) for i in indices), strict=True))
                # Entries evicted from this row are recalculated deterministically below.
            absent = [int(k) for k in unique if int(k) not in cache]
            if absent:
                values = np.array([(k >> 16, (k >> 8) & 255, k & 255) for k in absent])
                indices, _ = nearest_indices(srgb_to_oklab(values), centers, cancellation)
                cache.update(zip(absent, (int(i) for i in indices), strict=True))
            indices = np.asarray([cache[int(k)] for k in unique])[inverse]
            output[y, active, :3] = palette[indices]
        else:
            labs = srgb_to_oklab(rgb)
            first, _ = nearest_indices(labs, centers, cancellation)
            second = first.copy()
            if len(colors) > 1:
                for start in range(0, len(labs), 1024):
                    cancellation.raise_if_cancelled()
                    batch = labs[start : start + 1024]
                    distance = np.sum((batch[:, None] - centers[None]) ** 2, axis=2)
                    distance[np.arange(len(batch)), first[start : start + len(batch)]] = np.inf
                    second[start : start + len(batch)] = np.argmin(distance, axis=1)
            direction = centers[second] - centers[first]
            ratio = np.clip(
                np.sum((labs - centers[first]) * direction, axis=1)
                / np.maximum(1e-20, np.sum(direction**2, axis=1)),
                0,
                1,
            )
            xs = np.flatnonzero(active)
            threshold = (matrix[y % len(matrix), xs % len(matrix)] + 0.5) / matrix.size
            output[y, active, :3] = palette[np.where(threshold < ratio * strength, second, first)]
        progress(y + 1, source.shape[0], "映射有限颜色")


def _diffuse(
    source: np.ndarray,
    output: np.ndarray,
    coverage: np.ndarray,
    params: AiPixelCleanupParameters,
    palette: np.ndarray,
    centers: np.ndarray,
    strength: float,
    cancellation: ColorAdjustmentCancellation,
    progress: ProgressReporter,
) -> None:
    height, width = source.shape[:2]
    current = np.zeros((width, 3), dtype=np.float64)
    following = np.zeros_like(current)
    for y in range(height):
        cancellation.raise_if_cancelled()
        selected = _selected(coverage[y : min(height, y + 2)], params)
        following.fill(0)
        direction = 1 if y % 2 == 0 else -1
        for x in range(width) if direction == 1 else range(width - 1, -1, -1):
            if x % 256 == 0:
                cancellation.raise_if_cancelled()
            alpha = source[y, x, 3]
            if not alpha or not selected[0, x]:
                continue
            value = np.clip(source[y, x, :3].astype(np.float64) + current[x], 0, 255)
            lab = srgb_to_oklab(value)
            chosen = int(np.argmin(np.sum((centers - lab) ** 2, axis=1)))
            output[y, x, :3] = palette[chosen]
            error = (value - palette[chosen]) * strength * (int(alpha) / 255)
            nx = x + direction
            if 0 <= nx < width and source[y, nx, 3] and selected[0, nx]:
                current[nx] += error * (7 / 16)
            if y + 1 < height:
                for dx, weight in ((-direction, 3), (0, 5), (direction, 1)):
                    nx = x + dx
                    if 0 <= nx < width and source[y + 1, nx, 3] and selected[1, nx]:
                        following[nx] += error * (weight / 16)
        current, following = following, current
        progress(y + 1, height, "蛇形误差扩散")
