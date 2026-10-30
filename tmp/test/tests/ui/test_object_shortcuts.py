from math import hypot, isclose

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QTransform
from PySide6.QtWidgets import QApplication, QLineEdit

from easypicture.domain import (
    AffineTransform,
    FloatRect,
    Point,
    RasterObject,
    Size,
    SvgPrimitiveKind,
    SvgPrimitiveStyle,
    SvgRectGeometry,
    TextObject,
    create_svg_primitive,
)
from easypicture.imaging import ShapeGeometry, ShapeKind
from easypicture.render.shape_path import object_selection_path, shape_path
from easypicture.tools.object_hit_test import opaque_in_rect
from easypicture.ui.property_binding import interaction_coordinator
from tests.ui.test_property_transactions import _drag
from tests.ui.test_raster_editor import _window


def _key(widget, key, modifiers=Qt.KeyboardModifier.NoModifier):
    for kind in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        QApplication.sendEvent(widget, QKeyEvent(kind, key, modifiers))


def _mouse(
    window, kind, point, button=Qt.MouseButton.NoButton, modifiers=Qt.KeyboardModifier.NoModifier
):
    canvas = window.canvas_view
    local = QPointF(canvas.mapFromScene(QPointF(*point)))
    event = QMouseEvent(
        kind,
        local,
        QPointF(canvas.viewport().mapToGlobal(local.toPoint())),
        button,
        button,
        modifiers,
    )
    QApplication.sendEvent(canvas.viewport(), event)


def _pointer(window, backend, point):
    canvas = window.canvas_view
    backend.location = canvas.viewport().mapToGlobal(canvas.mapFromScene(QPointF(*point)))


@pytest.mark.parametrize(
    ("key", "end", "expected"),
    [
        (Qt.Key.Key_G, (52, 40), AffineTransform.translation(4, 8)),
        (Qt.Key.Key_S, (64, 32), AffineTransform.scale(2, 2)),
        (Qt.Key.Key_R, (32, 48), AffineTransform.rotation(90)),
    ],
)
def test_transform_shortcuts_preview_commit_once_and_undo(
    qtbot, tmp_path, virtual_pointer, key, end, expected
):
    window, controller = _window(qtbot, tmp_path)
    document = controller.document
    node = document.active_layer
    window.layer_panel.select_node(node)
    window.canvas_view.set_zoom(1)
    _pointer(window, virtual_pointer, (48, 32))
    stack = controller.commands.undo_stack.activeStack()
    before = stack.count()
    _key(window.canvas_view, key)
    assert interaction_coordinator(window).owner is window._tools["object"]
    for _ in range(3):
        _mouse(window, QEvent.Type.MouseMove, end)
        assert document.node_by_id(node.id).transform.is_identity
        assert stack.count() == before
    _key(window.canvas_view, Qt.Key.Key_Return)
    assert stack.count() == before + 1
    if key != Qt.Key.Key_G:
        expected = (
            AffineTransform.translation(-32, -32)
            .then(expected)
            .then(AffineTransform.translation(32, 32))
        )
    assert document.node_by_id(node.id).transform.almost_equals(expected)
    assert interaction_coordinator(window).owner is None
    stack.undo()
    assert document.node_by_id(node.id).transform.is_identity


@pytest.mark.parametrize("cancel", ["escape", "right", "deactivate", "delete"])
def test_transform_cancel_and_delete_cannot_steal_gesture(qtbot, tmp_path, virtual_pointer, cancel):
    window, controller = _window(qtbot, tmp_path)
    node = controller.document.active_layer
    window.layer_panel.select_node(node)
    _pointer(window, virtual_pointer, (48, 32))
    before = controller.commands.undo_stack.activeStack().count()
    _key(window.canvas_view, Qt.Key.Key_G)
    _mouse(window, QEvent.Type.MouseMove, (54, 36))
    if cancel == "right":
        _mouse(window, QEvent.Type.MouseButtonPress, (54, 36), Qt.MouseButton.RightButton)
        _mouse(window, QEvent.Type.MouseButtonRelease, (54, 36), Qt.MouseButton.RightButton)
    elif cancel == "deactivate":
        QApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
    else:
        if cancel == "delete":
            _key(window.canvas_view, Qt.Key.Key_Delete)
            assert interaction_coordinator(window).owner is window._tools["object"]
        _key(window.canvas_view, Qt.Key.Key_Escape)
    assert controller.document.node_by_id(node.id).transform.is_identity
    assert controller.commands.undo_stack.activeStack().count() == before
    assert interaction_coordinator(window).owner is None


