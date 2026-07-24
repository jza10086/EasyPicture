"""Application-wide Blender-inspired dark theme."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

DARK_STYLESHEET = """
QWidget {
    selection-background-color: #4772b3;
    selection-color: #f2f2f2;
}
QToolTip {
    color: #eeeeee;
    background-color: #202020;
    border: 1px solid #111111;
    padding: 3px 5px;
}
QMenuBar {
    background-color: #292929;
    border-bottom: 1px solid #151515;
    spacing: 2px;
}
QMenuBar::item {
    background: transparent;
    padding: 4px 8px;
}
QMenuBar::item:selected {
    background-color: #424242;
}
QMenu {
    background-color: #292929;
    border: 1px solid #151515;
    padding: 3px;
}
QMenu::item {
    padding: 5px 24px 5px 8px;
}
QMenu::item:selected {
    background-color: #4772b3;
}
QMenu::separator {
    background-color: #151515;
    height: 1px;
    margin: 3px 5px;
}
QToolBar {
    background-color: #303030;
    border: 0;
    border-bottom: 1px solid #171717;
    spacing: 2px;
    padding: 2px;
}
QToolBar#ToolsToolbar {
    background-color: #292929;
    border-right: 1px solid #151515;
}
QToolButton {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 2px;
    padding: 4px 6px;
}
QToolButton:hover {
    background-color: #424242;
    border-color: #505050;
}
QToolButton:checked {
    background-color: #4772b3;
    border-color: #5d88c7;
}
QPushButton {
    background-color: #3a3a3a;
    border: 1px solid #181818;
    border-radius: 2px;
    min-height: 20px;
    padding: 3px 8px;
}
QPushButton:hover {
    background-color: #484848;
}
QPushButton:pressed {
    background-color: #242424;
}
QPushButton:disabled {
    background-color: #303030;
    color: #777777;
}
QLineEdit,
QSpinBox,
QDoubleSpinBox,
QComboBox {
    background-color: #1f1f1f;
    border: 1px solid #151515;
    border-radius: 2px;
    padding: 2px 4px;
    min-height: 19px;
}
QLineEdit:focus,
QSpinBox:focus,
QDoubleSpinBox:focus,
QComboBox:focus {
    border-color: #5683c2;
}
QComboBox::drop-down,
QSpinBox::up-button,
QSpinBox::down-button,
QDoubleSpinBox::up-button,
QDoubleSpinBox::down-button {
    background-color: #343434;
    border: 0;
    border-left: 1px solid #151515;
}
QAbstractItemView {
    background-color: #1f1f1f;
    alternate-background-color: #242424;
    border: 1px solid #151515;
    outline: 0;
}
QAbstractItemView::item {
    min-height: 22px;
    padding: 1px 4px;
}
QAbstractItemView::item:selected {
    background-color: #4772b3;
    color: #f2f2f2;
}
QCheckBox {
    spacing: 5px;
}
QCheckBox::indicator {
    width: 13px;
    height: 13px;
}
QTabWidget::pane {
    border: 1px solid #151515;
    background-color: #292929;
}
QTabBar {
    background-color: #202020;
}
QTabBar::tab {
    background-color: #292929;
    border: 1px solid #151515;
    padding: 4px 10px;
}
QTabBar::tab:hover {
    background-color: #3a3a3a;
}
QTabBar::tab:selected {
    background-color: #424242;
    border-bottom-color: #4772b3;
}
QScrollBar:vertical {
    background-color: #202020;
    width: 12px;
    margin: 0;
}
QScrollBar:horizontal {
    background-color: #202020;
    height: 12px;
    margin: 0;
}
QScrollBar::handle {
    background-color: #505050;
    border-radius: 3px;
    min-height: 24px;
    min-width: 24px;
}
QScrollBar::handle:hover {
    background-color: #626262;
}
QScrollBar::add-line,
QScrollBar::sub-line,
QScrollBar::add-page,
QScrollBar::sub-page {
    background: transparent;
    border: 0;
}
QStatusBar {
    background-color: #292929;
    border-top: 1px solid #151515;
}
QStatusBar::item {
    border: 0;
}
QProgressBar {
    background-color: #1f1f1f;
    border: 1px solid #151515;
    border-radius: 2px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #4772b3;
}
"""


def dark_palette() -> QPalette:
    """Return the stable application palette used by the dark theme."""
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#292929",
        QPalette.ColorRole.WindowText: "#d7d7d7",
        QPalette.ColorRole.Base: "#1f1f1f",
        QPalette.ColorRole.AlternateBase: "#242424",
        QPalette.ColorRole.ToolTipBase: "#202020",
        QPalette.ColorRole.ToolTipText: "#eeeeee",
        QPalette.ColorRole.Text: "#d7d7d7",
        QPalette.ColorRole.Button: "#3a3a3a",
        QPalette.ColorRole.ButtonText: "#d7d7d7",
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Link: "#6f9ed9",
        QPalette.ColorRole.Highlight: "#4772b3",
        QPalette.ColorRole.HighlightedText: "#f2f2f2",
        QPalette.ColorRole.Light: "#4a4a4a",
        QPalette.ColorRole.Midlight: "#383838",
        QPalette.ColorRole.Mid: "#292929",
        QPalette.ColorRole.Dark: "#181818",
        QPalette.ColorRole.Shadow: "#0e0e0e",
        QPalette.ColorRole.PlaceholderText: "#858585",
    }
    for role, value in colors.items():
        palette.setColor(role, QColor(value))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#777777"))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Highlight,
        QColor("#3b4c63"),
    )
    return palette


def apply_dark_theme(app: QApplication) -> None:
    """Apply the application theme before constructing the main window."""
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(DARK_STYLESHEET)


__all__ = ["DARK_STYLESHEET", "apply_dark_theme", "dark_palette"]
