from __future__ import annotations

from uuid import uuid4

import numpy as np

from easypicture.domain import Size, SpriteFrameContentMode
from easypicture.imaging import TilePreviewMode, TileTransitionMode
from easypicture.jobs import CancellationToken, JobHandle, JobOutcome, JobStatus
from easypicture.pixels import Rect
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from easypicture.ui.settings import TileWorkspaceSettings
from easypicture.ui.tile_workspace_panel import TileWorkspacePanel
from easypicture.workspace import EditingSurfaceRef, WorkspaceController
from tests.fakes import FakeDialogService, make_settings


def test_tile_panel_only_shows_relevant_rows_and_preserves_values(qtbot):
    panel = TileWorkspacePanel(TileWorkspaceSettings(offset_x=7, boundary_percent=65))
    qtbot.addWidget(panel)
    panel.set_frames(uuid4(), "A", uuid4(), "B")
    panel.show()
    assert [panel.mode.itemText(i) for i in range(panel.mode.count())] == [
        "平铺",
        "相邻对比",
        "过渡预览",
    ]

    def assert_row(field, visible):
        assert field.isVisible() == visible
        label = panel._form.labelForField(field)
        if label is not None:
            assert label.isVisible() == visible

    for mode in (TilePreviewMode.OFFSET, TilePreviewMode.PAIR, TilePreviewMode.TRANSITION):
        panel.mode.setCurrentIndex(panel.mode.findData(mode))
        tiling = mode is TilePreviewMode.OFFSET
        transition = mode is TilePreviewMode.TRANSITION
        for field in (panel.repeat_scale, panel.offset_x, panel.offset_y, panel._offset_actions):
            assert_row(field, tiling)
        for field in (panel.tile_b, panel.pick_b):
            assert_row(field, not tiling)
        for field in (panel.transition_mode, panel.boundary, panel.transition_width):
            assert_row(field, transition)
        assert panel.generate.isVisible() == transition

    panel.transition_mode.setCurrentIndex(
        panel.transition_mode.findData(TileTransitionMode.HARD_CUT)
    )
    assert_row(panel.transition_width, False)
    assert_row(panel.boundary, True)
    panel.mode.setCurrentIndex(panel.mode.findData(TilePreviewMode.OFFSET))
    assert panel.offset_x.value() == 7
    assert panel.boundary.value() == 65


def test_tiling_does_not_require_hidden_tile_b(qtbot, tmp_path):
    window, controller = _window_with_tiles(qtbot, tmp_path)
    project = controller.project_document
    first, second = project.sprite_workspace.frames
    window._open_tile_workspace(first.id)
    window._start_tile_pick("b")
    window._apply_tile_pick(second.id)
    assert window._tile_cached_key[2:4] == (first.id, first.id)
    assert controller.commands.delete_sprite_frame(second.id)
    qtbot.waitUntil(lambda: window._tile_selection() == (first.id, None))
    assert controller.document.active_layer.pixels.allocated_tile_count > 0
    assert window._tile_cached_key[2:4] == (first.id, first.id)


def _window_with_tiles(qtbot, tmp_path, cell=32):
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(cell * 2, cell)
    controller = WorkspaceController(settings, dialogs, recovery_enabled=False)
    window = MainWindow(controller, settings)
    qtbot.addWidget(
        window,
        before_close_func=lambda _widget: [
            session.document.mark_clean() for session in controller.sessions
        ],
    )
    assert controller.request_new_document(window)
    project = controller.project_document
    assert project is not None
    if cell != 32:
        assert controller.commands.set_sprite_cell_size(Size(cell, cell), clear_frames=False)
    first_pixels = np.zeros((cell, cell, 4), dtype=np.uint8)
    first_pixels[...] = [255, 0, 0, 255]
    second_pixels = np.zeros((cell, cell, 4), dtype=np.uint8)
    second_pixels[...] = [0, 0, 255, 255]
    project.active_layer.pixels.write_region(Rect(0, 0, cell, cell), first_pixels)
    project.active_layer.pixels.write_region(Rect(cell, 0, cell, cell), second_pixels)
    assert controller.commands.create_sprite_frame(Rect(0, 0, cell, cell))
    assert controller.commands.create_sprite_frame(Rect(cell, 0, cell, cell))
    return window, controller


