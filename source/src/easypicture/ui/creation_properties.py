"""Persistent creation parameters, separate from selected-object properties."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtGui import QFont, QFontMetricsF
from PySide6.QtWidgets import (
    QCheckBox,
    QFontComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from easypicture.domain import RasterObject, Size, SvgObject, TextObject
from easypicture.domain.layer import RasterSampling
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.drag_value_spinbox import DragValueDoubleSpinBox
from easypicture.ui.settings import AppSettings
from easypicture.ui.tool_options import ShapeOptionsWidget


def text_size_for_height(text: TextObject) -> float:
    """Fit one font line to the drawn height in the renderer's point-size units."""
    font = QFont(text.font_family)
    font.setWeight(QFont.Weight(text.font_weight))
    font.setItalic(text.italic)
    available = max(1.0, text.frame.height - text.stroke_width)
    low, high = 1.0, 1024.0
    for _ in range(18):
        middle = (low + high) / 2
        font.setPointSizeF(middle)
        if QFontMetricsF(font).height() * text.line_spacing <= available:
            low = middle
        else:
            high = middle
    return low


class CreationPropertiesPanel(QWidget):
    def __init__(self, settings: AppSettings, shape: ShapeOptionsWidget, parent: QWidget) -> None:
        super().__init__(parent)
        self.settings = settings
        self._syncing = False
        self._text = settings.text_initialization()
        self._raster = settings.initialize_raster(RasterObject.create_blank("栅格对象", Size(1, 1)))
        self.last_label = QLabel("创建默认参数（按类型记忆）", self)
        self.kind_combo = BlenderComboBox(self)
        for label, kind in (("文字", "text"), ("图形", "shape"), ("位图", "raster")):
            self.kind_combo.addItem(label, kind)
        self.pages = QStackedWidget(self)
        text_page = QWidget(self)
        form = QFormLayout(text_page)
        self.auto_size = QCheckBox("字号随框选高度初始化", self)
        self.auto_size.setChecked(settings.text_auto_size())
        self.text_edit = QLineEdit(self._text.text, self)
        self.font_combo = QFontComboBox(self)
        self.font_combo.setCurrentFont(QFont(self._text.font_family))
        self.size_spin = self._number(1, 256, self._text.font_size, maximum=1024)
        self.bold_check = QCheckBox("粗体", self)
        self.italic_check = QCheckBox("斜体", self)
        self.spacing_spin = self._number(0.25, 4, self._text.line_spacing, maximum=8)
        self.opacity_spin = self._number(0, 100, self._text.opacity * 100)
        self.stroke_spin = self._number(0, 16, self._text.stroke_width, maximum=128)
        self.bold_check.setChecked(self._text.font_weight >= 700)
        self.italic_check.setChecked(self._text.italic)
        for label, field in (
            ("", self.auto_size),
            ("初始文本", self.text_edit),
            ("字体", self.font_combo),
            ("字号", self.size_spin),
            ("", self.bold_check),
            ("", self.italic_check),
            ("行距", self.spacing_spin),
            ("不透明度 %", self.opacity_spin),
            ("描边宽度", self.stroke_spin),
        ):
            form.addRow(label, field)
        form.addRow(QLabel("文字颜色使用主颜色，描边使用副颜色。", self))
        self.pages.addWidget(text_page)
        self.shape = ShapeOptionsWidget(settings, self, vertical=True)
        self.shape.set_settings(shape.settings())
        shape.settings_changed.connect(self.shape.set_settings)
        self.shape.settings_changed.connect(shape.set_settings)
        self.shape.settings_changed.connect(shape.settings_changed)
        self.pages.addWidget(self.shape)
        raster_page = QWidget(self)
        raster_form = QFormLayout(raster_page)
        self.raster_opacity = self._number(0, 100, self._raster.opacity * 100)
        self.alpha_lock = QCheckBox("锁定 Alpha", self)
        self.alpha_lock.setChecked(self._raster.alpha_locked)
        self.sampling = BlenderComboBox(self)
        for label, value in (("最近邻", RasterSampling.NEAREST), ("平滑", RasterSampling.SMOOTH)):
            self.sampling.addItem(label, value.value)
        self.sampling.setCurrentIndex(self.sampling.findData(self._raster.sampling.value))
        raster_form.addRow("不透明度 %", self.raster_opacity)
        raster_form.addRow(self.alpha_lock)
        raster_form.addRow("采样", self.sampling)
        self.pages.addWidget(raster_page)
        layout = QVBoxLayout(self)
        layout.addWidget(self.last_label)
        layout.addWidget(self.kind_combo)
        layout.addWidget(self.pages)
        note = QLabel("参数按类型记忆并用于下次创建；位置和尺寸由本次框选决定。", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        self.kind_combo.currentIndexChanged.connect(self.pages.setCurrentIndex)
        self.kind_combo.setCurrentIndex(self.kind_combo.findData(settings.last_created_kind()))
        self.pages.setCurrentIndex(self.kind_combo.currentIndex())
        self.auto_size.toggled.connect(self._save_text)
        self.text_edit.editingFinished.connect(self._save_text)
        self.font_combo.currentFontChanged.connect(self._save_text)
        for field in (self.size_spin, self.spacing_spin, self.opacity_spin, self.stroke_spin):
            field.editingFinished.connect(self._save_text)
        self.bold_check.toggled.connect(self._save_text)
        self.italic_check.toggled.connect(self._save_text)
        self.raster_opacity.editingFinished.connect(self._save_raster)
        self.alpha_lock.toggled.connect(self._save_raster)
        self.sampling.currentIndexChanged.connect(self._save_raster)
        self.size_spin.setEnabled(not self.auto_size.isChecked())

    def _number(
        self, low: float, high: float, value: float, *, maximum: float | None = None
    ) -> DragValueDoubleSpinBox:
        return DragValueDoubleSpinBox(
            low,
            high,
            value,
            manual_minimum=low,
            manual_maximum=high if maximum is None else maximum,
            decimals=2,
            parent=self,
        )

    def _save_text(self, _value: object = None) -> None:
        if self._syncing:
            return
        self._text = replace(
            self._text,
            text=self.text_edit.text(),
            font_family=self.font_combo.currentFont().family(),
            font_size=self.size_spin.value(),
            font_weight=700 if self.bold_check.isChecked() else 400,
            italic=self.italic_check.isChecked(),
            line_spacing=self.spacing_spin.value(),
            opacity=self.opacity_spin.value() / 100,
            stroke_width=self.stroke_spin.value(),
        )
        self.size_spin.setEnabled(not self.auto_size.isChecked())
        self.settings.set_text_auto_size(self.auto_size.isChecked())
        self.settings.set_text_initialization(self._text)
        self.settings.sync()

    def _save_raster(self, _value: object = None) -> None:
        self._raster = replace(
            self._raster,
            opacity=self.raster_opacity.value() / 100,
            alpha_locked=self.alpha_lock.isChecked(),
            sampling=RasterSampling(self.sampling.currentData()),
        )
        self.settings.set_raster_initialization(self._raster)
        self.settings.sync()

    def initialize_text(self, node: TextObject) -> TextObject:
        stroke = self.settings.background_color()
        text = replace(
            self._text,
            id=node.id,
            name=node.name,
            frame=node.frame,
            color=node.color,
            stroke_color=(stroke.red(), stroke.green(), stroke.blue(), stroke.alpha()),
        )
        return (
            replace(text, font_size=text_size_for_height(text))
            if self.auto_size.isChecked()
            else text
        )

    def initialize_raster(self, node: RasterObject) -> RasterObject:
        return replace(
            node,
            opacity=self._raster.opacity,
            alpha_locked=self._raster.alpha_locked,
            sampling=self._raster.sampling,
        )

    def record_created(self, node: RasterObject | TextObject | SvgObject) -> None:
        kind = (
            "text"
            if isinstance(node, TextObject)
            else "raster"
            if isinstance(node, RasterObject)
            else "shape"
        )
        self.settings.set_last_created_kind(kind)
        self.kind_combo.setCurrentIndex(self.kind_combo.findData(kind))
        self.last_label.setText(f"上次创建：{node.name}")
        if isinstance(node, TextObject):
            self._text = replace(node)
            self._syncing = True
            try:
                self.size_spin.syncValue(node.font_size)
            finally:
                self._syncing = False
            self.settings.set_text_initialization(node)
        elif isinstance(node, RasterObject):
            self.settings.set_raster_initialization(node)
        self.settings.sync()
