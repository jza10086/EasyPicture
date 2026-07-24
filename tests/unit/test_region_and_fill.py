from __future__ import annotations

import numpy as np

from easypicture.domain import Document
from easypicture.imaging import (
    FillRequest,
    RegionQuery,
    calculate_fill,
    find_region,
)
from easypicture.jobs import CancellationToken
from easypicture.jobs.runner import JobCancelled
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import TileCoord


def test_rgba_tolerance_includes_alpha() -> None:
    reference = np.zeros((1, 2, 4), dtype=np.uint8)
    reference[0, 0] = (10, 20, 30, 0)
    reference[0, 1] = (10, 20, 30, 1)

    exact = find_region(reference, RegionQuery((0, 0), tolerance=0, contiguous=False))
    tolerant = find_region(reference, RegionQuery((0, 0), tolerance=1, contiguous=False))

    assert tuple(exact.tiles[TileCoord(0, 0)][0, :2]) == (255, 0)
    assert tuple(tolerant.tiles[TileCoord(0, 0)][0, :2]) == (255, 255)


def test_four_and_eight_neighbor_regions_differ_on_diagonal() -> None:
    reference = np.full((3, 3, 4), 255, dtype=np.uint8)
    reference[0, 0] = 0
    reference[1, 1] = 0

    four = find_region(reference, RegionQuery((0, 0), connectivity=4))
    eight = find_region(reference, RegionQuery((0, 0), connectivity=8))

    tile_four = four.tiles[TileCoord(0, 0)]
    tile_eight = eight.tiles[TileCoord(0, 0)]
    assert tile_four[1, 1] == 0
    assert tile_eight[1, 1] == 255


def test_non_contiguous_region_selects_all_matching_pixels() -> None:
    reference = np.full((1, 4, 4), 255, dtype=np.uint8)
    reference[0, 0] = 0
    reference[0, 2] = 0

    result = find_region(reference, RegionQuery((0, 0), contiguous=False))

    assert tuple(result.tiles[TileCoord(0, 0)][0, :4]) == (255, 0, 255, 0)


def test_fill_returns_one_layer_tile_patch() -> None:
    document = Document.create(4, 1)
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[0, :2] = (255, 0, 0, 255)
    tile[0, 2:4] = (0, 0, 255, 255)
    document.active_layer.pixels.replace_tile(0, 0, tile)
    request = FillRequest(
        snapshot=DocumentSnapshot.capture(document),
        layer_id=document.active_layer_id,
        query=RegionQuery((0, 0)),
        color=(0, 255, 0, 255),
    )

    result = calculate_fill(request, CancellationToken(), lambda *_args: None)

    after = result.updates[TileCoord(0, 0)]
    assert tuple(after[0, 0]) == (0, 255, 0, 255)
    assert tuple(after[0, 1]) == (0, 255, 0, 255)
    assert tuple(after[0, 2]) == (0, 0, 255, 255)


def test_fill_respects_fractional_selection_mask() -> None:
    document = Document.create(1, 1)
    selection = np.zeros((256, 256), dtype=np.uint8)
    selection[0, 0] = 128
    document.selection.mask.replace_tile(0, 0, selection)
    request = FillRequest(
        snapshot=DocumentSnapshot.capture(document),
        layer_id=document.active_layer_id,
        query=RegionQuery((0, 0)),
        color=(255, 0, 0, 255),
    )

    result = calculate_fill(request, CancellationToken(), lambda *_args: None)

    assert tuple(result.updates[TileCoord(0, 0)][0, 0]) == (255, 0, 0, 128)


def test_merged_sampling_uses_visible_layers_but_updates_active_layer() -> None:
    document = Document.create(4, 1)
    bottom = document.active_layer
    bottom_tile = np.zeros((256, 256, 4), dtype=np.uint8)
    bottom_tile[0, :2] = (255, 0, 0, 255)
    bottom_tile[0, 2:4] = (0, 0, 255, 255)
    bottom.pixels.replace_tile(0, 0, bottom_tile)
    from easypicture.domain import RasterLayer

    top = RasterLayer.create_blank("顶层", document.size)
    document.insert_layer(top, 1)
    request = FillRequest(
        snapshot=DocumentSnapshot.capture(document),
        layer_id=top.id,
        query=RegionQuery((0, 0)),
        color=(0, 255, 0, 255),
        sample_merged=True,
    )

    result = calculate_fill(request, CancellationToken(), lambda *_args: None)

    after = result.updates[TileCoord(0, 0)]
    assert tuple(after[0, 0]) == (0, 255, 0, 255)
    assert tuple(after[0, 1]) == (0, 255, 0, 255)
    assert tuple(after[0, 2]) == (0, 0, 0, 0)


def test_fill_honors_pre_cancelled_token() -> None:
    document = Document.create(4, 4)
    token = CancellationToken()
    token.cancel()
    request = FillRequest(
        snapshot=DocumentSnapshot.capture(document),
        layer_id=document.active_layer_id,
        query=RegionQuery((0, 0)),
        color=(1, 2, 3, 255),
    )

    try:
        calculate_fill(request, token, lambda *_args: None)
    except JobCancelled:
        pass
    else:
        raise AssertionError("cancelled fill must stop before producing a result")


def test_fill_expands_behind_semtransparent_brush_edge() -> None:
    document = Document.create(3, 1)
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[0, 1] = (0, 0, 0, 128)
    tile[0, 2] = (0, 0, 0, 255)
    document.active_layer.pixels.replace_tile(0, 0, tile)
    request = FillRequest(
        snapshot=DocumentSnapshot.capture(document),
        layer_id=document.active_layer_id,
        query=RegionQuery((0, 0)),
        color=(0, 255, 0, 255),
        edge_expansion=1,
    )

    result = calculate_fill(request, CancellationToken(), lambda *_args: None)

    after = result.updates[TileCoord(0, 0)]
    assert tuple(after[0, 0]) == (0, 255, 0, 255)
    assert tuple(after[0, 1]) == (0, 127, 0, 255)
    assert tuple(after[0, 2]) == (0, 0, 0, 255)


def test_fill_edge_expansion_does_not_cross_opaque_barrier() -> None:
    document = Document.create(4, 1)
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[0, 1] = (0, 0, 0, 255)
    document.active_layer.pixels.replace_tile(0, 0, tile)
    request = FillRequest(
        snapshot=DocumentSnapshot.capture(document),
        layer_id=document.active_layer_id,
        query=RegionQuery((0, 0)),
        color=(255, 0, 0, 255),
        edge_expansion=2,
    )

    result = calculate_fill(request, CancellationToken(), lambda *_args: None)

    after = result.updates[TileCoord(0, 0)]
    assert tuple(after[0, 0]) == (255, 0, 0, 255)
    assert tuple(after[0, 1]) == (0, 0, 0, 255)
    assert tuple(after[0, 2]) == (0, 0, 0, 0)
