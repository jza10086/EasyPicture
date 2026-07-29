"""Qt-free, full-resolution color adjustment operations."""

from __future__ import annotations

import math
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

import numpy as np

from easypicture.pixels import DEFAULT_TILE_SIZE, RGBAArray, TileCoord

if TYPE_CHECKING:
    from easypicture.persistence.snapshots import DocumentSnapshot, LayerSnapshot

type ProgressReporter = Callable[[int, int, str], None]
type RGBColor = tuple[int, int, int]
type RGBAColor = tuple[int, int, int, int]

_WORKING_MEMORY_LIMIT = 64 * 1024 * 1024
_REC709 = np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32)


def _validate_rgba(color: RGBAColor) -> None:
    if len(color) != 4 or not all(0 <= channel <= 255 for channel in color):
        raise ValueError("RGBA channels must be between 0 and 255")


class ColorAdjustmentCancellation(Protocol):
    def raise_if_cancelled(self) -> None: ...


class ColorAdjustmentKind(StrEnum):
    INVERT = "invert"
    THRESHOLD = "threshold"
    BRIGHTNESS_CONTRAST = "brightness_contrast"
    GAMMA = "gamma"
    CURVES = "curves"
    HSL = "hsl"
    GRADIENT_MAP = "gradient_map"
    COLOR_REPLACE = "color_replace"
    PALETTE_MAP = "palette_map"


class ColorChannel(StrEnum):
    RGB = "rgb"
    RED = "red"
    GREEN = "green"
    BLUE = "blue"
    ALPHA = "alpha"


@dataclass(frozen=True, slots=True)
class InvertAdjustment:
    channel: ColorChannel = ColorChannel.RGB


@dataclass(frozen=True, slots=True)
class ThresholdAdjustment:
    threshold: int = 128

    def __post_init__(self) -> None:
        if not 0 <= self.threshold <= 255:
            raise ValueError("threshold must be between 0 and 255")


@dataclass(frozen=True, slots=True)
class BrightnessContrastAdjustment:
    brightness: int = 0
    contrast: int = 0

    def __post_init__(self) -> None:
        if not -100 <= self.brightness <= 100:
            raise ValueError("brightness must be between -100 and 100")
        if not -100 <= self.contrast <= 100:
            raise ValueError("contrast must be between -100 and 100")


@dataclass(frozen=True, slots=True)
class GammaAdjustment:
    gamma: float = 1.0
    channel: ColorChannel = ColorChannel.RGB

    def __post_init__(self) -> None:
        if not math.isfinite(self.gamma) or not 0.01 <= self.gamma <= 10.0:
            raise ValueError("gamma must be between 0.01 and 10")


@dataclass(frozen=True, slots=True)
class CurveDefinition:
    points: tuple[tuple[int, int], ...] = ((0, 0), (255, 255))

    def __post_init__(self) -> None:
        if not 2 <= len(self.points) <= 16:
            raise ValueError("a curve must contain between 2 and 16 points")
        if self.points[0][0] != 0 or self.points[-1][0] != 255:
            raise ValueError("curve endpoint X values must be 0 and 255")
        previous_x = -1
        for x, y in self.points:
            if not 0 <= x <= 255 or not 0 <= y <= 255:
                raise ValueError("curve points must lie between 0 and 255")
            if x <= previous_x:
                raise ValueError("curve point X values must be strictly increasing")
            previous_x = x


@dataclass(frozen=True, slots=True)
class CurvesAdjustment:
    rgb: CurveDefinition = CurveDefinition()
    red: CurveDefinition = CurveDefinition()
    green: CurveDefinition = CurveDefinition()
    blue: CurveDefinition = CurveDefinition()
    alpha: CurveDefinition = CurveDefinition()


@dataclass(frozen=True, slots=True)
class HslAdjustment:
    hue: int = 0
    saturation: int = 0
    lightness: int = 0

    def __post_init__(self) -> None:
        if not -180 <= self.hue <= 180:
            raise ValueError("hue must be between -180 and 180")
        if not -100 <= self.saturation <= 100:
            raise ValueError("saturation must be between -100 and 100")
        if not -100 <= self.lightness <= 100:
            raise ValueError("lightness must be between -100 and 100")


