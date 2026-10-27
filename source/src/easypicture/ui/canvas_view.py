"""Zoomable, pannable document canvas and coordinate mapping."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from uuid import UUID

from PySide6.QtCore import QLineF, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QCursor,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QPolygonF,
    QResizeEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QWidget

from easypicture.domain import (
    AffineTransform,
    Document,
    DocumentEvent,
    DocumentSizeChanged,
    Point,
    RasterObject,
    Size,
    SpriteWorkspaceChanged,
    TransformMatrix,
    VectorObject,
)
from easypicture.domain.coordinates import RasterMapping
from easypicture.domain.document import Unsubscribe
from easypicture.imaging import TransformInterpolation
from easypicture.pixels import RGBAArray, TileCoord
from easypicture.render import (
    CanvasRenderCoordinator,
    OpenGLCanvasBackend,
    RenderBackendKind,
)
from easypicture.ui.canvas_item import CanvasBoundsItem
from easypicture.ui.pointer_backend import pointer_backend
from easypicture.ui.selection_overlay import SelectionOverlayItem
from easypicture.ui.selection_style import (
    DEFAULT_SELECTION_OUTLINE_COLOR,
    DEFAULT_SELECTION_OUTLINE_WIDTH,
    MAX_SELECTION_OUTLINE_WIDTH,
)
from easypicture.ui.sprite_grid_overlay import SpriteGridOverlayItem

if TYPE_CHECKING:
    from easypicture.tools.controller import ToolController


class CanvasView(QGraphicsView):
    MIN_ZOOM = 0.0625
    MAX_ZOOM = 32.0
    ZOOM_STEP = 1.25
    MIN_PAN_MARGIN = 64.0

    zoom_changed = Signal(float)
    cursor_document_position_changed = Signal(object)
    render_backend_fallback = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        render_backend: RenderBackendKind = RenderBackendKind.SOFTWARE,
    ) -> None:
        super().__init__(parent)
        self._graphics_scene = QGraphicsScene(self)
        self.setScene(self._graphics_scene)
        self.setObjectName("CanvasView")
        self.setBackgroundBrush(QColor("#363636"))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self._requested_render_backend = render_backend
        self._opengl_backend: OpenGLCanvasBackend | None = None
        self._opengl_viewport: QOpenGLWidget | None = None
        self._fallback_scheduled = False
        platform_name = QGuiApplication.platformName().lower()
        platform_supports_gl_widget = platform_name not in {"minimal", "offscreen"}
        if render_backend is not RenderBackendKind.SOFTWARE and platform_supports_gl_widget:
            gl_viewport = QOpenGLWidget(self)
            gl_viewport.setUpdateBehavior(QOpenGLWidget.UpdateBehavior.NoPartialUpdate)
            self.setViewport(gl_viewport)
            self._opengl_viewport = gl_viewport
            self._opengl_backend = OpenGLCanvasBackend()
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        self._document: Document | None = None
        self._document_unsubscribe: Unsubscribe | None = None
        self._canvas_item: CanvasBoundsItem | None = None
        self._render_coordinator = CanvasRenderCoordinator(
            requested_backend=render_backend,
            parent=self,
        )
        self._render_coordinator.frame_requested.connect(self._request_viewport_frame)
        if render_backend is RenderBackendKind.SOFTWARE:
            self._render_coordinator.set_backend_diagnostics(
                RenderBackendKind.SOFTWARE,
                renderer="Qt QPainter full-frame compositor",
            )
        elif not platform_supports_gl_widget:
            self._render_coordinator.set_backend_diagnostics(
                RenderBackendKind.SOFTWARE,
                renderer="Qt QPainter full-frame compositor",
                fallback_reason=f"Qt {platform_name} platform does not support QOpenGLWidget",
            )
        self._selection_overlay: SelectionOverlayItem | None = None
        self._sprite_grid_overlay: SpriteGridOverlayItem | None = None
        self._pixel_grid_visible = False
        self._sprite_grid_visible = False
        self._selection_outline_color = QColor(DEFAULT_SELECTION_OUTLINE_COLOR)
        self._selection_outline_width = float(DEFAULT_SELECTION_OUTLINE_WIDTH)
        self._zoom = 1.0
        self._space_pressed = False
        self._panning = False
        self._pan_last = QPoint()
        self._tool_cursor: QCursor | None = None
        self._brush_cursor_position: QPointF | None = None
        self._object_selection: tuple[UUID, ...] = ()
        self._brush_cursor_diameter = 0.0
        self._brush_cursor_mapping: RasterMapping | None = None
        self._brush_cursor_visible = False
        self._brush_cursor_suspended = False
        self._tool_controller: ToolController | None = None
        self._interaction_scene_bounds: QRectF | None = None
        self._tile_heatmap_segments: tuple[tuple[QLineF, int], ...] = ()
        self._capture_overlays_requested = False
        self._captured_overlay_frame: QImage | None = None
        self._fit_request_id = 0
        self.scene().setSceneRect(QRectF(-100.0, -100.0, 200.0, 200.0))

    @property
    def document(self) -> Document | None:
        return self._document

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def render_coordinator(self) -> CanvasRenderCoordinator:
        return self._render_coordinator

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

    def set_sprite_grid_visibility(self, *, pixel_grid: bool, cell_grid: bool) -> None:
        self._pixel_grid_visible = bool(pixel_grid)
        self._sprite_grid_visible = bool(cell_grid)
        if self._sprite_grid_overlay is not None:
            self._sprite_grid_overlay.set_visibility(
                pixel_grid=self._pixel_grid_visible,
                cell_grid=self._sprite_grid_visible,
            )

    def set_tool_cursor(self, cursor: Qt.CursorShape | QCursor | None) -> None:
        self._tool_cursor = (
            None
            if cursor is None
            else QCursor(cursor)
            if isinstance(cursor, Qt.CursorShape)
            else QCursor(cursor)
        )
        self._apply_interaction_cursor()

    def update_brush_cursor(
        self,
        viewport_position: QPointF,
        diameter_document: float,
        *,
        mapping: RasterMapping | None = None,
        visible: bool,
        synchronous: bool = True,
    ) -> None:
        old_rect = self._brush_cursor_rect()
        self._brush_cursor_position = QPointF(viewport_position)
        self._brush_cursor_diameter = max(1.0, diameter_document)
        self._brush_cursor_visible = visible
        self._brush_cursor_mapping = mapping
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
        if self._document is not None:
            self._render_coordinator.set_pixel_preview(layer_id, updates)

    def replace_pixel_preview(
        self,
        layer_id: UUID,
        updates: Mapping[TileCoord, RGBAArray | None],
    ) -> None:
        if self._document is not None:
            self._render_coordinator.replace_pixel_preview(layer_id, updates)

    def clear_pixel_preview(self) -> None:
        self._render_coordinator.clear_pixel_preview()

    def set_transform_preview(
        self,
        layer_id: UUID,
        matrix: TransformMatrix,
        interpolation: TransformInterpolation,
    ) -> None:
        if self._document is not None:
            self._render_coordinator.set_transform_preview(
                layer_id,
                matrix,
                interpolation,
            )

    def clear_transform_preview(self) -> None:
        self._render_coordinator.clear_transform_preview()

    def replace_vector_preview(
        self,
        group_id: UUID,
        objects: tuple[VectorObject, ...],
    ) -> None:
        if self._document is not None:
            self._render_coordinator.replace_vector_preview(group_id, objects)

    def replace_transient_vector_preview(
        self,
        objects: tuple[VectorObject, ...],
    ) -> None:
        if self._document is not None:
            self._render_coordinator.replace_transient_vector_preview(objects)

    def clear_vector_preview(self) -> None:
        self._render_coordinator.clear_vector_preview()

    def set_geometry_preview(
        self,
        document_size: Size,
        matrix: TransformMatrix | None = None,
        *,
        layer_id: UUID | None = None,
        interpolation: TransformInterpolation = TransformInterpolation.LINEAR,
    ) -> None:
        if self._document is None:
            return
        self._render_coordinator.set_geometry_preview(
            document_size,
            matrix if matrix is not None else AffineTransform(),
            layer_id=layer_id,
            interpolation=interpolation,
        )
        if self._canvas_item is not None:
            self._canvas_item.set_size(document_size)
        if self._selection_overlay is not None:
            self._selection_overlay.setVisible(False)
        self._update_pan_bounds()

    def clear_geometry_preview(self) -> None:
        self._render_coordinator.clear_geometry_preview()
        if self._document is not None and self._canvas_item is not None:
            self._canvas_item.set_size(self._document.size)
        if self._selection_overlay is not None:
            self._selection_overlay.setVisible(True)
        self._update_pan_bounds()

    def request_render_capture(self, *, include_overlays: bool = False) -> None:
        self._captured_overlay_frame = None
        self._capture_overlays_requested = include_overlays
        if self._opengl_backend is not None and not include_overlays:
            self._opengl_backend.request_capture()
        self.viewport().update()

    def take_render_capture(self) -> QImage | None:
        if self._captured_overlay_frame is not None:
            image, self._captured_overlay_frame = self._captured_overlay_frame, None
            return image
        if self._opengl_backend is None:
            return None
        return self._opengl_backend.take_capture()

    def set_selection_overlay_visible(self, visible: bool) -> None:
        if self._selection_overlay is not None:
            self._selection_overlay.setVisible(visible)

    def set_interaction_scene_bounds(self, bounds: QRectF | None) -> None:
        self._interaction_scene_bounds = None if bounds is None else QRectF(bounds)
        self._update_pan_bounds()

    def set_tile_heatmap(
        self,
        segments: tuple[tuple[float, float, float, float, int], ...],
    ) -> None:
        self._tile_heatmap_segments = tuple(
            (QLineF(x1, y1, x2, y2), min(255, max(0, int(difference))))
            for x1, y1, x2, y2, difference in segments
        )
        self.viewport().update()

    def clear_tile_heatmap(self) -> None:
        if not self._tile_heatmap_segments:
            return
        self._tile_heatmap_segments = ()
        self.viewport().update()

    def set_document(
        self,
        document: Document | None,
        *,
        preserve_view: bool = False,
    ) -> None:
        previous_zoom = self._zoom
        previous_transform = QTransform(self.transform())
        previous_horizontal_scroll = self.horizontalScrollBar().value()
        previous_vertical_scroll = self.verticalScrollBar().value()
        self._tile_heatmap_segments = ()
        self._interaction_scene_bounds = None
        if self._document_unsubscribe is not None:
            self._document_unsubscribe()
            self._document_unsubscribe = None
        if self._selection_overlay is not None:
            self._selection_overlay.dispose()
            self.scene().removeItem(self._selection_overlay)
            self._selection_overlay = None
        if self._sprite_grid_overlay is not None:
            self.scene().removeItem(self._sprite_grid_overlay)
            self._sprite_grid_overlay = None
        if self._canvas_item is not None:
            self.scene().removeItem(self._canvas_item)
        self._document = document
        self._canvas_item = None
        self._render_coordinator.set_document(document)
        if document is None:
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.scene().setSceneRect(QRectF(-100.0, -100.0, 200.0, 200.0))
            self.set_zoom(1.0)
            self.cursor_document_position_changed.emit(None)
            return

        self._canvas_item = CanvasBoundsItem(document.size)
        self.scene().addItem(self._canvas_item)
        self._document_unsubscribe = document.subscribe(self._on_document_event)
        self._selection_overlay = SelectionOverlayItem(
            document,
            self,
            self._selection_outline_color,
            self._selection_outline_width,
        )
        self.scene().addItem(self._selection_overlay)
        self._sprite_grid_overlay = SpriteGridOverlayItem(document, lambda: self._zoom)
        self._sprite_grid_overlay.set_visibility(
            pixel_grid=self._pixel_grid_visible,
            cell_grid=self._sprite_grid_visible,
        )
        self.scene().addItem(self._sprite_grid_overlay)
        if preserve_view:
            # Animation playback replaces the preview Document on every frame.
            # Re-centering through scene coordinates quantizes the center to the
            # integer scroll-bar grid and accumulates a small positional error on
            # every replacement.  Restore the exact view transform and scroll-bar
            # state instead so identical-size preview surfaces remain stationary.
            self._zoom = previous_zoom
            self.setTransform(previous_transform)
            self._update_pan_bounds()
            self.horizontalScrollBar().setValue(previous_horizontal_scroll)
            self.verticalScrollBar().setValue(previous_vertical_scroll)
            self.zoom_changed.emit(self._zoom)
            self.viewport().update()
        else:
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
        canvas_item: CanvasBoundsItem,
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
    def document_to_layer(point: Point, layer: RasterObject) -> Point:
        return Point(point.x - layer.origin.x, point.y - layer.origin.y)

    def viewport_to_document(self, point: QPoint) -> Point:
        return self.scene_to_document(self.viewport_to_scene(point))

    def cancel_interaction(self) -> None:
        self._space_pressed = False
        self._panning = False
        pointer_backend().release(self.viewport())
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
        super().scrollContentsBy(dx, dy)
        self.viewport().update()

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

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self._tool_controller is not None:
            handler = getattr(
                self._tool_controller.active_tool,
                "mouse_double_click",
                None,
            )
            if callable(handler) and handler(event):
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if self._tool_controller is not None:
            handler = getattr(
                self._tool_controller.active_tool,
                "context_menu",
                None,
            )
            if callable(handler) and handler(event):
                event.accept()
                return
        super().contextMenuEvent(event)

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

    def drawBackground(self, painter: QPainter, rect: QRectF | QRect) -> None:
        """Draw one complete raster frame independently from scene item bounds."""
        exposed = QRectF(rect)
        backend = self._opengl_backend
        if backend is None or self._render_coordinator.geometry_preview is not None:
            self._render_coordinator.paint_software(painter, exposed)
            return
        try:
            initialized = backend.initialize(self._render_coordinator)
            rendered = initialized and backend.paint_layers(
                painter,
                self._render_coordinator,
                self.viewport().width(),
                self.viewport().height(),
                self.viewport().devicePixelRatioF(),
            )
        except Exception as error:
            rendered = False
            reason = f"{type(error).__name__}: {error}"
        else:
            reason = (
                "OpenGL canvas rendering failed"
                if initialized
                else "OpenGL 3.3 context initialization failed"
            )
        if not rendered:
            self._schedule_software_fallback(reason)
            self._render_coordinator.paint_software(painter, exposed)
            return

    show_raster_frames = False

    def drawForeground(self, painter: QPainter, rect: QRectF | QRect) -> None:
        super().drawForeground(painter, rect)
        if self.show_raster_frames and self.document is not None:
            from easypicture.domain import RasterObject
            from easypicture.domain.effective import iter_effective_leaves

            painter.save()
            pen = QPen(QColor("#69b9ff"), 1, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for entry in iter_effective_leaves(self.document):
                node = entry.leaf
                if not entry.visible or not isinstance(node, RasterObject):
                    continue
                matrix = self._render_coordinator.raster_display_transform(node)
                corners = [
                    matrix.map_point(Point(x, y))
                    for x, y in (
                        (0, 0),
                        (node.size.width, 0),
                        (node.size.width, node.size.height),
                        (0, node.size.height),
                    )
                ]
                polygon = QPolygonF([QPointF(point.x, point.y) for point in corners])
                if polygon.boundingRect().intersects(QRectF(rect)):
                    painter.drawPolygon(polygon)
            painter.restore()
        if self._object_selection:
            painter.save()
            pen = QPen(QColor("#ff9b38"), 1)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for bounds in self.render_coordinator.selection_bounds(self._object_selection):
                painter.drawRect(QRectF(bounds.x, bounds.y, bounds.width, bounds.height))
            painter.restore()
        if self._tile_heatmap_segments:
            painter.save()
            scene_to_view = painter.worldTransform()
            painter.resetTransform()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            for line, difference in self._tile_heatmap_segments:
                ratio = difference / 255.0
                color = QColor(255, round(138 * (1.0 - ratio)), 0, 230)
                first = scene_to_view.map(line.p1())
                second = scene_to_view.map(line.p2())
                # Integer device-aligned strips avoid the differing half-pixel
                # conventions of cosmetic QPen lines in raster and GL engines.
                if line.x1() == line.x2():
                    top = round(min(first.y(), second.y()))
                    bottom = round(max(first.y(), second.y()))
                    strip = QRect(round(first.x()) - 1, top, 3, max(1, bottom - top))
                else:
                    left = round(min(first.x(), second.x()))
                    right = round(max(first.x(), second.x()))
                    strip = QRect(left, round(first.y()) - 1, max(1, right - left), 3)
                painter.fillRect(strip, color)
            painter.restore()
        rect = self._brush_cursor_rect()
        if rect.isEmpty():
            self._capture_completed_frame(painter)
            return
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        outer = QPen(QColor(0, 0, 0, 220))
        outer.setWidth(3)
        outer.setCosmetic(True)
        painter.setPen(outer)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self._brush_cursor_mapping is None:
            painter.drawEllipse(rect)
        else:
            painter.drawPolygon(self._brush_cursor_polygon())
        inner = QPen(QColor(255, 255, 255, 235))
        inner.setWidth(1)
        inner.setCosmetic(True)
        painter.setPen(inner)
        if self._brush_cursor_mapping is None:
            painter.drawEllipse(rect)
        else:
            painter.drawPolygon(self._brush_cursor_polygon())
        painter.restore()

        self._capture_completed_frame(painter)

    def set_object_selection(self, ids: tuple[UUID, ...]) -> None:
        self._object_selection = ids
        self.viewport().update()

    def _capture_completed_frame(self, painter: QPainter) -> None:
        backend = self._opengl_backend
        if self._capture_overlays_requested and backend is not None and backend.is_initialized:
            self._capture_overlays_requested = False
            ratio = self.viewport().devicePixelRatioF()
            self._captured_overlay_frame = backend.capture_completed_frame(
                painter,
                round(self.viewport().width() * ratio),
                round(self.viewport().height() * ratio),
            )

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

    def _brush_cursor_polygon(self) -> QPolygonF:
        mapping = self._brush_cursor_mapping
        position = self._brush_cursor_position
        if mapping is None or position is None:
            return QPolygonF()
        center = self.viewport_to_document(position.toPoint())
        transform = self.viewportTransform()
        return QPolygonF(
            [
                transform.map(QPointF(point.x, point.y))
                for point in mapping.brush_outline(center, self._brush_cursor_diameter)
            ]
        )

    def _brush_cursor_rect(self) -> QRectF:
        if (
            self._brush_cursor_position is None
            or not self._brush_cursor_visible
            or self._brush_cursor_suspended
        ):
            return QRectF()
        if self._brush_cursor_mapping is not None:
            return self._brush_cursor_polygon().boundingRect()
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
        scene_content_rect = (
            canvas_rect
            if self._interaction_scene_bounds is None
            else canvas_rect.united(self._interaction_scene_bounds)
        )
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
            scene_content_rect.adjusted(
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
        del old_rect, synchronous
        self.viewport().update()

    def _set_brush_cursor_suspended(self, suspended: bool) -> None:
        if suspended == self._brush_cursor_suspended:
            return
        old_rect = self._brush_cursor_rect()
        self._brush_cursor_suspended = suspended
        self._invalidate_brush_cursor(old_rect)

    def _refresh_brush_cursor_under_pointer(self) -> None:
        if self._brush_cursor_position is None or self._document is None:
            return
        viewport_point = self.viewport().mapFromGlobal(pointer_backend().position())
        visible = self.viewport().rect().contains(viewport_point)
        self.update_brush_cursor(
            QPointF(viewport_point),
            self._brush_cursor_diameter,
            mapping=self._brush_cursor_mapping,
            visible=visible,
            synchronous=False,
        )

    def _request_viewport_frame(self) -> None:
        self.viewport().update()

    def _on_document_event(self, event: DocumentEvent) -> None:
        if isinstance(event, SpriteWorkspaceChanged):
            if self._sprite_grid_overlay is not None:
                self._sprite_grid_overlay.update()
            self.viewport().update()
            return
        if not isinstance(event, DocumentSizeChanged):
            return
        if self._canvas_item is not None:
            self._canvas_item.set_size(event.new_size)
        if self._sprite_grid_overlay is not None:
            self._sprite_grid_overlay.document_size_changed()
        self._update_pan_bounds()
        self.viewport().update()

    def _schedule_software_fallback(self, reason: str) -> None:
        if self._fallback_scheduled:
            return
        self._fallback_scheduled = True
        QTimer.singleShot(0, lambda: self._activate_software_fallback(reason))

    def _activate_software_fallback(self, reason: str) -> None:
        backend = self._opengl_backend
        viewport = self._opengl_viewport
        if backend is None:
            return
        if viewport is not None and viewport.isValid():
            viewport.makeCurrent()
            try:
                backend.release()
            finally:
                viewport.doneCurrent()
        self._opengl_backend = None
        self._opengl_viewport = None
        software_viewport = QWidget(self)
        software_viewport.setMouseTracking(True)
        self.setViewport(software_viewport)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self._render_coordinator.set_backend_diagnostics(
            RenderBackendKind.SOFTWARE,
            renderer="Qt QPainter full-frame compositor",
            fallback_reason=reason,
        )
        self._fallback_scheduled = False
        self.render_backend_fallback.emit(reason)
        self.viewport().update()


__all__ = ["CanvasView"]
