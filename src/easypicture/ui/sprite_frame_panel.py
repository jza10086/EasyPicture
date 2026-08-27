"""Bottom sprite-frame tray with lazy authoritative thumbnails."""

from __future__ import annotations

import re
from uuid import UUID

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QObject,
    QPoint,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QImage,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from easypicture.domain import (
    Document,
    SpriteAnimation,
    SpriteFrameContentMode,
    SpriteFrameDefinition,
    SpritePlaybackMode,
    SpriteWorkspaceChanged,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot, render_snapshot_region
from easypicture.pixels import Rect
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.settings import SPRITE_THUMBNAIL_SIZES, AppSettings


class _SpriteFrameList(QListWidget):
    """Wrapping icon grid with deterministic insertion-style reordering."""

    move_requested = Signal(object, int)
    reorder_requested = Signal(object)
    layout_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drop_indicator_index: int | None = None

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self.layout_changed.emit)

    def _target_insertion_row(self, position: QPoint) -> int:
        """Resolve a row-major insertion position for the wrapped icon grid."""
        for row in range(self.count()):
            rect = self.visualItemRect(self.item(row))
            if position.y() < rect.top():
                return row
            if rect.top() <= position.y() <= rect.bottom() and position.x() < rect.center().x():
                return row
        return self.count()

    def dropEvent(self, event: QDropEvent) -> None:
        selected_ids = {
            item.data(Qt.ItemDataRole.UserRole) for item in self.selectedItems()
        }
        current_order = [
            self.item(row).data(Qt.ItemDataRole.UserRole) for row in range(self.count())
        ]
        moved = [frame_id for frame_id in current_order if frame_id in selected_ids]
        if not moved or self.count() < 2:
            self._clear_drop_indicator()
            event.ignore()
            return
        position = event.position().toPoint()
        target_row = self._target_insertion_row(position)
        selected_before = sum(
            1
            for row, frame_id in enumerate(current_order)
            if row < target_row and frame_id in selected_ids
        )
        remaining = [frame_id for frame_id in current_order if frame_id not in selected_ids]
        insertion = max(0, min(len(remaining), target_row - selected_before))
        reordered = tuple(remaining[:insertion] + moved + remaining[insertion:])
        if reordered != tuple(current_order):
            # The domain event rebuilds this list.  Defer it until Qt has
            # completed the drag gesture so the live drag items stay valid.
            QTimer.singleShot(0, lambda order=reordered: self.reorder_requested.emit(order))
        self._clear_drop_indicator()
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self._drop_indicator_index = self._target_insertion_row(
            event.position().toPoint()
        )
        self.viewport().update()
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._clear_drop_indicator()
        super().dragLeaveEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        index = self._drop_indicator_index
        if index is None or self.count() == 0:
            return
        if index >= self.count():
            rect = self.visualItemRect(self.item(self.count() - 1))
            x = min(self.viewport().width() - 2, rect.right() + 2)
        else:
            rect = self.visualItemRect(self.item(index))
            x = max(1, rect.left() - 2)
        painter = QPainter(self.viewport())
        painter.setPen(QPen(QColor("#ff8a00"), 3))
        painter.drawLine(x, rect.top() + 3, x, rect.bottom() - 3)
        painter.end()

    def _clear_drop_indicator(self) -> None:
        if self._drop_indicator_index is None:
            return
        self._drop_indicator_index = None
        self.viewport().update()


