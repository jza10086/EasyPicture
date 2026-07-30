"""Unified Blender-style still-image export dialog."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from easypicture.persistence import (
    BmpExportOptions,
    ExportCapability,
    ExportFormat,
    GifExportOptions,
    ImageExportOptions,
    JpegExportOptions,
    PngExportOptions,
    TgaExportOptions,
    TiffCompression,
    TiffExportOptions,
    WebpExportOptions,
    canonical_extension,
    default_export_options,
    normalize_export_path,
)
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.settings import AppSettings


@dataclass(frozen=True, slots=True)
class ImageExportSpec:
    path: Path
    options: ImageExportOptions


_FORMAT_LABELS = {
    ExportFormat.PNG: "PNG",
    ExportFormat.JPEG: "JPEG",
    ExportFormat.WEBP: "WebP",
    ExportFormat.BMP: "BMP",
    ExportFormat.TIFF: "TIFF",
    ExportFormat.TGA: "TGA",
    ExportFormat.GIF: "GIF（静态）",
}


class ImageExportDialog(QDialog):
    """Collect a destination and format-specific options in one dialog."""

    def __init__(
        self,
        settings: AppSettings,
        suggested_name: str,
        initial_directory: Path,
        capabilities: tuple[ExportCapability, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ImageExportDialog")
        self.setWindowTitle("导出图片")
        self.setModal(True)
        self.resize(540, 380)
        self._settings = settings
        self._capabilities = {item.format: item for item in capabilities}
        self._pages: dict[ExportFormat, QWidget] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        self.path_edit = QLineEdit(self)
        self.path_edit.setObjectName("ExportPathEdit")
        self.browse_button = QPushButton("浏览…", self)
        path_row.addWidget(QLabel("文件", self))
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_button)
        root.addLayout(path_row)

        format_row = QHBoxLayout()
        format_row.setSpacing(6)
        self.format_combo = BlenderComboBox(self)
        self.format_combo.setObjectName("ExportFormatCombo")
        for export_format in ExportFormat:
            capability = self._capabilities.get(export_format)
            self.format_combo.addItem(
                _FORMAT_LABELS[export_format],
                export_format.value,
            )
            index = self.format_combo.count() - 1
            if capability is not None and not capability.available:
                model = cast(QStandardItemModel, self.format_combo.model())
                item = model.item(index)
                item.setEnabled(False)
                item.setToolTip(capability.reason or "编码器不可用")
        format_row.addWidget(QLabel("格式", self))
        format_row.addWidget(self.format_combo, 1)
        root.addLayout(format_row)

        self.option_stack = QStackedWidget(self)
        self.option_stack.setObjectName("ExportOptionStack")
        self._create_pages()
        root.addWidget(self.option_stack, 1)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.export_button = self.button_box.button(
            QDialogButtonBox.StandardButton.Save
        )
        self.export_button.setText("导出")
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "取消"
        )
        root.addWidget(self.button_box)

        initial_format = settings.last_export_format()
        if not self._format_available(initial_format):
            initial_format = next(
                export_format
                for export_format in ExportFormat
                if self._format_available(export_format)
            )
        self.format_combo.setCurrentIndex(
            max(0, self.format_combo.findData(initial_format.value))
        )
        base_name = Path(suggested_name).stem or "Untitled"
        self.path_edit.setText(
            str(initial_directory / f"{base_name}{canonical_extension(initial_format)}")
        )
        self._show_format(initial_format, update_path=False)

        self.format_combo.currentIndexChanged.connect(self._format_changed)
        self.browse_button.clicked.connect(self._browse)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

    @property
    def export_format(self) -> ExportFormat:
        return ExportFormat(str(self.format_combo.currentData()))

    def options(self) -> ImageExportOptions:
        export_format = self.export_format
        if export_format is ExportFormat.PNG:
            return PngExportOptions(self.png_compression.value())
        if export_format is ExportFormat.JPEG:
            return JpegExportOptions(self.jpeg_quality.value())
        if export_format is ExportFormat.WEBP:
            return WebpExportOptions(
                self.webp_lossless.isChecked(),
                self.webp_quality.value(),
            )
        if export_format is ExportFormat.BMP:
            return BmpExportOptions()
        if export_format is ExportFormat.TIFF:
            return TiffExportOptions(
                TiffCompression(str(self.tiff_compression.currentData()))
            )
        if export_format is ExportFormat.TGA:
            return TgaExportOptions(self.tga_rle.isChecked())
        if export_format is ExportFormat.GIF:
            return GifExportOptions(
                self.gif_colors.value(),
                self.gif_alpha_threshold.value(),
                self.gif_dither.isChecked(),
            )
        raise ValueError(f"unsupported export format: {export_format!r}")

    def specification(self) -> ImageExportSpec:
        options = self.options()
        path = normalize_export_path(Path(self.path_edit.text()), self.export_format)
        self._settings.set_export_state(
            self.export_format,
            path.parent,
            _serialize_options(options),
        )
        return ImageExportSpec(path, options)

    def accept(self) -> None:
        raw_path = self.path_edit.text().strip()
        if not raw_path:
            QMessageBox.warning(self, "无法导出", "请选择导出文件。")
            return
        path = normalize_export_path(Path(raw_path), self.export_format)
        if path.exists():
            result = QMessageBox.question(
                self,
                "覆盖现有文件？",
                f"“{path.name}”已经存在。是否覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if result != QMessageBox.StandardButton.Yes:
                return
        self.path_edit.setText(str(path))
        super().accept()

    def _create_pages(self) -> None:
        self.png_compression = DragValueSpinBox(0, 9, 6, parent=self)
        self.jpeg_quality = DragValueSpinBox(1, 95, 90, suffix="%", parent=self)
        self.webp_lossless = QCheckBox("无损压缩", self)
        self.webp_quality = DragValueSpinBox(0, 100, 90, suffix="%", parent=self)
        self.tiff_compression = BlenderComboBox(self)
        self.tiff_compression.addItem("无压缩", TiffCompression.NONE.value)
        self.tiff_compression.addItem("LZW", TiffCompression.LZW.value)
        self.tiff_compression.addItem("Deflate", TiffCompression.DEFLATE.value)
        self.tga_rle = QCheckBox("RLE 压缩", self)
        self.gif_colors = DragValueSpinBox(2, 256, 256, parent=self)
        self.gif_alpha_threshold = DragValueSpinBox(0, 255, 128, parent=self)
        self.gif_dither = QCheckBox("Floyd–Steinberg 抖动", self)

        self._add_page(
            ExportFormat.PNG,
            [
                ("压缩级别", self.png_compression),
                ("", QLabel("保留完整 Alpha，写入 sRGB ICC。", self)),
            ],
        )
        self._add_page(
            ExportFormat.JPEG,
            [
                ("质量", self.jpeg_quality),
                ("", QLabel("不支持透明度；透明区域将使用白色填充。", self)),
            ],
        )
        self._add_page(
            ExportFormat.WEBP,
            [
                ("", self.webp_lossless),
                ("质量/强度", self.webp_quality),
                ("", QLabel("保留 Alpha；无损模式适合继续编辑。", self)),
            ],
        )
        self._add_page(
            ExportFormat.BMP,
            [
                ("", QLabel("输出 24 位 RGB；透明区域将使用白色填充。", self)),
            ],
        )
        self._add_page(
            ExportFormat.TIFF,
            [
                ("压缩", self.tiff_compression),
                ("", QLabel("保留完整 Alpha，写入 sRGB ICC。", self)),
            ],
        )
        self._add_page(
            ExportFormat.TGA,
            [
                ("", self.tga_rle),
                ("", QLabel("保留完整 Alpha，固定为左上角方向。", self)),
            ],
        )
        self._add_page(
            ExportFormat.GIF,
            [
                ("颜色数", self.gif_colors),
                ("Alpha 阈值", self.gif_alpha_threshold),
                ("", self.gif_dither),
                (
                    "",
                    QLabel(
                        "静态单帧；GIF 只有一位透明度，不保留半透明和 ICC。",
                        self,
                    ),
                ),
            ],
        )
        for export_format in ExportFormat:
            self._restore_options(export_format)

    def _add_page(
        self,
        export_format: ExportFormat,
        rows: list[tuple[str, QWidget]],
    ) -> None:
        page = QWidget(self.option_stack)
        form = QFormLayout(page)
        form.setContentsMargins(8, 10, 8, 8)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        for label, widget in rows:
            if isinstance(widget, QLabel):
                widget.setWordWrap(True)
                widget.setProperty("secondary", True)
            form.addRow(label, widget)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._pages[export_format] = page
        self.option_stack.addWidget(page)

    def _format_changed(self, _index: int) -> None:
        self._show_format(self.export_format, update_path=True)

    def _show_format(
        self,
        export_format: ExportFormat,
        *,
        update_path: bool,
    ) -> None:
        self.option_stack.setCurrentWidget(self._pages[export_format])
        if update_path and self.path_edit.text().strip():
            path = normalize_export_path(
                Path(self.path_edit.text().strip()),
                export_format,
            )
            self.path_edit.setText(str(path))

    def _browse(self) -> None:
        export_format = self.export_format
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "选择导出文件",
            self.path_edit.text(),
            f"{_FORMAT_LABELS[export_format]} (*{canonical_extension(export_format)})",
        )
        if selected:
            self.path_edit.setText(
                str(normalize_export_path(Path(selected), export_format))
            )

    def _format_available(self, export_format: ExportFormat) -> bool:
        capability = self._capabilities.get(export_format)
        return capability is None or capability.available

    def _restore_options(self, export_format: ExportFormat) -> None:
        options = _deserialize_options(
            export_format,
            self._settings.export_options_state(export_format),
        )
        if isinstance(options, PngExportOptions):
            self.png_compression.setValue(options.compression)
        elif isinstance(options, JpegExportOptions):
            self.jpeg_quality.setValue(options.quality)
        elif isinstance(options, WebpExportOptions):
            self.webp_lossless.setChecked(options.lossless)
            self.webp_quality.setValue(options.quality)
        elif isinstance(options, TiffExportOptions):
            self.tiff_compression.setCurrentIndex(
                max(
                    0,
                    self.tiff_compression.findData(options.compression.value),
                )
            )
        elif isinstance(options, TgaExportOptions):
            self.tga_rle.setChecked(options.rle)
        elif isinstance(options, GifExportOptions):
            self.gif_colors.setValue(options.colors)
            self.gif_alpha_threshold.setValue(options.alpha_threshold)
            self.gif_dither.setChecked(options.dither)


def _serialize_options(options: ImageExportOptions) -> str:
    data: dict[str, object]
    if isinstance(options, PngExportOptions):
        data = {"compression": options.compression}
    elif isinstance(options, JpegExportOptions):
        data = {"quality": options.quality}
    elif isinstance(options, WebpExportOptions):
        data = {"lossless": options.lossless, "quality": options.quality}
    elif isinstance(options, BmpExportOptions):
        data = {}
    elif isinstance(options, TiffExportOptions):
        data = {"compression": options.compression.value}
    elif isinstance(options, TgaExportOptions):
        data = {"rle": options.rle}
    elif isinstance(options, GifExportOptions):
        data = {
            "colors": options.colors,
            "alpha_threshold": options.alpha_threshold,
            "dither": options.dither,
        }
    else:
        raise TypeError(f"unsupported export options: {type(options)!r}")
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _deserialize_options(
    export_format: ExportFormat,
    state: str | None,
) -> ImageExportOptions:
    if state is None:
        return default_export_options(export_format)
    try:
        raw = json.loads(state)
        if not isinstance(raw, dict):
            raise ValueError
        if export_format is ExportFormat.PNG:
            return PngExportOptions(int(raw["compression"]))
        if export_format is ExportFormat.JPEG:
            return JpegExportOptions(int(raw["quality"]))
        if export_format is ExportFormat.WEBP:
            return WebpExportOptions(
                bool(raw["lossless"]),
                int(raw["quality"]),
            )
        if export_format is ExportFormat.BMP:
            return BmpExportOptions()
        if export_format is ExportFormat.TIFF:
            return TiffExportOptions(TiffCompression(str(raw["compression"])))
        if export_format is ExportFormat.TGA:
            return TgaExportOptions(bool(raw["rle"]))
        if export_format is ExportFormat.GIF:
            return GifExportOptions(
                int(raw["colors"]),
                int(raw["alpha_threshold"]),
                bool(raw["dither"]),
            )
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        return default_export_options(export_format)


__all__ = ["ImageExportDialog", "ImageExportSpec"]