def test_tile_surface_is_unique_read_only_and_preserves_zoom(qtbot, tmp_path) -> None:
    window, controller = _window_with_tiles(qtbot, tmp_path)
    project = controller.project_document
    assert project is not None
    first = project.sprite_workspace.frames[0]
    window._open_tile_workspace(first.id)

    session = controller.active_session
    assert session is not None
    assert session.active_surface == EditingSurfaceRef("tile")
    assert session.active_surface_is_read_only
    assert session.open_surfaces.count(EditingSurfaceRef("tile")) == 1
    assert controller.document is not None
    assert controller.document.size.width == 96
    assert controller.document.size.height == 96
    assert not controller.commands.create_layer()

    window.canvas_view.set_zoom(3.0)
    window.tile_properties.offset_x.setValue(1)
    assert window.canvas_view.zoom == 3.0
    project.mark_clean()


def test_visible_tile_surface_with_enabled_sprite_grid_does_not_require_workspace(qtbot, tmp_path):
    window, controller = _window_with_tiles(qtbot, tmp_path)
    window.resize(1000, 700)
    window.show()
    window._open_tile_workspace(controller.project_document.sprite_workspace.frames[0].id)
    window.canvas_view.set_sprite_grid_visibility(pixel_grid=True, cell_grid=True)
    window.canvas_view.set_zoom(8)
    window.canvas_view.viewport().repaint()
    assert not window.grab().isNull()


def test_deleted_tile_clears_pixels_and_picker_cancels_on_surface_switch(qtbot, tmp_path):
    window, controller = _window_with_tiles(qtbot, tmp_path)
    project = controller.project_document
    first = project.sprite_workspace.frames[0]
    window._open_tile_workspace(first.id)
    window.canvas_view.set_zoom(2.5)
    assert controller.commands.delete_sprite_frame(first.id)
    qtbot.waitUntil(lambda: window._tile_selection() == (None, None))
    assert controller.document.active_layer.pixels.allocated_tile_count == 0
    assert not window._tile_heatmap_segments
    assert window.canvas_view.zoom == 2.5
    window._start_tile_pick("a")
    controller.set_active_surface(EditingSurfaceRef("source"))
    assert window._tile_pick_target is None


def test_parameter_changes_keep_one_job_and_only_latest_pending_request(
    qtbot, tmp_path, monkeypatch
):
    window, controller = _window_with_tiles(qtbot, tmp_path, cell=192)
    project = controller.project_document
    calls = []

    def submit(operation):
        handle = JobHandle(uuid4(), CancellationToken())
        calls.append((operation, handle))
        return handle

    monkeypatch.setattr(controller.preview_jobs, "submit", submit)
    window._open_tile_workspace(project.sprite_workspace.frames[0].id)
    assert len(calls) == 1
    blank = controller.document
    stale = calls[0][0](CancellationToken(), lambda *_args: None)
    for value in range(1, 6):
        window.tile_properties.tolerance.setValue(value)
    assert len(calls) == 1
    assert calls[0][1].token.cancelled
    assert window._tile_scheduler.pending_count == 1
    # Deliberately deliver success despite cancellation: it must still be rejected.
    calls[0][1].finished.emit(JobOutcome(calls[0][1].job_id, JobStatus.SUCCESS, stale))
    assert controller.document is blank
    assert len(calls) == 2
    current = calls[1][0](CancellationToken(), lambda *_args: None)
    calls[1][1].finished.emit(JobOutcome(calls[1][1].job_id, JobStatus.SUCCESS, current))
    assert controller.document is current.document
    assert window._tile_cached_key[-1].tolerance == 5
    assert window._tile_job is None


