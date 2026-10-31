from __future__ import annotations

from uuid import uuid4

import pytest

from PySide6.QtCore import QPoint, Qt

from easypicture.commands import DocumentCommandService
from easypicture.controller import ApplicationController
from easypicture.domain import (
    AffineTransform,
    Document,
    FloatRect,
    Point,
    Size,
    SvgCircleGeometry,
    SvgEllipseGeometry,
    SvgLineGeometry,
    SvgObject,
    SvgPolygonGeometry,
    SvgPolylineGeometry,
    SvgPrimitiveKind,
    SvgPrimitiveStyle,
    SvgRectGeometry,
    create_svg_primitive,
    parse_editable_svg,
)
from easypicture.imaging import ShapeOutputTarget
from easypicture.tools import ShapeTool
from easypicture.ui.dialogs import NewDocumentSpec
from easypicture.ui.main_window import MainWindow
from easypicture.ui.svg_properties import SvgPropertiesPanel
from tests.fakes import FakeDialogService, make_settings


def _panel_for_object(qtbot, geometry):
    document = Document.create(128, 96)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_vector_group("矢量组")
    group = document.vector_groups[-1]
    kind_by_geometry = {
        SvgRectGeometry: SvgPrimitiveKind.RECT,
        SvgCircleGeometry: SvgPrimitiveKind.CIRCLE,
        SvgEllipseGeometry: SvgPrimitiveKind.ELLIPSE,
        SvgLineGeometry: SvgPrimitiveKind.LINE,
        SvgPolylineGeometry: SvgPrimitiveKind.POLYLINE,
        SvgPolygonGeometry: SvgPrimitiveKind.POLYGON,
    }
    kind = kind_by_geometry[type(geometry)]
    vector = create_svg_primitive(
        kind,
        geometry,
        SvgPrimitiveStyle(fill=(255, 0, 0, 255), stroke=None),
        Size(128, 96),
    )
    assert commands.create_svg_object(group.id, vector)
    live = group.child_by_id(vector.id)
    panel = SvgPropertiesPanel(commands)
    qtbot.addWidget(panel)
    panel.set_object(live)
    commands.undo_stack.clear()
    return document, commands, panel, live


def test_svg_ellipse_properties_hide_duplicate_local_geometry(qtbot) -> None:
    _document, _commands, panel, _live = _panel_for_object(
        qtbot,
        SvgEllipseGeometry(40.0, 30.0, 20.0, 10.0),
    )

    assert panel.kind_label.text() == "椭圆"
    assert panel.geometry_title.isHidden()
    assert panel.geometry_stack.isHidden()


def test_svg_rect_properties_only_show_corner_specific_geometry(qtbot) -> None:
    _document, _commands, panel, _live = _panel_for_object(
        qtbot,
        SvgRectGeometry(4.0, 5.0, 20.0, 10.0, 2.0, 3.0),
    )

    assert not panel.geometry_stack.isHidden()
    assert all(field.isHidden() for field in panel.rect_fields[:4])
    assert all(not field.isHidden() for field in panel.rect_fields[4:])


def test_svg_line_properties_use_document_endpoints_without_duplicate_bounds(
    qtbot,
) -> None:
    _document, _commands, panel, live = _panel_for_object(
        qtbot,
        SvgLineGeometry(2.0, 3.0, 12.0, 13.0),
    )
    live.transform = AffineTransform.translation(20.0, 30.0)
    panel.set_object(live)

    assert tuple(field.value() for field in panel.line_fields) == (
        22.0,
        33.0,
        32.0,
        43.0,
    )


def test_svg_numeric_edit_previews_without_mutating_and_commits_once(qtbot) -> None:
    document, commands, panel, live = _panel_for_object(
        qtbot,
        SvgRectGeometry(4.0, 5.0, 20.0, 10.0),
    )
    revision = document.revision
    previews: list[object] = []
    panel.preview_changed.connect(previews.append)
    width_field = panel.rect_fields[2]
    width_field.interactionStarted.emit()
    width_field.setValue(36.0)
    assert previews
    assert document.revision == revision
    assert parse_editable_svg(live.svg).geometry == SvgRectGeometry(
        4.0, 5.0, 20.0, 10.0
    )  # type: ignore[union-attr]
    width_field.editingFinished.emit()
    assert commands.undo_stack.count() == 1
    assert parse_editable_svg(live.svg).geometry == SvgRectGeometry(
        4.0, 5.0, 36.0, 10.0
    )  # type: ignore[union-attr]
    commands.undo_stack.undo()
    assert parse_editable_svg(live.svg).geometry == SvgRectGeometry(
        4.0, 5.0, 20.0, 10.0
    )  # type: ignore[union-attr]


