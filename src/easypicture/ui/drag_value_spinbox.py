"""Blender-inspired integer input with explicit interaction states."""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QHideEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QValidator,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QSizePolicy,
    QWidget,
)

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_RANGE_FRACTION_PER_PIXEL = 0.01
DEFAULT_FINE_RANGE_FRACTION_PER_PIXEL = 0.001
FINE_DRAG_MULTIPLIER = 0.1
DRAG_THRESHOLD = 3
_EXPRESSION_CHARACTERS = re.compile(r"^[0-9eE+\-*/().\s]*$")

_BACKGROUND_COLOR = QColor("#1f1f1f")
_FILL_COLOR = QColor("#365f91")
_BORDER_COLOR = QColor("#151515")


@dataclass(frozen=True, slots=True)
class NumericDragSensitivity:
    """Application-wide numeric drag fractions of the property's soft range."""

    normal: float = DEFAULT_RANGE_FRACTION_PER_PIXEL
    fine: float = DEFAULT_FINE_RANGE_FRACTION_PER_PIXEL

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.normal)
            or not math.isfinite(self.fine)
            or self.normal <= 0.0
            or self.fine <= 0.0
        ):
            raise ValueError("numeric drag sensitivities must be finite and positive")


_numeric_drag_sensitivity = NumericDragSensitivity()


def configure_numeric_drag_sensitivity(normal: float, fine: float) -> None:
    """Apply application-wide drag sensitivity to default-configured fields."""

    global _numeric_drag_sensitivity
    _numeric_drag_sensitivity = NumericDragSensitivity(float(normal), float(fine))


def numeric_drag_sensitivity() -> NumericDragSensitivity:
    return _numeric_drag_sensitivity


class NumericEditState(Enum):
    """Mutually exclusive states for a numeric-field interaction."""

    IDLE = "idle"
    WAIT_DRAG = "wait_drag"
    DRAGGING = "dragging"
    TEXT_EDITING = "text_editing"
    TEXT_SELECTING = "text_selecting"
    WAIT_ENTER_RELEASE = "wait_enter_release"
    WAIT_RIGHT_RELEASE = "wait_right_release"
    WAIT_CONTEXT_MENU = "wait_context_menu"


@dataclass(frozen=True, slots=True)
class NumericPropertySpec:
    """Ranges and presentation metadata for an integer property."""

    soft_minimum: int
    soft_maximum: int
    hard_minimum: int
    hard_maximum: int
    step: int = 1
    suffix: str = ""
    range_fraction_per_pixel: float | None = None
    fine_range_fraction_per_pixel: float | None = None

    def __post_init__(self) -> None:
        if self.soft_minimum > self.soft_maximum:
            raise ValueError("soft minimum must not exceed soft maximum")
        if self.hard_minimum > self.soft_minimum:
            raise ValueError("hard minimum must not exceed soft minimum")
        if self.hard_maximum < self.soft_maximum:
            raise ValueError("hard maximum must not be less than soft maximum")
        if self.step <= 0:
            raise ValueError("step must be positive")
        if (
            self.range_fraction_per_pixel is not None
            and self.range_fraction_per_pixel <= 0.0
        ):
            raise ValueError("range fraction per pixel must be positive")
        if (
            self.fine_range_fraction_per_pixel is not None
            and self.fine_range_fraction_per_pixel <= 0.0
        ):
            raise ValueError("fine range fraction per pixel must be positive")


@dataclass(slots=True)
class _InteractionSession:
    start_value: int
    press_position: QPoint
    drag_anchor_value: float
    cursor_lock_position: QPoint | None = None
    fine_drag: bool = False


@dataclass(slots=True)
class _CancelCleanup:
    left_release_pending: bool
    right_release_pending: bool = True
    context_menu_pending: bool = True


class _GlobalInteractionFilter(QObject):
    def __init__(self, owner: DragValueSpinBox) -> None:
        super().__init__(owner)
        self._owner = owner

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        return self._owner._route_application_event(watched, event)


class _NumericLineEdit(QLineEdit):
    """Text surface whose pointer transitions belong to the numeric session."""

    def __init__(self, owner: DragValueSpinBox) -> None:
        super().__init__(owner)
        self._owner = owner

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner._begin_text_selection()  # noqa: SLF001
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner._begin_text_selection()  # noqa: SLF001
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner._finish_text_selection()  # noqa: SLF001


