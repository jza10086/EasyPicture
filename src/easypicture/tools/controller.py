"""Tool switching and event dispatch."""

from __future__ import annotations

from PySide6.QtGui import QKeyEvent, QMouseEvent

from easypicture.tools.protocol import Tool, ToolContext


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
        self._active_tool.deactivate()
        self.context.overlays.clear()
        self.context.canvas.cancel_interaction()
        self._active_tool = tool
        self._active_tool.activate(self.context)

    def mouse_press(self, event: QMouseEvent) -> bool:
        return self._active_tool.mouse_press(event)

    def mouse_move(self, event: QMouseEvent) -> bool:
        return self._active_tool.mouse_move(event)

    def mouse_release(self, event: QMouseEvent) -> bool:
        return self._active_tool.mouse_release(event)

    def key_press(self, event: QKeyEvent) -> bool:
        return self._active_tool.key_press(event)

    def key_release(self, event: QKeyEvent) -> bool:
        return self._active_tool.key_release(event)


__all__ = ["ToolController"]
