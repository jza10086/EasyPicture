from __future__ import annotations

import os
import subprocess
import sys
from uuid import uuid4

import pytest

from easypicture.domain import (
    ActiveLayerChanged,
    Document,
    DocumentModifiedChanged,
    DocumentRevisionChanged,
    RasterLayer,
    Selection,
    Size,
)
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION


def test_domain_import_does_not_load_qt() -> None:
    environment = os.environ.copy()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import easypicture.domain; "
                "raise SystemExit(1 if any(name.startswith('PySide6') "
                "for name in sys.modules) else 0)"
            ),
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (0, 1),
        (1, 0),
        (-1, 10),
        (MAX_DOCUMENT_DIMENSION + 1, 10),
        (10, MAX_DOCUMENT_DIMENSION + 1),
    ],
)
def test_document_rejects_out_of_range_dimensions(width: int, height: int) -> None:
    with pytest.raises(ValueError, match="between"):
        Document.create(width, height)


def test_document_create_builds_clean_transparent_default_layer() -> None:
    document = Document.create(640, 480)

    assert document.size == Size(640, 480)
    assert not document.modified
    assert document.revision == 0
    assert len(document.layers) == 1
    assert document.active_layer is document.layers[0]
    assert document.active_layer.name == "图层 1"
    assert document.active_layer.size == document.size
    assert document.active_layer.pixels.tile_size == 256
    assert document.active_layer.pixels.allocated_tile_count == 0
    assert document.selection.size == document.size
    assert document.selection.revision == 0


def test_new_documents_and_layers_have_unique_ids() -> None:
    first = Document.create(10, 10)
    second = Document.create(10, 10)

    assert first.id != second.id
    assert first.active_layer_id != second.active_layer_id


def test_record_change_and_mark_clean_emit_synchronous_events() -> None:
    document = Document.create(10, 10)
    events: list[object] = []
    unsubscribe = document.subscribe(events.append)

    document.record_change()
    document.record_change()
    document.mark_clean()
    unsubscribe()
    document.record_change()

    assert document.revision == 3
    assert document.modified
    assert isinstance(events[0], DocumentModifiedChanged)
    assert events[0].modified is True
    assert isinstance(events[1], DocumentRevisionChanged)
    assert isinstance(events[2], DocumentRevisionChanged)
    assert isinstance(events[3], DocumentModifiedChanged)
    assert events[3].modified is False
    assert len(events) == 4


def test_active_layer_change_validates_membership_and_emits_events() -> None:
    size = Size(20, 20)
    first = RasterLayer.create_blank("第一层", size)
    second = RasterLayer.create_blank("第二层", size)
    document = Document(
        document_id=uuid4(),
        size=size,
        layers=[first, second],
        active_layer_id=first.id,
        selection=Selection.create_empty(size),
    )
    events: list[object] = []
    document.subscribe(events.append)

    document.set_active_layer(second.id)

    assert document.active_layer is second
    assert document.revision == 1
    assert not document.modified
    assert isinstance(events[0], ActiveLayerChanged)
    assert isinstance(events[1], DocumentRevisionChanged)
    with pytest.raises(ValueError, match="belong"):
        document.set_active_layer(uuid4())