def test_axis_precision_click_confirm_and_delete_with_undo(qtbot, tmp_path, virtual_pointer):
    window, controller = _window(qtbot, tmp_path)
    node = controller.document.active_layer
    window.layer_panel.select_node(node)
    window.canvas_view.set_zoom(1)
    _pointer(window, virtual_pointer, (40, 32))
    _key(window.canvas_view, Qt.Key.Key_G)
    _key(window.canvas_view, Qt.Key.Key_X)
    _mouse(window, QEvent.Type.MouseMove, (60, 52), modifiers=Qt.KeyboardModifier.ShiftModifier)
    _mouse(
        window,
        QEvent.Type.MouseButtonPress,
        (60, 52),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
    )
    _mouse(window, QEvent.Type.MouseButtonRelease, (60, 52), Qt.MouseButton.LeftButton)
    matrix = controller.document.node_by_id(node.id).transform
    assert isclose(matrix.tx, 2) and matrix.ty == 0
    _key(window.layer_panel.tree_view, Qt.Key.Key_Delete)
    with pytest.raises(ValueError):
        controller.document.node_by_id(node.id)
    controller.commands.undo_stack.undo()
    assert controller.document.node_by_id(node.id).transform == matrix


def test_shortcuts_do_not_intercept_text_or_pixel_bucket_and_locked_batch(
    qtbot, tmp_path, virtual_pointer
):
    window, controller = _window(qtbot, tmp_path)
    node = controller.document.active_layer
    window.layer_panel.select_node(node)
    editor = QLineEdit("gsr", window)
    editor.show()
    editor.selectAll()
    _key(editor, Qt.Key.Key_Delete)
    assert editor.text() == ""
    assert controller.document.node_by_id(node.id)
    controller.commands.update_object_properties((node.id,), {"locked": True})
    _key(window.canvas_view, Qt.Key.Key_G)
    _key(window.canvas_view, Qt.Key.Key_Delete)
    assert interaction_coordinator(window).owner is None
    assert controller.document.node_by_id(node.id)
    controller.commands.update_object_properties((node.id,), {"locked": False})
    window._open_raster_editor()
    assert not window.create_object_toolbar_action.isVisible()
    window.tool_actions["bucket"].trigger()
    assert window.tool_controller.active_tool is window._tools["bucket"]
    event = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_G, Qt.KeyboardModifier.NoModifier)
    assert not window.eventFilter(window.canvas_view, event)


def test_create_menu_unifies_tools_and_shift_a_opens_same_menu(qtbot, tmp_path):
    window, _ = _window(qtbot, tmp_path)
    toolbar_actions = window.tools_toolbar.actions()
    for name in ("shape", "text", "raster_object"):
        assert window.tool_actions[name] not in toolbar_actions
    _key(window.canvas_view, Qt.Key.Key_A, Qt.KeyboardModifier.ShiftModifier)
    assert window.create_object_menu.isVisible()
    vector = window.create_vector_menu
    assert [action.text() for action in vector.actions()[:6]] == [
        "直线",
        "矩形",
        "圆角矩形",
        "椭圆",
        "折线",
        "多边形",
    ]
    window.create_object_menu.close()
    vector.actions()[3].trigger()
    assert window.tool_controller.active_tool is window._tools["shape"]
    assert window.tool_options_panel.shape.kind() == ShapeKind.ELLIPSE
    raster = window.create_raster_menu
    raster.actions()[0].trigger()
    assert window.tool_controller.active_tool is window._tools["raster_object"]


