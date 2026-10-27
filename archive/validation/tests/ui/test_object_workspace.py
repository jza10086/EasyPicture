from PySide6.QtCore import QEvent, QItemSelectionModel, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from easypicture.domain import AffineTransform, Collection, Size
from easypicture.tools.object_tool import ObjectTool
from easypicture.ui.property_binding import interaction_coordinator
from tests.ui.test_layer_panel import _window_with_document
from tests.ui.test_property_transactions import _drag


def _event(window, kind, x, y, buttons=Qt.MouseButton.LeftButton):
    point = QPointF(window.canvas_view.mapFromScene(QPointF(x, y)))
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    return QMouseEvent(kind, point, point, button, buttons, Qt.KeyboardModifier.NoModifier)


def test_multi_selection_search_and_reparent_preserve_workspace(qtbot, tmp_path):
    window, controller = _window_with_document(qtbot, tmp_path)
    document = controller.document
    panel = window.layer_panel
    panel.new_button.click()
    first, second = document.layers
    selection_model = panel.tree_view.selectionModel()
    selection_model.select(
        panel.tree_model.index_for_id(first.id),
        QItemSelectionModel.SelectionFlag.Select,
    )
    assert set(panel.selection.state.selected_ids) == {first.id, second.id}
    revision = document.revision
    panel.search.setText(first.name)
    assert not panel.tree_view.dragEnabled()
    assert not panel.tree_view.acceptDrops()
    assert document.revision == revision
    assert set(panel.selection.state.selected_ids) == {first.id, second.id}
    panel.search.clear()
    assert panel.tree_view.dragEnabled()
    group = Collection.create("Nested")
    assert controller.commands.create_object(group)
    assert controller.commands.move_node(first.id, group.id, 0)
    assert set(panel.selection.state.selected_ids) == {first.id, second.id}
    panel.delete_button.click()
    assert not document.layers
    controller.commands.undo_stack.undo()
    assert {layer.id for layer in document.layers} == {first.id, second.id}


def test_object_drag_previews_once_cancel_and_selection_does_not_switch_tool(qtbot, tmp_path):
    window, controller = _window_with_document(qtbot, tmp_path)
    document = controller.document
    layer = document.active_layer
    layer.size = Size(16, 16)
    window.canvas_view.set_zoom(4)
    window.canvas_view.centerOn(8, 8)
    window.tool_actions["object"].setChecked(True)
    window._activate_tool("object")
    tool = window.tool_controller.active_tool
    assert isinstance(tool, ObjectTool)
    count = controller.commands.undo_stack.count()
    for canceled in (True, False):
        assert window.tool_controller.mouse_press(
            _event(window, QEvent.Type.MouseButtonPress, 4, 4)
        )
        for x in (5, 6, 7):
            assert window.tool_controller.mouse_move(_event(window, QEvent.Type.MouseMove, x, 7))
        assert layer.transform.is_identity
        assert controller.commands.undo_stack.count() == count
        if canceled:
            assert tool.key_press(
                QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
            )
            assert tool.key_release(
                QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
            )
            window.tool_controller.mouse_release(
                _event(window, QEvent.Type.MouseButtonRelease, 7, 7, Qt.MouseButton.NoButton)
            )
        else:
            assert window.tool_controller.mouse_release(
                _event(window, QEvent.Type.MouseButtonRelease, 7, 7, Qt.MouseButton.NoButton)
            )
    assert layer.transform == AffineTransform.translation(3, 3)
    assert controller.commands.undo_stack.count() == count + 1
    assert controller.commands.create_collection("Vectors")
    group = document.collections[-1]
    assert controller.commands.create_text_object(group.id)
    panel = window.layer_panel
    panel.select_node(document.vector_group_by_id(group.id).children[-1])
    assert window.tool_controller.active_tool is tool
    assert document.active_layer_id is None


def test_shared_object_transform_field_survives_model_feedback_and_commits_once(qtbot, tmp_path):
    window, controller = _window_with_document(qtbot, tmp_path)
    window._activate_tool("object")
    panel = window.object_properties
    document = controller.document
    layer = document.active_layer
    count = controller.commands.undo_stack.count()
    field = panel.fields["rotation"]
    core, local, origin = _drag(field, (2, 3, 4))
    assert layer.transform.is_identity

    assert controller.commands.undo_stack.count() == count
    window.layer_panel._sync_controls()
    assert panel._session.active
    QApplication.sendEvent(
        core,
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            local,
            origin,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    assert controller.commands.undo_stack.count() == count + 1
    assert not layer.transform.is_identity
    controller.commands.undo_stack.undo()
    assert layer.transform.is_identity


def test_deleting_drag_target_cancels_preview_and_releases_owner_immediately(qtbot, tmp_path):
    window, controller = _window_with_document(qtbot, tmp_path)
    window._activate_tool("object")
    target = controller.document.active_layer.id
    assert window.tool_controller.mouse_press(_event(window, QEvent.Type.MouseButtonPress, 4, 4))
    assert window.tool_controller.mouse_move(_event(window, QEvent.Type.MouseMove, 7, 7))
    tool = window.tool_controller.active_tool
    assert interaction_coordinator(window.canvas_view).owner is tool
    assert controller.commands.objects.delete((target,))
    assert tool._start is None
    assert interaction_coordinator(window.canvas_view).owner is None
