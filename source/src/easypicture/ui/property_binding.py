"""Reusable, model-independent property transactions and feedback bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID
from weakref import WeakKeyDictionary

from PySide6.QtWidgets import QWidget


@dataclass(frozen=True)
class EditTarget:
    document_id: UUID
    object_id: UUID
    revision: int


class PropertyEditSession[T]:
    """Preview privately and commit at most once against the original target."""

    def __init__(
        self,
        current_target: Callable[[], EditTarget | None],
        publish: Callable[[T], None],
        commit: Callable[[T], bool],
        clear: Callable[[], None],
        validate: Callable[[T], bool] = lambda _value: True,
    ) -> None:
        self._current_target = current_target
        self._publish = publish
        self._commit = commit
        self._clear = clear
        self._validate = validate
        self._valid = True
        self._target: EditTarget | None = None
        self._initial: T | None = None
        self._candidate: T | None = None

    @property
    def active(self) -> bool:
        return self._target is not None

    def begin(self, initial: T) -> bool:
        if self.active:
            self.cancel()
        self._target = self._current_target()
        self._initial = initial
        self._candidate = initial
        self._valid = self._validate(initial)
        return self.active

    def preview(self, value: T) -> bool:
        if not self.active:
            return False
        if self._target != self._current_target():
            self.cancel()
            return False
        self._candidate = value
        self._valid = self._validate(value)
        if not self._valid:
            return False
        self._publish(value)
        return True

    def finish(self) -> bool:
        current = self._target == self._current_target()
        candidate = self._candidate
        changed = candidate is not None and candidate != self._initial
        active = self.active
        valid = self._valid
        self._reset()
        self._clear()
        if active and current and changed and valid and candidate is not None:
            return self._commit(candidate)
        return False

    def reject(self) -> None:
        """Keep the last valid preview, but refuse to commit invalid current input."""
        self._valid = False

    def cancel(self) -> None:
        self._reset()
        self._clear()

    def _reset(self) -> None:
        self._target = None
        self._initial = None
        self._candidate = None
        self._valid = True


class PropertyBinding[T]:
    """Re-read authoritative feedback after an interaction, never replay stale values."""

    def __init__(
        self,
        read: Callable[[], T],
        synchronize: Callable[[T], bool],
        validate: Callable[[T], bool] = lambda _value: True,
    ) -> None:
        self.read = read
        self.synchronize = synchronize
        self.validate = validate
        self.pending = False

    def refresh(self) -> bool:
        value = self.read()
        accepted = self.validate(value) and self.synchronize(value)
        self.pending = not accepted
        return accepted

    def finish(self) -> None:
        if self.pending:
            self.refresh()


class InteractionCoordinator:
    """One interaction owner per window, with explicit lifecycle cancellation."""

    def __init__(self) -> None:
        self.owner: object | None = None
        self._cancel: Callable[[], None] | None = None
        self._finish: Callable[[], None] | None = None

    def acquire(
        self,
        owner: object,
        cancel: Callable[[], None],
        *,
        finish: Callable[[], None] | None = None,
    ) -> bool:
        if self.owner is not None and self.owner is not owner:
            if self._finish is None:
                return False
            self._finish()
            if self.owner is not None:
                return False
        self.owner = owner
        self._cancel = cancel
        self._finish = finish
        return True

    def release(self, owner: object) -> None:
        if self.owner is owner:
            self.owner = None
            self._cancel = None
            self._finish = None

    def cancel(self) -> None:
        callback = self._cancel
        self.owner = None
        self._cancel = None
        self._finish = None
        if callback is not None:
            callback()


_windows: WeakKeyDictionary[QWidget, InteractionCoordinator] = WeakKeyDictionary()


def interaction_coordinator(widget: QWidget) -> InteractionCoordinator:
    window = widget.window()
    coordinator = _windows.get(window)
    if coordinator is None:
        coordinator = InteractionCoordinator()
        _windows[window] = coordinator
    return coordinator
