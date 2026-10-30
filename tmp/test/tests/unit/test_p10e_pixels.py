from dataclasses import replace

import numpy as np
import pytest

from easypicture.domain import Document
from easypicture.imaging.color_adjustments import (
    ColorAdjustmentRequest,
    PaletteMapAdjustment,
    calculate_color_adjustment,
)
from easypicture.imaging.palette_extraction import (
    PaletteExtractionRequest,
    extract_layer_palette,
)
from easypicture.imaging.pixel_outline import (
    PixelOutlineParameters,
    PixelOutlinePlacement,
    PixelOutlineRequest,
    calculate_pixel_outline,
)
from easypicture.jobs import CancellationToken, JobCancelled
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import Rect, TilePixelStore


def progress(*_args):
    pass


def document(pixels):
    doc = Document.create(pixels.shape[1], pixels.shape[0])
    doc.active_layer.pixels.write_region(Rect(0, 0, doc.width, doc.height), pixels)
    return doc


def extract(doc, count=16):
    return extract_layer_palette(
        PaletteExtractionRequest(DocumentSnapshot.capture(doc), doc.active_layer_id, count),
        CancellationToken(),
        progress,
    )


def outline(doc, **kwargs):
    result = calculate_pixel_outline(
        PixelOutlineRequest(
            DocumentSnapshot.capture(doc), doc.active_layer_id, PixelOutlineParameters(**kwargs)
        ),
        CancellationToken(),
        progress,
    )
    store = TilePixelStore()
    for coord, tile in result.updates.items():
        store.replace_tile(coord.x, coord.y, tile)
    return store.read_region(Rect(0, 0, doc.width, doc.height)), result


@pytest.mark.parametrize("count", [1, 16, 256])
def test_palette_limits_determinism_and_alpha(count):
    pixels = np.random.default_rng(17).integers(0, 256, (40, 70, 4), dtype=np.uint8)
    doc = document(pixels)
    before = DocumentSnapshot.capture(doc)
    result = extract(doc, count)
    assert 1 <= len(result.colors) <= count
    assert result == extract(doc, count)
    assert doc.revision == before.revision
    assert result.source_color_count == len(np.unique(pixels[pixels[..., 3] > 0, :3], axis=0))
    PaletteMapAdjustment(result.colors)


def test_palette_exact_colors_selection_and_empty():
    pixels = np.array([[(100, 0, 0, 1), (0, 100, 0, 255), (12, 55, 77, 0)]], np.uint8)
    doc = document(pixels)
    assert extract(doc).colors == ((0, 100, 0), (100, 0, 0))
    doc.selection.mask.write_region(Rect(0, 0, 3, 1), np.array([[1, 0, 0]], np.uint8))
    assert extract(doc).colors == ((100, 0, 0),)
    assert extract(document(np.zeros((3, 5, 4), np.uint8))).colors == ()


def test_single_color_mapping_preserves_alpha_and_hidden_rgb():
    doc = document(np.array([[(2, 3, 4, 0), (5, 6, 7, 125)]], np.uint8))
    result = calculate_color_adjustment(
        ColorAdjustmentRequest(
            DocumentSnapshot.capture(doc),
            doc.active_layer_id,
            PaletteMapAdjustment(((10, 20, 30),)),
        ),
        CancellationToken(),
        progress,
    )
    tile = next(iter(result.updates.values()))
    assert tuple(tile[0, 0]) == (10, 20, 30, 0)
    assert tuple(tile[0, 1]) == (10, 20, 30, 125)


@pytest.mark.parametrize("neighbors,expected", [(4, 4), (8, 8)])
def test_outline_neighborhood_and_source_unchanged(neighbors, expected):
    pixels = np.zeros((7, 7, 4), np.uint8)
    pixels[3, 3] = (10, 20, 30, 128)
    doc = document(pixels)
    output, _ = outline(doc, neighbors=neighbors)
    assert np.count_nonzero(output[..., 3]) == expected
    np.testing.assert_array_equal(doc.active_layer.pixels.read_region(Rect(0, 0, 7, 7)), pixels)


def test_inner_holes_selection_and_canvas_boundary():
    pixels = np.full((7, 7, 4), 255, np.uint8)
    pixels[3, 3] = 0
    doc = document(pixels)
    outer, result = outline(doc)
    assert result.clipped
    assert np.count_nonzero(outer[..., 3]) == 1
    inner, _ = outline(doc, placement=PixelOutlinePlacement.INNER)
    assert np.count_nonzero(inner[..., 3]) == 32
    doc.selection.mask.write_region(Rect(3, 3, 1, 1), np.array([[128]], np.uint8))
    selected, _ = outline(doc, color=(20, 30, 40, 200))
    assert tuple(selected[3, 3]) == (20, 30, 40, 100)
    assert np.count_nonzero(selected[..., 3]) == 1


@pytest.mark.parametrize("neighbors", [4, 8])
def test_outline_tile_halo_matches_translated_shape(neighbors):
    pixels = np.zeros((300, 300, 4), np.uint8)
    pixels[250:262, 250:262] = 255
    crossing, _ = outline(document(pixels), width=7, neighbors=neighbors)
    shifted = np.roll(pixels, (-180, -180), axis=(0, 1))
    reference, _ = outline(document(shifted), width=7, neighbors=neighbors)
    np.testing.assert_array_equal(crossing[235:277, 235:277], reference[55:97, 55:97])