def test_background_result_cannot_switch_project_or_reopen_closed_tab(qtbot, tmp_path, monkeypatch):
    window, controller = _window_with_tiles(qtbot, tmp_path, cell=192)
    project = controller.project_document
    calls = []

    def submit(operation):
        handle = JobHandle(uuid4(), CancellationToken())
        calls.append((operation, handle))
        return handle

    monkeypatch.setattr(controller.preview_jobs, "submit", submit)
    window._open_tile_workspace(project.sprite_workspace.frames[0].id)
    operation, handle = calls[0]
    result = operation(CancellationToken(), lambda *_args: None)
    controller.close_tile_surface()
    handle.finished.emit(JobOutcome(handle.job_id, JobStatus.SUCCESS, result))
    assert controller.active_session.active_surface.kind == "source"
    assert EditingSurfaceRef("tile") not in controller.active_session.open_surfaces
    window._open_tile_workspace(project.sprite_workspace.frames[0].id)
    operation, handle = calls[-1]
    result = operation(CancellationToken(), lambda *_args: None)
    assert controller.request_new_document(window)
    other_project = controller.project_document
    handle.finished.emit(JobOutcome(handle.job_id, JobStatus.SUCCESS, result))
    assert controller.project_document is other_project
    assert controller.document is other_project
    assert EditingSurfaceRef("tile") not in controller.active_session.open_surfaces


def test_tile_source_changes_refresh_without_pan_drift_and_ignore_metadata(qtbot, tmp_path):
    window, controller = _window_with_tiles(qtbot, tmp_path)
    project = controller.project_document
    first = project.sprite_workspace.frames[0]
    window._open_tile_workspace(first.id)
    old_preview = controller.document
    window.canvas_view.set_zoom(4)
    before_scroll = (
        window.canvas_view.horizontalScrollBar().value(),
        window.canvas_view.verticalScrollBar().value(),
    )
    assert controller.commands.update_sprite_frame(first.id, name="unchanged pixels")
    window._refresh_tile_workspace()
    assert controller.document is old_preview
    pixels = project.active_layer.pixels.read_tile(0, 0).copy()
    pixels[:32, :32] = [0, 255, 0, 255]
    from easypicture.pixels import TileCoord

    # Simulate a source update from the parent command service, not an edit on
    # the intentionally read-only Tile surface.
    assert controller.active_session.controller.commands.apply_pixel_tiles(
        project.active_layer_id, {TileCoord(0, 0): pixels}, "change source"
    )
    qtbot.waitUntil(lambda: controller.document is not old_preview)
    assert controller.document.active_layer.pixels.read_tile(0, 0)[0, 0].tolist() == [
        0,
        255,
        0,
        255,
    ]
    assert window.canvas_view.zoom == 4
    assert before_scroll == (
        window.canvas_view.horizontalScrollBar().value(),
        window.canvas_view.verticalScrollBar().value(),
    )


def test_tile_picker_pair_heatmap_and_transition_generation(qtbot, tmp_path) -> None:
    window, controller = _window_with_tiles(qtbot, tmp_path)
    project = controller.project_document
    assert project is not None
    first, second = project.sprite_workspace.frames
    window._open_tile_workspace(first.id)
    window._start_tile_pick("b")
    window.sprite_frame_panel.frame_clicked.emit(second.id)
    assert window._tile_selection() == (first.id, second.id)

    pair_index = window.tile_properties.mode.findData(TilePreviewMode.PAIR)
    window.tile_properties.mode.setCurrentIndex(pair_index)
    assert window.tile_properties.maximum_difference.text() == "255"
    assert window._tile_heatmap_segments

    transition_index = window.tile_properties.mode.findData(TilePreviewMode.TRANSITION)
    dither_index = window.tile_properties.transition_mode.findData(
        TileTransitionMode.ORDERED_DITHER
    )
    window.tile_properties.mode.setCurrentIndex(transition_index)
    window.tile_properties.transition_mode.setCurrentIndex(dither_index)
    before = len(project.sprite_workspace.frames)
    window._generate_tile_transition_frame()
    assert len(project.sprite_workspace.frames) == before + 1
    generated = project.sprite_workspace.frames[-1]
    assert generated.content_mode is SpriteFrameContentMode.DETACHED
    assert generated.id in project.sprite_workspace.ungrouped_frame_ids
    assert controller.active_session is not None
    assert controller.active_session.active_surface == EditingSurfaceRef("frame", generated.id)

    controller.commands.undo_stack.undo()
    assert len(project.sprite_workspace.frames) == before
    project.mark_clean()
