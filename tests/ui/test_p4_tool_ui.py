from __future__ import annotations

from PySide6.QtCore import QPointF, Qt

from easypicture.controller import ApplicationController
from easypicture.imaging import BrushSettings
from easypicture.tools import BucketSettings
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from tests.fakes import FakeDialogService, make_settings


def test_tool_settings_round_trip(tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    brush = BrushSettings(size=73, hardness=0.25, opacity=0.6, spacing=1.25, antialias=False)
    bucket = BucketSettings(
        tolerance=17,
        connectivity=8,
        contiguous=False,
        sample_merged=True,
    )

    settings.set_brush_settings(brush)
    settings.set_bucket_settings(bucket)
    settings.set_eyedropper_sample_merged(False)
    settings.sync()

    restored = make_settings(str(tmp_path / "settings.ini"))
    assert restored.brush_settings() == brush
    assert restored.bucket_settings() == bucket
    assert not restored.eyedropper_sample_merged()


def test_main_window_enables_p4_tools_only_with_document(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(128, 128)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)

    assert all(not action.isEnabled() for action in window.tool_actions.values())

    window.new_action.trigger()

    assert all(action.isEnabled() for action in window.tool_actions.values())
    assert window.tool_actions["brush"].isChecked()
    window.tool_actions["bucket"].trigger()
    assert window.tool_options_panel.currentWidget() is window.tool_options_panel.bucket


def test_brush_gesture_commits_one_undo_command(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(128, 128)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.resize(700, 500)
    window.show()
    window.new_action.trigger()
    qtbot.wait(1)
    start = window.canvas_view.mapFromScene(QPointF(30.0, 64.0))
    end = window.canvas_view.mapFromScene(QPointF(100.0, 64.0))

    qtbot.mousePress(window.canvas_view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(window.canvas_view.viewport(), end)
    qtbot.mouseRelease(window.canvas_view.viewport(), Qt.MouseButton.LeftButton, pos=end)

    assert controller.document is not None
    assert controller.document.modified
    assert controller.commands.undo_stack.count() == 1
    alpha = controller.document.active_layer.pixels.read_tile(0, 0)[64, 30:101, 3]
    assert alpha.max() > 0