def test_svg_rect_corner_ranges_follow_half_of_current_size(qtbot) -> None:
    _document, _commands, panel, _live = _panel_for_object(
        qtbot,
        SvgRectGeometry(4.0, 5.0, 20.0, 10.0, 8.0, 4.0),
    )
    _x, _y, width, height, radius_x, radius_y = panel.rect_fields

    assert radius_x.minimum() == 0.0
    assert radius_x.maximum() == 10.0
    assert radius_y.minimum() == 0.0
    assert radius_y.maximum() == 5.0

    width.setValue(8.0)
    height.setValue(6.0)

    assert radius_x.maximum() == 4.0
    assert radius_x.value() == 4.0
    assert radius_y.maximum() == 3.0
    assert radius_y.value() == 3.0


def test_polygon_vertex_controls_add_delete_and_reorder(qtbot) -> None:
    _document, commands, panel, live = _panel_for_object(
        qtbot,
        SvgPolygonGeometry(((4.0, 4.0), (30.0, 4.0), (18.0, 30.0))),
    )
    panel.vertex_combo.setCurrentIndex(0)
    panel.add_vertex_button.click()
    parsed = parse_editable_svg(live.svg)
    assert parsed is not None
    assert isinstance(parsed.geometry, SvgPolygonGeometry)
    assert len(parsed.geometry.points) == 4
    panel.vertex_up_button.click()
    panel.delete_vertex_button.click()
    parsed = parse_editable_svg(live.svg)
    assert parsed is not None
    assert isinstance(parsed.geometry, SvgPolygonGeometry)
    assert len(parsed.geometry.points) == 3
    assert commands.undo_stack.count() == 3


def test_resource_fill_can_be_overridden_and_restored(qtbot) -> None:
    document = Document.create(64, 64)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_vector_group()
    group = document.vector_groups[-1]
    vector = SvgObject(
        id=uuid4(),
        name="渐变矩形",
        element_kind="rect",
        svg="""<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">
        <defs><linearGradient id="g"><stop stop-color="#f00"/></linearGradient></defs>
        <rect x="4" y="4" width="40" height="30" fill="url(#g)"/>
        </svg>""",
        intrinsic_bounds=FloatRect(4.0, 4.0, 40.0, 30.0),
    )
    assert commands.create_svg_object(group.id, vector)
    live = group.child_by_id(vector.id)
    panel = SvgPropertiesPanel(commands)
    qtbot.addWidget(panel)
    panel.set_object(live)
    commands.undo_stack.clear()
    assert [panel.fill_mode.itemText(i) for i in range(panel.fill_mode.count())] == [
        "无",
        "纯色",
    ]
    assert panel.fill_mode.currentIndex() == -1
    panel.opacity_spin.interactionStarted.emit()
    panel.opacity_spin.setValue(75)
    panel.opacity_spin.editingFinished.emit()
    assert parse_editable_svg(live.svg).fill_resource == "url(#g)"
    commands.undo_stack.clear()
    panel.fill_mode.setCurrentIndex(panel.fill_mode.findData("solid"))
    parsed = parse_editable_svg(live.svg)
    assert parsed is not None and parsed.fill_resource is None
    panel.set_object(live)
    commands.undo_stack.undo()
    parsed = parse_editable_svg(live.svg)
    assert parsed is not None and parsed.fill_resource == "url(#g)"


def _window(qtbot, tmp_path) -> tuple[ApplicationController, MainWindow, ShapeTool]:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialogs = FakeDialogService()
    dialogs.next_document = NewDocumentSpec(128, 96)
    controller = ApplicationController(settings, dialogs)
    window = MainWindow(controller, settings)
    qtbot.addWidget(window)
    window.resize(900, 650)
    window.show()
    window.new_action.trigger()
    window.tool_actions["shape"].trigger()
    tool = window._tools["shape"]
    assert isinstance(tool, ShapeTool)
    return controller, window, tool


def _drag(qtbot, window: MainWindow, start: QPoint, end: QPoint) -> None:
    viewport = window.canvas_view.viewport()
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, end)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)


