from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.hub import runner
from scripts.swarm import readiness
from tests.chaos import hive

SWARM = "chaos"


def _offer(task, generation):
    return Envelope(
        id=readiness.offer_id(SWARM, {"id": task, "claim_generation": generation}),
        type="task.ready",
        swarm=SWARM,
        entity="task",
        key=task,
        version=generation,
        time=0,
        author="chaos",
        data={"lane": hive.LANE, "generation": generation},
    )


def _view(bus):
    return hive.readiness_view(SWARM, lambda: bus.state(hive.GENERATIONS))()


def test_a_first_offer_is_admitted_and_a_reopened_one_waits_for_its_generation():
    bus = open_bus(BusConfig(backend="memory"))
    view = _view(bus)

    assert readiness.withdrawal(_offer("t0011", 1), view) == ""
    assert readiness.withdrawal(_offer("t0011", 2), view) == "it is at generation 1"


def test_a_recorded_reopen_admits_the_next_generation_and_withdraws_the_old_one():
    bus = open_bus(BusConfig(backend="memory"))
    view = _view(bus)

    runner.record(bus.state(hive.GENERATIONS), "t0011", 2)

    assert readiness.withdrawal(_offer("t0011", 2), view) == ""
    assert readiness.withdrawal(_offer("t0011", 1), view) == "it is at generation 2"
    assert readiness.withdrawal(_offer("t0012", 1), view) == ""


def test_a_second_reopen_replaces_the_recorded_generation():
    bus = open_bus(BusConfig(backend="memory"))
    view = _view(bus)

    runner.record(bus.state(hive.GENERATIONS), "t0011", 2)
    runner.record(bus.state(hive.GENERATIONS), "t0011", 3)

    assert readiness.withdrawal(_offer("t0011", 3), view) == ""


def test_a_late_reopen_of_an_older_generation_keeps_the_newer_one():
    bus = open_bus(BusConfig(backend="memory"))
    view = _view(bus)

    runner.record(bus.state(hive.GENERATIONS), "t0011", 3)
    runner.record(bus.state(hive.GENERATIONS), "t0011", 2)

    assert readiness.withdrawal(_offer("t0011", 3), view) == ""
    assert readiness.withdrawal(_offer("t0011", 2), view) == "it is at generation 3"
