from __future__ import annotations

import gc

import numpy as np
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QImage, QPainter

from easypicture.render import rgba_tile_to_qimage


def test_bridge_returns_owned_premultiplied_qimage() -> None:
    source = np.asarray([[(12, 34, 56, 255)]], dtype=np.uint8)

    image = rgba_tile_to_qimage(source)
    source[...] = 0

    assert image.format() == QImage.Format.Format_RGBA8888_Premultiplied
    assert image.size().toTuple() == (1, 1)
    assert image.pixelColor(0, 0).getRgb() == (12, 34, 56, 255)


def test_bridge_survives_source_release() -> None:
    def make_image() -> QImage:
        source = np.asarray([[(90, 80, 70, 255)]], dtype=np.uint8)
        return rgba_tile_to_qimage(source)

    image = make_image()
    gc.collect()

    assert image.pixelColor(0, 0).getRgb() == (90, 80, 70, 255)


@pytest.mark.parametrize(
    "rgba",
    [
        (0, 0, 0, 0),
        (15, 25, 35, 255),
        (220, 30, 40, 128),
        (200, 100, 50, 1),
    ],
)
def test_bridge_handles_transparency_contract(rgba: tuple[int, int, int, int]) -> None:
    source = np.asarray([[rgba]], dtype=np.uint8)

    image = rgba_tile_to_qimage(source)
    actual = image.pixelColor(0, 0).getRgb()

    assert abs(actual[3] - rgba[3]) <= 1
    if rgba[3] == 255:
        assert actual == rgba
    if rgba[3] == 0:
        assert actual == (0, 0, 0, 0)
        assert tuple(int(value) for value in source[0, 0]) == rgba


def test_source_over_composite_matches_reference_within_one_lsb() -> None:
    source_rgba = (220, 30, 40, 128)
    destination_rgba = (20, 60, 210, 192)
    source = rgba_tile_to_qimage(np.asarray([[source_rgba]], dtype=np.uint8))
    destination = QImage(1, 1, QImage.Format.Format_RGBA8888_Premultiplied)
    destination.fill(QColor(*destination_rgba))

    painter = QPainter(destination)
    painter.drawImage(QPoint(0, 0), source)
    painter.end()

    actual = destination.pixelColor(0, 0).getRgb()
    expected = (134, 43, 113, 224)
    assert max(abs(left - right) for left, right in zip(actual, expected, strict=True)) <= 1


def test_bridge_rejects_invalid_arrays() -> None:
    with pytest.raises(TypeError, match="uint8"):
        rgba_tile_to_qimage(np.zeros((2, 2, 4), dtype=np.float32))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="shape"):
        rgba_tile_to_qimage(np.zeros((2, 2, 3), dtype=np.uint8))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="C-contiguous"):
        rgba_tile_to_qimage(np.zeros((4, 4, 4), dtype=np.uint8)[::2])
    with pytest.raises(ValueError, match="must not be empty"):
        rgba_tile_to_qimage(np.zeros((0, 0, 4), dtype=np.uint8))
