from dataclasses import replace
from uuid import uuid4

import numpy as np
from PySide6.QtCore import Qt

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, RasterLayer
from easypicture.imaging import ColorAdjustmentKind
from easypicture.imaging.color_adjustments import PaletteMapAdjustment
from easypicture.imaging.pixel_outline import (
    PixelOutlineParameters,
    PixelOutlineRequest,
    calculate_pixel_outline,
)
from easypicture.jobs import CancellationToken, JobHandle, JobOutcome, JobStatus
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import Rect, TileCoord
from easypicture.render import CanvasRenderCoordinator
from easypicture.ui.palette_presets import PalettePresetStore
from easypicture.workspace import EditingSurfaceRef
from tests.fakes import make_settings
from tests.ui.test_p7a_color_adjustment_ui import _window
from tests.ui.test_p10d_tile_workspace_ui import _window_with_tiles
from tests.unit.test_canvas_render_coordinator import _paint_frame, _pixel


def _result(doc, **kwargs):
    return calculate_pixel_outline(
        PixelOutlineRequest(
            DocumentSnapshot.capture(doc), doc.active_layer_id, PixelOutlineParameters(**kwargs)
        ),
        CancellationToken(),
        lambda *a: None,
    )


def _shape(doc):
    tile = np.zeros((256, 256, 4), np.uint8)
    tile[10:20, 10:20] = (80, 40, 20, 255)
    doc.replace_layer_tiles(doc.active_layer_id, {TileCoord(0, 0): tile})


def test_palette_extraction_preview_and_presets_are_not_content(qtbot, tmp_path):
    controller, window = _window(qtbot, tmp_path)
    doc = controller.document
    before_revision = doc.revision
    window.color_adjustment_actions[ColorAdjustmentKind.PALETTE_MAP].trigger()
    window.color_adjustment_controller.extract_palette(16)
    panel = window.color_adjustment_panel
    qtbot.waitUntil(lambda: panel.palette_source_count.text().endswith("：1"))
    qtbot.waitUntil(lambda: window.color_adjustment_controller.has_exact_result)
    assert panel.parameters() == PaletteMapAdjustment(((40, 80, 120),))
    assert doc.revision == before_revision
    assert controller.commands.undo_stack.count() == 0
    store = PalettePresetStore(window.settings)
    assert store.save("角色", panel.parameters().colors) == "角色"
    assert store.save("角色", panel.parameters().colors) == "角色.1"
    assert store.save("角色.1", ((0, 0, 0),), replacing="角色") == "角色.1.1"
    fresh = PalettePresetStore(make_settings(str(tmp_path / "settings.ini")))
    assert len(fresh.palettes()) == 2
    fresh.delete("角色.1")
    assert tuple(fresh.palettes()) == ("角色.1.1",)
    panel.apply_requested.emit()
    qtbot.waitUntil(lambda: not window.color_adjustment_controller.active)
    assert controller.commands.undo_stack.count() == 0


def test_outline_preview_apply_undo_and_zoom(qtbot, tmp_path):
    controller, window = _window(qtbot, tmp_path)
    doc = controller.document
    _shape(doc)
    source = doc.active_layer
    source.alpha_locked = True
    before = source.pixels.read_tile(0, 0).copy()
    before_revision = doc.revision
    window.canvas_view.set_zoom(3)
    window.pixel_outline_action.trigger()
    panel, session = window.pixel_outline_panel, window.color_adjustment_controller
    assert window.properties_stack.currentWidget() is panel
    assert session.active
    panel.outline_width.setValue(3)
    qtbot.waitUntil(lambda: session.has_exact_result)
    assert window.canvas_view.zoom == 3
    assert len(doc.layers) == 1 and doc.revision == before_revision
    assert len(window.canvas_view.render_coordinator.display_raster_children(doc.nodes[0])) == 2
    panel.apply_requested.emit()
    qtbot.waitUntil(lambda: not session.active)
    assert len(doc.layers) == 2
    created = doc.active_layer
    assert created.name == f"{source.name} 描边"
    assert doc.nodes[0].children == [source, created]
    assert controller.commands.undo_stack.count() == 1
    np.testing.assert_array_equal(source.pixels.read_tile(0, 0), before)
    controller.commands.undo_stack.undo()
    assert doc.active_layer is source and len(doc.layers) == 1
    controller.commands.undo_stack.redo()
    assert doc.active_layer is created and len(doc.layers) == 2