class SpriteFramePanel(QWidget):
    frame_selected = Signal(object)
    frame_clicked = Signal(object)
    new_requested = Signal()
    delete_requested = Signal(object)
    rename_requested = Signal(object, str)
    move_requested = Signal(object, int)
    reorder_requested = Signal(object, str)
    export_requested = Signal(object)
    view_size_changed = Signal(int)
    frame_activated = Signal(object)
    duplicate_requested = Signal(object)
    detach_requested = Signal(object)
    reset_requested = Signal(object)
    project_selected = Signal(int)
    group_selected = Signal(object)
    group_create_requested = Signal(str)
    group_rename_requested = Signal(object, str)
    group_move_requested = Signal(object, int)
    group_delete_requested = Signal(object)
    frame_group_move_requested = Signal(object, object, int)
    animation_open_requested = Signal(object)
    animation_fps_requested = Signal(object, int)
    animation_mode_requested = Signal(object, object)

    FRAME_ID_ROLE = int(Qt.ItemDataRole.UserRole)

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._document: Document | None = None
        self._snapshot: DocumentSnapshot | None = None
        self._thumbnail_generation = 0
        self._thumbnail_size = settings.sprite_thumbnail_size()
        self._thumbnail_cache: dict[UUID, QPixmap] = {}
        self._pending_thumbnail_ids: set[UUID] = set()
        self._active_group_id: UUID | None | str = "all"
        self.project_tabs = QTabBar(self)
        self.project_tabs.setExpanding(False)
        self.project_tabs.currentChanged.connect(self.project_selected.emit)
        self.group_tabs = QTabBar(self)
        self.group_tabs.setExpanding(False)
        self.group_tabs.setMovable(True)
        self.group_tabs.setAcceptDrops(True)
        self.group_tabs.installEventFilter(self)
        self._syncing_animation = False
        self.group_tabs.currentChanged.connect(self._group_tab_changed)
        self.group_tabs.tabMoved.connect(self._group_tab_moved)
        self.group_tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.group_tabs.customContextMenuRequested.connect(self._show_group_context_menu)
        self.animation_controls = QWidget(self)
        animation_controls_layout = QHBoxLayout(self.animation_controls)
        animation_controls_layout.setContentsMargins(0, 0, 0, 0)
        animation_controls_layout.setSpacing(4)
        self.animation_fps = DragValueSpinBox(
            1,
            60,
            12,
            manual_minimum=1,
            manual_maximum=240,
            suffix=" FPS",
            parent=self.animation_controls,
        )
        self.animation_mode = BlenderComboBox(self.animation_controls)
        for mode, label in (
            (SpritePlaybackMode.ONCE, "单次"),
            (SpritePlaybackMode.LOOP, "循环"),
            (SpritePlaybackMode.PING_PONG, "往返"),
        ):
            self.animation_mode.addItem(label, mode)
        self.animation_duration = QLabel("0.00 s", self.animation_controls)
        self.open_animation_button = QPushButton("打开动画预览", self.animation_controls)
        animation_controls_layout.addWidget(QLabel("FPS", self.animation_controls))
        animation_controls_layout.addWidget(self.animation_fps)
        animation_controls_layout.addWidget(QLabel("模式", self.animation_controls))
        animation_controls_layout.addWidget(self.animation_mode)
        animation_controls_layout.addWidget(QLabel("总时长", self.animation_controls))
        animation_controls_layout.addWidget(self.animation_duration)
        animation_controls_layout.addStretch(1)
        animation_controls_layout.addWidget(self.open_animation_button)
        self.animation_fps.editingFinished.connect(self._animation_fps_finished)
        self.animation_mode.currentIndexChanged.connect(self._animation_mode_changed)
        self.open_animation_button.clicked.connect(self._open_active_animation)
        self.list = _SpriteFrameList(self)
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setFlow(QListWidget.Flow.LeftToRight)
        self.list.setWrapping(True)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setUniformItemSizes(True)
        self.list.setSpacing(2)
        self.list.setWordWrap(False)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setSelectionRectVisible(True)
        self.list.setDragEnabled(True)
        self.list.setAcceptDrops(True)
        self.list.viewport().setAcceptDrops(True)
        self.list.setDragDropOverwriteMode(False)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setDropIndicatorShown(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.currentItemChanged.connect(self._selection_changed)
        self.list.itemClicked.connect(
            lambda item: self.frame_clicked.emit(item.data(self.FRAME_ID_ROLE))
        )
        self.list.itemDoubleClicked.connect(
            lambda item: self.frame_activated.emit(item.data(self.FRAME_ID_ROLE))
        )
        self.list.itemChanged.connect(self._item_changed)
        self.list.move_requested.connect(self.move_requested.emit)
        self.list.reorder_requested.connect(
            lambda order: self.reorder_requested.emit(order, "拖动排序精灵帧")
        )
        self.list.itemSelectionChanged.connect(self._update_actions)
        self.list.customContextMenuRequested.connect(self._show_context_menu)
        self.list.verticalScrollBar().valueChanged.connect(
            lambda _value: self._schedule_visible_thumbnails()
        )
        self.list.layout_changed.connect(self._schedule_visible_thumbnails)
        self.rename_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F2), self.list)
        self.rename_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.rename_shortcut.activated.connect(self._rename_current)
        self.delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.list)
        self.delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.delete_shortcut.activated.connect(self._delete_current)

        self.new_button = QPushButton("新帧", self)
        self.delete_button = QPushButton("删除", self)
        self.export_button = QPushButton("导出当前帧 PNG", self)
        self.sort_button = QPushButton("自动排序", self)
        self.sort_menu = QMenu(self.sort_button)
        self._add_sort_action("按名称升序", "name_ascending")
        self._add_sort_action("按名称降序", "name_descending")
        self._add_sort_action("按区域位置", "region")
        self._add_sort_action("反转当前顺序", "reverse")
        self.sort_button.setMenu(self.sort_menu)
        self.view_size_combo = BlenderComboBox(self)
        for size, label in zip(SPRITE_THUMBNAIL_SIZES, ("小", "中", "大"), strict=True):
            self.view_size_combo.addItem(label, size)
        self.view_size_combo.setCurrentIndex(
            self.view_size_combo.findData(self._thumbnail_size)
        )
        self.view_size_combo.currentIndexChanged.connect(self._view_size_selected)
        self.new_button.clicked.connect(self.new_requested.emit)
        self.delete_button.clicked.connect(self._delete_current)
        self.export_button.clicked.connect(self._export_current)
        self.controls = QWidget(self)
        self.controls.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        buttons = QHBoxLayout(self.controls)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(4)
        buttons.addWidget(self.new_button)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.sort_button)
        buttons.addStretch(1)
        buttons.addWidget(QLabel("视图", self))
        buttons.addWidget(self.view_size_combo)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(3)
        layout.addWidget(self.project_tabs)
        layout.addWidget(self.group_tabs)
        layout.addWidget(self.animation_controls)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.controls)
        self._apply_thumbnail_metrics()
        self.set_document(None)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.group_tabs and event.type() is QEvent.Type.MouseButtonDblClick:
            position_getter = getattr(event, "position", None)
            if callable(position_getter):
                index = self.group_tabs.tabAt(position_getter().toPoint())
                animation_id = self.group_tabs.tabData(index)
                if isinstance(animation_id, UUID):
                    self.animation_open_requested.emit(animation_id)
                    return True
        if watched is self.group_tabs and event.type() in {
            QEvent.Type.DragEnter,
            QEvent.Type.DragMove,
            QEvent.Type.Drop,
            QEvent.Type.DragLeave,
        }:
            if self.list.currentRow() < 0:
                return False
            if event.type() is QEvent.Type.DragLeave:
                return True
            position_getter = getattr(event, "position", None)
            if not callable(position_getter):
                return False
            index = self.group_tabs.tabAt(position_getter().toPoint())
            target = self.group_tabs.tabData(index)
            if target == "create" or target == "all" or index < 0:
                return False
            event.accept()
            if event.type() is QEvent.Type.Drop:
                frame_ids = self.selected_frame_ids
                if frame_ids:
                    insert_at = 0
                    if isinstance(target, UUID) and self._document is not None:
                        try:
                            insert_at = len(
                                self._document.sprite_workspace.animation_by_id(target).frame_ids
                            )
                        except ValueError:
                            return True
                    self.frame_group_move_requested.emit(frame_ids, target, insert_at)
            return True
        return super().eventFilter(watched, event)

    @property
    def selected_frame_id(self) -> UUID | None:
        item = self.list.currentItem()
        value = None if item is None else item.data(self.FRAME_ID_ROLE)
        return value if isinstance(value, UUID) else None

    @property
    def selected_frame_ids(self) -> tuple[UUID, ...]:
        selected = {
            item.data(self.FRAME_ID_ROLE) for item in self.list.selectedItems()
        }
        return tuple(
            frame_id
            for row in range(self.list.count())
            if isinstance(
                frame_id := self.list.item(row).data(self.FRAME_ID_ROLE),
                UUID,
            )
            and frame_id in selected
        )

    def set_document(self, document: Document | None) -> None:
        if document is not self._document:
            self._thumbnail_cache.clear()
            self._pending_thumbnail_ids.clear()
        self._document = document
        self.setEnabled(document is not None)
        self._refresh_group_tabs()
        self.refresh()

    def handle_workspace_change(self, event: SpriteWorkspaceChanged) -> None:
        """Refresh metadata without blanking stable thumbnail content."""
        self._refresh_group_tabs()
        self.refresh()
        if event.change == "cell_size":
            self.invalidate_thumbnails()
        elif event.change in {
            "updated",
            "detached",
            "reset_to_source",
            "duplicated",
            "frame_duration",
        }:
            self.invalidate_thumbnails(event.frame_ids)

    def set_projects(self, labels: tuple[str, ...], active_index: int) -> None:
        self.project_tabs.blockSignals(True)
        try:
            while self.project_tabs.count():
                self.project_tabs.removeTab(0)
            for label in labels:
                self.project_tabs.addTab(label)
            if 0 <= active_index < self.project_tabs.count():
                self.project_tabs.setCurrentIndex(active_index)
        finally:
            self.project_tabs.blockSignals(False)

    @property
    def active_group_id(self) -> UUID | None | str:
        return self._active_group_id

    def set_active_group(self, group_id: UUID | None | str) -> None:
        for index in range(self.group_tabs.count()):
            if self.group_tabs.tabData(index) == group_id:
                self.group_tabs.setCurrentIndex(index)
                return

    def _refresh_group_tabs(self) -> None:
        previous = self._active_group_id
        self.group_tabs.blockSignals(True)
        try:
            while self.group_tabs.count():
                self.group_tabs.removeTab(0)
            index = self.group_tabs.addTab("全部")
            self.group_tabs.setTabData(index, "all")
            index = self.group_tabs.addTab("未分组")
            self.group_tabs.setTabData(index, None)
            if self._document is not None:
                for group in self._document.sprite_workspace.animations:
                    index = self.group_tabs.addTab(group.name)
                    self.group_tabs.setTabData(index, group.id)
            index = self.group_tabs.addTab("+")
            self.group_tabs.setTabData(index, "create")
            target = next(
                (
                    item
                    for item in range(self.group_tabs.count())
                    if self.group_tabs.tabData(item) == previous
                ),
                0,
            )
            self.group_tabs.setCurrentIndex(target)
            self._active_group_id = self.group_tabs.tabData(target)
        finally:
            self.group_tabs.blockSignals(False)
        self._refresh_animation_controls()

    def _group_tab_changed(self, index: int) -> None:
        value = self.group_tabs.tabData(index)
        if value == "create":
            name, accepted = QInputDialog.getText(self, "新建动画", "名称")
            if accepted and name.strip():
                self.group_create_requested.emit(name.strip())
            self.set_active_group(self._active_group_id)
            return
        self._active_group_id = value
        self.group_selected.emit(value)
        self._refresh_animation_controls()
        self.refresh()

    def _group_tab_moved(self, _source: int, target: int) -> None:
        group_id = self.group_tabs.tabData(target)
        if isinstance(group_id, UUID):
            self.group_move_requested.emit(group_id, max(target - 2, 0))

    def _show_group_context_menu(self, position: QPoint) -> None:
        index = self.group_tabs.tabAt(position)
        group_id = self.group_tabs.tabData(index)
        if not isinstance(group_id, UUID):
            return
        menu = QMenu(self)
        open_preview = menu.addAction("打开动画预览")
        menu.addSeparator()
        rename = menu.addAction("重命名")
        delete = menu.addAction("删除")
        chosen = menu.exec(self.group_tabs.mapToGlobal(position))
        if chosen is open_preview:
            self.animation_open_requested.emit(group_id)
        elif chosen is rename:
            name, accepted = QInputDialog.getText(
                self,
                "重命名动画",
                "名称",
                text=self.group_tabs.tabText(index),
            )
            if accepted and name.strip():
                self.group_rename_requested.emit(group_id, name.strip())
        elif chosen is delete:
            self.group_delete_requested.emit(group_id)

    def _active_animation(self) -> SpriteAnimation | None:
        if self._document is None or not isinstance(self._active_group_id, UUID):
            return None
        try:
            return self._document.sprite_workspace.animation_by_id(
                self._active_group_id
            )
        except ValueError:
            return None

    def _refresh_animation_controls(self) -> None:
        animation = self._active_animation()
        self.animation_controls.setVisible(animation is not None)
        if animation is None or self._document is None:
            return
        self._syncing_animation = True
        try:
            self.animation_fps.syncValue(animation.fps)
            index = self.animation_mode.findData(animation.playback_mode)
            if index >= 0:
                self.animation_mode.setCurrentIndex(index)
            by_id = {
                frame.id: frame
                for frame in self._document.sprite_workspace.frames
            }
            ticks = sum(by_id[frame_id].duration_ticks for frame_id in animation.frame_ids)
            self.animation_duration.setText(f"{ticks / animation.fps:.2f} s")
            self.open_animation_button.setEnabled(bool(animation.frame_ids))
        finally:
            self._syncing_animation = False

    def _animation_fps_finished(self) -> None:
        animation = self._active_animation()
        if animation is not None and not self._syncing_animation:
            self.animation_fps_requested.emit(animation.id, self.animation_fps.value())

    def _animation_mode_changed(self, index: int) -> None:
        animation = self._active_animation()
        mode = self.animation_mode.itemData(index)
        if animation is None or self._syncing_animation:
            return
        try:
            resolved = SpritePlaybackMode(str(mode))
        except ValueError:
            return
        self.animation_mode_requested.emit(animation.id, resolved)

    def _open_active_animation(self) -> None:
        animation = self._active_animation()
        if animation is not None:
            self.animation_open_requested.emit(animation.id)

    def refresh(self, *, preserve_selection: bool = True) -> None:
        selected_ids = set(self.selected_frame_ids) if preserve_selection else set()
        current_id = self.selected_frame_id if preserve_selection else None
        self._thumbnail_generation += 1
        self._snapshot = (
            None if self._document is None else DocumentSnapshot.capture(self._document)
        )
        self.list.blockSignals(True)
        self.list.clear()
        if self._document is not None:
            workspace = self._document.sprite_workspace
            if self._active_group_id == "all":
                frames = workspace.frames
            elif self._active_group_id is None:
                ungrouped = set(workspace.ungrouped_frame_ids)
                frames = [frame for frame in workspace.frames if frame.id in ungrouped]
            elif isinstance(self._active_group_id, UUID):
                try:
                    group = workspace.animation_by_id(self._active_group_id)
                except ValueError:
                    frames = []
                else:
                    by_id = {frame.id: frame for frame in workspace.frames}
                    frames = [by_id[frame_id] for frame_id in group.frame_ids]
            else:
                frames = []
            for frame in frames:
                item = QListWidgetItem(frame.name)
                item.setData(self.FRAME_ID_ROLE, frame.id)
                item.setToolTip(
                    f"{frame.name}\n{frame.region.x}, {frame.region.y} · "
                    f"{frame.region.width} × {frame.region.height}"
                )
                item.setFlags(
                    item.flags()
                    | Qt.ItemFlag.ItemIsEditable
                    | Qt.ItemFlag.ItemIsDragEnabled
                    | Qt.ItemFlag.ItemIsDropEnabled
                )
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom
                )
                item.setSizeHint(self.list.gridSize())
                cached = self._thumbnail_cache.get(frame.id)
                if cached is not None:
                    item.setIcon(cached)
                else:
                    self._pending_thumbnail_ids.add(frame.id)
                self.list.addItem(item)
                if frame.id in selected_ids:
                    item.setSelected(True)
                if frame.id == current_id:
                    self.list.setCurrentItem(
                        item,
                        QItemSelectionModel.SelectionFlag.NoUpdate,
                    )
            valid_ids = {frame.id for frame in workspace.frames}
            self._thumbnail_cache = {
                frame_id: pixmap
                for frame_id, pixmap in self._thumbnail_cache.items()
                if frame_id in valid_ids
            }
            self._pending_thumbnail_ids.intersection_update(valid_ids)
        self.list.blockSignals(False)
        self._update_actions()
        self._refresh_animation_controls()
        self._schedule_visible_thumbnails()

    def select_frame(self, frame_id: UUID | None) -> None:
        if frame_id is None:
            self.list.clearSelection()
            self.list.setCurrentRow(-1)
            return
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.data(self.FRAME_ID_ROLE) == frame_id:
                self.list.setCurrentItem(item)
                self.list.scrollToItem(item)
                return

    def invalidate_thumbnails(
        self,
        frame_ids: tuple[UUID, ...] | None = None,
    ) -> None:
        self._thumbnail_generation += 1
        self._snapshot = (
            None if self._document is None else DocumentSnapshot.capture(self._document)
        )
        targets = (
            set(frame_ids)
            if frame_ids is not None
            else {
                self.list.item(index).data(self.FRAME_ID_ROLE)
                for index in range(self.list.count())
            }
        )
        self._pending_thumbnail_ids.update(
            frame_id for frame_id in targets if isinstance(frame_id, UUID)
        )
        self._schedule_visible_thumbnails()

    def _schedule_visible_thumbnails(self) -> None:
        generation = self._thumbnail_generation
        QTimer.singleShot(0, lambda: self._generate_next_visible(generation))

    def _generate_next_visible(self, generation: int) -> None:
        if generation != self._thumbnail_generation or self._snapshot is None:
            return
        viewport_rect = self.list.viewport().rect()
        center = viewport_rect.center()
        candidates = sorted(
            range(self.list.count()),
            key=lambda index: (
                self.list.visualItemRect(self.list.item(index)).center() - center
            ).manhattanLength(),
        )
        for index in candidates:
            item = self.list.item(index)
            frame_id = item.data(self.FRAME_ID_ROLE)
            if (
                frame_id not in self._pending_thumbnail_ids
                or not self.list.visualItemRect(item).intersects(viewport_rect)
            ):
                continue
            frame = self._frame(frame_id)
            if frame is None:
                return
            if frame.content_mode is SpriteFrameContentMode.LINKED:
                render_snapshot = self._snapshot
                region = frame.region
            else:
                embedded_snapshot = next(
                    (
                        snapshot
                        for snapshot in self._snapshot.embedded_documents
                        if snapshot.document_id == frame.embedded_document_id
                    ),
                    None,
                )
                if embedded_snapshot is None:
                    return
                render_snapshot = embedded_snapshot
                region = Rect(0, 0, frame.region.width, frame.region.height)
            pixels = render_snapshot_region(
                render_snapshot,
                region,
                CancellationToken(),
                lambda _done, _total, _message: None,
            )
            image = QImage(
                pixels.data,
                pixels.shape[1],
                pixels.shape[0],
                pixels.strides[0],
                QImage.Format.Format_RGBA8888,
            ).copy()
            pixmap = QPixmap.fromImage(image).scaled(
                self._thumbnail_size,
                self._thumbnail_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            if frame.duration_ticks > 1:
                badge = QPixmap(pixmap)
                painter = QPainter(badge)
                painter.setPen(QColor("#ffffff"))
                painter.fillRect(
                    max(0, badge.width() - 29),
                    0,
                    29,
                    17,
                    QColor(0, 0, 0, 180),
                )
                painter.drawText(
                    badge.rect().adjusted(0, 1, -3, 0),
                    Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                    f"×{frame.duration_ticks}",
                )
                painter.end()
                pixmap = badge
            self._thumbnail_cache[frame.id] = pixmap
            self._pending_thumbnail_ids.discard(frame.id)
            item.setIcon(pixmap)
            QTimer.singleShot(0, lambda: self._generate_next_visible(generation))
            return

    def _frame(self, frame_id: UUID) -> SpriteFrameDefinition | None:
        if self._document is None:
            return None
        try:
            return self._document.sprite_workspace.frame_by_id(frame_id)
        except ValueError:
            return None

    def _selection_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        self._update_actions()
        self.frame_selected.emit(
            None if current is None else current.data(self.FRAME_ID_ROLE)
        )

    def _item_changed(self, item: QListWidgetItem) -> None:
        frame_id = item.data(self.FRAME_ID_ROLE)
        text = item.text().strip()
        QTimer.singleShot(0, lambda: self.rename_requested.emit(frame_id, text))

    def _add_sort_action(self, label: str, mode: str) -> None:
        action = self.sort_menu.addAction(label)
        action.triggered.connect(lambda _checked=False: self._request_sort(mode))

    def _request_sort(self, mode: str) -> None:
        if self._document is None or self.list.count() < 2:
            return
        frames_by_id = {
            frame.id: frame for frame in self._document.sprite_workspace.frames
        }
        # Preserve the order currently presented by the active group.  In
        # particular, "reverse" must reverse the group's own order rather than
        # the source workspace's global frame order.
        frames = [
            frames_by_id[frame_id]
            for index in range(self.list.count())
            if (frame_id := self.list.item(index).data(self.FRAME_ID_ROLE))
            in frames_by_id
        ]
        if mode == "name_ascending":
            frames.sort(key=lambda frame: self._natural_name_key(frame.name))
            label = "按名称排序精灵帧"
        elif mode == "name_descending":
            frames.sort(key=lambda frame: self._natural_name_key(frame.name), reverse=True)
            label = "按名称倒序精灵帧"
        elif mode == "region":
            frames.sort(
                key=lambda frame: (
                    frame.region.y,
                    frame.region.x,
                    self._natural_name_key(frame.name),
                )
            )
            label = "按区域排序精灵帧"
        elif mode == "reverse":
            frames.reverse()
            label = "反转精灵帧顺序"
        else:
            return
        self.reorder_requested.emit(tuple(frame.id for frame in frames), label)

    @staticmethod
    def _natural_name_key(name: str) -> tuple[object, ...]:
        return tuple(
            (0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in re.split(r"(\d+)", name)
        )

    def _show_context_menu(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        if item is None:
            return
        if not item.isSelected():
            self.list.clearSelection()
            self.list.setCurrentItem(item)
        menu = QMenu(self)
        duplicate_action = menu.addAction("复制")
        frame = self._frame(item.data(self.FRAME_ID_ROLE))
        content_action = menu.addAction(
            "独立化并编辑"
            if frame is not None and frame.content_mode is SpriteFrameContentMode.LINKED
            else "重置为源区域"
        )
        menu.addSeparator()
        previous_action = menu.addAction("前移")
        next_action = menu.addAction("后移")
        rename_action = menu.addAction("重命名\tF2")
        menu.addSeparator()
        delete_action = menu.addAction("删除")
        selected_ids = self.selected_frame_ids
        selected_rows = [
            self.list.row(selected_item) for selected_item in self.list.selectedItems()
        ]
        previous_action.setEnabled(bool(selected_rows) and min(selected_rows) > 0)
        next_action.setEnabled(
            bool(selected_rows) and max(selected_rows) + 1 < self.list.count()
        )
        chosen = menu.exec(self.list.viewport().mapToGlobal(position))
        frame_id = item.data(self.FRAME_ID_ROLE)
        if chosen is duplicate_action:
            self.duplicate_requested.emit(frame_id)
        elif chosen is content_action and frame is not None:
            if frame.content_mode is SpriteFrameContentMode.LINKED:
                self.detach_requested.emit(frame_id)
            else:
                self.reset_requested.emit(frame_id)
        elif chosen is previous_action:
            self._move_selected_by(-1)
        elif chosen is next_action:
            self._move_selected_by(1)
        elif chosen is rename_action:
            self.list.editItem(item)
        elif chosen is delete_action:
            self.delete_requested.emit(selected_ids)

    def _move_selected_by(self, delta: int) -> None:
        selected = set(self.selected_frame_ids)
        order = [
            self.list.item(row).data(self.FRAME_ID_ROLE) for row in range(self.list.count())
        ]
        indices = range(1, len(order)) if delta < 0 else range(len(order) - 2, -1, -1)
        for index in indices:
            neighbor = index + delta
            if order[index] in selected and order[neighbor] not in selected:
                order[index], order[neighbor] = order[neighbor], order[index]
        self.reorder_requested.emit(tuple(order), "移动精灵帧")

    def _rename_current(self) -> None:
        item = self.list.currentItem()
        if item is not None:
            self.list.editItem(item)

    def _view_size_selected(self, index: int) -> None:
        size = self.view_size_combo.itemData(index)
        if not isinstance(size, int) or size == self._thumbnail_size:
            return
        self._thumbnail_size = size
        self._settings.set_sprite_thumbnail_size(size)
        self._settings.sync()
        self._apply_thumbnail_metrics()
        self.invalidate_thumbnails()
        self.view_size_changed.emit(size)

    def _apply_thumbnail_metrics(self) -> None:
        grid = QSize(self._thumbnail_size + 24, self._thumbnail_size + 34)
        self.list.setIconSize(QSize(self._thumbnail_size, self._thumbnail_size))
        self.list.setGridSize(grid)
        self.list.setMinimumHeight(grid.height() + 8)
        self.list.setMaximumHeight(16_777_215)
        self.list.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        for index in range(self.list.count()):
            self.list.item(index).setSizeHint(grid)

    def preferred_dock_height(self) -> int:
        """Compact height with one full thumbnail row and a minimal control row."""
        return self.list.gridSize().height() + 58

    def _delete_current(self) -> None:
        frame_ids = self.selected_frame_ids
        if frame_ids:
            self.delete_requested.emit(frame_ids)

    def _export_current(self) -> None:
        frame_id = self.selected_frame_id
        if frame_id is not None:
            self.export_requested.emit(frame_id)

    def _update_actions(self) -> None:
        selected = bool(self.selected_frame_ids)
        self.delete_button.setEnabled(selected)
        self.export_button.setEnabled(len(self.selected_frame_ids) == 1)


__all__ = ["SpriteFramePanel"]
