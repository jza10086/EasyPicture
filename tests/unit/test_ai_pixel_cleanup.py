from dataclasses import replace

import numpy as np
import pytest

from easypicture.domain import Document
from easypicture.imaging.ai_palette import oklab_to_srgb, srgb_to_oklab
from easypicture.imaging.ai_pixel_cleanup import (
    AiPixelCleanupCache,
    AiPixelCleanupParameters,
    AiPixelCleanupRequest,
    PixelCleanupAlpha,
    PixelCleanupDither,
    PixelCleanupOutput,
    PixelCleanupSelection,
    PixelGridSampling,
    calculate_ai_pixel_cleanup,
)
from easypicture.jobs import CancellationToken, JobCancelled
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import Rect, TilePixelStore


def make_document(rgba):
    document = Document.create(rgba.shape[1], rgba.shape[0])
    document.active_layer.pixels.write_region(Rect(0, 0, document.width, document.height), rgba)
    return document


def calculate(document, params=None, cache=None):
    params = params or AiPixelCleanupParameters()
    result = calculate_ai_pixel_cleanup(
        AiPixelCleanupRequest(DocumentSnapshot.capture(document), document.active_layer_id, params),
        CancellationToken(),
        lambda *_args: None,
        cache,
    )
    pixels = TilePixelStore.from_readonly_tiles(result.updates).read_region(
        Rect(0, 0, document.width, document.height)
    )
    return result, pixels


def test_oklab_reference_and_srgb_roundtrip():
    values = np.array(
        [
            [0, 0, 0],
            [255, 255, 255],
            [255, 0, 0],
            [0, 255, 0],
            [0, 0, 255],
            [17, 54, 190],
            [1, 2, 3],
        ],
        dtype=np.uint8,
    )
    lab = srgb_to_oklab(values)
    np.testing.assert_allclose(lab[2], [0.62795536, 0.22486306, 0.1258463], atol=1e-7)
    np.testing.assert_array_equal(oklab_to_srgb(lab), values)
    assert oklab_to_srgb(np.array([[0.5, 1.0, 1.0]])).dtype == np.uint8


def test_exact_colors_in_one_bucket_remain_exact_and_hidden_rgb_untouched():
    rgba = np.array(
        [[[100, 100, 100, 255], [101, 102, 103, 128], [102, 103, 101, 255], [250, 1, 9, 0]]],
        dtype=np.uint8,
    )
    result, actual = calculate(make_document(rgba), AiPixelCleanupParameters(target_colors=4))
    assert result.source_color_count == 3
    assert len(result.palette) == 3
    assert not result.changed
    np.testing.assert_array_equal(actual, rgba)


@pytest.mark.parametrize("count", [1, 16, 256])
def test_palette_determinism_count_and_locks(count):
    rng = np.random.default_rng(62)
    rgba = rng.integers(0, 256, (18, 30, 4), dtype=np.uint8)
    rgba[..., 3] = 255
    doc = make_document(rgba)
    locked = ((12, 34, 56),)
    params = AiPixelCleanupParameters(target_colors=count, locked_colors=locked)
    first, pixels = calculate(doc, params)
    second, again = calculate(doc, params)
    assert first.palette == second.palette
    assert locked[0] in first.palette
    assert len(first.palette) <= count
    assert first.result_color_count <= count
    np.testing.assert_array_equal(pixels, again)


def test_empty_and_validation():
    result, _ = calculate(make_document(np.zeros((4, 4, 4), dtype=np.uint8)))
    assert not result.palette and not result.updates and not result.changed
    with pytest.raises(ValueError, match="锁定"):
        AiPixelCleanupParameters(target_colors=1, locked_colors=((1, 2, 3), (4, 5, 6)))
    with pytest.raises(ValueError):
        AiPixelCleanupParameters(output=PixelCleanupOutput.PROJECT)
    # Fixed palette size is explicit: an old automatic target must not block edits.
    AiPixelCleanupParameters(
        target_colors=1, locked_colors=((1, 2, 3), (4, 5, 6)), fixed_palette=((1, 2, 3), (4, 5, 6))
    )


