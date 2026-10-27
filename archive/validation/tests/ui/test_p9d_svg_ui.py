from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel

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
    assert not panel.x_spin.isHidden()
    assert not panel.y_spin.isHidden()
    assert not panel.width_spin.isHidden()
    assert not panel.height_spin.isHidden()
    assert panel.geometry_title.isHidden()
    assert panel.geometry_stack.isHidden()
    pivot_label = panel.transform_form.labelForField(panel.pivot_x_spin)
    assert isinstance(pivot_label, QLabel)
    assert pivot_label.text() == "旋转中心 X"


def test_svg_rect_properties_only_show_corner_specific_geometry(qtbot) -> None:
    _document, _commands, panel, _live = _panel_for_object(
        qtbot,
        SvgRectGeometry(4.0, 5.0, 20.0, 10.0, 2.0, 3.0),
    )

    assert not panel.geometry_stack.isHidden()
    assert all(field.isHidden() for field in panel.rect_fields[:4])
    assert all(not field.isHidden() for field in panel.rect_fields[4:])


def test_svg_line_properties_use_document_endpoints_without_duplicate_bounds(qtbot) -> None:
    _document, _commands, panel, live = _panel_for_object(
        qtbot,
        SvgLineGeometry(2.0, 3.0, 12.0, 13.0),
    )
    live.transform = AffineTransform.translation(20.0, 30.0)
    panel.set_object(live)

    assert panel.x_spin.isHidden()
    assert panel.y_spin.isHidden()
    assert panel.width_spin.isHidden()
    assert panel.height_spin.isHidden()
    assert tuple(field.value() for field in panel.line_fields) == (22.0, 33.0, 32.0, 43.0)


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
    assert parse_editable_svg(live.svg).geometry == SvgRectGeometry(4.0, 5.0, 20.0, 10.0)  # type: ignore[union-attr]
    width_field.editingFinished.emit()
    assert commands.undo_stack.count() == 1
    assert parse_editable_svg(live.svg).geometry == SvgRectGeometry(4.0, 5.0, 36.0, 10.0)  # type: ignore[union-attr]
    commands.undo_stack.undo()
    assert parse_editable_svg(live.svg).geometry == SvgRectGeometry(4.0, 5.0, 20.0, 10.0)  # type: ignore[union-attr]


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
    panel.fill_mode.setCurrentIndex(panel.fill_mode.findData("solid"))
    parsed = parse_editable_svg(live.svg)
    assert parsed is not None and parsed.fill_resource is None
    panel.set_object(live)
    panel.reset_fill_resource_button.click()
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


def test_vector_shape_release_commits_and_continues_in_same_group(
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
    assert len(group.children) == 2  # Existing raster and the new SVG share their collection.
    assert controller.commands.undo_stack.count() == 1
    assert not window.canvas_view.render_coordinator.has_transient_vector_preview
    assert window.layer_panel.current_node() is group.children[0]
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
    assert window.tool_controller.active_tool is tool
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
