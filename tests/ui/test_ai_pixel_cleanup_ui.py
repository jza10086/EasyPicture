from dataclasses import replace
from uuid import uuid4

import numpy as np
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, RasterLayer
from easypicture.imaging.ai_pixel_cleanup import (
    AiPixelCleanupParameters,
    AiPixelCleanupRequest,
    PixelCleanupOutput,
    calculate_ai_pixel_cleanup,
)
from easypicture.jobs import CancellationToken, JobHandle, JobOutcome, JobStatus
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import Rect, TileCoord
from easypicture.render import CanvasRenderCoordinator
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from easypicture.workspace import WorkspaceController
from tests.fakes import FakeDialogService, make_settings
from tests.unit.test_canvas_render_coordinator import _paint_frame


def _window(qtbot, tmp_path):
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(64, 48)
    controller = WorkspaceController(settings, dialogs, recovery_enabled=False)
    window = MainWindow(controller, settings)
    qtbot.addWidget(
        window,
        before_close_func=lambda _widget: [
            session.document.mark_clean() for session in controller.sessions
        ],
    )
    window.resize(1000, 700)
    window.show()
    window.new_action.trigger()
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[:48, :64] = (40, 80, 120, 200)
    doc = controller.document
    doc.replace_layer_tiles(doc.active_layer_id, {TileCoord(0, 0): tile})
    return controller, window


def black_palette(window):
    panel = window.ai_pixel_cleanup_panel
    panel._presets.save("纯黑", ((0, 0, 0),))
    panel._refresh_presets()
    panel.preset.setCurrentIndex(panel.preset.findData("纯黑"))


def result_for(doc, **kwargs):
    return calculate_ai_pixel_cleanup(
        AiPixelCleanupRequest(
            DocumentSnapshot.capture(doc), doc.active_layer_id, AiPixelCleanupParameters(**kwargs)
        ),
        CancellationToken(),
        lambda *_args: None,
    )


def test_apply_new_layer_hides_source_one_undo_and_restores_opacity(qtbot, tmp_path):
    controller, window = _window(qtbot, tmp_path)
    doc = controller.document
    source = doc.active_layer
    source.opacity = 0.65
    before = source.pixels.read_tile(0, 0).copy()
    revision = doc.revision
    window.canvas_view.set_zoom(3)
    window.ai_pixel_cleanup_action.trigger()
    assert window.properties_stack.currentWidget() is window.ai_pixel_cleanup_panel
    black_palette(window)
    session = window.color_adjustment_controller
    qtbot.waitUntil(lambda: session.has_exact_result)
    assert doc.revision == revision and source.visible
    assert len(window.canvas_view.render_coordinator.display_raster_children(doc.nodes[0])) == 1
    assert window.canvas_view.zoom == 3
    window.ai_pixel_cleanup_panel.apply_requested.emit()
    qtbot.waitUntil(lambda: not session.active)
    assert len(doc.layers) == 2 and not source.visible
    created = doc.active_layer
    assert created.name == f"{source.name} 像素整理" and created.opacity == source.opacity
    np.testing.assert_array_equal(source.pixels.read_tile(0, 0), before)
    assert controller.commands.undo_stack.activeStack().count() == 1
    controller.commands.undo_stack.undo()
    assert doc.active_layer_id == source.id and source.visible and len(doc.layers) == 1
    controller.commands.undo_stack.redo()
    assert doc.active_layer_id == created.id and not source.visible


def test_lowres_project_does_not_change_source_or_undo(qtbot, tmp_path):
    controller, window = _window(qtbot, tmp_path)
    source = controller.document
    source.mark_clean()
    revision = source.revision
    window.ai_pixel_cleanup_action.trigger()
    panel = window.ai_pixel_cleanup_panel
    panel.grid_enabled.setChecked(True)
    panel.output.setCurrentIndex(panel.output.findData("project"))
    black_palette(window)
    qtbot.waitUntil(lambda: window.color_adjustment_controller.has_exact_result)
    panel.apply_requested.emit()
    qtbot.waitUntil(lambda: len(window.controller.sessions) == 2)
    result = window.controller.document
    assert (result.width, result.height) == (8, 6)
    assert result.modified and result.project_path is None
    assert result.display_name.endswith("像素整理")
    assert not result.sprite_workspace.frames and not result.embedded_documents
    assert source.revision == revision and not source.modified and source.active_layer.visible
    assert controller.commands.undo_stack.activeStack().count() == 0
    assert window._ai_grid_overlay is None


