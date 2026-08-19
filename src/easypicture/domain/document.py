"""Single-document aggregate and lifecycle events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
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
    GroupListChanged,
    GroupMembersChanged,
    GroupPropertiesChanged,
    LayerPixelsChanged,
    LayerPropertiesChanged,
    SelectionChanged,
    SpriteFrameContentChanged,
    SpriteWorkspaceChanged,
    VectorObjectChanged,
)
from easypicture.domain.geometry import PixelPoint, Size
from easypicture.domain.layer import RasterLayer
from easypicture.domain.selection import Selection
from easypicture.domain.sprite import (
    DocumentKind,
    SpriteFrameContentMode,
    SpriteWorkspace,
)
from easypicture.domain.vector import (
    LayerGroup,
    RasterGroupLayer,
    SvgElementObject,
    TextObject,
    VectorGroupLayer,
    VectorObject,
    VectorObjectKind,
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

    def __init__(
        self,
        *,
        document_id: UUID,
        size: Size,
        active_layer_id: UUID,
        selection: Selection,
        nodes: list[LayerGroup],
        sprite_workspace: SpriteWorkspace | None = None,
        kind: DocumentKind = DocumentKind.PROJECT,
        embedded_documents: Mapping[UUID, Document] | None = None,
        project_path: Path | None = None,
        source_name: str | None = None,
        modified: bool = False,
    ) -> None:
        if not nodes:
            raise ValueError("document must contain at least one root node")
        root_nodes = list(nodes)
        for node in root_nodes:
            if not isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
                raise ValueError("document contains an invalid top-level node")
        if len(root_nodes) > MAX_DOCUMENT_LAYERS:
            raise ValueError(f"document cannot contain more than {MAX_DOCUMENT_LAYERS} root groups")
        if not 1 <= size.width <= MAX_DOCUMENT_DIMENSION:
            raise ValueError("document width is out of range")
        if not 1 <= size.height <= MAX_DOCUMENT_DIMENSION:
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
        node_ids: set[UUID] = set()
        names: set[str] = set()
        for node in root_nodes:
            if node.id in node_ids:
                raise ValueError("node ids must be unique")
            node_ids.add(node.id)
            if node.name in names:
                raise ValueError("document node names must be unique")
            names.add(node.name)
            for child in node.children:
                if child.id in node_ids:
                    raise ValueError("node ids must be globally unique")
                node_ids.add(child.id)
                if child.name in names:
                    raise ValueError("document node names must be unique")
                names.add(child.name)
            if isinstance(node, RasterGroupLayer) and any(
                child.size != size or child.origin != PixelPoint()
                for child in node.children
            ):
                raise ValueError("raster layer geometry must match the canvas")
        raster_layers = [
            child
            for node in root_nodes
            if isinstance(node, RasterGroupLayer)
            for child in node.children
        ]
        if not raster_layers:
            raise ValueError("document must contain at least one raster layer")
        if len(raster_layers) > MAX_DOCUMENT_LAYERS:
            raise ValueError(
                f"document cannot contain more than {MAX_DOCUMENT_LAYERS} raster layers"
            )
        if all(layer.id != active_layer_id for layer in raster_layers):
            raise ValueError("active layer must belong to the document")
        self.id = document_id
        self.kind = kind
        self.size = size
        self._nodes = root_nodes
        self._layers = [
            child
            for node in self._nodes
            if isinstance(node, RasterGroupLayer)
            for child in node.children
        ]
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
        self._subscribe_embedded_documents()

    @classmethod
    def create(cls, width: int, height: int) -> Document:
        if not 1 <= width <= MAX_DOCUMENT_DIMENSION:
            raise ValueError(f"document width must be between 1 and {MAX_DOCUMENT_DIMENSION}")
        if not 1 <= height <= MAX_DOCUMENT_DIMENSION:
            raise ValueError(f"document height must be between 1 and {MAX_DOCUMENT_DIMENSION}")
        size = Size(width, height)
        layer = RasterLayer.create_blank("图层 1", size)
        group = RasterGroupLayer.create("栅格组")
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
    def create_sprite_frame(cls, size: Size) -> Document:
        layer = RasterLayer.create_blank("帧内容", size)
        group = RasterGroupLayer.create("栅格组")
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
        active_layer_id: UUID,
        selection: Selection,
        nodes: list[LayerGroup],
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
    def layers(self) -> tuple[RasterLayer, ...]:
        return tuple(self._layers)

    @property
    def nodes(self) -> tuple[LayerGroup, ...]:
        return tuple(self._nodes)

    @property
    def vector_groups(self) -> tuple[VectorGroupLayer, ...]:
        return tuple(node for node in self._nodes if isinstance(node, VectorGroupLayer))

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

    def record_change(self) -> None:
        revision = self._begin_content_change()
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))

    def layer_by_id(self, layer_id: UUID) -> RasterLayer:
        for layer in self._layers:
            if layer.id == layer_id:
                return layer
        raise ValueError("layer must belong to the document")

    def node_by_id(self, node_id: UUID) -> LayerGroup | RasterLayer | VectorObject:
        for node in self._nodes:
            if node.id == node_id:
                return node
            for child in node.children:
                if child.id == node_id:
                    return child
        raise ValueError("node must belong to the document")

    def vector_group_by_id(self, group_id: UUID) -> VectorGroupLayer:
        node = self.node_by_id(group_id)
        if not isinstance(node, VectorGroupLayer):
            raise ValueError("node is not a vector group")
        return node

    def layer_group_by_id(self, group_id: UUID) -> LayerGroup:
        node = self.node_by_id(group_id)
        if not isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
            raise ValueError("node is not a layer group")
        return node

    def raster_group_for_layer(self, layer_id: UUID) -> RasterGroupLayer:
        for group in self._nodes:
            if isinstance(group, RasterGroupLayer) and any(
                child.id == layer_id for child in group.children
            ):
                return group
        raise ValueError("raster layer must belong to a raster group")

    def text_object_by_id(self, object_id: UUID) -> tuple[VectorGroupLayer, TextObject]:
        node = self.node_by_id(object_id)
        if not isinstance(node, TextObject):
            raise ValueError("node is not a text object")
        for group in self.vector_groups:
            if any(child.id == object_id for child in group.children):
                return group, node
        raise ValueError("text object must belong to the document")

    def vector_object_by_id(self, object_id: UUID) -> tuple[VectorGroupLayer, VectorObject]:
        node = self.node_by_id(object_id)
        if not isinstance(node, (TextObject, SvgElementObject)):
            raise ValueError("node is not a vector object")
        for group in self.vector_groups:
            if any(child.id == object_id for child in group.children):
                return group, node
        raise ValueError("vector object must belong to the document")

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
        if layer.size != self.size or layer.origin != PixelPoint():
            raise ValueError("raster layer geometry must match the canvas")
        raster_groups = [node for node in self._nodes if isinstance(node, RasterGroupLayer)]
        if not raster_groups:
            group = RasterGroupLayer.create("栅格组")
            self._nodes.append(group)
        else:
            group = self.raster_group_for_layer(self._active_layer_id)
        if index == len(self._layers):
            child_index = len(group.children)
        else:
            target = self._layers[index]
            target_group = next(
                candidate
                for candidate in self._nodes
                if isinstance(candidate, RasterGroupLayer)
                and target in candidate.children
            )
            if target_group is group:
                child_index = group.children.index(target)
            else:
                child_index = len(group.children)
        group.children.insert(child_index, layer)
        group.record_change()
        self._refresh_raster_layers()
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
        groups: tuple[LayerGroup, ...],
        index: int | None = None,
    ) -> None:
        """Insert several top-level groups as one document change."""
        if not groups:
            raise ValueError("at least one group is required")
        if len(self._nodes) + len(groups) > MAX_DOCUMENT_LAYERS:
            raise ValueError("document cannot contain more than 256 root groups")

        existing_ids = {node.id for node in self._nodes} | {
            child.id for node in self._nodes for child in node.children
        }
        existing_names = {node.name for node in self._nodes} | {
            child.name for node in self._nodes for child in node.children
        }
        incoming_ids: set[UUID] = set()
        incoming_names: set[str] = set()
        incoming_raster_count = 0
        for group in groups:
            if isinstance(group, RasterGroupLayer):
                if any(not isinstance(child, RasterLayer) for child in group.children):
                    raise ValueError("raster group contains a non-raster child")
                if any(
                    child.size != self.size or child.origin != PixelPoint()
                    for child in group.children
                ):
                    raise ValueError("raster layer geometry must match the canvas")
                incoming_raster_count += len(group.children)
            elif isinstance(group, VectorGroupLayer):
                if any(
                    not isinstance(child, (TextObject, SvgElementObject))
                    for child in group.children
                ):
                    raise ValueError("vector group contains an unsupported child")
            else:
                raise ValueError("only layer groups may be inserted at the document root")

            group_ids = {group.id, *(child.id for child in group.children)}
            if len(group_ids) != len(group.children) + 1:
                raise ValueError("group and child ids must be distinct")
            if group_ids & (existing_ids | incoming_ids):
                raise ValueError("group id already belongs to the document")
            group_names = {group.name, *(child.name for child in group.children)}
            if len(group_names) != len(group.children) + 1:
                raise ValueError("group names must be unique")
            if group_names & (existing_names | incoming_names):
                raise ValueError("group names must be unique")
            incoming_ids.update(group_ids)
            incoming_names.update(group_names)

        if len(self._layers) + incoming_raster_count > MAX_DOCUMENT_LAYERS:
            raise ValueError("document cannot contain more than 256 raster layers")
        target = len(self._nodes) if index is None else index
        if not 0 <= target <= len(self._nodes):
            raise IndexError("group insertion index is out of range")
        self._nodes[target:target] = groups
        self._refresh_raster_layers()
        revision = self._begin_content_change()
        self._emit_group_list_changed(revision)
        for group in groups:
            if group.children:
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
        groups = [self.layer_group_by_id(group_id) for group_id in group_ids]
        removed_raster_count = sum(
            len(group.children)
            for group in groups
            if isinstance(group, RasterGroupLayer)
        )
        if len(self._layers) - removed_raster_count < 1:
            raise ValueError("document must retain at least one raster layer")
        old_active_id = self._active_layer_id
        removed_ids = set(group_ids)
        self._nodes[:] = [node for node in self._nodes if node.id not in removed_ids]
        self._refresh_raster_layers()
        if old_active_id not in {layer.id for layer in self._layers}:
            self._active_layer_id = self._layers[-1].id
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
        """Flatten one group into an adjacent compatible group.

        The operation is intentionally shallow.  When no compatible adjacent
        group exists, ``replacement_group`` is inserted at the source row and
        receives the children, so the document remains a strict two-level
        tree.
        """
        group = self.layer_group_by_id(group_id)
        if isinstance(group, RasterGroupLayer) and len(self._layers) == len(group.children):
            raise ValueError("document must retain at least one raster layer")
        source_index = self._nodes.index(group)
        target: LayerGroup | None = None
        if target_group_id is not None:
            target = self.layer_group_by_id(target_group_id)
            if type(target) is not type(group):
                raise ValueError("groups must have the same kind")
            if target is group:
                raise ValueError("a group cannot be ungrouped into itself")
        if target is None and replacement_group is None:
            raise ValueError("ungroup requires a compatible target group")
        if replacement_group is not None:
            if type(replacement_group) is not type(group):
                raise ValueError("replacement group has an incompatible kind")
            target = replacement_group

        self._nodes.pop(source_index)
        if isinstance(group, RasterGroupLayer):
            if isinstance(replacement_group, RasterGroupLayer):
                replacement_group.children.extend(group.children)
                self._nodes.insert(source_index, replacement_group)
            else:
                if not isinstance(target, RasterGroupLayer):
                    raise ValueError("raster group target is invalid")
                target_index = self._nodes.index(target)
                if target_index < source_index:
                    target.children.extend(group.children)
                else:
                    target.children[0:0] = group.children
        else:
            if isinstance(replacement_group, VectorGroupLayer):
                replacement_group.children.extend(group.children)
                self._nodes.insert(source_index, replacement_group)
            else:
                if not isinstance(target, VectorGroupLayer):
                    raise ValueError("vector group target is invalid")
                target_index = self._nodes.index(target)
                if target_index < source_index:
                    target.children.extend(group.children)
                else:
                    target.children[0:0] = group.children
        if isinstance(group, RasterGroupLayer):
            self._refresh_raster_layers()
        revision = self._begin_content_change()
        self._emit_group_list_changed(revision)
        if replacement_group is not None:
            self._emit_group_members_changed(revision, replacement_group)
        elif target is not None:
            self._emit_group_members_changed(revision, target)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=revision))
        return True

    def restore_group_structure(
        self,
        nodes: tuple[LayerGroup, ...],
        active_layer_id: UUID,
    ) -> None:
        """Restore a previously captured shallow group structure for undo."""
        self._nodes = list(nodes)
        self._refresh_raster_layers()
        self.layer_by_id(active_layer_id)
        old_active_id = self._active_layer_id
        self._active_layer_id = active_layer_id
        revision = self._begin_content_change()
        self._emit_group_list_changed(revision)
        for group in self._nodes:
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
        if not isinstance(target_group, RasterGroupLayer):
            raise ValueError("raster layer must move into a raster group")
        if source_group.locked or target_group.locked:
            raise PermissionError("raster group is locked")
        layer = source_group.child_by_id(layer_id)
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
        self._refresh_raster_layers()
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
        if any(existing.id == vector_object.id for node in self._nodes for existing in (
            node.children if isinstance(node, VectorGroupLayer) else ()
        )) or any(node.id == vector_object.id for node in self._nodes):
            raise ValueError("vector object id already belongs to the document")
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
        vector_object = group.child_by_id(object_id)
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
                object_kind=_vector_object_kind(child),
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
            "name", "text", "frame", "font_family", "font_size", "font_weight",
            "italic", "color", "stroke_color", "stroke_width",
            "horizontal_alignment", "vertical_alignment",
            "wrap_mode", "line_spacing", "visible", "opacity", "locked", "transform",
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
        candidate = SvgElementObject(**values)
        changed = frozenset(
            name
            for name in allowed
            if getattr(vector_object, name) != getattr(candidate, name)
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

    def remove_layer(self, layer_id: UUID) -> tuple[RasterLayer, int]:
        if len(self._layers) == 1:
            raise ValueError("document must retain at least one layer")
        index = self.layer_index(layer_id)
        old_active_id = self._active_layer_id
        group = self.raster_group_for_layer(layer_id)
        layer = group.child_by_id(layer_id)
        child_index = group.children.index(layer)
        group.children.pop(child_index)
        group.record_change()
        self._refresh_raster_layers()
        if layer_id == old_active_id:
            fallback_index = index - 1 if index > 0 else 0
            self._active_layer_id = self._layers[fallback_index].id
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
        layer = source_group.child_by_id(layer_id)
        source_group.children.remove(layer)
        self._refresh_raster_layers()
        if target_index == len(self._layers):
            target_group = next(
                group
                for group in reversed(self._nodes)
                if isinstance(group, RasterGroupLayer)
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
        self._refresh_raster_layers()
        revision = self._begin_content_change()
        self._emit_group_members_changed(revision, source_group)
        if target_group is not source_group:
            self._emit_group_members_changed(revision, target_group)
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
        if layer.locked or self.raster_group_for_layer(layer_id).locked:
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
        if layer.locked or self.raster_group_for_layer(layer_id).locked:
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

        resolved_sprite_workspace = (
            self.sprite_workspace.clone()
            if sprite_workspace is None
            else sprite_workspace.clone()
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
            resolved_sprite_workspace != self.sprite_workspace
            or resolved_embedded != self._embedded_documents
        )
        changed_sprite_frame_ids = tuple(
            sorted(
                {
                    frame.id
                    for frame in (
                        *self.sprite_workspace.frames,
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
        ):
            return False

        old_document_size = self.size
        old_bounds = {
            layer_id: self._document_tiles_for_layer_bounds(layer.origin, layer.size)
            for layer_id, layer in resolved_layers.items()
        }
        self.size = document_size
        self.selection.size = document_size
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

    def _refresh_raster_layers(self) -> None:
        self._layers = [
            child
            for node in self._nodes
            if isinstance(node, RasterGroupLayer)
            for child in node.children
        ]

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

    def set_active_layer(self, layer_id: UUID) -> None:
        if layer_id == self._active_layer_id:
            return
        self.layer_by_id(layer_id)
        self._active_layer_id = layer_id
        self._revision += 1
        self._emit_active_layer_changed(self._revision)
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=self._revision))

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
        group: RasterGroupLayer | VectorGroupLayer,
    ) -> None:
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
        group: RasterGroupLayer | VectorGroupLayer,
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
        for listener in tuple(self._listeners.values()):
            listener(event)


__all__ = [
    "Document",
    "MAX_DOCUMENT_DIMENSION",
    "MAX_DOCUMENT_LAYERS",
    "Unsubscribe",
]
