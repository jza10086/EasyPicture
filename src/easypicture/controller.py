"""Single-document lifecycle, persistence, and recovery controller."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from uuid import UUID

from PySide6.QtCore import QEventLoop, QObject, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtWidgets import QProgressDialog, QWidget

from easypicture.commands import DocumentCommandService
from easypicture.domain import (
    Document,
    DocumentEvent,
    DocumentModifiedChanged,
    LayerListChanged,
    LayerPixelsChanged,
    LayerPropertiesChanged,
    SelectionChanged,
)
from easypicture.domain.document import Unsubscribe
from easypicture.jobs import JobHandle, JobOutcome, JobRunner, JobStatus
from easypicture.jobs.runner import JobOperation
from easypicture.persistence import (
    DocumentSnapshot,
    ImageExportReceipt,
    ImageExportRequest,
    ImportedImage,
    LoadedProject,
    PngExportReceipt,
    SaveReceipt,
    export_image,
    export_png,
    import_image,
    load_project,
    save_project,
)
from easypicture.ui.dialogs import (
    DialogService,
    RecoveryDecision,
    UnsavedDecision,
)
from easypicture.ui.settings import AppSettings

DEFAULT_RECOVERY_INTERVAL_MS = 60_000


class ApplicationController(QObject):
    """Own the active document and keep all destructive flows transactional."""

    document_changed = Signal(object)
    document_event = Signal(object)
    status_message = Signal(str)

    def __init__(
        self,
        settings: AppSettings,
        dialogs: DialogService,
        parent: QObject | None = None,
        *,
        recovery_dir: Path | None = None,
        recovery_interval_ms: int = DEFAULT_RECOVERY_INTERVAL_MS,
        recovery_enabled: bool = True,
        job_runner: JobRunner | None = None,
    ) -> None:
        super().__init__(parent)
        if recovery_interval_ms <= 0:
            raise ValueError("recovery interval must be positive")
        self.settings = settings
        self.dialogs = dialogs
        self._document: Document | None = None
        self._unsubscribe: Unsubscribe | None = None
        self._dialog_parent: QWidget | None = None
        self.jobs = job_runner if job_runner is not None else JobRunner(self)
        self.commands = DocumentCommandService(
            confirm_history_reset=self._confirm_undo_history_reset,
            report_command_too_large=self._report_undo_command_too_large,
        )
        default_root = (
            Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppLocalDataLocation
                )
            )
            / "recovery"
        )
        self.recovery_dir = Path(recovery_dir) if recovery_dir is not None else default_root
        self.recovery_enabled = recovery_enabled
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setSingleShot(True)
        self._recovery_timer.setInterval(recovery_interval_ms)
        self._recovery_timer.timeout.connect(self._start_recovery_save)
        self._recovery_handle: JobHandle | None = None
        self._recovery_generation = 0

    @property
    def document(self) -> Document | None:
        return self._document

    @property
    def recovery_path(self) -> Path:
        return self.recovery_dir / "current.recovery.limg"

    def set_dialog_parent(self, parent: QWidget) -> None:
        self._dialog_parent = parent

    def request_new_document(self, parent: QWidget) -> bool:
        spec = self.dialogs.request_new_document(parent, self.settings.last_document_size())
        if spec is None or not self._resolve_unsaved(parent):
            return False
        self.settings.set_last_document_size(spec.size)
        self._replace_document(Document.create(spec.width, spec.height))
        return True

    def request_open_document(self, parent: QWidget) -> bool:
        path = self.dialogs.choose_open_file(parent)
        return False if path is None else self.open_path(parent, path)

    def open_path(self, parent: QWidget, path: Path) -> bool:
        candidate = self._load_candidate(parent, Path(path))
        if candidate is None or not self._resolve_unsaved(parent):
            return False
        document, notices = candidate
        self._replace_document(document)
        self.dialogs.show_warnings(parent, "导入提示", notices)
        return True

    def request_save(self, parent: QWidget) -> bool:
        document = self._document
        if document is None:
            return False
        if document.project_path is None:
            return self.request_save_as(parent)
        return self._save_to(parent, document.project_path)

    def request_save_as(self, parent: QWidget) -> bool:
        document = self._document
        if document is None:
            return False
        suggested = (
            document.project_path.name
            if document.project_path is not None
            else f"{Path(document.display_name).stem or 'Untitled'}.limg"
        )
        path = self.dialogs.choose_project_save_file(parent, suggested)
        return False if path is None else self._save_to(parent, path)

    def request_export_png(self, parent: QWidget) -> bool:
        document = self._document
        if document is None:
            return False
        suggested = f"{Path(document.display_name).stem or 'Untitled'}.png"
        path = self.dialogs.choose_png_export_file(parent, suggested)
        if path is None:
            return False
        snapshot = DocumentSnapshot.capture(document)
        outcome = self._run_foreground(
            parent,
            "导出 PNG",
            lambda token, progress: export_png(snapshot, path, token, progress),
        )
        if outcome.status is JobStatus.SUCCESS and isinstance(
            outcome.value,
            PngExportReceipt,
        ):
            self.status_message.emit(f"已导出 {outcome.value.path.name}")
            return True
        self._report_job_failure(parent, "导出失败", outcome)
        return False

    def request_export_image(self, parent: QWidget) -> bool:
        document = self._document
        if document is None:
            return False
        suggested = Path(document.display_name).stem or "Untitled"
        initial_directory = self.settings.last_export_directory()
        if initial_directory is None and document.project_path is not None:
            initial_directory = document.project_path.parent
        if initial_directory is None:
            pictures = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.PicturesLocation
            )
            initial_directory = Path(pictures) if pictures else Path.cwd()
        spec = self.dialogs.request_image_export(
            parent,
            suggested,
            initial_directory,
        )
        if spec is None:
            return False
        snapshot = DocumentSnapshot.capture(document)
        request = ImageExportRequest(snapshot, spec.path, spec.options)
        outcome = self._run_foreground(
            parent,
            "导出图片",
            lambda token, progress: export_image(request, token, progress),
        )
        if outcome.status is JobStatus.SUCCESS and isinstance(
            outcome.value,
            ImageExportReceipt,
        ):
            self.status_message.emit(f"已导出 {outcome.value.path.name}")
            return True
        self._report_job_failure(parent, "导出失败", outcome)
        return False

    def close_document(self, parent: QWidget) -> bool:
        if self._document is None:
            return True
        if not self._resolve_unsaved(parent):
            return False
        self._clear_recovery()
        self._replace_document(None)
        return True

    def request_exit(self, parent: QWidget) -> bool:
        return self.close_document(parent)

    def offer_recovery(self, parent: QWidget) -> bool:
        if not self.recovery_enabled or not self.recovery_path.exists():
            return False
        outcome = self._run_foreground(
            parent,
            "检查恢复草稿",
            lambda token, progress: load_project(self.recovery_path, token, progress),
        )
        if outcome.status is JobStatus.CANCELLED:
            return False
        if outcome.status is not JobStatus.SUCCESS or not isinstance(
            outcome.value,
            LoadedProject,
        ):
            self._quarantine_corrupt_recovery()
            self._report_job_failure(parent, "恢复草稿损坏", outcome)
            return False
        loaded = outcome.value
        if not loaded.is_recovery:
            self._quarantine_corrupt_recovery()
            self.dialogs.show_error(parent, "恢复草稿损坏", "该文件不是自动恢复草稿。")
            return False
        display_name = loaded.snapshot.source_name or (
            loaded.original_project_path.name
            if loaded.original_project_path is not None
            else "Untitled"
        )
        decision = self.dialogs.confirm_recovery(parent, display_name)
        if decision is RecoveryDecision.DISCARD:
            self._clear_recovery()
            return False
        if decision is RecoveryDecision.KEEP:
            return False
        document = loaded.snapshot.to_document(
            project_path=loaded.original_project_path,
            modified=True,
        )
        self._replace_document(document)
        self.status_message.emit("已恢复自动保存草稿")
        return True

    def _load_candidate(
        self,
        parent: QWidget,
        path: Path,
    ) -> tuple[Document, tuple[str, ...]] | None:
        if path.suffix.lower() == ".limg":
            outcome = self._run_foreground(
                parent,
                "打开项目",
                lambda token, progress: load_project(path, token, progress),
            )
            if outcome.status is JobStatus.SUCCESS and isinstance(
                outcome.value,
                LoadedProject,
            ):
                loaded = outcome.value
                if loaded.is_recovery:
                    self.dialogs.show_error(
                        parent,
                        "打开项目失败",
                        "自动恢复草稿只能通过启动恢复流程打开。",
                    )
                    return None
                document = loaded.snapshot.to_document(
                    project_path=path,
                )
                return document, ()
            self._report_job_failure(parent, "打开项目失败", outcome)
            return None

        outcome = self._run_foreground(
            parent,
            "导入图片",
            lambda token, progress: import_image(path, token, progress),
        )
        if outcome.status is JobStatus.SUCCESS and isinstance(outcome.value, ImportedImage):
            imported = outcome.value
            return imported.document, imported.warnings
        self._report_job_failure(parent, "导入图片失败", outcome)
        return None

    def _save_to(self, parent: QWidget, path: Path) -> bool:
        document = self._document
        if document is None:
            return False
        snapshot = DocumentSnapshot.capture(document)
        outcome = self._run_foreground(
            parent,
            "保存项目",
            lambda token, progress: save_project(snapshot, path, token, progress),
        )
        if outcome.status is not JobStatus.SUCCESS or not isinstance(
            outcome.value,
            SaveReceipt,
        ):
            self._report_job_failure(parent, "保存项目失败", outcome)
            return False
        receipt = outcome.value
        if self._document is not document:
            return False
        document.set_project_path(receipt.path)
        if not self.commands.mark_saved(receipt.document_id, receipt.revision):
            self.status_message.emit("项目快照已写入，但文档已有更新，仍保持未保存状态")
            return False
        self._clear_recovery()
        self.status_message.emit(f"已保存 {receipt.path.name}")
        return True

    def _resolve_unsaved(self, parent: QWidget) -> bool:
        document = self._document
        if document is None or not document.modified:
            return True
        decision = self.dialogs.confirm_unsaved(parent, document)
        if decision is UnsavedDecision.CANCEL:
            return False
        if decision is UnsavedDecision.SAVE:
            return self.request_save(parent)
        self._clear_recovery()
        return True

    def _replace_document(self, document: Document | None) -> None:
        self._recovery_timer.stop()
        self._cancel_recovery_job()
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = None
        self._document = document
        self.commands.attach_document(document)
        if document is not None:
            self._unsubscribe = document.subscribe(self._forward_document_event)
        self.document_changed.emit(document)

    def _forward_document_event(self, event: DocumentEvent) -> None:
        self.document_event.emit(event)
        if (
            self.recovery_enabled
            and self._document is not None
            and self._document.modified
            and isinstance(
                event,
                (
                    DocumentModifiedChanged,
                    LayerListChanged,
                    LayerPixelsChanged,
                    LayerPropertiesChanged,
                    SelectionChanged,
                ),
            )
        ):
            self._recovery_timer.start()

    def _start_recovery_save(self) -> None:
        document = self._document
        if (
            not self.recovery_enabled
            or document is None
            or not document.modified
            or self._recovery_handle is not None
        ):
            return
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        snapshot = DocumentSnapshot.capture(document)
        generation = self._recovery_generation
        handle = self.jobs.submit(
            lambda token, progress: save_project(
                snapshot,
                self.recovery_path,
                token,
                progress,
                recovery=True,
            )
        )
        self._recovery_handle = handle
        handle.finished.connect(
            lambda outcome: self._on_recovery_finished(
                outcome,
                generation,
                snapshot.document_id,
                snapshot.revision,
            )
        )

    def _on_recovery_finished(
        self,
        outcome: JobOutcome,
        generation: int,
        document_id: UUID,
        revision: int,
    ) -> None:
        self._recovery_handle = None
        if generation != self._recovery_generation:
            self.recovery_path.unlink(missing_ok=True)
            return
        if outcome.status is JobStatus.FAILED:
            self.status_message.emit("自动恢复草稿保存失败")
        document = self._document
        if (
            document is not None
            and document.id == document_id
            and document.modified
            and document.revision != revision
        ):
            self._recovery_timer.start()

    def _clear_recovery(self) -> None:
        self._recovery_generation += 1
        self._recovery_timer.stop()
        self._cancel_recovery_job()
        self.recovery_path.unlink(missing_ok=True)

    def _cancel_recovery_job(self) -> None:
        if self._recovery_handle is not None:
            self._recovery_handle.cancel()

    def _quarantine_corrupt_recovery(self) -> None:
        if not self.recovery_path.exists():
            return
        stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.recovery_path.with_name(f"current.recovery.corrupt-{stamp}.limg")
        try:
            os.replace(self.recovery_path, target)
        except OSError:
            return

    def _run_foreground(
        self,
        parent: QWidget,
        title: str,
        operation: JobOperation,
    ) -> JobOutcome:
        dialog = QProgressDialog("准备中…", "取消", 0, 0, parent)
        dialog.setWindowTitle(title)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        loop = QEventLoop()
        outcome_box: list[JobOutcome] = []
        handle = self.jobs.submit(operation)

        def update_progress(completed: int, total: int, message: str) -> None:
            dialog.setLabelText(message)
            if total > 0:
                dialog.setRange(0, total)
                dialog.setValue(min(completed, total))

        def finish(outcome: JobOutcome) -> None:
            outcome_box.append(outcome)
            loop.quit()

        handle.progress.connect(update_progress)
        handle.finished.connect(finish)
        dialog.canceled.connect(handle.cancel)
        dialog.open()
        loop.exec()
        dialog.close()
        if not outcome_box:
            return JobOutcome(handle.job_id, JobStatus.CANCELLED)
        return outcome_box[0]

    def _report_job_failure(
        self,
        parent: QWidget,
        title: str,
        outcome: JobOutcome,
    ) -> None:
        if outcome.status is JobStatus.CANCELLED:
            self.status_message.emit(f"{title.removesuffix('失败')}已取消")
            return
        error = outcome.error
        message = str(error) if error is not None else "未知错误"
        self.dialogs.show_error(parent, title, message)

    def _confirm_undo_history_reset(self, required_bytes: int, budget_bytes: int) -> bool:
        if self._dialog_parent is None:
            return False
        return self.dialogs.confirm_undo_history_reset(
            self._dialog_parent,
            required_bytes,
            budget_bytes,
        )

    def _report_undo_command_too_large(self, required_bytes: int, budget_bytes: int) -> None:
        if self._dialog_parent is None:
            return
        self.dialogs.show_undo_command_too_large(
            self._dialog_parent,
            required_bytes,
            budget_bytes,
        )


__all__ = ["ApplicationController", "DEFAULT_RECOVERY_INTERVAL_MS"]
