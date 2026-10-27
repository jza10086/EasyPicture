"""Versioned, validated, atomic EasyPicture project persistence."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID

import numpy as np
from PIL import Image

from easypicture import __version__
from easypicture.domain import (
    AffineTransform,
    DocumentKind,
    FloatRect,
    PixelPoint,
    Point,
    Size,
    SpriteAnimation,
    SpriteFrameContentMode,
    SpriteFrameDefinition,
    SpritePlaybackMode,
    SpriteWorkspace,
    SvgObject,
    TextHorizontalAlignment,
    TextObject,
    TextVerticalAlignment,
    TextWrapMode,
)
from easypicture.domain.coordinates import validate_object_transform
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION, MAX_DOCUMENT_LAYERS
from easypicture.domain.layer import RasterSampling
from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import ProgressSink
from easypicture.persistence.image_io import create_thumbnail_png
from easypicture.persistence.schema import PROJECT_SCHEMA_VERSION, require_current_schema
from easypicture.persistence.snapshots import (
    CollectionSnapshot,
    DocumentNodeSnapshot,
    DocumentSnapshot,
    LayerSnapshot,
    SpriteWorkspaceSnapshot,
    SvgElementSnapshot,
    TextObjectSnapshot,
)
from easypicture.pixels import DEFAULT_TILE_SIZE, MaskTileStore, Rect, TilePixelStore
from easypicture.temp_files import cleanup_stale_sibling_temps

PROJECT_FORMAT = "easypicture"
MANIFEST_PATH = "manifest.json"
THUMBNAIL_PATH = "thumbnail.png"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024
MAX_DECODED_BYTES = 4 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000
MAX_ARCHIVE_ENTRIES = MAX_DOCUMENT_LAYERS * 1024 + 1027
MAX_SVG_RESOURCE_BYTES = 16 * 1024 * 1024
MAX_SVG_DECODED_BYTES = 256 * 1024 * 1024
_SVG_URL_PATTERN = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
RGBA_TILE_BYTES = DEFAULT_TILE_SIZE * DEFAULT_TILE_SIZE * 4
MASK_TILE_BYTES = DEFAULT_TILE_SIZE * DEFAULT_TILE_SIZE


class ProjectFormatError(ValueError):
    """Raised when a project is malformed, unsafe, or unsupported."""


@dataclass(frozen=True, slots=True)
class SaveReceipt:
    path: Path
    document_id: UUID
    revision: int


@dataclass(frozen=True, slots=True)
class LoadedProject:
    snapshot: DocumentSnapshot
    is_recovery: bool
    original_project_path: Path | None


def save_project(
    snapshot: DocumentSnapshot,
    path: Path,
    token: CancellationToken,
    progress: ProgressSink,
    *,
    recovery: bool = False,
) -> SaveReceipt:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_sibling(destination)
    try:
        thumbnail, thumbnail_size = create_thumbnail_png(snapshot, token, progress)
        token.raise_if_cancelled()
        embedded_blobs: dict[UUID, bytes] = {}
        for embedded in snapshot.embedded_documents:
            token.raise_if_cancelled()
            handle = tempfile.NamedTemporaryFile(
                prefix=".easypicture-frame-",
                suffix=".limg",
                dir=destination.parent,
                delete=False,
            )
            embedded_path = Path(handle.name)
            handle.close()
            try:
                save_project(
                    embedded,
                    embedded_path,
                    token,
                    lambda _done, _total, _label: None,
                )
                embedded_blobs[embedded.document_id] = embedded_path.read_bytes()
            finally:
                embedded_path.unlink(missing_ok=True)
        manifest_layers_by_id: dict[UUID, dict[str, Any]] = {}
        manifest_nodes: list[dict[str, Any]] = []
        selection_tiles: list[dict[str, Any]] = []
        total_tiles = (
            sum(layer.pixels.allocated_tile_count for layer in snapshot.raster_layers)
            + snapshot.selection.allocated_tile_count
        )
        completed = 0

        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            _write_entry(archive, THUMBNAIL_PATH, thumbnail)
            if snapshot.kind is DocumentKind.PROJECT:
                assert snapshot.sprite_workspace is not None
                for frame in snapshot.sprite_workspace.frames:
                    if frame.content_mode is not SpriteFrameContentMode.DETACHED:
                        continue
                    assert frame.embedded_document_id is not None
                    try:
                        blob = embedded_blobs[frame.embedded_document_id]
                    except KeyError as error:
                        raise ProjectFormatError(
                            "detached sprite frame has no embedded document"
                        ) from error
                    _write_entry(
                        archive,
                        f"sprite_frames/{frame.id}/document.limg",
                        blob,
                    )
            for layer in snapshot.raster_layers:
                descriptors: list[dict[str, Any]] = []
                for coord, tile in layer.pixels.iter_allocated_tiles():
                    token.raise_if_cancelled()
                    if not np.any(tile):
                        completed += 1
                        progress(completed, max(total_tiles, 1), "跳过透明 Tile")
                        continue
                    raw = tile.tobytes(order="C")
                    member = f"layers/{layer.id}/tiles/{coord.x}_{coord.y}.rgba.z"
                    descriptor, encoded = _encode_tile(
                        coord.x,
                        coord.y,
                        member,
                        raw,
                        "rgba8-zlib",
                    )
                    _write_entry(archive, member, encoded)
                    descriptors.append(descriptor)
                    completed += 1
                    progress(completed, max(total_tiles, 1), "写入图层 Tile")
                layer_manifest = {
                    "type": "raster",
                    "id": str(layer.id),
                    "name": layer.name,
                    "size": {
                        "width": layer.size.width,
                        "height": layer.size.height,
                    },
                    "origin": [layer.origin.x, layer.origin.y],
                    "transform": list(layer.transform.values),
                    "transform_pivot": None
                    if layer.transform_pivot is None
                    else [layer.transform_pivot.x, layer.transform_pivot.y],
                    "sampling": layer.sampling.value,
                    "visible": layer.visible,
                    "opacity": layer.opacity,
                    "locked": layer.locked,
                    "alpha_locked": layer.alpha_locked,
                    "tiles": descriptors,
                }
                manifest_layers_by_id[layer.id] = layer_manifest

            for coord, tile in snapshot.selection.iter_allocated_tiles():
                token.raise_if_cancelled()
                if not np.any(tile):
                    completed += 1
                    progress(completed, max(total_tiles, 1), "跳过空选区 Tile")
                    continue
                raw = tile.tobytes(order="C")
                member = f"selection/tiles/{coord.x}_{coord.y}.mask.z"
                descriptor, encoded = _encode_tile(
                    coord.x,
                    coord.y,
                    member,
                    raw,
                    "mask8-zlib",
                )
                _write_entry(archive, member, encoded)
                selection_tiles.append(descriptor)
                completed += 1
                progress(completed, max(total_tiles, 1), "写入选区 Tile")

            def encode_node(node: DocumentNodeSnapshot) -> dict[str, Any]:
                if isinstance(node, LayerSnapshot):
                    return manifest_layers_by_id[node.id]
                if isinstance(node, TextObjectSnapshot):
                    return _text_object_manifest(node)
                if isinstance(node, SvgElementSnapshot):
                    raw_svg = node.svg.encode("utf-8")
                    member = f"vectors/{node.id}.svg.z"
                    _write_entry(archive, member, zlib.compress(raw_svg, level=1))
                    return _svg_object_manifest(node, member, raw_svg)
                return {
                    "type": "collection",
                    "id": str(node.id),
                    "name": node.name,
                    "visible": node.visible,
                    "opacity": node.opacity,
                    "locked": node.locked,
                    "children": [encode_node(child) for child in node.children],
                }

            manifest_nodes = [encode_node(node) for node in snapshot.nodes]
            manifest = {
                "format": PROJECT_FORMAT,
                "schema_version": PROJECT_SCHEMA_VERSION,
                "app_version": __version__,
                "document_kind": snapshot.kind.value,
                "document_id": str(snapshot.document_id),
                "canvas": {
                    "width": snapshot.size.width,
                    "height": snapshot.size.height,
                },
                "working_color_space": "sRGB",
                "active_layer_id": str(snapshot.active_layer_id)
                if snapshot.active_layer_id
                else None,
                "source_name": snapshot.source_name,
                "nodes": manifest_nodes,
                "sprite_workspace": (
                    _sprite_workspace_manifest(snapshot.sprite_workspace)
                    if snapshot.sprite_workspace is not None
                    else None
                ),
                "selection": {"tiles": selection_tiles},
                "thumbnail": {
                    "path": THUMBNAIL_PATH,
                    "width": thumbnail_size.width,
                    "height": thumbnail_size.height,
                },
                "recovery": {
                    "is_recovery": recovery,
                    "original_project_path": (
                        str(snapshot.project_path)
                        if recovery and snapshot.project_path is not None
                        else None
                    ),
                },
            }
            manifest_bytes = json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise ProjectFormatError("project manifest exceeds the size limit")
            _write_entry(archive, MANIFEST_PATH, manifest_bytes)

        _flush_file(temporary)
        token.raise_if_cancelled()
        _read_project(temporary, token, progress, build=False)
        token.raise_if_cancelled()
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return SaveReceipt(destination, snapshot.document_id, snapshot.revision)


def load_project(
    path: Path,
    token: CancellationToken,
    progress: ProgressSink,
) -> LoadedProject:
    loaded = _read_project(Path(path), token, progress, build=True)
    if not isinstance(loaded, LoadedProject):
        raise AssertionError("validated project did not produce a snapshot")
    return loaded


def validate_project(
    path: Path,
    token: CancellationToken,
    progress: ProgressSink,
) -> None:
    _read_project(Path(path), token, progress, build=False)


def _read_embedded_project(
    payload: bytes,
    token: CancellationToken,
    progress: ProgressSink,
) -> DocumentSnapshot:
    """Validate and decode a one-level embedded frame project."""
    handle = tempfile.NamedTemporaryFile(
        prefix="easypicture-embedded-",
        suffix=".limg",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        handle.write(payload)
        handle.flush()
        handle.close()
        loaded = _read_project(temporary, token, progress, build=True)
        if not isinstance(loaded, LoadedProject):
            raise ProjectFormatError("embedded frame document could not be decoded")
        if loaded.snapshot.embedded_documents:
            raise ProjectFormatError("embedded frame documents cannot be nested")
        return loaded.snapshot
    finally:
        if not handle.closed:
            handle.close()
        temporary.unlink(missing_ok=True)


def _read_project(
    path: Path,
    token: CancellationToken,
    progress: ProgressSink,
    *,
    build: bool,
) -> LoadedProject | None:
    token.raise_if_cancelled()
    try:
        archive = zipfile.ZipFile(path, mode="r")
    except (OSError, zipfile.BadZipFile) as error:
        raise ProjectFormatError("project is not a valid ZIP container") from error

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ProjectFormatError("project contains too many archive entries")
        names: set[str] = set()
        by_name: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            _validate_member_name(info.filename)
            if info.filename in names:
                raise ProjectFormatError("project contains duplicate archive entries")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ProjectFormatError("project uses an unsupported ZIP compression method")
            names.add(info.filename)
            by_name[info.filename] = info
        manifest_info = by_name.get(MANIFEST_PATH)
        if manifest_info is None or manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise ProjectFormatError("project manifest is missing or too large")
        try:
            manifest_raw = archive.read(manifest_info)
            manifest_value = json.loads(manifest_raw.decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as error:
            raise ProjectFormatError("project manifest is invalid") from error
        if not isinstance(manifest_value, dict):
            raise ProjectFormatError("project manifest must be an object")
        try:
            require_current_schema(manifest_value)
            manifest = manifest_value
        except ValueError as error:
            raise ProjectFormatError(str(error)) from error

        if manifest.get("format") != PROJECT_FORMAT:
            raise ProjectFormatError("project format marker is invalid")
        if manifest.get("working_color_space") != "sRGB":
            raise ProjectFormatError("project working color space is unsupported")
        kind_value = manifest.get("document_kind")
        try:
            document_kind = DocumentKind(kind_value) if isinstance(kind_value, str) else None
        except (TypeError, ValueError) as error:
            raise ProjectFormatError("project document kind is invalid") from error
        if document_kind is None:
            raise ProjectFormatError("project document kind is invalid")
        document_id = _uuid_field(manifest, "document_id")
        canvas = _object_field(manifest, "canvas")
        width = _bounded_int(canvas, "width", 1, MAX_DOCUMENT_DIMENSION)
        height = _bounded_int(canvas, "height", 1, MAX_DOCUMENT_DIMENSION)
        size = Size(width, height)
        if document_kind is DocumentKind.PROJECT:
            sprite_workspace = _parse_sprite_workspace(manifest, size)
        else:
            if manifest.get("sprite_workspace") is not None:
                raise ProjectFormatError(
                    "embedded frame document cannot contain a sprite workspace"
                )
            sprite_workspace = None
        active_layer_id = (
            None
            if manifest.get("active_layer_id") is None
            else _uuid_field(manifest, "active_layer_id")
        )
        source_name = _optional_filename(manifest.get("source_name"))
        nodes_value = manifest.get("nodes")
        if not isinstance(nodes_value, list):
            raise ProjectFormatError("project nodes must be a list")
        raster_node_values: list[dict[str, Any]] = []
        vector_values: list[dict[str, Any]] = []
        all_ids: set[UUID] = set()

        def validate_node(value: Any, depth: int = 0) -> None:
            if not isinstance(value, dict):
                raise ProjectFormatError("node entry must be an object")
            if depth > 64 or len(all_ids) >= 100000:
                raise ProjectFormatError("project tree exceeds structural limits")
            node_id = _uuid_field(value, "id")
            if node_id in all_ids:
                raise ProjectFormatError("project node UUIDs must be globally unique")
            all_ids.add(node_id)
            name = value.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 1024:
                raise ProjectFormatError("node name is invalid")
            _bool_field(value, "visible")
            _bool_field(value, "locked")
            _bounded_float(value, "opacity", 0.0, 1.0)
            node_type = value.get("type")
            if node_type == "collection":
                children = value.get("children")
                if not isinstance(children, list):
                    raise ProjectFormatError("collection children must be a list")
                for child in children:
                    validate_node(child, depth + 1)
            elif node_type == "raster":
                raster_node_values.append(value)
            elif node_type in {"svg_text", "svg_element"}:
                vector_values.append(value)
            else:
                raise ProjectFormatError("unknown object type; specialized groups are unsupported")

        for value in nodes_value:
            validate_node(value)
        if len(raster_node_values) > MAX_DOCUMENT_LAYERS:
            raise ProjectFormatError("too many raster objects")

        expected_names = {MANIFEST_PATH, THUMBNAIL_PATH}
        layer_snapshots: list[LayerSnapshot] = []
        layer_ids: set[UUID] = set()
        decoded_total = 0
        descriptor_total = sum(
            len(layer.get("tiles", []))
            for layer in raster_node_values
            if isinstance(layer, dict) and isinstance(layer.get("tiles"), list)
        )
        selection_value = _object_field(manifest, "selection")
        selection_descriptors = selection_value.get("tiles")
        if not isinstance(selection_descriptors, list):
            raise ProjectFormatError("selection tiles must be a list")
        descriptor_total += len(selection_descriptors)
        completed = 0

        for layer_value in raster_node_values:
            if not isinstance(layer_value, dict):
                raise ProjectFormatError("layer entry must be an object")
            layer_id = _uuid_field(layer_value, "id")
            if layer_id in layer_ids:
                raise ProjectFormatError("layer UUIDs must be unique")
            layer_ids.add(layer_id)
            name = layer_value.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 1024:
                raise ProjectFormatError("layer name is invalid")
            layer_size_value = _object_field(layer_value, "size")
            layer_size = Size(
                _bounded_int(layer_size_value, "width", 1, MAX_DOCUMENT_DIMENSION),
                _bounded_int(layer_size_value, "height", 1, MAX_DOCUMENT_DIMENSION),
            )
            origin_value = layer_value.get("origin")
            if (
                not isinstance(origin_value, list)
                or len(origin_value) != 2
                or not all(
                    isinstance(value, int) and not isinstance(value, bool) for value in origin_value
                )
                or any(abs(value) > MAX_DOCUMENT_DIMENSION * 4 for value in origin_value)
            ):
                raise ProjectFormatError("layer origin is invalid")
            matrix_value = layer_value.get("transform")
            if (
                not isinstance(matrix_value, list)
                or len(matrix_value) != 6
                or not all(_is_finite_number(item) for item in matrix_value)
            ):
                raise ProjectFormatError("raster transform is invalid")
            pivot_value = layer_value.get("transform_pivot")
            if pivot_value is not None and (
                not isinstance(pivot_value, list)
                or len(pivot_value) != 2
                or not all(_is_finite_number(item) for item in pivot_value)
            ):
                raise ProjectFormatError("raster pivot is invalid")
            try:
                raster_transform = AffineTransform(*matrix_value)
                validate_object_transform(raster_transform)
                sampling_value = layer_value.get("sampling")
                if not isinstance(sampling_value, str):
                    raise ValueError("raster sampling must be a string")
                sampling = RasterSampling(sampling_value)
            except (TypeError, ValueError) as error:
                raise ProjectFormatError("invalid raster transform or sampling") from error
            raster_pivot = None if pivot_value is None else Point(*pivot_value)
            visible = _bool_field(layer_value, "visible")
            locked = _bool_field(layer_value, "locked")
            alpha_locked = _bool_field(layer_value, "alpha_locked")
            opacity_value = layer_value.get("opacity")
            if (
                not isinstance(opacity_value, int | float)
                or isinstance(opacity_value, bool)
                or not 0.0 <= float(opacity_value) <= 1.0
            ):
                raise ProjectFormatError("layer opacity is invalid")
            descriptors = layer_value.get("tiles")
            if not isinstance(descriptors, list):
                raise ProjectFormatError("layer tiles must be a list")
            store = TilePixelStore(DEFAULT_TILE_SIZE)
            seen_coords: set[tuple[int, int]] = set()
            max_tx = (layer_size.width - 1) // DEFAULT_TILE_SIZE
            max_ty = (layer_size.height - 1) // DEFAULT_TILE_SIZE
            for descriptor in descriptors:
                token.raise_if_cancelled()
                coord, raw, member = _read_tile(
                    archive,
                    by_name,
                    descriptor,
                    encoding="rgba8-zlib",
                    expected_size=RGBA_TILE_BYTES,
                    max_tx=max_tx,
                    max_ty=max_ty,
                )
                if coord in seen_coords:
                    raise ProjectFormatError("layer contains duplicate Tile coordinates")
                seen_coords.add(coord)
                expected_member = f"layers/{layer_id}/tiles/{coord[0]}_{coord[1]}.rgba.z"
                if member != expected_member:
                    raise ProjectFormatError("layer Tile path does not match its descriptor")
                expected_names.add(member)
                decoded_total += len(raw)
                _check_decoded_total(decoded_total)
                if build:
                    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(
                        DEFAULT_TILE_SIZE,
                        DEFAULT_TILE_SIZE,
                        4,
                    )
                    store.replace_tile(coord[0], coord[1], pixels)
                completed += 1
                progress(completed, max(descriptor_total, 1), "校验项目 Tile")
            layer_snapshots.append(
                LayerSnapshot(
                    id=layer_id,
                    name=name.strip(),
                    size=layer_size,
                    origin=PixelPoint(origin_value[0], origin_value[1]),
                    visible=visible,
                    opacity=float(opacity_value),
                    locked=locked,
                    alpha_locked=alpha_locked,
                    pixels=store,
                    transform=raster_transform,
                    transform_pivot=raster_pivot,
                    sampling=sampling,
                )
            )

        if active_layer_id is not None and active_layer_id not in layer_ids:
            raise ProjectFormatError("active layer does not belong to the project")

        snapshots_by_id: dict[UUID, DocumentNodeSnapshot] = {
            layer.id: layer for layer in layer_snapshots
        }
        decoded_counter = [decoded_total]
        svg_decoded_counter = [0]
        vector_snapshots = _parse_vector_objects(
            vector_values,
            set(),
            archive,
            by_name,
            expected_names,
            decoded_counter,
            svg_decoded_counter,
        )
        snapshots_by_id.update((node.id, node) for node in vector_snapshots)
        decoded_total = decoded_counter[0]

        def decode_node(value: dict[str, Any]) -> DocumentNodeSnapshot:
            node_id = _uuid_field(value, "id")
            if value["type"] != "collection":
                return snapshots_by_id[node_id]
            return CollectionSnapshot(
                id=node_id,
                name=value["name"].strip(),
                visible=value["visible"],
                opacity=float(value["opacity"]),
                locked=value["locked"],
                children=tuple(decode_node(child) for child in value["children"]),
            )

        node_snapshots = [decode_node(value) for value in nodes_value]

        selection_store = MaskTileStore(DEFAULT_TILE_SIZE)
        selection_seen: set[tuple[int, int]] = set()
        max_selection_tx = (width - 1) // DEFAULT_TILE_SIZE
        max_selection_ty = (height - 1) // DEFAULT_TILE_SIZE
        for descriptor in selection_descriptors:
            token.raise_if_cancelled()
            coord, raw, member = _read_tile(
                archive,
                by_name,
                descriptor,
                encoding="mask8-zlib",
                expected_size=MASK_TILE_BYTES,
                max_tx=max_selection_tx,
                max_ty=max_selection_ty,
            )
            if coord in selection_seen:
                raise ProjectFormatError("selection contains duplicate Tile coordinates")
            selection_seen.add(coord)
            expected_member = f"selection/tiles/{coord[0]}_{coord[1]}.mask.z"
            if member != expected_member:
                raise ProjectFormatError("selection Tile path does not match its descriptor")
            expected_names.add(member)
            decoded_total += len(raw)
            _check_decoded_total(decoded_total)
            if build:
                selection_pixels = np.frombuffer(raw, dtype=np.uint8).reshape(
                    DEFAULT_TILE_SIZE,
                    DEFAULT_TILE_SIZE,
                )
                selection_store.replace_tile(coord[0], coord[1], selection_pixels)
            completed += 1
            progress(completed, max(descriptor_total, 1), "校验选区 Tile")

        embedded_snapshots: list[DocumentSnapshot] = []
        if sprite_workspace is not None:
            for frame in sprite_workspace.frames:
                if frame.content_mode is not SpriteFrameContentMode.DETACHED:
                    continue
                member = f"sprite_frames/{frame.id}/document.limg"
                embedded_info = by_name.get(member)
                if embedded_info is None:
                    raise ProjectFormatError("embedded sprite frame document is missing")
                if embedded_info.file_size > MAX_DECODED_BYTES:
                    raise ProjectFormatError("embedded sprite frame document is too large")
                decoded_total += embedded_info.file_size
                _check_decoded_total(decoded_total)
                expected_names.add(member)
                embedded = _read_embedded_project(
                    archive.read(embedded_info),
                    token,
                    progress,
                )
                if (
                    embedded.kind is not DocumentKind.SPRITE_FRAME
                    or embedded.size != sprite_workspace.cell_size
                    or embedded.document_id != frame.embedded_document_id
                ):
                    raise ProjectFormatError("embedded sprite frame document is invalid")
                embedded_snapshots.append(embedded)

        _validate_thumbnail(archive, by_name, manifest)
        if names != expected_names:
            raise ProjectFormatError("project contains undeclared or missing archive entries")

        recovery_value = _object_field(manifest, "recovery")
        is_recovery = _bool_field(recovery_value, "is_recovery")
        original_text = recovery_value.get("original_project_path")
        if original_text is not None and not isinstance(original_text, str):
            raise ProjectFormatError("recovery project path is invalid")
        original_path = Path(original_text) if original_text else None
        if original_path is not None and (not is_recovery or not original_path.is_absolute()):
            raise ProjectFormatError("recovery project path is invalid")
        if not build:
            return None
        snapshot = DocumentSnapshot(
            document_id=document_id,
            revision=0,
            size=size,
            active_layer_id=active_layer_id,
            selection=selection_store,
            project_path=(
                (original_path if is_recovery else path)
                if document_kind is DocumentKind.PROJECT
                else None
            ),
            source_name=source_name,
            nodes=tuple(node_snapshots),
            kind=document_kind,
            sprite_workspace=(
                SpriteWorkspaceSnapshot.capture(sprite_workspace)
                if sprite_workspace is not None
                else None
            ),
            embedded_documents=tuple(embedded_snapshots),
        )
        return LoadedProject(snapshot, is_recovery, original_path)


def _sprite_workspace_manifest(
    workspace: SpriteWorkspaceSnapshot,
) -> dict[str, Any]:
    return {
        "cell_size": {
            "width": workspace.cell_size.width,
            "height": workspace.cell_size.height,
        },
        "frames": [
            {
                "id": str(frame.id),
                "name": frame.name,
                "region": [
                    frame.region.x,
                    frame.region.y,
                    frame.region.width,
                    frame.region.height,
                ],
                "pivot": [frame.pivot.x, frame.pivot.y],
                "content_mode": frame.content_mode.value,
                "embedded_document_id": (
                    str(frame.embedded_document_id)
                    if frame.embedded_document_id is not None
                    else None
                ),
                "embedded_path": (
                    f"sprite_frames/{frame.id}/document.limg"
                    if frame.content_mode is SpriteFrameContentMode.DETACHED
                    else None
                ),
                "duration_ticks": frame.duration_ticks,
            }
            for frame in workspace.frames
        ],
        "animations": [
            {
                "id": str(animation.id),
                "name": animation.name,
                "frame_ids": [str(frame_id) for frame_id in animation.frame_ids],
                "fps": animation.fps,
                "playback_mode": animation.playback_mode.value,
            }
            for animation in workspace.animations
        ],
    }


def _parse_sprite_workspace(
    manifest: dict[str, Any],
    canvas_size: Size,
) -> SpriteWorkspace:
    value = _object_field(manifest, "sprite_workspace")
    cell = _object_field(value, "cell_size")
    cell_size = Size(
        _bounded_int(cell, "width", 1, canvas_size.width),
        _bounded_int(cell, "height", 1, canvas_size.height),
    )
    frames_value = value.get("frames")
    if not isinstance(frames_value, list) or len(frames_value) > 1024:
        raise ProjectFormatError("sprite frame count is invalid")
    frames: list[SpriteFrameDefinition] = []
    for frame_value in frames_value:
        if not isinstance(frame_value, dict):
            raise ProjectFormatError("sprite frame entry is invalid")
        frame_id = _uuid_field(frame_value, "id")
        name = frame_value.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 1024:
            raise ProjectFormatError("sprite frame name is invalid")
        region_value = frame_value.get("region")
        if (
            not isinstance(region_value, list)
            or len(region_value) != 4
            or not all(
                isinstance(item, int) and not isinstance(item, bool) for item in region_value
            )
        ):
            raise ProjectFormatError("sprite frame region is invalid")
        region = Rect(*region_value)
        if (
            region.width != cell_size.width
            or region.height != cell_size.height
            or region.right > canvas_size.width
            or region.bottom > canvas_size.height
        ):
            raise ProjectFormatError("sprite frame region is outside the canvas")
        pivot_value = frame_value.get("pivot")
        if (
            not isinstance(pivot_value, list)
            or len(pivot_value) != 2
            or not all(isinstance(item, int) and not isinstance(item, bool) for item in pivot_value)
        ):
            raise ProjectFormatError("sprite frame pivot is invalid")
        try:
            mode_value = frame_value.get("content_mode")
            try:
                content_mode = (
                    SpriteFrameContentMode(mode_value) if isinstance(mode_value, str) else None
                )
            except (TypeError, ValueError) as error:
                raise ProjectFormatError("sprite frame content mode is invalid") from error
            if content_mode is None:
                raise ProjectFormatError("sprite frame content mode is invalid")
            embedded_value = frame_value.get("embedded_document_id")
            if embedded_value is None:
                embedded_id = None
            elif isinstance(embedded_value, str):
                try:
                    embedded_id = UUID(embedded_value)
                except ValueError as error:
                    raise ProjectFormatError(
                        "sprite frame embedded document id is invalid"
                    ) from error
            else:
                raise ProjectFormatError("sprite frame embedded document id is invalid")
            expected_embedded_path = (
                f"sprite_frames/{frame_id}/document.limg"
                if content_mode is SpriteFrameContentMode.DETACHED
                else None
            )
            if frame_value.get("embedded_path") != expected_embedded_path:
                raise ProjectFormatError("sprite frame embedded document path is invalid")
            frames.append(
                SpriteFrameDefinition(
                    frame_id,
                    name.strip(),
                    region,
                    PixelPoint(pivot_value[0], pivot_value[1]),
                    content_mode,
                    embedded_id,
                    _bounded_int(frame_value, "duration_ticks", 1, 999),
                )
            )
        except ValueError as error:
            raise ProjectFormatError("sprite frame values are invalid") from error
    animations_value = value.get("animations")
    if not isinstance(animations_value, list) or len(animations_value) > 128:
        raise ProjectFormatError("sprite animation count is invalid")
    animations: list[SpriteAnimation] = []
    for animation_value in animations_value:
        if not isinstance(animation_value, dict):
            raise ProjectFormatError("sprite animation entry is invalid")
        animation_id = _uuid_field(animation_value, "id")
        name = animation_value.get("name")
        frame_ids_value = animation_value.get("frame_ids")
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 1024
            or not isinstance(frame_ids_value, list)
        ):
            raise ProjectFormatError("sprite animation values are invalid")
        try:
            frame_ids = tuple(UUID(value) for value in frame_ids_value)
        except (TypeError, ValueError) as error:
            raise ProjectFormatError("sprite animation member is invalid") from error
        try:
            mode_value = animation_value.get("playback_mode")
            if not isinstance(mode_value, str):
                raise ValueError("missing playback mode")
            animations.append(
                SpriteAnimation(
                    animation_id,
                    name.strip(),
                    frame_ids,
                    _bounded_int(animation_value, "fps", 1, 240),
                    SpritePlaybackMode(mode_value),
                )
            )
        except ValueError as error:
            raise ProjectFormatError("sprite animation values are invalid") from error
    try:
        workspace = SpriteWorkspace(
            cell_size,
            frames,
            animations,
        )
        workspace.validate_for_canvas(canvas_size)
    except ValueError as error:
        raise ProjectFormatError("sprite workspace is invalid") from error
    return workspace


def _encode_tile(
    x: int,
    y: int,
    member: str,
    raw: bytes,
    encoding: str,
) -> tuple[dict[str, Any], bytes]:
    encoded = zlib.compress(raw, level=1)
    return (
        {
            "x": x,
            "y": y,
            "path": member,
            "encoding": encoding,
            "raw_size": len(raw),
        },
        encoded,
    )


def _text_object_manifest(text_object: TextObjectSnapshot) -> dict[str, Any]:
    """Serialize a text snapshot without leaking Qt objects into the archive."""
    # The snapshot type is deliberately duck-typed here to keep this module's
    # archive code independent from the domain implementation details.
    return {
        "type": "svg_text",
        "id": str(text_object.id),
        "name": text_object.name,
        "text": text_object.text,
        "frame": [
            text_object.frame.x,
            text_object.frame.y,
            text_object.frame.width,
            text_object.frame.height,
        ],
        "font": {
            "family": text_object.font_family,
            "size": text_object.font_size,
            "weight": text_object.font_weight,
            "italic": text_object.italic,
        },
        "color": list(text_object.color),
        "stroke": {
            "color": list(text_object.stroke_color),
            "width": text_object.stroke_width,
        },
        "horizontal_alignment": text_object.horizontal_alignment.value,
        "vertical_alignment": text_object.vertical_alignment.value,
        "wrap_mode": text_object.wrap_mode.value,
        "line_spacing": text_object.line_spacing,
        "visible": text_object.visible,
        "opacity": text_object.opacity,
        "locked": text_object.locked,
        "transform": list(text_object.transform.values),
        "transform_pivot": (
            None
            if text_object.transform_pivot is None
            else [text_object.transform_pivot.x, text_object.transform_pivot.y]
        ),
    }


def _svg_object_manifest(
    element: SvgElementSnapshot,
    member: str,
    raw_svg: bytes,
) -> dict[str, Any]:
    return {
        "type": "svg_element",
        "id": str(element.id),
        "name": element.name,
        "element_kind": element.element_kind,
        "resource": {
            "path": member,
            "encoding": "svg-utf8-zlib",
            "raw_size": len(raw_svg),
        },
        "bounds": [
            element.intrinsic_bounds.x,
            element.intrinsic_bounds.y,
            element.intrinsic_bounds.width,
            element.intrinsic_bounds.height,
        ],
        "transform": list(element.transform.values),
        "transform_pivot": (
            None
            if element.transform_pivot is None
            else [element.transform_pivot.x, element.transform_pivot.y]
        ),
        "fill_override": (None if element.fill_override is None else list(element.fill_override)),
        "stroke_override": (
            None if element.stroke_override is None else list(element.stroke_override)
        ),
        "stroke_width_override": element.stroke_width_override,
        "visible": element.visible,
        "opacity": element.opacity,
        "locked": element.locked,
    }


def _parse_vector_objects(
    children_value: list[dict[str, Any]],
    used_ids: set[UUID],
    archive: zipfile.ZipFile,
    by_name: dict[str, zipfile.ZipInfo],
    expected_names: set[str],
    decoded_counter: list[int],
    svg_decoded_counter: list[int],
) -> tuple[TextObjectSnapshot | SvgElementSnapshot, ...]:
    snapshots: list[TextObjectSnapshot | SvgElementSnapshot] = []
    for child_value in children_value:
        if not isinstance(child_value, dict):
            raise ProjectFormatError("vector group contains an invalid child")
        child_type = child_value.get("type")
        if child_type == "svg_element":
            svg_snapshot, member, raw_size = _parse_svg_element(
                child_value,
                archive,
                by_name,
            )
            if svg_snapshot.id in used_ids:
                raise ProjectFormatError("project node UUIDs must be globally unique")
            used_ids.add(svg_snapshot.id)
            expected_names.add(member)
            decoded_counter[0] += raw_size
            _check_decoded_total(decoded_counter[0])
            svg_decoded_counter[0] += raw_size
            if svg_decoded_counter[0] > MAX_SVG_DECODED_BYTES:
                raise ProjectFormatError("project SVG resources exceed the size limit")
            snapshots.append(svg_snapshot)
            continue
        if child_type != "svg_text":
            raise ProjectFormatError("vector group contains an invalid child")
        object_id = _uuid_field(child_value, "id")
        if object_id in used_ids:
            raise ProjectFormatError("project node UUIDs must be globally unique")
        used_ids.add(object_id)
        child_name = child_value.get("name")
        text = child_value.get("text")
        if (
            not isinstance(child_name, str)
            or not child_name.strip()
            or len(child_name) > 1024
            or not isinstance(text, str)
        ):
            raise ProjectFormatError("text object metadata is invalid")
        frame_value = child_value.get("frame")
        if (
            not isinstance(frame_value, list)
            or len(frame_value) != 4
            or not all(_is_finite_number(item) for item in frame_value)
        ):
            raise ProjectFormatError("text frame is invalid")
        font_value = _object_field(child_value, "font")
        family = font_value.get("family")
        if not isinstance(family, str) or not family.strip() or len(family) > 255:
            raise ProjectFormatError("text font family is invalid")
        font_size = _bounded_float(font_value, "size", 1.0, 1024.0)
        font_weight = _bounded_int(font_value, "weight", 100, 900)
        if font_weight % 100:
            raise ProjectFormatError("text font weight is invalid")
        italic = _bool_field(font_value, "italic")
        color_value = child_value.get("color")
        if (
            not isinstance(color_value, list)
            or len(color_value) != 4
            or not all(
                isinstance(channel, int) and not isinstance(channel, bool) and 0 <= channel <= 255
                for channel in color_value
            )
        ):
            raise ProjectFormatError("text color is invalid")
        stroke_value = child_value.get("stroke")
        if not isinstance(stroke_value, dict):
            raise ProjectFormatError("text stroke is invalid")
        stroke_color_value = stroke_value.get("color")
        if (
            not isinstance(stroke_color_value, list)
            or len(stroke_color_value) != 4
            or not all(
                isinstance(channel, int) and not isinstance(channel, bool) and 0 <= channel <= 255
                for channel in stroke_color_value
            )
        ):
            raise ProjectFormatError("text stroke color is invalid")
        stroke_width = _bounded_float(stroke_value, "width", 0.0, 128.0)
        horizontal_value = child_value.get("horizontal_alignment")
        vertical_value = child_value.get("vertical_alignment")
        wrap_value = child_value.get("wrap_mode")
        if not isinstance(horizontal_value, str) or not isinstance(vertical_value, str):
            raise ProjectFormatError("text alignment is invalid")
        if not isinstance(wrap_value, str):
            raise ProjectFormatError("text wrap mode is invalid")
        try:
            horizontal = TextHorizontalAlignment(horizontal_value)
            vertical = TextVerticalAlignment(vertical_value)
            wrap_mode = TextWrapMode(wrap_value)
        except ValueError as error:
            raise ProjectFormatError("text alignment or wrap mode is invalid") from error
        transform_value = child_value.get("transform")
        if (
            not isinstance(transform_value, list)
            or len(transform_value) != 6
            or not all(_is_finite_number(item) for item in transform_value)
        ):
            raise ProjectFormatError("text transform is invalid")
        pivot_value = child_value.get("transform_pivot")
        if pivot_value is not None and (
            not isinstance(pivot_value, list)
            or len(pivot_value) != 2
            or not all(_is_finite_number(item) for item in pivot_value)
        ):
            raise ProjectFormatError("text transform pivot is invalid")
        text_opacity = _bounded_float(child_value, "opacity", 0.0, 1.0)
        line_spacing = _bounded_float(child_value, "line_spacing", 0.25, 8.0)
        object_locked = _bool_field(child_value, "locked")
        try:
            frame = FloatRect(*[float(cast(int | float, item)) for item in frame_value])
            transform = AffineTransform(
                *[float(cast(int | float, item)) for item in transform_value]
            )
            transform_pivot = (
                None
                if pivot_value is None
                else Point(
                    float(cast(int | float, pivot_value[0])),
                    float(cast(int | float, pivot_value[1])),
                )
            )
            snapshot = TextObjectSnapshot(
                id=object_id,
                name=child_name.strip(),
                text=text,
                frame=frame,
                font_family=family.strip(),
                font_size=font_size,
                font_weight=font_weight,
                italic=italic,
                color=tuple(color_value),
                stroke_color=tuple(stroke_color_value),
                stroke_width=stroke_width,
                horizontal_alignment=horizontal,
                vertical_alignment=vertical,
                wrap_mode=wrap_mode,
                line_spacing=line_spacing,
                visible=_bool_field(child_value, "visible"),
                opacity=text_opacity,
                locked=object_locked,
                transform=transform,
                transform_pivot=transform_pivot,
            )
            # Run the domain validator now, even for validation-only loads.
            TextObject(
                id=snapshot.id,
                name=snapshot.name,
                text=snapshot.text,
                frame=snapshot.frame,
                font_family=snapshot.font_family,
                font_size=snapshot.font_size,
                font_weight=snapshot.font_weight,
                italic=snapshot.italic,
                color=snapshot.color,
                stroke_color=snapshot.stroke_color,
                stroke_width=snapshot.stroke_width,
                horizontal_alignment=snapshot.horizontal_alignment,
                vertical_alignment=snapshot.vertical_alignment,
                wrap_mode=snapshot.wrap_mode,
                line_spacing=snapshot.line_spacing,
                visible=snapshot.visible,
                opacity=snapshot.opacity,
                locked=snapshot.locked,
                transform=snapshot.transform,
                transform_pivot=snapshot.transform_pivot,
            )
        except (TypeError, ValueError) as error:
            raise ProjectFormatError("text object values are invalid") from error
        snapshots.append(snapshot)
    return tuple(snapshots)


def _parse_svg_element(
    value: dict[str, Any],
    archive: zipfile.ZipFile,
    by_name: dict[str, zipfile.ZipInfo],
) -> tuple[SvgElementSnapshot, str, int]:
    import xml.etree.ElementTree as ET

    object_id = _uuid_field(value, "id")
    name = value.get("name")
    element_kind = value.get("element_kind")
    if not isinstance(name, str) or not name.strip() or len(name) > 1024:
        raise ProjectFormatError("SVG object name is invalid")
    if not isinstance(element_kind, str) or not element_kind.strip() or len(element_kind) > 64:
        raise ProjectFormatError("SVG element kind is invalid")
    resource = _object_field(value, "resource")
    member = resource.get("path")
    if not isinstance(member, str):
        raise ProjectFormatError("SVG resource path is invalid")
    _validate_member_name(member)
    if member != f"vectors/{object_id}.svg.z":
        raise ProjectFormatError("SVG resource path does not match its object")
    if resource.get("encoding") != "svg-utf8-zlib":
        raise ProjectFormatError("SVG resource encoding is unsupported")
    raw_size = _bounded_int(resource, "raw_size", 1, MAX_SVG_RESOURCE_BYTES)
    info = by_name.get(member)
    if info is None or info.file_size > raw_size + 1024:
        raise ProjectFormatError("SVG resource is missing or too large")
    try:
        encoded = archive.read(info)
    except (KeyError, RuntimeError, zipfile.BadZipFile) as error:
        raise ProjectFormatError("SVG resource cannot be read") from error
    if raw_size / max(len(encoded), 1) > MAX_COMPRESSION_RATIO:
        raise ProjectFormatError("SVG resource compression ratio is unsafe")
    raw = _safe_zlib_decompress(encoded, raw_size)
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ProjectFormatError("SVG resource contains unsafe declarations")
    try:
        svg = raw.decode("utf-8")
        root = ET.fromstring(svg)
    except (UnicodeDecodeError, ET.ParseError) as error:
        raise ProjectFormatError("SVG resource XML is invalid") from error
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ProjectFormatError("SVG resource root is invalid")
    _validate_persisted_svg(root)

    bounds_value = value.get("bounds")
    transform_value = value.get("transform")
    pivot_value = value.get("transform_pivot")
    if (
        not isinstance(bounds_value, list)
        or len(bounds_value) != 4
        or not all(_is_finite_number(item) for item in bounds_value)
    ):
        raise ProjectFormatError("SVG object bounds are invalid")
    if (
        not isinstance(transform_value, list)
        or len(transform_value) != 6
        or not all(_is_finite_number(item) for item in transform_value)
    ):
        raise ProjectFormatError("SVG object transform is invalid")
    if pivot_value is not None and (
        not isinstance(pivot_value, list)
        or len(pivot_value) != 2
        or not all(_is_finite_number(item) for item in pivot_value)
    ):
        raise ProjectFormatError("SVG object pivot is invalid")
    stroke_width_value = value.get("stroke_width_override")
    if stroke_width_value is not None and (
        not _is_finite_number(stroke_width_value)
        or not 0.0 <= float(cast(int | float, stroke_width_value)) <= 8192.0
    ):
        raise ProjectFormatError("SVG stroke width override is invalid")
    try:
        snapshot = SvgElementSnapshot(
            id=object_id,
            name=name.strip(),
            element_kind=element_kind.strip(),
            svg=svg,
            intrinsic_bounds=FloatRect(*[float(cast(int | float, item)) for item in bounds_value]),
            transform=AffineTransform(
                *[float(cast(int | float, item)) for item in transform_value]
            ),
            transform_pivot=(
                None
                if pivot_value is None
                else Point(
                    float(cast(int | float, pivot_value[0])),
                    float(cast(int | float, pivot_value[1])),
                )
            ),
            fill_override=_optional_rgba(value.get("fill_override")),
            stroke_override=_optional_rgba(value.get("stroke_override")),
            stroke_width_override=(
                None if stroke_width_value is None else float(cast(int | float, stroke_width_value))
            ),
            visible=_bool_field(value, "visible"),
            opacity=_bounded_float(value, "opacity", 0.0, 1.0),
            locked=_bool_field(value, "locked"),
        )
        SvgObject(
            id=snapshot.id,
            name=snapshot.name,
            element_kind=snapshot.element_kind,
            svg=snapshot.svg,
            intrinsic_bounds=snapshot.intrinsic_bounds,
            transform=snapshot.transform,
            transform_pivot=snapshot.transform_pivot,
            fill_override=snapshot.fill_override,
            stroke_override=snapshot.stroke_override,
            stroke_width_override=snapshot.stroke_width_override,
            visible=snapshot.visible,
            opacity=snapshot.opacity,
            locked=snapshot.locked,
        )
    except (TypeError, ValueError) as error:
        raise ProjectFormatError("SVG object values are invalid") from error
    return snapshot, member, raw_size


def _optional_rgba(value: object) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(
            isinstance(channel, int) and not isinstance(channel, bool) and 0 <= channel <= 255
            for channel in value
        )
    ):
        raise ProjectFormatError("SVG color override is invalid")
    return cast(tuple[int, int, int, int], tuple(value))


def _validate_persisted_svg(root: object) -> None:
    import xml.etree.ElementTree as ET

    if not isinstance(root, ET.Element):
        raise ProjectFormatError("SVG resource root is invalid")
    forbidden = {
        "animate",
        "animatemotion",
        "animatetransform",
        "set",
        "script",
        "foreignobject",
        "image",
        "audio",
        "video",
        "filter",
    }
    for element in root.iter():
        kind = element.tag.rsplit("}", 1)[-1].lower()
        if kind in forbidden:
            raise ProjectFormatError(f"SVG resource contains forbidden <{kind}>")
        if kind == "style" and element.text:
            css = element.text.lower()
            if "@import" in css or "@keyframes" in css:
                raise ProjectFormatError("SVG resource contains unsupported CSS")
        for attribute, raw_value in element.attrib.items():
            name = attribute.rsplit("}", 1)[-1].lower()
            value = raw_value.strip()
            if name.startswith("on") or "javascript:" in value.lower():
                raise ProjectFormatError("SVG resource contains scripting")
            if name == "href" and not value.startswith("#"):
                raise ProjectFormatError("SVG resource contains an external reference")
            urls = [match.group(2).strip() for match in _SVG_URL_PATTERN.finditer(value)]
            if any(not item.startswith("#") for item in urls):
                raise ProjectFormatError("SVG resource contains an external URL")


def _read_tile(
    archive: zipfile.ZipFile,
    by_name: dict[str, zipfile.ZipInfo],
    descriptor: object,
    *,
    encoding: str,
    expected_size: int,
    max_tx: int,
    max_ty: int,
) -> tuple[tuple[int, int], bytes, str]:
    if not isinstance(descriptor, dict):
        raise ProjectFormatError("Tile descriptor must be an object")
    x = _bounded_int(descriptor, "x", 0, max_tx)
    y = _bounded_int(descriptor, "y", 0, max_ty)
    member = descriptor.get("path")
    if not isinstance(member, str):
        raise ProjectFormatError("Tile path is invalid")
    _validate_member_name(member)
    if descriptor.get("encoding") != encoding:
        raise ProjectFormatError("Tile encoding is unsupported")
    raw_size = _bounded_int(descriptor, "raw_size", expected_size, expected_size)
    info = by_name.get(member)
    if info is None or info.file_size > raw_size + 1024:
        raise ProjectFormatError("Tile entry is missing or too large")
    try:
        encoded = archive.read(info)
    except (KeyError, RuntimeError, zipfile.BadZipFile) as error:
        raise ProjectFormatError("Tile entry cannot be read") from error
    if raw_size / max(len(encoded), 1) > MAX_COMPRESSION_RATIO:
        raise ProjectFormatError("Tile compression ratio is unsafe")
    raw = _safe_zlib_decompress(encoded, raw_size)
    return (x, y), raw, member


def _safe_zlib_decompress(encoded: bytes, expected_size: int) -> bytes:
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(encoded, expected_size + 1)
        if len(raw) > expected_size or decompressor.unconsumed_tail:
            raise ProjectFormatError("Tile expands beyond its declared size")
        raw += decompressor.flush(expected_size + 1 - len(raw))
    except zlib.error as error:
        raise ProjectFormatError("Tile compression stream is invalid") from error
    if (
        len(raw) != expected_size
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ProjectFormatError("Tile decoded length or stream boundary is invalid")
    return raw


def _validate_thumbnail(
    archive: zipfile.ZipFile,
    by_name: dict[str, zipfile.ZipInfo],
    manifest: dict[str, Any],
) -> None:
    value = _object_field(manifest, "thumbnail")
    if value.get("path") != THUMBNAIL_PATH:
        raise ProjectFormatError("thumbnail path is invalid")
    width = _bounded_int(value, "width", 1, 256)
    height = _bounded_int(value, "height", 1, 256)
    info = by_name.get(THUMBNAIL_PATH)
    if info is None or info.file_size > MAX_THUMBNAIL_BYTES:
        raise ProjectFormatError("thumbnail is missing or too large")
    try:
        data = archive.read(info)
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise ProjectFormatError("thumbnail metadata does not match its image")
            image.verify()
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise ProjectFormatError("thumbnail is invalid") from error


def _validate_member_name(name: str) -> None:
    if "\\" in name:
        raise ProjectFormatError("archive member paths must use forward slashes")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0].endswith(":")
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ProjectFormatError("archive member path is unsafe")


def _object_field(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ProjectFormatError(f"{key} must be an object")
    return value


def _bounded_int(mapping: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ProjectFormatError(f"{key} is outside its allowed range")
    return value


def _is_finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _bounded_float(
    mapping: dict[str, Any],
    key: str,
    minimum: float,
    maximum: float,
) -> float:
    value = mapping.get(key)
    numeric = cast(int | float, value)
    if not _is_finite_number(value) or not minimum <= float(numeric) <= maximum:
        raise ProjectFormatError(f"{key} is outside its allowed range")
    return float(numeric)


def _bool_field(mapping: dict[str, Any], key: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise ProjectFormatError(f"{key} must be a boolean")
    return value


def _uuid_field(mapping: dict[str, Any], key: str) -> UUID:
    value = mapping.get(key)
    if not isinstance(value, str):
        raise ProjectFormatError(f"{key} must be a UUID")
    try:
        return UUID(value)
    except ValueError as error:
        raise ProjectFormatError(f"{key} must be a UUID") from error


def _optional_filename(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or Path(value).name != value
        or "/" in value
        or "\\" in value
    ):
        raise ProjectFormatError("source_name is invalid")
    return value


def _check_decoded_total(total: int) -> None:
    if total > MAX_DECODED_BYTES:
        raise ProjectFormatError("project decoded pixel data exceeds the safety limit")


def _write_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o600 << 16
    archive.writestr(info, data)


def _temporary_sibling(destination: Path) -> Path:
    cleanup_stale_sibling_temps(destination)
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
    "LoadedProject",
    "ProjectFormatError",
    "SaveReceipt",
    "load_project",
    "save_project",
    "validate_project",
]
