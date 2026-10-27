from __future__ import annotations

import numpy as np

from easypicture.imaging import (
    BrushSettings,
    BrushStrokeEngine,
    PaintBlendMode,
    blend_color,
    erase_alpha,
)
from easypicture.pixels import MaskTileStore, Rect, TileCoord, TilePixelStore


class MemorySurface:
    def __init__(self, width: int = 256, height: int = 256) -> None:
        self.width = width
        self.height = height
        self.origin_x = 0
        self.origin_y = 0
        self.document_width = width
        self.document_height = height
        self.pixels = TilePixelStore()
        self.selection = MaskTileStore()
        self.selection_active = False

    def writable_tile(self, coord: TileCoord) -> np.ndarray:
        return self.pixels.writable_tile(coord.x, coord.y)

    def selection_region(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> np.ndarray | None:
        if not self.selection_active:
            return None
        return self.selection.read_region(Rect(x, y, width, height))


def test_fast_brush_interpolation_has_no_gaps() -> None:
    surface = MemorySurface(256, 64)
    settings = BrushSettings(size=12, hardness=1.0, spacing=0.25, antialias=False)
    stroke = BrushStrokeEngine(surface, settings, (255, 0, 0, 255))

    stroke.begin(12.0, 32.0)
    stroke.extend(220.0, 32.0)

    row = surface.pixels.read_tile(0, 0)[32, 12:220, 3]
    assert np.all(row > 0)


def test_spacing_residual_is_carried_across_pointer_events() -> None:
    surface = MemorySurface(128, 64)
    settings = BrushSettings(size=10, hardness=1.0, spacing=1.0, antialias=False)
    changed: list[frozenset[TileCoord]] = []
    stroke = BrushStrokeEngine(surface, settings, (0, 0, 0, 255), changed=changed.append)

    stroke.begin(10.0, 30.0)
    stroke.extend(16.0, 30.0)
    stroke.extend(21.0, 30.0)

    alpha = surface.pixels.read_tile(0, 0)[30, :, 3]
    assert alpha[10] > 0
    assert alpha[20] > 0
    assert len(changed) == 2


def test_half_transparent_brush_uses_straight_rgba_source_over() -> None:
    destination = np.zeros((1, 1, 4), dtype=np.uint8)
    destination[0, 0] = (0, 0, 255, 128)

    actual = blend_color(destination, (255, 0, 0, 128), np.ones((1, 1), dtype=np.float32))

    assert tuple(actual[0, 0]) == (170, 0, 85, 192)


def test_replace_and_common_blend_modes_have_distinct_pixel_semantics() -> None:
    destination = np.array([[[100, 150, 200, 255]]], dtype=np.uint8)
    coverage = np.ones((1, 1), dtype=np.float32)

    replaced = blend_color(
        destination,
        (200, 100, 50, 128),
        coverage,
        PaintBlendMode.REPLACE,
    )
    multiplied = blend_color(
        destination,
        (200, 100, 50, 255),
        coverage,
        PaintBlendMode.MULTIPLY,
    )
    screened = blend_color(
        destination,
        (200, 100, 50, 255),
        coverage,
        PaintBlendMode.SCREEN,
    )

    assert tuple(replaced[0, 0]) == (200, 100, 50, 128)
    assert tuple(multiplied[0, 0]) == (78, 59, 39, 255)
    assert tuple(screened[0, 0]) == (222, 191, 211, 255)


def test_eraser_preserves_rgb_when_alpha_reaches_zero() -> None:
    destination = np.array([[[17, 29, 41, 200]]], dtype=np.uint8)

    actual = erase_alpha(destination, np.ones((1, 1), dtype=np.float32))

    assert tuple(actual[0, 0]) == (17, 29, 41, 0)


def test_selection_mask_scales_brush_coverage() -> None:
    surface = MemorySurface(32, 32)
    surface.selection_active = True
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[8:24, 8:16] = 255
    surface.selection.replace_tile(0, 0, mask)
    stroke = BrushStrokeEngine(
        surface,
        BrushSettings(size=20, hardness=1.0, antialias=False),
        (255, 0, 0, 255),
    )

    stroke.begin(16.0, 16.0)

    tile = surface.pixels.read_tile(0, 0)
    assert tile[16, 12, 3] == 255
    assert tile[16, 20, 3] == 0


def test_large_dab_reports_nine_crossed_tiles() -> None:
    surface = MemorySurface(768, 768)
    changed: list[frozenset[TileCoord]] = []
    stroke = BrushStrokeEngine(
        surface,
        BrushSettings(size=800, hardness=1.0, antialias=False),
        (255, 0, 0, 255),
        changed=changed.append,
    )

    stroke.begin(384.0, 384.0)

    assert len(changed[-1]) == 9
