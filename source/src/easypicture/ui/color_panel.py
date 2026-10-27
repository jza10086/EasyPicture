"""Persistent foreground/background color controls."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QWidget

from easypicture.ui.dialogs import DialogService
from easypicture.ui.settings import AppSettings


class ColorPanel(QWidget):
    colors_changed = Signal(object, object)

    def __init__(
        self,
        settings: AppSettings,
        dialogs: DialogService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._dialogs = dialogs
        self._foreground = settings.foreground_color()
        self._background = settings.background_color()

        self.foreground_button = QPushButton("前景色", self)
        self.background_button = QPushButton("背景色", self)
        self.swap_button = QPushButton("交换颜色", self)
        self.foreground_button.setToolTip("打开前景色选择器")
        self.background_button.setToolTip("打开背景色选择器")
        self.foreground_button.clicked.connect(self._choose_foreground)
        self.background_button.clicked.connect(self._choose_background)
        self.swap_button.clicked.connect(self.swap_colors)

        layout = QGridLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(6)
        layout.addWidget(QLabel("当前颜色", self), 0, 0, 1, 2)
        layout.addWidget(self.foreground_button, 1, 0)
        layout.addWidget(self.background_button, 1, 1)
        layout.addWidget(self.swap_button, 2, 0, 1, 2)
        layout.setRowStretch(3, 1)
        self._refresh_buttons()

    @property
    def foreground(self) -> QColor:
        return QColor(self._foreground)

    @property
    def background(self) -> QColor:
        return QColor(self._background)

    def set_foreground(self, color: QColor) -> None:
        if not color.isValid() or color == self._foreground:
            return
        self._foreground = QColor(color)
        self._persist_and_emit()

    def set_background(self, color: QColor) -> None:
        if not color.isValid() or color == self._background:
            return
        self._background = QColor(color)
        self._persist_and_emit()

    def swap_colors(self) -> None:
        self._foreground, self._background = self._background, self._foreground
        self._persist_and_emit()

    def _choose_foreground(self) -> None:
        color = self._dialogs.choose_color(self, self._foreground, "选择前景色")
        if color is not None:
            self.set_foreground(color)

    def _choose_background(self) -> None:
        color = self._dialogs.choose_color(self, self._background, "选择背景色")
        if color is not None:
            self.set_background(color)

    def _persist_and_emit(self) -> None:
        self._settings.set_foreground_color(self._foreground)
        self._settings.set_background_color(self._background)
        self._settings.sync()
        self._refresh_buttons()
        self.colors_changed.emit(self.foreground, self.background)

    def _refresh_buttons(self) -> None:
        self.foreground_button.setStyleSheet(self._color_style(self._foreground))
        self.background_button.setStyleSheet(self._color_style(self._background))

    @staticmethod
    def _color_style(color: QColor) -> str:
        contrasting = "#ffffff" if color.lightness() < 128 else "#000000"
        rgba = f"{color.red()}, {color.green()}, {color.blue()}, {color.alpha()}"
        return f"QPushButton {{background-color: rgba({rgba});color: {contrasting};}}"


__all__ = ["ColorPanel"]
