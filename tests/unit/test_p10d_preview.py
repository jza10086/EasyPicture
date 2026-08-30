from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QSettings

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, Size
from easypicture.imaging import (
    TileDirection,
    TilePreviewMode,
    TileTransitionMode,
    TileTransitionRequest,
    compose_tile_pair,
    compose_transition_preview,
    generate_tile_transition,
    offset_tile,
    repeat_tile,
)
from easypicture.jobs import CancellationToken, JobCancelled
from easypicture.persistence import (
    DocumentSnapshot,
    SpriteFrameExportRequest,
    export_sprite_frame_png,
    load_project,
    save_project,
)
from easypicture.pixels import Rect, TileCoord, TilePixelStore
from easypicture.ui.settings import TileWorkspaceSettings
from easypicture.ui.tile_preview import (
    TilePreviewRequest,
    _preview_document,
    calculate_tile_preview,
    tile_source_signature,
)
from tests.fakes import make_settings


@pytest.mark.parametrize("saved_mode", ["repeat", "offset", "unknown"])
def test_old_tiling_preferences_load_as_single_offset_capable_mode(tmp_path, saved_mode):
    path = str(tmp_path / "mode.ini")
    raw = QSettings(path, QSettings.Format.IniFormat)
    raw.setValue("tile_workspace/preview_mode", saved_mode)
    raw.setValue("tile_workspace/offset_x", 7)
    raw.sync()
    settings = make_settings(path).tile_workspace_settings()
    assert settings.preview_mode is TilePreviewMode.OFFSET
    assert settings.offset_x == 7


def _progress(_done, _total, _label):
    pass


def _request(settings=None):
    document = Document.create(18, 7)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(Size(9, 7), clear_frames=False)
    pixels = np.random.default_rng(7).integers(0, 256, (7, 18, 4), dtype=np.uint8)
    pixels[..., 3] = 255
    document.active_layer.pixels.write_region(Rect(0, 0, 18, 7), pixels)
    assert service.create_sprite_frame(Rect(0, 0, 9, 7))
    assert service.create_sprite_frame(Rect(9, 0, 9, 7))
    first, second = document.sprite_workspace.frames
    request = TilePreviewRequest(
        DocumentSnapshot.capture(document), first.id, second.id,
        settings or TileWorkspaceSettings(),
    )
    return document, service, request


@pytest.mark.parametrize("direction", tuple(TileDirection))
@pytest.mark.parametrize("mode", tuple(TilePreviewMode))
def test_prepared_preview_matches_exact_layout_without_changing_source(direction, mode):
    settings = TileWorkspaceSettings(
        preview_mode=mode, direction=direction, offset_x=-2, offset_y=3, transition_width=5
    )
    document, service, request = _request(settings)
    before = (document.revision, service.undo_stack.count())
    result = calculate_tile_preview(request, CancellationToken(), _progress)
    first, second = result.sources
    if mode is TilePreviewMode.OFFSET:
        expected = repeat_tile(offset_tile(first, -2, 3), 3)
    elif mode is TilePreviewMode.PAIR:
        expected = compose_tile_pair(first, second, direction)
    else:
        transition = generate_tile_transition(TileTransitionRequest(
            first, second, direction, TileTransitionMode.ORDERED_DITHER, 50, 5,
        ))
        np.testing.assert_array_equal(result.transition, transition)
        expected = compose_transition_preview(first, transition, second, direction)
    rendered = result.document.active_layer.pixels.read_region(
        Rect(0, 0, result.document.width, result.document.height)
    )
    np.testing.assert_array_equal(rendered, expected)
    assert result.segments
    assert before == (document.revision, service.undo_stack.count())


def test_preview_shares_repeated_tiles_and_keeps_real_document_size_limit():
    pixels = np.full((256, 2048, 4), [21, 34, 56, 255], dtype=np.uint8)
    preview = _preview_document((pixels,), ((0,) * 5,), CancellationToken(), _progress)
    assert preview.width == 10240
    store = preview.active_layer.pixels
    assert np.shares_memory(store.read_tile(0, 0), store.read_tile(8, 0))
    np.testing.assert_array_equal(store.read_tile(39, 0)[0, 0], pixels[0, 0])
    with pytest.raises(ValueError):
        Document.create(10240, 256)
    with pytest.raises(ValueError):
        Document.create_sprite_frame(Size(10240, 256))


