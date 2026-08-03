"""Qt-free editable-shape geometry and Tile rasterization."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import numpy as np
from PIL import Image, ImageDraw

from easypicture.imaging.brush import PaintBlendMode, RGBAColor, blend_color
from easypicture.persistence.snapshots import DocumentSnapshot, LayerSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, RGBAArray, TileCoord

type ShapePoint = tuple[float, float]
type ProgressReporter = Callable[[int, int, str], None]

_ANTIALIAS_SCALE = 4
_RASTER_PADDING = 2
_GEOMETRY_EPSILON = 1e-6


class ShapeCancellation(Protocol):
    def raise_if_cancelled(self) -> None: ...


class ShapeKind(StrEnum):
    LINE = "line"
    RECTANGLE = "rectangle"
    ROUNDED_RECTANGLE = "rounded_rectangle"
    ELLIPSE = "ellipse"
    POLYLINE = "polyline"
    POLYGON = "polygon"


class ShapeOutputTarget(StrEnum):
    RASTER = "raster"
    VECTOR = "vector"


class ShapePaintStyle(StrEnum):
    STROKE = "stroke"
    FILL = "fill"
    STROKE_AND_FILL = "stroke_and_fill"


@dataclass(frozen=True, slots=True)
class ShapeGeometry:
    kind: ShapeKind
    points: tuple[ShapePoint, ...]
    corner_radius: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.corner_radius) or self.corner_radius < 0.0:
            raise ValueError("shape corner radius must be finite and non-negative")
        if any(not math.isfinite(value) for point in self.points for value in point):
            raise ValueError("shape points must be finite")
        if self.kind is ShapeKind.LINE:
            if len(self.points) != 2 or _points_equal(*self.points):
                raise ValueError("line geometry requires two distinct points")
        elif self.kind in (
            ShapeKind.RECTANGLE,
            ShapeKind.ROUNDED_RECTANGLE,
            ShapeKind.ELLIPSE,
        ):
            if len(self.points) != 2:
                raise ValueError("box geometry requires two corner points")
            left, top, right, bottom = self.bounds
            if right - left <= _GEOMETRY_EPSILON or bottom - top <= _GEOMETRY_EPSILON:
                raise ValueError("box geometry must have positive dimensions")
        elif self.kind is ShapeKind.POLYLINE:
            _validate_polyline(self.points)
        elif self.kind is ShapeKind.POLYGON:
            _validate_polygon(self.points)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        xs = tuple(point[0] for point in self.points)
        ys = tuple(point[1] for point in self.points)
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def effective_corner_radius(self) -> float:
        if self.kind is not ShapeKind.ROUNDED_RECTANGLE:
            return 0.0
        left, top, right, bottom = self.bounds
        return min(self.corner_radius, (right - left) / 2.0, (bottom - top) / 2.0)


@dataclass(frozen=True, slots=True)
class ShapeSettings:
    style: ShapePaintStyle = ShapePaintStyle.STROKE
    stroke_color: RGBAColor = (0, 0, 0, 255)
    fill_color: RGBAColor = (255, 255, 255, 255)
    stroke_width: float = 1.0
    opacity: float = 1.0
    antialias: bool = True
    blend_mode: PaintBlendMode = PaintBlendMode.NORMAL

    def __post_init__(self) -> None:
        if not all(
            0 <= channel <= 255
            for color in (self.stroke_color, self.fill_color)
            for channel in color
        ):
            raise ValueError("shape RGBA channels must be between 0 and 255")
        if not math.isfinite(self.stroke_width) or not 1.0 <= self.stroke_width <= 8192.0:
            raise ValueError("shape stroke width must be between 1 and 8192")
        if not math.isfinite(self.opacity) or not 0.0 <= self.opacity <= 1.0:
            raise ValueError("shape opacity must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ShapeRasterRequest:
    snapshot: DocumentSnapshot
    layer_id: UUID
    geometry: ShapeGeometry
    settings: ShapeSettings


@dataclass(frozen=True, slots=True)
class ShapeRasterResult:
    document_id: UUID
    revision: int
    layer_id: UUID
    updates: Mapping[TileCoord, RGBAArray]


def calculate_shape_raster(
    request: ShapeRasterRequest,
    cancellation: ShapeCancellation,
    progress: ProgressReporter,
) -> ShapeRasterResult:
    """Rasterize one shape over an immutable layer snapshot."""

    layer = _layer_by_id(request.snapshot, request.layer_id)
    if layer.locked:
        raise PermissionError("layer is locked")
    coords = _affected_tiles(request.snapshot, request.geometry, request.settings)
    updates: dict[TileCoord, RGBAArray] = {}
    selection_active = request.snapshot.selection.has_nonzero_data
    for index, coord in enumerate(coords, start=1):
        cancellation.raise_if_cancelled()
        fill_coverage, stroke_coverage = _tile_coverages(
            request.geometry,
            request.settings,
            coord,
        )
        if selection_active:
            selection = request.snapshot.selection.read_tile(coord.x, coord.y)
            weight = selection.astype(np.float32) / 255.0
            fill_coverage *= weight
            stroke_coverage *= weight
        fill_coverage *= request.settings.opacity
        stroke_coverage *= request.settings.opacity

        before = layer.pixels.read_tile(coord.x, coord.y)
        after = before.copy(order="C")
        if (
            request.geometry.kind not in (ShapeKind.LINE, ShapeKind.POLYLINE)
            and request.settings.style
            in (
                ShapePaintStyle.FILL,
                ShapePaintStyle.STROKE_AND_FILL,
            )
            and np.any(fill_coverage > 0.0)
        ):
            after = blend_color(
                after,
                request.settings.fill_color,
                fill_coverage,
                request.settings.blend_mode,
            )
        if (
            request.geometry.kind in (ShapeKind.LINE, ShapeKind.POLYLINE)
            or request.settings.style
            in (
                ShapePaintStyle.STROKE,
                ShapePaintStyle.STROKE_AND_FILL,
            )
        ) and np.any(stroke_coverage > 0.0):
            after = blend_color(
                after,
                request.settings.stroke_color,
                stroke_coverage,
                request.settings.blend_mode,
            )
        if layer.alpha_locked:
            transparent = before[..., 3] == 0
            after[..., 3] = before[..., 3]
            after[transparent] = before[transparent]
        _clear_tile_outside_canvas(
            after,
            coord,
            request.snapshot.size.width,
            request.snapshot.size.height,
        )
        if not np.array_equal(before, after):
            updates[coord] = after
        progress(index, len(coords), "生成图形预览")
    return ShapeRasterResult(
        document_id=request.snapshot.document_id,
        revision=request.snapshot.revision,
        layer_id=layer.id,
        updates=updates,
    )


def shape_affected_pixel_area(geometry: ShapeGeometry, settings: ShapeSettings) -> int:
    left, top, right, bottom = _expanded_bounds(geometry, settings)
    return max(0, math.ceil(right) - math.floor(left)) * max(
        0,
        math.ceil(bottom) - math.floor(top),
    )


def shape_affected_tile_count(
    geometry: ShapeGeometry,
    settings: ShapeSettings,
    width: int,
    height: int,
) -> int:
    return len(_coords_for_bounds(_expanded_bounds(geometry, settings), width, height))


def _affected_tiles(
    snapshot: DocumentSnapshot,
    geometry: ShapeGeometry,
    settings: ShapeSettings,
) -> tuple[TileCoord, ...]:
    return _coords_for_bounds(
        _expanded_bounds(geometry, settings),
        snapshot.size.width,
        snapshot.size.height,
    )


def _coords_for_bounds(
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[TileCoord, ...]:
    left, top, right, bottom = bounds
    first_x = max(0, math.floor(left))
    first_y = max(0, math.floor(top))
    last_x = min(width, math.ceil(right))
    last_y = min(height, math.ceil(bottom))
    if last_x <= first_x or last_y <= first_y:
        return ()
    return tuple(
        TileCoord(tx, ty)
        for ty in range(first_y // DEFAULT_TILE_SIZE, (last_y - 1) // DEFAULT_TILE_SIZE + 1)
        for tx in range(first_x // DEFAULT_TILE_SIZE, (last_x - 1) // DEFAULT_TILE_SIZE + 1)
    )


def _expanded_bounds(
    geometry: ShapeGeometry,
    settings: ShapeSettings,
) -> tuple[float, float, float, float]:
    left, top, right, bottom = geometry.bounds
    padding = settings.stroke_width / 2.0 + (1.0 if settings.antialias else 0.0)
    return left - padding, top - padding, right + padding, bottom + padding


def _tile_coverages(
    geometry: ShapeGeometry,
    settings: ShapeSettings,
    coord: TileCoord,
) -> tuple[np.ndarray, np.ndarray]:
    scale = _ANTIALIAS_SCALE if settings.antialias else 1
    # The preview uses a centered QPainter pen.  Reserve the complete outer
    # half-width here so the raster result cannot clip the outside half of a
    # thick stroke at the tile edge.
    padding = math.ceil(settings.stroke_width / 2.0) + (
        _RASTER_PADDING if settings.antialias else 0
    )
    side = (DEFAULT_TILE_SIZE + padding * 2) * scale
    fill_image = Image.new("L", (side, side), 0)
    stroke_image = Image.new("L", (side, side), 0)
    tile_left = coord.x * DEFAULT_TILE_SIZE - padding
    tile_top = coord.y * DEFAULT_TILE_SIZE - padding

    def local(point: ShapePoint) -> ShapePoint:
        return (
            (point[0] - tile_left) * scale,
            (point[1] - tile_top) * scale,
        )

    points = tuple(local(point) for point in geometry.points)
    width = max(1, round(settings.stroke_width * scale))
    fill_draw = ImageDraw.Draw(fill_image)
    stroke_draw = ImageDraw.Draw(stroke_image)
    if geometry.kind in (ShapeKind.LINE, ShapeKind.POLYLINE):
        _draw_round_line(stroke_draw, points, width)
    elif geometry.kind is ShapeKind.RECTANGLE:
        box = _normalized_box(points)
        fill_draw.rectangle(box, fill=255)
        _draw_centered_stroke(stroke_draw, ShapeKind.RECTANGLE, box, width)
    elif geometry.kind is ShapeKind.ROUNDED_RECTANGLE:
        box = _normalized_box(points)
        radius = geometry.effective_corner_radius * scale
        fill_draw.rounded_rectangle(box, radius=radius, fill=255)
        _draw_centered_stroke(
            stroke_draw,
            ShapeKind.ROUNDED_RECTANGLE,
            box,
            width,
            radius,
        )
    elif geometry.kind is ShapeKind.ELLIPSE:
        box = _normalized_box(points)
        fill_draw.ellipse(box, fill=255)
        _draw_centered_stroke(stroke_draw, ShapeKind.ELLIPSE, box, width)
    else:
        fill_draw.polygon(points, fill=255)
        _draw_round_line(stroke_draw, (*points, points[0]), width)

    fill = _mask_array(fill_image, scale, padding)
    stroke = _mask_array(stroke_image, scale, padding)
    return fill, stroke


def _mask_array(image: Image.Image, scale: int, padding: int) -> np.ndarray:
    if scale > 1:
        side = DEFAULT_TILE_SIZE + padding * 2
        image = image.resize((side, side), Image.Resampling.LANCZOS)
    if padding:
        image = image.crop(
            (
                padding,
                padding,
                padding + DEFAULT_TILE_SIZE,
                padding + DEFAULT_TILE_SIZE,
            )
        )
    return np.asarray(image, dtype=np.float32) / 255.0


def _draw_round_line(
    draw: ImageDraw.ImageDraw,
    points: tuple[ShapePoint, ...],
    width: int,
) -> None:
    draw.line(points, fill=255, width=width, joint="curve")
    radius = width / 2.0
    for point in points:
        draw.ellipse(
            (
                point[0] - radius,
                point[1] - radius,
                point[0] + radius,
                point[1] + radius,
            ),
            fill=255,
        )


def _draw_centered_stroke(
    draw: ImageDraw.ImageDraw,
    kind: ShapeKind,
    box: tuple[float, float, float, float],
    width: int,
    radius: float = 0.0,
) -> None:
    """Draw a centered outline like a QPainter path pen.

    Pillow's ``outline=`` is inset into a rectangle/ellipse.  Drawing an
    expanded outer shape and subtracting the contracted inner shape gives the
    same centerline semantics as the live QGraphicsPathItem preview.
    """
    if kind is ShapeKind.ROUNDED_RECTANGLE and radius <= _GEOMETRY_EPSILON:
        # Qt treats a zero-radius rounded rectangle as a regular rectangle,
        # including its default BevelJoin corners.
        kind = ShapeKind.RECTANGLE
    half = width / 2.0
    outer = _expanded_box(box, half)
    if kind is ShapeKind.ROUNDED_RECTANGLE:
        outer_radius = radius + half
        draw.rounded_rectangle(outer, radius=outer_radius, fill=255)
    elif kind is ShapeKind.ELLIPSE:
        draw.ellipse(outer, fill=255)
    else:
        # Ordinary rectangles use a miter join in the live preview, so their
        # outer corners remain right angles instead of being beveled.
        draw.rectangle(outer, fill=255)

    inner = _contracted_box(box, half)
    if inner is None:
        return
    if kind is ShapeKind.ROUNDED_RECTANGLE:
        inner_radius = max(0.0, radius - half)
        draw.rounded_rectangle(inner, radius=inner_radius, fill=0)
    elif kind is ShapeKind.ELLIPSE:
        draw.ellipse(inner, fill=0)
    else:
        draw.rectangle(inner, fill=0)


def _expanded_box(
    box: tuple[float, float, float, float],
    amount: float,
) -> tuple[float, float, float, float]:
    left, top, right, bottom = box
    return left - amount, top - amount, right + amount, bottom + amount


def _contracted_box(
    box: tuple[float, float, float, float],
    amount: float,
) -> tuple[float, float, float, float] | None:
    left, top, right, bottom = box
    contracted = (left + amount, top + amount, right - amount, bottom - amount)
    if contracted[2] <= contracted[0] or contracted[3] <= contracted[1]:
        return None
    return contracted


def _normalized_box(points: tuple[ShapePoint, ...]) -> tuple[float, float, float, float]:
    first, second = points
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[0], second[0]),
        max(first[1], second[1]),
    )


def _clear_tile_outside_canvas(
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


def _layer_by_id(snapshot: DocumentSnapshot, layer_id: UUID) -> LayerSnapshot:
    for layer in snapshot.raster_layers:
        if layer.id == layer_id:
            return layer
    raise ValueError(f"unknown layer id: {layer_id}")


def _validate_polygon(points: tuple[ShapePoint, ...]) -> None:
    if len(points) < 3:
        raise ValueError("polygon requires at least three points")
    for first in range(len(points)):
        for second in range(first + 1, len(points)):
            if _points_equal(points[first], points[second]):
                raise ValueError("polygon contains duplicate points")
    for index, point in enumerate(points):
        if _points_equal(point, points[(index + 1) % len(points)]):
            raise ValueError("polygon contains duplicate adjacent points")
    count = len(points)
    for first in range(count):
        a1 = points[first]
        a2 = points[(first + 1) % count]
        for second in range(first + 1, count):
            if second in (first, (first + 1) % count) or (second + 1) % count == first:
                continue
            b1 = points[second]
            b2 = points[(second + 1) % count]
            if _segments_intersect(a1, a2, b1, b2):
                raise ValueError("self-intersecting polygons are not supported")
    if abs(_polygon_signed_area(points)) <= _GEOMETRY_EPSILON:
        raise ValueError("polygon area must be non-zero")


def _validate_polyline(points: tuple[ShapePoint, ...]) -> None:
    if len(points) < 2:
        raise ValueError("polyline requires at least two points")
    if all(_points_equal(points[0], point) for point in points[1:]):
        raise ValueError("polyline requires at least two distinct points")
    if any(_points_equal(start, end) for start, end in zip(points, points[1:], strict=False)):
        raise ValueError("polyline contains duplicate adjacent points")


def _polygon_signed_area(points: tuple[ShapePoint, ...]) -> float:
    return 0.5 * sum(
        point[0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * point[1]
        for index, point in enumerate(points)
    )


def _segments_intersect(
    a1: ShapePoint,
    a2: ShapePoint,
    b1: ShapePoint,
    b2: ShapePoint,
) -> bool:
    def orientation(p: ShapePoint, q: ShapePoint, r: ShapePoint) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    values = (
        orientation(a1, a2, b1),
        orientation(a1, a2, b2),
        orientation(b1, b2, a1),
        orientation(b1, b2, a2),
    )
    if values[0] * values[1] < -_GEOMETRY_EPSILON and values[2] * values[3] < -_GEOMETRY_EPSILON:
        return True

    def on_segment(start: ShapePoint, end: ShapePoint, point: ShapePoint) -> bool:
        return (
            min(start[0], end[0]) - _GEOMETRY_EPSILON
            <= point[0]
            <= max(start[0], end[0]) + _GEOMETRY_EPSILON
            and min(start[1], end[1]) - _GEOMETRY_EPSILON
            <= point[1]
            <= max(start[1], end[1]) + _GEOMETRY_EPSILON
        )

    return (
        abs(values[0]) <= _GEOMETRY_EPSILON
        and on_segment(a1, a2, b1)
        or abs(values[1]) <= _GEOMETRY_EPSILON
        and on_segment(a1, a2, b2)
        or abs(values[2]) <= _GEOMETRY_EPSILON
        and on_segment(b1, b2, a1)
        or abs(values[3]) <= _GEOMETRY_EPSILON
        and on_segment(b1, b2, a2)
    )


def _points_equal(first: ShapePoint, second: ShapePoint) -> bool:
    return math.isclose(first[0], second[0], abs_tol=_GEOMETRY_EPSILON) and math.isclose(
        first[1],
        second[1],
        abs_tol=_GEOMETRY_EPSILON,
    )


__all__ = [
    "ShapeGeometry",
    "ShapeKind",
    "ShapePaintStyle",
    "ShapeOutputTarget",
    "ShapeRasterRequest",
    "ShapeRasterResult",
    "ShapeSettings",
    "calculate_shape_raster",
    "shape_affected_pixel_area",
    "shape_affected_tile_count",
]
