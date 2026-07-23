"""Single-document application controller."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, DocumentEvent
from easypicture.domain.document import Unsubscribe
from easypicture.ui.dialogs import DialogService
from easypicture.ui.settings import AppSettings


class ApplicationController(QObject):
    """Own the active document and coordinate destructive lifecycle prompts."""

    document_changed = Signal(object)
    document_event = Signal(object)

    def __init__(
        self,
        settings: AppSettings,
        dialogs: DialogService,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.dialogs = dialogs
        self._document: Document | None = None
        self._unsubscribe: Unsubscribe | None = None
        self._dialog_parent: QWidget | None = None
        self.commands = DocumentCommandService(
            confirm_history_reset=self._confirm_undo_history_reset,
            report_command_too_large=self._report_undo_command_too_large,
        )

    @property
    def document(self) -> Document | None:
        return self._document

    def set_dialog_parent(self, parent: QWidget) -> None:
        self._dialog_parent = parent

    def request_new_document(self, parent: QWidget) -> bool:
        spec = self.dialogs.request_new_document(parent, self.settings.last_document_size())
        if spec is None:
            return False
        if not self._can_discard_current(parent):
            return False
        self.settings.set_last_document_size(spec.size)
        self._replace_document(Document.create(spec.width, spec.height))
        return True

    def close_document(self, parent: QWidget) -> bool:
        if self._document is None:
            return True
        if not self._can_discard_current(parent):
            return False
        self._replace_document(None)
        return True

    def request_exit(self, parent: QWidget) -> bool:
        return self.close_document(parent)

    def _can_discard_current(self, parent: QWidget) -> bool:
        if self._document is None or not self._document.modified:
            return True
        return self.dialogs.confirm_discard(parent, self._document)

    def _replace_document(self, document: Document | None) -> None:
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


__all__ = ["ApplicationController"]
