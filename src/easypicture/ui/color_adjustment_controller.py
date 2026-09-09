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
from easypicture.imaging.ai_pixel_cleanup import (
    AiPixelCleanupCache,
    AiPixelCleanupParameters,
    AiPixelCleanupRequest,
    AiPixelCleanupResult,
    PixelCleanupOutput,
    calculate_ai_pixel_cleanup,
)
from easypicture.imaging.palette_extraction import (
    PaletteExtractionRequest,
    PaletteExtractionResult,
    extract_layer_palette,
)
from easypicture.imaging.pixel_outline import (
    PixelOutlineParameters,
    PixelOutlineRequest,
    PixelOutlineResult,
    calculate_pixel_outline,
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

type AdjustmentParameters = (
    ColorAdjustmentParameters | PixelOutlineParameters | AiPixelCleanupParameters
)
type AdjustmentResult = ColorAdjustmentResult | PixelOutlineResult | AiPixelCleanupResult


class ColorAdjustmentController(QObject):
    """Own one immutable-snapshot adjustment session and its latest-wins job."""

    active_changed = Signal(bool)
    progress_changed = Signal(int, int, str)
    preview_ready = Signal(object)
    sampled_color = Signal(object)
    sampling_changed = Signal(bool)
    palette_extracted = Signal(object)
    applying_changed = Signal(bool)
    ai_project_requested = Signal(object)

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
        self._parameters: AdjustmentParameters | None = None
        self._extraction_target: int | None = None
        self._generation = 0
        self._submitted_generation = -1
        self._result_generation = -1
        self._result: AdjustmentResult | None = None
        self._job: JobHandle | None = None
        self._unsubscribe: Unsubscribe | None = None
        self._preview_enabled = True
        self._pending_apply = False
        self._committing = False
        self._sampling = False
        self._ai_cache = AiPixelCleanupCache()
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
    def parameters(self) -> AdjustmentParameters | None:
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

    def start(self, parameters: AdjustmentParameters) -> bool:
        document = self._document_provider()
        if document is None:
            return False
        layer = document.active_layer
        if layer.locked or document.raster_group_for_layer(layer.id).locked:
            self._status("活动图层已锁定，无法执行颜色调整")
            return False
        if (
            layer.alpha_locked
            and not isinstance(parameters, (PixelOutlineParameters, AiPixelCleanupParameters))
            and adjustment_changes_alpha(parameters)
        ):
            self._status("活动图层已锁定透明像素，无法调整 Alpha")
            return False
        if self.active:
            self.cancel()
        self._snapshot = DocumentSnapshot.capture(document)
        self._ai_cache = AiPixelCleanupCache()
        self._layer_id = layer.id
        self._parameters = parameters
        self._generation += 1
        self._submitted_generation = -1
        self._result_generation = -1
        self._result = None
        self._preview_enabled = True
        self._pending_apply = False
        self._unsubscribe = document.subscribe(cast(DocumentEventListener, self._on_document_event))
        self.active_changed.emit(True)
        self._debounce.start()
        return True

    def update_parameters(self, parameters: AdjustmentParameters) -> bool:
        if not self.active or self._pending_apply:
            return False
        document = self._document_provider()
        if document is None or self._layer_id is None:
            self.cancel()
            return False
        layer = document.layer_by_id(self._layer_id)
        if (
            layer.alpha_locked
            and not isinstance(parameters, (PixelOutlineParameters, AiPixelCleanupParameters))
            and adjustment_changes_alpha(parameters)
        ):
            self._status("透明像素锁定已开启，Alpha 调整不可用")
            return False
        self._parameters = parameters
        self._extraction_target = None
        self._generation += 1
        self._pending_apply = False
        if self._job is not None:
            self._job.cancel()
        self._debounce.start()
        return True

    def extract_palette(self, target: int) -> None:
        if not self.active or isinstance(
            self._parameters, (PixelOutlineParameters, AiPixelCleanupParameters)
        ):
            return
        if not 1 <= target <= 256:
            return
        self._extraction_target = target
        self._generation += 1
        self._pending_apply = False
        if self._job is not None:
            self._job.cancel()
        self._debounce.start()

    def _clear_preview(self) -> None:
        self._canvas.clear_pixel_preview()
        self._canvas.render_coordinator.clear_raster_layer_preview()

    def set_preview_enabled(self, enabled: bool) -> None:
        self._preview_enabled = enabled
        if not enabled:
            self._clear_preview()
        elif self._result is not None:
            self._show_result(self._result)

    def apply(self) -> None:
        if not self.active:
            return
        self._pending_apply = True
        self.applying_changed.emit(True)
        self._debounce.stop()
        if self.has_exact_result and self._job is None and self._result is not None:
            self._commit(self._result)
            return
        if self._job is not None:
            if self._submitted_generation != self._generation:
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
        self._generation += 1
        self._clear_preview()
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
        snapshot, layer_id, parameters = self._snapshot, self._layer_id, self._parameters
        extraction_target = self._extraction_target
        generation = self._generation
        if extraction_target is not None:
            palette_request = PaletteExtractionRequest(snapshot, layer_id, extraction_target)
            handle = self._jobs.submit(
                lambda token, progress: extract_layer_palette(palette_request, token, progress)
            )
        elif isinstance(parameters, AiPixelCleanupParameters):
            cleanup_request = AiPixelCleanupRequest(snapshot, layer_id, parameters)
            cache = self._ai_cache
            handle = self._jobs.submit(
                lambda token, progress: calculate_ai_pixel_cleanup(
                    cleanup_request, token, progress, cache
                )
            )
        elif isinstance(parameters, PixelOutlineParameters):
            outline_request = PixelOutlineRequest(snapshot, layer_id, parameters)
            handle = self._jobs.submit(
                lambda token, progress: calculate_pixel_outline(outline_request, token, progress)
            )
        else:
            request = ColorAdjustmentRequest(snapshot, layer_id, parameters)
            handle = self._jobs.submit(
                lambda token, progress: calculate_color_adjustment(request, token, progress)
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
            self.cancel(f"调整失败：{outcome.error}")
            return
        if isinstance(outcome.value, PaletteExtractionResult):
            extracted = outcome.value
            self._extraction_target = None
            if not self._result_is_current(extracted):
                self.cancel("文档已变化，已丢弃过期提取结果")
                return
            pending_apply = self._pending_apply
            # This synchronous panel update installs the extracted palette, not a
            # user edit. Keep UI frozen but allow its new parameters into the session.
            self._pending_apply = False
            self.palette_extracted.emit(extracted)
            if pending_apply and extracted.colors:
                self._pending_apply = True
                self._debounce.stop()
                self._submit_latest()
            if not extracted.colors:
                self._pending_apply = False
                self.applying_changed.emit(False)
                self._status("当前层或选区内没有非透明像素，未生成调色板")
            return
        if not isinstance(
            outcome.value, (ColorAdjustmentResult, PixelOutlineResult, AiPixelCleanupResult)
        ):
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

    def _show_result(self, result: AdjustmentResult) -> None:
        if isinstance(result, AiPixelCleanupResult):
            if result.source_color_count:
                self._canvas.render_coordinator.replace_raster_layer_preview(
                    result.layer_id, result.updates, replace_source=True
                )
            else:
                self._clear_preview()
        elif isinstance(result, PixelOutlineResult):
            self._canvas.render_coordinator.replace_raster_layer_preview(
                result.layer_id, result.updates
            )
        else:
            self._canvas.replace_pixel_preview(result.layer_id, dict(result.updates))

    def _commit(self, result: AdjustmentResult) -> None:
        if not self._result_is_current(result):
            self.cancel("文档已变化，无法应用颜色调整")
            return
        if isinstance(result, AiPixelCleanupResult) and (
            result.parameters.output is PixelCleanupOutput.PROJECT
        ):
            self._clear_preview()
            self.cancel_sampling()
            self._reset_session()
            self.active_changed.emit(False)
            if result.source_color_count:
                self.ai_project_requested.emit(result)
            else:
                self._status("没有有效像素，未创建工程")
            return
        self._committing = True
        label = (
            "AI 像素整理"
            if isinstance(result, AiPixelCleanupResult)
            else "像素描边"
            if isinstance(result, PixelOutlineResult)
            else _LABELS[adjustment_kind(cast(ColorAdjustmentParameters, self._parameters)).value]
        )
        try:
            if isinstance(result, AiPixelCleanupResult):
                applied = self._commands.apply_ai_pixel_cleanup(result)
            elif isinstance(result, PixelOutlineResult):
                applied = self._commands.apply_pixel_outline(result)
            else:
                applied = self._commands.apply_pixel_tiles(result.layer_id, result.updates, label)
        finally:
            self._committing = False
        self._clear_preview()
        self.cancel_sampling()
        self._reset_session()
        self.active_changed.emit(False)
        self._status(f"已应用{label}" if applied else f"{label}未产生变化")

    def _result_is_current(self, result: AdjustmentResult | PaletteExtractionResult) -> bool:
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
        self._extraction_target = None
        self._result = None
        self._result_generation = -1
        self._pending_apply = False
        self.applying_changed.emit(False)


__all__ = ["ColorAdjustmentController"]
