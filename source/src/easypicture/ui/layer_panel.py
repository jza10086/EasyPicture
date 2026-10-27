"""Top-to-bottom layer list and undoable layer property controls."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID

import numpy as np
from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractListModel,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFocusEvent,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListView,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from easypicture.commands import DocumentCommandService
from easypicture.diagnostics import performance_counters
from easypicture.domain import (
    ActiveLayerChanged,
    AffineTransform,
    Collection,
    Document,
    DocumentEvent,
    DocumentNode,
    DocumentSizeChanged,
    FloatRect,
    GroupListChanged,
    GroupMembersChanged,
    GroupPropertiesChanged,
    LayerPixelsChanged,
    LayerPropertiesChanged,
    Point,
    RasterObject,
    SvgObject,
    TextObject,
    VectorObject,
    VectorObjectChanged,
    walk_nodes,
)
from easypicture.render import paint_vector_objects, rgba_tile_to_qimage
from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.property_binding import (
    EditTarget,
    PropertyEditSession,
    interaction_coordinator,
)
from easypicture.ui.selection_state import SelectionCoordinator, SelectionState

LAYER_MIME_TYPE = "application/x-easypicture-layer-id"
LAYER_THUMBNAIL_SIZE = 48
_THUMBNAIL_CONTENT_SIZE = 44
_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex
type ThumbnailKey = tuple[object, ...]


def _render_layer_thumbnail(
    layer: RasterObject,
    document_width: int,
    document_height: int,
) -> QIcon:
    performance_counters.thumbnails += 1
    scale = min(
        _THUMBNAIL_CONTENT_SIZE / document_width,
        _THUMBNAIL_CONTENT_SIZE / document_height,
    )
    content_width = max(1, round(document_width * scale))
    content_height = max(1, round(document_height * scale))
    sampled = np.zeros((content_height, content_width, 4), dtype=np.uint8)
    tile_size = layer.pixels.tile_size
    inverse = layer.world_transform.inverse()
    for target_y in range(content_height):
        for target_x in range(content_width):
            point = inverse.map_point(
                Point(
                    (target_x + 0.5) * document_width / content_width,
                    (target_y + 0.5) * document_height / content_height,
                )
            )
            if not 0 <= point.x < layer.size.width or not 0 <= point.y < layer.size.height:
                continue
            local_x, local_y = int(point.x), int(point.y)
            tile = layer.pixels.read_tile(local_x // tile_size, local_y // tile_size)
            sampled[target_y, target_x] = tile[local_y % tile_size, local_x % tile_size]

    thumbnail = QImage(
        LAYER_THUMBNAIL_SIZE,
        LAYER_THUMBNAIL_SIZE,
        QImage.Format.Format_RGBA8888_Premultiplied,
    )
    thumbnail.fill(QColor("#1b1b1b"))
    left = (LAYER_THUMBNAIL_SIZE - content_width) // 2
    top = (LAYER_THUMBNAIL_SIZE - content_height) // 2
    painter = QPainter(thumbnail)
    checker_size = 4
    for y in range(0, content_height, checker_size):
        for x in range(0, content_width, checker_size):
            shade = 104 if (x // checker_size + y // checker_size) % 2 == 0 else 74
            painter.fillRect(
                left + x,
                top + y,
                min(checker_size, content_width - x),
                min(checker_size, content_height - y),
                QColor(shade, shade, shade),
            )
    painter.drawImage(left, top, rgba_tile_to_qimage(sampled))
    painter.setPen(QPen(QColor("#151515"), 1.0))
    painter.drawRect(left, top, content_width - 1, content_height - 1)
    painter.end()
    return QIcon(QPixmap.fromImage(thumbnail))


def _render_raster_group_thumbnail(
    group: Collection,
    document_width: int,
    document_height: int,
) -> QIcon:
    """Sample a mixed subtree in its authoritative order with inherited opacity."""
    performance_counters.thumbnails += 1
    image = QImage(
        LAYER_THUMBNAIL_SIZE, LAYER_THUMBNAIL_SIZE, QImage.Format.Format_RGBA8888_Premultiplied
    )
    image.fill(QColor("#1b1b1b"))
    painter = QPainter(image)
    scale = min(
        _THUMBNAIL_CONTENT_SIZE / max(1, document_width),
        _THUMBNAIL_CONTENT_SIZE / max(1, document_height),
    )
    painter.translate(
        (LAYER_THUMBNAIL_SIZE - document_width * scale) / 2,
        (LAYER_THUMBNAIL_SIZE - document_height * scale) / 2,
    )
    painter.scale(scale, scale)

    def draw(node: DocumentNode, opacity: float) -> None:
        if not node.visible or node.opacity <= 0:
            return
        if isinstance(node, Collection):
            for child in node.children:
                draw(child, opacity * node.opacity)
        elif isinstance(node, RasterObject):
            from easypicture.render.raster_renderer import paint_raster_object

            paint_raster_object(painter, node, opacity=opacity * node.opacity)
        else:
            paint_vector_objects(painter, (node,), group_opacity=opacity)

    draw(group, 1.0)
    painter.end()
    return QIcon(QPixmap.fromImage(image))


def _render_text_thumbnail(
    objects: Sequence[VectorObject],
    document_width: int,
    document_height: int,
    *,
    group_opacity: float = 1.0,
    normalize_single: bool = False,
) -> QIcon:
    thumbnail = QImage(
        LAYER_THUMBNAIL_SIZE,
        LAYER_THUMBNAIL_SIZE,
        QImage.Format.Format_RGBA8888_Premultiplied,
    )
    thumbnail.fill(QColor("#1b1b1b"))
    if normalize_single and len(objects) == 1 and isinstance(objects[0], TextObject):
        source_width = max(1.0, objects[0].frame.width)
        source_height = max(1.0, objects[0].frame.height)
    else:
        source_width = float(max(1, document_width))
        source_height = float(max(1, document_height))
    scale = min(
        _THUMBNAIL_CONTENT_SIZE / source_width,
        _THUMBNAIL_CONTENT_SIZE / source_height,
    )
    content_width = max(1, round(source_width * scale))
    content_height = max(1, round(source_height * scale))
    left = (LAYER_THUMBNAIL_SIZE - content_width) // 2
    top = (LAYER_THUMBNAIL_SIZE - content_height) // 2
    painter = QPainter(thumbnail)
    checker_size = 4
    for y in range(0, content_height, checker_size):
        for x in range(0, content_width, checker_size):
            shade = 104 if (x // checker_size + y // checker_size) % 2 == 0 else 74
            painter.fillRect(
                left + x,
                top + y,
                min(checker_size, content_width - x),
                min(checker_size, content_height - y),
                QColor(shade, shade, shade),
            )
    painter.save()
    painter.setClipRect(left, top, content_width, content_height)
    painter.translate(left, top)
    painter.scale(scale, scale)
    if normalize_single and len(objects) == 1 and isinstance(objects[0], TextObject):
        source = objects[0]
        normalized = replace(
            source,
            frame=FloatRect(0.0, 0.0, source.frame.width, source.frame.height),
            transform=AffineTransform(),
        )
        paint_vector_objects(painter, (normalized,), group_opacity=group_opacity)
    else:
        paint_vector_objects(painter, objects, group_opacity=group_opacity)
    painter.restore()
    painter.setPen(QPen(QColor("#151515"), 1.0))
    painter.drawRect(left, top, content_width - 1, content_height - 1)
    painter.end()
    return QIcon(QPixmap.fromImage(thumbnail))


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
        self._thumbnail_cache: OrderedDict[UUID, tuple[ThumbnailKey, QIcon]] = OrderedDict()

    @property
    def document(self) -> Document | None:
        return self._document

    def set_document(self, document: Document | None) -> None:
        self.beginResetModel()
        self._document = document
        self._thumbnail_cache.clear()
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
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbnail(layer)
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
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        node = self.layer_for_index(index)
        if node is not None and self._document and not self._document.is_locked(node.id):
            flags |= (
                Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
        return flags

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

    def layer_for_index(self, index: ModelIndex) -> RasterObject | None:
        if (
            self._document is None
            or not index.isValid()
            or not 0 <= index.row() < len(self._document.layers)
        ):
            return None
        return self._document.layers[len(self._document.layers) - 1 - index.row()]

    def row_for_layer(self, layer: RasterObject) -> int:
        if self._document is None:
            return -1
        storage_index = self._document.layer_index(layer.id)
        return len(self._document.layers) - 1 - storage_index

    def handle_event(self, event: DocumentEvent) -> None:
        document = self._document
        if document is None or event.document_id != document.id:
            return
        if isinstance(event, (GroupListChanged, GroupMembersChanged)):
            self._thumbnail_cache.clear()
            self.beginResetModel()
            self.endResetModel()
            return
        if isinstance(event, DocumentSizeChanged):
            self._thumbnail_cache.clear()
            if self.rowCount() > 0:
                self.dataChanged.emit(
                    self.index(0, 0),
                    self.index(self.rowCount() - 1, 0),
                    [Qt.ItemDataRole.DecorationRole],
                )
            return
        if isinstance(event, LayerPixelsChanged) and self._document is not None:
            self._invalidate_thumbnail(event.layer_id)
            return
        if isinstance(event, LayerPropertiesChanged) and self._document is not None:
            try:
                layer = self._document.layer_by_id(event.layer_id)
            except ValueError:
                return
            if event.properties & {"size", "origin"}:
                self._thumbnail_cache.pop(layer.id, None)
            row = self.row_for_layer(layer)
            index = self.index(row, 0)
            self.dataChanged.emit(index, index)

    def _invalidate_thumbnail(self, layer_id: UUID) -> None:
        if self._document is None:
            return
        try:
            layer = self._document.layer_by_id(layer_id)
        except ValueError:
            return
        self._thumbnail_cache.pop(layer_id, None)
        row = self.row_for_layer(layer)
        index = self.index(row, 0)
        self.dataChanged.emit(
            index,
            index,
            [Qt.ItemDataRole.DecorationRole],
        )

    def _thumbnail(self, layer: RasterObject) -> QIcon:
        document = self._document
        if document is None:
            return QIcon()
        key: ThumbnailKey = (
            layer.pixels.generation,
            document.width,
            document.height,
            layer.size.width,
            layer.size.height,
            layer.origin.x,
            layer.origin.y,
            layer.transform,
        )
        cached = self._thumbnail_cache.get(layer.id)
        if cached is not None and cached[0] == key:
            self._thumbnail_cache.move_to_end(layer.id)
            return cached[1]
        icon = _render_layer_thumbnail(layer, document.width, document.height)
        self._thumbnail_cache[layer.id] = (key, icon)
        self._thumbnail_cache.move_to_end(layer.id)
        while len(self._thumbnail_cache) > 512:
            self._thumbnail_cache.popitem(last=False)
        return icon


class LayerTreeModel(QAbstractItemModel):
    """Incremental recursive object outliner, displaying frontmost objects first."""

    NodeIdRole = Qt.ItemDataRole.UserRole + 11
    NodeKindRole = Qt.ItemDataRole.UserRole + 12
    LockedRole = Qt.ItemDataRole.UserRole + 13

    def __init__(
        self,
        commands: DocumentCommandService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands = commands
        self._document: Document | None = None
        self._thumbnail_cache: OrderedDict[UUID, QIcon] = OrderedDict()
        self._root_rows: list[Any] = []
        self._child_rows: dict[UUID, list[Any]] = {}
        self._locations: dict[UUID, tuple[int, DocumentNode, UUID | None]] = {}
        self._tokens: dict[UUID, int] = {}
        self._token_ids: dict[int, UUID] = {}
        self._next_token = 1

    @property
    def document(self) -> Document | None:
        return self._document

    def set_document(self, document: Document | None) -> None:
        self.beginResetModel()
        self._document = document
        self._root_rows = list(reversed(document.nodes)) if document is not None else []
        self._child_rows = {
            node.id: list(reversed(node.children))
            for node in walk_nodes(self._root_rows)
            if isinstance(node, Collection)
        }
        self._thumbnail_cache.clear()
        self._reindex_rows()
        self.endResetModel()

    def _reindex_rows(self) -> None:
        self._locations.clear()

        def visit(rows: list[Any], parent_id: UUID | None) -> None:
            for row, node in enumerate(rows):
                self._locations[node.id] = (row, node, parent_id)
                if isinstance(node, Collection):
                    visit(self._child_rows.get(node.id, []), node.id)

        visit(self._root_rows, None)
        for node_id in tuple(self._tokens):
            if node_id not in self._locations:
                self._token_ids.pop(self._tokens.pop(node_id), None)
        for node_id in self._locations:
            if node_id not in self._tokens:
                self._tokens[node_id] = self._next_token
                self._token_ids[self._next_token] = node_id
                self._next_token += 1

    def _node_index(self, row: int, node_id: UUID) -> QModelIndex:
        # Qt does not retain Python objects supplied as internalPointer. Integer
        # tokens make stale indexes harmless after row removal or object replacement.
        token = self._tokens.get(node_id)
        return self.createIndex(row, 0, token) if token is not None else QModelIndex()

    def _roots(self) -> tuple[Any, ...]:
        return tuple(self._root_rows)

    def _children(self, node: object) -> tuple[Any, ...]:
        node_id = getattr(node, "id", None)
        return tuple(self._child_rows.get(node_id, ())) if isinstance(node_id, UUID) else ()

    def _sync_rows(self, rows: list[Any], desired: list[Any], parent: QModelIndex) -> None:
        wanted = {node.id for node in desired}
        for index in range(len(rows) - 1, -1, -1):
            if rows[index].id not in wanted:
                self.beginRemoveRows(parent, index, index)
                self._thumbnail_cache.pop(rows[index].id, None)
                rows.pop(index)
                self._reindex_rows()
                self.endRemoveRows()
        for index, node in enumerate(desired):
            source = next((i for i, old in enumerate(rows) if old.id == node.id), None)
            if source is None:
                self.beginInsertRows(parent, index, index)
                rows.insert(index, node)
                self._reindex_rows()
                self.endInsertRows()
            elif source != index:
                destination = index if source > index else index + 1
                if self.beginMoveRows(parent, source, source, parent, destination):
                    rows.insert(index, rows.pop(source))
                    self._reindex_rows()
                    self.endMoveRows()
            if rows[index] is not node:
                old_index = self._node_index(index, rows[index].id)
                rows[index] = node
                self._reindex_rows()
                new_index = self._node_index(index, node.id)
                self.changePersistentIndex(old_index, new_index)
                self._thumbnail_cache.pop(node.id, None)
                self.dataChanged.emit(new_index, new_index)

    def _sync_structure(self) -> None:
        document = self._document
        desired = list(reversed(document.nodes)) if document is not None else []
        self._sync_rows(self._root_rows, desired, QModelIndex())
        alive: set[UUID] = set()

        def sync(children: list[Any], parent: QModelIndex) -> None:
            for row, node in enumerate(children):
                if not isinstance(node, Collection):
                    continue
                alive.add(node.id)
                index = self.index(row, 0, parent)
                cached = self._child_rows.setdefault(node.id, [])
                self._sync_rows(cached, list(reversed(node.children)), index)
                sync(cached, index)

        sync(self._root_rows, QModelIndex())
        self._child_rows = {key: rows for key, rows in self._child_rows.items() if key in alive}
        self._reindex_rows()

    def index_for_id(self, node_id: UUID) -> QModelIndex:
        location = self._locations.get(node_id)
        return self._node_index(location[0], node_id) if location else QModelIndex()

    def index(
        self,
        row: int,
        column: int,
        parent: ModelIndex = _INVALID_INDEX,
    ) -> QModelIndex:
        if column != 0 or row < 0:
            return QModelIndex()
        values = (
            self._roots() if not parent.isValid() else self._children(self.node_for_index(parent))
        )
        if row >= len(values):
            return QModelIndex()
        return self._node_index(row, values[row].id)

    def parent(self, index: ModelIndex = _INVALID_INDEX) -> QModelIndex:  # type: ignore[override]
        if not index.isValid() or self._document is None:
            return QModelIndex()
        node = self.node_for_index(index)
        location = self._locations.get(node.id) if node else None
        if location and location[2] is not None:
            return self.index_for_id(location[2])
        return QModelIndex()

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        if not parent.isValid():
            return len(self._roots())
        return len(self._children(self.node_for_index(parent)))

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        _ = parent
        return 1

    def node_for_index(
        self,
        index: ModelIndex,
    ) -> DocumentNode | None:
        if not index.isValid():
            return None
        node_id = self._token_ids.get(index.internalId())
        location = self._locations.get(node_id) if node_id else None
        return location[1] if location else None

    def _node_locked(self, node: DocumentNode) -> bool:
        # During begin/end row notifications Qt still queries the old rows, while
        # the domain command has already removed them. Read the model's own tree.
        while True:
            if node.locked:
                return True
            location = self._locations.get(node.id)
            parent = self._locations.get(location[2]) if location and location[2] else None
            if parent is None:
                return False
            node = parent[1]

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        node = self.node_for_index(index)
        if node is None:
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return node.name
        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if node.visible else Qt.CheckState.Unchecked
        if role == self.NodeIdRole:
            return node.id
        if role == self.LockedRole:
            return self._node_locked(node)
        if role == self.NodeKindRole:
            if isinstance(node, Collection):
                return "collection"
            if isinstance(node, RasterObject):
                return "raster"
            return "svg" if isinstance(node, SvgObject) else "text"
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbnail(node)
        if role == Qt.ItemDataRole.ToolTipRole:
            kind = (
                "集合"
                if isinstance(node, Collection)
                else "栅格"
                if isinstance(node, RasterObject)
                else "SVG"
                if isinstance(node, SvgObject)
                else "文字"
            )
            locks = []
            if self._node_locked(node):
                locks.append("已锁定")
            if isinstance(node, RasterObject) and node.alpha_locked:
                locks.append("透明像素已锁定")
            suffix = f"；{'，'.join(locks)}" if locks else ""
            return f"{kind}；不透明度 {node.opacity * 100:.0f}%{suffix}"
        return None

    def flags(self, index: ModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        node = self.node_for_index(index)
        if node is not None and not self._node_locked(node):
            flags |= (
                Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
            if isinstance(node, Collection):
                flags |= Qt.ItemFlag.ItemIsDropEnabled
        return flags

    def mimeTypes(self) -> list[str]:
        return [LAYER_MIME_TYPE]

    def mimeData(self, indexes: Sequence[QModelIndex]) -> QMimeData:
        data = QMimeData()
        ids = []
        for index in indexes:
            node = self.node_for_index(index)
            if node is not None:
                ids.append(str(node.id))
        if ids:
            data.setData(LAYER_MIME_TYPE, "\n".join(dict.fromkeys(ids)).encode("ascii"))
        return data

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
        if action != Qt.DropAction.MoveAction or column > 0:
            return False
        document = self._document
        if document is None or not data.hasFormat(LAYER_MIME_TYPE):
            return False
        try:
            node_ids = tuple(
                UUID(value)
                for value in bytes(data.data(LAYER_MIME_TYPE).data()).decode("ascii").splitlines()
            )
            if not node_ids:
                return False
            for node_id in node_ids:
                document.node_by_id(node_id)
            destination = self.node_for_index(parent)
            if destination is not None and not isinstance(destination, Collection):
                return False
            siblings = list(document.nodes if destination is None else destination.children)
            displayed = list(reversed(siblings))
            # Qt drop row refers to the pre-removal model.
            insert_row = len(displayed) if row < 0 else min(row, len(displayed))
            insert_row -= sum(item.id in node_ids for item in displayed[:insert_row])
            displayed = [item for item in displayed if item.id not in node_ids]
            target_index = len(displayed) - insert_row
            return self.commands.objects.move(
                node_ids, None if destination is None else destination.id, target_index
            )
        except ValueError, UnicodeDecodeError:
            return False

    def setData(
        self,
        index: ModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        node = self.node_for_index(index)
        if node is None:
            return False
        if role == Qt.ItemDataRole.EditRole:
            if isinstance(node, Collection):
                return self.commands.rename_group(node.id, str(value))
            if isinstance(node, RasterObject):
                return self.commands.rename_layer(node.id, str(value))
            return self.commands.update_vector_object(node.id, name=str(value))
        if role == Qt.ItemDataRole.CheckStateRole:
            check_value = getattr(value, "value", value)
            visible = check_value == Qt.CheckState.Checked.value
            if isinstance(node, Collection):
                return self.commands.set_group_visible(node.id, visible)
            if isinstance(node, RasterObject):
                return self.commands.set_layer_visible(node.id, visible)
            return self.commands.set_text_object_visible(node.id, visible)
        return False

    def _notify_node(self, node_id: UUID, roles: list[int]) -> None:
        index = self.index_for_id(node_id)
        if index.isValid():
            self.dataChanged.emit(index, index, roles)

    def handle_event(self, event: DocumentEvent) -> None:
        document = self._document
        if document is None or event.document_id != document.id:
            return
        decoration: list[int] = [Qt.ItemDataRole.DecorationRole]
        if isinstance(event, (GroupListChanged, GroupMembersChanged)):
            self._sync_structure()
            if isinstance(event, GroupMembersChanged):
                self._thumbnail_cache.pop(event.group_id, None)
                self._notify_node(event.group_id, decoration)
            return
        if isinstance(event, DocumentSizeChanged):
            self._thumbnail_cache.clear()
            for root in self._roots():
                self._notify_node(root.id, decoration)
                for child in self._children(root):
                    self._notify_node(child.id, decoration)
            return
        if isinstance(event, LayerPixelsChanged):
            target_id = event.layer_id
            appearance = True
        elif isinstance(
            event,
            (LayerPropertiesChanged, VectorObjectChanged, GroupPropertiesChanged),
        ):
            target_id = (
                event.layer_id
                if isinstance(event, LayerPropertiesChanged)
                else event.object_id
                if isinstance(event, VectorObjectChanged)
                else event.group_id
            )
            appearance = bool(event.properties - {"name", "locked", "alpha_locked"})
        else:
            return
        if appearance:
            self._thumbnail_cache.pop(target_id, None)
        self._notify_node(target_id, [])
        if isinstance(event, GroupPropertiesChanged) and "locked" in event.properties:
            try:
                group = document.node_by_id(target_id)
            except ValueError:
                group = None
            if isinstance(group, Collection):
                for child in walk_nodes(group.children):
                    self._notify_node(child.id, [self.LockedRole, Qt.ItemDataRole.ToolTipRole])
        if appearance:
            document = self._document
            try:
                parent = document.parent_of(target_id) if document is not None else None
            except ValueError:
                parent = None
            while parent is not None and document is not None:
                self._thumbnail_cache.pop(parent.id, None)
                self._notify_node(parent.id, decoration)
                parent = document.parent_of(parent.id)

    def _thumbnail(
        self,
        node: DocumentNode,
    ) -> QIcon:
        cached = self._thumbnail_cache.get(node.id)
        if cached is not None:
            self._thumbnail_cache.move_to_end(node.id)
            return cached
        document = self._document
        if document is None:
            return QIcon()
        if isinstance(node, Collection):
            icon = _render_raster_group_thumbnail(node, document.width, document.height)
        elif isinstance(node, RasterObject):
            icon = _render_layer_thumbnail(node, document.width, document.height)
        else:
            icon = _render_text_thumbnail(
                (node,),
                document.width,
                document.height,
                normalize_single=True,
            )
        self._thumbnail_cache[node.id] = icon
        while len(self._thumbnail_cache) > 512:
            self._thumbnail_cache.popitem(last=False)
        return icon


class ObjectItemDelegate(QStyledItemDelegate):
    """Type and inherited-lock marks are UI metadata, separate from thumbnails."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: ModelIndex) -> None:
        content = QStyleOptionViewItem(option)
        content.rect.adjust(0, 0, -62, 0)
        super().paint(painter, content, index)
        painter.save()
        painter.setPen(QColor("#c0c8d2"))
        badge = {"collection": "▱", "raster": "▦", "text": "T", "svg": "◇"}.get(
            index.data(LayerTreeModel.NodeKindRole), ""
        )
        rect = QRect(option.rect.right() - 58, option.rect.y(), 28, option.rect.height())
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, badge)
        if index.data(LayerTreeModel.LockedRole):
            lock = QRect(option.rect.right() - 24, option.rect.center().y() - 2, 9, 8)
            painter.drawRect(lock)
            painter.drawArc(lock.adjusted(2, -5, -2, -5), 0, 180 * 16)
        painter.restore()