@dataclass(frozen=True, slots=True)
class GradientStop:
    position: float
    color: RGBAColor

    def __post_init__(self) -> None:
        if not math.isfinite(self.position) or not 0.0 <= self.position <= 1.0:
            raise ValueError("gradient stop position must be between 0 and 1")
        _validate_rgba(self.color)


@dataclass(frozen=True, slots=True)
class GradientMapAdjustment:
    stops: tuple[GradientStop, ...] = (
        GradientStop(0.0, (0, 0, 0, 255)),
        GradientStop(1.0, (255, 255, 255, 255)),
    )
    use_alpha: bool = False

    def __post_init__(self) -> None:
        if not 2 <= len(self.stops) <= 8:
            raise ValueError("gradient map must contain between 2 and 8 stops")
        positions = tuple(stop.position for stop in self.stops)
        if positions != tuple(sorted(positions)) or len(set(positions)) != len(positions):
            raise ValueError("gradient stop positions must be unique and sorted")


@dataclass(frozen=True, slots=True)
class ColorReplaceAdjustment:
    source: RGBAColor = (0, 0, 0, 255)
    target: RGBAColor = (255, 255, 255, 255)
    tolerance: int = 0
    softness: int = 0
    include_alpha: bool = False
    replace_alpha: bool = False

    def __post_init__(self) -> None:
        _validate_rgba(self.source)
        _validate_rgba(self.target)
        if not 0 <= self.tolerance <= 255:
            raise ValueError("color replacement tolerance must be between 0 and 255")
        if not 0 <= self.softness <= 255:
            raise ValueError("color replacement softness must be between 0 and 255")


@dataclass(frozen=True, slots=True)
class PaletteMapAdjustment:
    colors: tuple[RGBColor, ...] = ((0, 0, 0), (255, 255, 255))
    dither: bool = False

    def __post_init__(self) -> None:
        if not 2 <= len(self.colors) <= 32:
            raise ValueError("palette must contain between 2 and 32 colors")
        if len(set(self.colors)) != len(self.colors):
            raise ValueError("palette colors must be unique")
        for color in self.colors:
            if len(color) != 3 or not all(0 <= channel <= 255 for channel in color):
                raise ValueError("palette RGB channels must be between 0 and 255")


type ColorAdjustmentParameters = (
    InvertAdjustment
    | ThresholdAdjustment
    | BrightnessContrastAdjustment
    | GammaAdjustment
    | CurvesAdjustment
    | HslAdjustment
    | GradientMapAdjustment
    | ColorReplaceAdjustment
    | PaletteMapAdjustment
)


@dataclass(frozen=True, slots=True)
class ColorAdjustmentRequest:
    snapshot: DocumentSnapshot
    layer_id: UUID
    parameters: ColorAdjustmentParameters


@dataclass(frozen=True, slots=True)
class ColorAdjustmentResult:
    document_id: UUID
    source_revision: int
    layer_id: UUID
    updates: Mapping[TileCoord, RGBAArray | None]


def adjustment_kind(parameters: ColorAdjustmentParameters) -> ColorAdjustmentKind:
    kinds: tuple[tuple[type[object], ColorAdjustmentKind], ...] = (
        (InvertAdjustment, ColorAdjustmentKind.INVERT),
        (ThresholdAdjustment, ColorAdjustmentKind.THRESHOLD),
        (BrightnessContrastAdjustment, ColorAdjustmentKind.BRIGHTNESS_CONTRAST),
        (GammaAdjustment, ColorAdjustmentKind.GAMMA),
        (CurvesAdjustment, ColorAdjustmentKind.CURVES),
        (HslAdjustment, ColorAdjustmentKind.HSL),
        (GradientMapAdjustment, ColorAdjustmentKind.GRADIENT_MAP),
        (ColorReplaceAdjustment, ColorAdjustmentKind.COLOR_REPLACE),
        (PaletteMapAdjustment, ColorAdjustmentKind.PALETTE_MAP),
    )
    for parameter_type, kind in kinds:
        if isinstance(parameters, parameter_type):
            return kind
    raise TypeError("unsupported color adjustment parameters")


def adjustment_changes_alpha(parameters: ColorAdjustmentParameters) -> bool:
    if isinstance(parameters, InvertAdjustment | GammaAdjustment):
        return parameters.channel is ColorChannel.ALPHA
    if isinstance(parameters, CurvesAdjustment):
        identity = np.arange(256, dtype=np.uint8)
        return not np.array_equal(curve_lut(parameters.alpha), identity)
    if isinstance(parameters, GradientMapAdjustment):
        return parameters.use_alpha
    if isinstance(parameters, ColorReplaceAdjustment):
        return parameters.replace_alpha
    return False


