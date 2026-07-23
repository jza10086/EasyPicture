from __future__ import annotations

import numpy as np

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document, LayerPixelsChanged, PixelPoint, RasterLayer
from easypicture.pixels import TileCoord


def _tile(color: tuple[int, int, int, int] = (10, 20, 30, 255)) -> np.ndarray:
    pixels = np.empty((256, 256, 4), dtype=np.uint8)
    pixels[:] = color
    return pixels


def test_layer_commands_create_duplicate_move_delete_and_undo() -> None:
    document = Document.create(320, 240)
    service = DocumentCommandService()
    service.attach_document(document)
    original = document.active_layer

    assert service.create_layer()
    created = document.active_layer
    assert [layer.name for layer in document.layers] == ["图层 1", "图层 2"]
    assert document.modified

    assert service.rename_layer(created.id, "  顶层  ")
    assert document.active_layer.name == "顶层"
    assert service.duplicate_layer(created.id)
    duplicate = document.active_layer
    assert duplicate.id != created.id
    assert duplicate.name == "顶层 副本"
    assert service.move_layer(duplicate.id, 0)
    assert document.layers[0] is duplicate
    assert service.delete_layer(created.id)
    assert created not in document.layers

    for _ in range(4):
        service.undo_stack.undo()
    assert document.layers == (original, created)
    assert created.name == "图层 2"

    service.undo_stack.undo()
    assert document.layers == (original,)
    assert document.active_layer is original
    assert not document.modified

    for _ in range(5):
        service.undo_stack.redo()
    assert created not in document.layers
    assert document.modified


def test_last_layer_cannot_be_deleted_and_noops_do_not_create_commands() -> None:
    document = Document.create(10, 10)
    service = DocumentCommandService()
    service.attach_document(document)
    layer = document.active_layer

    assert not service.delete_layer(layer.id)
    assert not service.rename_layer(layer.id, "图层 1")
    assert not service.set_layer_visible(layer.id, True)
    assert not service.move_layer(layer.id, 0)
    assert service.undo_stack.count() == 0
    assert document.revision == 0
    assert not document.modified


def test_duplicate_uses_shared_tiles_then_copy_on_write() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    original = document.active_layer
    assert service.apply_pixel_tiles(original.id, {TileCoord(0, 0): _tile()}, "填充")

    assert service.duplicate_layer(original.id)
    duplicate = document.active_layer
    assert np.shares_memory(
        original.pixels.read_tile(0, 0),
        duplicate.pixels.read_tile(0, 0),
    )

    replacement = _tile((200, 100, 50, 255))
    assert service.apply_pixel_tiles(duplicate.id, {TileCoord(0, 0): replacement}, "修改")
    assert not np.shares_memory(
        original.pixels.read_tile(0, 0),
        duplicate.pixels.read_tile(0, 0),
    )
    assert tuple(original.pixels.read_tile(0, 0)[0, 0]) == (10, 20, 30, 255)