class LayerTreeView(QTreeView):
    """Tree view with stable Blender-like indentation and expansion state."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LayerTree")
        self.setItemDelegate(ObjectItemDelegate(self))
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setIndentation(18)
        self.setAnimated(False)
        self.setStyleSheet(
            """
            QTreeView#LayerTree {
                background: #242424;
                border: 1px solid #111111;
                outline: none;
                padding: 2px;
            }
            QTreeView#LayerTree::item {
                padding: 2px;
            }
            QTreeView#LayerTree::item:selected {
                background: #4b72a8;
                color: #ffffff;
            }
            """
        )
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def drawBranches(
        self,
        painter: QPainter,
        rect: QRect,
        index: ModelIndex,
    ) -> None:
        """Draw explicit hierarchy guides without image-based style assets."""
        parent = index.parent()
        if parent.isValid() and self.model() is not None:
            painter.save()
            pen = QPen(QColor("#555555"), 1.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            center_y = rect.center().y()
            indentation = self.indentation()
            branch_x = rect.right() - indentation // 2
            sibling_count = self.model().rowCount(parent)
            branch_bottom = rect.bottom() if index.row() < sibling_count - 1 else center_y
            painter.drawLine(branch_x, rect.top(), branch_x, branch_bottom)
            painter.drawLine(branch_x, center_y, rect.right(), center_y)
            painter.restore()
        super().drawBranches(painter, rect, index)

    def drawRow(
        self,
        painter: QPainter,
        options: QStyleOptionViewItem,
        index: ModelIndex,
    ) -> None:
        super().drawRow(painter, options, index)
        painter.save()
        pen = QPen(QColor("#303030"), 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLine(
            0,
            options.rect.bottom(),
            max(0, self.viewport().width() - 1),
            options.rect.bottom(),
        )
        painter.restore()


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
    node_selected = Signal(object)
    local_canvas_requested = Signal()
    resolution_requested = Signal()
    opacity_preview_changed = Signal(object, float)
    opacity_preview_cleared = Signal()

    def __init__(
        self,
        commands: DocumentCommandService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands = commands
        self.selection = SelectionCoordinator(self)
        self.model = LayerListModel(commands, self)
        self.list_view = LayerListView(commands, self)
        self.list_view.setObjectName("LayerList")
        self.list_view.setModel(self.model)
        self.list_view.setIconSize(QSize(LAYER_THUMBNAIL_SIZE, LAYER_THUMBNAIL_SIZE))
        self.list_view.setSpacing(2)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.list_view.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_view.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree_model = LayerTreeModel(commands, self)
        self.tree_view = LayerTreeView(self)
        self.tree_view.setModel(self.tree_model)
        self.tree_view.setIconSize(QSize(LAYER_THUMBNAIL_SIZE, LAYER_THUMBNAIL_SIZE))
        self.tree_view.hide()

        self.new_button = QPushButton("新建", self)
        self.new_group_button = QPushButton("集合", self)
        self.new_vector_group_button = QPushButton("集合", self)
        self.new_vector_group_button.hide()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索对象和集合")
        self.search.setClearButtonEnabled(True)
        self.delete_button = QPushButton("删除", self)
        self.duplicate_button = QPushButton("复制", self)
        self.up_button = QPushButton("上移", self)
        self.down_button = QPushButton("下移", self)
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        for button in (
            self.new_button,
            self.new_group_button,
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
        layout.addWidget(self.search)
        layout.addWidget(self.list_view, 1)
        layout.addWidget(self.tree_view, 1)
        layout.addLayout(button_layout)
        layout.addLayout(properties)

        self._syncing = False
        self._controls_target_id: UUID | None = None
        self._opacity_session = PropertyEditSession[float](
            self._opacity_target,
            self._publish_opacity,
            self._apply_opacity,
            self.opacity_preview_cleared.emit,
            validate=lambda value: 0.0 <= value <= 1.0,
        )
        self.list_view.selectionModel().currentChanged.connect(self._on_current_changed)
        self.tree_view.selectionModel().currentChanged.connect(self._on_tree_current_changed)
        self.tree_view.selectionModel().selectionChanged.connect(self._on_tree_selection_changed)
        self.selection.changed.connect(self._apply_selection)
        self.search.textChanged.connect(self._filter_tree)
        self.new_button.clicked.connect(self.commands.create_layer)
        self.new_group_button.clicked.connect(self._create_raster_group)
        self.new_vector_group_button.clicked.connect(self._create_vector_group)
        self.delete_button.clicked.connect(self._delete_current)
        self.duplicate_button.clicked.connect(self._duplicate_current)
        self.up_button.clicked.connect(self._move_current_up)
        self.down_button.clicked.connect(self._move_current_down)
        self.opacity_spin.editingFinished.connect(self._commit_opacity)
        self.opacity_spin.interactionStarted.connect(self._begin_opacity)
        self.opacity_spin.valueChanged.connect(self._preview_opacity)
        self.opacity_spin.interactionCanceled.connect(self._cancel_opacity)
        self.locked_check.toggled.connect(self._commit_locked)
        self.alpha_locked_check.toggled.connect(self._commit_alpha_locked)
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self._show_context_menu)
        self.set_document(None)

    def set_document(self, document: Document | None, *, workspace_id: UUID | None = None) -> None:
        self.opacity_spin.cancelInteraction()
        self._opacity_session.cancel()
        self.list_view.cancel_visibility_scrub()
        self.model.set_document(document)
        self.tree_model.set_document(document)
        self._sync_tree_visibility()
        self.setEnabled(document is not None)
        self._sync_active_layer()
        self.selection.bind(document, workspace_id)

    def handle_event(self, event: DocumentEvent) -> None:
        document = self.model.document
        if document is None or event.document_id != document.id:
            return
        selected = self.current_node()
        selected_id = selected.id if selected is not None else None
        structural = isinstance(event, (GroupListChanged, GroupMembersChanged))
        previous_syncing = self._syncing
        self._syncing = True
        try:
            self.model.handle_event(event)
            self.tree_model.handle_event(event)
            if structural and selected_id is not None:
                index = self._tree_index_for_id(selected_id)
                if index.isValid():
                    self.tree_view.setCurrentIndex(index)
                else:
                    self._sync_active_layer()
            elif isinstance(event, ActiveLayerChanged) and not previous_syncing:
                self._sync_active_layer()
                document = self.model.document
                if document is not None and document.active_layer_id is not None:
                    self.selection.select((document.active_layer_id,), document.active_layer_id)
            elif isinstance(event, LayerPropertiesChanged):
                if event.layer_id == selected_id:
                    self._sync_controls()
            elif isinstance(event, (VectorObjectChanged, GroupPropertiesChanged)):
                target_id = getattr(event, "object_id", getattr(event, "group_id", None))
                if target_id == selected_id or isinstance(event, GroupPropertiesChanged):
                    self._sync_controls()
        finally:
            self._syncing = previous_syncing
        self._sync_tree_visibility()
        if structural:
            self._syncing = True
            try:
                self.selection.prune(emit=True)
            finally:
                self._syncing = previous_syncing
            self._filter_tree()
        self._update_buttons()

    def _filter_tree(self) -> None:
        text = self.search.text().strip().casefold()

        def visit(parent: QModelIndex) -> bool:
            any_visible = False
            for row in range(self.tree_model.rowCount(parent)):
                index = self.tree_model.index(row, 0, parent)
                node = self.tree_model.node_for_index(index)
                child_visible = visit(index)
                visible = (
                    not text or (node is not None and text in node.name.casefold()) or child_visible
                )
                self.tree_view.setRowHidden(row, parent, not visible)
                if text and child_visible:
                    self.tree_view.setExpanded(index, True)
                any_visible |= visible
            return any_visible

        visit(QModelIndex())
        self.tree_view.setDragEnabled(not text)
        self.tree_view.setAcceptDrops(not text)

    def _on_tree_selection_changed(self, *_: object) -> None:
        if self._syncing:
            return
        if interaction_coordinator(self).owner is not None:
            self._apply_selection(self.selection.state)
            return
        nodes = [
            self.tree_model.node_for_index(index)
            for index in self.tree_view.selectionModel().selectedRows()
        ]
        active = self.current_node()
        self.selection.select(
            tuple(node.id for node in nodes if node is not None),
            active.id if active else None,
        )

    def _apply_selection(self, state: SelectionState) -> None:
        previous = self._syncing
        self._syncing = True
        try:
            model = self.tree_view.selectionModel()
            model.clearSelection()
            for node_id in state.selected_ids:
                index = self._tree_index_for_id(node_id)
                if index.isValid():
                    model.select(index, QItemSelectionModel.SelectionFlag.Select)
            active = self._tree_index_for_id(state.active_id) if state.active_id else QModelIndex()
            model.setCurrentIndex(active, QItemSelectionModel.SelectionFlag.NoUpdate)
            node = self.current_node()
            document = self.model.document
            if document is not None and not previous:
                document.set_active_layer(node.id if isinstance(node, RasterObject) else None)
            self._sync_controls()
            self._update_buttons()
        finally:
            self._syncing = previous
        self.node_selected.emit(self.current_node())

    def _sync_tree_visibility(self) -> None:
        document = self.model.document
        has_document = document is not None
        self.list_view.setVisible(False)
        self.tree_view.setVisible(has_document)

    def current_layer(self) -> RasterObject | None:
        if self.tree_model.document is not None:
            node = self.tree_model.node_for_index(self.tree_view.currentIndex())
            return node if isinstance(node, RasterObject) else None
        return self.model.layer_for_index(self.list_view.currentIndex())

    def current_node(
        self,
    ) -> DocumentNode | None:
        if self.tree_model.document is not None:
            node = self.tree_model.node_for_index(self.tree_view.currentIndex())
            if node is not None:
                try:
                    return self.tree_model.document.node_by_id(node.id)
                except ValueError:
                    pass
            return None
        return self.current_layer()

    def select_node(self, node: object) -> None:
        """Select a node from a canvas tool without changing document content."""
        if not isinstance(
            node,
            (Collection, RasterObject, Collection, TextObject, SvgObject),
        ):
            return
        if self.selection.document is not None:
            self.selection.select((node.id,), node.id)
            return
        if self.tree_model.document is not None:
            index = self._tree_index_for_id(node.id)
            if index.isValid():
                self.tree_view.setCurrentIndex(index)
                return
        if isinstance(node, RasterObject):
            index = self.model.index(self.model.row_for_layer(node), 0)
            self.list_view.setCurrentIndex(index)

    def _sync_active_layer(self) -> None:
        document = self.model.document
        self._syncing = True
        try:
            if document is None or document.active_layer_id is None:
                self.list_view.setCurrentIndex(QModelIndex())
            else:
                row = self.model.row_for_layer(document.active_layer)
                self.list_view.setCurrentIndex(self.model.index(row, 0))
                if self.tree_model.document is not None:
                    self.tree_view.setCurrentIndex(
                        self._tree_index_for_id(document.active_layer_id)
                    )
            self._sync_controls()
        finally:
            self._syncing = False
        self._update_buttons()

    def _sync_controls(self) -> None:
        node = self.current_node()
        was_syncing = self._syncing
        self._syncing = True
        try:
            target_id = node.id if node is not None else None
            if target_id != self._controls_target_id:
                self.opacity_spin.cancelInteraction()
                self._opacity_session.cancel()
                self._controls_target_id = target_id
            self.opacity_spin.syncValue(round(node.opacity * 100) if node else 100)
            self.locked_check.setChecked(node.locked if node else False)
            self.alpha_locked_check.setChecked(
                node.alpha_locked if isinstance(node, RasterObject) else False
            )
            document = self.model.document
            parent = document.parent_of(node.id) if document and node else None
            parent_locked = bool(document and parent and document.is_locked(parent.id))
            editable = node is not None and not node.locked and not parent_locked
            self.opacity_spin.setEnabled(editable)
            self.locked_check.setEnabled(node is not None and not parent_locked)
            self.alpha_locked_check.setEnabled(isinstance(node, RasterObject) and editable)
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
        self.node_selected.emit(layer)

    def _on_tree_current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        _ = previous
        if self._syncing:
            return
        if interaction_coordinator(self).owner is not None:
            self._syncing = True
            try:
                self.tree_view.setCurrentIndex(previous)
            finally:
                self._syncing = False
            return
        node = self.tree_model.node_for_index(current)
        self._sync_controls()
        self._update_buttons()
        self.node_selected.emit(node)

    def _tree_index_for_id(self, node_id: UUID) -> QModelIndex:
        return self.tree_model.index_for_id(node_id)

    def _create_vector_group(self) -> None:
        document = self.model.document
        if document is None or not self.commands.create_vector_group():
            return
        self.select_node(document.vector_groups[-1])

    def _create_raster_group(self) -> None:
        document = self.model.document
        if document is None or not self.commands.create_collection():
            return
        raster_groups = [node for node in document.nodes if isinstance(node, Collection)]
        if raster_groups:
            self.select_node(raster_groups[-1])

    def _create_text_object(self) -> None:
        document = self.model.document
        if document is None:
            return
        node = self.current_node()
        if isinstance(node, Collection):
            group = node
            insertion_index = None
        elif isinstance(node, (TextObject, SvgObject)):
            group, selected_object = document.vector_object_by_id(node.id)
            insertion_index = group.children.index(selected_object)
        else:
            if not self.commands.create_vector_group():
                return
            group = document.vector_groups[-1]
            insertion_index = None
        if self.commands.create_text_object(group.id, index=insertion_index):
            created = (
                group.children[-1] if insertion_index is None else group.children[insertion_index]
            )
            self.select_node(created)

    def _delete_current(self) -> None:
        self.commands.objects.delete(self.selection.state.selected_ids)

    def _show_context_menu(self, position: QPoint) -> None:
        """Show Blender-like actions for the row under the pointer."""
        index = self.tree_view.indexAt(position)
        menu = QMenu(self)
        if index.isValid():
            if not self.tree_view.selectionModel().isSelected(index):
                self.tree_view.setCurrentIndex(index)
            node = self.current_node()
            duplicate_action = menu.addAction("复制")
            move_up_action = menu.addAction("上移")
            move_down_action = menu.addAction("下移")
            delete_action = menu.addAction("删除")
            duplicate_action.setEnabled(self.duplicate_button.isEnabled())
            move_up_action.setEnabled(self.up_button.isEnabled())
            move_down_action.setEnabled(self.down_button.isEnabled())
            delete_action.setEnabled(self.delete_button.isEnabled())
            duplicate_action.triggered.connect(self._duplicate_current)
            move_up_action.triggered.connect(self._move_current_up)
            move_down_action.triggered.connect(self._move_current_down)
            delete_action.triggered.connect(self._delete_current)
            if isinstance(node, (Collection, TextObject, SvgObject)):
                rasterize_action = menu.addAction("栅格化")
                parent_locked = False
                document = self.model.document
                if document is not None and isinstance(node, (TextObject, SvgObject)):
                    try:
                        parent_locked = document.vector_object_by_id(node.id)[0].locked
                    except ValueError:
                        parent_locked = True
                rasterize_action.setEnabled(not node.locked and not parent_locked)
                rasterize_action.triggered.connect(self._rasterize_current)
            elif isinstance(node, RasterObject):
                apply_transform = menu.addAction("应用对象变换")
                apply_transform.setEnabled(not node.locked and not node.transform.is_identity)
                apply_transform.triggered.connect(lambda: self.commands.rasterize_object(node.id))
                local_canvas = menu.addAction("调整对象局部像素画布／裁切")
                local_canvas.setEnabled(not node.locked)
                local_canvas.triggered.connect(self.local_canvas_requested.emit)
                resolution = menu.addAction("调整对象像素分辨率")
                resolution.setEnabled(not node.locked)
                resolution.triggered.connect(self.resolution_requested.emit)
            menu.addSeparator()
        expand_all_action = menu.addAction("全部展开")
        collapse_all_action = menu.addAction("全部折叠")
        expand_all_action.triggered.connect(self.tree_view.expandAll)
        collapse_all_action.triggered.connect(self.tree_view.collapseAll)
        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def _rasterize_current(self) -> None:
        node = self.current_node()
        if isinstance(node, Collection):
            self.commands.rasterize_vector_group(node.id)
        elif isinstance(node, (TextObject, SvgObject)):
            self.commands.rasterize_vector_object(node.id)

    def _ungroup_current(self) -> None:
        node = self.current_node()
        if isinstance(node, Collection):
            self.commands.ungroup(node.id)

    def _duplicate_current(self) -> None:
        self.commands.objects.duplicate(self.selection.state.selected_ids)

    def _move_current_up(self) -> None:
        self._move_current(1)

    def _move_current_down(self) -> None:
        self._move_current(-1)

    def _move_current(self, delta: int) -> None:
        node = self.current_node()
        document = self.model.document
        if node is None or document is None:
            return
        parent_id, index = document.node_location(node.id)
        parent = document.parent_of(node.id)
        count = len(document.nodes if parent is None else parent.children)
        if 0 <= index + delta < count:
            self.commands.move_node(node.id, parent_id, index + delta)

    def _commit_opacity(self) -> None:
        if self._syncing:
            return
        value = self.opacity_spin.value() / 100.0
        if not self._opacity_session.active:
            self._begin_opacity()
            self._opacity_session.preview(value)
        self._opacity_session.finish()
        self._sync_controls()

    def _opacity_target(self) -> EditTarget | None:
        document = self.model.document
        node = self.current_node()
        if document is None or node is None:
            return None
        try:
            if document.node_by_id(node.id) is not node:
                return None
        except ValueError:
            return None
        return EditTarget(document.id, node.id, document.revision)

    def _begin_opacity(self) -> None:
        node = self.current_node()
        if not self._syncing and node is not None:
            self._opacity_session.begin(node.opacity)

    def _preview_opacity(self, value: int) -> None:
        if not self._syncing and self._opacity_session.active:
            self._opacity_session.preview(value / 100.0)

    def _publish_opacity(self, value: float) -> None:
        node = self.current_node()
        if node is not None:
            self.opacity_preview_changed.emit(node.id, value)

    def _cancel_opacity(self) -> None:
        self._opacity_session.cancel()
        if not self._syncing:
            self._sync_controls()

    def _apply_opacity(self, opacity: float) -> bool:
        node = self.current_node()
        if isinstance(node, RasterObject):
            return self.commands.set_layer_opacity(node.id, opacity)
        elif isinstance(node, Collection):
            return self.commands.set_group_opacity(node.id, opacity)
        elif isinstance(node, (TextObject, SvgObject)):
            return self.commands.set_text_object_opacity(node.id, opacity)
        return False

    def _commit_locked(self, locked: bool) -> None:
        if self._syncing:
            return
        node = self.current_node()
        if isinstance(node, RasterObject):
            self.commands.set_layer_locked(node.id, locked)
        elif isinstance(node, Collection):
            self.commands.set_group_locked(node.id, locked)
        elif isinstance(node, (TextObject, SvgObject)):
            self.commands.set_text_object_locked(node.id, locked)

    def _commit_alpha_locked(self, locked: bool) -> None:
        if self._syncing:
            return
        layer = self.current_layer()
        if layer is not None:
            self.commands.set_layer_alpha_locked(layer.id, locked)

    def _update_buttons(self) -> None:
        document = self.model.document
        node = self.current_node()
        has_node = document is not None and node is not None
        self.new_button.setEnabled(document is not None)
        self.new_group_button.setEnabled(document is not None)
        self.new_vector_group_button.setEnabled(document is not None)
        self.duplicate_button.setEnabled(has_node)
        self.delete_button.setEnabled(has_node)
        if not has_node or document is None or node is None:
            self.up_button.setEnabled(False)
            self.down_button.setEnabled(False)
            return
        parent_id, index = document.node_location(node.id)
        parent = document.parent_of(node.id)
        count = len(document.nodes if parent is None else parent.children)
        self.up_button.setEnabled(index < count - 1)
        self.down_button.setEnabled(index > 0)


__all__ = [
    "LAYER_MIME_TYPE",
    "LayerItemDelegate",
    "LayerListModel",
    "LayerListView",
    "LayerTreeModel",
    "LayerTreeView",
    "LayerPanel",
]
