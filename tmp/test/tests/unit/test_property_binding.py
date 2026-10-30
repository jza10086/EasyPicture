from uuid import uuid4

from easypicture.ui.property_binding import (
    EditTarget,
    InteractionCoordinator,
    PropertyBinding,
    PropertyEditSession,
)


def test_property_binding_rereads_after_deferred_feedback():
    current = [1]
    editing = [True]
    applied = []

    def synchronize(value):
        if editing[0]:
            return False
        applied.append(value)
        return True

    binding = PropertyBinding(lambda: current[0], synchronize)
    assert not binding.refresh()
    current[0] = 2
    editing[0] = False
    binding.finish()
    assert applied == [2]
    assert not binding.pending


def test_session_rejects_changed_document_and_clears_preview():
    target = [EditTarget(uuid4(), uuid4(), 0)]
    previews, committed, cleared = [], [], []
    session = PropertyEditSession(
        lambda: target[0],
        previews.append,
        lambda value: committed.append(value) or True,
        lambda: cleared.append(True),
    )
    session.begin(1)
    session.preview(2)
    target[0] = EditTarget(uuid4(), target[0].object_id, 0)
    assert not session.finish()
    assert committed == []
    assert previews == [2]
    assert cleared == [True]


def test_interaction_owner_is_exclusive_and_lifecycle_cancel_runs_once():
    coordinator = InteractionCoordinator()
    first, second = object(), object()
    cancelled = []
    assert coordinator.acquire(first, lambda: cancelled.append(first))
    assert not coordinator.acquire(second, lambda: cancelled.append(second))
    coordinator.release(second)
    assert coordinator.owner is first
    coordinator.cancel()
    coordinator.cancel()
    assert coordinator.owner is None
    assert cancelled == [first]


def test_invalid_final_text_discards_earlier_valid_preview():
    target = EditTarget(uuid4(), uuid4(), 0)
    previews, commits = [], []
    session = PropertyEditSession(
        lambda: target,
        previews.append,
        lambda value: commits.append(value) or True,
        lambda: None,
        validate=lambda value: bool(value.strip()),
    )
    session.begin("initial")
    assert session.preview("valid")
    assert not session.preview("")
    assert not session.finish()
    assert previews == ["valid"]
    assert commits == []


def test_text_owner_finishes_before_numeric_owner_begins():
    coordinator = InteractionCoordinator()
    text, numeric = object(), object()
    completed = []

    def finish():
        completed.append("text")
        coordinator.release(text)

    assert coordinator.acquire(text, lambda: None, finish=finish)
    assert coordinator.acquire(numeric, lambda: None)
    assert completed == ["text"]
    assert coordinator.owner is numeric
