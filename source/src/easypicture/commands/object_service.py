"""Object command interface sharing the document's one undo stack."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from easypicture.domain import AffineTransform, DocumentNode

if TYPE_CHECKING:
    from easypicture.commands.service import DocumentCommandService


class ObjectCommandService:
    def __init__(self, commands: DocumentCommandService) -> None:
        self._commands = commands

    def create(
        self, node: DocumentNode, parent_id: UUID | None = None, index: int | None = None
    ) -> bool:
        return self._commands.create_object(node, parent_id, index)

    def delete(self, ids: tuple[UUID, ...]) -> bool:
        return self._commands.delete_nodes(ids)

    def duplicate(self, ids: tuple[UUID, ...]) -> bool:
        return self._commands.duplicate_nodes(ids)

    def move(self, ids: tuple[UUID, ...], parent_id: UUID | None, index: int) -> bool:
        return self._commands.move_nodes(ids, parent_id, index)

    def transform(self, ids: tuple[UUID, ...], delta: AffineTransform) -> bool:
        return self._commands.transform_objects(ids, delta)

    def update(self, ids: tuple[UUID, ...], **properties: object) -> bool:
        return self._commands.update_object_properties(ids, properties)
