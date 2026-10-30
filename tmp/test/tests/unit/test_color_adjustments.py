from __future__ import annotations

import numpy as np
import pytest

from easypicture.domain import Document
from easypicture.imaging import (
    BrightnessContrastAdjustment,
    ColorAdjustmentRequest,
    ColorChannel,
    ColorReplaceAdjustment,
    CurveDefinition,
    CurvesAdjustment,
    GammaAdjustment,
    GradientMapAdjustment,
    GradientStop,
    HslAdjustment,
    InvertAdjustment,
    PaletteMapAdjustment,
    ThresholdAdjustment,
    calculate_color_adjustment,
    curve_lut,
    rgb_to_lab,
)
from easypicture.jobs import CancellationToken, JobCancelled
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import Rect, TileCoord


def _document(
    color: tuple[int, int, int, int] = (40, 80, 120, 200),
    *,
    width: int = 32,
    height: int = 32,
) -> Document:
    document = Document.create(width, height)
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[:height, :width] = color
    document.active_layer.pixels.replace_tile(0, 0, tile)
    return document


def _calculate(document: Document, parameters: object):
    return calculate_color_adjustment(
        ColorAdjustmentRequest(
            DocumentSnapshot.capture(document),
            document.active_layer_id,
            parameters,  # type: ignore[arg-type]
        ),
        CancellationToken(),
        lambda _value, _maximum, _message: None,
    )


def _tile(document: Document, parameters: object) -> np.ndarray:
    result = _calculate(document, parameters)
    update = result.updates[TileCoord(0, 0)]
    assert update is not None
    return update


def test_curve_definition_and_pchip_lut_are_bounded() -> None:
    curve = CurveDefinition(((0, 0), (64, 180), (180, 30), (255, 255)))
    lut = curve_lut(curve)
    assert lut.shape == (256,)
    assert lut.dtype == np.uint8
    assert lut[0] == 0
    assert lut[255] == 255
    with pytest.raises(ValueError, match="strictly"):
        CurveDefinition(((0, 0), (64, 100), (64, 120), (255, 255)))


def test_invert_preserves_alpha_and_hidden_rgb_is_adjusted() -> None:
    document = _document((10, 20, 30, 0))
    tile = _tile(document, InvertAdjustment())
    assert tuple(tile[0, 0]) == (245, 235, 225, 0)
    alpha = _tile(document, InvertAdjustment(ColorChannel.ALPHA))
    assert tuple(alpha[0, 0]) == (10, 20, 30, 255)


def test_threshold_and_half_selection_weight() -> None:
    document = _document((100, 100, 100, 200))
    selection = np.full((32, 32), 128, dtype=np.uint8)
    document.selection.mask.write_region(Rect(0, 0, 32, 32), selection)
    tile = _tile(document, ThresholdAdjustment(128))
    assert tuple(tile[0, 0]) == (50, 50, 50, 200)


def test_brightness_contrast_gamma_and_curves_generate_luts() -> None:
    document = _document((64, 128, 192, 64))
    brighter = _tile(document, BrightnessContrastAdjustment(brightness=20))
    assert np.all(brighter[0, 0, :3] > (64, 128, 192))

    gamma = _tile(document, GammaAdjustment(2.0, ColorChannel.RED))
    assert gamma[0, 0, 0] > 64
    assert tuple(gamma[0, 0, 1:]) == (128, 192, 64)

    curves = CurvesAdjustment(
        rgb=CurveDefinition(((0, 255), (255, 0))),
        alpha=CurveDefinition(((0, 255), (255, 0))),
    )
    curved = _tile(document, curves)
    assert tuple(curved[0, 0]) == (191, 127, 63, 191)


def test_hsl_rotates_red_to_green_and_preserves_alpha() -> None:
    document = _document((255, 0, 0, 77))
    tile = _tile(document, HslAdjustment(hue=120))
    assert tuple(tile[0, 0]) == (0, 255, 0, 77)


