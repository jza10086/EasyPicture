from __future__ import annotations

import numpy as np
import pytest

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, PixelPoint, Size, SpriteFrameContentMode
from easypicture.imaging import (
    TileDirection,
    TileTransitionMode,
    TileTransitionRequest,
    analyze_self_seams,
    analyze_tile_seam,
    compose_tile_pair,
    compose_transition_preview,
    generate_tile_transition,
    offset_tile,
    repeat_tile,
)
from easypicture.pixels import Rect


def _tile(width: int = 4, height: int = 4) -> np.ndarray:
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[..., 3] = 255
    return pixels


def test_seam_metrics_cover_tolerance_alpha_and_hidden_rgb() -> None:
    first = _tile()
    second = _tile()
    first[:, -1] = [10, 20, 30, 0]
    second[:, 0] = [10, 20, 30, 255]
    with_alpha = analyze_tile_seam(
        first,
        second,
        TileDirection.LEFT_TO_RIGHT,
    )
    without_alpha = analyze_tile_seam(
        first,
        second,
        TileDirection.LEFT_TO_RIGHT,
        include_alpha=False,
    )
    assert with_alpha.matching_percent == 0.0
    assert with_alpha.maximum_difference == 255
    assert without_alpha.matching_percent == 100.0

    second[2, 0] = [11, 20, 30, 0]
    tolerated = analyze_tile_seam(
        first,
        second,
        TileDirection.LEFT_TO_RIGHT,
        tolerance=1,
        include_alpha=False,
    )
    assert tolerated.matching_percent == 100.0


def test_self_seams_report_horizontal_and_vertical_positions() -> None:
    tile = _tile(3, 2)
    tile[0, 0] = [1, 0, 0, 255]
    horizontal, vertical = analyze_self_seams(tile)
    assert horizontal.mismatched_positions == (0,)
    assert vertical.mismatched_positions == (0,)


def test_integer_offset_normalizes_negative_and_large_values() -> None:
    tile = _tile(3, 2)
    tile[..., 0] = np.arange(6, dtype=np.uint8).reshape(2, 3)
    assert np.array_equal(offset_tile(tile, -1, 0), offset_tile(tile, 2, 0))
    assert np.array_equal(offset_tile(tile, 7, 5), offset_tile(tile, 1, 1))


def test_repeat_pair_and_transition_layouts() -> None:
    first = _tile(2, 1)
    second = _tile(2, 1)
    first[..., 0] = 255
    second[..., 2] = 255
    assert repeat_tile(first, 3).shape == (3, 6, 4)
    right_to_left = compose_tile_pair(first, second, TileDirection.RIGHT_TO_LEFT)
    assert right_to_left[0, 0].tolist() == [0, 0, 255, 255]
    preview = compose_transition_preview(
        first,
        first,
        second,
        TileDirection.TOP_TO_BOTTOM,
    )
    assert preview.shape == (3, 2, 4)


@pytest.mark.parametrize("direction", tuple(TileDirection))
def test_hard_cut_selects_only_whole_source_pixels(direction: TileDirection) -> None:
    first = _tile()
    second = _tile()
    first[...] = [255, 0, 0, 127]
    second[...] = [0, 0, 255, 200]
    result = generate_tile_transition(
        TileTransitionRequest(
            first,
            second,
            direction,
            TileTransitionMode.HARD_CUT,
            50.0,
            1,
        )
    )
    colors = {tuple(pixel) for pixel in result.reshape(-1, 4)}
    assert colors == {(255, 0, 0, 127), (0, 0, 255, 200)}


def test_bayer_transition_is_deterministic_and_creates_no_new_colors() -> None:
    first = _tile(8, 8)
    second = _tile(8, 8)
    first[...] = [4, 8, 12, 0]
    second[...] = [200, 210, 220, 255]
    request = TileTransitionRequest(
        first,
        second,
        TileDirection.LEFT_TO_RIGHT,
        TileTransitionMode.ORDERED_DITHER,
        50.0,
        6,
    )
    first_result = generate_tile_transition(request)
    second_result = generate_tile_transition(request)
    assert np.array_equal(first_result, second_result)
    assert {tuple(pixel) for pixel in first_result.reshape(-1, 4)} == {
        (4, 8, 12, 0),
        (200, 210, 220, 255),
    }


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        repeat_tile(_tile(), 4)
    with pytest.raises(ValueError):
        analyze_tile_seam(
            _tile(),
            _tile(3, 4),
            TileDirection.LEFT_TO_RIGHT,
        )


def test_detached_transition_frame_is_one_undoable_command() -> None:
    document = Document.create(8, 4)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(Size(4, 4), clear_frames=False)
    pixels = _tile()
    pixels[...] = [40, 80, 120, 255]
    command_count = service.undo_stack.count()
    frame_id = service.create_detached_sprite_frame(
        pixels,
        name="transition_001",
        source_region=Rect(0, 0, 4, 4),
        pivot=PixelPoint(1, 3),
        duration_ticks=2,
    )
    assert frame_id is not None
    assert service.undo_stack.count() == command_count + 1
    frame = document.sprite_workspace.frame_by_id(frame_id)
    assert frame.content_mode is SpriteFrameContentMode.DETACHED
    assert frame.pivot == PixelPoint(1, 3)
    assert frame.duration_ticks == 2
    assert frame_id in document.sprite_workspace.ungrouped_frame_ids
    embedded = document.embedded_document_for_frame(frame_id)
    assert np.array_equal(
        embedded.active_layer.pixels.read_region(Rect(0, 0, 4, 4)),
        pixels,
    )
    service.undo_stack.undo()
    with pytest.raises(ValueError):
        document.sprite_workspace.frame_by_id(frame_id)
    assert embedded.id not in document.embedded_documents
    service.undo_stack.redo()
    assert document.embedded_document_for_frame(frame_id) is embedded


def test_detached_transition_rejects_wrong_shape_without_history() -> None:
    document = Document.create(8, 4)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(Size(4, 4), clear_frames=False)
    before = service.undo_stack.count()
    assert service.create_detached_sprite_frame(
        _tile(3, 4),
        name="bad",
        source_region=Rect(0, 0, 4, 4),
    ) is None
    assert service.undo_stack.count() == before
