"""One transform inspector for raster, text and SVG objects and batch gestures."""

from __future__ import annotations

import copy
import math
from collections.abc import Callable
from dataclasses import replace
from typing import cast
from uuid import UUID

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QLabel, QPushButton, QWidget

from easypicture.commands import DocumentCommandService
from easypicture.domain import AffineTransform, Collection, Document, Point
from easypicture.domain.object_operations import (
    TransformableObject,
    edited_object_transform,
    object_basis,
    object_center_offset,
    object_local_bounds,
    object_pivot,
    object_world_transform,
    transformed_object_matrices,
)
from easypicture.ui.drag_value_spinbox import DragValueDoubleSpinBox
from easypicture.ui.numeric_ranges import (
    OBJECT_DIMENSION_RANGE,
    OBJECT_SCALE_RANGE,
    ROTATION_DEGREES_RANGE,
    SIGNED_COORDINATE_RANGE,
    sync_spatial_range,
)
from easypicture.ui.property_binding import EditTarget, PropertyEditSession
from easypicture.ui.selection_state import SelectionCoordinator, SelectionState


class ObjectTransformBlock(QWidget):
    preview_changed = Signal(object)
    center_preview_changed = Signal(object)
    preview_cleared = Signal()
    pivot_changed = Signal(object)

    def __init__(
        self,
        commands: DocumentCommandService,
        document: Callable[[], Document | None],
        selection: SelectionCoordinator,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands, self._document, self.selection = commands, document, selection
        self._ids: tuple[UUID, ...] = ()
        self._syncing = False
        self._pivot = Point(0, 0)
        self._custom_pivot = False
        self._initial_pivot = self._pivot
        self._initial_custom_pivot = False
        self._initial: TransformableObject | None = None
        self._values: dict[str, float] = {}
        self._field = ""
        self.message = QLabel(self)
        self.fields: dict[str, DragValueDoubleSpinBox] = {}
        self.form = QFormLayout(self)
        for name, label, limits in (
            ("x", "位置 X", SIGNED_COORDINATE_RANGE),
            ("y", "位置 Y", SIGNED_COORDINATE_RANGE),
            ("width", "宽度", OBJECT_DIMENSION_RANGE),
            ("height", "高度", OBJECT_DIMENSION_RANGE),
            ("rotation", "旋转 °", ROTATION_DEGREES_RANGE),
            ("scale_x", "缩放 X", OBJECT_SCALE_RANGE),
            ("scale_y", "缩放 Y", OBJECT_SCALE_RANGE),
            ("pivot_x", "中心偏移 X（局部）", SIGNED_COORDINATE_RANGE),
            ("pivot_y", "中心偏移 Y（局部）", SIGNED_COORDINATE_RANGE),
        ):
            field = DragValueDoubleSpinBox(
                limits.soft_minimum,
                limits.soft_maximum,
                limits.default_value,
                manual_minimum=limits.hard_minimum,
                manual_maximum=limits.hard_maximum,
                parent=self,
            )
            self.fields[name] = field
            self.form.addRow(label, field)
            field.interactionStarted.connect(lambda name=name: self._begin(name))
            field.valueChanged.connect(lambda value, name=name: self._preview(name, value))
            field.editingFinished.connect(self._finish)
            field.interactionCanceled.connect(self._cancel)
        self.center_button = QPushButton("重置中心", self)
        self.center_button.clicked.connect(self._reset_center)
        self.form.addRow(self.center_button)
        self.form.addRow(self.message)
        self._session = PropertyEditSession[tuple[AffineTransform, Point | None]](
            self._target,
            self._publish,
            self._commit,
            self.preview_cleared.emit,
            self._valid,
        )
        selection.changed.connect(self._selection_changed)
        self._selection_changed(selection.state)

    def _single(self) -> TransformableObject | None:
        document = self._document()
        if document is None or len(self._ids) != 1:
            return None
        node = document.node_by_id(self._ids[0])
        return None if isinstance(node, Collection) else node

    def _target(self) -> EditTarget | None:
        document = self._document()
        if document is None or not self._ids or self.selection.state.selected_ids != self._ids:
            return None
        return EditTarget(document.id, self._ids[0], document.revision)

    def _selection_changed(self, state: SelectionState) -> None:
        if self._ids != state.selected_ids:
            for field in self.fields.values():
                field.cancelInteraction()
            self._session.cancel()
            self._ids = state.selected_ids
            self._custom_pivot = False
        self.refresh()

    def refresh(self) -> None:
        if self._ids != self.selection.state.selected_ids:
            self._selection_changed(self.selection.state)
            return
        if self._session.active:
            return
        document = self._document()
        if self.selection.document is not document:
            return
        self.setEnabled(bool(document and self._ids))
        if document is None or not self._ids:
            self.pivot_changed.emit(None)
            return
        node = self._single()
        self._syncing = True
        try:
            if node is not None:
                bounds = object_local_bounds(node)
                self._pivot = object_pivot(node)
                values = self._object_values(node)
                for axis, extent, local_size in (
                    ("x", document.width, bounds.width),
                    ("y", document.height, bounds.height),
                ):
                    self.fields[f"scale_{axis}"].syncPropertyRange(
                        0.01,
                        min(100, max(2, extent / local_size)),
                        manual_minimum=-100,
                        manual_maximum=100,
                    )
            else:
                for axis in ("x", "y"):
                    self.fields[f"scale_{axis}"].syncPropertyRange(
                        0.1, 4, manual_minimum=-100, manual_maximum=100
                    )
                if not self._custom_pivot:
                    from easypicture.domain import walk_nodes

                    points: list[Point] = []
                    for key in self._ids:
                        for item in walk_nodes((document.node_by_id(key),)):
                            if isinstance(item, Collection):
                                continue
                            rect, matrix = object_local_bounds(item), object_world_transform(item)
                            points.extend(
                                matrix.map_point(Point(x, y))
                                for x, y in (
                                    (rect.x, rect.y),
                                    (rect.right, rect.y),
                                    (rect.right, rect.bottom),
                                    (rect.x, rect.bottom),
                                )
                            )
                    if points:
                        self._pivot = Point(
                            (min(p.x for p in points) + max(p.x for p in points)) / 2,
                            (min(p.y for p in points) + max(p.y for p in points)) / 2,
                        )
                values = dict(
                    x=0,
                    y=0,
                    rotation=0,
                    scale_x=1,
                    scale_y=1,
                    pivot_x=self._pivot.x,
                    pivot_y=self._pivot.y,
                )
            self._values = values
            for name, field in self.fields.items():
                self.form.setRowVisible(field, node is not None or name not in {"width", "height"})
                if name in values:
                    field.syncValue(values[name])
            for axis, extent in (("x", document.size.width), ("y", document.size.height)):
                sync_spatial_range(self.fields[axis], extent)
                sync_spatial_range(self.fields[f"pivot_{axis}"], extent)
            sync_spatial_range(self.fields["width"], document.size.width, dimension=True)
            sync_spatial_range(self.fields["height"], document.size.height, dimension=True)
            for name, label in {
                "x": "位置 X" if node else "平移 X",
                "y": "位置 Y" if node else "平移 Y",
                "pivot_x": "中心偏移 X（局部）" if node else "共同中心 X（全局）",
                "pivot_y": "中心偏移 Y（局部）" if node else "共同中心 Y（全局）",
            }.items():
                cast(QLabel, self.form.labelForField(self.fields[name])).setText(label)
            self.pivot_changed.emit(self._pivot)
        finally:
            self._syncing = False

    @staticmethod
    def _object_values(node: TransformableObject) -> dict[str, float]:
        bounds, matrix = object_local_bounds(node), object_world_transform(node)
        pivot, offset = object_pivot(node), object_center_offset(node)
        sx, sy = math.hypot(matrix.a, matrix.b), math.hypot(matrix.c, matrix.d)
        return dict(
            x=pivot.x,
            y=pivot.y,
            width=bounds.width * sx,
            height=bounds.height * sy,
            rotation=math.degrees(math.atan2(matrix.b, matrix.a)),
            scale_x=sx,
            scale_y=sy,
            pivot_x=offset.x,
            pivot_y=offset.y,
        )

    def transform_pivot(self) -> Point:
        self.refresh()
        return self._pivot

    def _reset_center(self) -> None:
        node = self._single()
        if node is not None:
            self.commands.objects.update((node.id,), transform_pivot=None)
        else:
            self._custom_pivot = False
        self.refresh()

    def _begin(self, name: str) -> None:
        self.refresh()
        self._field = name
        self._initial_pivot, self._initial_custom_pivot = self._pivot, self._custom_pivot
        self._initial = copy.copy(self._single())
        initial = (
            (self._initial.transform, self._initial.transform_pivot)
            if self._initial
            else (AffineTransform(), None)
        )
        self._session.begin(initial)

    def _preview(self, name: str, value: float) -> None:
        if self._syncing:
            return
        if not self._session.active:
            self._begin(name)
        node = self._initial
        if node is not None:
            values = self._values | {name: value}
            if name.startswith("scale"):
                # Negative manual scale flips about the same object center.
                ratio = value / self._values[name]
                pivot = object_pivot(node)
                basis = object_basis(node)
                delta = AffineTransform.scale(
                    ratio if name == "scale_x" else 1, ratio if name == "scale_y" else 1
                )
                delta = basis.inverse().then(delta).then(basis)
                delta = (
                    AffineTransform.translation(-pivot.x, -pivot.y)
                    .then(delta)
                    .then(AffineTransform.translation(pivot.x, pivot.y))
                )
                document = self._document()
                if document is None:
                    return
                try:
                    candidate = (
                        transformed_object_matrices(document, self._ids, delta)[node.id],
                        node.transform_pivot,
                    )
                except (ValueError, PermissionError) as error:
                    self._session.reject()
                    self.message.setText(str(error))
                    return
            else:
                try:
                    candidate = edited_object_transform(
                        node,
                        Point(values["x"], values["y"]),
                        values["width"],
                        values["height"],
                        values["rotation"],
                        Point(values["pivot_x"], values["pivot_y"]),
                    )
                except ValueError as error:
                    self._session.reject()
                    self.message.setText(str(error))
                    return
            self._session.preview(candidate)
        elif name.startswith("pivot"):
            self._pivot = Point(
                value if name == "pivot_x" else self._pivot.x,
                value if name == "pivot_y" else self._pivot.y,
            )
            self._custom_pivot = True
            self.pivot_changed.emit(self._pivot)
        else:
            if name in {"x", "y"}:
                delta = AffineTransform.translation(
                    value if name == "x" else 0, value if name == "y" else 0
                )
            else:
                delta = (
                    AffineTransform.rotation(value)
                    if name == "rotation"
                    else AffineTransform.scale(
                        value if name == "scale_x" else 1, value if name == "scale_y" else 1
                    )
                )
                delta = (
                    AffineTransform.translation(-self._pivot.x, -self._pivot.y)
                    .then(delta)
                    .then(AffineTransform.translation(self._pivot.x, self._pivot.y))
                )
            self._session.preview((delta, None))

    def _valid(self, candidate: tuple[AffineTransform, Point | None]) -> bool:
        document = self._document()
        if document is None:
            return False
        try:
            if self._initial is not None:
                if document.is_locked(self._initial.id):
                    raise PermissionError("对象或父集合已锁定")
                from easypicture.domain.object_operations import node_with_properties
                from easypicture.domain.spatial import object_bounds

                object_bounds(
                    node_with_properties(
                        self._initial, dict(transform=candidate[0], transform_pivot=candidate[1])
                    )
                )
            else:
                transformed_object_matrices(document, self._ids, candidate[0])
            self.message.clear()
            return True
        except (ValueError, PermissionError) as error:
            self.message.setText(str(error))
            return False

    def _publish(self, candidate: tuple[AffineTransform, Point | None]) -> None:
        document = self._document()
        if document is None:
            return
        if self._initial is not None:
            self.preview_changed.emit({self._initial.id: candidate[0]})
            self.center_preview_changed.emit({self._initial.id: candidate[1]})
            preview = replace(self._initial, transform=candidate[0], transform_pivot=candidate[1])
            self._syncing = True
            try:
                for name, value in self._object_values(preview).items():
                    if name != self._field:
                        self.fields[name].syncValue(value)
            finally:
                self._syncing = False
        else:
            self.preview_changed.emit(
                transformed_object_matrices(document, self._ids, candidate[0])
            )

    def _commit(self, candidate: tuple[AffineTransform, Point | None]) -> bool:
        if self._initial is not None:
            return self.commands.objects.update(
                self._ids, transform=candidate[0], transform_pivot=candidate[1]
            )
        return self.commands.objects.transform(self._ids, candidate[0])

    def _finish(self) -> None:
        self._session.finish()
        self.refresh()

    def _cancel(self) -> None:
        if self._session.active and self._initial is None:
            self._pivot, self._custom_pivot = self._initial_pivot, self._initial_custom_pivot
        self._session.cancel()
        self.refresh()
