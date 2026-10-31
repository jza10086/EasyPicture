"""Canvas object selection and document-space translation with private previews."""

from __future__ import annotations

from collections.abc import Callable
from math import atan2, degrees, hypot
from typing import cast
from uuid import UUID

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QGraphicsRectItem

from easypicture.domain import (
    AffineTransform,
    Collection,
    DocumentEvent,
    DocumentRevisionChanged,
    FloatRect,
    Point,
    RasterObject,
    SvgObject,
    TextObject,
    walk_nodes,
)
from easypicture.domain.coordinates import RasterMapping
from easypicture.domain.effective import iter_effective_leaves
from easypicture.domain.object_operations import object_basis, transformed_object_matrices
from easypicture.tools.object_hit_test import opaque_at, opaque_in_rect
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
        self._mode: str | None = None
        self._pivot = Point(0, 0)
        self._axis: str | None = None
        self._last_point = Point(0, 0)
        self._orientation = "global"
        self._basis = AffineTransform()
        self._axis_step = 0
        self._default_orientation = "global"

    def activate(self, context: ToolContext) -> None:
        super().activate(context)
        context.canvas.set_tool_cursor(Qt.CursorShape.ArrowCursor)

    def deactivate(self) -> None:
        self._cancel()
        if self._context:
            self._context.canvas.set_transform_axes(None)
            self._context.canvas.set_tool_cursor(None)
        super().deactivate()

    def _cancel(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._context:
            self._context.canvas.set_transform_axes(None)
            self._context.canvas.render_coordinator.clear_object_transform_preview()
            self._context.overlays.clear()
            interaction_coordinator(self._context.canvas).release(self)
        self._start = None
        self._box = None
        self._ids = ()
        self._delta = AffineTransform()
        self._mode = None
        self._axis = None

    def begin_transform(
        self, mode: str, point: Point, pivot: Point, orientation: str = "global"
    ) -> bool:
        context = self._context
        document = context.document_provider() if context else None
        if context is None or document is None or self._start is not None:
            return False
        ids = self._selection().state.selected_ids
        if not ids:
            return False
        try:
            transformed_object_matrices(document, ids, AffineTransform())
        except (ValueError, PermissionError) as error:
            if context.status:
                context.status(str(error))
            return False
        if not interaction_coordinator(context.canvas).acquire(self, self._cancel):
            return False
        self._ids, self._start, self._last_point = ids, point, point
        self._pivot, self._mode = pivot, mode
        self._orientation = self._default_orientation = orientation
        self._axis_step = 0
        active = self._selection().state.active_id
        node = document.node_by_id(active if active is not None else ids[0])
        leaves = [item for item in walk_nodes((node,)) if not isinstance(item, Collection)]
        self._basis = object_basis(leaves[0]) if leaves else AffineTransform()
        self._revision = document.revision
        self._unsubscribe = document.subscribe(self._document_changed)
        self._show_axes()
        return True

    def _show_axes(self) -> None:
        if self._context is None:
            return
        self._update_axes()
        if self._context.status:
            orientation = "局部" if self._orientation == "local" else "全局"
            self._context.status(
                f"{self._mode.upper() if self._mode else ''} · {orientation} {self._axis or 'XY'}"
                " · X/Y 切换轴与坐标系 · L 切换坐标系 · Enter 确认 · Esc 取消"
            )

    def _update_axes(self) -> None:
        if self._context is None or self._mode is None:
            return
        # Only the display follows the preview; gesture calculations keep their initial basis.
        basis = AffineTransform()
        if self._orientation == "local":
            linear = AffineTransform(self._delta.a, self._delta.b, self._delta.c, self._delta.d)
            basis = self._basis.then(linear)
        self._context.canvas.set_transform_axes(
            (self._delta.map_point(self._pivot), basis, self._axis)
        )

    def _finish(self) -> None:
        context, ids, delta = self._context, self._ids, self._delta
        self._cancel()
        if context and context.commands:
            context.commands.transform_objects(ids, delta)

    def _document_changed(self, event: DocumentEvent) -> None:
        if isinstance(event, DocumentRevisionChanged) and event.revision != self._revision:
            self._cancel()

    def mouse_press(self, event: QMouseEvent) -> bool:
        if self._mode is not None and event.button() == Qt.MouseButton.LeftButton:
            self.mouse_move(event)
            self._finish()
            self._canceled_button = True
            return True
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
            self._box.setPen(context.canvas.highlight_pen())
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
            self._last_point = point
            self._preview_transform(point, event.modifiers())
        return True

    def _preview_transform(self, point: Point, modifiers: Qt.KeyboardModifier) -> None:
        context = self._context
        if context is None or self._start is None:
            return
        document = context.document_provider()
        if document is None:
            return
        factor = 0.1 if modifiers & Qt.KeyboardModifier.ShiftModifier else 1.0
        dx, dy = point.x - self._start.x, point.y - self._start.y
        basis = self._basis if self._orientation == "local" and self._mode else AffineTransform()
        if self._mode == "r":
            angle = degrees(
                atan2(point.y - self._pivot.y, point.x - self._pivot.x)
                - atan2(self._start.y - self._pivot.y, self._start.x - self._pivot.x)
            )
            angle = (angle + 180) % 360 - 180
            delta = AffineTransform.rotation(angle * factor)
        elif self._mode == "s":
            initial = hypot(self._start.x - self._pivot.x, self._start.y - self._pivot.y)
            ratio = (
                hypot(point.x - self._pivot.x, point.y - self._pivot.y) / initial
                if initial > 1e-6
                else 1 + dx / 100
            )
            scale = 1 + (ratio - 1) * factor
            delta = AffineTransform.scale(
                1 if self._axis == "y" else scale, 1 if self._axis == "x" else scale
            )
            delta = basis.inverse().then(delta).then(basis)
        else:
            local = basis.inverse().map_point(Point(dx, dy))
            movement = basis.map_point(
                Point(
                    0 if self._axis == "y" else local.x * factor,
                    0 if self._axis == "x" else local.y * factor,
                )
            )
            delta = AffineTransform.translation(
                movement.x,
                movement.y,
            )
        if self._mode in {"s", "r"}:
            delta = (
                AffineTransform.translation(-self._pivot.x, -self._pivot.y)
                .then(delta)
                .then(AffineTransform.translation(self._pivot.x, self._pivot.y))
            )
        try:
            matrices = transformed_object_matrices(document, self._ids, delta)
        except ValueError, PermissionError:
            return
        self._delta = delta
        context.canvas.render_coordinator.set_object_transform_preview(matrices)
        self._update_axes()

    def mouse_release(self, event: QMouseEvent) -> bool:
        if self._canceled_button:
            self._canceled_button = False
            return True
        if self._mode is not None:
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
            query = FloatRect(rect.x(), rect.y(), rect.width(), rect.height())
            candidates = document.spatial_index.query(query)
            ids = tuple(
                entry.leaf.id
                for entry in iter_effective_leaves(document)
                if entry.visible
                and entry.leaf.id in candidates
                and (
                    not self._ignore_transparent()
                    or (
                        entry.opacity > 0
                        and opaque_in_rect(
                            cast(RasterObject | TextObject | SvgObject, entry.leaf), query
                        )
                    )
                )
            )
            selection = self._selection()
            selection.select((*selection.state.selected_ids, *ids) if self._extend else ids)
        elif context.commands:
            context.canvas.render_coordinator.clear_object_transform_preview()
            context.commands.transform_objects(self._ids, self._delta)
        self._cancel()
        return True

    def key_press(self, event: QKeyEvent) -> bool:
        if self._mode is not None and event.key() == Qt.Key.Key_L:
            self._consumed_keys.add(event.key())
            if not event.isAutoRepeat():
                self._orientation = "local" if self._orientation == "global" else "global"
                self._show_axes()
                self._preview_transform(self._last_point, event.modifiers())
            return True
        if self._mode is not None and event.key() in (Qt.Key.Key_X, Qt.Key.Key_Y):
            self._consumed_keys.add(event.key())
            if event.isAutoRepeat():
                return True
            axis = "x" if event.key() == Qt.Key.Key_X else "y"
            self._axis_step = (self._axis_step + 1) % 3 if self._axis == axis else 1
            self._axis = axis if self._axis_step else None
            self._orientation = self._default_orientation
            if self._axis_step == 2:
                self._orientation = "local" if self._orientation == "global" else "global"
            self._show_axes()
            self._preview_transform(self._last_point, event.modifiers())
            return True
        if self._start is not None and event.key() in (
            Qt.Key.Key_Escape,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            self._consumed_keys.add(event.key())
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._ids and self._context:
                self._finish()
            else:
                self._cancel()
            return True
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        if event.key() in self._consumed_keys:
            self._consumed_keys.remove(event.key())
            return True
        return False
