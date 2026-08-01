"""Versioned, validated, atomic EasyPicture project persistence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID, uuid4

import numpy as np
from PIL import Image

from easypicture import __version__
from easypicture.domain import (
    AffineTransform,
    FloatRect,
    PixelPoint,
    Point,
    Size,
    TextHorizontalAlignment,
    TextObject,
    TextVerticalAlignment,
    TextWrapMode,
)
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION, MAX_DOCUMENT_LAYERS
from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import ProgressSink
from easypicture.persistence.image_io import create_thumbnail_png
from easypicture.persistence.migrations import CURRENT_SCHEMA_VERSION, migrate_manifest
from easypicture.persistence.snapshots import (
    DocumentSnapshot,
    LayerSnapshot,
    RasterGroupSnapshot,
    TextObjectSnapshot,
    VectorGroupSnapshot,
)
from easypicture.pixels import DEFAULT_TILE_SIZE, MaskTileStore, TilePixelStore
from easypicture.temp_files import cleanup_stale_sibling_temps

PROJECT_FORMAT = "easypicture"
MANIFEST_PATH = "manifest.json"
THUMBNAIL_PATH = "thumbnail.png"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024
MAX_DECODED_BYTES = 4 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000
MAX_ARCHIVE_ENTRIES = MAX_DOCUMENT_LAYERS * 1024 + 1027
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
        manifest_layers: list[dict[str, Any]] = []
        manifest_layers_by_id: dict[UUID, dict[str, Any]] = {}
        manifest_nodes: list[dict[str, Any]] = []
        selection_tiles: list[dict[str, Any]] = []
        total_tiles = (
            sum(layer.pixels.allocated_tile_count for layer in snapshot.layers)
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
            for layer in snapshot.layers:
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
                    "visible": layer.visible,
                    "opacity": layer.opacity,
                    "locked": layer.locked,
                    "alpha_locked": layer.alpha_locked,
                    "tiles": descriptors,
                }
                manifest_layers.append(layer_manifest)
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

            ordered_nodes = snapshot.nodes
            for node in ordered_nodes:
                if isinstance(node, RasterGroupSnapshot):
                    manifest_nodes.append(
                        {
                            "type": "raster_group",
                            "id": str(node.id),
                            "name": node.name,
                            "visible": node.visible,
                            "opacity": node.opacity,
                            "locked": node.locked,
                            "children": [
                                manifest_layers_by_id[child.id] for child in node.children
                            ],
                        }
                    )
                elif isinstance(node, VectorGroupSnapshot):
                    manifest_nodes.append(
                        {
                            "type": "vector_group",
                            "id": str(node.id),
                            "name": node.name,
                            "visible": node.visible,
                            "opacity": node.opacity,
                            "locked": node.locked,
                            "children": [
                                _text_object_manifest(child) for child in node.children
                            ],
                        }
                    )
            if not snapshot.nodes:
                manifest_nodes.append(
                    {
                        "type": "raster_group",
                        "id": str(uuid4()),
                        "name": "栅格组",
                        "visible": True,
                        "opacity": 1.0,
                        "locked": False,
                        "children": [
                            manifest_layers_by_id[layer.id] for layer in snapshot.layers
                        ],
                    }
                )

            manifest = {
                "format": PROJECT_FORMAT,
                "schema_version": CURRENT_SCHEMA_VERSION,
                "app_version": __version__,
                "document_id": str(snapshot.document_id),
                "canvas": {
                    "width": snapshot.size.width,
                    "height": snapshot.size.height,
                },
                "working_color_space": "sRGB",
                "active_layer_id": str(snapshot.active_layer_id),
                "source_name": snapshot.source_name,
                "nodes": manifest_nodes,
                # Kept as a compatibility mirror for v1 tooling and diagnostics.
                "layers": manifest_layers,
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
            manifest = migrate_manifest(manifest_value)
        except ValueError as error:
            raise ProjectFormatError(str(error)) from error

        if manifest.get("format") != PROJECT_FORMAT:
            raise ProjectFormatError("project format marker is invalid")
        if manifest.get("working_color_space") != "sRGB":
            raise ProjectFormatError("project working color space is unsupported")
        document_id = _uuid_field(manifest, "document_id")
        canvas = _object_field(manifest, "canvas")
        width = _bounded_int(canvas, "width", 1, MAX_DOCUMENT_DIMENSION)
        height = _bounded_int(canvas, "height", 1, MAX_DOCUMENT_DIMENSION)
        size = Size(width, height)
        active_layer_id = _uuid_field(manifest, "active_layer_id")
        source_name = _optional_filename(manifest.get("source_name"))
        layers_value = manifest.get("layers")
        if layers_value is not None and (
            not isinstance(layers_value, list)
            or not 1 <= len(layers_value) <= MAX_DOCUMENT_LAYERS
        ):
            raise ProjectFormatError("project layer count is invalid")
        nodes_value = manifest.get("nodes", layers_value)
        if (
            not isinstance(nodes_value, list)
            or not 1 <= len(nodes_value) <= MAX_DOCUMENT_LAYERS
        ):
            raise ProjectFormatError("project node count is invalid")
        legacy_layers_by_id: dict[UUID, dict[str, Any]] = {}
        if isinstance(layers_value, list):
            for legacy_layer in layers_value:
                if not isinstance(legacy_layer, dict):
                    raise ProjectFormatError("layer entry must be an object")
                legacy_id = _uuid_field(legacy_layer, "id")
                if legacy_id in legacy_layers_by_id:
                    raise ProjectFormatError("layer UUIDs must be unique")
                legacy_layers_by_id[legacy_id] = legacy_layer
        raster_node_values: list[dict[str, Any]] = []
        group_node_values: list[dict[str, Any]] = []
        raster_group_values: list[dict[str, Any]] = []
        root_ids: set[UUID] = set()
        for node_value in nodes_value:
            if not isinstance(node_value, dict):
                raise ProjectFormatError("node entry must be an object")
            node_type = node_value.get("type")
            if node_type not in {"raster_group", "vector_group"}:
                raise ProjectFormatError("project nodes must be two-level groups")
            node_id = _uuid_field(node_value, "id")
            if node_id in root_ids:
                raise ProjectFormatError("node UUIDs must be unique")
            root_ids.add(node_id)
            name = node_value.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 1024:
                raise ProjectFormatError("group name is invalid")
            _bool_field(node_value, "visible")
            _bounded_float(node_value, "opacity", 0.0, 1.0)
            _bool_field(node_value, "locked")
            children = node_value.get("children")
            if not isinstance(children, list) or len(children) > MAX_DOCUMENT_LAYERS:
                raise ProjectFormatError("group children are invalid")
            if node_type == "raster_group":
                raster_group_values.append(node_value)
                for child in children:
                    if not isinstance(child, dict) or child.get("type") != "raster":
                        raise ProjectFormatError("raster group contains an invalid child")
                    child_id = _uuid_field(child, "id")
                    source = legacy_layers_by_id.get(child_id, child)
                    if child_id in root_ids:
                        raise ProjectFormatError("node UUIDs must be unique")
                    root_ids.add(child_id)
                    raster_node_values.append(source)
            else:
                group_node_values.append(node_value)
        if not raster_node_values:
            raise ProjectFormatError("project must contain a raster child layer")

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
                )
            )

        if active_layer_id not in layer_ids:
            raise ProjectFormatError("active layer does not belong to the project")

        layer_snapshots_by_id = {layer.id: layer for layer in layer_snapshots}
        raster_group_snapshots_by_id: dict[UUID, RasterGroupSnapshot] = {}
        for group_value in raster_group_values:
            group_id = _uuid_field(group_value, "id")
            children_value = group_value.get("children")
            assert isinstance(children_value, list)
            try:
                children = tuple(
                    layer_snapshots_by_id[_uuid_field(child, "id")]
                    for child in children_value
                    if isinstance(child, dict)
                )
            except KeyError as error:
                raise ProjectFormatError("raster group references an unknown child") from error
            raster_group_snapshots_by_id[group_id] = RasterGroupSnapshot(
                id=group_id,
                name=cast(str, group_value["name"]).strip(),
                children=children,
                visible=_bool_field(group_value, "visible"),
                opacity=_bounded_float(group_value, "opacity", 0.0, 1.0),
                locked=_bool_field(group_value, "locked"),
            )
        group_snapshots_by_id: dict[UUID, VectorGroupSnapshot] = {}
        used_object_ids = set(layer_ids) | root_ids
        for group_value in group_node_values:
            group_snapshot = _parse_text_group(group_value, used_object_ids)
            group_snapshots_by_id[group_snapshot.id] = group_snapshot

        node_snapshots: list[RasterGroupSnapshot | VectorGroupSnapshot] = []
        for node_value in nodes_value:
            node_id = _uuid_field(node_value, "id")
            node_type = node_value.get("type", "raster")
            if node_type == "raster_group":
                try:
                    node_snapshots.append(raster_group_snapshots_by_id[node_id])
                except KeyError as error:
                    raise ProjectFormatError("raster group is invalid") from error
            elif node_type == "vector_group":
                node_snapshots.append(group_snapshots_by_id[node_id])
            else:
                raise ProjectFormatError("unknown project node type")

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
            layers=tuple(layer_snapshots),
            active_layer_id=active_layer_id,
            selection=selection_store,
            project_path=original_path if is_recovery else path,
            source_name=source_name,
            nodes=tuple(node_snapshots),
        )
        return LoadedProject(snapshot, is_recovery, original_path)


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
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        encoded,
    )


def _text_object_manifest(text_object: TextObjectSnapshot) -> dict[str, Any]:
    """Serialize a text snapshot without leaking Qt objects into the archive."""
    # The snapshot type is deliberately duck-typed here to keep this module's
    # archive code independent from the domain implementation details.
    return {
        "type": "text",
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


def _parse_text_group(
    value: dict[str, Any],
    used_ids: set[UUID],
) -> VectorGroupSnapshot:
    group_id = _uuid_field(value, "id")
    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 1024:
        raise ProjectFormatError("text group name is invalid")
    visible = _bool_field(value, "visible")
    locked = _bool_field(value, "locked")
    opacity = _bounded_float(value, "opacity", 0.0, 1.0)
    children_value = value.get("children")
    if not isinstance(children_value, list) or len(children_value) > 256:
        raise ProjectFormatError("text group children are invalid")
    snapshots: list[TextObjectSnapshot] = []
    for child_value in children_value:
        if not isinstance(child_value, dict) or child_value.get("type") != "text":
            raise ProjectFormatError("text group contains an invalid child")
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
                isinstance(channel, int)
                and not isinstance(channel, bool)
                and 0 <= channel <= 255
                for channel in color_value
            )
        ):
            raise ProjectFormatError("text color is invalid")
        stroke_value = child_value.get(
            "stroke",
            {"color": [0, 0, 0, 255], "width": 0.0},
        )
        if not isinstance(stroke_value, dict):
            raise ProjectFormatError("text stroke is invalid")
        stroke_color_value = stroke_value.get("color")
        if (
            not isinstance(stroke_color_value, list)
            or len(stroke_color_value) != 4
            or not all(
                isinstance(channel, int)
                and not isinstance(channel, bool)
                and 0 <= channel <= 255
                for channel in stroke_color_value
            )
        ):
            raise ProjectFormatError("text stroke color is invalid")
        stroke_width = _bounded_float(stroke_value, "width", 0.0, 128.0)
        horizontal_value = child_value.get("horizontal_alignment")
        vertical_value = child_value.get("vertical_alignment")
        wrap_value = child_value.get("wrap_mode", TextWrapMode.AUTO.value)
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
            frame = FloatRect(
                *[
                    float(cast(int | float, item))
                    for item in frame_value
                ]
            )
            transform = AffineTransform(
                *[
                    float(cast(int | float, item))
                    for item in transform_value
                ]
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
    return VectorGroupSnapshot(
        id=group_id,
        name=name.strip(),
        children=tuple(snapshots),
        visible=visible,
        opacity=opacity,
        locked=locked,
    )


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
    checksum = descriptor.get("sha256")
    if (
        not isinstance(checksum, str)
        or len(checksum) != 64
        or any(character not in "0123456789abcdef" for character in checksum)
    ):
        raise ProjectFormatError("Tile checksum is invalid")
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
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise ProjectFormatError("Tile checksum does not match")
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
