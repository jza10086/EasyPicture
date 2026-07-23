"""One-worker Qt job runner with pure operation inputs and outcomes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from threading import Event
from typing import Any
from uuid import UUID, uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

type ProgressSink = Callable[[int, int, str], None]
type JobOperation = Callable[["CancellationToken", ProgressSink], Any]


class JobCancelled(Exception):
    """Cooperative cancellation sentinel."""


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelled


class JobStatus(Enum):
    SUCCESS = "success"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class JobOutcome:
    job_id: UUID
    status: JobStatus
    value: object | None = None
    error: BaseException | None = None


class JobHandle(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)

    def __init__(self, job_id: UUID, token: CancellationToken) -> None:
        super().__init__()
        self.job_id = job_id
        self.token = token

    def cancel(self) -> None:
        self.token.cancel()


class _WorkerSignals(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)


class _Worker(QRunnable):
    def __init__(
        self,
        job_id: UUID,
        token: CancellationToken,
        operation: JobOperation,
    ) -> None:
        super().__init__()
        self.job_id = job_id
        self.token = token
        self.operation = operation
        self.signals = _WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            self.token.raise_if_cancelled()
            value = self.operation(self.token, self.signals.progress.emit)
            self.token.raise_if_cancelled()
            outcome = JobOutcome(self.job_id, JobStatus.SUCCESS, value=value)
        except JobCancelled:
            outcome = JobOutcome(self.job_id, JobStatus.CANCELLED)
        except Exception as error:
            outcome = JobOutcome(self.job_id, JobStatus.FAILED, error=error)
        self.signals.finished.emit(outcome)


class JobRunner(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._workers: dict[UUID, _Worker] = {}

    def submit(self, operation: JobOperation) -> JobHandle:
        job_id = uuid4()
        token = CancellationToken()
        handle = JobHandle(job_id, token)
        worker = _Worker(job_id, token, operation)
        worker.signals.progress.connect(handle.progress)
        worker.signals.finished.connect(handle.finished)
        worker.signals.finished.connect(self._forget_worker)
        self._workers[job_id] = worker
        self._pool.start(worker)
        return handle

    @Slot(object)
    def _forget_worker(self, outcome: JobOutcome) -> None:
        self._workers.pop(outcome.job_id, None)


__all__ = [
    "CancellationToken",
    "JobCancelled",
    "JobHandle",
    "JobOperation",
    "JobOutcome",
    "JobRunner",
    "JobStatus",
    "ProgressSink",
]
