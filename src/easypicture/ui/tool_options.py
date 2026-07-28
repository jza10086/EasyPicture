"""Compact option widgets for the P4 editing tools."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QWidget,
)

from easypicture.imaging import (
    DEFAULT_BRUSH_SIZE_LIMIT,
    MAX_BRUSH_SIZE,
    BrushSettings,
    GeometryRect,
    PaintBlendMode,
    SelectionCombineMode,
    TransformInterpolation,
)
from easypicture.tools import BucketSettings, MagicWandSettings
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.drag_value_spinbox import (
    DragValueDoubleSpinBox,
    DragValueSpinBox,
    NumericEditState,
)
from easypicture.ui.settings import AppSettings


class BrushOptionsWidget(QWidget):
    settings_changed = Signal(object)

    def __init__(self, settings: BrushSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.size_spin = self._spin(
            1,
            DEFAULT_BRUSH_SIZE_LIMIT,
            settings.size,
            " px",
            manual_maximum=MAX_BRUSH_SIZE,
        )
        self.hardness_spin = self._spin(0, 100, round(settings.hardness * 100), "%")
        self.opacity_spin = self._spin(0, 100, round(settings.opacity * 100), "%")
        self.spacing_spin = self._spin(1, 200, round(settings.spacing * 100), "%")
        self.antialias_check = QCheckBox("抗锯齿", self)
        self.antialias_check.setChecked(settings.antialias)
        self.mode_combo = _paint_mode_combo(settings.mode, self)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(QLabel("模式", self))
        layout.addWidget(self.mode_combo)
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
        self.mode_combo.currentIndexChanged.connect(self._emit_settings)

    def settings(self) -> BrushSettings:
        return BrushSettings(
            size=self.size_spin.value(),
            hardness=self.hardness_spin.value() / 100.0,
            opacity=self.opacity_spin.value() / 100.0,
            spacing=self.spacing_spin.value() / 100.0,
            antialias=self.antialias_check.isChecked(),
            mode=PaintBlendMode(str(self.mode_combo.currentData())),
        )

    def _emit_settings(self, _value: object = None) -> None:
        self.settings_changed.emit(self.settings())

    def _spin(
        self,
        minimum: int,
        maximum: int,
        value: int,
        suffix: str,
        *,
        manual_maximum: int | None = None,
    ) -> DragValueSpinBox:
        return DragValueSpinBox(
            minimum,
            maximum,
            value,
            manual_maximum=manual_maximum,
            suffix=suffix,
            parent=self,
        )


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
        self.tolerance_spin = DragValueSpinBox(
            0,
            255,
            settings.tolerance,
            parent=self,
        )
        self.connectivity_combo = BlenderComboBox(self)
        self.connectivity_combo.addItem("4 邻域", 4)
        self.connectivity_combo.addItem("8 邻域", 8)
        self.connectivity_combo.setCurrentIndex(0 if settings.connectivity == 4 else 1)
        self.contiguous_check = QCheckBox("连续区域", self)
        self.contiguous_check.setChecked(settings.contiguous)
        self.sample_merged_check = QCheckBox("取样所有可见图层", self)
        self.sample_merged_check.setChecked(settings.sample_merged)
        self.edge_expansion_spin = DragValueSpinBox(
            0,
            8,
            settings.edge_expansion,
            suffix=" px",
            parent=self,
        )
        self.mode_combo = _paint_mode_combo(settings.mode, self)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(QLabel("模式", self))
        layout.addWidget(self.mode_combo)
        layout.addWidget(QLabel("容差", self))
        layout.addWidget(self.tolerance_spin)
        layout.addWidget(self.connectivity_combo)
        layout.addWidget(self.contiguous_check)
        layout.addWidget(self.sample_merged_check)
        layout.addWidget(QLabel("边缘覆盖", self))
        layout.addWidget(self.edge_expansion_spin)
        layout.addStretch(1)
        self.tolerance_spin.valueChanged.connect(self._emit_settings)
        self.connectivity_combo.currentIndexChanged.connect(self._emit_settings)
        self.contiguous_check.toggled.connect(self._emit_settings)
        self.sample_merged_check.toggled.connect(self._emit_settings)
        self.edge_expansion_spin.valueChanged.connect(self._emit_settings)
        self.mode_combo.currentIndexChanged.connect(self._emit_settings)

    def settings(self) -> BucketSettings:
        connectivity = int(self.connectivity_combo.currentData())
        return BucketSettings(
            tolerance=self.tolerance_spin.value(),
            connectivity=4 if connectivity == 4 else 8,
            contiguous=self.contiguous_check.isChecked(),
            sample_merged=self.sample_merged_check.isChecked(),
            edge_expansion=self.edge_expansion_spin.value(),
            mode=PaintBlendMode(str(self.mode_combo.currentData())),
        )

    def _emit_settings(self, _value: object = None) -> None:
        self.settings_changed.emit(self.settings())


def _paint_mode_combo(
    current: PaintBlendMode,
    parent: QWidget,
) -> BlenderComboBox:
    combo = BlenderComboBox(parent)
    for label, mode in (
        ("正常", PaintBlendMode.NORMAL),
        ("替换", PaintBlendMode.REPLACE),
        ("正片叠底", PaintBlendMode.MULTIPLY),
        ("滤色", PaintBlendMode.SCREEN),
        ("叠加", PaintBlendMode.OVERLAY),
    ):
        combo.addItem(label, mode.value)
    combo.setCurrentIndex(max(0, combo.findData(current.value)))
    return combo


def _add_selection_modes(
    combo: BlenderComboBox,
    current: SelectionCombineMode,
) -> None:
    for label, mode in (
        ("替换", SelectionCombineMode.REPLACE),
        ("添加", SelectionCombineMode.ADD),
        ("减去", SelectionCombineMode.SUBTRACT),
        ("相交", SelectionCombineMode.INTERSECT),
    ):
        combo.addItem(label, mode.value)
    combo.setCurrentIndex(max(0, combo.findData(current.value)))


class RectangleSelectionOptionsWidget(QWidget):
    settings_changed = Signal(object)
    deselect_requested = Signal()

    def __init__(
        self,
        mode: SelectionCombineMode,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode_combo = BlenderComboBox(self)
        _add_selection_modes(self.mode_combo, mode)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(QLabel("模式", self))
        layout.addWidget(self.mode_combo)
        self.deselect_button = QPushButton("取消选区", self)
        self.deselect_button.setToolTip("取消选区限制，后续编辑作用于整个画布")
        layout.addWidget(self.deselect_button)
        layout.addStretch(1)
        self.mode_combo.currentIndexChanged.connect(self._emit_mode)
        self.deselect_button.clicked.connect(self.deselect_requested)

    def mode(self) -> SelectionCombineMode:
        return SelectionCombineMode(str(self.mode_combo.currentData()))

    def set_mode(self, mode: SelectionCombineMode) -> None:
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(mode.value)))

    def _emit_mode(self, _index: int) -> None:
        self.settings_changed.emit(self.mode())


class MagicWandOptionsWidget(QWidget):
    settings_changed = Signal(object)
    deselect_requested = Signal()

    def __init__(
        self,
        settings: MagicWandSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode_combo = BlenderComboBox(self)
        _add_selection_modes(self.mode_combo, settings.mode)
        self.tolerance_spin = DragValueSpinBox(0, 255, settings.tolerance, parent=self)
        self.connectivity_combo = BlenderComboBox(self)
        self.connectivity_combo.addItem("4 邻域", 4)
        self.connectivity_combo.addItem("8 邻域", 8)
        self.connectivity_combo.setCurrentIndex(0 if settings.connectivity == 4 else 1)
        self.contiguous_check = QCheckBox("连续区域", self)
        self.contiguous_check.setChecked(settings.contiguous)
        self.sample_merged_check = QCheckBox("取样所有可见图层", self)
        self.sample_merged_check.setChecked(settings.sample_merged)
        self.include_alpha_check = QCheckBox("包含 Alpha", self)
        self.include_alpha_check.setChecked(settings.include_alpha)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(QLabel("模式", self))
        layout.addWidget(self.mode_combo)
        layout.addWidget(QLabel("容差", self))
        layout.addWidget(self.tolerance_spin)
        layout.addWidget(self.connectivity_combo)
        layout.addWidget(self.contiguous_check)
        layout.addWidget(self.sample_merged_check)
        layout.addWidget(self.include_alpha_check)
        self.deselect_button = QPushButton("取消选区", self)
        self.deselect_button.setToolTip("取消选区限制，后续编辑作用于整个画布")
        layout.addWidget(self.deselect_button)
        layout.addStretch(1)
        self.mode_combo.currentIndexChanged.connect(self._emit_settings)
        self.tolerance_spin.valueChanged.connect(self._emit_settings)
        self.connectivity_combo.currentIndexChanged.connect(self._emit_settings)
        self.contiguous_check.toggled.connect(self._emit_settings)
        self.sample_merged_check.toggled.connect(self._emit_settings)
        self.include_alpha_check.toggled.connect(self._emit_settings)
        self.deselect_button.clicked.connect(self.deselect_requested)

    def settings(self) -> MagicWandSettings:
        connectivity = int(self.connectivity_combo.currentData())
        return MagicWandSettings(
            tolerance=self.tolerance_spin.value(),
            connectivity=4 if connectivity == 4 else 8,
            contiguous=self.contiguous_check.isChecked(),
            sample_merged=self.sample_merged_check.isChecked(),
            include_alpha=self.include_alpha_check.isChecked(),
            mode=SelectionCombineMode(str(self.mode_combo.currentData())),
        )

    def set_mode(self, mode: SelectionCombineMode) -> None:
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(mode.value)))

    def _emit_settings(self, _value: object = None) -> None:
        self.settings_changed.emit(self.settings())


class TransformOptionsWidget(QWidget):
    options_changed = Signal()
    parameters_changed = Signal(object)
    parameter_edit_started = Signal(str)
    parameter_edit_finished = Signal()
    parameter_edit_canceled = Signal()
    commit_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._syncing = False
        self.interpolation_combo = BlenderComboBox(self)
        for label, interpolation in (
            ("最近邻", TransformInterpolation.NEAREST),
            ("双线性", TransformInterpolation.LINEAR),
            ("双三次", TransformInterpolation.CUBIC),
            ("Lanczos", TransformInterpolation.LANCZOS),
        ):
            self.interpolation_combo.addItem(label, interpolation.value)
        self.interpolation_combo.setCurrentIndex(
            max(
                0,
                self.interpolation_combo.findData(
                    settings.transform_interpolation().value
                ),
            )
        )
        self.irregular_check = QCheckBox("不规则编辑", self)
        self.irregular_check.setChecked(settings.transform_irregular_edit())
        self._pivot = (0.0, 0.0)
        self.width_spin = self._double(1.0, 8192.0, 1.0, " px")
        self.height_spin = self._double(1.0, 8192.0, 1.0, " px")
        self.rotation_spin = self._double(
            -180.0,
            180.0,
            0.0,
            "°",
            -36000.0,
            36000.0,
        )
        self.apply_button = QPushButton("应用", self)
        self.cancel_button = QPushButton("取消", self)
        self._last_parameters = self.parameters()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        for label, widget in (
            ("插值", self.interpolation_combo),
            ("W", self.width_spin),
            ("H", self.height_spin),
            ("旋转", self.rotation_spin),
        ):
            layout.addWidget(QLabel(label, self))
            layout.addWidget(widget)
        layout.addWidget(self.irregular_check)
        layout.addWidget(self.apply_button)
        layout.addWidget(self.cancel_button)
        layout.addStretch(1)
        self.interpolation_combo.currentIndexChanged.connect(self.options_changed)
        self.irregular_check.toggled.connect(self.options_changed)
        for field, label in zip(
            self.parameter_fields,
            ("宽度", "高度", "旋转"),
            strict=True,
        ):
            field.valueChanged.connect(self._emit_parameters)
            field.interactionStarted.connect(
                lambda label=label: self.parameter_edit_started.emit(label)
            )
            field.editingFinished.connect(self.parameter_edit_finished)
            field.interactionCanceled.connect(self.parameter_edit_canceled)
        self.apply_button.clicked.connect(self.commit_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)

    @property
    def parameter_fields(self) -> tuple[DragValueDoubleSpinBox, ...]:
        return (
            self.width_spin,
            self.height_spin,
            self.rotation_spin,
        )

    def interpolation(self) -> TransformInterpolation:
        return TransformInterpolation(str(self.interpolation_combo.currentData()))

    def irregular_edit(self) -> bool:
        return self.irregular_check.isChecked()

    def parameters(self) -> tuple[float, float, float, float, float]:
        return (
            self._pivot[0],
            self._pivot[1],
            self.width_spin.value(),
            self.height_spin.value(),
            self.rotation_spin.value(),
        )

    def set_parameters(
        self,
        values: tuple[float, float, float, float, float],
    ) -> None:
        self._syncing = True
        try:
            self._pivot = (values[0], values[1])
            for field, value in zip(self.parameter_fields, values[2:], strict=True):
                if field.interaction_state is NumericEditState.IDLE:
                    field.setValue(value)
            self._last_parameters = values
        finally:
            self._syncing = False

    def _emit_parameters(self, _value: float) -> None:
        if self._syncing:
            return
        values = list(self.parameters())
        resolved = tuple(values)
        self._last_parameters = resolved  # type: ignore[assignment]
        self.parameters_changed.emit(resolved)

    def _double(
        self,
        soft_minimum: float,
        soft_maximum: float,
        value: float,
        suffix: str,
        hard_minimum: float | None = None,
        hard_maximum: float | None = None,
    ) -> DragValueDoubleSpinBox:
        return DragValueDoubleSpinBox(
            soft_minimum,
            soft_maximum,
            value,
            manual_minimum=hard_minimum,
            manual_maximum=hard_maximum,
            suffix=suffix,
            parent=self,
        )


class CropOptionsWidget(QWidget):
    rect_changed = Signal(object)
    options_changed = Signal()
    commit_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._syncing = False
        self._original_ratio = 1.0
        self.ratio_combo = BlenderComboBox(self)
        for label, value in (
            ("自由", "free"),
            ("原始比例", "original"),
            ("1:1", "1:1"),
            ("4:3", "4:3"),
            ("3:2", "3:2"),
            ("16:9", "16:9"),
            ("自定义", "custom"),
        ):
            self.ratio_combo.addItem(label, value)
        self.custom_width = DragValueSpinBox(1, 100, 1, parent=self)
        self.custom_height = DragValueSpinBox(1, 100, 1, parent=self)
        self.grid_check = QCheckBox("三分网格", self)
        self.grid_check.setChecked(True)
        self.x_spin = DragValueSpinBox(
            -8192,
            8192,
            0,
            manual_minimum=-32768,
            manual_maximum=32768,
            suffix=" px",
            parent=self,
        )
        self.y_spin = DragValueSpinBox(
            -8192,
            8192,
            0,
            manual_minimum=-32768,
            manual_maximum=32768,
            suffix=" px",
            parent=self,
        )
        self.width_spin = DragValueSpinBox(1, 8192, 1, suffix=" px", parent=self)
        self.height_spin = DragValueSpinBox(1, 8192, 1, suffix=" px", parent=self)
        self.apply_button = QPushButton("应用", self)
        self.cancel_button = QPushButton("取消", self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        for label, widget in (
            ("比例", self.ratio_combo),
            ("X", self.x_spin),
            ("Y", self.y_spin),
            ("W", self.width_spin),
            ("H", self.height_spin),
        ):
            layout.addWidget(QLabel(label, self))
            layout.addWidget(widget)
        layout.addWidget(self.custom_width)
        layout.addWidget(QLabel(":", self))
        layout.addWidget(self.custom_height)
        layout.addWidget(self.grid_check)
        layout.addWidget(self.apply_button)
        layout.addWidget(self.cancel_button)
        layout.addStretch(1)
        for field in self.rect_fields:
            field.valueChanged.connect(self._emit_rect)
        self.ratio_combo.currentIndexChanged.connect(self._options_changed)
        self.custom_width.valueChanged.connect(self._options_changed)
        self.custom_height.valueChanged.connect(self._options_changed)
        self.grid_check.toggled.connect(self._options_changed)
        self.apply_button.clicked.connect(self.commit_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self._update_custom_visibility()

    @property
    def rect_fields(self) -> tuple[DragValueSpinBox, ...]:
        return self.x_spin, self.y_spin, self.width_spin, self.height_spin

    def set_original_size(self, width: int, height: int) -> None:
        self._original_ratio = width / max(1, height)

    def set_rect(self, rect: GeometryRect | None) -> None:
        if rect is None:
            return
        self._syncing = True
        try:
            for field, value in zip(
                self.rect_fields,
                (rect.x, rect.y, rect.width, rect.height),
                strict=True,
            ):
                if field.interaction_state is NumericEditState.IDLE:
                    field.setValue(value)
        finally:
            self._syncing = False

    def crop_rect_value(self) -> GeometryRect:
        return GeometryRect(
            self.x_spin.value(),
            self.y_spin.value(),
            self.width_spin.value(),
            self.height_spin.value(),
        )

    def ratio(self) -> float | None:
        value = str(self.ratio_combo.currentData())
        if value == "free":
            return None
        if value == "original":
            return self._original_ratio
        if value == "custom":
            return self.custom_width.value() / self.custom_height.value()
        left, right = value.split(":", maxsplit=1)
        return float(left) / float(right)

    def show_grid(self) -> bool:
        return self.grid_check.isChecked()

    def _emit_rect(self, _value: int) -> None:
        if not self._syncing:
            self.rect_changed.emit(self.crop_rect_value())

    def _options_changed(self, _value: object = None) -> None:
        self._update_custom_visibility()
        self.options_changed.emit()

    def _update_custom_visibility(self) -> None:
        custom = str(self.ratio_combo.currentData()) == "custom"
        self.custom_width.setVisible(custom)
        self.custom_height.setVisible(custom)


class ToolOptionsPanel(QStackedWidget):
    brush_settings_changed = Signal(object)
    bucket_settings_changed = Signal(object)
    eyedropper_settings_changed = Signal(bool)
    selection_mode_changed = Signal(object)
    magic_wand_settings_changed = Signal(object)
    deselect_requested = Signal()
    transform_options_changed = Signal()
    transform_parameters_changed = Signal(object)
    transform_parameter_edit_started = Signal(str)
    transform_parameter_edit_finished = Signal()
    transform_parameter_edit_canceled = Signal()
    transform_commit_requested = Signal()
    transform_cancel_requested = Signal()
    crop_rect_changed = Signal(object)
    crop_options_changed = Signal()
    crop_commit_requested = Signal()
    crop_cancel_requested = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.brush = BrushOptionsWidget(settings.brush_settings(), self)
        self.eyedropper = EyedropperOptionsWidget(settings.eyedropper_sample_merged(), self)
        self.bucket = BucketOptionsWidget(settings.bucket_settings(), self)
        self.rectangle_selection = RectangleSelectionOptionsWidget(
            settings.selection_combine_mode(),
            self,
        )
        self.magic_wand = MagicWandOptionsWidget(settings.magic_wand_settings(), self)
        self.transform = TransformOptionsWidget(settings, self)
        self.crop = CropOptionsWidget(self)
        self.addWidget(self.brush)
        self.addWidget(self.eyedropper)
        self.addWidget(self.bucket)
        self.addWidget(self.rectangle_selection)
        self.addWidget(self.magic_wand)
        self.addWidget(self.transform)
        self.addWidget(self.crop)
        self.brush.settings_changed.connect(self.brush_settings_changed)
        self.bucket.settings_changed.connect(self.bucket_settings_changed)
        self.eyedropper.settings_changed.connect(self.eyedropper_settings_changed)
        self.rectangle_selection.settings_changed.connect(self._selection_mode_from_rectangle)
        self.magic_wand.settings_changed.connect(self._magic_wand_settings_changed)
        self.transform.options_changed.connect(self.transform_options_changed)
        self.transform.parameters_changed.connect(self.transform_parameters_changed)
        self.transform.parameter_edit_started.connect(
            self.transform_parameter_edit_started
        )
        self.transform.parameter_edit_finished.connect(
            self.transform_parameter_edit_finished
        )
        self.transform.parameter_edit_canceled.connect(
            self.transform_parameter_edit_canceled
        )
        self.transform.commit_requested.connect(self.transform_commit_requested)
        self.transform.cancel_requested.connect(self.transform_cancel_requested)
        self.crop.rect_changed.connect(self.crop_rect_changed)
        self.crop.options_changed.connect(self.crop_options_changed)
        self.crop.commit_requested.connect(self.crop_commit_requested)
        self.crop.cancel_requested.connect(self.crop_cancel_requested)
        self.rectangle_selection.deselect_requested.connect(self.deselect_requested)
        self.magic_wand.deselect_requested.connect(self.deselect_requested)
        self.set_selection_active(False)

    def show_tool(self, tool_name: str) -> None:
        if tool_name in {"brush", "eraser"}:
            self.setCurrentWidget(self.brush)
        elif tool_name == "eyedropper":
            self.setCurrentWidget(self.eyedropper)
        elif tool_name == "bucket":
            self.setCurrentWidget(self.bucket)
        elif tool_name == "rectangle_selection":
            self.setCurrentWidget(self.rectangle_selection)
        elif tool_name == "magic_wand":
            self.setCurrentWidget(self.magic_wand)
        elif tool_name == "transform":
            self.setCurrentWidget(self.transform)
        elif tool_name == "crop":
            self.setCurrentWidget(self.crop)

    def set_selection_active(self, active: bool) -> None:
        self.rectangle_selection.deselect_button.setEnabled(active)
        self.magic_wand.deselect_button.setEnabled(active)

    def _selection_mode_from_rectangle(self, mode: object) -> None:
        if isinstance(mode, SelectionCombineMode):
            self.magic_wand.set_mode(mode)
            self.selection_mode_changed.emit(mode)

    def _magic_wand_settings_changed(self, settings: object) -> None:
        if isinstance(settings, MagicWandSettings):
            self.rectangle_selection.set_mode(settings.mode)
            self.selection_mode_changed.emit(settings.mode)
            self.magic_wand_settings_changed.emit(settings)


__all__ = [
    "BrushOptionsWidget",
    "BucketOptionsWidget",
    "CropOptionsWidget",
    "EyedropperOptionsWidget",
    "MagicWandOptionsWidget",
    "RectangleSelectionOptionsWidget",
    "TransformOptionsWidget",
    "ToolOptionsPanel",
]
