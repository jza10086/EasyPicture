"""Independent pixel-outline controls; all changes remain private until Apply."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from typing import cast

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from easypicture.imaging.color_adjustments import RGBAColor
from easypicture.imaging.pixel_outline import (
    PixelOutlineParameters,
    PixelOutlinePlacement,
    PixelOutlineResult,
)
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.dialogs import DialogService
from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.numeric_ranges import PIXEL_OUTLINE_WIDTH_RANGE
from easypicture.ui.settings import AppSettings


class PixelOutlinePanel(QWidget):
    parameters_changed = Signal(object)
    apply_requested = Signal()
    cancel_requested = Signal()
    preview_toggled = Signal(bool)

    def __init__(
        self,
        settings: AppSettings,
        dialogs: DialogService,
        foreground: Callable[[], RGBAColor],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings, self._dialogs, self._foreground = settings, dialogs, foreground
        self._loading = False
        self._color: RGBAColor = (0, 0, 0, 255)
        self.placement = BlenderComboBox(self)
        self.placement.addItem("外描边", PixelOutlinePlacement.OUTER.value)
        self.placement.addItem("内描边", PixelOutlinePlacement.INNER.value)
        width_range = PIXEL_OUTLINE_WIDTH_RANGE
        self.outline_width = DragValueSpinBox(
            int(width_range.soft_minimum),
            int(width_range.soft_maximum),
            int(width_range.default_value),
            manual_maximum=int(width_range.hard_maximum),
            suffix=" px",
            default_value=int(width_range.default_value),
            parent=self,
        )
        self.neighbors = BlenderComboBox(self)
        self.neighbors.addItem("8 邻域（方形）", 8)
        self.neighbors.addItem("4 邻域（菱形）", 4)
        self.threshold = DragValueSpinBox(1, 255, 128, default_value=128, parent=self)
        self.color_button = QPushButton("描边颜色…", self)
        self.preview_check = QCheckBox("显示预览", self)
        self.preview_check.setChecked(True)
        self.warning = QLabel("", self)
        self.warning.setWordWrap(True)
        self.progress_label = QLabel("", self)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.hide()
        self.apply_button = QPushButton("应用", self)
        self.cancel_button = QPushButton("取消", self)
        form = QFormLayout()
        form.addRow("位置", self.placement)
        form.addRow("宽度", self.outline_width)
        form.addRow("邻域", self.neighbors)
        form.addRow("Alpha 阈值", self.threshold)
        form.addRow("RGBA 颜色", self.color_button)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("像素描边 · 创建独立栅格层", self))
        layout.addLayout(form)
        layout.addWidget(self.preview_check)
        layout.addWidget(self.warning)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.progress_bar)
        buttons = QHBoxLayout()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        layout.addStretch(1)
        for spin in (self.outline_width, self.threshold):
            spin.valueChanged.connect(self._changed)
        for combo in (self.placement, self.neighbors):
            combo.currentIndexChanged.connect(self._changed)
        self.color_button.clicked.connect(self._choose_color)
        self.apply_button.clicked.connect(self.apply_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.preview_check.toggled.connect(self.preview_toggled)

    def activate(self) -> PixelOutlineParameters:
        params = PixelOutlineParameters(color=self._foreground())
        try:
            raw = self._settings.color_adjustment_state("pixel_outline")
            if raw:
                data = json.loads(raw)
                params = PixelOutlineParameters(
                    PixelOutlinePlacement(data["placement"]),
                    data["width"],
                    data["neighbors"],
                    data["alpha_threshold"],
                    cast(RGBAColor, tuple(data["color"])),
                )
        except ValueError, KeyError, TypeError:
            pass
        self._loading = True
        self._color = params.color
        self.outline_width.syncValue(params.width)
        self.threshold.syncValue(params.alpha_threshold)
        self.placement.setCurrentIndex(self.placement.findData(params.placement.value))
        self.neighbors.setCurrentIndex(self.neighbors.findData(params.neighbors))
        self.preview_check.setChecked(True)
        self._style_color()
        self.warning.clear()
        self.set_progress(0, 0, "")
        self._loading = False
        self._settings.set_color_adjustment_state("pixel_outline", json.dumps(asdict(params)))
        return params

    def parameters(self) -> PixelOutlineParameters:
        return PixelOutlineParameters(
            PixelOutlinePlacement(self.placement.currentData()),
            self.outline_width.value(),
            int(self.neighbors.currentData()),
            self.threshold.value(),
            self._color,
        )

    def _changed(self, *_args: object) -> None:
        if self._loading:
            return
        params = self.parameters()
        self._settings.set_color_adjustment_state("pixel_outline", json.dumps(asdict(params)))
        self.parameters_changed.emit(params)

    def _choose_color(self) -> None:
        color = self._dialogs.choose_color(self, QColor.fromRgb(*self._color), "选择像素描边颜色")
        if color is not None:
            self._color = (color.red(), color.green(), color.blue(), color.alpha())
            self._style_color()
            self._changed()

    def _style_color(self) -> None:
        text = "#ffffff" if QColor.fromRgb(*self._color).lightness() < 128 else "#000000"
        self.color_button.setStyleSheet(f"background-color: rgba{self._color}; color: {text};")

    def set_running(self, running: bool) -> None:
        self.apply_button.setText("等待结果…" if running else "应用")

    def set_applying(self, applying: bool) -> None:
        for control in (
            self.placement,
            self.outline_width,
            self.neighbors,
            self.threshold,
            self.color_button,
            self.apply_button,
        ):
            control.setEnabled(not applying)

    def set_progress(self, value: int, maximum: int, message: str) -> None:
        self.progress_bar.setVisible(maximum > 0 and value < maximum)
        self.progress_bar.setMaximum(maximum)
        self.progress_bar.setValue(value)
        self.progress_label.setText(message if 0 < value < maximum else "")

    def show_result(self, result: object) -> None:
        if isinstance(result, PixelOutlineResult):
            self.warning.setText(
                "描边可能被画布边界截断，不会扩大画布。"
                if result.clipped
                else "没有可见描边像素，不会创建图层。"
                if not result.updates
                else ""
            )
        self.set_running(False)