def test_outline_stale_generation_retains_complete_previous_preview(qtbot, tmp_path, monkeypatch):
    controller, window = _window(qtbot, tmp_path)
    doc = controller.document
    _shape(doc)
    handles = []

    def submit(_task):
        handle = JobHandle(uuid4(), CancellationToken())
        handles.append(handle)
        return handle

    monkeypatch.setattr(controller.jobs, "submit", submit)
    window.pixel_outline_action.trigger()
    session = window.color_adjustment_controller
    session._debounce.stop()
    session._submit_latest()
    handles[0].finished.emit(JobOutcome(handles[0].job_id, JobStatus.SUCCESS, value=_result(doc)))
    coordinator = window.canvas_view.render_coordinator
    old = coordinator.display_raster_children(doc.nodes[0])[1]
    window.pixel_outline_panel.outline_width.setValue(2)
    session._debounce.stop()
    session._submit_latest()
    window.pixel_outline_panel.outline_width.setValue(3)
    assert coordinator.display_raster_children(doc.nodes[0])[1] is old
    session._debounce.stop()
    handles[1].finished.emit(
        JobOutcome(handles[1].job_id, JobStatus.SUCCESS, value=_result(doc, width=2))
    )
    assert len(handles) == 3
    assert coordinator.display_raster_children(doc.nodes[0])[1] is old
    handles[2].finished.emit(
        JobOutcome(handles[2].job_id, JobStatus.SUCCESS, value=_result(doc, width=3))
    )
    assert coordinator.display_raster_children(doc.nodes[0])[1] is not old
    doc.record_change()
    assert not session.active
    assert len(coordinator.display_raster_children(doc.nodes[0])) == 1


def test_outline_cancel_failure_and_apply_while_running(qtbot, tmp_path):
    controller, window = _window(qtbot, tmp_path)
    doc = controller.document
    _shape(doc)
    window.pixel_outline_action.trigger()
    panel = window.pixel_outline_panel
    for width in (2, 4, 8):
        panel.outline_width.setValue(width)
    panel.apply_requested.emit()
    assert not panel.outline_width.isEnabled()
    assert panel.cancel_button.isEnabled()
    qtbot.waitUntil(lambda: not window.color_adjustment_controller.active)
    assert controller.commands.undo_stack.count() == 1
    assert panel.outline_width.isEnabled()


def test_outline_command_budget_empty_stale_and_active_restore(qapp):
    doc = Document.create(40, 40)
    _shape(doc)
    service = DocumentCommandService(undo_budget_bytes=1)
    service.attach_document(doc)
    revision = doc.revision
    assert not service.apply_pixel_outline(_result(doc))
    assert doc.revision == revision and len(doc.layers) == 1
    service.undo_budget_bytes = 1024 * 1024
    assert not service.apply_pixel_outline(replace(_result(doc), source_revision=revision - 1))
    assert not service.apply_pixel_outline(_result(doc, color=(0, 0, 0, 0)))
    source = doc.active_layer
    assert service.apply_pixel_outline(_result(doc))
    outline = doc.active_layer
    service.undo_stack.undo()
    other = RasterLayer.create_blank("其他", doc.size)
    doc.insert_layer(other, 1)
    service.undo_stack.redo()
    assert doc.active_layer is outline
    assert doc.nodes[0].children[:2] == [source, outline]


def test_transient_layer_real_stack_occlusion_atomic_replace(qapp):
    doc = Document.create(32, 32)
    _shape(doc)
    source = doc.active_layer
    upper = RasterLayer.create_blank("上层", doc.size)
    upper.pixels.write_region(Rect(0, 0, 32, 32), np.full((32, 32, 4), 255, np.uint8))
    doc.insert_layer(upper, 1, activate=False)
    coordinator = CanvasRenderCoordinator()
    coordinator.set_document(doc)
    result = _result(doc, color=(255, 0, 0, 255))
    before = coordinator.generation
    coordinator.replace_raster_layer_preview(source.id, result.updates)
    assert coordinator.generation == before + 1
    assert _pixel(_paint_frame(coordinator, 32, 32), 9, 10) == (255, 255, 255, 255)
    upper.visible = False
    assert _pixel(_paint_frame(coordinator, 32, 32), 9, 10) == (255, 0, 0, 255)
    coordinator.replace_raster_layer_preview(source.id, {})
    assert _pixel(_paint_frame(coordinator, 32, 32), 9, 10) != (255, 0, 0, 255)
    coordinator.clear_raster_layer_preview()
    assert len(coordinator.display_raster_children(doc.nodes[0])) == 2


def test_detached_surface_uses_parent_undo_and_switch_cancels(qtbot, tmp_path):
    window, controller = _window_with_tiles(qtbot, tmp_path)
    project = controller.project_document
    frame = project.sprite_workspace.frames[0]
    assert controller.open_frame_surface(frame.id)
    assert not window.pixel_outline_action.isEnabled()
    assert controller.commands.detach_sprite_frame(frame.id)
    controller.set_active_surface(EditingSurfaceRef("source"))
    assert controller.open_frame_surface(frame.id)
    doc = controller.document
    _shape(doc)
    source_pixels = project.active_layer.pixels.read_tile(0, 0).copy()
    window.pixel_outline_action.trigger()
    qtbot.waitUntil(lambda: window.color_adjustment_controller.has_exact_result)
    window.pixel_outline_panel.apply_requested.emit()
    qtbot.waitUntil(lambda: not window.color_adjustment_controller.active)
    assert len(doc.layers) == 2
    np.testing.assert_array_equal(project.active_layer.pixels.read_tile(0, 0), source_pixels)
    controller.commands.undo_stack.undo()
    assert len(doc.layers) == 1
    window.pixel_outline_action.trigger()
    controller.set_active_surface(EditingSurfaceRef("source"))
    assert not window.color_adjustment_controller.active