def test_cancel_and_layer_change_clear_preview_without_source_mutation(qtbot, tmp_path):
    controller, window = _window(qtbot, tmp_path)
    doc = controller.document
    before = doc.active_layer.pixels.read_tile(0, 0).copy()
    window.ai_pixel_cleanup_action.trigger()
    black_palette(window)
    qtbot.waitUntil(lambda: window.color_adjustment_controller.has_exact_result)
    window.ai_pixel_cleanup_panel.cancel_requested.emit()
    assert not window.color_adjustment_controller.active
    assert window._ai_grid_overlay is None and len(doc.layers) == 1
    np.testing.assert_array_equal(doc.active_layer.pixels.read_tile(0, 0), before)
    window.ai_pixel_cleanup_action.trigger()
    doc.set_layer_locked(doc.active_layer_id, True)
    assert not window.color_adjustment_controller.active


def test_panel_dynamic_fields_locks_and_restart_settings(qtbot, tmp_path):
    controller, window = _window(qtbot, tmp_path)
    window.ai_pixel_cleanup_action.trigger()
    panel = window.ai_pixel_cleanup_panel
    assert not panel.grid_fields.isVisible()
    panel.grid_enabled.setChecked(True)
    assert panel.grid_fields.isVisible() and panel.alpha.currentData() == "coverage"
    for value in (4, 6, 8, 10):
        panel.block_width.setValue(value)
        assert panel.block_height.value() == value
        assert window.color_adjustment_controller.parameters.block_width == value
    panel.output.setCurrentIndex(panel.output.findData("project"))
    panel.alpha.setCurrentIndex(panel.alpha.findData("preserve"))
    assert panel.alpha.currentData() == "coverage"
    panel.use_sampled_color((1, 2, 3, 255))
    assert (1, 2, 3) in panel.parameters().locked_colors
    window.color_adjustment_controller.cancel()
    window.ai_pixel_cleanup_action.trigger()
    assert panel.parameters().locked_colors == ()
    assert (
        panel.block_width.value() == 10 and panel.parameters().output is PixelCleanupOutput.PROJECT
    )


def test_invalid_lock_count_cannot_apply_previous_result_via_enter(qtbot, tmp_path):
    controller, window = _window(qtbot, tmp_path)
    window.ai_pixel_cleanup_action.trigger()
    panel = window.ai_pixel_cleanup_panel
    panel.use_sampled_color((1, 2, 3, 255))
    panel.use_sampled_color((4, 5, 6, 255))
    qtbot.waitUntil(lambda: window.color_adjustment_controller.has_exact_result)
    panel.target.setValue(1)
    assert not panel.apply_button.isEnabled()
    window._apply_color_adjustment()
    assert window.color_adjustment_controller.active
    assert len(controller.document.layers) == 1
    window.color_adjustment_controller.cancel()


def test_numeric_enter_ends_input_not_session(qtbot, tmp_path):
    _controller, window = _window(qtbot, tmp_path)
    window.ai_pixel_cleanup_action.trigger()
    panel = window.ai_pixel_cleanup_panel
    panel.target.setFocus()
    qtbot.mouseClick(panel.target, Qt.MouseButton.LeftButton)
    qtbot.keyClick(panel.target.lineEdit(), Qt.Key.Key_Return)
    assert window.color_adjustment_controller.active
    window.color_adjustment_controller.cancel()


