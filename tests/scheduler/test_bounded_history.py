from collections.abc import Iterator
from unittest.mock import patch

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus
from scripts.events.protocol import StateStore
from scripts.scheduler.core import Scheduler

pytestmark = pytest.mark.feature("scheduler-bounded-history")


@pytest.fixture
def scheduler() -> Iterator[Scheduler]:
    bus = open_bus(BusConfig())
    try:
        yield Scheduler(bus.state("scheduler"), machine="headed")
    finally:
        bus.close()


def finished(scheduler: Scheduler, count: int) -> None:
    for _ in range(count):
        timer = scheduler.timer(owner="process", payload="old wait", delay=1, now=0)
        scheduler.update(timer.id, lambda row: row.update(state="fired"))


def test_abandoned_only_history_is_reaped_without_new_work(scheduler: Scheduler) -> None:
    with patch("scripts.scheduler.core.time.time", return_value=0):
        finished(scheduler, 32)
    assert len(scheduler.records()) == 32
    scheduler.run_due(now=10_000)
    assert scheduler.records() == []


def test_new_timer_prunes_abandoned_history_and_preserves_live_work(scheduler: Scheduler) -> None:
    with patch("scripts.scheduler.core.time.time", return_value=0):
        finished(scheduler, 32)
        active = scheduler.interval(owner="operator", payload="keep", every=60, now=0)
        active.start(now=0)
    with patch("scripts.scheduler.core.time.time", return_value=10_000):
        current = scheduler.timer(owner="process", payload="new wait", delay=1)
    assert {row["id"] for row in scheduler.records()} == {active.id, current.id}
    assert active.state == "active"


def test_timer_polling_reads_one_record(scheduler: Scheduler) -> None:
    timer = scheduler.timer(owner="process", payload="wait", delay=1)
    timer.start()
    state: StateStore = scheduler.state
    original = state.get

    def read(key: str) -> object:
        assert key != scheduler.name("entries"), "timer polling loaded the whole history"
        return original(key)

    with patch.object(state, "get", side_effect=read):
        assert timer.state == "active"