def test_gradient_map_uses_rec709_luminance_and_optional_alpha() -> None:
    document = _document((255, 255, 255, 90))
    parameters = GradientMapAdjustment(
        (
            GradientStop(0.0, (10, 20, 30, 0)),
            GradientStop(1.0, (100, 110, 120, 220)),
        ),
        use_alpha=False,
    )
    tile = _tile(document, parameters)
    assert tuple(tile[0, 0]) == (100, 110, 120, 90)
    with_alpha = _tile(
        document,
        GradientMapAdjustment(parameters.stops, use_alpha=True),
    )
    assert tuple(with_alpha[0, 0]) == (100, 110, 120, 220)


def test_color_replace_tolerance_softness_and_alpha_controls() -> None:
    document = _document((100, 110, 120, 130))
    exact = _tile(
        document,
        ColorReplaceAdjustment(
            source=(100, 110, 120, 255),
            target=(200, 10, 20, 30),
            tolerance=0,
        ),
    )
    assert tuple(exact[0, 0]) == (200, 10, 20, 130)

    partial = _tile(
        document,
        ColorReplaceAdjustment(
            source=(90, 110, 120, 255),
            target=(200, 10, 20, 30),
            tolerance=20,
            softness=20,
            replace_alpha=True,
        ),
    )
    assert tuple(partial[0, 0]) == (150, 60, 70, 80)


def test_lab_palette_uses_perceptual_nearest_color() -> None:
    palette = ((0, 0, 0), (255, 255, 255), (255, 0, 0))
    source = np.asarray([[[240, 30, 20]]], dtype=np.uint8)
    lab = rgb_to_lab(source)
    assert lab.shape == (1, 1, 3)
    document = _document((240, 30, 20, 255))
    tile = _tile(document, PaletteMapAdjustment(palette))
    assert tuple(tile[0, 0]) == (255, 0, 0, 255)


def test_palette_dither_is_deterministic_across_tile_boundary() -> None:
    document = Document.create(260, 2)
    tile0 = np.zeros((256, 256, 4), dtype=np.uint8)
    tile1 = np.zeros((256, 256, 4), dtype=np.uint8)
    tile0[:2] = (127, 127, 127, 255)
    tile1[:2, :4] = (127, 127, 127, 255)
    document.active_layer.pixels.replace_tile(0, 0, tile0)
    document.active_layer.pixels.replace_tile(1, 0, tile1)
    result = _calculate(
        document,
        PaletteMapAdjustment(((0, 0, 0), (255, 255, 255)), dither=True),
    )
    left = result.updates[TileCoord(0, 0)]
    right = result.updates[TileCoord(1, 0)]
    assert left is not None and right is not None
    sequence = np.concatenate((left[0, :256, 0], right[0, :4, 0]))
    assert set(int(value) for value in sequence) == {0, 255}
    second = _calculate(
        document,
        PaletteMapAdjustment(((0, 0, 0), (255, 255, 255)), dither=True),
    )
    assert np.array_equal(second.updates[TileCoord(0, 0)], left)
    assert np.array_equal(second.updates[TileCoord(1, 0)], right)


def test_alpha_lock_matches_command_semantics() -> None:
    document = _document((20, 40, 60, 0))
    tile = document.active_layer.pixels.read_tile(0, 0).copy()
    tile[1, 1] = (20, 40, 60, 100)
    document.active_layer.pixels.replace_tile(0, 0, tile)
    document.active_layer.alpha_locked = True
    result = _tile(document, InvertAdjustment())
    assert tuple(result[0, 0]) == (20, 40, 60, 0)
    assert tuple(result[1, 1]) == (235, 215, 195, 100)


def test_cancelled_adjustment_aborts_before_mutation() -> None:
    document = _document()
    token = CancellationToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        calculate_color_adjustment(
            ColorAdjustmentRequest(
                DocumentSnapshot.capture(document),
                document.active_layer_id,
                InvertAdjustment(),
            ),
            token,
            lambda _value, _maximum, _message: None,
        )
