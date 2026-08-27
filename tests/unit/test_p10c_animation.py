from __future__ import annotations

from uuid import uuid4

from easypicture.commands import DocumentCommandService
from easypicture.domain import (
    Document,
    SpriteAnimation,
    SpriteFrameDefinition,
    SpritePlaybackCursor,
    SpritePlaybackMode,
    onion_neighbor_indices,
)
from easypicture.persistence.schema import migrate_to_current_schema
from easypicture.pixels import Rect


def _frames(count: int = 3) -> tuple[SpriteFrameDefinition, ...]:
    return tuple(
        SpriteFrameDefinition.create(f"frame_{index}", Rect(index, 0, 1, 1))
        for index in range(count)
    )


def test_playback_cursor_uses_ticks_without_timer_drift() -> None:
    frames = _frames()
    frames = (
        frames[0].with_changes(duration_ticks=1),
        frames[1].with_changes(duration_ticks=2),
        frames[2].with_changes(duration_ticks=1),
    )
    animation = SpriteAnimation(
        uuid4(),
        "走路",
        tuple(frame.id for frame in frames),
        10,
        SpritePlaybackMode.LOOP,
    )
    cursor = SpritePlaybackCursor(animation, {frame.id: frame for frame in frames})
    cursor.play(1.0)
    assert cursor.deadline == 1.1
    assert cursor.advance(1.31)
    assert cursor.index == 2
    assert cursor.deadline is not None and abs(cursor.deadline - 1.4) < 1e-9


def test_once_loop_and_ping_pong_sequences() -> None:
    frames = _frames()
    frame_map = {frame.id: frame for frame in frames}
    ids = tuple(frame.id for frame in frames)

    once = SpritePlaybackCursor(
        SpriteAnimation(uuid4(), "单次", ids, 10, SpritePlaybackMode.ONCE),
        frame_map,
    )
    once.play(0.0)
    once.advance(1.0)
    assert once.index == 2 and not once.playing

    ping_pong = SpritePlaybackCursor(
        SpriteAnimation(uuid4(), "往返", ids, 10, SpritePlaybackMode.PING_PONG),
        frame_map,
    )
    ping_pong.play(0.0)
    observed = []
    for tick in range(1, 6):
        ping_pong.advance(tick / 10 + 1e-6)
        observed.append(ping_pong.index)
    assert observed == [1, 2, 1, 0, 1]

    single = SpritePlaybackCursor(
        SpriteAnimation(
            uuid4(),
            "单帧",
            (frames[0].id,),
            10,
            SpritePlaybackMode.LOOP,
        ),
        frame_map,
    )
    single.play(0.0)
    assert not single.advance(1.0)
    assert single.index == 0 and single.playing


def test_onion_neighbors_follow_playback_path() -> None:
    frames = _frames(4)
    ids = tuple(frame.id for frame in frames)
    loop = SpriteAnimation(uuid4(), "循环", ids)
    assert onion_neighbor_indices(loop, 0, 1, 2, 2) == ((3, 2), (1, 2))
    once = loop.with_changes(playback_mode=SpritePlaybackMode.ONCE)
    assert onion_neighbor_indices(once, 0, 1, 2, 2) == ((), (1, 2))
    ping_pong = loop.with_changes(playback_mode=SpritePlaybackMode.PING_PONG)
    assert onion_neighbor_indices(ping_pong, 3, -1, 1, 2) == ((2,), (2, 1))


def test_animation_properties_and_frame_duration_are_atomic() -> None:
    document = Document.create(4, 2)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(document.size.__class__(2, 2), clear_frames=False)
    assert service.create_sprite_frame(Rect(0, 0, 2, 2))
    assert service.create_sprite_frame(Rect(2, 0, 2, 2))
    frame_ids = tuple(frame.id for frame in document.sprite_workspace.frames)
    assert service.create_sprite_animation("待机")
    animation = document.sprite_workspace.animations[0]
    assert service.move_sprite_frames_to_animation(frame_ids, animation.id, 0)
    assert service.set_sprite_animation_fps(animation.id, 24)
    assert service.set_sprite_animation_playback_mode(
        animation.id, SpritePlaybackMode.PING_PONG
    )
    assert service.set_sprite_frame_duration_ticks(frame_ids, 3)
    assert all(
        frame.duration_ticks == 3 for frame in document.sprite_workspace.frames
    )
    service.undo_stack.undo()
    assert all(
        frame.duration_ticks == 1 for frame in document.sprite_workspace.frames
    )


def test_schema_v3_groups_migrate_to_v4_animations() -> None:
    manifest = {
        "schema_version": 3,
        "canvas": {"width": 32, "height": 32},
        "document_kind": "project",
        "sprite_workspace": {
            "cell_size": {"width": 16, "height": 16},
            "frames": [
                {
                    "id": str(uuid4()),
                    "content_mode": "linked",
                    "embedded_document_id": None,
                    "embedded_path": None,
                }
            ],
            "groups": [
                {"id": str(uuid4()), "name": "待机", "frame_ids": []}
            ],
        },
    }
    migrated = migrate_to_current_schema(manifest)
    workspace = migrated["sprite_workspace"]
    assert migrated["schema_version"] == 4
    assert "groups" not in workspace
    assert workspace["animations"][0]["fps"] == 12
    assert workspace["animations"][0]["playback_mode"] == "loop"
    assert workspace["frames"][0]["duration_ticks"] == 1
