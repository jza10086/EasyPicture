"""Qt-free sprite workspace metadata for game-asset slicing."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid4

from easypicture.domain.geometry import PixelPoint, Size
from easypicture.pixels import Rect

MAX_SPRITE_FRAMES = 1024
MAX_SPRITE_ANIMATIONS = 128
_UNSET = object()


class DocumentKind(StrEnum):
    PROJECT = "project"
    SPRITE_FRAME = "sprite_frame"


class SpriteFrameContentMode(StrEnum):
    LINKED = "linked"
    DETACHED = "detached"


class SpritePlaybackMode(StrEnum):
    ONCE = "once"
    LOOP = "loop"
    PING_PONG = "ping_pong"


class AnimationDeleteMode(StrEnum):
    MOVE_TO_UNGROUPED = "move_to_ungrouped"
    DELETE_FRAMES = "delete_frames"


@dataclass(frozen=True, slots=True)
class SpriteAnimation:
    id: UUID
    name: str
    frame_ids: tuple[UUID, ...] = ()
    fps: int = 12
    playback_mode: SpritePlaybackMode = SpritePlaybackMode.LOOP

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("sprite animation name cannot be empty")
        if len(self.frame_ids) != len(set(self.frame_ids)):
            raise ValueError("sprite animation contains duplicate frames")
        if not 1 <= self.fps <= 240:
            raise ValueError("sprite animation fps must be in range 1..240")

    @classmethod
    def create(cls, name: str) -> SpriteAnimation:
        return cls(uuid4(), name.strip())

    def with_changes(
        self,
        *,
        name: str | None = None,
        frame_ids: tuple[UUID, ...] | None = None,
        fps: int | None = None,
        playback_mode: SpritePlaybackMode | None = None,
    ) -> SpriteAnimation:
        return replace(
            self,
            name=self.name if name is None else name.strip(),
            frame_ids=self.frame_ids if frame_ids is None else frame_ids,
            fps=self.fps if fps is None else fps,
            playback_mode=(self.playback_mode if playback_mode is None else playback_mode),
        )


@dataclass(frozen=True, slots=True)
class SpriteFrameDefinition:
    id: UUID
    name: str
    region: Rect
    pivot: PixelPoint
    content_mode: SpriteFrameContentMode = SpriteFrameContentMode.LINKED
    embedded_document_id: UUID | None = None
    duration_ticks: int = 1

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
        if not 1 <= self.duration_ticks <= 999:
            raise ValueError("sprite frame duration must be in range 1..999 ticks")

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
        duration_ticks: int | None = None,
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
            duration_ticks=(self.duration_ticks if duration_ticks is None else duration_ticks),
        )


@dataclass(slots=True)
class SpriteWorkspace:
    cell_size: Size
    frames: list[SpriteFrameDefinition] = field(default_factory=list)
    animations: list[SpriteAnimation] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.frames) > MAX_SPRITE_FRAMES:
            raise ValueError("sprite workspace contains too many frames")
        if len(self.animations) > MAX_SPRITE_ANIMATIONS:
            raise ValueError("sprite workspace contains too many animations")
        ids = [frame.id for frame in self.frames]
        names = [frame.name for frame in self.frames]
        if len(ids) != len(set(ids)):
            raise ValueError("sprite frame ids must be unique")
        if len(names) != len(set(names)):
            raise ValueError("sprite frame names must be unique")
        animation_ids = [animation.id for animation in self.animations]
        animation_names = [animation.name for animation in self.animations]
        if len(animation_ids) != len(set(animation_ids)):
            raise ValueError("sprite animation ids must be unique")
        if len(animation_names) != len(set(animation_names)):
            raise ValueError("sprite animation names must be unique")
        known_frames = set(ids)
        memberships: list[UUID] = []
        for animation in self.animations:
            if any(frame_id not in known_frames for frame_id in animation.frame_ids):
                raise ValueError("sprite animation references an unknown frame")
            memberships.extend(animation.frame_ids)
        if len(memberships) != len(set(memberships)):
            raise ValueError("sprite frame can only belong to one animation")
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
            list(self.animations),
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

    def animation_by_id(self, animation_id: UUID) -> SpriteAnimation:
        try:
            return next(animation for animation in self.animations if animation.id == animation_id)
        except StopIteration as error:
            raise ValueError("sprite animation does not belong to the workspace") from error

    def animation_for_frame(self, frame_id: UUID) -> SpriteAnimation | None:
        return next(
            (animation for animation in self.animations if frame_id in animation.frame_ids),
            None,
        )

    @property
    def ungrouped_frame_ids(self) -> tuple[UUID, ...]:
        assigned = {frame_id for animation in self.animations for frame_id in animation.frame_ids}
        return tuple(frame.id for frame in self.frames if frame.id not in assigned)


__all__ = [
    "MAX_SPRITE_FRAMES",
    "MAX_SPRITE_ANIMATIONS",
    "DocumentKind",
    "AnimationDeleteMode",
    "SpriteFrameContentMode",
    "SpriteFrameDefinition",
    "SpriteAnimation",
    "SpritePlaybackMode",
    "SpriteWorkspace",
]
