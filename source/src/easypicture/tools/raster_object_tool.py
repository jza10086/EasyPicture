"""Create bounded, initially transparent raster objects by drawing their rectangle."""

from __future__ import annotations

from math import ceil

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QGraphicsRectItem

from easypicture.domain import AffineTransform, RasterObject, Size
from easypicture.tools.insertion import object_insertion
from easypicture.tools.protocol import NullTool, ToolContext
from easypicture.ui.property_binding import interaction_coordinator


class RasterObjectTool(NullTool):
    def __init__(self) -> None:
        super().__init__()
        self._start: QPointF | None = None
        self._rect: QGraphicsRectItem | None = None
        self._document_id: object = None
        self._revision = -1
        self._cancelled_buttons: set[Qt.MouseButton] = set()
        self._escape = False

    def activate(self, context: ToolContext) -> None:
        super().activate(context)
        context.canvas.set_tool_cursor(Qt.CursorShape.CrossCursor)

    def cancel(self) -> None:
        if self._context:
            self._context.overlays.clear()
            interaction_coordinator(self._context.canvas).release(self)
        self._start = None
        self._rect = None

    def deactivate(self) -> None:
        self.cancel()
        if self._context:
            self._context.canvas.set_tool_cursor(None)
        super().deactivate()

    def mouse_press(self, event: QMouseEvent) -> bool:
        context = self._context
        if context is None:
            return False
        if event.button() == Qt.MouseButton.RightButton and self._start is not None:
            self.cancel()
            self._cancelled_buttons.update((Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton))
            return True
        document = context.document_provider()
        if event.button() != Qt.MouseButton.LeftButton or document is None:
            return False
        self._cancelled_buttons.discard(Qt.MouseButton.LeftButton)
        if not interaction_coordinator(context.canvas).acquire(self, self.cancel):
            return True
        point = context.canvas.viewport_to_document(event.position().toPoint())
        self._start = QPointF(point.x, point.y)
        self._document_id, self._revision = document.id, document.revision
        self._rect = QGraphicsRectItem()
        pen = context.canvas.highlight_pen()
        self._rect.setPen(pen)
        context.overlays.add(self._rect)
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        if self._context is None or self._start is None or self._rect is None:
            return False
        point = self._context.canvas.viewport_to_document(event.position().toPoint())
        self._rect.setRect(QRectF(self._start, QPointF(point.x, point.y)).normalized())
        return True

    def mouse_release(self, event: QMouseEvent) -> bool:
        if event.button() in self._cancelled_buttons:
            self._cancelled_buttons.discard(event.button())
            return True
        context = self._context
        if context is None or self._rect is None or event.button() != Qt.MouseButton.LeftButton:
            return False
        self.mouse_move(event)
        rect = self._rect.rect()
        self.cancel()
        document = context.document_provider()
        if (
            document is None
            or document.id != self._document_id
            or document.revision != self._revision
        ):
            return True
        if rect.width() < 1 or rect.height() < 1 or max(rect.width(), rect.height()) > 8192:
            if context.status:
                context.status("栅格区域的宽高必须在 1～8192 像素之间")
            return True
        node = RasterObject.create_blank("栅格对象", Size(ceil(rect.width()), ceil(rect.height())))
        node.transform = AffineTransform.translation(rect.x(), rect.y())
        if context.initialize_raster is not None:
            node = context.initialize_raster(node)
        selected = context.selected_node_provider() if context.selected_node_provider else None
        parent, index = object_insertion(document, selected)
        if context.commands and context.commands.create_object(node, parent, index):
            if context.select_node:
                context.select_node(document.node_by_id(node.id))
            if context.object_created is not None:
                context.object_created(node)
        elif context.status:
            context.status("无法在锁定的集合中添加栅格对象")
        return True

    def key_press(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Escape and self._start is not None:
            self.cancel()
            self._cancelled_buttons.add(Qt.MouseButton.LeftButton)
            self._escape = True
            return True
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Escape and self._escape:
            self._escape = False
            return True
        return False
