from __future__ import annotations

import numpy as np
import pytest

from easypicture.pixels import Rect, TileCoord, TilePixelStore


def rgba(height: int, width: int, value: tuple[int, int, int, int]) -> np.ndarray:
    pixels = np.empty((height, width, 4), dtype=np.uint8)
    pixels[...] = value
    return pixels


def test_missing_tile_is_transparent_read_only_and_sparse() -> None:
    store = TilePixelStore(tile_size=4)

    tile = store.read_tile(2, 3)

    assert tile.shape == (4, 4, 4)
    assert not tile.flags.writeable
    assert np.count_nonzero(tile) == 0
    assert store.allocated_tile_count == 0
    with pytest.raises(ValueError):
        tile[0, 0] = (1, 2, 3, 4)


def test_writable_tile_copy_on_write_preserves_previous_snapshot() -> None:
    store = TilePixelStore(tile_size=4)
    first = store.writable_tile(0, 0)
    first[...] = (10, 20, 30, 40)
    snapshot = store.read_tile(0, 0)

    second = store.writable_tile(0, 0)
    second[...] = (50, 60, 70, 80)

    assert np.all(snapshot == np.asarray((10, 20, 30, 40), dtype=np.uint8))
    assert np.all(store.read_tile(0, 0) == np.asarray((50, 60, 70, 80), dtype=np.uint8))
    assert store.revision == 2
    assert store.tile_revision(0, 0) == 2


@pytest.mark.parametrize(
    ("rect", "expected_tiles"),
    [
        (Rect(3, 0, 3, 2), 2),
        (Rect(3, 3, 2, 2), 4),
        (Rect(3, 3, 6, 6), 9),
    ],
)
def test_cross_tile_region_round_trip(rect: Rect, expected_tiles: int) -> None:
    store = TilePixelStore(tile_size=4)
    source = np.arange(rect.height * rect.width * 4, dtype=np.uint8).reshape(
        rect.height,
        rect.width,
        4,
    )

    dirty = store.write_region(rect, source)

    assert len(dirty.tiles) == expected_tiles
    assert store.allocated_tile_count == expected_tiles
    assert np.array_equal(store.read_region(rect), source)


def test_write_region_only_changes_overlapping_pixels() -> None:
    store = TilePixelStore(tile_size=4)
    base = rgba(4, 4, (5, 6, 7, 8))
    store.write_region(Rect(0, 0, 4, 4), base)
    patch = rgba(2, 2, (100, 110, 120, 130))

    store.write_region(Rect(1, 1, 2, 2), patch)

    expected = base.copy()
    expected[1:3, 1:3] = patch
    assert np.array_equal(store.read_region(Rect(0, 0, 4, 4)), expected)


def test_empty_write_is_a_no_op() -> None:
    store = TilePixelStore(tile_size=4)
    empty = np.empty((0, 0, 4), dtype=np.uint8)

    dirty = store.write_region(Rect(2, 2, 0, 0), empty)

    assert dirty.tiles == frozenset()
    assert store.revision == 0


def test_allocated_tile_iteration_is_stable_and_read_only() -> None:
    store = TilePixelStore(tile_size=4)
    store.writable_tile(2, 1)
    store.writable_tile(0, 1)
    store.writable_tile(1, 0)

    items = list(store.iter_allocated_tiles())

    assert [coord for coord, _tile in items] == [
        TileCoord(1, 0),
        TileCoord(0, 1),
        TileCoord(2, 1),
    ]
    assert all(not tile.flags.writeable for _coord, tile in items)


def test_rejects_invalid_tile_and_rectangle_coordinates() -> None:
    store = TilePixelStore(tile_size=4)

    with pytest.raises(ValueError, match="tile coordinates"):
        store.read_tile(-1, 0)
    with pytest.raises(ValueError, match="origin"):
        Rect(-1, 0, 1, 1)
    with pytest.raises(ValueError, match="dimensions"):
        Rect(0, 0, -1, 1)


def test_write_region_rejects_wrong_dtype() -> None:
    store = TilePixelStore(tile_size=4)
    pixels = np.zeros((2, 2, 4), dtype=np.float32)

    with pytest.raises(TypeError, match="uint8"):
        store.write_region(Rect(0, 0, 2, 2), pixels)  # type: ignore[arg-type]


def test_write_region_rejects_wrong_channel_count() -> None:
    store = TilePixelStore(tile_size=4)
    pixels = np.zeros((2, 2, 3), dtype=np.uint8)

    with pytest.raises(ValueError, match="shape"):
        store.write_region(Rect(0, 0, 2, 2), pixels)  # type: ignore[arg-type]


def test_write_region_rejects_non_contiguous_input() -> None:
    store = TilePixelStore(tile_size=4)
    pixels = np.zeros((8, 8, 4), dtype=np.uint8)[::2]
    assert pixels.shape == (4, 8, 4)
    assert not pixels.flags.c_contiguous

    with pytest.raises(ValueError, match="C-contiguous"):
        store.write_region(Rect(0, 0, 8, 4), pixels)
