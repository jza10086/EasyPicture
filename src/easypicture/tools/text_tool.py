"""Minimal editable text-object tool for the P9A two-level layer model."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal
from uuid import UUID

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
)

from easypicture.domain import (
    AffineTransform,
    DocumentEvent,
    FloatRect,
    Point,
    TextObject,
    VectorGroupLayer,
)
from easypicture.domain.document import Unsubscribe
from easypicture.tools.protocol import ToolContext


@dataclass(slots=True)
class _TextSession:
    group_id: UUID
    text_object: TextObject
    existing_id: UUID | None = None
    selected_all: bool = True
    cursor: int = 0


class TextTool:
    """Create and edit a fixed-frame text object without rasterizing it."""

    def __init__(
        self,
        group_provider: Callable[[], VectorGroupLayer | None] | None = None,
    ) -> None:
        self._context: ToolContext | None = None
        self._group_provider = group_provider
        self._draft_start: Point | None = None
        self._draft_rect: QRectF | None = None
        self._session: _TextSession | None = None
        self._unsubscribe: Unsubscribe | None = None
        self._overlay_rect: QGraphicsRectItem | None = None

    @property
    def has_active_session(self) -> bool:
        return self._session is not None or self._draft_start is not None

    def activate(self, context: ToolContext) -> None:
        self._context = context
        context.canvas.set_tool_cursor(Qt.CursorShape.IBeamCursor)

    def deactivate(self) -> None:
        self.cancel()
        if self._context is not None:
            self._context.canvas.set_tool_cursor(None)
        self._context = None

    def mouse_press(self, event: QMouseEvent) -> bool:
        context = self._context
        if context is None:
            return False
        if event.button() == Qt.MouseButton.RightButton and self._draft_start is not None:
            self._cancel_draft()
            return True
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if self._session is not None:
            return True
        if context.document_provider() is None:
            return False
        scene_point = context.canvas.mapToScene(event.position().toPoint())
        self._draft_start = Point(scene_point.x(), scene_point.y())
        self._draft_rect = QRectF(scene_point, scene_point)
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        if self._draft_start is None:
            return self._session is not None
        scene_point = (
            self._context.canvas.mapToScene(event.position().toPoint())
            if self._context
            else QPointF()
        )
        start = QPointF(self._draft_start.x, self._draft_start.y)
        self._draft_rect = QRectF(start, scene_point).normalized()
        self._show_draft_overlay(self._draft_rect)
        return True

    def mouse_release(self, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton or self._draft_start is None:
            return self._session is not None
        rect = self._draft_rect
        self._draft_start = None
        self._draft_rect = None
        if rect is None or rect.width() < 2.0 or rect.height() < 2.0:
            point = QPointF(rect.left(), rect.top()) if rect is not None else QPointF()
            rect = QRectF(point.x(), point.y(), 256.0, 64.0)
        self._create_text_object(rect)
        return True

    def mouse_double_click(self, event: QMouseEvent) -> bool:
        _ = event
        return False

    def key_press(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Escape and self._draft_start is not None:
            self._cancel_draft()
            return True
        session = self._session
        if session is None:
            return False
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
            return True
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._insert_text("\n")
            else:
                self.commit()
            return True
        if event.key() == Qt.Key.Key_Backspace:
            if session.selected_all:
                session.text_object.text = ""
                session.selected_all = False
                session.cursor = 0
            elif session.cursor > 0:
                text = session.text_object.text
                session.text_object.text = text[: session.cursor - 1] + text[session.cursor :]
                session.cursor -= 1
            self._refresh_preview()
            return True
        text = event.text()
        if text and text.isprintable():
            self._insert_text(text)
            return True
        return False

    def _create_text_object(self, rect: QRectF) -> None:
        context = self._context
        if context is None or context.commands is None:
            self._cancel_draft()
            return
        document = context.document_provider()
        if document is None:
            self._cancel_draft()
            return
        selected = (
            context.selected_node_provider()
            if context.selected_node_provider is not None
            else None
        )
        insertion_index: int | None = None
        group = selected if isinstance(selected, VectorGroupLayer) else None
        if isinstance(selected, TextObject):
            try:
                group, selected_object = document.text_object_by_id(selected.id)
            except ValueError:
                group = None
            else:
                insertion_index = group.children.index(selected_object)
        if group is None:
            if not context.commands.create_vector_group():
                self._cancel_draft()
                return
            group = document.vector_groups[-1] if document.vector_groups else None
        if group is None:
            self._cancel_draft()
            return
        frame = FloatRect(
            rect.x(),
            rect.y(),
            max(2.0, rect.width()),
            max(2.0, rect.height()),
        )
        self._cancel_draft()
        if not context.commands.create_text_object(
            group.id,
            frame,
            index=insertion_index,
        ):
            return
        created = (
            group.children[-1]
            if insertion_index is None
            else group.children[insertion_index]
        )
        if context.select_node is not None:
            context.select_node(created)

    def _cancel_draft(self) -> None:
        self._draft_start = None
        self._draft_rect = None
        if self._context is not None:
            self._context.overlays.clear()
        self._overlay_rect = None

    def key_release(self, event: QKeyEvent) -> bool:
        _ = event
        return self._session is not None

    def _insert_text(self, text: str) -> None:
        session = self._session
        if session is None:
            return
        if session.selected_all:
            session.text_object.text = text
            session.cursor = len(text)
            session.selected_all = False
        else:
            value = session.text_object.text
            session.text_object.text = value[: session.cursor] + text + value[session.cursor :]
            session.cursor += len(text)
        self._refresh_preview()

    def _start_session(
        self,
        rect: QRectF,
        *,
        existing: tuple[VectorGroupLayer, TextObject] | None = None,
    ) -> None:
        context = self._context
        if context is None:
            return
        document = context.document_provider()
        if document is None:
            return
        group = existing[0] if existing is not None else None
        if self._group_provider is not None:
            group = group or self._group_provider()
        if group is None and document.vector_groups:
            group = document.vector_groups[-1]
        if group is None and context.commands is not None:
            context.commands.create_vector_group()
            group = document.vector_groups[-1] if document.vector_groups else None
        if group is None:
            self._status("请先创建文字组")
            return
        if existing is None:
            text_object = TextObject.create(
                FloatRect(rect.x(), rect.y(), max(2.0, rect.width()), max(2.0, rect.height()))
            )
            existing_id = None
        else:
            text_object = replace(existing[1])
            existing_id = text_object.id
        self._session = _TextSession(group.id, text_object, existing_id=existing_id)
        self._unsubscribe = document.subscribe(self._on_document_event)
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        context = self._context
        session = self._session
        if context is None or session is None:
            return
        context.canvas.replace_vector_preview(
            session.group_id,
            self._group_preview_objects(session.group_id, session.text_object),
        )
        self._show_text_overlay(session.text_object)

    def _group_preview_objects(
        self,
        group_id: UUID,
        preview: TextObject,
    ) -> tuple[TextObject, ...]:
        if self._context is None:
            return (preview,)
        document = self._context.document_provider()
        if document is None:
            return (preview,)
        try:
            group = document.vector_group_by_id(group_id)
        except ValueError:
            return (preview,)
        result: list[TextObject] = []
        for child in group.children:
            if child.id == preview.id:
                result.append(preview)
            elif isinstance(child, TextObject):
                result.append(child)
        return tuple(result)

    def _show_draft_overlay(self, rect: QRectF) -> None:
        if self._context is None:
            return
        if self._overlay_rect is None:
            self._overlay_rect = QGraphicsRectItem()
            pen = QPen(QColor("#ff8a00"), 1.0)
            pen.setCosmetic(True)
            self._overlay_rect.setPen(pen)
            self._context.overlays.add(self._overlay_rect)
        self._overlay_rect.setRect(rect)

    def _show_text_overlay(self, text_object: TextObject) -> None:
        self._show_draft_overlay(
            QRectF(
                text_object.frame.x,
                text_object.frame.y,
                text_object.frame.width,
                text_object.frame.height,
            )
        )

    def commit(self) -> None:
        context = self._context
        session = self._session
        if context is None or session is None or context.commands is None:
            return
        if session.existing_id is None:
            context.commands.create_text_object(
                session.group_id,
                session.text_object.frame,
                text=session.text_object.text,
            )
        else:
            context.commands.update_text_object(
                session.existing_id,
                text=session.text_object.text,
            )
        self._finish()

    def cancel(self) -> None:
        self._finish()

    def _finish(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._context is not None:
            self._context.canvas.clear_vector_preview()
            self._context.overlays.clear()
        self._overlay_rect = None
        self._session = None
        self._draft_start = None
        self._draft_rect = None

    def _on_document_event(self, event: DocumentEvent) -> None:
        if self._session is not None and event.revision != 0:
            self.cancel()

    def _status(self, message: str) -> None:
        if self._context is not None and self._context.status is not None:
            self._context.status(message)

    def _find_text_object(
        self,
        scene_point: QPointF,
    ) -> tuple[VectorGroupLayer, TextObject] | None:
        context = self._context
        document = context.document_provider() if context is not None else None
        if document is None:
            return None
        point = Point(scene_point.x(), scene_point.y())
        for group in reversed(document.vector_groups):
            if not group.visible:
                continue
            for text_object in reversed(group.children):
                if not isinstance(text_object, TextObject):
                    continue
                if not text_object.visible:
                    continue
                try:
                    local = text_object.transform.inverse().map_point(point)
                except ValueError:
                    continue
                frame = text_object.frame
                if frame.x <= local.x <= frame.right and frame.y <= local.y <= frame.bottom:
                    return group, text_object
        return None



@dataclass(slots=True)
class _TextTransformSession:
    group_id: UUID
    object_id: UUID
    initial: TextObject
    current: TextObject
    press: Point | None = None
    drag_kind: Literal["move", "resize", "rotate", "pivot"] | None = None
    corner: int = -1
    initial_angle: float = 0.0
    locked: bool = False


class TextTransformTool:
    """Non-destructive transform tool for a selected vector text object.

    Raster ``TransformTool`` remains untouched.  This small adapter deliberately
    commits frame/affine changes through the text command service so selecting a
    text object can never accidentally transform the active raster layer.
    """

    def __init__(
        self,
        selected_provider: Callable[[], object | None],
        preview_sink: Callable[[TextObject], None] | None = None,
    ) -> None:
        self._selected_provider = selected_provider
        self._preview_sink = preview_sink
        self._context: ToolContext | None = None
        self._session: _TextTransformSession | None = None
        self._unsubscribe: Unsubscribe | None = None
        self._outline: QGraphicsPolygonItem | None = None
        self._handles: list[QGraphicsRectItem] = []
        self._pivot: QGraphicsEllipseItem | None = None
        self._rotation_handles: list[QGraphicsPathItem] = []
        self._suppress_right_release = False

    @property
    def has_active_session(self) -> bool:
        return self._session is not None

    def activate(self, context: ToolContext) -> None:
        self._context = context
        context.canvas.set_tool_cursor(Qt.CursorShape.ArrowCursor)
        self._begin_session()

    def deactivate(self) -> None:
        self.cancel()
        if self._context is not None:
            self._context.canvas.set_tool_cursor(None)
        self._context = None

    def mouse_press(self, event: QMouseEvent) -> bool:
        session = self._session
        context = self._context
        if context is None or session is None:
            return False
        if event.button() == Qt.MouseButton.RightButton:
            if session.drag_kind is None:
                return False
            session.current = replace(session.initial)
            session.drag_kind = None
            self._suppress_right_release = True
            self._refresh_preview()
            self._status("已撤销本次文字变换")
            return True
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if session.locked:
            self._status("文字对象已锁定，无法变换")
            return True
        point = self._document_point(event)
        hit, corner = self._hit_test(point)
        if hit is None:
            return False
        session.press = point
        session.drag_kind = hit
        session.corner = corner
        if hit == "rotate":
            center = self._center(session.current)
            session.initial_angle = math.atan2(point.y - center.y, point.x - center.x)
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        session = self._session
        if session is None or session.press is None or session.drag_kind is None:
            return False
        point = self._document_point(event)
        if session.drag_kind == "move":
            dx = point.x - session.press.x
            dy = point.y - session.press.y
            frame = session.initial.frame
            try:
                inverse = session.initial.transform.inverse()
                local_start = inverse.map_point(session.press)
                local_point = inverse.map_point(point)
                local_dx = local_point.x - local_start.x
                local_dy = local_point.y - local_start.y
            except ValueError:
                local_dx, local_dy = dx, dy
            pivot = self._center(session.initial)
            session.current = replace(
                session.initial,
                frame=FloatRect(
                    frame.x + local_dx,
                    frame.y + local_dy,
                    frame.width,
                    frame.height,
                ),
                transform_pivot=Point(pivot.x + dx, pivot.y + dy),
            )
        elif session.drag_kind == "resize":
            self._resize_from_corner(point)
        elif session.drag_kind == "rotate":
            self._rotate_from_point(point)
        else:
            session.current = replace(session.initial, transform_pivot=point)
        self._refresh_preview()
        return True

    def mouse_release(self, event: QMouseEvent) -> bool:
        if event.button() == Qt.MouseButton.RightButton and self._suppress_right_release:
            self._suppress_right_release = False
            return True
        session = self._session
        if event.button() != Qt.MouseButton.LeftButton or session is None:
            return False
        if session.drag_kind is None:
            return False
        session.drag_kind = None
        session.press = None
        if (
            session.current.frame != session.initial.frame
            or not session.current.transform.almost_equals(session.initial.transform)
            or session.current.transform_pivot != session.initial.transform_pivot
        ) and self._context is not None and self._context.commands is not None:
            self._context.commands.update_text_object(
                session.object_id,
                frame=session.current.frame,
                transform=session.current.transform,
                transform_pivot=session.current.transform_pivot,
            )
            self._begin_session()
        else:
            self._refresh_preview()
        return True

    def context_menu(self, event: object) -> bool:
        _ = event
        if self._suppress_right_release:
            self._suppress_right_release = False
            return True
        return False

    def key_press(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
            return True
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit()
            return True
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        _ = event
        return False

    def cancel(self) -> None:
        self._clear_session()

    def commit(self) -> None:
        session = self._session
        context = self._context
        if session is None or context is None or context.commands is None:
            return
        if (
            session.current.frame != session.initial.frame
            or not session.current.transform.almost_equals(session.initial.transform)
            or session.current.transform_pivot != session.initial.transform_pivot
        ):
            context.commands.update_text_object(
                session.object_id,
                frame=session.current.frame,
                transform=session.current.transform,
                transform_pivot=session.current.transform_pivot,
            )
        self._begin_session()

    def _begin_session(self) -> None:
        self._clear_session()
        context = self._context
        selected = self._selected_provider()
        if context is None or not isinstance(selected, TextObject):
            return
        document = context.document_provider()
        if document is None:
            return
        try:
            group, current = document.text_object_by_id(selected.id)
        except ValueError:
            return
        current_with_pivot = replace(
            current,
            transform_pivot=self._effective_pivot(current),
        )
        self._session = _TextTransformSession(
            group.id,
            current.id,
            current_with_pivot,
            replace(current_with_pivot),
            locked=group.locked or current.locked,
        )
        self._unsubscribe = document.subscribe(self._on_document_event)
        self._refresh_preview()
        if self._session.locked:
            self._status("文字对象已锁定；可查看边界但无法变换")

    def _clear_session(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._context is not None:
            self._context.canvas.clear_vector_preview()
            self._context.overlays.clear()
        self._session = None
        self._outline = None
        self._handles.clear()
        self._pivot = None
        self._rotation_handles.clear()

    def _refresh_preview(self) -> None:
        context = self._context
        session = self._session
        if context is None or session is None:
            return
        document = context.document_provider()
        objects: tuple[TextObject, ...] = (session.current,)
        if document is not None:
            try:
                group = document.vector_group_by_id(session.group_id)
            except ValueError:
                pass
            else:
                current_objects: list[TextObject] = []
                for child in group.children:
                    if child.id == session.object_id:
                        current_objects.append(session.current)
                    elif isinstance(child, TextObject):
                        current_objects.append(child)
                objects = tuple(current_objects)
        context.canvas.replace_vector_preview(session.group_id, objects)
        self._show_overlay(session.current)
        if self._preview_sink is not None:
            self._preview_sink(session.current)

    def _show_overlay(self, text_object: TextObject) -> None:
        context = self._context
        if context is None:
            return
        points = self._frame_handle_points(text_object)
        if self._outline is None:
            self._outline = QGraphicsPolygonItem()
            outline_pen = QPen(QColor("#ff8a00"), 1.0)
            outline_pen.setCosmetic(True)
            self._outline.setPen(outline_pen)
            self._outline.setBrush(Qt.BrushStyle.NoBrush)
            context.overlays.add(self._outline)
        self._outline.setPolygon(
            QPolygonF(
                [
                    QPointF(points[index].x, points[index].y)
                    for index in (0, 2, 4, 6)
                ]
            )
        )
        while len(self._handles) < 8:
            handle = QGraphicsRectItem(-4.0, -4.0, 8.0, 8.0)
            handle.setPen(QPen(QColor("#ff8a00"), 1.0))
            handle.setBrush(QColor("#1b1b1b"))
            handle.setFlag(
                QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations,
                True,
            )
            context.overlays.add(handle)
            self._handles.append(handle)
        for handle, point in zip(self._handles, points, strict=True):
            handle.setPos(point.x, point.y)
        center = self._center(text_object)
        if self._pivot is None:
            self._pivot = QGraphicsEllipseItem(-4.0, -4.0, 8.0, 8.0)
            pivot_pen = QPen(QColor("#ff8a00"), 1.0)
            pivot_pen.setCosmetic(True)
            self._pivot.setPen(pivot_pen)
            self._pivot.setBrush(Qt.BrushStyle.NoBrush)
            self._pivot.setFlag(
                QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations,
                True,
            )
            context.overlays.add(self._pivot)
        self._pivot.setPos(center.x, center.y)
        rotation_pen = QPen(QColor("#ff8a00"), 1.0)
        rotation_pen.setCosmetic(True)
        while len(self._rotation_handles) < 4:
            arrow = QGraphicsPathItem(_rotation_arrow_path())
            arrow.setPen(rotation_pen)
            arrow.setBrush(Qt.BrushStyle.NoBrush)
            arrow.setFlag(
                QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations,
                True,
            )
            context.overlays.add(arrow)
            self._rotation_handles.append(arrow)
        corners = (points[0], points[2], points[4], points[6])
        for arrow, rotation, corner in zip(
            self._rotation_handles,
            self._rotation_points(text_object),
            corners,
            strict=True,
        ):
            arrow.setPos(rotation.x, rotation.y)
            arrow.setRotation(
                math.degrees(
                    math.atan2(corner.y - center.y, corner.x - center.x)
                )
                + 135.0
            )

    def _hit_test(
        self,
        point: Point,
    ) -> tuple[Literal["move", "resize", "rotate", "pivot"] | None, int]:
        session = self._session
        if session is None:
            return None, -1
        tolerance = (
            8.0 / max(0.01, float(getattr(self._context.canvas, "zoom", 1.0)))
            if self._context
            else 8.0
        )
        pivot = self._center(session.current)
        if math.hypot(point.x - pivot.x, point.y - pivot.y) <= tolerance:
            return "pivot", -1
        for index, handle in enumerate(self._frame_handle_points(session.current)):
            if math.hypot(point.x - handle.x, point.y - handle.y) <= tolerance:
                return "resize", index
        for index, rotation in enumerate(self._rotation_points(session.current)):
            if math.hypot(point.x - rotation.x, point.y - rotation.y) <= tolerance * 1.5:
                return "rotate", index
        try:
            local = session.current.transform.inverse().map_point(point)
        except ValueError:
            local = point
        frame = session.current.frame
        if frame.x <= local.x <= frame.right and frame.y <= local.y <= frame.bottom:
            return "move", -1
        return None, -1

    def _resize_from_corner(self, point: Point) -> None:
        session = self._session
        if session is None:
            return
        frame = session.initial.frame
        try:
            local = session.initial.transform.inverse().map_point(point)
        except ValueError:
            local = point
        left, top, right, bottom = frame.x, frame.y, frame.right, frame.bottom
        handle = session.corner
        if handle in (0, 6, 7):
            left = min(local.x, frame.right - 1.0)
        if handle in (0, 1, 2):
            top = min(local.y, frame.bottom - 1.0)
        if handle in (2, 3, 4):
            right = max(local.x, frame.x + 1.0)
        if handle in (4, 5, 6):
            bottom = max(local.y, frame.y + 1.0)
        session.current = replace(
            session.initial,
            frame=FloatRect(left, top, right - left, bottom - top),
        )

    def _rotate_from_point(self, point: Point) -> None:
        session = self._session
        if session is None:
            return
        center = self._center(session.initial)
        angle = math.atan2(point.y - center.y, point.x - center.x)
        delta = math.degrees(angle - session.initial_angle)
        around_center = (
            AffineTransform.translation(-center.x, -center.y)
            .then(AffineTransform.rotation(delta))
            .then(AffineTransform.translation(center.x, center.y))
        )
        session.current = replace(
            session.initial,
            transform=session.initial.transform.then(around_center),
            transform_pivot=center,
        )

    @staticmethod
    def _center(text_object: TextObject) -> Point:
        return TextTransformTool._effective_pivot(text_object)

    @staticmethod
    def _effective_pivot(text_object: TextObject) -> Point:
        if text_object.transform_pivot is not None:
            return text_object.transform_pivot
        frame = text_object.frame
        return text_object.transform.map_point(
            Point(frame.x + frame.width / 2.0, frame.y + frame.height / 2.0)
        )

    @staticmethod
    def _frame_handle_points(text_object: TextObject) -> tuple[Point, ...]:
        frame = text_object.frame
        middle_x = frame.x + frame.width / 2.0
        middle_y = frame.y + frame.height / 2.0
        return tuple(
            text_object.transform.map_point(point)
            for point in (
                Point(frame.x, frame.y),
                Point(middle_x, frame.y),
                Point(frame.right, frame.y),
                Point(frame.right, middle_y),
                Point(frame.right, frame.bottom),
                Point(middle_x, frame.bottom),
                Point(frame.x, frame.bottom),
                Point(frame.x, middle_y),
            )
        )

    def _rotation_points(self, text_object: TextObject) -> tuple[Point, ...]:
        center = self._center(text_object)
        distance = (
            27.0 / max(0.01, float(getattr(self._context.canvas, "zoom", 1.0)))
            if self._context is not None
            else 27.0
        )
        corners = self._frame_handle_points(text_object)
        result: list[Point] = []
        for corner in (corners[0], corners[2], corners[4], corners[6]):
            dx = corner.x - center.x
            dy = corner.y - center.y
            length = max(math.hypot(dx, dy), 1e-6)
            result.append(
                Point(
                    corner.x + dx / length * distance,
                    corner.y + dy / length * distance,
                )
            )
        return tuple(result)

    def _document_point(self, event: QMouseEvent) -> Point:
        assert self._context is not None
        point = self._context.canvas.mapToScene(event.position().toPoint())
        return Point(point.x(), point.y())

    def _on_document_event(self, event: DocumentEvent) -> None:
        if self._session is not None and event.revision != 0:
            selected = self._selected_provider()
            if isinstance(selected, TextObject) and selected.id == self._session.object_id:
                self._begin_session()
            else:
                self._clear_session()

    def _status(self, message: str) -> None:
        if self._context is not None and self._context.status is not None:
            self._context.status(message)


def _rotation_arrow_path() -> QPainterPath:
    path = QPainterPath(QPointF(-7.0, 4.0))
    path.cubicTo(-7.0, -3.0, -3.0, -7.0, 4.0, -7.0)
    path.moveTo(1.0, -10.0)
    path.lineTo(5.0, -7.0)
    path.lineTo(1.0, -4.0)
    return path


__all__ = ["TextTool", "TextTransformTool"]
