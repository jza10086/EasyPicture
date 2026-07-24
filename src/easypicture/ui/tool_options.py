"""Compact option widgets for the P4 editing tools."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QStackedWidget,
    QWidget,
)

from easypicture.imaging import BrushSettings
from easypicture.tools import BucketSettings
from easypicture.ui.settings import AppSettings


class BrushOptionsWidget(QWidget):
    settings_changed = Signal(object)

    def __init__(self, settings: BrushSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.size_spin = self._spin(1, 1024, settings.size, " px")
        self.hardness_spin = self._spin(0, 100, round(settings.hardness * 100), "%")
        self.opacity_spin = self._spin(0, 100, round(settings.opacity * 100), "%")
        self.spacing_spin = self._spin(1, 200, round(settings.spacing * 100), "%")
        self.antialias_check = QCheckBox("抗锯齿", self)
        self.antialias_check.setChecked(settings.antialias)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        for label, widget in (
            ("大小", self.size_spin),
            ("硬度", self.hardness_spin),
            ("不透明度", self.opacity_spin),
            ("间距", self.spacing_spin),
        ):
            layout.addWidget(QLabel(label, self))
            layout.addWidget(widget)
        layout.addWidget(self.antialias_check)
        layout.addStretch(1)
        for spin in (
            self.size_spin,
            self.hardness_spin,
            self.opacity_spin,
            self.spacing_spin,
        ):
            spin.valueChanged.connect(self._emit_settings)
        self.antialias_check.toggled.connect(self._emit_settings)

    def settings(self) -> BrushSettings:
        return BrushSettings(
            size=self.size_spin.value(),
            hardness=self.hardness_spin.value() / 100.0,
            opacity=self.opacity_spin.value() / 100.0,
            spacing=self.spacing_spin.value() / 100.0,
            antialias=self.antialias_check.isChecked(),
        )

    def _emit_settings(self, _value: object = None) -> None:
        self.settings_changed.emit(self.settings())

    def _spin(
        self,
        minimum: int,
        maximum: int,
        value: int,
        suffix: str,
    ) -> QSpinBox:
        spin = QSpinBox(self)
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setSuffix(suffix)
        return spin


class EyedropperOptionsWidget(QWidget):
    settings_changed = Signal(bool)

    def __init__(self, sample_merged: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.sample_merged_check = QCheckBox("取样所有可见图层", self)
        self.sample_merged_check.setChecked(sample_merged)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(self.sample_merged_check)
        layout.addStretch(1)
        self.sample_merged_check.toggled.connect(self.settings_changed)

    def sample_merged(self) -> bool:
        return self.sample_merged_check.isChecked()


class BucketOptionsWidget(QWidget):
    settings_changed = Signal(object)

    def __init__(self, settings: BucketSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tolerance_spin = QSpinBox(self)
        self.tolerance_spin.setRange(0, 255)
        self.tolerance_spin.setValue(settings.tolerance)
        self.connectivity_combo = QComboBox(self)
        self.connectivity_combo.addItem("4 邻域", 4)
        self.connectivity_combo.addItem("8 邻域", 8)
        self.connectivity_combo.setCurrentIndex(0 if settings.connectivity == 4 else 1)
        self.contiguous_check = QCheckBox("连续区域", self)
        self.contiguous_check.setChecked(settings.contiguous)
        self.sample_merged_check = QCheckBox("取样所有可见图层", self)
        self.sample_merged_check.setChecked(settings.sample_merged)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(QLabel("容差", self))
        layout.addWidget(self.tolerance_spin)
        layout.addWidget(self.connectivity_combo)
        layout.addWidget(self.contiguous_check)
        layout.addWidget(self.sample_merged_check)
        layout.addStretch(1)
        self.tolerance_spin.valueChanged.connect(self._emit_settings)
        self.connectivity_combo.currentIndexChanged.connect(self._emit_settings)
        self.contiguous_check.toggled.connect(self._emit_settings)
        self.sample_merged_check.toggled.connect(self._emit_settings)

    def settings(self) -> BucketSettings:
        connectivity = int(self.connectivity_combo.currentData())
        return BucketSettings(
            tolerance=self.tolerance_spin.value(),
            connectivity=4 if connectivity == 4 else 8,
            contiguous=self.contiguous_check.isChecked(),
            sample_merged=self.sample_merged_check.isChecked(),
        )

    def _emit_settings(self, _value: object = None) -> None:
        self.settings_changed.emit(self.settings())


class ToolOptionsPanel(QStackedWidget):
    brush_settings_changed = Signal(object)
    bucket_settings_changed = Signal(object)
    eyedropper_settings_changed = Signal(bool)

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.brush = BrushOptionsWidget(settings.brush_settings(), self)
        self.eyedropper = EyedropperOptionsWidget(settings.eyedropper_sample_merged(), self)
        self.bucket = BucketOptionsWidget(settings.bucket_settings(), self)
        self.addWidget(self.brush)
        self.addWidget(self.eyedropper)
        self.addWidget(self.bucket)
        self.brush.settings_changed.connect(self.brush_settings_changed)
        self.bucket.settings_changed.connect(self.bucket_settings_changed)
        self.eyedropper.settings_changed.connect(self.eyedropper_settings_changed)

    def show_tool(self, tool_name: str) -> None:
        if tool_name in {"brush", "eraser"}:
            self.setCurrentWidget(self.brush)
        elif tool_name == "eyedropper":
            self.setCurrentWidget(self.eyedropper)
        elif tool_name == "bucket":
            self.setCurrentWidget(self.bucket)


__all__ = [
    "BrushOptionsWidget",
    "BucketOptionsWidget",
    "EyedropperOptionsWidget",
    "ToolOptionsPanel",
]