def test_pixel_patch_restores_missing_tiles_and_clean_state() -> None:
    document = Document.create(512, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    layer = document.active_layer
    updates = {
        TileCoord(0, 0): _tile((255, 0, 0, 128)),
        TileCoord(1, 0): _tile((0, 0, 255, 128)),
    }

    assert service.apply_pixel_tiles(layer.id, updates, "测试像素")
    assert layer.pixels.allocated_tile_count == 2
    assert service.patch_store.allocated_bytes > 0
    assert document.modified

    service.undo_stack.undo()
    assert layer.pixels.allocated_tile_count == 0
    assert not document.modified

    service.undo_stack.redo()
    assert layer.pixels.allocated_tile_count == 2
    assert document.modified


def test_layer_lock_and_alpha_lock_are_enforced() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    layer = document.active_layer
    original = np.zeros((256, 256, 4), dtype=np.uint8)
    original[0, 0] = (50, 60, 70, 128)
    original[0, 1] = (7, 8, 9, 0)
    assert service.apply_pixel_tiles(layer.id, {TileCoord(0, 0): original}, "初始像素")

    assert service.set_layer_alpha_locked(layer.id, True)
    proposed = _tile((0, 255, 0, 255))
    assert service.apply_pixel_tiles(layer.id, {TileCoord(0, 0): proposed}, "透明锁测试")
    result = layer.pixels.read_tile(0, 0)
    assert tuple(result[0, 0]) == (0, 255, 0, 128)
    assert tuple(result[0, 1]) == (7, 8, 9, 0)

    assert service.set_layer_locked(layer.id, True)
    assert not service.apply_pixel_tiles(
        layer.id,
        {TileCoord(0, 0): _tile((1, 2, 3, 4))},
        "锁定测试",
    )


def test_pixel_command_rejects_non_contiguous_input_without_change() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    non_contiguous = np.zeros((256, 512, 4), dtype=np.uint8)[:, ::2]
    assert not non_contiguous.flags.c_contiguous

    assert not service.apply_pixel_tiles(
        document.active_layer_id,
        {TileCoord(0, 0): non_contiguous},
        "非法像素",
    )
    assert service.undo_stack.count() == 0
    assert not document.modified


def test_pixel_event_maps_non_aligned_layer_tile_to_document_tiles() -> None:
    document = Document.create(600, 600)
    layer = RasterLayer.create_blank(
        "偏移层",
        document.size,
        origin=PixelPoint(10, 20),
    )
    document.insert_layer(layer, 1)
    document.mark_clean()
    events: list[object] = []
    document.subscribe(events.append)
    service = DocumentCommandService()
    service.attach_document(document)

    assert service.apply_pixel_tiles(layer.id, {TileCoord(0, 0): _tile()}, "偏移写入")

    event = next(event for event in events if isinstance(event, LayerPixelsChanged))
    assert event.layer_tiles == frozenset({TileCoord(0, 0)})
    assert event.document_tiles == frozenset(
        {
            TileCoord(0, 0),
            TileCoord(1, 0),
            TileCoord(0, 1),
            TileCoord(1, 1),
        }
    )


def test_undo_budget_cancel_preserves_history_and_document() -> None:
    rng = np.random.default_rng(123)
    first = rng.integers(0, 256, (256, 256, 4), dtype=np.uint8)
    second = rng.integers(0, 256, (256, 256, 4), dtype=np.uint8)
    confirmations: list[tuple[int, int]] = []
    service = DocumentCommandService(
        undo_budget_bytes=400_000,
        confirm_history_reset=lambda required, budget: (
            confirmations.append((required, budget)) or False
        ),
    )
    document = Document.create(512, 256)
    service.attach_document(document)
    layer = document.active_layer
    assert service.apply_pixel_tiles(layer.id, {TileCoord(0, 0): first}, "第一块")
    revision = document.revision

    assert not service.apply_pixel_tiles(layer.id, {TileCoord(1, 0): second}, "第二块")
    assert confirmations
    assert service.undo_stack.count() == 1
    assert document.revision == revision
    assert not layer.pixels.has_tile(1, 0)


def test_undo_budget_confirm_resets_history_and_invalidates_clean_index() -> None:
    rng = np.random.default_rng(456)
    first = rng.integers(0, 256, (256, 256, 4), dtype=np.uint8)
    second = rng.integers(0, 256, (256, 256, 4), dtype=np.uint8)
    service = DocumentCommandService(
        undo_budget_bytes=400_000,
        confirm_history_reset=lambda _required, _budget: True,
    )
    document = Document.create(512, 256)
    service.attach_document(document)
    layer = document.active_layer
    assert service.apply_pixel_tiles(layer.id, {TileCoord(0, 0): first}, "第一块")

    assert service.apply_pixel_tiles(layer.id, {TileCoord(1, 0): second}, "第二块")
    assert service.undo_stack.count() == 1
    assert service.undo_stack.cleanIndex() == -1
    assert service.history_bytes <= service.undo_budget_bytes
    assert service.patch_store.allocated_bytes <= service.undo_budget_bytes
    service.undo_stack.undo()
    assert document.modified
    assert layer.pixels.has_tile(0, 0)
    assert not layer.pixels.has_tile(1, 0)


def test_single_command_over_budget_is_rejected() -> None:
    reported: list[tuple[int, int]] = []
    service = DocumentCommandService(
        undo_budget_bytes=100,
        report_command_too_large=lambda required, budget: reported.append((required, budget)),
    )
    document = Document.create(256, 256)
    service.attach_document(document)

    assert not service.apply_pixel_tiles(
        document.active_layer_id,
        {TileCoord(0, 0): _tile()},
        "超大命令",
    )
    assert reported
    assert service.undo_stack.count() == 0
    assert service.patch_store.allocated_bytes == 0
    assert not document.modified


def test_deleted_layer_retained_pixels_count_toward_budget() -> None:
    reported: list[tuple[int, int]] = []
    document = Document.create(256, 256)
    retained = RasterLayer.create_blank("大图层", document.size)
    retained.pixels.replace_tile(0, 0, _tile())
    document.insert_layer(retained, 1)
    document.mark_clean()
    service = DocumentCommandService(
        undo_budget_bytes=100,
        report_command_too_large=lambda required, budget: reported.append((required, budget)),
    )
    service.attach_document(document)

    assert not service.delete_layer(retained.id)
    assert retained in document.layers
    assert reported == [(retained.pixels.allocated_bytes, 100)]


def test_one_hundred_property_commands_undo_and_redo_consistently() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    layer = document.active_layer

    for index in range(100):
        assert service.set_layer_visible(layer.id, index % 2 == 1)
    assert service.undo_stack.count() == 100
    assert layer.visible

    for _ in range(100):
        service.undo_stack.undo()
    assert layer.visible
    assert not document.modified

    for _ in range(100):
        service.undo_stack.redo()
    assert layer.visible
    assert document.modified


def test_patch_handles_are_released_when_undo_limit_discards_oldest() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService(undo_budget_bytes=10 * 1024 * 1024)
    service.attach_document(document)
    layer = document.active_layer

    for index in range(101):
        assert service.apply_pixel_tiles(
            layer.id,
            {TileCoord(0, 0): _tile((index, 20, 30, 255))},
            f"像素 {index}",
        )

    assert service.undo_stack.count() == 100
    assert service.patch_store.handle_count == 200
    assert service.history_bytes <= service.undo_budget_bytes


def test_visibility_scrub_is_one_undoable_macro() -> None:
    document = Document.create(256, 256)
    service = DocumentCommandService()
    service.attach_document(document)
    assert service.create_layer()
    assert service.create_layer()
    layer_ids = [layer.id for layer in document.layers]
    baseline_count = service.undo_stack.count()

    assert service.begin_visibility_scrub()
    for layer_id in layer_ids:
        assert service.set_layer_visible(layer_id, False)
    service.end_visibility_scrub()

    assert service.undo_stack.count() == baseline_count + 1
    assert service.undo_stack.undoText() == "批量切换图层可见性"
    assert not any(layer.visible for layer in document.layers)

    service.undo_stack.undo()
    assert all(layer.visible for layer in document.layers)
