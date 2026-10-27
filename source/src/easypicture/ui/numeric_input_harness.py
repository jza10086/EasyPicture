"""Isolated manual and automatic harness for Blender-style numeric input."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QFocusEvent, QKeyEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from easypicture.ui.drag_value_spinbox import (
    DRAG_THRESHOLD,
    DragValueDoubleSpinBox,
    DragValueSpinBox,
    NumericEditState,
)

if TYPE_CHECKING:
    from collections.abc import Callable

type NumericField = DragValueSpinBox | DragValueDoubleSpinBox

_TEXT_STATES = {
    NumericEditState.TEXT_EDITING,
    NumericEditState.TEXT_SELECTING,
}
_DRAG_STATES = {
    NumericEditState.WAIT_DRAG,
    NumericEditState.DRAGGING,
}
_LOGGED_EVENT_TYPES = {
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.KeyPress,
    QEvent.Type.KeyRelease,
    QEvent.Type.FocusIn,
    QEvent.Type.FocusOut,
}


@dataclass(frozen=True, slots=True)
class AutomaticReport:
    name: str
    passed: bool
    checks: int
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "checks": self.checks,
            "errors": list(self.errors),
        }


class NumericInteractionProbe(QObject):
    """Record event ownership and reject text-to-drag transitions."""

    def __init__(
        self,
        name: str,
        field: NumericField,
        append_log: Callable[[str, bool], None],
        update_status: Callable[[NumericEditState, int], None],
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self.name = name
        self.field = field
        self._append_log = append_log
        self._update_status = update_status
        self._previous_state = field.interaction_state
        self.block_failures = 0
        self._clock = QElapsedTimer()
        self._clock.start()
        field.interactionStateChanged.connect(self._state_changed)
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)
        self._update_status(self._previous_state, self.block_failures)

    def reset(self) -> None:
        self.block_failures = 0
        self._previous_state = self.field.interaction_state
        self._update_status(self._previous_state, self.block_failures)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not self._belongs_to_field(watched):
            return False
        if event.type() not in _LOGGED_EVENT_TYPES:
            if not (
                isinstance(event, QMouseEvent)
                and event.type() == QEvent.Type.MouseMove
                and event.buttons() != Qt.MouseButton.NoButton
            ):
                return False
        detail = self._event_detail(event)
        target = type(watched).__name__
        object_name = watched.objectName()
        if object_name:
            target = f"{target}#{object_name}"
        self._append_log(
            f"{self._clock.elapsed():05d}ms [{self.name}] EVENT "
            f"{event.type().name} target={target} state="
            f"{self.field.interaction_state.value}{detail}",
            False,
        )
        return False

    def _state_changed(self, state: object) -> None:
        if not isinstance(state, NumericEditState):
            return
        invalid = self._previous_state in _TEXT_STATES and state in _DRAG_STATES
        if invalid:
            self.block_failures += 1
        self._append_log(
            f"{self._clock.elapsed():05d}ms [{self.name}] STATE "
            f"{self._previous_state.value} -> {state.value}",
            invalid,
        )
        self._previous_state = state
        self._update_status(state, self.block_failures)

    def _belongs_to_field(self, watched: QObject) -> bool:
        current: QObject | None = watched
        while current is not None:
            if current is self.field:
                return True
            current = current.parent()
        return False

    @staticmethod
    def _event_detail(event: QEvent) -> str:
        if isinstance(event, QMouseEvent):
            return (
                f" button={event.button().name} buttons={event.buttons().value}"
                f" global=({event.globalPosition().x():.0f},"
                f"{event.globalPosition().y():.0f})"
            )
        if isinstance(event, QKeyEvent):
            return f" key={event.key()} text={event.text()!r}"
        if isinstance(event, QFocusEvent):
            return f" reason={event.reason().name}"
        return ""


class NumericInputHarness(QMainWindow):
    """Small window that contains no editor, document, tools, or settings."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EasyPicture 数值输入隔离测试")
        self.resize(860, 680)
        self._status_labels: dict[str, QLabel] = {}
        self._failure_labels: dict[str, QLabel] = {}
        self._probes: list[NumericInteractionProbe] = []

        host = QWidget(self)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        description = QLabel(
            "本窗口不加载 MainWindow、画布、工具、设置或文档。\n"
            "手动验证：单击进入文本并全选；再次点击/拖选只能在 "
            "TEXT_EDITING 与 TEXT_SELECTING 之间切换；按 Esc 退出后，"
            "从空闲状态水平拖动才允许进入 DRAGGING。",
            host,
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self.integer_field = DragValueSpinBox(
            -100,
            100,
            25,
            manual_minimum=-1000,
            manual_maximum=1000,
            suffix=" px",
            parent=host,
        )
        self.double_field = DragValueDoubleSpinBox(
            -100.0,
            100.0,
            12.5,
            manual_minimum=-1000.0,
            manual_maximum=1000.0,
            suffix=" px",
            parent=host,
        )
        layout.addWidget(self._make_field_group("整数控件", "integer", self.integer_field))
        layout.addWidget(self._make_field_group("浮点控件", "double", self.double_field))

        button_row = QHBoxLayout()
        self.run_button = QPushButton("运行自动交互序列", host)
        self.reset_button = QPushButton("重置控件", host)
        self.clear_button = QPushButton("清空日志", host)
        button_row.addWidget(self.run_button)
        button_row.addWidget(self.reset_button)
        button_row.addWidget(self.clear_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.result_label = QLabel("尚未运行自动序列", host)
        self.result_label.setObjectName("HarnessResult")
        layout.addWidget(self.result_label)
        self.log = QPlainTextEdit(host)
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        layout.addWidget(self.log, 1)
        self.setCentralWidget(host)

        for name, field in (
            ("integer", self.integer_field),
            ("double", self.double_field),
        ):
            self._probes.append(
                NumericInteractionProbe(
                    name,
                    field,
                    self._append_log,
                    self._probe_update_callback(name),
                    self,
                )
            )

        self.run_button.clicked.connect(self.run_automatic_checks)
        self.reset_button.clicked.connect(self.reset_fields)
        self.clear_button.clicked.connect(self.log.clear)
        self._apply_style()

    def _make_field_group(
        self,
        title: str,
        name: str,
        field: NumericField,
    ) -> QGroupBox:
        group = QGroupBox(title, self)
        grid = QGridLayout(group)
        grid.addWidget(QLabel("测试控件", group), 0, 0)
        grid.addWidget(field, 0, 1)
        state_label = QLabel(NumericEditState.IDLE.value, group)
        failure_label = QLabel("0", group)
        self._status_labels[name] = state_label
        self._failure_labels[name] = failure_label
        grid.addWidget(QLabel("当前状态", group), 1, 0)
        grid.addWidget(state_label, 1, 1)
        grid.addWidget(QLabel("阻断失败", group), 2, 0)
        grid.addWidget(failure_label, 2, 1)
        return group

    def reset_fields(self) -> None:
        self.integer_field.setValue(25)
        self.double_field.setValue(12.5)
        for probe in self._probes:
            probe.reset()
        self.result_label.setText("已重置")
        self.result_label.setStyleSheet("")

    def run_automatic_checks(self) -> tuple[AutomaticReport, ...]:
        self.reset_fields()
        reports = (
            run_automatic_sequence(
                "integer",
                self.integer_field,
                self.integer_field,
            ),
            run_automatic_sequence(
                "double",
                self.double_field,
                self.double_field._field,  # noqa: SLF001
            ),
        )
        passed = all(report.passed for report in reports)
        for report in reports:
            level = "PASS" if report.passed else "FAIL"
            self._append_log(
                f"[AUTO] {report.name}: {level}; checks={report.checks}; "
                f"errors={list(report.errors)}",
                not report.passed,
            )
        if passed and all(probe.block_failures == 0 for probe in self._probes):
            self.result_label.setText("PASS：文本模式与拖动模式切换及阻断均正确")
            self.result_label.setStyleSheet("color: #67c587; font-weight: 600;")
        else:
            self.result_label.setText("FAIL：检测到错误状态迁移，请查看日志")
            self.result_label.setStyleSheet("color: #ff6b6b; font-weight: 600;")
        return reports

    def _append_log(self, message: str, failure: bool) -> None:
        prefix = "!!! " if failure else ""
        self.log.appendPlainText(f"{prefix}{message}")

    def _update_probe(
        self,
        name: str,
        state: NumericEditState,
        failures: int,
    ) -> None:
        self._status_labels[name].setText(state.value)
        self._failure_labels[name].setText(str(failures))
        color = "#ff6b6b" if failures else "#67c587"
        self._failure_labels[name].setStyleSheet(f"color: {color}; font-weight: 600;")

    def _probe_update_callback(
        self,
        name: str,
    ) -> Callable[[NumericEditState, int], None]:
        def update(state: NumericEditState, failures: int) -> None:
            self._update_probe(name, state, failures)

        return update

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #242424; color: #e6e6e6; }
            QGroupBox {
                border: 1px solid #4b4b4b;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; }
            QPushButton {
                background: #3a3a3a;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px 10px;
            }
            QPushButton:hover { background: #4a4a4a; }
            QPlainTextEdit {
                background: #171717;
                border: 1px solid #4b4b4b;
                font-family: Consolas;
                font-size: 10pt;
            }
            """
        )


def run_automatic_sequence(
    name: str,
    field: NumericField,
    idle_surface: QWidget,
) -> AutomaticReport:
    """Exercise the exact state boundaries without any application UI."""

    errors: list[str] = []
    checks = 0

    def expect(condition: bool, label: str) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            errors.append(f"{label}: state={field.interaction_state.value}")

    application = QApplication.instance()
    if application is None:
        return AutomaticReport(name, False, 0, ("QApplication is missing",))
    if field.interaction_state is not NumericEditState.IDLE:
        if isinstance(field, DragValueSpinBox):
            field.setValue(field.value())
        else:
            field.setValue(field.value())
    application.processEvents()

    center = idle_surface.rect().center()
    QTest.mousePress(idle_surface, Qt.MouseButton.LeftButton, pos=center)
    expect(field.interaction_state is NumericEditState.WAIT_DRAG, "首次按下等待拖动")
    QTest.mouseRelease(idle_surface, Qt.MouseButton.LeftButton, pos=center)
    expect(field.interaction_state is NumericEditState.TEXT_EDITING, "首次释放进入文本")
    expect(field.lineEdit().hasSelectedText(), "首次进入文本全选")

    global_position = field.mapToGlobal(QPoint(2, max(2, field.height() // 2)))
    window = field.window()
    window_position = window.mapFromGlobal(global_position)
    top_level_press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(window_position),
        QPointF(global_position),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window, top_level_press)
    expect(
        field.interaction_state is NumericEditState.TEXT_SELECTING,
        "顶层窗口阶段的框内按下仍归文本会话",
    )
    top_level_release = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(window_position),
        QPointF(global_position),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window, top_level_release)
    expect(
        field.interaction_state is NumericEditState.TEXT_EDITING,
        "顶层窗口阶段的框内释放返回文本",
    )

    facade_position = QPoint(2, max(2, field.height() // 2))
    QTest.mousePress(field, Qt.MouseButton.LeftButton, pos=facade_position)
    expect(
        field.interaction_state is NumericEditState.TEXT_SELECTING,
        "二次按下只能进入文本选择",
    )
    QTest.mouseMove(field, QPoint(max(20, field.width() - 4), facade_position.y()))
    expect(field.interaction_state not in _DRAG_STATES, "二次拖选阻断数值拖动")
    QTest.mouseRelease(
        field,
        Qt.MouseButton.LeftButton,
        pos=QPoint(max(20, field.width() - 4), facade_position.y()),
    )
    expect(field.interaction_state is NumericEditState.TEXT_EDITING, "二次释放返回文本")

    editor = field.lineEdit()
    editor_y = max(2, editor.height() // 2)
    QTest.mousePress(editor, Qt.MouseButton.LeftButton, pos=QPoint(8, editor_y))
    expect(
        field.interaction_state is NumericEditState.TEXT_SELECTING,
        "编辑器按下进入文本选择",
    )
    QTest.mouseMove(editor, QPoint(max(20, editor.width() - 8), editor_y))
    expect(field.interaction_state not in _DRAG_STATES, "编辑器拖选阻断数值拖动")
    QTest.mouseRelease(
        editor,
        Qt.MouseButton.LeftButton,
        pos=QPoint(max(20, editor.width() - 8), editor_y),
    )
    expect(field.interaction_state is NumericEditState.TEXT_EDITING, "拖选释放返回文本")

    QTest.keyClick(editor, Qt.Key.Key_Escape)
    expect(field.interaction_state is NumericEditState.IDLE, "Esc 退出文本")

    drag_start = idle_surface.rect().center()
    drag_end = QPoint(
        drag_start.x() + DRAG_THRESHOLD + 12,
        drag_start.y(),
    )
    QTest.mousePress(idle_surface, Qt.MouseButton.LeftButton, pos=drag_start)
    expect(field.interaction_state is NumericEditState.WAIT_DRAG, "拖动按下等待")
    QTest.mouseMove(idle_surface, drag_end)
    expect(field.interaction_state is NumericEditState.DRAGGING, "空闲态允许数值拖动")
    QTest.mouseRelease(idle_surface, Qt.MouseButton.LeftButton, pos=drag_end)
    expect(field.interaction_state is NumericEditState.IDLE, "拖动释放提交")

    return AutomaticReport(name, not errors, checks, tuple(errors))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--auto-test",
        action="store_true",
        help="run the isolated interaction sequence and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    application = QApplication.instance()
    owns_application = application is None
    if application is None:
        application = QApplication(["easypicture-numeric-input-harness"])
    window = NumericInputHarness()
    window.show()
    if not args.auto_test:
        return application.exec() if owns_application else 0

    exit_code = 1

    def execute() -> None:
        nonlocal exit_code
        reports = window.run_automatic_checks()
        payload = {
            "passed": all(report.passed for report in reports),
            "reports": [report.as_dict() for report in reports],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        exit_code = 0 if payload["passed"] else 1
        application.quit()

    QTimer.singleShot(0, execute)
    application.exec()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AutomaticReport",
    "NumericInputHarness",
    "NumericInteractionProbe",
    "main",
    "run_automatic_sequence",
]
