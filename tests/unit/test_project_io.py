from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, FloatRect, PixelPoint, RasterLayer
from easypicture.jobs import CancellationToken, JobCancelled
from easypicture.persistence import DocumentSnapshot
from easypicture.persistence.project_io import (
    MANIFEST_PATH,
    ProjectFormatError,
    load_project,
    save_project,
)


def _progress(_completed: int, _total: int, _message: str) -> None:
    return


def _rewrite_manifest(
    source_path: Path,
    target_path: Path,
    mutate,
) -> None:
    with zipfile.ZipFile(source_path) as source, zipfile.ZipFile(
        target_path,
        "w",
        compression=zipfile.ZIP_STORED,
    ) as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == MANIFEST_PATH:
                manifest = json.loads(data)
                mutate(manifest)
                data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            target.writestr(info.filename, data)


def _sample_document() -> Document:
    document = Document.create(300, 260)
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[0, 0] = (11, 22, 33, 0)
    tile[1, 1] = (200, 50, 25, 128)
    document.active_layer.pixels.replace_tile(0, 0, tile)
    document.active_layer.record_pixel_change()
    document.set_layer_name(document.active_layer_id, "底图 α")
    document.set_layer_opacity(document.active_layer_id, 0.75)
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[3, 4] = 127
    document.selection.mask.replace_tile(0, 0, mask)
    top = RasterLayer.create_blank("隐藏层", document.size)
    top.visible = False
    top.opacity = 0.5
    top.locked = True
    top.alpha_locked = True
    document.insert_layer(top, 1)
    return document


def test_project_round_trip_preserves_sparse_pixels_and_selection(tmp_path: Path) -> None:
    document = _sample_document()
    snapshot = DocumentSnapshot.capture(document)
    path = tmp_path / "sample.limg"

    receipt = save_project(snapshot, path, CancellationToken(), _progress)
    loaded = load_project(path, CancellationToken(), _progress)
    restored = loaded.snapshot.to_document(project_path=path)

    assert receipt.document_id == document.id
    assert restored.id == document.id
    assert restored.project_path == path
    assert restored.display_name == "sample.limg"
    assert restored.size == document.size
    assert restored.layers[0].name == "底图 α"
    assert restored.layers[0].opacity == 0.75
    assert tuple(restored.layers[0].pixels.read_tile(0, 0)[0, 0]) == (11, 22, 33, 0)
    assert restored.active_layer.name == "隐藏层"
    assert restored.active_layer.size == document.size
    assert restored.active_layer.origin == PixelPoint()
    assert not restored.active_layer.visible
    assert restored.active_layer.locked
    assert restored.active_layer.alpha_locked
    assert restored.selection.mask.read_tile(0, 0)[3, 4] == 127


def test_failed_atomic_replace_preserves_existing_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "existing.limg"
    path.write_bytes(b"original")

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("easypicture.persistence.project_io.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated"):
        save_project(
            DocumentSnapshot.capture(_sample_document()),
            path,
            CancellationToken(),
            _progress,
        )

    assert path.read_bytes() == b"original"
    assert not list(tmp_path.glob(".existing.limg.*.tmp"))


@pytest.mark.parametrize("schema_version", [5, 6, 999])
def test_non_current_schema_is_rejected(
    tmp_path: Path,
    schema_version: int,
) -> None:
    original = tmp_path / "original.limg"
    altered = tmp_path / "future.limg"
    save_project(
        DocumentSnapshot.capture(_sample_document()),
        original,
        CancellationToken(),
        _progress,
    )
    with (
        zipfile.ZipFile(original) as source,
        zipfile.ZipFile(
            altered,
            "w",
            compression=zipfile.ZIP_STORED,
        ) as target,
    ):
        for info in source.infolist():
            data = source.read(info)
            if info.filename == MANIFEST_PATH:
                manifest = json.loads(data)
                manifest["schema_version"] = schema_version
                data = json.dumps(manifest).encode()
            target.writestr(info.filename, data)

    with pytest.raises(ProjectFormatError, match="version is unsupported"):
        load_project(altered, CancellationToken(), _progress)


def test_schema_v1_migrates_to_empty_sprite_workspace(tmp_path: Path) -> None:
    current = tmp_path / "current.limg"
    legacy = tmp_path / "legacy.limg"
    save_project(
        DocumentSnapshot.capture(_sample_document()),
        current,
        CancellationToken(),
        _progress,
    )

    def downgrade(manifest: dict[str, object]) -> None:
        manifest["schema_version"] = 1
        manifest.pop("sprite_workspace", None)

    _rewrite_manifest(current, legacy, downgrade)
    restored = load_project(legacy, CancellationToken(), _progress).snapshot.to_document()

    assert restored.sprite_workspace.frames == []
    assert restored.sprite_workspace.cell_size.width == 32
    assert restored.sprite_workspace.cell_size.height == 32


def test_path_traversal_entry_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.limg"
    save_project(
        DocumentSnapshot.capture(_sample_document()),
        path,
        CancellationToken(),
        _progress,
    )
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("../escape", b"x")

    with pytest.raises(ProjectFormatError, match="unsafe"):
        load_project(path, CancellationToken(), _progress)


def test_windows_absolute_entry_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "unsafe-windows.limg"
    save_project(
        DocumentSnapshot.capture(_sample_document()),
        path,
        CancellationToken(),
        _progress,
    )
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("C:/escape", b"x")

    with pytest.raises(ProjectFormatError, match="unsafe"):
        load_project(path, CancellationToken(), _progress)


