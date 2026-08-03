"""Blender-style geometry and paint editor for static SVG child objects."""

from __future__ import annotations

import math
from dataclasses import replace
from uuid import UUID

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from easypicture.commands import DocumentCommandService
from easypicture.domain import (
    AffineTransform,
    EditableSvgPrimitive,
    Point,
    SvgCircleGeometry,
    SvgElementObject,
    SvgEllipseGeometry,
    SvgFillRule,
    SvgLineCap,
    SvgLineGeometry,
    SvgLineJoin,
    SvgPolygonGeometry,
    SvgPolylineGeometry,
    SvgPrimitiveGeometry,
    SvgPrimitiveStyle,
    SvgRectGeometry,
    parse_editable_svg,
    primitive_bounds,
    update_editable_svg,
)
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.color_dialog import ColorPickerDialog
from easypicture.ui.drag_value_spinbox import (
    DragValueDoubleSpinBox,
    NumericEditState,
)
from easypicture.ui.numeric_ranges import (
    DASH_LENGTH_RANGE,
    DASH_OFFSET_RANGE,
    MITER_LIMIT_RANGE,
    OBJECT_DIMENSION_RANGE,
    OBJECT_RADIUS_RANGE,
    ROTATION_DEGREES_RANGE,
    SIGNED_COORDINATE_RANGE,
    STROKE_WIDTH_RANGE,
    UiNumericRange,
)


