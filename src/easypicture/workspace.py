"""Multi-project workspace sessions and two-level editing surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoGroup
from PySide6.QtWidgets import QMessageBox, QWidget

from easypicture.commands import DocumentCommandService
from easypicture.controller import ApplicationController
from easypicture.domain import Document, DocumentKind, SpriteFrameContentMode
from easypicture.domain.document import Unsubscribe
from easypicture.jobs import CancellationToken, JobRunner
from easypicture.persistence import (
    DocumentSnapshot,
    render_animation_preview,
    render_snapshot_region,
)
from easypicture.pixels import Rect, RGBAArray
from easypicture.ui.dialogs import DialogService
from easypicture.ui.notifications import NotificationService
from easypicture.ui.settings import AppSettings


@dataclass(frozen=True, slots=True)
class EditingSurfaceRef:
    kind: Literal["source", "frame", "animation"]
    frame_id: UUID | None = None
    animation_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.kind == "source":
            if self.frame_id is not None or self.animation_id is not None:
                raise ValueError("source surfaces cannot reference content")
        elif self.kind == "frame":
            if self.frame_id is None or self.animation_id is not None:
                raise ValueError("frame surfaces require exactly one frame")
        elif self.animation_id is None or self.frame_id is not None:
            raise ValueError("animation surfaces require exactly one animation")


@dataclass(frozen=True, slots=True)
class CanvasViewState:
    zoom: float = 1.0
    center_x: float = 0.0
    center_y: float = 0.0
    tool_name: str = "brush"
    selected_node_id: UUID | None = None


@dataclass(slots=True)
class ProjectSession:
    id: UUID
    controller: ApplicationController
    open_surfaces: list[EditingSurfaceRef] = field(
        default_factory=lambda: [EditingSurfaceRef("source")]
    )
    active_surface: EditingSurfaceRef = EditingSurfaceRef("source")
    active_sprite_animation_id: UUID | None = None
    canvas_states: dict[EditingSurfaceRef, CanvasViewState] = field(default_factory=dict)
    linked_surface_documents: dict[UUID, tuple[int, Document]] = field(
        default_factory=dict
    )
    animation_surface_documents: dict[UUID, tuple[int, Document]] = field(
        default_factory=dict
    )
    animation_surface_indices: dict[UUID, int] = field(default_factory=dict)

    @property
    def document(self) -> Document:
        document = self.controller.document
        if document is None:
            raise RuntimeError("project session has no document")
        return document

    @property
    def edit_document(self) -> Document:
        if self.active_surface.kind == "source":
            return self.document
        if self.active_surface.kind == "animation":
            assert self.active_surface.animation_id is not None
            animation_id = self.active_surface.animation_id
            cached = self.animation_surface_documents.get(animation_id)
            if cached is not None and cached[0] == self.document.revision:
                return cached[1]
            animation = self.document.sprite_workspace.animation_by_id(animation_id)
            if not animation.frame_ids:
                preview = Document.create_sprite_frame(
                    self.document.sprite_workspace.cell_size
                )
            else:
                index = min(
                    max(self.animation_surface_indices.get(animation_id, 0), 0),
                    len(animation.frame_ids) - 1,
                )
                pixels = render_animation_preview(
                    DocumentSnapshot.capture(self.document),
                    animation_id,
                    index,
                    1,
                    CancellationToken(),
                    lambda _done, _total, _label: None,
                )
                preview = self._preview_document(pixels)
            preview.mark_clean()
            self.animation_surface_documents[animation_id] = (
                self.document.revision,
                preview,
            )
            return preview
        assert self.active_surface.frame_id is not None
        frame = self.document.sprite_workspace.frame_by_id(self.active_surface.frame_id)
        if frame.content_mode is SpriteFrameContentMode.DETACHED:
            return self.document.embedded_document_for_frame(self.active_surface.frame_id)
        cached = self.linked_surface_documents.get(frame.id)
        if cached is not None and cached[0] == self.document.revision:
            return cached[1]
        pixels = render_snapshot_region(
            DocumentSnapshot.capture(self.document),
            frame.region,
            CancellationToken(),
            lambda _done, _total, _label: None,
        )
        preview = self._preview_document(pixels)
        preview.mark_clean()
        self.linked_surface_documents[frame.id] = (self.document.revision, preview)
        return preview

    def _preview_document(self, pixels: RGBAArray) -> Document:
        preview = Document.create_sprite_frame(self.document.sprite_workspace.cell_size)
        preview.active_layer.pixels.write_region(
            Rect(0, 0, preview.width, preview.height),
            pixels,
        )
        preview.active_layer.record_pixel_change()
        preview.mark_clean()
        return preview

    @property
    def active_surface_is_linked(self) -> bool:
        if self.active_surface.kind != "frame":
            return False
        assert self.active_surface.frame_id is not None
        frame = self.document.sprite_workspace.frame_by_id(self.active_surface.frame_id)
        return frame.content_mode is SpriteFrameContentMode.LINKED

    @property
    def active_surface_is_read_only(self) -> bool:
        return self.active_surface.kind == "animation" or self.active_surface_is_linked


class WorkspaceCommandProxy(QObject):
    """Stable command facade backed by the active project's own undo stack."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.undo_stack = QUndoGroup(self)
        self._service: DocumentCommandService | None = None
        self._read_only = False

    def set_service(
        self,
        service: DocumentCommandService | None,
        *,
        read_only: bool = False,
    ) -> None:
        self._service = service
        self._read_only = read_only
        if service is None:
            self.undo_stack.setActiveStack(None)  # type: ignore[arg-type]
        else:
            self.undo_stack.setActiveStack(service.undo_stack)

    @property
    def read_only(self) -> bool:
        return self._read_only

    def __getattr__(self, name: str) -> Any:
        service = self._service
        sprite_workspace_methods = {
            "create_sprite_frame",
            "update_sprite_frame",
            "delete_sprite_frame",
            "delete_sprite_frames",
            "move_sprite_frame",
            "reorder_sprite_frames",
            "set_sprite_cell_size",
            "detach_sprite_frame",
            "reset_sprite_frame_to_source",
            "duplicate_sprite_frame",
            "create_sprite_animation",
            "rename_sprite_animation",
            "move_sprite_animation",
            "delete_sprite_animation",
            "move_sprite_frame_to_animation",
            "move_sprite_frames_to_animation",
            "reorder_sprite_animation_frames",
            "set_sprite_animation_fps",
            "set_sprite_animation_playback_mode",
            "set_sprite_frame_duration_ticks",
            "reorder_ungrouped_sprite_frames",
        }
        if service is None or (self._read_only and name not in sprite_workspace_methods):
            def unavailable(*_args: object, **_kwargs: object) -> bool:
                return False

            return unavailable
        return getattr(service, name)


