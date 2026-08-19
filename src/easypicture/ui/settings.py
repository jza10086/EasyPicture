"""Typed application settings stored through QSettings."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QMainWindow

from easypicture.domain import Size
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION
from easypicture.imaging import (
    MAX_BRUSH_SIZE,
    BrushSettings,
    PaintBlendMode,
    SelectionCombineMode,
    ShapeKind,
    ShapeOutputTarget,
    ShapePaintStyle,
    ShapeSettings,
    TransformInterpolation,
)
from easypicture.persistence import ExportFormat
from easypicture.render import RenderBackendKind
from easypicture.tools import BucketSettings, MagicWandSettings
from easypicture.ui.selection_style import (
    DEFAULT_SELECTION_OUTLINE_COLOR,
    DEFAULT_SELECTION_OUTLINE_WIDTH,
    MAX_SELECTION_OUTLINE_WIDTH,
)

DEFAULT_DOCUMENT_SIZE = Size(1920, 1080)
DEFAULT_FOREGROUND = QColor("#000000")
DEFAULT_BACKGROUND = QColor("#ffffff")
DEFAULT_NUMERIC_DRAG_SENSITIVITY = 0.01
DEFAULT_NUMERIC_FINE_DRAG_SENSITIVITY = 0.001
MIN_NUMERIC_DRAG_SENSITIVITY = 0.0001
MAX_NUMERIC_DRAG_SENSITIVITY = 0.1
MAX_RECENT_FILES = 10
SPRITE_THUMBNAIL_SIZES = (48, 64, 96)
WINDOW_STATE_VERSION = 2


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

    def group_expanded(self, group_id: UUID) -> bool:
        value = self._settings.value(f"layers/groups/{group_id}/expanded", True)
        if isinstance(value, bool):
            return value
        return str(value).lower() not in {"0", "false", "no"}

    def set_group_expanded(self, group_id: UUID, expanded: bool) -> None:
        self._settings.setValue(
            f"layers/groups/{group_id}/expanded",
            bool(expanded),
        )

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

    def render_backend(self) -> RenderBackendKind:
        value = str(
            self._settings.value(
                "render/backend",
                RenderBackendKind.AUTO.value,
            )
        )
        try:
            return RenderBackendKind(value)
        except ValueError:
            return RenderBackendKind.AUTO

    def set_render_backend(self, backend: RenderBackendKind) -> None:
        self._settings.setValue("render/backend", backend.value)

    def color_adjustment_state(self, kind: str) -> str | None:
        value = self._settings.value(f"color_adjustments/{kind}")
        return None if value is None else str(value)

    def set_color_adjustment_state(self, kind: str, state: str) -> None:
        self._settings.setValue(f"color_adjustments/{kind}", state)

    def last_export_format(self) -> ExportFormat:
        value = str(
            self._settings.value(
                "export/last_format",
                ExportFormat.PNG.value,
            )
        )
        try:
            return ExportFormat(value)
        except ValueError:
            return ExportFormat.PNG

    def last_export_directory(self) -> Path | None:
        value = self._settings.value("export/last_directory")
        if value is None or not str(value).strip():
            return None
        return Path(str(value))

    def export_options_state(self, export_format: ExportFormat) -> str | None:
        value = self._settings.value(f"export/options/{export_format.value}")
        return None if value is None else str(value)

    def set_export_state(
        self,
        export_format: ExportFormat,
        directory: Path,
        state: str,
    ) -> None:
        self._settings.setValue("export/last_format", export_format.value)
        self._settings.setValue("export/last_directory", str(directory))
        self._settings.setValue(f"export/options/{export_format.value}", state)
        self._settings.sync()

    def recent_files(self) -> tuple[Path, ...]:
        raw = self._settings.value("files/recent", [])
        if isinstance(raw, (list, tuple)):
            values = list(raw)
        elif raw is None:
            values = []
        else:
            values = [raw]
        result: list[Path] = []
        seen: set[str] = set()
        for value in values:
            path = normalize_recent_path(Path(str(value)))
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
            if len(result) >= MAX_RECENT_FILES:
                break
        return tuple(result)

    def add_recent_file(self, path: Path) -> tuple[Path, ...]:
        candidate = normalize_recent_path(path)
        candidate_key = os.path.normcase(str(candidate))
        paths = [
            existing
            for existing in self.recent_files()
            if os.path.normcase(str(existing)) != candidate_key
        ]
        paths.insert(0, candidate)
        self._set_recent_files(paths[:MAX_RECENT_FILES])
        return tuple(paths[:MAX_RECENT_FILES])

    def remove_recent_file(self, path: Path) -> tuple[Path, ...]:
        candidate_key = os.path.normcase(str(normalize_recent_path(path)))
        paths = [
            existing
            for existing in self.recent_files()
            if os.path.normcase(str(existing)) != candidate_key
        ]
        self._set_recent_files(paths)
        return tuple(paths)

    def clear_recent_files(self) -> None:
        self._set_recent_files([])

    def numeric_drag_sensitivity(self) -> float:
        return self._bounded_float(
            "ui/numeric_drag_sensitivity",
            DEFAULT_NUMERIC_DRAG_SENSITIVITY,
            MIN_NUMERIC_DRAG_SENSITIVITY,
            MAX_NUMERIC_DRAG_SENSITIVITY,
        )

    def numeric_fine_drag_sensitivity(self) -> float:
        return self._bounded_float(
            "ui/numeric_fine_drag_sensitivity",
            DEFAULT_NUMERIC_FINE_DRAG_SENSITIVITY,
            MIN_NUMERIC_DRAG_SENSITIVITY,
            MAX_NUMERIC_DRAG_SENSITIVITY,
        )

    def set_numeric_drag_sensitivity(self, normal: float, fine: float) -> None:
        self._settings.setValue(
            "ui/numeric_drag_sensitivity",
            min(MAX_NUMERIC_DRAG_SENSITIVITY, max(MIN_NUMERIC_DRAG_SENSITIVITY, normal)),
        )
        self._settings.setValue(
            "ui/numeric_fine_drag_sensitivity",
            min(MAX_NUMERIC_DRAG_SENSITIVITY, max(MIN_NUMERIC_DRAG_SENSITIVITY, fine)),
        )

    def pixel_exact_coordinates(self) -> bool:
        return self._bool_value("sprite/pixel_exact_coordinates", False)

    def set_pixel_exact_coordinates(self, enabled: bool) -> None:
        self._settings.setValue("sprite/pixel_exact_coordinates", bool(enabled))

    def pixel_grid_visible(self) -> bool:
        return self._bool_value("sprite/pixel_grid_visible", False)

    def set_pixel_grid_visible(self, visible: bool) -> None:
        self._settings.setValue("sprite/pixel_grid_visible", bool(visible))

    def sprite_grid_visible(self) -> bool:
        return self._bool_value("sprite/cell_grid_visible", True)

    def set_sprite_grid_visible(self, visible: bool) -> None:
        self._settings.setValue("sprite/cell_grid_visible", bool(visible))

    def sprite_grid_snap_enabled(self) -> bool:
        return self._bool_value("sprite/cell_grid_snap", False)

    def set_sprite_grid_snap_enabled(self, enabled: bool) -> None:
        self._settings.setValue("sprite/cell_grid_snap", bool(enabled))

    def sprite_thumbnail_size(self) -> int:
        value = self._int_value("sprite/thumbnail_size", 64)
        return value if value in SPRITE_THUMBNAIL_SIZES else 64

    def set_sprite_thumbnail_size(self, size: int) -> None:
        resolved = size if size in SPRITE_THUMBNAIL_SIZES else 64
        self._settings.setValue("sprite/thumbnail_size", resolved)

    def brush_settings(self) -> BrushSettings:
        return BrushSettings(
            size=self._bounded_int("tools/brush/size", 32, 1, MAX_BRUSH_SIZE),
            hardness=self._bounded_float("tools/brush/hardness", 0.8, 0.0, 1.0),
            opacity=self._bounded_float("tools/brush/opacity", 1.0, 0.0, 1.0),
            spacing=self._bounded_float("tools/brush/spacing", 0.15, 0.01, 2.0),
            antialias=self._bool_value("tools/brush/antialias", True),
            mode=self._paint_blend_mode("tools/brush/mode"),
        )

    def set_brush_settings(self, settings: BrushSettings) -> None:
        self._settings.setValue("tools/brush/size", settings.size)
        self._settings.setValue("tools/brush/hardness", settings.hardness)
        self._settings.setValue("tools/brush/opacity", settings.opacity)
        self._settings.setValue("tools/brush/spacing", settings.spacing)
        self._settings.setValue("tools/brush/antialias", settings.antialias)
        self._settings.setValue("tools/brush/mode", settings.mode.value)

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
            mode=self._paint_blend_mode("tools/bucket/mode"),
        )

    def set_bucket_settings(self, settings: BucketSettings) -> None:
        self._settings.setValue("tools/bucket/tolerance", settings.tolerance)
        self._settings.setValue("tools/bucket/connectivity", settings.connectivity)
        self._settings.setValue("tools/bucket/contiguous", settings.contiguous)
        self._settings.setValue("tools/bucket/sample_merged", settings.sample_merged)
        self._settings.setValue("tools/bucket/edge_expansion", settings.edge_expansion)
        self._settings.setValue("tools/bucket/mode", settings.mode.value)

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

    def shape_kind(self) -> ShapeKind:
        value = str(self._settings.value("tools/shape/kind", ShapeKind.RECTANGLE.value))
        try:
            return ShapeKind(value)
        except ValueError:
            return ShapeKind.RECTANGLE

    def shape_settings(self) -> ShapeSettings:
        style_value = str(
            self._settings.value(
                "tools/shape/style",
                ShapePaintStyle.STROKE.value,
            )
        )

        try:
            style = ShapePaintStyle(style_value)
        except ValueError:
            style = ShapePaintStyle.STROKE
        stroke = self._color_value("tools/shape/stroke_color", DEFAULT_FOREGROUND)
        fill = self._color_value("tools/shape/fill_color", DEFAULT_BACKGROUND)
        return ShapeSettings(
            style=style,
            stroke_color=(
                stroke.red(),
                stroke.green(),
                stroke.blue(),
                stroke.alpha(),
            ),
            fill_color=(
                fill.red(),
                fill.green(),
                fill.blue(),
                fill.alpha(),
            ),
            stroke_width=self._bounded_float(
                "tools/shape/stroke_width",
                1.0,
                1.0,
                8192.0,
            ),
            opacity=self._bounded_float(
                "tools/shape/opacity",
                1.0,
                0.0,
                1.0,
            ),
            antialias=self._bool_value("tools/shape/antialias", True),
            blend_mode=self._paint_blend_mode("tools/shape/blend_mode"),
        )

    def shape_output_target(self) -> ShapeOutputTarget:
        value = str(
            self._settings.value(
                "tools/shape/output_target",
                ShapeOutputTarget.RASTER.value,
            )
        )
        try:
            return ShapeOutputTarget(value)
        except ValueError:
            return ShapeOutputTarget.RASTER

    def set_shape_options(
        self,
        kind: ShapeKind,
        settings: ShapeSettings,
        output_target: ShapeOutputTarget | None = None,
    ) -> None:
        self._settings.setValue("tools/shape/kind", kind.value)
        self._settings.setValue("tools/shape/style", settings.style.value)
        self._settings.setValue(
            "tools/shape/stroke_color",
            QColor.fromRgb(*settings.stroke_color).name(QColor.NameFormat.HexArgb),
        )
        self._settings.setValue(
            "tools/shape/fill_color",
            QColor.fromRgb(*settings.fill_color).name(QColor.NameFormat.HexArgb),
        )
        self._settings.setValue("tools/shape/stroke_width", settings.stroke_width)
        self._settings.setValue("tools/shape/opacity", settings.opacity)
        self._settings.setValue("tools/shape/antialias", settings.antialias)
        self._settings.setValue("tools/shape/blend_mode", settings.blend_mode.value)
        if output_target is not None:
            self._settings.setValue("tools/shape/output_target", output_target.value)

    def transform_interpolation(self) -> TransformInterpolation:
        value = str(
            self._settings.value(
                "tools/transform/interpolation",
                TransformInterpolation.LINEAR.value,
            )
        )
        try:
            return TransformInterpolation(value)
        except ValueError:
            return TransformInterpolation.LINEAR

    def transform_irregular_edit(self) -> bool:
        return self._bool_value("tools/transform/irregular_edit", False)

    def set_transform_options(
        self,
        interpolation: TransformInterpolation,
        irregular_edit: bool,
    ) -> None:
        self._settings.setValue("tools/transform/interpolation", interpolation.value)
        self._settings.setValue("tools/transform/irregular_edit", irregular_edit)

    def restore_window(self, window: QMainWindow) -> None:
        geometry = self._settings.value("window/geometry")
        state = self._settings.value("window/state")
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            window.restoreGeometry(geometry)
        if isinstance(state, QByteArray) and not state.isEmpty():
            window.restoreState(state, WINDOW_STATE_VERSION)
        self._ensure_window_visible(window)

    def save_window(self, window: QMainWindow) -> None:
        self._settings.setValue("window/geometry", window.saveGeometry())
        self._settings.setValue("window/state", window.saveState(WINDOW_STATE_VERSION))
        self._settings.sync()

    def sync(self) -> None:
        self._settings.sync()

    @staticmethod
    def _ensure_window_visible(window: QMainWindow) -> None:
        screens = QGuiApplication.screens()
        if not screens:
            return
        frame = window.frameGeometry()
        screen = max(
            screens,
            key=lambda candidate: (
                frame.intersected(candidate.availableGeometry()).width()
                * frame.intersected(candidate.availableGeometry()).height()
            ),
        )
        available = screen.availableGeometry()
        width = min(window.width(), available.width())
        height = min(window.height(), available.height())
        if (width, height) != (window.width(), window.height()):
            window.resize(width, height)
        x = min(max(frame.x(), available.left()), available.right() - width + 1)
        y = min(max(frame.y(), available.top()), available.bottom() - height + 1)
        window.move(x, y)

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

    def _paint_blend_mode(self, key: str) -> PaintBlendMode:
        value = str(self._settings.value(key, PaintBlendMode.NORMAL.value))
        try:
            return PaintBlendMode(value)
        except ValueError:
            return PaintBlendMode.NORMAL

    def _set_recent_files(self, paths: list[Path]) -> None:
        self._settings.setValue("files/recent", [str(path) for path in paths])
        self._settings.sync()


def normalize_recent_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(str(path))))


__all__ = [
    "AppSettings",
    "DEFAULT_BACKGROUND",
    "DEFAULT_DOCUMENT_SIZE",
    "DEFAULT_FOREGROUND",
    "DEFAULT_NUMERIC_DRAG_SENSITIVITY",
    "DEFAULT_NUMERIC_FINE_DRAG_SENSITIVITY",
    "DEFAULT_SELECTION_OUTLINE_COLOR",
    "DEFAULT_SELECTION_OUTLINE_WIDTH",
    "MAX_SELECTION_OUTLINE_WIDTH",
    "MAX_NUMERIC_DRAG_SENSITIVITY",
    "MAX_RECENT_FILES",
    "MIN_NUMERIC_DRAG_SENSITIVITY",
    "normalize_recent_path",
]
