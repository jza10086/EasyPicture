"""Copy-on-write document snapshots safe for persistence workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from easypicture.domain import Document, PixelPoint, RasterLayer, Selection, Size
from easypicture.pixels import MaskTileStore, TilePixelStore


@dataclass(frozen=True, slots=True)
class LayerSnapshot:
    id: UUID
    name: str
    size: Size
    origin: PixelPoint
    visible: bool
    opacity: float
    locked: bool
    alpha_locked: bool
    pixels: TilePixelStore

    @classmethod
    def capture(cls, layer: RasterLayer) -> LayerSnapshot:
        return cls(
            id=layer.id,
            name=layer.name,
            size=layer.size,
            origin=layer.origin,
            visible=layer.visible,
            opacity=layer.opacity,
            locked=layer.locked,
            alpha_locked=layer.alpha_locked,
            pixels=layer.pixels.clone_shared(),
        )

    def to_layer(self) -> RasterLayer:
        return RasterLayer(
            id=self.id,
            name=self.name,
            size=self.size,
            origin=self.origin,
            pixels=self.pixels.clone_shared(),
            visible=self.visible,
            opacity=self.opacity,
            locked=self.locked,
            alpha_locked=self.alpha_locked,
        )


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    document_id: UUID
    revision: int
    size: Size
    layers: tuple[LayerSnapshot, ...]
    active_layer_id: UUID
    selection: MaskTileStore
    project_path: Path | None
    source_name: str | None

    @classmethod
    def capture(cls, document: Document) -> DocumentSnapshot:
        return cls(
            document_id=document.id,
            revision=document.revision,
            size=document.size,
            layers=tuple(LayerSnapshot.capture(layer) for layer in document.layers),
            active_layer_id=document.active_layer_id,
            selection=document.selection.mask.clone_shared(),
            project_path=document.project_path,
            source_name=document.source_name,
        )

    def to_document(
        self,
        *,
        project_path: Path | None = None,
        modified: bool = False,
    ) -> Document:
        return Document.restore(
            document_id=self.document_id,
            size=self.size,
            layers=[layer.to_layer() for layer in self.layers],
            active_layer_id=self.active_layer_id,
            selection=Selection(size=self.size, mask=self.selection.clone_shared()),
            project_path=project_path,
            source_name=self.source_name,
            modified=modified,
        )
