"""Rectangle and magic-wand selection tools."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import QGraphicsRectItem, QProgressDialog

from easypicture.imaging import (
    BACKGROUND_SELECTION_PIXEL_THRESHOLD,
    MagicSelectionRequest,
    RegionQuery,
    SelectionBulkOperation,
    SelectionBulkRequest,
    SelectionCombineMode,
    SelectionOperationResult,
    calculate_bulk_selection,
    calculate_magic_selection,
    combine_selection_tiles,
    rectangle_candidate,
)
from easypicture.jobs import JobHandle, JobOutcome, JobStatus
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import Rect
from easypicture.tools.protocol import ToolContext


@dataclass(frozen=True, slots=True)
class MagicWandSettings:
    tolerance: int = 0
    connectivity: Literal[4, 8] = 4
    contiguous: bool = True
    sample_merged: bool = False
    include_alpha: bool = True
    mode: SelectionCombineMode = SelectionCombineMode.REPLACE

    def __post_init__(self) -> None:
        if not 0 <= self.tolerance <= 255:
            raise ValueError("magic-wand tolerance must be between 0 and 255")
        if self.connectivity not in (4, 8):
            raise ValueError("magic-wand connectivity must be 4 or 8")


def mode_from_modifiers(
    default: SelectionCombineMode,
    modifiers: Qt.KeyboardModifier,
) -> SelectionCombineMode:
    shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
    alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
    if shift and alt:
        return SelectionCombineMode.INTERSECT
    if shift:
        return SelectionCombineMode.ADD
    if alt:
        return SelectionCombineMode.SUBTRACT
    return default


class RectangleSelectionTool:
    def __init__(self, mode_provider: Callable[[], SelectionCombineMode]) -> None:
        self._mode_provider = mode_provider
        self._context: ToolContext | None = None
        self._start_document: QPointF | None = None
        self._start_viewport: QPointF | None = None
        self._mode = SelectionCombineMode.REPLACE
        self._preview: QGraphicsRectItem | None = None
        self._handle: JobHandle | None = None
        self._progress: QProgressDialog | None = None

    def activate(self, context: ToolContext) -> None:
        self._context = context
        context.canvas.set_tool_cursor(Qt.CursorShape.CrossCursor)

    def deactivate(self) -> None:
        context = self._context
        self._cancel()
        self._cancel_job()
        if context is not None:
            if self._handle is not None and context.set_busy is not None:
                context.set_busy(False)
            context.canvas.set_tool_cursor(None)
        self._handle = None
        self._context = None

    def mouse_press(self, event: QMouseEvent) -> bool:
        if (
            self._context is None
            or event.button() != Qt.MouseButton.LeftButton
            or self._handle is not None
        ):
            return False
        if self._context.document_provider() is None:
            return False
        point = self._context.canvas.viewport_to_document(event.position().toPoint())
        self._start_document = QPointF(point.x, point.y)
        self._start_viewport = QPointF(event.position())
        self._mode = mode_from_modifiers(self._mode_provider(), event.modifiers())
        self._preview = QGraphicsRectItem()
        pen = QPen(QColor("#5b9bd5"))
        pen.setCosmetic(True)
        pen.setWidth(1)
        pen.setStyle(Qt.PenStyle.DashLine)
        self._preview.setPen(pen)
        self._preview.setBrush(Qt.BrushStyle.NoBrush)
        self._context.overlays.add(self._preview)
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        if self._context is None or self._start_document is None or self._preview is None:
            return False
        point = self._context.canvas.viewport_to_document(event.position().toPoint())
        self._preview.setRect(
            QRectF(self._start_document, QPointF(point.x, point.y)).normalized()
        )
        return True

    def mouse_release(self, event: QMouseEvent) -> bool:
        if (
            self._context is None
            or self._start_document is None
            or self._start_viewport is None
            or event.button() != Qt.MouseButton.LeftButton
        ):
            return False
        start_document = QPointF(self._start_document)
        start_viewport = QPointF(self._start_viewport)
        mode = self._mode
        point = self._context.canvas.viewport_to_document(event.position().toPoint())
        self._cancel()
        if (QPointF(event.position()) - start_viewport).manhattanLength() < 1.0:
            return True
        document = self._context.document_provider()
        commands = self._context.commands
        if document is None or commands is None:
            return True
        left = max(0, math.floor(min(start_document.x(), point.x)))
        top = max(0, math.floor(min(start_document.y(), point.y)))
        right = min(document.width, math.ceil(max(start_document.x(), point.x)))
        bottom = min(document.height, math.ceil(max(start_document.y(), point.y)))
        if right <= left or bottom <= top:
            return True
        rect = Rect(left, top, right - left, bottom - top)
        if (
            rect.width * rect.height > BACKGROUND_SELECTION_PIXEL_THRESHOLD
            and self._context.jobs is not None
        ):
            request = SelectionBulkRequest(
                DocumentSnapshot.capture(document),
                SelectionBulkOperation.RECTANGLE,
                mode,
                rect,
            )
            handle = self._context.jobs.submit(
                lambda token, progress: calculate_bulk_selection(
                    request,
                    token,
                    progress,
                )
            )
            self._handle = handle
            self._show_progress(handle)
            if self._context.set_busy is not None:
                self._context.set_busy(True)
            handle.finished.connect(self._on_finished)
            return True
        candidate = rectangle_candidate(document.size, rect)
        updates = combine_selection_tiles(
            document.selection.mask,
            candidate,
            document.size,
            mode,
        )
        if not commands.apply_selection_tiles(updates, "矩形选区"):
            self._status("矩形选区未产生变化")
        return True

    def key_press(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Escape and (
            self._start_document is not None or self._handle is not None
        ):
            if self._start_document is not None:
                self._cancel()
            if self._handle is not None:
                self._cancel_job()
            return True
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        _ = event
        return False

    def _cancel(self) -> None:
        if self._context is not None and self._preview is not None:
            self._context.overlays.clear()
        self._preview = None
        self._start_document = None
        self._start_viewport = None

    def _status(self, message: str) -> None:
        if self._context is not None and self._context.status is not None:
            self._context.status(message)

    def _show_progress(self, handle: JobHandle) -> None:
        if self._context is None:
            return
        dialog = QProgressDialog("正在生成矩形选区…", "取消", 0, 0, self._context.canvas)
        dialog.setWindowTitle("矩形选区")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(150)
        dialog.setAutoClose(False)
        dialog.canceled.connect(handle.cancel)
        handle.progress.connect(
            lambda value, maximum, message: MagicWandTool._update_progress(
                dialog,
                value,
                maximum,
                message,
            )
        )
        self._progress = dialog

    def _on_finished(self, outcome: JobOutcome) -> None:
        context = self._context
        self._close_progress()
        self._handle = None
        if context is None:
            return
        if context.set_busy is not None:
            context.set_busy(False)
        if outcome.status is JobStatus.CANCELLED:
            self._status("已取消矩形选区")
            return
        if outcome.status is JobStatus.FAILED:
            self._status(f"矩形选区失败：{outcome.error}")
            return
        if not isinstance(outcome.value, SelectionOperationResult):
            self._status("矩形选区失败：无效的任务结果")
            return
        result = outcome.value
        document = context.document_provider()
        if (
            document is None
            or document.id != result.document_id
            or document.revision != result.revision
        ):
            self._status("文档已变化，已丢弃过期矩形选区结果")
            return
        if context.commands is None or not context.commands.apply_selection_tiles(
            result.updates,
            "矩形选区",
        ):
            self._status("矩形选区未产生变化")

    def _cancel_job(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
        self._close_progress()

    def _close_progress(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress.deleteLater()
            self._progress = None


class MagicWandTool:
    def __init__(self, settings_provider: Callable[[], MagicWandSettings]) -> None:
        self._settings_provider = settings_provider
        self._context: ToolContext | None = None
        self._handle: JobHandle | None = None
        self._progress: QProgressDialog | None = None

    def activate(self, context: ToolContext) -> None:
        self._context = context
        context.canvas.set_tool_cursor(Qt.CursorShape.CrossCursor)

    def deactivate(self) -> None:
        context = self._context
        self._cancel_job()
        if context is not None:
            if self._handle is not None and context.set_busy is not None:
                context.set_busy(False)
            context.canvas.set_tool_cursor(None)
        self._handle = None
        self._context = None

    def mouse_press(self, event: QMouseEvent) -> bool:
        if (
            self._context is None
            or event.button() != Qt.MouseButton.LeftButton
            or self._handle is not None
        ):
            return False
        document = self._context.document_provider()
        if document is None or self._context.jobs is None:
            return False
        point = self._context.canvas.viewport_to_document(event.position().toPoint())
        seed = (math.floor(point.x), math.floor(point.y))
        if not 0 <= seed[0] < document.width or not 0 <= seed[1] < document.height:
            return False
        settings = self._settings_provider()
        request = MagicSelectionRequest(
            snapshot=DocumentSnapshot.capture(document),
            layer_id=document.active_layer_id,
            query=RegionQuery(
                seed=seed,
                tolerance=settings.tolerance,
                connectivity=settings.connectivity,
                contiguous=settings.contiguous,
                include_alpha=settings.include_alpha,
            ),
            mode=mode_from_modifiers(settings.mode, event.modifiers()),
            sample_merged=settings.sample_merged,
        )
        handle = self._context.jobs.submit(
            lambda token, progress: calculate_magic_selection(request, token, progress)
        )
        self._handle = handle
        self._show_progress(handle)
        if self._context.set_busy is not None:
            self._context.set_busy(True)
        handle.finished.connect(self._on_finished)
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        _ = event
        return False

    def mouse_release(self, event: QMouseEvent) -> bool:
        _ = event
        return False

    def key_press(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Escape and self._handle is not None:
            self._cancel_job()
            return True
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        _ = event
        return False

    def _show_progress(self, handle: JobHandle) -> None:
        if self._context is None:
            return
        dialog = QProgressDialog("正在计算选区…", "取消", 0, 0, self._context.canvas)
        dialog.setWindowTitle("魔棒")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(150)
        dialog.setAutoClose(False)
        dialog.canceled.connect(handle.cancel)
        handle.progress.connect(
            lambda value, maximum, message: self._update_progress(
                dialog,
                value,
                maximum,
                message,
            )
        )
        self._progress = dialog

    @staticmethod
    def _update_progress(
        dialog: QProgressDialog,
        value: int,
        maximum: int,
        message: str,
    ) -> None:
        dialog.setLabelText(message)
        dialog.setMaximum(maximum)
        dialog.setValue(value)

    def _on_finished(self, outcome: JobOutcome) -> None:
        context = self._context
        self._close_progress()
        self._handle = None
        if context is None:
            return
        if context.set_busy is not None:
            context.set_busy(False)
        if outcome.status is JobStatus.CANCELLED:
            self._status("已取消魔棒选区")
            return
        if outcome.status is JobStatus.FAILED:
            self._status(f"魔棒失败：{outcome.error}")
            return
        if not isinstance(outcome.value, SelectionOperationResult):
            self._status("魔棒失败：无效的任务结果")
            return
        result = outcome.value
        document = context.document_provider()
        if (
            document is None
            or document.id != result.document_id
            or document.revision != result.revision
        ):
            self._status("文档已变化，已丢弃过期魔棒结果")
            return
        if context.commands is None or not context.commands.apply_selection_tiles(
            result.updates,
            "魔棒选区",
        ):
            self._status("魔棒选区未产生变化")

    def _cancel_job(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
        self._close_progress()

    def _close_progress(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress.deleteLater()
            self._progress = None

    def _status(self, message: str) -> None:
        if self._context is not None and self._context.status is not None:
            self._context.status(message)


__all__ = [
    "MagicWandSettings",
    "MagicWandTool",
    "RectangleSelectionTool",
    "mode_from_modifiers",
]
