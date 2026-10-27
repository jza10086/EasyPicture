"""Tool switching and event dispatch."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from easypicture.tools.protocol import Tool, ToolContext
from easypicture.ui.property_binding import interaction_coordinator


class ToolController:
    def __init__(self, context: ToolContext, initial_tool: Tool) -> None:
        self.context = context
        self._active_tool = initial_tool
        self._active_tool.activate(context)

    @property
    def active_tool(self) -> Tool:
        return self._active_tool

    def set_tool(self, tool: Tool) -> None:
        if tool is self._active_tool:
            return
        interaction_coordinator(self.context.canvas).release(self._active_tool)
        self._active_tool.deactivate()
        self.context.overlays.clear()
        self.context.canvas.cancel_interaction()
        self._active_tool = tool
        self._active_tool.activate(self.context)

    def reset_active_tool(self) -> None:
        interaction_coordinator(self.context.canvas).release(self._active_tool)
        self._active_tool.deactivate()
        self.context.overlays.clear()
        self.context.canvas.cancel_interaction()
        self._active_tool.activate(self.context)

    def mouse_press(self, event: QMouseEvent) -> bool:
        coordinator = interaction_coordinator(self.context.canvas)
        if coordinator.owner is not None and coordinator.owner is not self._active_tool:
            return True
        handled = self._active_tool.mouse_press(event)
        if handled and event.button() == Qt.MouseButton.LeftButton:
            coordinator.acquire(self._active_tool, self.reset_active_tool)
        return handled

    def mouse_move(self, event: QMouseEvent) -> bool:
        return self._active_tool.mouse_move(event)

    def mouse_release(self, event: QMouseEvent) -> bool:
        tool = self._active_tool
        interaction_coordinator(self.context.canvas).release(tool)
        return tool.mouse_release(event)

    def key_press(self, event: QKeyEvent) -> bool:
        handled = self._active_tool.key_press(event)
        if handled and event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            interaction_coordinator(self.context.canvas).release(self._active_tool)
        return handled

    def key_release(self, event: QKeyEvent) -> bool:
        return self._active_tool.key_release(event)


__all__ = ["ToolController"]
