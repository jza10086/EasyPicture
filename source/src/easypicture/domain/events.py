"""Immutable synchronous document events."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from easypicture.domain.geometry import FloatRect, Size
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
    layer_id: UUID | None


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


class ChangeCategory(StrEnum):
    CONTENT = "content"
    STYLE = "style"
    TRANSFORM = "transform"
    STRUCTURE = "structure"
    METADATA = "metadata"
    SELECTION = "selection"


@dataclass(frozen=True, slots=True)
class DocumentVersions:
    content: int = 0
    style: int = 0
    transform: int = 0
    structure: int = 0
    metadata: int = 0
    selection: int = 0

    def advance(self, categories: frozenset[ChangeCategory]) -> DocumentVersions:
        return DocumentVersions(
            **{
                category.value: getattr(self, category.value) + int(category in categories)
                for category in ChangeCategory
            }
        )

    @property
    def rendering(self) -> tuple[int, int, int, int]:
        return self.content, self.style, self.transform, self.structure


@dataclass(frozen=True, slots=True)
class ObjectBoundsChange:
    object_id: UUID
    old: FloatRect | None
    new: FloatRect | None


def event_categories(event: DocumentEvent) -> frozenset[ChangeCategory]:
    if isinstance(event, (GroupListChanged, GroupMembersChanged)):
        return frozenset({ChangeCategory.STRUCTURE})
    if isinstance(event, LayerPixelsChanged):
        return frozenset(
            {ChangeCategory.CONTENT if event.layer_tiles else ChangeCategory.TRANSFORM}
        )
    if isinstance(event, SpriteFrameContentChanged):
        return frozenset({ChangeCategory.CONTENT})
    if isinstance(event, DocumentSizeChanged):
        return frozenset({ChangeCategory.TRANSFORM})
    if isinstance(event, SelectionChanged):
        return frozenset({ChangeCategory.SELECTION})
    if isinstance(event, SpriteWorkspaceChanged):
        return frozenset({ChangeCategory.METADATA})
    result: set[ChangeCategory] = set()
    if isinstance(event, (GroupPropertiesChanged, LayerPropertiesChanged, VectorObjectChanged)):
        for name in event.properties:
            if name in {"name", "locked", "alpha_locked"}:
                result.add(ChangeCategory.METADATA)
            elif name in {"created", "deleted", "group", "order"}:
                result.add(ChangeCategory.STRUCTURE)
            elif name in {"transform", "transform_pivot", "frame", "origin", "size"}:
                result.add(ChangeCategory.TRANSFORM)
            elif name in {"text", "svg", "element_kind", "intrinsic_bounds"}:
                result.add(ChangeCategory.CONTENT)
            else:
                result.add(ChangeCategory.STYLE)
    return frozenset(result)


def affects_rendered_content(event: DocumentEvent) -> bool:
    """Metadata and selection never invalidate authoritative image consumers."""
    if isinstance(
        event,
        (DocumentSizeChanged, LayerPixelsChanged, GroupListChanged, GroupMembersChanged),
    ):
        return True
    if isinstance(event, (GroupPropertiesChanged, LayerPropertiesChanged, VectorObjectChanged)):
        return bool(event.properties - {"name", "locked", "alpha_locked"})
    return False


@dataclass(frozen=True, slots=True)
class DocumentChangeSet:
    """All content events emitted by one completed document revision."""

    document_id: UUID
    revision: int
    events: tuple[DocumentEvent, ...]
    versions: DocumentVersions = DocumentVersions()
    bounds_changes: tuple[ObjectBoundsChange, ...] = ()

    @property
    def categories(self) -> frozenset[ChangeCategory]:
        if not self.events:
            return frozenset({ChangeCategory.CONTENT})
        return frozenset(category for event in self.events for category in event_categories(event))

    @property
    def object_properties(self) -> dict[UUID, frozenset[str]]:
        result: dict[UUID, frozenset[str]] = {}
        for event in self.events:
            if isinstance(event, LayerPropertiesChanged):
                object_id = event.layer_id
            elif isinstance(event, VectorObjectChanged):
                object_id = event.object_id
            elif isinstance(event, GroupPropertiesChanged):
                object_id = event.group_id
            else:
                continue
            result[object_id] = result.get(object_id, frozenset()) | event.properties
        return result

    @property
    def layer_tiles(self) -> dict[UUID, frozenset[TileCoord]]:
        result: dict[UUID, frozenset[TileCoord]] = {}
        for event in self.events:
            if isinstance(event, LayerPixelsChanged):
                result[event.layer_id] = result.get(event.layer_id, frozenset()) | event.layer_tiles
        return result

    @property
    def structural_operations(self) -> tuple[GroupListChanged | GroupMembersChanged, ...]:
        return tuple(
            event
            for event in self.events
            if isinstance(event, (GroupListChanged, GroupMembersChanged))
        )

    @property
    def affects_rendering(self) -> bool:
        return not self.events or any(affects_rendered_content(event) for event in self.events)

    @property
    def object_ids(self) -> frozenset[UUID]:
        result: set[UUID] = set()
        for event in self.events:
            for name in ("layer_id", "object_id", "group_id"):
                value = getattr(event, name, None)
                if isinstance(value, UUID):
                    result.add(value)
        return frozenset(result)

    @property
    def document_tiles(self) -> frozenset[TileCoord]:
        return frozenset(
            tile
            for event in self.events
            if isinstance(event, LayerPixelsChanged)
            for tile in event.document_tiles
        )


__all__ = [
    "ActiveLayerChanged",
    "DocumentEvent",
    "DocumentChangeSet",
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
    "affects_rendered_content",
]
