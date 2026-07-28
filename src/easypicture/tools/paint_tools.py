"""Mouse-driven brush, eraser, eyedropper, and paint-bucket tools."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QProgressDialog

from easypicture.imaging import (
    BrushSettings,
    BrushStrokeEngine,
    FillRequest,
    FillResult,
    PaintBlendMode,
    RegionQuery,
    RGBAColor,
    calculate_fill,
    sample_document_pixel,
)
from easypicture.jobs import JobHandle, JobOutcome, JobStatus
from easypicture.persistence import DocumentSnapshot
from easypicture.tools.edit_session import PixelEditSession
from easypicture.tools.protocol import ToolContext


@dataclass(frozen=True, slots=True)
class BucketSettings:
    tolerance: int = 0
    connectivity: Literal[4, 8] = 4
    contiguous: bool = True
    sample_merged: bool = False
    edge_expansion: int = 1
    mode: PaintBlendMode = PaintBlendMode.NORMAL

    def __post_init__(self) -> None:
        if not 0 <= self.tolerance <= 255:
            raise ValueError("bucket tolerance must be between 0 and 255")
        if self.connectivity not in (4, 8):
            raise ValueError("bucket connectivity must be 4 or 8")
        if not 0 <= self.edge_expansion <= 8:
            raise ValueError("bucket edge expansion must be between 0 and 8")


class BrushTool:
    def __init__(
        self,
        settings_provider: Callable[[], BrushSettings],
        *,
        eraser: bool = False,
    ) -> None:
        self._settings_provider = settings_provider
        self._eraser = eraser
        self._context: ToolContext | None = None
        self._session: PixelEditSession | None = None
        self._stroke: BrushStrokeEngine | None = None

    def activate(self, context: ToolContext) -> None:
        self._context = context
        context.canvas.set_tool_cursor(Qt.CursorShape.BlankCursor)

    def deactivate(self) -> None:
        self._cancel()
        if self._context is not None:
            self._context.canvas.clear_brush_cursor()
            self._context.canvas.set_tool_cursor(None)
        self._context = None

    def mouse_press(self, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton or self._context is None:
            return False
        document = self._context.document_provider()
        commands = self._context.commands
        if document is None or commands is None:
            return False
        point = self._context.canvas.viewport_to_document(event.position().toPoint())
        self._update_cursor(event.position())
        if not 0 <= point.x < document.width or not 0 <= point.y < document.height:
            return False
        layer = document.active_layer
        if layer.locked:
            self._status("图层已锁定")
            return True
        if self._eraser and layer.alpha_locked:
            self._status("透明像素已锁定，橡皮不会改变当前图层")
            return True
        try:
            session = PixelEditSession(document, layer.id, commands, self._context.canvas)
        except PermissionError:
            self._status("图层已锁定")
            return True
        color = self._foreground()
        self._session = session
        self._stroke = BrushStrokeEngine(
            session,
            self._settings_provider(),
            color,
            eraser=self._eraser,
            changed=session.publish,
        )
        local_x = point.x - layer.origin.x
        local_y = point.y - layer.origin.y
        self._stroke.begin(local_x, local_y)
        self._context.canvas.viewport().grabMouse()
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        if self._context is None:
            return False
        point = self._context.canvas.viewport_to_document(event.position().toPoint())
        self._update_cursor(event.position())
        if self._stroke is None or self._session is None:
            return False
        self._stroke.extend(
            point.x - self._session.origin_x,
            point.y - self._session.origin_y,
        )
        return True

    def mouse_release(self, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton or self._session is None:
            return False
        label = "橡皮擦笔划" if self._eraser else "画笔笔划"
        committed = self._session.commit(label)
        self._release_mouse()
        self._session = None
        self._stroke = None
        if not committed:
            self._status("笔划未产生像素变化")
        return True

    def key_press(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Escape and self._session is not None:
            self._cancel()
            self._status("已取消当前笔划")
            return True
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        _ = event
        return False

    def _foreground(self) -> RGBAColor:
        if self._context is None or self._context.foreground_provider is None:
            return (0, 0, 0, 255)
        return self._context.foreground_provider()

    def _update_cursor(self, position: QPointF) -> None:
        if self._context is None:
            return
        document = self._context.document_provider()
        visible = document is not None
        self._context.canvas.update_brush_cursor(
            position,
            float(self._settings_provider().size),
            visible=visible,
            synchronous=True,
        )

    def _cancel(self) -> None:
        if self._session is not None:
            self._session.cancel()
        self._session = None
        self._stroke = None
        self._release_mouse()

    def _release_mouse(self) -> None:
        if self._context is None:
            return
        viewport = self._context.canvas.viewport()
        if viewport.mouseGrabber() is viewport:
            viewport.releaseMouse()

    def _status(self, message: str) -> None:
        if self._context is not None and self._context.status is not None:
            self._context.status(message)


class EyedropperTool:
    def __init__(self, sample_merged_provider: Callable[[], bool]) -> None:
        self._sample_merged_provider = sample_merged_provider
        self._context: ToolContext | None = None

    def activate(self, context: ToolContext) -> None:
        self._context = context
        context.canvas.set_tool_cursor(Qt.CursorShape.CrossCursor)

    def deactivate(self) -> None:
        if self._context is not None:
            self._context.canvas.set_tool_cursor(None)
        self._context = None

    def mouse_press(self, event: QMouseEvent) -> bool:
        if self._context is None or event.button() not in (
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.RightButton,
        ):
            return False
        document = self._context.document_provider()
        if document is None:
            return False
        point = self._context.canvas.viewport_to_document(event.position().toPoint())
        x, y = math.floor(point.x), math.floor(point.y)
        if not 0 <= x < document.width or not 0 <= y < document.height:
            return False
        color = sample_document_pixel(
            document,
            x,
            y,
            merged=self._sample_merged_provider(),
        )
        setter = (
            self._context.set_foreground
            if event.button() == Qt.MouseButton.LeftButton
            else self._context.set_background
        )
        if setter is not None:
            setter(color)
        if self._context.status is not None:
            self._context.status(f"已取样 RGBA{color}")
        return True

    def mouse_move(self, event: QMouseEvent) -> bool:
        _ = event
        return False

    def mouse_release(self, event: QMouseEvent) -> bool:
        _ = event
        return False

    def key_press(self, event: QKeyEvent) -> bool:
        _ = event
        return False

    def key_release(self, event: QKeyEvent) -> bool:
        _ = event
        return False


class BucketTool:
    def __init__(self, settings_provider: Callable[[], BucketSettings]) -> None:
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
        self._context = None

    def mouse_press(self, event: QMouseEvent) -> bool:
        if (
            self._context is None
            or event.button() != Qt.MouseButton.LeftButton
            or self._handle is not None
        ):
            return False
        document = self._context.document_provider()
        if document is None or self._context.jobs is None or self._context.commands is None:
            return False
        layer = document.active_layer
        if layer.locked:
            self._status("图层已锁定")
            return True
        point = self._context.canvas.viewport_to_document(event.position().toPoint())
        seed = (math.floor(point.x), math.floor(point.y))
        if not 0 <= seed[0] < document.width or not 0 <= seed[1] < document.height:
            return False
        settings = self._settings_provider()
        request = FillRequest(
            snapshot=DocumentSnapshot.capture(document),
            layer_id=layer.id,
            query=RegionQuery(
                seed=seed,
                tolerance=settings.tolerance,
                connectivity=settings.connectivity,
                contiguous=settings.contiguous,
            ),
            color=self._foreground(),
            sample_merged=settings.sample_merged,
            edge_expansion=settings.edge_expansion,
            mode=settings.mode,
        )
        handle = self._context.jobs.submit(
            lambda token, progress: calculate_fill(request, token, progress)
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
        dialog = QProgressDialog("正在计算填充区域…", "取消", 0, 0, self._context.canvas)
        dialog.setWindowTitle("油漆桶")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(150)
        dialog.setAutoClose(False)
        dialog.canceled.connect(handle.cancel)

        def update_progress(value: int, maximum: int, message: str) -> None:
            dialog.setLabelText(message)
            dialog.setMaximum(maximum)
            dialog.setValue(value)

        handle.progress.connect(update_progress)
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
            self._status("已取消填充")
            return
        if outcome.status is JobStatus.FAILED:
            self._status(f"填充失败：{outcome.error}")
            return
        if not isinstance(outcome.value, FillResult):
            self._status("填充失败：无效的任务结果")
            return
        result = outcome.value
        document = context.document_provider()
        if (
            document is None
            or document.id != result.document_id
            or document.revision != result.revision
            or document.active_layer_id != result.layer_id
        ):
            self._status("文档已变化，已丢弃过期填充结果")
            return
        commands = context.commands
        if commands is None or not commands.apply_pixel_tiles(
            result.layer_id,
            result.updates,
            "油漆桶填充",
        ):
            self._status("填充未产生像素变化")
            return
        self._status("填充完成")

    def _cancel_job(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
        self._close_progress()

    def _close_progress(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress.deleteLater()
            self._progress = None

    def _foreground(self) -> RGBAColor:
        if self._context is None or self._context.foreground_provider is None:
            return (0, 0, 0, 255)
        return self._context.foreground_provider()

    def _status(self, message: str) -> None:
        if self._context is not None and self._context.status is not None:
            self._context.status(message)


__all__ = ["BrushTool", "BucketSettings", "BucketTool", "EyedropperTool"]
