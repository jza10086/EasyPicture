"""Top-to-bottom layer list and undoable layer property controls."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QMimeData,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    Qt,
)
from PySide6.QtGui import QFocusEvent, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QListView,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from easypicture.commands import DocumentCommandService
from easypicture.domain import (
    ActiveLayerChanged,
    Document,
    DocumentEvent,
    LayerListChanged,
    LayerPropertiesChanged,
    RasterLayer,
)
from easypicture.ui.drag_value_spinbox import DragValueSpinBox

LAYER_MIME_TYPE = "application/x-easypicture-layer-id"
_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex


class LayerListModel(QAbstractListModel):
    LayerIdRole = Qt.ItemDataRole.UserRole + 1
    OpacityRole = Qt.ItemDataRole.UserRole + 2
    LockedRole = Qt.ItemDataRole.UserRole + 3
    AlphaLockedRole = Qt.ItemDataRole.UserRole + 4

    def __init__(
        self,
        commands: DocumentCommandService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands = commands
        self._document: Document | None = None

    @property
    def document(self) -> Document | None:
        return self._document

    def set_document(self, document: Document | None) -> None:
        self.beginResetModel()
        self._document = document
        self.endResetModel()

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        if parent.isValid() or self._document is None:
            return 0
        return len(self._document.layers)

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        layer = self.layer_for_index(index)
        if layer is None:
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return layer.name
        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if layer.visible else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole:
            locks = []
            if layer.locked:
                locks.append("已锁定")
            if layer.alpha_locked:
                locks.append("透明像素已锁定")
            suffix = f"；{'，'.join(locks)}" if locks else ""
            return f"不透明度 {layer.opacity * 100:.0f}%{suffix}"
        if role == self.LayerIdRole:
            return layer.id
        if role == self.OpacityRole:
            return layer.opacity
        if role == self.LockedRole:
            return layer.locked
        if role == self.AlphaLockedRole:
            return layer.alpha_locked
        return None

    def setData(
        self,
        index: ModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        layer = self.layer_for_index(index)
        if layer is None:
            return False
        if role == Qt.ItemDataRole.EditRole:
            return self.commands.rename_layer(layer.id, str(value))
        if role == Qt.ItemDataRole.CheckStateRole:
            check_value = getattr(value, "value", value)
            visible = check_value == Qt.CheckState.Checked.value
            return self.commands.set_layer_visible(layer.id, visible)
        return False

    def flags(self, index: ModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEditable
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsDropEnabled
        )

    def mimeTypes(self) -> list[str]:
        return [LAYER_MIME_TYPE]

    def mimeData(self, indexes: Sequence[QModelIndex]) -> QMimeData:
        mime = QMimeData()
        for index in indexes:
            layer = self.layer_for_index(index)
            if layer is not None:
                mime.setData(LAYER_MIME_TYPE, str(layer.id).encode("ascii"))
                break
        return mime

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def dropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: ModelIndex,
    ) -> bool:
        _ = column
        if action == Qt.DropAction.IgnoreAction:
            return True
        if (
            action != Qt.DropAction.MoveAction
            or self._document is None
            or not data.hasFormat(LAYER_MIME_TYPE)
        ):
            return False
        try:
            layer_id_text = bytearray(data.data(LAYER_MIME_TYPE).data()).decode("ascii")
            source_layer = next(
                layer for layer in self._document.layers if str(layer.id) == layer_id_text
            )
        except StopIteration, UnicodeDecodeError:
            return False
        source_row = self.row_for_layer(source_layer)
        destination_row = parent.row() if parent.isValid() else row
        if destination_row < 0:
            destination_row = self.rowCount()
        if source_row < destination_row:
            destination_row -= 1
        destination_row = max(0, min(destination_row, self.rowCount() - 1))
        target_index = self.rowCount() - 1 - destination_row
        return self.commands.move_layer(source_layer.id, target_index)

    def layer_for_index(self, index: ModelIndex) -> RasterLayer | None:
        if (
            self._document is None
            or not index.isValid()
            or not 0 <= index.row() < len(self._document.layers)
        ):
            return None
        return self._document.layers[len(self._document.layers) - 1 - index.row()]

    def row_for_layer(self, layer: RasterLayer) -> int:
        if self._document is None:
            return -1
        storage_index = self._document.layer_index(layer.id)
        return len(self._document.layers) - 1 - storage_index

    def handle_event(self, event: DocumentEvent) -> None:
        if isinstance(event, LayerListChanged):
            self.beginResetModel()
            self.endResetModel()
            return
        if isinstance(event, LayerPropertiesChanged) and self._document is not None:
            try:
                layer = self._document.layer_by_id(event.layer_id)
            except ValueError:
                return
            row = self.row_for_layer(layer)
            index = self.index(row, 0)
            self.dataChanged.emit(index, index)


class LayerItemDelegate(QStyledItemDelegate):
    def visibility_rect(self, view: QListView, index: QModelIndex) -> QRect:
        option = QStyleOptionViewItem()
        option.initFrom(view.viewport())
        option.rect = view.visualRect(index)
        self.initStyleOption(option, index)
        return view.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator,
            option,
            view.viewport(),
        )

    def text_rect(self, view: QListView, index: QModelIndex) -> QRect:
        option = QStyleOptionViewItem()
        option.initFrom(view.viewport())
        option.rect = view.visualRect(index)
        self.initStyleOption(option, index)
        return view.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemText,
            option,
            view.viewport(),
        )


class LayerListView(QListView):
    """Keep visibility scrubbing separate from selection and layer reordering."""

    def __init__(
        self,
        commands: DocumentCommandService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands = commands
        self.layer_delegate = LayerItemDelegate(self)
        self.setItemDelegate(self.layer_delegate)
        self.setMouseTracking(True)
        self._visibility_scrubbing = False
        self._scrub_target_visible = False
        self._scrub_last_row = -1
        self._scrubbed_layer_ids: set[object] = set()

    @property
    def visibility_scrubbing(self) -> bool:
        return self._visibility_scrubbing

    def visibility_rect(self, index: QModelIndex) -> QRect:
        return self.layer_delegate.visibility_rect(self, index)

    def text_rect(self, index: QModelIndex) -> QRect:
        return self.layer_delegate.text_rect(self, index)

    def cancel_visibility_scrub(self) -> None:
        if not self._visibility_scrubbing:
            return
        self._visibility_scrubbing = False
        self._scrub_last_row = -1
        self._scrubbed_layer_ids.clear()
        self.commands.end_visibility_scrub()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and index.isValid():
            if self.visibility_rect(index).contains(event.position().toPoint()):
                self._begin_visibility_scrub(index)
                event.accept()
                return
            if not self.text_rect(index).contains(event.position().toPoint()):
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._visibility_scrubbing:
            if not event.buttons() & Qt.MouseButton.LeftButton:
                self.cancel_visibility_scrub()
                event.accept()
                return
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                first_row = min(self._scrub_last_row, index.row())
                last_row = max(self._scrub_last_row, index.row())
                for row in range(first_row, last_row + 1):
                    self._apply_visibility_to_row(row)
                self._scrub_last_row = index.row()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._visibility_scrubbing and event.button() == Qt.MouseButton.LeftButton:
            self.cancel_visibility_scrub()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        index = self.indexAt(event.position().toPoint())
        if (
            event.button() == Qt.MouseButton.LeftButton
            and index.isValid()
            and self.visibility_rect(index).contains(event.position().toPoint())
        ):
            self._begin_visibility_scrub(index)
            self.cancel_visibility_scrub()
            event.accept()
            return
        if (
            event.button() == Qt.MouseButton.LeftButton
            and index.isValid()
            and not self.text_rect(index).contains(event.position().toPoint())
        ):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.cancel_visibility_scrub()
        super().focusOutEvent(event)

    def _begin_visibility_scrub(self, index: QModelIndex) -> None:
        if self._visibility_scrubbing:
            return
        model = self.model()
        if not isinstance(model, LayerListModel):
            return
        layer = model.layer_for_index(index)
        if layer is None or not self.commands.begin_visibility_scrub():
            return
        self._visibility_scrubbing = True
        self._scrub_target_visible = not layer.visible
        self._scrub_last_row = index.row()
        self._scrubbed_layer_ids.clear()
        self._apply_visibility_to_row(index.row())

    def _apply_visibility_to_row(self, row: int) -> None:
        model = self.model()
        if not isinstance(model, LayerListModel):
            return
        layer = model.layer_for_index(model.index(row, 0))
        if layer is None or layer.id in self._scrubbed_layer_ids:
            return
        self._scrubbed_layer_ids.add(layer.id)
        self.commands.set_layer_visible(layer.id, self._scrub_target_visible)


class LayerPanel(QWidget):
    def __init__(
        self,
        commands: DocumentCommandService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands = commands
        self.model = LayerListModel(commands, self)
        self.list_view = LayerListView(commands, self)
        self.list_view.setObjectName("LayerList")
        self.list_view.setModel(self.model)
        self.list_view.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.list_view.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_view.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        self.new_button = QPushButton("新建", self)
        self.delete_button = QPushButton("删除", self)
        self.duplicate_button = QPushButton("复制", self)
        self.up_button = QPushButton("上移", self)
        self.down_button = QPushButton("下移", self)
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        for button in (
            self.new_button,
            self.delete_button,
            self.duplicate_button,
            self.up_button,
            self.down_button,
        ):
            button_layout.addWidget(button)

        self.opacity_spin = DragValueSpinBox(0, 100, 100, suffix="%", parent=self)
        self.opacity_spin.setObjectName("LayerOpacity")
        self.locked_check = QCheckBox("锁定图层", self)
        self.alpha_locked_check = QCheckBox("锁定透明像素", self)
        properties = QFormLayout()
        properties.addRow("不透明度", self.opacity_spin)
        properties.addRow(self.locked_check)
        properties.addRow(self.alpha_locked_check)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.list_view, 1)
        layout.addLayout(button_layout)
        layout.addLayout(properties)

        self._syncing = False
        self.list_view.selectionModel().currentChanged.connect(self._on_current_changed)
        self.new_button.clicked.connect(self.commands.create_layer)
        self.delete_button.clicked.connect(self._delete_current)
        self.duplicate_button.clicked.connect(self._duplicate_current)
        self.up_button.clicked.connect(self._move_current_up)
        self.down_button.clicked.connect(self._move_current_down)
        self.opacity_spin.editingFinished.connect(self._commit_opacity)
        self.locked_check.toggled.connect(self._commit_locked)
        self.alpha_locked_check.toggled.connect(self._commit_alpha_locked)
        self.set_document(None)

    def set_document(self, document: Document | None) -> None:
        self.list_view.cancel_visibility_scrub()
        self.model.set_document(document)
        self.setEnabled(document is not None)
        self._sync_active_layer()

    def handle_event(self, event: DocumentEvent) -> None:
        self.model.handle_event(event)
        if isinstance(event, (LayerListChanged, ActiveLayerChanged)):
            self._sync_active_layer()
        elif isinstance(event, LayerPropertiesChanged):
            document = self.model.document
            if document is not None and event.layer_id == document.active_layer_id:
                self._sync_controls()
        self._update_buttons()

    def current_layer(self) -> RasterLayer | None:
        return self.model.layer_for_index(self.list_view.currentIndex())

    def _sync_active_layer(self) -> None:
        document = self.model.document
        self._syncing = True
        try:
            if document is None:
                self.list_view.setCurrentIndex(QModelIndex())
            else:
                row = self.model.row_for_layer(document.active_layer)
                self.list_view.setCurrentIndex(self.model.index(row, 0))
            self._sync_controls()
        finally:
            self._syncing = False
        self._update_buttons()

    def _sync_controls(self) -> None:
        layer = self.current_layer()
        was_syncing = self._syncing
        self._syncing = True
        try:
            self.opacity_spin.setValue(round(layer.opacity * 100) if layer else 100)
            self.locked_check.setChecked(layer.locked if layer else False)
            self.alpha_locked_check.setChecked(layer.alpha_locked if layer else False)
        finally:
            self._syncing = was_syncing

    def _on_current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        _ = previous
        if self._syncing:
            return
        layer = self.model.layer_for_index(current)
        document = self.model.document
        if layer is not None and document is not None:
            document.set_active_layer(layer.id)
        self._sync_controls()
        self._update_buttons()

    def _delete_current(self) -> None:
        layer = self.current_layer()
        if layer is not None:
            self.commands.delete_layer(layer.id)

    def _duplicate_current(self) -> None:
        layer = self.current_layer()
        if layer is not None:
            self.commands.duplicate_layer(layer.id)

    def _move_current_up(self) -> None:
        layer = self.current_layer()
        document = self.model.document
        if layer is not None and document is not None:
            index = document.layer_index(layer.id)
            self.commands.move_layer(layer.id, index + 1)

    def _move_current_down(self) -> None:
        layer = self.current_layer()
        document = self.model.document
        if layer is not None and document is not None:
            index = document.layer_index(layer.id)
            self.commands.move_layer(layer.id, index - 1)

    def _commit_opacity(self) -> None:
        if self._syncing:
            return
        layer = self.current_layer()
        if layer is not None:
            self.commands.set_layer_opacity(layer.id, self.opacity_spin.value() / 100.0)

    def _commit_locked(self, locked: bool) -> None:
        if self._syncing:
            return
        layer = self.current_layer()
        if layer is not None:
            self.commands.set_layer_locked(layer.id, locked)

    def _commit_alpha_locked(self, locked: bool) -> None:
        if self._syncing:
            return
        layer = self.current_layer()
        if layer is not None:
            self.commands.set_layer_alpha_locked(layer.id, locked)

    def _update_buttons(self) -> None:
        document = self.model.document
        layer = self.current_layer()
        has_layer = document is not None and layer is not None
        self.new_button.setEnabled(document is not None)
        self.duplicate_button.setEnabled(has_layer)
        self.delete_button.setEnabled(
            has_layer and document is not None and len(document.layers) > 1
        )
        if not has_layer or document is None or layer is None:
            self.up_button.setEnabled(False)
            self.down_button.setEnabled(False)
            return
        index = document.layer_index(layer.id)
        self.up_button.setEnabled(index < len(document.layers) - 1)
        self.down_button.setEnabled(index > 0)


__all__ = [
    "LAYER_MIME_TYPE",
    "LayerItemDelegate",
    "LayerListModel",
    "LayerListView",
    "LayerPanel",
]
