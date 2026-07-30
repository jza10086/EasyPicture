"""Shared authoritative image buffers used by thumbnails and exports."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from PIL import ImageCms

from easypicture.imaging.composite import compose_document_tile
from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import ProgressSink
from easypicture.persistence.snapshots import DocumentSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, TileCoord

MEMMAP_PIXEL_THRESHOLD = 4096 * 4096


@contextmanager
def composite_buffer(
    snapshot: DocumentSnapshot,
    token: CancellationToken,
    progress: ProgressSink,
) -> Iterator[np.ndarray]:
    """Compose all visible layers into an RGBA array or temporary memmap."""
    pixel_count = snapshot.size.width * snapshot.size.height
    temporary_path: Path | None = None
    if pixel_count > MEMMAP_PIXEL_THRESHOLD:
        handle, raw_path = tempfile.mkstemp(
            prefix="easypicture-composite-",
            suffix=".rgba",
        )
        os.close(handle)
        temporary_path = Path(raw_path)
        output: np.ndarray = np.memmap(
            temporary_path,
            mode="w+",
            dtype=np.uint8,
            shape=(snapshot.size.height, snapshot.size.width, 4),
        )
        output[:] = 0
    else:
        output = np.zeros(
            (snapshot.size.height, snapshot.size.width, 4),
            dtype=np.uint8,
        )

    try:
        tiles_x = (snapshot.size.width + DEFAULT_TILE_SIZE - 1) // DEFAULT_TILE_SIZE
        tiles_y = (snapshot.size.height + DEFAULT_TILE_SIZE - 1) // DEFAULT_TILE_SIZE
        total = tiles_x * tiles_y
        completed = 0
        for ty in range(tiles_y):
            for tx in range(tiles_x):
                token.raise_if_cancelled()
                tile = compose_document_tile(snapshot, TileCoord(tx, ty))
                left = tx * DEFAULT_TILE_SIZE
                top = ty * DEFAULT_TILE_SIZE
                width = min(DEFAULT_TILE_SIZE, snapshot.size.width - left)
                height = min(DEFAULT_TILE_SIZE, snapshot.size.height - top)
                output[top : top + height, left : left + width] = tile[:height, :width]
                completed += 1
                progress(completed, total, "合成可见图层")
        yield output
    finally:
        if isinstance(output, np.memmap):
            output.flush()
            del output
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def srgb_profile_bytes() -> bytes:
    """Return a generated sRGB profile rather than propagating source metadata."""
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def temporary_sibling(destination: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    return Path(raw_path)


def flush_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


__all__ = [
    "MEMMAP_PIXEL_THRESHOLD",
    "composite_buffer",
    "flush_file",
    "srgb_profile_bytes",
    "temporary_sibling",
]