class SvgPropertiesPanel(QWidget):
    object_changed = Signal(object)
    preview_changed = Signal(object)
    preview_cleared = Signal()

    def __init__(
        self,
        commands: DocumentCommandService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands = commands
        self._object: SvgElementObject | None = None
        self._primitive: EditableSvgPrimitive | None = None
        self._preview_object: SvgElementObject | None = None
        self._syncing = False
        self._interaction_active = False
        self._fill_color = QColor(255, 255, 255, 255)
        self._stroke_color = QColor(0, 0, 0, 255)
        self._resource_source_svg: str | None = None
        self._resource_object_id: UUID | None = None

        self.name_edit = QLineEdit(self)
        self.kind_label = QLabel(self)
        self.x_spin = self._range_field(SIGNED_COORDINATE_RANGE)
        self.y_spin = self._range_field(SIGNED_COORDINATE_RANGE)
        self.width_spin = self._range_field(OBJECT_DIMENSION_RANGE)
        self.height_spin = self._range_field(OBJECT_DIMENSION_RANGE)
        self.rotation_spin = self._range_field(ROTATION_DEGREES_RANGE, suffix="°")
        self.pivot_x_spin = self._range_field(SIGNED_COORDINATE_RANGE)
        self.pivot_y_spin = self._range_field(SIGNED_COORDINATE_RANGE)
        self.opacity_spin = self._field(0.0, 100.0, suffix="%")
        self.visible_check = QCheckBox("可见", self)
        self.locked_check = QCheckBox("锁定对象", self)

        self.geometry_stack = QStackedWidget(self)
        self.geometry_none = QLabel("该 SVG 图元暂不支持直接几何编辑", self)
        self.geometry_none.setWordWrap(True)
        self.geometry_stack.addWidget(self.geometry_none)
        self.rect_page, self.rect_fields = self._geometry_page(
            ("X", "Y", "宽度", "高度", "圆角 X", "圆角 Y"),
            (
                SIGNED_COORDINATE_RANGE,
                SIGNED_COORDINATE_RANGE,
                OBJECT_DIMENSION_RANGE,
                OBJECT_DIMENSION_RANGE,
                OBJECT_RADIUS_RANGE,
                OBJECT_RADIUS_RANGE,
            ),
        )
        self.circle_page, self.circle_fields = self._geometry_page(
            ("中心 X", "中心 Y", "半径"),
            (SIGNED_COORDINATE_RANGE, SIGNED_COORDINATE_RANGE, OBJECT_RADIUS_RANGE),
        )
        self.ellipse_page, self.ellipse_fields = self._geometry_page(
            ("中心 X", "中心 Y", "半径 X", "半径 Y"),
            (
                SIGNED_COORDINATE_RANGE,
                SIGNED_COORDINATE_RANGE,
                OBJECT_RADIUS_RANGE,
                OBJECT_RADIUS_RANGE,
            ),
        )
        self.line_page, self.line_fields = self._geometry_page(
            ("X1", "Y1", "X2", "Y2"),
            (SIGNED_COORDINATE_RANGE,) * 4,
        )
        self.points_page = QWidget(self)
        points_form = QFormLayout(self.points_page)
        points_form.setContentsMargins(0, 0, 0, 0)
        self.vertex_combo = BlenderComboBox(self)
        self.vertex_x_spin = self._range_field(SIGNED_COORDINATE_RANGE)
        self.vertex_y_spin = self._range_field(SIGNED_COORDINATE_RANGE)
        self.vertex_count_label = QLabel(self)
        points_form.addRow("顶点", self.vertex_combo)
        points_form.addRow("顶点 X", self.vertex_x_spin)
        points_form.addRow("顶点 Y", self.vertex_y_spin)
        points_form.addRow("顶点总数", self.vertex_count_label)
        point_buttons = QWidget(self)
        point_buttons_layout = QHBoxLayout(point_buttons)
        point_buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.add_vertex_button = QPushButton("添加", point_buttons)
        self.delete_vertex_button = QPushButton("删除", point_buttons)
        self.vertex_up_button = QPushButton("上移", point_buttons)
        self.vertex_down_button = QPushButton("下移", point_buttons)
        for button in (
            self.add_vertex_button,
            self.delete_vertex_button,
            self.vertex_up_button,
            self.vertex_down_button,
        ):
            point_buttons_layout.addWidget(button)
        points_form.addRow(point_buttons)
        for page in (
            self.rect_page,
            self.circle_page,
            self.ellipse_page,
            self.line_page,
            self.points_page,
        ):
            self.geometry_stack.addWidget(page)

        self.fill_mode = BlenderComboBox(self)
        self.stroke_mode = BlenderComboBox(self)
        for combo in (self.fill_mode, self.stroke_mode):
            combo.addItem("无", "none")
            combo.addItem("纯色", "solid")
            combo.addItem("资源（只读）", "resource")
        self.fill_button = QPushButton("填充颜色", self)
        self.stroke_button = QPushButton("描边颜色", self)
        self.reset_fill_resource_button = QPushButton("重置填充覆盖", self)
        self.reset_stroke_resource_button = QPushButton("重置描边覆盖", self)
        self.stroke_width_spin = self._range_field(STROKE_WIDTH_RANGE, suffix=" px")
        self.line_cap_combo = BlenderComboBox(self)
        for label, value in (("平头", "butt"), ("圆头", "round"), ("方头", "square")):
            self.line_cap_combo.addItem(label, value)
        self.line_join_combo = BlenderComboBox(self)
        for label, value in (("斜接", "miter"), ("圆角", "round"), ("斜角", "bevel")):
            self.line_join_combo.addItem(label, value)
        self.miter_limit_spin = self._range_field(MITER_LIMIT_RANGE)
        self.fill_rule_combo = BlenderComboBox(self)
        self.fill_rule_combo.addItem("非零", "nonzero")
        self.fill_rule_combo.addItem("奇偶", "evenodd")
        self.dash_list = QListWidget(self)
        self.dash_list.setMaximumHeight(92)
        self.dash_value_spin = self._range_field(DASH_LENGTH_RANGE, suffix=" px")
        self.dash_offset_spin = self._range_field(DASH_OFFSET_RANGE, suffix=" px")
        dash_buttons = QWidget(self)
        dash_buttons_layout = QHBoxLayout(dash_buttons)
        dash_buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.add_dash_button = QPushButton("添加", dash_buttons)
        self.delete_dash_button = QPushButton("删除", dash_buttons)
        self.dash_up_button = QPushButton("上移", dash_buttons)
        self.dash_down_button = QPushButton("下移", dash_buttons)
        for button in (
            self.add_dash_button,
            self.delete_dash_button,
            self.dash_up_button,
            self.dash_down_button,
        ):
            dash_buttons_layout.addWidget(button)

        self.transform_form = QFormLayout()
        self.transform_form.addRow("名称", self.name_edit)
        self.transform_form.addRow("SVG 元素", self.kind_label)
        self.transform_form.addRow("位置 X", self.x_spin)
        self.transform_form.addRow("位置 Y", self.y_spin)
        self.transform_form.addRow("宽度", self.width_spin)
        self.transform_form.addRow("高度", self.height_spin)
        self.transform_form.addRow("旋转", self.rotation_spin)
        self.transform_form.addRow("旋转中心 X", self.pivot_x_spin)
        self.transform_form.addRow("旋转中心 Y", self.pivot_y_spin)
        self.transform_form.addRow("不透明度", self.opacity_spin)
        self.transform_form.addRow(self.visible_check)
        self.transform_form.addRow(self.locked_check)
        style_form = QFormLayout()
        style_form.addRow("填充", self.fill_mode)
        style_form.addRow("填充颜色", self.fill_button)
        style_form.addRow(self.reset_fill_resource_button)
        style_form.addRow("描边", self.stroke_mode)
        style_form.addRow("描边颜色", self.stroke_button)
        style_form.addRow(self.reset_stroke_resource_button)
        style_form.addRow("描边宽度", self.stroke_width_spin)
        style_form.addRow("端点", self.line_cap_combo)
        style_form.addRow("连接", self.line_join_combo)
        style_form.addRow("斜接限制", self.miter_limit_spin)
        style_form.addRow("填充规则", self.fill_rule_combo)
        style_form.addRow("虚线段", self.dash_list)
        style_form.addRow("段长度", self.dash_value_spin)
        style_form.addRow(dash_buttons)
        style_form.addRow("虚线偏移", self.dash_offset_spin)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(QLabel("SVG 矢量属性", self))
        layout.addLayout(self.transform_form)
        self.geometry_title = QLabel("图元几何", self)
        layout.addWidget(self.geometry_title)
        layout.addWidget(self.geometry_stack)
        layout.addWidget(QLabel("填充与描边", self))
        layout.addLayout(style_form)
        layout.addStretch(1)
        self._connect_signals()
        self.set_object(None)

    def _connect_signals(self) -> None:
        self.name_edit.editingFinished.connect(self._commit_candidate)
        self.rect_fields[2].valueChanged.connect(self._rect_size_changed)
        self.rect_fields[3].valueChanged.connect(self._rect_size_changed)
        fields = (
            self.x_spin,
            self.y_spin,
            self.width_spin,
            self.height_spin,
            self.rotation_spin,
            self.pivot_x_spin,
            self.pivot_y_spin,
            self.opacity_spin,
            self.stroke_width_spin,
            self.miter_limit_spin,
            self.dash_offset_spin,
            self.vertex_x_spin,
            self.vertex_y_spin,
            self.dash_value_spin,
            *self.rect_fields,
            *self.circle_fields,
            *self.ellipse_fields,
            *self.line_fields,
        )
        for field in fields:
            field.valueChanged.connect(self._preview_from_controls)
            field.interactionStarted.connect(self._begin_interaction)
            field.editingFinished.connect(self._finish_interaction)
            field.interactionCanceled.connect(self._cancel_interaction)
        for combo in (
            self.fill_mode,
            self.stroke_mode,
            self.line_cap_combo,
            self.line_join_combo,
            self.fill_rule_combo,
        ):
            combo.currentIndexChanged.connect(self._commit_candidate)
        self.visible_check.toggled.connect(self._commit_candidate)
        self.locked_check.toggled.connect(self._commit_candidate)
        self.fill_button.clicked.connect(lambda: self._pick_color("fill"))
        self.stroke_button.clicked.connect(lambda: self._pick_color("stroke"))
        self.reset_fill_resource_button.clicked.connect(lambda: self._reset_resource("fill"))
        self.reset_stroke_resource_button.clicked.connect(
            lambda: self._reset_resource("stroke")
        )
        self.vertex_combo.currentIndexChanged.connect(self._sync_selected_vertex)
        self.dash_list.currentRowChanged.connect(self._sync_selected_dash)
        self.add_vertex_button.clicked.connect(self._add_vertex)
        self.delete_vertex_button.clicked.connect(self._delete_vertex)
        self.vertex_up_button.clicked.connect(lambda: self._move_vertex(-1))
        self.vertex_down_button.clicked.connect(lambda: self._move_vertex(1))
        self.add_dash_button.clicked.connect(self._add_dash)
        self.delete_dash_button.clicked.connect(self._delete_dash)
        self.dash_up_button.clicked.connect(lambda: self._move_dash(-1))
        self.dash_down_button.clicked.connect(lambda: self._move_dash(1))

    def _field(
        self,
        minimum: float,
        maximum: float,
        *,
        suffix: str = "",
        manual_minimum: float | None = None,
        manual_maximum: float | None = None,
    ) -> DragValueDoubleSpinBox:
        return DragValueDoubleSpinBox(
            minimum,
            maximum,
            0.0,
            manual_minimum=minimum if manual_minimum is None else manual_minimum,
            manual_maximum=maximum if manual_maximum is None else manual_maximum,
            decimals=2,
            suffix=suffix,
            parent=self,
        )

    def _range_field(
        self,
        numeric_range: UiNumericRange,
        *,
        suffix: str = "",
    ) -> DragValueDoubleSpinBox:
        return self._field(
            numeric_range.soft_minimum,
            numeric_range.soft_maximum,
            suffix=suffix,
            manual_minimum=numeric_range.hard_minimum,
            manual_maximum=numeric_range.hard_maximum,
        )

    def _geometry_page(
        self,
        labels: tuple[str, ...],
        ranges: tuple[UiNumericRange, ...],
    ) -> tuple[QWidget, tuple[DragValueDoubleSpinBox, ...]]:
        page = QWidget(self)
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        fields = tuple(
            self._range_field(numeric_range, suffix=" px")
            for numeric_range in ranges
        )
        for label, field in zip(labels, fields, strict=True):
            form.addRow(label, field)
        return page, fields

    def _set_common_bounds_visible(self, visible: bool) -> None:
        for field in (self.x_spin, self.y_spin, self.width_spin, self.height_spin):
            self._set_form_row_visible(self.transform_form, field, visible)

    def _set_geometry_rows_visible(
        self,
        page: QWidget,
        fields: tuple[DragValueDoubleSpinBox, ...],
        visible: bool,
    ) -> None:
        form = page.layout()
        if not isinstance(form, QFormLayout):
            return
        for field in fields:
            self._set_form_row_visible(form, field, visible)

    @staticmethod
    def _set_form_row_visible(
        form: QFormLayout,
        field: QWidget,
        visible: bool,
    ) -> None:
        field.setVisible(visible)
        label = form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    def set_object(self, vector_object: SvgElementObject | None) -> None:
        if vector_object is None or vector_object.id != self._resource_object_id:
            original = parse_editable_svg(vector_object.svg) if vector_object else None
            self._resource_object_id = vector_object.id if vector_object else None
            self._resource_source_svg = (
                vector_object.svg
                if vector_object is not None
                and original is not None
                and (original.fill_resource is not None or original.stroke_resource is not None)
                else None
            )
        self._object = vector_object
        self._preview_object = None
        self._primitive = parse_editable_svg(vector_object.svg) if vector_object else None
        was_syncing = self._syncing
        self._syncing = True
        try:
            self.setEnabled(vector_object is not None)
            if vector_object is None:
                return
            self._sync_transform(vector_object)
            self._sync_primitive(self._primitive)
        finally:
            self._syncing = was_syncing

    def set_preview_object(self, vector_object: SvgElementObject | None) -> None:
        if vector_object is None:
            self._preview_object = None
            if self._object is not None:
                self.set_object(self._object)
            return
        if self._object is None or vector_object.id != self._object.id:
            return
        self._preview_object = vector_object
        self._primitive = parse_editable_svg(vector_object.svg)
        was_syncing = self._syncing
        self._syncing = True
        try:
            self._sync_transform(vector_object)
            self._sync_primitive(self._primitive)
        finally:
            self._syncing = was_syncing

    def _sync_transform(self, vector_object: SvgElementObject) -> None:
        bounds = vector_object.intrinsic_bounds
        matrix = vector_object.transform
        top_left = matrix.map_point(Point(bounds.x, bounds.y))
        width = bounds.width * math.hypot(matrix.a, matrix.b)
        height = bounds.height * math.hypot(matrix.c, matrix.d)
        pivot = vector_object.transform_pivot or matrix.map_point(
            Point(bounds.x + bounds.width / 2.0, bounds.y + bounds.height / 2.0)
        )
        self.name_edit.setText(vector_object.name)
        self.kind_label.setText(
            {
                "rect": "矩形",
                "circle": "圆形",
                "ellipse": "椭圆",
                "line": "直线",
                "polyline": "折线",
                "polygon": "多边形",
            }.get(vector_object.element_kind, vector_object.element_kind)
        )
        self._set_field(self.x_spin, top_left.x)
        self._set_field(self.y_spin, top_left.y)
        self._set_field(self.width_spin, width)
        self._set_field(self.height_spin, height)
        self._set_field(self.rotation_spin, math.degrees(math.atan2(matrix.b, matrix.a)))
        self._set_field(self.pivot_x_spin, pivot.x)
        self._set_field(self.pivot_y_spin, pivot.y)
        self._set_field(self.opacity_spin, vector_object.opacity * 100.0)
        self.visible_check.setChecked(vector_object.visible)
        self.locked_check.setChecked(vector_object.locked)

    def _sync_primitive(self, primitive: EditableSvgPrimitive | None) -> None:
        self.geometry_stack.setCurrentWidget(self.geometry_none)
        self._set_common_bounds_visible(True)
        self.geometry_title.show()
        self.geometry_stack.show()
        self._set_geometry_rows_visible(self.rect_page, self.rect_fields, True)
        self._set_geometry_rows_visible(self.circle_page, self.circle_fields, True)
        self._set_geometry_rows_visible(self.ellipse_page, self.ellipse_fields, True)
        self._set_geometry_rows_visible(self.line_page, self.line_fields, True)
        if primitive is None:
            for widget in (
                self.fill_mode,
                self.stroke_mode,
                self.fill_button,
                self.stroke_button,
                self.reset_fill_resource_button,
                self.reset_stroke_resource_button,
                self.stroke_width_spin,
                self.line_cap_combo,
                self.line_join_combo,
                self.miter_limit_spin,
                self.fill_rule_combo,
                self.dash_list,
                self.dash_value_spin,
                self.dash_offset_spin,
            ):
                widget.setEnabled(False)
            return
        for widget in (
            self.fill_mode,
            self.stroke_mode,
            self.fill_button,
            self.stroke_button,
            self.reset_fill_resource_button,
            self.reset_stroke_resource_button,
            self.stroke_width_spin,
            self.line_cap_combo,
            self.line_join_combo,
            self.miter_limit_spin,
            self.fill_rule_combo,
            self.dash_list,
            self.dash_value_spin,
            self.dash_offset_spin,
        ):
            widget.setEnabled(True)
        geometry = primitive.geometry
        if isinstance(geometry, SvgRectGeometry):
            self.geometry_stack.setCurrentWidget(self.rect_page)
            self._set_geometry_rows_visible(self.rect_page, self.rect_fields[:4], False)
            self._set_rect_corner_ranges(geometry.width, geometry.height)
            self._set_fields(
                self.rect_fields,
                (geometry.x, geometry.y, geometry.width, geometry.height, geometry.rx, geometry.ry),
            )
        elif isinstance(geometry, SvgCircleGeometry):
            self.geometry_stack.setCurrentWidget(self.circle_page)
            self._set_fields(self.circle_fields, (geometry.cx, geometry.cy, geometry.r))
            self.geometry_title.hide()
            self.geometry_stack.hide()
        elif isinstance(geometry, SvgEllipseGeometry):
            self.geometry_stack.setCurrentWidget(self.ellipse_page)
            self._set_fields(
                self.ellipse_fields,
                (geometry.cx, geometry.cy, geometry.rx, geometry.ry),
            )
            self.geometry_title.hide()
            self.geometry_stack.hide()
        elif isinstance(geometry, SvgLineGeometry):
            self.geometry_stack.setCurrentWidget(self.line_page)
            self._set_common_bounds_visible(False)
            source = self._preview_object or self._object
            transform = source.transform if source is not None else AffineTransform()
            start = transform.map_point(Point(geometry.x1, geometry.y1))
            end = transform.map_point(Point(geometry.x2, geometry.y2))
            self._set_fields(
                self.line_fields,
                (start.x, start.y, end.x, end.y),
            )
        else:
            self.geometry_stack.setCurrentWidget(self.points_page)
            points = geometry.points
            current = max(0, self.vertex_combo.currentIndex())
            self.vertex_combo.clear()
            for index in range(len(points)):
                self.vertex_combo.addItem(f"顶点 {index + 1}", index)
            self.vertex_combo.setCurrentIndex(min(current, len(points) - 1))
            self.vertex_count_label.setText(str(len(points)))
            self._sync_selected_vertex()
        self._fill_color = QColor(*(primitive.style.fill or (255, 255, 255, 255)))
        self._stroke_color = QColor(*(primitive.style.stroke or (0, 0, 0, 255)))
        self._set_combo(
            self.fill_mode,
            "resource" if primitive.fill_resource else "solid" if primitive.style.fill else "none",
        )
        self._set_combo(
            self.stroke_mode,
            "resource"
            if primitive.stroke_resource
            else "solid"
            if primitive.style.stroke
            else "none",
        )
        self._update_color_button(self.fill_button, self._fill_color)
        self._update_color_button(self.stroke_button, self._stroke_color)
        self._set_field(self.stroke_width_spin, primitive.style.stroke_width)
        self._set_combo(self.line_cap_combo, primitive.style.line_cap.value)
        self._set_combo(self.line_join_combo, primitive.style.line_join.value)
        self._set_field(self.miter_limit_spin, primitive.style.miter_limit)
        self._set_combo(self.fill_rule_combo, primitive.style.fill_rule.value)
        self._set_field(self.dash_offset_spin, primitive.style.dash_offset)
        self._sync_dash_list(primitive.style.dash_array)
        resource_primitive = (
            parse_editable_svg(self._resource_source_svg)
            if self._resource_source_svg is not None
            else None
        )
        self.reset_fill_resource_button.setEnabled(
            resource_primitive is not None and resource_primitive.fill_resource is not None
        )
        self.reset_stroke_resource_button.setEnabled(
            resource_primitive is not None and resource_primitive.stroke_resource is not None
        )

    def _candidate(self) -> SvgElementObject | None:
        source = self._preview_object or self._object
        if source is None:
            return None
        primitive = self._primitive
        transform, pivot = self._transform_values(self._object or source)
        svg = source.svg
        bounds = source.intrinsic_bounds
        if primitive is not None:
            try:
                geometry = self._geometry_from_controls(primitive.geometry)
                style = self._style_from_controls(primitive.style)
                svg = update_editable_svg(
                    source.svg,
                    geometry,
                    style,
                    preserve_fill_resource=str(self.fill_mode.currentData()) == "resource",
                    preserve_stroke_resource=str(self.stroke_mode.currentData()) == "resource",
                )
                bounds = primitive_bounds(geometry, style)
            except TypeError, ValueError:
                return None
        try:
            return SvgElementObject(
                id=source.id,
                name=self.name_edit.text(),
                element_kind=source.element_kind,
                svg=svg,
                intrinsic_bounds=bounds,
                transform=transform,
                transform_pivot=pivot,
                fill_override=source.fill_override,
                stroke_override=source.stroke_override,
                stroke_width_override=source.stroke_width_override,
                visible=self.visible_check.isChecked(),
                opacity=self.opacity_spin.value() / 100.0,
                locked=self.locked_check.isChecked(),
            )
        except TypeError, ValueError:
            return None

    def _transform_values(
        self,
        source: SvgElementObject,
    ) -> tuple[AffineTransform, Point]:
        bounds = source.intrinsic_bounds
        old = source.transform
        current_width = max(1e-9, bounds.width * math.hypot(old.a, old.b))
        current_height = max(1e-9, bounds.height * math.hypot(old.c, old.d))
        current_rotation = math.degrees(math.atan2(old.b, old.a))
        top_left = old.map_point(Point(bounds.x, bounds.y))
        pivot = Point(self.pivot_x_spin.value(), self.pivot_y_spin.value())
        delta = (
            AffineTransform.translation(-pivot.x, -pivot.y)
            .then(
                AffineTransform.scale(
                    self.width_spin.value() / current_width,
                    self.height_spin.value() / current_height,
                )
            )
            .then(AffineTransform.rotation(self.rotation_spin.value() - current_rotation))
            .then(AffineTransform.translation(pivot.x, pivot.y))
            .then(
                AffineTransform.translation(
                    self.x_spin.value() - top_left.x,
                    self.y_spin.value() - top_left.y,
                )
            )
        )
        return old.then(delta), pivot

    def _geometry_from_controls(
        self,
        current: SvgPrimitiveGeometry,
    ) -> SvgPrimitiveGeometry:
        if isinstance(current, SvgRectGeometry):
            x, y, width, height, rx, ry = (field.value() for field in self.rect_fields)
            return SvgRectGeometry(x, y, width, height, min(rx, width / 2), min(ry, height / 2))
        if isinstance(current, SvgCircleGeometry):
            return SvgCircleGeometry(*(field.value() for field in self.circle_fields))
        if isinstance(current, SvgEllipseGeometry):
            return SvgEllipseGeometry(*(field.value() for field in self.ellipse_fields))
        if isinstance(current, SvgLineGeometry):
            source = self._preview_object or self._object
            inverse = source.transform.inverse() if source is not None else AffineTransform()
            start = inverse.map_point(
                Point(self.line_fields[0].value(), self.line_fields[1].value())
            )
            end = inverse.map_point(
                Point(self.line_fields[2].value(), self.line_fields[3].value())
            )
            return SvgLineGeometry(start.x, start.y, end.x, end.y)
        points = list(current.points)
        index = self.vertex_combo.currentIndex()
        if 0 <= index < len(points):
            document_point = Point(self.vertex_x_spin.value(), self.vertex_y_spin.value())
            source = self._preview_object or self._object
            local = (
                source.transform.inverse().map_point(document_point) if source else document_point
            )
            points[index] = (local.x, local.y)
        return type(current)(tuple(points))

    def _rect_size_changed(self, _value: float) -> None:
        if self._syncing:
            return
        self._set_rect_corner_ranges(
            self.rect_fields[2].value(),
            self.rect_fields[3].value(),
        )

    def _set_rect_corner_ranges(self, width: float, height: float) -> None:
        was_syncing = self._syncing
        self._syncing = True
        try:
            radius_x = max(0.0, width / 2.0)
            radius_y = max(0.0, height / 2.0)
            self.rect_fields[4].setPropertyRange(
                OBJECT_RADIUS_RANGE.soft_minimum,
                min(OBJECT_RADIUS_RANGE.soft_maximum, radius_x),
                manual_minimum=OBJECT_RADIUS_RANGE.hard_minimum,
                manual_maximum=radius_x,
            )
            self.rect_fields[5].setPropertyRange(
                OBJECT_RADIUS_RANGE.soft_minimum,
                min(OBJECT_RADIUS_RANGE.soft_maximum, radius_y),
                manual_minimum=OBJECT_RADIUS_RANGE.hard_minimum,
                manual_maximum=radius_y,
            )
        finally:
            self._syncing = was_syncing

    def _style_from_controls(self, current: SvgPrimitiveStyle) -> SvgPrimitiveStyle:
        fill_mode = str(self.fill_mode.currentData())
        stroke_mode = str(self.stroke_mode.currentData())
        fill = (
            (
                self._fill_color.red(),
                self._fill_color.green(),
                self._fill_color.blue(),
                self._fill_color.alpha(),
            )
            if fill_mode == "solid"
            else None
        )
        stroke = (
            (
                self._stroke_color.red(),
                self._stroke_color.green(),
                self._stroke_color.blue(),
                self._stroke_color.alpha(),
            )
            if stroke_mode == "solid"
            else None
        )
        dash_array = tuple(
            float(self.dash_list.item(index).data(256)) for index in range(self.dash_list.count())
        )
        return replace(
            current,
            fill=fill,
            stroke=stroke,
            stroke_width=self.stroke_width_spin.value(),
            line_cap=SvgLineCap(str(self.line_cap_combo.currentData())),
            line_join=SvgLineJoin(str(self.line_join_combo.currentData())),
            miter_limit=self.miter_limit_spin.value(),
            dash_array=dash_array,
            dash_offset=self.dash_offset_spin.value(),
            fill_rule=SvgFillRule(str(self.fill_rule_combo.currentData())),
        )

    def _preview_from_controls(self, _value: object = None) -> None:
        if self._syncing:
            return
        if self.sender() is self.dash_value_spin:
            row = self.dash_list.currentRow()
            if 0 <= row < self.dash_list.count():
                value = self.dash_value_spin.value()
                self.dash_list.item(row).setData(256, value)
                self.dash_list.item(row).setText(f"段 {row + 1}: {value:.2f} px")
        candidate = self._candidate()
        if candidate is None:
            return
        self._preview_object = candidate
        self._primitive = parse_editable_svg(candidate.svg)
        self.preview_changed.emit(candidate)

    def _begin_interaction(self, _label: object = None) -> None:
        if not self._syncing:
            self._interaction_active = True

    def _finish_interaction(self) -> None:
        if self._syncing:
            return
        self._interaction_active = False
        self._commit_candidate()

    def _cancel_interaction(self) -> None:
        if self._syncing:
            return
        self._interaction_active = False
        self._preview_object = None
        self.preview_cleared.emit()
        self.set_object(self._object)

    def _commit_candidate(self, _value: object = None) -> None:
        if self._syncing or self._interaction_active or self._object is None:
            return
        candidate = self._candidate()
        if candidate is None:
            return
        changes = {
            field: getattr(candidate, field)
            for field in (
                "name",
                "svg",
                "intrinsic_bounds",
                "transform",
                "transform_pivot",
                "opacity",
                "visible",
                "locked",
            )
            if getattr(candidate, field) != getattr(self._object, field)
        }
        self._preview_object = None
        self.preview_cleared.emit()
        if changes and self.commands.update_vector_object(self._object.id, **changes):
            self.object_changed.emit(self._object)

    def _pick_color(self, field: str) -> None:
        current = self._fill_color if field == "fill" else self._stroke_color
        dialog = ColorPickerDialog(current, "SVG 颜色", self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if field == "fill":
            self._fill_color = dialog.selected_color
            self._set_combo(self.fill_mode, "solid")
            self._update_color_button(self.fill_button, self._fill_color)
        else:
            self._stroke_color = dialog.selected_color
            self._set_combo(self.stroke_mode, "solid")
            self._update_color_button(self.stroke_button, self._stroke_color)
        self._commit_candidate()

    def _reset_resource(self, field: str) -> None:
        source = self._preview_object or self._object
        primitive = self._primitive
        original_svg = self._resource_source_svg
        if source is None or primitive is None or original_svg is None:
            return
        original = parse_editable_svg(original_svg)
        if original is None:
            return
        if field == "fill" and original.fill_resource is None:
            return
        if field == "stroke" and original.stroke_resource is None:
            return
        try:
            restored_style = replace(
                primitive.style,
                fill=None if field == "fill" else primitive.style.fill,
                stroke=None if field == "stroke" else primitive.style.stroke,
            )
            svg = update_editable_svg(
                original_svg,
                primitive.geometry,
                restored_style,
                preserve_fill_resource=(
                    field == "fill" or str(self.fill_mode.currentData()) == "resource"
                ),
                preserve_stroke_resource=(
                    field == "stroke" or str(self.stroke_mode.currentData()) == "resource"
                ),
            )
            candidate = replace(
                source,
                svg=svg,
                intrinsic_bounds=primitive_bounds(primitive.geometry, restored_style),
            )
        except TypeError, ValueError:
            return
        self._preview_object = candidate
        self._primitive = parse_editable_svg(svg)
        was_syncing = self._syncing
        self._syncing = True
        try:
            self._sync_primitive(self._primitive)
        finally:
            self._syncing = was_syncing
        self._commit_candidate()

    def _sync_selected_vertex(self, _index: int = 0) -> None:
        primitive = self._primitive
        if primitive is None or not isinstance(
            primitive.geometry,
            (SvgPolylineGeometry, SvgPolygonGeometry),
        ):
            return
        geometry = primitive.geometry
        index = self.vertex_combo.currentIndex()
        if not 0 <= index < len(geometry.points):
            return
        point = geometry.points[index]
        source = self._preview_object or self._object
        document_point = source.transform.map_point(Point(*point)) if source else Point(*point)
        was_syncing = self._syncing
        self._syncing = True
        try:
            self._set_field(self.vertex_x_spin, document_point.x)
            self._set_field(self.vertex_y_spin, document_point.y)
        finally:
            self._syncing = was_syncing

    def _add_vertex(self) -> None:
        primitive = self._point_primitive()
        if primitive is None:
            return
        geometry = primitive.geometry
        assert isinstance(geometry, (SvgPolylineGeometry, SvgPolygonGeometry))
        points = list(geometry.points)
        index = max(0, self.vertex_combo.currentIndex())
        next_index = (index + 1) % len(points)
        first, second = points[index], points[next_index]
        points.insert(index + 1, ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2))
        self._replace_points(primitive, points, index + 1)

    def _delete_vertex(self) -> None:
        primitive = self._point_primitive()
        if primitive is None:
            return
        geometry = primitive.geometry
        assert isinstance(geometry, (SvgPolylineGeometry, SvgPolygonGeometry))
        minimum = 2 if isinstance(geometry, SvgPolylineGeometry) else 3
        if len(geometry.points) <= minimum:
            return
        index = max(0, self.vertex_combo.currentIndex())
        points = list(geometry.points)
        del points[index]
        self._replace_points(primitive, points, min(index, len(points) - 1))

    def _move_vertex(self, delta: int) -> None:
        primitive = self._point_primitive()
        if primitive is None:
            return
        geometry = primitive.geometry
        assert isinstance(geometry, (SvgPolylineGeometry, SvgPolygonGeometry))
        index = self.vertex_combo.currentIndex()
        target = index + delta
        if index < 0 or not 0 <= target < len(geometry.points):
            return
        points = list(geometry.points)
        points[index], points[target] = points[target], points[index]
        self._replace_points(primitive, points, target)

    def _replace_points(
        self,
        primitive: EditableSvgPrimitive,
        points: list[tuple[float, float]],
        selected: int,
    ) -> None:
        try:
            if isinstance(primitive.geometry, SvgPolylineGeometry):
                geometry: SvgPrimitiveGeometry = SvgPolylineGeometry(tuple(points))
            elif isinstance(primitive.geometry, SvgPolygonGeometry):
                geometry = SvgPolygonGeometry(tuple(points))
            else:
                return
            source = self._preview_object or self._object
            if source is None:
                return
            svg = update_editable_svg(source.svg, geometry, primitive.style)
            candidate = replace(
                source,
                svg=svg,
                intrinsic_bounds=primitive_bounds(geometry, primitive.style),
            )
        except TypeError, ValueError:
            return
        self._preview_object = candidate
        self._primitive = parse_editable_svg(svg)
        was_syncing = self._syncing
        self._syncing = True
        try:
            self._sync_primitive(self._primitive)
            self.vertex_combo.setCurrentIndex(selected)
        finally:
            self._syncing = was_syncing
        self._commit_candidate()

    def _point_primitive(self) -> EditableSvgPrimitive | None:
        primitive = self._primitive
        if primitive is None or not isinstance(
            primitive.geometry,
            (SvgPolylineGeometry, SvgPolygonGeometry),
        ):
            return None
        return primitive

    def _sync_dash_list(self, values: tuple[float, ...]) -> None:
        current = max(0, self.dash_list.currentRow())
        self.dash_list.clear()
        for index, value in enumerate(values):
            self.dash_list.addItem(f"段 {index + 1}: {value:.2f} px")
            self.dash_list.item(index).setData(256, value)
        if values:
            self.dash_list.setCurrentRow(min(current, len(values) - 1))

    def _sync_selected_dash(self, row: int) -> None:
        if row < 0 or row >= self.dash_list.count():
            return
        was_syncing = self._syncing
        self._syncing = True
        try:
            self._set_field(self.dash_value_spin, float(self.dash_list.item(row).data(256)))
        finally:
            self._syncing = was_syncing

    def _add_dash(self) -> None:
        values = self._dash_values()
        values.append(4.0)
        self._sync_dash_list(tuple(values))
        self.dash_list.setCurrentRow(len(values) - 1)
        self._commit_candidate()

    def _delete_dash(self) -> None:
        row = self.dash_list.currentRow()
        if row < 0:
            return
        values = self._dash_values()
        del values[row]
        self._sync_dash_list(tuple(values))
        self._commit_candidate()

    def _move_dash(self, delta: int) -> None:
        row = self.dash_list.currentRow()
        target = row + delta
        values = self._dash_values()
        if row < 0 or not 0 <= target < len(values):
            return
        values[row], values[target] = values[target], values[row]
        self._sync_dash_list(tuple(values))
        self.dash_list.setCurrentRow(target)
        self._commit_candidate()

    def _dash_values(self) -> list[float]:
        row = self.dash_list.currentRow()
        values = [
            float(self.dash_list.item(index).data(256)) for index in range(self.dash_list.count())
        ]
        if (
            0 <= row < len(values)
            and self.dash_value_spin.interaction_state is not NumericEditState.IDLE
        ):
            values[row] = self.dash_value_spin.value()
        return values

    @staticmethod
    def _set_field(field: DragValueDoubleSpinBox, value: float) -> None:
        if field.interaction_state is NumericEditState.IDLE:
            field.setValue(value)

    def _set_fields(
        self,
        fields: tuple[DragValueDoubleSpinBox, ...],
        values: tuple[float, ...],
    ) -> None:
        for field, value in zip(fields, values, strict=True):
            self._set_field(field, value)

    @staticmethod
    def _set_combo(combo: BlenderComboBox, value: str) -> None:
        combo.setCurrentIndex(max(0, combo.findData(value)))

    @staticmethod
    def _update_color_button(button: QPushButton, color: QColor) -> None:
        foreground = "#ffffff" if color.lightness() < 128 else "#000000"
        button.setStyleSheet(
            "QPushButton {"
            f"background-color: {color.name(QColor.NameFormat.HexArgb)};"
            f"color: {foreground};"
            "}"
        )


__all__ = ["SvgPropertiesPanel"]