def test_vector_shape_release_selects_object_and_shift_a_recreates_in_same_group(
    qtbot,
    tmp_path,
) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    options = window.tool_options_panel.shape
    options.output_combo.setCurrentIndex(
        options.output_combo.findData(ShapeOutputTarget.VECTOR.value)
    )
    assert window.settings.shape_output_target() is ShapeOutputTarget.VECTOR
    _drag(
        qtbot,
        window,
        window.canvas_view.mapFromScene(10.0, 10.0),
        window.canvas_view.mapFromScene(70.0, 50.0),
    )
    document = controller.document
    assert document is not None
    assert len(document.vector_groups) == 1
    group = document.vector_groups[0]
    assert (
        len(group.children) == 2
    )  # Existing raster and the new SVG share their collection.
    assert controller.commands.undo_stack.count() == 1
    assert not window.canvas_view.render_coordinator.has_transient_vector_preview
    assert window.layer_panel.current_node() is group.children[0]
    assert window.tool_controller.active_tool is window._tools["object"]

    from tests.ui.test_object_shortcuts import _key

    _key(window.canvas_view, Qt.Key.Key_A, Qt.KeyboardModifier.ShiftModifier)
    assert window.create_object_menu.isVisible()
    window.create_object_menu.close()
    window.create_vector_menu.actions()[1].trigger()
    assert window.tool_controller.active_tool is tool
    _drag(
        qtbot,
        window,
        window.canvas_view.mapFromScene(74.0, 12.0),
        window.canvas_view.mapFromScene(112.0, 44.0),
    )
    assert len(document.vector_groups) == 1
    assert len(group.children) == 3
    assert controller.commands.undo_stack.count() == 2
    assert window.layer_panel.current_node() is group.children[0]
    assert window.tool_controller.active_tool is window._tools["object"]
    controller.commands.undo_stack.undo()
    assert len(group.children) == 2
    controller.commands.undo_stack.undo()
    assert not document.vector_groups


def test_existing_svg_canvas_drag_commits_one_step_and_keeps_handles(
    qtbot,
    tmp_path,
) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    document = controller.document
    assert document is not None
    assert controller.commands.create_vector_group()
    group = document.vector_groups[-1]
    vector = create_svg_primitive(
        SvgPrimitiveKind.RECT,
        SvgRectGeometry(10.0, 10.0, 40.0, 30.0),
        SvgPrimitiveStyle(fill=(255, 0, 0, 255), stroke=None),
        document.size,
    )
    assert controller.commands.create_svg_object(group.id, vector)
    live = group.child_by_id(vector.id)
    controller.commands.undo_stack.clear()
    window.layer_panel.select_node(live)
    window._activate_edit_mode(window.edit_mode.findData("vector"))
    qtbot.waitUntil(lambda: tool.has_active_session)

    viewport = window.canvas_view.viewport()
    start = window.canvas_view.mapFromScene(10.0, 10.0)
    end = window.canvas_view.mapFromScene(5.0, 6.0)
    assert window.canvas_view.render_coordinator.vector_preview_object_count == 1
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    assert window.canvas_view.render_coordinator.vector_preview_object_count == 0
    qtbot.mouseMove(viewport, end)
    assert window.canvas_view.render_coordinator.vector_preview_object_count == 0
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    assert window.canvas_view.render_coordinator.vector_preview_object_count == 1
    assert controller.commands.undo_stack.count() == 1
    assert tool.has_active_session
    parsed = parse_editable_svg(group.child_by_id(vector.id).svg)
    assert parsed is not None
    assert parsed.geometry == SvgRectGeometry(5.0, 6.0, 45.0, 34.0)
    controller.commands.undo_stack.undo()
    parsed = parse_editable_svg(group.child_by_id(vector.id).svg)
    assert parsed is not None
    assert parsed.geometry == SvgRectGeometry(10.0, 10.0, 40.0, 30.0)


