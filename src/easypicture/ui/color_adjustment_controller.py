"""Lifecycle controller for non-destructive color adjustment previews."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast
from uuid import UUID

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, DocumentEvent
from easypicture.domain.document import Unsubscribe
from easypicture.domain.events import DocumentEventListener
from easypicture.imaging import (
    ColorAdjustmentParameters,
    ColorAdjustmentRequest,
    ColorAdjustmentResult,
    adjustment_changes_alpha,
    adjustment_kind,
    calculate_color_adjustment,
    sample_document_pixel,
)
from easypicture.jobs import JobHandle, JobOutcome, JobRunner, JobStatus
from easypicture.persistence import DocumentSnapshot
from easypicture.ui.canvas_view import CanvasView

_LABELS = {
    "invert": "反相",
    "threshold": "阈值",
    "brightness_contrast": "亮度/对比度",
    "gamma": "伽马",
    "curves": "曲线",
    "hsl": "色相/饱和度/明度",
    "gradient_map": "渐变映射",
    "color_replace": "颜色替换",
    "palette_map": "调色板映射",
}


class ColorAdjustmentController(QObject):
    """Own one immutable-snapshot adjustment session and its latest-wins job."""

    active_changed = Signal(bool)
    progress_changed = Signal(int, int, str)
    preview_ready = Signal(object)
    sampled_color = Signal(object)
    sampling_changed = Signal(bool)

    def __init__(
        self,
        canvas: CanvasView,
        document_provider: Callable[[], Document | None],
        commands: DocumentCommandService,
        jobs: JobRunner,
        status: Callable[[str], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._canvas = canvas
        self._document_provider = document_provider
        self._commands = commands
        self._jobs = jobs
        self._status = status
        self._snapshot: DocumentSnapshot | None = None
        self._layer_id: UUID | None = None
        self._parameters: ColorAdjustmentParameters | None = None
        self._generation = 0
        self._submitted_generation = -1
        self._result_generation = -1
        self._result: ColorAdjustmentResult | None = None
        self._job: JobHandle | None = None
        self._unsubscribe: Unsubscribe | None = None
        self._preview_enabled = True
        self._pending_apply = False
        self._committing = False
        self._sampling = False
        self._sample_merged = False
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(50)
        self._debounce.timeout.connect(self._submit_latest)
        self._canvas.viewport().installEventFilter(self)

    @property
    def active(self) -> bool:
        return self._snapshot is not None

    @property
    def parameters(self) -> ColorAdjustmentParameters | None:
        return self._parameters

    @property
    def has_exact_result(self) -> bool:
        return self._result is not None and self._result_generation == self._generation

    @property
    def running(self) -> bool:
        return self._job is not None

    @property
    def sampling(self) -> bool:
        return self._sampling

    def start(self, parameters: ColorAdjustmentParameters) -> bool:
        document = self._document_provider()
        if document is None:
            return False
        layer = document.active_layer
        if layer.locked:
            self._status("活动图层已锁定，无法执行颜色调整")
            return False
        if layer.alpha_locked and adjustment_changes_alpha(parameters):
            self._status("活动图层已锁定透明像素，无法调整 Alpha")
            return False
        if self.active:
            self.cancel()
        self._snapshot = DocumentSnapshot.capture(document)
        self._layer_id = layer.id
        self._parameters = parameters
        self._generation = 1
        self._submitted_generation = -1
        self._result_generation = -1
        self._result = None
        self._preview_enabled = True
        self._pending_apply = False
        self._unsubscribe = document.subscribe(
            cast(DocumentEventListener, self._on_document_event)
        )
        self.active_changed.emit(True)
        self._debounce.start()
        return True

    def update_parameters(self, parameters: ColorAdjustmentParameters) -> bool:
        if not self.active:
            return False
        document = self._document_provider()
        if document is None or self._layer_id is None:
            self.cancel()
            return False
        layer = document.layer_by_id(self._layer_id)
        if layer.alpha_locked and adjustment_changes_alpha(parameters):
            self._status("透明像素锁定已开启，Alpha 调整不可用")
            return False
        self._parameters = parameters
        self._generation += 1
        self._pending_apply = False
        if self._job is not None:
            self._job.cancel()
        self._debounce.start()
        return True

    def set_preview_enabled(self, enabled: bool) -> None:
        self._preview_enabled = enabled
        if not enabled:
            self._canvas.clear_pixel_preview()
        elif self.has_exact_result and self._result is not None:
            self._show_result(self._result)

    def apply(self) -> None:
        if not self.active:
            return
        self._pending_apply = True
        self._debounce.stop()
        if self.has_exact_result and self._job is None and self._result is not None:
            self._commit(self._result)
            return
        if self._job is not None:
            self._job.cancel()
            return
        self._submit_latest()

    def cancel(self, message: str | None = None) -> None:
        if not self.active:
            self.cancel_sampling()
            return
        self._debounce.stop()
        if self._job is not None:
            self._job.cancel()
            self._job = None
        self._canvas.clear_pixel_preview()
        self.cancel_sampling()
        self._reset_session()
        self.active_changed.emit(False)
        if message:
            self._status(message)

    def begin_sampling(self, *, merged: bool) -> None:
        if not self.active:
            return
        self._sampling = True
        self._sample_merged = merged
        self._canvas.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self.sampling_changed.emit(True)
        self._status("在画布中单击以吸取颜色，Esc 或右键取消")

    def cancel_sampling(self) -> None:
        if not self._sampling:
            return
        self._sampling = False
        self._canvas.viewport().unsetCursor()
        self.sampling_changed.emit(False)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self._canvas.viewport() or not self._sampling:
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)
            if key_event.key() == Qt.Key.Key_Escape:
                self.cancel_sampling()
                return True
            return False
        if event.type() != QEvent.Type.MouseButtonPress:
            return False
        mouse_event = cast(QMouseEvent, event)
        if mouse_event.button() == Qt.MouseButton.RightButton:
            self.cancel_sampling()
            return True
        if mouse_event.button() != Qt.MouseButton.LeftButton:
            return True
        document = self._document_provider()
        if document is None:
            self.cancel_sampling()
            return True
        point = self._canvas.viewport_to_document(mouse_event.position().toPoint())
        x = int(point.x)
        y = int(point.y)
        if not 0 <= x < document.width or not 0 <= y < document.height:
            self._status("画布外无法吸取颜色")
            return True
        color = sample_document_pixel(document, x, y, merged=self._sample_merged)
        self.sampled_color.emit(color)
        self.cancel_sampling()
        return True

    def _submit_latest(self) -> None:
        if (
            not self.active
            or self._snapshot is None
            or self._layer_id is None
            or self._parameters is None
        ):
            return
        if self._job is not None:
            self._job.cancel()
            return
        request = ColorAdjustmentRequest(
            self._snapshot,
            self._layer_id,
            self._parameters,
        )
        generation = self._generation
        handle = self._jobs.submit(
            lambda token, progress: calculate_color_adjustment(
                request,
                token,
                progress,
            )
        )
        self._job = handle
        self._submitted_generation = generation
        handle.progress.connect(
            lambda value, maximum, message, expected=generation: self._on_progress(
                expected,
                value,
                maximum,
                message,
            )
        )
        handle.finished.connect(
            lambda outcome, expected=generation, expected_handle=handle: self._on_finished(
                expected_handle,
                expected,
                outcome,
            )
        )

    def _on_progress(
        self,
        generation: int,
        value: int,
        maximum: int,
        message: str,
    ) -> None:
        if self.active and generation == self._submitted_generation:
            self.progress_changed.emit(value, maximum, message)

    def _on_finished(
        self,
        handle: JobHandle,
        generation: int,
        outcome: JobOutcome,
    ) -> None:
        if self._job is not handle:
            return
        self._job = None
        if not self.active:
            return
        if generation != self._generation:
            if not self._debounce.isActive():
                self._submit_latest()
            return
        if outcome.status is JobStatus.CANCELLED:
            if not self._debounce.isActive():
                self._submit_latest()
            return
        if outcome.status is JobStatus.FAILED:
            self._status(f"颜色调整失败：{outcome.error}")
            self._pending_apply = False
            return
        if not isinstance(outcome.value, ColorAdjustmentResult):
            self._status("颜色调整失败：任务返回了无效结果")
            self._pending_apply = False
            return
        result = outcome.value
        if not self._result_is_current(result):
            self.cancel("文档已变化，已丢弃过期颜色调整结果")
            return
        self._result = result
        self._result_generation = generation
        if self._preview_enabled:
            self._show_result(result)
        self.preview_ready.emit(result)
        if self._pending_apply:
            self._commit(result)

    def _show_result(self, result: ColorAdjustmentResult) -> None:
        self._canvas.replace_pixel_preview(result.layer_id, dict(result.updates))

    def _commit(self, result: ColorAdjustmentResult) -> None:
        if not self._result_is_current(result):
            self.cancel("文档已变化，无法应用颜色调整")
            return
        label = _LABELS[adjustment_kind(cast(ColorAdjustmentParameters, self._parameters)).value]
        self._committing = True
        applied = self._commands.apply_pixel_tiles(
            result.layer_id,
            result.updates,
            label,
        )
        self._committing = False
        self._canvas.clear_pixel_preview()
        self.cancel_sampling()
        self._reset_session()
        self.active_changed.emit(False)
        self._status(f"已应用{label}" if applied else f"{label}未产生变化")

    def _result_is_current(self, result: ColorAdjustmentResult) -> bool:
        document = self._document_provider()
        return (
            document is not None
            and self._snapshot is not None
            and document.id == result.document_id == self._snapshot.document_id
            and document.revision == result.source_revision == self._snapshot.revision
            and document.active_layer_id == result.layer_id == self._layer_id
        )

    def _on_document_event(self, _event: DocumentEvent) -> None:
        if self._committing or self._snapshot is None:
            return
        document = self._document_provider()
        if (
            document is None
            or document.id != self._snapshot.document_id
            or document.revision != self._snapshot.revision
            or document.active_layer_id != self._layer_id
        ):
            self.cancel("文档或活动图层已变化，颜色调整已取消")

    def _reset_session(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._snapshot = None
        self._layer_id = None
        self._parameters = None
        self._result = None
        self._result_generation = -1
        self._pending_apply = False


__all__ = ["ColorAdjustmentController"]
