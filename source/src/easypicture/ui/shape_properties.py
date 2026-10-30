"""Precise Blender-style property editor for an active shape session."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
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
from easypicture.ui.numeric_ranges import (
    OBJECT_RADIUS_RANGE,
    SIGNED_COORDINATE_RANGE,
    sync_spatial_range,
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
        self._rounded_page, self._rounded_fields = self._make_page(("X", "Y", "W", "H", "半径"))
        self._polygon_page = QWidget(self)
        polygon_layout = QGridLayout(self._polygon_page)
        polygon_layout.setContentsMargins(0, 0, 0, 0)
        self._polygon_fields = tuple(self._make_field(label) for label in ("X", "Y", "W", "H"))
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
        self.vertex_count_label = QLabel(self)
        polygon_layout.addWidget(QLabel("顶点总数", self), 7, 0)
        polygon_layout.addWidget(self.vertex_count_label, 7, 1)
        vertex_actions = QWidget(self)
        vertex_actions_layout = QHBoxLayout(vertex_actions)
        vertex_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.add_vertex_button = QPushButton("添加", vertex_actions)
        self.delete_vertex_button = QPushButton("删除", vertex_actions)
        self.move_vertex_up_button = QPushButton("上移", vertex_actions)
        self.move_vertex_down_button = QPushButton("下移", vertex_actions)
        for button in (
            self.add_vertex_button,
            self.delete_vertex_button,
            self.move_vertex_up_button,
            self.move_vertex_down_button,
        ):
            vertex_actions_layout.addWidget(button)
        polygon_layout.addWidget(vertex_actions, 8, 0, 1, 2)
        self.vertex_combo.currentIndexChanged.connect(self._sync_selected_vertex)
        self.vertex_x.valueChanged.connect(self._emit_vertex)
        self.vertex_y.valueChanged.connect(self._emit_vertex)
        self.add_vertex_button.clicked.connect(self._add_vertex)
        self.delete_vertex_button.clicked.connect(self._delete_vertex)
        self.move_vertex_up_button.clicked.connect(lambda: self._move_vertex(-1))
        self.move_vertex_down_button.clicked.connect(lambda: self._move_vertex(1))
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
        self._rounded_fields[2].valueChanged.connect(self._rounded_size_changed)
        self._rounded_fields[3].valueChanged.connect(self._rounded_size_changed)
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

    def set_canvas_size(self, width: int, height: int) -> None:
        for fields in (
            self._line_fields,
            self._box_fields,
            self._rounded_fields[:4],
            self._polygon_fields,
        ):
            for index, field in enumerate(fields):
                sync_spatial_range(field, width if index % 2 == 0 else height)
        sync_spatial_range(self.vertex_x, width)
        sync_spatial_range(self.vertex_y, height)

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
                self._set_rounded_radius_range(
                    end_x - start_x,
                    end_y - start_y,
                )
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
            elif geometry.kind in (ShapeKind.POLYGON, ShapeKind.POLYLINE):
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
                    self.vertex_combo.setCurrentIndex(min(current, len(geometry.points) - 1))
                self._sync_selected_vertex()
                self.vertex_count_label.setText(str(len(geometry.points)))
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
            SIGNED_COORDINATE_RANGE.soft_minimum,
            SIGNED_COORDINATE_RANGE.soft_maximum,
            0.0,
            manual_minimum=SIGNED_COORDINATE_RANGE.hard_minimum,
            manual_maximum=SIGNED_COORDINATE_RANGE.hard_maximum,
            suffix=" px",
            default_value=32.0 if label in {"W", "H", "X2", "Y2"} else 0.0,
            parent=self,
        )
        field.interactionStarted.connect(lambda label=label: self.edit_started.emit(label))
        field.editingFinished.connect(self.edit_finished)
        field.interactionCanceled.connect(self.edit_canceled)
        return field

    def _rounded_size_changed(self, _value: float) -> None:
        if self._syncing:
            return
        self._set_rounded_radius_range(
            self._rounded_fields[2].value(),
            self._rounded_fields[3].value(),
        )

    def _set_rounded_radius_range(self, width: float, height: float) -> None:
        maximum = min(abs(width), abs(height)) / 2.0
        was_syncing = self._syncing
        self._syncing = True
        try:
            self._rounded_fields[4].setPropertyRange(
                OBJECT_RADIUS_RANGE.soft_minimum,
                maximum,
                manual_minimum=OBJECT_RADIUS_RANGE.hard_minimum,
                manual_maximum=maximum,
            )
        finally:
            self._syncing = was_syncing

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
        x, y, width, height, radius = (field.value() for field in self._rounded_fields)
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
        if (
            self._syncing
            or geometry is None
            or geometry.kind not in (ShapeKind.POLYGON, ShapeKind.POLYLINE)
        ):
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
        self._try_emit(ShapeGeometry(geometry.kind, points))

    def _sync_selected_vertex(self, _index: int = 0) -> None:
        geometry = self._geometry
        if geometry is None or geometry.kind not in (
            ShapeKind.POLYGON,
            ShapeKind.POLYLINE,
        ):
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
        if (
            self._syncing
            or geometry is None
            or geometry.kind not in (ShapeKind.POLYGON, ShapeKind.POLYLINE)
        ):
            return
        index = self.vertex_combo.currentIndex()
        if index < 0 or index >= len(geometry.points):
            return
        points = list(geometry.points)
        points[index] = (self.vertex_x.value(), self.vertex_y.value())
        try:
            self._try_emit(ShapeGeometry(geometry.kind, tuple(points)))
        except ValueError:
            return

    def _add_vertex(self) -> None:
        geometry = self._point_geometry()
        if geometry is None:
            return
        index = max(0, self.vertex_combo.currentIndex())
        next_index = (index + 1) % len(geometry.points)
        first = geometry.points[index]
        second = geometry.points[next_index]
        point = ((first[0] + second[0]) / 2.0, (first[1] + second[1]) / 2.0)
        points = list(geometry.points)
        points.insert(index + 1, point)
        try:
            self._try_emit(ShapeGeometry(geometry.kind, tuple(points)))
        except ValueError:
            return
        self.vertex_combo.setCurrentIndex(index + 1)

    def _delete_vertex(self) -> None:
        geometry = self._point_geometry()
        if geometry is None:
            return
        minimum = 2 if geometry.kind is ShapeKind.POLYLINE else 3
        if len(geometry.points) <= minimum:
            return
        index = max(0, self.vertex_combo.currentIndex())
        points = list(geometry.points)
        del points[index]
        try:
            self._try_emit(ShapeGeometry(geometry.kind, tuple(points)))
        except ValueError:
            return

    def _move_vertex(self, delta: int) -> None:
        geometry = self._point_geometry()
        if geometry is None:
            return
        index = self.vertex_combo.currentIndex()
        target = index + delta
        if index < 0 or not 0 <= target < len(geometry.points):
            return
        points = list(geometry.points)
        points[index], points[target] = points[target], points[index]
        try:
            self._try_emit(ShapeGeometry(geometry.kind, tuple(points)))
        except ValueError:
            return
        self.vertex_combo.setCurrentIndex(target)

    def _point_geometry(self) -> ShapeGeometry | None:
        geometry = self._geometry
        if geometry is None or geometry.kind not in (
            ShapeKind.POLYGON,
            ShapeKind.POLYLINE,
        ):
            return None
        return geometry

    def _try_emit(self, geometry: ShapeGeometry) -> None:
        self._geometry = geometry
        self.geometry_changed.emit(geometry)


__all__ = ["ShapePropertiesPanel"]
