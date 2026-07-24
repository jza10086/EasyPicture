from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QGraphicsRectItem

from easypicture.domain import Document, PixelPoint, Point, RasterLayer, Size
from easypicture.tools import NullTool, ToolContext, ToolController
from easypicture.ui.canvas_view import CanvasView
from easypicture.ui.overlays import OverlayManager


def test_canvas_coordinate_mapping_survives_zoom_and_pan(qtbot) -> None:
    view = CanvasView()
    qtbot.addWidget(view)
    view.resize(800, 600)
    view.show()
    document = Document.create(400, 300)
    view.set_document(document)
    view.set_zoom(2.0)
    view.centerOn(QPointF(150.0, 120.0))
    qtbot.wait(1)

    viewport_point = view.mapFromScene(QPointF(75.0, 80.0))
    mapped = view.viewport_to_document(viewport_point)

    assert abs(mapped.x - 75.0) <= 1.0
    assert abs(mapped.y - 80.0) <= 1.0

    viewport_point = QPoint(130, 90)
    global_point = view.viewport().mapToGlobal(viewport_point)
    assert view.global_to_viewport(global_point) == viewport_point


def test_zoom_keeps_viewport_anchor_stable(qtbot) -> None:
    view = CanvasView()
    qtbot.addWidget(view)
    view.resize(640, 480)
    view.show()
    view.set_document(Document.create(1200, 900))
    anchor = QPoint(170, 140)
    before = view.mapToScene(anchor)

    view.set_zoom(2.0, anchor)
    after = view.mapToScene(anchor)

    assert abs(before.x() - after.x()) <= 0.5
    assert abs(before.y() - after.y()) <= 0.5


def test_fit_document_recenters_after_scrollbar_layout_change(qtbot) -> None:
    view = CanvasView()
    qtbot.addWidget(view)
    view.resize(800, 600)
    view.show()
    view.set_document(Document.create(1920, 1080))
    view.set_zoom(2.0)
    qtbot.waitUntil(view.verticalScrollBar().isVisible)

    view.fit_document()
    qtbot.waitUntil(lambda: not view.verticalScrollBar().isVisible())
    qtbot.wait(1)

    viewport_center = view.viewport().rect().center()
    document_center = view.mapFromScene(QPointF(960.0, 540.0))
    assert abs(document_center.x() - viewport_center.x()) <= 2

    view.fit_document()
    view.set_zoom(1.5)
    qtbot.wait(1)
    assert view.zoom == 1.5


def test_middle_mouse_pan_moves_scrollbars_without_editing(qtbot) -> None:
    view = CanvasView()
    qtbot.addWidget(view)
    view.resize(320, 240)
    view.show()
    document = Document.create(1600, 1200)
    view.set_document(document)
    view.centerOn(800.0, 600.0)
    before = (
        view.horizontalScrollBar().value(),
        view.verticalScrollBar().value(),
    )

    qtbot.mousePress(
        view.viewport(),
        Qt.MouseButton.MiddleButton,
        pos=QPoint(150, 120),
    )
    qtbot.mouseMove(view.viewport(), QPoint(190, 150))
    qtbot.mouseRelease(
        view.viewport(),
        Qt.MouseButton.MiddleButton,
        pos=QPoint(190, 150),
    )

    after = (
        view.horizontalScrollBar().value(),
        view.verticalScrollBar().value(),
    )
    assert after != before
    assert document.revision == 0
    assert document.active_layer.pixels.revision == 0


def test_document_to_layer_subtracts_nonzero_origin() -> None:
    layer = RasterLayer.create_blank(
        "偏移层",
        Size(100, 100),
        origin=PixelPoint(-20, 35),
    )

    point = CanvasView.document_to_layer(Point(30.5, 80.0), layer)

    assert point == Point(50.5, 45.0)


def test_view_navigation_does_not_change_pixels_or_revisions(qtbot) -> None:
    view = CanvasView()
    qtbot.addWidget(view)
    document = Document.create(400, 300)
    view.set_document(document)
    layer_revision = document.active_layer.pixels.revision
    document_revision = document.revision

    view.zoom_in()
    view.zoom_out()
    view.actual_size()
    view.fit_document()
    view.centerOn(200.0, 150.0)

    assert document.active_layer.pixels.revision == layer_revision
    assert document.revision == document_revision
    assert not document.modified


