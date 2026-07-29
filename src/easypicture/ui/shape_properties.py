"""Precise Blender-style property editor for an active shape session."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from easypicture.imaging import ShapeGeometry, ShapeKind
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.drag_value_spinbox import (
    DragValueDoubleSpinBox,
    NumericEditState,
)


class ShapePropertiesPanel(QWidget):
    geometry_changed = Signal(object)
    edit_started = Signal(str)
    edit_finished = Signal()
    edit_canceled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._geometry: ShapeGeometry | None = None
        self._syncing = False
        title = QLabel("图形属性", self)
        title.setObjectName("ShapePropertiesTitle")
        hint = QLabel("拖动数值可连续调整；活动字段不会被画布回写覆盖。", self)
        hint.setWordWrap(True)
        hint.setObjectName("ShapePropertiesHint")
        self._stack = QStackedWidget(self)
        self._line_page, self._line_fields = self._make_page(("X1", "Y1", "X2", "Y2"))
        self._box_page, self._box_fields = self._make_page(("X", "Y", "W", "H"))
        self._rounded_page, self._rounded_fields = self._make_page(
            ("X", "Y", "W", "H", "半径")
        )
        self._polygon_page = QWidget(self)
        polygon_layout = QGridLayout(self._polygon_page)
        polygon_layout.setContentsMargins(0, 0, 0, 0)
        self._polygon_fields = tuple(
            self._make_field(label) for label in ("X", "Y", "W", "H")
        )
        for index, (label, field) in enumerate(
            zip(("X", "Y", "W", "H"), self._polygon_fields, strict=True)
        ):
            polygon_layout.addWidget(QLabel(label, self), index, 0)
            polygon_layout.addWidget(field, index, 1)
        self.vertex_combo = BlenderComboBox(self)
        polygon_layout.addWidget(QLabel("顶点", self), 4, 0)
        polygon_layout.addWidget(self.vertex_combo, 4, 1)
        self.vertex_x = self._make_field("顶点 X")
        self.vertex_y = self._make_field("顶点 Y")
        polygon_layout.addWidget(QLabel("顶点 X", self), 5, 0)
        polygon_layout.addWidget(self.vertex_x, 5, 1)
        polygon_layout.addWidget(QLabel("顶点 Y", self), 6, 0)
        polygon_layout.addWidget(self.vertex_y, 6, 1)
        self.vertex_combo.currentIndexChanged.connect(self._sync_selected_vertex)
        self.vertex_x.valueChanged.connect(self._emit_vertex)
        self.vertex_y.valueChanged.connect(self._emit_vertex)
        for widget in (
            self._line_page,
            self._box_page,
            self._rounded_page,
            self._polygon_page,
        ):
            self._stack.addWidget(widget)
        for field in self._line_fields:
            field.valueChanged.connect(self._emit_line)
        for field in self._box_fields:
            field.valueChanged.connect(self._emit_box)
        for field in self._rounded_fields:
            field.valueChanged.connect(self._emit_rounded)
        for field in self._polygon_fields:
            field.valueChanged.connect(self._emit_polygon_bounds)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self._stack)
        layout.addStretch(1)
        self.set_geometry(None)

    def set_geometry(self, geometry: ShapeGeometry | None) -> None:
        self._geometry = geometry
        self.setEnabled(geometry is not None)
        if geometry is None:
            return
        self._syncing = True
        try:
            left, top, right, bottom = geometry.bounds
            if geometry.kind is ShapeKind.LINE:
                self._stack.setCurrentWidget(self._line_page)
                self._set_fields(
                    self._line_fields,
                    (*geometry.points[0], *geometry.points[1]),
                )
            elif geometry.kind is ShapeKind.ROUNDED_RECTANGLE:
                self._stack.setCurrentWidget(self._rounded_page)
                start_x, start_y = geometry.points[0]
                end_x, end_y = geometry.points[1]
                self._set_fields(
                    self._rounded_fields,
                    (
                        start_x,
                        start_y,
                        end_x - start_x,
                        end_y - start_y,
                        geometry.effective_corner_radius,
                    ),
                )
            elif geometry.kind is ShapeKind.POLYGON:
                self._stack.setCurrentWidget(self._polygon_page)
                self._set_fields(
                    self._polygon_fields,
                    (left, top, right - left, bottom - top),
                )
                if self.vertex_combo.count() != len(geometry.points):
                    current = max(0, self.vertex_combo.currentIndex())
                    self.vertex_combo.clear()
                    for index in range(len(geometry.points)):
                        self.vertex_combo.addItem(f"顶点 {index + 1}", index)
                    self.vertex_combo.setCurrentIndex(
                        min(current, len(geometry.points) - 1)
                    )
                self._sync_selected_vertex()
            else:
                self._stack.setCurrentWidget(self._box_page)
                start_x, start_y = geometry.points[0]
                end_x, end_y = geometry.points[1]
                self._set_fields(
                    self._box_fields,
                    (
                        start_x,
                        start_y,
                        end_x - start_x,
                        end_y - start_y,
                    ),
                )
        finally:
            self._syncing = False

    def _make_page(
        self,
        labels: tuple[str, ...],
    ) -> tuple[QWidget, tuple[DragValueDoubleSpinBox, ...]]:
        page = QWidget(self)
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        fields = tuple(self._make_field(label) for label in labels)
        for row, (label, field) in enumerate(zip(labels, fields, strict=True)):
            layout.addWidget(QLabel(label, page), row, 0)
            layout.addWidget(field, row, 1)
        return page, fields

    def _make_field(self, label: str) -> DragValueDoubleSpinBox:
        field = DragValueDoubleSpinBox(
            -8192.0,
            8192.0,
            0.0,
            manual_minimum=-32768.0,
            manual_maximum=32768.0,
            suffix=" px",
            parent=self,
        )
        field.interactionStarted.connect(
            lambda label=label: self.edit_started.emit(label)
        )
        field.editingFinished.connect(self.edit_finished)
        field.interactionCanceled.connect(self.edit_canceled)
        return field

    def _set_fields(
        self,
        fields: tuple[DragValueDoubleSpinBox, ...],
        values: tuple[float, ...],
    ) -> None:
        for field, value in zip(fields, values, strict=True):
            if field.interaction_state is NumericEditState.IDLE:
                field.setValue(value)

    def _emit_line(self, _value: float) -> None:
        if self._syncing or self._geometry is None:
            return
        values = tuple(field.value() for field in self._line_fields)
        try:
            self._try_emit(
                ShapeGeometry(
                    ShapeKind.LINE,
                    ((values[0], values[1]), (values[2], values[3])),
                )
            )
        except ValueError:
            return

    def _emit_box(self, _value: float) -> None:
        if self._syncing or self._geometry is None:
            return
        x, y, width, height = (field.value() for field in self._box_fields)
        if width != 0.0 and height != 0.0:
            try:
                self._try_emit(
                    ShapeGeometry(
                        self._geometry.kind,
                        ((x, y), (x + width, y + height)),
                    )
                )
            except ValueError:
                return

    def _emit_rounded(self, _value: float) -> None:
        if self._syncing:
            return
        x, y, width, height, radius = (
            field.value() for field in self._rounded_fields
        )
        if width != 0.0 and height != 0.0:
            try:
                self._try_emit(
                    ShapeGeometry(
                        ShapeKind.ROUNDED_RECTANGLE,
                        ((x, y), (x + width, y + height)),
                        max(0.0, radius),
                    )
                )
            except ValueError:
                return

    def _emit_polygon_bounds(self, _value: float) -> None:
        geometry = self._geometry
        if self._syncing or geometry is None or geometry.kind is not ShapeKind.POLYGON:
            return
        old_left, old_top, old_right, old_bottom = geometry.bounds
        x, y, width, height = (field.value() for field in self._polygon_fields)
        old_width = old_right - old_left
        old_height = old_bottom - old_top
        if width == 0.0 or height == 0.0 or old_width == 0.0 or old_height == 0.0:
            return
        points = tuple(
            (
                x + (px - old_left) / old_width * width,
                y + (py - old_top) / old_height * height,
            )
            for px, py in geometry.points
        )
        self._try_emit(ShapeGeometry(ShapeKind.POLYGON, points))

    def _sync_selected_vertex(self, _index: int = 0) -> None:
        geometry = self._geometry
        if geometry is None or geometry.kind is not ShapeKind.POLYGON:
            return
        index = max(0, self.vertex_combo.currentIndex())
        if index >= len(geometry.points):
            return
        self._syncing = True
        try:
            self._set_fields((self.vertex_x, self.vertex_y), geometry.points[index])
        finally:
            self._syncing = False

    def _emit_vertex(self, _value: float) -> None:
        geometry = self._geometry
        if self._syncing or geometry is None or geometry.kind is not ShapeKind.POLYGON:
            return
        index = self.vertex_combo.currentIndex()
        if index < 0 or index >= len(geometry.points):
            return
        points = list(geometry.points)
        points[index] = (self.vertex_x.value(), self.vertex_y.value())
        try:
            self._try_emit(ShapeGeometry(ShapeKind.POLYGON, tuple(points)))
        except ValueError:
            return

    def _try_emit(self, geometry: ShapeGeometry) -> None:
        self._geometry = geometry
        self.geometry_changed.emit(geometry)


__all__ = ["ShapePropertiesPanel"]