def test_engine_commit_budget_stale_and_transparency_lock(qapp):
    doc = Document.create(16, 16)
    doc.active_layer.pixels.write_region(Rect(0, 0, 16, 16), np.full((16, 16, 4), 150, np.uint8))
    doc.active_layer.alpha_locked = True
    result = result_for(doc, fixed_palette=((0, 0, 0),))
    commands = DocumentCommandService()
    commands.attach_document(doc)
    assert not commands.apply_ai_pixel_cleanup(replace(result, source_revision=doc.revision + 1))
    assert len(doc.layers) == 1
    assert commands.apply_ai_pixel_cleanup(result)
    commands.undo_stack.undo()
    assert len(doc.layers) == 1 and doc.active_layer.visible


def test_replacement_preview_matches_committed_with_occlusion(qapp):
    doc = Document.create(64, 48)
    tile = np.full((256, 256, 4), (60, 90, 150, 170), np.uint8)
    doc.replace_layer_tiles(doc.active_layer_id, {TileCoord(0, 0): tile})
    source = doc.active_layer
    source.opacity = 0.7
    doc.nodes[0].opacity = 0.6
    upper = RasterLayer.create_blank("遮挡", doc.size)
    cover = np.zeros_like(tile)
    cover[:18, :18] = (200, 10, 30, 130)
    upper.pixels.replace_tile(0, 0, cover)
    doc.insert_layer(upper, 1)
    doc.set_active_layer(source.id)
    result = result_for(doc, fixed_palette=((0, 0, 0),))
    renderer = CanvasRenderCoordinator()
    renderer.set_document(doc)
    renderer.replace_raster_layer_preview(source.id, result.updates, replace_source=True)
    before = _paint_frame(renderer, 64, 48)
    commands = DocumentCommandService()
    commands.attach_document(doc)
    assert commands.apply_ai_pixel_cleanup(result)
    after = _paint_frame(renderer, 64, 48)
    assert before == after


def test_detached_surface_commit_updates_parent_and_switch_cancels(qtbot, tmp_path):
    controller, window = _window(qtbot, tmp_path)
    root = controller.project_document
    source = root.active_layer.pixels.read_tile(0, 0).copy()
    assert controller.commands.create_sprite_frame(Rect(0, 0, 32, 32))
    frame = root.sprite_workspace.frames[0]
    assert controller.commands.detach_sprite_frame(frame.id)
    assert controller.open_frame_surface(frame.id)
    child = controller.document
    revision = root.revision
    window.ai_pixel_cleanup_action.trigger()
    black_palette(window)
    window.ai_pixel_cleanup_panel.apply_requested.emit()
    qtbot.waitUntil(lambda: not window.color_adjustment_controller.active)
    assert len(child.layers) == 2 and root.revision > revision
    np.testing.assert_array_equal(root.active_layer.pixels.read_tile(0, 0), source)
    controller.commands.undo_stack.undo()
    assert len(child.layers) == 1 and child.active_layer.visible
    window.ai_pixel_cleanup_action.trigger()
    controller.request_new_document(window)
    assert not window.color_adjustment_controller.active and window._ai_grid_overlay is None


def test_latest_generation_preserves_complete_previous_result(qtbot, tmp_path, monkeypatch):
    controller, window = _window(qtbot, tmp_path)
    doc = controller.document
    handles = []

    def submit(_task):
        handle = JobHandle(uuid4(), CancellationToken())
        handles.append(handle)
        return handle

    monkeypatch.setattr(controller.jobs, "submit", submit)
    window.ai_pixel_cleanup_action.trigger()
    session = window.color_adjustment_controller
    session._debounce.stop()
    session._submit_latest()
    handles[0].finished.emit(
        JobOutcome(handles[0].job_id, JobStatus.SUCCESS, value=result_for(doc))
    )
    renderer = window.canvas_view.render_coordinator
    old = renderer.display_raster_children(doc.nodes[0])[0]
    window.ai_pixel_cleanup_panel.target.setValue(16)
    session._debounce.stop()
    session._submit_latest()
    window.ai_pixel_cleanup_panel.target.setValue(8)
    session._debounce.stop()
    handles[1].finished.emit(
        JobOutcome(handles[1].job_id, JobStatus.SUCCESS, value=result_for(doc, target_colors=16))
    )
    assert len(handles) == 3 and renderer.display_raster_children(doc.nodes[0])[0] is old
    handles[2].finished.emit(
        JobOutcome(handles[2].job_id, JobStatus.SUCCESS, value=result_for(doc, target_colors=8))
    )
    assert renderer.display_raster_children(doc.nodes[0])[0] is not old
    doc.record_change()
    assert not session.active
    assert renderer.display_raster_children(doc.nodes[0])[0] is doc.active_layer


