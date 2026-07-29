"""Editable raster shape tool with session-local history."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, cast

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QGraphicsPathItem, QGraphicsRectItem, QProgressDialog

from easypicture.domain import DocumentEvent, Point
from easypicture.imaging import (
    ShapeGeometry,
    ShapeKind,
    ShapeRasterRequest,
    ShapeRasterResult,
    ShapeSettings,
    calculate_shape_raster,
    shape_affected_pixel_area,
    shape_affected_tile_count,
)
from easypicture.jobs import JobHandle, JobOutcome, JobStatus
from easypicture.persistence import DocumentSnapshot
from easypicture.tools.protocol import ToolContext

type ShapeHandle = Literal[
    "p0",
    "p1",
    "tl",
    "tm",
    "tr",
    "mr",
    "br",
    "bm",
    "bl",
    "ml",
    "radius",
] | tuple[Literal["vertex"], int]
type DragKind = Literal["create", "move", "handle"]

_ORANGE = QColor("#ff8a00")
_HANDLE_RADIUS_PX = 4.0
_HIT_RADIUS_PX = 9.0
_BACKGROUND_AREA = 4_000_000
_BACKGROUND_TILES = 64


class _NoCancellation:
    def raise_if_cancelled(self) -> None:
        return


@dataclass(slots=True)
class _Drag:
    kind: DragKind
    start: Point
    initial: ShapeGeometry | None
    handle: ShapeHandle | None = None
    current: ShapeGeometry | None = None


@dataclass(slots=True)
class _Session:
    snapshot: DocumentSnapshot
    geometry: ShapeGeometry
    settings: ShapeSettings
    unsubscribe: Callable[[], None]
    history: list[ShapeGeometry] = field(default_factory=list)
    history_index: int = 0
    drag: _Drag | None = None
    generation: int = 0
    latest_result: ShapeRasterResult | None = None
    pending_commit: bool = False
    property_start: ShapeGeometry | None = None


class ShapeTool:
    """Create and edit one temporary shape before one pixel command commits it."""

    def __init__(
        self,
        kind_provider: Callable[[], ShapeKind],
        settings_provider: Callable[[], ShapeSettings],
        geometry_sink: Callable[[ShapeGeometry | None], None] | None = None,
        history_sink: Callable[[], None] | None = None,
    ) -> None:
        self._kind_provider = kind_provider
        self._settings_provider = settings_provider
        self._geometry_sink = geometry_sink
        self._history_sink = history_sink
        self._context: ToolContext | None = None
        self._session: _Session | None = None
        self._draft_start: Point | None = None
        self._draft_geometry: ShapeGeometry | None = None
        self._polygon_points: list[tuple[float, float]] = []
        self._polygon_hover: Point | None = None
        self._overlay_items: list[object] = []
        self._handle: JobHandle | None = None
        self._progress: QProgressDialog | None = None
        self._suppress_right_release = False
        self._suppress_context_menu = False

    @property
    def has_active_session(self) -> bool:
        return self._session is not None

    @property
    def can_undo_preview(self) -> bool:
        session = self._session
        return session is not None and session.history_index > 0

    @property
    def can_redo_preview(self) -> bool:
        session = self._session
        return session is not None and session.history_index < len(session.history) - 1

    def activate(self, context: ToolContext) -> None:
        self._context = context
        context.canvas.set_tool_cursor(Qt.CursorShape.CrossCursor)

    def deactivate(self) -> None:
        self.cancel()
        if self._context is not None:
            self._context.canvas.set_tool_cursor(None)
        self._context = None

    def settings_changed(self) -> None:
        session = self._session
        if session is None:
            return
        settings = self._settings_provider()
        if settings == session.settings:
            return
        session.settings = settings
        self._request_preview()

    def kind_changed(self) -> None:
        self.cancel()

    def mouse_press(self, event: QMouseEvent) -> bool:
        context = self._context
        if context is None:
            return False
        if event.button() == Qt.MouseButton.RightButton:
            if self._session is not None and self._session.drag is not None:
                self._session.drag = None
                self._rebuild_overlay(self._session.geometry)
                self._emit_geometry(self._session.geometry)
                self._request_preview()
                self._suppress_right_release = True
                self._suppress_context_menu = True
                return True
            if self._polygon_points:
                self._polygon_points.pop()
                self._polygon_hover = None
                self._rebuild_polygon_draft()
                self._suppress_right_release = True
                self._suppress_context_menu = True
                return True
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        document = context.document_provider()
        if document is None:
            return False
        if document.active_layer.locked:
            self._status("图层已锁定，无法绘制图形")
            return True
        point = self._event_point(event)
        if self._session is not None:
            handle = self._hit_handle(point)
            if handle is not None:
                self._session.drag = _Drag(
                    "handle",
                    point,
                    self._session.geometry,
                    handle=handle,
                )
                self._suspend_exact_preview()
                return True
            if self._contains(self._session.geometry, point):
                self._session.drag = _Drag("move", point, self._session.geometry)
                self._suspend_exact_preview()
                return True
            return True
        kind = self._kind_provider()
        if kind is ShapeKind.POLYGON:
            self._polygon_points.append((point.x, point.y))
            self._polygon_hover = point
            self._rebuild_polygon_draft()
            return True
        self._draft_start = point
        self._draft_geometry = None
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        point = self._event_point(event)
        session = self._session
        if session is not None and session.drag is not None:
            geometry = self._dragged_geometry(
                session.drag,
                point,
                event.modifiers(),
            )
            if geometry is not None:
                session.drag.current = geometry
                self._rebuild_overlay(geometry, lightweight_preview=True)
                self._emit_geometry(geometry)
            return True
        if self._draft_start is not None:
            geometry = self._created_geometry(
                self._kind_provider(),
                self._draft_start,
                point,
                event.modifiers(),
            )
            self._draft_geometry = geometry
            if geometry is not None:
                self._rebuild_overlay(geometry, lightweight_preview=True)
            return True
        if self._polygon_points:
            self._polygon_hover = point
            self._rebuild_polygon_draft()
            return True
        return False

    def mouse_release(self, event: QMouseEvent) -> bool:
        if event.button() == Qt.MouseButton.RightButton and self._suppress_right_release:
            self._suppress_right_release = False
            return True
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        session = self._session
        if session is not None and session.drag is not None:
            geometry = session.drag.current
            session.drag = None
            if geometry is not None and geometry != session.geometry:
                self._set_geometry(geometry, record=True)
            else:
                self._rebuild_overlay(session.geometry)
                self._request_preview()
            return True
        if self._draft_start is not None:
            geometry = self._draft_geometry
            self._draft_start = None
            self._draft_geometry = None
            if geometry is not None:
                self._start_session(geometry)
            else:
                self._clear_overlay()
            return True
        return False

    def mouse_double_click(self, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if self._polygon_points:
            if len(self._polygon_points) > 1:
                click = self._event_point(event)
                if _distance(self._polygon_points[-1], (click.x, click.y)) < 1e-6:
                    self._polygon_points.pop()
            self._close_polygon()
            return True
        if self._session is not None and self._contains(
            self._session.geometry,
            self._event_point(event),
        ):
            self.commit()
            return True
        return False

    def context_menu(self, event: object) -> bool:
        _ = event
        if self._suppress_context_menu:
            self._suppress_context_menu = False
            return True
        return False

    def key_press(self, event: QKeyEvent) -> bool:
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_Z:
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    return self.redo_preview()
                return self.undo_preview()
            if event.key() == Qt.Key.Key_Y:
                return self.redo_preview()
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._polygon_points:
                self._close_polygon()
            elif self._session is not None:
                self.commit()
            return True
        if event.key() == Qt.Key.Key_Escape:
            if self._session is not None or self._polygon_points or self._draft_start:
                self.cancel()
                return True
        if event.key() == Qt.Key.Key_Backspace and self._polygon_points:
            self._polygon_points.pop()
            self._rebuild_polygon_draft()
            return True
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        _ = event
        return False

    def set_geometry(self, geometry: ShapeGeometry) -> None:
        session = self._session
        if session is None or geometry == session.geometry:
            return
        if session.property_start is not None:
            session.geometry = geometry
            self._rebuild_overlay(geometry, lightweight_preview=True)
            self._emit_geometry(geometry)
            return
        self._set_geometry(geometry, record=True)

    def begin_property_edit(self) -> None:
        if self._session is not None and self._session.property_start is None:
            self._session.property_start = self._session.geometry
            self._suspend_exact_preview()

    def finish_property_edit(self) -> None:
        session = self._session
        if session is None or session.property_start is None:
            return
        start = session.property_start
        session.property_start = None
        if session.geometry != start:
            del session.history[session.history_index + 1 :]
            session.history.append(session.geometry)
            session.history_index = len(session.history) - 1
        self._request_preview()
        self._notify_history()

    def cancel_property_edit(self) -> None:
        session = self._session
        if session is None or session.property_start is None:
            return
        geometry = session.property_start
        session.property_start = None
        session.geometry = geometry
        self._geometry_changed()

    def undo_preview(self) -> bool:
        session = self._session
        if session is None or session.history_index <= 0:
            return False
        session.history_index -= 1
        session.geometry = session.history[session.history_index]
        self._geometry_changed()
        return True

    def redo_preview(self) -> bool:
        session = self._session
        if session is None or session.history_index >= len(session.history) - 1:
            return False
        session.history_index += 1
        session.geometry = session.history[session.history_index]
        self._geometry_changed()
        return True

    def commit(self) -> bool:
        session = self._session
        context = self._context
        if session is None or context is None:
            return False
        if self._handle is not None or session.latest_result is None:
            session.pending_commit = True
            return True
        if not self._is_current(session):
            self._status("文档已变化，临时图形已取消")
            self.cancel()
            return False
        result = session.latest_result
        updates = dict(result.updates)
        commands = context.commands
        self._clear_session()
        if commands is None or not updates:
            self._status("图形未产生像素变化")
            return False
        return commands.apply_pixel_tiles(result.layer_id, updates, "绘制图形")

    def cancel(self) -> None:
        self._cancel_job()
        self._draft_start = None
        self._draft_geometry = None
        self._polygon_points.clear()
        self._polygon_hover = None
        self._clear_session()
        self._clear_overlay()

    def _start_session(self, geometry: ShapeGeometry) -> None:
        context = self._context
        if context is None:
            return
        document = context.document_provider()
        if document is None:
            return
        snapshot = DocumentSnapshot.capture(document)

        def on_event(event: DocumentEvent) -> None:
            _ = event
            session = self._session
            if session is not None and not self._is_current(session):
                self._status("文档或活动图层已变化，临时图形已取消")
                self.cancel()

        session = _Session(
            snapshot=snapshot,
            geometry=geometry,
            settings=self._settings_provider(),
            unsubscribe=document.subscribe(on_event),
            history=[geometry],
        )
        self._session = session
        self._rebuild_overlay(geometry)
        self._emit_geometry(geometry)
        self._request_preview()
        self._notify_history()

    def _close_polygon(self) -> None:
        points = tuple(self._polygon_points)
        self._polygon_points.clear()
        self._polygon_hover = None
        try:
            geometry = ShapeGeometry(ShapeKind.POLYGON, points)
        except ValueError as error:
            self._status(f"多边形无效：{error}")
            self._clear_overlay()
            return
        self._start_session(geometry)

    def _set_geometry(self, geometry: ShapeGeometry, *, record: bool) -> None:
        session = self._session
        if session is None:
            return
        session.geometry = geometry
        if record:
            del session.history[session.history_index + 1 :]
            session.history.append(geometry)
            session.history_index = len(session.history) - 1
        self._geometry_changed()

    def _geometry_changed(self) -> None:
        session = self._session
        if session is None:
            return
        self._rebuild_overlay(session.geometry)
        self._emit_geometry(session.geometry)
        self._request_preview()
        self._notify_history()

    def _request_preview(self) -> None:
        session = self._session
        context = self._context
        if session is None or context is None:
            return
        self._cancel_job()
        session.generation += 1
        generation = session.generation
        session.latest_result = None
        self._rebuild_overlay(session.geometry, lightweight_preview=True)
        context.canvas.clear_pixel_preview()
        request = ShapeRasterRequest(
            session.snapshot,
            session.snapshot.active_layer_id,
            session.geometry,
            session.settings,
        )
        area = shape_affected_pixel_area(session.geometry, session.settings)
        tiles = shape_affected_tile_count(
            session.geometry,
            session.settings,
            session.snapshot.size.width,
            session.snapshot.size.height,
        )
        if (
            context.jobs is not None
            and (area > _BACKGROUND_AREA or tiles > _BACKGROUND_TILES)
        ):
            handle = context.jobs.submit(
                lambda token, progress: calculate_shape_raster(
                    request,
                    token,
                    progress,
                )
            )
            self._handle = handle
            handle.finished.connect(
                lambda outcome, current=generation, source=handle: self._preview_finished(
                    outcome,
                    current,
                    source,
                )
            )
            self._show_progress(handle)
            return
        try:
            result = calculate_shape_raster(
                request,
                _NoCancellation(),
                lambda _value, _maximum, _message: None,
            )
        except (PermissionError, ValueError) as error:
            self._status(str(error))
            return
        self._accept_preview(result, generation)

    def _suspend_exact_preview(self) -> None:
        session = self._session
        if session is None or self._context is None:
            return
        self._cancel_job()
        session.generation += 1
        session.latest_result = None
        session.pending_commit = False
        self._rebuild_overlay(session.geometry, lightweight_preview=True)
        self._context.canvas.clear_pixel_preview()

    def _preview_finished(
        self,
        outcome: JobOutcome,
        generation: int,
        handle: JobHandle,
    ) -> None:
        if self._handle is not handle:
            return
        self._close_progress()
        self._handle = None
        if outcome.status is JobStatus.CANCELLED:
            return
        if outcome.status is JobStatus.FAILED:
            self._status(f"图形预览失败：{outcome.error}")
            return
        if not isinstance(outcome.value, ShapeRasterResult):
            self._status("图形预览返回了无效结果")
            return
        self._accept_preview(outcome.value, generation)

    def _accept_preview(self, result: ShapeRasterResult, generation: int) -> None:
        session = self._session
        context = self._context
        if (
            session is None
            or context is None
            or generation != session.generation
            or result.document_id != session.snapshot.document_id
            or result.revision != session.snapshot.revision
            or result.layer_id != session.snapshot.active_layer_id
            or not self._is_current(session)
        ):
            return
        session.latest_result = result
        context.canvas.set_pixel_preview(result.layer_id, dict(result.updates))
        self._rebuild_overlay(session.geometry)
        if session.pending_commit:
            session.pending_commit = False
            self.commit()

    def _is_current(self, session: _Session) -> bool:
        if self._context is None:
            return False
        document = self._context.document_provider()
        return (
            document is not None
            and document.id == session.snapshot.document_id
            and document.revision == session.snapshot.revision
            and document.active_layer_id == session.snapshot.active_layer_id
        )

    def _clear_session(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            session.unsubscribe()
        if self._context is not None:
            self._context.canvas.clear_pixel_preview()
        self._emit_geometry(None)
        self._notify_history()
        self._clear_overlay()

    def _cancel_job(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._close_progress()

    def _show_progress(self, handle: JobHandle) -> None:
        if self._context is None:
            return
        dialog = QProgressDialog(
            "正在生成图形预览…",
            "取消",
            0,
            0,
            self._context.canvas,
        )
        dialog.setWindowTitle("图形预览")
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setMinimumDuration(150)
        dialog.setAutoClose(False)
        dialog.canceled.connect(handle.cancel)
        handle.progress.connect(
            lambda value, maximum, message: self._update_progress(
                dialog,
                value,
                maximum,
                message,
            )
        )
        self._progress = dialog

    @staticmethod
    def _update_progress(
        dialog: QProgressDialog,
        value: int,
        maximum: int,
        message: str,
    ) -> None:
        dialog.setMaximum(maximum)
        dialog.setValue(value)
        if message:
            dialog.setLabelText(message)

    def _close_progress(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress.deleteLater()
            self._progress = None

    def _event_point(self, event: QMouseEvent) -> Point:
        assert self._context is not None
        return self._context.canvas.viewport_to_document(event.position().toPoint())

    def _created_geometry(
        self,
        kind: ShapeKind,
        start: Point,
        end: Point,
        modifiers: Qt.KeyboardModifier,
    ) -> ShapeGeometry | None:
        x0, y0 = start.x, start.y
        x1, y1 = end.x, end.y
        if kind is ShapeKind.LINE and modifiers & Qt.KeyboardModifier.ShiftModifier:
            distance = math.hypot(x1 - x0, y1 - y0)
            angle = round(math.atan2(y1 - y0, x1 - x0) / (math.pi / 4)) * math.pi / 4
            x1 = x0 + math.cos(angle) * distance
            y1 = y0 + math.sin(angle) * distance
        elif (
            kind in (
                ShapeKind.RECTANGLE,
                ShapeKind.ROUNDED_RECTANGLE,
                ShapeKind.ELLIPSE,
            )
            and modifiers & Qt.KeyboardModifier.ShiftModifier
        ):
            size = max(abs(x1 - x0), abs(y1 - y0))
            x1 = x0 + math.copysign(size, x1 - x0 or 1.0)
            y1 = y0 + math.copysign(size, y1 - y0 or 1.0)
        if modifiers & Qt.KeyboardModifier.AltModifier:
            x0, x1 = x0 - (x1 - x0), x1
            y0, y1 = y0 - (y1 - y0), y1
        try:
            return ShapeGeometry(kind, ((x0, y0), (x1, y1)))
        except ValueError:
            return None

    def _dragged_geometry(
        self,
        drag: _Drag,
        point: Point,
        modifiers: Qt.KeyboardModifier,
    ) -> ShapeGeometry | None:
        initial = drag.initial
        if initial is None:
            return None
        dx = point.x - drag.start.x
        dy = point.y - drag.start.y
        if drag.kind == "move":
            return _translated(initial, dx, dy)
        handle = drag.handle
        if isinstance(handle, tuple):
            points = list(initial.points)
            points[handle[1]] = (point.x, point.y)
            try:
                return ShapeGeometry(initial.kind, tuple(points), initial.corner_radius)
            except ValueError:
                return None
        if handle in {"p0", "p1"}:
            points = list(initial.points)
            points[0 if handle == "p0" else 1] = (point.x, point.y)
            try:
                return ShapeGeometry(initial.kind, tuple(points), initial.corner_radius)
            except ValueError:
                return None
        if handle == "radius":
            left, top, right, bottom = initial.bounds
            radius = max(0.0, min(point.x - left, (right - left) / 2.0, (bottom - top) / 2.0))
            return ShapeGeometry(initial.kind, initial.points, radius)
        left, top, right, bottom = initial.bounds
        if handle is not None:
            if "l" in handle:
                left = point.x
            if "r" in handle:
                right = point.x
            if handle in {"tl", "tm", "tr"}:
                top = point.y
            if handle in {"bl", "bm", "br"}:
                bottom = point.y
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            size = max(abs(right - left), abs(bottom - top))
            if handle in {"tl", "ml", "bl"}:
                left = right - math.copysign(size, right - left or 1.0)
            else:
                right = left + math.copysign(size, right - left or 1.0)
            if handle in {"tl", "tm", "tr"}:
                top = bottom - math.copysign(size, bottom - top or 1.0)
            else:
                bottom = top + math.copysign(size, bottom - top or 1.0)
        try:
            return ShapeGeometry(
                initial.kind,
                ((left, top), (right, bottom)),
                initial.corner_radius,
            )
        except ValueError:
            return None

    def _rebuild_polygon_draft(self) -> None:
        self._clear_overlay()
        if not self._polygon_points or self._context is None:
            return
        points = list(self._polygon_points)
        if self._polygon_hover is not None:
            points.append((self._polygon_hover.x, self._polygon_hover.y))
        path = QPainterPath(QPointF(*points[0]))
        for point in points[1:]:
            path.lineTo(QPointF(*point))
        self._add_path(path)
        for point in self._polygon_points:
            self._add_handle(point)

    def _rebuild_overlay(
        self,
        geometry: ShapeGeometry,
        *,
        lightweight_preview: bool = False,
    ) -> None:
        self._clear_overlay()
        path = _shape_path(geometry)
        if lightweight_preview:
            self._add_lightweight_shape(path, geometry)
        self._add_path(path)
        if geometry.kind is ShapeKind.LINE:
            self._add_handle(geometry.points[0])
            self._add_handle(geometry.points[1])
        elif geometry.kind is ShapeKind.POLYGON:
            for point in geometry.points:
                self._add_handle(point)
        else:
            for point in _box_handles(geometry.bounds).values():
                self._add_handle(point)
            if geometry.kind is ShapeKind.ROUNDED_RECTANGLE:
                left, top, _, _ = geometry.bounds
                self._add_handle((left + geometry.effective_corner_radius, top))

    def _add_path(self, path: QPainterPath) -> None:
        if self._context is None:
            return
        item = QGraphicsPathItem(path)
        pen = QPen(_ORANGE)
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        item.setPen(pen)
        item.setBrush(Qt.BrushStyle.NoBrush)
        self._context.overlays.add(item)
        self._overlay_items.append(item)

    def _add_lightweight_shape(
        self,
        path: QPainterPath,
        geometry: ShapeGeometry,
    ) -> None:
        if self._context is None:
            return
        settings = self._settings_provider()
        item = QGraphicsPathItem(path)
        opacity = settings.opacity
        stroke = QColor.fromRgb(*settings.stroke_color)
        fill = QColor.fromRgb(*settings.fill_color)
        stroke.setAlphaF(stroke.alphaF() * opacity)
        fill.setAlphaF(fill.alphaF() * opacity)
        if geometry.kind is ShapeKind.LINE or settings.style.value in {
            "stroke",
            "stroke_and_fill",
        }:
            pen = QPen(stroke)
            pen.setWidthF(settings.stroke_width)
            pen.setCosmetic(False)
            item.setPen(pen)
        else:
            item.setPen(QPen(Qt.PenStyle.NoPen))
        if geometry.kind is not ShapeKind.LINE and settings.style.value in {
            "fill",
            "stroke_and_fill",
        }:
            item.setBrush(QBrush(fill))
        else:
            item.setBrush(Qt.BrushStyle.NoBrush)
        self._context.overlays.add(item)
        self._overlay_items.append(item)

    def _add_handle(self, point: tuple[float, float]) -> None:
        if self._context is None:
            return
        item = QGraphicsRectItem(-_HANDLE_RADIUS_PX, -_HANDLE_RADIUS_PX, 8.0, 8.0)
        item.setFlag(item.GraphicsItemFlag.ItemIgnoresTransformations, True)
        item.setPos(QPointF(*point))
        item.setPen(QPen(_ORANGE))
        item.setBrush(QBrush(QColor("#282828")))
        self._context.overlays.add(item)
        self._overlay_items.append(item)

    def _clear_overlay(self) -> None:
        if self._context is not None:
            self._context.overlays.clear()
        self._overlay_items.clear()

    def _hit_handle(self, point: Point) -> ShapeHandle | None:
        session = self._session
        context = self._context
        if session is None or context is None:
            return None
        threshold = _HIT_RADIUS_PX / max(context.canvas.zoom, 1e-6)
        geometry = session.geometry
        if geometry.kind is ShapeKind.LINE:
            candidates: list[tuple[ShapeHandle, tuple[float, float]]] = [
                ("p0", geometry.points[0]),
                ("p1", geometry.points[1]),
            ]
        elif geometry.kind is ShapeKind.POLYGON:
            candidates = [
                (("vertex", index), value)
                for index, value in enumerate(geometry.points)
            ]
        else:
            candidates = [
                (cast(ShapeHandle, name), value)
                for name, value in _box_handles(geometry.bounds).items()
            ]
            if geometry.kind is ShapeKind.ROUNDED_RECTANGLE:
                left, top, _, _ = geometry.bounds
                candidates.append(
                    (
                        "radius",
                        (left + geometry.effective_corner_radius, top),
                    )
                )
        for name, candidate in candidates:
            if _distance((point.x, point.y), candidate) <= threshold:
                return name
        return None

    def _contains(self, geometry: ShapeGeometry, point: Point) -> bool:
        path = _shape_path(geometry)
        threshold = 8.0 / max(self._context.canvas.zoom if self._context else 1.0, 1e-6)
        if geometry.kind is ShapeKind.LINE:
            return _distance_to_segment(
                (point.x, point.y),
                geometry.points[0],
                geometry.points[1],
            ) <= threshold
        return path.contains(QPointF(point.x, point.y))

    def _emit_geometry(self, geometry: ShapeGeometry | None) -> None:
        if self._geometry_sink is not None:
            self._geometry_sink(geometry)

    def _notify_history(self) -> None:
        if self._history_sink is not None:
            self._history_sink()

    def _status(self, message: str) -> None:
        if self._context is not None and self._context.status is not None:
            self._context.status(message)


def _shape_path(geometry: ShapeGeometry) -> QPainterPath:
    path = QPainterPath()
    if geometry.kind is ShapeKind.LINE:
        path.moveTo(QPointF(*geometry.points[0]))
        path.lineTo(QPointF(*geometry.points[1]))
        return path
    if geometry.kind is ShapeKind.POLYGON:
        polygon = QPolygonF([QPointF(*point) for point in geometry.points])
        path.addPolygon(polygon)
        path.closeSubpath()
        return path
    left, top, right, bottom = geometry.bounds
    rect = QRectF(left, top, right - left, bottom - top)
    if geometry.kind is ShapeKind.ELLIPSE:
        path.addEllipse(rect)
    elif geometry.kind is ShapeKind.ROUNDED_RECTANGLE:
        radius = geometry.effective_corner_radius
        path.addRoundedRect(rect, radius, radius)
    else:
        path.addRect(rect)
    return path


def _box_handles(
    bounds: tuple[float, float, float, float],
) -> dict[str, tuple[float, float]]:
    left, top, right, bottom = bounds
    middle_x = (left + right) / 2.0
    middle_y = (top + bottom) / 2.0
    return {
        "tl": (left, top),
        "tm": (middle_x, top),
        "tr": (right, top),
        "mr": (right, middle_y),
        "br": (right, bottom),
        "bm": (middle_x, bottom),
        "bl": (left, bottom),
        "ml": (left, middle_y),
    }


def _translated(geometry: ShapeGeometry, dx: float, dy: float) -> ShapeGeometry:
    return ShapeGeometry(
        geometry.kind,
        tuple((x + dx, y + dy) for x, y in geometry.points),
        geometry.corner_radius,
    )


def _distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _distance_to_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    denominator = dx * dx + dy * dy
    if denominator <= 1e-12:
        return _distance(point, start)
    projection = (
        (point[0] - start[0]) * dx + (point[1] - start[1]) * dy
    ) / denominator
    projection = min(1.0, max(0.0, projection))
    nearest = (start[0] + projection * dx, start[1] + projection * dy)
    return _distance(point, nearest)


__all__ = ["ShapeTool"]
