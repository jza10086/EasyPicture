from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy

from easypicture.domain import SpritePlaybackMode
from easypicture.pixels import Rect
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from easypicture.workspace import EditingSurfaceRef, WorkspaceController
from tests.fakes import FakeDialogService, make_settings


def _window_with_animation(qtbot, tmp_path):
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(64, 32)
    controller = WorkspaceController(settings, dialogs, recovery_enabled=False)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    assert controller.request_new_document(window)
    assert controller.commands.create_sprite_frame(Rect(0, 0, 32, 32))
    assert controller.commands.create_sprite_frame(Rect(32, 0, 32, 32))
    assert controller.commands.create_sprite_animation("待机")
    workspace = controller.project_document.sprite_workspace
    animation = workspace.animations[0]
    assert controller.commands.move_sprite_frames_to_animation(
        tuple(frame.id for frame in workspace.frames), animation.id, 0
    )
    return window, controller, animation.id


def test_animation_surface_is_read_only_and_has_transport(qtbot, tmp_path) -> None:
    window, controller, animation_id = _window_with_animation(qtbot, tmp_path)
    window._open_sprite_animation_surface(animation_id)

    assert controller.active_session is not None
    assert controller.active_session.active_surface == EditingSurfaceRef(
        "animation", animation_id=animation_id
    )
    assert controller.active_session.active_surface_is_read_only
    assert window.animation_toolbar.isVisibleTo(window)
    assert window.animation_toolbar.position_label.text() == "1 / 2"
    assert not controller.commands.create_layer()

    window.canvas_view.set_zoom(2.5)
    window._seek_animation("next")
    assert window.animation_toolbar.position_label.text() == "2 / 2"
    assert window.canvas_view.zoom == 2.5
    controller.project_document.mark_clean()


def test_animation_panel_changes_fps_mode_and_batch_ticks(qtbot, tmp_path) -> None:
    window, controller, animation_id = _window_with_animation(qtbot, tmp_path)
    project = controller.project_document
    assert project is not None
    window.sprite_frame_panel.set_active_group(animation_id)
    window._set_sprite_animation_fps(animation_id, 24)
    mode_index = window.sprite_frame_panel.animation_mode.findData(SpritePlaybackMode.PING_PONG)
    window.sprite_frame_panel.animation_mode.setCurrentIndex(mode_index)
    frame_ids = tuple(frame.id for frame in project.sprite_workspace.frames)
    window.sprite_frame_panel.list.selectAll()
    window._set_selected_sprite_frame_duration(3)

    animation = project.sprite_workspace.animation_by_id(animation_id)
    assert animation.fps == 24
    assert animation.playback_mode is SpritePlaybackMode.PING_PONG
    assert all(
        project.sprite_workspace.frame_by_id(frame_id).duration_ticks == 3 for frame_id in frame_ids
    )
    window.sprite_properties.pivot_marker_radius.setValue(7)
    window.sprite_properties.pivot_marker_radius.editingFinished.emit()
    assert window.settings.sprite_pivot_marker_radius() == 7
    project.mark_clean()


def test_playback_keeps_selected_animation_mode(qtbot, tmp_path) -> None:
    window, controller, animation_id = _window_with_animation(qtbot, tmp_path)
    window._open_sprite_animation_surface(animation_id)
    mode_index = window.animation_toolbar.mode.findData(SpritePlaybackMode.PING_PONG)
    window.animation_toolbar.mode.setCurrentIndex(mode_index)

    window._toggle_animation_playback(True)
    toolbar_mode = window.animation_toolbar.mode.currentData()
    window._toggle_animation_playback(False)

    animation = controller.project_document.sprite_workspace.animation_by_id(animation_id)
    assert animation.playback_mode is SpritePlaybackMode.PING_PONG
    assert SpritePlaybackMode(str(toolbar_mode)) is SpritePlaybackMode.PING_PONG
    controller.project_document.mark_clean()


def test_animation_panel_exposes_open_preview(qtbot, tmp_path) -> None:
    window, controller, animation_id = _window_with_animation(qtbot, tmp_path)
    panel = window.sprite_frame_panel
    panel.set_active_group(animation_id)
    spy = QSignalSpy(panel.animation_open_requested)
    qtbot.mouseClick(panel.open_animation_button, Qt.MouseButton.LeftButton)
    assert spy.count() == 1
    assert spy.at(0) == [animation_id]
    controller.project_document.mark_clean()


def test_slice_reference_preview_defaults_to_latest_and_supports_manual_pick(
    qtbot,
    tmp_path,
) -> None:
    window, controller, animation_id = _window_with_animation(qtbot, tmp_path)
    project = controller.project_document
    assert project is not None
    first, latest = project.sprite_workspace.frames
    window.sprite_frame_panel.set_active_group(animation_id)

    window._new_sprite_frame()
    assert window._slice_preview_reference_id == latest.id
    assert window._slice_preview_enabled
    tool = window._tools["sprite_slice"]
    assert tool._overlay is not None
    assert tool._overlay._reference_image is not None

    window.sprite_properties.slice_preview_opacity.setValue(64)
    assert tool._overlay._reference_opacity == 0.64

    window._confirm_sprite_region(Rect(0, 0, 32, 32))
    newest = project.sprite_workspace.frames[-1]
    assert newest.id not in {first.id, latest.id}
    assert window._slice_preview_reference_id == newest.id

    qtbot.mouseClick(
        window.sprite_properties.pick_slice_preview,
        Qt.MouseButton.LeftButton,
    )
    assert window._slice_preview_picking
    window.sprite_frame_panel.frame_clicked.emit(first.id)
    assert window._slice_preview_reference_id == first.id
    assert window._slice_preview_manual_reference
    assert not window._slice_preview_picking
    assert window._sprite_editing_frame_id is None
    project.mark_clean()