def test_polygon_canvas_insert_and_delete_each_commit_one_step(qtbot, tmp_path) -> None:
    controller, window, tool = _window(qtbot, tmp_path)
    document = controller.document
    assert document is not None
    assert controller.commands.create_vector_group()
    group = document.vector_groups[-1]
    vector = create_svg_primitive(
        SvgPrimitiveKind.POLYGON,
        SvgPolygonGeometry(((10.0, 10.0), (60.0, 10.0), (35.0, 60.0))),
        SvgPrimitiveStyle(fill=(255, 0, 0, 255), stroke=None),
        document.size,
    )
    assert controller.commands.create_svg_object(group.id, vector)
    controller.commands.undo_stack.clear()
    window.layer_panel.select_node(group.child_by_id(vector.id))
    window._activate_edit_mode(window.edit_mode.findData("vector"))
    qtbot.waitUntil(lambda: tool.has_active_session)

    assert tool._insert_vertex_at_nearest_segment(Point(35.0, 10.0))
    parsed = parse_editable_svg(group.child_by_id(vector.id).svg)
    assert parsed is not None and isinstance(parsed.geometry, SvgPolygonGeometry)
    assert len(parsed.geometry.points) == 4
    assert controller.commands.undo_stack.count() == 1
    assert tool._delete_selected_vertex()
    parsed = parse_editable_svg(group.child_by_id(vector.id).svg)
    assert parsed is not None and isinstance(parsed.geometry, SvgPolygonGeometry)
    assert len(parsed.geometry.points) == 3
    assert controller.commands.undo_stack.count() == 2


def test_creation_colors_follow_palette_without_recoloring_existing_objects(
    qtbot, tmp_path, monkeypatch
):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor
    from easypicture.imaging import ShapeKind, ShapePaintStyle

    controller, window, tool = _window(qtbot, tmp_path)
    palette = window.color_panel
    assert palette.foreground_button.text() == "主颜色"
    assert palette.background_button.text() == "副颜色"
    palette.set_foreground(QColor(10, 20, 30, 255))
    palette.set_background(QColor(40, 50, 60, 128))
    options = window.tool_options_panel.shape
    options.kind_combo.setCurrentIndex(
        options.kind_combo.findData(ShapeKind.RECTANGLE.value)
    )
    options.style_combo.setCurrentIndex(
        options.style_combo.findData(ShapePaintStyle.STROKE_AND_FILL.value)
    )
    options.output_combo.setCurrentIndex(
        options.output_combo.findData(ShapeOutputTarget.VECTOR.value)
    )
    _drag(
        qtbot,
        window,
        window.canvas_view.mapFromScene(10, 10),
        window.canvas_view.mapFromScene(70, 50),
    )
    vector = window.layer_panel.current_node()
    original = vector.svg
    from PySide6.QtWidgets import QDialog
    from easypicture.ui.color_dialog import ColorPickerDialog

    coordinator = window.canvas_view.render_coordinator

    def preview_dialog(dialog):
        dialog._set_color(QColor(5, 15, 25, 255))
        shown = coordinator.selection_nodes((vector.id,))[0]
        assert parse_editable_svg(shown.svg).style.fill == (5, 15, 25, 255)
        assert vector.svg == original
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ColorPickerDialog, "exec", preview_dialog)
    window.svg_properties._pick_color("fill")
    assert not coordinator.has_transient_vector_preview
    assert vector.svg == original
    style = parse_editable_svg(vector.svg).style
    assert style.stroke == (10, 20, 30, 255)
    assert style.fill == (40, 50, 60, 128)
    palette.swap_colors()
    assert vector.svg == original
    assert options.settings().stroke_color == (40, 50, 60, 128)
    assert options.settings().fill_color == (10, 20, 30, 255)
    window.tool_actions["text"].trigger()
    window._tools["text"]._create_text_object(QRectF(4, 5, 20, 10))
    text = window.layer_panel.current_node()
    assert text.color == (40, 50, 60, 128)


@pytest.mark.parametrize(
    "geometry,expected",
    [
        (SvgRectGeometry(4, 5, 20, 10), (40, 30)),
        (SvgEllipseGeometry(40, 30, 20, 10), (80, 60)),
        (SvgCircleGeometry(40, 30, 10), (40, 60)),
    ],
)
@pytest.mark.parametrize("commit", [True, False])
def test_svg_dimensions_share_object_axes_and_preserve_center(
    qtbot, geometry, expected, commit
):
    from easypicture.domain.object_operations import object_local_bounds, object_pivot
    import math

    _, commands, panel, live = _panel_for_object(qtbot, geometry)
    original_svg = live.svg
    original_matrix = AffineTransform.scale(-2, 3).then(AffineTransform.rotation(30))
    live.transform = original_matrix
    live.transform_pivot = Point(9, 12)
    panel.set_object(live)
    assert not panel.length_spin.isHidden()
    assert not panel.width_spin.isHidden()
    assert panel.length_spin.value() == pytest.approx(expected[0])
    assert panel.width_spin.value() == pytest.approx(expected[1])
    previews = []
    panel.preview_changed.connect(previews.append)
    panel.length_spin.interactionStarted.emit()
    panel.length_spin.setValue(60)
    panel.length_spin.setValue(70)
    panel.width_spin.setValue(50)
    assert commands.undo_stack.count() == 0
    assert live.transform == original_matrix
    preview = previews[-1]
    bounds = object_local_bounds(preview)
    assert bounds.width * math.hypot(
        preview.transform.a, preview.transform.b
    ) == pytest.approx(70)
    assert bounds.height * math.hypot(
        preview.transform.c, preview.transform.d
    ) == pytest.approx(50)
    assert object_pivot(preview) == Point(9, 12)
    assert preview.svg == original_svg
    if commit:
        panel.length_spin.editingFinished.emit()
        assert commands.undo_stack.count() == 1
        assert live.transform == preview.transform
        commands.undo_stack.undo()
        assert live.transform == original_matrix
        assert live.transform_pivot == Point(9, 12)
    else:
        panel.length_spin.interactionCanceled.emit()
        assert commands.undo_stack.count() == 0
        assert live.transform == original_matrix
        assert panel.length_spin.value() == pytest.approx(expected[0])