class DragValueSpinBox(QWidget):
    """Edit an integer as text or by horizontally dragging its soft range."""

    valueChanged = Signal(int)
    editingFinished = Signal()
    interactionStarted = Signal()
    interactionCanceled = Signal()
    interactionStateChanged = Signal(object)

    def __init__(
        self,
        drag_minimum: int,
        drag_maximum: int,
        value: int,
        *,
        manual_minimum: int | None = None,
        manual_maximum: int | None = None,
        suffix: str = "",
        range_fraction_per_pixel: float | None = None,
        fine_range_fraction_per_pixel: float | None = None,
        display_divisor: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if display_divisor <= 0:
            raise ValueError("display divisor must be positive")
        self._display_divisor = display_divisor
        hard_minimum = drag_minimum if manual_minimum is None else manual_minimum
        hard_maximum = drag_maximum if manual_maximum is None else manual_maximum
        self._spec = NumericPropertySpec(
            soft_minimum=drag_minimum,
            soft_maximum=drag_maximum,
            hard_minimum=hard_minimum,
            hard_maximum=hard_maximum,
            suffix=suffix,
            range_fraction_per_pixel=range_fraction_per_pixel,
            fine_range_fraction_per_pixel=fine_range_fraction_per_pixel,
        )
        self._value = self._clamp_hard(value)
        self._state = NumericEditState.IDLE
        self._session: _InteractionSession | None = None
        self._cancel_cleanup: _CancelCleanup | None = None
        self._cursor_hidden = False
        self._forwarding_editor_event = False
        self._global_filter_installed = False
        self._global_filter = _GlobalInteractionFilter(self)

        self._editor = _NumericLineEdit(self)
        self._editor.setObjectName("DragValueEditor")
        self._editor.setFrame(False)
        self._editor.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._editor.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self._editor.setStyleSheet(
            """
            QLineEdit {
                color: #eeeeee;
                background: transparent;
                border: 0;
                padding: 0 5px;
                margin: 0;
                selection-background-color: #5d88c7;
                selection-color: #ffffff;
            }
            """
        )

        # Once text editing owns the interaction, even a click on the narrow
        # painted margin must keep keyboard focus in the editor.  Without a
        # focus proxy QWidget's default mouse handling focuses the outer drag
        # surface; the next key then commits the session and a following click
        # starts a new drag.
        self.setFocusProxy(self._editor)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(25)
        self.setToolTip(
            "左右拖动按范围比例修改；按住 Shift 精细调整；"
            "单击后全选并可输入表达式；右键撤销"
        )
        self._restore_display_text()
        self._sync_editor_mode()

    @property
    def interaction_state(self) -> NumericEditState:
        return self._state

    @property
    def property_spec(self) -> NumericPropertySpec:
        return self._spec

    @property
    def drag_minimum(self) -> int:
        return self._spec.soft_minimum

    @property
    def drag_maximum(self) -> int:
        return self._spec.soft_maximum

    @property
    def range_fraction_per_pixel(self) -> float:
        configured = numeric_drag_sensitivity()
        return (
            configured.normal
            if self._spec.range_fraction_per_pixel is None
            else self._spec.range_fraction_per_pixel
        )

    @property
    def fine_range_fraction_per_pixel(self) -> float:
        if self._spec.fine_range_fraction_per_pixel is not None:
            return self._spec.fine_range_fraction_per_pixel
        if self._spec.range_fraction_per_pixel is not None:
            return self._spec.range_fraction_per_pixel * FINE_DRAG_MULTIPLIER
        return numeric_drag_sensitivity().fine

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:
        """Set a hard-range-clamped value, cancelling an active interaction."""

        if self._state is not NumericEditState.IDLE:
            self._abort_interaction(restore_value=False)
        self._set_value_internal(value)
        self._restore_display_text()

    def syncValue(self, value: int) -> bool:
        """Apply model feedback without interrupting an active user interaction.

        ``setValue()`` deliberately remains the authoritative/force-update API.
        Views that mirror a value which originated from this control must use
        this method so their feedback loop cannot cancel dragging or text input.
        """

        if self._state is not NumericEditState.IDLE:
            return False
        self.setValue(value)
        return True

    def minimum(self) -> int:
        return self._spec.hard_minimum

    def maximum(self) -> int:
        return self._spec.hard_maximum

    def setMinimum(self, minimum: int) -> None:
        self.setRange(minimum, self.maximum())

    def setMaximum(self, maximum: int) -> None:
        self.setRange(self.minimum(), maximum)

    def setRange(self, minimum: int, maximum: int) -> None:
        if minimum > self.drag_minimum or maximum < self.drag_maximum:
            raise ValueError("hard range must contain the soft drag range")
        self._spec = replace(
            self._spec,
            hard_minimum=minimum,
            hard_maximum=maximum,
        )
        self.setValue(self._value)

    def setPropertyRange(
        self,
        drag_minimum: int,
        drag_maximum: int,
        *,
        manual_minimum: int | None = None,
        manual_maximum: int | None = None,
    ) -> None:
        """Atomically replace soft and hard limits for a dynamic property."""
        hard_minimum = drag_minimum if manual_minimum is None else manual_minimum
        hard_maximum = drag_maximum if manual_maximum is None else manual_maximum
        self._spec = replace(
            self._spec,
            soft_minimum=drag_minimum,
            soft_maximum=drag_maximum,
            hard_minimum=hard_minimum,
            hard_maximum=hard_maximum,
        )
        self.setValue(self._value)

    def syncPropertyRange(
        self,
        drag_minimum: int,
        drag_maximum: int,
        *,
        manual_minimum: int | None = None,
        manual_maximum: int | None = None,
    ) -> bool:
        """Update a model-derived range only while the control is idle."""

        if self._state is not NumericEditState.IDLE:
            return False
        self.setPropertyRange(
            drag_minimum,
            drag_maximum,
            manual_minimum=manual_minimum,
            manual_maximum=manual_maximum,
        )
        return True

    def suffix(self) -> str:
        return self._spec.suffix

    def setSuffix(self, suffix: str) -> None:
        self._spec = replace(self._spec, suffix=suffix)
        if not self._is_text_active:
            self._restore_display_text()

    def lineEdit(self) -> QLineEdit:
        return self._editor

    def drag_delta_for_pixels(self, pixels: int, *, fine: bool = False) -> float:
        """Return the value delta for a horizontal drag distance."""

        extent = self.drag_maximum - self.drag_minimum
        fraction = (
            self.fine_range_fraction_per_pixel
            if fine
            else self.range_fraction_per_pixel
        )
        return pixels * extent * fraction

    def validate(
        self,
        input_text: str,
        position: int,
    ) -> tuple[QValidator.State, str, int]:
        expression = self._expression_text(input_text)
        if not _EXPRESSION_CHARACTERS.fullmatch(expression):
            return QValidator.State.Invalid, input_text, position
        if not expression:
            return QValidator.State.Intermediate, input_text, position
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError:
            return QValidator.State.Intermediate, input_text, position
        try:
            self._evaluate_expression_tree(tree)
        except (ArithmeticError, ValueError):
            return QValidator.State.Invalid, input_text, position
        return QValidator.State.Acceptable, input_text, position

    def valueFromText(self, text: str) -> int:
        result = self._evaluate_text(text)
        return self._value if result is None else result

    def sizeHint(self) -> QSize:
        content = f"{self.maximum()}{self.suffix()}"
        width = max(84, self.fontMetrics().horizontalAdvance(content) + 16)
        return QSize(width, 25)

    def minimumSizeHint(self) -> QSize:
        return QSize(48, 25)

    def resizeEvent(self, event: QResizeEvent) -> None:
        # The editor owns the complete hit area in text mode.  Its stylesheet
        # supplies the visual padding, so there are no painted edge strips
        # that can route a second click back to the drag surface.
        self._editor.setGeometry(self.rect())
        super().resizeEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bounds = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(bounds, 2.0, 2.0)

        painter.fillPath(path, _BACKGROUND_COLOR)
        if not self._is_text_active:
            ratio = self._fill_ratio()
            if ratio > 0.0:
                painter.save()
                painter.setClipPath(path)
                fill_rect = QRectF(bounds)
                fill_rect.setWidth(bounds.width() * ratio)
                painter.fillRect(fill_rect, _FILL_COLOR)
                painter.restore()

        painter.setPen(QPen(_BORDER_COLOR, 1.0))
        painter.drawPath(path)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._state is NumericEditState.IDLE
            and self.isEnabled()
        ):
            position = event.globalPosition().toPoint()
            self._session = _InteractionSession(
                start_value=self._value,
                press_position=position,
                drag_anchor_value=float(self._value),
            )
            self.interactionStarted.emit()
            self._set_state(NumericEditState.WAIT_DRAG)
            self.grabMouse()
            event.accept()
            return
        if (
            event.button() == Qt.MouseButton.RightButton
            and self._state is not NumericEditState.IDLE
        ):
            self._begin_right_cancel()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._state not in (
            NumericEditState.WAIT_DRAG,
            NumericEditState.DRAGGING,
        ):
            super().mouseMoveEvent(event)
            return
        if not event.buttons() & Qt.MouseButton.LeftButton:
            # Cursor warping and a completed click can leave a queued hover
            # move behind.  Such an event must never turn the next click into
            # a drag interaction.
            event.accept()
            return
        session = self._session
        if session is None:
            self._set_state(NumericEditState.IDLE)
            return

        current_position = event.globalPosition().toPoint()
        if self._state is NumericEditState.WAIT_DRAG:
            if abs(current_position.x() - session.press_position.x()) < DRAG_THRESHOLD:
                event.accept()
                return
            session.cursor_lock_position = QPoint(session.press_position)
            session.fine_drag = bool(
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            )
            self._hide_cursor()
            self._set_state(NumericEditState.DRAGGING)

        lock_position = session.cursor_lock_position or session.press_position
        delta_x = current_position.x() - lock_position.x()
        fine_drag = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        session.fine_drag = fine_drag
        candidate = session.drag_anchor_value + self.drag_delta_for_pixels(
            delta_x,
            fine=fine_drag,
        )
        candidate = max(self.drag_minimum, min(self.drag_maximum, candidate))
        session.drag_anchor_value = candidate
        self._set_value_internal(round(candidate))
        if current_position != lock_position:
            QCursor.setPos(lock_position)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._state is NumericEditState.WAIT_DRAG:
            self._release_mouse()
            self._set_state(NumericEditState.TEXT_EDITING)
            self._editor.setFocus(Qt.FocusReason.MouseFocusReason)
            self._editor.selectAll()
            event.accept()
            return
        if self._state is NumericEditState.DRAGGING:
            self._finish_drag(commit=True)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            self._state is NumericEditState.IDLE
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            self._session = _InteractionSession(
                start_value=self._value,
                press_position=QCursor.pos(),
                drag_anchor_value=float(self._value),
            )
            self.interactionStarted.emit()
            self._set_state(NumericEditState.TEXT_EDITING)
            self._editor.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self._editor.selectAll()
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        if self._state is not NumericEditState.IDLE:
            self._abort_interaction(restore_value=True)
        super().hideEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        if (
            event.type() == QEvent.Type.EnabledChange
            and not self.isEnabled()
            and self._state is not NumericEditState.IDLE
        ):
            self._abort_interaction(restore_value=True)
        super().changeEvent(event)

    def _route_application_event(self, watched: QObject, event: QEvent) -> bool:
        if self._forwarding_editor_event:
            return False
        state = self._state
        if state is NumericEditState.IDLE:
            return False

        if event.type() == QEvent.Type.WindowDeactivate:
            if self._is_text_active:
                self._commit_text()
            else:
                self._abort_interaction(restore_value=True)
            return False

        if state is NumericEditState.WAIT_ENTER_RELEASE:
            return self._route_wait_enter_release(event)
        if state in (
            NumericEditState.WAIT_RIGHT_RELEASE,
            NumericEditState.WAIT_CONTEXT_MENU,
        ):
            return self._route_cancel_cleanup(event)

        if isinstance(event, QKeyEvent):
            if (
                self._is_text_active
                and event.type() == QEvent.Type.ShortcutOverride
                and self._belongs_to_input(watched)
            ):
                # While the line edit owns the session, standard editing keys
                # such as Ctrl+A and characters that are also application
                # shortcuts (for example "-" for zoom) belong to the text.
                event.accept()
                return True
            if (
                event.type() == QEvent.Type.ShortcutOverride
                and event.key()
                in (
                    Qt.Key.Key_Return,
                    Qt.Key.Key_Enter,
                    Qt.Key.Key_Escape,
                    Qt.Key.Key_Tab,
                    Qt.Key.Key_Backtab,
                )
            ):
                event.accept()
                return True
            if event.type() == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    if self._is_text_active:
                        self._commit_text(wait_for_enter_release=True)
                        return True
                if event.key() == Qt.Key.Key_Escape:
                    self._abort_interaction(restore_value=True)
                    return True
                if (
                    self._is_text_active
                    and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)
                ):
                    backwards = event.key() == Qt.Key.Key_Backtab or bool(
                        event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                    )
                    self._commit_text()
                    self.focusNextPrevChild(not backwards)
                    return True
                if (
                    self._is_text_active
                    and not self._belongs_to_input(watched)
                    and self._keyboard_focus_is_outside()
                ):
                    self._commit_text()
                    return False

        if isinstance(event, QMouseEvent):
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.RightButton
            ):
                self._begin_right_cancel()
                return True
            if (
                self._is_text_active
                and event.type()
                in (
                    QEvent.Type.MouseButtonPress,
                    QEvent.Type.MouseButtonDblClick,
                )
                and event.button() == Qt.MouseButton.LeftButton
            ):
                if self._mouse_event_hits_input(event):
                    if self._belongs_to_editor(watched):
                        return False
                    return self._forward_event_to_editor(event)
                self._commit_text()
                return False
            if (
                self._state is NumericEditState.TEXT_SELECTING
                and event.type()
                in (
                    QEvent.Type.MouseMove,
                    QEvent.Type.MouseButtonRelease,
                )
                and not self._belongs_to_editor(watched)
            ):
                return self._forward_event_to_editor(event)

        return False

    def _route_wait_enter_release(self, event: QEvent) -> bool:
        if isinstance(event, QKeyEvent) and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            if event.type() in (
                QEvent.Type.ShortcutOverride,
                QEvent.Type.KeyPress,
                QEvent.Type.KeyRelease,
            ):
                if event.type() == QEvent.Type.KeyRelease:
                    self._set_state(NumericEditState.IDLE)
                event.accept()
                return True
        if isinstance(event, (QKeyEvent, QMouseEvent)):
            self._set_state(NumericEditState.IDLE)
        return False

    def _route_cancel_cleanup(self, event: QEvent) -> bool:
        cleanup = self._cancel_cleanup
        if cleanup is None:
            self._set_state(NumericEditState.IDLE)
            return False

        if event.type() == QEvent.Type.ContextMenu:
            cleanup.context_menu_pending = False
            self._finish_cancel_cleanup_if_ready()
            return True
        if isinstance(event, QMouseEvent):
            if event.type() == QEvent.Type.MouseButtonRelease:
                if event.button() == Qt.MouseButton.RightButton:
                    cleanup.right_release_pending = False
                    self._set_state(NumericEditState.WAIT_CONTEXT_MENU)
                    self._finish_cancel_cleanup_if_ready()
                    return True
                if (
                    event.button() == Qt.MouseButton.LeftButton
                    and cleanup.left_release_pending
                ):
                    cleanup.left_release_pending = False
                    self._finish_cancel_cleanup_if_ready()
                    return True
            if event.type() == QEvent.Type.MouseButtonPress:
                cleanup.context_menu_pending = False
                cleanup.left_release_pending = False
                cleanup.right_release_pending = False
                self._finish_cancel_cleanup_if_ready()
                return False
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            cleanup.context_menu_pending = False
            cleanup.left_release_pending = False
            cleanup.right_release_pending = False
            self._finish_cancel_cleanup_if_ready()
        return False

    def _set_state(self, state: NumericEditState) -> None:
        if state is self._state:
            return
        self._state = state
        self._sync_global_filter()
        self._sync_editor_mode()
        self.update()
        self.interactionStateChanged.emit(state)

    @property
    def _is_text_active(self) -> bool:
        return self._state in (
            NumericEditState.TEXT_EDITING,
            NumericEditState.TEXT_SELECTING,
        )

    def _sync_editor_mode(self) -> None:
        text_editing = self._is_text_active
        self._editor.setReadOnly(not text_editing)
        self._editor.setFocusPolicy(
            Qt.FocusPolicy.StrongFocus if text_editing else Qt.FocusPolicy.NoFocus
        )
        self._editor.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            not text_editing,
        )
        self._editor.setCursor(
            Qt.CursorShape.IBeamCursor
            if text_editing
            else Qt.CursorShape.SizeHorCursor
        )
        self.setCursor(
            Qt.CursorShape.IBeamCursor
            if text_editing
            else Qt.CursorShape.SizeHorCursor
        )
        if not text_editing:
            self._editor.deselect()

    def _sync_global_filter(self) -> None:
        required = self._state is not NumericEditState.IDLE
        application = QApplication.instance()
        if application is None or required == self._global_filter_installed:
            return
        if required:
            application.installEventFilter(self._global_filter)
        else:
            application.removeEventFilter(self._global_filter)
        self._global_filter_installed = required

    def _set_value_internal(self, value: int) -> bool:
        resolved = self._clamp_hard(value)
        if resolved == self._value:
            return False
        self._value = resolved
        if not self._is_text_active:
            self._restore_display_text()
        self.valueChanged.emit(resolved)
        self.update()
        return True

    def _clamp_hard(self, value: int) -> int:
        return max(self.minimum(), min(self.maximum(), int(value)))

    def _fill_ratio(self) -> float:
        extent = self.drag_maximum - self.drag_minimum
        if extent <= 0:
            return 1.0
        return max(
            0.0,
            min(1.0, (self._value - self.drag_minimum) / float(extent)),
        )

    def _format_value(self, value: int) -> str:
        if self._display_divisor == 1:
            formatted = str(value)
        else:
            decimals = max(0, round(math.log10(self._display_divisor)))
            formatted = f"{value / self._display_divisor:.{decimals}f}"
        return f"{formatted}{self.suffix()}"

    def _restore_display_text(self) -> None:
        self._editor.setText(self._format_value(self._value))

    def _commit_text(self, *, wait_for_enter_release: bool = False) -> None:
        if not self._is_text_active:
            return
        session = self._session
        result = self._evaluate_text(self._editor.text())
        valid = result is not None
        if result is not None:
            self._set_value_internal(result)
        elif session is not None:
            self._set_value_internal(session.start_value)
        self._session = None
        self._release_mouse()
        self._restore_cursor()
        self._restore_display_text()
        self._set_state(
            NumericEditState.WAIT_ENTER_RELEASE
            if wait_for_enter_release
            else NumericEditState.IDLE
        )
        self.clearFocus()
        if valid:
            self.editingFinished.emit()
        else:
            self.interactionCanceled.emit()

    def _finish_drag(self, *, commit: bool) -> None:
        if self._state is not NumericEditState.DRAGGING:
            return
        session = self._session
        if not commit and session is not None:
            self._set_value_internal(session.start_value)
        self._release_mouse()
        self._restore_cursor()
        self._session = None
        self._restore_display_text()
        self._set_state(NumericEditState.IDLE)
        self.clearFocus()
        if commit:
            self.editingFinished.emit()

    def _abort_interaction(self, *, restore_value: bool) -> None:
        session = self._session
        had_interaction = session is not None
        if restore_value and session is not None:
            self._set_value_internal(session.start_value)
        self._release_mouse()
        self._restore_cursor()
        self._session = None
        self._cancel_cleanup = None
        self._restore_display_text()
        self._set_state(NumericEditState.IDLE)
        self.clearFocus()
        if had_interaction:
            self.interactionCanceled.emit()

    def _begin_right_cancel(self) -> None:
        left_pending = self._state in (
            NumericEditState.WAIT_DRAG,
            NumericEditState.DRAGGING,
            NumericEditState.TEXT_SELECTING,
        )
        session = self._session
        if session is not None:
            self._set_value_internal(session.start_value)
        self._release_mouse()
        self._restore_cursor()
        self._session = None
        self._restore_display_text()
        self._cancel_cleanup = _CancelCleanup(left_release_pending=left_pending)
        self._set_state(NumericEditState.WAIT_RIGHT_RELEASE)
        self.clearFocus()
        self.interactionCanceled.emit()

    def _finish_cancel_cleanup_if_ready(self) -> None:
        cleanup = self._cancel_cleanup
        if cleanup is None:
            self._set_state(NumericEditState.IDLE)
            return
        if (
            cleanup.left_release_pending
            or cleanup.right_release_pending
            or cleanup.context_menu_pending
        ):
            return
        self._cancel_cleanup = None
        self._set_state(NumericEditState.IDLE)

    def _hide_cursor(self) -> None:
        if self._cursor_hidden:
            return
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.BlankCursor))
        self._cursor_hidden = True

    def _restore_cursor(self) -> None:
        session = self._session
        lock_position = (
            None if session is None else session.cursor_lock_position
        )
        if self._cursor_hidden:
            QApplication.restoreOverrideCursor()
            self._cursor_hidden = False
        if lock_position is not None:
            QCursor.setPos(lock_position)

    def _release_mouse(self) -> None:
        grabber = QWidget.mouseGrabber()
        if grabber is self:
            self.releaseMouse()
        elif grabber is self._editor:
            self._editor.releaseMouse()

    def _begin_text_selection(self) -> None:
        if self._state is NumericEditState.TEXT_EDITING:
            self._set_state(NumericEditState.TEXT_SELECTING)

    def _finish_text_selection(self) -> None:
        if self._state is NumericEditState.TEXT_SELECTING:
            self._set_state(NumericEditState.TEXT_EDITING)

    def _forward_event_to_editor(self, event: QMouseEvent) -> bool:
        """Route an active text gesture through the one editing surface."""

        local_position = QPointF(
            self._editor.mapFromGlobal(event.globalPosition().toPoint())
        )
        forwarded = QMouseEvent(
            event.type(),
            local_position,
            event.globalPosition(),
            event.button(),
            event.buttons(),
            event.modifiers(),
        )
        self._forwarding_editor_event = True
        try:
            QApplication.sendEvent(self._editor, forwarded)
        finally:
            self._forwarding_editor_event = False
        event.accept()
        return True

    def _belongs_to_editor(self, watched: QObject) -> bool:
        current: QObject | None = watched
        while current is not None:
            if current is self._editor:
                return True
            current = current.parent()
        return False

    def _mouse_event_hits_input(self, event: QMouseEvent) -> bool:
        position = self.mapFromGlobal(event.globalPosition().toPoint())
        return self.rect().contains(position)

    def _belongs_to_input(self, watched: QObject) -> bool:
        current: QObject | None = watched
        while current is not None:
            if current is self:
                return True
            if (
                isinstance(current, QWidget)
                and current.focusProxy() is self
            ):
                return True
            current = current.parent()
        return False

    def _keyboard_focus_is_outside(self) -> bool:
        focus_widget = QApplication.focusWidget()
        return focus_widget is None or not self._belongs_to_input(focus_widget)

    def _expression_text(self, text: str) -> str:
        expression = text.strip()
        suffix = self.suffix().strip()
        if suffix and expression.endswith(suffix):
            expression = expression[: -len(suffix)].strip()
        return expression

    def _evaluate_text(self, text: str) -> int | None:
        expression = self._expression_text(text)
        if not expression or not _EXPRESSION_CHARACTERS.fullmatch(expression):
            return None
        try:
            tree = ast.parse(expression, mode="eval")
            result = self._evaluate_expression_tree(tree)
        except (ArithmeticError, SyntaxError, ValueError):
            return None
        return self._clamp_hard(round(result * self._display_divisor))

    @classmethod
    def _evaluate_expression_tree(cls, tree: ast.Expression) -> float:
        if sum(1 for _node in ast.walk(tree)) > 64:
            raise ValueError("expression is too complex")

        binary_operations: dict[type[ast.operator], Callable[[float, float], float]] = {
            ast.Add: lambda left, right: left + right,
            ast.Sub: lambda left, right: left - right,
            ast.Mult: lambda left, right: left * right,
            ast.Div: lambda left, right: left / right,
        }

        def evaluate(node: ast.AST) -> float:
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool)
            ):
                result = float(node.value)
            elif isinstance(node, ast.UnaryOp) and isinstance(
                node.op,
                (ast.UAdd, ast.USub),
            ):
                operand = evaluate(node.operand)
                result = operand if isinstance(node.op, ast.UAdd) else -operand
            elif isinstance(node, ast.BinOp):
                operation = binary_operations.get(type(node.op))
                if operation is None:
                    raise ValueError("operator is not allowed")
                result = operation(evaluate(node.left), evaluate(node.right))
            else:
                raise ValueError("expression element is not allowed")
            if not math.isfinite(result):
                raise ArithmeticError("result is not finite")
            return result

        return evaluate(tree.body)


