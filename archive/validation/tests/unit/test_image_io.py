from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document
from easypicture.imaging import compose_document_tile
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot, export_png, import_image
from easypicture.pixels import TileCoord


def _progress(_completed: int, _total: int, _message: str) -> None:
    return


def test_png_import_is_clean_sparse_document_with_source_name(tmp_path: Path) -> None:
    path = tmp_path / "source.png"
    pixels = np.zeros((7, 9, 4), dtype=np.uint8)
    pixels[1, 2] = (10, 20, 30, 128)
    Image.fromarray(pixels, "RGBA").save(path)

    result = import_image(path, CancellationToken(), _progress)

    assert result.document.size.width == 9
    assert result.document.size.height == 7
    assert result.document.display_name == "source.png"
    assert result.document.project_path is None
    assert not result.document.modified
    assert tuple(result.document.active_layer.pixels.read_tile(0, 0)[1, 2]) == (
        10,
        20,
        30,
        128,
    )


def test_png_export_preserves_alpha_without_changing_document(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    pixels = np.zeros((3, 4, 4), dtype=np.uint8)
    pixels[0, 0] = (255, 0, 0, 128)
    Image.fromarray(pixels, "RGBA").save(source)
    document = import_image(source, CancellationToken(), _progress).document
    document.record_change()
    revision = document.revision

    export_png(
        DocumentSnapshot.capture(document),
        output,
        CancellationToken(),
        _progress,
    )

    with Image.open(output) as image:
        exported = np.asarray(image.convert("RGBA"))
    assert tuple(exported[0, 0]) == (255, 0, 0, 128)
    assert document.modified
    assert document.revision == revision


@pytest.mark.parametrize(
    ("extension", "format_name"),
    [
        (".jpg", "JPEG"),
        (".webp", "WEBP"),
        (".bmp", "BMP"),
        (".tiff", "TIFF"),
        (".tga", "TGA"),
    ],
)
def test_supported_pillow_formats_import(
    tmp_path: Path,
    extension: str,
    format_name: str,
) -> None:
    path = tmp_path / f"sample{extension}"
    Image.new("RGB", (6, 5), (12, 34, 56)).save(path, format=format_name)

    result = import_image(path, CancellationToken(), _progress)

    assert result.document.size.width == 6
    assert result.document.size.height == 5
    assert result.document.active_layer.pixels.has_tile(0, 0)


def test_exif_orientation_is_applied_before_document_size(tmp_path: Path) -> None:
    path = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (2, 3), (200, 100, 50))
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, exif=exif)

    result = import_image(path, CancellationToken(), _progress)

    assert result.document.size.width == 3
    assert result.document.size.height == 2


def test_multiframe_tiff_imports_first_frame_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "pages.tiff"
    first = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
    second = Image.new("RGBA", (4, 4), (0, 0, 255, 255))
    first.save(path, save_all=True, append_images=[second])

    result = import_image(path, CancellationToken(), _progress)

    assert result.warnings
    assert tuple(result.document.active_layer.pixels.read_tile(0, 0)[0, 0]) == (
        255,
        0,
        0,
        255,
    )


def test_invalid_icc_falls_back_to_srgb_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "invalid-profile.png"
    Image.new("RGBA", (2, 2), (1, 2, 3, 4)).save(path, icc_profile=b"invalid")

    result = import_image(path, CancellationToken(), _progress)

    assert result.warnings == ("嵌入的 ICC 配置无效，已按 sRGB 解释。",)


def test_authoritative_compositor_matches_reference_for_transparency() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    blue = np.empty((256, 256, 4), dtype=np.uint8)
    blue[:] = (0, 0, 255, 128)
    assert service.apply_pixel_tiles(
        document.active_layer_id,
        {TileCoord(0, 0): blue},
        "蓝色",
    )
    assert service.create_layer()
    red = np.empty((256, 256, 4), dtype=np.uint8)
    red[:] = (255, 0, 0, 128)
    assert service.apply_pixel_tiles(
        document.active_layer_id,
        {TileCoord(0, 0): red},
        "红色",
    )

    actual = compose_document_tile(DocumentSnapshot.capture(document), TileCoord(0, 0))[0, 0]

    assert tuple(actual) == (170, 0, 85, 192)
