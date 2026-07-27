from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMainWindow

from easypicture.domain import Document, Size
from easypicture.ui.color_panel import ColorPanel
from tests.fakes import FakeDialogService, make_settings


def test_settings_round_trip_size_and_colors(tmp_path) -> None:
    path = str(tmp_path / "settings.ini")
    first = make_settings(path)
    first.set_last_document_size(Size(1234, 567))
    first.set_foreground_color(QColor("#123456"))
    first.set_background_color(QColor("#abcdef"))
    first.set_selection_outline_style(QColor("#35a7ff"), 4)
    first.sync()

    second = make_settings(path)

    assert second.last_document_size() == Size(1234, 567)
    assert second.foreground_color().name() == "#123456"
    assert second.background_color().name() == "#abcdef"
    assert second.selection_outline_color().name() == "#35a7ff"
    assert second.selection_outline_width() == 4


def test_invalid_settings_fall_back_to_defaults(tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    settings._settings.setValue("new_document/width", 0)  # noqa: SLF001
    settings._settings.setValue("new_document/height", 9000)  # noqa: SLF001
    settings._settings.setValue("colors/foreground", "invalid")  # noqa: SLF001
    settings._settings.setValue("selection/outline_color", "invalid")  # noqa: SLF001
    settings._settings.setValue("selection/outline_width", 99)  # noqa: SLF001

    assert settings.last_document_size() == Size(1920, 1080)
    assert settings.foreground_color().name() == "#000000"
    assert settings.selection_outline_color().name() == "#f28c28"
    assert settings.selection_outline_width() == 8


def test_color_panel_persists_without_modifying_document(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    panel = ColorPanel(settings, dialogs)
    qtbot.addWidget(panel)
    document = Document.create(10, 10)

    panel.set_foreground(QColor("#ff0000"))
    panel.set_background(QColor("#0000ff"))
    panel.swap_colors()

    assert panel.foreground.name() == "#0000ff"
    assert panel.background.name() == "#ff0000"
    assert settings.foreground_color().name() == "#0000ff"
    assert settings.background_color().name() == "#ff0000"
    assert not document.modified
    assert document.revision == 0


def test_window_geometry_round_trip(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    first = QMainWindow()
    qtbot.addWidget(first)
    first.resize(777, 555)
    settings.save_window(first)

    second = QMainWindow()
    qtbot.addWidget(second)
    settings.restore_window(second)

    assert second.size() == first.size()
