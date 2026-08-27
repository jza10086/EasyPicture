"""Fixed-size, integer-coordinate manual sprite-frame slicing tool."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from easypicture.pixels import Rect
from easypicture.tools.protocol import ToolContext


class SpriteSliceOverlayItem(QGraphicsItem):
    BUTTON_WIDTH_PX = 72.0
    BUTTON_HEIGHT_PX = 24.0
    BUTTON_GAP_PX = 7.0

    def __init__(self, region: Rect, zoom_provider: Callable[[], float]) -> None:
        super().__init__()
        self.region = region
        self._zoom_provider = zoom_provider
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._reference_image: QImage | None = None
        self._reference_opacity = 0.5

    def boundingRect(self) -> QRectF:
        zoom = max(0.0625, self._zoom_provider())
        margin = 4.0 / zoom
        return self.region_rect().united(self.button_rect()).adjusted(
            -margin,
            -margin,
            margin,
            margin,
        )

    def region_rect(self) -> QRectF:
        return QRectF(
            float(self.region.x),
            float(self.region.y),
            float(self.region.width),
            float(self.region.height),
        )

    def button_rect(self) -> QRectF:
        zoom = max(0.0625, self._zoom_provider())
        width = self.BUTTON_WIDTH_PX / zoom
        height = self.BUTTON_HEIGHT_PX / zoom
        gap = self.BUTTON_GAP_PX / zoom
        frame = self.region_rect()
        return QRectF(frame.center().x() - width / 2.0, frame.bottom() + gap, width, height)

    def set_region(self, region: Rect) -> None:
        self.prepareGeometryChange()
        self.region = region
        self.update()

    def set_reference_preview(self, image: QImage | None, opacity: float) -> None:
        self._reference_image = None if image is None else image.copy()
        self._reference_opacity = min(max(float(opacity), 0.0), 1.0)
        self.update()

    def set_reference_opacity(self, opacity: float) -> None:
        self._reference_opacity = min(max(float(opacity), 0.0), 1.0)
        self.update()

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        _ = option, widget
        zoom = max(0.0625, self._zoom_provider())
        orange = QColor("#ff8a00")
        pen = QPen(orange)
        pen.setCosmetic(True)
        pen.setWidth(2)
        painter.save()
        if self._reference_image is not None:
            painter.save()
            painter.setOpacity(self._reference_opacity)
            painter.drawImage(self.region_rect(), self._reference_image)
            painter.restore()
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.region_rect())
        button = self.button_rect()
        painter.setBrush(QColor("#3a3a3a"))
        painter.drawRoundedRect(button, 3.0 / zoom, 3.0 / zoom)
        painter.setPen(QColor("#f0f0f0"))
        font = QFont(painter.font())
        font.setPixelSize(max(1, round(12.0 / zoom)))
        painter.setFont(font)
        painter.drawText(button, Qt.AlignmentFlag.AlignCenter, "确定")
        painter.restore()


class SpriteSliceTool:
    def __init__(
        self,
        region_sink: Callable[[Rect | None], None],
        confirm_sink: Callable[[Rect], None],
        cancel_sink: Callable[[], None],
        snap_provider: Callable[[], bool],
    ) -> None:
        self._region_sink = region_sink
        self._confirm_sink = confirm_sink
        self._cancel_sink = cancel_sink
        self._snap_provider = snap_provider
        self._context: ToolContext | None = None
        self._region: Rect | None = None
        self._overlay: SpriteSliceOverlayItem | None = None
        self._dragging = False
        self._press_point: QPointF | None = None
        self._gesture_region: Rect | None = None
        self._suppress_right_release = False

    @property
    def region(self) -> Rect | None:
        return self._region

    def activate(self, context: ToolContext) -> None:
        self._context = context
        document = context.document_provider()
        if document is None:
            return
        size = document.sprite_workspace.cell_size
        self._region = Rect(0, 0, size.width, size.height)
        self._overlay = SpriteSliceOverlayItem(self._region, lambda: context.canvas.zoom)
        context.canvas.zoom_changed.connect(self._zoom_changed)
        context.overlays.add(self._overlay)
        context.canvas.set_tool_cursor(Qt.CursorShape.SizeAllCursor)
        self._update_interaction_bounds()
        self._publish()

    def deactivate(self) -> None:
        if self._context is not None:
            try:
                self._context.canvas.zoom_changed.disconnect(self._zoom_changed)
            except RuntimeError:
                pass
            self._context.canvas.set_interaction_scene_bounds(None)
            self._context.canvas.set_tool_cursor(None)
        self._context = None
        self._region = None
        self._overlay = None
        self._dragging = False
        self._press_point = None
        self._gesture_region = None
        self._region_sink(None)

    def set_region(self, region: Rect) -> None:
        if self._context is None:
            return
        self._region = self._clamp(region.x, region.y)
        self._refresh_overlay()
        self._publish()

    def set_reference_preview(self, image: QImage | None, opacity: float) -> None:
        if self._overlay is not None:
            self._overlay.set_reference_preview(image, opacity)

    def set_reference_opacity(self, opacity: float) -> None:
        if self._overlay is not None:
            self._overlay.set_reference_opacity(opacity)

    def reset_for_cell_size(self) -> None:
        if self._context is None:
            return
        document = self._context.document_provider()
        if document is None:
            return
        current = self._region or Rect(0, 0, 1, 1)
        size = document.sprite_workspace.cell_size
        self._region = Rect(current.x, current.y, size.width, size.height)
        self._region = self._clamp(self._region.x, self._region.y)
        self._refresh_overlay()
        self._publish()

    def mouse_press(self, event: QMouseEvent) -> bool:
        if self._context is None or self._region is None or self._overlay is None:
            return False
        if event.button() == Qt.MouseButton.RightButton and self._dragging:
            self._restore_gesture()
            self._suppress_right_release = True
            return True
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        point = self._document_point(event)
        if self._overlay.button_rect().contains(point):
            self._confirm_sink(self._region)
            return True
        if not self._overlay.region_rect().contains(point):
            return False
        self._dragging = True
        self._press_point = point
        self._gesture_region = self._region
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        if (
            not self._dragging
            or self._press_point is None
            or self._gesture_region is None
        ):
            return False
        point = self._document_point(event)
        x = round(self._gesture_region.x + point.x() - self._press_point.x())
        y = round(self._gesture_region.y + point.y() - self._press_point.y())
        if self._snap_provider() and not bool(event.modifiers() & Qt.KeyboardModifier.AltModifier):
            x, y = self._snapped_position(x, y)
        self._region = self._clamp(x, y)
        self._refresh_overlay()
        self._publish()
        return True

    def mouse_release(self, event: QMouseEvent) -> bool:
        if event.button() == Qt.MouseButton.RightButton and self._suppress_right_release:
            self._suppress_right_release = False
            return True
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return False
        self._dragging = False
        self._press_point = None
        self._gesture_region = None
        return True

    def key_press(self, event: QKeyEvent) -> bool:
        if self._region is None:
            return False
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm_sink(self._region)
            return True
        if event.key() == Qt.Key.Key_Escape:
            if self._dragging:
                self._restore_gesture()
            else:
                self._cancel_sink()
            return True
        directions: dict[int, tuple[int, int]] = {
            Qt.Key.Key_Left.value: (-1, 0),
            Qt.Key.Key_Right.value: (1, 0),
            Qt.Key.Key_Up.value: (0, -1),
            Qt.Key.Key_Down.value: (0, 1),
        }
        delta = directions.get(event.key())
        if delta is None:
            return False
        document = self._context.document_provider() if self._context is not None else None
        if document is None:
            return False
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            delta = (
                delta[0] * document.sprite_workspace.cell_size.width,
                delta[1] * document.sprite_workspace.cell_size.height,
            )
        self._region = self._clamp(self._region.x + delta[0], self._region.y + delta[1])
        self._refresh_overlay()
        self._publish()
        return True

    def key_release(self, event: QKeyEvent) -> bool:
        _ = event
        return False

    def _document_point(self, event: QMouseEvent) -> QPointF:
        assert self._context is not None
        point = self._context.canvas.viewport_to_document(event.position().toPoint())
        return QPointF(point.x, point.y)

    def _clamp(self, x: int, y: int) -> Rect:
        assert self._context is not None and self._region is not None
        document = self._context.document_provider()
        if document is None:
            return self._region
        width = document.sprite_workspace.cell_size.width
        height = document.sprite_workspace.cell_size.height
        return Rect(
            min(max(0, int(x)), document.width - width),
            min(max(0, int(y)), document.height - height),
            width,
            height,
        )

    def _snapped_position(self, x: int, y: int) -> tuple[int, int]:
        assert self._context is not None
        document = self._context.document_provider()
        if document is None:
            return x, y
        workspace = document.sprite_workspace
        snapped_x = round(x / workspace.cell_size.width) * workspace.cell_size.width
        snapped_y = round(y / workspace.cell_size.height) * workspace.cell_size.height
        return snapped_x, snapped_y

    def _restore_gesture(self) -> None:
        if self._gesture_region is not None:
            self._region = self._gesture_region
            self._refresh_overlay()
            self._publish()
        self._dragging = False
        self._press_point = None
        self._gesture_region = None

    def _refresh_overlay(self) -> None:
        if self._overlay is not None and self._region is not None:
            self._overlay.set_region(self._region)
        self._update_interaction_bounds()

    def _update_interaction_bounds(self) -> None:
        if self._context is None or self._overlay is None:
            return
        self._context.canvas.set_interaction_scene_bounds(self._overlay.boundingRect())

    def _publish(self) -> None:
        self._region_sink(self._region)

    def _zoom_changed(self, _zoom: float) -> None:
        if self._overlay is not None:
            self._overlay.prepareGeometryChange()
            self._overlay.update()
        self._update_interaction_bounds()


__all__ = ["SpriteSliceOverlayItem", "SpriteSliceTool"]