@pytest.mark.parametrize(
    "geometry,caps,joins,rule",
    [
        (SvgRectGeometry(4, 5, 20, 10), False, False, False),
        (SvgEllipseGeometry(40, 30, 20, 10), False, False, False),
        (SvgCircleGeometry(40, 30, 10), False, False, False),
        (SvgLineGeometry(4, 5, 20, 10), True, False, False),
        (SvgPolylineGeometry(((0, 0), (20, 10), (0, 20))), True, True, False),
        (SvgPolygonGeometry(((0, 0), (20, 0), (20, 20), (0, 20))), False, True, False),
        (
            SvgPolygonGeometry(((0, -30), (18, 24), (-28, -9), (28, -9), (-18, 24))),
            False,
            True,
            True,
        ),
    ],
)
def test_svg_only_exposes_effective_style_controls(qtbot, geometry, caps, joins, rule):
    from easypicture.domain import update_editable_svg

    _, commands, panel, live = _panel_for_object(qtbot, geometry)
    assert commands.update_vector_object(
        live.id,
        svg=update_editable_svg(
            live.svg,
            geometry,
            SvgPrimitiveStyle(fill=(255, 0, 0, 255), stroke=(0, 0, 0, 255)),
        ),
    )
    panel.set_object(live)
    assert panel.line_cap_combo.isHidden() == (not caps)
    assert panel.line_join_combo.isHidden() == (not joins)
    assert panel.miter_limit_spin.isHidden() == (not joins)
    assert panel.fill_rule_combo.isHidden() == (not rule)
    if joins:
        panel.line_join_combo.setCurrentIndex(panel.line_join_combo.findData("round"))
        assert panel.miter_limit_spin.isHidden()
    panel.stroke_mode.setCurrentIndex(panel.stroke_mode.findData("none"))
    assert panel.stroke_width_spin.isHidden()
    assert panel.line_cap_combo.isHidden()
    assert panel.line_join_combo.isHidden()
    assert panel.dash_buttons.isHidden()


def test_triangle_drag_creates_selected_editable_polygon(qtbot, tmp_path):
    from easypicture.imaging import ShapeKind
    from tests.ui.test_raster_editor import (
        _window as workspace_window,
        _drag as workspace_drag,
    )

    window, controller = workspace_window(qtbot, tmp_path)
    options = window.tool_options_panel.shape
    index = options.kind_combo.findData(ShapeKind.TRIANGLE.value)
    assert index >= 0
    window.create_vector_menu.actions()[index].trigger()
    workspace_drag(window, "shape", (10, 10), (50, 40))
    node = window.layer_panel.current_node()
    assert isinstance(node, SvgObject)
    assert node.name == "三角形"
    primitive = parse_editable_svg(node.svg)
    assert primitive.geometry == SvgPolygonGeometry(((30, 10), (50, 40), (10, 40)))
    assert window.tool_controller.active_tool is window._tools["object"]
    assert window.svg_properties.kind_label.text() == "三角形"
    assert not window.svg_properties.vertex_combo.isHidden()
    controller.commands.undo_stack.undo()
    with pytest.raises(ValueError):
        controller.document.node_by_id(node.id)
    controller.commands.undo_stack.redo()
    assert (
        parse_editable_svg(controller.document.node_by_id(node.id).svg).geometry
        == primitive.geometry
    )
