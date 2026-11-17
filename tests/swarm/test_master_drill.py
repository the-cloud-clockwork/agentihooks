"""The master drill retires the front door master of a scratch swarm, leaving its seat empty, and records the successor
the tick's enforcing night watch forces up through the tick's own master launch, on the claude ai login within thirty
minutes of a drill clock."""

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.gates import log as gate_log
from scripts.inbox.seats import seat_address
from scripts.swarm import capacity, cli, master_drill
from scripts.swarm.health import merge_stall, night_watch
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import MANUAL_SCALING, MASTER, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import Placed
from scripts.swarm_v2 import front_door
from scripts.swarm_v2 import masters as master_seats
from scripts.tick.steps import masters
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.unit

SCRATCH = "proof-abc123-eng-1"
LIVE = "live-swarm"
NOW = 1_800_000_000_000
MINUTE = 60_000
START = NOW - 30 * MINUTE
OLD = "master@abc123-0001"
NO_SLOT = "no session slot for the master, waiting"
QUIET = {"repo": "o/r", "queue": 0, "merged": 0, "failed": "none observed", "streak": 0, "head": 0}


class LoginRuntime(FakeRuntime):
    """The tick tests' fake runtime, launching each master on the route the test names after refusing the first
    ``refused`` session slots it is asked for."""

    def __init__(self, route: str, refused: int = 0) -> None:
        """Launch on ``route`` and refuse the first ``refused`` slot checks."""
        super().__init__()
        self.route, self.refused = route, refused

    def has_capacity(self, config: SwarmConfig) -> bool:
        """Record the check and refuse it while refused checks remain."""
        self.capacity_for.append(config.slug)
        return len(self.capacity_for) > self.refused

    def spawn(self, config: SwarmConfig, lane: str, name: str, task: dict[str, Any]) -> Placed:
        """Launch through the fake runtime and report the named route."""
        placed: Placed = super().spawn(config, lane, name, task)
        return replace(placed, route_kind=self.route)


def seat_master(
    store: RedisStore, runtime: FakeRuntime, slug: str, name: str, seat: str, state: str = "working"
) -> str:
    """Store a live master named ``name`` in ``seat`` of ``slug`` and return its name."""
    record = AgentRecord(
        name,
        MASTER,
        MASTER,
        started_at=NOW - 60 * MINUTE,
        state=state,
        seat=seat,
        pane_id=f"pane-{name}",
        harness="claude",
        route_kind="master-login",
    )
    store.put_agent(slug, record)
    runtime.live.add(name)
    return name


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> SimpleNamespace:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    for slug, scratch in ((SCRATCH, True), (LIVE, False), ("proof-abc123-eng-2", False), ("scratch-swarm", True)):
        store.create(
            SwarmConfig(slug, str(tmp_path), max_eng=0, max_ci=0, max_plan=0, scaling=MANUAL_SCALING, scratch=scratch)
        )
        store.update(slug, state="running")
    ledger: Any = FakeLedger([])
    ledger.alerts = []
    ledger.read = []
    ledger.raise_alert = lambda slug, source, text: ledger.alerts.append((source, text))
    ledger.tasks = lambda slug: ledger.read.append(slug) or []
    runtime = LoginRuntime("master-login")
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path)
    monkeypatch.setattr(merge_stall, "observe", lambda repo, at: {**QUIET, "at": at})
    monkeypatch.setattr(capacity, "ready_work", lambda slug, store, doc: ({}, {"eng": [], "ci": [], "plan": []}))
    seat_master(store, runtime, SCRATCH, OLD, front_door.seat(SCRATCH))
    return SimpleNamespace(store=store, ledger=ledger, runtime=runtime, clock=[])


def drill(rig: SimpleNamespace, slug: str = SCRATCH) -> master_drill.Drill:
    """A drill on ``slug`` through the tick's own night watch pass, recording the clock of every pass."""

    def watch_pass(*args: Any) -> list[str]:
        rig.clock.append(args[5])
        return masters._night_watch(*args)

    return master_drill.Drill(rig.store, slug, rig.ledger, rig.runtime, {"tasks": []}, NOW, watch_pass)


