"""Replaceable user-dialog boundary and default Qt implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from easypicture.domain import Document, Size
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION


@dataclass(frozen=True, slots=True)
class NewDocumentSpec:
    width: int
    height: int

    @property
    def size(self) -> Size:
        return Size(self.width, self.height)


class DialogService(Protocol):
    def request_new_document(
        self,
        parent: QWidget,
        default_size: Size,
    ) -> NewDocumentSpec | None: ...

    def confirm_discard(self, parent: QWidget, document: Document) -> bool: ...

    def choose_color(
        self,
        parent: QWidget,
        current: QColor,
        title: str,
    ) -> QColor | None: ...


class NewDocumentDialog(QDialog):
    def __init__(self, default_size: Size, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建文档")
        self.setModal(True)

        self.width_spin = QSpinBox(self)
        self.width_spin.setRange(1, MAX_DOCUMENT_DIMENSION)
        self.width_spin.setValue(default_size.width)
        self.width_spin.setSuffix(" px")

        self.height_spin = QSpinBox(self)
        self.height_spin.setRange(1, MAX_DOCUMENT_DIMENSION)
        self.height_spin.setValue(default_size.height)
        self.height_spin.setSuffix(" px")

        form = QFormLayout()
        form.addRow("宽度", self.width_spin)
        form.addRow("高度", self.height_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def specification(self) -> NewDocumentSpec:
        return NewDocumentSpec(self.width_spin.value(), self.height_spin.value())


class QtDialogService:
    def request_new_document(
        self,
        parent: QWidget,
        default_size: Size,
    ) -> NewDocumentSpec | None:
        dialog = NewDocumentDialog(default_size, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.specification()

    def confirm_discard(self, parent: QWidget, document: Document) -> bool:
        _ = document
        result = QMessageBox.question(
            parent,
            "放弃未保存的更改？",
            "当前文档包含未保存的更改。是否放弃这些更改？",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return result == QMessageBox.StandardButton.Discard

    def choose_color(
        self,
        parent: QWidget,
        current: QColor,
        title: str,
    ) -> QColor | None:
        color = QColorDialog.getColor(
            current,
            parent,
            title,
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        return color if color.isValid() else None


__all__ = [
    "DialogService",
    "NewDocumentDialog",
    "NewDocumentSpec",
    "QtDialogService",
]
