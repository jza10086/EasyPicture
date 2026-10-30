"""Undoable document mutation service built on QUndoStack."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import UUID, uuid4

import numpy as np
from PySide6.QtGui import QUndoCommand, QUndoStack

from easypicture.commands.object_service import ObjectCommandService
from easypicture.commands.patch_store import PatchHandle, UndoPatchStore
from easypicture.domain import (
    AffineTransform,
    AnimationDeleteMode,
    Collection,
    Document,
    DocumentNode,
    FloatRect,
    PixelPoint,
    Point,
    RasterObject,
    Size,
    SpriteAnimation,
    SpriteFrameContentMode,
    SpriteFrameDefinition,
    SpritePlaybackMode,
    SpriteWorkspace,
    SvgObject,
    TextObject,
    VectorObject,
    walk_nodes,
)
from easypicture.domain.document import MAX_DOCUMENT_LAYERS
from easypicture.imaging.ai_pixel_cleanup import AiPixelCleanupResult
from easypicture.imaging.geometry_ops import DocumentGeometryResult
from easypicture.imaging.pixel_outline import PixelOutlineResult
from easypicture.imaging.transform import RasterTransformResult
from easypicture.jobs import CancellationToken
from easypicture.persistence.snapshots import DocumentSnapshot
from easypicture.persistence.sprite_io import render_snapshot_region
from easypicture.pixels import MaskArray, Rect, RGBAArray, TileCoord, TilePixelStore

DEFAULT_UNDO_BUDGET_BYTES = 1024 * 1024 * 1024
DEFAULT_UNDO_LIMIT = 100
ConfirmHistoryReset = Callable[[int, int], bool]
ReportCommandTooLarge = Callable[[int, int], None]


def _unique_name(requested: str, existing: set[str]) -> str:
    """Return a stable Blender-like numeric suffix for a duplicate name."""
    normalized = requested.strip()
    if normalized not in existing:
        return normalized
    suffix = 1
    while f"{normalized}.{suffix}" in existing:
        suffix += 1
    return f"{normalized}.{suffix}"


def _document_names(document: Document, *, exclude: set[UUID] | None = None) -> set[str]:
    """Return names across both group rows and all child rows."""
    excluded = exclude or set()
    return {node.name for node in walk_nodes(document.nodes) if node.id not in excluded}


def _clone_group[T: DocumentNode](group: T) -> T:
    """Copy structure and Tile handles for undo, preserving identity and revisions."""
    import copy

    clone = copy.copy(group)
    if isinstance(clone, Collection):
        clone.children = [_clone_group(child) for child in clone.children]
    elif isinstance(clone, RasterObject):
        clone.pixels = clone.pixels.clone_shared()
    return clone


class _DocumentCommand(QUndoCommand):
    def __init__(self, document: Document, text: str, *, storage_bytes: int = 0) -> None:
        super().__init__(text)
        self.document = document
        self.command_id = uuid4()
        self.storage_bytes = storage_bytes
        self._released = False

    def release_payload(self) -> None:
        self._released = True


class _InsertLayerCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        layer: RasterObject,
        index: int,
        text: str,
        *,
        storage_bytes: int = 0,
    ) -> None:
        super().__init__(document, text, storage_bytes=storage_bytes)
        self.layer = layer
        self.index = index

    def redo(self) -> None:
        self.document.insert_layer(self.layer, self.index, activate=True)

    def undo(self) -> None:
        self.document.remove_layer(self.layer.id)


class _InsertOutlineCommand(_InsertLayerCommand):
    def __init__(self, document: Document, layer: RasterObject, source_id: UUID) -> None:
        super().__init__(
            document,
            layer,
            document.layer_index(source_id) + 1,
            "像素描边",
            storage_bytes=layer.pixels.allocated_bytes,
        )
        self.source_id = source_id
        self.previous_active_id = document.active_layer_id

    def redo(self) -> None:
        # An unrelated UI selection between undo/redo must not change the target group.
        self.document.set_active_layer(self.source_id)
        super().redo()

    def undo(self) -> None:
        super().undo()
        self.document.set_active_layer(self.previous_active_id)


class _InsertCleanupCommand(_InsertOutlineCommand):
    def __init__(self, document: Document, layer: RasterObject, source_id: UUID) -> None:
        super().__init__(document, layer, source_id)
        self.setText("AI 像素整理")
        self.source_visible = document.layer_by_id(source_id).visible

    def redo(self) -> None:
        super().redo()
        self.document.set_layer_visible(self.source_id, False)

    def undo(self) -> None:
        self.document.set_layer_visible(self.source_id, self.source_visible)
        super().undo()


class _DeleteLayerCommand(_DocumentCommand):
    def __init__(self, document: Document, layer: RasterObject, index: int) -> None:
        super().__init__(
            document,
            "删除图层",
            storage_bytes=layer.pixels.allocated_bytes,
        )
        self.layer = layer
        self.index = index
        self.was_active = document.active_layer_id == layer.id

    def redo(self) -> None:
        self.document.remove_layer(self.layer.id)

    def undo(self) -> None:
        self.document.insert_layer(self.layer, self.index, activate=self.was_active)


class _MoveLayerCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        layer_id: UUID,
        source_index: int,
        target_index: int,
    ) -> None:
        super().__init__(document, "移动图层")
        self.layer_id = layer_id
        self.source_index = source_index
        self.target_index = target_index

    def redo(self) -> None:
        self.document.move_layer(self.layer_id, self.target_index)

    def undo(self) -> None:
        self.document.move_layer(self.layer_id, self.source_index)


class _LayerPropertyCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        layer_id: UUID,
        setter_name: str,
        before: object,
        after: object,
        text: str,
    ) -> None:
        super().__init__(document, text)
        self.layer_id = layer_id
        self.setter_name = setter_name
        self.before = before
        self.after = after

    def _apply(self, value: object) -> None:
        setter = getattr(self.document, self.setter_name)
        setter(self.layer_id, value)

    def redo(self) -> None:
        self._apply(self.after)

    def undo(self) -> None:
        self._apply(self.before)


class _InsertGroupCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        group: Collection,
        index: int,
        text: str,
        *,
        storage_bytes: int = 0,
    ) -> None:
        super().__init__(document, text, storage_bytes=storage_bytes)
        self.group = group
        self.index = index

    def redo(self) -> None:
        self.document.insert_group(self.group, self.index)

    def undo(self) -> None:
        self.document.remove_group(self.group.id)


class _ObjectTransformsCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        before: dict[UUID, AffineTransform],
        after: dict[UUID, AffineTransform],
        delta: AffineTransform,
    ) -> None:
        super().__init__(document, "变换对象")
        self.before, self.after = before, after
        self.before_pivots: dict[UUID, Point | None] = {}
        self.after_pivots: dict[UUID, Point | None] = {}
        for key in before:
            node = cast(RasterObject | VectorObject, document.node_by_id(key))
            pivot = node.transform_pivot
            self.before_pivots[key] = pivot
            self.after_pivots[key] = None if pivot is None else delta.map_point(pivot)

    def redo(self) -> None:
        self.document.apply_object_transforms(self.after, self.after_pivots)

    def undo(self) -> None:
        self.document.apply_object_transforms(self.before, self.before_pivots)


class _MoveNodeCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        node_id: UUID,
        before: tuple[UUID | None, int],
        after: tuple[UUID | None, int],
    ) -> None:
        super().__init__(document, "移动对象")
        self.node_id, self.before, self.after = node_id, before, after

    def redo(self) -> None:
        self.document.move_node(self.node_id, *self.after)

    def undo(self) -> None:
        self.document.move_node(self.node_id, *self.before)


class _InsertGroupsCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        groups: tuple[DocumentNode, ...],
        index: int,
        text: str,
    ) -> None:
        storage_bytes = sum(
            child.pixels.allocated_bytes
            for child in walk_nodes(groups)
            if isinstance(child, RasterObject)
        )
        super().__init__(document, text, storage_bytes=storage_bytes)
        self.groups = groups
        self.index = index

    def redo(self) -> None:
        self.document.insert_groups(self.groups, self.index)

    def undo(self) -> None:
        self.document.remove_groups(tuple(group.id for group in self.groups))


class _DeleteGroupCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        group: Collection,
        index: int,
    ) -> None:
        storage_bytes = sum(
            child.pixels.allocated_bytes
            for child in group.children
            if isinstance(child, RasterObject)
        )
        super().__init__(document, "删除图层组", storage_bytes=storage_bytes)
        self.group = group
        self.index = index

    def redo(self) -> None:
        self.document.remove_group(self.group.id)

    def undo(self) -> None:
        self.document.insert_group(self.group, self.index)


class _MoveGroupCommand(_DocumentCommand):
    def __init__(self, document: Document, group_id: UUID, source: int, target: int) -> None:
        super().__init__(document, "移动图层组")
        self.group_id = group_id
        self.source = source
        self.target = target

    def redo(self) -> None:
        self.document.move_group(self.group_id, self.target)

    def undo(self) -> None:
        self.document.move_group(self.group_id, self.source)


class _MoveRasterChildCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        layer_id: UUID,
        source_group: UUID,
        source_index: int,
        target_group: UUID,
        target_index: int,
    ) -> None:
        super().__init__(document, "移动栅格子层")
        self.layer_id = layer_id
        self.source_group = source_group
        self.source_index = source_index
        self.target_group = target_group
        self.target_index = target_index

    def redo(self) -> None:
        self.document.move_raster_layer_to_group(
            self.layer_id, self.target_group, self.target_index
        )

    def undo(self) -> None:
        self.document.move_raster_layer_to_group(
            self.layer_id, self.source_group, self.source_index
        )


class _MoveVectorObjectToGroupCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        object_id: UUID,
        source_group: UUID,
        source_index: int,
        target_group: UUID,
        target_index: int,
    ) -> None:
        super().__init__(document, "移动矢量子层")
        self.object_id = object_id
        self.source_group = source_group
        self.source_index = source_index
        self.target_group = target_group
        self.target_index = target_index

    def redo(self) -> None:
        self.document.move_vector_object_to_group(
            self.object_id, self.target_group, self.target_index
        )

    def undo(self) -> None:
        self.document.move_vector_object_to_group(
            self.object_id, self.source_group, self.source_index
        )


class _UngroupCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        group: Collection,
        target_group: Collection | None,
        replacement_group: Collection | None,
        text: str,
    ) -> None:
        storage_bytes = sum(
            child.pixels.allocated_bytes
            for child in group.children
            if isinstance(child, RasterObject)
        )
        super().__init__(document, text, storage_bytes=storage_bytes)
        self.group = group
        self.target_group = target_group
        self.replacement_group = replacement_group
        # Keep an immutable structural snapshot.  The live groups are
        # mutated by ``ungroup_group`` (their children move to the target),
        # so retaining the original object references would make undo restore
        # already-empty groups and lose the children.
        self.before_nodes = tuple(_clone_group(node) for node in document.nodes)
        self.before_active_id = document.active_layer_id

    def redo(self) -> None:
        if self.replacement_group is not None:
            # A redo follows an undo with a restored source group.  The
            # replacement instance may still hold the children from the first
            # execution; clear it before moving the source children again.
            self.replacement_group.children.clear()
        self.document.ungroup_group(
            self.group.id,
            target_group_id=self.target_group.id if self.target_group is not None else None,
            replacement_group=self.replacement_group,
        )

    def undo(self) -> None:
        self.document.restore_group_structure(self.before_nodes, self.before_active_id)


class _ObjectPropertiesCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        before: dict[UUID, dict[str, object]],
        after: dict[UUID, dict[str, object]],
    ) -> None:
        super().__init__(document, "编辑对象属性")
        self.before, self.after = before, after

    def redo(self) -> None:
        self.document.apply_node_properties(self.after)

    def undo(self) -> None:
        self.document.apply_node_properties(self.before)


class _ReplaceGroupStructureCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        before_nodes: tuple[DocumentNode, ...],
        after_nodes: tuple[DocumentNode, ...],
        before_active_id: UUID | None,
        after_active_id: UUID | None,
        text: str,
        *,
        storage_bytes: int,
    ) -> None:
        super().__init__(document, text, storage_bytes=storage_bytes)
        self.before_nodes = before_nodes
        self.after_nodes = after_nodes
        self.before_active_id = before_active_id
        self.after_active_id = after_active_id

    def redo(self) -> None:
        self.document.restore_group_structure(
            tuple(_clone_group(node) for node in self.after_nodes),
            self.after_active_id,
        )

    def undo(self) -> None:
        self.document.restore_group_structure(
            tuple(_clone_group(node) for node in self.before_nodes),
            self.before_active_id,
        )


class _InsertVectorObjectCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        group_id: UUID,
        text_object: VectorObject,
        index: int,
        text: str,
    ) -> None:
        super().__init__(document, text)
        self.group_id = group_id
        self.text_object = text_object
        self.index = index

    def redo(self) -> None:
        self.document.insert_vector_object(self.group_id, self.text_object, self.index)

    def undo(self) -> None:
        self.document.remove_vector_object(self.group_id, self.text_object.id)


class _DeleteVectorObjectCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        group_id: UUID,
        text_object: VectorObject,
        index: int,
    ) -> None:
        super().__init__(document, "删除文字对象")
        self.group_id = group_id
        self.text_object = text_object
        self.index = index

    def redo(self) -> None:
        self.document.remove_vector_object(self.group_id, self.text_object.id)

    def undo(self) -> None:
        self.document.insert_vector_object(self.group_id, self.text_object, self.index)


class _MoveVectorObjectCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        group_id: UUID,
        object_id: UUID,
        source_index: int,
        target_index: int,
    ) -> None:
        super().__init__(document, "移动文字对象")
        self.group_id = group_id
        self.object_id = object_id
        self.source_index = source_index
        self.target_index = target_index

    def redo(self) -> None:
        self.document.move_vector_object(self.group_id, self.object_id, self.target_index)

    def undo(self) -> None:
        self.document.move_vector_object(self.group_id, self.object_id, self.source_index)


class _GroupPropertyCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        group_id: UUID,
        property_name: str,
        before: object,
        after: object,
        text: str,
    ) -> None:
        super().__init__(document, text)
        self.group_id = group_id
        self.property_name = property_name
        self.before = before
        self.after = after

    def _apply(self, value: object) -> None:
        self.document.set_group_property(self.group_id, self.property_name, value)

    def redo(self) -> None:
        self._apply(self.after)

    def undo(self) -> None:
        self._apply(self.before)


class _VectorObjectPropertyCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        object_id: UUID,
        before: Mapping[str, object],
        after: Mapping[str, object],
        text: str,
    ) -> None:
        super().__init__(document, text)
        self.object_id = object_id
        self.before = dict(before)
        self.after = dict(after)

    def _apply(self, values: Mapping[str, object]) -> None:
        self.document.set_vector_object_properties(self.object_id, **values)

    def redo(self) -> None:
        self._apply(self.after)

    def undo(self) -> None:
        self._apply(self.before)


class _SpriteWorkspaceCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        before: SpriteWorkspace,
        after: SpriteWorkspace,
        text: str,
        *,
        change: str,
        frame_ids: tuple[UUID, ...] = (),
        before_embedded: Mapping[UUID, Document] | None = None,
        after_embedded: Mapping[UUID, Document] | None = None,
        storage_bytes: int = 0,
    ) -> None:
        super().__init__(document, text, storage_bytes=storage_bytes)
        self.before = before.clone()
        self.after = after.clone()
        self.change = change
        self.frame_ids = frame_ids
        self.before_embedded = dict(
            document.embedded_documents if before_embedded is None else before_embedded
        )
        self.after_embedded = dict(
            self.before_embedded if after_embedded is None else after_embedded
        )

    def _apply(
        self,
        workspace: SpriteWorkspace,
        embedded_documents: Mapping[UUID, Document],
    ) -> None:
        self.document.replace_sprite_workspace(
            workspace,
            change=self.change,
            frame_ids=self.frame_ids,
            embedded_documents=embedded_documents,
        )

    def redo(self) -> None:
        self._apply(self.after, self.after_embedded)

    def undo(self) -> None:
        self._apply(self.before, self.before_embedded)


@dataclass(frozen=True, slots=True)
class _TilePatch:
    coord: TileCoord
    before: PatchHandle
    after: PatchHandle


class _PixelPatchCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        layer_id: UUID,
        patches: tuple[_TilePatch, ...],
        patch_store: UndoPatchStore,
        text: str,
    ) -> None:
        storage_bytes = sum(
            patch.before.stored_bytes + patch.after.stored_bytes for patch in patches
        )
        super().__init__(document, text, storage_bytes=storage_bytes)
        self.layer_id = layer_id
        self.patches = patches
        self.patch_store = patch_store

    def _apply(self, *, use_after: bool) -> None:
        updates = {
            patch.coord: self.patch_store.restore(patch.after if use_after else patch.before)
            for patch in self.patches
        }
        self.document.replace_layer_tiles(self.layer_id, updates)

    def redo(self) -> None:
        self._apply(use_after=True)

    def undo(self) -> None:
        self._apply(use_after=False)

    def release_payload(self) -> None:
        if self._released:
            return
        for patch in self.patches:
            self.patch_store.release(patch.before)
            self.patch_store.release(patch.after)
        super().release_payload()


class _SelectionPatchCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        patches: tuple[_TilePatch, ...],
        patch_store: UndoPatchStore,
        text: str,
    ) -> None:
        storage_bytes = sum(
            patch.before.stored_bytes + patch.after.stored_bytes for patch in patches
        )
        super().__init__(document, text, storage_bytes=storage_bytes)
        self.patches = patches
        self.patch_store = patch_store

    def _apply(self, *, use_after: bool) -> None:
        updates = {
            patch.coord: self.patch_store.restore(patch.after if use_after else patch.before)
            for patch in self.patches
        }
        self.document.replace_selection_tiles(updates)

    def redo(self) -> None:
        self._apply(use_after=True)

    def undo(self) -> None:
        self._apply(use_after=False)

    def release_payload(self) -> None:
        if self._released:
            return
        for patch in self.patches:
            self.patch_store.release(patch.before)
            self.patch_store.release(patch.after)
        super().release_payload()


class _RasterTransformCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        layer_id: UUID,
        before_size: Size,
        after_size: Size,
        before_origin: PixelPoint,
        after_origin: PixelPoint,
        layer_patches: tuple[_TilePatch, ...],
        selection_patches: tuple[_TilePatch, ...],
        patch_store: UndoPatchStore,
        text: str,
    ) -> None:
        storage_bytes = sum(
            patch.before.stored_bytes + patch.after.stored_bytes
            for patch in layer_patches + selection_patches
        )
        super().__init__(document, text, storage_bytes=storage_bytes)
        self.layer_id = layer_id
        self.before_size = before_size
        self.after_size = after_size
        self.before_origin = before_origin
        self.after_origin = after_origin
        self.layer_patches = layer_patches
        self.selection_patches = selection_patches
        self.patch_store = patch_store

    def _apply(self, *, use_after: bool) -> None:
        layer_updates = {
            patch.coord: self.patch_store.restore(patch.after if use_after else patch.before)
            for patch in self.layer_patches
        }
        selection_updates = {
            patch.coord: self.patch_store.restore(patch.after if use_after else patch.before)
            for patch in self.selection_patches
        }
        self.document.apply_raster_transform(
            self.layer_id,
            size=self.after_size if use_after else self.before_size,
            origin=self.after_origin if use_after else self.before_origin,
            layer_updates=layer_updates,
            selection_updates=selection_updates,
        )

    def redo(self) -> None:
        self._apply(use_after=True)

    def undo(self) -> None:
        self._apply(use_after=False)

    def release_payload(self) -> None:
        if self._released:
            return
        for patch in self.layer_patches + self.selection_patches:
            self.patch_store.release(patch.before)
            self.patch_store.release(patch.after)
        super().release_payload()


@dataclass(frozen=True, slots=True)
class _LayerGeometryPatch:
    layer_id: UUID
    before_size: Size
    after_size: Size
    before_origin: PixelPoint
    after_origin: PixelPoint
    pixel_patches: tuple[_TilePatch, ...]


class _DocumentGeometryCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        before_document_size: Size,
        after_document_size: Size,
        layers: tuple[_LayerGeometryPatch, ...],
        selection_patches: tuple[_TilePatch, ...],
        before_sprite_workspace: SpriteWorkspace,
        after_sprite_workspace: SpriteWorkspace,
        before_embedded_documents: Mapping[UUID, Document],
        after_embedded_documents: Mapping[UUID, Document],
        patch_store: UndoPatchStore,
        text: str,
        vector_transforms: tuple[tuple[UUID, AffineTransform, AffineTransform], ...] = (),
    ) -> None:
        storage_bytes = sum(
            patch.before.stored_bytes + patch.after.stored_bytes
            for layer in layers
            for patch in layer.pixel_patches
        ) + sum(patch.before.stored_bytes + patch.after.stored_bytes for patch in selection_patches)
        super().__init__(document, text, storage_bytes=storage_bytes)
        self.before_document_size = before_document_size
        self.after_document_size = after_document_size
        self.layers = layers
        self.selection_patches = selection_patches
        self.before_sprite_workspace = before_sprite_workspace.clone()
        self.after_sprite_workspace = after_sprite_workspace.clone()
        self.before_embedded_documents = dict(before_embedded_documents)
        self.after_embedded_documents = dict(after_embedded_documents)
        self.patch_store = patch_store
        self.vector_transforms = vector_transforms

    def _apply(self, *, use_after: bool) -> None:
        layer_geometry = {
            layer.layer_id: (
                layer.after_size if use_after else layer.before_size,
                layer.after_origin if use_after else layer.before_origin,
            )
            for layer in self.layers
        }
        layer_updates = {
            layer.layer_id: {
                patch.coord: self.patch_store.restore(patch.after if use_after else patch.before)
                for patch in layer.pixel_patches
            }
            for layer in self.layers
            if layer.pixel_patches
        }
        selection_updates = {
            patch.coord: self.patch_store.restore(patch.after if use_after else patch.before)
            for patch in self.selection_patches
        }
        self.document.apply_geometry_transaction(
            document_size=(self.after_document_size if use_after else self.before_document_size),
            layer_geometry=layer_geometry,
            layer_updates=layer_updates,
            selection_updates=selection_updates,
            sprite_workspace=(
                self.after_sprite_workspace if use_after else self.before_sprite_workspace
            ),
            embedded_documents=(
                self.after_embedded_documents if use_after else self.before_embedded_documents
            ),
            vector_transforms={
                object_id: after if use_after else before
                for object_id, before, after in self.vector_transforms
            },
        )

    def redo(self) -> None:
        self._apply(use_after=True)

    def undo(self) -> None:
        self._apply(use_after=False)

    def release_payload(self) -> None:
        if self._released:
            return
        for layer in self.layers:
            for patch in layer.pixel_patches:
                self.patch_store.release(patch.before)
                self.patch_store.release(patch.after)
        for patch in self.selection_patches:
            self.patch_store.release(patch.before)
            self.patch_store.release(patch.after)
        super().release_payload()


class DocumentCommandService:
    """The only application-layer entry point for undoable document changes."""

    def __init__(
        self,
        *,
        undo_budget_bytes: int = DEFAULT_UNDO_BUDGET_BYTES,
        confirm_history_reset: ConfirmHistoryReset | None = None,
        report_command_too_large: ReportCommandTooLarge | None = None,
    ) -> None:
        if undo_budget_bytes <= 0:
            raise ValueError("undo budget must be positive")
        self.undo_budget_bytes = undo_budget_bytes
        self.confirm_history_reset = confirm_history_reset or (lambda _required, _budget: False)
        self.report_command_too_large = report_command_too_large or (
            lambda _required, _budget: None
        )
        self.undo_stack = QUndoStack()
        self.objects = ObjectCommandService(self)
        self.undo_stack.setUndoLimit(DEFAULT_UNDO_LIMIT)
        self.patch_store = UndoPatchStore()
        self._document: Document | None = None
        self._project_document: Document | None = None
        self._commands: dict[UUID, _DocumentCommand] = {}
        self._suppress_clean_sync = False
        self._visibility_macro_open = False
        self.undo_stack.cleanChanged.connect(self._on_clean_changed)

    @property
    def document(self) -> Document | None:
        return self._document

    @property
    def project_document(self) -> Document | None:
        return self._project_document

    @property
    def history_bytes(self) -> int:
        return sum(command.storage_bytes for command in self._commands.values())

    def attach_document(self, document: Document | None) -> None:
        if self._visibility_macro_open:
            self.end_visibility_scrub()
        self._clear_stack()
        self._document = document
        self._project_document = document
        self._suppress_clean_sync = True
        try:
            if document is not None and document.modified:
                self.undo_stack.resetClean()
            else:
                self.undo_stack.setClean()
        finally:
            self._suppress_clean_sync = False

    def set_edit_document(self, document: Document) -> None:
        """Change the active source/frame editing target without resetting history."""
        project = self._project_document
        if project is None:
            raise RuntimeError("no project document is attached")
        from easypicture.domain.raster_edit import RasterEditDocument

        owner = document.owner if isinstance(document, RasterEditDocument) else document
        if owner is not project and all(
            candidate is not owner for candidate in project.embedded_documents.values()
        ):
            raise ValueError("edit document does not belong to the current project")
        if self._visibility_macro_open:
            self.end_visibility_scrub()
        self._document = document

    def create_sprite_frame(self, region: Rect) -> bool:
        document = self._require_project_document()
        workspace = document.sprite_workspace
        if (
            region.width != workspace.cell_size.width
            or region.height != workspace.cell_size.height
            or region.right > document.width
            or region.bottom > document.height
            or len(workspace.frames) >= 1024
        ):
            return False
        used = {frame.name for frame in workspace.frames}
        index = 1
        while f"frame_{index:03d}" in used:
            index += 1
        frame = SpriteFrameDefinition.create(f"frame_{index:03d}", region)
        after = workspace.clone()
        after.frames.append(frame)
        return self._push(
            _SpriteWorkspaceCommand(
                document,
                workspace,
                after,
                "创建精灵帧",
                change="created",
                frame_ids=(frame.id,),
            )
        )

    def update_sprite_frame(
        self,
        frame_id: UUID,
        *,
        region: Rect | None = None,
        pivot: PixelPoint | None = None,
        name: str | None = None,
    ) -> bool:
        document = self._require_project_document()
        workspace = document.sprite_workspace
        try:
            current = workspace.frame_by_id(frame_id)
        except ValueError:
            return False
        resolved_region = current.region if region is None else region
        if (
            resolved_region.width != workspace.cell_size.width
            or resolved_region.height != workspace.cell_size.height
            or resolved_region.right > document.width
            or resolved_region.bottom > document.height
        ):
            return False
        resolved_name = current.name if name is None else name.strip()
        if not resolved_name:
            return False
        existing_names = {frame.name for frame in workspace.frames if frame.id != frame_id}
        resolved_name = _unique_name(resolved_name, existing_names)
        try:
            updated = current.with_changes(
                name=resolved_name,
                region=resolved_region,
                pivot=pivot,
            )
        except ValueError:
            return False
        if updated == current:
            return False
        after = workspace.clone()
        after.frames[after.frames.index(current)] = updated
        return self._push(
            _SpriteWorkspaceCommand(
                document,
                workspace,
                after,
                "更新精灵帧",
                change="updated",
                frame_ids=(frame_id,),
            )
        )

    def delete_sprite_frame(self, frame_id: UUID) -> bool:
        return self.delete_sprite_frames((frame_id,))

    def delete_sprite_frames(self, frame_ids: tuple[UUID, ...]) -> bool:
        document = self._require_project_document()
        workspace = document.sprite_workspace
        unique_ids = tuple(dict.fromkeys(frame_ids))
        if not unique_ids:
            return False
        removed_ids = set(unique_ids)
        frames_by_id = {frame.id: frame for frame in workspace.frames}
        if any(frame_id not in frames_by_id for frame_id in unique_ids):
            return False
        after = workspace.clone()
        after.frames = [frame for frame in after.frames if frame.id not in removed_ids]
        after.animations = [
            animation.with_changes(
                frame_ids=tuple(item for item in animation.frame_ids if item not in removed_ids)
            )
            for animation in after.animations
        ]
        after_embedded = dict(document.embedded_documents)
        for frame_id in unique_ids:
            embedded_id = frames_by_id[frame_id].embedded_document_id
            if embedded_id is not None:
                after_embedded.pop(embedded_id, None)
        return self._push(
            _SpriteWorkspaceCommand(
                document,
                workspace,
                after,
                "删除精灵帧" if len(unique_ids) == 1 else f"删除 {len(unique_ids)} 个精灵帧",
                change="deleted",
                frame_ids=unique_ids,
                after_embedded=after_embedded,
            )
        )

    def move_sprite_frame(self, frame_id: UUID, target_index: int) -> bool:
        document = self._require_project_document()
        workspace = document.sprite_workspace
        try:
            frame = workspace.frame_by_id(frame_id)
            source_index = workspace.frames.index(frame)
        except ValueError:
            return False
        if source_index == target_index or not 0 <= target_index < len(workspace.frames):
            return False
        after = workspace.clone()
        moved = after.frames.pop(source_index)
        after.frames.insert(target_index, moved)
        return self._push(
            _SpriteWorkspaceCommand(
                document,
                workspace,
                after,
                "移动精灵帧",
                change="reordered",
                frame_ids=(frame_id,),
            )
        )

    def reorder_sprite_frames(
        self,
        frame_ids: tuple[UUID, ...],
        *,
        label: str = "排序精灵帧",
    ) -> bool:
        document = self._require_project_document()
        workspace = document.sprite_workspace
        current_ids = tuple(frame.id for frame in workspace.frames)
        if frame_ids == current_ids or len(frame_ids) != len(current_ids):
            return False
        if len(set(frame_ids)) != len(frame_ids) or set(frame_ids) != set(current_ids):
            return False
        frames_by_id = {frame.id: frame for frame in workspace.frames}
        after = workspace.clone()
        after.frames = [frames_by_id[frame_id] for frame_id in frame_ids]
        return self._push(
            _SpriteWorkspaceCommand(
                document,
                workspace,
                after,
                label,
                change="reordered",
                frame_ids=frame_ids,
            )
        )

    def set_sprite_cell_size(self, size: Size, *, clear_frames: bool) -> bool:
        document = self._require_project_document()
        workspace = document.sprite_workspace
        if size == workspace.cell_size:
            return False
        if size.width > document.width or size.height > document.height:
            return False
        if workspace.frames and not clear_frames:
            return False
        after = workspace.clone()
        after.cell_size = size
        if clear_frames:
            after.frames.clear()
            after.animations = [
                animation.with_changes(frame_ids=()) for animation in after.animations
            ]
        return self._push(
            _SpriteWorkspaceCommand(
                document,
                workspace,
                after,
                "更改单帧尺寸",
                change="cell_size",
                frame_ids=tuple(frame.id for frame in workspace.frames),
                after_embedded={} if clear_frames else None,
            )
        )

    def detach_sprite_frame(self, frame_id: UUID) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        try:
            frame = workspace.frame_by_id(frame_id)
        except ValueError:
            return False
        if frame.content_mode is SpriteFrameContentMode.DETACHED:
            return False
        pixels = render_snapshot_region(
            DocumentSnapshot.capture(project),
            frame.region,
            CancellationToken(),
            lambda _done, _total, _label: None,
        )
        embedded = Document.create_sprite_frame(workspace.cell_size)
        embedded.active_layer.pixels.write_region(
            Rect(0, 0, workspace.cell_size.width, workspace.cell_size.height),
            pixels,
        )
        updated = frame.with_changes(
            content_mode=SpriteFrameContentMode.DETACHED,
            embedded_document_id=embedded.id,
        )
        after = workspace.clone()
        after.frames[after.frames.index(frame)] = updated
        after_embedded = dict(project.embedded_documents)
        after_embedded[embedded.id] = embedded
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "独立化精灵帧",
                change="detached",
                frame_ids=(frame_id,),
                after_embedded=after_embedded,
            )
        )

    def create_detached_sprite_frame(
        self,
        pixels: RGBAArray,
        *,
        name: str,
        source_region: Rect,
        pivot: PixelPoint | None = None,
        duration_ticks: int = 1,
    ) -> UUID | None:
        """Create one ungrouped editable frame and embedded document atomically."""
        project = self._require_project_document()
        workspace = project.sprite_workspace
        height, width = pixels.shape[:2] if pixels.ndim == 3 else (0, 0)
        if (
            pixels.dtype != np.uint8
            or pixels.ndim != 3
            or pixels.shape[2] != 4
            or not pixels.flags.c_contiguous
            or (width, height) != (workspace.cell_size.width, workspace.cell_size.height)
            or len(workspace.frames) >= 1024
            or source_region.width != width
            or source_region.height != height
            or source_region.x < 0
            or source_region.y < 0
            or source_region.right > project.width
            or source_region.bottom > project.height
            or not 1 <= duration_ticks <= 999
        ):
            return None
        resolved_name = _unique_name(
            name.strip() or "transition_001",
            {frame.name for frame in workspace.frames},
        )
        resolved_pivot = pivot or PixelPoint(width // 2, height // 2)
        try:
            embedded = Document.create_sprite_frame(workspace.cell_size)
            embedded.active_layer.pixels.write_region(
                Rect(0, 0, width, height),
                pixels,
            )
            embedded.active_layer.record_pixel_change()
            embedded.mark_clean()
            frame = SpriteFrameDefinition(
                id=uuid4(),
                name=resolved_name,
                region=source_region,
                pivot=resolved_pivot,
                content_mode=SpriteFrameContentMode.DETACHED,
                embedded_document_id=embedded.id,
                duration_ticks=duration_ticks,
            )
        except ValueError:
            return None
        after = workspace.clone()
        after.frames.append(frame)
        after_embedded = dict(project.embedded_documents)
        after_embedded[embedded.id] = embedded
        if not self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "生成 Tile 过渡帧",
                change="created_detached",
                frame_ids=(frame.id,),
                after_embedded=after_embedded,
                storage_bytes=embedded.active_layer.pixels.allocated_bytes,
            )
        ):
            return None
        return frame.id

    def reset_sprite_frame_to_source(self, frame_id: UUID) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        try:
            frame = workspace.frame_by_id(frame_id)
        except ValueError:
            return False
        if frame.content_mode is SpriteFrameContentMode.LINKED:
            return False
        updated = frame.with_changes(
            content_mode=SpriteFrameContentMode.LINKED,
            embedded_document_id=None,
        )
        after = workspace.clone()
        after.frames[after.frames.index(frame)] = updated
        after_embedded = dict(project.embedded_documents)
        assert frame.embedded_document_id is not None
        after_embedded.pop(frame.embedded_document_id, None)
        if self._document is project.embedded_documents.get(frame.embedded_document_id):
            self._document = project
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "重置精灵帧为源区域",
                change="reset_to_source",
                frame_ids=(frame_id,),
                after_embedded=after_embedded,
            )
        )

    def duplicate_sprite_frame(self, frame_id: UUID) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        try:
            frame = workspace.frame_by_id(frame_id)
        except ValueError:
            return False
        if len(workspace.frames) >= 1024:
            return False
        new_frame_id = uuid4()
        embedded_id: UUID | None = None
        after_embedded = dict(project.embedded_documents)
        if frame.embedded_document_id is not None:
            source = project.embedded_document_by_id(frame.embedded_document_id)
            source_snapshot = DocumentSnapshot.capture(source)
            embedded_id = uuid4()
            copied_snapshot = replace(source_snapshot, document_id=embedded_id)
            after_embedded[embedded_id] = copied_snapshot.to_document()
        copied = SpriteFrameDefinition(
            id=new_frame_id,
            name=_unique_name(frame.name, {item.name for item in workspace.frames}),
            region=frame.region,
            pivot=frame.pivot,
            content_mode=frame.content_mode,
            embedded_document_id=embedded_id,
            duration_ticks=frame.duration_ticks,
        )
        after = workspace.clone()
        source_index = after.frames.index(frame)
        after.frames.insert(source_index + 1, copied)
        animation = after.animation_for_frame(frame_id)
        if animation is not None:
            ids = list(animation.frame_ids)
            ids.insert(ids.index(frame_id) + 1, new_frame_id)
            after.animations[after.animations.index(animation)] = animation.with_changes(
                frame_ids=tuple(ids)
            )
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "复制精灵帧",
                change="duplicated",
                frame_ids=(new_frame_id,),
                after_embedded=after_embedded,
            )
        )

    def create_sprite_animation(self, name: str = "动画") -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        if len(workspace.animations) >= 128:
            return False
        resolved = _unique_name(name, {animation.name for animation in workspace.animations})
        animation = SpriteAnimation.create(resolved)
        after = workspace.clone()
        after.animations.append(animation)
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "新建动画",
                change="animation_created",
            )
        )

    def rename_sprite_animation(self, animation_id: UUID, name: str) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        try:
            animation = workspace.animation_by_id(animation_id)
        except ValueError:
            return False
        normalized = name.strip()
        if not normalized:
            return False
        normalized = _unique_name(
            normalized,
            {item.name for item in workspace.animations if item.id != animation_id},
        )
        updated = animation.with_changes(name=normalized)
        if updated == animation:
            return False
        after = workspace.clone()
        after.animations[after.animations.index(animation)] = updated
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "重命名动画",
                change="animation_renamed",
            )
        )

    def set_sprite_animation_fps(self, animation_id: UUID, fps: int) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        try:
            animation = workspace.animation_by_id(animation_id)
            updated = animation.with_changes(fps=fps)
        except ValueError:
            return False
        if updated == animation:
            return False
        after = workspace.clone()
        after.animations[after.animations.index(animation)] = updated
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "更改动画 FPS",
                change="animation_fps",
                frame_ids=animation.frame_ids,
            )
        )

    def set_sprite_animation_playback_mode(
        self,
        animation_id: UUID,
        playback_mode: SpritePlaybackMode,
    ) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        try:
            animation = workspace.animation_by_id(animation_id)
        except ValueError:
            return False
        updated = animation.with_changes(playback_mode=playback_mode)
        if updated == animation:
            return False
        after = workspace.clone()
        after.animations[after.animations.index(animation)] = updated
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "更改动画播放模式",
                change="animation_playback_mode",
                frame_ids=animation.frame_ids,
            )
        )

    def set_sprite_frame_duration_ticks(
        self,
        frame_ids: tuple[UUID, ...],
        duration_ticks: int,
    ) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        unique_ids = tuple(dict.fromkeys(frame_ids))
        if not unique_ids or not 1 <= duration_ticks <= 999:
            return False
        frames_by_id = {frame.id: frame for frame in workspace.frames}
        if any(frame_id not in frames_by_id for frame_id in unique_ids):
            return False
        if all(frames_by_id[frame_id].duration_ticks == duration_ticks for frame_id in unique_ids):
            return False
        changed = set(unique_ids)
        after = workspace.clone()
        after.frames = [
            frame.with_changes(duration_ticks=duration_ticks) if frame.id in changed else frame
            for frame in after.frames
        ]
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "更改帧保持拍数",
                change="frame_duration",
                frame_ids=unique_ids,
            )
        )

    def move_sprite_animation(self, animation_id: UUID, target_index: int) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        try:
            animation = workspace.animation_by_id(animation_id)
            source_index = workspace.animations.index(animation)
        except ValueError:
            return False
        if not 0 <= target_index < len(workspace.animations) or source_index == target_index:
            return False
        after = workspace.clone()
        after.animations.insert(target_index, after.animations.pop(source_index))
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "移动动画",
                change="animation_reordered",
            )
        )

    def delete_sprite_animation(
        self,
        animation_id: UUID,
        mode: AnimationDeleteMode,
    ) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        try:
            animation = workspace.animation_by_id(animation_id)
        except ValueError:
            return False
        after = workspace.clone()
        after.animations.remove(animation)
        after_embedded = dict(project.embedded_documents)
        changed_ids = animation.frame_ids
        if mode is AnimationDeleteMode.DELETE_FRAMES:
            removed = set(animation.frame_ids)
            for frame in tuple(after.frames):
                if frame.id not in removed:
                    continue
                after.frames.remove(frame)
                if frame.embedded_document_id is not None:
                    after_embedded.pop(frame.embedded_document_id, None)
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "删除动画",
                change="animation_deleted",
                frame_ids=changed_ids,
                after_embedded=after_embedded,
            )
        )

    def move_sprite_frame_to_animation(
        self,
        frame_id: UUID,
        animation_id: UUID | None,
        target_index: int,
    ) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        try:
            workspace.frame_by_id(frame_id)
        except ValueError:
            return False
        after = workspace.clone()
        for index, animation in enumerate(after.animations):
            if frame_id in animation.frame_ids:
                after.animations[index] = animation.with_changes(
                    frame_ids=tuple(item for item in animation.frame_ids if item != frame_id)
                )
        if animation_id is not None:
            try:
                target = after.animation_by_id(animation_id)
            except ValueError:
                return False
            ids = list(target.frame_ids)
            insert_at = min(max(target_index, 0), len(ids))
            ids.insert(insert_at, frame_id)
            after.animations[after.animations.index(target)] = target.with_changes(
                frame_ids=tuple(ids)
            )
        if after == workspace:
            return False
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "移动精灵帧到动画",
                change="frame_animation_changed",
                frame_ids=(frame_id,),
            )
        )

    def move_sprite_frames_to_animation(
        self,
        frame_ids: tuple[UUID, ...],
        animation_id: UUID | None,
        target_index: int,
    ) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        unique_ids = tuple(dict.fromkeys(frame_ids))
        if not unique_ids:
            return False
        all_ids = {frame.id for frame in workspace.frames}
        if any(frame_id not in all_ids for frame_id in unique_ids):
            return False
        moved_ids = set(unique_ids)
        after = workspace.clone()
        for index, animation in enumerate(after.animations):
            if moved_ids.intersection(animation.frame_ids):
                after.animations[index] = animation.with_changes(
                    frame_ids=tuple(
                        frame_id for frame_id in animation.frame_ids if frame_id not in moved_ids
                    )
                )
        if animation_id is not None:
            try:
                target = after.animation_by_id(animation_id)
            except ValueError:
                return False
            ids = list(target.frame_ids)
            insert_at = min(max(target_index, 0), len(ids))
            ids[insert_at:insert_at] = unique_ids
            after.animations[after.animations.index(target)] = target.with_changes(
                frame_ids=tuple(ids)
            )
        else:
            grouped = {
                frame_id for animation in after.animations for frame_id in animation.frame_ids
            }
            current = [
                frame.id
                for frame in after.frames
                if frame.id not in grouped and frame.id not in moved_ids
            ]
            insert_at = min(max(target_index, 0), len(current))
            current[insert_at:insert_at] = unique_ids
            replacements = iter(current)
            ungrouped_slots = {frame.id for frame in after.frames if frame.id not in grouped}
            by_id = {frame.id: frame for frame in after.frames}
            after.frames = [
                by_id[next(replacements)] if frame.id in ungrouped_slots else frame
                for frame in after.frames
            ]
        if after == workspace:
            return False
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                "移动精灵帧到动画",
                change="frame_animation_changed",
                frame_ids=unique_ids,
            )
        )

    def reorder_sprite_animation_frames(
        self,
        animation_id: UUID,
        frame_ids: tuple[UUID, ...],
        *,
        label: str = "排序动画帧",
    ) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        try:
            animation = workspace.animation_by_id(animation_id)
        except ValueError:
            return False
        if (
            frame_ids == animation.frame_ids
            or len(frame_ids) != len(animation.frame_ids)
            or set(frame_ids) != set(animation.frame_ids)
        ):
            return False
        after = workspace.clone()
        after.animations[after.animations.index(animation)] = animation.with_changes(
            frame_ids=frame_ids
        )
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                label,
                change="animation_frames_reordered",
                frame_ids=frame_ids,
            )
        )

    def reorder_ungrouped_sprite_frames(
        self,
        frame_ids: tuple[UUID, ...],
        *,
        label: str = "排序未分组帧",
    ) -> bool:
        project = self._require_project_document()
        workspace = project.sprite_workspace
        current = workspace.ungrouped_frame_ids
        if frame_ids == current or len(frame_ids) != len(current) or set(frame_ids) != set(current):
            return False
        replacements = iter(frame_ids)
        ungrouped = set(current)
        by_id = {frame.id: frame for frame in workspace.frames}
        after = workspace.clone()
        after.frames = [
            by_id[next(replacements)] if frame.id in ungrouped else frame
            for frame in workspace.frames
        ]
        return self._push(
            _SpriteWorkspaceCommand(
                project,
                workspace,
                after,
                label,
                change="ungrouped_frames_reordered",
                frame_ids=frame_ids,
            )
        )

    def _commit_object_tree(
        self,
        after: list[DocumentNode],
        active: UUID | None,
        label: str,
    ) -> bool:
        document = self._require_document()
        nodes = walk_nodes(after)
        rasters = [node for node in nodes if isinstance(node, RasterObject)]
        if len(rasters) > MAX_DOCUMENT_LAYERS:
            return False
        if active is not None and all(node.id != active for node in rasters):
            active = rasters[-1].id if rasters else None
        before = tuple(_clone_group(node) for node in document.nodes)
        return self._push(
            _ReplaceGroupStructureCommand(
                document,
                before,
                tuple(after),
                document.active_layer_id,
                active,
                label,
                storage_bytes=max(
                    sum(node.pixels.allocated_bytes for node in rasters),
                    sum(node.pixels.allocated_bytes for node in document.layers),
                ),
            )
        )

    def create_object(
        self, node: DocumentNode, parent_id: UUID | None = None, index: int | None = None
    ) -> bool:
        document = self._require_document()
        try:
            incoming = walk_nodes((node,))
            if any(item.id in document._node_index for item in incoming):
                return False
            if parent_id is not None and document.is_locked(parent_id):
                return False
            after = [_clone_group(item) for item in document.nodes]
            siblings = after
            if parent_id is not None:
                parent = next(item for item in walk_nodes(after) if item.id == parent_id)
                if not isinstance(parent, Collection):
                    return False
                siblings = parent.children
            position = len(siblings) if index is None else index
            if not 0 <= position <= len(siblings):
                return False
            siblings.insert(position, node)
            active = node.id if isinstance(node, RasterObject) else document.active_layer_id
            return self._commit_object_tree(after, active, "创建对象")
        except ValueError, StopIteration:
            return False

    def create_collection(self, name: str = "集合", parent_id: UUID | None = None) -> bool:
        name = _unique_name(name, _document_names(self._require_document()))
        return self.create_object(Collection.create(name), parent_id)

    def delete_nodes(self, node_ids: tuple[UUID, ...]) -> bool:
        document = self._require_document()
        if not node_ids:
            return False
        try:
            targets = [document.node_by_id(node_id) for node_id in dict.fromkeys(node_ids)]
            if any(document.is_locked(node.id) for node in targets):
                return False
            if any(node.locked for target in targets for node in walk_nodes((target,))):
                return False
        except ValueError:
            return False
        removed = set(node_ids)

        def prune(nodes: tuple[DocumentNode, ...] | list[DocumentNode]) -> list[DocumentNode]:
            result: list[DocumentNode] = []
            for node in nodes:
                if node.id in removed:
                    continue
                clone = _clone_group(node)
                if isinstance(clone, Collection):
                    clone.children = prune(clone.children)
                result.append(clone)
            return result

        return self._commit_object_tree(prune(document.nodes), document.active_layer_id, "删除对象")

    def duplicate_node(self, node_id: UUID) -> bool:
        return self.duplicate_nodes((node_id,))

    def update_object_properties(
        self, ids: tuple[UUID, ...], properties: Mapping[str, object]
    ) -> bool:
        allowed = {
            "name",
            "visible",
            "opacity",
            "locked",
            "transform",
            "transform_pivot",
            "alpha_locked",
            "sampling",
        }
        if not ids or not properties or set(properties) - allowed:
            return False
        from easypicture.domain.object_operations import node_with_properties

        document = self._require_document()
        before: dict[UUID, dict[str, object]] = {}
        after: dict[UUID, dict[str, object]] = {}
        try:
            for key in dict.fromkeys(ids):
                node = document.node_by_id(key)
                if document.is_locked(key):
                    parent = document.parent_of(key)
                    if properties != {"locked": False} or parent and document.is_locked(parent.id):
                        return False
                node_with_properties(node, properties)
                changed = {
                    name: value
                    for name, value in properties.items()
                    if getattr(node, name) != value
                }
                if changed:
                    before[key] = {name: getattr(node, name) for name in changed}
                    after[key] = changed
        except ValueError, TypeError:
            return False
        return bool(after) and self._push(_ObjectPropertiesCommand(document, before, after))

    def _selected_roots(self, ids: tuple[UUID, ...]) -> tuple[DocumentNode, ...]:
        document = self._require_document()
        selected = set(ids)
        for key in selected:
            node = document.node_by_id(key)
            if any(document.is_locked(child.id) for child in walk_nodes((node,))):
                raise PermissionError("选中目标包含锁定对象")
        roots = []
        for node in walk_nodes(document.nodes):
            if node.id not in selected:
                continue
            parent = document.parent_of(node.id)
            while parent is not None and parent.id not in selected:
                parent = document.parent_of(parent.id)
            if parent is None:
                roots.append(node)
        return tuple(roots)

    def duplicate_nodes(self, ids: tuple[UUID, ...]) -> bool:
        document = self._require_document()
        try:
            roots = self._selected_roots(ids)
        except ValueError, PermissionError:
            return False
        if not roots:
            return False
        after = [_clone_group(node) for node in document.nodes]
        parents = {node.id: node for node in walk_nodes(after) if isinstance(node, Collection)}
        names = _document_names(document)
        active = document.active_layer_id
        for source in roots:
            parent = document.parent_of(source.id)
            siblings = parents[parent.id].children if parent else after
            index = next(index for index, item in enumerate(siblings) if item.id == source.id)
            duplicate = source.duplicate()
            for original, copied in zip(
                walk_nodes((source,)), walk_nodes((duplicate,)), strict=True
            ):
                copied.name = _unique_name(original.name, names)
                names.add(copied.name)
            siblings.insert(index + 1, duplicate)
            if isinstance(duplicate, RasterObject):
                active = duplicate.id
        return self._commit_object_tree(after, active, "复制对象")

    def move_nodes(self, ids: tuple[UUID, ...], parent_id: UUID | None, index: int) -> bool:
        if len(ids) == 1:
            return self.move_node(ids[0], parent_id, index)
        document = self._require_document()
        try:
            roots = self._selected_roots(ids)
            if parent_id is not None:
                parent = document.node_by_id(parent_id)
                if not isinstance(parent, Collection) or document.is_locked(parent_id):
                    return False
                if any(parent_id == child.id for root in roots for child in walk_nodes((root,))):
                    return False
        except ValueError, PermissionError:
            return False
        if not roots:
            return False
        selected = {node.id for node in roots}

        def prune(nodes: tuple[DocumentNode, ...] | list[DocumentNode]) -> list[DocumentNode]:
            result = []
            for node in nodes:
                if node.id in selected:
                    continue
                clone = _clone_group(node)
                if isinstance(clone, Collection):
                    clone.children = prune(clone.children)
                result.append(clone)
            return result

        after = prune(document.nodes)
        destination = next((node for node in walk_nodes(after) if node.id == parent_id), None)
        siblings = destination.children if isinstance(destination, Collection) else after
        if not 0 <= index <= len(siblings):
            return False
        siblings[index:index] = [_clone_group(node) for node in roots]

        def structure(nodes: tuple[DocumentNode, ...] | list[DocumentNode]) -> tuple[object, ...]:
            return tuple(
                (node.id, structure(node.children) if isinstance(node, Collection) else ())
                for node in nodes
            )

        if structure(after) == structure(document.nodes):
            return False
        return self._commit_object_tree(after, document.active_layer_id, "移动对象")

    def transform_objects(self, node_ids: tuple[UUID, ...], delta: AffineTransform) -> bool:
        """Apply one document-space delta to each unique descendant object atomically."""
        from easypicture.domain.coordinates import validate_object_transform
        from easypicture.domain.spatial import object_bounds

        document = self._require_document()
        before: dict[UUID, AffineTransform] = {}
        after: dict[UUID, AffineTransform] = {}
        try:
            validate_object_transform(delta)
            for node_id in dict.fromkeys(node_ids):
                root = document.node_by_id(node_id)
                if document.is_locked(node_id):
                    return False
                for node in walk_nodes((root,)):
                    if isinstance(node, Collection):
                        if node.locked:
                            return False
                        continue
                    if document.is_locked(node.id):
                        return False
                    before[node.id] = node.transform
                    if isinstance(node, RasterObject):
                        matrix = node.world_transform.then(delta).then(
                            AffineTransform.translation(-node.origin.x, -node.origin.y)
                        )
                    else:
                        matrix = node.transform.then(delta)
                    validate_object_transform(matrix)
                    object_bounds(replace(node, transform=matrix))
                    after[node.id] = matrix
        except ValueError:
            return False
        return before != after and self._push(
            _ObjectTransformsCommand(document, before, after, delta)
        )

    def move_node(self, node_id: UUID, parent_id: UUID | None, index: int) -> bool:
        document = self._require_document()
        try:
            before = document.node_location(node_id)
            document.validate_node_move(node_id, parent_id, index)
        except ValueError, IndexError, PermissionError:
            return False
        after = (parent_id, index)
        return before != after and self._push(_MoveNodeCommand(document, node_id, before, after))

    def create_layer(self) -> bool:
        document = self._require_document()
        if len(document.layers) >= MAX_DOCUMENT_LAYERS:
            return False
        names = _document_names(document)
        number = 1
        while f"图层 {number}" in names:
            number += 1
        layer = RasterObject.create_blank(f"图层 {number}", document.size)
        index = (
            document.layer_index(document.active_layer_id) + 1
            if document.active_layer_id is not None
            else len(document.layers)
        )
        return self._push(_InsertLayerCommand(document, layer, index, "新建图层"))

    def apply_pixel_outline(self, result: PixelOutlineResult) -> bool:
        document = self._require_document()
        if (
            document.id != result.document_id
            or document.revision != result.source_revision
            or document.active_layer_id != result.layer_id
            or len(document.layers) >= MAX_DOCUMENT_LAYERS
            or not result.updates
        ):
            return False
        source = document.layer_by_id(result.layer_id)
        if document.is_locked(source.id):
            return False
        name = _unique_name(f"{source.name} 描边", _document_names(document))
        layer = source.duplicate(name)
        for coord in result.updates:
            if (
                coord.x < 0
                or coord.y < 0
                or coord.x * 256 >= source.size.width
                or (coord.y * 256 >= source.size.height)
            ):
                raise ValueError("outline tile outside canvas")
        layer.pixels = TilePixelStore.from_readonly_tiles(result.updates)
        if not layer.pixels.allocated_tile_count:
            return False
        return self._push(_InsertOutlineCommand(document, layer, source.id))

    def apply_ai_pixel_cleanup(self, result: AiPixelCleanupResult) -> bool:
        document = self._require_document()
        if (
            document.id != result.document_id
            or document.revision != result.source_revision
            or document.active_layer_id != result.layer_id
            or not result.changed
            or not result.source_color_count
            or len(document.layers) >= MAX_DOCUMENT_LAYERS
        ):
            return False
        source = document.layer_by_id(result.layer_id)
        if document.is_locked(source.id):
            return False
        for coord in result.updates:
            if (
                coord.x < 0
                or coord.y < 0
                or coord.x * 256 >= source.size.width
                or (coord.y * 256 >= source.size.height)
            ):
                raise ValueError("cleanup tile outside canvas")
        name = _unique_name(f"{source.name} 像素整理", _document_names(document))
        layer = source.duplicate(name)
        layer.opacity, layer.visible = source.opacity, source.visible
        layer.pixels = TilePixelStore.from_readonly_tiles(result.updates)
        return self._push(_InsertCleanupCommand(document, layer, source.id))

    def create_raster_group(self, name: str = "集合") -> bool:
        document = self._require_document()
        if len(document.nodes) >= MAX_DOCUMENT_LAYERS:
            return False
        group_name = _unique_name(name, _document_names(document))
        group = Collection.create(group_name)
        return self._push(_InsertGroupCommand(document, group, len(document.nodes), "新建集合"))

    def create_vector_group(self, name: str = "集合") -> bool:
        document = self._require_document()
        if len(document.nodes) >= MAX_DOCUMENT_LAYERS:
            return False
        group_name = _unique_name(name, _document_names(document))
        group = Collection.create(group_name)
        return self._push(_InsertGroupCommand(document, group, len(document.nodes), "新建集合"))

    def append_groups(
        self,
        groups: tuple[DocumentNode, ...],
        label: str = "追加文件",
    ) -> bool:
        """Append prepared groups as one undoable document operation."""
        document = self._require_document()
        if not groups:
            return False
        if len(document.nodes) + len(groups) > MAX_DOCUMENT_LAYERS:
            return False
        raster_count = sum(isinstance(node, RasterObject) for node in walk_nodes(groups))
        if len(document.layers) + raster_count > MAX_DOCUMENT_LAYERS:
            return False
        return self._push(_InsertGroupsCommand(document, groups, len(document.nodes), label))

    def delete_group(self, group_id: UUID) -> bool:
        return self.delete_nodes((group_id,))

    def duplicate_group(self, group_id: UUID) -> bool:
        return self.duplicate_node(group_id)

    def rename_group(self, group_id: UUID, name: str) -> bool:
        document = self._require_document()
        try:
            group = document.layer_group_by_id(group_id)
        except ValueError:
            return False
        normalized = _unique_name(name, _document_names(document, exclude={group_id}))
        if not normalized:
            return False
        return self._push_group_property(group, "name", normalized, "重命名图层组")

    def move_group(self, group_id: UUID, target_index: int) -> bool:
        document = self._require_document()
        try:
            source = next(
                index for index, group in enumerate(document.nodes) if group.id == group_id
            )
        except StopIteration:
            return False
        if source == target_index or not 0 <= target_index < len(document.nodes):
            return False
        return self._push(_MoveGroupCommand(document, group_id, source, target_index))

    def set_group_visible(self, group_id: UUID, visible: bool) -> bool:
        return self._set_generic_group_property(group_id, "visible", visible, "切换组可见性")

    def set_group_opacity(self, group_id: UUID, opacity: float) -> bool:
        if not 0.0 <= opacity <= 1.0:
            return False
        return self._set_generic_group_property(group_id, "opacity", opacity, "更改组不透明度")

    def set_group_locked(self, group_id: UUID, locked: bool) -> bool:
        return self._set_generic_group_property(group_id, "locked", locked, "切换组锁定")

    def ungroup(self, group_id: UUID) -> bool:
        document = self._require_document()
        try:
            group = document.layer_group_by_id(group_id)
            if document.is_locked(group_id):
                return False
        except ValueError:
            return False
        return self._push(_UngroupCommand(document, group, None, None, "取消分组"))

    def move_raster_layer_to_group(
        self,
        layer_id: UUID,
        target_group_id: UUID,
        target_index: int,
    ) -> bool:
        document = self._require_document()
        try:
            source_group = document.raster_group_for_layer(layer_id)
            target_group = document.layer_group_by_id(target_group_id)
        except ValueError:
            return False
        source_index = source_group.children.index(source_group.child_by_id(layer_id))
        return self._push(
            _MoveRasterChildCommand(
                document,
                layer_id,
                source_group.id,
                source_index,
                target_group.id,
                target_index,
            )
        )

    def move_vector_object_to_group(
        self,
        object_id: UUID,
        target_group_id: UUID,
        target_index: int,
    ) -> bool:
        document = self._require_document()
        try:
            source_group, vector_object = document.vector_object_by_id(object_id)
            target_group = document.vector_group_by_id(target_group_id)
        except ValueError:
            return False
        source_index = source_group.children.index(vector_object)
        return self._push(
            _MoveVectorObjectToGroupCommand(
                document,
                object_id,
                source_group.id,
                source_index,
                target_group.id,
                target_index,
            )
        )

    def delete_layer(self, layer_id: UUID) -> bool:
        return self.delete_nodes((layer_id,))

    def duplicate_layer(self, layer_id: UUID) -> bool:
        document = self._require_document()
        if len(document.layers) >= MAX_DOCUMENT_LAYERS:
            return False
        source = document.layer_by_id(layer_id)
        layer = source.duplicate()
        layer.name = _unique_name(
            layer.name,
            _document_names(document),
        )
        index = document.layer_index(layer_id) + 1
        return self._push(
            _InsertLayerCommand(
                document,
                layer,
                index,
                "复制图层",
                storage_bytes=layer.pixels.allocated_bytes,
            )
        )

    def rename_layer(self, layer_id: UUID, name: str) -> bool:
        document = self._require_document()
        normalized = name.strip()
        if not normalized:
            return False
        layer = document.layer_by_id(layer_id)
        normalized = _unique_name(
            normalized,
            _document_names(document, exclude={layer_id}),
        )
        return self._push_property(
            layer_id,
            "set_layer_name",
            layer.name,
            normalized,
            "重命名图层",
        )

    def move_layer(self, layer_id: UUID, target_index: int) -> bool:
        document = self._require_document()
        source_index = document.layer_index(layer_id)
        if source_index == target_index:
            return False
        if not 0 <= target_index < len(document.layers):
            return False
        return self._push(_MoveLayerCommand(document, layer_id, source_index, target_index))

    def create_text_object(
        self,
        group_id: UUID,
        frame: FloatRect | None = None,
        *,
        name: str = "文本",
        text: str = "文本",
        index: int | None = None,
    ) -> bool:
        document = self._require_document()
        try:
            group = document.vector_group_by_id(group_id)
        except ValueError:
            return False
        if group.locked:
            return False
        if frame is None:
            frame = FloatRect(0.0, 0.0, 256.0, 64.0)
        target_index = len(group.children) if index is None else index
        if not 0 <= target_index <= len(group.children):
            return False
        name = _unique_name(name, _document_names(document))
        try:
            text_object = TextObject.create(frame, name=name, text=text)
        except ValueError:
            return False
        return self._push(
            _InsertVectorObjectCommand(
                document,
                group_id,
                text_object,
                target_index,
                "新建文字",
            )
        )

    def create_svg_object(
        self,
        group_id: UUID,
        svg_object: SvgObject,
        *,
        index: int | None = None,
        label: str = "创建矢量图形",
    ) -> bool:
        document = self._require_document()
        try:
            group = document.vector_group_by_id(group_id)
        except ValueError:
            return False
        if group.locked:
            return False
        target_index = len(group.children) if index is None else index
        if not 0 <= target_index <= len(group.children):
            return False
        candidate = replace(
            svg_object,
            name=_unique_name(svg_object.name, _document_names(document)),
        )
        return self._push(
            _InsertVectorObjectCommand(
                document,
                group_id,
                candidate,
                target_index,
                label,
            )
        )

    def create_svg_object_in_new_group(
        self,
        svg_object: SvgObject,
        *,
        group_name: str = "集合",
        label: str = "创建矢量图形",
    ) -> bool:
        """Insert a new vector group and its first object as one undo step."""
        document = self._require_document()
        if len(document.nodes) >= MAX_DOCUMENT_LAYERS:
            return False
        names = _document_names(document)
        group = Collection.create(_unique_name(group_name, names))
        candidate = replace(
            svg_object,
            name=_unique_name(svg_object.name, names | {group.name}),
        )
        group.children.append(candidate)
        return self._push(
            _InsertGroupCommand(
                document,
                group,
                len(document.nodes),
                label,
            )
        )

    def delete_text_object(self, object_id: UUID) -> bool:
        document = self._require_document()
        try:
            group, text_object = document.vector_object_by_id(object_id)
        except ValueError:
            return False
        if group.locked:
            return False
        return self._push(
            _DeleteVectorObjectCommand(
                document,
                group.id,
                text_object,
                group.children.index(text_object),
            )
        )

    def duplicate_text_object(self, object_id: UUID) -> bool:
        document = self._require_document()
        try:
            group, text_object = document.vector_object_by_id(object_id)
        except ValueError:
            return False
        if group.locked:
            return False
        duplicate = text_object.duplicate()
        duplicate.name = _unique_name(
            duplicate.name,
            _document_names(document),
        )
        return self._push(
            _InsertVectorObjectCommand(
                document,
                group.id,
                duplicate,
                group.children.index(text_object) + 1,
                "复制文字",
            )
        )

    def move_text_object(self, object_id: UUID, target_index: int) -> bool:
        document = self._require_document()
        try:
            group, text_object = document.vector_object_by_id(object_id)
        except ValueError:
            return False
        source_index = group.children.index(text_object)
        if source_index == target_index or not 0 <= target_index < len(group.children):
            return False
        if group.locked:
            return False
        return self._push(
            _MoveVectorObjectCommand(
                document,
                group.id,
                object_id,
                source_index,
                target_index,
            )
        )

    def update_text_object(self, object_id: UUID, **changes: object) -> bool:
        document = self._require_document()
        try:
            group, text_object = document.text_object_by_id(object_id)
        except ValueError:
            return False
        if not changes:
            return False
        if not self._can_update_node(object_id, changes):
            return False
        candidate_changes: dict[str, Any] = dict(changes)
        if "name" in candidate_changes:
            normalized = str(candidate_changes["name"]).strip()
            if not normalized:
                return False
            candidate_changes["name"] = _unique_name(
                normalized,
                _document_names(document, exclude={object_id}),
            )
        try:
            # ``replace`` executes the same domain validation as the real
            # command without mutating the live object.
            replace(text_object, **candidate_changes)
        except PermissionError, TypeError, ValueError:
            return False
        before = {key: getattr(text_object, key) for key in candidate_changes}
        after: dict[str, Any] = candidate_changes
        if all(before[key] == after[key] for key in before):
            return False
        return self._push(
            _VectorObjectPropertyCommand(
                document,
                object_id,
                before,
                after,
                "编辑文字属性",
            )
        )

    def set_text_object_visible(self, object_id: UUID, visible: bool) -> bool:
        return self.update_vector_object(object_id, visible=visible)

    def set_text_object_opacity(self, object_id: UUID, opacity: float) -> bool:
        if not 0.0 <= opacity <= 1.0:
            return False
        return self.update_vector_object(object_id, opacity=opacity)

    def set_text_object_locked(self, object_id: UUID, locked: bool) -> bool:
        return self.update_vector_object(object_id, locked=locked)

    def update_vector_object(self, object_id: UUID, **changes: object) -> bool:
        document = self._require_document()
        try:
            group, vector_object = document.vector_object_by_id(object_id)
        except ValueError:
            return False
        if not changes:
            return False
        if not self._can_update_node(object_id, changes):
            return False
        candidate_changes: dict[str, Any] = dict(changes)
        if "name" in candidate_changes:
            normalized = str(candidate_changes["name"]).strip()
            if not normalized:
                return False
            candidate_changes["name"] = _unique_name(
                normalized,
                _document_names(document, exclude={object_id}),
            )
        try:
            replace(vector_object, **candidate_changes)
        except PermissionError, TypeError, ValueError:
            return False
        before = {key: getattr(vector_object, key) for key in candidate_changes}
        if all(before[key] == candidate_changes[key] for key in before):
            return False
        return self._push(
            _VectorObjectPropertyCommand(
                document,
                object_id,
                before,
                candidate_changes,
                "编辑矢量对象属性",
            )
        )

    def rasterize_vector_group(self, group_id: UUID) -> bool:
        return self.rasterize_object(group_id)

    def rasterize_vector_object(self, object_id: UUID) -> bool:
        return self.rasterize_object(object_id)

    def replace_object(self, replacement: DocumentNode, label: str) -> bool:
        document = self._require_document()
        try:
            if document.is_locked(replacement.id):
                return False
            parent_id, index = document.node_location(replacement.id)
        except ValueError:
            return False
        after = [_clone_group(node) for node in document.nodes]
        destination = next((node for node in walk_nodes(after) if node.id == parent_id), None)
        siblings = destination.children if isinstance(destination, Collection) else after
        siblings[index] = replacement
        active = (
            replacement.id if isinstance(replacement, RasterObject) else document.active_layer_id
        )
        return self._commit_object_tree(after, active, label)

    def rasterize_object(self, object_id: UUID) -> bool:
        from easypicture.imaging.object_rasterize import rasterize_object

        document = self._require_document()
        try:
            node = document.node_by_id(object_id)
            if any(document.is_locked(child.id) for child in walk_nodes((node,))):
                return False
            if isinstance(node, RasterObject) and node.transform.is_identity:
                return False
            result = rasterize_object(node)
        except ValueError:
            return False
        return self.replace_object(
            result, "应用对象变换" if isinstance(node, RasterObject) else "栅格化对象"
        )

    def _push_group_property(
        self,
        group: Collection,
        property_name: str,
        value: object,
        label: str,
    ) -> bool:
        if getattr(group, property_name) == value or not self._can_update_node(
            group.id, {property_name: value}
        ):
            return False
        return self._push(
            _GroupPropertyCommand(
                self._require_document(),
                group.id,
                property_name,
                getattr(group, property_name),
                value,
                label,
            )
        )

    def _set_generic_group_property(
        self,
        group_id: UUID,
        property_name: str,
        value: object,
        label: str,
    ) -> bool:
        document = self._require_document()
        try:
            group = document.layer_group_by_id(group_id)
        except ValueError:
            return False
        if getattr(group, property_name) == value:
            return False
        return self._push_group_property(group, property_name, value, label)

    def set_layer_visible(self, layer_id: UUID, visible: bool) -> bool:
        layer = self._require_document().layer_by_id(layer_id)
        return self._push_property(
            layer_id,
            "set_layer_visible",
            layer.visible,
            visible,
            "切换图层可见性",
        )

    def begin_visibility_scrub(self) -> bool:
        if self._document is None or self._visibility_macro_open:
            return False
        self.undo_stack.beginMacro("批量切换图层可见性")
        self._visibility_macro_open = True
        return True

    def end_visibility_scrub(self) -> None:
        if not self._visibility_macro_open:
            return
        self.undo_stack.endMacro()
        self._visibility_macro_open = False
        self._reconcile_commands()

    def set_layer_opacity(self, layer_id: UUID, opacity: float) -> bool:
        if not 0.0 <= opacity <= 1.0:
            return False
        layer = self._require_document().layer_by_id(layer_id)
        return self._push_property(
            layer_id,
            "set_layer_opacity",
            layer.opacity,
            opacity,
            "更改图层不透明度",
        )

    def set_layer_locked(self, layer_id: UUID, locked: bool) -> bool:
        layer = self._require_document().layer_by_id(layer_id)
        return self._push_property(
            layer_id,
            "set_layer_locked",
            layer.locked,
            locked,
            "切换图层锁定",
        )

    def set_layer_alpha_locked(self, layer_id: UUID, locked: bool) -> bool:
        layer = self._require_document().layer_by_id(layer_id)
        return self._push_property(
            layer_id,
            "set_layer_alpha_locked",
            layer.alpha_locked,
            locked,
            "切换透明像素锁定",
        )

    def apply_pixel_tiles(
        self,
        layer_id: UUID,
        updates: Mapping[TileCoord, RGBAArray | None],
        label: str,
    ) -> bool:
        document = self._require_document()
        try:
            normalized = document.prepare_pixel_updates(layer_id, updates)
        except PermissionError, TypeError, ValueError:
            return False
        if not normalized:
            return False
        layer = document.layer_by_id(layer_id)
        patches: list[_TilePatch] = []
        for coord, after_pixels in normalized.items():
            before_pixels = (
                layer.pixels.read_tile(coord.x, coord.y)
                if layer.pixels.has_tile(coord.x, coord.y)
                else None
            )
            patches.append(
                _TilePatch(
                    coord=coord,
                    before=self.patch_store.capture(before_pixels),
                    after=self.patch_store.capture(after_pixels),
                )
            )
        return self._push(
            _PixelPatchCommand(
                document,
                layer_id,
                tuple(patches),
                self.patch_store,
                label.strip() or "像素变更",
            )
        )

    def clear_selected_pixels(self, layer_id: UUID) -> bool:
        """Make selected pixels transparent while preserving their hidden RGB."""

        document = self._require_document()
        if not document.selection.is_active:
            return False
        try:
            layer = document.layer_by_id(layer_id)
        except ValueError:
            return False
        if document.is_locked(layer.id) or layer.alpha_locked:
            return False
        updates: dict[TileCoord, RGBAArray] = {}
        from easypicture.domain.coordinates import RasterMapping
        from easypicture.domain.layer import RasterSampling

        mapping = RasterMapping(layer.world_transform, layer.size)
        for coord, before in layer.pixels.iter_allocated_tiles():
            mask = mapping.selection_coverage(
                document.selection.mask,
                document.size,
                coord.x * layer.pixels.tile_size,
                coord.y * layer.pixels.tile_size,
                layer.pixels.tile_size,
                layer.pixels.tile_size,
                smooth=layer.sampling is RasterSampling.SMOOTH,
            )
            after = before.copy()
            remaining = 1.0 - mask.astype(np.float32) / 255.0
            after[..., 3] = np.rint(before[..., 3].astype(np.float32) * remaining).astype(np.uint8)
            if not np.array_equal(before, after):
                updates[coord] = after
        return self.apply_pixel_tiles(layer_id, updates, "清除选区像素")

    def apply_selection_tiles(
        self,
        updates: Mapping[TileCoord, MaskArray | None],
        label: str,
    ) -> bool:
        document = self._require_document()
        try:
            normalized = document.prepare_selection_updates(updates)
        except TypeError, ValueError:
            return False
        if not normalized:
            return False
        selection = document.selection.mask
        patches: list[_TilePatch] = []
        for coord, after_pixels in normalized.items():
            before_pixels = (
                selection.read_tile(coord.x, coord.y)
                if selection.has_tile(coord.x, coord.y)
                else None
            )
            patches.append(
                _TilePatch(
                    coord=coord,
                    before=self.patch_store.capture_mask(before_pixels),
                    after=self.patch_store.capture_mask(after_pixels),
                )
            )
        return self._push(
            _SelectionPatchCommand(
                document,
                tuple(patches),
                self.patch_store,
                label.strip() or "选区变更",
            )
        )

    def apply_raster_transform(
        self,
        result: RasterTransformResult,
        label: str = "自由变换",
    ) -> bool:
        document = self._require_document()
        if result.document_id != document.id or result.source_revision != document.revision:
            return False
        try:
            layer = document.layer_by_id(result.layer_id)
        except ValueError:
            return False
        if document.is_locked(layer.id) or layer.alpha_locked:
            return False
        from easypicture.imaging.geometry_ops import LayerGeometryResult

        geometry = DocumentGeometryResult(
            result.document_id,
            result.source_revision,
            document.size,
            tuple(
                LayerGeometryResult(
                    item.id,
                    result.layer_size if item.id == layer.id else item.size,
                    result.layer_origin if item.id == layer.id else item.origin,
                    result.layer_tiles if item.id == layer.id else None,
                )
                for item in document.layers
            ),
            result.selection_tiles,
            object_transforms={layer.id: result.layer_transform},
        )
        return self.apply_document_geometry(geometry, label)

    def apply_document_geometry(
        self,
        result: DocumentGeometryResult,
        label: str,
        *,
        clear_sprite_frames: bool = False,
        sprite_frame_offset: PixelPoint | None = None,
        delete_out_of_bounds_sprite_frames: bool = False,
    ) -> bool:
        document = self._require_document()
        if (
            result.document_id != document.id
            or result.source_revision != document.revision
            or result.document_size.width > 8192
            or result.document_size.height > 8192
        ):
            return False
        current_ids = {layer.id for layer in document.layers}
        result_ids = {layer.layer_id for layer in result.layers}
        if current_ids != result_ids:
            return False

        layer_results = {layer.layer_id: layer for layer in result.layers}
        offset = result.vector_offset
        translation = AffineTransform.translation(offset.x, offset.y)
        vector_transforms = tuple(
            (vector.id, vector.transform, vector.transform.then(translation))
            for group in document.vector_groups
            for vector in group.children
            if isinstance(vector, (TextObject, SvgObject))
            if offset != PixelPoint()
        )
        if result.object_transforms is not None:
            vector_transforms = tuple(
                (node.id, node.transform, result.object_transforms[node.id])
                for node in walk_nodes(document.nodes)
                if not isinstance(node, Collection)
                and node.id in result.object_transforms
                and node.transform != result.object_transforms[node.id]
            )
        layers: list[_LayerGeometryPatch] = []
        selection_patches: list[_TilePatch] = []
        captured_patches: list[_TilePatch] = []
        try:
            for layer in document.layers:
                resolved = layer_results[layer.id]
                if max(resolved.size.width, resolved.size.height) > 8192:
                    raise ValueError("raster local size exceeds the project limit")
                if max(abs(resolved.origin.x), abs(resolved.origin.y)) > 8192 * 4:
                    raise ValueError("raster origin exceeds the project limit")
                pixel_patches: list[_TilePatch] = []
                if resolved.tiles is not None:
                    before_coords = {
                        coord for coord, _pixels in layer.pixels.iter_allocated_tiles()
                    }
                    for coord in sorted(
                        before_coords | set(resolved.tiles),
                        key=lambda item: (item.y, item.x),
                    ):
                        before = (
                            layer.pixels.read_tile(coord.x, coord.y)
                            if coord in before_coords
                            else None
                        )
                        after = resolved.tiles.get(coord)
                        if before is None and after is None:
                            continue
                        if (
                            before is not None
                            and after is not None
                            and np.array_equal(before, after)
                        ):
                            continue
                        patch = _TilePatch(
                            coord,
                            self.patch_store.capture(before),
                            self.patch_store.capture(after),
                        )
                        pixel_patches.append(patch)
                        captured_patches.append(patch)
                layers.append(
                    _LayerGeometryPatch(
                        layer.id,
                        layer.size,
                        resolved.size,
                        layer.origin,
                        resolved.origin,
                        tuple(pixel_patches),
                    )
                )

            before_selection_coords = {
                coord for coord, _pixels in document.selection.mask.iter_allocated_tiles()
            }
            for coord in sorted(
                before_selection_coords | set(result.selection_tiles),
                key=lambda item: (item.y, item.x),
            ):
                before_mask = (
                    document.selection.mask.read_tile(coord.x, coord.y)
                    if coord in before_selection_coords
                    else None
                )
                after_mask = result.selection_tiles.get(coord)
                if before_mask is None and after_mask is None:
                    continue
                if (
                    before_mask is not None
                    and after_mask is not None
                    and np.array_equal(before_mask, after_mask)
                ):
                    continue
                patch = _TilePatch(
                    coord,
                    self.patch_store.capture_mask(before_mask),
                    self.patch_store.capture_mask(after_mask),
                )
                selection_patches.append(patch)
                captured_patches.append(patch)
        except TypeError, ValueError:
            for patch in captured_patches:
                self.patch_store.release(patch.before)
                self.patch_store.release(patch.after)
            return False

        before_sprite_workspace = (
            document._sprite_workspace.clone()
            if document._sprite_workspace is not None
            else SpriteWorkspace(Size(1, 1), [])
        )
        after_embedded_documents = document.embedded_documents
        if clear_sprite_frames:
            after_sprite_workspace = SpriteWorkspace(
                cell_size=Size(
                    min(before_sprite_workspace.cell_size.width, result.document_size.width),
                    min(before_sprite_workspace.cell_size.height, result.document_size.height),
                ),
                frames=[],
            )
            after_embedded_documents = {}
        elif sprite_frame_offset is not None:
            translated = [
                frame.with_changes(
                    region=Rect(
                        frame.region.x + sprite_frame_offset.x,
                        frame.region.y + sprite_frame_offset.y,
                        frame.region.width,
                        frame.region.height,
                    )
                )
                for frame in before_sprite_workspace.frames
            ]
            retained = [
                frame
                for frame in translated
                if (
                    frame.region.x >= 0
                    and frame.region.y >= 0
                    and frame.region.right <= result.document_size.width
                    and frame.region.bottom <= result.document_size.height
                )
            ]
            if len(retained) != len(translated) and not delete_out_of_bounds_sprite_frames:
                for patch in captured_patches:
                    self.patch_store.release(patch.before)
                    self.patch_store.release(patch.after)
                return False
            retained_ids = {frame.id for frame in retained}
            animations = [
                animation.with_changes(
                    frame_ids=tuple(
                        frame_id for frame_id in animation.frame_ids if frame_id in retained_ids
                    )
                )
                for animation in before_sprite_workspace.animations
            ]
            cell_size = before_sprite_workspace.cell_size
            if not retained:
                cell_size = Size(
                    min(cell_size.width, result.document_size.width),
                    min(cell_size.height, result.document_size.height),
                )
            after_sprite_workspace = SpriteWorkspace(cell_size, retained, animations)
            retained_documents = {
                frame.embedded_document_id
                for frame in retained
                if frame.content_mode is SpriteFrameContentMode.DETACHED
            }
            after_embedded_documents = {
                document_id: embedded
                for document_id, embedded in document.embedded_documents.items()
                if document_id in retained_documents
            }
        else:
            after_sprite_workspace = before_sprite_workspace.clone()
            if not after_sprite_workspace.frames:
                after_sprite_workspace.cell_size = Size(
                    min(after_sprite_workspace.cell_size.width, result.document_size.width),
                    min(after_sprite_workspace.cell_size.height, result.document_size.height),
                )
            try:
                after_sprite_workspace.validate_for_canvas(result.document_size)
            except ValueError:
                for patch in captured_patches:
                    self.patch_store.release(patch.before)
                    self.patch_store.release(patch.after)
                return False

        changed = (
            document.size != result.document_size
            or bool(selection_patches)
            or bool(vector_transforms)
            or before_sprite_workspace != after_sprite_workspace
            or any(
                layer.before_size != layer.after_size
                or layer.before_origin != layer.after_origin
                or layer.pixel_patches
                for layer in layers
            )
        )
        if not changed:
            return False
        return self._push(
            _DocumentGeometryCommand(
                document,
                document.size,
                result.document_size,
                tuple(layers),
                tuple(selection_patches),
                before_sprite_workspace,
                after_sprite_workspace,
                document.embedded_documents,
                after_embedded_documents,
                self.patch_store,
                label.strip() or "几何变更",
                vector_transforms,
            )
        )

    def clear_history_preserving_modified(self) -> None:
        # A pending command may already own newly captured handles. Releasing
        # current stack commands is sufficient; do not clear those untracked blobs.
        self._clear_stack(clear_patch_store=False)
        self._suppress_clean_sync = True
        try:
            self.undo_stack.resetClean()
        finally:
            self._suppress_clean_sync = False

    def mark_saved(self, document_id: UUID, revision: int) -> bool:
        document = self._project_document
        if document is None or document.id != document_id or document.revision != revision:
            return False
        self.undo_stack.setClean()
        document.mark_clean()
        for embedded in document.embedded_documents.values():
            embedded.mark_clean()
        return True

    def _push_property(
        self,
        layer_id: UUID,
        setter_name: str,
        before: object,
        after: object,
        text: str,
    ) -> bool:
        if before == after:
            return False
        if not self._can_update_node(layer_id, {setter_name.removeprefix("set_layer_"): after}):
            return False
        return self._push(
            _LayerPropertyCommand(
                self._require_document(),
                layer_id,
                setter_name,
                before,
                after,
                text,
            )
        )

    def _can_update_node(self, node_id: UUID, changes: Mapping[str, object]) -> bool:
        document = self._require_document()
        if not document.is_locked(node_id):
            return True
        parent = document.parent_of(node_id)
        return changes == {"locked": False} and (
            parent is None or not document.is_locked(parent.id)
        )

    def _push(self, command: _DocumentCommand) -> bool:
        required = command.storage_bytes
        if required > self.undo_budget_bytes:
            self.report_command_too_large(required, self.undo_budget_bytes)
            command.release_payload()
            return False
        if self.history_bytes + required > self.undo_budget_bytes:
            if not self.confirm_history_reset(required, self.undo_budget_bytes):
                command.release_payload()
                return False
            self.clear_history_preserving_modified()

        self._commands[command.command_id] = command
        self.undo_stack.push(command)
        self._reconcile_commands()
        return True

    def _reconcile_commands(self) -> None:
        active_ids: set[UUID] = set()

        def collect_ids(command: QUndoCommand) -> None:
            command_id = getattr(command, "command_id", None)
            if isinstance(command_id, UUID):
                active_ids.add(command_id)
            for child_index in range(command.childCount()):
                collect_ids(command.child(child_index))

        for index in range(self.undo_stack.count()):
            collect_ids(self.undo_stack.command(index))
        for command_id in tuple(self._commands):
            if command_id not in active_ids:
                command = self._commands.pop(command_id)
                command.release_payload()

    def _clear_stack(self, *, clear_patch_store: bool = True) -> None:
        for command in self._commands.values():
            command.release_payload()
        self._commands.clear()
        self._suppress_clean_sync = True
        try:
            self.undo_stack.clear()
        finally:
            self._suppress_clean_sync = False
        if clear_patch_store:
            self.patch_store.clear()

    def _require_document(self) -> Document:
        if self._document is None:
            raise RuntimeError("no document is attached")
        return self._document

    def _require_project_document(self) -> Document:
        if self._project_document is None:
            raise RuntimeError("no project document is attached")
        return self._project_document

    def _on_clean_changed(self, clean: bool) -> None:
        if self._suppress_clean_sync or self._project_document is None:
            return
        if clean:
            self._project_document.mark_clean()


__all__ = [
    "DEFAULT_UNDO_BUDGET_BYTES",
    "DEFAULT_UNDO_LIMIT",
    "DocumentCommandService",
]