def calculate_color_adjustment(
    request: ColorAdjustmentRequest,
    cancellation: ColorAdjustmentCancellation,
    progress: ProgressReporter,
) -> ColorAdjustmentResult:
    """Calculate exact full-Tile replacements over an immutable snapshot."""

    layer = _layer_by_id(request.snapshot, request.layer_id)
    if layer.locked:
        raise PermissionError("layer is locked")
    if isinstance(request.parameters, PaletteMapAdjustment) and request.parameters.dither:
        updates = _calculate_dithered_palette(request, layer, cancellation, progress)
    else:
        coords = _affected_coords(request.snapshot, layer, request.parameters)
        updates = {}
        total = len(coords)
        for index, coord in enumerate(coords, start=1):
            cancellation.raise_if_cancelled()
            before = layer.pixels.read_tile(coord.x, coord.y)
            adjusted = _apply_adjustment(before, request.parameters)
            after = _apply_constraints(
                before,
                adjusted,
                request.snapshot,
                layer,
                coord,
            )
            _clear_outside_canvas(
                after,
                coord,
                request.snapshot.size.width,
                request.snapshot.size.height,
            )
            if not np.array_equal(before, after):
                updates[coord] = after if np.any(after) else None
            progress(index, total, "生成颜色调整预览")
    return ColorAdjustmentResult(
        document_id=request.snapshot.document_id,
        source_revision=request.snapshot.revision,
        layer_id=layer.id,
        updates=updates,
    )


