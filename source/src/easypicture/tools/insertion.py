"""One insertion rule for shape, text and raster creation."""

from __future__ import annotations

from uuid import UUID

from easypicture.domain import Collection, Document


def object_insertion(document: Document, selected: object) -> tuple[UUID | None, int]:
    key = getattr(selected, "id", None)
    if isinstance(key, UUID):
        try:
            node = document.node_by_id(key)
        except ValueError:
            pass
        else:
            if isinstance(node, Collection):
                return node.id, 0
            return document.node_location(node.id)
    return None, 0
