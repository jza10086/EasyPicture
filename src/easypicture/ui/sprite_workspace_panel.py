"""Property editor for the sprite workspace and active slicing rectangle."""

from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from easypicture.domain import Document, PixelPoint, Size, SpriteFrameDefinition
from easypicture.pixels import Rect
from easypicture.ui.drag_value_spinbox import DragValueSpinBox


class SpriteWorkspacePanel(QWidget):
    cell_size_requested = Signal(object)
    region_requested = Signal(object)
    frame_requested = Signal(object, object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document: Document | None = None
        self._region: Rect | None = None
        self._frame_id: UUID | None = None
        self._syncing = False

        self.cell_width = DragValueSpinBox(1, 8192, 32, suffix=" px", parent=self)
        self.cell_height = DragValueSpinBox(1, 8192, 32, suffix=" px", parent=self)
        self.region_x = DragValueSpinBox(0, 8192, 0, suffix=" px", parent=self)
        self.region_y = DragValueSpinBox(0, 8192, 0, suffix=" px", parent=self)
        self.region_size = QLabel("32 × 32 px", self)
        self.frame_name = QLineEdit(self)
        self.pivot_x = DragValueSpinBox(0, 8192, 16, suffix=" px", parent=self)
        self.pivot_y = DragValueSpinBox(0, 8192, 16, suffix=" px", parent=self)
        self.center_pivot = QPushButton("中心", self)
        self.bottom_center_pivot = QPushButton("底部中心", self)

        workspace_form = QFormLayout()
        workspace_form.addRow("单格宽度", self.cell_width)
        workspace_form.addRow("单格高度", self.cell_height)
        slice_form = QFormLayout()
        slice_form.addRow("区域 X", self.region_x)
        slice_form.addRow("区域 Y", self.region_y)
        slice_form.addRow("宽 × 高", self.region_size)
        frame_form = QFormLayout()
        frame_form.addRow("名称", self.frame_name)
        frame_form.addRow("锚点 X", self.pivot_x)
        frame_form.addRow("锚点 Y", self.pivot_y)
        presets = QWidget(self)
        presets_layout = QHBoxLayout(presets)
        presets_layout.setContentsMargins(0, 0, 0, 0)
        presets_layout.addWidget(self.center_pivot)
        presets_layout.addWidget(self.bottom_center_pivot)
        frame_form.addRow("锚点预设", presets)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(QLabel("精灵工作区", self))
        layout.addLayout(workspace_form)
        layout.addWidget(QLabel("当前切片", self))
        layout.addLayout(slice_form)
        layout.addWidget(QLabel("当前帧", self))
        layout.addLayout(frame_form)
        layout.addStretch(1)

        self.cell_width.editingFinished.connect(self._emit_cell_size)
        self.cell_height.editingFinished.connect(self._emit_cell_size)
        self.region_x.valueChanged.connect(self._emit_region)
        self.region_y.valueChanged.connect(self._emit_region)
        self.frame_name.editingFinished.connect(self._emit_frame)
        self.pivot_x.editingFinished.connect(self._emit_frame)
        self.pivot_y.editingFinished.connect(self._emit_frame)
        self.center_pivot.clicked.connect(lambda: self._set_pivot(bottom=False))
        self.bottom_center_pivot.clicked.connect(lambda: self._set_pivot(bottom=True))
        self.set_document(None)

    def set_document(self, document: Document | None) -> None:
        self._document = document
        self.setEnabled(document is not None)
        if document is None:
            return
        workspace = document.sprite_workspace
        for field, maximum in (
            (self.cell_width, document.width),
            (self.cell_height, document.height),
            (self.region_x, max(0, document.width - workspace.cell_size.width)),
            (self.region_y, max(0, document.height - workspace.cell_size.height)),
        ):
            minimum = 1 if field in (self.cell_width, self.cell_height) else 0
            field.setPropertyRange(
                minimum,
                maximum,
                manual_minimum=minimum,
                manual_maximum=maximum,
            )
        self._syncing = True
        try:
            self.cell_width.setValue(workspace.cell_size.width)
            self.cell_height.setValue(workspace.cell_size.height)
            self.region_size.setText(
                f"{workspace.cell_size.width} × {workspace.cell_size.height} px"
            )
            self.pivot_x.setPropertyRange(0, max(0, workspace.cell_size.width - 1))
            self.pivot_y.setPropertyRange(0, max(0, workspace.cell_size.height - 1))
        finally:
            self._syncing = False

    def set_region(self, region: Rect | None) -> None:
        self._region = region
        if region is None:
            return
        self._syncing = True
        try:
            # Region feedback commonly originates from one of these controls.
            # Never force that active control back through setValue(), because
            # setValue() intentionally terminates its drag/text session.
            self.region_x.syncValue(region.x)
            self.region_y.syncValue(region.y)
            self.region_size.setText(f"{region.width} × {region.height} px")
        finally:
            self._syncing = False

    def set_frame(self, frame: SpriteFrameDefinition | None) -> None:
        self._frame_id = None if frame is None else frame.id
        self._syncing = True
        try:
            if frame is None:
                self.frame_name.setText("")
                self.frame_name.setPlaceholderText("新增时自动命名")
                if self._document is not None:
                    size = self._document.sprite_workspace.cell_size
                    self.pivot_x.setValue(size.width // 2)
                    self.pivot_y.setValue(size.height // 2)
            else:
                self.frame_name.setText(frame.name)
                self.pivot_x.setValue(frame.pivot.x)
                self.pivot_y.setValue(frame.pivot.y)
        finally:
            self._syncing = False

    def _emit_cell_size(self) -> None:
        if not self._syncing:
            self.cell_size_requested.emit(Size(self.cell_width.value(), self.cell_height.value()))

    def _emit_region(self, _value: int) -> None:
        if self._syncing or self._region is None:
            return
        self.region_requested.emit(
            Rect(
                self.region_x.value(),
                self.region_y.value(),
                self._region.width,
                self._region.height,
            )
        )

    def _emit_frame(self) -> None:
        if self._syncing or self._frame_id is None:
            return
        self.frame_requested.emit(
            self._frame_id,
            self.frame_name.text(),
            PixelPoint(self.pivot_x.value(), self.pivot_y.value()),
        )

    def _set_pivot(self, *, bottom: bool) -> None:
        if self._document is None:
            return
        size = self._document.sprite_workspace.cell_size
        self._syncing = True
        try:
            self.pivot_x.setValue(size.width // 2)
            self.pivot_y.setValue(size.height - 1 if bottom else size.height // 2)
        finally:
            self._syncing = False
        self._emit_frame()


__all__ = ["SpriteWorkspacePanel"]
