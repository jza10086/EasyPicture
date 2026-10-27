from __future__ import annotations

import numpy as np

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, SelectionChanged, Size
from easypicture.imaging import (
    RegionQuery,
    SelectionAdjustment,
    SelectionAdjustmentRequest,
    SelectionBulkOperation,
    SelectionBulkRequest,
    SelectionCombineMode,
    calculate_bulk_selection,
    calculate_selection_adjustment,
    clear_selection_tiles,
    combine_selection_tiles,
    invert_selection_tiles,
    rectangle_candidate,
    select_all_candidate,
)
from easypicture.imaging.region import find_region
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import MaskTileStore, Rect, TileCoord


class _NeverCancelled:
    def raise_if_cancelled(self) -> None:
        pass


def _mask(value: int) -> np.ndarray:
    return np.full((256, 256), value, dtype=np.uint8)


def test_selection_combine_modes_use_fuzzy_set_math() -> None:
    size = Size(256, 256)
    current = MaskTileStore()
    current.replace_tile(0, 0, _mask(128))
    candidate = {TileCoord(0, 0): _mask(64)}

    expected = {
        SelectionCombineMode.REPLACE: 64,
        SelectionCombineMode.ADD: 128,
        SelectionCombineMode.SUBTRACT: 128,
        SelectionCombineMode.INTERSECT: 64,
    }
    for mode, value in expected.items():
        updates = combine_selection_tiles(current, candidate, size, mode)
        result = updates.get(TileCoord(0, 0), current.read_tile(0, 0))
        assert result is not None
        assert int(result[0, 0]) == value


def test_rectangle_candidate_clips_edges_and_spans_tiles() -> None:
    result = rectangle_candidate(Size(300, 300), Rect(250, 250, 100, 100))

    assert set(result) == {
        TileCoord(0, 0),
        TileCoord(1, 0),
        TileCoord(0, 1),
        TileCoord(1, 1),
    }
    assert result[TileCoord(0, 0)][249, 249] == 0
    assert result[TileCoord(0, 0)][250, 250] == 255
    assert result[TileCoord(1, 1)][43, 43] == 255
    assert result[TileCoord(1, 1)][44, 44] == 0


def test_selection_command_updates_modified_events_and_clean_state() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    events: list[object] = []
    document.subscribe(events.append)

    assert service.apply_selection_tiles(
        {TileCoord(0, 0): _mask(255)},
        "全选",
    )
    assert document.modified
    assert document.selection.is_active
    assert any(isinstance(event, SelectionChanged) for event in events)

    service.undo_stack.undo()
    assert not document.selection.is_active
    assert not document.modified
    service.undo_stack.redo()
    assert document.selection.is_active


def test_selection_noop_and_zero_tiles_do_not_create_commands() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)

    assert not service.apply_selection_tiles({TileCoord(0, 0): None}, "无变化")
    assert not service.apply_selection_tiles({TileCoord(0, 0): _mask(0)}, "无变化")
    assert service.undo_stack.count() == 0
    assert not document.modified


def test_select_all_invert_and_clear() -> None:
    size = Size(300, 257)
    store = MaskTileStore()
    all_tiles = select_all_candidate(size)
    for coord, tile in all_tiles.items():
        store.replace_tile(coord.x, coord.y, tile)
    assert store.has_nonzero_data

    inverted = invert_selection_tiles(store, size)
    assert inverted
    assert all(value is None for value in inverted.values())
    cleared = clear_selection_tiles(store)
    assert set(cleared) == set(all_tiles)


def test_feather_produces_fractional_selection_edges() -> None:
    document = Document.create(64, 64)
    hard = rectangle_candidate(document.size, Rect(16, 16, 32, 32))
    for coord, tile in hard.items():
        document.selection.mask.replace_tile(coord.x, coord.y, tile)
    request = SelectionAdjustmentRequest(
        DocumentSnapshot.capture(document),
        SelectionAdjustment.FEATHER,
        6,
    )

    result = calculate_selection_adjustment(
        request,
        _NeverCancelled(),
        lambda _value, _maximum, _message: None,
    )

    tile = result.updates[TileCoord(0, 0)]
    assert tile is not None
    assert tile[32, 32] == 255
    assert 0 < tile[16, 16] < 255
    assert tile[0, 0] == 0


def test_region_query_can_ignore_alpha() -> None:
    pixels = np.zeros((1, 2, 4), dtype=np.uint8)
    pixels[0, 0] = (20, 30, 40, 0)
    pixels[0, 1] = (20, 30, 40, 255)

    rgba = find_region(
        pixels,
        RegionQuery(seed=(0, 0), contiguous=False),
    )
    rgb = find_region(
        pixels,
        RegionQuery(seed=(0, 0), contiguous=False, include_alpha=False),
    )

    assert list(rgba.tiles.values())[0][0, 0] == 255
    assert list(rgba.tiles.values())[0][0, 1] == 0
    assert list(rgb.tiles.values())[0][0, 1] == 255


def test_bulk_select_all_returns_revision_checked_tile_updates() -> None:
    document = Document.create(300, 300)
    snapshot = DocumentSnapshot.capture(document)

    result = calculate_bulk_selection(
        SelectionBulkRequest(snapshot, SelectionBulkOperation.SELECT_ALL),
        _NeverCancelled(),
        lambda _value, _maximum, _message: None,
    )

    assert result.document_id == document.id
    assert result.revision == document.revision
    assert set(result.updates) == {
        TileCoord(0, 0),
        TileCoord(1, 0),
        TileCoord(0, 1),
        TileCoord(1, 1),
    }
