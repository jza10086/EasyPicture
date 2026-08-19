from __future__ import annotations

from easypicture.domain import DocumentKind
from easypicture.pixels import Rect
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from easypicture.workspace import EditingSurfaceRef, WorkspaceController
from tests.fakes import FakeDialogService, make_settings


def test_two_project_tabs_share_active_workspace(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(64, 64)
    controller = WorkspaceController(
        settings,
        dialogs,
        recovery_enabled=False,
    )
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)

    assert controller.request_new_document(window)
    assert controller.request_new_document(window)
    assert len(controller.sessions) == 2
    assert window.workspace_tabs.count() == 2
    assert window.sprite_frame_panel.project_tabs.count() == 2
    assert controller.sessions[0].document.display_name == "Untitled"
    assert controller.sessions[1].document.display_name == "Untitled.1"
    assert window.workspace_tabs.tabText(0) == "Untitled*"
    assert window.workspace_tabs.tabText(1) == "Untitled.1*"

    controller.set_active_session(0)
    assert controller.active_session is controller.sessions[0]
    assert window.workspace_tabs.currentIndex() == 0
    assert window.windowTitle() == "Untitled* — EasyPicture"


def test_detached_frame_opens_as_second_level_surface(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(32, 32)
    controller = WorkspaceController(settings, dialogs, recovery_enabled=False)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    assert controller.request_new_document(window)
    project = controller.project_document
    assert project is not None
    assert controller.commands.create_sprite_frame(Rect(0, 0, 32, 32))
    frame = project.sprite_workspace.frames[0]
    assert controller.commands.detach_sprite_frame(frame.id)

    assert controller.open_frame_surface(frame.id)
    assert controller.document is not None
    assert controller.document.kind is DocumentKind.SPRITE_FRAME
    assert controller.active_session is not None
    assert controller.active_session.active_surface == EditingSurfaceRef("frame", frame.id)
    assert window.surface_tabs.count() == 2

    controller.set_active_surface(EditingSurfaceRef("source"))
    assert controller.document is project
    project.mark_clean()


def test_linked_frame_opens_read_only_and_can_be_detached(qtbot, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(32, 32)
    controller = WorkspaceController(settings, dialogs, recovery_enabled=False)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    assert controller.request_new_document(window)
    project = controller.project_document
    assert project is not None
    assert controller.commands.create_sprite_frame(Rect(0, 0, 32, 32))
    frame = project.sprite_workspace.frames[0]

    assert controller.open_frame_surface(frame.id)
    assert controller.document is not None
    assert controller.document.kind is DocumentKind.SPRITE_FRAME
    assert controller.active_session is not None
    assert controller.active_session.active_surface_is_linked
    assert "只读" in window.surface_tabs.tabText(1)
    assert not controller.commands.create_layer()

    assert controller.commands.detach_sprite_frame(frame.id)
    assert not controller.active_session.active_surface_is_linked
    assert controller.commands.create_layer()
    project.mark_clean()
