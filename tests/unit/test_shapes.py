from __future__ import annotations

import numpy as np
import pytest

from easypicture.domain import Document
from easypicture.imaging import (
    PaintBlendMode,
    ShapeGeometry,
    ShapeKind,
    ShapePaintStyle,
    ShapeRasterRequest,
    ShapeSettings,
    calculate_shape_raster,
    shape_affected_tile_count,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import Rect, TileCoord


def _raster(
    document: Document,
    geometry: ShapeGeometry,
    settings: ShapeSettings,
):
    return calculate_shape_raster(
        ShapeRasterRequest(
            DocumentSnapshot.capture(document),
            document.active_layer_id,
            geometry,
            settings,
        ),
        CancellationToken(),
        lambda _value, _maximum, _message: None,
    )


def test_shape_geometry_validates_boxes_and_polygons() -> None:
    with pytest.raises(ValueError, match="positive"):
        ShapeGeometry(ShapeKind.RECTANGLE, ((10.0, 10.0), (10.0, 20.0)))
    with pytest.raises(ValueError, match="three"):
        ShapeGeometry(ShapeKind.POLYGON, ((0.0, 0.0), (10.0, 0.0)))
    with pytest.raises(ValueError, match="self-intersecting"):
        ShapeGeometry(
            ShapeKind.POLYGON,
            ((0.0, 0.0), (10.0, 10.0), (0.0, 10.0), (10.0, 0.0)),
        )
    with pytest.raises(ValueError, match="duplicate"):
        ShapeGeometry(
            ShapeKind.POLYGON,
            ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 0.0)),
        )

    concave = ShapeGeometry(
        ShapeKind.POLYGON,
        ((0.0, 0.0), (20.0, 0.0), (10.0, 8.0), (20.0, 20.0), (0.0, 20.0)),
    )
    assert concave.bounds == (0.0, 0.0, 20.0, 20.0)


def test_rounded_rectangle_clamps_corner_radius() -> None:
    geometry = ShapeGeometry(
        ShapeKind.ROUNDED_RECTANGLE,
        ((10.0, 20.0), (110.0, 60.0)),
        999.0,
    )
    assert geometry.effective_corner_radius == 20.0


def test_rectangle_uses_independent_fill_and_stroke_colors() -> None:
    document = Document.create(64, 64)
    result = _raster(
        document,
        ShapeGeometry(ShapeKind.RECTANGLE, ((8.0, 8.0), (56.0, 56.0))),
        ShapeSettings(
            style=ShapePaintStyle.STROKE_AND_FILL,
            stroke_color=(255, 0, 0, 255),
            fill_color=(0, 0, 255, 255),
            stroke_width=4.0,
            antialias=False,
        ),
    )
    tile = result.updates[next(iter(result.updates))]
    assert tuple(tile[32, 32]) == (0, 0, 255, 255)
    assert tuple(tile[8, 32]) == (255, 0, 0, 255)
    assert tuple(tile[0, 0]) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    ("kind", "corner_radius"),
    (
        (ShapeKind.RECTANGLE, 0.0),
        (ShapeKind.ROUNDED_RECTANGLE, 6.0),
        (ShapeKind.ELLIPSE, 0.0),
    ),
)
def test_box_stroke_is_centered_on_preview_path(
    kind: ShapeKind,
    corner_radius: float,
) -> None:
    document = Document.create(64, 64)
    result = _raster(
        document,
        ShapeGeometry(kind, ((20.0, 20.0), (44.0, 44.0)), corner_radius),
        ShapeSettings(
            style=ShapePaintStyle.STROKE,
            stroke_color=(255, 0, 0, 255),
            stroke_width=4.0,
            antialias=False,
        ),
    )
    tile = result.updates[TileCoord(0, 0)]
    # QPainter's pen is centered on the path.  A Pillow inset outline would
    # leave these outer pixels transparent and make the final shape smaller.
    assert tile[18, 32, 3] == 255
    assert tile[16, 32, 3] == 0
    assert tile[22, 32, 3] == 0


def test_rectangle_stroke_keeps_right_angle_corners() -> None:
    document = Document.create(64, 64)
    result = _raster(
        document,
        ShapeGeometry(ShapeKind.RECTANGLE, ((20.0, 20.0), (44.0, 44.0))),
        ShapeSettings(
            style=ShapePaintStyle.STROKE,
            stroke_width=4.0,
            antialias=False,
        ),
    )
    tile = result.updates[TileCoord(0, 0)]
    # The preview uses a miter join for ordinary rectangles.  A bevel join
    # would leave the diagonal corner pixel transparent.
    assert tile[18, 18, 3] == 255


