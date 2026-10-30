from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget

from easypicture.domain import Document, Size
from easypicture.ui.dialogs import (
    NewDocumentSpec,
    RecoveryDecision,
    UnsavedDecision,
)
from easypicture.ui.export_dialog import ImageExportSpec
from easypicture.ui.settings import AppSettings


class FakeDialogService:
    def __init__(self) -> None:
        self.next_document: NewDocumentSpec | None = None
        self.discard_result = True
        self.discard_calls = 0
        self.next_color: QColor | None = None
        self.undo_reset_result = True
        self.undo_reset_calls: list[tuple[int, int]] = []
        self.undo_too_large_calls: list[tuple[int, int]] = []
        self.project_open_path: Path | None = None
        self.image_import_path: Path | None = None
        self.append_path: Path | None = None
        self.project_save_path: Path | None = None
        self.png_export_path: Path | None = None
        self.image_export_spec: ImageExportSpec | None = None
        self.unsaved_decision: UnsavedDecision | None = None
        self.recovery_decision = RecoveryDecision.KEEP
        self.errors: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, tuple[str, ...]]] = []
        self.selection_radius: int | None = None

    def request_new_document(
        self,
        parent: QWidget,
        default_size: Size,
    ) -> NewDocumentSpec | None:
        _ = parent, default_size
        return self.next_document

    def confirm_discard(self, parent: QWidget, document: Document) -> bool:
        _ = parent, document
        self.discard_calls += 1
        return self.discard_result

    def confirm_unsaved(
        self,
        parent: QWidget,
        document: Document,
    ) -> UnsavedDecision:
        _ = parent, document
        self.discard_calls += 1
        if self.unsaved_decision is not None:
            return self.unsaved_decision
        return UnsavedDecision.DISCARD if self.discard_result else UnsavedDecision.CANCEL

    def choose_project_open_file(self, parent: QWidget) -> Path | None:
        _ = parent
        return self.project_open_path

    def choose_image_import_file(self, parent: QWidget) -> Path | None:
        _ = parent
        return self.image_import_path

    def choose_append_file(self, parent: QWidget) -> Path | None:
        _ = parent
        return self.append_path

    def choose_project_save_file(
        self,
        parent: QWidget,
        suggested_name: str,
    ) -> Path | None:
        _ = parent, suggested_name
        return self.project_save_path

    def choose_png_export_file(
        self,
        parent: QWidget,
        suggested_name: str,
    ) -> Path | None:
        _ = parent, suggested_name
        return self.png_export_path

    def request_image_export(
        self,
        parent: QWidget,
        suggested_name: str,
        initial_directory: Path,
    ) -> ImageExportSpec | None:
        _ = parent, suggested_name, initial_directory
        return self.image_export_spec

    def confirm_recovery(
        self,
        parent: QWidget,
        display_name: str,
    ) -> RecoveryDecision:
        _ = parent, display_name
        return self.recovery_decision

    def show_error(self, parent: QWidget, title: str, message: str) -> None:
        _ = parent
        self.errors.append((title, message))

    def show_warnings(
        self,
        parent: QWidget,
        title: str,
        messages: tuple[str, ...],
    ) -> None:
        _ = parent
        if messages:
            self.warnings.append((title, messages))

    def choose_color(
        self,
        parent: QWidget,
        current: QColor,
        title: str,
    ) -> QColor | None:
        _ = parent, current, title
        return QColor(self.next_color) if self.next_color is not None else None

    def confirm_undo_history_reset(
        self,
        parent: QWidget,
        required_bytes: int,
        budget_bytes: int,
    ) -> bool:
        _ = parent
        self.undo_reset_calls.append((required_bytes, budget_bytes))
        return self.undo_reset_result

    def show_undo_command_too_large(
        self,
        parent: QWidget,
        required_bytes: int,
        budget_bytes: int,
    ) -> None:
        _ = parent
        self.undo_too_large_calls.append((required_bytes, budget_bytes))

    def request_selection_radius(
        self,
        parent: QWidget,
        title: str,
        maximum: int,
    ) -> int | None:
        _ = parent, title, maximum
        return self.selection_radius


def make_settings(path: str) -> AppSettings:
    settings = QSettings(path, QSettings.Format.IniFormat)
    return AppSettings(settings)
