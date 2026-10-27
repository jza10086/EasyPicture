"""Canvas object selection and document-space translation with private previews."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import QGraphicsRectItem

from easypicture.domain import (
    AffineTransform,
    Collection,
    DocumentEvent,
    DocumentRevisionChanged,
    FloatRect,
    Point,
    RasterObject,
    walk_nodes,
)
from easypicture.domain.coordinates import RasterMapping, validate_object_transform
from easypicture.domain.effective import iter_effective_leaves
from easypicture.tools.object_hit_test import opaque_at
from easypicture.tools.protocol import NullTool, ToolContext
from easypicture.ui.property_binding import interaction_coordinator
from easypicture.ui.selection_state import SelectionCoordinator


class ObjectTool(NullTool):
    def __init__(
        self,
        selection: Callable[[], SelectionCoordinator],
        ignore_transparent: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__()
        self._selection = selection
        self._ignore_transparent = ignore_transparent
        self._start: Point | None = None
        self._revision = -1
        self._ids: tuple[UUID, ...] = ()
        self._delta = AffineTransform()
        self._box: QGraphicsRectItem | None = None
        self._extend = False
        self._consumed_keys: set[int] = set()
        self._canceled_button = False
        self._unsubscribe: Callable[[], None] | None = None

    def activate(self, context: ToolContext) -> None:
        super().activate(context)
        context.canvas.set_tool_cursor(Qt.CursorShape.ArrowCursor)

    def deactivate(self) -> None:
        self._cancel()
        if self._context:
            self._context.canvas.set_tool_cursor(None)
        super().deactivate()

    def _cancel(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._context:
            self._context.canvas.render_coordinator.clear_object_transform_preview()
            self._context.overlays.clear()
            interaction_coordinator(self._context.canvas).release(self)
        self._start = None
        self._box = None
        self._ids = ()
        self._delta = AffineTransform()

    def _document_changed(self, event: DocumentEvent) -> None:
        if isinstance(event, DocumentRevisionChanged) and event.revision != self._revision:
            self._cancel()

    def mouse_press(self, event: QMouseEvent) -> bool:
        if event.button() == Qt.MouseButton.RightButton and self._start is not None:
            self._cancel()
            self._canceled_button = True
            return True
        context = self._context
        document = context.document_provider() if context else None
        if not context or not document or event.button() != Qt.MouseButton.LeftButton:
            return False
        point = context.canvas.viewport_to_document(event.position().toPoint())
        candidates = document.spatial_index.at(point)
        hit: UUID | None = None
        for entry in reversed(iter_effective_leaves(document)):
            if not entry.visible or entry.leaf.id not in candidates:
                continue
            node = document.node_by_id(entry.leaf.id)
            if isinstance(node, RasterObject):
                if not RasterMapping(node.world_transform, node.size).contains_document_point(
                    point
                ):
                    continue
            if self._ignore_transparent() and (
                entry.opacity <= 0 or isinstance(node, Collection) or not opaque_at(node, point)
            ):
                continue
            hit = node.id
            break
        selection = self._selection()
        self._extend = bool(
            event.modifiers()
            & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier)
        )
        if hit is not None:
            selected = selection.state.selected_ids
            if self._extend:
                selection.select(
                    tuple(key for key in selected if key != hit)
                    if hit in selected
                    else (*selected, hit),
                    hit,
                )
                return True
            if hit not in selected:
                selection.select((hit,), hit)
            self._ids = selection.state.selected_ids
            if any(
                document.is_locked(node.id)
                for key in self._ids
                for node in walk_nodes((document.node_by_id(key),))
            ):
                if context.status:
                    context.status("选中对象包含锁定目标，不能移动")
                self._ids = ()
                return True
        else:
            if not self._extend:
                selection.select(())
            self._box = QGraphicsRectItem()
            pen = QPen(QColor("#ff9b38"))
            pen.setCosmetic(True)
            self._box.setPen(pen)
            context.overlays.add(self._box)
        self._start = point
        self._revision = document.revision
        self._unsubscribe = document.subscribe(self._document_changed)
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        context = self._context
        if not context or self._start is None:
            return False
        document = context.document_provider()
        if document is None or document.revision != self._revision:
            self._cancel()
            return True
        point = context.canvas.viewport_to_document(event.position().toPoint())
        if self._box:
            self._box.setRect(
                QRectF(
                    QPointF(self._start.x, self._start.y),
                    QPointF(point.x, point.y),
                ).normalized()
            )
        else:
            delta = AffineTransform.translation(point.x - self._start.x, point.y - self._start.y)
            matrices: dict[UUID, AffineTransform] = {}
            try:
                for key in self._ids:
                    for node in walk_nodes((document.node_by_id(key),)):
                        if isinstance(node, Collection):
                            continue
                        matrix = node.transform.then(delta)
                        if isinstance(node, RasterObject):
                            matrix = node.world_transform.then(delta).then(
                                AffineTransform.translation(-node.origin.x, -node.origin.y)
                            )
                        validate_object_transform(matrix)
                        matrices[node.id] = matrix
            except ValueError:
                return True
            self._delta = delta
            context.canvas.render_coordinator.set_object_transform_preview(matrices)
        return True

    def mouse_release(self, event: QMouseEvent) -> bool:
        if self._canceled_button:
            self._canceled_button = False
            return True
        if self._start is None or not self._context:
            return False
        self.mouse_move(event)
        context = self._context
        document = context.document_provider()
        if self._start is None or document is None:
            return True
        if self._box:
            rect = self._box.rect()
            if rect.isEmpty():
                self._cancel()
                return True
            candidates = document.spatial_index.query(
                FloatRect(rect.x(), rect.y(), rect.width(), rect.height())
            )
            ids = tuple(
                entry.leaf.id
                for entry in iter_effective_leaves(document)
                if entry.visible and entry.leaf.id in candidates
            )
            selection = self._selection()
            selection.select((*selection.state.selected_ids, *ids) if self._extend else ids)
        elif context.commands:
            context.canvas.render_coordinator.clear_object_transform_preview()
            context.commands.transform_objects(self._ids, self._delta)
        self._cancel()
        return True

    def key_press(self, event: QKeyEvent) -> bool:
        if self._start is not None and event.key() in (
            Qt.Key.Key_Escape,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            self._consumed_keys.add(event.key())
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._ids and self._context:
                if self._context.commands:
                    self._context.commands.transform_objects(self._ids, self._delta)
            self._cancel()
            return True
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        if event.key() in self._consumed_keys:
            self._consumed_keys.remove(event.key())
            return True
        return False
