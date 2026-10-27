"""Ownership boundary for temporary tool overlays."""

from __future__ import annotations

from PySide6.QtWidgets import QGraphicsItem, QGraphicsItemGroup, QGraphicsScene


class OverlayManager:
    def __init__(self, scene: QGraphicsScene) -> None:
        self._scene = scene
        self._group = QGraphicsItemGroup()
        self._group.setZValue(10_000.0)
        self._scene.addItem(self._group)

    @property
    def count(self) -> int:
        return len(self._group.childItems())

    def add(self, item: QGraphicsItem) -> None:
        self._group.addToGroup(item)

    def clear(self) -> None:
        for item in tuple(self._group.childItems()):
            self._group.removeFromGroup(item)
            if item.scene() is self._scene:
                self._scene.removeItem(item)


__all__ = ["OverlayManager"]
