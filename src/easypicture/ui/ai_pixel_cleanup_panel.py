"""Compact AI pixel-art workflow; persistent preferences never contain document state."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import cast

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from easypicture.domain import Size
from easypicture.imaging.ai_pixel_cleanup import (
    AiPixelCleanupParameters,
    AiPixelCleanupResult,
    PixelCleanupAlpha,
    PixelCleanupDither,
    PixelCleanupOutput,
    PixelCleanupSelection,
    PixelGridSampling,
)
from easypicture.imaging.color_adjustments import RGBColor
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.dialogs import DialogService
from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.numeric_ranges import AI_PALETTE_COLOR_COUNT_RANGE, AI_PIXEL_BLOCK_RANGE
from easypicture.ui.palette_presets import PalettePresetStore
from easypicture.ui.settings import AppSettings


class AiPixelCleanupPanel(QWidget):
    parameters_changed = Signal(object)
    apply_requested = Signal()
    cancel_requested = Signal()
    preview_toggled = Signal(bool)
    sample_requested = Signal()
    grid_visibility_changed = Signal(bool)

    def __init__(
        self, settings: AppSettings, dialogs: DialogService, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._settings, self._dialogs = settings, dialogs
        self._presets = PalettePresetStore(settings)
        self._size = Size(1, 1)
        self._loading = False
        self._applying = False
        self._palette: tuple[RGBColor, ...] = ()
        self._locked: set[RGBColor] = set()
        self._selected = 0
        self._fixed = False
        self._buttons: list[QPushButton] = []
        self.controls = QWidget(self)
        contents = QVBoxLayout(self.controls)
        contents.setContentsMargins(0, 0, 0, 0)
        contents.setSpacing(5)
        grid_group = QGroupBox("网格", self.controls)
        grid_layout = QVBoxLayout(grid_group)
        self.grid_enabled = QCheckBox("网格规范化", grid_group)
        grid_layout.addWidget(self.grid_enabled)
        self.grid_fields = QWidget(grid_group)
        self.grid_form = QFormLayout(self.grid_fields)
        self.block_width = DragValueSpinBox(
            int(AI_PIXEL_BLOCK_RANGE.soft_minimum),
            int(AI_PIXEL_BLOCK_RANGE.soft_maximum),
            8,
            manual_maximum=int(AI_PIXEL_BLOCK_RANGE.hard_maximum),
            suffix=" px",
            default_value=8,
            parent=self,
        )
        self.block_height = DragValueSpinBox(
            int(AI_PIXEL_BLOCK_RANGE.soft_minimum),
            int(AI_PIXEL_BLOCK_RANGE.soft_maximum),
            8,
            manual_maximum=int(AI_PIXEL_BLOCK_RANGE.hard_maximum),
            suffix=" px",
            default_value=8,
            parent=self,
        )
        self.link_size = QCheckBox("宽高联动", self)
        self.link_size.setChecked(True)
        self.offset_x = DragValueSpinBox(0, 7, 0, suffix=" px", default_value=0, parent=self)
        self.offset_y = DragValueSpinBox(0, 7, 0, suffix=" px", default_value=0, parent=self)
        self.sampling = self._combo((("色块优先", "dominant"), ("中心采样", "center")))
        self.logical_size = QLabel(self)
        self.grid_visible = QCheckBox("显示网格及实际处理范围", self)
        self.grid_visible.setChecked(True)
        for name, control in (
            ("像素块宽", self.block_width),
            ("像素块高", self.block_height),
            ("", self.link_size),
            ("对齐 X", self.offset_x),
            ("对齐 Y", self.offset_y),
            ("取色方式", self.sampling),
            ("逻辑分辨率", self.logical_size),
            ("", self.grid_visible),
        ):
            self.grid_form.addRow(name, control)
        grid_layout.addWidget(self.grid_fields)
        self.alpha = self._combo(
            (("保留原 Alpha", "preserve"), ("网格覆盖率", "coverage"), ("阈值硬边", "hard"))
        )
        self.alpha_threshold = DragValueSpinBox(1, 255, 128, default_value=128, parent=self)
        self.alpha_form = QFormLayout()
        self.alpha_form.addRow("透明度", self.alpha)
        self.alpha_form.addRow("Alpha 阈值", self.alpha_threshold)
        grid_layout.addLayout(self.alpha_form)
        contents.addWidget(grid_group)

        color_group = QGroupBox("颜色", self.controls)
        colors_layout = QVBoxLayout(color_group)
        self.target = DragValueSpinBox(
            int(AI_PALETTE_COLOR_COUNT_RANGE.soft_minimum),
            int(AI_PALETTE_COLOR_COUNT_RANGE.soft_maximum),
            32,
            manual_maximum=int(AI_PALETTE_COLOR_COUNT_RANGE.hard_maximum),
            default_value=32,
            parent=self,
        )
        self.protect_details = QCheckBox("保守保护小细节", self)
        self.protect_details.setChecked(True)
        color_form = QFormLayout()
        color_form.addRow("目标色数", self.target)
        colors_layout.addLayout(color_form)
        quick = QHBoxLayout()
        self._quick_colors: list[QPushButton] = []
        for count in (8, 16, 24, 32, 48, 64):
            button = QPushButton(str(count), self)
            button.setMaximumWidth(44)
            button.clicked.connect(lambda _checked=False, value=count: self.target.setValue(value))
            quick.addWidget(button)
            self._quick_colors.append(button)
        colors_layout.addLayout(quick)
        colors_layout.addWidget(self.protect_details)
        self.mode_label = QLabel(self)
        colors_layout.addWidget(self.mode_label)
        self.preset = BlenderComboBox(self)
        colors_layout.addWidget(self.preset)
        preset_buttons = QHBoxLayout()
        for text, slot in (
            ("保存…", self._save_preset),
            ("重命名…", self._rename_preset),
            ("删除预设", self._delete_preset),
        ):
            button = QPushButton(text, self)
            button.clicked.connect(slot)
            preset_buttons.addWidget(button)
        colors_layout.addLayout(preset_buttons)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(96)
        scroll.setMaximumHeight(180)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        color_widget = QWidget(scroll)
        self.palette_grid = QGridLayout(color_widget)
        self.palette_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.palette_grid.setSpacing(3)
        scroll.setWidget(color_widget)
        colors_layout.addWidget(scroll)
        commands = QGridLayout()
        for index, (text, slot) in enumerate(
            (
                ("添加颜色…", self._add_color),
                ("编辑颜色…", self._edit_color),
                ("锁定/解锁", self._toggle_lock),
                ("删除颜色", self._remove_color),
                ("左移", lambda: self._move_color(-1)),
                ("右移", lambda: self._move_color(1)),
            )
        ):
            button = QPushButton(text, self)
            button.clicked.connect(slot)
            commands.addWidget(button, index // 2, index % 2)
        colors_layout.addLayout(commands)
        self.sample_button = QPushButton("从来源取色并锁定", self)
        self.sample_button.clicked.connect(self.sample_requested)
        colors_layout.addWidget(self.sample_button)
        regenerate = QPushButton("重新自动配色（保留锁色）", self)
        regenerate.clicked.connect(self._regenerate)
        colors_layout.addWidget(regenerate)
        self.dither = self._combo(
            (
                ("无", "none"),
                ("Bayer 2×2", "bayer2"),
                ("Bayer 4×4", "bayer4"),
                ("Floyd–Steinberg", "floyd_steinberg"),
            )
        )
        self.strength = DragValueSpinBox(0, 100, 35, suffix="%", default_value=35, parent=self)
        self.dither_form = QFormLayout()
        self.dither_form.addRow("抖动", self.dither)
        self.dither_form.addRow("强度", self.strength)
        colors_layout.addLayout(self.dither_form)
        contents.addWidget(color_group)

        output_group = QGroupBox("结果", self.controls)
        output_form = QFormLayout(output_group)
        self.selection = self._combo((("严格像素", "strict"), ("柔和混合", "soft")))
        self.output = self._combo((("新结果层并隐藏来源", "layer"), ("低分辨率新工程", "project")))
        self.preview_check = QCheckBox("显示结果（关闭查看原图）", self)
        self.preview_check.setChecked(True)
        self.statistics = QLabel("等待分析…", self)
        self.statistics.setWordWrap(True)
        output_form.addRow("选区方式", self.selection)
        output_form.addRow("输出", self.output)
        output_form.addRow(self.preview_check)
        output_form.addRow(self.statistics)
        contents.addWidget(output_group)
        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        self.error = QLabel(self)
        self.error.setWordWrap(True)
        self.progress_label = QLabel(self)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.hide()
        self.apply_button = QPushButton("应用", self)
        self.cancel_button = QPushButton("取消", self)
        row = QHBoxLayout()
        row.addWidget(self.apply_button)
        row.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(QLabel("AI 像素整理", self))
        layout.addWidget(self.controls)
        layout.addWidget(self.notice)
        layout.addWidget(self.error)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.progress_bar)
        layout.addLayout(row)
        layout.addStretch(1)
        for spin in (
            self.target,
            self.offset_x,
            self.offset_y,
            self.strength,
            self.alpha_threshold,
        ):
            spin.valueChanged.connect(self._changed)
        self.block_width.valueChanged.connect(lambda _v: self._size_changed(True))
        self.block_height.valueChanged.connect(lambda _v: self._size_changed(False))
        self.grid_enabled.toggled.connect(self._grid_toggled)
        for combo in (self.sampling, self.alpha, self.dither, self.selection, self.output):
            combo.currentIndexChanged.connect(self._changed)
        self.protect_details.toggled.connect(self._changed)
        self.preset.currentIndexChanged.connect(self._load_preset)
        self.preview_check.toggled.connect(self.preview_toggled)
        self.grid_visible.toggled.connect(self.grid_visibility_changed)
        self.apply_button.clicked.connect(self.apply_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self._refresh_presets()
        self._sync_visibility()

    def _combo(self, entries: tuple[tuple[str, str], ...]) -> BlenderComboBox:
        combo = BlenderComboBox(self)
        for text, value in entries:
            combo.addItem(text, value)
        return combo

    def activate(self, size: Size) -> AiPixelCleanupParameters:
        self._size = size
        self._loading = True
        self._palette, self._locked, self._selected, self._fixed = (), set(), 0, False
        params = AiPixelCleanupParameters()
        try:
            data = json.loads(self._settings.color_adjustment_state("ai_pixel_cleanup") or "{}")
            for key, kind in (
                ("sampling", PixelGridSampling),
                ("alpha", PixelCleanupAlpha),
                ("selection", PixelCleanupSelection),
                ("dither", PixelCleanupDither),
                ("output", PixelCleanupOutput),
            ):
                if key in data:
                    data[key] = kind(data[key])
            data.pop("locked_colors", None)
            data.pop("fixed_palette", None)
            params = AiPixelCleanupParameters(**data)
        except ValueError, TypeError, KeyError:
            pass
        self.grid_enabled.setChecked(params.grid_enabled)
        for spin, value in (
            (self.target, params.target_colors),
            (self.block_width, params.block_width),
            (self.block_height, params.block_height),
            (self.strength, params.dither_strength),
            (self.alpha_threshold, params.alpha_threshold),
        ):
            spin.syncValue(value)
        self.offset_x.syncPropertyRange(0, params.block_width - 1)
        self.offset_y.syncPropertyRange(0, params.block_height - 1)
        self.offset_x.syncValue(params.offset_x)
        self.offset_y.syncValue(params.offset_y)
        for combo, enum_value in (
            (self.sampling, params.sampling),
            (self.alpha, params.alpha),
            (self.selection, params.selection),
            (self.dither, params.dither),
            (self.output, params.output),
        ):
            combo.setCurrentIndex(combo.findData(enum_value.value))
        self.protect_details.setChecked(params.protect_details)
        self.link_size.setChecked(params.block_width == params.block_height)
        self.preview_check.setChecked(True)
        self.statistics.setText("等待分析…")
        self.error.clear()
        self.set_progress(0, 0, "")
        self._refresh_presets()
        self._rebuild_palette()
        self._loading = False
        self._sync_visibility()
        return self.parameters()

    def parameters(self) -> AiPixelCleanupParameters:
        return AiPixelCleanupParameters(
            target_colors=self.target.value(),
            protect_details=self.protect_details.isChecked(),
            locked_colors=tuple(sorted(self._locked)),
            fixed_palette=self._palette if self._fixed else None,
            grid_enabled=self.grid_enabled.isChecked(),
            block_width=self.block_width.value(),
            block_height=self.block_height.value(),
            offset_x=self.offset_x.value(),
            offset_y=self.offset_y.value(),
            sampling=PixelGridSampling(self.sampling.currentData()),
            alpha=PixelCleanupAlpha(self.alpha.currentData()),
            alpha_threshold=self.alpha_threshold.value(),
            selection=PixelCleanupSelection(self.selection.currentData()),
            dither=PixelCleanupDither(self.dither.currentData()),
            dither_strength=self.strength.value(),
            output=PixelCleanupOutput(self.output.currentData()),
        )

    def _sync_visibility(self) -> None:
        grid = self.grid_enabled.isChecked()
        self.grid_fields.setVisible(grid)
        self.alpha_form.setRowVisible(self.alpha, grid)
        self.alpha_form.setRowVisible(self.alpha_threshold, self.alpha.currentData() == "hard")
        self.dither_form.setRowVisible(self.strength, self.dither.currentData() != "none")
        self.target.setEnabled(not self._fixed)
        for button in self._quick_colors:
            button.setEnabled(not self._fixed)
        self.mode_label.setText("固定调色板" if self._fixed else "自动配色 · 编辑颜色会自动锁定")
        self.output.setEnabled(grid)
        self.notice.setText(
            "锁色保证颜色保留，不保证过粗网格中的细节保留。"
            + (
                "柔和选区可能产生表外颜色。"
                if self.selection.currentData() == "soft"
                else "严格选区按 128 阈值处理；开启网格时按整格覆盖率决定。"
            )
            + ("透明度重采样会改变轮廓。" if self.alpha.currentData() != "preserve" else "")
        )
        try:
            size = self.parameters().logical_size(self._size)
            self.logical_size.setText(f"{size.width} × {size.height}")
        except ValueError:
            pass

    def _grid_toggled(self, enabled: bool) -> None:
        if self._loading:
            return
        self._loading = True
        self.alpha.setCurrentIndex(self.alpha.findData("coverage" if enabled else "preserve"))
        if not enabled:
            self.output.setCurrentIndex(self.output.findData("layer"))
        self._loading = False
        self._changed()

    def _size_changed(self, width: bool) -> None:
        if self._loading:
            return
        self._loading = True
        if self.link_size.isChecked():
            if width:
                self.block_height.syncValue(self.block_width.value())
            else:
                self.block_width.syncValue(self.block_height.value())
        self.offset_x.syncPropertyRange(0, self.block_width.value() - 1)
        self.offset_y.syncPropertyRange(0, self.block_height.value() - 1)
        self._loading = False
        self._changed()

    def _changed(self, *_args: object) -> None:
        if self._loading or self._applying:
            return
        if self.output.currentData() == "project" and self.alpha.currentData() == "preserve":
            self.alpha.blockSignals(True)
            self.alpha.setCurrentIndex(self.alpha.findData("coverage"))
            self.alpha.blockSignals(False)
        self._sync_visibility()
        try:
            params = self.parameters()
        except ValueError as error:
            self.error.setText(str(error))
            self.apply_button.setEnabled(False)
            return
        self.error.clear()
        self.apply_button.setEnabled(True)
        data = asdict(params)
        data.pop("locked_colors")
        data.pop("fixed_palette")
        self._settings.set_color_adjustment_state("ai_pixel_cleanup", json.dumps(data))
        self.parameters_changed.emit(params)

    def show_result(self, result: object) -> None:
        if not isinstance(result, AiPixelCleanupResult):
            return
        self.statistics.setText(
            f"原始有效 RGB：{result.source_color_count}\n"
            f"调色板：{len(result.palette)} 色 · 结果区域：{result.result_color_count} 色"
        )
        if result.palette != self._palette:
            self._palette = result.palette
            self._selected = min(self._selected, max(0, len(self._palette) - 1))
            self._rebuild_palette()
        self.set_running(False)
        if not result.source_color_count:
            self.error.setText("当前层或选区内没有有效像素")
            self.apply_button.setEnabled(False)

    def _rebuild_palette(self) -> None:
        while self.palette_grid.count():
            item = self.palette_grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._buttons = []
        for index, color in enumerate(self._palette):
            button = QPushButton(self)
            button.setFixedSize(29, 29)
            button.setProperty("paletteIndex", index)
            button.setToolTip(f"RGB {color} · 双击编辑，右键锁色")
            button.clicked.connect(lambda _checked=False, i=index: self._select_color(i))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda position, i=index, b=button: self._color_menu(i, b, position)
            )
            button.installEventFilter(self)
            self.palette_grid.addWidget(button, index // 8, index % 8)
            self._buttons.append(button)
        self._style_palette()

    def _style_palette(self) -> None:
        for index, button in enumerate(self._buttons):
            color = self._palette[index]
            border = "#ff8a00" if index == self._selected else "#222222"
            text = "white" if QColor(*color).lightness() < 128 else "black"
            button.setText("锁" if color in self._locked else "")
            button.setStyleSheet(f"background:rgb{color};color:{text};border:2px solid {border};")

    def _select_color(self, index: int) -> None:
        self._selected = index
        self._style_palette()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseButtonDblClick and watched in self._buttons:
            self._select_color(int(watched.property("paletteIndex")))
            self._edit_color()
            return True
        return super().eventFilter(watched, event)

    def _color_menu(self, index: int, button: QPushButton, position: object) -> None:
        from PySide6.QtCore import QPoint

        self._select_color(index)
        menu = QMenu(button)
        action = menu.addAction("解锁颜色" if self._palette[index] in self._locked else "锁定颜色")
        action.triggered.connect(self._toggle_lock)
        menu.exec(button.mapToGlobal(cast(QPoint, position)))

    def _toggle_lock(self) -> None:
        if not self._palette:
            return
        color = self._palette[self._selected]
        if color in self._locked:
            self._locked.remove(color)
        else:
            self._locked.add(color)
        self._style_palette()
        self._changed()

    def _add_color(self) -> None:
        color = self._dialogs.choose_color(self, QColor(0, 0, 0), "添加并锁定颜色")
        if color is not None:
            self.use_sampled_color((color.red(), color.green(), color.blue(), color.alpha()))

    def use_sampled_color(self, rgba: object) -> None:
        values = cast(tuple[int, int, int, int], rgba)
        if not values[3]:
            self.error.setText("完全透明像素不作为有效锁色")
            return
        color = values[:3]
        if color not in self._palette:
            if len(self._palette) >= 256:
                self.error.setText("最多 256 个颜色，请先删除一个颜色")
                return
            self._palette += (color,)
        self._locked.add(color)
        self._selected = self._palette.index(color)
        self._rebuild_palette()
        self._changed()

    def _edit_color(self) -> None:
        if not self._palette:
            return
        previous = self._palette[self._selected]
        color = self._dialogs.choose_color(self, QColor(*previous), "编辑并锁定颜色")
        if color is None:
            return
        rgb = (color.red(), color.green(), color.blue())
        self._locked.discard(previous)
        self._locked.add(rgb)
        palette = list(self._palette)
        palette[self._selected] = rgb
        self._palette = tuple(dict.fromkeys(palette))
        self._selected = self._palette.index(rgb)
        self._rebuild_palette()
        self._changed()

    def _remove_color(self) -> None:
        if len(self._palette) <= 1:
            return
        palette = list(self._palette)
        self._locked.discard(palette.pop(self._selected))
        self._palette = tuple(palette)
        self._selected = min(self._selected, len(palette) - 1)
        self._fixed = True
        self._rebuild_palette()
        self._changed()

    def _move_color(self, offset: int) -> None:
        target = self._selected + offset
        if not 0 <= target < len(self._palette):
            return
        palette = list(self._palette)
        palette[target], palette[self._selected] = palette[self._selected], palette[target]
        self._palette, self._selected, self._fixed = tuple(palette), target, True
        self._rebuild_palette()
        self._changed()

    def _regenerate(self) -> None:
        self._fixed = False
        self._changed()

    def _refresh_presets(self, name: str | None = None) -> None:
        self.preset.blockSignals(True)
        self.preset.clear()
        self.preset.addItem("本机调色板预设…", None)
        for existing in self._presets.palettes():
            self.preset.addItem(existing, existing)
        self.preset.setCurrentIndex(max(0, self.preset.findData(name)))
        self.preset.blockSignals(False)

    def _load_preset(self, _index: int) -> None:
        colors = self._presets.palettes().get(str(self.preset.currentData()))
        if colors:
            self._palette, self._locked, self._selected, self._fixed = colors, set(), 0, True
            self._rebuild_palette()
            self._changed()

    def _save_preset(self) -> None:
        if not self._palette:
            return
        name, accepted = QInputDialog.getText(self, "保存调色板", "名称", text="AI 像素调色板")
        if accepted:
            try:
                self._refresh_presets(self._presets.save(name, self._palette))
            except ValueError as error:
                self.error.setText(str(error))

    def _rename_preset(self) -> None:
        old = self.preset.currentData()
        if old is None:
            return
        name, accepted = QInputDialog.getText(self, "重命名调色板", "名称", text=str(old))
        if accepted:
            try:
                self._refresh_presets(
                    self._presets.save(name, self._presets.palettes()[old], replacing=old)
                )
            except ValueError as error:
                self.error.setText(str(error))

    def _delete_preset(self) -> None:
        name = self.preset.currentData()
        if name is not None:
            self._presets.delete(str(name))
            self._refresh_presets()

    def set_running(self, running: bool) -> None:
        self.apply_button.setText("等待结果…" if running else "应用")

    def set_applying(self, applying: bool) -> None:
        self._applying = applying
        self.controls.setEnabled(not applying)
        self.apply_button.setEnabled(not applying)

    def set_progress(self, value: int, maximum: int, message: str) -> None:
        self.progress_bar.setVisible(maximum > 0 and value < maximum)
        self.progress_bar.setMaximum(maximum)
        self.progress_bar.setValue(value)
        self.progress_label.setText(message if maximum > 0 and value < maximum else "")