def test_zoom_is_clamped(qtbot) -> None:
    view = CanvasView()
    qtbot.addWidget(view)

    view.set_zoom(0.0001)
    assert view.zoom == CanvasView.MIN_ZOOM
    view.set_zoom(1000.0)
    assert view.zoom == CanvasView.MAX_ZOOM


class RecordingTool(NullTool):
    def __init__(self, name: str, log: list[str]) -> None:
        super().__init__()
        self.name = name
        self.log = log

    def activate(self, context: ToolContext) -> None:
        super().activate(context)
        self.log.append(f"{self.name}:activate")

    def deactivate(self) -> None:
        self.log.append(f"{self.name}:deactivate")
        super().deactivate()

    def mouse_press(self, event: QMouseEvent) -> bool:
        _ = event
        self.log.append(f"{self.name}:press")
        return True

    def key_press(self, event: QKeyEvent) -> bool:
        _ = event
        self.log.append(f"{self.name}:key")
        return True


def test_tool_switch_deactivates_clears_overlay_and_resets_cursor(qtbot) -> None:
    view = CanvasView()
    qtbot.addWidget(view)
    overlays = OverlayManager(view.scene())
    context = ToolContext(view, overlays, lambda: None)
    log: list[str] = []
    first = RecordingTool("first", log)
    second = RecordingTool("second", log)
    controller = ToolController(context, first)
    overlays.add(QGraphicsRectItem(0, 0, 10, 10))
    view.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)

    controller.set_tool(second)

    assert log == ["first:activate", "first:deactivate", "second:activate"]
    assert overlays.count == 0
    assert view.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor


def test_middle_pan_restores_hidden_brush_cursor(qtbot) -> None:
    view = CanvasView()
    qtbot.addWidget(view)
    view.resize(320, 240)
    view.show()
    view.set_document(Document.create(800, 600))
    view.set_tool_cursor(Qt.CursorShape.BlankCursor)

    qtbot.mousePress(
        view.viewport(),
        Qt.MouseButton.MiddleButton,
        pos=QPoint(120, 100),
    )
    assert view.viewport().cursor().shape() == Qt.CursorShape.ClosedHandCursor
    qtbot.mouseRelease(
        view.viewport(),
        Qt.MouseButton.MiddleButton,
        pos=QPoint(120, 100),
    )

    assert view.viewport().cursor().shape() == Qt.CursorShape.BlankCursor


def test_space_pan_restores_hidden_brush_cursor(qtbot) -> None:
    view = CanvasView()
    qtbot.addWidget(view)
    view.show()
    view.set_document(Document.create(200, 200))
    view.set_tool_cursor(Qt.CursorShape.BlankCursor)

    qtbot.keyPress(view, Qt.Key.Key_Space)
    assert view.viewport().cursor().shape() == Qt.CursorShape.OpenHandCursor
    qtbot.keyRelease(view, Qt.Key.Key_Space)

    assert view.viewport().cursor().shape() == Qt.CursorShape.BlankCursor


def test_viewport_brush_cursor_clears_previous_fast_position(qtbot) -> None:
    view = CanvasView()
    qtbot.addWidget(view)
    view.resize(320, 240)
    view.show()
    view.set_document(Document.create(256, 256))
    qtbot.wait(1)

    baseline = view.viewport().grab().toImage().convertToFormat(
        QImage.Format.Format_RGBA8888
    )
    view.update_brush_cursor(QPointF(80.0, 100.0), 24.0, visible=True)
    first = view.viewport().grab().toImage().convertToFormat(
        QImage.Format.Format_RGBA8888
    )
    for x in range(100, 221, 10):
        view.update_brush_cursor(QPointF(float(x), 100.0), 24.0, visible=True)
    second = view.viewport().grab().toImage().convertToFormat(
        QImage.Format.Format_RGBA8888
    )

    def patch(image, center_x: int) -> bytes:
        pixels = image.copy(center_x - 16, 84, 32, 32)
        return bytes(pixels.bits())

    assert patch(first, 80) != patch(baseline, 80)
    assert patch(second, 80) == patch(baseline, 80)
    assert patch(second, 220) != patch(baseline, 220)
