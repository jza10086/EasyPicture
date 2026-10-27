"""Qt-free sprite animation playback and onion-skin traversal."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from easypicture.domain.sprite import (
    SpriteAnimation,
    SpriteFrameDefinition,
    SpritePlaybackMode,
)


def _next_index(
    frame_count: int,
    index: int,
    direction: int,
    mode: SpritePlaybackMode,
) -> tuple[int, int, bool]:
    if frame_count <= 1:
        return 0, 1, mode is SpritePlaybackMode.ONCE
    if mode is SpritePlaybackMode.ONCE:
        if index >= frame_count - 1:
            return frame_count - 1, 1, True
        return index + 1, 1, False
    if mode is SpritePlaybackMode.LOOP:
        return (index + 1) % frame_count, 1, False
    candidate = index + direction
    if candidate >= frame_count:
        direction = -1
        candidate = frame_count - 2
    elif candidate < 0:
        direction = 1
        candidate = 1
    return candidate, direction, False


def _previous_index(
    frame_count: int,
    index: int,
    direction: int,
    mode: SpritePlaybackMode,
) -> tuple[int, int] | None:
    if frame_count <= 1:
        return None
    if mode is SpritePlaybackMode.ONCE:
        return None if index <= 0 else (index - 1, 1)
    if mode is SpritePlaybackMode.LOOP:
        return ((index - 1) % frame_count, 1)
    candidate = index - direction
    previous_direction = direction
    if candidate >= frame_count:
        previous_direction = -1
        candidate = frame_count - 2
    elif candidate < 0:
        previous_direction = 1
        candidate = 1
    return candidate, previous_direction


@dataclass(slots=True)
class SpritePlaybackCursor:
    """Deadline-based cursor; delayed timer callbacks cannot accumulate drift."""

    animation: SpriteAnimation
    frames: dict[UUID, SpriteFrameDefinition]
    index: int = 0
    direction: int = 1
    deadline: float | None = None
    playing: bool = False

    def __post_init__(self) -> None:
        missing = set(self.animation.frame_ids).difference(self.frames)
        if missing:
            raise ValueError("sprite animation references a missing frame")
        if self.animation.frame_ids:
            self.index = min(max(self.index, 0), len(self.animation.frame_ids) - 1)
        else:
            self.index = 0
        self.direction = 1 if self.direction >= 0 else -1

    @property
    def frame_id(self) -> UUID | None:
        if not self.animation.frame_ids:
            return None
        return self.animation.frame_ids[self.index]

    @property
    def total_duration_seconds(self) -> float:
        ticks = sum(self.frames[frame_id].duration_ticks for frame_id in self.animation.frame_ids)
        return ticks / self.animation.fps

    def _duration(self) -> float:
        frame_id = self.frame_id
        if frame_id is None:
            return 0.0
        return self.frames[frame_id].duration_ticks / self.animation.fps

    def play(self, now: float) -> None:
        if self.frame_id is None:
            self.playing = False
            self.deadline = None
            return
        self.playing = True
        self.deadline = now + self._duration()

    def pause(self) -> None:
        self.playing = False
        self.deadline = None

    def restart_current_tick(self, now: float) -> None:
        if self.playing:
            self.deadline = now + self._duration()

    def set_index(self, index: int, now: float | None = None) -> bool:
        if not self.animation.frame_ids:
            return False
        resolved = min(max(index, 0), len(self.animation.frame_ids) - 1)
        changed = resolved != self.index
        self.index = resolved
        self.direction = 1
        if now is not None:
            self.restart_current_tick(now)
        return changed

    def step(self, offset: int, now: float | None = None) -> bool:
        if not self.animation.frame_ids or offset == 0:
            return False
        resolved = min(
            max(self.index + offset, 0),
            len(self.animation.frame_ids) - 1,
        )
        changed = resolved != self.index
        self.index = resolved
        self.direction = 1
        if now is not None:
            self.restart_current_tick(now)
        return changed

    def advance(self, now: float) -> bool:
        if not self.playing or self.deadline is None or now < self.deadline:
            return False
        changed = False
        steps = 0
        while self.playing and self.deadline is not None and now >= self.deadline:
            next_index, next_direction, terminal = _next_index(
                len(self.animation.frame_ids),
                self.index,
                self.direction,
                self.animation.playback_mode,
            )
            if terminal:
                self.playing = False
                self.deadline = None
                break
            changed = changed or next_index != self.index
            self.index = next_index
            self.direction = next_direction
            self.deadline += self._duration()
            steps += 1
            if steps >= 10_000:
                self.deadline = now + self._duration()
                break
        return changed


def onion_neighbor_indices(
    animation: SpriteAnimation,
    current_index: int,
    direction: int,
    previous_count: int,
    next_count: int,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return previous/next unique frame definitions along the playback path."""

    frame_count = len(animation.frame_ids)
    if frame_count <= 1:
        return (), ()
    previous: list[int] = []
    cursor = current_index
    cursor_direction = 1 if direction >= 0 else -1
    for _ in range(max(previous_count, 0)):
        item = _previous_index(
            frame_count,
            cursor,
            cursor_direction,
            animation.playback_mode,
        )
        if item is None:
            break
        cursor, cursor_direction = item
        if cursor == current_index or cursor in previous:
            break
        previous.append(cursor)

    following: list[int] = []
    cursor = current_index
    cursor_direction = 1 if direction >= 0 else -1
    for _ in range(max(next_count, 0)):
        cursor, cursor_direction, terminal = _next_index(
            frame_count,
            cursor,
            cursor_direction,
            animation.playback_mode,
        )
        if terminal or cursor == current_index or cursor in following:
            break
        following.append(cursor)
    return tuple(previous), tuple(following)


__all__ = ["SpritePlaybackCursor", "onion_neighbor_indices"]
