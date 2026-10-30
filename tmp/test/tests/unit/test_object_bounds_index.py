from uuid import uuid4

import pytest

from easypicture.commands import DocumentCommandService
from easypicture.domain import AffineTransform, Document, FloatRect, Point
from easypicture.domain.spatial import ObjectBoundsIndex


def test_index_handles_negative_coordinates_large_bounds_and_replacement():
    index = ObjectBoundsIndex(cell_size=16, maximum_cells=4)
    small, large = uuid4(), uuid4()
    index.update(small, FloatRect(-20, -20, 10, 10))
    index.update(large, FloatRect(-10000, -10000, 20000, 20000))
    assert index.at(Point(-15, -15)) == frozenset({small, large})
    assert index.query(FloatRect(200, 200, 10, 10)) == frozenset({large})
    index.update(small, FloatRect(210, 210, 5, 5))
    assert index.at(Point(-15, -15)) == frozenset({large})
    assert index.at(Point(211, 211)) == frozenset({small, large})
    index.remove((small, large))
    assert index.object_ids == frozenset()


def test_transform_changes_bounds_and_lookup_survives_delete_undo(qapp):
    document = Document.create(128, 128)
    commands = DocumentCommandService()
    commands.attach_document(document)
    commands.create_vector_group()
    group = document.vector_groups[0]
    commands.create_text_object(group.id, frame=FloatRect(0, 0, 20, 10))
    text = group.children[0]
    initial = document.spatial_index.bounds(text.id)
    changes = []
    document.subscribe_changes(changes.append)
    commands.update_text_object(text.id, transform=AffineTransform.translation(100, 0))
    assert len(changes[-1].bounds_changes) == 1
    delta = changes[-1].bounds_changes[0]
    assert delta.old == initial
    assert delta.new.x == initial.x + 100
    assert text.id not in document.spatial_index.at(Point(5, 5))
    assert text.id in document.spatial_index.at(Point(105, 5))
    commands.delete_text_object(text.id)
    assert document.spatial_index.bounds(text.id) is None
    with pytest.raises(ValueError, match="belong"):
        document.node_by_id(text.id)
    commands.undo_stack.undo()
    assert document.node_by_id(text.id).id == text.id
    assert document.spatial_index.bounds(text.id) == delta.new


def test_metadata_does_not_move_bounds_or_invalidate_spatial_entries(qapp):
    document = Document.create(32, 32)
    layer = document.active_layer
    before = document.spatial_index.bounds(layer.id)
    changes = []
    document.subscribe_changes(changes.append)
    document.set_layer_name(layer.id, "new name")
    assert changes[-1].bounds_changes == ()
    assert document.spatial_index.bounds(layer.id) is before
