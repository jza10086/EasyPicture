from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from easypicture.domain import (
    AffineTransform,
    Collection,
    FloatRect,
    RasterObject,
    Size,
    TextObject,
)
from easypicture.domain.raster_edit import RasterEditDocument
from easypicture.pixels import Rect, TileCoord
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from easypicture.workspace import EditingSurfaceRef, WorkspaceController
from tests.fakes import FakeDialogService, make_settings


def _window(qtbot, tmp_path):
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(64, 64)
    controller = WorkspaceController(settings, dialogs, recovery_enabled=False)
    controller.request_exit = lambda _parent: True
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    assert controller.request_new_document(window)
    window.resize(1000, 720)
    window.show()
    return window, controller


def _tile():
    pixels = np.zeros((256, 256, 4), np.uint8)
    pixels[2:5, 3:7] = (25, 60, 230, 255)
    return pixels


def _drag(window, name, first=(8, 9), last=(24, 29)):
    window._activate_tool(name)
    tool = window.tool_controller.active_tool
    for kind, position in (
        (QEvent.Type.MouseButtonPress, first),
        (QEvent.Type.MouseMove, last),
        (QEvent.Type.MouseButtonRelease, last),
    ):
        point = QPointF(window.canvas_view.mapFromScene(QPointF(*position)))
        event = QMouseEvent(
            kind,
            point,
            point,
            Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton
            if kind == QEvent.Type.MouseButtonRelease
            else Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        {
            QEvent.Type.MouseButtonPress: tool.mouse_press,
            QEvent.Type.MouseMove: tool.mouse_move,
            QEvent.Type.MouseButtonRelease: tool.mouse_release,
        }[kind](event)


def test_raster_tab_is_local_and_shares_project_history(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    owner = controller.project_document
    layer = owner.active_layer
    matrix = (
        AffineTransform.scale(2, 3)
        .then(AffineTransform.rotation(37))
        .then(AffineTransform.translation(20, 10))
    )
    assert controller.commands.update_object_properties((layer.id,), {"transform": matrix})
    window.layer_panel.select_node(layer)
    assert not window.tool_actions["brush"].isVisible()
    assert window.tool_actions["raster_object"].isVisible()
    window.edit_raster_action.trigger()
    view = controller.document
    assert isinstance(view, RasterEditDocument)
    assert view.size == layer.size
    assert view.active_layer.transform.is_identity
    assert window.tool_actions["brush"].isVisible()
    assert not window.tool_actions["shape"].isVisible()
    assert not window.tool_actions["text"].isEnabled()
    before = controller.commands.undo_stack.activeStack().count()
    assert controller.commands.apply_pixel_tiles(layer.id, {TileCoord(0, 0): _tile()}, "paint")
    assert controller.commands.undo_stack.activeStack().count() == before + 1
    np.testing.assert_array_equal(layer.pixels.read_tile(0, 0), _tile())
    assert layer.transform == matrix
    window._return_object_canvas()
    assert controller.document is owner
    controller.commands.undo_stack.undo()
    assert not np.any(layer.pixels.read_tile(0, 0))
    controller.commands.undo_stack.redo()
    assert controller.open_raster_surface(layer.id)
    assert controller.document is view
    np.testing.assert_array_equal(view.active_layer.pixels.read_tile(0, 0), _tile())


def test_raster_frame_writes_only_to_detached_frame(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    project = controller.project_document
    source_layer = project.active_layer
    assert controller.commands.create_sprite_frame(Rect(0, 0, 32, 32))
    frame = project.sprite_workspace.frames[0]
    assert controller.commands.detach_sprite_frame(frame.id)
    assert controller.open_frame_surface(frame.id)
    owner = controller.document
    layer = owner.active_layer
    assert controller.open_raster_surface(layer.id)
    assert controller.commands.apply_pixel_tiles(layer.id, {TileCoord(0, 0): _tile()}, "paint")
    assert np.any(owner.active_layer.pixels.read_tile(0, 0))
    assert not np.any(source_layer.pixels.read_tile(0, 0))
    surface = controller.active_session.active_surface
    controller.close_raster_surface(surface)
    assert controller.document is owner
    controller.commands.undo_stack.undo()
    assert not np.any(owner.active_layer.pixels.read_tile(0, 0))


def test_deleted_target_closes_editor_and_undo_restores_pixels(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    owner = controller.project_document
    key = owner.active_layer_id
    assert controller.open_raster_surface(key)
    assert controller.commands.apply_pixel_tiles(key, {TileCoord(0, 0): _tile()}, "paint")
    window._return_object_canvas()
    assert controller.commands.delete_nodes((key,))
    assert all(surface.kind != "raster" for surface in controller.active_session.open_surfaces)
    controller.commands.undo_stack.undo()
    controller.commands.undo_stack.undo()
    assert not np.any(owner.layer_by_id(key).pixels.read_tile(0, 0))
    controller.commands.undo_stack.redo()
    assert controller.open_raster_surface(key)
    np.testing.assert_array_equal(controller.document.active_layer.pixels.read_tile(0, 0), _tile())


def test_rectangle_creation_and_shape_inherit_nested_parent(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    document = controller.document
    parent = Collection.create("Nested")
    assert controller.commands.create_object(parent, document.nodes[0].id)
    text = TextObject.create(FloatRect(0, 0, 5, 5))
    assert controller.commands.create_object(text, parent.id)
    window.layer_panel.select_node(document.node_by_id(text.id))
    count = len(document.collections)
    before = controller.commands.undo_stack.activeStack().count()
    _drag(window, "raster_object")
    raster = window.layer_panel.current_node()
    assert isinstance(raster, RasterObject)
    assert raster.size == Size(16, 20)
    assert document.parent_of(raster.id).id == parent.id
    assert len(document.collections) == count
    assert controller.commands.undo_stack.activeStack().count() == before + 1
    _drag(window, "shape", (31, 30), (45, 42))
    shape = window.layer_panel.current_node()
    assert document.parent_of(shape.id).id == parent.id
    assert len(document.collections) == count
    _drag(window, "shape", (46, 44), (60, 60))
    assert document.parent_of(window.layer_panel.current_node().id).id == parent.id
    assert len(document.collections) == count


def test_root_object_creation_stays_at_root(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    document = controller.document
    text = TextObject.create(FloatRect(0, 0, 5, 5))
    assert controller.commands.create_object(text)
    window.layer_panel.select_node(document.node_by_id(text.id))
    count = len(document.collections)
    _drag(window, "shape")
    assert document.parent_of(window.layer_panel.current_node().id) is None
    assert len(document.collections) == count
    _drag(window, "text", (31, 31), (50, 50))
    assert isinstance(window.layer_panel.current_node(), TextObject)
    assert document.parent_of(window.layer_panel.current_node().id) is None
    assert len(document.collections) == count


def test_raster_tabs_are_unique_and_project_scoped(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    first = controller.active_session
    key = first.document.active_layer_id
    assert controller.open_raster_surface(key)
    view = controller.document
    assert controller.open_raster_surface(key)
    assert len(first.open_surfaces) == 2
    assert controller.request_new_document(window)
    assert controller.document is controller.project_document
    assert not window.tool_actions["brush"].isVisible()
    controller.set_active_session(0)
    assert controller.document is view
    assert window.tool_actions["brush"].isVisible()
    controller.close_raster_surface(first.active_surface)
    assert controller.active_session.active_surface == EditingSurfaceRef("source")


def test_local_brush_bucket_selection_and_project_roundtrip(qtbot, tmp_path):
    from easypicture.jobs import CancellationToken
    from easypicture.persistence import DocumentSnapshot
    from easypicture.persistence.project_io import load_project, save_project

    window, controller = _window(qtbot, tmp_path)
    owner = controller.document
    key = owner.active_layer_id
    matrix = AffineTransform.scale(2, -3).then(AffineTransform.rotation(35))
    assert controller.commands.update_object_properties((key,), {"transform": matrix})
    window.layer_panel.select_node(owner.active_layer)
    window._activate_tool("transform")
    assert window.tool_controller.active_tool is window._tools["object"]
    window._open_raster_editor()
    view = controller.document
    before = controller.commands.undo_stack.activeStack().count()
    _drag(window, "brush", (10, 16), (40, 16))
    assert controller.commands.undo_stack.activeStack().count() == before + 1
    painted = owner.active_layer.pixels.read_tile(0, 0).copy()
    assert painted[16, 10:41, 3].max() > 0
    assert owner.active_layer.transform == matrix
    _drag(window, "rectangle_selection", (4, 4), (50, 50))
    assert view.selection.is_active
    assert not owner.selection.is_active
    _drag(window, "bucket", (25, 30), (25, 30))
    qtbot.waitUntil(lambda: not window._editing_busy, timeout=10000)
    pixels = owner.active_layer.pixels.read_tile(0, 0).copy()
    assert pixels[30, 25, 3] > 0
    assert pixels[60, 60, 3] == 0
    path = tmp_path / "raster.limg"

    def progress(*_args):
        pass

    save_project(DocumentSnapshot.capture(owner), path, CancellationToken(), progress)
    restored = load_project(path, CancellationToken(), progress).snapshot.to_document()
    assert restored.layer_by_id(key).transform == matrix
    np.testing.assert_array_equal(restored.layer_by_id(key).pixels.read_tile(0, 0), pixels)
    window._return_object_canvas()
    controller.commands.undo_stack.undo()
    np.testing.assert_array_equal(owner.layer_by_id(key).pixels.read_tile(0, 0), painted)


def test_raster_lock_and_metadata_refresh(qtbot, tmp_path):
    from easypicture.domain.events import LayerPixelsChanged

    window, controller = _window(qtbot, tmp_path)
    owner = controller.document
    key = owner.active_layer_id
    parent = owner.parent_of(key)
    assert controller.commands.update_object_properties((parent.id,), {"locked": True})
    assert not controller.open_raster_surface(key)
    assert controller.commands.update_object_properties((parent.id,), {"locked": False})
    window._open_raster_editor()
    view = controller.document
    events = []
    view.subscribe(events.append)
    owner.set_layer_name(key, "Renamed")
    assert view.active_layer.name == "Renamed"
    assert not any(isinstance(event, LayerPixelsChanged) for event in events)
    window._set_editing_busy(True)
    window._set_editing_busy(False)
    assert not window.layer_panel.isEnabled()
    assert not window.append_action.isEnabled()
    assert not window.tool_actions["shape"].isEnabled()


def test_external_target_deletion_closes_active_raster_tab(qtbot, tmp_path):
    from easypicture.commands import DocumentCommandService

    window, controller = _window(qtbot, tmp_path)
    owner = controller.document
    key = owner.active_layer_id
    assert controller.open_raster_surface(key)
    commands = DocumentCommandService()
    commands.attach_document(owner)
    assert commands.delete_nodes((key,))
    assert controller.document is owner
    assert controller.active_session.active_surface.kind == "source"
    assert not window.tool_actions["brush"].isVisible()
    commands.attach_document(None)


def test_switching_surface_cancels_unfinished_pixel_stroke(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    owner = controller.document
    key = owner.active_layer_id
    window._open_raster_editor()
    before = controller.commands.undo_stack.activeStack().count()
    viewport = window.canvas_view.viewport()
    start = window.canvas_view.mapFromScene(QPointF(12, 12))
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, window.canvas_view.mapFromScene(QPointF(35, 12)))
    controller.set_active_surface(EditingSurfaceRef("source"))
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=start)
    assert controller.commands.undo_stack.activeStack().count() == before
    assert not np.any(owner.layer_by_id(key).pixels.read_tile(0, 0))


def test_raster_creation_cancels_without_history_and_boundary_tracks_preview(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    document = controller.document
    raster = document.active_layer
    coordinator = window.canvas_view.render_coordinator
    preview = AffineTransform.rotation(45).then(AffineTransform.translation(10, 20))
    coordinator.set_object_transform_preview({raster.id: preview})
    assert coordinator.raster_display_transform(raster) == preview
    assert raster.transform.is_identity
    coordinator.clear_object_transform_preview()
    window._activate_tool("raster_object")
    tool = window.tool_controller.active_tool
    point = QPointF(window.canvas_view.mapFromScene(QPointF(5, 5)))
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        point,
        point,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        point,
        point,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    before = controller.commands.undo_stack.activeStack().count()
    assert tool.mouse_press(press)
    assert tool.key_press(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    assert tool.key_release(
        QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    assert tool.mouse_release(release)
    assert controller.commands.undo_stack.activeStack().count() == before
