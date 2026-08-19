"""Grouped sprite-atlas export options and authoritative preview."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from easypicture.domain import SpriteFrameContentMode
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot, render_snapshot_region
from easypicture.pixels import Rect
from easypicture.ui.drag_value_spinbox import DragValueSpinBox


@dataclass(frozen=True, slots=True)
class AtlasExportSpec:
    path: Path
    included_group_ids: tuple[UUID, ...]
    include_ungrouped: bool
    horizontal_spacing: int
    vertical_spacing: int
    margin: int


class AtlasExportDialog(QDialog):
    def __init__(
        self,
        snapshot: DocumentSnapshot,
        suggested_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        workspace = snapshot.sprite_workspace
        if workspace is None:
            raise ValueError("atlas dialog requires a project snapshot")
        self._snapshot = snapshot
        self._workspace = workspace
        self.setWindowTitle("导出完整精灵图")
        self.resize(620, 560)

        self.path_edit = QLineEdit(str(suggested_path.with_suffix(".png")), self)
        browse = QPushButton("浏览…", self)
        browse.clicked.connect(self._browse)
        path_row = QWidget(self)
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(browse)

        self.group_checks: dict[UUID, QCheckBox] = {}
        group_box = QWidget(self)
        group_layout = QVBoxLayout(group_box)
        group_layout.setContentsMargins(0, 0, 0, 0)
        for group in workspace.groups:
            checkbox = QCheckBox(group.name, group_box)
            checkbox.setChecked(bool(group.frame_ids))
            checkbox.toggled.connect(self._refresh_preview)
            self.group_checks[group.id] = checkbox
            group_layout.addWidget(checkbox)
        self.ungrouped_check = QCheckBox("未分组", group_box)
        self.ungrouped_check.setChecked(bool(workspace.ungrouped_frame_ids))
        self.ungrouped_check.toggled.connect(self._refresh_preview)
        group_layout.addWidget(self.ungrouped_check)

        self.horizontal_spacing = self._number(0, 1024)
        self.vertical_spacing = self._number(0, 1024)
        self.margin = self._number(0, 4096)
        for control in (self.horizontal_spacing, self.vertical_spacing, self.margin):
            control.valueChanged.connect(self._refresh_preview)
        form = QFormLayout()
        form.addRow("输出路径", path_row)
        form.addRow("包含分组", group_box)
        form.addRow("水平间距", self.horizontal_spacing)
        form.addRow("垂直间距", self.vertical_spacing)
        form.addRow("外边距", self.margin)

        self.preview = QLabel(self)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(320, 220)
        preview_scroll = QScrollArea(self)
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setWidget(self.preview)
        self.size_label = QLabel(self)
        self.size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(preview_scroll, 1)
        layout.addWidget(self.size_label)
        layout.addWidget(buttons)
        self._refresh_preview()

    @staticmethod
    def _number(minimum: int, maximum: int) -> DragValueSpinBox:
        return DragValueSpinBox(minimum, maximum, minimum)

    @property
    def spec(self) -> AtlasExportSpec:
        return AtlasExportSpec(
            path=Path(self.path_edit.text()).with_suffix(".png"),
            included_group_ids=tuple(
                group_id
                for group_id, checkbox in self.group_checks.items()
                if checkbox.isChecked()
            ),
            include_ungrouped=self.ungrouped_check.isChecked(),
            horizontal_spacing=self.horizontal_spacing.value(),
            vertical_spacing=self.vertical_spacing.value(),
            margin=self.margin.value(),
        )

    def _browse(self) -> None:
        filename, _filter = QFileDialog.getSaveFileName(
            self,
            "导出完整精灵图",
            self.path_edit.text(),
            "PNG 图片 (*.png)",
        )
        if filename:
            self.path_edit.setText(str(Path(filename).with_suffix(".png")))

    def _selected_rows(self) -> list[tuple[UUID | None, tuple[UUID, ...]]]:
        rows: list[tuple[UUID | None, tuple[UUID, ...]]] = [
            (group.id, group.frame_ids)
            for group in self._workspace.groups
            if self.group_checks[group.id].isChecked() and group.frame_ids
        ]
        if self.ungrouped_check.isChecked() and self._workspace.ungrouped_frame_ids:
            rows.append((None, self._workspace.ungrouped_frame_ids))
        return rows

    def _refresh_preview(self, *_args: object) -> None:
        rows = self._selected_rows()
        if not rows:
            self.preview.clear()
            self.preview.setText("请选择至少一个非空分组")
            self.size_label.setText("0 × 0 px")
            return
        cell = self._workspace.cell_size
        columns = max(len(frame_ids) for _, frame_ids in rows)
        width = self.margin.value() * 2 + columns * cell.width
        width += max(columns - 1, 0) * self.horizontal_spacing.value()
        height = self.margin.value() * 2 + len(rows) * cell.height
        height += max(len(rows) - 1, 0) * self.vertical_spacing.value()
        pixels = np.zeros((height, width, 4), dtype=np.uint8)
        by_id = {frame.id: frame for frame in self._workspace.frames}
        embedded = {
            item.document_id: item for item in self._snapshot.embedded_documents
        }
        for row, (_group_id, frame_ids) in enumerate(rows):
            y = self.margin.value() + row * (cell.height + self.vertical_spacing.value())
            for column, frame_id in enumerate(frame_ids):
                frame = by_id[frame_id]
                x = self.margin.value() + column * (
                    cell.width + self.horizontal_spacing.value()
                )
                if frame.content_mode is SpriteFrameContentMode.LINKED:
                    snapshot = self._snapshot
                    region = frame.region
                else:
                    if frame.embedded_document_id not in embedded:
                        continue
                    snapshot = embedded[frame.embedded_document_id]
                    region = Rect(0, 0, cell.width, cell.height)
                pixels[y : y + cell.height, x : x + cell.width] = render_snapshot_region(
                    snapshot,
                    region,
                    CancellationToken(),
                    lambda _done, _total, _label: None,
                )
        image = QImage(
            pixels.data,
            width,
            height,
            pixels.strides[0],
            QImage.Format.Format_RGBA8888,
        ).copy()
        pixmap = QPixmap.fromImage(image)
        self.preview.setPixmap(
            pixmap.scaled(
                520,
                320,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        )
        self.size_label.setText(f"{width} × {height} px · {sum(len(ids) for _, ids in rows)} 帧")

    def _accept_if_valid(self) -> None:
        if self._selected_rows() and self.path_edit.text().strip():
            self.accept()


__all__ = ["AtlasExportDialog", "AtlasExportSpec"]
