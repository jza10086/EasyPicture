from __future__ import annotations

import json
import zipfile
from pathlib import Path

from easypicture.commands import DocumentCommandService
from easypicture.domain import (
    Document,
    FloatRect,
    GroupListChanged,
    GroupMembersChanged,
    GroupPropertiesChanged,
    RasterGroupLayer,
    VectorGroupLayer,
    VectorObjectChanged,
    VectorObjectKind,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import PROJECT_SCHEMA_VERSION, DocumentSnapshot
from easypicture.persistence.project_io import load_project, save_project


def _progress(_completed: int, _total: int, _message: str) -> None:
    return


def test_new_document_uses_a_raster_group_and_flat_raster_view() -> None:
    document = Document.create(64, 48)

    assert len(document.nodes) == 1
    assert isinstance(document.nodes[0], RasterGroupLayer)
    assert document.nodes[0].children == [document.active_layer]
    assert document.layers == (document.active_layer,)


def test_group_commands_and_cross_group_child_moves_are_undoable() -> None:
    document = Document.create(64, 48)
    commands = DocumentCommandService()
    commands.attach_document(document)
    raster_root = document.nodes[0]
    assert isinstance(raster_root, RasterGroupLayer)

    assert commands.create_raster_group("Photos")
    photos = next(group for group in document.nodes if group.name == "Photos")
    assert isinstance(photos, RasterGroupLayer)
    assert commands.move_raster_layer_to_group(document.active_layer_id, photos.id, 0)
    assert document.layers == (document.active_layer,)
    assert photos.children == [document.active_layer]
    commands.undo_stack.undo()
    assert raster_root.children == [document.active_layer]
    commands.undo_stack.redo()
    assert photos.children == [document.active_layer]

    assert commands.create_vector_group("Labels")
    labels = next(group for group in document.nodes if group.name == "Labels")
    assert isinstance(labels, VectorGroupLayer)
    assert commands.create_text_object(
        labels.id,
        FloatRect(2.0, 2.0, 24.0, 12.0),
        name="Caption",
        text="P9C",
    )
    text_object = labels.children[0]
    assert commands.create_vector_group("Other")
    other = next(group for group in document.nodes if group.name == "Other")
    assert isinstance(other, VectorGroupLayer)
    assert commands.move_vector_object_to_group(text_object.id, other.id, 0)
    assert other.children == [text_object]
    commands.undo_stack.undo()
    assert labels.children == [text_object]


def test_group_and_vector_changes_emit_only_canonical_events() -> None:
    document = Document.create(32, 32)
    commands = DocumentCommandService()
    commands.attach_document(document)
    events: list[object] = []
    document.subscribe(events.append)

    assert commands.create_vector_group("Labels")
    assert sum(isinstance(event, GroupListChanged) for event in events) == 1
    events.clear()

    group = document.vector_groups[0]
    assert commands.create_text_object(group.id, FloatRect(0, 0, 16, 12))
    member_events = [event for event in events if isinstance(event, GroupMembersChanged)]
    object_events = [event for event in events if isinstance(event, VectorObjectChanged)]
    assert len(member_events) == 1
    assert len(object_events) == 1
    assert object_events[0].group_id == group.id
    assert object_events[0].object_kind is VectorObjectKind.SVG_TEXT
    assert not any(isinstance(event, GroupListChanged) for event in events)
    events.clear()

    assert commands.set_group_opacity(group.id, 0.5)
    property_events = [event for event in events if isinstance(event, GroupPropertiesChanged)]
    assert len(property_events) == 1
    assert property_events[0].properties == frozenset({"opacity"})
    assert not any(isinstance(event, GroupMembersChanged) for event in events)


def test_group_names_are_unique_and_last_raster_layer_is_protected() -> None:
    document = Document.create(32, 32)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_raster_group("栅格组")
    assert [node.name for node in document.nodes] == ["栅格组", "栅格组.1"]
    assert not commands.delete_group(document.nodes[0].id)
    assert commands.delete_group(document.nodes[1].id)
    assert len(document.nodes) == 1


def test_ungroup_and_duplicate_preserve_children_and_undo() -> None:
    document = Document.create(32, 32)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_vector_group("Labels")
    group = document.vector_groups[0]
    assert commands.create_text_object(group.id, FloatRect(0, 0, 10, 10), name="Text")
    assert commands.duplicate_group(group.id)
    duplicate = document.vector_groups[-1]
    assert duplicate.name == "Labels.1"
    assert duplicate.children[0].name == "Text.1"
    assert commands.ungroup(duplicate.id)
    assert len(document.vector_groups) == 1
    assert len(document.vector_groups[0].children) == 2
    commands.undo_stack.undo()
    assert len(document.vector_groups) == 2


def test_ungroup_undo_redo_restores_children_without_duplication() -> None:
    document = Document.create(32, 32)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_vector_group("Labels")
    group = document.vector_groups[0]
    assert commands.create_text_object(group.id, FloatRect(0, 0, 10, 10), name="A")
    assert commands.create_text_object(group.id, FloatRect(10, 0, 10, 10), name="B")
    assert commands.ungroup(group.id)
    assert len(document.vector_groups) == 1
    assert [child.name for child in document.vector_groups[0].children] == ["A", "B"]
    commands.undo_stack.undo()
    assert [child.name for child in document.vector_groups[0].children] == ["A", "B"]
    commands.undo_stack.redo()
    assert [child.name for child in document.vector_groups[0].children] == ["A", "B"]


def test_current_schema_round_trip_keeps_interleaved_groups(tmp_path: Path, qapp) -> None:
    _ = qapp
    document = Document.create(64, 48)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_vector_group("Labels")
    labels = document.vector_groups[0]
    assert commands.create_text_object(labels.id, FloatRect(1, 1, 20, 10), text="中文")
    path = tmp_path / "groups.limg"
    save_project(DocumentSnapshot.capture(document), path, CancellationToken(), _progress)

    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["schema_version"] == PROJECT_SCHEMA_VERSION
    assert "layers" not in manifest
    assert [node["type"] for node in manifest["nodes"]] == ["raster_group", "vector_group"]
    restored = load_project(path, CancellationToken(), _progress).snapshot.to_document()
    assert len(restored.nodes) == 2
    assert isinstance(restored.nodes[0], RasterGroupLayer)
    assert isinstance(restored.nodes[1], VectorGroupLayer)
    assert restored.vector_groups[0].children[0].text == "中文"
