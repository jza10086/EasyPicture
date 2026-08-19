from __future__ import annotations

from PySide6.QtCore import QPoint, Qt

from easypicture.domain import Document
from easypicture.pixels import Rect
from easypicture.ui.drag_value_spinbox import NumericEditState
from easypicture.ui.sprite_workspace_panel import SpriteWorkspacePanel


def test_region_feedback_does_not_interrupt_continuous_numeric_drag(qtbot) -> None:
    document = Document.create(128, 128)
    panel = SpriteWorkspacePanel()
    qtbot.addWidget(panel)
    panel.set_document(document)
    panel.set_region(Rect(0, 0, 32, 32))
    panel.region_requested.connect(panel.set_region)
    panel.resize(320, 480)
    panel.show()

    field = panel.region_x
    field.resize(180, field.height())
    qtbot.mousePress(field, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    qtbot.mouseMove(field, QPoint(70, 10))
    assert field.interaction_state is NumericEditState.DRAGGING
    first_value = field.value()

    qtbot.mouseMove(field, QPoint(105, 10))
    assert field.interaction_state is NumericEditState.DRAGGING
    assert field.value() != first_value

    qtbot.mouseRelease(field, Qt.MouseButton.LeftButton, pos=QPoint(105, 10))
    assert field.interaction_state is NumericEditState.IDLE
