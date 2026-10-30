from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, QMimeData, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QDragMoveEvent, QDropEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QAbstractItemView

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, Size
from easypicture.pixels import Rect
from easypicture.ui.sprite_frame_panel import SpriteFramePanel
from tests.fakes import make_settings


def _panel_with_frames(
    qtbot,
    tmp_path,
    *,
    frame_count: int = 3,
) -> tuple[SpriteFramePanel, Document]:
    settings = make_settings(str(tmp_path / "settings.ini"))
    document = Document.create(128, 64)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.set_sprite_cell_size(Size(16, 16), clear_frames=False)
    for index in range(frame_count):
        assert commands.create_sprite_frame(Rect((index % 8) * 16, (index // 8) * 16, 16, 16))
    panel = SpriteFramePanel(settings)
    qtbot.addWidget(panel)
    panel.set_document(document)
    return panel, document


def test_sprite_frame_items_keep_fixed_footprint_and_middle_elision(
    qtbot,
    tmp_path,
) -> None:
    panel, _document = _panel_with_frames(qtbot, tmp_path)
    initial_grid = panel.list.gridSize()

    assert panel.list.textElideMode() is Qt.TextElideMode.ElideMiddle
    assert initial_grid == QSize(88, 98)
    assert panel.list.item(0).sizeHint() == initial_grid

    panel.list.item(0).setText("x")
    assert panel.list.gridSize() == initial_grid
    assert panel.list.item(0).sizeHint() == initial_grid


def test_sprite_frame_grid_wraps_without_horizontal_scrolling(qtbot, tmp_path) -> None:
    panel, _document = _panel_with_frames(qtbot, tmp_path, frame_count=12)
    panel.resize(210, 260)
    panel.show()
    qtbot.waitExposed(panel)
    qtbot.wait(20)

    assert panel.list.isWrapping()
    assert panel.list.horizontalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    first_second_row = max(1, panel.list.viewport().width() // panel.list.gridSize().width())
    assert first_second_row < panel.list.count()
    assert panel.list.visualItemRect(panel.list.item(first_second_row)).top() > (
        panel.list.visualItemRect(panel.list.item(0)).top()
    )


def test_sprite_frame_wrapped_grid_uses_row_major_drop_insertion(qtbot, tmp_path) -> None:
    panel, _document = _panel_with_frames(qtbot, tmp_path, frame_count=12)
    panel.resize(210, 260)
    panel.show()
    qtbot.waitExposed(panel)
    qtbot.wait(20)

    first_second_row = max(1, panel.list.viewport().width() // panel.list.gridSize().width())
    second_row = panel.list.visualItemRect(panel.list.item(first_second_row))
    assert (
        panel.list._target_insertion_row(QPoint(second_row.left(), second_row.center().y()))
        == first_second_row
    )


def test_sprite_frame_view_size_and_sort_actions_are_explicit(
    qtbot,
    tmp_path,
) -> None:
    panel, document = _panel_with_frames(qtbot, tmp_path)
    sort_spy = QSignalSpy(panel.reorder_requested)
    move_spy = QSignalSpy(panel.move_requested)

    panel._request_sort("reverse")
    assert sort_spy.count() == 1
    order, label = sort_spy.at(0)
    assert order == tuple(reversed(tuple(frame.id for frame in document.sprite_workspace.frames)))
    assert label == "反转精灵帧顺序"

    frame_id = document.sprite_workspace.frames[0].id
    panel.list.move_requested.emit(frame_id, 2)
    assert move_spy.count() == 1
    assert move_spy.at(0) == [frame_id, 2]

    panel.view_size_combo.setCurrentIndex(panel.view_size_combo.findData(96))
    assert panel.list.iconSize() == QSize(96, 96)
    assert panel.list.gridSize() == QSize(120, 130)
    assert panel._settings.sprite_thumbnail_size() == 96


def test_sprite_frame_f2_starts_rename(qtbot, tmp_path, monkeypatch) -> None:
    panel, _document = _panel_with_frames(qtbot, tmp_path)
    panel.list.setCurrentRow(0)
    edited: list[object] = []
    monkeypatch.setattr(panel.list, "editItem", edited.append)

    panel.rename_shortcut.activated.emit()

    assert edited == [panel.list.item(0)]


def test_sprite_frame_extended_selection_and_delete_shortcut(qtbot, tmp_path) -> None:
    panel, document = _panel_with_frames(qtbot, tmp_path)
    delete_spy = QSignalSpy(panel.delete_requested)
    panel.list.item(0).setSelected(True)
    panel.list.item(2).setSelected(True)
    panel.list.setCurrentItem(
        panel.list.item(2),
        QItemSelectionModel.SelectionFlag.NoUpdate,
    )

    assert panel.list.selectionMode() is QAbstractItemView.SelectionMode.ExtendedSelection
    assert panel.selected_frame_ids == (
        document.sprite_workspace.frames[0].id,
        document.sprite_workspace.frames[2].id,
    )
    panel.delete_shortcut.activated.emit()
    assert delete_spy.count() == 1
    assert delete_spy.at(0)[0] == panel.selected_frame_ids


def test_refresh_keeps_existing_thumbnail_while_reordering(qtbot, tmp_path) -> None:
    panel, _document = _panel_with_frames(qtbot, tmp_path)
    panel.show()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(lambda: not panel.list.item(0).icon().isNull())
    frame_id = panel.list.item(0).data(panel.FRAME_ID_ROLE)
    panel.refresh()

    item = next(
        panel.list.item(index)
        for index in range(panel.list.count())
        if panel.list.item(index).data(panel.FRAME_ID_ROLE) == frame_id
    )
    assert not item.icon().isNull()


def test_dragging_frame_over_animation_does_not_activate_hovered_tab(
    qtbot,
    tmp_path,
) -> None:
    panel, document = _panel_with_frames(qtbot, tmp_path)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_sprite_animation("待机")
    panel.set_document(document)
    animation = document.sprite_workspace.animations[0]
    target_index = next(
        index
        for index in range(panel.group_tabs.count())
        if panel.group_tabs.tabData(index) == animation.id
    )
    panel.group_tabs.setCurrentIndex(0)
    panel.list.setCurrentRow(0)
    target_position = panel.group_tabs.tabRect(target_index).center()
    drag_move = QDragMoveEvent(
        target_position,
        Qt.DropAction.MoveAction,
        QMimeData(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    assert panel.eventFilter(panel.group_tabs, drag_move)
    qtbot.wait(400)

    assert panel.group_tabs.currentIndex() == 0

    move_spy = QSignalSpy(panel.frame_group_move_requested)
    drop = QDropEvent(
        QPointF(target_position),
        Qt.DropAction.MoveAction,
        QMimeData(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    assert panel.eventFilter(panel.group_tabs, drop)
    assert move_spy.count() == 1
    assert move_spy.at(0) == [panel.selected_frame_ids, animation.id, 0]