def test_alpha_weight_bias_and_selection_statistics():
    pixels = np.full((8, 8, 4), (255, 0, 0, 255), dtype=np.uint8)
    pixels[:4] = (0, 0, 255, 1)
    result, _ = calculate(
        make_document(pixels), AiPixelCleanupParameters(target_colors=1, protect_details=False)
    )
    assert result.palette[0][0] > 200 and result.palette[0][2] < 40


def test_grid_dominant_center_coverage_padding_and_lowres():
    pixels = np.full((5, 5, 4), (220, 10, 10, 255), dtype=np.uint8)
    pixels[1, 1] = (0, 20, 255, 255)
    doc = make_document(pixels)
    params = AiPixelCleanupParameters(
        grid_enabled=True,
        block_width=2,
        block_height=2,
        alpha=PixelCleanupAlpha.COVERAGE,
        output=PixelCleanupOutput.PROJECT,
    )
    result, _ = calculate(doc, params)
    small = TilePixelStore.from_readonly_tiles(result.logical_tiles).read_region(Rect(0, 0, 3, 3))
    assert (result.logical_size.width, result.logical_size.height) == (3, 3)
    np.testing.assert_array_equal(small[0, 0], (220, 10, 10, 255))
    assert small[-1, -1, 3] == 64
    centered, _ = calculate(doc, replace(params, sampling=PixelGridSampling.CENTER))
    centered_pixels = TilePixelStore.from_readonly_tiles(centered.logical_tiles).read_region(
        Rect(0, 0, 3, 3)
    )
    np.testing.assert_array_equal(centered_pixels[0, 0], (0, 20, 255, 255))


@pytest.mark.parametrize("mode", list(PixelCleanupDither))
def test_dither_palette_membership_hidden_rgb_and_determinism(mode):
    pixels = np.full((8, 520, 4), (110, 110, 110, 255), dtype=np.uint8)
    pixels[:, 255:258] = (87, 14, 193, 0)
    doc = make_document(pixels)
    params = AiPixelCleanupParameters(
        fixed_palette=((0, 0, 0), (255, 255, 255)), dither=mode, dither_strength=100
    )
    first, actual = calculate(doc, params)
    second, again = calculate(doc, params)
    assert first.result_color_count <= 2
    np.testing.assert_array_equal(actual[:, 255:258], pixels[:, 255:258])
    np.testing.assert_array_equal(actual, again)
    if mode is not PixelCleanupDither.NONE:
        _, disabled = calculate(doc, replace(params, dither_strength=0))
        _, plain = calculate(doc, replace(params, dither=PixelCleanupDither.NONE))
        np.testing.assert_array_equal(disabled, plain)


def test_grid_repeats_whole_logical_pixels_and_preserves_alpha():
    pixels = np.full((12, 12, 4), (111, 111, 111, 255), dtype=np.uint8)
    pixels[0, 0] = (12, 34, 56, 0)
    params = AiPixelCleanupParameters(
        grid_enabled=True,
        block_width=3,
        block_height=3,
        fixed_palette=((0, 0, 0), (255, 255, 255)),
        dither=PixelCleanupDither.BAYER2,
        dither_strength=100,
        alpha=PixelCleanupAlpha.COVERAGE,
    )
    _, actual = calculate(make_document(pixels), params)
    np.testing.assert_array_equal(actual, np.repeat(np.repeat(actual[::3, ::3], 3, 0), 3, 1))
    _, preserved = calculate(
        make_document(pixels), replace(params, alpha=PixelCleanupAlpha.PRESERVE)
    )
    np.testing.assert_array_equal(preserved[..., 3], pixels[..., 3])
    np.testing.assert_array_equal(preserved[0, 0], pixels[0, 0])


