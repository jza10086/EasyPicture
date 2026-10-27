"""Blender-style property panel for the read-only Tile inspection surface."""

from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from easypicture.imaging import (
    TileDirection,
    TilePreviewMode,
    TileSeamMetrics,
    TileTransitionMode,
)
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.settings import TileWorkspaceSettings


class TileWorkspacePanel(QWidget):
    parameters_changed = Signal()
    pick_requested = Signal(str)
    generate_requested = Signal()
    cancel_requested = Signal()

    def __init__(
        self,
        settings: TileWorkspaceSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._syncing = False
        self._tile_a_id: UUID | None = None
        self._tile_b_id: UUID | None = None
        self._tile_width = 1
        self._tile_height = 1
        self._busy = False

        self.mode = BlenderComboBox(self)
        for preview_value, label in (
            (TilePreviewMode.OFFSET, "平铺"),
            (TilePreviewMode.PAIR, "相邻对比"),
            (TilePreviewMode.TRANSITION, "过渡预览"),
        ):
            self.mode.addItem(label, preview_value)
        self.repeat_scale = BlenderComboBox(self)
        self.repeat_scale.addItem("3 × 3", 3)
        self.repeat_scale.addItem("5 × 5", 5)
        self.direction = BlenderComboBox(self)
        for direction_value, label in (
            (TileDirection.LEFT_TO_RIGHT, "从左到右"),
            (TileDirection.RIGHT_TO_LEFT, "从右到左"),
            (TileDirection.TOP_TO_BOTTOM, "从上到下"),
            (TileDirection.BOTTOM_TO_TOP, "从下到上"),
        ):
            self.direction.addItem(label, direction_value)
        self.tile_a = QLabel("未选择", self)
        self.tile_b = QLabel("未选择", self)
        self.pick_a = QPushButton("选择 Tile A", self)
        self.pick_b = QPushButton("选择 Tile B", self)
        self.tolerance = DragValueSpinBox(0, 255, settings.tolerance, parent=self)
        self.include_alpha = QCheckBox("包含 Alpha", self)
        self.heatmap = QCheckBox("显示接缝热力图", self)
        self.offset_x = DragValueSpinBox(-8192, 8192, settings.offset_x, suffix=" px", parent=self)
        self.offset_y = DragValueSpinBox(-8192, 8192, settings.offset_y, suffix=" px", parent=self)
        self.center_edges = QPushButton("边缘移到中心", self)
        self.reset_offset = QPushButton("重置偏移", self)
        self.transition_mode = BlenderComboBox(self)
        self.transition_mode.addItem("硬切", TileTransitionMode.HARD_CUT)
        self.transition_mode.addItem("有序抖动", TileTransitionMode.ORDERED_DITHER)
        self.transition_mode.setDefaultValue(TileTransitionMode.ORDERED_DITHER)
        self.boundary = DragValueSpinBox(0, 100, settings.boundary_percent, suffix="%", parent=self)
        self.transition_width = DragValueSpinBox(
            1, 8192, settings.transition_width, suffix=" px", parent=self
        )
        defaults = TileWorkspaceSettings()
        for field, default in (
            (self.tolerance, defaults.tolerance),
            (self.offset_x, defaults.offset_x),
            (self.offset_y, defaults.offset_y),
            (self.boundary, defaults.boundary_percent),
            (self.transition_width, defaults.transition_width),
        ):
            field.setDefaultValue(default)
        self.matching = QLabel("—", self)
        self.mean_difference = QLabel("—", self)
        self.maximum_difference = QLabel("—", self)
        self.generate = QPushButton("生成独立过渡帧", self)
        self.progress_label = QLabel("", self)
        self.cancel = QPushButton("取消预览计算", self)
        self.progress_label.hide()
        self.cancel.hide()

        self._form = form = QFormLayout()
        form.addRow("预览模式", self.mode)
        form.addRow("平铺规模", self.repeat_scale)
        form.addRow("Tile A", self.tile_a)
        form.addRow("", self.pick_a)
        form.addRow("Tile B", self.tile_b)
        form.addRow("", self.pick_b)
        form.addRow("方向", self.direction)
        form.addRow("容差", self.tolerance)
        form.addRow("", self.include_alpha)
        form.addRow("", self.heatmap)
        form.addRow("偏移 X", self.offset_x)
        form.addRow("偏移 Y", self.offset_y)
        self._offset_actions = offset_actions = QWidget(self)
        offset_layout = QHBoxLayout(offset_actions)
        offset_layout.setContentsMargins(0, 0, 0, 0)
        offset_layout.addWidget(self.center_edges)
        offset_layout.addWidget(self.reset_offset)
        form.addRow("", offset_actions)
        form.addRow("过渡方式", self.transition_mode)
        form.addRow("边界位置", self.boundary)
        form.addRow("过渡宽度", self.transition_width)
        form.addRow("匹配率", self.matching)
        form.addRow("平均差值", self.mean_difference)
        form.addRow("最大差值", self.maximum_difference)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(QLabel("Tile 工作区", self))
        layout.addLayout(form)
        layout.addWidget(self.generate)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.cancel)
        layout.addStretch(1)

        self._set_combo(self.mode, settings.preview_mode)
        self._set_combo(self.repeat_scale, settings.repeat_scale)
        self._set_combo(self.direction, settings.direction)
        self._set_combo(self.transition_mode, settings.transition_mode)
        self.include_alpha.setChecked(settings.include_alpha)
        self.heatmap.setChecked(settings.heatmap_visible)

        for combo in (self.mode, self.repeat_scale, self.direction, self.transition_mode):
            combo.currentIndexChanged.connect(self._changed)
        for field in (
            self.tolerance,
            self.offset_x,
            self.offset_y,
            self.boundary,
            self.transition_width,
        ):
            field.valueChanged.connect(self._changed)
        self.include_alpha.toggled.connect(self._changed)
        self.heatmap.toggled.connect(self._changed)
        self.pick_a.clicked.connect(lambda: self.pick_requested.emit("a"))
        self.pick_b.clicked.connect(lambda: self.pick_requested.emit("b"))
        self.center_edges.clicked.connect(self._move_edges_to_center)
        self.reset_offset.clicked.connect(self._reset_offset)
        self.generate.clicked.connect(self.generate_requested.emit)
        self.cancel.clicked.connect(self.cancel_requested.emit)
        self._refresh_visibility()

    @staticmethod
    def _set_combo(combo: BlenderComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def set_tile_size(self, width: int, height: int) -> None:
        self._tile_width = max(1, int(width))
        self._tile_height = max(1, int(height))
        width = self._tile_width
        height = self._tile_height
        self.offset_x.syncPropertyRange(-width, width, manual_minimum=-8192, manual_maximum=8192)
        self.offset_y.syncPropertyRange(-height, height, manual_minimum=-8192, manual_maximum=8192)
        maximum = max(
            1,
            width
            if self.direction_value
            in {
                TileDirection.LEFT_TO_RIGHT,
                TileDirection.RIGHT_TO_LEFT,
            }
            else height,
        )
        self.transition_width.syncPropertyRange(
            1, maximum, manual_minimum=1, manual_maximum=maximum
        )
        self.transition_width.syncValue(min(self.transition_width.value(), maximum))

    def set_frames(
        self,
        tile_a_id: UUID | None,
        tile_a_name: str | None,
        tile_b_id: UUID | None,
        tile_b_name: str | None,
    ) -> None:
        self._tile_a_id = tile_a_id
        self._tile_b_id = tile_b_id
        self.tile_a.setText(tile_a_name or "未选择")
        self.tile_b.setText(tile_b_name or "未选择")
        self._refresh_visibility()

    def set_metrics(self, metrics: TileSeamMetrics | None) -> None:
        if metrics is None:
            values = ("—", "—", "—")
        else:
            values = (
                f"{metrics.matching_percent:.2f}%",
                f"{metrics.mean_difference:.2f}",
                str(metrics.maximum_difference),
            )
        self.matching.setText(values[0])
        self.mean_difference.setText(values[1])
        self.maximum_difference.setText(values[2])

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.progress_label.setVisible(busy)
        self.cancel.setVisible(busy)
        if busy:
            self.progress_label.setText("正在计算，保留上一份完整预览…")
        self._refresh_visibility()

    def set_progress(self, done: int, total: int, label: str) -> None:
        if self._busy:
            self.progress_label.setText(f"{label} {100 * done // max(1, total)}%")

    @property
    def direction_value(self) -> TileDirection:
        return TileDirection(str(self.direction.currentData()))

    def settings_value(self) -> TileWorkspaceSettings:
        return TileWorkspaceSettings(
            preview_mode=TilePreviewMode(str(self.mode.currentData())),
            repeat_scale=int(self.repeat_scale.currentData()),
            direction=self.direction_value,
            tolerance=self.tolerance.value(),
            include_alpha=self.include_alpha.isChecked(),
            heatmap_visible=self.heatmap.isChecked(),
            offset_x=self.offset_x.value(),
            offset_y=self.offset_y.value(),
            transition_mode=TileTransitionMode(str(self.transition_mode.currentData())),
            boundary_percent=self.boundary.value(),
            transition_width=self.transition_width.value(),
        )

    def _changed(self, *_args: object) -> None:
        if self._syncing:
            return
        self._refresh_visibility()
        self.parameters_changed.emit()

    def _refresh_visibility(self) -> None:
        mode = TilePreviewMode(str(self.mode.currentData()))
        is_offset = mode is TilePreviewMode.OFFSET
        is_transition = mode is TilePreviewMode.TRANSITION
        self._form.setRowVisible(self.tile_b, not is_offset)
        self._form.setRowVisible(self.pick_b, not is_offset)
        direction_label = self._form.labelForField(self.direction)
        if isinstance(direction_label, QLabel):
            direction_label.setText("统计方向" if is_offset else "方向")
        for offset_widget in (
            self.repeat_scale,
            self.offset_x,
            self.offset_y,
            self._offset_actions,
        ):
            self._form.setRowVisible(offset_widget, is_offset)
        transition_enabled = (
            is_transition and self._tile_a_id is not None and self._tile_b_id is not None
        )
        for transition_widget in (
            self.transition_mode,
            self.boundary,
            self.transition_width,
        ):
            self._form.setRowVisible(transition_widget, is_transition)
        self._form.setRowVisible(
            self.transition_width,
            is_transition
            and TileTransitionMode(str(self.transition_mode.currentData()))
            is TileTransitionMode.ORDERED_DITHER,
        )
        self.generate.setVisible(is_transition)
        self.generate.setEnabled(transition_enabled and not self._busy)

    def _move_edges_to_center(self) -> None:
        self.offset_x.setValue(self._tile_width // 2)
        self.offset_y.setValue(self._tile_height // 2)

    def _reset_offset(self) -> None:
        self.offset_x.setValue(0)
        self.offset_y.setValue(0)


__all__ = ["TileWorkspacePanel"]