def test_box_selection_ignores_transparency_including_transformed_pixel_cells(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    document = controller.document
    empty = document.active_layer
    raster = RasterObject.create_blank("pixel", Size(16, 16))
    tile = np.zeros((256, 256, 4), np.uint8)
    tile[8, 8] = (255, 0, 0, 255)
    raster.pixels.replace_tile(0, 0, tile)
    raster.transform = (
        AffineTransform.scale(2, 3)
        .then(AffineTransform.rotation(30))
        .then(AffineTransform.translation(20, 5))
    )
    center = raster.world_transform.map_point(Point(8.5, 8.5))
    assert opaque_in_rect(raster, FloatRect(center.x - 0.1, center.y - 0.1, 0.2, 0.2))
    assert not opaque_in_rect(raster, FloatRect(0, 0, 4, 4))
    controller.commands.create_object(raster)
    window._activate_tool("object")
    window.tool_options_panel.ignore_transparent.setChecked(True)
    _mouse(window, QEvent.Type.MouseButtonPress, (-10, -10), Qt.MouseButton.LeftButton)
    _mouse(window, QEvent.Type.MouseMove, (70, 70))
    _mouse(window, QEvent.Type.MouseButtonRelease, (70, 70), Qt.MouseButton.LeftButton)
    assert window.layer_panel.selection.state.selected_ids == (raster.id,)
    window.tool_options_panel.ignore_transparent.setChecked(False)
    _mouse(window, QEvent.Type.MouseButtonPress, (-10, -10), Qt.MouseButton.LeftButton)
    _mouse(window, QEvent.Type.MouseMove, (70, 70))
    _mouse(window, QEvent.Type.MouseButtonRelease, (70, 70), Qt.MouseButton.LeftButton)
    assert set(window.layer_panel.selection.state.selected_ids) == {raster.id, empty.id}


def test_vector_box_coverage_and_creation_outline_match_after_transform(qapp):
    node = create_svg_primitive(
        SvgPrimitiveKind.RECT,
        SvgRectGeometry(4, 6, 24, 20, 3, 3),
        SvgPrimitiveStyle(fill=None, stroke=(255, 0, 0, 255), stroke_width=4),
        Size(64, 64),
    )
    assert not opaque_in_rect(node, FloatRect(10, 12, 2, 2))
    assert opaque_in_rect(node, FloatRect(3, 12, 2, 2))
    assert not opaque_in_rect(
        TextObject.create(FloatRect(0, 0, 100, 100), text=" "), FloatRect(10, 10, 20, 20)
    )
    creation = shape_path(ShapeGeometry(ShapeKind.ROUNDED_RECTANGLE, ((4, 6), (28, 26)), 3))
    assert object_selection_path(node) == creation
    assert creation.boundingRect() == QRectF(4, 6, 24, 20)
    node.transform = AffineTransform.scale(2, 3).then(AffineTransform.rotation(40))
    assert object_selection_path(node) == QTransform(*node.transform.values).map(creation)


def test_object_fields_allow_manual_values_beyond_soft_range_and_cancel_drag(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    node = controller.document.active_layer
    window.layer_panel.select_node(node)
    field = window.object_properties.fields["x"]
    assert (field.drag_minimum, field.drag_maximum) == (-64, 64)
    _key(field._field, Qt.Key.Key_Return)
    field._field._editor.setText("1000")
    _key(field._field._editor, Qt.Key.Key_Return)
    assert controller.document.node_by_id(node.id).transform.tx == 968
    before = controller.commands.undo_stack.activeStack().count()
    core, _, _ = _drag(field, (4, 10))
    _key(core, Qt.Key.Key_Escape)
    assert controller.commands.undo_stack.activeStack().count() == before
    assert controller.document.node_by_id(node.id).transform.tx == 968


@pytest.mark.parametrize("kind", ["raster", "svg", "text"])
def test_shared_object_center_dimensions_and_local_offset(qtbot, tmp_path, kind):
    from math import hypot

    from easypicture.domain.object_operations import (
        object_center_offset,
        object_local_bounds,
        object_pivot,
    )

    window, controller = _window(qtbot, tmp_path)
    document = controller.document
    if kind == "svg":
        node = create_svg_primitive(
            SvgPrimitiveKind.RECT,
            SvgRectGeometry(10, 20, 40, 20),
            SvgPrimitiveStyle(fill=(255, 0, 0, 255), stroke=(0, 0, 0, 255), stroke_width=10),
            document.size,
        )
        controller.commands.create_object(node)
    elif kind == "text":
        node = TextObject.create(FloatRect(10, 20, 40, 20))
        controller.commands.create_object(node)
    else:
        node = document.active_layer
    window.layer_panel.select_node(document.node_by_id(node.id))
    block = window.object_properties
    fields = block.fields
    bounds = object_local_bounds(document.node_by_id(node.id))
    assert fields["width"].value() == bounds.width  # excludes the SVG stroke
    assert fields["height"].value() == bounds.height
    original = document.node_by_id(node.id).transform
    original_center = object_pivot(document.node_by_id(node.id))
    stack = controller.commands.undo_stack.activeStack()
    before = stack.count()
    field = fields["pivot_x"]
    field.interactionStarted.emit()
    field.setValue(5)
    assert document.node_by_id(node.id).transform_pivot is None
    assert window.canvas_view.render_coordinator.selection_centers((node.id,))[0] == Point(
        original_center.x + 5, original_center.y
    )
    field.editingFinished.emit()
    assert stack.count() == before + 1
    live = document.node_by_id(node.id)
    assert live.transform == original  # moving the origin does not move the content
    assert object_center_offset(live) == Point(5, 0)
    pivot = object_pivot(live)
    field = fields["rotation"]
    field.interactionStarted.emit()
    field.setValue(90)
    field.editingFinished.emit()
    live = document.node_by_id(node.id)
    assert object_pivot(live) == pivot
    assert isclose(hypot(live.transform.a, live.transform.b), 1)
    assert isclose(block.transform_pivot().x, pivot.x)
    controller.commands.transform_objects((node.id,), AffineTransform.translation(10, -5))
    moved = object_pivot(document.node_by_id(node.id))
    assert moved == Point(pivot.x + 10, pivot.y - 5)
    assert object_center_offset(document.node_by_id(node.id)).x == pytest.approx(5)
    stack.undo()
    assert object_pivot(document.node_by_id(node.id)) == pivot
    stack.undo()
    assert document.node_by_id(node.id).transform.almost_equals(original)
    stack.undo()
    assert document.node_by_id(node.id).transform_pivot is None


def test_local_global_axis_cycle_and_single_commit(qtbot, tmp_path, virtual_pointer):
    from easypicture.domain.object_operations import object_pivot

    window, controller = _window(qtbot, tmp_path)
    node = controller.document.active_layer
    matrix = (
        AffineTransform.translation(-32, -32)
        .then(AffineTransform.rotation(90))
        .then(AffineTransform.translation(32, 32))
    )
    controller.commands.update_object_properties((node.id,), {"transform": matrix})
    window.layer_panel.select_node(controller.document.node_by_id(node.id))
    window.canvas_view.set_zoom(1)
    _pointer(window, virtual_pointer, (48, 32))
    stack = controller.commands.undo_stack.activeStack()
    count = stack.count()
    _key(window.canvas_view, Qt.Key.Key_G)
    _key(window.canvas_view, Qt.Key.Key_X)
    _mouse(window, QEvent.Type.MouseMove, (58, 52))
    render = window.canvas_view.render_coordinator
    assert object_pivot(render.selection_nodes((node.id,))[0]).x == pytest.approx(42)
    assert object_pivot(render.selection_nodes((node.id,))[0]).y == pytest.approx(32)
    _key(window.canvas_view, Qt.Key.Key_X)  # local X is global Y for this object
    assert object_pivot(render.selection_nodes((node.id,))[0]).x == pytest.approx(32)
    assert object_pivot(render.selection_nodes((node.id,))[0]).y == pytest.approx(52)
    _key(window.canvas_view, Qt.Key.Key_X)  # no axis constraint
    assert object_pivot(render.selection_nodes((node.id,))[0]).x == pytest.approx(42)
    _key(window.canvas_view, Qt.Key.Key_Escape)
    assert stack.count() == count
    assert window.canvas_view._transform_axes is None
    window.transform_orientation.setCurrentIndex(1)
    _pointer(window, virtual_pointer, (48, 32))
    _key(window.canvas_view, Qt.Key.Key_S)
    _key(window.canvas_view, Qt.Key.Key_X)
    _mouse(window, QEvent.Type.MouseMove, (64, 32))
    _key(window.canvas_view, Qt.Key.Key_Return)
    assert stack.count() == count + 1
    live = controller.document.node_by_id(node.id)
    assert hypot(live.transform.a, live.transform.b) == pytest.approx(2)
    assert hypot(live.transform.c, live.transform.d) == pytest.approx(1)
    assert object_pivot(live).x == pytest.approx(32)
    stack.undo()
    assert controller.document.node_by_id(node.id).transform.almost_equals(matrix)


def test_canvas_ranges_change_with_document_and_preserve_manual_entry(qtbot, tmp_path):
    from easypicture.domain.object_operations import object_pivot
    from easypicture.ui.dialogs import NewDocumentSpec

    window, controller = _window(qtbot, tmp_path)
    controller.dialogs.next_document = NewDocumentSpec(1920, 1080)
    assert controller.request_new_document(window)
    node = controller.document.active_layer
    window.layer_panel.select_node(node)
    fields = window.object_properties.fields
    assert (fields["x"].drag_minimum, fields["x"].drag_maximum) == (-1920, 1920)
    assert (fields["y"].drag_minimum, fields["y"].drag_maximum) == (-1080, 1080)
    assert fields["width"].drag_maximum == 1920
    field = fields["x"]
    _key(field._field, Qt.Key.Key_Return)
    field._field._editor.setText("3000")
    _key(field._field._editor, Qt.Key.Key_Return)
    assert object_pivot(controller.document.node_by_id(node.id)).x == 3000
    controller.dialogs.next_document = NewDocumentSpec(320, 200)
    assert controller.request_new_document(window)
    assert fields["x"].drag_maximum == 320
    assert window.shape_properties.vertex_y.drag_maximum == 200
    assert window.tool_options_panel.crop.x_spin.drag_minimum == -320


@pytest.mark.parametrize("zoom", [1, 3])
def test_center_ring_uses_screen_pixels_and_highlight_style(qtbot, tmp_path, zoom):
    from PySide6.QtGui import QColor, QImage

    window, controller = _window(qtbot, tmp_path)
    node = controller.document.active_layer
    window.layer_panel.select_node(node)
    canvas = window.canvas_view
    canvas.set_highlight_outline_style(QColor("#ff00ff"), 3)
    canvas.set_center_marker_style(QColor("#00ff00"), 7.5)
    canvas.set_zoom(zoom)
    qtbot.wait(20)
    image = canvas.viewport().grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    data = (
        np.frombuffer(image.constBits(), np.uint8)
        .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
        .reshape(image.height(), image.width(), 4)
    )
    center = canvas.mapFromScene(QPointF(32, 32))
    crop = data[center.y() - 12 : center.y() + 13, center.x() - 12 : center.x() + 13]
    ys, xs = np.nonzero((crop[:, :, 1] > 180) & (crop[:, :, 0] < 50) & (crop[:, :, 2] < 50))
    assert len(xs) >= 12
    assert 14 <= xs.max() - xs.min() <= 16
    assert 14 <= ys.max() - ys.min() <= 16
    assert canvas.highlight_pen().color() == QColor("#ff00ff")
    assert canvas.highlight_pen().widthF() == 3


def test_object_preview_feedback_and_invalid_scale_do_not_commit(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    node = controller.document.active_layer
    window.layer_panel.select_node(node)
    fields = window.object_properties.fields
    stack = controller.commands.undo_stack.activeStack()
    count = stack.count()
    fields["width"].interactionStarted.emit()
    fields["width"].setValue(128)
    assert fields["scale_x"].value() == 2
    assert controller.document.node_by_id(node.id).transform.is_identity
    fields["width"].interactionCanceled.emit()
    assert fields["scale_x"].value() == 1
    fields["scale_x"].interactionStarted.emit()
    fields["scale_x"].setValue(2)
    fields["scale_x"].setValue(0)
    assert window.object_properties.message.text()
    fields["scale_x"].editingFinished.emit()
    assert stack.count() == count
    assert controller.document.node_by_id(node.id).transform.is_identity
