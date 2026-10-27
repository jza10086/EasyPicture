from __future__ import annotations

import json
import zipfile
from uuid import uuid4

import numpy as np
import pytest
from PySide6.QtCore import QPersistentModelIndex

from easypicture.commands import DocumentCommandService
from easypicture.domain import (
    Collection,
    Document,
    FloatRect,
    RasterObject,
    Size,
    TextObject,
    iter_effective_leaves,
    walk_nodes,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot
from easypicture.persistence.project_io import load_project, save_project
from easypicture.ui.layer_panel import LayerTreeModel


def _progress(*_args) -> None:
    pass


def test_empty_and_pure_vector_documents_round_trip(tmp_path, qapp):
    document = Document.create_empty(32, 24)
    commands = DocumentCommandService()
    commands.attach_document(document)
    for name in ("empty", "vector"):
        path = tmp_path / f"{name}.limg"
        save_project(DocumentSnapshot.capture(document), path, CancellationToken(), _progress)
        restored = load_project(path, CancellationToken(), _progress).snapshot.to_document()
        assert restored.layers == ()
        assert restored.active_layer_id is None
        assert [node.id for node in restored.nodes] == [node.id for node in document.nodes]
        if name == "empty":
            text = TextObject.create(FloatRect(1, 1, 20, 12), name="Text")
            assert commands.create_object(text)
            assert document.parent_of(text.id) is None
    assert commands.delete_nodes((document.nodes[0].id,))
    assert document.nodes == ()
    commands.undo_stack.undo()
    assert len(document.nodes) == 1


def test_batch_object_commands_are_atomic_and_preserve_unrelated_instances(qapp):
    document = Document.create(32, 24)
    commands = DocumentCommandService()
    commands.attach_document(document)
    first = document.active_layer
    assert commands.create_layer()
    second = document.active_layer
    changes = []
    document.subscribe_changes(changes.append)
    assert commands.objects.update((first.id, second.id), name="Pair")
    assert first.name == second.name == "Pair"
    assert not changes[-1].structural_operations
    assert document.layers[0] is first and document.layers[1] is second
    assert not commands.objects.update((first.id,), opacity="invalid")
    assert commands.objects.duplicate((first.id, second.id))
    assert document.layer_by_id(first.id) is first
    assert document.layer_by_id(second.id) is second
    before = commands.undo_stack.count()
    second.locked = True
    assert not commands.objects.delete((first.id, second.id))
    assert commands.undo_stack.count() == before


def test_nested_mixed_collection_order_inheritance_and_persistence(tmp_path, qapp):
    document = Document.create_empty(32, 24)
    raster = RasterObject.create_blank("Pixels", Size(50, 40))
    raster.pixels.writable_tile(0, 0)[35, 45] = (11, 22, 33, 255)
    text = TextObject.create(FloatRect(1, 1, 20, 12), name="Text")
    inner = Collection(uuid4(), "Inner", [text, raster], opacity=0.5, locked=True)
    outer = Collection(uuid4(), "Outer", [inner], opacity=0.5)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_object(outer)
    effective = iter_effective_leaves(document)
    assert [item.leaf.id for item in effective] == [text.id, raster.id]
    assert all(item.locked and item.opacity == 0.25 for item in effective)
    assert not commands.move_node(text.id, None, 0)
    path = tmp_path / "mixed.limg"
    save_project(DocumentSnapshot.capture(document), path, CancellationToken(), _progress)
    restored = load_project(path, CancellationToken(), _progress).snapshot.to_document()
    assert [node.id for node in walk_nodes(restored.nodes)] == [
        node.id for node in walk_nodes(document.nodes)
    ]
    np.testing.assert_array_equal(
        restored.layers[0].pixels.read_tile(0, 0), raster.pixels.read_tile(0, 0)
    )
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["nodes"][0]["type"] == "collection"
    assert manifest["nodes"][0]["children"][0]["type"] == "collection"


def test_move_cycle_rejection_nested_tree_and_undo(qapp):
    document = Document.create(32, 24)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_collection("Outer")
    outer = document.nodes[-1]
    assert commands.create_collection("Inner", outer.id)
    inner = document.node_by_id(outer.id).children[0]
    raster_id = document.active_layer_id
    assert raster_id is not None
    model = LayerTreeModel(commands)
    model.set_document(document)
    document.subscribe(model.handle_event)
    persistent_outer = QPersistentModelIndex(model.index_for_id(outer.id))
    assert commands.move_node(raster_id, inner.id, 0)
    assert model.node_for_index(model.index_for_id(raster_id).parent()).id == inner.id
    count = commands.undo_stack.count()
    assert not commands.move_node(outer.id, inner.id, 0)
    assert commands.undo_stack.count() == count
    assert persistent_outer.isValid()
    commands.undo_stack.undo()
    assert document.parent_of(raster_id).id == document.nodes[0].id
    commands.undo_stack.redo()
    assert document.parent_of(raster_id).id == inner.id


def test_duplicate_has_independent_pixels_and_delete_last_raster(qapp):
    document = Document.create(8, 8)
    commands = DocumentCommandService()
    commands.attach_document(document)
    source_id = document.active_layer.id
    document.active_layer.pixels.writable_tile(0, 0)[0, 0] = (20, 30, 40, 255)
    assert commands.duplicate_node(document.nodes[0].id)
    original, duplicate = document.layers
    assert original.id == source_id and duplicate.id != source_id
    duplicate.pixels.writable_tile(0, 0)[0, 0] = (90, 80, 70, 255)
    assert tuple(original.pixels.read_tile(0, 0)[0, 0]) == (20, 30, 40, 255)
    assert commands.delete_nodes(tuple(node.id for node in document.nodes))
    assert document.nodes == () and document.active_layer_id is None
    commands.undo_stack.undo()
    assert len(document.layers) == 2


def test_reject_shared_children_and_cycles_before_document_construction():
    raster = RasterObject.create_blank("Pixels", Size(8, 8))
    with pytest.raises(ValueError, match="unique"):
        Collection(uuid4(), "Duplicate", [raster, raster])
    first = Collection.create("First")
    second = Collection.create("Second")
    first.children.append(second)
    second.children.append(first)
    with pytest.raises(ValueError, match="cycles"):
        walk_nodes((first,))


def test_inherited_lock_rejects_all_property_entry_points_and_noop_move(qapp):
    document = Document.create(8, 8)
    commands = DocumentCommandService()
    commands.attach_document(document)
    group = document.nodes[0]
    raster = document.active_layer
    text = TextObject.create(FloatRect(0, 0, 4, 4))
    assert commands.create_object(text, group.id)
    before = commands.undo_stack.count()
    assert not commands.objects.move((raster.id, text.id), group.id, 0)
    assert commands.undo_stack.count() == before
    assert commands.objects.update((group.id,), locked=True)
    before = commands.undo_stack.count()
    assert not commands.rename_layer(raster.id, "Blocked")
    assert not commands.set_layer_visible(raster.id, False)
    assert not commands.set_layer_alpha_locked(raster.id, True)
    assert not commands.update_text_object(text.id, text="Blocked")
    assert not commands.set_text_object_locked(text.id, False)
    assert commands.undo_stack.count() == before
    assert commands.objects.update((group.id,), locked=False)


def test_outliner_indexes_do_not_retain_dangling_python_pointers(qapp):
    import gc

    document = Document.create(8, 8)
    commands = DocumentCommandService()
    commands.attach_document(document)
    model = LayerTreeModel(commands)
    model.set_document(document)
    unsubscribe = document.subscribe(model.handle_event)
    queried_rows = []

    def query_removing_row(parent, first, _last):
        index = model.index(first, 0, parent)
        queried_rows.append((model.data(index, model.LockedRole), model.flags(index)))

    model.rowsAboutToBeRemoved.connect(query_removing_row)
    for number in range(20):
        node = TextObject.create(FloatRect(0, 0, 8, 8), name=f"Text {number}")
        assert commands.objects.create(node)
        stale = model.index_for_id(node.id)
        persistent = QPersistentModelIndex(stale)
        assert model.node_for_index(stale) is document.node_by_id(node.id)
        assert commands.objects.delete((node.id,))
        assert not persistent.isValid()
        assert model.node_for_index(stale) is None
        commands.undo_stack.undo()
        assert model.node_for_index(model.index_for_id(node.id)).id == node.id
        assert model.node_for_index(stale) is None
        commands.clear_history_preserving_modified()
        del node
        gc.collect()
    unsubscribe()
    assert len(queried_rows) == 20
    model.set_document(None)
