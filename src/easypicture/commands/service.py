"""Undoable document mutation service built on QUndoStack."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

from PySide6.QtGui import QUndoCommand, QUndoStack

from easypicture.commands.patch_store import PatchHandle, UndoPatchStore
from easypicture.domain import Document, RasterLayer
from easypicture.pixels import RGBAArray, TileCoord

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
        self.undo_stack.cleanChanged.connect(self._on_clean_changed)

    @property
    def document(self) -> Document | None:
        return self._document

    @property
    def history_bytes(self) -> int:
        return sum(command.storage_bytes for command in self._commands.values())

    def attach_document(self, document: Document | None) -> None:
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

    def clear_history_preserving_modified(self) -> None:
        # A pending command may already own newly captured handles. Releasing
        # current stack commands is sufficient; do not clear those untracked blobs.
        self._clear_stack(clear_patch_store=False)
        self._suppress_clean_sync = True
        try:
            self.undo_stack.resetClean()
        finally:
            self._suppress_clean_sync = False

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
        for index in range(self.undo_stack.count()):
            command = self.undo_stack.command(index)
            command_id = getattr(command, "command_id", None)
            if isinstance(command_id, UUID):
                active_ids.add(command_id)
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
