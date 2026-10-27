"""Non-destructive working Tile session for one interactive pixel edit."""

from __future__ import annotations

from uuid import UUID

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document
from easypicture.domain.coordinates import RasterMapping
from easypicture.domain.layer import RasterSampling
from easypicture.pixels import MaskArray, RGBAArray, TileCoord
from easypicture.ui.canvas_view import CanvasView


class PixelEditSession:
    """Keep private Tile copies until one command commits the whole gesture."""

    def __init__(
        self,
        document: Document,
        layer_id: UUID,
        commands: DocumentCommandService,
        canvas: CanvasView,
    ) -> None:
        layer = document.layer_by_id(layer_id)
        if document.is_locked(layer.id):
            raise PermissionError("layer is locked")
        self.document = document
        self.document_id = document.id
        self.document_revision = document.revision
        self.layer_id = layer_id
        self.layer = layer
        self.commands = commands
        self.canvas = canvas
        self.width = layer.size.width
        self.height = layer.size.height
        self.mapping = RasterMapping(layer.world_transform, layer.size)
        self.sampling = layer.sampling
        self.origin_x = layer.origin.x
        self.origin_y = layer.origin.y
        self.document_width = document.width
        self.document_height = document.height
        self._tiles: dict[TileCoord, RGBAArray] = {}
        self._changed: set[TileCoord] = set()
        self._selection = document.selection.mask.clone_shared()
        self._selection_active = document.selection.is_active
        self._closed = False

    @property
    def changed_tiles(self) -> frozenset[TileCoord]:
        return frozenset(self._changed)

    @property
    def is_current(self) -> bool:
        return (
            not self._closed
            and self.document.id == self.document_id
            and self.document.revision == self.document_revision
            and self.document.active_layer_id == self.layer_id
        )

    def writable_tile(self, coord: TileCoord) -> RGBAArray:
        if self._closed:
            raise RuntimeError("pixel edit session is closed")
        tile = self._tiles.get(coord)
        if tile is None:
            tile = self.layer.pixels.read_tile(coord.x, coord.y).copy(order="C")
            self._tiles[coord] = tile
        return tile

    def selection_region(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> MaskArray | None:
        if not self._selection_active:
            return None
        return self.mapping.selection_coverage(
            self._selection,
            self.document.size,
            x - self.origin_x,
            y - self.origin_y,
            width,
            height,
            smooth=self.sampling is RasterSampling.SMOOTH,
        )

    def publish(self, coords: frozenset[TileCoord]) -> None:
        if self._closed or not coords:
            return
        self._changed.update(coords)
        self.canvas.set_pixel_preview(
            self.layer_id,
            {coord: self._tiles[coord] for coord in coords},
        )

    def commit(self, label: str) -> bool:
        if self._closed:
            return False
        if not self.is_current:
            self.cancel()
            return False
        updates = {coord: self._tiles[coord] for coord in self._changed}
        self.canvas.clear_pixel_preview()
        self._closed = True
        if not updates:
            return False
        return self.commands.apply_pixel_tiles(self.layer_id, updates, label)

    def cancel(self) -> None:
        if self._closed:
            return
        self.canvas.clear_pixel_preview()
        self._closed = True
        self._tiles.clear()
        self._changed.clear()


__all__ = ["PixelEditSession"]
