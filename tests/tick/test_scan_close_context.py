"""A tick pass's process scan ends only in the context that opened it, so the slow lane survives closing it."""

import contextvars
from collections.abc import Callable
from threading import Event, Thread

import pytest

from scripts.swarm import reaper
from scripts.tick import engine, pool, sites
from scripts.tick.adapters.processes import PassProcesses
from scripts.tick.step import Context, Result
from tests.tick.test_handed_lane_guards import Step, _ctx, _never_shut

pytestmark = pytest.mark.unit

HOLD_S = 10.0
JOIN_S = 10.0


def _on_lane(lane: contextvars.Context, work: Callable[[], object]) -> list[BaseException]:
    raised: list[BaseException] = []

    def run() -> None:
        try:
            work()
        except BaseException as exc:
            raised.append(exc)

    thread = Thread(target=lane.run, args=(run,), name="tick-slow-lane")
    thread.start()
    thread.join(JOIN_S)
    assert not thread.is_alive()
    return raised


def _scanning() -> sites.TickSites:
    return sites.TickSites({}, {}, {}, lambda: 7, scanning=True)


def _close_on_the_lane(found: sites.TickSites, outer: PassProcesses) -> tuple[list[BaseException], bool, object]:
    reaper.PASS.set(outer)
    lane = contextvars.copy_context()
    assert found.hooks()["start"]() is True
    scanning = reaper.PASS.get()
    raised = _on_lane(lane, found.close)
    held = scanning is not outer and reaper.PASS.get() is scanning
    found.close()
    return raised, held, reaper.PASS.get()


@pytest.mark.feature("swarm-tick-scan-closes-in-its-own-context")
def test_a_scan_closed_from_the_slow_lane_context_stays_open_until_the_pass_that_opened_it_closes() -> None:
    outer = PassProcesses(dict)
    raised, held, after = contextvars.copy_context().run(_close_on_the_lane, _scanning(), outer)
    assert raised == []
    assert held
    assert after is outer


def _pass(
    found: sites.TickSites, relayed: Event
) -> tuple[tuple[engine.StepRecord, ...], list[tuple[engine.StepRecord, ...]], object]:
    def relay() -> bool:
        relayed.set()
        found.close()
        return True

    def hold(ctx: Context) -> Result:
        relayed.wait(HOLD_S)
        return Result()

    steps = (
        Step("first", "start"),
        Step("hold", "start", hold, budget_s=2 * HOLD_S),
        Step("relay step", "relay", needs=frozenset({"hold"})),
        Step("slow step", "late", lane=engine.SLOW, needs=frozenset({"first"})),
    )
    hooks = {"start": found.hooks()["start"], "relay": relay}
    gate = engine.SiteGate(hooks, _never_shut, sites=("start", "relay", "late"))
    later: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(workers=3, critical_s=3 * HOLD_S, slow_s=3 * HOLD_S):
        records = engine.run_graph(steps, _ctx(), finished=later.append, gate=gate)
    found.close()
    return records, later, reaper.PASS.get()


@pytest.mark.feature("swarm-tick-scan-closes-in-its-own-context")
def test_the_slow_lane_survives_opening_the_relay_hook_that_closes_the_pass_scan() -> None:
    relayed = Event()
    before = reaper.PASS.get()
    records, later, after = contextvars.copy_context().run(_pass, _scanning(), relayed)
    assert relayed.is_set()
    assert [(record.name, record.outcome) for record in later[0]] == [("slow step", engine.OK)]
    assert {record.name: record.outcome for record in records}["relay step"] == engine.OK
    assert after is before
