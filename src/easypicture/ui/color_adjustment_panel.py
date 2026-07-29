"""Blender-style right-side controls for color adjustment sessions."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import cast

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from easypicture.imaging import (
    BrightnessContrastAdjustment,
    ColorAdjustmentKind,
    ColorAdjustmentParameters,
    ColorChannel,
    ColorReplaceAdjustment,
    CurveDefinition,
    CurvesAdjustment,
    GammaAdjustment,
    GradientMapAdjustment,
    GradientStop,
    HslAdjustment,
    InvertAdjustment,
    PaletteMapAdjustment,
    ThresholdAdjustment,
    curve_lut,
)
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.dialogs import DialogService
from easypicture.ui.drag_value_spinbox import DragValueDoubleSpinBox, DragValueSpinBox
from easypicture.ui.settings import AppSettings

type RGBAColor = tuple[int, int, int, int]
type RGBColor = tuple[int, int, int]

_LABELS = {
    ColorAdjustmentKind.INVERT: "反相",
    ColorAdjustmentKind.THRESHOLD: "阈值",
    ColorAdjustmentKind.BRIGHTNESS_CONTRAST: "亮度/对比度",
    ColorAdjustmentKind.GAMMA: "伽马",
    ColorAdjustmentKind.CURVES: "曲线",
    ColorAdjustmentKind.HSL: "色相/饱和度/明度",
    ColorAdjustmentKind.GRADIENT_MAP: "渐变映射",
    ColorAdjustmentKind.COLOR_REPLACE: "颜色替换",
    ColorAdjustmentKind.PALETTE_MAP: "调色板映射",
}


class CurveEditor(QWidget):
    curve_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._curve = CurveDefinition()
        self._selected = 0
        self._dragging = False
        self.setMinimumSize(220, 180)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    @property
    def curve(self) -> CurveDefinition:
        return self._curve

    def set_curve(self, curve: CurveDefinition) -> None:
        self._curve = curve
        self._selected = min(self._selected, len(curve.points) - 1)
        self.update()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._graph_rect()
        painter.fillRect(rect, QColor("#202020"))
        painter.setPen(QPen(QColor("#3f3f3f"), 1.0))
        for index in range(1, 4):
            x = rect.left() + rect.width() * index / 4.0
            y = rect.top() + rect.height() * index / 4.0
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        samples = curve_lut(self._curve)
        path = QPainterPath()
        for value, mapped in enumerate(samples):
            point = self._curve_to_widget(float(value), float(mapped))
            if value == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        painter.setPen(QPen(QColor("#68a1ef"), 2.0))
        painter.drawPath(path)
        for index, curve_point in enumerate(self._curve.points):
            position = self._curve_to_widget(
                float(curve_point[0]),
                float(curve_point[1]),
            )
            painter.setPen(QPen(QColor("#111111"), 1.0))
            painter.setBrush(
                QColor("#ff8a00") if index == self._selected else QColor("#dddddd")
            )
            painter.drawEllipse(position, 4.5, 4.5)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            index = self._nearest_point(event.position())
            if index is not None and index not in (0, len(self._curve.points) - 1):
                points = list(self._curve.points)
                points.pop(index)
                self._selected = max(0, index - 1)
                self._set_points(points)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        index = self._nearest_point(event.position())
        if index is None and len(self._curve.points) < 16:
            x, y = self._widget_to_curve(event.position())
            points = list(self._curve.points)
            points.append((x, y))
            points.sort(key=lambda point: point[0])
            self._selected = points.index((x, y))
            self._set_points(points)
        elif index is not None:
            self._selected = index
            self.update()
        self._dragging = self._selected >= 0

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        x, y = self._widget_to_curve(event.position())
        points = list(self._curve.points)
        if self._selected == 0:
            x = 0
        elif self._selected == len(points) - 1:
            x = 255
        else:
            x = min(points[self._selected + 1][0] - 1, max(points[self._selected - 1][0] + 1, x))
        points[self._selected] = (x, y)
        self._set_points(points)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
            and self._selected not in (0, len(self._curve.points) - 1)
        ):
            points = list(self._curve.points)
            points.pop(self._selected)
            self._selected = max(0, self._selected - 1)
            self._set_points(points)
            return
        super().keyPressEvent(event)

    def _set_points(self, points: list[tuple[int, int]]) -> None:
        self._curve = CurveDefinition(tuple(points))
        self.curve_changed.emit(self._curve)
        self.update()

    def _graph_rect(self) -> QRectF:
        return QRectF(8.0, 8.0, max(1.0, self.width() - 16.0), max(1.0, self.height() - 16.0))

    def _curve_to_widget(self, x: float, y: float) -> QPointF:
        rect = self._graph_rect()
        return QPointF(
            rect.left() + x / 255.0 * rect.width(),
            rect.bottom() - y / 255.0 * rect.height(),
        )

    def _widget_to_curve(self, point: QPointF) -> tuple[int, int]:
        rect = self._graph_rect()
        x = round(np.clip((point.x() - rect.left()) / rect.width(), 0.0, 1.0) * 255.0)
        y = round(np.clip((rect.bottom() - point.y()) / rect.height(), 0.0, 1.0) * 255.0)
        return x, y

    def _nearest_point(self, position: QPointF) -> int | None:
        distances = [
            math.hypot(
                position.x() - self._curve_to_widget(float(point[0]), float(point[1])).x(),
                position.y() - self._curve_to_widget(float(point[0]), float(point[1])).y(),
            )
            for point in self._curve.points
        ]
        index = int(np.argmin(distances))
        return index if distances[index] <= 10.0 else None


class GradientEditor(QWidget):
    stops_changed = Signal(object)
    color_edit_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stops = GradientMapAdjustment().stops
        self._selected = 0
        self._dragging = False
        self.setMinimumHeight(64)

    @property
    def stops(self) -> tuple[GradientStop, ...]:
        return self._stops

    @property
    def selected_index(self) -> int:
        return self._selected

    def set_stops(self, stops: tuple[GradientStop, ...]) -> None:
        self._stops = stops
        self._selected = min(self._selected, len(stops) - 1)
        self.update()

    def set_selected_color(self, color: RGBAColor) -> None:
        stops = list(self._stops)
        stops[self._selected] = GradientStop(stops[self._selected].position, color)
        self._stops = tuple(stops)
        self.stops_changed.emit(self._stops)
        self.update()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        rect = QRectF(8.0, 8.0, max(1.0, self.width() - 16.0), 28.0)
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        for stop in self._stops:
            gradient.setColorAt(stop.position, QColor.fromRgb(*stop.color))
        painter.fillRect(rect, QBrush(gradient))
        painter.setPen(QPen(QColor("#111111"), 1.0))
        painter.drawRect(rect)
        for index, stop in enumerate(self._stops):
            x = rect.left() + rect.width() * stop.position
            path = QPainterPath()
            path.moveTo(x, rect.bottom() + 2.0)
            path.lineTo(x - 5.0, rect.bottom() + 11.0)
            path.lineTo(x + 5.0, rect.bottom() + 11.0)
            path.closeSubpath()
            painter.setBrush(
                QColor("#ff8a00") if index == self._selected else QColor.fromRgb(*stop.color)
            )
            painter.drawPath(path)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            index = self._nearest_stop(event.position().x())
            if index is not None and len(self._stops) > 2:
                stops = list(self._stops)
                stops.pop(index)
                self._selected = min(index, len(stops) - 1)
                self._set_stops(stops)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index = self._nearest_stop(event.position().x())
        if index is None and len(self._stops) < 8:
            position = self._position_from_x(event.position().x())
            color = self._interpolated_color(position)
            stops = [*self._stops, GradientStop(position, color)]
            stops.sort(key=lambda stop: stop.position)
            self._selected = next(
                index for index, stop in enumerate(stops) if stop.position == position
            )
            self._set_stops(stops)
        elif index is not None:
            self._selected = index
            self.update()
        self._dragging = True

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        stops = list(self._stops)
        position = self._position_from_x(event.position().x())
        lower = stops[self._selected - 1].position + 0.001 if self._selected > 0 else 0.0
        upper = (
            stops[self._selected + 1].position - 0.001
            if self._selected < len(stops) - 1
            else 1.0
        )
        stops[self._selected] = GradientStop(
            min(upper, max(lower, position)),
            stops[self._selected].color,
        )
        self._set_stops(stops)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        index = self._nearest_stop(event.position().x())
        if index is not None:
            self._selected = index
            self.color_edit_requested.emit(index)

    def _set_stops(self, stops: list[GradientStop]) -> None:
        self._stops = tuple(stops)
        self.stops_changed.emit(self._stops)
        self.update()

    def _nearest_stop(self, x: float) -> int | None:
        rect_width = max(1.0, self.width() - 16.0)
        positions = [8.0 + stop.position * rect_width for stop in self._stops]
        index = int(np.argmin([abs(x - position) for position in positions]))
        return index if abs(x - positions[index]) <= 10.0 else None

    def _position_from_x(self, x: float) -> float:
        return float(np.clip((x - 8.0) / max(1.0, self.width() - 16.0), 0.0, 1.0))

    def _interpolated_color(self, position: float) -> RGBAColor:
        positions = np.asarray([stop.position for stop in self._stops], dtype=np.float32)
        colors = np.asarray([stop.color for stop in self._stops], dtype=np.float32)
        return cast(
            RGBAColor,
            tuple(
                int(round(float(np.interp(position, positions, colors[:, channel]))))
                for channel in range(4)
            ),
        )


class ColorAdjustmentPanel(QWidget):
    parameters_changed = Signal(object)
    apply_requested = Signal()
    cancel_requested = Signal()
    preview_toggled = Signal(bool)
    sample_requested = Signal(bool)

    def __init__(
        self,
        settings: AppSettings,
        dialogs: DialogService,
        foreground_provider: Callable[[], RGBAColor],
        background_provider: Callable[[], RGBAColor],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._dialogs = dialogs
        self._foreground_provider = foreground_provider
        self._background_provider = background_provider
        self._kind = ColorAdjustmentKind.INVERT
        self._loading = False
        self._curve_values = {
            channel: CurveDefinition() for channel in ColorChannel
        }
        self._palette: tuple[RGBColor, ...] = ((0, 0, 0), (255, 255, 255))
        self._selected_palette = 0
        self._replace_source: RGBAColor = (0, 0, 0, 255)
        self._replace_target: RGBAColor = (255, 255, 255, 255)
        self._pages: dict[ColorAdjustmentKind, QWidget] = {}

        self.title_label = QLabel("颜色调整", self)
        self.title_label.setStyleSheet("font-weight: 600;")
        self.preview_check = QCheckBox("显示预览", self)
        self.preview_check.setChecked(True)
        self.stack = QStackedWidget(self)
        self._build_pages()
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self.stack)
        self.progress_label = QLabel("", self)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self.apply_button = QPushButton("应用", self)
        self.cancel_button = QPushButton("取消", self)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.title_label)
        layout.addWidget(self.preview_check)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.progress_bar)
        layout.addLayout(buttons)

        self.preview_check.toggled.connect(self.preview_toggled)
        self.apply_button.clicked.connect(self.apply_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)

    @property
    def kind(self) -> ColorAdjustmentKind:
        return self._kind

    def activate(self, kind: ColorAdjustmentKind) -> ColorAdjustmentParameters:
        self._kind = kind
        self.title_label.setText(_LABELS[kind])
        self.stack.setCurrentWidget(self._pages[kind])
        self.preview_check.setChecked(True)
        self._restore_state(kind)
        self.set_progress(0, 0, "")
        return self.parameters()

    def parameters(self) -> ColorAdjustmentParameters:
        if self._kind is ColorAdjustmentKind.INVERT:
            return InvertAdjustment(self._combo_channel(self.invert_channel))
        if self._kind is ColorAdjustmentKind.THRESHOLD:
            return ThresholdAdjustment(self.threshold.value())
        if self._kind is ColorAdjustmentKind.BRIGHTNESS_CONTRAST:
            return BrightnessContrastAdjustment(
                self.brightness.value(),
                self.contrast.value(),
            )
        if self._kind is ColorAdjustmentKind.GAMMA:
            return GammaAdjustment(
                self.gamma.value(),
                self._combo_channel(self.gamma_channel),
            )
        if self._kind is ColorAdjustmentKind.CURVES:
            return CurvesAdjustment(
                rgb=self._curve_values[ColorChannel.RGB],
                red=self._curve_values[ColorChannel.RED],
                green=self._curve_values[ColorChannel.GREEN],
                blue=self._curve_values[ColorChannel.BLUE],
                alpha=self._curve_values[ColorChannel.ALPHA],
            )
        if self._kind is ColorAdjustmentKind.HSL:
            return HslAdjustment(
                self.hue.value(),
                self.saturation.value(),
                self.lightness.value(),
            )
        if self._kind is ColorAdjustmentKind.GRADIENT_MAP:
            return GradientMapAdjustment(
                self.gradient_editor.stops,
                self.gradient_alpha.isChecked(),
            )
        if self._kind is ColorAdjustmentKind.COLOR_REPLACE:
            return ColorReplaceAdjustment(
                self._replace_source,
                self._replace_target,
                self.replace_tolerance.value(),
                self.replace_softness.value(),
                self.replace_include_alpha.isChecked(),
                self.replace_alpha.isChecked(),
            )
        return PaletteMapAdjustment(self._palette, self.palette_dither.isChecked())

    def set_parameters(self, parameters: ColorAdjustmentParameters) -> None:
        self._loading = True
        try:
            self._set_controls(parameters)
        finally:
            self._loading = False

    def set_progress(self, value: int, maximum: int, message: str) -> None:
        visible = maximum > 0 and value < maximum
        self.progress_label.setVisible(visible)
        self.progress_bar.setVisible(visible)
        if visible:
            self.progress_label.setText(message)
            self.progress_bar.setMaximum(maximum)
            self.progress_bar.setValue(value)

    def set_running(self, running: bool) -> None:
        self.apply_button.setText("等待结果…" if running else "应用")

    def use_sampled_source(self, color: RGBAColor) -> None:
        self._replace_source = color
        self._refresh_replace_colors()
        self._emit_parameters()

    def _build_pages(self) -> None:
        self._pages[ColorAdjustmentKind.INVERT] = self._invert_page()
        self._pages[ColorAdjustmentKind.THRESHOLD] = self._threshold_page()
        self._pages[ColorAdjustmentKind.BRIGHTNESS_CONTRAST] = (
            self._brightness_contrast_page()
        )
        self._pages[ColorAdjustmentKind.GAMMA] = self._gamma_page()
        self._pages[ColorAdjustmentKind.CURVES] = self._curves_page()
        self._pages[ColorAdjustmentKind.HSL] = self._hsl_page()
        self._pages[ColorAdjustmentKind.GRADIENT_MAP] = self._gradient_page()
        self._pages[ColorAdjustmentKind.COLOR_REPLACE] = self._replace_page()
        self._pages[ColorAdjustmentKind.PALETTE_MAP] = self._palette_page()
        for kind in ColorAdjustmentKind:
            self.stack.addWidget(self._pages[kind])

    def _invert_page(self) -> QWidget:
        page = QWidget(self)
        self.invert_channel = self._channel_combo(page)
        form = QFormLayout(page)
        form.addRow("通道", self.invert_channel)
        return page

    def _threshold_page(self) -> QWidget:
        page = QWidget(self)
        self.threshold = DragValueSpinBox(0, 255, 128, parent=page)
        form = QFormLayout(page)
        form.addRow("阈值", self.threshold)
        return page

    def _brightness_contrast_page(self) -> QWidget:
        page = QWidget(self)
        self.brightness = DragValueSpinBox(-100, 100, 0, suffix="%", parent=page)
        self.contrast = DragValueSpinBox(-100, 100, 0, suffix="%", parent=page)
        form = QFormLayout(page)
        form.addRow("亮度", self.brightness)
        form.addRow("对比度", self.contrast)
        return page

    def _gamma_page(self) -> QWidget:
        page = QWidget(self)
        self.gamma = DragValueDoubleSpinBox(
            0.1,
            4.0,
            1.0,
            manual_minimum=0.01,
            manual_maximum=10.0,
            decimals=2,
            parent=page,
        )
        self.gamma_channel = self._channel_combo(page)
        form = QFormLayout(page)
        form.addRow("通道", self.gamma_channel)
        form.addRow("伽马", self.gamma)
        return page

    def _curves_page(self) -> QWidget:
        page = QWidget(self)
        self.curves_channel = self._channel_combo(page)
        self.curve_editor = CurveEditor(page)
        reset = QPushButton("重置当前通道", page)
        layout = QVBoxLayout(page)
        layout.addWidget(self.curves_channel)
        layout.addWidget(self.curve_editor)
        layout.addWidget(reset)
        self.curves_channel.currentIndexChanged.connect(self._curve_channel_changed)
        self.curve_editor.curve_changed.connect(self._curve_changed)
        reset.clicked.connect(self._reset_curve)
        return page

    def _hsl_page(self) -> QWidget:
        page = QWidget(self)
        self.hue = DragValueSpinBox(-180, 180, 0, suffix="°", parent=page)
        self.saturation = DragValueSpinBox(-100, 100, 0, suffix="%", parent=page)
        self.lightness = DragValueSpinBox(-100, 100, 0, suffix="%", parent=page)
        form = QFormLayout(page)
        form.addRow("色相", self.hue)
        form.addRow("饱和度", self.saturation)
        form.addRow("明度", self.lightness)
        return page

    def _gradient_page(self) -> QWidget:
        page = QWidget(self)
        self.gradient_editor = GradientEditor(page)
        self.gradient_alpha = QCheckBox("使用渐变 Alpha", page)
        foreground = QPushButton("设为前景/背景渐变", page)
        layout = QVBoxLayout(page)
        layout.addWidget(self.gradient_editor)
        layout.addWidget(self.gradient_alpha)
        layout.addWidget(foreground)
        self.gradient_editor.stops_changed.connect(self._emit_parameters)
        self.gradient_editor.color_edit_requested.connect(self._edit_gradient_color)
        foreground.clicked.connect(self._set_foreground_background_gradient)
        return page

    def _replace_page(self) -> QWidget:
        page = QWidget(self)
        self.replace_source = QPushButton("源颜色", page)
        self.replace_target = QPushButton("目标颜色", page)
        self.replace_sample = QPushButton("从画布吸取源色", page)
        self.replace_sample_scope = BlenderComboBox(page)
        self.replace_sample_scope.addItem("当前层", False)
        self.replace_sample_scope.addItem("可见合成", True)
        self.replace_tolerance = DragValueSpinBox(0, 255, 0, parent=page)
        self.replace_softness = DragValueSpinBox(0, 255, 0, parent=page)
        self.replace_include_alpha = QCheckBox("比较 Alpha", page)
        self.replace_alpha = QCheckBox("替换 Alpha", page)
        form = QFormLayout(page)
        form.addRow("源颜色", self.replace_source)
        form.addRow("目标颜色", self.replace_target)
        form.addRow("取样范围", self.replace_sample_scope)
        form.addRow("", self.replace_sample)
        form.addRow("容差", self.replace_tolerance)
        form.addRow("柔和度", self.replace_softness)
        form.addRow("", self.replace_include_alpha)
        form.addRow("", self.replace_alpha)
        self.replace_source.clicked.connect(lambda: self._choose_replace_color(True))
        self.replace_target.clicked.connect(lambda: self._choose_replace_color(False))
        self.replace_sample.clicked.connect(
            lambda: self.sample_requested.emit(bool(self.replace_sample_scope.currentData()))
        )
        self._refresh_replace_colors()
        return page

    def _palette_page(self) -> QWidget:
        page = QWidget(self)
        self.palette_grid = QGridLayout()
        self.palette_dither = QCheckBox("Floyd–Steinberg 抖动", page)
        add_foreground = QPushButton("添加前景色", page)
        add_background = QPushButton("添加背景色", page)
        add_custom = QPushButton("添加颜色…", page)
        remove = QPushButton("删除选中色", page)
        move_left = QPushButton("左移", page)
        move_right = QPushButton("右移", page)
        buttons = QGridLayout()
        buttons.addWidget(add_foreground, 0, 0)
        buttons.addWidget(add_background, 0, 1)
        buttons.addWidget(add_custom, 1, 0)
        buttons.addWidget(remove, 1, 1)
        buttons.addWidget(move_left, 2, 0)
        buttons.addWidget(move_right, 2, 1)
        layout = QVBoxLayout(page)
        layout.addLayout(self.palette_grid)
        layout.addWidget(self.palette_dither)
        layout.addLayout(buttons)
        add_foreground.clicked.connect(
            lambda: self._add_palette_color(self._foreground_provider()[:3])
        )
        add_background.clicked.connect(
            lambda: self._add_palette_color(self._background_provider()[:3])
        )
        add_custom.clicked.connect(self._choose_palette_color)
        remove.clicked.connect(self._remove_palette_color)
        move_left.clicked.connect(lambda: self._move_palette_color(-1))
        move_right.clicked.connect(lambda: self._move_palette_color(1))
        self._refresh_palette()
        return page

    def _channel_combo(self, parent: QWidget) -> BlenderComboBox:
        combo = BlenderComboBox(parent)
        for label, channel in (
            ("RGB", ColorChannel.RGB),
            ("红", ColorChannel.RED),
            ("绿", ColorChannel.GREEN),
            ("蓝", ColorChannel.BLUE),
            ("Alpha", ColorChannel.ALPHA),
        ):
            combo.addItem(label, channel.value)
        return combo

    def _connect_controls(self) -> None:
        pass

    def showEvent(self, event: QShowEvent) -> None:
        if not getattr(self, "_signals_connected", False):
            self._signals_connected = True
            for control in (
                self.invert_channel,
                self.threshold,
                self.brightness,
                self.contrast,
                self.gamma,
                self.gamma_channel,
                self.hue,
                self.saturation,
                self.lightness,
                self.gradient_alpha,
                self.replace_tolerance,
                self.replace_softness,
                self.replace_include_alpha,
                self.replace_alpha,
                self.palette_dither,
            ):
                signal = (
                    control.currentIndexChanged
                    if isinstance(control, BlenderComboBox)
                    else control.valueChanged
                    if isinstance(control, (DragValueSpinBox, DragValueDoubleSpinBox))
                    else control.toggled
                )
                signal.connect(self._emit_parameters)
        super().showEvent(event)

    def _combo_channel(self, combo: BlenderComboBox) -> ColorChannel:
        return ColorChannel(str(combo.currentData()))

    def _curve_channel_changed(self, _index: int) -> None:
        self.curve_editor.set_curve(self._curve_values[self._combo_channel(self.curves_channel)])

    def _curve_changed(self, curve: object) -> None:
        if isinstance(curve, CurveDefinition):
            self._curve_values[self._combo_channel(self.curves_channel)] = curve
            self._emit_parameters()

    def _reset_curve(self) -> None:
        channel = self._combo_channel(self.curves_channel)
        self._curve_values[channel] = CurveDefinition()
        self.curve_editor.set_curve(self._curve_values[channel])
        self._emit_parameters()

    def _edit_gradient_color(self, index: int) -> None:
        stop = self.gradient_editor.stops[index]
        selected = self._dialogs.choose_color(
            self,
            QColor.fromRgb(*stop.color),
            "选择渐变颜色",
        )
        if selected is not None:
            self.gradient_editor.set_selected_color(_qcolor_rgba(selected))

    def _set_foreground_background_gradient(self) -> None:
        self.gradient_editor.set_stops(
            (
                GradientStop(0.0, self._foreground_provider()),
                GradientStop(1.0, self._background_provider()),
            )
        )
        self._emit_parameters()

    def _choose_replace_color(self, source: bool) -> None:
        current = self._replace_source if source else self._replace_target
        selected = self._dialogs.choose_color(
            self,
            QColor.fromRgb(*current),
            "选择源颜色" if source else "选择目标颜色",
        )
        if selected is None:
            return
        if source:
            self._replace_source = _qcolor_rgba(selected)
        else:
            self._replace_target = _qcolor_rgba(selected)
        self._refresh_replace_colors()
        self._emit_parameters()

    def _refresh_replace_colors(self) -> None:
        self._style_color_button(self.replace_source, self._replace_source)
        self._style_color_button(self.replace_target, self._replace_target)

    def _choose_palette_color(self) -> None:
        selected = self._dialogs.choose_color(
            self,
            QColor.fromRgb(*self._foreground_provider()),
            "添加调色板颜色",
        )
        if selected is not None:
            self._add_palette_color((selected.red(), selected.green(), selected.blue()))

    def _add_palette_color(self, color: RGBColor) -> None:
        if color in self._palette or len(self._palette) >= 32:
            return
        self._palette = (*self._palette, color)
        self._selected_palette = len(self._palette) - 1
        self._refresh_palette()
        self._emit_parameters()

    def _remove_palette_color(self) -> None:
        if len(self._palette) <= 2:
            return
        colors = list(self._palette)
        colors.pop(self._selected_palette)
        self._palette = tuple(colors)
        self._selected_palette = min(self._selected_palette, len(colors) - 1)
        self._refresh_palette()
        self._emit_parameters()

    def _select_palette(self, index: int) -> None:
        self._selected_palette = index
        self._refresh_palette()

    def _move_palette_color(self, offset: int) -> None:
        target = self._selected_palette + offset
        if not 0 <= target < len(self._palette):
            return
        colors = list(self._palette)
        colors[self._selected_palette], colors[target] = (
            colors[target],
            colors[self._selected_palette],
        )
        self._palette = tuple(colors)
        self._selected_palette = target
        self._refresh_palette()
        self._emit_parameters()

    def _edit_palette(self, index: int) -> None:
        color = self._palette[index]
        selected = self._dialogs.choose_color(
            self,
            QColor.fromRgb(*color),
            "编辑调色板颜色",
        )
        if selected is None:
            return
        replacement = (selected.red(), selected.green(), selected.blue())
        if replacement in self._palette and replacement != color:
            return
        colors = list(self._palette)
        colors[index] = replacement
        self._palette = tuple(colors)
        self._refresh_palette()
        self._emit_parameters()

    def _refresh_palette(self) -> None:
        while self.palette_grid.count():
            item = self.palette_grid.takeAt(0)
            widget = None if item is None else item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, color in enumerate(self._palette):
            button = QPushButton("", self)
            button.setFixedSize(30, 24)
            border = "#ff8a00" if index == self._selected_palette else "#111111"
            button.setStyleSheet(
                f"background-color: rgb{color}; border: 2px solid {border};"
            )
            button.clicked.connect(
                lambda _checked=False, selected=index: self._select_palette(selected)
            )
            button.installEventFilter(self)
            button.setProperty("paletteIndex", index)
            self.palette_grid.addWidget(button, index // 6, index % 6)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QPushButton) and watched.property("paletteIndex") is not None:
            if event.type() == QEvent.Type.MouseButtonDblClick:
                self._edit_palette(int(str(watched.property("paletteIndex"))))
                return True
        return super().eventFilter(watched, event)

    def _emit_parameters(self, _value: object = None) -> None:
        if self._loading:
            return
        parameters = self.parameters()
        self._settings.set_color_adjustment_state(
            self._kind.value,
            json.dumps(_serialize_parameters(parameters), ensure_ascii=False),
        )
        self._settings.sync()
        self.parameters_changed.emit(parameters)

    def _restore_state(self, kind: ColorAdjustmentKind) -> None:
        raw = self._settings.color_adjustment_state(kind.value)
        if not raw:
            return
        try:
            data = json.loads(raw)
            parameters = _deserialize_parameters(kind, data)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        self._loading = True
        try:
            self.set_parameters(parameters)
        finally:
            self._loading = False

    def _set_controls(self, parameters: ColorAdjustmentParameters) -> None:
        if isinstance(parameters, InvertAdjustment):
            self._set_combo_channel(self.invert_channel, parameters.channel)
        elif isinstance(parameters, ThresholdAdjustment):
            self.threshold.setValue(parameters.threshold)
        elif isinstance(parameters, BrightnessContrastAdjustment):
            self.brightness.setValue(parameters.brightness)
            self.contrast.setValue(parameters.contrast)
        elif isinstance(parameters, GammaAdjustment):
            self.gamma.setValue(parameters.gamma)
            self._set_combo_channel(self.gamma_channel, parameters.channel)
        elif isinstance(parameters, CurvesAdjustment):
            self._curve_values = {
                ColorChannel.RGB: parameters.rgb,
                ColorChannel.RED: parameters.red,
                ColorChannel.GREEN: parameters.green,
                ColorChannel.BLUE: parameters.blue,
                ColorChannel.ALPHA: parameters.alpha,
            }
            self.curve_editor.set_curve(
                self._curve_values[self._combo_channel(self.curves_channel)]
            )
        elif isinstance(parameters, HslAdjustment):
            self.hue.setValue(parameters.hue)
            self.saturation.setValue(parameters.saturation)
            self.lightness.setValue(parameters.lightness)
        elif isinstance(parameters, GradientMapAdjustment):
            self.gradient_editor.set_stops(parameters.stops)
            self.gradient_alpha.setChecked(parameters.use_alpha)
        elif isinstance(parameters, ColorReplaceAdjustment):
            self._replace_source = parameters.source
            self._replace_target = parameters.target
            self.replace_tolerance.setValue(parameters.tolerance)
            self.replace_softness.setValue(parameters.softness)
            self.replace_include_alpha.setChecked(parameters.include_alpha)
            self.replace_alpha.setChecked(parameters.replace_alpha)
            self._refresh_replace_colors()
        elif isinstance(parameters, PaletteMapAdjustment):
            self._palette = parameters.colors
            self.palette_dither.setChecked(parameters.dither)
            self._refresh_palette()

    @staticmethod
    def _set_combo_channel(combo: BlenderComboBox, channel: ColorChannel) -> None:
        combo.setCurrentIndex(max(0, combo.findData(channel.value)))

    @staticmethod
    def _style_color_button(button: QPushButton, color: RGBAColor) -> None:
        contrast = "#ffffff" if QColor.fromRgb(*color).lightness() < 128 else "#000000"
        button.setStyleSheet(
            "QPushButton {"
            f"background-color: rgba({color[0]}, {color[1]}, {color[2]}, {color[3]});"
            f"color: {contrast};"
            "}"
        )


def _qcolor_rgba(color: QColor) -> RGBAColor:
    return color.red(), color.green(), color.blue(), color.alpha()


def _serialize_curve(curve: CurveDefinition) -> list[list[int]]:
    return [[x, y] for x, y in curve.points]


def _serialize_parameters(parameters: ColorAdjustmentParameters) -> dict[str, object]:
    if isinstance(parameters, InvertAdjustment):
        return {"channel": parameters.channel.value}
    if isinstance(parameters, ThresholdAdjustment):
        return {"threshold": parameters.threshold}
    if isinstance(parameters, BrightnessContrastAdjustment):
        return {"brightness": parameters.brightness, "contrast": parameters.contrast}
    if isinstance(parameters, GammaAdjustment):
        return {"gamma": parameters.gamma, "channel": parameters.channel.value}
    if isinstance(parameters, CurvesAdjustment):
        return {
            "rgb": _serialize_curve(parameters.rgb),
            "red": _serialize_curve(parameters.red),
            "green": _serialize_curve(parameters.green),
            "blue": _serialize_curve(parameters.blue),
            "alpha": _serialize_curve(parameters.alpha),
        }
    if isinstance(parameters, HslAdjustment):
        return {
            "hue": parameters.hue,
            "saturation": parameters.saturation,
            "lightness": parameters.lightness,
        }
    if isinstance(parameters, GradientMapAdjustment):
        return {
            "stops": [
                [stop.position, *stop.color] for stop in parameters.stops
            ],
            "use_alpha": parameters.use_alpha,
        }
    if isinstance(parameters, ColorReplaceAdjustment):
        return {
            "source": list(parameters.source),
            "target": list(parameters.target),
            "tolerance": parameters.tolerance,
            "softness": parameters.softness,
            "include_alpha": parameters.include_alpha,
            "replace_alpha": parameters.replace_alpha,
        }
    return {"colors": [list(color) for color in parameters.colors], "dither": parameters.dither}


def _deserialize_parameters(
    kind: ColorAdjustmentKind,
    data: dict[str, object],
) -> ColorAdjustmentParameters:
    if kind is ColorAdjustmentKind.INVERT:
        return InvertAdjustment(ColorChannel(str(data["channel"])))
    if kind is ColorAdjustmentKind.THRESHOLD:
        return ThresholdAdjustment(int(str(data["threshold"])))
    if kind is ColorAdjustmentKind.BRIGHTNESS_CONTRAST:
        return BrightnessContrastAdjustment(
            int(str(data["brightness"])),
            int(str(data["contrast"])),
        )
    if kind is ColorAdjustmentKind.GAMMA:
        return GammaAdjustment(
            float(str(data["gamma"])),
            ColorChannel(str(data["channel"])),
        )
    if kind is ColorAdjustmentKind.CURVES:
        def curve(key: str) -> CurveDefinition:
            return CurveDefinition(
                tuple(
                    (int(point[0]), int(point[1]))
                    for point in cast(list[list[int]], data[key])
                )
            )

        return CurvesAdjustment(
            curve("rgb"),
            curve("red"),
            curve("green"),
            curve("blue"),
            curve("alpha"),
        )
    if kind is ColorAdjustmentKind.HSL:
        return HslAdjustment(
            int(str(data["hue"])),
            int(str(data["saturation"])),
            int(str(data["lightness"])),
        )
    if kind is ColorAdjustmentKind.GRADIENT_MAP:
        stops = tuple(
            GradientStop(
                float(item[0]),
                cast(RGBAColor, tuple(int(value) for value in item[1:5])),
            )
            for item in cast(list[list[float]], data["stops"])
        )
        return GradientMapAdjustment(stops, bool(data["use_alpha"]))
    if kind is ColorAdjustmentKind.COLOR_REPLACE:
        return ColorReplaceAdjustment(
            cast(RGBAColor, tuple(int(value) for value in cast(list[int], data["source"]))),
            cast(RGBAColor, tuple(int(value) for value in cast(list[int], data["target"]))),
            int(str(data["tolerance"])),
            int(str(data["softness"])),
            bool(data["include_alpha"]),
            bool(data["replace_alpha"]),
        )
    return PaletteMapAdjustment(
        tuple(
            cast(RGBColor, tuple(int(value) for value in color))
            for color in cast(list[list[int]], data["colors"])
        ),
        bool(data["dither"]),
    )


__all__ = ["ColorAdjustmentPanel", "CurveEditor", "GradientEditor"]
