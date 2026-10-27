from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from easypicture.domain import Document
from easypicture.imaging import compose_document_tile
from easypicture.jobs import CancellationToken, JobCancelled
from easypicture.persistence import (
    BmpExportOptions,
    DocumentSnapshot,
    ExportFormat,
    GifExportOptions,
    ImageExportRequest,
    JpegExportOptions,
    PngExportOptions,
    TgaExportOptions,
    TiffCompression,
    TiffExportOptions,
    WebpExportOptions,
    detect_export_capabilities,
    export_image,
    normalize_export_path,
)
from easypicture.pixels import TileCoord


def _progress(_completed: int, _total: int, _message: str) -> None:
    return


def _snapshot(pixels: np.ndarray) -> DocumentSnapshot:
    height, width = pixels.shape[:2]
    document = Document.create(width, height)
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[:height, :width] = pixels
    document.active_layer.pixels.replace_tile(0, 0, tile)
    return DocumentSnapshot.capture(document)


def _sample_pixels() -> np.ndarray:
    pixels = np.zeros((4, 8, 4), dtype=np.uint8)
    pixels[:, :2] = (255, 0, 0, 255)
    pixels[:, 2:4] = (0, 255, 0, 128)
    pixels[:, 4:6] = (12, 34, 56, 0)
    pixels[:, 6:] = (0, 0, 255, 255)
    return pixels


@pytest.mark.parametrize(
    ("options", "suffix", "format_name"),
    [
        (PngExportOptions(), ".png", "PNG"),
        (JpegExportOptions(), ".jpg", "JPEG"),
        (WebpExportOptions(), ".webp", "WEBP"),
        (BmpExportOptions(), ".bmp", "BMP"),
        (TiffExportOptions(), ".tiff", "TIFF"),
        (TgaExportOptions(), ".tga", "TGA"),
        (GifExportOptions(), ".gif", "GIF"),
    ],
)
def test_all_export_formats_reopen(
    tmp_path: Path,
    options,
    suffix: str,
    format_name: str,
) -> None:
    output = tmp_path / f"image{suffix}"
    receipt = export_image(
        ImageExportRequest(_snapshot(_sample_pixels()), output, options),
        CancellationToken(),
        _progress,
    )

    assert receipt.path == output
    assert receipt.file_size == output.stat().st_size
    with Image.open(output) as image:
        image.load()
        assert image.format == format_name
        assert image.size == (8, 4)


@pytest.mark.parametrize(
    "options",
    [
        PngExportOptions(),
        WebpExportOptions(lossless=True),
        TiffExportOptions(TiffCompression.LZW),
        TiffExportOptions(TiffCompression.DEFLATE),
        TgaExportOptions(rle=False),
        TgaExportOptions(rle=True),
    ],
)
def test_lossless_rgba_formats_preserve_authoritative_composite(
    tmp_path: Path,
    options,
) -> None:
    pixels = _sample_pixels()
    snapshot = _snapshot(pixels)
    expected = compose_document_tile(snapshot, TileCoord(0, 0))[:4, :8]
    output = tmp_path / "lossless"
    receipt = export_image(
        ImageExportRequest(snapshot, output, options),
        CancellationToken(),
        _progress,
    )

    with Image.open(receipt.path) as image:
        actual = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    assert np.array_equal(actual, expected)
    assert tuple(actual[0, 4]) == (0, 0, 0, 0)


def test_jpeg_and_bmp_flatten_alpha_to_white(tmp_path: Path) -> None:
    pixels = np.zeros((32, 96, 4), dtype=np.uint8)
    pixels[:, :32] = (255, 0, 0, 255)
    pixels[:, 32:64] = (0, 255, 0, 128)
    pixels[:, 64:] = (12, 34, 56, 0)
    snapshot = _snapshot(pixels)
    bmp = tmp_path / "flat.bmp"
    jpg = tmp_path / "flat.jpg"

    export_image(
        ImageExportRequest(snapshot, bmp, BmpExportOptions()),
        CancellationToken(),
        _progress,
    )
    export_image(
        ImageExportRequest(snapshot, jpg, JpegExportOptions(95)),
        CancellationToken(),
        _progress,
    )

    with Image.open(bmp) as image:
        bmp_pixels = np.asarray(image.convert("RGB"), dtype=np.uint8)
    assert tuple(bmp_pixels[0, 0]) == (255, 0, 0)
    assert tuple(bmp_pixels[0, 40]) == (127, 255, 127)
    assert tuple(bmp_pixels[0, 80]) == (255, 255, 255)
    with Image.open(jpg) as image:
        jpg_pixels = np.asarray(image.convert("RGB"), dtype=np.uint8)
    assert np.max(np.abs(jpg_pixels[16, 80].astype(int) - 255)) <= 3


