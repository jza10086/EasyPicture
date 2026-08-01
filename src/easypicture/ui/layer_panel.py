"""Top-to-bottom layer list and undoable layer property controls."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID

import numpy as np
from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractListModel,
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
from easypicture.domain import (
    ActiveLayerChanged,
    AffineTransform,
    Document,
    DocumentEvent,
    DocumentSizeChanged,
    FloatRect,
    GroupListChanged,
    GroupMembersChanged,
    GroupPropertiesChanged,
    LayerListChanged,
    LayerPixelsChanged,
    LayerPropertiesChanged,
    LayerTreeChanged,
    RasterGroupLayer,
    RasterLayer,
    TextObject,
    TextObjectChanged,
    VectorGroupLayer,
    VectorGroupPropertiesChanged,
)
from easypicture.render import paint_text_objects, rgba_tile_to_qimage
from easypicture.ui.drag_value_spinbox import DragValueSpinBox

LAYER_MIME_TYPE = "application/x-easypicture-layer-id"
LAYER_THUMBNAIL_SIZE = 48
_THUMBNAIL_CONTENT_SIZE = 44
_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex
type ThumbnailKey = tuple[int, int, int, int, int, int, int]


def _render_layer_thumbnail(
    layer: RasterLayer,
    document_width: int,
    document_height: int,
) -> QIcon:
    scale = min(
        _THUMBNAIL_CONTENT_SIZE / document_width,
        _THUMBNAIL_CONTENT_SIZE / document_height,
    )
    content_width = max(1, round(document_width * scale))
    content_height = max(1, round(document_height * scale))
    sampled = np.zeros((content_height, content_width, 4), dtype=np.uint8)
    tile_size = layer.pixels.tile_size
    tile_cache: dict[tuple[int, int], np.ndarray] = {}
    for target_y in range(content_height):
        document_y = min(
            document_height - 1,
            int((target_y + 0.5) * document_height / content_height),
        )
        local_y = document_y - layer.origin.y
        if not 0 <= local_y < layer.size.height:
            continue
        tile_y, pixel_y = divmod(local_y, tile_size)
        for target_x in range(content_width):
            document_x = min(
                document_width - 1,
                int((target_x + 0.5) * document_width / content_width),
            )
            local_x = document_x - layer.origin.x
            if not 0 <= local_x < layer.size.width:
                continue
            tile_x, pixel_x = divmod(local_x, tile_size)
            cache_key = (tile_x, tile_y)
            tile = tile_cache.get(cache_key)
            if tile is None:
                tile = layer.pixels.read_tile(tile_x, tile_y)
                tile_cache[cache_key] = tile
            sampled[target_y, target_x] = tile[pixel_y, pixel_x]

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
    group: RasterGroupLayer,
    document_width: int,
    document_height: int,
) -> QIcon:
    """Render a small source-over preview for a raster group.

    Group rows must not use a single arbitrary child as their preview: doing
    so makes a group thumbnail stale or visually misleading as soon as a
    second child is added.  The thumbnail is deliberately sampled at its
    final size, so this stays cheap even for a large canvas while preserving
    the same child order and opacity semantics as the canvas compositor.
    """
    scale = min(
        _THUMBNAIL_CONTENT_SIZE / max(1, document_width),
        _THUMBNAIL_CONTENT_SIZE / max(1, document_height),
    )
    content_width = max(1, round(document_width * scale))
    content_height = max(1, round(document_height * scale))
    sampled = np.zeros((content_height, content_width, 4), dtype=np.uint8)
    for layer in group.children:
        if not layer.visible or layer.opacity <= 0.0:
            continue
        tile_cache: dict[tuple[int, int], np.ndarray] = {}
        for target_y in range(content_height):
            document_y = min(
                document_height - 1,
                int((target_y + 0.5) * document_height / content_height),
            )
            local_y = document_y - layer.origin.y
            if not 0 <= local_y < layer.size.height:
                continue
            tile_y, pixel_y = divmod(local_y, layer.pixels.tile_size)
            for target_x in range(content_width):
                document_x = min(
                    document_width - 1,
                    int((target_x + 0.5) * document_width / content_width),
                )
                local_x = document_x - layer.origin.x
                if not 0 <= local_x < layer.size.width:
                    continue
                tile_x, pixel_x = divmod(local_x, layer.pixels.tile_size)
                cache_key = (tile_x, tile_y)
                tile = tile_cache.get(cache_key)
                if tile is None:
                    tile = layer.pixels.read_tile(tile_x, tile_y)
                    tile_cache[cache_key] = tile
                source = tile[pixel_y, pixel_x]
                source_alpha = float(source[3]) / 255.0 * layer.opacity * group.opacity
                if source_alpha <= 0.0:
                    continue
                destination = sampled[target_y, target_x]
                destination_alpha = float(destination[3]) / 255.0
                output_alpha = source_alpha + destination_alpha * (1.0 - source_alpha)
                if output_alpha <= 0.0:
                    continue
                output_rgb = (
                    source[:3].astype(np.float32) * source_alpha
                    + destination[:3].astype(np.float32)
                    * destination_alpha
                    * (1.0 - source_alpha)
                ) / output_alpha
                destination[:3] = np.clip(np.rint(output_rgb), 0, 255).astype(np.uint8)
                destination[3] = np.uint8(np.clip(np.rint(output_alpha * 255.0), 0, 255))

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


def _render_text_thumbnail(
    objects: Sequence[TextObject],
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
    if normalize_single and len(objects) == 1:
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
    if normalize_single and len(objects) == 1:
        source = objects[0]
        normalized = replace(
            source,
            frame=FloatRect(0.0, 0.0, source.frame.width, source.frame.height),
            transform=AffineTransform(),
        )
        paint_text_objects(painter, (normalized,), group_opacity=group_opacity)
    else:
        paint_text_objects(painter, objects, group_opacity=group_opacity)
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
        self._thumbnail_cache: dict[UUID, tuple[ThumbnailKey, QIcon]] = {}

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

    def _thumbnail(self, layer: RasterLayer) -> QIcon:
        document = self._document
        if document is None:
            return QIcon()
        key: ThumbnailKey = (
            layer.pixels.revision,
            document.width,
            document.height,
            layer.size.width,
            layer.size.height,
            layer.origin.x,
            layer.origin.y,
        )
        cached = self._thumbnail_cache.get(layer.id)
        if cached is not None and cached[0] == key:
            return cached[1]
        icon = _render_layer_thumbnail(layer, document.width, document.height)
        self._thumbnail_cache[layer.id] = (key, icon)
        return icon


class LayerTreeModel(QAbstractItemModel):
    """Two-level node model used when a document contains editable text."""

    NodeIdRole = Qt.ItemDataRole.UserRole + 11
    NodeKindRole = Qt.ItemDataRole.UserRole + 12

    def __init__(
        self,
        commands: DocumentCommandService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands = commands
        self._document: Document | None = None
        self._thumbnail_cache: dict[UUID, QIcon] = {}

    @property
    def document(self) -> Document | None:
        return self._document

    def set_document(self, document: Document | None) -> None:
        self.beginResetModel()
        self._document = document
        self._thumbnail_cache.clear()
        self.endResetModel()

    def _roots(self) -> tuple[object, ...]:
        if self._document is None:
            return ()
        return tuple(reversed(self._document.nodes))

    def _children(self, node: object) -> tuple[RasterLayer | TextObject, ...]:
        if isinstance(node, RasterGroupLayer):
            return tuple(reversed(node.children))
        if isinstance(node, VectorGroupLayer):
            return tuple(reversed(node.children))
        return ()

    def index(
        self,
        row: int,
        column: int,
        parent: ModelIndex = _INVALID_INDEX,
    ) -> QModelIndex:
        if column != 0 or row < 0:
            return QModelIndex()
        values = self._roots() if not parent.isValid() else self._children(parent.internalPointer())
        if row >= len(values):
            return QModelIndex()
        return self.createIndex(row, column, values[row])

    def parent(self, index: ModelIndex = _INVALID_INDEX) -> QModelIndex:  # type: ignore[override]
        if not index.isValid() or self._document is None:
            return QModelIndex()
        child = index.internalPointer()
        if not isinstance(child, (RasterLayer, TextObject)):
            return QModelIndex()
        for root_row, root in enumerate(self._roots()):
            if isinstance(root, (RasterGroupLayer, VectorGroupLayer)) and any(
                candidate.id == child.id for candidate in root.children
            ):
                return self.createIndex(root_row, 0, root)
        return QModelIndex()

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        if not parent.isValid():
            return len(self._roots())
        return len(self._children(parent.internalPointer()))

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        _ = parent
        return 1

    def node_for_index(
        self,
        index: ModelIndex,
    ) -> RasterGroupLayer | RasterLayer | VectorGroupLayer | TextObject | None:
        if not index.isValid():
            return None
        node = index.internalPointer()
        if isinstance(node, (RasterGroupLayer, RasterLayer, VectorGroupLayer, TextObject)):
            return node
        return None

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
        if role == self.NodeKindRole:
            if isinstance(node, RasterGroupLayer):
                return "raster_group"
            if isinstance(node, RasterLayer):
                return "raster"
            if isinstance(node, VectorGroupLayer):
                return "vector_group"
            return "text"
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbnail(node)
        if role == Qt.ItemDataRole.ToolTipRole:
            if isinstance(node, RasterGroupLayer):
                return f"栅格组；不透明度 {node.opacity * 100:.0f}%"
            if isinstance(node, VectorGroupLayer):
                return f"矢量组；不透明度 {node.opacity * 100:.0f}%"
            if isinstance(node, TextObject):
                return f"文字；不透明度 {node.opacity * 100:.0f}%"
            locks = []
            if node.locked:
                locks.append("已锁定")
            if node.alpha_locked:
                locks.append("透明像素已锁定")
            suffix = f"；{'，'.join(locks)}" if locks else ""
            return f"不透明度 {node.opacity * 100:.0f}%{suffix}"
        return None

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
        data = QMimeData()
        for index in indexes:
            node = self.node_for_index(index)
            if node is not None:
                data.setData(LAYER_MIME_TYPE, str(node.id).encode("ascii"))
                break
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
        """Move roots or same-group children without allowing cross-group nesting."""
        if action != Qt.DropAction.MoveAction or column > 0:
            return False
        document = self._document
        if document is None or not data.hasFormat(LAYER_MIME_TYPE):
            return False
        try:
            node_text = bytearray(data.data(LAYER_MIME_TYPE).data()).decode("ascii")
            node_id = UUID(node_text)
        except (ValueError, UnicodeDecodeError):
            return False

        try:
            source = document.node_by_id(node_id)
        except ValueError:
            return False
        if parent.isValid():
            if isinstance(source, RasterLayer):
                destination = self.node_for_index(parent)
                if not isinstance(destination, RasterGroupLayer):
                    return False
                source_raster_group = document.raster_group_for_layer(source.id)
                displayed_raster = list(reversed(destination.children))
                if source in displayed_raster:
                    displayed_raster.remove(source)
                insert_row = (
                    len(displayed_raster)
                    if row < 0
                    else max(0, min(row, len(displayed_raster)))
                )
                target_index = len(destination.children) - insert_row
                if (
                    source_raster_group is destination
                    and source_raster_group.children.index(source) < target_index
                ):
                    target_index -= 1
                return self.commands.move_raster_layer_to_group(
                    source.id,
                    destination.id,
                    max(0, target_index),
                )
            if isinstance(source, TextObject):
                destination_vector = self.node_for_index(parent)
                if not isinstance(destination_vector, VectorGroupLayer):
                    return False
                source_vector_group, _ = document.text_object_by_id(source.id)
                displayed_vector = list(reversed(destination_vector.children))
                source_text = source
                if source_text in displayed_vector:
                    displayed_vector.remove(source_text)
                insert_row = (
                    len(displayed_vector)
                    if row < 0
                    else max(0, min(row, len(displayed_vector)))
                )
                target_index = len(destination_vector.children) - insert_row
                if (
                    source_vector_group is destination_vector
                    and source_vector_group.children.index(source_text) < target_index
                ):
                    target_index -= 1
                return self.commands.move_vector_object_to_group(
                    source_text.id,
                    destination_vector.id,
                    max(0, target_index),
                )
            return False

        if not isinstance(source, (RasterGroupLayer, VectorGroupLayer)):
            return False
        displayed_roots = list(reversed(document.nodes))
        try:
            displayed_roots.remove(source)
        except ValueError:
            return False
        insert_row = (
            len(displayed_roots) if row < 0 else max(0, min(row, len(displayed_roots)))
        )
        displayed_roots.insert(insert_row, source)
        desired_storage = list(reversed(displayed_roots))
        return self.commands.move_group(source.id, desired_storage.index(source))

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
            if isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
                return self.commands.rename_group(node.id, str(value))
            if isinstance(node, RasterLayer):
                return self.commands.rename_layer(node.id, str(value))
            return self.commands.update_text_object(node.id, name=str(value))
        if role == Qt.ItemDataRole.CheckStateRole:
            check_value = getattr(value, "value", value)
            visible = check_value == Qt.CheckState.Checked.value
            if isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
                return self.commands.set_group_visible(node.id, visible)
            if isinstance(node, RasterLayer):
                return self.commands.set_layer_visible(node.id, visible)
            return self.commands.set_text_object_visible(node.id, visible)
        return False

    def handle_event(self, event: DocumentEvent) -> None:
        if isinstance(event, (LayerTreeChanged, GroupListChanged, GroupMembersChanged)):
            self.beginResetModel()
            self._thumbnail_cache.clear()
            self.endResetModel()
            return
        if isinstance(event, LayerPixelsChanged):
            self._thumbnail_cache.pop(event.layer_id, None)
            if self._document is not None:
                try:
                    group = self._document.raster_group_for_layer(event.layer_id)
                except ValueError:
                    group = None
                if group is not None:
                    self._thumbnail_cache.pop(group.id, None)
                    group_index = next(
                        (
                            row
                            for row, candidate in enumerate(self._roots())
                            if isinstance(candidate, (RasterGroupLayer, VectorGroupLayer))
                            and candidate.id == group.id
                        ),
                        -1,
                    )
                    if group_index >= 0:
                        group_model_index = self.index(group_index, 0)
                        self.dataChanged.emit(
                            group_model_index,
                            group_model_index,
                            [Qt.ItemDataRole.DecorationRole],
                        )
        if isinstance(
            event,
            (
                LayerPropertiesChanged,
                TextObjectChanged,
                VectorGroupPropertiesChanged,
                GroupPropertiesChanged,
            ),
        ):
            self._thumbnail_cache.clear()
            if self.rowCount() > 0:
                self.dataChanged.emit(
                    self.index(0, 0),
                    self.index(self.rowCount() - 1, 0),
                    [Qt.ItemDataRole.DecorationRole],
                )
                for root_row, root in enumerate(self._roots()):
                    if (
                        not isinstance(root, (RasterGroupLayer, VectorGroupLayer))
                        or not root.children
                    ):
                        continue
                    root_index = self.index(root_row, 0)
                    self.dataChanged.emit(
                        self.index(0, 0, root_index),
                        self.index(len(root.children) - 1, 0, root_index),
                        [Qt.ItemDataRole.DecorationRole],
                    )

    def _thumbnail(
        self,
        node: RasterGroupLayer | RasterLayer | VectorGroupLayer | TextObject,
    ) -> QIcon:
        cached = self._thumbnail_cache.get(node.id)
        if cached is not None:
            return cached
        document = self._document
        if document is None:
            return QIcon()
        if isinstance(node, RasterGroupLayer):
            icon = _render_raster_group_thumbnail(node, document.width, document.height)
        elif isinstance(node, RasterLayer):
            icon = _render_layer_thumbnail(node, document.width, document.height)
        elif isinstance(node, VectorGroupLayer):
            icon = _render_text_thumbnail(
                node.children,
                document.width,
                document.height,
                group_opacity=node.opacity,
            )
        else:
            icon = _render_text_thumbnail(
                (node,),
                document.width,
                document.height,
                normalize_single=True,
            )
        self._thumbnail_cache[node.id] = icon
        return icon


class LayerTreeView(QTreeView):
    """Tree view with stable Blender-like indentation and expansion state."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LayerTree")
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
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
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
        self.list_view.setIconSize(
            QSize(LAYER_THUMBNAIL_SIZE, LAYER_THUMBNAIL_SIZE)
        )
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
        self.new_group_button = QPushButton("栅格组", self)
        self.new_vector_group_button = QPushButton("矢量组", self)
        self.delete_button = QPushButton("删除", self)
        self.duplicate_button = QPushButton("复制", self)
        self.up_button = QPushButton("上移", self)
        self.down_button = QPushButton("下移", self)
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        for button in (
            self.new_button,
            self.new_group_button,
            self.new_vector_group_button,
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
        layout.addWidget(self.tree_view, 1)
        layout.addLayout(button_layout)
        layout.addLayout(properties)

        self._syncing = False
        self.list_view.selectionModel().currentChanged.connect(self._on_current_changed)
        self.tree_view.selectionModel().currentChanged.connect(self._on_tree_current_changed)
        self.new_button.clicked.connect(self.commands.create_layer)
        self.new_group_button.clicked.connect(self._create_raster_group)
        self.new_vector_group_button.clicked.connect(self._create_text_group)
        self.delete_button.clicked.connect(self._delete_current)
        self.duplicate_button.clicked.connect(self._duplicate_current)
        self.up_button.clicked.connect(self._move_current_up)
        self.down_button.clicked.connect(self._move_current_down)
        self.opacity_spin.editingFinished.connect(self._commit_opacity)
        self.locked_check.toggled.connect(self._commit_locked)
        self.alpha_locked_check.toggled.connect(self._commit_alpha_locked)
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self._show_context_menu)
        self.set_document(None)

    def set_document(self, document: Document | None) -> None:
        self.list_view.cancel_visibility_scrub()
        self.model.set_document(document)
        self.tree_model.set_document(document)
        self._sync_tree_visibility()
        self.setEnabled(document is not None)
        self._sync_active_layer()

    def handle_event(self, event: DocumentEvent) -> None:
        self.model.handle_event(event)
        self.tree_model.handle_event(event)
        if isinstance(event, (LayerListChanged, ActiveLayerChanged)):
            self._sync_active_layer()
        elif isinstance(event, LayerPropertiesChanged):
            document = self.model.document
            if document is not None and event.layer_id == document.active_layer_id:
                self._sync_controls()
        self._sync_tree_visibility()
        self._update_buttons()

    def _sync_tree_visibility(self) -> None:
        document = self.model.document
        has_document = document is not None
        self.list_view.setVisible(False)
        self.tree_view.setVisible(has_document)

    def current_layer(self) -> RasterLayer | None:
        if self.tree_view.isVisible():
            node = self.tree_model.node_for_index(self.tree_view.currentIndex())
            return node if isinstance(node, RasterLayer) else None
        return self.model.layer_for_index(self.list_view.currentIndex())

    def current_node(
        self,
    ) -> RasterGroupLayer | RasterLayer | VectorGroupLayer | TextObject | None:
        if self.tree_view.isVisible():
            return self.tree_model.node_for_index(self.tree_view.currentIndex())
        return self.current_layer()

    def select_node(self, node: object) -> None:
        """Select a node from a canvas tool without changing document content."""
        if not isinstance(node, (RasterGroupLayer, RasterLayer, VectorGroupLayer, TextObject)):
            return
        if self.tree_view.isVisible():
            index = self._tree_index_for_id(node.id)
            if index.isValid():
                self.tree_view.setCurrentIndex(index)
                return
        if isinstance(node, RasterLayer):
            index = self.model.index(self.model.row_for_layer(node), 0)
            self.list_view.setCurrentIndex(index)

    def _sync_active_layer(self) -> None:
        document = self.model.document
        self._syncing = True
        try:
            if document is None:
                self.list_view.setCurrentIndex(QModelIndex())
            else:
                row = self.model.row_for_layer(document.active_layer)
                self.list_view.setCurrentIndex(self.model.index(row, 0))
                if self.tree_view.isVisible():
                    self.tree_view.setCurrentIndex(self._tree_index_for_id(document.active_layer_id))
            self._sync_controls()
        finally:
            self._syncing = False
        self._update_buttons()

    def _sync_controls(self) -> None:
        node = self.current_node()
        was_syncing = self._syncing
        self._syncing = True
        try:
            self.opacity_spin.setValue(round(node.opacity * 100) if node else 100)
            self.locked_check.setChecked(node.locked if node else False)
            self.alpha_locked_check.setChecked(
                node.alpha_locked if isinstance(node, RasterLayer) else False
            )
            self.alpha_locked_check.setEnabled(isinstance(node, RasterLayer))
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
        node = self.tree_model.node_for_index(current)
        document = self.model.document
        if isinstance(node, RasterLayer) and document is not None:
            document.set_active_layer(node.id)
        self._sync_controls()
        self._update_buttons()
        self.node_selected.emit(node)

    def _tree_index_for_id(self, node_id: UUID) -> QModelIndex:
        for row in range(self.tree_model.rowCount()):
            root_index = self.tree_model.index(row, 0)
            root = self.tree_model.node_for_index(root_index)
            if root is not None and root.id == node_id:
                return root_index
            for child_row in range(self.tree_model.rowCount(root_index)):
                child_index = self.tree_model.index(child_row, 0, root_index)
                child = self.tree_model.node_for_index(child_index)
                if child is not None and child.id == node_id:
                    return child_index
        return QModelIndex()

    def _create_text_group(self) -> None:
        document = self.model.document
        if document is None or not self.commands.create_vector_group():
            return
        self.select_node(document.text_groups[-1])

    def _create_raster_group(self) -> None:
        document = self.model.document
        if document is None or not self.commands.create_raster_group():
            return
        raster_groups = [node for node in document.nodes if isinstance(node, RasterGroupLayer)]
        if raster_groups:
            self.select_node(raster_groups[-1])

    def _create_text_object(self) -> None:
        document = self.model.document
        if document is None:
            return
        node = self.current_node()
        if isinstance(node, VectorGroupLayer):
            group = node
            insertion_index = None
        elif isinstance(node, TextObject):
            group, selected_object = document.text_object_by_id(node.id)
            insertion_index = group.children.index(selected_object)
        else:
            if not self.commands.create_vector_group():
                return
            group = document.text_groups[-1]
            insertion_index = None
        if self.commands.create_text_object(group.id, index=insertion_index):
            created = (
                group.children[-1]
                if insertion_index is None
                else group.children[insertion_index]
            )
            self.select_node(created)

    def _delete_current(self) -> None:
        node = self.current_node()
        if isinstance(node, RasterLayer):
            self.commands.delete_layer(node.id)
        elif isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
            self.commands.delete_group(node.id)
        elif isinstance(node, TextObject):
            self.commands.delete_text_object(node.id)

    def _show_context_menu(self, position: QPoint) -> None:
        """Show Blender-like actions for the row under the pointer."""
        index = self.tree_view.indexAt(position)
        if not index.isValid():
            return
        self.tree_view.setCurrentIndex(index)
        menu = QMenu(self)
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
        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def _ungroup_current(self) -> None:
        node = self.current_node()
        if isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
            self.commands.ungroup(node.id)

    def _duplicate_current(self) -> None:
        node = self.current_node()
        if isinstance(node, RasterLayer):
            self.commands.duplicate_layer(node.id)
        elif isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
            self.commands.duplicate_group(node.id)
        elif isinstance(node, TextObject):
            self.commands.duplicate_text_object(node.id)

    def _move_current_up(self) -> None:
        node = self.current_node()
        document = self.model.document
        if node is None or document is None:
            return
        if isinstance(node, RasterLayer):
            index = document.layer_index(node.id)
            if index < len(document.layers) - 1:
                self.commands.move_layer(node.id, index + 1)
        elif isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
            index = document.nodes.index(node)
            if index < len(document.nodes) - 1:
                self.commands.move_group(node.id, index + 1)
        else:
            group, _ = document.text_object_by_id(node.id)
            index = group.children.index(node)
            if index < len(group.children) - 1:
                self.commands.move_text_object(node.id, index + 1)

    def _move_current_down(self) -> None:
        node = self.current_node()
        document = self.model.document
        if node is None or document is None:
            return
        if isinstance(node, RasterLayer):
            index = document.layer_index(node.id)
            if index > 0:
                self.commands.move_layer(node.id, index - 1)
        elif isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
            index = document.nodes.index(node)
            if index > 0:
                self.commands.move_group(node.id, index - 1)
        else:
            group, _ = document.text_object_by_id(node.id)
            index = group.children.index(node)
            if index > 0:
                self.commands.move_text_object(node.id, index - 1)

    def _commit_opacity(self) -> None:
        if self._syncing:
            return
        node = self.current_node()
        if isinstance(node, RasterLayer):
            self.commands.set_layer_opacity(node.id, self.opacity_spin.value() / 100.0)
        elif isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
            self.commands.set_group_opacity(node.id, self.opacity_spin.value() / 100.0)
        elif isinstance(node, TextObject):
            self.commands.set_text_object_opacity(
                node.id, self.opacity_spin.value() / 100.0
            )

    def _commit_locked(self, locked: bool) -> None:
        if self._syncing:
            return
        node = self.current_node()
        if isinstance(node, RasterLayer):
            self.commands.set_layer_locked(node.id, locked)
        elif isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
            self.commands.set_group_locked(node.id, locked)
        elif isinstance(node, TextObject):
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
        self.delete_button.setEnabled(
            has_node
            and document is not None
            and (not isinstance(node, RasterLayer) or len(document.layers) > 1)
        )
        if not has_node or document is None or node is None:
            self.up_button.setEnabled(False)
            self.down_button.setEnabled(False)
            return
        if isinstance(node, RasterLayer):
            index = document.layer_index(node.id)
            self.up_button.setEnabled(index < len(document.layers) - 1)
            self.down_button.setEnabled(index > 0)
        elif isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
            index = document.nodes.index(node)
            self.up_button.setEnabled(index < len(document.nodes) - 1)
            self.down_button.setEnabled(index > 0)
        else:
            group, _ = document.text_object_by_id(node.id)
            index = group.children.index(node)
            self.up_button.setEnabled(index < len(group.children) - 1)
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
