from __future__ import annotations

import numpy as np
from PySide6.QtGui import QImage

from easypicture.commands import DocumentCommandService
from easypicture.domain import Document
from easypicture.pixels import TileCoord
from easypicture.tools import PixelEditSession
from easypicture.ui.canvas_view import CanvasView


def _pixel(image: QImage, x: int, y: int) -> tuple[int, int, int, int]:
    straight = image.convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = np.frombuffer(
        straight.bits(),
        dtype=np.uint8,
        count=straight.sizeInBytes(),
    ).reshape(straight.height(), straight.width(), 4)
    return tuple(int(value) for value in pixels[y, x])  # type: ignore[return-value]


def test_session_preview_does_not_mutate_document_until_commit(qtbot) -> None:
    document = Document.create(256, 256)
    commands = DocumentCommandService()
    commands.attach_document(document)
    view = CanvasView()
    qtbot.addWidget(view)
    view.set_document(document)
    session = PixelEditSession(document, document.active_layer_id, commands, view)
    tile = session.writable_tile(TileCoord(0, 0))
    tile[10, 20] = (255, 0, 0, 255)

    session.publish(frozenset({TileCoord(0, 0)}))

    assert document.active_layer.pixels.read_tile(0, 0)[10, 20, 3] == 0
    assert document.revision == 0
    assert not document.modified
    assert view.renderer is not None
    assert _pixel(view.renderer.render_tile(TileCoord(0, 0)), 20, 10) == (255, 0, 0, 255)

    assert session.commit("画笔笔划")
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[10, 20]) == (255, 0, 0, 255)
    assert document.modified
    assert commands.undo_stack.count() == 1


def test_session_cancel_removes_preview_and_keeps_document_clean(qtbot) -> None:
    document = Document.create(256, 256)
    commands = DocumentCommandService()
    commands.attach_document(document)
    view = CanvasView()
    qtbot.addWidget(view)
    view.set_document(document)
    session = PixelEditSession(document, document.active_layer_id, commands, view)
    session.writable_tile(TileCoord(0, 0))[0, 0] = (1, 2, 3, 255)
    session.publish(frozenset({TileCoord(0, 0)}))

    session.cancel()

    assert view.renderer is not None
    assert view.renderer.preview_tile_count == 0
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[0, 0]) == (0, 0, 0, 0)
    assert not document.modified


def test_session_rejects_commit_after_revision_changes(qtbot) -> None:
    document = Document.create(256, 256)
    commands = DocumentCommandService()
    commands.attach_document(document)
    view = CanvasView()
    qtbot.addWidget(view)
    view.set_document(document)
    session = PixelEditSession(document, document.active_layer_id, commands, view)
    session.writable_tile(TileCoord(0, 0))[0, 0] = (255, 0, 0, 255)
    session.publish(frozenset({TileCoord(0, 0)}))
    document.record_change()

    assert not session.commit("过期笔划")
    assert tuple(document.active_layer.pixels.read_tile(0, 0)[0, 0]) == (0, 0, 0, 0)
    assert commands.undo_stack.count() == 0


def test_preview_is_composited_at_active_layer_position(qtbot) -> None:
    document = Document.create(256, 256)
    commands = DocumentCommandService()
    commands.attach_document(document)
    bottom = document.active_layer
    assert commands.create_layer()
    top = document.active_layer
    opaque_blue = np.zeros((256, 256, 4), dtype=np.uint8)
    opaque_blue[:] = (0, 0, 255, 255)
    assert commands.apply_pixel_tiles(top.id, {TileCoord(0, 0): opaque_blue}, "顶层")
    document.set_active_layer(bottom.id)
    view = CanvasView()
    qtbot.addWidget(view)
    view.set_document(document)
    session = PixelEditSession(document, bottom.id, commands, view)
    session.writable_tile(TileCoord(0, 0))[0, 0] = (255, 0, 0, 255)
    session.publish(frozenset({TileCoord(0, 0)}))

    assert view.renderer is not None
    assert _pixel(view.renderer.render_tile(TileCoord(0, 0)), 0, 0) == (0, 0, 255, 255)

    session.cancel()
