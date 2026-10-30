from __future__ import annotations

import numpy as np
import pytest

from easypicture.pixels import MaskTileStore, Rect


def test_missing_mask_tile_is_zero_read_only_and_sparse() -> None:
    store = MaskTileStore(tile_size=4)

    tile = store.read_tile(3, 2)

    assert tile.shape == (4, 4)
    assert not tile.flags.writeable
    assert np.count_nonzero(tile) == 0
    assert store.allocated_tile_count == 0
    assert not store.has_nonzero_data


def test_allocated_zero_mask_does_not_count_as_selection_content() -> None:
    store = MaskTileStore(tile_size=4)
    store.writable_tile(0, 0)

    assert store.allocated_tile_count == 1
    assert not store.has_nonzero_data

    store.writable_tile(0, 0)[1, 1] = 1
    assert store.has_nonzero_data


def test_mask_copy_on_write_preserves_snapshot() -> None:
    store = MaskTileStore(tile_size=4)
    first = store.writable_tile(0, 0)
    first[...] = 64
    snapshot = store.read_tile(0, 0)

    second = store.writable_tile(0, 0)
    second[...] = 192

    assert np.all(snapshot == 64)
    assert np.all(store.read_tile(0, 0) == 192)
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
def test_mask_cross_tile_round_trip(rect: Rect, expected_tiles: int) -> None:
    store = MaskTileStore(tile_size=4)
    source = np.arange(rect.width * rect.height, dtype=np.uint8).reshape(
        rect.height,
        rect.width,
    )

    dirty = store.write_region(rect, source)

    assert len(dirty.tiles) == expected_tiles
    assert np.array_equal(store.read_region(rect), source)


def test_mask_rejects_invalid_arrays() -> None:
    store = MaskTileStore(tile_size=4)
    with pytest.raises(TypeError, match="uint8"):
        store.write_region(
            Rect(0, 0, 2, 2),
            np.zeros((2, 2), dtype=np.float32),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="shape"):
        store.write_region(
            Rect(0, 0, 2, 2),
            np.zeros((2, 2, 1), dtype=np.uint8),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="C-contiguous"):
        store.write_region(Rect(0, 0, 4, 2), np.zeros((4, 4), dtype=np.uint8)[::2])
