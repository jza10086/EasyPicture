from __future__ import annotations

from uuid import uuid4

import numpy as np

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, PixelPoint, SpriteAnimation
from easypicture.jobs import CancellationToken
from easypicture.persistence import (
    DocumentSnapshot,
    render_animation_preview,
    render_frame_reference_preview,
)
from easypicture.pixels import Rect


def test_onion_skin_uses_cell_coordinates_instead_of_pivot_alignment() -> None:
    document = Document.create(4, 2)
    pixels = np.zeros((2, 4, 4), dtype=np.uint8)
    pixels[0, 0] = [255, 255, 255, 255]
    document.active_layer.pixels.write_region(Rect(0, 0, 4, 2), pixels)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(document.size.__class__(2, 2), clear_frames=False)
    assert service.create_sprite_frame(Rect(0, 0, 2, 2))
    assert service.create_sprite_frame(Rect(2, 0, 2, 2))
    first, second = document.sprite_workspace.frames
    assert service.update_sprite_frame(first.id, pivot=PixelPoint(0, 0))
    assert service.update_sprite_frame(second.id, pivot=PixelPoint(1, 0))
    animation = SpriteAnimation(uuid4(), "测试", (first.id, second.id))
    document.sprite_workspace.animations.append(animation)
    result = render_animation_preview(
        DocumentSnapshot.capture(document),
        animation.id,
        1,
        1,
        CancellationToken(),
        lambda _done, _total, _label: None,
        previous_count=1,
        opacity=1.0,
        previous_color=(255, 0, 0, 255),
        pivot_radius=0,
    )
    assert result[0, 0].tolist() == [255, 0, 0, 255]
    assert result[0, 1, 3] == 0


def test_onion_skin_is_drawn_over_opaque_current_frame() -> None:
    document = Document.create(4, 1)
    pixels = np.zeros((1, 4, 4), dtype=np.uint8)
    pixels[0, 0] = [255, 255, 255, 255]
    pixels[0, 2] = [0, 0, 255, 255]
    document.active_layer.pixels.write_region(Rect(0, 0, 4, 1), pixels)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(document.size.__class__(2, 1), clear_frames=False)
    assert service.create_sprite_frame(Rect(0, 0, 2, 1))
    assert service.create_sprite_frame(Rect(2, 0, 2, 1))
    first, second = document.sprite_workspace.frames
    animation = SpriteAnimation(uuid4(), "测试", (first.id, second.id))
    document.sprite_workspace.animations.append(animation)

    result = render_animation_preview(
        DocumentSnapshot.capture(document),
        animation.id,
        1,
        1,
        CancellationToken(),
        lambda _done, _total, _label: None,
        previous_count=1,
        opacity=0.5,
        previous_color=(255, 0, 0, 255),
        pivot_radius=0,
    )

    assert result[0, 0].tolist() == [128, 0, 127, 255]


def test_onion_skin_does_not_tint_unchanged_opaque_background() -> None:
    document = Document.create(6, 1)
    background = [24, 24, 24, 255]
    pixels = np.array(
        [
            [
                [255, 255, 255, 255],
                background,
                background,
                background,
                [0, 0, 255, 255],
                background,
            ]
        ],
        dtype=np.uint8,
    )
    document.active_layer.pixels.write_region(Rect(0, 0, 6, 1), pixels)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.set_sprite_cell_size(document.size.__class__(3, 1), clear_frames=False)
    assert service.create_sprite_frame(Rect(0, 0, 3, 1))
    assert service.create_sprite_frame(Rect(3, 0, 3, 1))
    first, second = document.sprite_workspace.frames
    animation = SpriteAnimation(uuid4(), "测试", (first.id, second.id))
    document.sprite_workspace.animations.append(animation)

    result = render_animation_preview(
        DocumentSnapshot.capture(document),
        animation.id,
        1,
        1,
        CancellationToken(),
        lambda _done, _total, _label: None,
        previous_count=1,
        opacity=0.5,
        previous_color=(255, 0, 0, 255),
        pivot_radius=0,
    )

    assert result[0, 2].tolist() == background


def test_slice_reference_preview_preserves_original_rgba() -> None:
    document = Document.create(2, 1)
    pixels = np.zeros((1, 2, 4), dtype=np.uint8)
    pixels[0, 0] = [12, 180, 90, 200]
    document.active_layer.pixels.write_region(Rect(0, 0, 2, 1), pixels)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.create_sprite_frame(Rect(0, 0, 2, 1))
    frame = document.sprite_workspace.frames[0]

    result = render_frame_reference_preview(
        DocumentSnapshot.capture(document),
        frame.id,
        CancellationToken(),
        lambda _done, _total, _label: None,
        pivot_radius=0,
    )

    assert result[0, 0].tolist() == [12, 180, 90, 200]


def test_animation_and_reference_previews_draw_red_pivot_marker() -> None:
    document = Document.create(5, 5)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.create_sprite_frame(Rect(0, 0, 5, 5))
    frame = document.sprite_workspace.frames[0]
    animation = SpriteAnimation(uuid4(), "测试", (frame.id,))
    document.sprite_workspace.animations.append(animation)
    snapshot = DocumentSnapshot.capture(document)

    animation_pixels = render_animation_preview(
        snapshot,
        animation.id,
        0,
        1,
        CancellationToken(),
        lambda _done, _total, _label: None,
        pivot_radius=1,
    )
    reference_pixels = render_frame_reference_preview(
        snapshot,
        frame.id,
        CancellationToken(),
        lambda _done, _total, _label: None,
        pivot_radius=1,
    )

    assert animation_pixels[2, 2].tolist() == [255, 0, 0, 255]
    assert reference_pixels[2, 2].tolist() == [255, 0, 0, 255]
