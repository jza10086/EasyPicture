"""Single-document aggregate and lifecycle events."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import fields, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np

from easypicture.domain.events import (
    ActiveLayerChanged,
    ChangeCategory,
    DocumentChangeSet,
    DocumentEvent,
    DocumentEventListener,
    DocumentModifiedChanged,
    DocumentPathChanged,
    DocumentRevisionChanged,
    DocumentSizeChanged,
    DocumentVersions,
    GroupListChanged,
    GroupMembersChanged,
    GroupPropertiesChanged,
    LayerPixelsChanged,
    LayerPropertiesChanged,
    ObjectBoundsChange,
    SelectionChanged,
    SpriteFrameContentChanged,
    SpriteWorkspaceChanged,
    VectorObjectChanged,
    event_categories,
)
from easypicture.domain.geometry import AffineTransform, FloatRect, PixelPoint, Point, Size
from easypicture.domain.layer import RasterObject
from easypicture.domain.selection import Selection
from easypicture.domain.spatial import ObjectBoundsIndex, bounds_description, object_bounds
from easypicture.domain.sprite import (
    DocumentKind,
    SpriteFrameContentMode,
    SpriteWorkspace,
)
from easypicture.domain.vector import (
    Collection,
    DocumentNode,
    LayerGroup,
    SvgObject,
    TextObject,
    VectorObject,
    VectorObjectKind,
    walk_nodes,
)
from easypicture.pixels import (
    DEFAULT_TILE_SIZE,
    MaskArray,
    RGBAArray,
    TileCoord,
)
from easypicture.pixels.types import validate_mask_array, validate_rgba_array

MAX_DOCUMENT_DIMENSION = 8192
MAX_DOCUMENT_LAYERS = 256
type Unsubscribe = Callable[[], None]


def _vector_object_kind(vector_object: VectorObject) -> VectorObjectKind:
    return (
        VectorObjectKind.SVG_TEXT
        if isinstance(vector_object, TextObject)
        else VectorObjectKind.SVG_ELEMENT
    )


class Document:
    """Qt-free aggregate for one EasyPicture document."""

    kind: DocumentKind
    size: Size
    _maximum_dimension = MAX_DOCUMENT_DIMENSION

    def __init__(
        self,
        *,
        document_id: UUID,
        size: Size,
        active_layer_id: UUID | None,
        selection: Selection,
        nodes: list[DocumentNode],
        sprite_workspace: SpriteWorkspace | None = None,
        kind: DocumentKind = DocumentKind.PROJECT,
        embedded_documents: Mapping[UUID, Document] | None = None,
        project_path: Path | None = None,
        source_name: str | None = None,
        modified: bool = False,
    ) -> None:
        root_nodes = list(nodes)
        all_nodes = walk_nodes(root_nodes)
        if not 1 <= size.width <= self._maximum_dimension:
            raise ValueError("document width is out of range")
        if not 1 <= size.height <= self._maximum_dimension:
            raise ValueError("document height is out of range")
        if selection.size != size:
            raise ValueError("selection size must match the document")
        if kind is DocumentKind.PROJECT:
            resolved_sprite_workspace = (
                SpriteWorkspace.create(size)
                if sprite_workspace is None
                else sprite_workspace.clone()
            )
            resolved_sprite_workspace.validate_for_canvas(size)
        else:
            if sprite_workspace is not None:
                raise ValueError("embedded frame document cannot contain a sprite workspace")
            if project_path is not None:
                raise ValueError("embedded frame document cannot have a project path")
            resolved_sprite_workspace = None
        resolved_embedded_documents = dict(embedded_documents or {})
        if kind is DocumentKind.SPRITE_FRAME and resolved_embedded_documents:
            raise ValueError("embedded frame document cannot contain embedded documents")
        if kind is DocumentKind.PROJECT:
            assert resolved_sprite_workspace is not None
            detached_ids = {
                frame.embedded_document_id
                for frame in resolved_sprite_workspace.frames
                if frame.content_mode is SpriteFrameContentMode.DETACHED
            }
            if detached_ids != set(resolved_embedded_documents):
                raise ValueError("detached sprite frames and embedded documents must match")
            for embedded in resolved_embedded_documents.values():
                if embedded.kind is not DocumentKind.SPRITE_FRAME:
                    raise ValueError("project embedded documents must be sprite frames")
                if embedded.size != resolved_sprite_workspace.cell_size:
                    raise ValueError("embedded frame size must match sprite cell size")
        for node in all_nodes:
            if (
                isinstance(node, RasterObject)
                and max(node.size.width, node.size.height) > self._maximum_dimension
            ):
                raise ValueError("raster local size exceeds the project limit")
        raster_layers = [node for node in all_nodes if isinstance(node, RasterObject)]
        if len(raster_layers) > MAX_DOCUMENT_LAYERS:
            raise ValueError(
                f"document cannot contain more than {MAX_DOCUMENT_LAYERS} raster layers"
            )
        if active_layer_id is not None and all(
            layer.id != active_layer_id for layer in raster_layers
        ):
            raise ValueError("active layer must belong to the document")
        self.id = document_id
        self.kind = kind
        self.size = size
        self._nodes = root_nodes
        self._root_group = Collection(self.id, "Document", self._nodes)
        self._active_layer_id = active_layer_id
        self.selection = selection
        self._sprite_workspace = resolved_sprite_workspace
        self._embedded_documents = resolved_embedded_documents
        self._embedded_unsubscribes: dict[UUID, Unsubscribe] = {}
        self._project_path = Path(project_path) if project_path is not None else None
        self._source_name = Path(source_name).name if source_name else None
        self._modified = modified
        self._revision = 0
        self._listeners: dict[int, DocumentEventListener] = {}
        self._next_listener_token = 0
        self._change_listeners: dict[int, Callable[[DocumentChangeSet], None]] = {}
        self._pending_change_events: list[DocumentEvent] = []
        self._versions = DocumentVersions()
        self.spatial_index = ObjectBoundsIndex()
        self._node_index: dict[UUID, LayerGroup | RasterObject | VectorObject] = {}
        self._parent_index: dict[UUID, LayerGroup] = {}
        self._pending_bounds: dict[UUID, ObjectBoundsChange] = {}
        self._bounds_descriptions: dict[UUID, tuple[object, ...]] = {}
        self._sync_node_index()
        self._pending_bounds.clear()
        self._subscribe_embedded_documents()

    @classmethod
    def create(cls, width: int, height: int) -> Document:
        if not 1 <= width <= MAX_DOCUMENT_DIMENSION:
            raise ValueError(f"document width must be between 1 and {MAX_DOCUMENT_DIMENSION}")
        if not 1 <= height <= MAX_DOCUMENT_DIMENSION:
            raise ValueError(f"document height must be between 1 and {MAX_DOCUMENT_DIMENSION}")
        size = Size(width, height)
        layer = RasterObject.create_blank("图层 1", size)
        group = Collection.create("集合")
        group.children.append(layer)
        return cls(
            document_id=uuid4(),
            size=size,
            active_layer_id=layer.id,
            selection=Selection.create_empty(size),
            nodes=[group],
            sprite_workspace=SpriteWorkspace.create(size),
        )

    @classmethod
    def create_empty(cls, width: int, height: int) -> Document:
        size = Size(width, height)
        return cls(
            document_id=uuid4(),
            size=size,
            active_layer_id=None,
            selection=Selection.create_empty(size),
            nodes=[],
        )

    def require_raster_target(self) -> UUID:
        if self._active_layer_id is None:
            raise ValueError("no editable raster object is selected")
        return self._active_layer_id

    @classmethod
    def create_sprite_frame(cls, size: Size) -> Document:
        layer = RasterObject.create_blank("帧内容", size)
        group = Collection.create("集合")
        group.children.append(layer)
        return cls(
            document_id=uuid4(),
            size=size,
            active_layer_id=layer.id,
            selection=Selection.create_empty(size),
            nodes=[group],
            kind=DocumentKind.SPRITE_FRAME,
        )

    @classmethod
    def restore(
        cls,
        *,
        document_id: UUID,
        size: Size,
        active_layer_id: UUID | None,
        selection: Selection,
        nodes: list[DocumentNode],
        sprite_workspace: SpriteWorkspace | None = None,
        kind: DocumentKind = DocumentKind.PROJECT,
        embedded_documents: Mapping[UUID, Document] | None = None,
        project_path: Path | None = None,
        source_name: str | None = None,
        modified: bool = False,
    ) -> Document:
        return cls(
            document_id=document_id,
            size=size,
            active_layer_id=active_layer_id,
            selection=selection,
            nodes=nodes,
            sprite_workspace=sprite_workspace,
            kind=kind,
            embedded_documents=embedded_documents,
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
    def layers(self) -> tuple[RasterObject, ...]:
        return self._layers

    @property
    def _layers(self) -> tuple[RasterObject, ...]:
        # This is a query over the node tree, never a separately mutable list.
        return tuple(node for node in walk_nodes(self._nodes) if isinstance(node, RasterObject))

    @property
    def nodes(self) -> tuple[DocumentNode, ...]:
        return tuple(self._nodes)

    @property
    def collections(self) -> tuple[Collection, ...]:
        return tuple(node for node in self._node_index.values() if isinstance(node, Collection))

    @property
    def vector_groups(self) -> tuple[Collection, ...]:
        candidates = (*self.collections, self._root_group)
        return tuple(
            group
            for group in candidates
            if (group is not self._root_group and not group.children)
            or any(isinstance(child, (TextObject, SvgObject)) for child in group.children)
        )

    def parent_of(self, node_id: UUID) -> Collection | None:
        self.node_by_id(node_id)
        return self._parent_index.get(node_id)

    def is_locked(self, node_id: UUID) -> bool:
        node = self.node_by_id(node_id)
        if node.locked:
            return True
        parent = self._parent_index.get(node_id)
        return parent is not None and self.is_locked(parent.id)

    @property
    def active_layer_id(self) -> UUID | None:
        return self._active_layer_id

    @property
    def active_layer(self) -> RasterObject:
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

    @property
    def versions(self) -> DocumentVersions:
        return self._versions

    @property
    def sprite_workspace(self) -> SpriteWorkspace:
        if self._sprite_workspace is None:
            raise ValueError("embedded frame document has no sprite workspace")
        return self._sprite_workspace

    @property
    def embedded_documents(self) -> Mapping[UUID, Document]:
        return dict(self._embedded_documents)

    def embedded_document_by_id(self, document_id: UUID) -> Document:
        try:
            return self._embedded_documents[document_id]
        except KeyError as error:
            raise ValueError("embedded document does not belong to the project") from error

    def embedded_document_for_frame(self, frame_id: UUID) -> Document:
        frame = self.sprite_workspace.frame_by_id(frame_id)
        if frame.embedded_document_id is None:
            raise ValueError("sprite frame is not detached")
        return self.embedded_document_by_id(frame.embedded_document_id)

    def _subscribe_embedded_documents(self) -> None:
        for unsubscribe in self._embedded_unsubscribes.values():
            unsubscribe()
        self._embedded_unsubscribes.clear()
        if self.kind is not DocumentKind.PROJECT:
            return
        for embedded_id, embedded in self._embedded_documents.items():
            self._embedded_unsubscribes[embedded_id] = embedded.subscribe(
                lambda event, document_id=embedded_id: self._on_embedded_event(
                    document_id,
                    event,
                )
            )

    def _on_embedded_event(self, embedded_id: UUID, event: DocumentEvent) -> None:
        if not isinstance(event, DocumentRevisionChanged):
            return
        frame = next(
            (
                item
                for item in self.sprite_workspace.frames
                if item.embedded_document_id == embedded_id
            ),
            None,
        )
        if frame is None:
            return
        revision = self._begin_content_change()
        self._emit(
            SpriteFrameContentChanged(
                document_id=self.id,
                revision=revision,
                frame_id=frame.id,
                embedded_document_id=embedded_id,
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))

    def subscribe(self, listener: DocumentEventListener) -> Unsubscribe:
        token = self._next_listener_token
        self._next_listener_token += 1
        self._listeners[token] = listener

        def unsubscribe() -> None:
            self._listeners.pop(token, None)

        return unsubscribe

    def subscribe_changes(
        self,
        listener: Callable[[DocumentChangeSet], None],
    ) -> Unsubscribe:
        """Subscribe to completed content revisions, not individual notifications."""
        token = self._next_listener_token
        self._next_listener_token += 1
        self._change_listeners[token] = listener

        def unsubscribe() -> None:
            self._change_listeners.pop(token, None)

        return unsubscribe

    def record_change(self) -> None:
        self._sync_node_index()
        revision = self._begin_content_change()
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))

    def layer_by_id(self, layer_id: UUID | None) -> RasterObject:
        layer = self._node_index.get(layer_id) if layer_id is not None else None
        if isinstance(layer, RasterObject):
            return layer
        raise ValueError("layer must belong to the document")

    def node_by_id(self, node_id: UUID) -> LayerGroup | RasterObject | VectorObject:
        try:
            return self._node_index[node_id]
        except KeyError as error:
            raise ValueError("node must belong to the document") from error

    def vector_group_by_id(self, group_id: UUID) -> Collection:
        node = self._root_group if group_id == self.id else self.node_by_id(group_id)
        if not isinstance(node, Collection):
            raise ValueError("node is not a vector group")
        return node

    def layer_group_by_id(self, group_id: UUID) -> LayerGroup:
        node = self._root_group if group_id == self.id else self.node_by_id(group_id)
        if not isinstance(node, Collection):
            raise ValueError("node is not a layer group")
        return node

    def raster_group_for_layer(self, layer_id: UUID | None) -> Collection:
        group = (
            self._parent_index.get(layer_id, self._root_group)
            if layer_id is not None
            else self._root_group
        )
        if isinstance(group, Collection):
            return group
        raise ValueError("raster layer must belong to a raster group")

    def text_object_by_id(self, object_id: UUID) -> tuple[Collection, TextObject]:
        node = self.node_by_id(object_id)
        if not isinstance(node, TextObject):
            raise ValueError("node is not a text object")
        group = self._parent_index.get(object_id, self._root_group)
        if isinstance(group, Collection):
            return group, node
        raise ValueError("text object must belong to the document")

    def vector_object_by_id(self, object_id: UUID) -> tuple[Collection, VectorObject]:
        node = self.node_by_id(object_id)
        if not isinstance(node, (TextObject, SvgObject)):
            raise ValueError("node is not a vector object")
        group = self._parent_index.get(object_id, self._root_group)
        if isinstance(group, Collection):
            return group, node
        raise ValueError("vector object must belong to the document")

    def node_location(self, node_id: UUID) -> tuple[UUID | None, int]:
        node = self.node_by_id(node_id)
        parent = self.parent_of(node_id)
        siblings = self._nodes if parent is None else parent.children
        return (None if parent is None else parent.id, siblings.index(node))

    def validate_node_move(self, node_id: UUID, parent_id: UUID | None, index: int) -> None:
        node = self.node_by_id(node_id)
        if self.is_locked(node_id) or (parent_id is not None and self.is_locked(parent_id)):
            raise PermissionError("object or ancestor collection is locked")
        parent = self._root_group if parent_id is None else self.layer_group_by_id(parent_id)
        if any(descendant.id == parent.id for descendant in walk_nodes((node,))):
            raise ValueError("a collection cannot be moved into itself or its descendants")
        old_parent = self._parent_index.get(node_id, self._root_group)
        limit = len(parent.children) - int(parent is old_parent)
        if not 0 <= index <= limit:
            raise IndexError("node destination index is out of range")

    def move_node(self, node_id: UUID, parent_id: UUID | None, index: int) -> bool:
        """Move any node; index is its final bottom-to-top sibling position."""
        self.validate_node_move(node_id, parent_id, index)
        node = self.node_by_id(node_id)
        old_parent = self._parent_index.get(node_id, self._root_group)
        parent = self._root_group if parent_id is None else self.layer_group_by_id(parent_id)
        if old_parent is parent and old_parent.children.index(node) == index:
            return False
        old_parent.children.remove(node)
        parent.children.insert(index, node)
        old_parent.record_change()
        if parent is not old_parent:
            parent.record_change()
        self._sync_node_index()
        revision = self._begin_content_change()
        self._emit_group_members_changed(revision, old_parent)
        if parent is not old_parent:
            self._emit_group_members_changed(revision, parent)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def layer_index(self, layer_id: UUID | None) -> int:
        for index, layer in enumerate(self._layers):
            if layer.id == layer_id:
                return index
        raise ValueError("layer must belong to the document")

    def insert_layer(
        self,
        layer: RasterObject,
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
        if max(layer.size.width, layer.size.height) > MAX_DOCUMENT_DIMENSION:
            raise ValueError("raster local size exceeds the project limit")
        raster_groups = [node for node in self._nodes if isinstance(node, Collection)]
        if not raster_groups:
            group = Collection.create("集合")
            self._nodes.append(group)
        else:
            group = self.raster_group_for_layer(self._active_layer_id)
        if index == len(self._layers):
            child_index = len(group.children)
        else:
            target = self._layers[index]
            target_group = next(
                candidate
                for candidate in self.collections
                if isinstance(candidate, Collection) and target in candidate.children
            )
            if target_group is group:
                child_index = group.children.index(target)
            else:
                child_index = len(group.children)
        group.children.insert(child_index, layer)
        group.record_change()
        self._sync_node_index()
        if activate:
            self._active_layer_id = layer.id
        revision = self._begin_content_change()
        self._emit_group_members_changed(revision, group)
        if self._active_layer_id != old_active_id:
            self._emit_active_layer_changed(revision)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))

    def insert_group(
        self,
        group: LayerGroup,
        index: int | None = None,
    ) -> None:
        """Insert a top-level group and mark the document changed."""
        self.insert_groups((group,), index)

    def insert_groups(
        self,
        groups: tuple[DocumentNode, ...],
        index: int | None = None,
    ) -> None:
        """Insert several top-level groups as one document change."""
        if not groups:
            raise ValueError("at least one group is required")
        if len(self._nodes) + len(groups) > MAX_DOCUMENT_LAYERS:
            raise ValueError("document cannot contain more than 256 root groups")

        incoming = walk_nodes(groups)
        existing_ids = set(self._node_index)
        if any(node.id in existing_ids for node in incoming):
            raise ValueError("node id already belongs to the document")
        raster_count = 0
        for node in incoming:
            if isinstance(node, RasterObject):
                raster_count += 1
                if max(node.size.width, node.size.height) > MAX_DOCUMENT_DIMENSION:
                    raise ValueError("raster local size exceeds the project limit")
        if len(self._layers) + raster_count > MAX_DOCUMENT_LAYERS:
            raise ValueError("document cannot contain more than 256 raster layers")
        target = len(self._nodes) if index is None else index
        if not 0 <= target <= len(self._nodes):
            raise IndexError("group insertion index is out of range")
        self._nodes[target:target] = groups
        self._sync_node_index()
        revision = self._begin_content_change()
        self._emit_group_list_changed(revision)
        for group in groups:
            if isinstance(group, Collection) and group.children:
                self._emit_group_members_changed(revision, group)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))

    def remove_group(self, group_id: UUID) -> tuple[LayerGroup, int]:
        group = self.layer_group_by_id(group_id)
        index = self._nodes.index(group)
        self.remove_groups((group_id,))
        return group, index

    def remove_groups(self, group_ids: tuple[UUID, ...]) -> None:
        """Remove several top-level groups as one document change."""
        if not group_ids or len(set(group_ids)) != len(group_ids):
            raise ValueError("group ids must be non-empty and unique")
        for group_id in group_ids:
            self.layer_group_by_id(group_id)
        old_active_id = self._active_layer_id
        removed_ids = set(group_ids)
        self._nodes[:] = [node for node in self._nodes if node.id not in removed_ids]
        self._sync_node_index()
        if old_active_id not in {layer.id for layer in self._layers}:
            self._active_layer_id = self._layers[-1].id if self._layers else None
        revision = self._begin_content_change()
        self._emit_group_list_changed(revision)
        if self._active_layer_id != old_active_id:
            self._emit_active_layer_changed(revision)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))

    def move_group(self, group_id: UUID, target_index: int) -> bool:
        group = self.layer_group_by_id(group_id)
        if not 0 <= target_index < len(self._nodes):
            raise IndexError("target group index is out of range")
        source_index = self._nodes.index(group)
        if source_index == target_index:
            return False
        self._nodes.pop(source_index)
        self._nodes.insert(target_index, group)
        revision = self._begin_content_change()
        self._emit_group_list_changed(revision)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def ungroup_group(
        self,
        group_id: UUID,
        *,
        target_group_id: UUID | None = None,
        replacement_group: LayerGroup | None = None,
    ) -> bool:
        """Promote children into their collection's former sibling position."""
        group = self.layer_group_by_id(group_id)
        if self.is_locked(group_id):
            raise PermissionError("collection or ancestor is locked")
        parent = self._parent_index.get(group_id, self._root_group)
        index = parent.children.index(group)
        for child in group.children:
            child.opacity *= group.opacity
            child.visible = child.visible and group.visible
            child.locked = child.locked or group.locked
        parent.children[index : index + 1] = group.children
        parent.record_change()
        self._sync_node_index()
        revision = self._begin_content_change()
        self._emit_group_members_changed(revision, parent)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def restore_group_structure(
        self,
        nodes: tuple[DocumentNode, ...],
        active_layer_id: UUID | None,
    ) -> None:
        """Restore a previously captured shallow group structure for undo."""
        prospective = walk_nodes(nodes)
        if active_layer_id is not None and not any(
            isinstance(node, RasterObject) and node.id == active_layer_id for node in prospective
        ):
            raise ValueError("active raster object is missing")
        old_members = {
            group.id: tuple(node.id for node in group.children) for group in self.collections
        }

        def reuse(candidate: DocumentNode) -> DocumentNode:
            old = self._node_index.get(candidate.id)
            if isinstance(candidate, Collection):
                candidate.children = [reuse(child) for child in candidate.children]
            if old is None or type(old) is not type(candidate):
                return candidate
            unchanged = all(
                getattr(old, info.name) == getattr(candidate, info.name)
                for info in fields(candidate)
                if info.name not in {"children", "pixels", "_revision"}
            )
            if isinstance(old, RasterObject) and isinstance(candidate, RasterObject):
                unchanged = unchanged and old.pixels.generation == candidate.pixels.generation
            if unchanged:
                if isinstance(old, Collection) and isinstance(candidate, Collection):
                    old.children[:] = candidate.children
                return old
            return candidate

        self._nodes = [reuse(node) for node in nodes]
        self._root_group.children = self._nodes
        self._sync_node_index()
        if active_layer_id is not None:
            self.layer_by_id(active_layer_id)
        old_active_id = self._active_layer_id
        self._active_layer_id = active_layer_id
        revision = self._begin_content_change()
        self._emit_group_list_changed(revision)
        for group in self.collections:
            if old_members.get(group.id) != tuple(child.id for child in group.children):
                self._emit_group_members_changed(revision, group)
        if old_active_id != active_layer_id:
            self._emit_active_layer_changed(revision)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))

    def move_raster_layer_to_group(
        self,
        layer_id: UUID,
        target_group_id: UUID,
        target_index: int,
    ) -> bool:
        source_group = self.raster_group_for_layer(layer_id)
        target_group = self.layer_group_by_id(target_group_id)
        if not isinstance(target_group, Collection):
            raise ValueError("raster layer must move into a raster group")
        if source_group.locked or target_group.locked:
            raise PermissionError("raster group is locked")
        layer = self.layer_by_id(layer_id)
        source_index = source_group.children.index(layer)
        if source_group is target_group and source_index == target_index:
            return False
        if not 0 <= target_index <= len(target_group.children):
            raise IndexError("target raster child index is out of range")
        source_group.children.pop(source_index)
        if source_group is target_group and source_index < target_index:
            target_index -= 1
        target_group.children.insert(target_index, layer)
        source_group.record_change()
        if source_group is not target_group:
            target_group.record_change()
        self._sync_node_index()
        revision = self._begin_content_change()
        self._emit_group_members_changed(revision, source_group)
        if target_group is not source_group:
            self._emit_group_members_changed(revision, target_group)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def move_vector_object_to_group(
        self,
        object_id: UUID,
        target_group_id: UUID,
        target_index: int,
    ) -> bool:
        source_group, text_object = self.vector_object_by_id(object_id)
        target_group = self.vector_group_by_id(target_group_id)
        if source_group.locked or target_group.locked:
            raise PermissionError("vector group is locked")
        source_index = source_group.children.index(text_object)
        if not 0 <= target_index <= len(target_group.children):
            raise IndexError("target vector child index is out of range")
        if source_group is target_group and source_index == target_index:
            return False
        source_group.children.pop(source_index)
        if source_group is target_group and source_index < target_index:
            target_index -= 1
        target_group.children.insert(target_index, text_object)
        source_group.record_change()
        if source_group is not target_group:
            target_group.record_change()
        revision = self._begin_content_change()
        self._emit_group_members_changed(revision, source_group)
        if target_group is not source_group:
            self._emit_group_members_changed(revision, target_group)
        self._emit(
            VectorObjectChanged(
                document_id=self.id,
                revision=revision,
                group_id=target_group.id,
                object_id=object_id,
                object_kind=_vector_object_kind(text_object),
                properties=frozenset({"group", "order"}),
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def insert_vector_object(
        self,
        group_id: UUID,
        vector_object: VectorObject,
        index: int | None = None,
    ) -> None:
        group = self.vector_group_by_id(group_id)
        if group.locked:
            raise PermissionError("vector group is locked")
        if vector_object.id in self._node_index:
            raise ValueError("vector object id already belongs to the document")
        object_bounds(vector_object)
        target = len(group.children) if index is None else index
        if not 0 <= target <= len(group.children):
            raise IndexError("vector object insertion index is out of range")
        group.children.insert(target, vector_object)
        group.record_change()
        revision = self._begin_content_change()
        self._emit_group_members_changed(revision, group)
        self._emit(
            VectorObjectChanged(
                document_id=self.id,
                revision=revision,
                group_id=group.id,
                object_id=vector_object.id,
                object_kind=_vector_object_kind(vector_object),
                properties=frozenset({"created"}),
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))

    def remove_vector_object(
        self,
        group_id: UUID,
        object_id: UUID,
    ) -> tuple[VectorObject, int]:
        group = self.vector_group_by_id(group_id)
        if group.locked:
            raise PermissionError("vector group is locked")
        _, vector_object = self.vector_object_by_id(object_id)
        index = group.children.index(vector_object)
        group.children.pop(index)
        group.record_change()
        revision = self._begin_content_change()
        self._emit_group_members_changed(revision, group)
        self._emit(
            VectorObjectChanged(
                document_id=self.id,
                revision=revision,
                group_id=group.id,
                object_id=object_id,
                object_kind=_vector_object_kind(vector_object),
                properties=frozenset({"deleted"}),
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return vector_object, index

    def move_vector_object(
        self,
        group_id: UUID,
        object_id: UUID,
        target_index: int,
    ) -> bool:
        group = self.vector_group_by_id(group_id)
        if group.locked:
            raise PermissionError("vector group is locked")
        if not 0 <= target_index < len(group.children):
            raise IndexError("target vector object index is out of range")
        source_index = next(
            index for index, child in enumerate(group.children) if child.id == object_id
        )
        if source_index == target_index:
            return False
        child = group.children.pop(source_index)
        group.children.insert(target_index, child)
        group.record_change()
        revision = self._begin_content_change()
        self._emit_group_members_changed(revision, group)
        self._emit(
            VectorObjectChanged(
                document_id=self.id,
                revision=revision,
                group_id=group.id,
                object_id=object_id,
                object_kind=_vector_object_kind(self.vector_object_by_id(child.id)[1]),
                properties=frozenset({"order"}),
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def set_group_property(
        self,
        group_id: UUID,
        property_name: str,
        value: object,
    ) -> bool:
        group = self.layer_group_by_id(group_id)
        if property_name == "name":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("group name must not be empty")
            value = value.strip()
        elif property_name == "opacity":
            if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                raise ValueError("group opacity must be between 0 and 1")
            value = float(value)
        elif property_name in {"visible", "locked"} and not isinstance(value, bool):
            raise ValueError(f"group {property_name} must be a boolean")
        if property_name not in {"name", "visible", "opacity", "locked"}:
            raise ValueError("unsupported group property")
        if getattr(group, property_name) == value:
            return False
        setattr(group, property_name, value)
        group.record_change()
        revision = self._begin_content_change()
        self._emit(
            GroupPropertiesChanged(
                document_id=self.id,
                revision=revision,
                group_id=group.id,
                properties=frozenset({property_name}),
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def set_text_object_properties(
        self,
        object_id: UUID,
        **changes: object,
    ) -> frozenset[str]:
        group, text_object = self.text_object_by_id(object_id)
        if group.locked or text_object.locked:
            raise PermissionError("text object is locked")
        allowed = {
            "name",
            "text",
            "frame",
            "font_family",
            "font_size",
            "font_weight",
            "italic",
            "color",
            "stroke_color",
            "stroke_width",
            "horizontal_alignment",
            "vertical_alignment",
            "wrap_mode",
            "line_spacing",
            "visible",
            "opacity",
            "locked",
            "transform",
            "transform_pivot",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported text object properties: {sorted(unknown)}")
        values: dict[str, Any] = {
            "id": text_object.id,
            "name": text_object.name,
            "text": text_object.text,
            "frame": text_object.frame,
            "font_family": text_object.font_family,
            "font_size": text_object.font_size,
            "font_weight": text_object.font_weight,
            "italic": text_object.italic,
            "color": text_object.color,
            "stroke_color": text_object.stroke_color,
            "stroke_width": text_object.stroke_width,
            "horizontal_alignment": text_object.horizontal_alignment,
            "vertical_alignment": text_object.vertical_alignment,
            "wrap_mode": text_object.wrap_mode,
            "line_spacing": text_object.line_spacing,
            "visible": text_object.visible,
            "opacity": text_object.opacity,
            "locked": text_object.locked,
            "transform": text_object.transform,
            "transform_pivot": text_object.transform_pivot,
        }
        values.update(changes)
        candidate = TextObject(**values)
        object_bounds(candidate)
        changed = frozenset(
            name for name in allowed if getattr(text_object, name) != getattr(candidate, name)
        )
        if not changed:
            return frozenset()
        for name in changed:
            setattr(text_object, name, getattr(candidate, name))
        text_object.record_change()
        group.record_change()
        revision = self._begin_content_change()
        self._emit(
            VectorObjectChanged(
                document_id=self.id,
                revision=revision,
                group_id=group.id,
                object_id=text_object.id,
                object_kind=VectorObjectKind.SVG_TEXT,
                properties=changed,
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return changed

    def set_vector_object_properties(
        self,
        object_id: UUID,
        **changes: object,
    ) -> frozenset[str]:
        group, vector_object = self.vector_object_by_id(object_id)
        if isinstance(vector_object, TextObject):
            return self.set_text_object_properties(object_id, **changes)
        if group.locked or vector_object.locked:
            raise PermissionError("vector object is locked")
        allowed = {
            "name",
            "element_kind",
            "svg",
            "intrinsic_bounds",
            "transform",
            "transform_pivot",
            "fill_override",
            "stroke_override",
            "stroke_width_override",
            "visible",
            "opacity",
            "locked",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported vector object properties: {sorted(unknown)}")
        values: dict[str, Any] = {
            "id": vector_object.id,
            "name": vector_object.name,
            "element_kind": vector_object.element_kind,
            "svg": vector_object.svg,
            "intrinsic_bounds": vector_object.intrinsic_bounds,
            "transform": vector_object.transform,
            "transform_pivot": vector_object.transform_pivot,
            "fill_override": vector_object.fill_override,
            "stroke_override": vector_object.stroke_override,
            "stroke_width_override": vector_object.stroke_width_override,
            "visible": vector_object.visible,
            "opacity": vector_object.opacity,
            "locked": vector_object.locked,
        }
        values.update(changes)
        candidate = SvgObject(**values)
        object_bounds(candidate)
        changed = frozenset(
            name for name in allowed if getattr(vector_object, name) != getattr(candidate, name)
        )
        if not changed:
            return frozenset()
        for name in changed:
            setattr(vector_object, name, getattr(candidate, name))
        vector_object.record_change()
        group.record_change()
        revision = self._begin_content_change()
        self._emit(
            VectorObjectChanged(
                document_id=self.id,
                revision=revision,
                group_id=group.id,
                object_id=vector_object.id,
                object_kind=VectorObjectKind.SVG_ELEMENT,
                properties=changed,
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return changed

    def remove_layer(self, layer_id: UUID) -> tuple[RasterObject, int]:
        index = self.layer_index(layer_id)
        old_active_id = self._active_layer_id
        group = self.raster_group_for_layer(layer_id)
        layer = self.layer_by_id(layer_id)
        child_index = group.children.index(layer)
        group.children.pop(child_index)
        group.record_change()
        self._sync_node_index()
        if layer_id == old_active_id:
            fallback_index = index - 1 if index > 0 else 0
            self._active_layer_id = self._layers[fallback_index].id if self._layers else None
        revision = self._begin_content_change()
        self._emit_group_members_changed(revision, group)
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
        source_group = self.raster_group_for_layer(layer_id)
        layer = self.layer_by_id(layer_id)
        source_group.children.remove(layer)
        self._sync_node_index()
        if target_index == len(self._layers):
            target_group = next(
                group for group in reversed(self._nodes) if isinstance(group, Collection)
            )
            target_child_index = len(target_group.children)
        else:
            target_layer = self._layers[target_index]
            target_group = self.raster_group_for_layer(target_layer.id)
            target_child_index = target_group.children.index(target_layer)
        target_group.children.insert(target_child_index, layer)
        source_group.record_change()
        if target_group is not source_group:
            target_group.record_change()
        self._sync_node_index()
        revision = self._begin_content_change()
        self._emit_group_members_changed(revision, source_group)
        if target_group is not source_group:
            self._emit_group_members_changed(revision, target_group)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def apply_node_properties(self, updates: Mapping[UUID, Mapping[str, object]]) -> bool:
        from easypicture.domain.object_operations import node_with_properties

        candidates: list[tuple[DocumentNode, dict[str, object]]] = []
        for key, properties in updates.items():
            node = self.node_by_id(key)
            node_with_properties(node, properties)
            changes = {
                name: value for name, value in properties.items() if getattr(node, name) != value
            }
            if changes:
                candidates.append((node, changes))
        if not candidates:
            return False
        for node, changes in candidates:
            for name, value in changes.items():
                setattr(node, name, value)
            if not isinstance(node, RasterObject):
                node.record_change()
        revision = self._begin_content_change()
        for node, changes in candidates:
            property_names = frozenset(changes)
            if isinstance(node, Collection):
                self._emit(GroupPropertiesChanged(self.id, revision, node.id, property_names))
            elif isinstance(node, RasterObject):
                self._emit(LayerPropertiesChanged(self.id, revision, node.id, property_names))
            else:
                parent = self.parent_of(node.id) or self._root_group
                self._emit(
                    VectorObjectChanged(
                        self.id,
                        revision,
                        parent.id,
                        node.id,
                        _vector_object_kind(node),
                        property_names,
                    )
                )
        self._emit(DocumentRevisionChanged(self.id, revision))
        return True

    def apply_object_transforms(self, matrices: Mapping[UUID, AffineTransform]) -> bool:
        from easypicture.domain.coordinates import validate_object_transform

        changes: list[tuple[RasterObject | VectorObject, AffineTransform]] = []
        for node_id, matrix in matrices.items():
            node = self.node_by_id(node_id)
            if isinstance(node, Collection):
                raise ValueError("collections do not have a transform")
            if self.is_locked(node_id):
                raise PermissionError("object or ancestor collection is locked")
            validate_object_transform(matrix)
            candidate = replace(node, transform=matrix)
            object_bounds(candidate)
            if node.transform != matrix:
                changes.append((node, matrix))
        if not changes:
            return False
        for node, matrix in changes:
            node.transform = matrix
            if not isinstance(node, RasterObject):
                node.record_change()
        revision = self._begin_content_change()
        for node, _ in changes:
            if isinstance(node, RasterObject):
                self._emit(
                    LayerPropertiesChanged(self.id, revision, node.id, frozenset({"transform"}))
                )
            else:
                parent = self._parent_index.get(node.id, self._root_group)
                self._emit(
                    VectorObjectChanged(
                        self.id,
                        revision,
                        parent.id,
                        node.id,
                        _vector_object_kind(node),
                        frozenset({"transform"}),
                    )
                )
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
        if self.is_locked(layer_id):
            raise PermissionError("layer or raster group is locked")
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
        if self.is_locked(layer_id):
            raise PermissionError("layer or raster group is locked")
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
        sprite_workspace: SpriteWorkspace | None = None,
        embedded_documents: Mapping[UUID, Document] | None = None,
        vector_transforms: Mapping[UUID, AffineTransform] | None = None,
    ) -> bool:
        """Atomically apply output bounds and object geometry without implicit clipping."""
        if (
            document_size.width > MAX_DOCUMENT_DIMENSION
            or document_size.height > MAX_DOCUMENT_DIMENSION
        ):
            raise ValueError("document geometry exceeds the project limit")
        vector_changes: list[tuple[Collection, VectorObject | RasterObject, AffineTransform]] = []
        for object_id, matrix in (vector_transforms or {}).items():
            vector = self.node_by_id(object_id)
            if isinstance(vector, Collection):
                raise ValueError("collections do not have transforms")
            group = self.parent_of(object_id) or self._root_group
            from easypicture.domain.coordinates import validate_object_transform

            validate_object_transform(matrix)
            object_bounds(replace(vector, transform=matrix))
            if vector.transform != matrix:
                vector_changes.append((group, vector, matrix))
        resolved_layers: dict[UUID, RasterObject] = {}
        pixel_changes: dict[UUID, dict[TileCoord, RGBAArray | None]] = {}
        geometry_changed: dict[UUID, tuple[Size, PixelPoint, Size, PixelPoint]] = {}
        for layer_id, (size, origin) in layer_geometry.items():
            layer = self.layer_by_id(layer_id)
            resolved_layers[layer_id] = layer
            if max(size.width, size.height) > MAX_DOCUMENT_DIMENSION:
                raise ValueError("raster local size exceeds the project limit")
            if max(abs(origin.x), abs(origin.y)) > MAX_DOCUMENT_DIMENSION * 4:
                raise ValueError("raster origin exceeds the project limit")
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
                if before is not None and pixels is not None and np.array_equal(before, pixels):
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
            if before is not None and pixels is not None and np.array_equal(before, pixels):
                continue
            selection_changes[coord] = pixels

        current_workspace = self._sprite_workspace or SpriteWorkspace(Size(1, 1), [])
        resolved_sprite_workspace = (
            current_workspace.clone() if sprite_workspace is None else sprite_workspace.clone()
        )
        resolved_sprite_workspace.validate_for_canvas(document_size)
        resolved_embedded = (
            dict(self._embedded_documents)
            if embedded_documents is None
            else dict(embedded_documents)
        )
        detached_ids = {
            frame.embedded_document_id
            for frame in resolved_sprite_workspace.frames
            if frame.content_mode is SpriteFrameContentMode.DETACHED
        }
        if detached_ids != set(resolved_embedded):
            raise ValueError("geometry result has invalid embedded frame documents")
        sprite_workspace_changed = (
            resolved_sprite_workspace != current_workspace
            or resolved_embedded != self._embedded_documents
        )
        changed_sprite_frame_ids = tuple(
            sorted(
                {
                    frame.id
                    for frame in (
                        *current_workspace.frames,
                        *resolved_sprite_workspace.frames,
                    )
                },
                key=str,
            )
        )
        size_changed = self.size != document_size
        if (
            not size_changed
            and not geometry_changed
            and not pixel_changes
            and not selection_changes
            and not sprite_workspace_changed
            and not vector_changes
        ):
            return False

        old_document_size = self.size
        old_bounds = {
            layer_id: self._document_tiles_for_layer_bounds(layer.origin, layer.size)
            for layer_id, layer in resolved_layers.items()
        }
        for group, vector, matrix in vector_changes:
            vector.transform = matrix
            if not isinstance(vector, RasterObject):
                vector.record_change()
            group.record_change()
        self.size = document_size
        self.selection.size = document_size
        if self._sprite_workspace is not None:
            self._sprite_workspace = resolved_sprite_workspace
        self._embedded_documents = resolved_embedded
        self._subscribe_embedded_documents()
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
        for group, vector, _matrix in vector_changes:
            if isinstance(vector, RasterObject):
                self._emit(
                    LayerPropertiesChanged(self.id, revision, vector.id, frozenset({"transform"}))
                )
                continue
            self._emit(
                VectorObjectChanged(
                    self.id,
                    revision,
                    group.id,
                    vector.id,
                    _vector_object_kind(vector),
                    frozenset({"transform"}),
                )
            )
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
                before_size, before_origin, after_size, after_origin = geometry_changed[layer_id]
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
        if sprite_workspace_changed:
            self._emit(
                SpriteWorkspaceChanged(
                    document_id=self.id,
                    revision=revision,
                    change="geometry_reset",
                    frame_ids=changed_sprite_frame_ids,
                )
            )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def _sync_node_index(self) -> None:
        nodes = {node.id: node for node in walk_nodes(self._nodes)}
        parents: dict[UUID, Collection] = {}
        for group in nodes.values():
            if isinstance(group, Collection):
                for child in group.children:
                    parents[child.id] = group
        removed = self.spatial_index.object_ids - nodes.keys()
        for object_id in removed:
            self._record_bounds(object_id, None)
            self._bounds_descriptions.pop(object_id, None)
        self.spatial_index.remove(removed)
        self._node_index = nodes
        self._parent_index = parents
        for node in nodes.values():
            if isinstance(node, (RasterObject, TextObject, SvgObject)):
                self._sync_object_bounds(node)

    def _sync_object_bounds(self, node: RasterObject | TextObject | SvgObject) -> None:
        description = bounds_description(node)
        if self._bounds_descriptions.get(node.id) == description:
            return
        bounds = object_bounds(node)
        self._record_bounds(node.id, bounds)
        self.spatial_index.update(node.id, bounds)
        self._bounds_descriptions[node.id] = description

    def _record_bounds(self, object_id: UUID, bounds: FloatRect | None) -> None:
        old = self.spatial_index.bounds(object_id)
        if old == bounds:
            return
        previous = self._pending_bounds.get(object_id)
        self._pending_bounds[object_id] = ObjectBoundsChange(
            object_id, old if previous is None else previous.old, bounds
        )

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

    def set_source_name(self, name: str | None) -> None:
        """Set the unsaved/imported display identity without modifying content."""
        normalized = Path(name).name if name else None
        self._source_name = normalized

    def set_active_layer(self, layer_id: UUID | None) -> None:
        if layer_id == self._active_layer_id:
            return
        if layer_id is not None:
            self.layer_by_id(layer_id)
        self._active_layer_id = layer_id
        self._emit_active_layer_changed(self._revision)

    def replace_sprite_workspace(
        self,
        workspace: SpriteWorkspace,
        *,
        change: str,
        frame_ids: tuple[UUID, ...] = (),
        embedded_documents: Mapping[UUID, Document] | None = None,
    ) -> bool:
        workspace.validate_for_canvas(self.size)
        resolved_embedded = (
            dict(self._embedded_documents)
            if embedded_documents is None
            else dict(embedded_documents)
        )
        detached_ids = {
            frame.embedded_document_id
            for frame in workspace.frames
            if frame.content_mode is SpriteFrameContentMode.DETACHED
        }
        if detached_ids != set(resolved_embedded):
            raise ValueError("detached sprite frames and embedded documents must match")
        for embedded in resolved_embedded.values():
            if embedded.kind is not DocumentKind.SPRITE_FRAME:
                raise ValueError("embedded document must be a sprite frame")
            if embedded.size != workspace.cell_size:
                raise ValueError("embedded frame size must match sprite cell size")
        if workspace == self.sprite_workspace and resolved_embedded == self._embedded_documents:
            return False
        self._sprite_workspace = workspace.clone()
        self._embedded_documents = resolved_embedded
        self._subscribe_embedded_documents()
        revision = self._begin_content_change()
        self._emit(
            SpriteWorkspaceChanged(
                document_id=self.id,
                revision=revision,
                change=change,
                frame_ids=frame_ids,
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def _set_layer_property(self, layer_id: UUID, property_name: str, value: object) -> bool:
        layer = self.layer_by_id(layer_id)
        if self.raster_group_for_layer(layer_id).locked and property_name != "locked":
            raise PermissionError("raster group is locked")
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
        layer: RasterObject,
        layer_tiles: set[TileCoord],
    ) -> frozenset[TileCoord]:
        tile_size = layer.pixels.tile_size
        result: set[TileCoord] = set()
        for coord in layer_tiles:
            points = [
                layer.world_transform.map_point(Point(x, y))
                for x in (coord.x * tile_size, min(layer.size.width, (coord.x + 1) * tile_size))
                for y in (coord.y * tile_size, min(layer.size.height, (coord.y + 1) * tile_size))
            ]
            left = max(0, math.floor(min(point.x for point in points)))
            top = max(0, math.floor(min(point.y for point in points)))
            right = min(self.width, math.ceil(max(point.x for point in points)))
            bottom = min(self.height, math.ceil(max(point.y for point in points)))
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

    def _emit_group_list_changed(self, revision: int) -> None:
        self._emit(
            GroupListChanged(
                document_id=self.id,
                revision=revision,
                group_ids=tuple(node.id for node in self._nodes),
            )
        )

    def _emit_group_members_changed(
        self,
        revision: int,
        group: Collection,
    ) -> None:
        if group is self._root_group:
            self._emit_group_list_changed(revision)
            return
        self._emit(
            GroupMembersChanged(
                document_id=self.id,
                revision=revision,
                group_id=group.id,
                child_ids=tuple(child.id for child in group.children),
            )
        )

    def _emit_group_properties_changed(
        self,
        revision: int,
        group: Collection,
        properties: frozenset[str],
    ) -> None:
        self._emit(
            GroupPropertiesChanged(
                document_id=self.id,
                revision=revision,
                group_id=group.id,
                properties=properties,
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
        from easypicture.diagnostics import performance_counters

        performance_counters.domain_events += 1
        if isinstance(event, (GroupListChanged, GroupMembersChanged)):
            self._sync_node_index()
        elif isinstance(event, (LayerPropertiesChanged, VectorObjectChanged)):
            object_id = (
                event.layer_id if isinstance(event, LayerPropertiesChanged) else event.object_id
            )
            if event.properties & {
                "transform",
                "frame",
                "origin",
                "size",
                "intrinsic_bounds",
                "stroke_width",
                "stroke_width_override",
                "svg",
                "stroke_override",
            }:
                node = self._node_index.get(object_id)
                if isinstance(node, (RasterObject, TextObject, SvgObject)):
                    self._sync_object_bounds(node)
        if isinstance(
            event,
            (
                DocumentSizeChanged,
                GroupListChanged,
                GroupMembersChanged,
                GroupPropertiesChanged,
                LayerPropertiesChanged,
                LayerPixelsChanged,
                VectorObjectChanged,
                SelectionChanged,
                SpriteWorkspaceChanged,
                SpriteFrameContentChanged,
            ),
        ):
            self._pending_change_events.append(event)
        changes: DocumentChangeSet | None = None
        if isinstance(event, DocumentRevisionChanged):
            performance_counters.change_sets += 1
            categories = frozenset(
                category
                for pending in self._pending_change_events
                for category in event_categories(pending)
            )
            if not self._pending_change_events:
                categories = frozenset({ChangeCategory.CONTENT})
            self._versions = self._versions.advance(categories)
            changes = DocumentChangeSet(
                self.id,
                event.revision,
                tuple(self._pending_change_events),
                self._versions,
                tuple(self._pending_bounds.values()),
            )
            self._pending_change_events.clear()
            self._pending_bounds.clear()
        for listener in tuple(self._listeners.values()):
            listener(event)
        if changes is not None:
            for change_listener in tuple(self._change_listeners.values()):
                change_listener(changes)


__all__ = [
    "Document",
    "MAX_DOCUMENT_DIMENSION",
    "MAX_DOCUMENT_LAYERS",
    "Unsubscribe",
]
