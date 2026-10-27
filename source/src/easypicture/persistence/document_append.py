"""Prepare external document content for atomic append into another document."""

from __future__ import annotations

from dataclasses import dataclass

from easypicture.domain import (
    Collection,
    Document,
    DocumentNode,
    walk_nodes,
)


@dataclass(frozen=True, slots=True)
class DocumentAppendPayload:
    groups: tuple[DocumentNode, ...]
    warnings: tuple[str, ...] = ()


def prepare_document_append(
    source: Document,
    target: Document,
    *,
    include_empty_raster_groups: bool = True,
) -> DocumentAppendPayload:
    """Clone source groups with new identities and target-canvas raster geometry."""
    used_names = {node.name for node in walk_nodes(target.nodes)}
    groups: list[DocumentNode] = []
    for original in source.nodes:
        if (
            not include_empty_raster_groups
            and isinstance(original, Collection)
            and not original.children
        ):
            continue
        duplicate = original.duplicate()
        for original_node, copied in zip(
            walk_nodes((original,)), walk_nodes((duplicate,)), strict=True
        ):
            copied.name = _unique_name(original_node.name, used_names)
            used_names.add(copied.name)
        groups.append(duplicate)
    warnings = ("来源工程的精灵帧定义未追加。",) if source.sprite_workspace.frames else ()
    return DocumentAppendPayload(tuple(groups), warnings)


def _unique_name(requested: str, existing: set[str]) -> str:
    normalized = requested.strip()
    if normalized not in existing:
        return normalized
    suffix = 1
    while f"{normalized}.{suffix}" in existing:
        suffix += 1
    return f"{normalized}.{suffix}"


__all__ = ["DocumentAppendPayload", "prepare_document_append"]
