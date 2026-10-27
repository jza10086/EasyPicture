"""Exact corner-coordinate editor for the active transform session."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from easypicture.ui.drag_value_spinbox import (
    DragValueDoubleSpinBox,
    NumericEditState,
)
from easypicture.ui.numeric_ranges import SIGNED_COORDINATE_RANGE

type CornerValues = tuple[float, float, float, float, float, float, float, float]
type PivotValues = tuple[float, float]


class TransformPropertiesPanel(QWidget):
    corners_changed = Signal(object)
    pivot_changed = Signal(object)
    edit_started = Signal(str)
    edit_finished = Signal()
    edit_canceled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._syncing = False
        self._corner_defaults_set = False
        self._pivot_defaults_set = False
        title = QLabel("自由变换角点", self)
        title.setObjectName("TransformPropertiesTitle")
        hint = QLabel(
            "关闭“不规则编辑”时，角点保持规则矩形；开启后可单独调整角点。",
            self,
        )
        hint.setWordWrap(True)
        hint.setObjectName("TransformPropertiesHint")

        pivot_grid = QGridLayout()
        pivot_grid.setContentsMargins(0, 0, 0, 0)
        pivot_grid.setHorizontalSpacing(6)
        pivot_grid.addWidget(QLabel("中心点", self), 0, 0)
        self._pivot_fields: list[DragValueDoubleSpinBox] = []
        for column, label in enumerate(("X", "Y"), start=1):
            field = self._make_coordinate_field()
            field.valueChanged.connect(self._emit_pivot)
            self._connect_edit_signals(field, f"中心点 {label}")
            pivot_grid.addWidget(QLabel(label, self), 0, column * 2 - 1)
            pivot_grid.addWidget(field, 0, column * 2)
            self._pivot_fields.append(field)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(5)
        grid.addWidget(QLabel("角点", self), 0, 0)
        grid.addWidget(QLabel("X", self), 0, 1)
        grid.addWidget(QLabel("Y", self), 0, 2)
        self._fields: list[DragValueDoubleSpinBox] = []
        for row, label in enumerate(("左上", "右上", "右下", "左下"), start=1):
            grid.addWidget(QLabel(label, self), row, 0)
            for column in (1, 2):
                field = self._make_coordinate_field()
                field.valueChanged.connect(self._emit_corners)
                axis = "X" if column == 1 else "Y"
                self._connect_edit_signals(field, f"{label} {axis}")
                grid.addWidget(field, row, column)
                self._fields.append(field)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addLayout(pivot_grid)
        layout.addLayout(grid)
        layout.addStretch(1)
        self.set_session_active(False)

    def corners(self) -> CornerValues:
        return tuple(field.value() for field in self._fields)  # type: ignore[return-value]

    def pivot(self) -> PivotValues:
        return tuple(field.value() for field in self._pivot_fields)  # type: ignore[return-value]

    def set_corners(self, values: CornerValues | None) -> None:
        if values is None:
            self.set_session_active(False)
            return
        self.set_session_active(True)
        if not self._corner_defaults_set:
            for field, value in zip(self._fields, values, strict=True):
                field.setDefaultValue(value)
            self._corner_defaults_set = True
        self._syncing = True
        try:
            for field, value in zip(self._fields, values, strict=True):
                if field.interaction_state is NumericEditState.IDLE:
                    field.setValue(value)
        finally:
            self._syncing = False

    def set_pivot(self, values: PivotValues | None) -> None:
        if values is None:
            self.set_session_active(False)
            return
        self.set_session_active(True)
        if not self._pivot_defaults_set:
            for field, value in zip(self._pivot_fields, values, strict=True):
                field.setDefaultValue(value)
            self._pivot_defaults_set = True
        self._syncing = True
        try:
            for field, value in zip(self._pivot_fields, values, strict=True):
                if field.interaction_state is NumericEditState.IDLE:
                    field.setValue(value)
        finally:
            self._syncing = False

    def set_session_active(self, active: bool) -> None:
        if not active:
            self._corner_defaults_set = False
            self._pivot_defaults_set = False
        for field in (*self._pivot_fields, *self._fields):
            field.setEnabled(active)
        self.setToolTip("" if active else "当前没有可编辑的自由变换会话")

    def _emit_corners(self, _value: float) -> None:
        if not self._syncing:
            self.corners_changed.emit(self.corners())

    def _emit_pivot(self, _value: float) -> None:
        if not self._syncing:
            self.pivot_changed.emit(self.pivot())

    def _make_coordinate_field(self) -> DragValueDoubleSpinBox:
        return DragValueDoubleSpinBox(
            SIGNED_COORDINATE_RANGE.soft_minimum,
            SIGNED_COORDINATE_RANGE.soft_maximum,
            0.0,
            manual_minimum=SIGNED_COORDINATE_RANGE.hard_minimum,
            manual_maximum=SIGNED_COORDINATE_RANGE.hard_maximum,
            suffix=" px",
            parent=self,
        )

    def _connect_edit_signals(
        self,
        field: DragValueDoubleSpinBox,
        label: str,
    ) -> None:
        field.interactionStarted.connect(lambda label=label: self.edit_started.emit(label))
        field.editingFinished.connect(self.edit_finished)
        field.interactionCanceled.connect(self.edit_canceled)


__all__ = ["CornerValues", "PivotValues", "TransformPropertiesPanel"]
