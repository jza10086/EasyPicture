"""Zoomable, pannable document canvas and coordinate mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QWidget

from easypicture.domain import Document, Point, RasterLayer
from easypicture.pixels import RGBAArray, TileCoord
from easypicture.render import TileRenderer
from easypicture.ui.canvas_item import CanvasItem
from easypicture.ui.selection_overlay import SelectionOverlayItem
from easypicture.ui.selection_style import (
    DEFAULT_SELECTION_OUTLINE_COLOR,
    DEFAULT_SELECTION_OUTLINE_WIDTH,
    MAX_SELECTION_OUTLINE_WIDTH,
)

if TYPE_CHECKING:
    from easypicture.tools.controller import ToolController


class CanvasView(QGraphicsView):
    MIN_ZOOM = 0.0625
    MAX_ZOOM = 32.0
    ZOOM_STEP = 1.25
    MIN_PAN_MARGIN = 64.0

    zoom_changed = Signal(float)
    cursor_document_position_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._graphics_scene = QGraphicsScene(self)
        self.setScene(self._graphics_scene)
        self.setObjectName("CanvasView")
        self.setBackgroundBrush(QColor("#363636"))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        self._document: Document | None = None
        self._canvas_item: CanvasItem | None = None
        self._selection_overlay: SelectionOverlayItem | None = None
        self._selection_outline_color = QColor(DEFAULT_SELECTION_OUTLINE_COLOR)
        self._selection_outline_width = float(DEFAULT_SELECTION_OUTLINE_WIDTH)
        self._zoom = 1.0
        self._space_pressed = False
        self._panning = False
        self._pan_last = QPoint()
        self._tool_cursor: QCursor | None = None
        self._brush_cursor_position: QPointF | None = None
        self._brush_cursor_diameter = 0.0
        self._brush_cursor_visible = False
        self._brush_cursor_suspended = False
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

    def set_selection_outline_style(self, color: QColor, width: float) -> None:
        resolved_color = QColor(color)
        resolved_color.setAlpha(255)
        self._selection_outline_color = resolved_color
        self._selection_outline_width = min(
            float(MAX_SELECTION_OUTLINE_WIDTH),
            max(1.0, width),
        )
        if self._selection_overlay is not None:
            self._selection_overlay.set_style(
                self._selection_outline_color,
                self._selection_outline_width,
            )

    def set_tool_cursor(self, cursor: Qt.CursorShape | QCursor | None) -> None:
        self._tool_cursor = (
            None
            if cursor is None
            else QCursor(cursor) if isinstance(cursor, Qt.CursorShape) else QCursor(cursor)
        )
        self._apply_interaction_cursor()

    def update_brush_cursor(
        self,
        viewport_position: QPointF,
        diameter_document: float,
        *,
        visible: bool,
        synchronous: bool = True,
    ) -> None:
        old_rect = self._brush_cursor_rect()
        self._brush_cursor_position = QPointF(viewport_position)
        self._brush_cursor_diameter = max(1.0, diameter_document)
        self._brush_cursor_visible = visible
        self._invalidate_brush_cursor(old_rect, synchronous=synchronous)

    def clear_brush_cursor(self) -> None:
        old_rect = self._brush_cursor_rect()
        self._brush_cursor_position = None
        self._brush_cursor_diameter = 0.0
        self._brush_cursor_visible = False
        self._invalidate_brush_cursor(old_rect)

    def set_pixel_preview(
        self,
        layer_id: UUID,
        updates: dict[TileCoord, RGBAArray],
    ) -> None:
        renderer = self.renderer
        if renderer is not None:
            renderer.set_preview_tiles(layer_id, updates)

    def clear_pixel_preview(self) -> None:
        renderer = self.renderer
        if renderer is not None:
            renderer.clear_preview()

    def set_document(self, document: Document | None) -> None:
        if self._selection_overlay is not None:
            self._selection_overlay.dispose()
            self.scene().removeItem(self._selection_overlay)
            self._selection_overlay = None
        if self._canvas_item is not None:
            self._canvas_item.dispose()
            self.scene().removeItem(self._canvas_item)
        self._document = document
        self._canvas_item = None
        if document is None:
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.scene().setSceneRect(QRectF(-100.0, -100.0, 200.0, 200.0))
            self.set_zoom(1.0)
            self.cursor_document_position_changed.emit(None)
            return

        renderer = TileRenderer(document, parent=self)
        self._canvas_item = CanvasItem(document.size, renderer)
        self.scene().addItem(self._canvas_item)
        self._selection_overlay = SelectionOverlayItem(
            document,
            self,
            self._selection_outline_color,
            self._selection_outline_width,
        )
        self.scene().addItem(self._selection_overlay)
        self.set_zoom(1.0)
        self.centerOn(self._canvas_item)

    def set_zoom(self, zoom: float, anchor: QPoint | None = None) -> None:
        self._fit_request_id += 1
        clamped = min(self.MAX_ZOOM, max(self.MIN_ZOOM, zoom))
        if anchor is None:
            anchor = self.viewport().rect().center()
        scene_anchor = self.mapToScene(anchor)
        self._zoom = clamped
        transform = QTransform()
        transform.scale(clamped, clamped)
        self.setTransform(transform)
        self._update_pan_bounds()
        moved_anchor = self.mapToScene(anchor)
        delta = moved_anchor - scene_anchor
        self.translate(delta.x(), delta.y())
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
            self._update_pan_bounds()
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
        self._refresh_brush_cursor_under_pointer()
        self._set_brush_cursor_suspended(False)
        self._apply_interaction_cursor()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = self.ZOOM_STEP if event.angleDelta().y() > 0 else 1.0 / self.ZOOM_STEP
            self.set_zoom(self._zoom * factor, event.position().toPoint())
            event.accept()
            return
        super().wheelEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_pan_bounds()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        old_rect = self._brush_cursor_rect()
        if old_rect.isEmpty():
            super().scrollContentsBy(dx, dy)
            return

        # QGraphicsView normally scrolls by blitting the existing viewport
        # pixels. The brush outline is painted above the scene, so it must be
        # removed before that blit or its pixels are copied to a stale position.
        self._brush_cursor_suspended = True
        self._invalidate_brush_cursor(old_rect, synchronous=True)
        try:
            super().scrollContentsBy(dx, dy)
        finally:
            self._brush_cursor_suspended = False
        self._invalidate_brush_cursor(QRectF(), synchronous=True)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pan_button = event.button() == Qt.MouseButton.MiddleButton
        space_pan = self._space_pressed and event.button() == Qt.MouseButton.LeftButton
        if pan_button or space_pan:
            self._panning = True
            self._pan_last = event.position().toPoint()
            self._set_brush_cursor_suspended(True)
            self._apply_interaction_cursor()
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
            self._apply_interaction_cursor()
            if not self._space_pressed:
                self._refresh_brush_cursor_under_pointer()
                self._set_brush_cursor_suspended(False)
            event.accept()
            return
        if self._tool_controller is not None and self._tool_controller.mouse_release(event):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_pressed = True
            self._set_brush_cursor_suspended(True)
            self._apply_interaction_cursor()
            event.accept()
            return
        if self._tool_controller is not None and self._tool_controller.key_press(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_pressed = False
            self._apply_interaction_cursor()
            if not self._panning:
                self._refresh_brush_cursor_under_pointer()
                self._set_brush_cursor_suspended(False)
            event.accept()
            return
        if self._tool_controller is not None and self._tool_controller.key_release(event):
            event.accept()
            return
        super().keyReleaseEvent(event)

    def leaveEvent(self, event: object) -> None:
        self.cursor_document_position_changed.emit(None)
        old_rect = self._brush_cursor_rect()
        self._brush_cursor_visible = False
        self._invalidate_brush_cursor(old_rect)
        super().leaveEvent(event)  # type: ignore[arg-type]

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        rect = self._brush_cursor_rect()
        if rect.isEmpty():
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        outer = QPen(QColor(0, 0, 0, 220))
        outer.setWidth(3)
        outer.setCosmetic(True)
        painter.setPen(outer)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)
        inner = QPen(QColor(255, 255, 255, 235))
        inner.setWidth(1)
        inner.setCosmetic(True)
        painter.setPen(inner)
        painter.drawEllipse(rect)
        painter.end()

    def _emit_cursor_position(self, viewport_point: QPoint) -> None:
        if self._document is None:
            self.cursor_document_position_changed.emit(None)
            return
        point = self.viewport_to_document(viewport_point)
        if 0.0 <= point.x < self._document.width and 0.0 <= point.y < self._document.height:
            self.cursor_document_position_changed.emit(point)
        else:
            self.cursor_document_position_changed.emit(None)

    def _apply_interaction_cursor(self) -> None:
        if self._panning:
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._space_pressed:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        elif self._tool_cursor is not None:
            self.viewport().setCursor(self._tool_cursor)
        else:
            self.viewport().unsetCursor()

    def _brush_cursor_rect(self) -> QRectF:
        if (
            self._brush_cursor_position is None
            or not self._brush_cursor_visible
            or self._brush_cursor_suspended
        ):
            return QRectF()
        diameter = max(1.0, self._brush_cursor_diameter * self._zoom)
        radius = diameter / 2.0
        return QRectF(
            self._brush_cursor_position.x() - radius,
            self._brush_cursor_position.y() - radius,
            diameter,
            diameter,
        )

    def _update_pan_bounds(self) -> None:
        if self._canvas_item is None:
            return

        canvas_rect = self._canvas_item.sceneBoundingRect()
        available = self.maximumViewportSize()
        horizontal_fits = canvas_rect.width() * self._zoom <= available.width()
        vertical_fits = canvas_rect.height() * self._zoom <= available.height()
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            if horizontal_fits
            else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            if vertical_fits
            else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        viewport_size = self.viewport().size()
        zoom = max(self.MIN_ZOOM, self._zoom)
        horizontal_margin = max(
            self.MIN_PAN_MARGIN,
            viewport_size.width() / (2.0 * zoom),
        )
        vertical_margin = max(
            self.MIN_PAN_MARGIN,
            viewport_size.height() / (2.0 * zoom),
        )
        self.scene().setSceneRect(
            canvas_rect.adjusted(
                -horizontal_margin,
                -vertical_margin,
                horizontal_margin,
                vertical_margin,
            )
        )

    def _invalidate_brush_cursor(
        self,
        old_rect: QRectF,
        *,
        synchronous: bool = False,
    ) -> None:
        new_rect = self._brush_cursor_rect()
        if old_rect.isEmpty() and new_rect.isEmpty():
            return
        dirty_rects = [rect for rect in (old_rect, new_rect) if not rect.isEmpty()]
        if len(dirty_rects) == 2 and dirty_rects[0].intersects(dirty_rects[1]):
            dirty_rects = [dirty_rects[0].united(dirty_rects[1])]
        viewport_rect = QRectF(self.viewport().rect())
        for dirty in dirty_rects:
            target = (
                dirty.adjusted(-4.0, -4.0, 4.0, 4.0)
                .intersected(viewport_rect)
                .toAlignedRect()
            )
            if target.isEmpty():
                continue
            if synchronous:
                self.viewport().repaint(target)
            else:
                self.viewport().update(target)

    def _set_brush_cursor_suspended(self, suspended: bool) -> None:
        if suspended == self._brush_cursor_suspended:
            return
        old_rect = self._brush_cursor_rect()
        self._brush_cursor_suspended = suspended
        self._invalidate_brush_cursor(old_rect)

    def _refresh_brush_cursor_under_pointer(self) -> None:
        if self._brush_cursor_position is None or self._document is None:
            return
        viewport_point = self.viewport().mapFromGlobal(QCursor.pos())
        visible = self.viewport().rect().contains(viewport_point)
        self.update_brush_cursor(
            QPointF(viewport_point),
            self._brush_cursor_diameter,
            visible=visible,
            synchronous=False,
        )


__all__ = ["CanvasView"]