def test_readonly_tile_store_preserves_copy_on_write():
    pixels = np.full((256, 256, 4), 127, dtype=np.uint8)
    with pytest.raises(ValueError):
        TilePixelStore.from_readonly_tiles({TileCoord(0, 0): pixels})
    pixels.flags.writeable = False
    store = TilePixelStore.from_readonly_tiles({TileCoord(0, 0): pixels, TileCoord(1, 0): pixels})
    clone = store.clone_shared()
    store.writable_tile(0, 0)[0, 0] = 0
    assert clone.read_tile(0, 0)[0, 0, 0] == 127
    assert store.read_tile(1, 0)[0, 0, 0] == 127


def test_preview_and_transition_cancel_at_bounded_work_boundaries():
    _, _, request = _request()
    token = CancellationToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        calculate_tile_preview(request, token, _progress)
    pixels = np.zeros((256, 256, 4), dtype=np.uint8)
    with pytest.raises(JobCancelled):
        generate_tile_transition(TileTransitionRequest(
            pixels, pixels, TileDirection.TOP_TO_BOTTOM,
            TileTransitionMode.ORDERED_DITHER, 50, 256,
        ), check_cancelled=token.raise_if_cancelled)


def test_transition_budget_failure_is_atomic():
    document = Document.create(4, 4)
    service = DocumentCommandService(undo_budget_bytes=1)
    service.attach_document(document)
    before = (document.revision, document.modified, service.undo_stack.count())
    assert service.create_detached_sprite_frame(
        np.full((4, 4, 4), 255, dtype=np.uint8), name="transition_001",
        source_region=Rect(0, 0, 4, 4),
    ) is None
    assert not document.embedded_documents
    assert not document.sprite_workspace.frames
    assert before == (document.revision, document.modified, service.undo_stack.count())


def test_transition_roundtrip_and_export_exclude_heatmap(tmp_path):
    document, service, request = _request(
        TileWorkspaceSettings(preview_mode=TilePreviewMode.TRANSITION)
    )
    result = calculate_tile_preview(request, CancellationToken(), _progress)
    assert result.transition is not None
    first = document.sprite_workspace.frames[0]
    frame_id = service.create_detached_sprite_frame(
        result.transition, name="transition_001", source_region=first.region,
        pivot=first.pivot, duration_ticks=3,
    )
    assert frame_id is not None
    path = tmp_path / "tile.limg"
    save_project(DocumentSnapshot.capture(document), path, CancellationToken(), _progress)
    loaded = load_project(path, CancellationToken(), _progress).snapshot.to_document()
    assert loaded.sprite_workspace.frame_by_id(frame_id).duration_ticks == 3
    revision = loaded.revision
    image_path = tmp_path / "transition.png"
    export_sprite_frame_png(SpriteFrameExportRequest(
        DocumentSnapshot.capture(loaded), frame_id, image_path,
    ), CancellationToken(), _progress)
    with Image.open(image_path) as image:
        np.testing.assert_array_equal(np.array(image.convert("RGBA")), result.transition)
        assert image.info["icc_profile"]
    assert loaded.revision == revision


def test_settings_roundtrip_and_source_signature_ignore_frame_names(tmp_path):
    app_settings = make_settings(str(tmp_path / "tile-settings.ini"))
    settings = TileWorkspaceSettings(
        preview_mode=TilePreviewMode.OFFSET, repeat_scale=5,
        direction=TileDirection.BOTTOM_TO_TOP, tolerance=28, include_alpha=False,
        heatmap_visible=False, offset_x=-30, offset_y=22,
        transition_mode=TileTransitionMode.HARD_CUT, boundary_percent=70, transition_width=19,
    )
    app_settings.set_tile_workspace_settings(settings)
    assert app_settings.tile_workspace_settings() == settings
    document, service, request = _request()
    before = tile_source_signature(document, (request.first_id, request.second_id))
    assert service.update_sprite_frame(request.first_id, name="renamed")
    assert before == tile_source_signature(document, (request.first_id, request.second_id))
    document.active_layer.pixels.writable_tile(0, 0)[0, 0] = 0
    assert before != tile_source_signature(document, (request.first_id, request.second_id))
    assert replace(settings, offset_x=0) != settings
