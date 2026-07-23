from __future__ import annotations

import time
from threading import Event

from easypicture.jobs import JobRunner, JobStatus
from easypicture.jobs.runner import CancellationToken, JobCancelled


def test_cancellation_token_raises_after_cancel() -> None:
    token = CancellationToken()
    assert not token.cancelled

    token.cancel()

    assert token.cancelled
    try:
        token.raise_if_cancelled()
    except JobCancelled:
        pass
    else:
        raise AssertionError("cancelled token did not raise")


def test_job_status_values_are_stable() -> None:
    assert JobStatus.SUCCESS.value == "success"
    assert JobStatus.CANCELLED.value == "cancelled"
    assert JobStatus.FAILED.value == "failed"


def test_job_runner_reports_success_on_main_thread(qtbot) -> None:
    runner = JobRunner()
    handle = runner.submit(lambda _token, _progress: 42)

    with qtbot.waitSignal(handle.finished, timeout=2000) as blocker:
        pass

    outcome = blocker.args[0]
    assert outcome.status is JobStatus.SUCCESS
    assert outcome.value == 42


def test_job_runner_cooperatively_cancels(qtbot) -> None:
    runner = JobRunner()
    started = Event()

    def operation(token: CancellationToken, _progress) -> None:
        started.set()
        while True:
            token.raise_if_cancelled()
            time.sleep(0.001)

    handle = runner.submit(operation)
    qtbot.waitUntil(started.is_set, timeout=2000)

    with qtbot.waitSignal(handle.finished, timeout=2000) as blocker:
        handle.cancel()

    assert blocker.args[0].status is JobStatus.CANCELLED
