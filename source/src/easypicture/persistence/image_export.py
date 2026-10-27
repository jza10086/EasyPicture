"""Authoritative flattened export for common still-image formats."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

import numpy as np
from PIL import Image, features

from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import ProgressSink
from easypicture.persistence.image_buffers import (
    composite_buffer,
    flush_file,
    srgb_profile_bytes,
    temporary_sibling,
)
from easypicture.persistence.snapshots import DocumentSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE


class ExportFormat(StrEnum):
    PNG = "png"
    JPEG = "jpeg"
    WEBP = "webp"
    BMP = "bmp"
    TIFF = "tiff"
    TGA = "tga"
    GIF = "gif"
    SVG = "svg"


class SvgVectorExportMode(StrEnum):
    PRESERVE_VECTOR = "preserve_vector"
    RASTERIZE_VECTOR = "rasterize_vector"


class TiffCompression(StrEnum):
    NONE = "none"
    LZW = "lzw"
    DEFLATE = "deflate"


@dataclass(frozen=True, slots=True)
class PngExportOptions:
    compression: int = 6

    def __post_init__(self) -> None:
        if not 0 <= self.compression <= 9:
            raise ValueError("PNG compression must be between 0 and 9")


@dataclass(frozen=True, slots=True)
class JpegExportOptions:
    quality: int = 90

    def __post_init__(self) -> None:
        if not 1 <= self.quality <= 95:
            raise ValueError("JPEG quality must be between 1 and 95")


@dataclass(frozen=True, slots=True)
class WebpExportOptions:
    lossless: bool = True
    quality: int = 90

    def __post_init__(self) -> None:
        if not 0 <= self.quality <= 100:
            raise ValueError("WebP quality must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class BmpExportOptions:
    pass


@dataclass(frozen=True, slots=True)
class TiffExportOptions:
    compression: TiffCompression = TiffCompression.DEFLATE


@dataclass(frozen=True, slots=True)
class TgaExportOptions:
    rle: bool = True


@dataclass(frozen=True, slots=True)
class GifExportOptions:
    colors: int = 256
    alpha_threshold: int = 128
    dither: bool = True

    def __post_init__(self) -> None:
        if not 2 <= self.colors <= 256:
            raise ValueError("GIF colors must be between 2 and 256")
        if not 0 <= self.alpha_threshold <= 255:
            raise ValueError("GIF alpha threshold must be between 0 and 255")


@dataclass(frozen=True, slots=True)
class SvgExportOptions:
    vector_mode: SvgVectorExportMode = SvgVectorExportMode.PRESERVE_VECTOR


ImageExportOptions = (
    PngExportOptions
    | JpegExportOptions
    | WebpExportOptions
    | BmpExportOptions
    | TiffExportOptions
    | TgaExportOptions
    | GifExportOptions
    | SvgExportOptions
)


@dataclass(frozen=True, slots=True)
class ImageExportRequest:
    snapshot: DocumentSnapshot
    path: Path
    options: ImageExportOptions


@dataclass(frozen=True, slots=True)
class ImageExportReceipt:
    path: Path
    document_id: UUID
    revision: int
    format: ExportFormat
    width: int
    height: int
    file_size: int


@dataclass(frozen=True, slots=True)
class ExportCapability:
    format: ExportFormat
    available: bool
    reason: str | None = None


_FORMAT_CODES = {
    ExportFormat.PNG: "PNG",
    ExportFormat.JPEG: "JPEG",
    ExportFormat.WEBP: "WEBP",
    ExportFormat.BMP: "BMP",
    ExportFormat.TIFF: "TIFF",
    ExportFormat.TGA: "TGA",
    ExportFormat.GIF: "GIF",
    ExportFormat.SVG: "SVG",
}
_CANONICAL_EXTENSIONS = {
    ExportFormat.PNG: ".png",
    ExportFormat.JPEG: ".jpg",
    ExportFormat.WEBP: ".webp",
    ExportFormat.BMP: ".bmp",
    ExportFormat.TIFF: ".tiff",
    ExportFormat.TGA: ".tga",
    ExportFormat.GIF: ".gif",
    ExportFormat.SVG: ".svg",
}
_EXTENSION_ALIASES = {
    ExportFormat.PNG: frozenset({".png"}),
    ExportFormat.JPEG: frozenset({".jpg", ".jpeg"}),
    ExportFormat.WEBP: frozenset({".webp"}),
    ExportFormat.BMP: frozenset({".bmp"}),
    ExportFormat.TIFF: frozenset({".tif", ".tiff"}),
    ExportFormat.TGA: frozenset({".tga"}),
    ExportFormat.GIF: frozenset({".gif"}),
    ExportFormat.SVG: frozenset({".svg"}),
}


def export_format_for_options(options: ImageExportOptions) -> ExportFormat:
    if isinstance(options, PngExportOptions):
        return ExportFormat.PNG
    if isinstance(options, JpegExportOptions):
        return ExportFormat.JPEG
    if isinstance(options, WebpExportOptions):
        return ExportFormat.WEBP
    if isinstance(options, BmpExportOptions):
        return ExportFormat.BMP
    if isinstance(options, TiffExportOptions):
        return ExportFormat.TIFF
    if isinstance(options, TgaExportOptions):
        return ExportFormat.TGA
    if isinstance(options, GifExportOptions):
        return ExportFormat.GIF
    if isinstance(options, SvgExportOptions):
        return ExportFormat.SVG
    raise TypeError(f"unsupported export options: {type(options)!r}")


def default_export_options(export_format: ExportFormat) -> ImageExportOptions:
    if export_format is ExportFormat.PNG:
        return PngExportOptions()
    if export_format is ExportFormat.JPEG:
        return JpegExportOptions()
    if export_format is ExportFormat.WEBP:
        return WebpExportOptions()
    if export_format is ExportFormat.BMP:
        return BmpExportOptions()
    if export_format is ExportFormat.TIFF:
        return TiffExportOptions()
    if export_format is ExportFormat.TGA:
        return TgaExportOptions()
    if export_format is ExportFormat.GIF:
        return GifExportOptions()
    if export_format is ExportFormat.SVG:
        return SvgExportOptions()
    raise ValueError(f"unsupported export format: {export_format!r}")


def canonical_extension(export_format: ExportFormat) -> str:
    return _CANONICAL_EXTENSIONS[export_format]


def normalize_export_path(path: Path, export_format: ExportFormat) -> Path:
    candidate = Path(path)
    if candidate.suffix.lower() in _EXTENSION_ALIASES[export_format]:
        return candidate
    return candidate.with_suffix(canonical_extension(export_format))


def detect_export_capabilities() -> tuple[ExportCapability, ...]:
    Image.init()
    registered = set(Image.SAVE)
    checks: dict[ExportFormat, tuple[bool, str | None]] = {
        ExportFormat.PNG: ("PNG" in registered, "Pillow 未提供 PNG 编码器"),
        ExportFormat.JPEG: (
            "JPEG" in registered and bool(features.check("jpg")),
            "Pillow 未提供 JPEG 编码器",
        ),
        ExportFormat.WEBP: (
            "WEBP" in registered and bool(features.check("webp")),
            "Pillow 未提供 WebP 编码器",
        ),
        ExportFormat.BMP: ("BMP" in registered, "Pillow 未提供 BMP 编码器"),
        ExportFormat.TIFF: (
            "TIFF" in registered and bool(features.check("libtiff")),
            "Pillow 未提供压缩 TIFF 编码器",
        ),
        ExportFormat.TGA: ("TGA" in registered, "Pillow 未提供 TGA 编码器"),
        ExportFormat.GIF: ("GIF" in registered, "Pillow 未提供 GIF 编码器"),
        ExportFormat.SVG: (True, None),
    }
    return tuple(
        ExportCapability(export_format, available, None if available else reason)
        for export_format, (available, reason) in checks.items()
    )


def export_image(
    request: ImageExportRequest,
    token: CancellationToken,
    progress: ProgressSink,
) -> ImageExportReceipt:
    export_format = export_format_for_options(request.options)
    capability = next(item for item in detect_export_capabilities() if item.format is export_format)
    if not capability.available:
        raise RuntimeError(capability.reason or "导出编码器不可用")

    if isinstance(request.options, SvgExportOptions):
        from easypicture.persistence.svg_export import export_svg

        return export_svg(request, token, progress)

    destination = normalize_export_path(Path(request.path), export_format)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_sibling(destination)
    snapshot = request.snapshot
    tiles_x = (snapshot.size.width + DEFAULT_TILE_SIZE - 1) // DEFAULT_TILE_SIZE
    tiles_y = (snapshot.size.height + DEFAULT_TILE_SIZE - 1) // DEFAULT_TILE_SIZE
    tile_total = tiles_x * tiles_y

    def composite_progress(done: int, _total: int, message: str) -> None:
        progress(done, tile_total + 2, message)

    try:
        with composite_buffer(snapshot, token, composite_progress) as pixels:
            token.raise_if_cancelled()
            progress(tile_total, tile_total + 2, "编码图片")
            encoded, save_kwargs = _prepare_image(pixels, request.options)
            encoded.save(
                temporary,
                format=_FORMAT_CODES[export_format],
                **save_kwargs,
            )
            token.raise_if_cancelled()
            flush_file(temporary)
            progress(tile_total + 1, tile_total + 2, "验证导出文件")
            _validate_export(
                temporary,
                export_format,
                request.options,
                snapshot.size.width,
                snapshot.size.height,
                pixels,
            )
            token.raise_if_cancelled()
        os.replace(temporary, destination)
        progress(tile_total + 2, tile_total + 2, "导出完成")
    finally:
        temporary.unlink(missing_ok=True)

    return ImageExportReceipt(
        destination,
        snapshot.document_id,
        snapshot.revision,
        export_format,
        snapshot.size.width,
        snapshot.size.height,
        destination.stat().st_size,
    )


def _prepare_image(
    pixels: np.ndarray,
    options: ImageExportOptions,
) -> tuple[Image.Image, dict[str, object]]:
    rgba = np.asarray(pixels)
    clean_rgba = Image.fromarray(rgba, "RGBA")
    clean_rgba.info.clear()
    profile = srgb_profile_bytes()
    if isinstance(options, PngExportOptions):
        return clean_rgba, {
            "compress_level": options.compression,
            "icc_profile": profile,
        }
    if isinstance(options, JpegExportOptions):
        return Image.fromarray(_flatten_white(rgba), "RGB"), {
            "quality": options.quality,
            "optimize": True,
            "progressive": False,
            "subsampling": 0,
            "icc_profile": profile,
            "exif": b"",
        }
    if isinstance(options, WebpExportOptions):
        return clean_rgba, {
            "lossless": options.lossless,
            "quality": options.quality,
            "method": 4,
            "exact": True,
            "icc_profile": profile,
            "exif": b"",
            "xmp": b"",
        }
    if isinstance(options, BmpExportOptions):
        return Image.fromarray(_flatten_white(rgba), "RGB"), {}
    if isinstance(options, TiffExportOptions):
        compression = {
            TiffCompression.NONE: "raw",
            TiffCompression.LZW: "tiff_lzw",
            TiffCompression.DEFLATE: "tiff_adobe_deflate",
        }[options.compression]
        return clean_rgba, {
            "compression": compression,
            "icc_profile": profile,
        }
    if isinstance(options, TgaExportOptions):
        kwargs: dict[str, object] = {"orientation": 1}
        if options.rle:
            kwargs["compression"] = "tga_rle"
        return clean_rgba, kwargs
    if isinstance(options, GifExportOptions):
        gif, transparency = _prepare_gif(rgba, options)
        kwargs = {
            "save_all": False,
            "optimize": False,
            "interlace": True,
        }
        if transparency is not None:
            kwargs["transparency"] = transparency
        return gif, kwargs
    raise TypeError(f"unsupported export options: {type(options)!r}")


def _flatten_white(rgba: np.ndarray) -> np.ndarray:
    source = rgba.astype(np.uint16)
    alpha = source[..., 3:4]
    return ((source[..., :3] * alpha + 255 * (255 - alpha) + 127) // 255).astype(np.uint8)


def _prepare_gif(
    rgba: np.ndarray,
    options: GifExportOptions,
) -> tuple[Image.Image, int | None]:
    alpha = rgba[..., 3]
    transparent = alpha < options.alpha_threshold
    has_transparency = bool(np.any(transparent))
    visible_colors = options.colors - (1 if has_transparency else 0)
    visible_colors = max(1, visible_colors)
    rgb = np.array(rgba[..., :3], copy=True)
    palette = _build_opaque_palette(rgb, ~transparent, visible_colors)
    if has_transparency:
        rgb[transparent] = palette[0]

    palette_bytes = bytearray()
    for color in palette:
        palette_bytes.extend(int(channel) for channel in color)
    while len(palette_bytes) < 768:
        palette_bytes.extend(int(channel) for channel in palette[-1])
    palette_image = Image.new("P", (1, 1))
    palette_image.putpalette(bytes(palette_bytes[:768]))
    source = Image.fromarray(rgb, "RGB")
    dither = Image.Dither.FLOYDSTEINBERG if options.dither else Image.Dither.NONE
    quantized = source.quantize(palette=palette_image, dither=dither)
    indices = np.asarray(quantized, dtype=np.uint8).copy()
    invalid = indices >= visible_colors
    if np.any(invalid):
        indices[invalid] = visible_colors - 1

    transparency: int | None = None
    output_palette = list(palette)
    if has_transparency:
        transparency = visible_colors
        indices[transparent] = transparency
        output_palette.append((0, 0, 0))
    raw_palette = bytearray()
    for color in output_palette:
        raw_palette.extend(int(channel) for channel in color)
    while len(raw_palette) < 768:
        raw_palette.extend((0, 0, 0))
    result = Image.fromarray(indices, "P")
    result.putpalette(bytes(raw_palette[:768]))
    return result, transparency


def _build_opaque_palette(
    rgb: np.ndarray,
    opaque: np.ndarray,
    colors: int,
) -> list[tuple[int, int, int]]:
    height, width = opaque.shape
    stride = max(1, int(np.ceil(np.sqrt((height * width) / 1_000_000))))
    rows: list[np.ndarray] = []
    remaining = 1_000_000
    for y in range(0, height, stride):
        row_mask = opaque[y, ::stride]
        if not np.any(row_mask):
            continue
        row = rgb[y, ::stride][row_mask]
        if row.shape[0] > remaining:
            row = row[:remaining]
        rows.append(row)
        remaining -= row.shape[0]
        if remaining <= 0:
            break
    if not rows:
        return [(0, 0, 0)]
    samples = np.concatenate(rows, axis=0).reshape(1, -1, 3)
    sample_image = Image.fromarray(samples, "RGB")
    quantized = sample_image.quantize(
        colors=colors,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    )
    used = quantized.getcolors(maxcolors=256) or []
    count = max(1, min(colors, len(used)))
    raw = quantized.getpalette() or [0, 0, 0]
    palette = [(raw[index * 3], raw[index * 3 + 1], raw[index * 3 + 2]) for index in range(count)]
    return palette or [(0, 0, 0)]


def _validate_export(
    path: Path,
    export_format: ExportFormat,
    options: ImageExportOptions,
    width: int,
    height: int,
    expected_rgba: np.ndarray,
) -> None:
    with Image.open(path) as check:
        check.load()
        if check.format != _FORMAT_CODES[export_format]:
            raise OSError("导出文件格式验证失败")
        if check.size != (width, height):
            raise OSError("导出文件尺寸验证失败")
        if export_format is ExportFormat.GIF:
            if not isinstance(options, GifExportOptions):
                raise OSError("GIF 导出参数验证失败")
            if int(getattr(check, "n_frames", 1)) != 1:
                raise OSError("GIF 必须只有一帧")
            colors = check.convert("P").getcolors(maxcolors=257)
            if colors is None or len(colors) > options.colors:
                raise OSError("GIF 调色板验证失败")
            return
        if export_format in {
            ExportFormat.PNG,
            ExportFormat.TIFF,
            ExportFormat.TGA,
        } or (
            export_format is ExportFormat.WEBP
            and isinstance(options, WebpExportOptions)
            and options.lossless
        ):
            _compare_lossless(check, expected_rgba)
        elif export_format is ExportFormat.BMP:
            _compare_lossless_rgb(check, _flatten_white(expected_rgba))


def _compare_lossless(image: Image.Image, expected: np.ndarray) -> None:
    width, height = image.size
    for top in range(0, height, DEFAULT_TILE_SIZE):
        bottom = min(height, top + DEFAULT_TILE_SIZE)
        actual = np.asarray(
            image.crop((0, top, width, bottom)).convert("RGBA"),
            dtype=np.uint8,
        )
        if not np.array_equal(actual, expected[top:bottom]):
            raise OSError("无损导出像素验证失败")


def _compare_lossless_rgb(image: Image.Image, expected: np.ndarray) -> None:
    width, height = image.size
    for top in range(0, height, DEFAULT_TILE_SIZE):
        bottom = min(height, top + DEFAULT_TILE_SIZE)
        actual = np.asarray(
            image.crop((0, top, width, bottom)).convert("RGB"),
            dtype=np.uint8,
        )
        if not np.array_equal(actual, expected[top:bottom]):
            raise OSError("无损导出像素验证失败")


__all__ = [
    "BmpExportOptions",
    "ExportCapability",
    "ExportFormat",
    "GifExportOptions",
    "ImageExportOptions",
    "ImageExportReceipt",
    "ImageExportRequest",
    "JpegExportOptions",
    "PngExportOptions",
    "TgaExportOptions",
    "TiffCompression",
    "TiffExportOptions",
    "WebpExportOptions",
    "SvgExportOptions",
    "SvgVectorExportMode",
    "canonical_extension",
    "default_export_options",
    "detect_export_capabilities",
    "export_format_for_options",
    "export_image",
    "normalize_export_path",
]
