"""Interactive rectangle and quadrilateral transform tool."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, cast

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QContextMenuEvent,
    QKeyEvent,
    QMouseEvent,
    QPainterPath,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QProgressDialog,
)

from easypicture.domain import (
    ActiveLayerChanged,
    DocumentEvent,
    DocumentRevisionChanged,
    GroupListChanged,
    GroupMembersChanged,
    Point,
    ProjectiveTransform,
)
from easypicture.imaging import (
    RasterTransformRequest,
    RasterTransformResult,
    TransformInterpolation,
    calculate_raster_transform,
    projective_from_quad,
)
from easypicture.jobs import JobHandle, JobOutcome, JobStatus
from easypicture.persistence import DocumentSnapshot
from easypicture.tools.protocol import ToolContext

type ParameterValues = tuple[float, float, float, float, float]
type CornerValues = tuple[float, float, float, float, float, float, float, float]
type PivotValues = tuple[float, float]
type HandleName = Literal["tl", "tm", "tr", "mr", "br", "bm", "bl", "ml"]
type CornerName = Literal["tl", "tr", "br", "bl"]
type RotationName = Literal["rotate_tl", "rotate_tr", "rotate_br", "rotate_bl"]
type DragKind = Literal["move", "resize", "rotate", "pivot"]


@dataclass(frozen=True, slots=True)
class TransformParameters:
    x: float
    y: float
    width: float
    height: float
    rotation: float = 0.0

    @property
    def values(self) -> ParameterValues:
        return self.x, self.y, self.width, self.height, self.rotation

    @classmethod
    def from_values(cls, values: ParameterValues) -> TransformParameters:
        return cls(*values)


@dataclass(frozen=True, slots=True)
class _SourceBounds:
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def center(self) -> Point:
        return Point(
            (self.left + self.right) / 2.0,
            (self.top + self.bottom) / 2.0,
        )

    @property
    def corners(self) -> tuple[Point, Point, Point, Point]:
        return (
            Point(self.left, self.top),
            Point(self.right, self.top),
            Point(self.right, self.bottom),
            Point(self.left, self.bottom),
        )


@dataclass(slots=True)
class _Drag:
    kind: DragKind
    press: Point
    initial_corners: tuple[Point, Point, Point, Point]
    initial_pivot: Point
    handle: HandleName | RotationName | None = None


@dataclass(frozen=True, slots=True)
class _TransformState:
    corners: tuple[Point, Point, Point, Point]
    pivot: Point


@dataclass(slots=True)
class _Session:
    snapshot: DocumentSnapshot
    bounds: _SourceBounds
    corners: tuple[Point, Point, Point, Point]
    pivot: Point
    matrix: ProjectiveTransform
    unsubscribe: Callable[[], None]
    drag: _Drag | None = None
    history: list[_TransformState] = field(default_factory=list)
    history_labels: list[str] = field(default_factory=list)
    history_index: int = 0
    pending_state: _TransformState | None = None
    pending_label: str | None = None


class TransformTool:
    def __init__(
        self,
        interpolation_provider: Callable[[], TransformInterpolation],
        irregular_provider: Callable[[], bool],
        parameter_sink: Callable[[ParameterValues], None],
        corner_sink: Callable[[CornerValues | None], None],
        pivot_sink: Callable[[PivotValues | None], None],
        history_sink: Callable[[], None] | None = None,
        pixel_exact_provider: Callable[[], bool] | None = None,
    ) -> None:
        self._interpolation_provider = interpolation_provider
        self._irregular_provider = irregular_provider
        self._parameter_sink = parameter_sink
        self._corner_sink = corner_sink
        self._pivot_sink = pivot_sink
        self._history_sink = history_sink
        self._pixel_exact_provider = pixel_exact_provider or (lambda: False)
        self._context: ToolContext | None = None
        self._session: _Session | None = None
        self._handle: JobHandle | None = None
        self._progress: QProgressDialog | None = None
        self._committing = False
        self._last_irregular = irregular_provider()
        self._suppress_right_release = False
        self._suppress_context_menu = False

    @property
    def has_active_session(self) -> bool:
        return self._session is not None

    @property
    def can_undo_preview(self) -> bool:
        session = self._session
        return session is not None and (self._pending_changed(session) or session.history_index > 0)

    @property
    def can_redo_preview(self) -> bool:
        session = self._session
        return (
            session is not None
            and session.pending_state is None
            and session.history_index < len(session.history) - 1
        )

    @property
    def preview_undo_text(self) -> str:
        session = self._session
        if session is None:
            return ""
        if self._pending_changed(session):
            return session.pending_label or "自由变换调整"
        if session.history_index <= 0:
            return ""
        return session.history_labels[session.history_index - 1]

    @property
    def preview_redo_text(self) -> str:
        session = self._session
        if (
            session is None
            or session.pending_state is not None
            or session.history_index >= len(session.history) - 1
        ):
            return ""
        return session.history_labels[session.history_index]

    def activate(self, context: ToolContext) -> None:
        self._context = context
        context.canvas.set_tool_cursor(Qt.CursorShape.ArrowCursor)
        self.restart_session()

    def deactivate(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._close_progress()
        if self._context is not None and self._context.set_busy is not None:
            self._context.set_busy(False)
        self._clear_session()
        if self._context is not None:
            self._context.canvas.set_tool_cursor(None)
        self._context = None

    def restart_session(self) -> None:
        self._clear_session()
        self._begin_session()

    def refresh_options(self) -> None:
        session = self._session
        context = self._context
        if session is None or context is None:
            self._last_irregular = self._irregular_provider()
            return
        irregular = self._irregular_provider()
        if self._last_irregular and not irregular:
            regular = _regularized_corners(session.corners)
            before = self._state(session)
            if self._set_geometry(regular, session.pivot):
                self._record_completed_change(before, "规则化变换框")
                self._status("已恢复规则矩形变换框")
        self._last_irregular = irregular
        context.canvas.set_transform_preview(
            session.snapshot.require_raster_target(),
            session.matrix,
            self._interpolation_provider(),
        )

    def begin_preview_edit(self, label: str) -> None:
        session = self._session
        if session is None:
            return
        if session.pending_state is None:
            session.pending_state = self._state(session)
            session.pending_label = label
        self._notify_history_changed()

    def finish_preview_edit(self) -> None:
        session = self._session
        if session is None or session.pending_state is None:
            return
        before = session.pending_state
        label = session.pending_label or "自由变换调整"
        session.pending_state = None
        session.pending_label = None
        self._push_history(session, before, self._state(session), label)

    def cancel_preview_edit(self) -> None:
        session = self._session
        if session is None or session.pending_state is None:
            return
        state = session.pending_state
        session.pending_state = None
        session.pending_label = None
        self._restore_state(state)
        self._notify_history_changed()

    def undo_preview(self) -> bool:
        session = self._session
        if session is None:
            return False
        if session.pending_state is not None:
            self.cancel_preview_edit()
            return True
        if session.history_index <= 0:
            return False
        session.history_index -= 1
        self._restore_state(session.history[session.history_index])
        self._notify_history_changed()
        return True

    def redo_preview(self) -> bool:
        session = self._session
        if (
            session is None
            or session.pending_state is not None
            or session.history_index >= len(session.history) - 1
        ):
            return False
        session.history_index += 1
        self._restore_state(session.history[session.history_index])
        self._notify_history_changed()
        return True

    def apply_numeric_parameters(self, values: object) -> None:
        if not (
            isinstance(values, tuple)
            and len(values) == 5
            and all(isinstance(value, (int, float)) for value in values)
        ):
            return
        session = self._session
        if session is None:
            return
        requested = TransformParameters.from_values(
            tuple(float(value) for value in values)  # type: ignore[arg-type]
        )
        if self._pixel_exact_provider():
            requested = TransformParameters.from_values(
                (
                    float(round(requested.x)),
                    float(round(requested.y)),
                    float(max(1, round(requested.width))),
                    float(max(1, round(requested.height))),
                    requested.rotation,
                )
            )
        if not _valid_parameters(requested):
            self._parameter_sink(self._parameters(session).values)
            return
        current = self._parameters(session)
        if current.width <= 1e-9 or current.height <= 1e-9:
            return
        scale_x = requested.width / current.width
        scale_y = requested.height / current.height
        corners = tuple(
            _map_about_frame(
                point,
                session.pivot,
                current.rotation,
                requested.rotation,
                scale_x,
                scale_y,
                Point(requested.x, requested.y),
            )
            for point in session.corners
        )
        before = self._state(session)
        if self._set_geometry(
            cast(tuple[Point, Point, Point, Point], corners),
            Point(requested.x, requested.y),
            sync_parameters=False,
        ):
            self._record_completed_change(before, "变换参数")
            return
        self._parameter_sink(current.values)

    def apply_corner_values(self, values: object) -> None:
        if not (
            isinstance(values, tuple)
            and len(values) == 8
            and all(isinstance(value, (int, float)) for value in values)
        ):
            return
        session = self._session
        if session is None:
            return
        requested = cast(
            tuple[Point, Point, Point, Point],
            tuple(
                Point(float(values[index]), float(values[index + 1])) for index in range(0, 8, 2)
            ),
        )
        if self._pixel_exact_provider():
            requested = cast(
                tuple[Point, Point, Point, Point],
                tuple(Point(float(round(point.x)), float(round(point.y))) for point in requested),
            )
        if self._irregular_provider():
            corners = requested
        else:
            changed = max(
                range(4),
                key=lambda index: _distance(session.corners[index], requested[index]),
            )
            if _distance(session.corners[changed], requested[changed]) <= 1e-9:
                return
            corners = _resize_regular_corner(
                session.corners,
                changed,
                requested[changed],
            )
        before = self._state(session)
        if not self._set_geometry(corners, session.pivot, sync_corners=True):
            self._corner_sink(_corner_values(session.corners))
            return
        self._record_completed_change(before, "角点坐标")

    def apply_pivot_values(self, values: object) -> None:
        if not (
            isinstance(values, tuple)
            and len(values) == 2
            and all(isinstance(value, (int, float)) for value in values)
        ):
            return
        session = self._session
        if session is None:
            return
        pivot = Point(float(values[0]), float(values[1]))
        before = self._state(session)
        if not self._set_geometry(session.corners, pivot, sync_corners=False):
            self._pivot_sink((session.pivot.x, session.pivot.y))
            return
        self._record_completed_change(before, "中心点坐标")

    def commit(self) -> None:
        session = self._session
        context = self._context
        if session is None or context is None or self._handle is not None or context.jobs is None:
            return
        if session.pending_state is not None:
            self.finish_preview_edit()
        if session.matrix.is_identity:
            self._status("变换未产生变化")
            self._clear_session()
            return
        request = RasterTransformRequest(
            session.snapshot,
            session.snapshot.require_raster_target(),
            session.matrix,
            self._interpolation_provider(),
        )
        handle = context.jobs.submit(
            lambda token, progress: calculate_raster_transform(request, token, progress)
        )
        self._handle = handle
        self._committing = True
        if context.set_busy is not None:
            context.set_busy(True)
        dialog = QProgressDialog("正在应用自由变换…", "取消", 0, 0, context.canvas)
        dialog.setWindowTitle("自由变换")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
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
        handle.finished.connect(self._on_finished)

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            return
        self._clear_session()
        self._status("已取消自由变换")

    def mouse_press(self, event: QMouseEvent) -> bool:
        if self._handle is not None:
            return False
        if event.button() == Qt.MouseButton.RightButton:
            session = self._session
            if session is None or session.drag is None:
                self._suppress_right_release = False
                self._suppress_context_menu = False
                return False
            self._restore_state(
                _TransformState(
                    session.drag.initial_corners,
                    session.drag.initial_pivot,
                )
            )
            session.drag = None
            self._suppress_right_release = True
            self._suppress_context_menu = True
            self._status("已撤销本次拖动")
            return True
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        self._suppress_context_menu = False
        if self._session is None:
            self._begin_session()
            return self._session is not None
        session = self._session
        if session is None or self._context is None:
            return False
        point = self._document_point(event)
        hit = self._hit_test(point)
        handle: HandleName | RotationName | None = None
        if hit == "pivot":
            kind: DragKind = "pivot"
        elif hit in _HANDLE_NAMES:
            kind = "resize"
            handle = cast(HandleName, hit)
        elif hit in _ROTATION_NAMES:
            kind = "rotate"
            handle = cast(RotationName, hit)
        elif hit == "inside":
            kind = "move"
        else:
            return False
        session.drag = _Drag(
            kind,
            point,
            session.corners,
            session.pivot,
            handle,
        )
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        session = self._session
        if session is None:
            return False
        if session.drag is None:
            self._update_hover_cursor(self._hit_test(self._document_point(event)))
            return False
        self._update_drag(self._document_point(event), event.modifiers())
        return True

    def mouse_release(self, event: QMouseEvent) -> bool:
        session = self._session
        if event.button() == Qt.MouseButton.RightButton and self._suppress_right_release:
            self._suppress_right_release = False
            return True
        if event.button() != Qt.MouseButton.LeftButton or session is None or session.drag is None:
            return False
        drag_kind = session.drag.kind
        before = _TransformState(
            session.drag.initial_corners,
            session.drag.initial_pivot,
        )
        label = {
            "move": "移动",
            "resize": "缩放",
            "rotate": "旋转",
            "pivot": "移动中心点",
        }[session.drag.kind]
        self._update_drag(self._document_point(event), event.modifiers())
        session.drag = None
        after = self._state(session)
        self._push_history(session, before, after, label)
        if drag_kind != "pivot" and not self._states_equal(before, after):
            self.commit()
        return True

    def context_menu(self, event: QContextMenuEvent) -> bool:
        _ = event
        if not self._suppress_context_menu:
            return False
        self._suppress_context_menu = False
        return True

    def mouse_double_click(self, event: QMouseEvent) -> bool:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._session is not None
            and self._hit_test(self._document_point(event)) == "inside"
        ):
            self.commit()
            return True
        return False

    def key_press(self, event: QKeyEvent) -> bool:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit()
            return True
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
            return True
        if event.key() in (
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            session = self._session
            if session is None:
                return False
            step = 10.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
            dx = (
                -step
                if event.key() == Qt.Key.Key_Left
                else step
                if event.key() == Qt.Key.Key_Right
                else 0.0
            )
            dy = (
                -step
                if event.key() == Qt.Key.Key_Up
                else step
                if event.key() == Qt.Key.Key_Down
                else 0.0
            )
            before = self._state(session)
            if self._set_geometry(
                cast(
                    tuple[Point, Point, Point, Point],
                    tuple(Point(point.x + dx, point.y + dy) for point in session.corners),
                ),
                Point(session.pivot.x + dx, session.pivot.y + dy),
            ):
                self._record_completed_change(before, "键盘移动")
            return True
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        _ = event
        return False

    def _begin_session(self) -> None:
        context = self._context
        if context is None:
            return
        document = context.document_provider()
        if document is None:
            return
        if document.active_layer_id is None:
            self._status("请先选择可编辑的栅格对象")
            return
        layer = document.active_layer
        if layer.locked:
            self._status("图层已锁定，无法变换")
            return
        if layer.alpha_locked:
            self._status("请先关闭透明像素锁定再进行变换")
            return
        snapshot = DocumentSnapshot.capture(document)
        bounds = self._source_bounds(snapshot)
        corners = bounds.corners
        session = _Session(
            snapshot,
            bounds,
            corners,
            bounds.center,
            ProjectiveTransform(),
            document.subscribe(self._on_document_event),
        )
        session.history.append(self._state(session))
        self._session = session
        self._last_irregular = self._irregular_provider()
        context.canvas.set_selection_overlay_visible(False)
        self._update_preview()
        self._notify_history_changed()

    def _clear_session(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            session.unsubscribe()
        self._corner_sink(None)
        self._pivot_sink(None)
        self._suppress_right_release = False
        self._suppress_context_menu = False
        if self._context is not None:
            self._context.canvas.clear_transform_preview()
            self._context.canvas.set_selection_overlay_visible(True)
            self._context.canvas.set_interaction_scene_bounds(None)
            self._context.overlays.clear()
        self._notify_history_changed()

    def _on_document_event(self, event: DocumentEvent) -> None:
        session = self._session
        if session is None or self._committing:
            return
        document = self._context.document_provider() if self._context is not None else None
        layer_exists = (
            document is not None
            and document.id == session.snapshot.document_id
            and any(
                layer.id == session.snapshot.require_raster_target() for layer in document.layers
            )
        )
        layer_changed = isinstance(
            event,
            (GroupListChanged, GroupMembersChanged, ActiveLayerChanged),
        ) and (
            not layer_exists
            or document is None
            or document.active_layer_id != session.snapshot.require_raster_target()
        )
        revision_changed = (
            isinstance(event, DocumentRevisionChanged)
            and event.revision != session.snapshot.revision
        )
        if layer_changed or revision_changed:
            self._clear_session()
            self._status("图层或文档已变化，已安全退出自由变换")

    def _set_geometry(
        self,
        corners: tuple[Point, Point, Point, Point],
        pivot: Point,
        *,
        sync_parameters: bool = True,
        sync_corners: bool = True,
    ) -> bool:
        session = self._session
        if session is None or not _valid_quad(corners) or not _valid_point(pivot):
            return False
        try:
            matrix = projective_from_quad(session.bounds.corners, corners)
        except ValueError:
            return False
        session.corners = corners
        session.pivot = pivot
        session.matrix = matrix
        self._update_preview(
            sync_parameters=sync_parameters,
            sync_corners=sync_corners,
        )
        return True

    @staticmethod
    def _state(session: _Session) -> _TransformState:
        return _TransformState(session.corners, session.pivot)

    @staticmethod
    def _states_equal(left: _TransformState, right: _TransformState) -> bool:
        values = (
            *(value for point in left.corners for value in (point.x, point.y)),
            left.pivot.x,
            left.pivot.y,
        )
        other = (
            *(value for point in right.corners for value in (point.x, point.y)),
            right.pivot.x,
            right.pivot.y,
        )
        return all(
            math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8)
            for a, b in zip(values, other, strict=True)
        )

    def _pending_changed(self, session: _Session) -> bool:
        return session.pending_state is not None and not self._states_equal(
            session.pending_state, self._state(session)
        )

    def _record_completed_change(
        self,
        before: _TransformState,
        label: str,
    ) -> None:
        session = self._session
        if session is None:
            return
        if session.pending_state is not None:
            self._notify_history_changed()
            return
        self._push_history(session, before, self._state(session), label)

    def _push_history(
        self,
        session: _Session,
        before: _TransformState,
        after: _TransformState,
        label: str,
    ) -> None:
        if self._states_equal(before, after):
            self._notify_history_changed()
            return
        del session.history[session.history_index + 1 :]
        del session.history_labels[session.history_index :]
        session.history.append(after)
        session.history_labels.append(label)
        session.history_index += 1
        self._notify_history_changed()

    def _restore_state(self, state: _TransformState) -> None:
        self._set_geometry(state.corners, state.pivot)

    def _notify_history_changed(self) -> None:
        if self._history_sink is not None:
            self._history_sink()

    def _update_preview(
        self,
        *,
        sync_parameters: bool = True,
        sync_corners: bool = True,
    ) -> None:
        session = self._session
        context = self._context
        if session is None or context is None:
            return
        context.canvas.set_transform_preview(
            session.snapshot.require_raster_target(),
            session.matrix,
            self._interpolation_provider(),
        )
        if sync_parameters:
            self._parameter_sink(self._parameters(session).values)
        if sync_corners:
            self._corner_sink(_corner_values(session.corners))
        self._pivot_sink((session.pivot.x, session.pivot.y))
        left = min(point.x for point in session.corners)
        top = min(point.y for point in session.corners)
        right = max(point.x for point in session.corners)
        bottom = max(point.y for point in session.corners)
        context.canvas.set_interaction_scene_bounds(QRectF(left, top, right - left, bottom - top))
        self._rebuild_overlay()

    def _update_drag(
        self,
        point: Point,
        modifiers: Qt.KeyboardModifier,
    ) -> None:
        session = self._session
        if session is None or session.drag is None:
            return
        drag = session.drag
        corners = drag.initial_corners
        pivot = drag.initial_pivot
        if drag.kind == "move":
            dx = point.x - drag.press.x
            dy = point.y - drag.press.y
            if self._pixel_exact_provider():
                dx = float(round(dx))
                dy = float(round(dy))
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                if abs(dx) >= abs(dy):
                    dy = 0.0
                else:
                    dx = 0.0
            corners = cast(
                tuple[Point, Point, Point, Point],
                tuple(Point(value.x + dx, value.y + dy) for value in corners),
            )
            pivot = Point(pivot.x + dx, pivot.y + dy)
        elif drag.kind == "rotate":
            start_angle = math.atan2(
                drag.press.y - pivot.y,
                drag.press.x - pivot.x,
            )
            current_angle = math.atan2(point.y - pivot.y, point.x - pivot.x)
            degrees = math.degrees(current_angle - start_angle)
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                degrees = round(degrees / 15.0) * 15.0
            corners = cast(
                tuple[Point, Point, Point, Point],
                tuple(_rotate(value, pivot, degrees) for value in corners),
            )
        elif drag.kind == "pivot":
            pivot = (
                Point(float(round(point.x)), float(round(point.y)))
                if self._pixel_exact_provider()
                else point
            )
        elif drag.kind == "resize" and drag.handle in _HANDLE_NAMES:
            if self._pixel_exact_provider():
                point = Point(float(round(point.x)), float(round(point.y)))
            handle = cast(HandleName, drag.handle)
            if handle in _CORNER_NAMES:
                index = _CORNER_NAMES.index(handle)
                if self._irregular_provider():
                    values = list(corners)
                    values[index] = point
                    corners = cast(tuple[Point, Point, Point, Point], tuple(values))
                else:
                    corners = _resize_regular_corner(corners, index, point)
            else:
                corners = _resize_edge(corners, handle, drag.press, point)
        self._set_geometry(corners, pivot)

    def _parameters(self, session: _Session) -> TransformParameters:
        top_width = _distance(session.corners[0], session.corners[1])
        bottom_width = _distance(session.corners[3], session.corners[2])
        left_height = _distance(session.corners[0], session.corners[3])
        right_height = _distance(session.corners[1], session.corners[2])
        rotation = math.degrees(
            math.atan2(
                session.corners[1].y - session.corners[0].y,
                session.corners[1].x - session.corners[0].x,
            )
        )
        return TransformParameters(
            session.pivot.x,
            session.pivot.y,
            (top_width + bottom_width) / 2.0,
            (left_height + right_height) / 2.0,
            rotation,
        )

    def _rebuild_overlay(self) -> None:
        context = self._context
        session = self._session
        if context is None or session is None:
            return
        context.overlays.clear()
        points = _handle_points(session.corners)
        polygon = QGraphicsPolygonItem(
            QPolygonF([QPointF(point.x, point.y) for point in session.corners])
        )
        pen = context.canvas.highlight_pen()
        polygon.setPen(pen)
        polygon.setBrush(Qt.BrushStyle.NoBrush)
        context.overlays.add(polygon)
        for point in points.values():
            handle = QGraphicsRectItem(-4.0, -4.0, 8.0, 8.0)
            handle.setFlag(
                QGraphicsRectItem.GraphicsItemFlag.ItemIgnoresTransformations,
                True,
            )
            handle.setPen(pen)
            handle.setBrush(QBrush(QColor("#242424")))
            handle.setPos(point.x, point.y)
            context.overlays.add(handle)
        for name, point in self._rotation_points().items():
            arrow = QGraphicsPathItem(_rotation_arrow_path())
            arrow.setFlag(
                QGraphicsPathItem.GraphicsItemFlag.ItemIgnoresTransformations,
                True,
            )
            arrow.setPen(pen)
            arrow.setBrush(Qt.BrushStyle.NoBrush)
            arrow.setPos(point.x, point.y)
            corner_index = _ROTATION_NAMES.index(name)
            corner = session.corners[corner_index]
            arrow.setRotation(
                math.degrees(
                    math.atan2(
                        corner.y - session.pivot.y,
                        corner.x - session.pivot.x,
                    )
                )
                + 135.0
            )
            context.overlays.add(arrow)
        radius = context.canvas.center_marker_radius
        pivot_item = QGraphicsEllipseItem(-radius, -radius, radius * 2, radius * 2)
        pivot_item.setFlag(
            QGraphicsEllipseItem.GraphicsItemFlag.ItemIgnoresTransformations,
            True,
        )
        pivot_item.setPen(context.canvas.center_marker_pen())
        pivot_item.setBrush(Qt.BrushStyle.NoBrush)
        pivot_item.setPos(session.pivot.x, session.pivot.y)
        context.overlays.add(pivot_item)

    def _rotation_points(self) -> dict[RotationName, Point]:
        session = self._session
        context = self._context
        if session is None or context is None:
            return {}
        distance = 27.0 / max(context.canvas.zoom, 1e-9)
        result: dict[RotationName, Point] = {}
        for name, corner in zip(_ROTATION_NAMES, session.corners, strict=True):
            dx = corner.x - session.pivot.x
            dy = corner.y - session.pivot.y
            length = max(math.hypot(dx, dy), 1e-9)
            result[name] = Point(
                corner.x + dx / length * distance,
                corner.y + dy / length * distance,
            )
        return result

    def _hit_test(self, point: Point) -> str | HandleName | RotationName | None:
        session = self._session
        context = self._context
        if session is None or context is None:
            return None
        point_view = context.canvas.mapFromScene(QPointF(point.x, point.y))
        pivot_view = context.canvas.mapFromScene(QPointF(session.pivot.x, session.pivot.y))
        if _view_distance(point_view, pivot_view) <= 10.0:
            return "pivot"
        for name, rotation_point in self._rotation_points().items():
            handle_view = context.canvas.mapFromScene(QPointF(rotation_point.x, rotation_point.y))
            if _view_distance(point_view, handle_view) <= 13.0:
                return name
        for handle_name, handle_point in _handle_points(session.corners).items():
            handle_view = context.canvas.mapFromScene(QPointF(handle_point.x, handle_point.y))
            if _view_distance(point_view, handle_view) <= 10.0:
                return handle_name
        polygon = QPolygonF([QPointF(value.x, value.y) for value in session.corners])
        if polygon.containsPoint(QPointF(point.x, point.y), Qt.FillRule.OddEvenFill):
            return "inside"
        return None

    def _update_hover_cursor(
        self,
        hit: str | HandleName | RotationName | None,
    ) -> None:
        if self._context is None:
            return
        if hit in {"tl", "br"}:
            cursor = Qt.CursorShape.SizeFDiagCursor
        elif hit in {"tr", "bl"}:
            cursor = Qt.CursorShape.SizeBDiagCursor
        elif hit in {"tm", "bm"}:
            cursor = Qt.CursorShape.SizeVerCursor
        elif hit in {"ml", "mr"}:
            cursor = Qt.CursorShape.SizeHorCursor
        elif hit == "inside":
            cursor = Qt.CursorShape.SizeAllCursor
        elif hit == "pivot" or hit in _ROTATION_NAMES:
            cursor = Qt.CursorShape.CrossCursor
        else:
            cursor = Qt.CursorShape.ArrowCursor
        self._context.canvas.set_tool_cursor(cursor)

    @staticmethod
    def _source_bounds(snapshot: DocumentSnapshot) -> _SourceBounds:
        layer = next(
            layer for layer in snapshot.raster_layers if layer.id == snapshot.active_layer_id
        )
        from easypicture.domain.coordinates import RasterMapping

        bounds = RasterMapping(layer.world_transform, layer.size).bounds
        if not snapshot.selection.has_nonzero_data:
            return _SourceBounds(
                bounds.x,
                bounds.y,
                bounds.right,
                bounds.bottom,
            )
        left = snapshot.size.width
        top = snapshot.size.height
        right = 0
        bottom = 0
        for coord, tile in snapshot.selection.iter_allocated_tiles():
            ys, xs = np.nonzero(tile)
            if not xs.size:
                continue
            left = min(left, coord.x * 256 + int(xs.min()))
            top = min(top, coord.y * 256 + int(ys.min()))
            right = max(right, coord.x * 256 + int(xs.max()) + 1)
            bottom = max(bottom, coord.y * 256 + int(ys.max()) + 1)
        if right <= left or bottom <= top:
            return _SourceBounds(
                bounds.x,
                bounds.y,
                bounds.right,
                bounds.bottom,
            )
        return _SourceBounds(float(left), float(top), float(right), float(bottom))

    @staticmethod
    def _update_progress(
        dialog: QProgressDialog,
        value: int,
        maximum: int,
        message: str,
    ) -> None:
        dialog.setLabelText(message)
        dialog.setMaximum(maximum)
        dialog.setValue(value)

    def _on_finished(self, outcome: JobOutcome) -> None:
        context = self._context
        self._handle = None
        self._committing = False
        self._close_progress()
        if context is None:
            return
        if context.set_busy is not None:
            context.set_busy(False)
        if outcome.status is JobStatus.CANCELLED:
            self._status("已取消变换提交，可继续调整")
            return
        if outcome.status is JobStatus.FAILED:
            self._status(f"自由变换失败：{outcome.error}")
            return
        if not isinstance(outcome.value, RasterTransformResult):
            self._status("自由变换失败：无效的任务结果")
            return
        result = outcome.value
        document = context.document_provider()
        if (
            document is None
            or document.id != result.document_id
            or document.revision != result.source_revision
            or not any(layer.id == result.layer_id for layer in document.layers)
        ):
            self._clear_session()
            self._status("文档已变化，已丢弃过期变换结果")
            return
        self._clear_session()
        if context.commands is None or not context.commands.apply_raster_transform(result):
            self._status("自由变换未提交：结果无变化、超限或撤销预算不足")
            return
        self._status("已应用自由变换")
        self._begin_session()

    def _close_progress(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress.deleteLater()
            self._progress = None

    def _document_point(self, event: QMouseEvent) -> Point:
        assert self._context is not None
        return self._context.canvas.viewport_to_document(event.position().toPoint())

    def _status(self, message: str) -> None:
        if self._context is not None and self._context.status is not None:
            self._context.status(message)


_HANDLE_NAMES: tuple[HandleName, ...] = (
    "tl",
    "tm",
    "tr",
    "mr",
    "br",
    "bm",
    "bl",
    "ml",
)
_CORNER_NAMES: tuple[CornerName, ...] = ("tl", "tr", "br", "bl")
_ROTATION_NAMES: tuple[RotationName, ...] = (
    "rotate_tl",
    "rotate_tr",
    "rotate_br",
    "rotate_bl",
)


def _valid_parameters(parameters: TransformParameters) -> bool:
    return (
        all(math.isfinite(value) for value in parameters.values)
        and 1.0 <= parameters.width <= 8192.0
        and 1.0 <= parameters.height <= 8192.0
        and abs(parameters.x) <= 32768.0
        and abs(parameters.y) <= 32768.0
        and abs(parameters.rotation) <= 36000.0
    )


def _valid_point(point: Point) -> bool:
    return (
        math.isfinite(point.x)
        and math.isfinite(point.y)
        and abs(point.x) <= 32768.0
        and abs(point.y) <= 32768.0
    )


def _valid_quad(corners: tuple[Point, Point, Point, Point]) -> bool:
    if not all(_valid_point(point) for point in corners):
        return False
    crosses = []
    for index in range(4):
        first = corners[index]
        second = corners[(index + 1) % 4]
        third = corners[(index + 2) % 4]
        crosses.append(
            (second.x - first.x) * (third.y - second.y)
            - (second.y - first.y) * (third.x - second.x)
        )
    if any(abs(value) <= 1e-6 for value in crosses):
        return False
    if not (all(value > 0.0 for value in crosses) or all(value < 0.0 for value in crosses)):
        return False
    left = min(point.x for point in corners)
    top = min(point.y for point in corners)
    right = max(point.x for point in corners)
    bottom = max(point.y for point in corners)
    return 1.0 <= right - left <= 8192.0 and 1.0 <= bottom - top <= 8192.0


def _corner_values(corners: tuple[Point, Point, Point, Point]) -> CornerValues:
    return (
        corners[0].x,
        corners[0].y,
        corners[1].x,
        corners[1].y,
        corners[2].x,
        corners[2].y,
        corners[3].x,
        corners[3].y,
    )


def _handle_points(
    corners: tuple[Point, Point, Point, Point],
) -> dict[HandleName, Point]:
    return {
        "tl": corners[0],
        "tm": _midpoint(corners[0], corners[1]),
        "tr": corners[1],
        "mr": _midpoint(corners[1], corners[2]),
        "br": corners[2],
        "bm": _midpoint(corners[2], corners[3]),
        "bl": corners[3],
        "ml": _midpoint(corners[3], corners[0]),
    }


def _resize_edge(
    corners: tuple[Point, Point, Point, Point],
    handle: HandleName,
    press: Point,
    point: Point,
) -> tuple[Point, Point, Point, Point]:
    indices = {
        "tm": (0, 1, 3, 2),
        "mr": (1, 2, 0, 3),
        "bm": (2, 3, 1, 0),
        "ml": (3, 0, 2, 1),
    }.get(handle)
    if indices is None:
        return corners
    first, second, opposite_first, opposite_second = indices
    side_midpoint = _midpoint(corners[first], corners[second])
    opposite_midpoint = _midpoint(
        corners[opposite_first],
        corners[opposite_second],
    )
    nx = side_midpoint.x - opposite_midpoint.x
    ny = side_midpoint.y - opposite_midpoint.y
    length = max(math.hypot(nx, ny), 1e-9)
    nx /= length
    ny /= length
    amount = (point.x - press.x) * nx + (point.y - press.y) * ny
    values = list(corners)
    values[first] = Point(corners[first].x + nx * amount, corners[first].y + ny * amount)
    values[second] = Point(
        corners[second].x + nx * amount,
        corners[second].y + ny * amount,
    )
    return cast(tuple[Point, Point, Point, Point], tuple(values))


def _resize_regular_corner(
    corners: tuple[Point, Point, Point, Point],
    index: int,
    requested: Point,
) -> tuple[Point, Point, Point, Point]:
    u = _unit(corners[1].x - corners[0].x, corners[1].y - corners[0].y)
    left_vector = (corners[3].x - corners[0].x, corners[3].y - corners[0].y)
    v = (-u[1], u[0])
    if v[0] * left_vector[0] + v[1] * left_vector[1] < 0.0:
        v = (-v[0], -v[1])
    signs = ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0))
    sx, sy = signs[index]
    opposite = corners[(index + 2) % 4]
    dx = requested.x - opposite.x
    dy = requested.y - opposite.y
    width = max(1.0, sx * (dx * u[0] + dy * u[1]))
    height = max(1.0, sy * (dx * v[0] + dy * v[1]))
    center = Point(
        opposite.x + sx * u[0] * width / 2.0 + sy * v[0] * height / 2.0,
        opposite.y + sx * u[1] * width / 2.0 + sy * v[1] * height / 2.0,
    )
    return cast(
        tuple[Point, Point, Point, Point],
        tuple(
            Point(
                center.x + sign_x * u[0] * width / 2.0 + sign_y * v[0] * height / 2.0,
                center.y + sign_x * u[1] * width / 2.0 + sign_y * v[1] * height / 2.0,
            )
            for sign_x, sign_y in signs
        ),
    )


def _regularized_corners(
    corners: tuple[Point, Point, Point, Point],
) -> tuple[Point, Point, Point, Point]:
    center = Point(
        sum(point.x for point in corners) / 4.0,
        sum(point.y for point in corners) / 4.0,
    )
    width = max(1.0, (_distance(corners[0], corners[1]) + _distance(corners[3], corners[2])) / 2.0)
    height = max(1.0, (_distance(corners[0], corners[3]) + _distance(corners[1], corners[2])) / 2.0)
    u = _unit(corners[1].x - corners[0].x, corners[1].y - corners[0].y)
    v = (-u[1], u[0])
    if v[0] * (corners[3].x - corners[0].x) + v[1] * (corners[3].y - corners[0].y) < 0.0:
        v = (-v[0], -v[1])
    return cast(
        tuple[Point, Point, Point, Point],
        tuple(
            Point(
                center.x + sx * u[0] * width / 2.0 + sy * v[0] * height / 2.0,
                center.y + sx * u[1] * width / 2.0 + sy * v[1] * height / 2.0,
            )
            for sx, sy in ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0))
        ),
    )


def _map_about_frame(
    point: Point,
    old_pivot: Point,
    old_rotation: float,
    new_rotation: float,
    scale_x: float,
    scale_y: float,
    new_pivot: Point,
) -> Point:
    radians = math.radians(-old_rotation)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    dx = point.x - old_pivot.x
    dy = point.y - old_pivot.y
    local_x = (cosine * dx - sine * dy) * scale_x
    local_y = (sine * dx + cosine * dy) * scale_y
    radians = math.radians(new_rotation)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    return Point(
        new_pivot.x + cosine * local_x - sine * local_y,
        new_pivot.y + sine * local_x + cosine * local_y,
    )


def _rotation_arrow_path() -> QPainterPath:
    path = QPainterPath(QPointF(-7.0, 4.0))
    path.cubicTo(-7.0, -3.0, -3.0, -7.0, 4.0, -7.0)
    path.moveTo(1.0, -10.0)
    path.lineTo(5.0, -7.0)
    path.lineTo(1.0, -4.0)
    return path


def _rotate(point: Point, pivot: Point, degrees: float) -> Point:
    radians = math.radians(degrees)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    dx = point.x - pivot.x
    dy = point.y - pivot.y
    return Point(
        pivot.x + cosine * dx - sine * dy,
        pivot.y + sine * dx + cosine * dy,
    )


def _midpoint(first: Point, second: Point) -> Point:
    return Point((first.x + second.x) / 2.0, (first.y + second.y) / 2.0)


def _distance(first: Point, second: Point) -> float:
    return math.hypot(first.x - second.x, first.y - second.y)


def _unit(x: float, y: float) -> tuple[float, float]:
    length = max(math.hypot(x, y), 1e-9)
    return x / length, y / length


def _view_distance(first: object, second: object) -> float:
    first_point = cast(QPointF, first)
    second_point = cast(QPointF, second)
    return math.hypot(
        first_point.x() - second_point.x(),
        first_point.y() - second_point.y(),
    )


__all__ = ["TransformParameters", "TransformTool"]
