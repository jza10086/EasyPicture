"""Pillow import, authoritative flattening, thumbnails, and minimal PNG export."""

from __future__ import annotations

import os
import tempfile
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
from PIL import Image, ImageCms, ImageOps

from easypicture import __version__
from easypicture.domain import Document, PixelPoint, RasterLayer, Selection, Size
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION
from easypicture.imaging import compose_document_tile
from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import ProgressSink
from easypicture.persistence.snapshots import DocumentSnapshot
from easypicture.pixels import DEFAULT_TILE_SIZE, TileCoord, TilePixelStore

SUPPORTED_IMPORT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".tga")
_MEMMAP_PIXEL_THRESHOLD = 4096 * 4096


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
    layer = RasterLayer(
        id=uuid4(),
        name=layer_name,
        size=size,
        origin=PixelPoint(),
        pixels=store,
    )
    document = Document.restore(
        document_id=uuid4(),
        size=size,
        layers=[layer],
        active_layer_id=layer.id,
        selection=Selection.create_empty(size),
        source_name=source_path.name,
    )
    return ImportedImage(document, tuple(notices))


def export_png(
    snapshot: DocumentSnapshot,
    path: Path,
    token: CancellationToken,
    progress: ProgressSink,
) -> PngExportReceipt:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_sibling(destination)
    try:
        with _composite_buffer(snapshot, token, progress) as pixels:
            token.raise_if_cancelled()
            image = Image.fromarray(np.asarray(pixels), "RGBA")
            image.save(
                temporary,
                format="PNG",
                icc_profile=_srgb_profile_bytes(),
                software=f"EasyPicture {__version__}",
            )
        _flush_file(temporary)
        token.raise_if_cancelled()
        with Image.open(temporary) as check:
            check.verify()
        token.raise_if_cancelled()
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return PngExportReceipt(destination, snapshot.document_id, snapshot.revision)


def create_thumbnail_png(
    snapshot: DocumentSnapshot,
    token: CancellationToken,
    progress: ProgressSink,
) -> tuple[bytes, Size]:
    with _composite_buffer(snapshot, token, progress) as pixels:
        image = Image.fromarray(np.asarray(pixels), "RGBA")
        image.thumbnail((256, 256), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="PNG", icc_profile=_srgb_profile_bytes())
        return output.getvalue(), Size(*image.size)


@contextmanager
def _composite_buffer(
    snapshot: DocumentSnapshot,
    token: CancellationToken,
    progress: ProgressSink,
) -> Iterator[np.ndarray]:
    pixel_count = snapshot.size.width * snapshot.size.height
    temporary_path: Path | None = None
    if pixel_count > _MEMMAP_PIXEL_THRESHOLD:
        handle, raw_path = tempfile.mkstemp(prefix="easypicture-composite-", suffix=".rgba")
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


def _srgb_profile_bytes() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _validate_image_size(width: int, height: int) -> None:
    if not 1 <= width <= MAX_DOCUMENT_DIMENSION:
        raise ValueError(f"image width must be between 1 and {MAX_DOCUMENT_DIMENSION}")
    if not 1 <= height <= MAX_DOCUMENT_DIMENSION:
        raise ValueError(f"image height must be between 1 and {MAX_DOCUMENT_DIMENSION}")


def _temporary_sibling(destination: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    return Path(raw_path)


def _flush_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


__all__ = [
    "ImportedImage",
    "PngExportReceipt",
    "SUPPORTED_IMPORT_EXTENSIONS",
    "create_thumbnail_png",
    "export_png",
    "import_image",
]