def test_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    original = tmp_path / "original.limg"
    altered = tmp_path / "bad-checksum.limg"
    save_project(
        DocumentSnapshot.capture(_sample_document()),
        original,
        CancellationToken(),
        _progress,
    )
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(altered, "w") as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == MANIFEST_PATH:
                manifest = json.loads(data)
                raster_group = next(
                    node for node in manifest["nodes"] if node["type"] == "raster_group"
                )
                raster_group["children"][0]["tiles"][0]["sha256"] = "0" * 64
                data = json.dumps(manifest).encode()
            target.writestr(info.filename, data)

    with pytest.raises(ProjectFormatError, match="checksum"):
        load_project(altered, CancellationToken(), _progress)


def test_declared_decoded_limit_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "limited.limg"
    save_project(
        DocumentSnapshot.capture(_sample_document()),
        path,
        CancellationToken(),
        _progress,
    )
    monkeypatch.setattr("easypicture.persistence.project_io.MAX_DECODED_BYTES", 1)

    with pytest.raises(ProjectFormatError, match="safety limit"):
        load_project(path, CancellationToken(), _progress)


def test_cancelled_save_preserves_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "cancelled.limg"
    path.write_bytes(b"original")
    token = CancellationToken()
    token.cancel()

    with pytest.raises(JobCancelled):
        save_project(DocumentSnapshot.capture(_sample_document()), path, token, _progress)

    assert path.read_bytes() == b"original"


def test_allocated_all_zero_tiles_are_not_serialized(tmp_path: Path) -> None:
    document = Document.create(256, 256)
    document.active_layer.pixels.writable_tile(0, 0)
    document.selection.mask.writable_tile(0, 0)
    path = tmp_path / "sparse.limg"

    save_project(
        DocumentSnapshot.capture(document),
        path,
        CancellationToken(),
        _progress,
    )

    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read(MANIFEST_PATH))
    assert "layers" not in manifest
    raster_group = next(
        node for node in manifest["nodes"] if node["type"] == "raster_group"
    )
    assert raster_group["children"][0]["tiles"] == []
    assert manifest["selection"]["tiles"] == []


def test_missing_nodes_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.limg"
    invalid = tmp_path / "missing-nodes.limg"
    save_project(
        DocumentSnapshot.capture(_sample_document()),
        source,
        CancellationToken(),
        _progress,
    )
    _rewrite_manifest(source, invalid, lambda manifest: manifest.pop("nodes"))

    with pytest.raises(ProjectFormatError, match="node count"):
        load_project(invalid, CancellationToken(), _progress)


@pytest.mark.parametrize("missing_field", ["stroke", "wrap_mode"])
def test_current_text_fields_are_required(
    tmp_path: Path,
    missing_field: str,
    qapp,
) -> None:
    _ = qapp
    document = Document.create(64, 64)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_vector_group("文字")
    group = document.vector_groups[0]
    assert commands.create_text_object(group.id, FloatRect(0, 0, 32, 16))
    source = tmp_path / "source-text.limg"
    invalid = tmp_path / f"missing-{missing_field}.limg"
    save_project(DocumentSnapshot.capture(document), source, CancellationToken(), _progress)

    def remove_field(manifest) -> None:
        vector_group = next(
            node for node in manifest["nodes"] if node["type"] == "vector_group"
        )
        vector_group["children"][0].pop(missing_field)

    _rewrite_manifest(source, invalid, remove_field)
    with pytest.raises(ProjectFormatError, match="stroke|wrap"):
        load_project(invalid, CancellationToken(), _progress)


def test_old_text_type_is_rejected(tmp_path: Path, qapp) -> None:
    _ = qapp
    document = Document.create(64, 64)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_vector_group("文字")
    group = document.vector_groups[0]
    assert commands.create_text_object(group.id, FloatRect(0, 0, 32, 16))
    source = tmp_path / "source-text.limg"
    invalid = tmp_path / "old-text-type.limg"
    save_project(DocumentSnapshot.capture(document), source, CancellationToken(), _progress)

    def use_old_type(manifest) -> None:
        vector_group = next(
            node for node in manifest["nodes"] if node["type"] == "vector_group"
        )
        vector_group["children"][0]["type"] = "text"

    _rewrite_manifest(source, invalid, use_old_type)
    with pytest.raises(ProjectFormatError, match="invalid child"):
        load_project(invalid, CancellationToken(), _progress)


@pytest.mark.parametrize(
    ("size_value", "origin_value"),
    [({"width": 32, "height": 64}, [0, 0]), ({"width": 64, "height": 64}, [1, 0])],
)
def test_non_canvas_raster_geometry_is_rejected(
    tmp_path: Path,
    size_value: dict[str, int],
    origin_value: list[int],
) -> None:
    source = tmp_path / "source.limg"
    invalid = tmp_path / "invalid-geometry.limg"
    document = Document.create(64, 64)
    save_project(DocumentSnapshot.capture(document), source, CancellationToken(), _progress)

    def change_geometry(manifest) -> None:
        raster_group = next(
            node for node in manifest["nodes"] if node["type"] == "raster_group"
        )
        layer = raster_group["children"][0]
        layer["size"] = size_value
        layer["origin"] = origin_value

    _rewrite_manifest(source, invalid, change_geometry)
    with pytest.raises(ProjectFormatError, match="geometry must match"):
        load_project(invalid, CancellationToken(), _progress)
