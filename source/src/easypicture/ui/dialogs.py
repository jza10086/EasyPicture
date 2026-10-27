"""Replaceable user-dialog boundary and default Qt implementation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from easypicture.domain import Document, Size
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION
from easypicture.persistence import detect_export_capabilities
from easypicture.runtime import RuntimePaths
from easypicture.ui.color_dialog import ColorPickerDialog
from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.export_dialog import ImageExportDialog, ImageExportSpec
from easypicture.ui.numeric_ranges import DOCUMENT_DIMENSION_RANGE
from easypicture.ui.settings import AppSettings


@dataclass(frozen=True, slots=True)
class NewDocumentSpec:
    width: int
    height: int

    @property
    def size(self) -> Size:
        return Size(self.width, self.height)


class UnsavedDecision(Enum):
    SAVE = "save"
    DISCARD = "discard"
    CANCEL = "cancel"


class RecoveryDecision(Enum):
    RESTORE = "restore"
    DISCARD = "discard"
    KEEP = "keep"


class DialogService(Protocol):
    def request_new_document(
        self,
        parent: QWidget,
        default_size: Size,
    ) -> NewDocumentSpec | None: ...

    def confirm_discard(self, parent: QWidget, document: Document) -> bool: ...

    def confirm_unsaved(
        self,
        parent: QWidget,
        document: Document,
    ) -> UnsavedDecision: ...

    def choose_project_open_file(self, parent: QWidget) -> Path | None: ...

    def choose_image_import_file(self, parent: QWidget) -> Path | None: ...

    def choose_append_file(self, parent: QWidget) -> Path | None: ...

    def choose_project_save_file(
        self,
        parent: QWidget,
        suggested_name: str,
    ) -> Path | None: ...

    def choose_png_export_file(
        self,
        parent: QWidget,
        suggested_name: str,
    ) -> Path | None: ...

    def request_image_export(
        self,
        parent: QWidget,
        suggested_name: str,
        initial_directory: Path,
    ) -> ImageExportSpec | None: ...

    def confirm_recovery(self, parent: QWidget, display_name: str) -> RecoveryDecision: ...

    def show_error(self, parent: QWidget, title: str, message: str) -> None: ...

    def show_warnings(self, parent: QWidget, title: str, messages: tuple[str, ...]) -> None: ...

    def choose_color(
        self,
        parent: QWidget,
        current: QColor,
        title: str,
    ) -> QColor | None: ...

    def confirm_undo_history_reset(
        self,
        parent: QWidget,
        required_bytes: int,
        budget_bytes: int,
    ) -> bool: ...

    def show_undo_command_too_large(
        self,
        parent: QWidget,
        required_bytes: int,
        budget_bytes: int,
    ) -> None: ...

    def request_selection_radius(
        self,
        parent: QWidget,
        title: str,
        maximum: int,
    ) -> int | None: ...


class NewDocumentDialog(QDialog):
    def __init__(self, default_size: Size, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建文档")
        self.setModal(True)

        self.width_spin = DragValueSpinBox(
            int(DOCUMENT_DIMENSION_RANGE.soft_minimum),
            int(DOCUMENT_DIMENSION_RANGE.soft_maximum),
            default_size.width,
            manual_minimum=1,
            manual_maximum=MAX_DOCUMENT_DIMENSION,
            suffix=" px",
            parent=self,
        )
        self.height_spin = DragValueSpinBox(
            int(DOCUMENT_DIMENSION_RANGE.soft_minimum),
            int(DOCUMENT_DIMENSION_RANGE.soft_maximum),
            default_size.height,
            manual_minimum=1,
            manual_maximum=MAX_DOCUMENT_DIMENSION,
            suffix=" px",
            parent=self,
        )

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


class SelectionRadiusDialog(QDialog):
    """Request a selection adjustment radius with proportional dragging."""

    def __init__(
        self,
        title: str,
        maximum: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.radius_spin = DragValueSpinBox(
            1,
            maximum,
            1,
            suffix=" px",
            parent=self,
        )
        self.radius_spin.setMinimumWidth(180)

        form = QFormLayout()
        form.addRow("半径", self.radius_spin)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    @property
    def radius(self) -> int:
        return self.radius_spin.value()


class QtDialogService:
    def __init__(
        self,
        settings: AppSettings | None = None,
        runtime_paths: RuntimePaths | None = None,
    ) -> None:
        self._settings = settings if settings is not None else AppSettings()
        self._runtime_paths = runtime_paths if runtime_paths is not None else RuntimePaths.from_qt()

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

    def confirm_unsaved(
        self,
        parent: QWidget,
        document: Document,
    ) -> UnsavedDecision:
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("保存更改？")
        box.setText(f"是否保存对“{document.display_name}”的更改？")
        box.setInformativeText("不保存将丢失上次项目保存后的更改。")
        save_button = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton("放弃", QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return UnsavedDecision.SAVE
        if clicked is discard_button:
            return UnsavedDecision.DISCARD
        _ = cancel_button
        return UnsavedDecision.CANCEL

    def choose_project_open_file(self, parent: QWidget) -> Path | None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            parent,
            "打开 EasyPicture 项目",
            "",
            "EasyPicture 项目 (*.limg)",
        )
        return Path(path) if path else None

    def choose_image_import_file(self, parent: QWidget) -> Path | None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            parent,
            "导入图片",
            "",
            (
                "支持的图片 (*.svg *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.tga);;"
                "位图 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.tga);;"
                "SVG 矢量图 (*.svg);;所有文件 (*)"
            ),
        )
        return Path(path) if path else None

    def choose_append_file(self, parent: QWidget) -> Path | None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            parent,
            "追加图片或项目",
            "",
            (
                "支持的文件 (*.limg *.svg *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.tga);;"
                "EasyPicture 项目 (*.limg);;图片 (*.svg *.png *.jpg *.jpeg *.webp "
                "*.bmp *.tif *.tiff *.tga);;所有文件 (*)"
            ),
        )
        return Path(path) if path else None

    def choose_project_save_file(
        self,
        parent: QWidget,
        suggested_name: str,
    ) -> Path | None:
        path, _selected_filter = QFileDialog.getSaveFileName(
            parent,
            "保存 EasyPicture 项目",
            suggested_name,
            "EasyPicture 项目 (*.limg)",
        )
        if not path:
            return None
        result = Path(path)
        return result if result.suffix.lower() == ".limg" else result.with_suffix(".limg")

    def choose_png_export_file(
        self,
        parent: QWidget,
        suggested_name: str,
    ) -> Path | None:
        path, _selected_filter = QFileDialog.getSaveFileName(
            parent,
            "导出 PNG",
            suggested_name,
            "PNG 图片 (*.png)",
        )
        if not path:
            return None
        result = Path(path)
        return result if result.suffix.lower() == ".png" else result.with_suffix(".png")

    def request_image_export(
        self,
        parent: QWidget,
        suggested_name: str,
        initial_directory: Path,
    ) -> ImageExportSpec | None:
        dialog = ImageExportDialog(
            self._settings,
            suggested_name,
            initial_directory,
            detect_export_capabilities(),
            parent,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.specification()

    def confirm_recovery(self, parent: QWidget, display_name: str) -> RecoveryDecision:
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("发现恢复草稿")
        box.setText(f"发现“{display_name}”的自动恢复草稿。")
        box.setInformativeText("是否恢复上次异常退出前保存的内容？")
        restore_button = box.addButton("恢复", QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton("丢弃", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(restore_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is restore_button:
            return RecoveryDecision.RESTORE
        if clicked is discard_button:
            return RecoveryDecision.DISCARD
        return RecoveryDecision.KEEP

    def show_error(self, parent: QWidget, title: str, message: str) -> None:
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(title)
        box.setText(message)
        box.setInformativeText("详细信息已写入日志。")
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        log_button = box.addButton("打开日志目录", QMessageBox.ButtonRole.ActionRole)
        box.exec()
        if box.clickedButton() is log_button:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._runtime_paths.logs)))

    def show_warnings(
        self,
        parent: QWidget,
        title: str,
        messages: tuple[str, ...],
    ) -> None:
        if messages:
            box = QMessageBox(parent)
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(title)
            box.setText("\n".join(messages))
            box.setStandardButtons(QMessageBox.StandardButton.Close)
            box.open()

    def choose_color(
        self,
        parent: QWidget,
        current: QColor,
        title: str,
    ) -> QColor | None:
        dialog = ColorPickerDialog(current, title, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected_color

    def confirm_undo_history_reset(
        self,
        parent: QWidget,
        required_bytes: int,
        budget_bytes: int,
    ) -> bool:
        result = QMessageBox.warning(
            parent,
            "撤销历史空间不足",
            (
                "执行此操作需要重置现有撤销历史。\n\n"
                f"当前操作：{required_bytes / (1024 * 1024):.1f} MiB\n"
                f"撤销预算：{budget_bytes / (1024 * 1024):.0f} MiB\n\n"
                "是否清空旧撤销历史并继续？"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return result == QMessageBox.StandardButton.Yes

    def show_undo_command_too_large(
        self,
        parent: QWidget,
        required_bytes: int,
        budget_bytes: int,
    ) -> None:
        QMessageBox.critical(
            parent,
            "操作超出撤销预算",
            (
                f"此操作需要 {required_bytes / (1024 * 1024):.1f} MiB 撤销空间，"
                f"超过 {budget_bytes / (1024 * 1024):.0f} MiB 上限，因此未执行。"
            ),
        )

    def request_selection_radius(
        self,
        parent: QWidget,
        title: str,
        maximum: int,
    ) -> int | None:
        dialog = SelectionRadiusDialog(title, maximum, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.radius


__all__ = [
    "DialogService",
    "NewDocumentDialog",
    "NewDocumentSpec",
    "QtDialogService",
    "RecoveryDecision",
    "SelectionRadiusDialog",
    "UnsavedDecision",
]
