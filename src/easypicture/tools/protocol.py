"""Tool protocol and P1 no-op implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from PySide6.QtGui import QKeyEvent, QMouseEvent

from easypicture.domain import Document

if TYPE_CHECKING:
    from easypicture.commands import DocumentCommandService
    from easypicture.imaging import RGBAColor
    from easypicture.jobs import JobRunner
    from easypicture.ui.canvas_view import CanvasView
    from easypicture.ui.overlays import OverlayManager


@dataclass(frozen=True, slots=True)
class ToolContext:
    canvas: CanvasView
    overlays: OverlayManager
    document_provider: Callable[[], Document | None]
    commands: DocumentCommandService | None = None
    foreground_provider: Callable[[], RGBAColor] | None = None
    set_foreground: Callable[[RGBAColor], None] | None = None
    set_background: Callable[[RGBAColor], None] | None = None
    jobs: JobRunner | None = None
    set_busy: Callable[[bool], None] | None = None
    status: Callable[[str], None] | None = None


class Tool(Protocol):
    def activate(self, context: ToolContext) -> None: ...

    def deactivate(self) -> None: ...

    def mouse_press(self, event: QMouseEvent) -> bool: ...

    def mouse_move(self, event: QMouseEvent) -> bool: ...

    def mouse_release(self, event: QMouseEvent) -> bool: ...

    def key_press(self, event: QKeyEvent) -> bool: ...

    def key_release(self, event: QKeyEvent) -> bool: ...


class NullTool:
    def __init__(self) -> None:
        self._context: ToolContext | None = None

    def activate(self, context: ToolContext) -> None:
        self._context = context

    def deactivate(self) -> None:
        self._context = None

    def mouse_press(self, event: QMouseEvent) -> bool:
        _ = event
        return False

    def mouse_move(self, event: QMouseEvent) -> bool:
        _ = event
        return False

    def mouse_release(self, event: QMouseEvent) -> bool:
        _ = event
        return False

    def key_press(self, event: QKeyEvent) -> bool:
        _ = event
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        _ = event
        return False


__all__ = ["NullTool", "Tool", "ToolContext"]
