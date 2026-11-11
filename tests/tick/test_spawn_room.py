"""A lane reserve: the steps a reserved step needs run against their lane less its reserve, so it still starts."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from threading import Event

import pytest

from scripts.tick import engine, pool, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result
from scripts.tick.steps import budgets

pytestmark = pytest.mark.unit

CRITICAL = Lane("critical")


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class Step:
    name: str
    group: str
    action: Callable[[Context], Result] = field(default=lambda ctx: Result())
    needs: frozenset[str] = frozenset()
    lane: Lane = CRITICAL
    budget_s: float = 5.0
    interval_s: float = 0.0
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    effects: frozenset[str] = frozenset()
    feature: str = ""

    def run(self, ctx: Context) -> Result:
        return self.action(ctx)


def _ctx() -> Context:
    return Context(View(), Deadline.after(10.0))


def _stuck(release: Event) -> Callable[[Context], Result]:
    def run(ctx: Context) -> Result:
        release.wait(3.0)
        return Result()

    return run


def _left(seen: list[float]) -> Callable[[Context], Result]:
    def run(ctx: Context) -> Result:
        seen.append(ctx.deadline.remaining)
        return Result()

    return run


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_spawn_still_runs_with_its_reserve_when_the_steps_it_needs_are_slow() -> None:
    release, seen = Event(), []
    steps = (
        Step("reap", "agents", _stuck(release)),
        Step("quota notice", "agents", _stuck(release)),
        Step("spawn", "place", _left(seen), frozenset({"reap", "quota notice"})),
    )
    try:
        with pool.running(workers=4, critical_s=1.0, reserves={"spawn": 0.6}):
            records = {record.name: record for record in engine.run_graph(steps, _ctx())}
    finally:
        release.set()
    assert records["spawn"].outcome == engine.OK
    assert records["reap"].cut and not records["reap"].abandoned
    assert records["quota notice"].cut
    assert len(seen) == 1 and 0.0 < seen[0] <= 0.6


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_without_a_reserve_slow_needs_spend_the_lane_and_spawn_is_skipped() -> None:
    release = Event()
    steps = (Step("reap", "agents", _stuck(release)), Step("spawn", "place", needs=frozenset({"reap"})))
    try:
        with pool.running(workers=2, critical_s=0.3, reserves={}):
            records = {record.name: record for record in engine.run_graph(steps, _ctx())}
    finally:
        release.set()
    assert records["spawn"].skipped


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_a_step_spawn_does_not_need_keeps_the_whole_lane() -> None:
    release, seen = Event(), []
    steps = (
        Step("reap", "agents", _stuck(release)),
        Step("spawn", "place", needs=frozenset({"reap"})),
        Step("beside", "agents", _left(seen)),
        Step("after", "close", _left(seen), frozenset({"spawn"})),
    )
    try:
        with pool.running(workers=4, critical_s=1.0, reserves={"spawn": 0.6}):
            records = {record.name: record for record in engine.run_graph(steps, _ctx())}
    finally:
        release.set()
    assert records["beside"].outcome == engine.OK and records["after"].outcome == engine.OK
    assert seen[0] > 0.6


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_a_need_through_another_step_is_held_back_by_the_reserve_too() -> None:
    release, seen = Event(), []
    steps = (
        Step("collect", "agents", _stuck(release)),
        Step("reap", "agents", _left(seen), frozenset({"collect"})),
        Step("spawn", "place", needs=frozenset({"reap"})),
    )
    try:
        with pool.running(workers=4, critical_s=1.0, reserves={"spawn": 0.6}):
            records = {record.name: record for record in engine.run_graph(steps, _ctx())}
    finally:
        release.set()
    assert records["collect"].cut
    assert records["reap"].skipped and not seen
    assert records["spawn"].outcome == engine.OK


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_a_step_spawn_does_not_need_is_dropped_after_a_need_the_reserve_cut() -> None:
    release, seen = Event(), []
    steps = (
        Step("collect", "agents", _stuck(release)),
        Step("spawn", "place", needs=frozenset({"collect"})),
        Step("rank", "place", _left(seen), frozenset({"collect"})),
        Step("after rank", "close", _left(seen), frozenset({"rank"})),
    )
    try:
        with pool.running(workers=4, critical_s=1.0, reserves={"spawn": 0.6}):
            records = {record.name: record for record in engine.run_graph(steps, _ctx())}
    finally:
        release.set()
    assert records["spawn"].outcome == engine.OK
    assert records["rank"].skipped and records["after rank"].skipped and not seen


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_the_reserve_reaches_a_slow_step_of_an_earlier_site_through_its_site_edge() -> None:
    release, seen = Event(), []
    rows = frozenset({Domain("pass:rows")})
    steps = (Step("collect", "observe", _stuck(release), writes=rows), Step("spawn", "place", _left(seen), reads=rows))
    edged = registry.site_edges(steps, ("observe", "place"))
    try:
        with pool.running(workers=2, critical_s=1.0, reserves={"spawn": 0.6}):
            records = {record.name: record for record in engine.run_graph(edged, _ctx())}
    finally:
        release.set()
    assert records["collect"].cut
    assert records["spawn"].outcome == engine.OK
    assert len(seen) == 1 and seen[0] <= 0.6


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_spawn_starts_when_its_needs_are_cut_while_a_step_it_does_not_need_still_runs() -> None:
    release, seen = Event(), []
    steps = (
        Step("collect", "agents", _stuck(release)),
        Step("intent check", "agents", _stuck(release)),
        Step("reap", "agents", needs=frozenset({"collect"})),
        Step("spawn", "place", _left(seen), frozenset({"reap"})),
    )
    try:
        with pool.running(workers=4, critical_s=1.0, reserves={"spawn": 0.6}):
            records = {record.name: record for record in engine.run_graph(steps, _ctx())}
    finally:
        release.set()
    assert records["reap"].skipped
    assert records["spawn"].outcome == engine.OK
    assert len(seen) == 1 and 0.3 < seen[0] <= 0.6


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_a_pass_reserves_the_critical_lane_after_the_spawn_start_bound_for_spawn() -> None:
    with pool.running() as found:
        assert dict(found.reserves) == {"spawn": budgets.CRITICAL_S - budgets.SPAWN_START_S}
    assert budgets.SPAWN_START_S == 40.0


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_a_shorter_critical_lane_reserves_the_same_share_for_spawn() -> None:
    with pool.running(critical_s=3.0) as found:
        assert dict(found.reserves) == {"spawn": 1.0}
    assert dict(pool.reserved(0.6)) == {"spawn": pytest.approx(0.2)}


def critical_pass(lane: Deadline) -> pool.Pass:
    with pool.running() as found:
        return replace(found, critical=lane)


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_a_lane_less_its_reserve_keeps_the_lane_clock() -> None:
    lane = engine._lane_of(Step("a", "g"), critical_pass(Deadline(100.0, lambda: 90.0)), {"a": 4.0})
    assert lane.expires_at == 96.0
    assert lane.remaining == 6.0


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_a_step_whose_whole_lane_ran_out_drops_its_dependents_with_its_needs_intact() -> None:
    assert engine._drops(Step("a", "g"), set(), critical_pass(Deadline(100.0, lambda: 100.0)))
    assert not engine._drops(Step("a", "g"), set(), critical_pass(Deadline(100.0, lambda: 90.0)))


@pytest.mark.feature("swarm-tick-spawn-reserve")
def test_a_need_shared_by_two_reserved_steps_holds_back_the_larger_reserve() -> None:
    need = Step("a", "g")
    small = Step("small", "g", needs=frozenset({"a"}))
    large = Step("large", "g", needs=frozenset({"a"}))
    held = engine._held_back((need, small, large), {"small": 2.0, "large": 5.0})
    assert held == {"a": 5.0}
