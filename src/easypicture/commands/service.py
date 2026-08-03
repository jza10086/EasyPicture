"""Undoable document mutation service built on QUndoStack."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

import numpy as np
from PySide6.QtGui import QUndoCommand, QUndoStack

from easypicture.commands.patch_store import PatchHandle, UndoPatchStore
from easypicture.domain import (
    Document,
    FloatRect,
    LayerGroup,
    PixelPoint,
    RasterGroupLayer,
    RasterLayer,
    Size,
    SvgElementObject,
    TextObject,
    VectorGroupLayer,
    VectorObject,
)
from easypicture.domain.document import MAX_DOCUMENT_LAYERS
from easypicture.imaging.geometry_ops import DocumentGeometryResult
from easypicture.imaging.transform import RasterTransformResult
from easypicture.imaging.vector_rasterize import rasterize_vector_objects
from easypicture.pixels import MaskArray, RGBAArray, TileCoord

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
    names: set[str] = set()
    for group in document.nodes:
        if group.id not in excluded:
            names.add(group.name)
        for child in group.children:
            if child.id not in excluded:
                names.add(child.name)
    return names


def _clone_group(
    group: RasterGroupLayer | VectorGroupLayer,
) -> RasterGroupLayer | VectorGroupLayer:
    """Clone a group for a structural undo snapshot without duplicating IDs."""
    clone: RasterGroupLayer | VectorGroupLayer
    if isinstance(group, RasterGroupLayer):
        children = [replace(child, pixels=child.pixels.clone_shared()) for child in group.children]
        clone = replace(group, children=children)
        clone._revision = group.revision
        for original_text, copied_text in zip(group.children, clone.children, strict=True):
            copied_text._revision = original_text.revision
    else:
        clone = replace(group, children=[replace(child) for child in group.children])
        clone._revision = group.revision
        for original_object, copied_object in zip(group.children, clone.children, strict=True):
            copied_object._revision = original_object.revision
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
        layer: RasterLayer,
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


class _DeleteLayerCommand(_DocumentCommand):
    def __init__(self, document: Document, layer: RasterLayer, index: int) -> None:
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
        group: RasterGroupLayer | VectorGroupLayer,
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


class _InsertGroupsCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        groups: tuple[RasterGroupLayer | VectorGroupLayer, ...],
        index: int,
        text: str,
    ) -> None:
        storage_bytes = sum(
            child.pixels.allocated_bytes
            for group in groups
            if isinstance(group, RasterGroupLayer)
            for child in group.children
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
        group: RasterGroupLayer | VectorGroupLayer,
        index: int,
    ) -> None:
        storage_bytes = sum(
            child.pixels.allocated_bytes
            for child in group.children
            if isinstance(child, RasterLayer)
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
        group: RasterGroupLayer | VectorGroupLayer,
        target_group: RasterGroupLayer | VectorGroupLayer | None,
        replacement_group: RasterGroupLayer | VectorGroupLayer | None,
        text: str,
    ) -> None:
        storage_bytes = sum(
            child.pixels.allocated_bytes
            for child in group.children
            if isinstance(child, RasterLayer)
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


class _ReplaceGroupStructureCommand(_DocumentCommand):
    def __init__(
        self,
        document: Document,
        before_nodes: tuple[LayerGroup, ...],
        after_nodes: tuple[LayerGroup, ...],
        before_active_id: UUID,
        after_active_id: UUID,
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
        self.document.restore_group_structure(self.after_nodes, self.after_active_id)

    def undo(self) -> None:
        self.document.restore_group_structure(self.before_nodes, self.before_active_id)


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
        patch_store: UndoPatchStore,
        text: str,
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
        self.patch_store = patch_store

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
        self.undo_stack.setUndoLimit(DEFAULT_UNDO_LIMIT)
        self.patch_store = UndoPatchStore()
        self._document: Document | None = None
        self._commands: dict[UUID, _DocumentCommand] = {}
        self._suppress_clean_sync = False
        self._visibility_macro_open = False
        self.undo_stack.cleanChanged.connect(self._on_clean_changed)

    @property
    def document(self) -> Document | None:
        return self._document

    @property
    def history_bytes(self) -> int:
        return sum(command.storage_bytes for command in self._commands.values())

    def attach_document(self, document: Document | None) -> None:
        if self._visibility_macro_open:
            self.end_visibility_scrub()
        self._clear_stack()
        self._document = document
        self._suppress_clean_sync = True
        try:
            if document is not None and document.modified:
                self.undo_stack.resetClean()
            else:
                self.undo_stack.setClean()
        finally:
            self._suppress_clean_sync = False

    def create_layer(self) -> bool:
        document = self._require_document()
        if len(document.layers) >= MAX_DOCUMENT_LAYERS:
            return False
        names = _document_names(document)
        number = 1
        while f"图层 {number}" in names:
            number += 1
        layer = RasterLayer.create_blank(f"图层 {number}", document.size)
        index = document.layer_index(document.active_layer_id) + 1
        return self._push(_InsertLayerCommand(document, layer, index, "新建图层"))

    def create_raster_group(self, name: str = "栅格组") -> bool:
        document = self._require_document()
        if len(document.nodes) >= MAX_DOCUMENT_LAYERS:
            return False
        group_name = _unique_name(name, _document_names(document))
        group = RasterGroupLayer.create(group_name)
        return self._push(_InsertGroupCommand(document, group, len(document.nodes), "新建栅格组"))

    def create_vector_group(self, name: str = "矢量组") -> bool:
        document = self._require_document()
        if len(document.nodes) >= MAX_DOCUMENT_LAYERS:
            return False
        group_name = _unique_name(name, _document_names(document))
        group = VectorGroupLayer.create(group_name)
        return self._push(_InsertGroupCommand(document, group, len(document.nodes), "新建矢量组"))

    def append_groups(
        self,
        groups: tuple[RasterGroupLayer | VectorGroupLayer, ...],
        label: str = "追加文件",
    ) -> bool:
        """Append prepared groups as one undoable document operation."""
        document = self._require_document()
        if not groups:
            return False
        if len(document.nodes) + len(groups) > MAX_DOCUMENT_LAYERS:
            return False
        raster_count = sum(
            len(group.children) for group in groups if isinstance(group, RasterGroupLayer)
        )
        if len(document.layers) + raster_count > MAX_DOCUMENT_LAYERS:
            return False
        return self._push(_InsertGroupsCommand(document, groups, len(document.nodes), label))

    def delete_group(self, group_id: UUID) -> bool:
        document = self._require_document()
        try:
            group = document.layer_group_by_id(group_id)
            index = document.nodes.index(group)
        except ValueError:
            return False
        try:
            if isinstance(group, RasterGroupLayer) and len(group.children) == len(document.layers):
                return False
        except ValueError:
            return False
        return self._push(_DeleteGroupCommand(document, group, index))

    def duplicate_group(self, group_id: UUID) -> bool:
        document = self._require_document()
        try:
            group = document.layer_group_by_id(group_id)
        except ValueError:
            return False
        duplicate = group.duplicate(_unique_name(group.name, _document_names(document)))
        used_names = _document_names(document)
        used_names.add(duplicate.name)
        if isinstance(group, RasterGroupLayer) and isinstance(duplicate, RasterGroupLayer):
            for raster_original, raster_child in zip(
                group.children,
                duplicate.children,
                strict=True,
            ):
                raster_child.name = _unique_name(raster_original.name, used_names)
                used_names.add(raster_child.name)
        elif isinstance(group, VectorGroupLayer) and isinstance(duplicate, VectorGroupLayer):
            for vector_original, vector_child in zip(
                group.children,
                duplicate.children,
                strict=True,
            ):
                vector_child.name = _unique_name(vector_original.name, used_names)
                used_names.add(vector_child.name)
        else:
            return False
        return self._push(
            _InsertGroupCommand(
                document,
                duplicate,
                document.nodes.index(group) + 1,
                "复制图层组",
                storage_bytes=sum(
                    child.pixels.allocated_bytes
                    for child in duplicate.children
                    if isinstance(child, RasterLayer)
                ),
            )
        )

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
        except ValueError:
            return False
        source_index = document.nodes.index(group)
        target: RasterGroupLayer | VectorGroupLayer | None = None
        for candidate_index in (source_index - 1, source_index + 1):
            if not 0 <= candidate_index < len(document.nodes):
                continue
            candidate = document.nodes[candidate_index]
            if type(candidate) is type(group):
                target = candidate
                break
        replacement: RasterGroupLayer | VectorGroupLayer | None = None
        if target is None:
            names = _document_names(document, exclude={group.id})
            base_name = "栅格组" if isinstance(group, RasterGroupLayer) else "矢量组"
            replacement_name = _unique_name(base_name, names)
            replacement = (
                RasterGroupLayer.create(replacement_name)
                if isinstance(group, RasterGroupLayer)
                else VectorGroupLayer.create(replacement_name)
            )
        return self._push(
            _UngroupCommand(
                document,
                group,
                target,
                replacement,
                "取消分组",
            )
        )

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
        if not isinstance(target_group, RasterGroupLayer):
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
        document = self._require_document()
        if len(document.layers) == 1:
            return False
        layer = document.layer_by_id(layer_id)
        return self._push(_DeleteLayerCommand(document, layer, document.layer_index(layer_id)))

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
        svg_object: SvgElementObject,
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
        svg_object: SvgElementObject,
        *,
        group_name: str = "矢量组",
        label: str = "创建矢量图形",
    ) -> bool:
        """Insert a new vector group and its first object as one undo step."""
        document = self._require_document()
        if len(document.nodes) >= MAX_DOCUMENT_LAYERS:
            return False
        names = _document_names(document)
        group = VectorGroupLayer.create(_unique_name(group_name, names))
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
        if (group.locked or text_object.locked) and set(changes) != {"locked"}:
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
        if (group.locked or vector_object.locked) and set(changes) != {"locked"}:
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
        document = self._require_document()
        try:
            live_group = document.vector_group_by_id(group_id)
        except ValueError:
            return False
        if live_group.locked:
            return False
        before_nodes = tuple(_clone_group(node) for node in document.nodes)
        after_nodes = [_clone_group(node) for node in document.nodes]
        index = next(position for position, node in enumerate(after_nodes) if node.id == group_id)
        group = after_nodes[index]
        if not isinstance(group, VectorGroupLayer):
            return False
        store = rasterize_vector_objects(
            tuple(group.children),
            document.size,
            opacity=group.opacity,
        )
        existing_names = _document_names(document)
        layer_name = _unique_name(f"{group.name} 栅格", existing_names)
        layer = RasterLayer(
            id=uuid4(),
            name=layer_name,
            size=document.size,
            origin=PixelPoint(),
            pixels=store,
        )
        after_nodes[index] = RasterGroupLayer(
            id=group.id,
            name=group.name,
            children=[layer],
            visible=group.visible,
            opacity=1.0,
            locked=group.locked,
        )
        return self._push(
            _ReplaceGroupStructureCommand(
                document,
                before_nodes,
                tuple(after_nodes),
                document.active_layer_id,
                layer.id,
                "栅格化矢量组",
                storage_bytes=store.allocated_bytes,
            )
        )

    def rasterize_vector_object(self, object_id: UUID) -> bool:
        document = self._require_document()
        try:
            live_group, live_object = document.vector_object_by_id(object_id)
        except ValueError:
            return False
        if live_group.locked or live_object.locked:
            return False
        before_nodes = tuple(_clone_group(node) for node in document.nodes)
        after_nodes = [_clone_group(node) for node in document.nodes]
        root_index = next(
            position for position, node in enumerate(after_nodes) if node.id == live_group.id
        )
        group = after_nodes[root_index]
        if not isinstance(group, VectorGroupLayer):
            return False
        vector_object = group.child_by_id(object_id)
        child_index = group.children.index(vector_object)
        lower_children = list(group.children[:child_index])
        upper_children = list(group.children[child_index + 1 :])
        store = rasterize_vector_objects(
            (vector_object,),
            document.size,
            include_hidden=True,
        )
        names = _document_names(document)
        raster_group_name = _unique_name(f"{vector_object.name} 栅格组", names)
        names.add(raster_group_name)
        raster_layer = RasterLayer(
            id=uuid4(),
            name=_unique_name(f"{vector_object.name} 栅格", names),
            size=document.size,
            origin=PixelPoint(),
            pixels=store,
            visible=vector_object.visible,
            locked=vector_object.locked,
        )
        raster_group = RasterGroupLayer(
            id=uuid4(),
            name=raster_group_name,
            children=[raster_layer],
            visible=group.visible,
            opacity=group.opacity,
            locked=group.locked,
        )
        replacement: list[LayerGroup] = []
        original_id_used = False
        if lower_children:
            replacement.append(replace(group, children=lower_children))
            original_id_used = True
        replacement.append(raster_group)
        if upper_children:
            upper_name = _unique_name(f"{group.name}.1", names | {raster_group_name})
            replacement.append(
                replace(
                    group,
                    id=uuid4() if original_id_used else group.id,
                    name=upper_name if original_id_used else group.name,
                    children=upper_children,
                )
            )
        after_nodes[root_index : root_index + 1] = replacement
        return self._push(
            _ReplaceGroupStructureCommand(
                document,
                before_nodes,
                tuple(after_nodes),
                document.active_layer_id,
                raster_layer.id,
                "栅格化矢量子图层",
                storage_bytes=store.allocated_bytes,
            )
        )

    def _push_group_property(
        self,
        group: RasterGroupLayer | VectorGroupLayer,
        property_name: str,
        value: object,
        label: str,
    ) -> bool:
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
        if layer.locked or layer.alpha_locked:
            return False
        updates: dict[TileCoord, RGBAArray] = {}
        for coord, mask in document.selection.mask.iter_allocated_tiles():
            if not layer.pixels.has_tile(coord.x, coord.y):
                continue
            before = layer.pixels.read_tile(coord.x, coord.y)
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
        if layer.locked or layer.alpha_locked:
            return False
        if result.layer_size != document.size or result.layer_origin != PixelPoint():
            return False

        layer_patches: list[_TilePatch] = []
        selection_patches: list[_TilePatch] = []
        try:
            before_layer_coords = {coord for coord, _pixels in layer.pixels.iter_allocated_tiles()}
            for coord in sorted(
                before_layer_coords | set(result.layer_tiles),
                key=lambda item: (item.y, item.x),
            ):
                before = (
                    layer.pixels.read_tile(coord.x, coord.y)
                    if coord in before_layer_coords
                    else None
                )
                after = result.layer_tiles.get(coord)
                if before is None and after is None:
                    continue
                if before is not None and after is not None and np.array_equal(before, after):
                    continue
                layer_patches.append(
                    _TilePatch(
                        coord,
                        self.patch_store.capture(before),
                        self.patch_store.capture(after),
                    )
                )

            if document.selection.is_active or result.selection_tiles:
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
                    selection_patches.append(
                        _TilePatch(
                            coord,
                            self.patch_store.capture_mask(before_mask),
                            self.patch_store.capture_mask(after_mask),
                        )
                    )
        except TypeError, ValueError:
            for patch in layer_patches + selection_patches:
                self.patch_store.release(patch.before)
                self.patch_store.release(patch.after)
            return False

        if (
            not layer_patches
            and not selection_patches
            and layer.size == result.layer_size
            and layer.origin == result.layer_origin
        ):
            return False
        return self._push(
            _RasterTransformCommand(
                document,
                result.layer_id,
                layer.size,
                result.layer_size,
                layer.origin,
                result.layer_origin,
                tuple(layer_patches),
                tuple(selection_patches),
                self.patch_store,
                label.strip() or "自由变换",
            )
        )

    def apply_document_geometry(
        self,
        result: DocumentGeometryResult,
        label: str,
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
        layers: list[_LayerGeometryPatch] = []
        selection_patches: list[_TilePatch] = []
        captured_patches: list[_TilePatch] = []
        try:
            for layer in document.layers:
                resolved = layer_results[layer.id]
                if resolved.size != result.document_size or resolved.origin != PixelPoint():
                    raise ValueError("layers must match the document canvas")
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

        changed = (
            document.size != result.document_size
            or bool(selection_patches)
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
                self.patch_store,
                label.strip() or "几何变更",
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
        document = self._document
        if document is None or document.id != document_id or document.revision != revision:
            return False
        self.undo_stack.setClean()
        document.mark_clean()
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

    def _on_clean_changed(self, clean: bool) -> None:
        if self._suppress_clean_sync or self._document is None:
            return
        if clean:
            self._document.mark_clean()


__all__ = [
    "DEFAULT_UNDO_BUDGET_BYTES",
    "DEFAULT_UNDO_LIMIT",
    "DocumentCommandService",
]
