"""Zoomable, pannable document canvas and coordinate mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent, QTransform, QWheelEvent
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QWidget

from easypicture.domain import Document, Point, RasterLayer
from easypicture.render import TileRenderer
from easypicture.ui.canvas_item import CanvasItem

if TYPE_CHECKING:
    from easypicture.tools.controller import ToolController


class CanvasView(QGraphicsView):
    MIN_ZOOM = 0.0625
    MAX_ZOOM = 32.0
    ZOOM_STEP = 1.25

    zoom_changed = Signal(float)
    cursor_document_position_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._graphics_scene = QGraphicsScene(self)
        self.setScene(self._graphics_scene)
        self.setObjectName("CanvasView")
        self.setBackgroundBrush(Qt.GlobalColor.darkGray)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        self._document: Document | None = None
        self._canvas_item: CanvasItem | None = None
        self._zoom = 1.0
        self._space_pressed = False
        self._panning = False
        self._pan_last = QPoint()
        self._tool_controller: ToolController | None = None
        self._fit_request_id = 0
        self.scene().setSceneRect(QRectF(-100.0, -100.0, 200.0, 200.0))

    @property
    def document(self) -> Document | None:
        return self._document

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def renderer(self) -> TileRenderer | None:
        return self._canvas_item.renderer if self._canvas_item is not None else None

    def set_tool_controller(self, controller: ToolController) -> None:
        self._tool_controller = controller

    def set_document(self, document: Document | None) -> None:
        if self._canvas_item is not None:
            self._canvas_item.dispose()
            self.scene().removeItem(self._canvas_item)
        self._document = document
        self._canvas_item = None
        if document is None:
            self.scene().setSceneRect(QRectF(-100.0, -100.0, 200.0, 200.0))
            self.set_zoom(1.0)
            self.cursor_document_position_changed.emit(None)
            return

        renderer = TileRenderer(document, parent=self)
        self._canvas_item = CanvasItem(document.size, renderer)
        self.scene().addItem(self._canvas_item)
        margin = 64.0
        self.scene().setSceneRect(
            self._canvas_item.boundingRect().adjusted(-margin, -margin, margin, margin)
        )
        self.set_zoom(1.0)
        self.centerOn(self._canvas_item)

    def set_zoom(self, zoom: float, anchor: QPoint | None = None) -> None:
        self._fit_request_id += 1
        clamped = min(self.MAX_ZOOM, max(self.MIN_ZOOM, zoom))
        if anchor is None:
            anchor = self.viewport().rect().center()
        scene_anchor = self.mapToScene(anchor)
        transform = QTransform()
        transform.scale(clamped, clamped)
        self.setTransform(transform)
        moved_anchor = self.mapToScene(anchor)
        delta = moved_anchor - scene_anchor
        self.translate(delta.x(), delta.y())
        self._zoom = clamped
        self.zoom_changed.emit(self._zoom)

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom * self.ZOOM_STEP)

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom / self.ZOOM_STEP)

    def actual_size(self) -> None:
        self.set_zoom(1.0)

    def fit_document(self) -> None:
        if self._canvas_item is None:
            return
        self._fit_request_id += 1
        self._apply_document_fit()
        request_id = self._fit_request_id
        canvas_item = self._canvas_item
        QTimer.singleShot(
            0,
            lambda: self._refine_document_fit(request_id, canvas_item),
        )

    def _apply_document_fit(self) -> None:
        if self._canvas_item is None:
            return
        self.fitInView(self._canvas_item.boundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
        fitted = abs(self.transform().m11())
        self._zoom = min(self.MAX_ZOOM, max(self.MIN_ZOOM, fitted))
        if fitted != self._zoom:
            self.set_zoom(self._zoom)
        else:
            self.zoom_changed.emit(self._zoom)

    def _refine_document_fit(
        self,
        request_id: int,
        canvas_item: CanvasItem,
    ) -> None:
        if request_id != self._fit_request_id or canvas_item is not self._canvas_item:
            return
        self._apply_document_fit()

    def global_to_viewport(self, point: QPoint) -> QPoint:
        return self.viewport().mapFromGlobal(point)

    def viewport_to_scene(self, point: QPoint) -> QPointF:
        return self.mapToScene(point)

    def scene_to_document(self, point: QPointF) -> Point:
        if self._canvas_item is None:
            return Point(point.x(), point.y())
        local = self._canvas_item.mapFromScene(point)
        return Point(local.x(), local.y())

    @staticmethod
    def document_to_layer(point: Point, layer: RasterLayer) -> Point:
        return Point(point.x - layer.origin.x, point.y - layer.origin.y)

    def viewport_to_document(self, point: QPoint) -> Point:
        return self.scene_to_document(self.viewport_to_scene(point))

    def cancel_interaction(self) -> None:
        self._space_pressed = False
        self._panning = False
        mouse_grabber = QWidget.mouseGrabber()
        if mouse_grabber is self.viewport():
            self.viewport().releaseMouse()
        self.viewport().unsetCursor()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = self.ZOOM_STEP if event.angleDelta().y() > 0 else 1.0 / self.ZOOM_STEP
            self.set_zoom(self._zoom * factor, event.position().toPoint())
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pan_button = event.button() == Qt.MouseButton.MiddleButton
        space_pan = self._space_pressed and event.button() == Qt.MouseButton.LeftButton
        if pan_button or space_pan:
            self._panning = True
            self._pan_last = event.position().toPoint()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if self._tool_controller is not None and self._tool_controller.mouse_press(event):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._emit_cursor_position(event.position().toPoint())
        if self._panning:
            current = event.position().toPoint()
            delta = current - self._pan_last
            self._pan_last = current
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        if self._tool_controller is not None and self._tool_controller.mouse_move(event):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._panning and event.button() in (
            Qt.MouseButton.MiddleButton,
            Qt.MouseButton.LeftButton,
        ):
            self._panning = False
            self.viewport().setCursor(
                Qt.CursorShape.OpenHandCursor if self._space_pressed else Qt.CursorShape.ArrowCursor
            )
            event.accept()
            return
        if self._tool_controller is not None and self._tool_controller.mouse_release(event):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_pressed = True
            if not self._panning:
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        if self._tool_controller is not None and self._tool_controller.key_press(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_pressed = False
            if not self._panning:
                self.viewport().unsetCursor()
            event.accept()
            return
        if self._tool_controller is not None and self._tool_controller.key_release(event):
            event.accept()
            return
        super().keyReleaseEvent(event)

    def leaveEvent(self, event: object) -> None:
        self.cursor_document_position_changed.emit(None)
        super().leaveEvent(event)  # type: ignore[arg-type]

    def _emit_cursor_position(self, viewport_point: QPoint) -> None:
        if self._document is None:
            self.cursor_document_position_changed.emit(None)
            return
        point = self.viewport_to_document(viewport_point)
        if 0.0 <= point.x < self._document.width and 0.0 <= point.y < self._document.height:
            self.cursor_document_position_changed.emit(point)
        else:
            self.cursor_document_position_changed.emit(None)


__all__ = ["CanvasView"]
