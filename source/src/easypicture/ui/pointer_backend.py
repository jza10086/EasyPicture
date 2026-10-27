"""Pointer effects isolated from widget event/state logic.

Offscreen execution always uses the virtual implementation, including child
processes used by CLI tests. No test needs to move the user's system pointer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget


class PointerBackend(Protocol):
    def position(self) -> QPoint: ...
    def move(self, position: QPoint) -> None: ...
    def capture(self, widget: QWidget) -> None: ...
    def release(self, widget: QWidget) -> None: ...
    def override(self, shape: Qt.CursorShape) -> None: ...
    def restore(self) -> None: ...


class NativePointerBackend:
    @staticmethod
    def _require_native() -> None:
        if QGuiApplication.platformName() in {"offscreen", "minimal"}:
            raise RuntimeError("native pointer effects are forbidden in headless execution")

    def position(self) -> QPoint:
        self._require_native()
        return QCursor.pos()

    def move(self, position: QPoint) -> None:
        self._require_native()
        QCursor.setPos(position)

    def capture(self, widget: QWidget) -> None:
        self._require_native()
        widget.grabMouse()

    def release(self, widget: QWidget) -> None:
        self._require_native()
        if QWidget.mouseGrabber() is widget:
            widget.releaseMouse()

    def override(self, shape: Qt.CursorShape) -> None:
        self._require_native()
        QApplication.setOverrideCursor(QCursor(shape))

    def restore(self) -> None:
        self._require_native()
        QApplication.restoreOverrideCursor()


@dataclass
class VirtualPointerBackend:
    location: QPoint = field(default_factory=QPoint)
    captured: QWidget | None = None
    overrides: list[Qt.CursorShape] = field(default_factory=list)
    moves: list[QPoint] = field(default_factory=list)

    def position(self) -> QPoint:
        return QPoint(self.location)

    def move(self, position: QPoint) -> None:
        self.location = QPoint(position)
        self.moves.append(QPoint(position))

    def capture(self, widget: QWidget) -> None:
        self.captured = widget

    def release(self, widget: QWidget) -> None:
        if self.captured is widget:
            self.captured = None

    def override(self, shape: Qt.CursorShape) -> None:
        self.overrides.append(shape)

    def restore(self) -> None:
        if self.overrides:
            self.overrides.pop()


_native = NativePointerBackend()
_virtual = VirtualPointerBackend()
_configured: PointerBackend | None = None


def configure_pointer_backend(backend: PointerBackend | None) -> None:
    global _configured
    _configured = backend


def pointer_backend() -> PointerBackend:
    if _configured is not None:
        return _configured
    if QGuiApplication.platformName() in {"offscreen", "minimal"}:
        return _virtual
    return _native
