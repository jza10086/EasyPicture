"""Single-document aggregate and lifecycle events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np

from easypicture.domain.events import (
    ActiveLayerChanged,
    DocumentEvent,
    DocumentEventListener,
    DocumentModifiedChanged,
    DocumentPathChanged,
    DocumentRevisionChanged,
    DocumentSizeChanged,
    LayerListChanged,
    LayerPixelsChanged,
    LayerPropertiesChanged,
    SelectionChanged,
)
from easypicture.domain.geometry import PixelPoint, Size
from easypicture.domain.layer import RasterLayer
from easypicture.domain.selection import Selection
from easypicture.pixels import (
    DEFAULT_TILE_SIZE,
    MaskArray,
    Rect,
    RGBAArray,
    TileCoord,
    TilePixelStore,
)
from easypicture.pixels.types import validate_mask_array, validate_rgba_array

MAX_DOCUMENT_DIMENSION = 8192
MAX_DOCUMENT_LAYERS = 256
type Unsubscribe = Callable[[], None]


class Document:
    """Qt-free aggregate for one EasyPicture document."""

    def __init__(
        self,
        *,
        document_id: UUID,
        size: Size,
        layers: list[RasterLayer],
        active_layer_id: UUID,
        selection: Selection,
        project_path: Path | None = None,
        source_name: str | None = None,
        modified: bool = False,
    ) -> None:
        if not layers:
            raise ValueError("document must contain at least one layer")
        if len(layers) > MAX_DOCUMENT_LAYERS:
            raise ValueError(f"document cannot contain more than {MAX_DOCUMENT_LAYERS} layers")
        if not 1 <= size.width <= MAX_DOCUMENT_DIMENSION:
            raise ValueError("document width is out of range")
        if not 1 <= size.height <= MAX_DOCUMENT_DIMENSION:
            raise ValueError("document height is out of range")
        if selection.size != size:
            raise ValueError("selection size must match the document")
        layer_ids = {layer.id for layer in layers}
        if len(layer_ids) != len(layers):
            raise ValueError("layer ids must be unique")
        if all(layer.id != active_layer_id for layer in layers):
            raise ValueError("active layer must belong to the document")
        self.id = document_id
        self.size = size
        self._layers = [self._normalize_layer(layer, size) for layer in layers]
        self._active_layer_id = active_layer_id
        self.selection = selection
        self._project_path = Path(project_path) if project_path is not None else None
        self._source_name = Path(source_name).name if source_name else None
        self._modified = modified
        self._revision = 0
        self._listeners: dict[int, DocumentEventListener] = {}
        self._next_listener_token = 0

    @classmethod
    def create(cls, width: int, height: int) -> Document:
        if not 1 <= width <= MAX_DOCUMENT_DIMENSION:
            raise ValueError(f"document width must be between 1 and {MAX_DOCUMENT_DIMENSION}")
        if not 1 <= height <= MAX_DOCUMENT_DIMENSION:
            raise ValueError(f"document height must be between 1 and {MAX_DOCUMENT_DIMENSION}")
        size = Size(width, height)
        layer = RasterLayer.create_blank("图层 1", size)
        return cls(
            document_id=uuid4(),
            size=size,
            layers=[layer],
            active_layer_id=layer.id,
            selection=Selection.create_empty(size),
        )

    @classmethod
    def restore(
        cls,
        *,
        document_id: UUID,
        size: Size,
        layers: list[RasterLayer],
        active_layer_id: UUID,
        selection: Selection,
        project_path: Path | None = None,
        source_name: str | None = None,
        modified: bool = False,
    ) -> Document:
        return cls(
            document_id=document_id,
            size=size,
            layers=layers,
            active_layer_id=active_layer_id,
            selection=selection,
            project_path=project_path,
            source_name=source_name,
            modified=modified,
        )

    @property
    def width(self) -> int:
        return self.size.width

    @property
    def height(self) -> int:
        return self.size.height

    @property
    def layers(self) -> tuple[RasterLayer, ...]:
        return tuple(self._layers)

    @property
    def active_layer_id(self) -> UUID:
        return self._active_layer_id

    @property
    def active_layer(self) -> RasterLayer:
        return self.layer_by_id(self._active_layer_id)

    @property
    def modified(self) -> bool:
        return self._modified

    @property
    def project_path(self) -> Path | None:
        return self._project_path

    @property
    def source_name(self) -> str | None:
        return self._source_name

    @property
    def display_name(self) -> str:
        if self._project_path is not None:
            return self._project_path.name
        return self._source_name or "Untitled"

    @property
    def revision(self) -> int:
        return self._revision

    def subscribe(self, listener: DocumentEventListener) -> Unsubscribe:
        token = self._next_listener_token
        self._next_listener_token += 1
        self._listeners[token] = listener

        def unsubscribe() -> None:
            self._listeners.pop(token, None)

        return unsubscribe

    def record_change(self) -> None:
        revision = self._begin_content_change()
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))

    def layer_by_id(self, layer_id: UUID) -> RasterLayer:
        for layer in self._layers:
            if layer.id == layer_id:
                return layer
        raise ValueError("layer must belong to the document")

    def layer_index(self, layer_id: UUID) -> int:
        for index, layer in enumerate(self._layers):
            if layer.id == layer_id:
                return index
        raise ValueError("layer must belong to the document")

    def insert_layer(
        self,
        layer: RasterLayer,
        index: int,
        *,
        activate: bool = True,
    ) -> None:
        if len(self._layers) >= MAX_DOCUMENT_LAYERS:
            raise ValueError(f"document cannot contain more than {MAX_DOCUMENT_LAYERS} layers")
        if any(existing.id == layer.id for existing in self._layers):
            raise ValueError("layer id already belongs to the document")
        if not 0 <= index <= len(self._layers):
            raise IndexError("layer insertion index is out of range")
        old_active_id = self._active_layer_id
        layer = self._normalize_layer(layer, self.size)
        self._layers.insert(index, layer)
        if activate:
            self._active_layer_id = layer.id
        revision = self._begin_content_change()
        self._emit_layer_list_changed(revision)
        if self._active_layer_id != old_active_id:
            self._emit_active_layer_changed(revision)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))

    def remove_layer(self, layer_id: UUID) -> tuple[RasterLayer, int]:
        if len(self._layers) == 1:
            raise ValueError("document must retain at least one layer")
        index = self.layer_index(layer_id)
        old_active_id = self._active_layer_id
        layer = self._layers.pop(index)
        if layer_id == old_active_id:
            fallback_index = index - 1 if index > 0 else 0
            self._active_layer_id = self._layers[fallback_index].id
        revision = self._begin_content_change()
        self._emit_layer_list_changed(revision)
        if self._active_layer_id != old_active_id:
            self._emit_active_layer_changed(revision)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return layer, index

    def move_layer(self, layer_id: UUID, target_index: int) -> bool:
        if not 0 <= target_index < len(self._layers):
            raise IndexError("target layer index is out of range")
        source_index = self.layer_index(layer_id)
        if source_index == target_index:
            return False
        layer = self._layers.pop(source_index)
        self._layers.insert(target_index, layer)
        revision = self._begin_content_change()
        self._emit_layer_list_changed(revision)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def set_layer_name(self, layer_id: UUID, name: str) -> bool:
        normalized = name.strip()
        if not normalized:
            raise ValueError("layer name must not be empty")
        return self._set_layer_property(layer_id, "name", normalized)

    def set_layer_visible(self, layer_id: UUID, visible: bool) -> bool:
        return self._set_layer_property(layer_id, "visible", visible)

    def set_layer_opacity(self, layer_id: UUID, opacity: float) -> bool:
        if not 0.0 <= opacity <= 1.0:
            raise ValueError("layer opacity must be between 0 and 1")
        return self._set_layer_property(layer_id, "opacity", opacity)

    def set_layer_locked(self, layer_id: UUID, locked: bool) -> bool:
        return self._set_layer_property(layer_id, "locked", locked)

    def set_layer_alpha_locked(self, layer_id: UUID, locked: bool) -> bool:
        return self._set_layer_property(layer_id, "alpha_locked", locked)

    def prepare_pixel_updates(
        self,
        layer_id: UUID,
        updates: Mapping[TileCoord, RGBAArray | None],
    ) -> dict[TileCoord, RGBAArray | None]:
        """Validate and normalize proposed full-Tile writes without mutating."""
        layer = self.layer_by_id(layer_id)
        if layer.locked:
            raise PermissionError("layer is locked")
        tile_size = layer.pixels.tile_size
        max_tx = (layer.size.width - 1) // tile_size
        max_ty = (layer.size.height - 1) // tile_size
        normalized: dict[TileCoord, RGBAArray | None] = {}
        for coord, proposed in updates.items():
            if coord.x > max_tx or coord.y > max_ty:
                raise ValueError("pixel Tile lies outside the layer")
            before = layer.pixels.read_tile(coord.x, coord.y)
            if proposed is None:
                candidate = np.zeros((tile_size, tile_size, 4), dtype=np.uint8)
            else:
                validate_rgba_array(
                    proposed,
                    expected_width=tile_size,
                    expected_height=tile_size,
                )
                candidate = proposed.copy(order="C")
            if layer.alpha_locked:
                transparent = before[..., 3] == 0
                candidate[..., 3] = before[..., 3]
                candidate[transparent] = before[transparent]

            valid_width = min(tile_size, layer.size.width - coord.x * tile_size)
            valid_height = min(tile_size, layer.size.height - coord.y * tile_size)
            if valid_width < tile_size:
                candidate[:, valid_width:] = 0
            if valid_height < tile_size:
                candidate[valid_height:, :] = 0

            if np.array_equal(before, candidate):
                continue
            normalized[coord] = candidate if np.any(candidate) else None
        return normalized

    def replace_layer_tiles(
        self,
        layer_id: UUID,
        updates: Mapping[TileCoord, RGBAArray | None],
    ) -> frozenset[TileCoord]:
        """Apply already-normalized Tile replacements and emit one content change."""
        layer = self.layer_by_id(layer_id)
        changed: set[TileCoord] = set()
        for coord, pixels in updates.items():
            if layer.pixels.replace_tile(coord.x, coord.y, pixels):
                changed.add(coord)
        if not changed:
            return frozenset()

        layer.record_pixel_change()
        document_tiles = self._document_tiles_for_layer_tiles(layer, changed)
        revision = self._begin_content_change()
        self._emit(
            LayerPixelsChanged(
                document_id=self.id,
                revision=revision,
                layer_id=layer.id,
                layer_revision=layer.revision,
                layer_tiles=frozenset(changed),
                document_tiles=document_tiles,
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return frozenset(changed)

    def prepare_selection_updates(
        self,
        updates: Mapping[TileCoord, MaskArray | None],
    ) -> dict[TileCoord, MaskArray | None]:
        """Validate proposed full-Tile selection writes without mutating."""
        tile_size = self.selection.mask.tile_size
        max_tx = (self.width - 1) // tile_size
        max_ty = (self.height - 1) // tile_size
        normalized: dict[TileCoord, MaskArray | None] = {}
        for coord, proposed in updates.items():
            if coord.x > max_tx or coord.y > max_ty:
                raise ValueError("selection Tile lies outside the document")
            before = self.selection.mask.read_tile(coord.x, coord.y)
            if proposed is None:
                candidate = np.zeros((tile_size, tile_size), dtype=np.uint8)
            else:
                validate_mask_array(
                    proposed,
                    expected_width=tile_size,
                    expected_height=tile_size,
                )
                candidate = proposed.copy(order="C")

            valid_width = min(tile_size, self.width - coord.x * tile_size)
            valid_height = min(tile_size, self.height - coord.y * tile_size)
            if valid_width < tile_size:
                candidate[:, valid_width:] = 0
            if valid_height < tile_size:
                candidate[valid_height:, :] = 0
            if np.array_equal(before, candidate):
                continue
            normalized[coord] = candidate if np.any(candidate) else None
        return normalized

    def replace_selection_tiles(
        self,
        updates: Mapping[TileCoord, MaskArray | None],
    ) -> frozenset[TileCoord]:
        """Apply normalized selection Tile replacements as one content change."""
        changed: set[TileCoord] = set()
        for coord, pixels in updates.items():
            if self.selection.mask.replace_tile(coord.x, coord.y, pixels):
                changed.add(coord)
        if not changed:
            return frozenset()

        revision = self._begin_content_change()
        self._emit(
            SelectionChanged(
                document_id=self.id,
                revision=revision,
                selection_revision=self.selection.revision,
                tiles=frozenset(changed),
                active=self.selection.is_active,
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return frozenset(changed)

    def apply_raster_transform(
        self,
        layer_id: UUID,
        *,
        size: Size,
        origin: PixelPoint,
        layer_updates: Mapping[TileCoord, RGBAArray | None],
        selection_updates: Mapping[TileCoord, MaskArray | None],
    ) -> bool:
        """Atomically apply transformed geometry, pixels and selection patches."""
        if size != self.size or origin != PixelPoint():
            raise ValueError("raster layers must match the canvas geometry")
        layer = self.layer_by_id(layer_id)
        old_size = layer.size
        old_origin = layer.origin
        geometry_changed = old_size != size or old_origin != origin

        pixel_changes: dict[TileCoord, RGBAArray | None] = {}
        max_tx = (size.width - 1) // DEFAULT_TILE_SIZE
        max_ty = (size.height - 1) // DEFAULT_TILE_SIZE
        for coord, pixels in layer_updates.items():
            if coord.x > max_tx or coord.y > max_ty:
                if pixels is not None:
                    raise ValueError("transformed pixel Tile lies outside the layer")
            elif pixels is not None:
                validate_rgba_array(
                    pixels,
                    expected_width=DEFAULT_TILE_SIZE,
                    expected_height=DEFAULT_TILE_SIZE,
                )
            before = (
                layer.pixels.read_tile(coord.x, coord.y)
                if layer.pixels.has_tile(coord.x, coord.y)
                else None
            )
            if before is None and pixels is None:
                continue
            if before is not None and pixels is not None and np.array_equal(before, pixels):
                continue
            pixel_changes[coord] = pixels

        selection_changes = self.prepare_selection_updates(selection_updates)
        if not geometry_changed and not pixel_changes and not selection_changes:
            return False

        old_document_tiles = self._document_tiles_for_layer_bounds(old_origin, old_size)
        layer.size = size
        layer.origin = origin
        for coord, pixels in pixel_changes.items():
            layer.pixels.replace_tile(coord.x, coord.y, pixels)
        if pixel_changes or geometry_changed:
            layer.record_pixel_change()
        for coord, pixels in selection_changes.items():
            self.selection.mask.replace_tile(coord.x, coord.y, pixels)

        revision = self._begin_content_change()
        if geometry_changed:
            properties = {
                name
                for name, before, after in (
                    ("size", old_size, size),
                    ("origin", old_origin, origin),
                )
                if before != after
            }
            self._emit(
                LayerPropertiesChanged(
                    document_id=self.id,
                    revision=revision,
                    layer_id=layer_id,
                    properties=frozenset(properties),
                )
            )
        if pixel_changes or geometry_changed:
            new_document_tiles = self._document_tiles_for_layer_bounds(origin, size)
            self._emit(
                LayerPixelsChanged(
                    document_id=self.id,
                    revision=revision,
                    layer_id=layer_id,
                    layer_revision=layer.revision,
                    layer_tiles=frozenset(pixel_changes),
                    document_tiles=old_document_tiles | new_document_tiles,
                )
            )
        if selection_changes:
            self._emit(
                SelectionChanged(
                    document_id=self.id,
                    revision=revision,
                    selection_revision=self.selection.revision,
                    tiles=frozenset(selection_changes),
                    active=self.selection.is_active,
                )
            )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def apply_geometry_transaction(
        self,
        *,
        document_size: Size,
        layer_geometry: Mapping[UUID, tuple[Size, PixelPoint]],
        layer_updates: Mapping[UUID, Mapping[TileCoord, RGBAArray | None]],
        selection_updates: Mapping[TileCoord, MaskArray | None],
    ) -> bool:
        """Atomically apply a document size and multi-layer geometry transaction."""
        if (
            document_size.width > MAX_DOCUMENT_DIMENSION
            or document_size.height > MAX_DOCUMENT_DIMENSION
        ):
            raise ValueError("document geometry exceeds the project limit")
        resolved_layers: dict[UUID, RasterLayer] = {}
        pixel_changes: dict[UUID, dict[TileCoord, RGBAArray | None]] = {}
        geometry_changed: dict[UUID, tuple[Size, PixelPoint, Size, PixelPoint]] = {}
        for layer_id, (size, origin) in layer_geometry.items():
            layer = self.layer_by_id(layer_id)
            resolved_layers[layer_id] = layer
            if size != document_size or origin != PixelPoint():
                raise ValueError("raster layers must match the canvas geometry")
            if layer.size != size or layer.origin != origin:
                geometry_changed[layer_id] = (
                    layer.size,
                    layer.origin,
                    size,
                    origin,
                )
            changes: dict[TileCoord, RGBAArray | None] = {}
            max_tx = (size.width - 1) // DEFAULT_TILE_SIZE
            max_ty = (size.height - 1) // DEFAULT_TILE_SIZE
            for coord, pixels in layer_updates.get(layer_id, {}).items():
                if coord.x > max_tx or coord.y > max_ty:
                    if pixels is not None:
                        raise ValueError("geometry pixel Tile lies outside the layer")
                elif pixels is not None:
                    validate_rgba_array(
                        pixels,
                        expected_width=DEFAULT_TILE_SIZE,
                        expected_height=DEFAULT_TILE_SIZE,
                    )
                before = (
                    layer.pixels.read_tile(coord.x, coord.y)
                    if layer.pixels.has_tile(coord.x, coord.y)
                    else None
                )
                if before is None and pixels is None:
                    continue
                if (
                    before is not None
                    and pixels is not None
                    and np.array_equal(before, pixels)
                ):
                    continue
                changes[coord] = pixels
            if changes:
                pixel_changes[layer_id] = changes

        selection_changes: dict[TileCoord, MaskArray | None] = {}
        max_selection_tx = (document_size.width - 1) // DEFAULT_TILE_SIZE
        max_selection_ty = (document_size.height - 1) // DEFAULT_TILE_SIZE
        for coord, pixels in selection_updates.items():
            if coord.x > max_selection_tx or coord.y > max_selection_ty:
                if pixels is not None:
                    raise ValueError("geometry selection Tile lies outside the document")
            elif pixels is not None:
                validate_mask_array(
                    pixels,
                    expected_width=DEFAULT_TILE_SIZE,
                    expected_height=DEFAULT_TILE_SIZE,
                )
            before = (
                self.selection.mask.read_tile(coord.x, coord.y)
                if self.selection.mask.has_tile(coord.x, coord.y)
                else None
            )
            if before is None and pixels is None:
                continue
            if (
                before is not None
                and pixels is not None
                and np.array_equal(before, pixels)
            ):
                continue
            selection_changes[coord] = pixels

        size_changed = self.size != document_size
        if (
            not size_changed
            and not geometry_changed
            and not pixel_changes
            and not selection_changes
        ):
            return False

        old_document_size = self.size
        old_bounds = {
            layer_id: self._document_tiles_for_layer_bounds(layer.origin, layer.size)
            for layer_id, layer in resolved_layers.items()
        }
        self.size = document_size
        self.selection.size = document_size
        for layer_id, layer in resolved_layers.items():
            state = layer_geometry[layer_id]
            layer.size, layer.origin = state
            for coord, pixels in pixel_changes.get(layer_id, {}).items():
                layer.pixels.replace_tile(coord.x, coord.y, pixels)
            if layer_id in geometry_changed or layer_id in pixel_changes:
                layer.record_pixel_change()
        for coord, pixels in selection_changes.items():
            self.selection.mask.replace_tile(coord.x, coord.y, pixels)

        revision = self._begin_content_change()
        if size_changed:
            self._emit(
                DocumentSizeChanged(
                    document_id=self.id,
                    revision=revision,
                    old_size=old_document_size,
                    new_size=document_size,
                )
            )
        for layer_id, layer in resolved_layers.items():
            if layer_id in geometry_changed:
                before_size, before_origin, after_size, after_origin = geometry_changed[
                    layer_id
                ]
                properties = {
                    name
                    for name, before, after in (
                        ("size", before_size, after_size),
                        ("origin", before_origin, after_origin),
                    )
                    if before != after
                }
                self._emit(
                    LayerPropertiesChanged(
                        document_id=self.id,
                        revision=revision,
                        layer_id=layer_id,
                        properties=frozenset(properties),
                    )
                )
            if layer_id in geometry_changed or layer_id in pixel_changes:
                self._emit(
                    LayerPixelsChanged(
                        document_id=self.id,
                        revision=revision,
                        layer_id=layer_id,
                        layer_revision=layer.revision,
                        layer_tiles=frozenset(pixel_changes.get(layer_id, {})),
                        document_tiles=old_bounds[layer_id]
                        | self._document_tiles_for_layer_bounds(
                            layer.origin,
                            layer.size,
                        ),
                    )
                )
        if size_changed or selection_changes:
            self._emit(
                SelectionChanged(
                    document_id=self.id,
                    revision=revision,
                    selection_revision=self.selection.revision,
                    tiles=frozenset(selection_changes),
                    active=self.selection.is_active,
                )
            )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    @staticmethod
    def _normalize_layer(layer: RasterLayer, canvas_size: Size) -> RasterLayer:
        if layer.size == canvas_size and layer.origin == PixelPoint():
            return layer
        target = TilePixelStore(DEFAULT_TILE_SIZE)
        for coord, tile in layer.pixels.iter_allocated_tiles():
            local_left = coord.x * DEFAULT_TILE_SIZE
            local_top = coord.y * DEFAULT_TILE_SIZE
            local_right = min(layer.size.width, local_left + DEFAULT_TILE_SIZE)
            local_bottom = min(layer.size.height, local_top + DEFAULT_TILE_SIZE)
            document_left = layer.origin.x + local_left
            document_top = layer.origin.y + local_top
            document_right = layer.origin.x + local_right
            document_bottom = layer.origin.y + local_bottom
            clipped_left = max(0, document_left)
            clipped_top = max(0, document_top)
            clipped_right = min(canvas_size.width, document_right)
            clipped_bottom = min(canvas_size.height, document_bottom)
            if clipped_right <= clipped_left or clipped_bottom <= clipped_top:
                continue
            source_left = clipped_left - document_left
            source_top = clipped_top - document_top
            portion = np.ascontiguousarray(
                tile[
                    source_top : source_top + clipped_bottom - clipped_top,
                    source_left : source_left + clipped_right - clipped_left,
                ]
            )
            if np.any(portion):
                target.write_region(
                    Rect(
                        clipped_left,
                        clipped_top,
                        clipped_right - clipped_left,
                        clipped_bottom - clipped_top,
                    ),
                    portion,
                )
        layer.size = canvas_size
        layer.origin = PixelPoint()
        layer.pixels = target
        return layer

    def _begin_content_change(self) -> int:
        self._revision += 1
        if not self._modified:
            self._modified = True
            self._emit(
                DocumentModifiedChanged(
                    document_id=self.id,
                    revision=self._revision,
                    modified=True,
                )
            )
        return self._revision

    def mark_clean(self) -> None:
        if not self._modified:
            return
        self._modified = False
        self._emit(
            DocumentModifiedChanged(
                document_id=self.id,
                revision=self._revision,
                modified=False,
            )
        )

    def set_project_path(self, path: Path | None) -> None:
        normalized = Path(path) if path is not None else None
        if normalized == self._project_path:
            return
        self._project_path = normalized
        self._emit(
            DocumentPathChanged(
                document_id=self.id,
                revision=self._revision,
                project_path=str(normalized) if normalized is not None else None,
            )
        )

    def set_active_layer(self, layer_id: UUID) -> None:
        if layer_id == self._active_layer_id:
            return
        self.layer_by_id(layer_id)
        self._active_layer_id = layer_id
        self._revision += 1
        self._emit_active_layer_changed(self._revision)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=self._revision))

    def _set_layer_property(self, layer_id: UUID, property_name: str, value: object) -> bool:
        layer = self.layer_by_id(layer_id)
        if getattr(layer, property_name) == value:
            return False
        setattr(layer, property_name, value)
        revision = self._begin_content_change()
        self._emit(
            LayerPropertiesChanged(
                document_id=self.id,
                revision=revision,
                layer_id=layer_id,
                properties=frozenset({property_name}),
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def _document_tiles_for_layer_tiles(
        self,
        layer: RasterLayer,
        layer_tiles: set[TileCoord],
    ) -> frozenset[TileCoord]:
        tile_size = layer.pixels.tile_size
        result: set[TileCoord] = set()
        for coord in layer_tiles:
            left = max(0, layer.origin.x + coord.x * tile_size)
            top = max(0, layer.origin.y + coord.y * tile_size)
            right = min(
                self.width,
                layer.origin.x + min(layer.size.width, (coord.x + 1) * tile_size),
            )
            bottom = min(
                self.height,
                layer.origin.y + min(layer.size.height, (coord.y + 1) * tile_size),
            )
            if right <= left or bottom <= top:
                continue
            first_tx = left // tile_size
            first_ty = top // tile_size
            last_tx = (right - 1) // tile_size
            last_ty = (bottom - 1) // tile_size
            for ty in range(first_ty, last_ty + 1):
                for tx in range(first_tx, last_tx + 1):
                    result.add(TileCoord(tx, ty))
        return frozenset(result)

    def _document_tiles_for_layer_bounds(
        self,
        origin: PixelPoint,
        size: Size,
    ) -> frozenset[TileCoord]:
        left = max(0, origin.x)
        top = max(0, origin.y)
        right = min(self.width, origin.x + size.width)
        bottom = min(self.height, origin.y + size.height)
        if right <= left or bottom <= top:
            return frozenset()
        return frozenset(
            TileCoord(tx, ty)
            for ty in range(top // DEFAULT_TILE_SIZE, (bottom - 1) // DEFAULT_TILE_SIZE + 1)
            for tx in range(left // DEFAULT_TILE_SIZE, (right - 1) // DEFAULT_TILE_SIZE + 1)
        )

    def _emit_layer_list_changed(self, revision: int) -> None:
        self._emit(
            LayerListChanged(
                document_id=self.id,
                revision=revision,
                layer_ids=tuple(layer.id for layer in self._layers),
            )
        )

    def _emit_active_layer_changed(self, revision: int) -> None:
        self._emit(
            ActiveLayerChanged(
                document_id=self.id,
                revision=revision,
                layer_id=self._active_layer_id,
            )
        )

    def _emit(self, event: DocumentEvent) -> None:
        for listener in tuple(self._listeners.values()):
            listener(event)


__all__ = [
    "Document",
    "MAX_DOCUMENT_DIMENSION",
    "MAX_DOCUMENT_LAYERS",
    "Unsubscribe",
]
