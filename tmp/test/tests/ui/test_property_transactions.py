from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, SvgRectGeometry
from easypicture.ui.drag_value_spinbox import NumericEditState
from easypicture.ui.text_properties import TextPropertiesPanel
from tests.ui.test_layer_panel import _window_with_document
from tests.ui.test_p9d_svg_ui import _panel_for_object


def _drag(field, deltas=(10, 20, 30)):
    core = getattr(field, "_field", field)
    local = QPointF(core.rect().center())
    origin = QPointF(core.mapToGlobal(local.toPoint()))
    QApplication.sendEvent(
        core,
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            local,
            origin,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    for delta in deltas:
        QApplication.sendEvent(
            core,
            QMouseEvent(
                QEvent.Type.MouseMove,
                local + QPointF(delta, 0),
                origin + QPointF(delta, 0),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
    return core, local, origin


def _text_panel(qtbot):
    document = Document.create(128, 96)
    commands = DocumentCommandService()
    commands.attach_document(document)
    commands.create_vector_group()
    group = document.vector_groups[-1]
    commands.create_text_object(group.id)
    live = group.children[0]
    panel = TextPropertiesPanel(commands)
    qtbot.addWidget(panel)
    panel.set_object(live)
    panel.show()
    commands.undo_stack.clear()
    return commands, panel, live


def test_text_drag_previews_and_commits_once(qtbot):
    commands, panel, live = _text_panel(qtbot)
    original = live.font_size
    previews = []
    panel.preview_changed.connect(previews.append)
    core, local, origin = _drag(panel.size_spin)
    assert panel.size_spin.interaction_state is NumericEditState.DRAGGING
    assert live.font_size == original
    assert commands.undo_stack.count() == 0
    assert len(previews) == 3
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
    assert commands.undo_stack.count() == 1
    assert live.font_size == previews[-1].font_size
    commands.undo_stack.undo()
    assert live.font_size == original


def test_text_drag_cancel_has_no_history(qtbot):
    commands, panel, live = _text_panel(qtbot)
    original = live.font_size
    core, _, _ = _drag(panel.size_spin)
    QApplication.sendEvent(
        core, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    assert commands.undo_stack.count() == 0
    assert live.font_size == original
    assert panel.size_spin.value() == original


def test_svg_corner_feedback_keeps_dragging(qtbot):
    _, commands, panel, live = _panel_for_object(qtbot, SvgRectGeometry(4, 5, 20, 10, 2, 2))
    panel.show()
    previews = []
    panel.preview_changed.connect(previews.append)
    _drag(panel.rect_fields[4], (4,))
    assert panel.rect_fields[4].interaction_state is NumericEditState.DRAGGING
    panel.set_preview_object(previews[-1])
    assert panel.rect_fields[4].interaction_state is NumericEditState.DRAGGING
    assert commands.undo_stack.count() == 0
    assert live is not previews[-1]


def test_switching_text_target_cancels_old_drag(qtbot):
    commands, panel, first = _text_panel(qtbot)
    document = commands.document
    group = document.vector_groups[0]
    commands.create_text_object(group.id, name="Other")
    second = group.children[-1]
    commands.undo_stack.clear()
    core, local, origin = _drag(panel.size_spin)
    panel.set_object(second)
    assert panel.size_spin.interaction_state is NumericEditState.IDLE
    QApplication.sendEvent(
        core,
        QMouseEvent(
            QEvent.Type.MouseMove,
            local + QPointF(30, 0),
            origin + QPointF(30, 0),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    assert first.font_size == second.font_size == 32
    assert commands.undo_stack.count() == 0


def test_external_change_rejects_stale_text_commit(qtbot):
    commands, panel, live = _text_panel(qtbot)
    core, local, origin = _drag(panel.size_spin)
    commands.update_text_object(live.id, font_size=20)
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
    assert live.font_size == panel.size_spin.value() == 20
    assert commands.undo_stack.count() == 1


def test_document_switch_discards_old_inspector_session(qtbot, tmp_path):
    window, controller = _window_with_document(qtbot, tmp_path)
    window.show()
    controller.commands.create_vector_group()
    old_document = controller.document
    group = old_document.vector_groups[-1]
    controller.commands.create_text_object(group.id)
    live = group.children[0]
    window.layer_panel.select_node(live)
    _drag(window.text_properties.size_spin)
    assert live.font_size == 32
    assert controller.request_new_document(window)
    assert controller.document is not old_document
    assert window.text_properties._object is None
    assert window.svg_properties._object is None
    assert window.text_properties.size_spin.interaction_state is NumericEditState.IDLE
    assert live.font_size == 32


def test_layer_opacity_uses_private_preview_and_one_commit(qtbot, tmp_path):
    window, controller = _window_with_document(qtbot, tmp_path)
    window.show()
    document = controller.document
    live = document.active_layer
    controller.commands.undo_stack.clear()
    field = window.layer_panel.opacity_spin
    core, local, origin = _drag(field, (-8, -16, -32))
    assert field.interaction_state is NumericEditState.DRAGGING
    assert live.opacity == 1.0
    assert controller.commands.undo_stack.count() == 0
    displayed = window.canvas_view.render_coordinator.display_raster_children(document.nodes[0])
    assert displayed[0].opacity == field.value() / 100.0 < 1
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
    assert controller.commands.undo_stack.count() == 1
    assert live.opacity == field.value() / 100.0
    controller.commands.undo_stack.undo()
    assert live.opacity == 1.0


def test_svg_name_is_one_text_session_and_invalid_name_restores(qtbot):
    _, commands, panel, live = _panel_for_object(qtbot, SvgRectGeometry(4, 5, 20, 10))
    panel.show()
    qtbot.mouseClick(panel.name_edit, Qt.MouseButton.LeftButton)
    panel.name_edit.selectAll()
    original = live.name
    qtbot.keyClicks(panel.name_edit, "renamed")
    assert live.name == original
    assert commands.undo_stack.count() == 0
    qtbot.keyClick(panel.name_edit, Qt.Key.Key_Return)
    assert live.name == "renamed"
    assert commands.undo_stack.count() == 1
    panel.name_edit.selectAll()
    qtbot.keyClick(panel.name_edit, Qt.Key.Key_Backspace)
    qtbot.keyClick(panel.name_edit, Qt.Key.Key_Return)
    assert panel.name_edit.text() == live.name == "renamed"
    assert commands.undo_stack.count() == 1
