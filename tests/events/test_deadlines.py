import pytest

from scripts.events.envelope import Envelope

pytestmark = pytest.mark.unit


def source(version=1):
    return Envelope(
        id=f"source-{version}",
        type="execution.idle",
        swarm="crew",
        entity="execution",
        key="execution-one",
        version=version,
        time=1000,
        author="lifecycle",
        data={},
    )


@pytest.mark.parametrize("kind", ["idle", "stale", "handoff"])
def test_shared_deadline_contract_has_kind_specific_identity_and_causal_expiry(kind):
    from scripts.events import deadlines

    event = source()
    request = deadlines.schedule(event, kind, 1200)
    assert request.type == "deadline.scheduled"
    assert request.entity == "timer"
    assert request.swarm == "crew"
    assert request.version == 1
    assert request.time == 1000
    assert request.author == "lifecycle"
    assert request.cause == event.id
    assert request.data == {"kind": kind, "entity": "execution", "key": "execution-one", "deadline_ms": 1200}
    assert request == deadlines.schedule(event, kind, 1200)
    assert len({deadlines.schedule(event, k, 1200).key for k in ["idle", "stale", "handoff"]}) == 3
    assert deadlines.subject(request) == "swarm.crew.deadline.scheduled"
    expiry = deadlines.expired(request)
    assert expiry.type == "deadline.expired"
    assert expiry.entity == request.entity
    assert expiry.key == request.key
    assert expiry.swarm == request.swarm
    assert expiry.version == request.version
    assert expiry.data == request.data
    assert expiry.time == 1200
    assert expiry.cause == request.id
    assert expiry.author == "timers"
    assert expiry.id != request.id
    assert expiry == deadlines.expired(request)
    cancellation = deadlines.cancel(source(2), kind)
    assert cancellation.key == request.key
    assert cancellation.version == 2
    assert cancellation.type == "deadline.cancelled"
    assert cancellation.data == {**request.data, "deadline_ms": None}


@pytest.mark.parametrize("deadline", [-1, True, "1200", 1.5, None])
def test_schedule_refuses_invalid_deadlines(deadline):
    from scripts.events import deadlines

    with pytest.raises(ValueError) as error:
        deadlines.schedule(source(), "idle", deadline)
    assert str(error.value) == "deadline must be a non negative integer"


def test_shared_contract_refuses_unknown_kinds():
    from scripts.events import deadlines

    with pytest.raises(ValueError) as error:
        deadlines.schedule(source(), "unknown", 1200)
    assert str(error.value) == "unknown deadline kind 'unknown'"