def test_gif_is_static_and_uses_alpha_threshold(tmp_path: Path) -> None:
    pixels = np.array(
        [
            [
                (255, 0, 0, 0),
                (0, 255, 0, 127),
                (0, 0, 255, 128),
                (255, 255, 0, 255),
            ]
        ],
        dtype=np.uint8,
    )
    output = tmp_path / "threshold.gif"
    export_image(
        ImageExportRequest(
            _snapshot(pixels),
            output,
            GifExportOptions(colors=4, alpha_threshold=128, dither=False),
        ),
        CancellationToken(),
        _progress,
    )

    with Image.open(output) as image:
        assert image.n_frames == 1
        assert image.info.get("duration", 0) == 0
        assert "loop" not in image.info
        assert "comment" not in image.info
        assert len(image.getcolors(maxcolors=257) or []) <= 4
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    assert list(rgba[0, :, 3]) == [0, 0, 255, 255]


def test_gif_dither_is_deterministic_and_hidden_rgb_is_ignored(tmp_path: Path) -> None:
    x = np.linspace(0, 255, 64, dtype=np.uint8)
    pixels = np.zeros((16, 64, 4), dtype=np.uint8)
    pixels[..., 0] = x
    pixels[..., 1] = x[::-1]
    pixels[..., 3] = 255
    pixels[:, 24:40, 3] = 0
    first = pixels.copy()
    second = pixels.copy()
    second[:, 24:40, :3] = (201, 17, 99)
    options = GifExportOptions(colors=16, alpha_threshold=128, dither=True)
    output_a = tmp_path / "a.gif"
    output_b = tmp_path / "b.gif"

    export_image(
        ImageExportRequest(_snapshot(first), output_a, options),
        CancellationToken(),
        _progress,
    )
    export_image(
        ImageExportRequest(_snapshot(second), output_b, options),
        CancellationToken(),
        _progress,
    )

    assert output_a.read_bytes() == output_b.read_bytes()


@pytest.mark.parametrize(
    ("options", "suffix"),
    [
        (PngExportOptions(), ".png"),
        (JpegExportOptions(), ".jpg"),
        (WebpExportOptions(), ".webp"),
        (TiffExportOptions(), ".tiff"),
    ],
)
def test_export_writes_fresh_icc_and_strips_source_metadata(
    tmp_path: Path,
    options,
    suffix: str,
) -> None:
    output = tmp_path / f"clean{suffix}"
    export_image(
        ImageExportRequest(_snapshot(_sample_pixels()), output, options),
        CancellationToken(),
        _progress,
    )

    with Image.open(output) as image:
        assert isinstance(image.info.get("icc_profile"), bytes)
        assert "exif" not in image.info
        assert "software" not in image.info
        assert "comment" not in image.info
        exif = image.getexif()
        assert 274 not in exif
        assert 34853 not in exif


def test_tiff_export_does_not_write_application_identity(tmp_path: Path) -> None:
    output = tmp_path / "neutral.tiff"
    export_image(
        ImageExportRequest(
            _snapshot(_sample_pixels()),
            output,
            TiffExportOptions(),
        ),
        CancellationToken(),
        _progress,
    )

    with Image.open(output) as image:
        assert image.tag_v2.get(305) is None


def test_cancelled_export_preserves_existing_target(tmp_path: Path) -> None:
    output = tmp_path / "cancelled.png"
    output.write_bytes(b"original")
    token = CancellationToken()
    token.cancel()

    with pytest.raises(JobCancelled):
        export_image(
            ImageExportRequest(
                _snapshot(_sample_pixels()),
                output,
                PngExportOptions(),
            ),
            token,
            _progress,
        )

    assert output.read_bytes() == b"original"
    assert not list(tmp_path.glob(".cancelled.png.*.tmp"))


def test_validation_failure_preserves_existing_target(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from easypicture.persistence import image_export

    output = tmp_path / "existing.png"
    output.write_bytes(b"original")

    def fail_validation(*_args, **_kwargs) -> None:
        raise OSError("validation failed")

    monkeypatch.setattr(image_export, "_validate_export", fail_validation)
    with pytest.raises(OSError, match="validation failed"):
        export_image(
            ImageExportRequest(
                _snapshot(_sample_pixels()),
                output,
                PngExportOptions(),
            ),
            CancellationToken(),
            _progress,
        )
    assert output.read_bytes() == b"original"
    assert not list(tmp_path.glob(".existing.png.*.tmp"))


def test_capabilities_and_extension_normalization() -> None:
    capabilities = detect_export_capabilities()
    assert {item.format for item in capabilities} == set(ExportFormat)
    assert all(item.available for item in capabilities)
    assert normalize_export_path(Path("photo.jpeg"), ExportFormat.JPEG) == Path("photo.jpeg")
    assert normalize_export_path(Path("photo.png"), ExportFormat.GIF) == Path("photo.gif")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PngExportOptions(-1),
        lambda: JpegExportOptions(96),
        lambda: WebpExportOptions(quality=101),
        lambda: GifExportOptions(colors=1),
        lambda: GifExportOptions(alpha_threshold=256),
    ],
)
def test_export_parameter_boundaries(factory) -> None:
    with pytest.raises(ValueError):
        factory()
