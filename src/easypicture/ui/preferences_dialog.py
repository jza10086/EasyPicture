"""Application-wide appearance preferences."""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.selection_style import MAX_SELECTION_OUTLINE_WIDTH


class PreferencesDialog(QDialog):
    """Edit application preferences that are not part of a project."""

    def __init__(
        self,
        selection_color: QColor,
        selection_width: int,
        parent: QWidget | None = None,
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
            suffix=" px",
            parent=self,
        )
        self.selection_width_spin.setMinimumWidth(120)

        form = QFormLayout()
        form.addRow("选区颜色", self.selection_color_button)
        form.addRow("选区粗细", self.selection_width_spin)

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
