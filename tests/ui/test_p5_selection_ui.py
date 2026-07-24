from __future__ import annotations

from PySide6.QtCore import QPointF, Qt

from easypicture.controller import ApplicationController
from easypicture.imaging import SelectionCombineMode
from easypicture.tools import MagicWandSettings, mode_from_modifiers
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from tests.fakes import FakeDialogService, make_settings


def _window(qtbot, tmp_path) -> tuple[ApplicationController, MainWindow]:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(300, 300)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.resize(760, 560)
    window.show()
    window.new_action.trigger()
    qtbot.wait(1)
    return controller, window


def test_rectangle_selection_commits_once_and_persists_across_tool_switch(
    qtbot,
    tmp_path,
) -> None:
    controller, window = _window(qtbot, tmp_path)
    window.tool_actions["rectangle_selection"].trigger()
    start = window.canvas_view.mapFromScene(QPointF(250.0, 250.0))
    end = window.canvas_view.mapFromScene(QPointF(290.0, 290.0))

    qtbot.mousePress(window.canvas_view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(window.canvas_view.viewport(), end)
    qtbot.mouseRelease(window.canvas_view.viewport(), Qt.MouseButton.LeftButton, pos=end)

    document = controller.document
    assert document is not None
    assert document.selection.is_active
    assert document.modified
    assert controller.commands.undo_stack.count() == 1
    assert window.canvas_view._selection_overlay is not None
    assert window.canvas_view._selection_overlay._timer.isActive()

    window.tool_actions["brush"].trigger()
    assert document.selection.is_active
    assert window.overlay_manager.count == 0

    controller.commands.undo_stack.undo()
    assert not document.selection.is_active
    assert not document.modified
    assert not window.canvas_view._selection_overlay._timer.isActive()


def test_selection_menu_actions_and_enablement(qtbot, tmp_path) -> None:
    controller, window = _window(qtbot, tmp_path)
    assert window.select_all_action.isEnabled()
    assert not window.deselect_action.isEnabled()

    window.select_all_action.trigger()

    document = controller.document
    assert document is not None
    assert document.selection.is_active
    assert window.deselect_action.isEnabled()
    assert window.invert_selection_action.isEnabled()

    window.deselect_action.trigger()
    assert not document.selection.is_active
    assert not window.deselect_action.isEnabled()


def test_selection_modifier_precedence() -> None:
    assert (
        mode_from_modifiers(SelectionCombineMode.REPLACE, Qt.KeyboardModifier.NoModifier)
        is SelectionCombineMode.REPLACE
    )
    assert (
        mode_from_modifiers(
            SelectionCombineMode.REPLACE,
            Qt.KeyboardModifier.ShiftModifier,
        )
        is SelectionCombineMode.ADD
    )
    assert (
        mode_from_modifiers(
            SelectionCombineMode.REPLACE,
            Qt.KeyboardModifier.AltModifier,
        )
        is SelectionCombineMode.SUBTRACT
    )
    assert (
        mode_from_modifiers(
            SelectionCombineMode.REPLACE,
            Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier,
        )
        is SelectionCombineMode.INTERSECT
    )


def test_magic_wand_settings_round_trip(tmp_path) -> None:
    path = str(tmp_path / "settings.ini")
    settings = make_settings(path)
    expected = MagicWandSettings(
        tolerance=31,
        connectivity=8,
        contiguous=False,
        sample_merged=True,
        include_alpha=False,
        mode=SelectionCombineMode.INTERSECT,
    )
    settings.set_magic_wand_settings(expected)
    settings.sync()

    restored = make_settings(path)
    assert restored.magic_wand_settings() == expected
    assert restored.selection_combine_mode() is SelectionCombineMode.INTERSECT
