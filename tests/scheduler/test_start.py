from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "kind,options,deadline",
    [
        ("timer", {"delay": 10}, 130),
        ("stopwatch", {"limit": 10}, 130),
        ("timer", {"at": 110}, 110),
        ("cron", {"expression": "* * * * *"}, 180),
    ],
)
def test_start_uses_the_activation_clock_and_preserves_absolute_deadlines(monkeypatch, kind, options, deadline):
    import fakeredis

    from scripts.scheduler import core

    clock = [100]
    monkeypatch.setattr(core, "time", SimpleNamespace(time=lambda: clock[0]))
    scheduler = core.Scheduler(fakeredis.FakeRedis(decode_responses=True), machine="proof")
    entry = getattr(scheduler, kind)(owner="operator", payload="notice", **options)
    clock[0] = 120
    if kind == "stopwatch":
        assert entry.elapsed() == 0
    entry.start()
    assert entry.record["started_at"] == 120
    assert entry.record["due"] == deadline
    if deadline > 120:
        assert scheduler.run_due(now=deadline - 1) == 0
    fired_at = max(120, deadline)
    assert scheduler.run_due(now=fired_at) == 1
    assert scheduler.run_due(now=fired_at + 1) == 0
    if kind == "stopwatch":
        assert entry.elapsed(now=140) == 10