def successor(rig: SimpleNamespace) -> AgentRecord:
    """The one front door master the drill left after retiring the old one."""
    (found,) = [
        agent
        for agent in rig.store.agents(SCRATCH)
        if agent.lane == MASTER and agent.name != OLD and front_door.holds(SCRATCH, agent.seat)
    ]
    return found


@pytest.mark.feature("master-drill")
def test_the_drill_retires_the_front_door_master_and_records_a_login_successor_forced_up_at_ten_minutes(
    rig: SimpleNamespace,
) -> None:
    record = master_drill.run(drill(rig))
    found = successor(rig)
    assert record == {
        "swarm": SCRATCH,
        "at": NOW,
        "from": START,
        "mode": "enforce",
        "retired": [OLD],
        "actions": record["actions"],
        "seated_minute": 10,
        "successor": found.name,
        "route": "claude ai login",
        "recovered": True,
    }
    assert f"spawned master {found.name}" in record["actions"]
    assert (found.seat, found.state, found.route_kind, found.started_at) == (
        front_door.seat(SCRATCH),
        "working",
        "master-login",
        START + 10 * MINUTE,
    )
    assert OLD in rig.runtime.killed and OLD not in {agent.name for agent in rig.store.agents(SCRATCH)}
    assert json.loads(rig.store.redis.get(rig.store.key(SCRATCH, master_drill.KEY))) == record
    assert rig.store.config(SCRATCH).gates["nightwatch"] == "enforce"
    assert rig.clock == [START + minute * MINUTE for minute in range(11)]
    assert [source for source, _ in rig.ledger.alerts] == ["seat"]


@pytest.mark.feature("master-drill")
def test_a_successor_on_the_token_fallback_is_recorded_without_a_recovery(rig: SimpleNamespace) -> None:
    rig.runtime.route = "subscription"
    record = master_drill.run(drill(rig))
    assert (record["seated_minute"], record["successor"]) == (10, successor(rig).name)
    assert (record["route"], record["recovered"]) == ("token fallback", False)


@pytest.mark.feature("master-drill")
def test_a_successor_forced_up_at_the_thirtieth_minute_still_recovers(rig: SimpleNamespace) -> None:
    rig.runtime.refused = 2
    record = master_drill.run(drill(rig))
    assert record["actions"].count(NO_SLOT) == 2
    assert (record["seated_minute"], record["route"], record["recovered"]) == (30, "claude ai login", True)
    assert successor(rig).started_at == NOW
    assert rig.clock == [START + minute * MINUTE for minute in range(31)]


@pytest.mark.feature("master-drill")
def test_a_seat_left_empty_for_thirty_minutes_records_no_successor(rig: SimpleNamespace) -> None:
    rig.runtime.refused = 99
    record = master_drill.run(drill(rig))
    assert record["actions"].count(NO_SLOT) == 3
    assert (record["seated_minute"], record["successor"], record["route"], record["recovered"]) == (
        None,
        "",
        "",
        False,
    )
    assert rig.clock == [START + minute * MINUTE for minute in range(31)]
    assert json.loads(rig.store.redis.get(rig.store.key(SCRATCH, master_drill.KEY))) == record


@pytest.mark.feature("master-drill")
def test_the_drill_starts_the_night_watch_seat_episode_over(rig: SimpleNamespace) -> None:
    spent = [NOW - minute * MINUTE for minute in (3, 2, 1)]
    stale = {"at": NOW, night_watch.SEAT: {"since": NOW - 120 * MINUTE, "respawned": NOW}, "respawns": spent}
    rig.store.redis.set(rig.store.key(SCRATCH, night_watch.KEY), json.dumps(stale))
    record = master_drill.run(drill(rig))
    assert (record["seated_minute"], record["recovered"]) == (10, True)


