"""Serial latest-request scheduling for one interactive preview session."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from PySide6.QtCore import QObject, QTimer

from easypicture.jobs.runner import (
    CancellationToken,
    JobHandle,
    JobOperation,
    JobOutcome,
    JobRunner,
    JobStatus,
)


@dataclass(frozen=True, slots=True)
class PreviewTarget:
    project_id: UUID
    document_id: UUID
    object_id: UUID
    dependencies: tuple[int, ...]
    generation: int


@dataclass(slots=True)
class _Request:
    handle: JobHandle
    operation: JobOperation
    target: PreviewTarget
    current_target: Callable[[], PreviewTarget | None]

    @property
    def current(self) -> bool:
        return not self.handle.token.cancelled and self.current_target() == self.target


class PreviewScheduler(QObject):
    """Keep at most one running operation and one replaceable pending request.

    Only the runner starts worker threads. All scheduling and signal delivery
    happens on the owning UI thread. A late successful worker result is converted
    to cancellation unless its complete target still matches the live session.
    """

    def __init__(self, runner: JobRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runner = runner
        self._running: tuple[_Request, JobHandle] | None = None
        self._pending: _Request | None = None
        self.discarded_results = 0

    @property
    def running(self) -> bool:
        return self._running is not None

    @property
    def pending_count(self) -> int:
        return int(self._pending is not None)

    def submit(
        self,
        operation: JobOperation,
        target: PreviewTarget,
        current_target: Callable[[], PreviewTarget | None],
    ) -> JobHandle:
        handle = JobHandle(uuid4(), CancellationToken())
        request = _Request(handle, operation, target, current_target)
        if self._pending is not None:
            self._cancel_pending()
        if self._running is None:
            self._start(request)
        else:
            old_request, worker = self._running
            old_request.handle.cancel()
            worker.cancel()
            self._pending = request
        return handle

    def cancel(self) -> None:
        self._cancel_pending()
        if self._running is not None:
            request, worker = self._running
            request.handle.cancel()
            worker.cancel()

    def _cancel_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            pending.handle.cancel()
            self._defer_cancelled(pending)

    def _defer_cancelled(self, request: _Request) -> None:
        # submit() callers connect signals after receiving the handle.
        QTimer.singleShot(
            0,
            self,
            lambda: request.handle.finished.emit(
                JobOutcome(request.handle.job_id, JobStatus.CANCELLED)
            ),
        )

    def _start(self, request: _Request) -> None:
        if not request.current:
            self._defer_cancelled(request)
            return
        worker = self._runner.submit(
            lambda _token, progress: request.operation(request.handle.token, progress)
        )
        self._running = request, worker
        worker.progress.connect(
            lambda value, maximum, message: self._progress(request, value, maximum, message)
        )
        worker.finished.connect(lambda outcome: self._finished(request, outcome))

    def _progress(self, request: _Request, value: int, maximum: int, message: str) -> None:
        if request.current:
            request.handle.progress.emit(value, maximum, message)

    def _finished(self, request: _Request, outcome: JobOutcome) -> None:
        if self._running is None or self._running[0] is not request:
            return
        self._running = None
        pending, self._pending = self._pending, None
        current = request.current
        if not current:
            self.discarded_results += 1
        result = JobOutcome(
            request.handle.job_id,
            outcome.status if current else JobStatus.CANCELLED,
            outcome.value if current else None,
            outcome.error if current else None,
        )
        # Install the latest pending request before callbacks can submit more.
        if pending is not None:
            self._start(pending)
        request.handle.finished.emit(result)
