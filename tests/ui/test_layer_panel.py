from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from easypicture.controller import ApplicationController
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.main_window import MainWindow
from tests.fakes import FakeDialogService, make_settings


def _window_with_document(qtbot, tmp_path) -> tuple[MainWindow, ApplicationController]:
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(640, 480)
    settings = make_settings(str(tmp_path / "settings.ini"))
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    assert controller.request_new_document(window)
    return window, controller


def _names(window: MainWindow) -> list[str]:
    model = window.layer_panel.model
    return [
        str(model.data(model.index(row, 0), Qt.ItemDataRole.DisplayRole))
        for row in range(model.rowCount())
    ]


def _visibility_point(window: MainWindow, row: int) -> QPoint:
    panel = window.layer_panel
    return panel.list_view.visibility_rect(panel.model.index(row, 0)).center()


def _text_point(window: MainWindow, row: int) -> QPoint:
    panel = window.layer_panel
    return panel.list_view.text_rect(panel.model.index(row, 0)).center()


def test_layer_panel_shows_top_layer_first_and_buttons_are_undoable(qtbot, tmp_path) -> None:
    window, controller = _window_with_document(qtbot, tmp_path)
    panel = window.layer_panel

    assert _names(window) == ["图层 1"]
    assert not panel.delete_button.isEnabled()
    panel.new_button.click()

    assert _names(window) == ["图层 2", "图层 1"]
    assert controller.document is not None
    assert controller.document.active_layer.name == "图层 2"
    assert window.undo_action.isEnabled()
    assert window.undo_action.text() == "撤销 新建图层"

    window.undo_action.trigger()
    assert _names(window) == ["图层 1"]
    assert not controller.document.modified
    assert window.redo_action.isEnabled()

    window.redo_action.trigger()
    assert _names(window) == ["图层 2", "图层 1"]


def test_layer_edit_visibility_opacity_and_locks_round_trip(qtbot, tmp_path) -> None:
    window, controller = _window_with_document(qtbot, tmp_path)
    panel = window.layer_panel
    assert isinstance(panel.opacity_spin, DragValueSpinBox)
    document = controller.document
    assert document is not None
    index = panel.model.index(0, 0)

    assert panel.model.setData(index, "  背景  ", Qt.ItemDataRole.EditRole)
    assert panel.model.setData(index, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    panel.opacity_spin.setValue(35)
    panel.opacity_spin.editingFinished.emit()
    panel.locked_check.click()
    panel.alpha_locked_check.click()

    layer = document.active_layer
    assert layer.name == "背景"
    assert not layer.visible
    assert layer.opacity == 0.35
    assert layer.locked
    assert layer.alpha_locked

    for _ in range(5):
        controller.commands.undo_stack.undo()
    assert layer.name == "图层 1"
    assert layer.visible
    assert layer.opacity == 1.0
    assert not layer.locked
    assert not layer.alpha_locked
    assert not document.modified


def test_layer_buttons_and_drag_drop_reorder(qtbot, tmp_path) -> None:
    window, controller = _window_with_document(qtbot, tmp_path)
    panel = window.layer_panel
    panel.new_button.click()
    panel.new_button.click()
    assert _names(window) == ["图层 3", "图层 2", "图层 1"]

    panel.down_button.click()
    assert _names(window) == ["图层 2", "图层 3", "图层 1"]

    bottom_index = panel.model.index(2, 0)
    mime = panel.model.mimeData([bottom_index])
    assert panel.model.dropMimeData(
        mime,
        Qt.DropAction.MoveAction,
        0,
        0,
        panel.model.index(-1, -1),
    )
    assert _names(window)[0] == "图层 1"
    assert controller.commands.undo_stack.undoText() == "移动图层"


def test_selecting_active_layer_does_not_mark_document_modified(qtbot, tmp_path) -> None:
    window, controller = _window_with_document(qtbot, tmp_path)
    panel = window.layer_panel
    panel.new_button.click()
    document = controller.document
    assert document is not None
    document.mark_clean()

    panel.list_view.setCurrentIndex(panel.model.index(1, 0))

    assert document.active_layer.name == "图层 1"
    assert not document.modified


def test_visibility_double_click_is_not_swallowed_by_rename(qtbot, tmp_path) -> None:
    window, controller = _window_with_document(qtbot, tmp_path)
    window.show()
    qtbot.wait(1)
    document = controller.document
    assert document is not None
    active_id = document.active_layer_id

    QTest.mouseDClick(
        window.layer_panel.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=_visibility_point(window, 0),
    )

    assert not document.active_layer.visible
    assert document.active_layer_id == active_id
    assert controller.commands.undo_stack.count() == 1
    assert not window.layer_panel.list_view.visibility_scrubbing


def test_visibility_scrub_batches_rows_without_selecting_or_reordering(
    qtbot,
    tmp_path,
) -> None:
    window, controller = _window_with_document(qtbot, tmp_path)
    panel = window.layer_panel
    for _ in range(3):
        panel.new_button.click()
    window.show()
    qtbot.wait(1)
    document = controller.document
    assert document is not None
    active_id = document.active_layer_id
    before_names = _names(window)
    before_count = controller.commands.undo_stack.count()

    QTest.mousePress(
        panel.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=_visibility_point(window, 0),
    )
    QTest.mouseMove(panel.list_view.viewport(), _visibility_point(window, 2))
    QTest.mouseRelease(
        panel.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=_visibility_point(window, 2),
    )

    assert [layer.visible for layer in reversed(document.layers)] == [
        False,
        False,
        False,
        True,
    ]
    assert document.active_layer_id == active_id
    assert _names(window) == before_names
    assert controller.commands.undo_stack.count() == before_count + 1
    assert controller.commands.undo_stack.undoText() == "批量切换图层可见性"

    controller.commands.undo_stack.undo()
    assert all(layer.visible for layer in document.layers)


def test_only_text_region_changes_active_layer(qtbot, tmp_path) -> None:
    window, controller = _window_with_document(qtbot, tmp_path)
    panel = window.layer_panel
    panel.new_button.click()
    window.show()
    qtbot.wait(1)
    document = controller.document
    assert document is not None
    top_id = document.active_layer_id

    QTest.mouseClick(
        panel.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=_visibility_point(window, 1),
    )
    assert document.active_layer_id == top_id

    QTest.mouseClick(
        panel.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=_text_point(window, 1),
    )
    assert document.active_layer.name == "图层 1"
