"""Interactive non-destructive crop rectangle tool."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Literal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from easypicture.imaging import GeometryRect
from easypicture.tools.protocol import ToolContext

type CropHandle = Literal[
    "move",
    "left",
    "right",
    "top",
    "bottom",
    "top_left",
    "top_right",
    "bottom_left",
    "bottom_right",
]


class CropOverlayItem(QGraphicsItem):
    """Full-scene crop shade, checker extension, border, handles and thirds grid."""

    def __init__(self, document_rect: QRectF, crop_rect: QRectF) -> None:
        super().__init__()
        self.document_rect = QRectF(document_rect)
        self.crop_rect = QRectF(crop_rect)
        self.show_grid = True
        self.setZValue(9_500.0)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def boundingRect(self) -> QRectF:
        margin = max(self.document_rect.width(), self.document_rect.height(), 4096.0)
        return self.document_rect.united(self.crop_rect).adjusted(
            -margin,
            -margin,
            margin,
            margin,
        )

    def set_crop_rect(self, rect: QRectF) -> None:
        self.prepareGeometryChange()
        self.crop_rect = QRectF(rect)
        self.update()

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        _ = option, widget
        painter.save()
        outside = QPainterPath()
        outside.addRect(self.boundingRect())
        crop = QPainterPath()
        crop.addRect(self.crop_rect)
        painter.fillPath(outside.subtracted(crop), QColor(0, 0, 0, 145))

        extension = QPainterPath()
        extension.addRect(self.crop_rect)
        document = QPainterPath()
        document.addRect(self.document_rect)
        painter.setClipPath(extension.subtracted(document))
        cell = 16
        left = math.floor(self.crop_rect.left() / cell) * cell
        top = math.floor(self.crop_rect.top() / cell) * cell
        right = math.ceil(self.crop_rect.right() / cell) * cell
        bottom = math.ceil(self.crop_rect.bottom() / cell) * cell
        for grid_y in range(top, bottom, cell):
            for grid_x in range(left, right, cell):
                color = (
                    QColor("#f0f0f0")
                    if (grid_x // cell + grid_y // cell) % 2
                    else QColor("#cfcfcf")
                )
                painter.fillRect(QRectF(grid_x, grid_y, cell, cell), color)
        painter.setClipping(False)

        orange = QColor("#ff8a00")
        border = QPen(orange)
        border.setCosmetic(True)
        border.setWidth(1)
        painter.setPen(border)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.crop_rect)
        if self.show_grid:
            grid = QPen(QColor(255, 255, 255, 150))
            grid.setCosmetic(True)
            grid.setWidth(1)
            painter.setPen(grid)
            for fraction in (1.0 / 3.0, 2.0 / 3.0):
                line_x = self.crop_rect.left() + self.crop_rect.width() * fraction
                line_y = self.crop_rect.top() + self.crop_rect.height() * fraction
                painter.drawLine(
                    QPointF(line_x, self.crop_rect.top()),
                    QPointF(line_x, self.crop_rect.bottom()),
                )
                painter.drawLine(
                    QPointF(self.crop_rect.left(), line_y),
                    QPointF(self.crop_rect.right(), line_y),
                )

        scale = max(1e-6, abs(painter.worldTransform().m11()))
        handle_size = 7.0 / scale
        painter.setPen(border)
        painter.setBrush(QColor("#202020"))
        for point in _handle_points(self.crop_rect):
            painter.drawRect(
                QRectF(
                    point.x() - handle_size / 2.0,
                    point.y() - handle_size / 2.0,
                    handle_size,
                    handle_size,
                )
            )
        painter.restore()


class CropTool:
    def __init__(
        self,
        ratio_provider: Callable[[], float | None],
        grid_provider: Callable[[], bool],
        rect_sink: Callable[[GeometryRect | None], None],
        commit_sink: Callable[[GeometryRect], None],
        cancel_sink: Callable[[], None],
    ) -> None:
        self._ratio_provider = ratio_provider
        self._grid_provider = grid_provider
        self._rect_sink = rect_sink
        self._commit_sink = commit_sink
        self._cancel_sink = cancel_sink
        self._context: ToolContext | None = None
        self._rect: QRectF | None = None
        self._overlay: CropOverlayItem | None = None
        self._handle: CropHandle | None = None
        self._press_point: QPointF | None = None
        self._gesture_rect: QRectF | None = None
        self._suppress_right_release = False
        self._suppress_context_menu = False

    @property
    def crop_rect(self) -> GeometryRect | None:
        return None if self._rect is None else _geometry_rect(self._rect)

    def activate(self, context: ToolContext) -> None:
        self._context = context
        document = context.document_provider()
        if document is None:
            return
        self._rect = QRectF(0.0, 0.0, float(document.width), float(document.height))
        self._overlay = CropOverlayItem(
            QRectF(0.0, 0.0, float(document.width), float(document.height)),
            self._rect,
        )
        self._overlay.show_grid = self._grid_provider()
        context.overlays.add(self._overlay)
        context.canvas.set_interaction_scene_bounds(self._rect)
        context.canvas.set_tool_cursor(Qt.CursorShape.CrossCursor)
        self._publish()

    def deactivate(self) -> None:
        if self._context is not None:
            self._context.canvas.set_interaction_scene_bounds(None)
            self._context.canvas.set_tool_cursor(None)
        self._context = None
        self._rect = None
        self._overlay = None
        self._handle = None
        self._press_point = None
        self._gesture_rect = None
        self._suppress_right_release = False
        self._suppress_context_menu = False
        self._rect_sink(None)

    def set_rect(self, rect: GeometryRect) -> None:
        if self._context is None:
            return
        self._rect = QRectF(
            float(rect.x),
            float(rect.y),
            float(rect.width),
            float(rect.height),
        )
        self._update_overlay()
        self._publish()

    def refresh_options(self) -> None:
        if self._overlay is not None:
            self._overlay.show_grid = self._grid_provider()
            self._overlay.update()

    def commit(self) -> None:
        rect = self.crop_rect
        if rect is not None:
            self._commit_sink(rect)

    def cancel(self) -> None:
        if self._context is not None:
            self._context.canvas.set_interaction_scene_bounds(None)
        self._rect_sink(None)
        self._cancel_sink()

    def mouse_press(self, event: QMouseEvent) -> bool:
        if (
            self._context is None
            or self._rect is None
            or event.button() != Qt.MouseButton.LeftButton
        ):
            if event.button() == Qt.MouseButton.RightButton and self._handle is not None:
                self._restore_gesture()
                self._suppress_right_release = True
                self._suppress_context_menu = True
                return True
            return False
        point = _document_point(self._context, event)
        self._handle = self._hit_test(point)
        self._press_point = point
        self._gesture_rect = QRectF(self._rect)
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        if (
            self._context is None
            or self._handle is None
            or self._press_point is None
            or self._gesture_rect is None
        ):
            return False
        point = _document_point(self._context, event)
        delta = point - self._press_point
        rect = QRectF(self._gesture_rect)
        if self._handle == "move":
            rect.translate(delta)
        else:
            if "left" in self._handle:
                rect.setLeft(rect.left() + delta.x())
            if "right" in self._handle:
                rect.setRight(rect.right() + delta.x())
            if "top" in self._handle:
                rect.setTop(rect.top() + delta.y())
            if "bottom" in self._handle:
                rect.setBottom(rect.bottom() + delta.y())
            rect = rect.normalized()
            rect = self._apply_ratio(rect, self._handle)
        self._rect = _minimum_rect(rect)
        self._update_overlay()
        self._publish()
        return True

    def mouse_release(self, event: QMouseEvent) -> bool:
        if event.button() == Qt.MouseButton.RightButton and self._suppress_right_release:
            self._suppress_right_release = False
            return True
        if event.button() != Qt.MouseButton.LeftButton or self._handle is None:
            return False
        self._handle = None
        self._press_point = None
        self._gesture_rect = None
        return True

    def key_press(self, event: QKeyEvent) -> bool:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit()
            return True
        if event.key() == Qt.Key.Key_Escape:
            if self._handle is not None:
                self._restore_gesture()
            else:
                self.cancel()
            return True
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        _ = event
        return False

    def context_menu(self, event: QContextMenuEvent) -> bool:
        _ = event
        if not self._suppress_context_menu:
            return False
        self._suppress_context_menu = False
        return True

    def _hit_test(self, point: QPointF) -> CropHandle:
        assert self._rect is not None and self._context is not None
        tolerance = 9.0 / max(self._context.canvas.zoom, 1e-6)
        rect = self._rect
        near_left = abs(point.x() - rect.left()) <= tolerance
        near_right = abs(point.x() - rect.right()) <= tolerance
        near_top = abs(point.y() - rect.top()) <= tolerance
        near_bottom = abs(point.y() - rect.bottom()) <= tolerance
        if near_left and near_top:
            return "top_left"
        if near_right and near_top:
            return "top_right"
        if near_left and near_bottom:
            return "bottom_left"
        if near_right and near_bottom:
            return "bottom_right"
        if near_left:
            return "left"
        if near_right:
            return "right"
        if near_top:
            return "top"
        if near_bottom:
            return "bottom"
        return "move"

    def _apply_ratio(self, rect: QRectF, handle: CropHandle) -> QRectF:
        ratio = self._ratio_provider()
        if ratio is None or ratio <= 0.0:
            return rect
        width = max(1.0, rect.width())
        height = max(1.0, rect.height())
        if width / height > ratio:
            height = width / ratio
        else:
            width = height * ratio
        result = QRectF(rect)
        if "left" in handle:
            result.setLeft(result.right() - width)
        else:
            result.setRight(result.left() + width)
        if "top" in handle:
            result.setTop(result.bottom() - height)
        else:
            result.setBottom(result.top() + height)
        return result

    def _restore_gesture(self) -> None:
        if self._gesture_rect is not None:
            self._rect = QRectF(self._gesture_rect)
            self._update_overlay()
            self._publish()
        self._handle = None
        self._press_point = None
        self._gesture_rect = None

    def _update_overlay(self) -> None:
        if self._overlay is not None and self._rect is not None:
            self._overlay.set_crop_rect(self._rect)
        if self._context is not None:
            self._context.canvas.set_interaction_scene_bounds(self._rect)

    def _publish(self) -> None:
        self._rect_sink(self.crop_rect)


def _document_point(context: ToolContext, event: QMouseEvent) -> QPointF:
    point = context.canvas.viewport_to_document(event.position().toPoint())
    return QPointF(point.x, point.y)


def _geometry_rect(rect: QRectF) -> GeometryRect:
    left = math.floor(rect.left())
    top = math.floor(rect.top())
    right = math.ceil(rect.right())
    bottom = math.ceil(rect.bottom())
    return GeometryRect(left, top, max(1, right - left), max(1, bottom - top))


def _minimum_rect(rect: QRectF) -> QRectF:
    return QRectF(rect.left(), rect.top(), max(1.0, rect.width()), max(1.0, rect.height()))


def _handle_points(rect: QRectF) -> tuple[QPointF, ...]:
    center = rect.center()
    return (
        rect.topLeft(),
        QPointF(center.x(), rect.top()),
        rect.topRight(),
        QPointF(rect.right(), center.y()),
        rect.bottomRight(),
        QPointF(center.x(), rect.bottom()),
        rect.bottomLeft(),
        QPointF(rect.left(), center.y()),
    )


__all__ = ["CropOverlayItem", "CropTool"]
