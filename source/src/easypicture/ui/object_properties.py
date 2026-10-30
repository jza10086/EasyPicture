"""Reusable batch transform block: private preview, one command per field gesture."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QLabel, QPushButton, QWidget

from easypicture.commands import DocumentCommandService
from easypicture.domain import AffineTransform, Document, Point
from easypicture.domain.object_operations import transformed_object_matrices
from easypicture.ui.drag_value_spinbox import DragValueDoubleSpinBox
from easypicture.ui.numeric_ranges import (
    OBJECT_SCALE_RANGE,
    ROTATION_DEGREES_RANGE,
    SIGNED_COORDINATE_RANGE,
)
from easypicture.ui.property_binding import EditTarget, PropertyEditSession
from easypicture.ui.selection_state import SelectionCoordinator, SelectionState


class ObjectTransformBlock(QWidget):
    preview_changed = Signal(object)
    preview_cleared = Signal()

    def __init__(
        self,
        commands: DocumentCommandService,
        document: Callable[[], Document | None],
        selection: SelectionCoordinator,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.commands = commands
        self._document = document
        self.selection = selection
        self._ids: tuple[UUID, ...] = ()
        self._field = ""
        self._syncing = False
        self._pivot = Point(0, 0)
        self._custom_pivot = False
        self.message = QLabel(self)
        self.fields: dict[str, DragValueDoubleSpinBox] = {}
        form = QFormLayout(self)
        for name, label, limits in (
            ("x", "平移 X", SIGNED_COORDINATE_RANGE),
            ("y", "平移 Y", SIGNED_COORDINATE_RANGE),
            ("rotation", "旋转 °", ROTATION_DEGREES_RANGE),
            ("scale_x", "缩放 X", OBJECT_SCALE_RANGE),
            ("scale_y", "缩放 Y", OBJECT_SCALE_RANGE),
            ("pivot_x", "共同中心 X", SIGNED_COORDINATE_RANGE),
            ("pivot_y", "共同中心 Y", SIGNED_COORDINATE_RANGE),
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
            form.addRow(label, field)
            if name.startswith("pivot"):
                field.editingFinished.connect(self._set_pivot)
            else:
                field.interactionStarted.connect(lambda name=name: self._begin(name))
                field.valueChanged.connect(lambda value, name=name: self._preview(name, value))
                field.editingFinished.connect(self._finish)
                field.interactionCanceled.connect(self._cancel)
        self.center_button = QPushButton("重置为共同边界中心", self)
        self.center_button.clicked.connect(self._center)
        form.addRow(self.center_button)
        form.addRow(self.message)
        self._session = PropertyEditSession[AffineTransform](
            self._target,
            self._publish,
            self._commit,
            self.preview_cleared.emit,
            self._valid,
        )
        selection.changed.connect(self._selection_changed)
        self._selection_changed(selection.state)

    def _target(self) -> EditTarget | None:
        document = self._document()
        if document is None or not self._ids or self.selection.state.selected_ids != self._ids:
            return None
        return EditTarget(document.id, self._ids[0], document.revision)

    def _selection_changed(self, state: SelectionState) -> None:
        if self._ids == state.selected_ids:
            return
        for field in self.fields.values():
            field.cancelInteraction()
        self._cancel()
        self._ids = state.selected_ids
        self.setEnabled(bool(self._ids))
        self._center()

    def _center(self) -> None:
        self._custom_pivot = False
        document = self._document()
        if document is None:
            return
        try:
            ids = transformed_object_matrices(document, self._ids, AffineTransform())
            bounds = [document.spatial_index.bounds(key) for key in ids]
            valid = [rect for rect in bounds if rect is not None]
            if valid:
                self._pivot = Point(
                    (min(r.x for r in valid) + max(r.right for r in valid)) / 2,
                    (min(r.y for r in valid) + max(r.bottom for r in valid)) / 2,
                )
                self.fields["pivot_x"].syncValue(self._pivot.x)
                self.fields["pivot_y"].syncValue(self._pivot.y)
            self.message.clear()
        except (ValueError, PermissionError) as error:
            self.message.setText(str(error))

    def _set_pivot(self) -> None:
        self._custom_pivot = True
        self._pivot = Point(self.fields["pivot_x"].value(), self.fields["pivot_y"].value())

    def transform_pivot(self) -> Point:
        if not self._custom_pivot:
            self._center()
        return self._pivot

    def _begin(self, name: str) -> None:
        self._field = name
        self._session.begin(AffineTransform())

    def _preview(self, name: str, value: float) -> None:
        if self._syncing:
            return
        if not self._session.active:
            self._begin(name)
        if name in ("x", "y"):
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
        self._session.preview(delta)

    def _valid(self, delta: AffineTransform) -> bool:
        document = self._document()
        if document is None:
            return False
        try:
            transformed_object_matrices(document, self._ids, delta)
            self.message.clear()
            return True
        except (ValueError, PermissionError) as error:
            self.message.setText(str(error))
            return False

    def _publish(self, delta: AffineTransform) -> None:
        document = self._document()
        if document is not None:
            self.preview_changed.emit(transformed_object_matrices(document, self._ids, delta))

    def _commit(self, delta: AffineTransform) -> bool:
        return self.commands.objects.transform(self._ids, delta)

    def _finish(self) -> None:
        self._session.finish()
        self._reset_fields()

    def _cancel(self) -> None:
        self._session.cancel()
        self._reset_fields()

    def _reset_fields(self) -> None:
        self._syncing = True
        try:
            for name, field in self.fields.items():
                if not name.startswith("pivot"):
                    field.syncValue(1 if name.startswith("scale") else 0)
        finally:
            self._syncing = False
