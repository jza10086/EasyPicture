"""Workspace selection, independent of document content and undo history."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from PySide6.QtCore import QObject, Signal

from easypicture.domain import Collection, Document, RasterObject


class EditMode(StrEnum):
    OBJECT = "object"
    PIXEL = "pixel"
    VECTOR = "vector"


@dataclass(frozen=True, slots=True)
class SelectionState:
    selected_ids: tuple[UUID, ...] = ()
    active_id: UUID | None = None
    content_target_id: UUID | None = None
    mode: EditMode = EditMode.OBJECT


class SelectionCoordinator(QObject):
    changed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.document: Document | None = None
        self.state = SelectionState()
        self._states: dict[tuple[UUID | None, UUID], SelectionState] = {}
        self._key: tuple[UUID | None, UUID] | None = None

    def bind(self, document: Document | None, workspace_id: UUID | None = None) -> None:
        if self._key is not None:
            self._states[self._key] = self.state
        self.document = document
        self._key = (workspace_id, document.id) if document else None
        self.state = (
            self._states.get(self._key, SelectionState()) if self._key else SelectionState()
        )
        if document and not self.state.selected_ids and document.active_layer_id:
            self.state = SelectionState((document.active_layer_id,), document.active_layer_id)
        self.prune(emit=True)

    def forget(self, document_id: UUID) -> None:
        for key in tuple(self._states):
            if key[1] == document_id:
                self._states.pop(key)

    def retain_workspaces(self, ids: set[UUID]) -> None:
        """Drop selection records of closed projects, including detached frames."""
        for key in tuple(self._states):
            if key[0] is not None and key[0] not in ids:
                self._states.pop(key)
        if self._key and self._key[0] is not None and self._key[0] not in ids:
            self._key = None

    def select(self, ids: tuple[UUID, ...], active: UUID | None = None) -> None:
        document = self.document
        available = {}
        if document:
            for key in dict.fromkeys(ids):
                try:
                    available[key] = document.node_by_id(key)
                except ValueError:
                    pass
        selected = tuple(dict.fromkeys(key for key in ids if key in available))
        active = active if active in selected else (selected[-1] if selected else None)
        node = available.get(active) if active else None
        target = (
            active
            if node is not None
            and not isinstance(node, Collection)
            and (
                self.state.mode == EditMode.PIXEL
                and isinstance(node, RasterObject)
                or self.state.mode == EditMode.VECTOR
                and not isinstance(node, RasterObject)
            )
            else None
        )
        state = SelectionState(selected, active, target, self.state.mode)
        if state != self.state:
            self.state = state
            self.changed.emit(state)

    def set_mode(self, mode: EditMode) -> None:
        if mode == self.state.mode:
            return
        self.state = SelectionState(
            self.state.selected_ids,
            self.state.active_id,
            None,
            mode,
        )
        before = self.state
        self.select(self.state.selected_ids, self.state.active_id)
        if self.state == before:
            self.changed.emit(self.state)

    def prune(self, *, emit: bool = False) -> None:
        before = self.state
        self.select(before.selected_ids, before.active_id)
        if emit and self.state == before:
            self.changed.emit(self.state)