def test_line_is_always_stroked_even_with_fill_style() -> None:
    document = Document.create(64, 64)
    result = _raster(
        document,
        ShapeGeometry(ShapeKind.LINE, ((5.0, 32.0), (58.0, 32.0))),
        ShapeSettings(
            style=ShapePaintStyle.FILL,
            stroke_color=(12, 34, 56, 255),
            fill_color=(255, 0, 0, 255),
            stroke_width=3.0,
            antialias=False,
        ),
    )
    tile = result.updates[next(iter(result.updates))]
    assert tuple(tile[32, 30]) == (12, 34, 56, 255)


def test_shape_respects_half_selection_weight() -> None:
    document = Document.create(32, 32)
    selection = np.full((32, 32), 128, dtype=np.uint8)
    document.selection.mask.write_region(Rect(0, 0, 32, 32), selection)
    result = _raster(
        document,
        ShapeGeometry(ShapeKind.RECTANGLE, ((0.0, 0.0), (31.0, 31.0))),
        ShapeSettings(
            style=ShapePaintStyle.FILL,
            fill_color=(255, 0, 0, 255),
            antialias=False,
        ),
    )
    tile = result.updates[next(iter(result.updates))]
    assert tuple(tile[16, 16]) == (255, 0, 0, 128)


def test_shape_alpha_lock_preserves_transparent_pixels_and_alpha() -> None:
    document = Document.create(32, 32)
    layer = document.active_layer
    original = np.zeros((256, 256, 4), dtype=np.uint8)
    original[:32, :32] = (10, 20, 30, 0)
    original[16, 16] = (20, 40, 60, 100)
    layer.pixels.replace_tile(0, 0, original)
    layer.alpha_locked = True

    result = _raster(
        document,
        ShapeGeometry(ShapeKind.RECTANGLE, ((0.0, 0.0), (31.0, 31.0))),
        ShapeSettings(
            style=ShapePaintStyle.FILL,
            fill_color=(255, 0, 0, 255),
            blend_mode=PaintBlendMode.REPLACE,
            antialias=False,
        ),
    )
    tile = result.updates[next(iter(result.updates))]
    assert tuple(tile[0, 0]) == (10, 20, 30, 0)
    assert tile[16, 16, 3] == 100
    assert tuple(tile[16, 16, :3]) != (20, 40, 60)


def test_antialiased_shape_has_no_empty_tile_seam() -> None:
    document = Document.create(520, 64)
    geometry = ShapeGeometry(ShapeKind.LINE, ((4.0, 32.0), (516.0, 32.0)))
    settings = ShapeSettings(
        stroke_color=(255, 255, 255, 255),
        stroke_width=5.0,
        antialias=True,
    )
    result = _raster(document, geometry, settings)

    left = result.updates[next(coord for coord in result.updates if coord.x == 0)]
    right = result.updates[next(coord for coord in result.updates if coord.x == 1)]
    assert left[32, 255, 3] > 0
    assert right[32, 0, 3] > 0
    assert abs(int(left[32, 255, 3]) - int(right[32, 0, 3])) <= 1
    assert shape_affected_tile_count(geometry, settings, 520, 64) == 3


@pytest.mark.parametrize("mode", tuple(PaintBlendMode))
def test_all_paint_blend_modes_generate_shape_updates(mode: PaintBlendMode) -> None:
    document = Document.create(64, 64)
    base = np.zeros((256, 256, 4), dtype=np.uint8)
    base[:64, :64] = (80, 120, 160, 255)
    document.active_layer.pixels.replace_tile(0, 0, base)
    result = _raster(
        document,
        ShapeGeometry(ShapeKind.ELLIPSE, ((8.0, 8.0), (56.0, 56.0))),
        ShapeSettings(
            style=ShapePaintStyle.FILL,
            fill_color=(200, 40, 90, 180),
            blend_mode=mode,
        ),
    )
    assert TileCoord(0, 0) in result.updates
    assert not np.array_equal(result.updates[TileCoord(0, 0)], base)


def test_shape_clips_outside_canvas_and_concave_polygon_fills() -> None:
    document = Document.create(64, 64)
    geometry = ShapeGeometry(
        ShapeKind.POLYGON,
        ((-20.0, 5.0), (50.0, 5.0), (25.0, 25.0), (50.0, 60.0), (-20.0, 60.0)),
    )
    result = _raster(
        document,
        geometry,
        ShapeSettings(
            style=ShapePaintStyle.FILL,
            fill_color=(20, 180, 80, 255),
        ),
    )
    tile = result.updates[TileCoord(0, 0)]
    assert tile[10, 2, 3] > 0
    assert tile[30, 45, 3] == 0
    assert np.all(tile[64:, :, :] == 0)
    assert np.all(tile[:, 64:, :] == 0)