def test_continuous_grid_drag_survives_feedback_and_right_click_restores(qtbot, tmp_path):
    _controller, window = _window(qtbot, tmp_path)
    window.ai_pixel_cleanup_action.trigger()
    panel = window.ai_pixel_cleanup_panel
    panel.grid_enabled.setChecked(True)
    field = panel.block_width
    start = field.rect().center()
    qtbot.mousePress(field, Qt.MouseButton.LeftButton, pos=start)
    values = []
    for dx in (25, 50, 75):
        point = start + type(start)(dx, 0)
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(point),
            QPointF(field.mapToGlobal(point)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(field, event)
        values.append(field.value())
        assert panel.block_height.value() == field.value()
    assert values[0] < values[1] < values[2]
    qtbot.mouseClick(field, Qt.MouseButton.RightButton)
    assert field.value() == 8 and panel.block_height.value() == 8
    window.color_adjustment_controller.cancel()


def test_cleanup_save_frame_atlas_excludes_preview_and_preserves_schema(qapp, tmp_path):
    import json
    import zipfile

    from PIL import Image

    from easypicture.persistence import (
        SpriteAtlasExportRequest,
        export_sprite_atlas,
        load_project,
        save_project,
    )
    from easypicture.persistence.sprite_io import SpriteFrameExportRequest, export_sprite_frame_png

    doc = Document.create(32, 32)
    doc.active_layer.pixels.write_region(
        Rect(0, 0, 32, 32), np.full((32, 32, 4), (90, 130, 170, 190), np.uint8)
    )
    commands = DocumentCommandService()
    commands.attach_document(doc)
    commands.create_sprite_frame(Rect(0, 0, 32, 32))
    frame = doc.sprite_workspace.frames[0]
    result = result_for(doc, fixed_palette=((10, 20, 30),))
    renderer = CanvasRenderCoordinator()
    renderer.set_document(doc)
    renderer.replace_raster_layer_preview(doc.active_layer_id, result.updates, replace_source=True)

    def export_frame(name):
        export_sprite_frame_png(
            SpriteFrameExportRequest(DocumentSnapshot.capture(doc), frame.id, tmp_path / name),
            CancellationToken(),
            lambda *_args: None,
        )

    export_frame("before.png")
    with Image.open(tmp_path / "before.png") as image:
        assert image.getpixel((0, 0)) == (90, 130, 170, 190)
    assert commands.apply_ai_pixel_cleanup(result)
    before = doc.revision, doc.modified, commands.undo_stack.count()
    snapshot = DocumentSnapshot.capture(doc)
    save_project(snapshot, tmp_path / "result.limg", CancellationToken(), lambda *_args: None)
    restored = load_project(
        tmp_path / "result.limg", CancellationToken(), lambda *_args: None
    ).snapshot.to_document()
    assert len(restored.layers) == 2 and not restored.layers[0].visible
    np.testing.assert_array_equal(
        restored.layers[0].pixels.read_tile(0, 0)[0, 0], (90, 130, 170, 190)
    )
    with zipfile.ZipFile(tmp_path / "result.limg") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == 4 and "ai_pixel_cleanup" not in manifest
    export_frame("after.png")
    export_sprite_atlas(
        SpriteAtlasExportRequest(snapshot, tmp_path / "atlas.png"),
        CancellationToken(),
        lambda *_args: None,
    )
    for name in ("after.png", "atlas.png"):
        with Image.open(tmp_path / name) as image:
            assert image.getpixel((0, 0)) == (10, 20, 30, 190)
            assert image.info.get("icc_profile")
    assert before == (doc.revision, doc.modified, commands.undo_stack.count())
