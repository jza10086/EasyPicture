from __future__ import annotations

import pytest
from PySide6.QtGui import QFontDatabase

from easypicture.domain import RasterObject, SvgObject, TextObject, parse_editable_svg
from easypicture.domain.layer import RasterSampling
from easypicture.render.text_renderer import cached_text_layout
from tests.fakes import make_settings
from tests.ui.test_raster_editor import _drag, _window


@pytest.fixture(autouse=True)
def text_font(qapp):
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    assert font_id >= 0
    yield
    QFontDatabase.removeApplicationFont(font_id)


@pytest.mark.parametrize("height", [24, 64, 120])
def test_text_height_initializes_font_and_finishes_creation(qtbot, tmp_path, height):
    window, controller = _window(qtbot, tmp_path)
    panel = window.creation_properties
    assert window.inspector_tabs.widget(2) is panel
    assert window.inspector_tabs.tabText(2) == "初始化"
    assert panel.auto_size.isChecked()
    before = controller.commands.undo_stack.activeStack().count()
    _drag(window, "text", (0, 0), (400, height))
    text = window.layer_panel.current_node()
    assert isinstance(text, TextObject)
    assert text.frame.height == pytest.approx(height, abs=1)
    layout = cached_text_layout(text)
    assert text.frame.height - 3 <= layout.height <= text.frame.height + 1
    assert text.font_size == pytest.approx(panel.size_spin.value(), abs=0.01)
    assert controller.commands.undo_stack.activeStack().count() == before + 1
    assert window.tool_controller.active_tool is window._tools["object"]
    assert window.tool_actions["object"].isChecked()
    assert window.layer_panel.selection.state.active_id == text.id
    controller.commands.undo_stack.undo()
    with pytest.raises(ValueError):
        controller.document.node_by_id(text.id)
    controller.commands.undo_stack.redo()
    assert controller.document.node_by_id(text.id).font_size == text.font_size


def test_manual_text_defaults_reuse_and_persist_without_changing_existing(
    qtbot, tmp_path
):
    window, controller = _window(qtbot, tmp_path)
    panel = window.creation_properties
    panel.auto_size.setChecked(False)
    panel.size_spin.setValue(19)
    panel.size_spin.editingFinished.emit()
    panel.text_edit.setText("初始文字")
    panel.text_edit.editingFinished.emit()
    panel.bold_check.setChecked(True)
    _drag(window, "text", (0, 0), (200, 30))
    first = window.layer_panel.current_node()
    _drag(window, "text", (0, 0), (300, 90))
    second = window.layer_panel.current_node()
    assert first.id != second.id
    assert first.font_size == second.font_size == 19
    assert first.font_weight == second.font_weight == 700
    assert first.text == second.text == "初始文字"
    panel.size_spin.setValue(27)
    panel.size_spin.editingFinished.emit()
    assert first.font_size == second.font_size == 19
    restored = make_settings(str(tmp_path / "settings.ini"))
    assert not restored.text_auto_size()
    assert restored.text_initialization().font_size == 27
    assert restored.text_initialization().text == "初始文字"
    assert restored.last_created_kind() == "text"


def test_shape_and_raster_initialization_syncs_with_tools(qtbot, tmp_path):
    window, controller = _window(qtbot, tmp_path)
    panel = window.creation_properties
    panel.shape.stroke_width_spin.setValue(7)
    assert window.tool_options_panel.shape.settings().stroke_width == 7
    _drag(window, "shape")
    svg = window.layer_panel.current_node()
    assert isinstance(svg, SvgObject)
    assert parse_editable_svg(svg.svg).style.stroke_width == 7
    assert panel.kind_combo.currentData() == "shape"
    assert window.tool_controller.active_tool is window._tools["object"]
    window.tool_options_panel.shape.stroke_width_spin.setValue(3)
    assert panel.shape.settings().stroke_width == 3
    panel.raster_opacity.setValue(40)
    panel.raster_opacity.editingFinished.emit()
    panel.sampling.setCurrentIndex(panel.sampling.findData(RasterSampling.SMOOTH.value))
    _drag(window, "raster_object")
    raster = window.layer_panel.current_node()
    assert isinstance(raster, RasterObject)
    assert raster.opacity == 0.4
    assert raster.sampling is RasterSampling.SMOOTH
    assert panel.kind_combo.currentData() == "raster"
    assert window.tool_controller.active_tool is window._tools["object"]
    assert panel.last_label.text() == f"上次创建：{raster.name}"
