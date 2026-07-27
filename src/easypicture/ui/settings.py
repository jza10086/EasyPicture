"""Typed application settings stored through QSettings."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMainWindow

from easypicture.domain import Size
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION
from easypicture.imaging import MAX_BRUSH_SIZE, BrushSettings, SelectionCombineMode
from easypicture.tools import BucketSettings, MagicWandSettings
from easypicture.ui.selection_style import (
    DEFAULT_SELECTION_OUTLINE_COLOR,
    DEFAULT_SELECTION_OUTLINE_WIDTH,
    MAX_SELECTION_OUTLINE_WIDTH,
)

DEFAULT_DOCUMENT_SIZE = Size(1920, 1080)
DEFAULT_FOREGROUND = QColor("#000000")
DEFAULT_BACKGROUND = QColor("#ffffff")


class AppSettings:
    """Small typed boundary around QSettings."""

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings if settings is not None else QSettings()

    def last_document_size(self) -> Size:
        width = self._int_value("new_document/width", DEFAULT_DOCUMENT_SIZE.width)
        height = self._int_value("new_document/height", DEFAULT_DOCUMENT_SIZE.height)
        if not 1 <= width <= MAX_DOCUMENT_DIMENSION:
            width = DEFAULT_DOCUMENT_SIZE.width
        if not 1 <= height <= MAX_DOCUMENT_DIMENSION:
            height = DEFAULT_DOCUMENT_SIZE.height
        return Size(width, height)

    def set_last_document_size(self, size: Size) -> None:
        self._settings.setValue("new_document/width", size.width)
        self._settings.setValue("new_document/height", size.height)

    def foreground_color(self) -> QColor:
        return self._color_value("colors/foreground", DEFAULT_FOREGROUND)

    def background_color(self) -> QColor:
        return self._color_value("colors/background", DEFAULT_BACKGROUND)

    def set_foreground_color(self, color: QColor) -> None:
        self._settings.setValue("colors/foreground", color.name(QColor.NameFormat.HexArgb))

    def set_background_color(self, color: QColor) -> None:
        self._settings.setValue("colors/background", color.name(QColor.NameFormat.HexArgb))

    def selection_outline_color(self) -> QColor:
        color = self._color_value(
            "selection/outline_color",
            DEFAULT_SELECTION_OUTLINE_COLOR,
        )
        color.setAlpha(255)
        return color

    def selection_outline_width(self) -> int:
        return self._bounded_int(
            "selection/outline_width",
            DEFAULT_SELECTION_OUTLINE_WIDTH,
            1,
            MAX_SELECTION_OUTLINE_WIDTH,
        )

    def set_selection_outline_style(self, color: QColor, width: int) -> None:
        opaque = QColor(color)
        opaque.setAlpha(255)
        self._settings.setValue(
            "selection/outline_color",
            opaque.name(QColor.NameFormat.HexRgb),
        )
        self._settings.setValue(
            "selection/outline_width",
            min(MAX_SELECTION_OUTLINE_WIDTH, max(1, width)),
        )

    def brush_settings(self) -> BrushSettings:
        return BrushSettings(
            size=self._bounded_int("tools/brush/size", 32, 1, MAX_BRUSH_SIZE),
            hardness=self._bounded_float("tools/brush/hardness", 0.8, 0.0, 1.0),
            opacity=self._bounded_float("tools/brush/opacity", 1.0, 0.0, 1.0),
            spacing=self._bounded_float("tools/brush/spacing", 0.15, 0.01, 2.0),
            antialias=self._bool_value("tools/brush/antialias", True),
        )

    def set_brush_settings(self, settings: BrushSettings) -> None:
        self._settings.setValue("tools/brush/size", settings.size)
        self._settings.setValue("tools/brush/hardness", settings.hardness)
        self._settings.setValue("tools/brush/opacity", settings.opacity)
        self._settings.setValue("tools/brush/spacing", settings.spacing)
        self._settings.setValue("tools/brush/antialias", settings.antialias)

    def bucket_settings(self) -> BucketSettings:
        connectivity = self._bounded_int("tools/bucket/connectivity", 4, 4, 8)
        return BucketSettings(
            tolerance=self._bounded_int("tools/bucket/tolerance", 0, 0, 255),
            connectivity=4 if connectivity != 8 else 8,
            contiguous=self._bool_value("tools/bucket/contiguous", True),
            sample_merged=self._bool_value("tools/bucket/sample_merged", False),
            edge_expansion=self._bounded_int(
                "tools/bucket/edge_expansion",
                1,
                0,
                8,
            ),
        )

    def set_bucket_settings(self, settings: BucketSettings) -> None:
        self._settings.setValue("tools/bucket/tolerance", settings.tolerance)
        self._settings.setValue("tools/bucket/connectivity", settings.connectivity)
        self._settings.setValue("tools/bucket/contiguous", settings.contiguous)
        self._settings.setValue("tools/bucket/sample_merged", settings.sample_merged)
        self._settings.setValue("tools/bucket/edge_expansion", settings.edge_expansion)

    def eyedropper_sample_merged(self) -> bool:
        return self._bool_value("tools/eyedropper/sample_merged", True)

    def set_eyedropper_sample_merged(self, sample_merged: bool) -> None:
        self._settings.setValue("tools/eyedropper/sample_merged", sample_merged)

    def selection_combine_mode(self) -> SelectionCombineMode:
        value = str(
            self._settings.value(
                "tools/selection/combine_mode",
                SelectionCombineMode.REPLACE.value,
            )
        )
        try:
            return SelectionCombineMode(value)
        except ValueError:
            return SelectionCombineMode.REPLACE

    def set_selection_combine_mode(self, mode: SelectionCombineMode) -> None:
        self._settings.setValue("tools/selection/combine_mode", mode.value)

    def magic_wand_settings(self) -> MagicWandSettings:
        connectivity = self._bounded_int("tools/magic_wand/connectivity", 4, 4, 8)
        return MagicWandSettings(
            tolerance=self._bounded_int("tools/magic_wand/tolerance", 0, 0, 255),
            connectivity=4 if connectivity != 8 else 8,
            contiguous=self._bool_value("tools/magic_wand/contiguous", True),
            sample_merged=self._bool_value("tools/magic_wand/sample_merged", False),
            include_alpha=self._bool_value("tools/magic_wand/include_alpha", True),
            mode=self.selection_combine_mode(),
        )

    def set_magic_wand_settings(self, settings: MagicWandSettings) -> None:
        self._settings.setValue("tools/magic_wand/tolerance", settings.tolerance)
        self._settings.setValue("tools/magic_wand/connectivity", settings.connectivity)
        self._settings.setValue("tools/magic_wand/contiguous", settings.contiguous)
        self._settings.setValue("tools/magic_wand/sample_merged", settings.sample_merged)
        self._settings.setValue("tools/magic_wand/include_alpha", settings.include_alpha)
        self.set_selection_combine_mode(settings.mode)

    def restore_window(self, window: QMainWindow) -> None:
        geometry = self._settings.value("window/geometry")
        state = self._settings.value("window/state")
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            window.restoreGeometry(geometry)
        if isinstance(state, QByteArray) and not state.isEmpty():
            window.restoreState(state)

    def save_window(self, window: QMainWindow) -> None:
        self._settings.setValue("window/geometry", window.saveGeometry())
        self._settings.setValue("window/state", window.saveState())
        self._settings.sync()

    def sync(self) -> None:
        self._settings.sync()

    def _int_value(self, key: str, default: int) -> int:
        value = self._settings.value(key, default)
        try:
            return int(str(value))
        except TypeError, ValueError:
            return default

    def _color_value(self, key: str, default: QColor) -> QColor:
        value = self._settings.value(key, default.name(QColor.NameFormat.HexArgb))
        color = QColor(str(value))
        return color if color.isValid() else QColor(default)

    def _bounded_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        return min(maximum, max(minimum, self._int_value(key, default)))

    def _bounded_float(
        self,
        key: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        value = self._settings.value(key, default)
        try:
            parsed = float(str(value))
        except TypeError, ValueError:
            parsed = default
        return min(maximum, max(minimum, parsed))

    def _bool_value(self, key: str, default: bool) -> bool:
        value = self._settings.value(key, default)
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default


__all__ = [
    "AppSettings",
    "DEFAULT_BACKGROUND",
    "DEFAULT_DOCUMENT_SIZE",
    "DEFAULT_FOREGROUND",
    "DEFAULT_SELECTION_OUTLINE_COLOR",
    "DEFAULT_SELECTION_OUTLINE_WIDTH",
    "MAX_SELECTION_OUTLINE_WIDTH",
]