class WorkspaceController(QObject):
    """Own several project controllers while presenting one active UI binding."""

    document_changed = Signal(object)
    document_event = Signal(object)
    status_message = Signal(str)
    recent_files_changed = Signal(object)
    sessions_changed = Signal()
    active_session_changed = Signal(int)
    surfaces_changed = Signal()
    animation_surface_changed = Signal(object)

    def __init__(
        self,
        settings: AppSettings,
        dialogs: DialogService,
        parent: QObject | None = None,
        *,
        recovery_enabled: bool = True,
        initial_controller: ApplicationController | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.dialogs = dialogs
        self.jobs = (
            initial_controller.jobs
            if initial_controller is not None
            else JobRunner(self)
        )
        self.notifications = NotificationService(self)
        self._command_proxy = WorkspaceCommandProxy(self)
        self.commands = cast(DocumentCommandService, self._command_proxy)
        self.recovery_enabled = recovery_enabled
        self._dialog_parent: QWidget | None = None
        self._sessions: list[ProjectSession] = []
        self._active_index = -1
        self._surface_unsubscribe: Unsubscribe | None = None
        self._initial_controller = initial_controller
        if initial_controller is not None and initial_controller.document is not None:
            self._add_controller(initial_controller)

    @property
    def sessions(self) -> tuple[ProjectSession, ...]:
        return tuple(self._sessions)

    @property
    def active_session(self) -> ProjectSession | None:
        if not 0 <= self._active_index < len(self._sessions):
            return None
        return self._sessions[self._active_index]

    @property
    def document(self) -> Document | None:
        session = self.active_session
        return session.edit_document if session is not None else None

    @property
    def project_document(self) -> Document | None:
        session = self.active_session
        return session.document if session is not None else None

    @property
    def recent_files(self) -> tuple[Path, ...]:
        return self.settings.recent_files()

    def set_dialog_parent(self, parent: QWidget) -> None:
        self._dialog_parent = parent
        for session in self._sessions:
            session.controller.set_dialog_parent(parent)

    def _fresh_controller(self) -> ApplicationController:
        controller = ApplicationController(
            self.settings,
            self.dialogs,
            recovery_enabled=self.recovery_enabled,
            job_runner=self.jobs,
        )
        if self._dialog_parent is not None:
            controller.set_dialog_parent(self._dialog_parent)
        return controller

    def _add_controller(self, controller: ApplicationController) -> ProjectSession:
        session = ProjectSession(uuid4(), controller)
        self._sessions.append(session)
        controller.status_message.connect(self.status_message.emit)
        controller.recent_files_changed.connect(self.recent_files_changed.emit)
        controller.notifications.notice.connect(self.notifications.notice.emit)
        controller.document_event.connect(
            lambda event, current=session: self._on_project_event(current, event)
        )
        self.sessions_changed.emit()
        self.set_active_session(len(self._sessions) - 1)
        return session

    def request_new_document(self, parent: QWidget) -> bool:
        controller = self._fresh_controller()
        if not controller.request_new_document(parent):
            return False
        assert controller.document is not None
        used_names = {session.document.display_name for session in self._sessions}
        name = "Untitled"
        suffix = 1
        while name in used_names:
            name = f"Untitled.{suffix}"
            suffix += 1
        controller.document.set_source_name(None if name == "Untitled" else name)
        self._add_controller(controller)
        return True

    def request_open_document(self, parent: QWidget) -> bool:
        path = self.dialogs.choose_project_open_file(parent)
        return False if path is None else self.open_path(parent, path)

    def open_path(self, parent: QWidget, path: Path) -> bool:
        resolved = Path(path).resolve()
        for index, session in enumerate(self._sessions):
            project_path = session.document.project_path
            if project_path is not None and project_path.resolve() == resolved:
                self.set_active_session(index)
                return True
        controller = self._fresh_controller()
        if not controller.open_path(parent, resolved):
            return False
        self._add_controller(controller)
        return True

    def open_recent_path(self, parent: QWidget, path: Path) -> bool:
        return self.open_path(parent, path)

    def request_import_document(self, parent: QWidget) -> bool:
        path = self.dialogs.choose_image_import_file(parent)
        if path is None:
            return False
        controller = self._fresh_controller()
        if not controller.import_path(parent, path):
            return False
        self._add_controller(controller)
        return True

    def set_active_session(self, index: int) -> None:
        if not 0 <= index < len(self._sessions) or index == self._active_index:
            return
        self._unsubscribe_surface()
        self._active_index = index
        session = self._sessions[index]
        edit_document = session.edit_document
        session.controller.commands.set_edit_document(
            session.document if session.active_surface_is_read_only else edit_document
        )
        self._command_proxy.set_service(
            session.controller.commands,
            read_only=session.active_surface_is_read_only,
        )
        self._subscribe_surface(edit_document)
        self.active_session_changed.emit(index)
        self.document_changed.emit(edit_document)
        self.surfaces_changed.emit()

    def move_session(self, source_index: int, target_index: int) -> None:
        if (
            not 0 <= source_index < len(self._sessions)
            or not 0 <= target_index < len(self._sessions)
            or source_index == target_index
        ):
            return
        active = self.active_session
        self._sessions.insert(target_index, self._sessions.pop(source_index))
        if active is not None:
            self._active_index = self._sessions.index(active)
        self.sessions_changed.emit()

    def open_frame_surface(self, frame_id: UUID) -> bool:
        session = self.active_session
        if session is None:
            return False
        session.document.sprite_workspace.frame_by_id(frame_id)
        surface = EditingSurfaceRef("frame", frame_id)
        if surface not in session.open_surfaces:
            session.open_surfaces.append(surface)
        return self.set_active_surface(surface)

    def open_animation_surface(self, animation_id: UUID) -> bool:
        session = self.active_session
        if session is None:
            return False
        session.document.sprite_workspace.animation_by_id(animation_id)
        surface = EditingSurfaceRef("animation", animation_id=animation_id)
        if surface not in session.open_surfaces:
            session.open_surfaces.append(surface)
        return self.set_active_surface(surface)

    def update_animation_surface(
        self,
        animation_id: UUID,
        frame_index: int,
        pixels: RGBAArray,
    ) -> bool:
        session = self.active_session
        if session is None:
            return False
        animation = session.document.sprite_workspace.animation_by_id(animation_id)
        if not animation.frame_ids:
            return False
        resolved = min(max(frame_index, 0), len(animation.frame_ids) - 1)
        preview = session._preview_document(pixels)
        session.animation_surface_indices[animation_id] = resolved
        session.animation_surface_documents[animation_id] = (
            session.document.revision,
            preview,
        )
        surface = EditingSurfaceRef("animation", animation_id=animation_id)
        if session.active_surface == surface:
            session.controller.commands.set_edit_document(session.document)
            self._command_proxy.set_service(
                session.controller.commands,
                read_only=True,
            )
            self.animation_surface_changed.emit(preview)
        return True

    def set_active_surface(self, surface: EditingSurfaceRef) -> bool:
        session = self.active_session
        if session is None or surface not in session.open_surfaces:
            return False
        if session.active_surface == surface:
            return True
        self._unsubscribe_surface()
        session.active_surface = surface
        edit_document = session.edit_document
        session.controller.commands.set_edit_document(
            session.document if session.active_surface_is_read_only else edit_document
        )
        self._command_proxy.set_service(
            session.controller.commands,
            read_only=session.active_surface_is_read_only,
        )
        self._subscribe_surface(edit_document)
        self.document_changed.emit(edit_document)
        self.surfaces_changed.emit()
        return True

    def close_frame_surface(self, frame_id: UUID) -> None:
        session = self.active_session
        if session is None:
            return
        surface = EditingSurfaceRef("frame", frame_id)
        if surface not in session.open_surfaces:
            return
        session.open_surfaces.remove(surface)
        if session.active_surface == surface:
            self.set_active_surface(EditingSurfaceRef("source"))
        self.surfaces_changed.emit()

    def close_animation_surface(self, animation_id: UUID) -> None:
        session = self.active_session
        if session is None:
            return
        surface = EditingSurfaceRef("animation", animation_id=animation_id)
        if surface not in session.open_surfaces:
            return
        session.open_surfaces.remove(surface)
        session.animation_surface_documents.pop(animation_id, None)
        session.animation_surface_indices.pop(animation_id, None)
        if session.active_surface == surface:
            self.set_active_surface(EditingSurfaceRef("source"))
        self.surfaces_changed.emit()

    def close_document(self, parent: QWidget) -> bool:
        session = self.active_session
        if session is None or not session.controller.close_document(parent):
            return False
        self._remove_session(self._active_index)
        return True

    def close_session(self, index: int, parent: QWidget) -> bool:
        if not 0 <= index < len(self._sessions):
            return False
        session = self._sessions[index]
        if not session.controller.close_document(parent):
            return False
        self._remove_session(index)
        return True

    def _remove_session(self, index: int) -> None:
        self._unsubscribe_surface()
        self._sessions.pop(index)
        self._active_index = -1
        self.sessions_changed.emit()
        if self._sessions:
            self.set_active_session(min(index, len(self._sessions) - 1))
        else:
            self._command_proxy.set_service(None)
            self.document_changed.emit(None)
            self.surfaces_changed.emit()

    def request_exit(self, parent: QWidget) -> bool:
        modified = [session for session in self._sessions if session.document.modified]
        if modified:
            names = "\n".join(f"• {session.document.display_name}" for session in modified)
            box = QMessageBox(parent)
            box.setWindowTitle("退出 EasyPicture")
            box.setText("以下工程包含未保存更改：")
            box.setInformativeText(names)
            save_button = box.addButton("全部保存", QMessageBox.ButtonRole.AcceptRole)
            discard_button = box.addButton("全部放弃", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is save_button:
                for session in modified:
                    if not session.controller.request_save(parent):
                        return False
            elif clicked is not discard_button:
                return False
        for session in self._sessions:
            session.controller.discard_recovery()
        return True

    def request_append_document(self, parent: QWidget) -> bool:
        session = self.active_session
        return False if session is None else session.controller.request_append_document(parent)

    def request_save(self, parent: QWidget) -> bool:
        session = self.active_session
        return False if session is None else session.controller.request_save(parent)

    def request_save_as(self, parent: QWidget) -> bool:
        session = self.active_session
        return False if session is None else session.controller.request_save_as(parent)

    def request_export_image(self, parent: QWidget) -> bool:
        session = self.active_session
        return False if session is None else session.controller.request_export_image(parent)

    def request_export_sprite_frame(self, parent: QWidget, frame_id: UUID) -> bool:
        session = self.active_session
        return False if session is None else session.controller.request_export_sprite_frame(
            parent, frame_id
        )

    def export_sprite_atlas_to(
        self,
        parent: QWidget,
        path: Path,
        *,
        included_animation_ids: tuple[UUID, ...] | None = None,
        include_ungrouped: bool = True,
        horizontal_spacing: int = 0,
        vertical_spacing: int = 0,
        margin: int = 0,
    ) -> bool:
        session = self.active_session
        return False if session is None else session.controller.export_sprite_atlas_to(
            parent,
            path,
            included_animation_ids=included_animation_ids,
            include_ungrouped=include_ungrouped,
            horizontal_spacing=horizontal_spacing,
            vertical_spacing=vertical_spacing,
            margin=margin,
        )

    def clear_recent_files(self) -> None:
        self.settings.clear_recent_files()
        self.recent_files_changed.emit(self.recent_files)

    def offer_recovery(self, parent: QWidget) -> None:
        seed = self._initial_controller or self._fresh_controller()
        paths = tuple(sorted(seed.recovery_dir.glob("*.recovery.limg")))
        if not paths and seed.recovery_path.exists():
            paths = (seed.recovery_path,)
        for index, path in enumerate(paths):
            controller = seed if index == 0 and seed.document is None else self._fresh_controller()
            if controller.offer_recovery(parent, path) and controller.document is not None:
                self._add_controller(controller)

    def _subscribe_surface(self, document: Document) -> None:
        if document.kind is DocumentKind.SPRITE_FRAME:
            self._surface_unsubscribe = document.subscribe(
                lambda event: self.document_event.emit(event)
            )

    def _on_project_event(self, session: ProjectSession, event: object) -> None:
        session.linked_surface_documents.clear()
        session.animation_surface_documents.clear()
        valid_frames = {frame.id for frame in session.document.sprite_workspace.frames}
        valid_animations = {
            animation.id for animation in session.document.sprite_workspace.animations
        }
        session.open_surfaces = [
            surface
            for surface in session.open_surfaces
            if surface.kind == "source"
            or (surface.kind == "frame" and surface.frame_id in valid_frames)
            or (
                surface.kind == "animation"
                and surface.animation_id in valid_animations
            )
        ]
        if session.active_surface not in session.open_surfaces:
            session.active_surface = EditingSurfaceRef("source")
            session.controller.commands.set_edit_document(session.document)
            if session is self.active_session:
                self._unsubscribe_surface()
                self._command_proxy.set_service(session.controller.commands)
                self.document_changed.emit(session.document)
                self.surfaces_changed.emit()
        elif session is self.active_session and (
            session.active_surface_is_read_only or self._command_proxy.read_only
        ):
            self._unsubscribe_surface()
            edit_document = session.edit_document
            session.controller.commands.set_edit_document(
                session.document if session.active_surface_is_read_only else edit_document
            )
            self._command_proxy.set_service(
                session.controller.commands,
                read_only=session.active_surface_is_read_only,
            )
            self._subscribe_surface(edit_document)
            self.document_changed.emit(edit_document)
            self.surfaces_changed.emit()
        if session is self.active_session:
            self.document_event.emit(event)

    def _unsubscribe_surface(self) -> None:
        if self._surface_unsubscribe is not None:
            self._surface_unsubscribe()
            self._surface_unsubscribe = None


__all__ = [
    "CanvasViewState",
    "EditingSurfaceRef",
    "ProjectSession",
    "WorkspaceController",
]
