from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from PIL import Image

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, PixelPoint, Size
from easypicture.imaging import (
    CanvasAnchor,
    CanvasResizeRequest,
    calculate_canvas_resize,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import (
    DocumentSnapshot,
    SpriteFrameExportRequest,
    export_sprite_frame_png,
    load_project,
    save_project,
)
from easypicture.pixels import Rect


def _progress(_completed: int, _total: int, _message: str) -> None:
    return


def test_sprite_workspace_defaults_and_commands_are_undoable(qtbot) -> None:
    document = Document.create(20, 12)
    service = DocumentCommandService()
    service.attach_document(document)

    assert document.sprite_workspace.cell_size == Size(20, 12)
    assert service.set_sprite_cell_size(Size(4, 3), clear_frames=False)
    assert service.create_sprite_frame(Rect(4, 3, 4, 3))
    frame = document.sprite_workspace.frames[0]
    assert frame.name == "frame_001"
    assert frame.pivot == PixelPoint(2, 1)
    assert service.update_sprite_frame(
        frame.id,
        name="idle",
        pivot=PixelPoint(2, 2),
    )
    assert document.sprite_workspace.frames[0].name == "idle"
    assert service.undo_stack.count() == 3

    service.undo_stack.undo()
    assert document.sprite_workspace.frames[0].name == "frame_001"
    service.undo_stack.undo()
    assert document.sprite_workspace.frames == []
    service.undo_stack.redo()
    assert document.sprite_workspace.frames[0].region == Rect(4, 3, 4, 3)


def test_project_round_trip_preserves_sprite_workspace(tmp_path: Path) -> None:
    document = Document.create(64, 48)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(Size(16, 12), clear_frames=False)
    assert service.create_sprite_frame(Rect(16, 12, 16, 12))
    frame = document.sprite_workspace.frames[0]
    assert service.update_sprite_frame(frame.id, name="run_01", pivot=PixelPoint(8, 11))

    path = tmp_path / "sprites.limg"
    save_project(DocumentSnapshot.capture(document), path, CancellationToken(), _progress)
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert "grid_offset" not in manifest["sprite_workspace"]
    restored = load_project(path, CancellationToken(), _progress).snapshot.to_document()

    assert restored.sprite_workspace.cell_size == Size(16, 12)
    assert restored.sprite_workspace.frames[0].name == "run_01"
    assert restored.sprite_workspace.frames[0].pivot == PixelPoint(8, 11)


def test_export_sprite_frame_uses_latest_visible_composite(tmp_path: Path) -> None:
    document = Document.create(8, 8)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(Size(4, 4), clear_frames=False)
    assert service.create_sprite_frame(Rect(2, 1, 4, 4))
    frame = document.sprite_workspace.frames[0]
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    pixels[1:5, 2:6] = (10, 20, 30, 128)
    document.active_layer.pixels.replace_tile(0, 0, pixels)
    document.active_layer.record_pixel_change()

    output = tmp_path / "frame.png"
    receipt = export_sprite_frame_png(
        SpriteFrameExportRequest(DocumentSnapshot.capture(document), frame.id, output),
        CancellationToken(),
        _progress,
    )

    assert receipt.width == 4
    assert receipt.height == 4
    with Image.open(output) as image:
        assert image.size == (4, 4)
        assert tuple(np.asarray(image.convert("RGBA"))[0, 0]) == (10, 20, 30, 128)


def test_geometry_clear_restores_frames_in_same_undo_step(qtbot) -> None:
    document = Document.create(16, 16)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(Size(4, 4), clear_frames=False)
    assert service.create_sprite_frame(Rect(8, 8, 4, 4))
    frame = document.sprite_workspace.frames[0]
    result = calculate_canvas_resize(
        CanvasResizeRequest(
            DocumentSnapshot.capture(document),
            Size(10, 10),
            CanvasAnchor.TOP_LEFT,
        ),
        CancellationToken(),
        _progress,
    )

    assert not service.apply_document_geometry(result, "resize")
    assert service.apply_document_geometry(
        result,
        "resize",
        clear_sprite_frames=True,
    )
    assert document.sprite_workspace.frames == []
    service.undo_stack.undo()
    assert document.size == Size(16, 16)
    assert document.sprite_workspace.frames == [frame]


def test_canvas_expand_translates_and_preserves_frames_animation_and_detached_content(
    qtbot,
) -> None:
    document = Document.create(16, 16)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(Size(4, 4), clear_frames=False)
    assert service.create_sprite_frame(Rect(0, 0, 4, 4))
    frame = document.sprite_workspace.frames[0]
    assert service.detach_sprite_frame(frame.id)
    assert service.create_sprite_animation("待机")
    animation = document.sprite_workspace.animations[0]
    assert service.move_sprite_frame_to_animation(frame.id, animation.id, 0)
    embedded = dict(document.embedded_documents)
    result = calculate_canvas_resize(
        CanvasResizeRequest(DocumentSnapshot.capture(document), Size(24, 20), CanvasAnchor.CENTER),
        CancellationToken(),
        _progress,
    )

    assert service.apply_document_geometry(
        result,
        "扩大画布",
        sprite_frame_offset=PixelPoint(4, 2),
    )
    moved = document.sprite_workspace.frames[0]
    assert moved.id == frame.id and moved.region == Rect(4, 2, 4, 4)
    assert document.sprite_workspace.animations[0].frame_ids == (frame.id,)
    assert document.embedded_documents == embedded
    service.undo_stack.undo()
    assert document.size == Size(16, 16)
    assert document.sprite_workspace.frames[0].region == Rect(0, 0, 4, 4)
    assert document.embedded_documents == embedded


def test_canvas_shrink_deletes_only_out_of_bounds_frames_and_filters_animation(qtbot) -> None:
    document = Document.create(16, 8)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(Size(4, 4), clear_frames=False)
    assert service.create_sprite_frame(Rect(0, 0, 4, 4))
    assert service.create_sprite_frame(Rect(12, 0, 4, 4))
    first, second = document.sprite_workspace.frames
    assert service.detach_sprite_frame(second.id)
    first, second = document.sprite_workspace.frames
    embedded = dict(document.embedded_documents)
    assert service.create_sprite_animation("移动")
    animation = document.sprite_workspace.animations[0]
    assert service.move_sprite_frames_to_animation((first.id, second.id), animation.id, 0)
    result = calculate_canvas_resize(
        CanvasResizeRequest(DocumentSnapshot.capture(document), Size(12, 8), CanvasAnchor.TOP_LEFT),
        CancellationToken(),
        _progress,
    )

    assert not service.apply_document_geometry(
        result,
        "缩小画布",
        sprite_frame_offset=PixelPoint(),
    )
    assert service.apply_document_geometry(
        result,
        "缩小画布",
        sprite_frame_offset=PixelPoint(),
        delete_out_of_bounds_sprite_frames=True,
    )
    assert [frame.id for frame in document.sprite_workspace.frames] == [first.id]
    assert document.sprite_workspace.animations[0].frame_ids == (first.id,)
    assert document.embedded_documents == {}
    service.undo_stack.undo()
    assert [frame.id for frame in document.sprite_workspace.frames] == [first.id, second.id]
    assert document.embedded_documents == embedded


def test_sprite_frames_support_atomic_automatic_reordering(qtbot) -> None:
    document = Document.create(16, 8)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(Size(4, 4), clear_frames=False)
    for x in (0, 4, 8):
        assert service.create_sprite_frame(Rect(x, 0, 4, 4))
    before = tuple(frame.id for frame in document.sprite_workspace.frames)
    reversed_ids = tuple(reversed(before))

    assert service.reorder_sprite_frames(reversed_ids, label="反转精灵帧顺序")
    assert tuple(frame.id for frame in document.sprite_workspace.frames) == reversed_ids
    assert service.undo_stack.undoText() == "反转精灵帧顺序"

    service.undo_stack.undo()
    assert tuple(frame.id for frame in document.sprite_workspace.frames) == before
    assert not service.reorder_sprite_frames(before)
    assert not service.reorder_sprite_frames(before[:-1])


def test_sprite_workspace_allows_multiple_frames_for_the_same_region(qtbot) -> None:
    document = Document.create(16, 16)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(Size(4, 4), clear_frames=False)
    region = Rect(4, 4, 4, 4)

    assert service.create_sprite_frame(region)
    assert service.create_sprite_frame(region)

    first, second = document.sprite_workspace.frames
    assert first.id != second.id
    assert first.name == "frame_001"
    assert second.name == "frame_002"
    assert first.region == second.region == region
