"""Pillow import, authoritative flattening, thumbnails, and minimal PNG export."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
from PIL import Image, ImageCms, ImageOps

from easypicture.domain import (
    Collection,
    Document,
    PixelPoint,
    RasterObject,
    Selection,
    Size,
)
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION
from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import ProgressSink
from easypicture.persistence.image_buffers import composite_buffer, srgb_profile_bytes
from easypicture.persistence.image_export import (
    ImageExportRequest,
    PngExportOptions,
    export_image,
)
from easypicture.persistence.snapshots import DocumentSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, TilePixelStore

SUPPORTED_IMPORT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".tga")


@dataclass(frozen=True, slots=True)
class ImportedImage:
    document: Document
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PngExportReceipt:
    path: Path
    document_id: UUID
    revision: int


def import_image(
    path: Path,
    token: CancellationToken,
    progress: ProgressSink,
) -> ImportedImage:
    source_path = Path(path)
    notices: list[str] = []
    token.raise_if_cancelled()
    progress(0, 1, "读取图片")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(source_path) as opened:
            frame_count = int(getattr(opened, "n_frames", 1))
            if frame_count > 1:
                notices.append(f"该文件包含 {frame_count} 帧，仅导入第一帧。")
            opened.seek(0)
            icc_profile = opened.info.get("icc_profile")
            oriented = ImageOps.exif_transpose(opened)
            token.raise_if_cancelled()
            width, height = oriented.size
            _validate_image_size(width, height)
            rgba = _convert_to_srgb_rgba(oriented, icc_profile, notices)

    size = Size(width, height)
    store = TilePixelStore(DEFAULT_TILE_SIZE)
    tiles_x = (width + DEFAULT_TILE_SIZE - 1) // DEFAULT_TILE_SIZE
    tiles_y = (height + DEFAULT_TILE_SIZE - 1) // DEFAULT_TILE_SIZE
    total = tiles_x * tiles_y
    completed = 0
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            token.raise_if_cancelled()
            left = tx * DEFAULT_TILE_SIZE
            top = ty * DEFAULT_TILE_SIZE
            right = min(left + DEFAULT_TILE_SIZE, width)
            bottom = min(top + DEFAULT_TILE_SIZE, height)
            crop = np.asarray(rgba.crop((left, top, right, bottom)), dtype=np.uint8)
            tile = np.zeros(
                (DEFAULT_TILE_SIZE, DEFAULT_TILE_SIZE, 4),
                dtype=np.uint8,
            )
            tile[: bottom - top, : right - left] = crop
            if np.any(tile):
                store.replace_tile(tx, ty, tile)
            completed += 1
            progress(completed, total, "构建图片 Tile")

    layer_name = source_path.stem.strip() or "图层 1"
    layer = RasterObject(
        id=uuid4(),
        name=layer_name,
        size=size,
        origin=PixelPoint(),
        pixels=store,
    )
    group = Collection.create("集合")
    group.children.append(layer)
    document = Document.restore(
        document_id=uuid4(),
        size=size,
        active_layer_id=layer.id,
        selection=Selection.create_empty(size),
        nodes=[group],
        source_name=source_path.name,
    )
    return ImportedImage(document, tuple(notices))


def export_png(
    snapshot: DocumentSnapshot,
    path: Path,
    token: CancellationToken,
    progress: ProgressSink,
) -> PngExportReceipt:
    receipt = export_image(
        ImageExportRequest(snapshot, Path(path), PngExportOptions()),
        token,
        progress,
    )
    return PngExportReceipt(receipt.path, receipt.document_id, receipt.revision)


def create_thumbnail_png(
    snapshot: DocumentSnapshot,
    token: CancellationToken,
    progress: ProgressSink,
) -> tuple[bytes, Size]:
    with composite_buffer(snapshot, token, progress) as pixels:
        image = Image.fromarray(np.asarray(pixels), "RGBA")
        image.thumbnail((256, 256), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="PNG", icc_profile=srgb_profile_bytes())
        return output.getvalue(), Size(*image.size)


def _convert_to_srgb_rgba(
    image: Image.Image,
    icc_profile: object,
    notices: list[str],
) -> Image.Image:
    if not isinstance(icc_profile, bytes) or not icc_profile:
        return image.convert("RGBA")
    try:
        source_profile = ImageCms.ImageCmsProfile(BytesIO(icc_profile))
        target_profile = ImageCms.createProfile("sRGB")
        has_alpha = "A" in image.getbands()
        alpha = image.getchannel("A") if has_alpha else None
        color_image = image.convert("RGB") if has_alpha else image
        converted = ImageCms.profileToProfile(
            color_image,
            source_profile,
            target_profile,
            outputMode="RGB",
        )
        if converted is None:
            raise ImageCms.PyCMSError("ICC conversion returned no image")
        rgba = converted.convert("RGBA")
        if alpha is not None:
            rgba.putalpha(alpha)
        return rgba
    except ImageCms.PyCMSError, OSError, ValueError:
        notices.append("嵌入的 ICC 配置无效，已按 sRGB 解释。")
        return image.convert("RGBA")


def _validate_image_size(width: int, height: int) -> None:
    if not 1 <= width <= MAX_DOCUMENT_DIMENSION:
        raise ValueError(f"image width must be between 1 and {MAX_DOCUMENT_DIMENSION}")
    if not 1 <= height <= MAX_DOCUMENT_DIMENSION:
        raise ValueError(f"image height must be between 1 and {MAX_DOCUMENT_DIMENSION}")


__all__ = [
    "ImportedImage",
    "PngExportReceipt",
    "SUPPORTED_IMPORT_EXTENSIONS",
    "create_thumbnail_png",
    "export_png",
    "import_image",
]
