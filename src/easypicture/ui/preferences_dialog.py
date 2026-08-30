"""Application-wide appearance preferences."""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from easypicture.render import CanvasRenderDiagnostics, RenderBackendKind
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.drag_value_spinbox import (
    DragValueDoubleSpinBox,
    DragValueSpinBox,
)
from easypicture.ui.selection_style import (
    DEFAULT_SELECTION_OUTLINE_WIDTH,
    MAX_SELECTION_OUTLINE_WIDTH,
)
from easypicture.ui.settings import (
    DEFAULT_NUMERIC_DRAG_SENSITIVITY,
    DEFAULT_NUMERIC_FINE_DRAG_SENSITIVITY,
    MAX_NUMERIC_DRAG_SENSITIVITY,
    MIN_NUMERIC_DRAG_SENSITIVITY,
)


class PreferencesDialog(QDialog):
    """Edit application preferences that are not part of a project."""

    def __init__(
        self,
        selection_color: QColor,
        selection_width: int,
        render_backend: RenderBackendKind = RenderBackendKind.AUTO,
        render_diagnostics: CanvasRenderDiagnostics | None = None,
        parent: QWidget | None = None,
        *,
        numeric_drag_sensitivity: float = DEFAULT_NUMERIC_DRAG_SENSITIVITY,
        numeric_fine_drag_sensitivity: float = DEFAULT_NUMERIC_FINE_DRAG_SENSITIVITY,
        pixel_exact_coordinates: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("全局设置")
        self.setModal(True)
        self._selection_color = QColor(selection_color)
        self._selection_color.setAlpha(255)

        self.selection_color_button = QPushButton(self)
        self.selection_color_button.setToolTip("设置选区轮廓颜色")
        self.selection_color_button.clicked.connect(self._choose_selection_color)
        self._refresh_color_button()

        self.selection_width_spin = DragValueSpinBox(
            1,
            MAX_SELECTION_OUTLINE_WIDTH,
            selection_width,
            default_value=DEFAULT_SELECTION_OUTLINE_WIDTH,
            suffix=" px",
            parent=self,
        )
        self.selection_width_spin.setMinimumWidth(120)
        sensitivity_minimum = MIN_NUMERIC_DRAG_SENSITIVITY * 100.0
        sensitivity_maximum = MAX_NUMERIC_DRAG_SENSITIVITY * 100.0
        self.numeric_drag_sensitivity_spin = DragValueDoubleSpinBox(
            sensitivity_minimum,
            sensitivity_maximum,
            numeric_drag_sensitivity * 100.0,
            default_value=DEFAULT_NUMERIC_DRAG_SENSITIVITY * 100.0,
            suffix=" %",
            decimals=3,
            parent=self,
        )
        self.numeric_fine_drag_sensitivity_spin = DragValueDoubleSpinBox(
            sensitivity_minimum,
            sensitivity_maximum,
            numeric_fine_drag_sensitivity * 100.0,
            default_value=DEFAULT_NUMERIC_FINE_DRAG_SENSITIVITY * 100.0,
            suffix=" %",
            decimals=3,
            parent=self,
        )
        self.pixel_exact_coordinates_check = QCheckBox(
            "栅格工具使用整数坐标",
            self,
        )
        self.pixel_exact_coordinates_check.setChecked(pixel_exact_coordinates)

        self.render_backend_combo = BlenderComboBox(self)
        self.render_backend_combo.addItem("自动（推荐）", RenderBackendKind.AUTO)
        self.render_backend_combo.addItem("OpenGL", RenderBackendKind.OPENGL)
        self.render_backend_combo.addItem("软件", RenderBackendKind.SOFTWARE)
        backend_index = self.render_backend_combo.findData(render_backend)
        self.render_backend_combo.setCurrentIndex(max(0, backend_index))

        diagnostics = render_diagnostics
        if diagnostics is None:
            diagnostics_text = "尚未初始化"
        else:
            active = {
                RenderBackendKind.AUTO: "自动",
                RenderBackendKind.OPENGL: "OpenGL",
                RenderBackendKind.SOFTWARE: "软件",
            }[diagnostics.active_backend]
            detail = " / ".join(
                value
                for value in (
                    diagnostics.vendor,
                    diagnostics.renderer,
                    diagnostics.version,
                )
                if value
            )
            diagnostics_text = active if not detail else f"{active} — {detail}"
            if diagnostics.fallback_reason:
                diagnostics_text += f"\n回退原因：{diagnostics.fallback_reason}"
        self.render_diagnostics_label = QLabel(diagnostics_text, self)
        self.render_diagnostics_label.setWordWrap(True)

        form = QFormLayout()
        form.addRow("像素精确坐标", self.pixel_exact_coordinates_check)
        form.addRow("选区颜色", self.selection_color_button)
        form.addRow("选区粗细", self.selection_width_spin)
        form.addRow("数值拖动灵敏度", self.numeric_drag_sensitivity_spin)
        form.addRow("Shift 精细灵敏度", self.numeric_fine_drag_sensitivity_spin)
        form.addRow("渲染后端", self.render_backend_combo)
        form.addRow("当前状态", self.render_diagnostics_label)
        form.addRow("", QLabel("渲染后端切换将在重启应用后生效。", self))

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

    @property
    def selection_color(self) -> QColor:
        return QColor(self._selection_color)

    @property
    def selection_width(self) -> int:
        return self.selection_width_spin.value()

    @property
    def render_backend(self) -> RenderBackendKind:
        value = self.render_backend_combo.currentData()
        try:
            return RenderBackendKind(str(value))
        except ValueError:
            return RenderBackendKind.AUTO

    @property
    def numeric_drag_sensitivity(self) -> float:
        return self.numeric_drag_sensitivity_spin.value() / 100.0

    @property
    def numeric_fine_drag_sensitivity(self) -> float:
        return self.numeric_fine_drag_sensitivity_spin.value() / 100.0

    @property
    def pixel_exact_coordinates(self) -> bool:
        return self.pixel_exact_coordinates_check.isChecked()

    def _choose_selection_color(self) -> None:
        color = QColorDialog.getColor(
            self._selection_color,
            self,
            "选择选区轮廓颜色",
        )
        if not color.isValid():
            return
        color.setAlpha(255)
        self._selection_color = color
        self._refresh_color_button()

    def _refresh_color_button(self) -> None:
        value = self._selection_color.name(QColor.NameFormat.HexRgb).upper()
        foreground = "#111111" if self._selection_color.lightness() >= 145 else "#F2F2F2"
        self.selection_color_button.setText(value)
        self.selection_color_button.setStyleSheet(
            "QPushButton {"
            f"background-color: {value};"
            f"color: {foreground};"
            "border: 1px solid #151515;"
            "padding: 4px 16px;"
            "}"
        )


__all__ = ["PreferencesDialog"]
