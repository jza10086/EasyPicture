from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPersistentModelIndex, Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QTabBar

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document
from easypicture.domain.events import ChangeCategory
from easypicture.pixels import TileCoord
from easypicture.render import CanvasRenderCoordinator
from easypicture.ui.layer_panel import LayerPanel
from easypicture.ui.tab_sync import synchronize_tabs
from tests.ui.test_sprite_frame_panel import _panel_with_frames


def test_metadata_preserves_source_images_and_issues_no_frame(qapp):
    document = Document.create(512, 256)
    layer = document.active_layer
    layer.pixels.replace_tile(0, 0, np.full((256, 256, 4), 255, np.uint8))
    renderer = CanvasRenderCoordinator()
    renderer.set_document(document)
    image = renderer.display_tile_image(layer, TileCoord(0, 0), "source")
    frames = QSignalSpy(renderer.frame_requested)
    document.set_layer_name(layer.id, "Renamed")
    document.set_layer_locked(layer.id, True)
    assert renderer.display_tile_image(layer, TileCoord(0, 0), "source") is image
    assert frames.count() == 0
    renderer.dispose()


def test_pixel_change_preserves_other_tile_and_emits_one_frame(qapp):
    document = Document.create(512, 256)
    layer = document.active_layer
    renderer = CanvasRenderCoordinator()
    renderer.set_document(document)
    untouched = renderer.display_tile_image(layer, TileCoord(1, 0), "source")
    changes = []
    unsubscribe = document.subscribe_changes(changes.append)
    frames = QSignalSpy(renderer.frame_requested)
    document.replace_layer_tiles(layer.id, {TileCoord(0, 0): np.full((256, 256, 4), 128, np.uint8)})
    assert renderer.display_tile_image(layer, TileCoord(1, 0), "source") is untouched
    assert frames.count() == 1
    assert len(changes) == 1
    assert changes[0].document_tiles == frozenset({TileCoord(0, 0)})
    assert layer.id in changes[0].object_ids
    unsubscribe()
    renderer.dispose()


def test_document_versions_separate_metadata_pixels_style_and_selection(qapp):
    document = Document.create(32, 32)
    changes = []
    document.subscribe_changes(changes.append)
    layer = document.active_layer
    document.set_layer_name(layer.id, "metadata")
    assert document.versions.metadata == 1
    assert document.versions.rendering == (0, 0, 0, 0)
    assert changes[-1].object_properties == {layer.id: frozenset({"name"})}
    document.set_layer_opacity(layer.id, 0.5)
    assert document.versions.rendering == (0, 1, 0, 0)
    document.replace_layer_tiles(layer.id, {TileCoord(0, 0): np.full((256, 256, 4), 255, np.uint8)})
    assert changes[-1].categories == frozenset({ChangeCategory.CONTENT})
    assert changes[-1].layer_tiles == {layer.id: frozenset({TileCoord(0, 0)})}
    document.replace_selection_tiles({TileCoord(0, 0): np.full((256, 256), 255, np.uint8)})
    assert document.versions.rendering == (1, 1, 0, 0)
    assert document.versions.selection == 1


def test_tree_insert_keeps_vector_selection_without_model_reset(qtbot):
    document = Document.create(128, 96)
    commands = DocumentCommandService()
    commands.attach_document(document)
    commands.create_vector_group()
    group = document.vector_groups[-1]
    commands.create_text_object(group.id)
    selected = group.children[0]
    panel = LayerPanel(commands)
    qtbot.addWidget(panel)
    panel.set_document(document)
    panel.show()
    unsubscribe = document.subscribe(panel.handle_event)
    panel.select_node(selected)
    persistent = QPersistentModelIndex(panel.tree_view.currentIndex())
    resets = QSignalSpy(panel.tree_model.modelReset)
    try:
        commands.create_text_object(group.id, name="Second")
        assert resets.count() == 0
        assert panel.current_node() is selected
        assert persistent.isValid()
        assert persistent.data(Qt.ItemDataRole.DisplayRole) == selected.name
        commands.undo_stack.undo()
        assert resets.count() == 0
        assert panel.current_node() is selected
    finally:
        unsubscribe()


def test_tab_rename_keeps_tab_controls(qtbot):
    tabs = QTabBar()
    qtbot.addWidget(tabs)
    synchronize_tabs(tabs, [("a", "First"), ("b", "Second")], "a")
    changes = QSignalSpy(tabs.currentChanged)
    synchronize_tabs(tabs, [("a", "Renamed"), ("b", "Second")], "a")
    assert tabs.count() == 2
    assert tabs.tabData(0) == "a"
    assert tabs.tabText(0) == "Renamed"
    assert changes.count() == 0


def test_thumbnail_invalidations_capture_one_snapshot_per_event_loop(qtbot, tmp_path):
    panel, _document = _panel_with_frames(qtbot, tmp_path)
    qtbot.waitUntil(lambda: panel.snapshot_capture_count == 1)
    baseline = panel.snapshot_capture_count
    panel.invalidate_thumbnails()
    panel.invalidate_thumbnails()
    panel.invalidate_thumbnails()
    assert panel.snapshot_capture_count == baseline
    qtbot.waitUntil(lambda: panel.snapshot_capture_count == baseline + 1)
    qtbot.wait(5)
    assert panel.snapshot_capture_count == baseline + 1
