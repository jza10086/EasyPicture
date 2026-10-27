"""A local-pixel editing view that writes through to one authoritative raster object."""

from __future__ import annotations

from collections.abc import Mapping
from copy import copy
from uuid import UUID, uuid4

from easypicture.domain.document import Document
from easypicture.domain.events import (
    DocumentEvent,
    DocumentRevisionChanged,
    DocumentSizeChanged,
    LayerPixelsChanged,
    LayerPropertiesChanged,
)
from easypicture.domain.geometry import AffineTransform, PixelPoint
from easypicture.domain.layer import RasterObject
from easypicture.domain.selection import Selection
from easypicture.domain.sprite import DocumentKind
from easypicture.pixels import RGBAArray, TileCoord


class RasterEditDocument(Document):
    """Transient projection, never serialized; project commands retain their usual undo stack."""

    def __init__(self, owner: Document, object_id: UUID) -> None:
        self.owner = owner
        self.object_id = object_id
        node = self._local_node()
        super().__init__(
            document_id=uuid4(),
            size=node.size,
            active_layer_id=node.id,
            selection=Selection.create_empty(node.size),
            nodes=[node],
            kind=DocumentKind.SPRITE_FRAME,
        )
        self._source_state = self._state()
        self._unsubscribe_owner = owner.subscribe(self._owner_changed)

    def _local_node(self) -> RasterObject:
        node = copy(self.owner.layer_by_id(self.object_id))
        node.pixels = node.pixels.clone_shared()
        node.origin = PixelPoint()
        node.transform = AffineTransform()
        node.transform_pivot = None
        node.opacity = 1.0
        node.visible = True
        node.locked = self.owner.is_locked(node.id)
        return node

    def _state(self) -> tuple[object, ...]:
        node = self.owner.layer_by_id(self.object_id)
        return (
            node.pixels.generation,
            node.size,
            node.name,
            self.owner.is_locked(node.id),
            node.alpha_locked,
            node.sampling,
        )

    def dispose(self) -> None:
        self._unsubscribe_owner()

    def _owner_changed(self, event: DocumentEvent) -> None:
        try:
            self.refresh(
                event.layer_tiles
                if isinstance(event, LayerPixelsChanged) and event.layer_id == self.object_id
                else None
            )
        except ValueError:
            # Deletion closes the tab. Keep this view alive for commands preceding the deletion.
            pass

    def refresh(self, tiles: frozenset[TileCoord] | None = None) -> None:
        state = self._state()
        if state == self._source_state:
            return
        previous = self.active_layer
        pixels_changed = state[:2] != self._source_state[:2]
        node = self._local_node()
        old_size = self.size
        changed = (
            tiles
            if tiles is not None
            else frozenset(
                coord
                for store in (previous.pixels, node.pixels)
                for coord, _tile in store.iter_allocated_tiles()
            )
        )
        self._source_state = state
        self.size = node.size
        if self.size != old_size:
            self.selection = Selection.create_empty(self.size)
        self._nodes = [node]
        self._root_group.children = self._nodes
        self._sync_node_index()
        revision = self._begin_content_change()
        if self.size != old_size:
            self._emit(DocumentSizeChanged(self.id, revision, old_size, self.size))
        if pixels_changed:
            self._emit(
                LayerPixelsChanged(self.id, revision, node.id, node.revision, changed, changed)
            )
        properties = frozenset(
            name
            for name in ("name", "locked", "alpha_locked", "sampling")
            if getattr(previous, name) != getattr(node, name)
        )
        if properties:
            self._emit(LayerPropertiesChanged(self.id, revision, node.id, properties))
        self._emit(DocumentRevisionChanged(self.id, revision))

    def prepare_pixel_updates(
        self, layer_id: UUID, updates: Mapping[TileCoord, RGBAArray | None]
    ) -> dict[TileCoord, RGBAArray | None]:
        if layer_id != self.object_id:
            raise ValueError("raster editor cannot write another object")
        self.refresh()
        return self.owner.prepare_pixel_updates(layer_id, updates)

    def replace_layer_tiles(
        self, layer_id: UUID, updates: Mapping[TileCoord, RGBAArray | None]
    ) -> frozenset[TileCoord]:
        if layer_id != self.object_id:
            raise ValueError("raster editor cannot write another object")
        changed = self.owner.replace_layer_tiles(layer_id, updates)
        self.refresh(changed)
        return changed