@pytest.mark.feature("master-drill")
def test_the_drill_drives_a_paused_scratch_swarm_and_leaves_it_paused(rig: SimpleNamespace) -> None:
    rig.store.update(SCRATCH, state="paused")
    record = master_drill.run(drill(rig))
    assert (record["seated_minute"], record["recovered"]) == (10, True)
    assert rig.store.config(SCRATCH).state == "paused"


@pytest.mark.feature("master-drill")
def test_only_live_front_door_masters_are_retired_and_only_a_front_door_master_succeeds(rig: SimpleNamespace) -> None:
    second = seat_master(
        rig.store, rig.runtime, SCRATCH, "master@abc123-0002", seat_address(SCRATCH, master_seats.seat(2))
    )
    record = master_drill.run(drill(rig))
    names = {agent.name for agent in rig.store.agents(SCRATCH)}
    assert record["retired"] == [OLD] and second in names and second not in rig.runtime.killed
    assert (record["seated_minute"], record["successor"]) == (10, successor(rig).name)


@pytest.mark.feature("master-drill")
def test_a_front_door_master_the_runtime_cannot_retire_refuses_the_drill(rig: SimpleNamespace) -> None:
    rig.runtime.stuck.add(OLD)
    with pytest.raises(SwarmError) as refused:
        master_drill.run(drill(rig))
    assert str(refused.value) == master_drill.NOT_RETIRED.format(name=OLD, slug=SCRATCH)
    assert OLD in {agent.name for agent in rig.store.agents(SCRATCH)}
    assert rig.clock == [] and rig.store.redis.get(rig.store.key(SCRATCH, master_drill.KEY)) is None


@pytest.mark.feature("master-drill")
@pytest.mark.parametrize("slug", [LIVE, "proof-abc123-eng-2", "scratch-swarm"])
def test_the_drill_refuses_a_swarm_that_is_not_a_scratch_proof_swarm_and_leaves_it_untouched(
    rig: SimpleNamespace, slug: str
) -> None:
    name = seat_master(rig.store, rig.runtime, slug, f"master@{slug}", front_door.seat(slug))
    with pytest.raises(SwarmError) as refused:
        master_drill.run(drill(rig, slug))
    assert str(refused.value) == master_drill.NOT_SCRATCH.format(slug=slug)
    assert name in rig.runtime.live and name in {agent.name for agent in rig.store.agents(slug)}
    assert "nightwatch" not in rig.store.config(slug).gates and rig.clock == []
    assert rig.store.redis.get(rig.store.key(slug, master_drill.KEY)) is None


@pytest.mark.feature("master-drill")
@pytest.mark.parametrize("state", ["stopped", "stopping"])
def test_the_drill_refuses_a_scratch_swarm_that_is_not_running_or_paused(rig: SimpleNamespace, state: str) -> None:
    rig.store.update(SCRATCH, state=state)
    with pytest.raises(SwarmError) as refused:
        master_drill.run(drill(rig))
    assert str(refused.value) == master_drill.NOT_RUNNING.format(slug=SCRATCH, state=state)
    assert OLD in rig.runtime.live and "nightwatch" not in rig.store.config(SCRATCH).gates and rig.clock == []


@pytest.mark.feature("master-drill")
def test_the_master_drill_command_prints_the_record_of_a_drill_through_the_routed_runtime(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    routes: list[dict[str, Any]] = []
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient", lambda: rig.ledger)
    monkeypatch.setattr("scripts.swarm_v2.runtime.routed.routed", lambda **kwargs: routes.append(kwargs) or rig.runtime)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    args = cli.build_parser().parse_args([SCRATCH, "master-drill"])
    args.handler(rig.store, args)
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(rig.store.redis.get(rig.store.key(SCRATCH, master_drill.KEY)))
    assert (printed["at"], printed["seated_minute"], printed["recovered"]) == (NOW, 10, True)
    assert rig.ledger.read == [SCRATCH]
    assert [list(kwargs) for kwargs in routes] == [["herdr"]] and isinstance(routes[0]["herdr"], HerdrRuntime)
