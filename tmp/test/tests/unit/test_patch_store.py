from __future__ import annotations

import numpy as np

from easypicture.commands import UndoPatchStore


def test_patch_store_round_trips_compressed_raw_and_missing_tiles() -> None:
    store = UndoPatchStore()
    solid = np.zeros((256, 256, 4), dtype=np.uint8)
    random = np.random.default_rng(1234).integers(
        0,
        256,
        (256, 256, 4),
        dtype=np.uint8,
    )

    missing_handle = store.capture(None)
    solid_handle = store.capture(solid)
    random_handle = store.capture(random)

    assert missing_handle.codec == "missing"
    assert solid_handle.codec == "zlib"
    assert random_handle.codec == "raw"
    assert store.restore(missing_handle) is None
    assert np.array_equal(store.restore(solid_handle), solid)
    assert np.array_equal(store.restore(random_handle), random)
    assert solid_handle.uncompressed_bytes == solid.nbytes
    assert random_handle.uncompressed_bytes == random.nbytes

    store.release(solid_handle)
    store.release(random_handle)
    assert store.allocated_bytes == 0
    assert store.handle_count == 0
