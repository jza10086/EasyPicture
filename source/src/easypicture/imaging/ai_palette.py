"""Deterministic, alpha/selection-weighted OKLab palette analysis (no Qt)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import numpy as np

from easypicture.imaging.color_adjustments import (
    ColorAdjustmentCancellation,
    ProgressReporter,
    RGBColor,
)
from easypicture.pixels import DEFAULT_TILE_SIZE, Rect

if TYPE_CHECKING:
    from easypicture.persistence.snapshots import DocumentSnapshot, LayerSnapshot


def srgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    values = np.asarray(rgb, dtype=np.float64) / 255.0
    linear = np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)
    lms = linear @ np.array(
        [
            [0.4122214708, 0.2119034982, 0.0883024619],
            [0.5363325363, 0.6806995451, 0.2817188376],
            [0.0514459929, 0.1073969566, 0.6299787005],
        ]
    )
    return cast(
        np.ndarray,
        np.cbrt(lms)
        @ np.array(
            [
                [0.2104542553, 1.9779984951, 0.0259040371],
                [0.7936177850, -2.4285922050, 0.7827717662],
                [-0.0040720468, 0.4505937099, -0.8086757660],
            ]
        ),
    )


def _linear_rgb(lab: np.ndarray) -> np.ndarray:
    lms = lab @ np.array(
        [
            [1.0, 1.0, 1.0],
            [0.3963377774, -0.1055613458, -0.0894841775],
            [0.2158037573, -0.0638541728, -1.2914855480],
        ]
    )
    return cast(
        np.ndarray,
        lms**3
        @ np.array(
            [
                [4.0767416621, -1.2684380046, -0.0041960863],
                [-3.3077115913, 2.6097574011, -0.7034186147],
                [0.2309699292, -0.3413193965, 1.7076147010],
            ]
        ),
    )


def oklab_to_srgb(lab: np.ndarray) -> np.ndarray:
    """Keep lightness/hue; binary-search chroma into the sRGB gamut."""
    values = np.asarray(lab, dtype=np.float64).reshape(-1, 3).copy()
    values[:, 0] = np.clip(values[:, 0], 0, 1)
    linear = _linear_rgb(values)
    outside = np.any((linear < -1e-7) | (linear > 1 + 1e-7), axis=1)
    if np.any(outside):
        original = values[outside].copy()
        low, high = np.zeros(len(original)), np.ones(len(original))
        for _ in range(24):
            mid = (low + high) / 2
            trial = original.copy()
            trial[:, 1:] *= mid[:, None]
            candidate = _linear_rgb(trial)
            inside = np.all((candidate >= 0) & (candidate <= 1), axis=1)
            low = np.where(inside, mid, low)
            high = np.where(inside, high, mid)
        original[:, 1:] *= low[:, None]
        linear[outside] = _linear_rgb(original)
    linear = np.clip(linear, 0, 1)
    encoded = np.where(linear <= 0.0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - 0.055)
    return cast(np.ndarray, np.rint(encoded * 255).astype(np.uint8).reshape(np.asarray(lab).shape))


def nearest_indices(
    lab: np.ndarray,
    centers: np.ndarray,
    cancellation: ColorAdjustmentCancellation,
) -> tuple[np.ndarray, np.ndarray]:
    """Bound distance scratch space independently of image/palette sizes."""
    labels = np.empty(len(lab), dtype=np.int32)
    errors = np.empty(len(lab), dtype=np.float64)
    batch_size = max(256, min(8192, 262144 // len(centers)))
    for start in range(0, len(lab), batch_size):
        cancellation.raise_if_cancelled()
        batch = lab[start : start + batch_size]
        distance = (batch[:, 0, None] - centers[None, :, 0]) ** 2
        distance += (batch[:, 1, None] - centers[None, :, 1]) ** 2
        distance += (batch[:, 2, None] - centers[None, :, 2]) ** 2
        selected = np.argmin(distance, axis=1)
        labels[start : start + len(batch)] = selected
        errors[start : start + len(batch)] = distance[np.arange(len(batch)), selected]
    return labels, errors


@dataclass(frozen=True, slots=True)
class PaletteAnalysis:
    labs: np.ndarray
    weights: np.ndarray
    source_color_count: int
    exact_colors: tuple[RGBColor, ...]


def analyze_palette(
    snapshot: DocumentSnapshot,
    layer: LayerSnapshot,
    protect_details: bool,
    cancellation: ColorAdjustmentCancellation,
    progress: ProgressReporter,
) -> PaletteAnalysis:
    seen = np.zeros(1 << 24, dtype=np.bool_)
    sums = np.zeros((1 << 18, 3), dtype=np.float64)
    weights = np.zeros(1 << 18, dtype=np.float64)
    tiles = sorted(layer.pixels.iter_allocated_tiles(), key=lambda item: (item[0].y, item[0].x))
    for index, (coord, tile) in enumerate(tiles):
        cancellation.raise_if_cancelled()
        x, y = coord.x * DEFAULT_TILE_SIZE, coord.y * DEFAULT_TILE_SIZE
        w, h = (
            min(DEFAULT_TILE_SIZE, snapshot.size.width - x),
            min(DEFAULT_TILE_SIZE, snapshot.size.height - y),
        )
        if w <= 0 or h <= 0:
            continue
        pixels = tile[:h, :w]
        selection = (
            snapshot.selection.read_region(Rect(x, y, w, h)).astype(np.float64) / 255
            if snapshot.selection.has_nonzero_data
            else np.ones((h, w))
        )
        weight = pixels[..., 3].astype(np.float64) / 255 * selection
        eligible = weight > 0
        if not np.any(eligible):
            continue
        if protect_details:
            # A 1 px halo avoids artificial importance at Tile boundaries.
            halo = np.zeros((h + 2, w + 2, 4), dtype=np.uint8)
            left, top = max(0, x - 1), max(0, y - 1)
            right, bottom = (
                min(snapshot.size.width, x + w + 1),
                min(snapshot.size.height, y + h + 1),
            )
            halo[top - y + 1 : bottom - y + 1, left - x + 1 : right - x + 1] = (
                layer.pixels.read_region(Rect(left, top, right - left, bottom - top))
            )
            halo_lab = srgb_to_oklab(halo[..., :3])
            middle = halo_lab[1:-1, 1:-1]
            edge = np.zeros((h, w), dtype=np.float64)
            for oy, ox in ((0, 1), (2, 1), (1, 0), (1, 2)):
                neighbor = halo_lab[oy : oy + h, ox : ox + w]
                visible = halo[oy : oy + h, ox : ox + w, 3] > 0
                delta = np.sqrt(np.sum((middle - neighbor) ** 2, axis=2))
                edge = np.maximum(edge, np.where(visible, delta, 0))
            weight *= 1 + 0.35 * np.clip(edge / 0.1, 0, 1)
        rgb = pixels[..., :3][eligible].astype(np.uint32)
        packed = rgb[:, 0] << 16 | rgb[:, 1] << 8 | rgb[:, 2]
        seen[packed] = True
        buckets = (rgb[:, 0] >> 2) << 12 | (rgb[:, 1] >> 2) << 6 | (rgb[:, 2] >> 2)
        ids, inverse = np.unique(buckets, return_inverse=True)
        pixel_weights = weight[eligible]
        weights[ids] += np.bincount(inverse, weights=pixel_weights)
        for channel in range(3):
            sums[ids, channel] += np.bincount(inverse, weights=pixel_weights * rgb[:, channel])
        progress(index + 1, max(1, len(tiles)), "分析原始颜色与细节")
    cancellation.raise_if_cancelled()
    count = int(np.count_nonzero(seen))
    exact = (
        tuple((int(v >> 16), int((v >> 8) & 255), int(v & 255)) for v in np.flatnonzero(seen))
        if count <= 256
        else ()
    )
    ids = np.flatnonzero(weights)
    labs = srgb_to_oklab(sums[ids] / weights[ids, None])
    return PaletteAnalysis(labs, weights[ids], count, exact)


def _median_centers(
    labs: np.ndarray,
    weights: np.ndarray,
    count: int,
    cancellation: ColorAdjustmentCancellation,
) -> np.ndarray:
    partitions = [np.arange(len(labs))]
    while len(partitions) < min(count, len(labs)):
        cancellation.raise_if_cancelled()
        scores = []
        for ids in partitions:
            cancellation.raise_if_cancelled()
            if len(ids) < 2:
                scores.append(-1.0)
                continue
            mean = np.average(labs[ids], axis=0, weights=weights[ids])
            scores.append(float(np.sum((labs[ids] - mean) ** 2 * weights[ids, None])))
        chosen = int(np.argmax(scores))
        if scores[chosen] < 0:
            break
        ids = partitions.pop(chosen)
        axis = int(np.argmax(np.ptp(labs[ids], axis=0)))
        ordered = ids[np.argsort(labs[ids, axis], kind="stable")]
        split = int(np.searchsorted(np.cumsum(weights[ordered]), weights[ordered].sum() / 2)) + 1
        split = max(1, min(len(ordered) - 1, split))
        partitions[chosen:chosen] = [ordered[:split], ordered[split:]]
    return np.asarray([np.average(labs[ids], axis=0, weights=weights[ids]) for ids in partitions])


def generate_palette(
    analysis: PaletteAnalysis,
    target: int,
    locked: tuple[RGBColor, ...],
    cancellation: ColorAdjustmentCancellation,
    progress: ProgressReporter,
) -> tuple[RGBColor, ...]:
    if not analysis.source_color_count:
        return ()
    union = set(analysis.exact_colors) | set(locked)
    if analysis.source_color_count <= target and len(union) <= target:
        return _sort_palette(tuple(union))
    labs, weights = analysis.labs, analysis.weights
    fixed = srgb_to_oklab(np.asarray(locked).reshape(-1, 3))
    available = target - len(locked)
    if available == 0:
        return _sort_palette(locked)
    # Give fixed colors their assignments, but retain all source bins for refinement.
    free = _median_centers(labs, weights, available, cancellation)
    centers = np.concatenate((fixed, free))
    previous: np.ndarray | None = None
    for iteration in range(8):
        labels, errors = nearest_indices(labs, centers, cancellation)
        if previous is not None and np.array_equal(labels, previous):
            break
        updated = centers.copy()
        used_reseeds: set[int] = set()
        for index in range(len(fixed), len(centers)):
            assigned = labels == index
            if np.any(assigned):
                updated[index] = np.average(labs[assigned], axis=0, weights=weights[assigned])
            else:
                scores = errors * weights
                if used_reseeds:
                    scores[list(used_reseeds)] = -1
                worst = int(np.argmax(scores))
                used_reseeds.add(worst)
                updated[index] = labs[worst]
        displacement = float(np.sqrt(np.sum((updated - centers) ** 2, axis=1)).max())
        centers, previous = updated, labels
        progress(iteration + 1, 8, "优化 AI 像素调色板")
        if displacement < 1e-5:
            break
    cancellation.raise_if_cancelled()
    generated = oklab_to_srgb(centers[len(fixed) :])
    return _sort_palette(
        tuple(set(locked) | {(int(rgb[0]), int(rgb[1]), int(rgb[2])) for rgb in generated})
    )


def _sort_palette(colors: tuple[RGBColor, ...]) -> tuple[RGBColor, ...]:
    ordered = sorted(colors)
    if not ordered:
        return ()
    lightness = srgb_to_oklab(np.asarray(ordered))[:, 0]
    return tuple(ordered[int(i)] for i in np.argsort(lightness, kind="stable"))
