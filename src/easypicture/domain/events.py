"""Immutable synchronous document events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

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
class LayerListChanged:
    document_id: UUID
    revision: int
    layer_ids: tuple[UUID, ...]


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


type DocumentEvent = (
    DocumentModifiedChanged
    | ActiveLayerChanged
    | DocumentRevisionChanged
    | LayerListChanged
    | LayerPropertiesChanged
    | LayerPixelsChanged
)


class DocumentEventListener(Protocol):
    def __call__(self, event: DocumentEvent) -> None: ...


__all__ = [
    "ActiveLayerChanged",
    "DocumentEvent",
    "DocumentEventListener",
    "DocumentModifiedChanged",
    "DocumentRevisionChanged",
    "LayerListChanged",
    "LayerPixelsChanged",
    "LayerPropertiesChanged",
]
