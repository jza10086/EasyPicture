"""Blender-style dialogs for canvas, image, and layer geometry changes."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from easypicture.domain import Size
from easypicture.imaging import CanvasAnchor, TransformInterpolation
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.drag_value_spinbox import DragValueSpinBox


class CanvasSizeDialog(QDialog):
    preview_changed = Signal(object, object)

    def __init__(
        self,
        size: Size,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("CanvasSizeDialog")
        self.setWindowTitle("调整画布大小")
        self.width_spin = DragValueSpinBox(
            1,
            8192,
            size.width,
            suffix=" px",
            parent=self,
        )
        self.height_spin = DragValueSpinBox(
            1,
            8192,
            size.height,
            suffix=" px",
            parent=self,
        )
        self.anchor_combo = BlenderComboBox(self)
        for label, anchor in (
            ("左上", CanvasAnchor.TOP_LEFT),
            ("上", CanvasAnchor.TOP),
            ("右上", CanvasAnchor.TOP_RIGHT),
            ("左", CanvasAnchor.LEFT),
            ("中心", CanvasAnchor.CENTER),
            ("右", CanvasAnchor.RIGHT),
            ("左下", CanvasAnchor.BOTTOM_LEFT),
            ("下", CanvasAnchor.BOTTOM),
            ("右下", CanvasAnchor.BOTTOM_RIGHT),
        ):
            self.anchor_combo.addItem(label, anchor.value)
        self.anchor_combo.setCurrentIndex(
            self.anchor_combo.findData(CanvasAnchor.CENTER.value)
        )
        form = QFormLayout()
        form.addRow("宽度", self.width_spin)
        form.addRow("高度", self.height_spin)
        form.addRow("锚点", self.anchor_combo)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.width_spin.valueChanged.connect(self._emit_preview)
        self.height_spin.valueChanged.connect(self._emit_preview)
        self.anchor_combo.currentIndexChanged.connect(self._emit_preview)

    @property
    def size_value(self) -> Size:
        return Size(self.width_spin.value(), self.height_spin.value())

    @property
    def anchor(self) -> CanvasAnchor:
        return CanvasAnchor(str(self.anchor_combo.currentData()))

    def _emit_preview(self, _value: object = None) -> None:
        self.preview_changed.emit(self.size_value, self.anchor)


class ScaleGeometryDialog(QDialog):
    preview_changed = Signal(object, object)

    def __init__(
        self,
        title: str,
        size: Size,
        interpolation: TransformInterpolation,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ScaleGeometryDialog")
        self.setWindowTitle(title)
        self._source_size = size
        self._syncing = False
        self.width_spin = DragValueSpinBox(
            1,
            8192,
            size.width,
            suffix=" px",
            parent=self,
        )
        self.height_spin = DragValueSpinBox(
            1,
            8192,
            size.height,
            suffix=" px",
            parent=self,
        )
        self.link_check = QCheckBox("保持宽高比", self)
        self.link_check.setChecked(True)
        self.interpolation_combo = BlenderComboBox(self)
        for label, mode in (
            ("最近邻", TransformInterpolation.NEAREST),
            ("双线性", TransformInterpolation.LINEAR),
            ("双三次", TransformInterpolation.CUBIC),
            ("Lanczos", TransformInterpolation.LANCZOS),
        ):
            self.interpolation_combo.addItem(label, mode.value)
        self.interpolation_combo.setCurrentIndex(
            max(0, self.interpolation_combo.findData(interpolation.value))
        )

        size_row = QHBoxLayout()
        size_row.addWidget(self.width_spin)
        size_row.addWidget(self.height_spin)
        form = QFormLayout()
        form.addRow("尺寸", size_row)
        form.addRow("", self.link_check)
        form.addRow("插值", self.interpolation_combo)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.width_spin.valueChanged.connect(self._width_changed)
        self.height_spin.valueChanged.connect(self._height_changed)
        self.interpolation_combo.currentIndexChanged.connect(self._emit_preview)

    @property
    def size_value(self) -> Size:
        return Size(self.width_spin.value(), self.height_spin.value())

    @property
    def interpolation(self) -> TransformInterpolation:
        return TransformInterpolation(str(self.interpolation_combo.currentData()))

    def _width_changed(self, value: int) -> None:
        if self._syncing:
            return
        if self.link_check.isChecked():
            self._syncing = True
            try:
                self.height_spin.setValue(
                    max(1, round(value * self._source_size.height / self._source_size.width))
                )
            finally:
                self._syncing = False
        self._emit_preview()

    def _height_changed(self, value: int) -> None:
        if self._syncing:
            return
        if self.link_check.isChecked():
            self._syncing = True
            try:
                self.width_spin.setValue(
                    max(1, round(value * self._source_size.width / self._source_size.height))
                )
            finally:
                self._syncing = False
        self._emit_preview()

    def _emit_preview(self, _value: object = None) -> None:
        self.preview_changed.emit(self.size_value, self.interpolation)


__all__ = ["CanvasSizeDialog", "ScaleGeometryDialog"]
