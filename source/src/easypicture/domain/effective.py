"""Shared recursive, pass-through render traversal for live and snapshot documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from easypicture.domain.document import Document
    from easypicture.domain.vector import DocumentNode
    from easypicture.persistence.snapshots import DocumentNodeSnapshot, DocumentSnapshot


@dataclass(frozen=True, slots=True)
class EffectiveLeaf:
    group_id: UUID | None
    leaf: DocumentNode | DocumentNodeSnapshot
    visible: bool
    opacity: float
    locked: bool


def iter_effective_leaves(source: Document | DocumentSnapshot) -> tuple[EffectiveLeaf, ...]:
    from easypicture.domain.vector import Collection
    from easypicture.persistence.snapshots import CollectionSnapshot

    result: list[EffectiveLeaf] = []

    def visit(
        node: DocumentNode | DocumentNodeSnapshot,
        parent: UUID | None,
        visible: bool,
        opacity: float,
        locked: bool,
    ) -> None:
        visible = visible and node.visible
        opacity *= node.opacity
        locked = locked or node.locked
        if isinstance(node, (Collection, CollectionSnapshot)):
            for child in node.children:
                visit(child, node.id, visible, opacity, locked)
        else:
            result.append(EffectiveLeaf(parent, node, visible, opacity, locked))

    for node in source.nodes:
        visit(node, None, True, 1.0, False)
    return tuple(result)
