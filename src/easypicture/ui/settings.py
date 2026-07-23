"""Typed application settings stored through QSettings."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMainWindow

from easypicture.domain import Size
from easypicture.domain.document import MAX_DOCUMENT_DIMENSION

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


__all__ = [
    "AppSettings",
    "DEFAULT_BACKGROUND",
    "DEFAULT_DOCUMENT_SIZE",
    "DEFAULT_FOREGROUND",
]
