"""Undoable document mutation service built on QUndoStack."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

import numpy as np
from PySide6.QtGui import QUndoCommand, QUndoStack

from easypicture.commands.patch_store import PatchHandle, UndoPatchStore
from easypicture.domain import Document, PixelPoint, RasterLayer, Size
from easypicture.domain.document import MAX_DOCUMENT_LAYERS
from easypicture.imaging.geometry_ops import DocumentGeometryResult
from easypicture.imaging.transform import RasterTransformResult
from easypicture.pixels import MaskArray, RGBAArray, TileCoord

DEFAULT_UNDO_BUDGET_BYTES = 1024 * 1024 * 1024
DEFAULT_UNDO_LIMIT = 100
ConfirmHistoryReset = Callable[[int, int], bool]
ReportCommandTooLarge = Callable[[int, int], None]


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
        ) + sum(
            patch.before.stored_bytes + patch.after.stored_bytes
            for patch in selection_patches
        )
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
                patch.coord: self.patch_store.restore(
                    patch.after if use_after else patch.before
                )
                for patch in layer.pixel_patches
            }
            for layer in self.layers
            if layer.pixel_patches
        }
        selection_updates = {
            patch.coord: self.patch_store.restore(
                patch.after if use_after else patch.before
            )
            for patch in self.selection_patches
        }
        self.document.apply_geometry_transaction(
            document_size=(
                self.after_document_size
                if use_after
                else self.before_document_size
            ),
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
        names = {layer.name for layer in document.layers}
        number = 1
        while f"图层 {number}" in names:
            number += 1
        layer = RasterLayer.create_blank(f"图层 {number}", document.size)
        index = document.layer_index(document.active_layer_id) + 1
        return self._push(_InsertLayerCommand(document, layer, index, "新建图层"))

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
            after[..., 3] = np.rint(
                before[..., 3].astype(np.float32) * remaining
            ).astype(np.uint8)
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
        if (
            result.document_id != document.id
            or result.source_revision != document.revision
        ):
            return False
        try:
            layer = document.layer_by_id(result.layer_id)
        except ValueError:
            return False
        if layer.locked or layer.alpha_locked:
            return False
        if (
            result.layer_size != document.size
            or result.layer_origin != PixelPoint()
        ):
            return False

        layer_patches: list[_TilePatch] = []
        selection_patches: list[_TilePatch] = []
        try:
            before_layer_coords = {
                coord for coord, _pixels in layer.pixels.iter_allocated_tiles()
            }
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
                    coord
                    for coord, _pixels in document.selection.mask.iter_allocated_tiles()
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
        except (TypeError, ValueError):
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
                if (
                    resolved.size != result.document_size
                    or resolved.origin != PixelPoint()
                ):
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
        except (TypeError, ValueError):
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
