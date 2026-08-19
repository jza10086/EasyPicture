"""Qt-free sprite workspace metadata for game-asset slicing."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid4

from easypicture.domain.geometry import PixelPoint, Size
from easypicture.pixels import Rect

MAX_SPRITE_FRAMES = 1024
MAX_SPRITE_FRAME_GROUPS = 128
_UNSET = object()


class DocumentKind(StrEnum):
    PROJECT = "project"
    SPRITE_FRAME = "sprite_frame"


class SpriteFrameContentMode(StrEnum):
    LINKED = "linked"
    DETACHED = "detached"


class GroupDeleteMode(StrEnum):
    MOVE_TO_UNGROUPED = "move_to_ungrouped"
    DELETE_FRAMES = "delete_frames"


@dataclass(frozen=True, slots=True)
class SpriteFrameGroup:
    id: UUID
    name: str
    frame_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("sprite frame group name cannot be empty")
        if len(self.frame_ids) != len(set(self.frame_ids)):
            raise ValueError("sprite frame group contains duplicate frames")

    @classmethod
    def create(cls, name: str) -> SpriteFrameGroup:
        return cls(uuid4(), name.strip())

    def with_changes(
        self,
        *,
        name: str | None = None,
        frame_ids: tuple[UUID, ...] | None = None,
    ) -> SpriteFrameGroup:
        return replace(
            self,
            name=self.name if name is None else name.strip(),
            frame_ids=self.frame_ids if frame_ids is None else frame_ids,
        )


@dataclass(frozen=True, slots=True)
class SpriteFrameDefinition:
    id: UUID
    name: str
    region: Rect
    pivot: PixelPoint
    content_mode: SpriteFrameContentMode = SpriteFrameContentMode.LINKED
    embedded_document_id: UUID | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("sprite frame name cannot be empty")
        if self.region.is_empty:
            raise ValueError("sprite frame region cannot be empty")
        if not 0 <= self.pivot.x < self.region.width:
            raise ValueError("sprite frame pivot x is out of range")
        if not 0 <= self.pivot.y < self.region.height:
            raise ValueError("sprite frame pivot y is out of range")
        if self.content_mode is SpriteFrameContentMode.LINKED:
            if self.embedded_document_id is not None:
                raise ValueError("linked sprite frame cannot reference an embedded document")
        elif self.embedded_document_id is None:
            raise ValueError("detached sprite frame must reference an embedded document")

    @classmethod
    def create(cls, name: str, region: Rect) -> SpriteFrameDefinition:
        return cls(
            id=uuid4(),
            name=name.strip(),
            region=region,
            pivot=PixelPoint(region.width // 2, region.height // 2),
        )

    def with_changes(
        self,
        *,
        name: str | None = None,
        region: Rect | None = None,
        pivot: PixelPoint | None = None,
        content_mode: SpriteFrameContentMode | None = None,
        embedded_document_id: UUID | None | object = _UNSET,
    ) -> SpriteFrameDefinition:
        resolved_document_id = (
            self.embedded_document_id
            if embedded_document_id is _UNSET
            else cast(UUID | None, embedded_document_id)
        )
        return replace(
            self,
            name=self.name if name is None else name.strip(),
            region=self.region if region is None else region,
            pivot=self.pivot if pivot is None else pivot,
            content_mode=self.content_mode if content_mode is None else content_mode,
            embedded_document_id=resolved_document_id,
        )


@dataclass(slots=True)
class SpriteWorkspace:
    cell_size: Size
    frames: list[SpriteFrameDefinition] = field(default_factory=list)
    groups: list[SpriteFrameGroup] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.frames) > MAX_SPRITE_FRAMES:
            raise ValueError("sprite workspace contains too many frames")
        if len(self.groups) > MAX_SPRITE_FRAME_GROUPS:
            raise ValueError("sprite workspace contains too many frame groups")
        ids = [frame.id for frame in self.frames]
        names = [frame.name for frame in self.frames]
        if len(ids) != len(set(ids)):
            raise ValueError("sprite frame ids must be unique")
        if len(names) != len(set(names)):
            raise ValueError("sprite frame names must be unique")
        group_ids = [group.id for group in self.groups]
        group_names = [group.name for group in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("sprite frame group ids must be unique")
        if len(group_names) != len(set(group_names)):
            raise ValueError("sprite frame group names must be unique")
        known_frames = set(ids)
        memberships: list[UUID] = []
        for group in self.groups:
            if any(frame_id not in known_frames for frame_id in group.frame_ids):
                raise ValueError("sprite frame group references an unknown frame")
            memberships.extend(group.frame_ids)
        if len(memberships) != len(set(memberships)):
            raise ValueError("sprite frame can only belong to one group")
        for frame in self.frames:
            if (frame.region.width, frame.region.height) != (
                self.cell_size.width,
                self.cell_size.height,
            ):
                raise ValueError("sprite frame size must match workspace cell size")

    @classmethod
    def create(cls, canvas_size: Size) -> SpriteWorkspace:
        return cls(Size(min(32, canvas_size.width), min(32, canvas_size.height)))

    def clone(self) -> SpriteWorkspace:
        return SpriteWorkspace(
            self.cell_size,
            list(self.frames),
            list(self.groups),
        )

    def validate_for_canvas(self, canvas_size: Size) -> None:
        if self.cell_size.width > canvas_size.width or self.cell_size.height > canvas_size.height:
            raise ValueError("sprite cell size exceeds the canvas")
        for frame in self.frames:
            if frame.region.right > canvas_size.width or frame.region.bottom > canvas_size.height:
                raise ValueError("sprite frame region exceeds the canvas")

    def frame_by_id(self, frame_id: UUID) -> SpriteFrameDefinition:
        try:
            return next(frame for frame in self.frames if frame.id == frame_id)
        except StopIteration as error:
            raise ValueError("sprite frame does not belong to the workspace") from error

    def group_by_id(self, group_id: UUID) -> SpriteFrameGroup:
        try:
            return next(group for group in self.groups if group.id == group_id)
        except StopIteration as error:
            raise ValueError("sprite frame group does not belong to the workspace") from error

    def group_for_frame(self, frame_id: UUID) -> SpriteFrameGroup | None:
        return next(
            (group for group in self.groups if frame_id in group.frame_ids),
            None,
        )

    @property
    def ungrouped_frame_ids(self) -> tuple[UUID, ...]:
        grouped = {frame_id for group in self.groups for frame_id in group.frame_ids}
        return tuple(frame.id for frame in self.frames if frame.id not in grouped)


__all__ = [
    "MAX_SPRITE_FRAMES",
    "MAX_SPRITE_FRAME_GROUPS",
    "DocumentKind",
    "GroupDeleteMode",
    "SpriteFrameContentMode",
    "SpriteFrameDefinition",
    "SpriteFrameGroup",
    "SpriteWorkspace",
]
