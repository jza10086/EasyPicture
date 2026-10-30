from dataclasses import replace
from threading import Event
from uuid import uuid4

from easypicture.jobs import JobRunner, JobStatus
from easypicture.jobs.preview_scheduler import PreviewScheduler, PreviewTarget


def test_scheduler_keeps_only_latest_pending_and_discards_old_success(qtbot):
    runner = JobRunner()
    scheduler = PreviewScheduler(runner)
    current = [PreviewTarget(uuid4(), uuid4(), uuid4(), (1,), 1)]
    started, release = Event(), Event()
    executed, outcomes = [], []

    def slow(_token, _progress):
        started.set()
        assert release.wait(5)
        executed.append(1)
        return 1

    first = scheduler.submit(slow, current[0], lambda: current[0])
    first.finished.connect(outcomes.append)
    qtbot.waitUntil(started.is_set)
    try:
        for generation in range(2, 21):
            current[0] = replace(current[0], generation=generation)
            handle = scheduler.submit(
                lambda _token, _progress, value=generation: executed.append(value) or value,
                current[0],
                lambda: current[0],
            )
            handle.finished.connect(outcomes.append)
            assert scheduler.pending_count == 1
        release.set()
        qtbot.waitUntil(lambda: len(outcomes) == 20, timeout=5000)
        assert executed == [1, 20]
        successes = [item.value for item in outcomes if item.status is JobStatus.SUCCESS]
        assert successes == [20]
        assert scheduler.discarded_results == 1
    finally:
        release.set()
        runner.wait_for_idle()


def test_cancelled_target_never_publishes_worker_result(qtbot):
    runner = JobRunner()
    scheduler = PreviewScheduler(runner)
    target = PreviewTarget(uuid4(), uuid4(), uuid4(), (0,), 1)
    current = [target]
    released = Event()
    outcomes = []
    handle = scheduler.submit(
        lambda _token, _progress: released.wait(5) or "result", target, lambda: current[0]
    )
    handle.finished.connect(outcomes.append)
    current[0] = replace(target, document_id=uuid4())
    released.set()
    qtbot.waitUntil(lambda: bool(outcomes), timeout=5000)
    assert outcomes[0].status is JobStatus.CANCELLED
    assert outcomes[0].value is None
    runner.wait_for_idle()
