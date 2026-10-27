from math import isclose

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent

from easypicture.diagnostics import performance_counters
from easypicture.domain import (
    AffineTransform,
    Collection,
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
from easypicture.pixels import TileCoord
from easypicture.tools.object_hit_test import opaque_at
from easypicture.ui.icons import ICON_ROOT, TOOL_ICONS, icon, object_icon
from tests.ui.test_raster_editor import _window


def test_studios_separate_tools_and_keep_object_inspector(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    raster = controller.document.active_layer
    text = TextObject.create(FloatRect(5, 5, 20, 20))
    controller.commands.create_object(text)
    window.layer_panel.select_node(controller.document.node_by_id(text.id))
    assert window.properties_stack.currentWidget() is window.text_properties
    window._activate_tool("shape")
    assert window.properties_stack.currentWidget() is window.text_properties
    assert window.tool_details_stack.currentWidget() is window.shape_properties
    assert window.inspector_tabs.widget(1) is window.color_panel
    assert window.inspector_tabs.isAncestorOf(window.layer_panel.object_controls)
    assert not window.layer_panel.isAncestorOf(window.layer_panel.object_controls)
    window.layer_panel.select_node(raster)
    window.studio_actions["pixel"].trigger()
    assert window._is_raster_editor()
    assert window.studio_actions["pixel"].isChecked()
    assert not window.layers_dock.isVisible()
    assert window.properties_dock.isVisible()
    assert window.inspector_tabs.currentWidget() is window.color_panel
    assert window.tool_actions["brush"].isVisible()
    assert not window.tool_actions["shape"].isVisible()
    window.studio_actions["vector"].trigger()
    assert not window._is_raster_editor()
    assert window.layers_dock.isVisible()
    assert window.object_properties.isVisible()


def test_wheel_zooms_without_modifier_and_preserves_anchor(qtbot, tmp_path):
    window, _ = _window(qtbot, tmp_path)
    canvas = window.canvas_view
    canvas.set_zoom(2)
    anchor = canvas.viewport().rect().center()
    before = canvas.viewport_to_document(anchor)
    event = QWheelEvent(
        QPointF(anchor),
        QPointF(anchor),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    canvas.wheelEvent(event)
    assert isclose(canvas.zoom, 2 * canvas.ZOOM_STEP)
    after = canvas.viewport_to_document(anchor)
    assert abs(after.x - before.x) < 1
    assert abs(after.y - before.y) < 1
    assert event.isAccepted()


def test_transparent_pick_switch_passes_through_transformed_raster(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    document = controller.document
    lower = document.active_layer
    upper = RasterObject.create_blank("Top", Size(16, 16))
    upper.transform = AffineTransform.scale(2, 2).then(AffineTransform.translation(8, 8))
    tile = np.zeros((256, 256, 4), np.uint8)
    tile[5, 5] = (200, 80, 10, 255)
    upper.pixels.replace_tile(0, 0, tile)
    controller.commands.create_object(upper)
    window._activate_tool("object")

    def pick(x, y):
        point = QPointF(window.canvas_view.mapFromScene(QPointF(x, y)))
        for kind, buttons in (
            (QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton),
        ):
            event = QMouseEvent(
                kind,
                point,
                point,
                Qt.MouseButton.LeftButton,
                buttons,
                Qt.KeyboardModifier.NoModifier,
            )
            if kind == QEvent.Type.MouseButtonPress:
                window.tool_controller.mouse_press(event)
            else:
                window.tool_controller.mouse_release(event)

    pick(12, 12)
    assert window.layer_panel.current_node().id == upper.id
    tile[:] = (20, 30, 60, 255)
    controller.commands.apply_pixel_tiles(lower.id, {TileCoord(0, 0): tile}, "base")
    window.tool_options_panel.ignore_transparent.setChecked(True)
    assert window.settings.ignore_transparent_objects()
    pick(12, 12)
    assert window.layer_panel.current_node().id == lower.id
    pick(19, 19)
    assert window.layer_panel.current_node().id == upper.id


def test_svg_hollow_area_and_text_whitespace_do_not_hit(qapp):
    svg = create_svg_primitive(
        SvgPrimitiveKind.RECT,
        SvgRectGeometry(4, 4, 24, 24),
        SvgPrimitiveStyle(fill=None, stroke=(255, 0, 0, 255), stroke_width=4),
        Size(32, 32),
    )
    assert not opaque_at(svg, Point(16, 16))
    assert opaque_at(svg, Point(4, 16))
    text = TextObject.create(FloatRect(0, 0, 100, 100), text=" ")
    assert not opaque_at(text, Point(50, 50))


def test_outliner_icons_never_render_pixels_and_assets_are_valid(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    document = controller.document
    model = window.layer_panel.tree_model
    layer = document.active_layer
    before = performance_counters.thumbnails
    index = model.index_for_id(layer.id)
    first = model.data(index, Qt.ItemDataRole.DecorationRole)
    changes = []
    model.dataChanged.connect(lambda *_args: changes.append(True))
    controller.commands.apply_pixel_tiles(
        layer.id, {TileCoord(0, 0): np.full((256, 256, 4), 255, np.uint8)}, "paint"
    )
    second = model.data(index, Qt.ItemDataRole.DecorationRole)
    assert first.cacheKey() == second.cacheKey()
    assert not changes
    assert performance_counters.thumbnails == before
    assert object_icon(Collection.create("Group")).cacheKey() != first.cacheKey()
    assert object_icon(TextObject.create(FloatRect(0, 0, 4, 4))).cacheKey() != first.cacheKey()
    for name in TOOL_ICONS.values():
        assert not icon(name).isNull()
    assert (ICON_ROOT / "LICENSE.txt").is_file()
