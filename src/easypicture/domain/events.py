"""Immutable synchronous document events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from easypicture.domain.geometry import Size
from easypicture.domain.vector import VectorObjectKind
from easypicture.pixels import TileCoord


@dataclass(frozen=True, slots=True)
class DocumentModifiedChanged:
    document_id: UUID
    revision: int
    modified: bool


@dataclass(frozen=True, slots=True)
class ActiveLayerChanged:
    document_id: UUID
    revision: int
    layer_id: UUID


@dataclass(frozen=True, slots=True)
class DocumentRevisionChanged:
    document_id: UUID
    revision: int


@dataclass(frozen=True, slots=True)
class DocumentSizeChanged:
    document_id: UUID
    revision: int
    old_size: Size
    new_size: Size


@dataclass(frozen=True, slots=True)
class DocumentPathChanged:
    document_id: UUID
    revision: int
    project_path: str | None


@dataclass(frozen=True, slots=True)
class GroupListChanged:
    """Top-level group order or membership changed."""

    document_id: UUID
    revision: int
    group_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class GroupPropertiesChanged:
    document_id: UUID
    revision: int
    group_id: UUID
    properties: frozenset[str]


@dataclass(frozen=True, slots=True)
class GroupMembersChanged:
    document_id: UUID
    revision: int
    group_id: UUID
    child_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class VectorObjectChanged:
    document_id: UUID
    revision: int
    group_id: UUID
    object_id: UUID
    object_kind: VectorObjectKind
    properties: frozenset[str]


@dataclass(frozen=True, slots=True)
class LayerPropertiesChanged:
    document_id: UUID
    revision: int
    layer_id: UUID
    properties: frozenset[str]


@dataclass(frozen=True, slots=True)
class LayerPixelsChanged:
    document_id: UUID
    revision: int
    layer_id: UUID
    layer_revision: int
    layer_tiles: frozenset[TileCoord]
    document_tiles: frozenset[TileCoord]


@dataclass(frozen=True, slots=True)
class SelectionChanged:
    document_id: UUID
    revision: int
    selection_revision: int
    tiles: frozenset[TileCoord]
    active: bool


@dataclass(frozen=True, slots=True)
class SpriteWorkspaceChanged:
    document_id: UUID
    revision: int
    change: str
    frame_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class SpriteFrameContentChanged:
    document_id: UUID
    revision: int
    frame_id: UUID
    embedded_document_id: UUID


type DocumentEvent = (
    DocumentModifiedChanged
    | DocumentPathChanged
    | ActiveLayerChanged
    | DocumentRevisionChanged
    | DocumentSizeChanged
    | GroupListChanged
    | GroupPropertiesChanged
    | GroupMembersChanged
    | VectorObjectChanged
    | LayerPropertiesChanged
    | LayerPixelsChanged
    | SelectionChanged
    | SpriteWorkspaceChanged
    | SpriteFrameContentChanged
)


class DocumentEventListener(Protocol):
    def __call__(self, event: DocumentEvent) -> None: ...


__all__ = [
    "ActiveLayerChanged",
    "DocumentEvent",
    "DocumentEventListener",
    "DocumentModifiedChanged",
    "DocumentPathChanged",
    "DocumentRevisionChanged",
    "DocumentSizeChanged",
    "GroupListChanged",
    "GroupPropertiesChanged",
    "GroupMembersChanged",
    "LayerPixelsChanged",
    "LayerPropertiesChanged",
    "SelectionChanged",
    "SpriteWorkspaceChanged",
    "SpriteFrameContentChanged",
    "VectorObjectChanged",
]