def curve_lut(curve: CurveDefinition) -> np.ndarray:
    """Return a shape-preserving PCHIP curve sampled at byte values."""

    x = np.asarray([point[0] for point in curve.points], dtype=np.float64)
    y = np.asarray([point[1] for point in curve.points], dtype=np.float64)
    if len(x) == 2:
        values = np.interp(np.arange(256, dtype=np.float64), x, y)
        return np.clip(np.rint(values), 0, 255).astype(np.uint8)
    h = np.diff(x)
    delta = np.diff(y) / h
    slopes = np.zeros_like(y)
    for index in range(1, len(x) - 1):
        left = delta[index - 1]
        right = delta[index]
        if left == 0.0 or right == 0.0 or np.sign(left) != np.sign(right):
            slopes[index] = 0.0
        else:
            first_weight = 2.0 * h[index] + h[index - 1]
            second_weight = h[index] + 2.0 * h[index - 1]
            slopes[index] = (first_weight + second_weight) / (
                first_weight / left + second_weight / right
            )
    slopes[0] = _endpoint_slope(h[0], h[1], delta[0], delta[1])
    slopes[-1] = _endpoint_slope(h[-1], h[-2], delta[-1], delta[-2])

    samples = np.arange(256, dtype=np.float64)
    interval = np.searchsorted(x, samples, side="right") - 1
    interval = np.clip(interval, 0, len(x) - 2)
    local_h = x[interval + 1] - x[interval]
    t = (samples - x[interval]) / local_h
    h00 = 2.0 * t**3 - 3.0 * t**2 + 1.0
    h10 = t**3 - 2.0 * t**2 + t
    h01 = -2.0 * t**3 + 3.0 * t**2
    h11 = t**3 - t**2
    values = (
        h00 * y[interval]
        + h10 * local_h * slopes[interval]
        + h01 * y[interval + 1]
        + h11 * local_h * slopes[interval + 1]
    )
    return np.clip(np.rint(values), 0, 255).astype(np.uint8)


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Convert sRGB bytes/floats in the 0..255 range to CIE Lab D65."""

    value = np.asarray(rgb, dtype=np.float32) / 255.0
    linear = np.where(
        value <= 0.04045,
        value / 12.92,
        ((value + 0.055) / 1.055) ** 2.4,
    )
    x = linear[..., 0] * 0.4124564 + linear[..., 1] * 0.3575761 + linear[..., 2] * 0.1804375
    y = linear[..., 0] * 0.2126729 + linear[..., 1] * 0.7151522 + linear[..., 2] * 0.0721750
    z = linear[..., 0] * 0.0193339 + linear[..., 1] * 0.1191920 + linear[..., 2] * 0.9503041
    xyz = np.stack((x / 0.95047, y, z / 1.08883), axis=-1)
    epsilon = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    converted = np.where(
        xyz > epsilon,
        np.cbrt(xyz),
        (kappa * xyz + 16.0) / 116.0,
    )
    return np.stack(
        (
            116.0 * converted[..., 1] - 16.0,
            500.0 * (converted[..., 0] - converted[..., 1]),
            200.0 * (converted[..., 1] - converted[..., 2]),
        ),
        axis=-1,
    ).astype(np.float32)


def _apply_adjustment(
    before: RGBAArray,
    parameters: ColorAdjustmentParameters,
) -> RGBAArray:
    if isinstance(parameters, InvertAdjustment):
        output = before.copy(order="C")
        channels = _channel_indices(parameters.channel)
        output[..., channels] = 255 - output[..., channels]
        return output
    if isinstance(parameters, ThresholdAdjustment):
        output = before.copy(order="C")
        luminance = np.sum(before[..., :3].astype(np.float32) * _REC709, axis=-1)
        value = np.where(luminance >= parameters.threshold, 255, 0).astype(np.uint8)
        output[..., :3] = value[..., None]
        return output
    if isinstance(parameters, BrightnessContrastAdjustment):
        output = before.copy(order="C")
        rgb = before[..., :3].astype(np.float32) / 255.0
        factor = 2.0 ** (parameters.contrast / 50.0)
        adjusted = (rgb - 0.5) * factor + 0.5 + parameters.brightness / 100.0
        output[..., :3] = np.clip(np.rint(adjusted * 255.0), 0, 255).astype(np.uint8)
        return output
    if isinstance(parameters, GammaAdjustment):
        output = before.copy(order="C")
        values = np.arange(256, dtype=np.float32) / 255.0
        lut = np.clip(
            np.rint(np.power(values, 1.0 / parameters.gamma) * 255.0),
            0,
            255,
        ).astype(np.uint8)
        channels = _channel_indices(parameters.channel)
        output[..., channels] = lut[before[..., channels]]
        return output
    if isinstance(parameters, CurvesAdjustment):
        output = before.copy(order="C")
        master = curve_lut(parameters.rgb)
        output[..., :3] = master[output[..., :3]]
        for index, curve in enumerate(
            (parameters.red, parameters.green, parameters.blue, parameters.alpha)
        ):
            output[..., index] = curve_lut(curve)[output[..., index]]
        return output
    if isinstance(parameters, HslAdjustment):
        output = before.copy(order="C")
        output[..., :3] = _adjust_hsl(before[..., :3], parameters)
        return output
    if isinstance(parameters, GradientMapAdjustment):
        output = before.copy(order="C")
        luminance = np.sum(before[..., :3].astype(np.float32) * _REC709, axis=-1) / 255.0
        positions = np.asarray([stop.position for stop in parameters.stops], dtype=np.float32)
        colors = np.asarray([stop.color for stop in parameters.stops], dtype=np.float32)
        last_channel = 4 if parameters.use_alpha else 3
        for channel in range(last_channel):
            output[..., channel] = np.clip(
                np.rint(np.interp(luminance, positions, colors[:, channel])),
                0,
                255,
            ).astype(np.uint8)
        return output
    if isinstance(parameters, ColorReplaceAdjustment):
        return _replace_color(before, parameters)
    if isinstance(parameters, PaletteMapAdjustment):
        output = before.copy(order="C")
        output[..., :3] = _nearest_palette(before[..., :3], parameters.colors)
        return output
    raise TypeError("unsupported color adjustment parameters")


def _apply_constraints(
    before: RGBAArray,
    adjusted: RGBAArray,
    snapshot: DocumentSnapshot,
    layer: LayerSnapshot,
    coord: TileCoord,
) -> RGBAArray:
    if snapshot.selection.has_nonzero_data:
        weight = snapshot.selection.read_tile(coord.x, coord.y).astype(np.float32) / 255.0
        output = np.clip(
            np.rint(
                before.astype(np.float32) * (1.0 - weight[..., None])
                + adjusted.astype(np.float32) * weight[..., None]
            ),
            0,
            255,
        ).astype(np.uint8)
    else:
        output = adjusted
    if layer.alpha_locked:
        transparent = before[..., 3] == 0
        output[..., 3] = before[..., 3]
        output[transparent] = before[transparent]
    return np.ascontiguousarray(output)


def _replace_color(before: RGBAArray, parameters: ColorReplaceAdjustment) -> RGBAArray:
    compared_channels = 4 if parameters.include_alpha else 3
    source = np.asarray(parameters.source[:compared_channels], dtype=np.int16)
    values = before[..., :compared_channels].astype(np.int16)
    distance = np.max(np.abs(values - source), axis=-1).astype(np.float32)
    inner = float(max(0, parameters.tolerance - parameters.softness))
    outer = float(parameters.tolerance)
    if outer <= inner:
        coverage = (distance <= outer).astype(np.float32)
    else:
        coverage = np.where(
            distance <= inner,
            1.0,
            np.clip((outer - distance) / (outer - inner), 0.0, 1.0),
        ).astype(np.float32)
    output = before.astype(np.float32)
    target = np.asarray(parameters.target, dtype=np.float32)
    last_channel = 4 if parameters.replace_alpha else 3
    output[..., :last_channel] = (
        output[..., :last_channel] * (1.0 - coverage[..., None])
        + target[:last_channel] * coverage[..., None]
    )
    return np.clip(np.rint(output), 0, 255).astype(np.uint8)


def _adjust_hsl(rgb: np.ndarray, parameters: HslAdjustment) -> np.ndarray:
    value = rgb.astype(np.float32) / 255.0
    maximum = np.max(value, axis=-1)
    minimum = np.min(value, axis=-1)
    delta = maximum - minimum
    lightness = (maximum + minimum) / 2.0
    denominator = 1.0 - np.abs(2.0 * lightness - 1.0)
    saturation = np.divide(
        delta,
        denominator,
        out=np.zeros_like(delta),
        where=(delta > 0.0) & (denominator > 0.0),
    )
    hue = np.zeros_like(delta)
    nonzero = delta > 0.0
    red_max = nonzero & (maximum == value[..., 0])
    green_max = nonzero & (maximum == value[..., 1]) & ~red_max
    blue_max = nonzero & ~(red_max | green_max)
    hue[red_max] = np.mod(
        (value[..., 1][red_max] - value[..., 2][red_max]) / delta[red_max],
        6.0,
    )
    hue[green_max] = (
        (value[..., 2][green_max] - value[..., 0][green_max]) / delta[green_max] + 2.0
    )
    hue[blue_max] = (
        (value[..., 0][blue_max] - value[..., 1][blue_max]) / delta[blue_max] + 4.0
    )
    hue = np.mod(hue * 60.0 + parameters.hue, 360.0)
    saturation = _shift_unit(saturation, parameters.saturation / 100.0)
    lightness = _shift_unit(lightness, parameters.lightness / 100.0)

    chroma = (1.0 - np.abs(2.0 * lightness - 1.0)) * saturation
    sector = hue / 60.0
    secondary = chroma * (1.0 - np.abs(np.mod(sector, 2.0) - 1.0))
    zeros = np.zeros_like(chroma)
    rgb_prime = np.zeros((*chroma.shape, 3), dtype=np.float32)
    conditions = tuple((sector >= index) & (sector < index + 1) for index in range(6))
    choices = (
        np.stack((chroma, secondary, zeros), axis=-1),
        np.stack((secondary, chroma, zeros), axis=-1),
        np.stack((zeros, chroma, secondary), axis=-1),
        np.stack((zeros, secondary, chroma), axis=-1),
        np.stack((secondary, zeros, chroma), axis=-1),
        np.stack((chroma, zeros, secondary), axis=-1),
    )
    for condition, choice in zip(conditions, choices, strict=True):
        rgb_prime[condition] = choice[condition]
    match = lightness - chroma / 2.0
    return cast(
        np.ndarray,
        np.clip(np.rint((rgb_prime + match[..., None]) * 255.0), 0, 255).astype(
            np.uint8
        ),
    )


def _shift_unit(values: np.ndarray, amount: float) -> np.ndarray:
    if amount >= 0.0:
        return values + (1.0 - values) * amount
    return values * (1.0 + amount)


def _nearest_palette(rgb: np.ndarray, colors: tuple[RGBColor, ...]) -> np.ndarray:
    flat = rgb.reshape(-1, 3)
    palette = np.asarray(colors, dtype=np.uint8)
    palette_lab = rgb_to_lab(palette)
    output = np.empty_like(flat)
    batch_size = 65_536
    for start in range(0, len(flat), batch_size):
        batch = flat[start : start + batch_size]
        lab = rgb_to_lab(batch)
        distance = np.sum((lab[:, None, :] - palette_lab[None, :, :]) ** 2, axis=-1)
        output[start : start + len(batch)] = palette[np.argmin(distance, axis=1)]
    return output.reshape(rgb.shape)


def _calculate_dithered_palette(
    request: ColorAdjustmentRequest,
    layer: LayerSnapshot,
    cancellation: ColorAdjustmentCancellation,
    progress: ProgressReporter,
) -> dict[TileCoord, RGBAArray | None]:
    parameters = request.parameters
    if not isinstance(parameters, PaletteMapAdjustment):
        raise TypeError("dithered palette calculation requires palette parameters")
    width = request.snapshot.size.width
    height = request.snapshot.size.height
    with _rgba_surface(width, height) as surface:
        for coord, tile in layer.pixels.iter_allocated_tiles():
            left = coord.x * DEFAULT_TILE_SIZE
            top = coord.y * DEFAULT_TILE_SIZE
            valid_width = min(DEFAULT_TILE_SIZE, width - left)
            valid_height = min(DEFAULT_TILE_SIZE, height - top)
            surface[top : top + valid_height, left : left + valid_width] = tile[
                :valid_height, :valid_width
            ]
        with _rgba_surface(width, height) as adjusted:
            _floyd_steinberg(
                surface,
                adjusted,
                parameters.colors,
                cancellation,
                progress,
            )
            updates: dict[TileCoord, RGBAArray | None] = {}
            coords = _affected_coords(request.snapshot, layer, parameters)
            for index, coord in enumerate(coords, start=1):
                cancellation.raise_if_cancelled()
                left = coord.x * DEFAULT_TILE_SIZE
                top = coord.y * DEFAULT_TILE_SIZE
                tile = np.zeros(
                    (DEFAULT_TILE_SIZE, DEFAULT_TILE_SIZE, 4),
                    dtype=np.uint8,
                )
                valid_width = min(DEFAULT_TILE_SIZE, width - left)
                valid_height = min(DEFAULT_TILE_SIZE, height - top)
                tile[:valid_height, :valid_width] = adjusted[
                    top : top + valid_height,
                    left : left + valid_width,
                ]
                before = layer.pixels.read_tile(coord.x, coord.y)
                after = _apply_constraints(
                    before,
                    tile,
                    request.snapshot,
                    layer,
                    coord,
                )
                if not np.array_equal(before, after):
                    updates[coord] = after if np.any(after) else None
                progress(index, len(coords), "整理调色板结果")
            return updates


def _floyd_steinberg(
    rgba: np.ndarray,
    output: np.ndarray,
    colors: tuple[RGBColor, ...],
    cancellation: ColorAdjustmentCancellation,
    progress: ProgressReporter,
) -> None:
    output[:] = rgba
    palette = np.asarray(colors, dtype=np.float32)
    palette_lab = rgb_to_lab(palette)
    current_error = np.zeros((rgba.shape[1] + 2, 3), dtype=np.float32)
    next_error = np.zeros_like(current_error)
    for y in range(rgba.shape[0]):
        cancellation.raise_if_cancelled()
        next_error.fill(0.0)
        for x in range(rgba.shape[1]):
            original = rgba[y, x]
            value = np.clip(original[:3].astype(np.float32) + current_error[x + 1], 0, 255)
            lab = rgb_to_lab(value[None, :])[0]
            index = int(np.argmin(np.sum((palette_lab - lab) ** 2, axis=1)))
            selected = palette[index]
            output[y, x, :3] = np.rint(selected).astype(np.uint8)
            if original[3] == 0:
                continue
            error = value - selected
            current_error[x + 2] += error * (7.0 / 16.0)
            next_error[x] += error * (3.0 / 16.0)
            next_error[x + 1] += error * (5.0 / 16.0)
            next_error[x + 2] += error * (1.0 / 16.0)
        current_error, next_error = next_error, current_error
        progress(y + 1, rgba.shape[0], "应用调色板抖动")


def _affected_coords(
    snapshot: DocumentSnapshot,
    layer: LayerSnapshot,
    parameters: ColorAdjustmentParameters,
) -> tuple[TileCoord, ...]:
    coords = {coord for coord, _tile in layer.pixels.iter_allocated_tiles()}
    if _may_create_alpha(parameters):
        if snapshot.selection.has_nonzero_data:
            coords.update(coord for coord, _tile in snapshot.selection.iter_allocated_tiles())
        else:
            max_tx = (snapshot.size.width - 1) // DEFAULT_TILE_SIZE
            max_ty = (snapshot.size.height - 1) // DEFAULT_TILE_SIZE
            coords.update(
                TileCoord(tx, ty)
                for ty in range(max_ty + 1)
                for tx in range(max_tx + 1)
            )
    return tuple(sorted(coords, key=lambda coord: (coord.y, coord.x)))


def _may_create_alpha(parameters: ColorAdjustmentParameters) -> bool:
    if isinstance(parameters, InvertAdjustment):
        return parameters.channel is ColorChannel.ALPHA
    if isinstance(parameters, CurvesAdjustment):
        return bool(curve_lut(parameters.alpha)[0])
    if isinstance(parameters, GradientMapAdjustment):
        return parameters.use_alpha and parameters.stops[0].color[3] > 0
    if isinstance(parameters, ColorReplaceAdjustment):
        return parameters.replace_alpha and parameters.target[3] > 0
    return False


def _channel_indices(channel: ColorChannel) -> list[int]:
    if channel is ColorChannel.RGB:
        return [0, 1, 2]
    return [
        {
            ColorChannel.RED: 0,
            ColorChannel.GREEN: 1,
            ColorChannel.BLUE: 2,
            ColorChannel.ALPHA: 3,
        }[channel]
    ]


def _endpoint_slope(
    first_h: float,
    second_h: float,
    first_delta: float,
    second_delta: float,
) -> float:
    slope = ((2.0 * first_h + second_h) * first_delta - first_h * second_delta) / (
        first_h + second_h
    )
    if np.sign(slope) != np.sign(first_delta):
        return 0.0
    if np.sign(first_delta) != np.sign(second_delta) and abs(slope) > abs(3.0 * first_delta):
        return 3.0 * first_delta
    return float(slope)


def _clear_outside_canvas(
    tile: RGBAArray,
    coord: TileCoord,
    width: int,
    height: int,
) -> None:
    valid_width = min(DEFAULT_TILE_SIZE, width - coord.x * DEFAULT_TILE_SIZE)
    valid_height = min(DEFAULT_TILE_SIZE, height - coord.y * DEFAULT_TILE_SIZE)
    if valid_width < DEFAULT_TILE_SIZE:
        tile[:, valid_width:] = 0
    if valid_height < DEFAULT_TILE_SIZE:
        tile[valid_height:, :] = 0


@contextmanager
def _rgba_surface(width: int, height: int) -> Iterator[np.ndarray]:
    byte_count = width * height * 4
    if byte_count <= _WORKING_MEMORY_LIMIT:
        yield np.zeros((height, width, 4), dtype=np.uint8)
        return
    with tempfile.TemporaryDirectory(prefix="easypicture-adjustment-") as directory:
        path = Path(directory) / "surface.rgba"
        surface = np.memmap(path, mode="w+", dtype=np.uint8, shape=(height, width, 4))
        surface[:] = 0
        try:
            yield surface
        finally:
            surface.flush()
            del surface


def _layer_by_id(snapshot: DocumentSnapshot, layer_id: UUID) -> LayerSnapshot:
    for layer in snapshot.layers:
        if layer.id == layer_id:
            return layer
    raise ValueError(f"unknown layer id: {layer_id}")


__all__ = [
    "BrightnessContrastAdjustment",
    "ColorAdjustmentKind",
    "ColorAdjustmentParameters",
    "ColorAdjustmentRequest",
    "ColorAdjustmentResult",
    "ColorChannel",
    "ColorReplaceAdjustment",
    "CurveDefinition",
    "CurvesAdjustment",
    "GammaAdjustment",
    "GradientMapAdjustment",
    "GradientStop",
    "HslAdjustment",
    "InvertAdjustment",
    "PaletteMapAdjustment",
    "ThresholdAdjustment",
    "adjustment_changes_alpha",
    "adjustment_kind",
    "calculate_color_adjustment",
    "curve_lut",
    "rgb_to_lab",
]