def test_selection_strict_soft_and_palette_scope():
    pixels = np.full((2, 4, 4), (100, 100, 100, 255), dtype=np.uint8)
    doc = make_document(pixels)
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[:2, :4] = [0, 64, 128, 255]
    doc.selection.mask.replace_tile(0, 0, mask)
    params = AiPixelCleanupParameters(fixed_palette=((0, 0, 0),))
    _, strict = calculate(doc, params)
    np.testing.assert_array_equal(strict[0, :, 0], [100, 100, 0, 0])
    result, soft = calculate(doc, replace(params, selection=PixelCleanupSelection.SOFT))
    np.testing.assert_array_equal(soft[0, :, 0], [100, 75, 50, 0])
    assert result.result_color_count == 3


def test_locks_and_cancel():
    doc = make_document(np.full((4, 4, 4), 255, dtype=np.uint8))
    doc.active_layer.locked = True
    with pytest.raises(PermissionError):
        calculate(doc)
    doc.active_layer.locked = False
    token = CancellationToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        calculate_ai_pixel_cleanup(
            AiPixelCleanupRequest(
                DocumentSnapshot.capture(doc), doc.active_layer_id, AiPixelCleanupParameters()
            ),
            token,
            lambda *_args: None,
        )


def test_cache_reuses_analysis_when_grid_or_mapping_changes(monkeypatch):
    import easypicture.imaging.ai_pixel_cleanup as module

    pixels = np.full((8, 8, 4), 255, dtype=np.uint8)
    doc = make_document(pixels)
    cache = AiPixelCleanupCache(identity=(doc.id, doc.revision, doc.active_layer_id))
    calculate(doc, cache=cache)
    assert cache.analysis is not None
    monkeypatch.setattr(module, "analyze_palette", lambda *_args: pytest.fail("analysis repeated"))
    calculate(doc, AiPixelCleanupParameters(grid_enabled=True), cache)


def test_large_cell_bounded_sampler_and_unit_grid():
    pixels = np.full((193, 195, 4), (220, 30, 20, 255), np.uint8)
    pixels[:35] = (10, 20, 230, 255)
    doc = make_document(pixels)
    result, _ = calculate(
        doc,
        AiPixelCleanupParameters(
            grid_enabled=True,
            block_width=195,
            block_height=193,
            alpha=PixelCleanupAlpha.COVERAGE,
            output=PixelCleanupOutput.PROJECT,
        ),
    )
    np.testing.assert_array_equal(
        result.logical_tiles[next(iter(result.logical_tiles))][0, 0], [220, 30, 20, 255]
    )
    _, actual = calculate(
        doc,
        AiPixelCleanupParameters(
            grid_enabled=True,
            block_width=1,
            block_height=1,
        ),
    )
    np.testing.assert_array_equal(actual, pixels)


def test_all_working_memmaps_close_on_success_and_dither_cancel(tmp_path, monkeypatch):
    from easypicture.imaging import color_adjustments
    from easypicture.temp_files import OwnedTemporaryDirectory, TemporaryFileManager

    manager = TemporaryFileManager(tmp_path / "owned")
    monkeypatch.setattr(color_adjustments, "_WORKING_MEMORY_LIMIT", 1)
    monkeypatch.setattr(
        color_adjustments,
        "owned_temporary_directory",
        lambda prefix: OwnedTemporaryDirectory(manager, prefix),
    )
    doc = make_document(np.full((32, 32, 4), (140, 130, 120, 255), np.uint8))
    params = AiPixelCleanupParameters(
        fixed_palette=((0, 0, 0), (255, 255, 255)), dither=PixelCleanupDither.FLOYD_STEINBERG
    )
    calculate(doc, params)
    assert not list(manager.root.glob("adjustment-*"))
    token = CancellationToken()

    def progress(_done, _total, label):
        if label == "蛇形误差扩散":
            token.cancel()

    with pytest.raises(JobCancelled):
        calculate_ai_pixel_cleanup(
            AiPixelCleanupRequest(DocumentSnapshot.capture(doc), doc.active_layer_id, params),
            token,
            progress,
        )
    assert not list(manager.root.glob("adjustment-*"))
