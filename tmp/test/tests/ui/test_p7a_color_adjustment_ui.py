from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, Qt

from easypicture.controller import ApplicationController
from easypicture.imaging import (
    BrightnessContrastAdjustment,
    ColorAdjustmentKind,
    ColorReplaceAdjustment,
)
from easypicture.pixels import TileCoord
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from tests.fakes import FakeDialogService, make_settings


def _window(qtbot, tmp_path) -> tuple[ApplicationController, MainWindow]:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(64, 48)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.resize(900, 650)
    window.show()
    window.new_action.trigger()
    document = controller.document
    assert document is not None
    tile = np.zeros((256, 256, 4), dtype=np.uint8)
    tile[:48, :64] = (40, 80, 120, 200)
    document.replace_layer_tiles(
        document.active_layer_id,
        {TileCoord(0, 0): tile},
    )
    return controller, window


def test_apply_waits_for_full_result_freezes_parameters_and_cancel_drops_late_result(
    qtbot, tmp_path, monkeypatch
):
    from dataclasses import replace
    from uuid import uuid4

    from easypicture.domain import Size
    from easypicture.imaging import ColorAdjustmentResult, InvertAdjustment
    from easypicture.jobs import CancellationToken, JobHandle, JobOutcome, JobStatus

    controller, window = _window(qtbot, tmp_path)
    document = controller.document
    handles = []

    def submit(_task):
        handle = JobHandle(uuid4(), CancellationToken())
        handles.append(handle)
        return handle

    monkeypatch.setattr(controller.preview_jobs, "submit", submit)
    session = window.color_adjustment_controller
    for cancel in (True, False):
        window.color_adjustment_actions[ColorAdjustmentKind.INVERT].trigger()
        session._debounce.stop()
        session._submit_latest()
        tile = document.active_layer.pixels.read_tile(0, 0).copy()
        tile[:48, :64, :3] = 255 - tile[:48, :64, :3]
        tile.flags.writeable = False
        full = ColorAdjustmentResult(
            document.id, document.revision, document.active_layer_id, {TileCoord(0, 0): tile}
        )
        low = replace(full, preview_size=Size(32, 24))
        handles[-1].finished.emit(JobOutcome(handles[-1].job_id, JobStatus.SUCCESS, value=low))
        assert not session.has_exact_result
        assert controller.commands.undo_stack.count() == 0
        session.apply()
        assert not session.update_parameters(InvertAdjustment())
        assert controller.commands.undo_stack.count() == 0
        if cancel:
            session.cancel()
        handles[-1].finished.emit(JobOutcome(handles[-1].job_id, JobStatus.SUCCESS, value=full))
        assert not session.active
        assert controller.commands.undo_stack.count() == (0 if cancel else 1)
    np.testing.assert_array_equal(document.active_layer.pixels.read_tile(0, 0), tile)


