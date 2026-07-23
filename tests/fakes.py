from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget

from easypicture.domain import Document, Size
from easypicture.ui.dialogs import NewDocumentSpec
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


def make_settings(path: str) -> AppSettings:
    settings = QSettings(path, QSettings.Format.IniFormat)
    return AppSettings(settings)
