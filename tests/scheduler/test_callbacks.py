import json

import pytest

pytestmark = pytest.mark.unit


def capture_signal(event, delivery_id, redis):
    redis.hset("proof:signals", delivery_id, json.dumps(event))


def test_a_module_function_connects_as_a_durable_named_slot():
    import fakeredis

    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import restore

    assert restore({"kind": "callable", "reference": f"{__name__}:capture_signal"}).record() == {
        "kind": "callable",
        "reference": f"{__name__}:capture_signal",
    }
    redis = fakeredis.FakeRedis(decode_responses=True)
    scheduler = Scheduler(redis, machine="proof")
    timer = scheduler.timer(owner="operator", delay=5, payload="call slot", now=100)
    timer.timeout.connect(capture_signal)
    timer.start(now=100)
    assert timer.timeout.name == "timeout"
    restarted = Scheduler(redis, machine="proof")
    assert restarted.run_due(now=110) == 1
    assert restarted.run_due(now=120) == 0
    events = list(redis.hgetall("proof:signals").values())
    assert len(events) == 1
    event = json.loads(events[0])
    assert event["signal"] == "timeout"
    assert event["owner"] == "operator"
    assert event["payload"] == "call slot"
    assert event["scheduled_at"] == 105
    assert event["fired_at"] == 110


def test_a_local_closure_cannot_be_connected_to_a_durable_signal():
    import fakeredis

    from scripts.scheduler.core import Scheduler

    timer = Scheduler(fakeredis.FakeRedis(decode_responses=True)).timer(
        owner="operator", delay=5, payload="test", now=100
    )

    def local_slot(*args):
        pass

    with pytest.raises(ValueError, match="^a slot must be an importable module function$"):
        timer.timeout.connect(local_slot)


def test_a_builtin_cannot_be_connected_as_a_durable_function_slot():
    import fakeredis

    from scripts.scheduler.core import Scheduler

    timer = Scheduler(fakeredis.FakeRedis(decode_responses=True)).timer(
        owner="operator", delay=1, payload="notice", now=0
    )
    with pytest.raises(ValueError, match="^a slot must be an importable module function$"):
        timer.timeout.connect(len)


def test_a_main_module_function_cannot_be_restored_in_the_runner(monkeypatch):
    import fakeredis

    from scripts.scheduler.core import Scheduler

    monkeypatch.setattr(capture_signal, "__module__", "__main__")
    timer = Scheduler(fakeredis.FakeRedis(decode_responses=True)).timer(
        owner="operator", delay=1, payload="notice", now=0
    )
    with pytest.raises(ValueError, match="^a slot must be an importable module function$"):
        timer.timeout.connect(capture_signal)
