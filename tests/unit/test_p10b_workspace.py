from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from easypicture.commands import DocumentCommandService
from easypicture.domain import (
    Document,
    GroupDeleteMode,
    SpriteFrameContentMode,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import (
    DocumentSnapshot,
    SpriteAtlasExportRequest,
    export_sprite_atlas,
    load_project,
    save_project,
)
from easypicture.pixels import Rect, TileCoord


def _progress(_done: int, _total: int, _label: str) -> None:
    pass


def _project_with_two_frames() -> tuple[Document, DocumentCommandService]:
    document = Document.create(32, 16)
    red = np.zeros((16, 16, 4), dtype=np.uint8)
    red[..., 0] = 255
    red[..., 3] = 255
    document.active_layer.pixels.write_region(Rect(0, 0, 16, 16), red)
    service = DocumentCommandService()
    service.attach_document(document)
    service.set_sprite_cell_size(document.size.__class__(16, 16), clear_frames=False)
    assert service.create_sprite_frame(Rect(0, 0, 16, 16))
    assert service.create_sprite_frame(Rect(16, 0, 16, 16))
    return document, service


def test_detached_frame_is_embedded_and_undoable() -> None:
    document, service = _project_with_two_frames()
    frame = document.sprite_workspace.frames[0]
    assert service.detach_sprite_frame(frame.id)
    detached = document.sprite_workspace.frame_by_id(frame.id)
    assert detached.content_mode is SpriteFrameContentMode.DETACHED
    embedded = document.embedded_document_for_frame(frame.id)
    assert embedded.size.width == 16
    assert embedded.active_layer.pixels.read_tile(0, 0)[0, 0].tolist() == [255, 0, 0, 255]

    service.undo_stack.undo()
    assert (
        document.sprite_workspace.frame_by_id(frame.id).content_mode
        is SpriteFrameContentMode.LINKED
    )
    assert not document.embedded_documents
    service.undo_stack.redo()
    assert document.embedded_document_for_frame(frame.id) is embedded


def test_groups_are_single_membership_and_delete_modes() -> None:
    document, service = _project_with_two_frames()
    first, second = document.sprite_workspace.frames
    assert service.create_sprite_frame_group("待机")
    group = document.sprite_workspace.groups[0]
    assert service.move_sprite_frame_to_group(first.id, group.id, 0)
    assert service.move_sprite_frame_to_group(second.id, group.id, 1)
    assert document.sprite_workspace.group_by_id(group.id).frame_ids == (first.id, second.id)
    assert service.delete_sprite_frame_group(group.id, GroupDeleteMode.MOVE_TO_UNGROUPED)
    assert document.sprite_workspace.ungrouped_frame_ids == (first.id, second.id)


def test_schema_v3_round_trip_with_detached_frame(tmp_path: Path) -> None:
    document, service = _project_with_two_frames()
    frame = document.sprite_workspace.frames[0]
    assert service.detach_sprite_frame(frame.id)
    assert service.create_sprite_frame_group("攻击")
    group = document.sprite_workspace.groups[0]
    assert service.move_sprite_frame_to_group(frame.id, group.id, 0)
    path = tmp_path / "project.limg"
    save_project(DocumentSnapshot.capture(document), path, CancellationToken(), _progress)
    loaded = load_project(path, CancellationToken(), _progress).snapshot.to_document()
    loaded_frame = loaded.sprite_workspace.frame_by_id(frame.id)
    assert loaded_frame.content_mode is SpriteFrameContentMode.DETACHED
    assert loaded.sprite_workspace.groups[0].frame_ids == (frame.id,)
    assert loaded.embedded_document_for_frame(frame.id).size.width == 16


def test_grouped_atlas_exports_png_and_json(tmp_path: Path) -> None:
    document, service = _project_with_two_frames()
    first = document.sprite_workspace.frames[0]
    assert service.create_sprite_frame_group("待机")
    group = document.sprite_workspace.groups[0]
    assert service.move_sprite_frame_to_group(first.id, group.id, 0)
    receipt = export_sprite_atlas(
        SpriteAtlasExportRequest(
            DocumentSnapshot.capture(document),
            tmp_path / "atlas.png",
            horizontal_spacing=1,
            vertical_spacing=2,
            margin=1,
        ),
        CancellationToken(),
        _progress,
    )
    with Image.open(receipt.png_path) as image:
        assert image.size == (18, 36)
    manifest = json.loads(receipt.json_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert [group["name"] for group in manifest["groups"]] == ["待机", "未分组"]
    assert len(manifest["frames"]) == 2


def test_child_edit_marks_parent_modified() -> None:
    document, service = _project_with_two_frames()
    frame = document.sprite_workspace.frames[0]
    assert service.detach_sprite_frame(frame.id)
    document.mark_clean()
    embedded = document.embedded_document_for_frame(frame.id)
    service.set_edit_document(embedded)
    pixels = embedded.active_layer.pixels.read_tile(0, 0).copy()
    pixels[0, 0] = [0, 255, 0, 255]
    assert service.apply_pixel_tiles(
        embedded.active_layer_id,
        {TileCoord(0, 0): pixels},
        "修改独立帧",
    )
    assert document.modified


def test_multi_frame_delete_and_group_move_are_atomic() -> None:
    document, service = _project_with_two_frames()
    first, second = document.sprite_workspace.frames
    assert service.create_sprite_frame_group("攻击")
    group = document.sprite_workspace.groups[0]

    assert service.move_sprite_frames_to_group((first.id, second.id), group.id, 0)
    assert document.sprite_workspace.group_by_id(group.id).frame_ids == (
        first.id,
        second.id,
    )
    service.undo_stack.undo()
    assert document.sprite_workspace.ungrouped_frame_ids == (first.id, second.id)

    assert service.delete_sprite_frames((first.id, second.id))
    assert not document.sprite_workspace.frames
    service.undo_stack.undo()
    assert tuple(frame.id for frame in document.sprite_workspace.frames) == (
        first.id,
        second.id,
    )