class DragValueDoubleSpinBox(QWidget):
    """Floating-point facade over the proven integer interaction state machine."""

    valueChanged = Signal(float)
    editingFinished = Signal()
    interactionStarted = Signal()
    interactionCanceled = Signal()
    interactionStateChanged = Signal(object)

    def __init__(
        self,
        drag_minimum: float,
        drag_maximum: float,
        value: float,
        *,
        manual_minimum: float | None = None,
        manual_maximum: float | None = None,
        suffix: str = "",
        decimals: int = 2,
        range_fraction_per_pixel: float | None = None,
        fine_range_fraction_per_pixel: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not 0 <= decimals <= 6:
            raise ValueError("decimals must be between 0 and 6")
        self._factor: int = 10**decimals
        self._field: DragValueSpinBox = DragValueSpinBox(
            round(drag_minimum * self._factor),
            round(drag_maximum * self._factor),
            round(value * self._factor),
            manual_minimum=(
                None if manual_minimum is None else round(manual_minimum * self._factor)
            ),
            manual_maximum=(
                None if manual_maximum is None else round(manual_maximum * self._factor)
            ),
            suffix=suffix,
            range_fraction_per_pixel=range_fraction_per_pixel,
            fine_range_fraction_per_pixel=fine_range_fraction_per_pixel,
            display_divisor=self._factor,
            parent=self,
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._field)
        self.setFocusProxy(self._field)
        self.setSizePolicy(self._field.sizePolicy())
        self._field.valueChanged.connect(
            lambda scaled: self.valueChanged.emit(scaled / self._factor)
        )
        self._field.editingFinished.connect(self.editingFinished)
        self._field.interactionStarted.connect(self.interactionStarted)
        self._field.interactionCanceled.connect(self.interactionCanceled)
        self._field.interactionStateChanged.connect(self.interactionStateChanged)

    @property
    def interaction_state(self) -> NumericEditState:
        return self._field.interaction_state

    @property
    def drag_minimum(self) -> float:
        return float(self._field.drag_minimum) / self._factor

    @property
    def drag_maximum(self) -> float:
        return float(self._field.drag_maximum) / self._factor

    def value(self) -> float:
        return float(self._field.value()) / self._factor

    def setValue(self, value: float) -> None:
        self._field.setValue(round(value * self._factor))

    def syncValue(self, value: float) -> bool:
        return self._field.syncValue(round(value * self._factor))

    def minimum(self) -> float:
        return float(self._field.minimum()) / self._factor

    def maximum(self) -> float:
        return float(self._field.maximum()) / self._factor

    def setRange(self, minimum: float, maximum: float) -> None:
        self._field.setRange(
            round(minimum * self._factor),
            round(maximum * self._factor),
        )

    def setPropertyRange(
        self,
        drag_minimum: float,
        drag_maximum: float,
        *,
        manual_minimum: float | None = None,
        manual_maximum: float | None = None,
    ) -> None:
        self._field.setPropertyRange(
            round(drag_minimum * self._factor),
            round(drag_maximum * self._factor),
            manual_minimum=(
                None if manual_minimum is None else round(manual_minimum * self._factor)
            ),
            manual_maximum=(
                None if manual_maximum is None else round(manual_maximum * self._factor)
            ),
        )

    def syncPropertyRange(
        self,
        drag_minimum: float,
        drag_maximum: float,
        *,
        manual_minimum: float | None = None,
        manual_maximum: float | None = None,
    ) -> bool:
        return self._field.syncPropertyRange(
            round(drag_minimum * self._factor),
            round(drag_maximum * self._factor),
            manual_minimum=(
                None if manual_minimum is None else round(manual_minimum * self._factor)
            ),
            manual_maximum=(
                None if manual_maximum is None else round(manual_maximum * self._factor)
            ),
        )

    def suffix(self) -> str:
        return self._field.suffix()

    def setSuffix(self, suffix: str) -> None:
        self._field.setSuffix(suffix)

    def lineEdit(self) -> QLineEdit:
        return self._field.lineEdit()

    def drag_delta_for_pixels(self, pixels: int, *, fine: bool = False) -> float:
        return float(self._field.drag_delta_for_pixels(pixels, fine=fine)) / self._factor

    def validate(
        self,
        text: str,
        position: int,
    ) -> tuple[QValidator.State, str, int]:
        return self._field.validate(text, position)

    def valueFromText(self, text: str) -> float:
        return float(self._field.valueFromText(text)) / self._factor


__all__ = [
    "DEFAULT_FINE_RANGE_FRACTION_PER_PIXEL",
    "DEFAULT_RANGE_FRACTION_PER_PIXEL",
    "DRAG_THRESHOLD",
    "DragValueDoubleSpinBox",
    "DragValueSpinBox",
    "FINE_DRAG_MULTIPLIER",
    "NumericDragSensitivity",
    "NumericEditState",
    "NumericPropertySpec",
    "configure_numeric_drag_sensitivity",
    "numeric_drag_sensitivity",
]