def test_outline_enter_in_number_does_not_apply(qtbot, tmp_path):
    controller, window = _window(qtbot, tmp_path)
    _shape(controller.document)
    window.pixel_outline_action.trigger()
    control = window.pixel_outline_panel.outline_width
    qtbot.mouseClick(control, Qt.MouseButton.LeftButton)
    qtbot.keyClicks(control.lineEdit(), "4")
    qtbot.keyClick(control.lineEdit(), Qt.Key.Key_Return)
    assert window.color_adjustment_controller.active
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    assert not window.color_adjustment_controller.active


def test_applied_outline_persistence_frame_atlas_and_preview_exclusion(qapp, tmp_path):
    import json
    import zipfile

    from PIL import Image

    from easypicture.persistence import (
        SpriteAtlasExportRequest,
        export_sprite_atlas,
        load_project,
        save_project,
    )
    from easypicture.persistence.sprite_io import (
        SpriteFrameExportRequest,
        export_sprite_frame_png,
    )

    doc = Document.create(32, 32)
    _shape(doc)
    service = DocumentCommandService()
    service.attach_document(doc)
    assert service.create_sprite_frame(Rect(0, 0, 32, 32))
    frame = doc.sprite_workspace.frames[0]
    result = _result(doc, color=(255, 0, 0, 255))
    render = CanvasRenderCoordinator()
    render.set_document(doc)
    render.replace_raster_layer_preview(doc.active_layer_id, result.updates)

    def progress(*_args):
        pass

    export_sprite_frame_png(
        SpriteFrameExportRequest(
            DocumentSnapshot.capture(doc), frame.id, tmp_path / "preview-excluded.png"
        ),
        CancellationToken(),
        progress,
    )
    with Image.open(tmp_path / "preview-excluded.png") as image:
        assert image.getpixel((9, 10))[3] == 0
    assert service.apply_pixel_outline(result)
    before = (doc.revision, doc.modified, service.undo_stack.count())
    save_project(
        DocumentSnapshot.capture(doc), tmp_path / "outline.limg", CancellationToken(), progress
    )
    restored = load_project(
        tmp_path / "outline.limg", CancellationToken(), progress
    ).snapshot.to_document()
    assert len(restored.layers) == 2
    with zipfile.ZipFile(tmp_path / "outline.limg") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == 4
        assert "palette_presets" not in manifest
    export_sprite_frame_png(
        SpriteFrameExportRequest(DocumentSnapshot.capture(doc), frame.id, tmp_path / "frame.png"),
        CancellationToken(),
        progress,
    )
    export_sprite_atlas(
        SpriteAtlasExportRequest(DocumentSnapshot.capture(doc), tmp_path / "atlas.png"),
        CancellationToken(),
        progress,
    )
    for name in ("frame.png", "atlas.png"):
        with Image.open(tmp_path / name) as image:
            assert image.getpixel((9, 10)) == (255, 0, 0, 255)
            assert image.info.get("icc_profile")
    assert before == (doc.revision, doc.modified, service.undo_stack.count())


def test_apply_during_palette_extraction_waits_for_new_palette(qtbot, tmp_path):
    controller, window = _window(qtbot, tmp_path)
    window.color_adjustment_actions[ColorAdjustmentKind.PALETTE_MAP].trigger()
    session = window.color_adjustment_controller
    session.extract_palette(1)
    session.apply()
    qtbot.waitUntil(lambda: not session.active)
    assert window.color_adjustment_panel.parameters().colors == ((40, 80, 120),)
    assert controller.commands.undo_stack.count() == 0


def test_failed_outline_task_clears_private_preview(qtbot, tmp_path, monkeypatch):
    controller, window = _window(qtbot, tmp_path)
    _shape(controller.document)
    window.pixel_outline_action.trigger()
    session = window.color_adjustment_controller
    qtbot.waitUntil(lambda: session.has_exact_result)
    handle = JobHandle(uuid4(), CancellationToken())
    monkeypatch.setattr(controller.jobs, "submit", lambda _task: handle)
    window.pixel_outline_panel.outline_width.setValue(8)
    session._debounce.stop()
    session._submit_latest()
    handle.finished.emit(JobOutcome(handle.job_id, JobStatus.FAILED, error=ValueError("test")))
    assert not session.active
    assert (
        len(
            window.canvas_view.render_coordinator.display_raster_children(
                controller.document.nodes[0]
            )
        )
        == 1
    )
