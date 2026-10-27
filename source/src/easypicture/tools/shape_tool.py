"""Editable raster shape tool with session-local history."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal, cast
from uuid import UUID

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QGraphicsPathItem, QGraphicsRectItem, QProgressDialog

from easypicture.domain import (
    Collection,
    DocumentEvent,
    EditableSvgPrimitive,
    Point,
    Size,
    SvgCircleGeometry,
    SvgEllipseGeometry,
    SvgLineGeometry,
    SvgObject,
    SvgPolygonGeometry,
    SvgPolylineGeometry,
    SvgPrimitiveKind,
    SvgPrimitiveStyle,
    SvgRectGeometry,
    TextObject,
    create_svg_primitive,
    parse_editable_svg,
    primitive_bounds,
    update_editable_svg,
)
from easypicture.imaging import (
    ShapeGeometry,
    ShapeKind,
    ShapeOutputTarget,
    ShapePaintStyle,
    ShapeRasterRequest,
    ShapeRasterResult,
    ShapeSettings,
    calculate_shape_raster,
    shape_affected_pixel_area,
    shape_affected_tile_count,
)
from easypicture.jobs import JobHandle, JobOutcome, JobStatus
from easypicture.jobs.preview_scheduler import PreviewScheduler, PreviewTarget
from easypicture.persistence import DocumentSnapshot
from easypicture.render.shape_path import shape_path as _shape_path
from easypicture.tools.protocol import ToolContext

type ShapeHandle = (
    Literal[
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
    ]
    | tuple[Literal["vertex"], int]
)
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
    output_target: ShapeOutputTarget = ShapeOutputTarget.RASTER
    vector_group_id: object | None = None
    vector_index: int | None = None
    vector_object: SvgObject | None = None
    existing_object_id: UUID | None = None
    selected_vertex_index: int | None = None


class ShapeTool:
    """Create and edit one temporary shape before one pixel command commits it."""

    def __init__(
        self,
        kind_provider: Callable[[], ShapeKind],
        settings_provider: Callable[[], ShapeSettings],
        output_target_provider: Callable[[], ShapeOutputTarget] | None = None,
        geometry_sink: Callable[[ShapeGeometry | None], None] | None = None,
        history_sink: Callable[[], None] | None = None,
        vector_object_sink: Callable[[SvgObject | None], None] | None = None,
        pixel_exact_provider: Callable[[], bool] | None = None,
    ) -> None:
        self._kind_provider = kind_provider
        self._settings_provider = settings_provider
        self._output_target_provider = output_target_provider or (lambda: ShapeOutputTarget.RASTER)
        self._geometry_sink = geometry_sink
        self._history_sink = history_sink
        self._vector_object_sink = vector_object_sink
        self._pixel_exact_provider = pixel_exact_provider or (lambda: False)
        self._context: ToolContext | None = None
        self._session: _Session | None = None
        self._draft_start: Point | None = None
        self._draft_geometry: ShapeGeometry | None = None
        self._polygon_points: list[tuple[float, float]] = []
        self._polygon_hover: Point | None = None
        self._overlay_items: list[object] = []
        self._handle: JobHandle | None = None
        self._scheduler: PreviewScheduler | None = None
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
        if self._scheduler is None and context.jobs is not None:
            self._scheduler = PreviewScheduler(context.jobs, context.canvas)
        context.canvas.set_tool_cursor(Qt.CursorShape.CrossCursor)
        selected = (
            context.selected_node_provider() if context.selected_node_provider is not None else None
        )
        if isinstance(selected, SvgObject):
            self._start_existing_svg_session(selected)

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

    def edit_svg_object(self, vector_object: SvgObject) -> None:
        """Start editing the selected primitive even when this tool is already active."""
        if self._session is not None and self._session.existing_object_id == vector_object.id:
            return
        self.cancel()
        self._start_existing_svg_session(vector_object)

    def set_svg_preview_object(self, vector_object: SvgObject) -> None:
        session = self._session
        if (
            session is None
            or session.existing_object_id != vector_object.id
            or session.output_target is not ShapeOutputTarget.VECTOR
        ):
            return
        primitive = parse_editable_svg(vector_object.svg)
        if primitive is None:
            return
        try:
            geometry = _svg_to_shape_geometry(primitive.geometry, vector_object)
        except ValueError:
            return
        session.vector_object = vector_object
        session.geometry = geometry
        self._rebuild_overlay(geometry)
        self._emit_geometry(geometry)

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
            if self._draft_start is not None:
                self._draft_start = None
                self._draft_geometry = None
                self._clear_overlay()
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
        if self._output_target_provider() is ShapeOutputTarget.RASTER and (
            document.active_layer_id is None or document.is_locked(document.active_layer_id)
        ):
            self._status("图层已锁定，无法绘制图形")
            return True
        point = self._event_point(event)
        if self._session is not None:
            handle = self._hit_handle(point)
            if handle is not None:
                if isinstance(handle, tuple):
                    self._session.selected_vertex_index = handle[1]
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
        if kind in (ShapeKind.POLYGON, ShapeKind.POLYLINE):
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
                if session.existing_object_id is not None:
                    self._commit_existing_and_resume()
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
                self.commit()
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
            self._close_point_shape()
            return True
        if self._session is not None:
            point = self._event_point(event)
            if self._insert_vertex_at_nearest_segment(point):
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
                self._close_point_shape()
            elif self._session is not None:
                self.commit()
            return True
        if event.key() == Qt.Key.Key_Escape:
            if self._session is not None or self._polygon_points or self._draft_start:
                self.cancel()
                return True
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._polygon_points:
                self._polygon_points.pop()
                self._rebuild_polygon_draft()
                return True
            if self._delete_selected_vertex():
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
        if session.output_target is ShapeOutputTarget.VECTOR:
            if session.vector_object is None:
                session.pending_commit = True
                return True
        elif self._handle is not None or session.latest_result is None:
            session.pending_commit = True
            return True
        if not self._is_current(session):
            self._status("文档已变化，临时图形已取消")
            self.cancel()
            return False
        commands = context.commands
        if session.output_target is ShapeOutputTarget.VECTOR:
            vector_object = session.vector_object
            group_id = session.vector_group_id
            index = session.vector_index
            existing_id = session.existing_object_id
            self._clear_session()
            if (
                commands is None
                or vector_object is None
                or (
                    not commands.update_vector_object(
                        existing_id,
                        svg=vector_object.svg,
                        intrinsic_bounds=vector_object.intrinsic_bounds,
                    )
                    if existing_id is not None
                    else (
                        not commands.create_svg_object(
                            group_id,
                            vector_object,
                            index=index,
                        )
                        if isinstance(group_id, UUID)
                        else not commands.create_svg_object_in_new_group(vector_object)
                    )
                )
            ):
                self._status("矢量图形未能写入目标集合")
                return False
            document = context.document_provider()
            if document is not None and context.select_node is not None:
                try:
                    created_group, created = document.vector_object_by_id(vector_object.id)
                except ValueError:
                    pass
                else:
                    # A newly created vector shape leaves the containing group
                    # selected so the next gesture creates another sibling
                    # instead of immediately entering edit mode for this object.
                    context.select_node(created_group if existing_id is None else created)
            if existing_id is None:
                self._status("已创建矢量图形，可继续绘制")
            return True
        result = session.latest_result
        if result is None:
            session.pending_commit = True
            return True
        updates = dict(result.updates)
        self._clear_session()
        if commands is None or not updates:
            self._status("图形未产生像素变化")
            return False
        applied = commands.apply_pixel_tiles(result.layer_id, updates, "绘制图形")
        if applied:
            self._status("已绘制图形，可继续绘制")
        return applied

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
        output_target = self._output_target_provider()
        vector_group: Collection | None = None
        vector_index: int | None = None
        if output_target is ShapeOutputTarget.VECTOR:
            selected = (
                context.selected_node_provider()
                if context.selected_node_provider is not None
                else None
            )
            if isinstance(selected, Collection):
                vector_group = selected
            elif isinstance(selected, (TextObject, SvgObject)):
                try:
                    vector_group, selected_object = document.vector_object_by_id(selected.id)
                except ValueError:
                    vector_group = None
                else:
                    vector_index = vector_group.children.index(selected_object)
            if vector_group is not None and vector_group.locked:
                self._status("请先选择一个未锁定的集合")
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
            output_target=output_target,
            vector_group_id=vector_group.id if vector_group is not None else None,
            vector_index=vector_index,
        )
        self._session = session
        self._rebuild_overlay(geometry)
        self._emit_geometry(geometry)
        self._request_preview()
        self._notify_history()

    def _start_existing_svg_session(self, vector_object: SvgObject) -> None:
        context = self._context
        if context is None or self._session is not None:
            return
        document = context.document_provider()
        if document is None:
            return
        primitive = parse_editable_svg(vector_object.svg)
        if primitive is None:
            return
        try:
            group, live_object = document.vector_object_by_id(vector_object.id)
            geometry = _svg_to_shape_geometry(primitive.geometry, vector_object)
        except ValueError:
            return
        if group.locked or live_object.locked:
            return
        snapshot = DocumentSnapshot.capture(document)

        def on_event(event: DocumentEvent) -> None:
            _ = event
            session = self._session
            if session is not None and not self._is_current(session):
                self.cancel()

        session = _Session(
            snapshot=snapshot,
            geometry=geometry,
            settings=_svg_to_shape_settings(primitive),
            unsubscribe=document.subscribe(on_event),
            history=[geometry],
            output_target=ShapeOutputTarget.VECTOR,
            vector_group_id=group.id,
            vector_index=group.children.index(live_object),
            vector_object=replace(vector_object),
            existing_object_id=vector_object.id,
        )
        self._session = session
        self._rebuild_overlay(geometry)
        self._emit_geometry(geometry)
        self._request_preview()
        self._notify_history()

    def _close_point_shape(self) -> None:
        points = tuple(self._polygon_points)
        self._polygon_points.clear()
        self._polygon_hover = None
        try:
            geometry = ShapeGeometry(self._kind_provider(), points)
        except ValueError as error:
            self._status(f"多边形无效：{error}")
            self._clear_overlay()
            return
        self._start_session(geometry)
        self.commit()

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

    def _commit_existing_and_resume(self) -> bool:
        """Commit one imported-SVG edit and immediately restore its handles."""
        session = self._session
        context = self._context
        if session is None or context is None or session.existing_object_id is None:
            return False
        object_id = session.existing_object_id
        selected_vertex_index = session.selected_vertex_index
        if not self.commit():
            return False
        document = context.document_provider()
        if document is None:
            return True
        try:
            _group, live_object = document.vector_object_by_id(object_id)
        except ValueError:
            return True
        if isinstance(live_object, SvgObject) and self._session is None:
            self._start_existing_svg_session(live_object)
            self._restore_selected_vertex(selected_vertex_index)
        return True

    def _restore_selected_vertex(self, index: int | None) -> None:
        if self._session is not None:
            self._session.selected_vertex_index = index

    def _insert_vertex_at_nearest_segment(self, point: Point) -> bool:
        session = self._session
        if session is None or session.geometry.kind not in {
            ShapeKind.POLYLINE,
            ShapeKind.POLYGON,
        }:
            return False
        points = session.geometry.points
        segments = list(zip(points, points[1:], strict=False))
        if session.geometry.kind is ShapeKind.POLYGON:
            segments.append((points[-1], points[0]))
        threshold = 8.0 / max(self._context.canvas.zoom if self._context else 1.0, 1e-6)
        distances = [
            _distance_to_segment((point.x, point.y), start, end) for start, end in segments
        ]
        if not distances:
            return False
        segment_index = min(range(len(distances)), key=distances.__getitem__)
        if distances[segment_index] > threshold:
            return False
        updated = list(points)
        insert_index = segment_index + 1
        updated.insert(insert_index, (point.x, point.y))
        try:
            geometry = ShapeGeometry(
                session.geometry.kind,
                tuple(updated),
                session.geometry.corner_radius,
            )
        except ValueError:
            return False
        session.drag = None
        session.selected_vertex_index = insert_index
        self._set_geometry(geometry, record=True)
        if session.existing_object_id is not None:
            self._commit_existing_and_resume()
        return True

    def _delete_selected_vertex(self) -> bool:
        session = self._session
        if session is None or session.geometry.kind not in {
            ShapeKind.POLYLINE,
            ShapeKind.POLYGON,
        }:
            return False
        index = session.selected_vertex_index
        if index is None or not 0 <= index < len(session.geometry.points):
            return False
        minimum = 2 if session.geometry.kind is ShapeKind.POLYLINE else 3
        if len(session.geometry.points) <= minimum:
            self._status(f"{('折线' if minimum == 2 else '多边形')}至少需要 {minimum} 个顶点")
            return True
        updated = list(session.geometry.points)
        updated.pop(index)
        try:
            geometry = ShapeGeometry(
                session.geometry.kind,
                tuple(updated),
                session.geometry.corner_radius,
            )
        except ValueError as error:
            self._status(str(error))
            return True
        session.selected_vertex_index = min(index, len(updated) - 1)
        self._set_geometry(geometry, record=True)
        if session.existing_object_id is not None:
            self._commit_existing_and_resume()
        return True

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
        if session.output_target is ShapeOutputTarget.VECTOR:
            vector_object = self._vector_object_for_session(session)
            if vector_object is None:
                self._status("无法生成矢量图形预览")
                return
            session.vector_object = vector_object
            if self._vector_object_sink is not None:
                self._vector_object_sink(vector_object)
            if isinstance(session.vector_group_id, UUID):
                context.canvas.replace_vector_preview(
                    session.vector_group_id,
                    self._vector_group_preview(
                        session.vector_group_id,
                        vector_object,
                        session.vector_index,
                    ),
                )
            else:
                context.canvas.replace_transient_vector_preview((vector_object,))
            self._rebuild_overlay(session.geometry)
            if session.pending_commit:
                session.pending_commit = False
                self.commit()
            return
        context.canvas.clear_pixel_preview()
        request = ShapeRasterRequest(
            session.snapshot,
            session.snapshot.require_raster_target(),
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
        if context.jobs is not None and (area > _BACKGROUND_AREA or tiles > _BACKGROUND_TILES):
            target = self._preview_target()
            if target is None or self._scheduler is None:
                return
            handle = self._scheduler.submit(
                lambda token, progress: calculate_shape_raster(
                    request,
                    token,
                    progress,
                ),
                target,
                self._preview_target,
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
        if session.output_target is ShapeOutputTarget.RASTER:
            self._context.canvas.clear_pixel_preview()
        elif isinstance(session.vector_group_id, UUID):
            document = self._context.document_provider()
            if document is None:
                self._context.canvas.clear_vector_preview()
                return
            try:
                group = document.vector_group_by_id(session.vector_group_id)
            except ValueError:
                self._context.canvas.clear_vector_preview()
                return
            self._context.canvas.replace_vector_preview(
                group.id,
                tuple(
                    child
                    for child in group.children
                    if child.id != session.existing_object_id
                    and isinstance(child, (TextObject, SvgObject))
                ),
            )
        else:
            self._context.canvas.clear_vector_preview()

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
            self._context.canvas.clear_vector_preview()
        if self._vector_object_sink is not None:
            self._vector_object_sink(None)
        self._emit_geometry(None)
        self._notify_history()
        self._clear_overlay()

    def _cancel_job(self) -> None:
        if self._scheduler is not None:
            self._scheduler.cancel()
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._close_progress()

    def _preview_target(self) -> PreviewTarget | None:
        context, session = self._context, self._session
        if context is None or session is None:
            return None
        document = context.document_provider()
        project = context.project_provider() if context.project_provider else document
        if document is None or project is None or document.active_layer_id is None:
            return None
        return PreviewTarget(
            project.id,
            document.id,
            document.active_layer_id,
            (document.revision, document.selection.revision),
            session.generation,
        )

    def _vector_object_for_session(self, session: _Session) -> SvgObject | None:
        document = self._context.document_provider() if self._context is not None else None
        if document is None:
            return None
        try:
            shape_geometry = session.geometry
            if session.existing_object_id is not None and session.vector_object is not None:
                shape_geometry = _shape_geometry_to_local(
                    shape_geometry,
                    session.vector_object,
                )
            kind, geometry = _shape_to_svg_geometry(shape_geometry)
            style = _shape_to_svg_style(session.geometry, session.settings)
            if session.vector_object is None:
                return create_svg_primitive(
                    kind,
                    geometry,
                    style,
                    Size(document.width, document.height),
                )
            svg = update_editable_svg(session.vector_object.svg, geometry, style)
            return replace(
                session.vector_object,
                element_kind=kind.value,
                svg=svg,
                intrinsic_bounds=primitive_bounds(geometry, style),
            )
        except TypeError, ValueError:
            return None

    def _vector_group_preview(
        self,
        group_id: UUID,
        vector_object: SvgObject,
        index: int | None,
    ) -> tuple[TextObject | SvgObject, ...]:
        if self._context is None:
            return (vector_object,)
        document = self._context.document_provider()
        if document is None:
            return (vector_object,)
        try:
            group = document.vector_group_by_id(group_id)
        except ValueError:
            return (vector_object,)
        objects = list(group.children)
        if session := self._session:
            existing_id = session.existing_object_id
        else:
            existing_id = None
        if existing_id is not None:
            objects[group.children.index(group.child_by_id(existing_id))] = vector_object
        else:
            objects.insert(len(objects) if index is None else index, vector_object)
        return tuple(item for item in objects if isinstance(item, (TextObject, SvgObject)))

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
        point = self._context.canvas.viewport_to_document(event.position().toPoint())
        target = (
            self._session.output_target
            if self._session is not None
            else self._output_target_provider()
        )
        if self._pixel_exact_provider() and target is ShapeOutputTarget.RASTER:
            return Point(float(round(point.x)), float(round(point.y)))
        return point

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
            kind
            in (
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
        self._add_path(
            path,
            miter_join=(
                geometry.kind is ShapeKind.RECTANGLE
                or (
                    geometry.kind is ShapeKind.ROUNDED_RECTANGLE
                    and geometry.effective_corner_radius <= 1e-6
                )
            ),
        )
        if geometry.kind is ShapeKind.LINE:
            self._add_handle(geometry.points[0])
            self._add_handle(geometry.points[1])
        elif geometry.kind in (ShapeKind.POLYGON, ShapeKind.POLYLINE):
            for point in geometry.points:
                self._add_handle(point)
        else:
            for point in _box_handles(geometry.bounds).values():
                self._add_handle(point)
            if geometry.kind is ShapeKind.ROUNDED_RECTANGLE:
                left, top, _, _ = geometry.bounds
                self._add_handle((left + geometry.effective_corner_radius, top))

    def _add_path(self, path: QPainterPath, *, miter_join: bool = False) -> None:
        if self._context is None:
            return
        item = QGraphicsPathItem(path)
        pen = QPen(_ORANGE)
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        if miter_join:
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
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
        if geometry.kind in (ShapeKind.LINE, ShapeKind.POLYLINE) or settings.style.value in {
            "stroke",
            "stroke_and_fill",
        }:
            pen = QPen(stroke)
            pen.setWidthF(settings.stroke_width)
            pen.setCosmetic(False)
            if geometry.kind is ShapeKind.RECTANGLE or (
                geometry.kind is ShapeKind.ROUNDED_RECTANGLE
                and geometry.effective_corner_radius <= 1e-6
            ):
                pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            item.setPen(pen)
        else:
            item.setPen(QPen(Qt.PenStyle.NoPen))
        if geometry.kind not in (ShapeKind.LINE, ShapeKind.POLYLINE) and settings.style.value in {
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
        elif geometry.kind in (ShapeKind.POLYGON, ShapeKind.POLYLINE):
            candidates = [(("vertex", index), value) for index, value in enumerate(geometry.points)]
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
        if geometry.kind in (ShapeKind.LINE, ShapeKind.POLYLINE):
            segments = zip(geometry.points, geometry.points[1:], strict=False)
            return (
                min(_distance_to_segment((point.x, point.y), start, end) for start, end in segments)
                <= threshold
            )
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


def _shape_to_svg_geometry(
    geometry: ShapeGeometry,
) -> tuple[
    SvgPrimitiveKind,
    SvgRectGeometry
    | SvgCircleGeometry
    | SvgEllipseGeometry
    | SvgLineGeometry
    | SvgPolylineGeometry
    | SvgPolygonGeometry,
]:
    left, top, right, bottom = geometry.bounds
    if geometry.kind in (ShapeKind.RECTANGLE, ShapeKind.ROUNDED_RECTANGLE):
        radius = geometry.effective_corner_radius
        return (
            SvgPrimitiveKind.RECT,
            SvgRectGeometry(
                left,
                top,
                right - left,
                bottom - top,
                radius,
                radius,
            ),
        )
    if geometry.kind is ShapeKind.ELLIPSE:
        rx = (right - left) / 2.0
        ry = (bottom - top) / 2.0
        cx = left + rx
        cy = top + ry
        if math.isclose(rx, ry, rel_tol=1e-9, abs_tol=1e-9):
            return SvgPrimitiveKind.CIRCLE, SvgCircleGeometry(cx, cy, rx)
        return SvgPrimitiveKind.ELLIPSE, SvgEllipseGeometry(cx, cy, rx, ry)
    if geometry.kind is ShapeKind.LINE:
        return SvgPrimitiveKind.LINE, SvgLineGeometry(
            *geometry.points[0],
            *geometry.points[1],
        )
    if geometry.kind is ShapeKind.POLYLINE:
        return SvgPrimitiveKind.POLYLINE, SvgPolylineGeometry(geometry.points)
    return SvgPrimitiveKind.POLYGON, SvgPolygonGeometry(geometry.points)


def _shape_to_svg_style(
    geometry: ShapeGeometry,
    settings: ShapeSettings,
) -> SvgPrimitiveStyle:
    stroke_enabled = geometry.kind in (ShapeKind.LINE, ShapeKind.POLYLINE) or (
        settings.style.value in {"stroke", "stroke_and_fill"}
    )
    fill_enabled = geometry.kind not in (ShapeKind.LINE, ShapeKind.POLYLINE) and (
        settings.style.value in {"fill", "stroke_and_fill"}
    )
    return SvgPrimitiveStyle(
        fill=settings.fill_color if fill_enabled else None,
        stroke=settings.stroke_color if stroke_enabled else None,
        stroke_width=settings.stroke_width,
        opacity=settings.opacity,
    )


def _svg_to_shape_geometry(
    geometry: (
        SvgRectGeometry
        | SvgCircleGeometry
        | SvgEllipseGeometry
        | SvgLineGeometry
        | SvgPolylineGeometry
        | SvgPolygonGeometry
    ),
    vector_object: SvgObject,
) -> ShapeGeometry:
    matrix = vector_object.transform

    def mapped(x: float, y: float) -> tuple[float, float]:
        point = matrix.map_point(Point(x, y))
        return point.x, point.y

    if isinstance(geometry, SvgLineGeometry):
        return ShapeGeometry(
            ShapeKind.LINE,
            (mapped(geometry.x1, geometry.y1), mapped(geometry.x2, geometry.y2)),
        )
    if isinstance(geometry, SvgPolylineGeometry):
        return ShapeGeometry(
            ShapeKind.POLYLINE,
            tuple(mapped(*point) for point in geometry.points),
        )
    if isinstance(geometry, SvgPolygonGeometry):
        return ShapeGeometry(
            ShapeKind.POLYGON,
            tuple(mapped(*point) for point in geometry.points),
        )
    if abs(matrix.b) > 1e-9 or abs(matrix.c) > 1e-9:
        raise ValueError("rotated SVG box primitives use the transform tool")
    if isinstance(geometry, SvgRectGeometry):
        return ShapeGeometry(
            ShapeKind.ROUNDED_RECTANGLE
            if geometry.rx > 0.0 or geometry.ry > 0.0
            else ShapeKind.RECTANGLE,
            (
                mapped(geometry.x, geometry.y),
                mapped(geometry.x + geometry.width, geometry.y + geometry.height),
            ),
            max(geometry.rx * abs(matrix.a), geometry.ry * abs(matrix.d)),
        )
    if isinstance(geometry, SvgCircleGeometry):
        return ShapeGeometry(
            ShapeKind.ELLIPSE,
            (
                mapped(geometry.cx - geometry.r, geometry.cy - geometry.r),
                mapped(geometry.cx + geometry.r, geometry.cy + geometry.r),
            ),
        )
    return ShapeGeometry(
        ShapeKind.ELLIPSE,
        (
            mapped(geometry.cx - geometry.rx, geometry.cy - geometry.ry),
            mapped(geometry.cx + geometry.rx, geometry.cy + geometry.ry),
        ),
    )


def _shape_geometry_to_local(
    geometry: ShapeGeometry,
    vector_object: SvgObject,
) -> ShapeGeometry:
    inverse = vector_object.transform.inverse()
    points = tuple(
        (mapped.x, mapped.y)
        for x, y in geometry.points
        for mapped in (inverse.map_point(Point(x, y)),)
    )
    scale = max(
        math.hypot(inverse.a, inverse.b),
        math.hypot(inverse.c, inverse.d),
    )
    return ShapeGeometry(
        geometry.kind,
        points,
        geometry.corner_radius * scale,
    )


def _svg_to_shape_settings(primitive: EditableSvgPrimitive) -> ShapeSettings:
    style = primitive.style
    if style.fill is not None and style.stroke is not None:
        paint_style = ShapePaintStyle.STROKE_AND_FILL
    elif style.fill is not None:
        paint_style = ShapePaintStyle.FILL
    else:
        paint_style = ShapePaintStyle.STROKE
    return ShapeSettings(
        style=paint_style,
        stroke_color=style.stroke or (0, 0, 0, 0),
        fill_color=style.fill or (0, 0, 0, 0),
        stroke_width=max(1.0, style.stroke_width),
        opacity=style.opacity,
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
    projection = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator
    projection = min(1.0, max(0.0, projection))
    nearest = (start[0] + projection * dx, start[1] + projection * dy)
    return _distance(point, nearest)


__all__ = ["ShapeTool"]
