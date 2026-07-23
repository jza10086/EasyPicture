"""Immutable synchronous document events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


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


type DocumentEvent = DocumentModifiedChanged | ActiveLayerChanged | DocumentRevisionChanged


class DocumentEventListener(Protocol):
    def __call__(self, event: DocumentEvent) -> None: ...


__all__ = [
    "ActiveLayerChanged",
    "DocumentEvent",
    "DocumentEventListener",
    "DocumentModifiedChanged",
    "DocumentRevisionChanged",
]