def test_adjustment_preview_is_non_destructive_and_applies_one_command(
    qtbot,
    tmp_path,
) -> None:
    controller, window = _window(qtbot, tmp_path)
    document = controller.document
    assert document is not None
    before = document.active_layer.pixels.read_tile(0, 0).copy()
    revision = document.revision

    window.color_adjustment_actions[ColorAdjustmentKind.BRIGHTNESS_CONTRAST].trigger()
    panel = window.color_adjustment_panel
    panel.brightness.setValue(25)
    qtbot.waitUntil(
        lambda: window.color_adjustment_controller.has_exact_result,
        timeout=3000,
    )

    assert document.revision == revision
    assert np.array_equal(document.active_layer.pixels.read_tile(0, 0), before)
    assert window.canvas_view.render_coordinator.preview_tile_count == 1
    assert window.tool_details_stack.currentWidget() is panel
    assert not window.layer_panel.isEnabled()
    assert not window.undo_action.isEnabled()
    assert not window.save_action.isEnabled()

    qtbot.mouseClick(panel.apply_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(
        lambda: not window.color_adjustment_controller.active,
        timeout=3000,
    )
    assert controller.commands.undo_stack.count() == 1
    assert document.revision == revision + 1
    assert not np.array_equal(document.active_layer.pixels.read_tile(0, 0), before)
    controller.commands.undo_stack.undo()
    assert np.array_equal(document.active_layer.pixels.read_tile(0, 0), before)


def test_adjustment_cancel_and_revision_change_clear_preview(qtbot, tmp_path) -> None:
    controller, window = _window(qtbot, tmp_path)
    document = controller.document
    assert document is not None
    before = document.active_layer.pixels.read_tile(0, 0).copy()
    window.color_adjustment_actions[ColorAdjustmentKind.INVERT].trigger()
    qtbot.waitUntil(
        lambda: window.color_adjustment_controller.has_exact_result,
        timeout=3000,
    )
    qtbot.mouseClick(
        window.color_adjustment_panel.cancel_button,
        Qt.MouseButton.LeftButton,
    )
    assert not window.color_adjustment_controller.active
    assert window.canvas_view.render_coordinator.preview_tile_count == 0
    assert np.array_equal(document.active_layer.pixels.read_tile(0, 0), before)

    window.color_adjustment_actions[ColorAdjustmentKind.INVERT].trigger()
    assert window.color_adjustment_controller.active
    document.record_change()
    qtbot.waitUntil(lambda: not window.color_adjustment_controller.active)
    assert window.canvas_view.render_coordinator.preview_tile_count == 0


def test_latest_parameter_generation_wins_and_state_is_persisted(qtbot, tmp_path) -> None:
    _controller, window = _window(qtbot, tmp_path)
    window.color_adjustment_actions[ColorAdjustmentKind.BRIGHTNESS_CONTRAST].trigger()
    panel = window.color_adjustment_panel
    for value in (10, 20, 35):
        panel.brightness.setValue(value)
    qtbot.waitUntil(
        lambda: window.color_adjustment_controller.has_exact_result,
        timeout=3000,
    )
    parameters = window.color_adjustment_controller.parameters
    assert isinstance(parameters, BrightnessContrastAdjustment)
    assert parameters.brightness == 35
    saved = window.settings.color_adjustment_state(ColorAdjustmentKind.BRIGHTNESS_CONTRAST.value)
    assert saved is not None and '"brightness": 35' in saved


def test_color_replace_canvas_sampler_uses_authoritative_pixel(qtbot, tmp_path) -> None:
    _controller, window = _window(qtbot, tmp_path)
    window.color_adjustment_actions[ColorAdjustmentKind.COLOR_REPLACE].trigger()
    panel = window.color_adjustment_panel
    qtbot.mouseClick(panel.replace_sample, Qt.MouseButton.LeftButton)
    assert window.color_adjustment_controller.sampling
    assert not window.color_adjustment_controller.eventFilter(
        window.canvas_view.viewport(),
        QEvent(QEvent.Type.Paint),
    )
    viewport_point = window.canvas_view.mapFromScene(10.0, 10.0)
    qtbot.mouseClick(
        window.canvas_view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=viewport_point,
    )
    assert not window.color_adjustment_controller.sampling
    parameters = panel.parameters()
    assert isinstance(parameters, ColorReplaceAdjustment)
    assert parameters.source == (40, 80, 120, 200)


def test_all_adjustment_actions_open_right_panel_and_finish_exact_preview(
    qtbot,
    tmp_path,
) -> None:
    _controller, window = _window(qtbot, tmp_path)
    for kind in ColorAdjustmentKind:
        window.color_adjustment_actions[kind].trigger()
        assert window.color_adjustment_controller.active
        assert window.color_adjustment_panel.kind is kind
        assert window.tool_details_stack.currentWidget() is window.color_adjustment_panel
        qtbot.waitUntil(
            lambda: window.color_adjustment_controller.has_exact_result,
            timeout=3000,
        )
        window.color_adjustment_controller.cancel()
        assert not window.color_adjustment_controller.active


def test_enter_applies_adjustment_after_numeric_editor_finishes(
    qtbot,
    tmp_path,
) -> None:
    controller, window = _window(qtbot, tmp_path)
    window.color_adjustment_actions[ColorAdjustmentKind.BRIGHTNESS_CONTRAST].trigger()
    panel = window.color_adjustment_panel
    panel.brightness.setValue(20)
    qtbot.waitUntil(
        lambda: window.color_adjustment_controller.has_exact_result,
        timeout=3000,
    )

    qtbot.mouseClick(panel.brightness, Qt.MouseButton.LeftButton)
    qtbot.keyClick(panel.brightness.lineEdit(), Qt.Key.Key_Return)
    assert window.color_adjustment_controller.active

    qtbot.keyClick(window, Qt.Key.Key_Return)
    qtbot.waitUntil(
        lambda: not window.color_adjustment_controller.active,
        timeout=3000,
    )
    assert controller.commands.undo_stack.count() == 1
