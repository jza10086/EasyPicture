"""Flatten the strict two-level tree for render and export consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from easypicture.domain.document import Document
    from easypicture.persistence.snapshots import DocumentSnapshot


@dataclass(frozen=True, slots=True)
class EffectiveLeaf:
    """A leaf with group visibility, opacity and lock state already applied."""

    group_id: UUID
    leaf: object
    visible: bool
    opacity: float
    locked: bool


def iter_effective_leaves(
    source: Document | DocumentSnapshot,
) -> tuple[EffectiveLeaf, ...]:
    """Return leaves in bottom-to-top order for either live or snapshot data."""
    result: list[EffectiveLeaf] = []
    for group in source.nodes:
        group_visible = bool(group.visible)
        group_opacity = float(group.opacity)
        group_locked = bool(group.locked)
        for leaf in group.children:
            result.append(
                EffectiveLeaf(
                    group_id=group.id,
                    leaf=leaf,
                    visible=group_visible and bool(leaf.visible),
                    opacity=max(0.0, min(1.0, group_opacity * float(leaf.opacity))),
                    locked=group_locked or bool(leaf.locked),
                )
            )
    return tuple(result)


__all__ = ["EffectiveLeaf", "iter_effective_leaves"]
