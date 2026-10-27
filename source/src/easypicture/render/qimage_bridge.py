"""Convert authoritative straight-RGBA NumPy buffers to owned QImages."""

from __future__ import annotations

import numpy as np
from PySide6.QtGui import QImage

from easypicture.pixels.types import RGBAArray, validate_rgba_array


def rgba_tile_to_qimage(tile: RGBAArray) -> QImage:
    """Return an owned premultiplied QImage derived from a straight-RGBA tile.

    The temporary QImage borrows ``tile`` only for the duration of the format
    conversion. The returned QImage owns detached storage, so it remains valid
    if the NumPy source is later mutated or released.
    """
    validate_rgba_array(tile)
    height, width, _channels = tile.shape
    if width == 0 or height == 0:
        raise ValueError("QImage source must not be empty")

    borrowed = QImage(
        memoryview(tile),
        width,
        height,
        int(tile.strides[0]),
        QImage.Format.Format_RGBA8888,
    )
    if borrowed.isNull():
        raise ValueError("failed to construct QImage from the RGBA tile")

    converted = borrowed.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    if converted.isNull():
        raise ValueError("failed to convert RGBA tile to premultiplied QImage")
    return converted.copy()


def qimage_to_rgba(image: QImage) -> RGBAArray:
    """Copy an owned QImage into a contiguous straight-RGBA NumPy array."""
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    if converted.isNull():
        raise ValueError("failed to convert QImage to RGBA8888")
    height = converted.height()
    width = converted.width()
    stride = converted.bytesPerLine()
    raw = np.frombuffer(
        converted.constBits(),
        dtype=np.uint8,
        count=height * stride,
    ).reshape(height, stride)
    # ``raw`` borrows QImage storage; force an owning copy before ``converted``
    # goes out of scope.
    return np.array(
        raw[:, : width * 4].reshape(height, width, 4),
        dtype=np.uint8,
        order="C",
        copy=True,
    )


__all__ = ["qimage_to_rgba", "rgba_tile_to_qimage"]