def test_cancellation_locks_and_validation():
    doc = document(np.full((8, 8, 4), 255, np.uint8))
    request = PaletteExtractionRequest(DocumentSnapshot.capture(doc), doc.active_layer_id)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        extract_layer_palette(request, token, progress)
    doc.nodes[0].locked = True
    with pytest.raises(PermissionError):
        extract(doc)
    with pytest.raises(PermissionError):
        outline(doc)
    for count in (0, 257):
        with pytest.raises(ValueError):
            replace(request, target_color_count=count)
    with pytest.raises(ValueError):
        PixelOutlineParameters(width=65)


def test_dither_transparent_barrier_and_cross_tile_scan():
    from easypicture.imaging.color_adjustments import _floyd_steinberg

    rng = np.random.default_rng(19)
    pixels = rng.integers(0, 256, (3, 520, 4), dtype=np.uint8)
    pixels[..., 3] = 255
    pixels[:, 256:259, 3] = 0
    palette = ((0, 0, 0), (255, 255, 255))
    output = np.empty_like(pixels)
    _floyd_steinberg(pixels, output, palette, CancellationToken(), progress)
    right = np.empty_like(pixels[:, 259:])
    _floyd_steinberg(pixels[:, 259:], right, palette, CancellationToken(), progress)
    np.testing.assert_array_equal(output[:, 259:], right)
    doc = document(pixels)
    result = calculate_color_adjustment(
        ColorAdjustmentRequest(
            DocumentSnapshot.capture(doc),
            doc.active_layer_id,
            PaletteMapAdjustment(palette, dither=True),
        ),
        CancellationToken(),
        progress,
    )
    for coord, tile in result.updates.items():
        doc.active_layer.pixels.replace_tile(coord.x, coord.y, tile)
    np.testing.assert_array_equal(doc.active_layer.pixels.read_region(Rect(0, 0, 520, 3)), output)


def test_extraction_bounds_sample_count_and_ignores_transparent_area(monkeypatch):
    from PIL import Image

    from easypicture.imaging import palette_extraction

    pixels = np.zeros((530, 600, 4), np.uint8)
    pixels[..., 0] = np.arange(600, dtype=np.uint16)[None, :] % 256
    pixels[..., 1] = np.arange(530, dtype=np.uint16)[:, None] % 256
    pixels[..., 3] = 255
    pixels[:4, :, 3] = 0
    sizes = []
    original = Image.Image.quantize

    def quantize(image, *args, **kwargs):
        sizes.append(image.width * image.height)
        return original(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "quantize", quantize)
    result = extract(document(pixels), 16)
    assert len(result.colors) <= 16
    assert sizes == [palette_extraction.MAX_PALETTE_SAMPLES]


def test_dither_memmap_cleanup_on_cancel(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from easypicture.imaging import color_adjustments

    @contextmanager
    def temporary_directory(**_kwargs):
        yield str(tmp_path)

    monkeypatch.setattr(color_adjustments, "_WORKING_MEMORY_LIMIT", 1)
    monkeypatch.setattr(color_adjustments, "owned_temporary_directory", temporary_directory)
    token = CancellationToken()
    with color_adjustments._rgba_surface(32, 32) as rgba:
        assert isinstance(rgba, np.memmap)
        rgba[:] = 255
        token.cancel()
        with pytest.raises(JobCancelled):
            color_adjustments._floyd_steinberg(
                rgba, np.empty_like(rgba), ((0, 0, 0),), token, progress
            )
    # Windows only permits unlinking an mmap after every mapping handle is closed.
    del rgba
    (tmp_path / "surface.rgba").unlink()


def test_chunked_lab_distances_match_full_reference_and_tie_order():
    from easypicture.imaging.color_adjustments import _nearest_palette, rgb_to_lab

    rng = np.random.default_rng(71)
    rgb = rng.integers(0, 256, (1200, 3), dtype=np.uint8)
    colors = tuple(tuple(int(v) for v in row) for row in rgb[:256])
    a, b = rgb_to_lab(rgb), rgb_to_lab(np.asarray(colors))
    indices = np.argmin(np.sum((a[:, None, :] - b[None, :, :]) ** 2, axis=-1), axis=1)
    np.testing.assert_array_equal(_nearest_palette(rgb, colors), np.asarray(colors)[indices])


def test_large_memmap_cancel_closes_before_owned_directory_cleanup(tmp_path, monkeypatch):
    from easypicture.imaging import color_adjustments
    from easypicture.temp_files import OwnedTemporaryDirectory, TemporaryFileManager

    manager = TemporaryFileManager(tmp_path / "owned")
    monkeypatch.setattr(
        color_adjustments,
        "owned_temporary_directory",
        lambda prefix: OwnedTemporaryDirectory(manager, prefix),
    )
    with pytest.raises(JobCancelled), color_adjustments._rgba_surface(4097, 4097) as surface:
        assert isinstance(surface, np.memmap)
        assert surface.nbytes > 64 * 1024 * 1024
        raise JobCancelled
    assert not list(manager.root.glob("adjustment-*"))
