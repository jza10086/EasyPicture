from __future__ import annotations

from easypicture.commands import DocumentCommandService
from easypicture.imaging import ShapeGeometry, ShapeKind
from easypicture.ui.dialogs import NewDocumentDialog, NewDocumentSpec
from easypicture.ui.numeric_ranges import COMMON_CANVAS_SIZE, SIGNED_COORDINATE_RANGE
from easypicture.ui.shape_properties import ShapePropertiesPanel
from easypicture.ui.svg_properties import SvgPropertiesPanel
from easypicture.ui.text_properties import TextPropertiesPanel
from easypicture.ui.transform_properties import TransformPropertiesPanel


def test_spatial_fields_share_common_soft_ranges_and_keep_hard_entry(qtbot) -> None:
    commands = DocumentCommandService()
    shape = ShapePropertiesPanel()
    svg = SvgPropertiesPanel(commands)
    text = TextPropertiesPanel(commands)
    transform = TransformPropertiesPanel()
    new_document = NewDocumentDialog(NewDocumentSpec(1920, 1080).size)
    for widget in (shape, svg, text, transform, new_document):
        qtbot.addWidget(widget)

    assert shape._box_fields[2].drag_maximum == SIGNED_COORDINATE_RANGE.soft_maximum
    assert shape._box_fields[2].maximum() == 32768.0
    assert svg.width_spin.drag_maximum == COMMON_CANVAS_SIZE
    assert svg.width_spin.maximum() == 32768.0
    assert svg.rotation_spin.drag_minimum == -180.0
    assert svg.rotation_spin.maximum() == 36000.0
    assert text.width_spin.drag_maximum == COMMON_CANVAS_SIZE
    assert text.width_spin.maximum() == 8192.0
    assert text.size_spin.drag_maximum == 256.0
    assert text.size_spin.maximum() == 1024.0
    assert transform._fields[0].drag_maximum == SIGNED_COORDINATE_RANGE.soft_maximum
    assert transform._fields[0].maximum() == 32768.0
    assert new_document.width_spin.drag_maximum == int(COMMON_CANVAS_SIZE)
    assert new_document.width_spin.maximum() == 8192


def test_raster_rounded_radius_range_tracks_current_short_edge(qtbot) -> None:
    panel = ShapePropertiesPanel()
    qtbot.addWidget(panel)
    panel.set_geometry(
        ShapeGeometry(
            ShapeKind.ROUNDED_RECTANGLE,
            ((10.0, 10.0), (110.0, 50.0)),
            18.0,
        )
    )
    width = panel._rounded_fields[2]
    radius = panel._rounded_fields[4]

    assert radius.drag_maximum == 20.0
    assert radius.maximum() == 20.0

    width.setValue(20.0)

    assert radius.drag_maximum == 10.0
    assert radius.maximum() == 10.0
    assert radius.value() == 10.0
