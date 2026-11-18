"""The master drill retires the front door master of a scratch swarm, leaving its seat empty, and records the successor
the tick's enforcing night watch forces up through the tick's own master launch, on the claude ai login within thirty
minutes of a drill clock."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.gates import log as gate_log
from scripts.hive import reports
from scripts.inbox.seats import seat_address
from scripts.swarm import capacity, cli, master_drill, naming, reaper
from scripts.swarm.health import merge_stall, night_watch
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import MANUAL_SCALING, MASTER, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import Placed
from scripts.swarm_v2 import front_door
from scripts.swarm_v2 import masters as master_seats
from scripts.tick.steps import masters
from tests.swarm.client_class import client_class
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.unit

SCRATCH = "proof-abc123-eng-1"
LIVE = "live-swarm"
NOW = 1_800_000_000_000
MINUTE = 60_000
START = NOW - 30 * MINUTE
OLD = "master@abc123-0001"
NO_SLOT = "no hive for master advertises a claude slot"
QUIET = {"repo": "o/r", "queue": 0, "merged": 0, "failed": "none observed", "streak": 0, "head": 0}


class LoginRuntime(FakeRuntime):
    """The tick tests' fake runtime, launching each master on the route the test names after refusing the first
    ``refused`` placements it is asked for, the refusal a forced master start still meets."""

    def __init__(self, route: str, refused: int = 0) -> None:
        """Launch on ``route``, refuse the first ``refused`` placements and report every launch."""
        super().__init__()
        self.route, self.refused, self.reports, self.placement = route, refused, True, ""
        self.events: list[dict[str, Any]] = []
        self.placements: list[dict[str, Any]] = []

    def placement_refusal(self, lane: str, task: dict[str, Any]) -> str:
        """Record the placement asked for and refuse it while refused placements remain."""
        self.placements.append(task)
        return NO_SLOT if len(self.placements) <= self.refused else ""

    def reported(self, agent: AgentRecord) -> bool:
        """Whether a live launched agent reported, while the test lets launches report."""
        return self.reports and agent.name in self.live

    def spawn(self, config: SwarmConfig, lane: str, name: str, task: dict[str, Any]) -> Placed:
        """Launch through the fake runtime, record a ledger write by the launched agent and report the named route and
        placement."""
        placed: Placed = super().spawn(config, lane, name, task)
        self.events.append({"by": name, "at": NOW})
        return replace(placed, route_kind=self.route, placement=self.placement, harness=self.harness)


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
    return SimpleNamespace(store=store, ledger=ledger, runtime=runtime, clock=[], docs=[])


def drill(rig: SimpleNamespace, slug: str = SCRATCH, doc: dict[str, Any] | None = None) -> master_drill.Drill:
    """A drill on ``slug`` and ``doc``, by default a document with no tasks, through the tick's own night watch pass,
    recording the clock and the ledger document of every pass."""

    def watch_pass(*args: Any) -> list[str]:
        rig.clock.append(args[5])
        rig.docs.append(args[4])
        return masters._night_watch(*args)

    document = {"tasks": []} if doc is None else doc
    return master_drill.Drill(rig.store, slug, rig.ledger, rig.runtime, document, NOW, watch_pass)


def successor(rig: SimpleNamespace) -> AgentRecord:
    """The one front door master the drill left after retiring the old one."""
    (found,) = [
        agent
        for agent in rig.store.agents(SCRATCH)
        if agent.lane == MASTER
        and agent.name != OLD
        and agent.state != "finished"
        and front_door.holds(SCRATCH, agent.seat)
    ]
    return found


@pytest.mark.feature("master-drill")
def test_the_drill_retires_the_front_door_master_and_records_a_login_successor_forced_up_at_ten_minutes(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "scratchpad" / "agentihooks" / naming.scratch_folder(SCRATCH, MASTER)
    home.mkdir(parents=True)
    monkeypatch.setattr(reaper, "SCRATCH", tmp_path / "scratchpad")
    rig.store.update(SCRATCH, gates={"stall": "observe"})
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
    assert json.loads(rig.store.redis.get(rig.store.key(SCRATCH, "master-drill"))) == record
    assert rig.store.config(SCRATCH).gates == {"stall": "observe", "nightwatch": "enforce"}
    assert rig.runtime.homes[OLD] == [home.resolve()]
    assert rig.clock == [START + minute * MINUTE for minute in range(11)]
    assert rig.docs == [{"tasks": []}] * 11
    assert [source for source, _ in rig.ledger.alerts] == ["seat"]


@pytest.mark.feature("master-drill")
def test_a_successor_on_the_token_fallback_is_recorded_without_a_recovery(rig: SimpleNamespace) -> None:
    rig.runtime.placement = "fallback"
    record = master_drill.run(drill(rig))
    assert (record["seated_minute"], record["successor"]) == (10, successor(rig).name)
    assert (record["route"], record["recovered"]) == ("token fallback", False)


@pytest.mark.feature("master-drill")
def test_a_working_codex_successor_on_the_login_route_kind_is_recorded_without_a_recovery(rig: SimpleNamespace) -> None:
    rig.runtime.harness = "codex"
    record = master_drill.run(drill(rig))
    assert (record["seated_minute"], record["successor"]) == (10, successor(rig).name)
    assert (successor(rig).state, successor(rig).harness, successor(rig).route_kind) == (
        "working",
        "codex",
        "master-login",
    )
    assert (record["route"], record["recovered"]) == ("", False)


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
def test_only_unfinished_front_door_masters_are_retired_and_only_a_front_door_master_succeeds(
    rig: SimpleNamespace,
) -> None:
    second = seat_master(
        rig.store, rig.runtime, SCRATCH, "master@abc123-0002", seat_address(SCRATCH, master_seats.seat(2))
    )
    ended = seat_master(rig.store, rig.runtime, SCRATCH, "master@abc123-0000", front_door.seat(SCRATCH), "finished")
    rig.runtime.live.discard(ended)
    record = master_drill.run(drill(rig))
    names = {agent.name for agent in rig.store.agents(SCRATCH)}
    assert record["retired"] == [OLD] and {second, ended} <= names
    assert rig.runtime.killed == [OLD] and second in rig.runtime.live
    assert (record["seated_minute"], record["successor"]) == (10, successor(rig).name)


@pytest.mark.feature("master-drill")
def test_a_swarm_whose_master_retired_with_no_new_tasks_sleeps_through_the_drill(rig: SimpleNamespace) -> None:
    rig.store.redis.set(rig.store.key(SCRATCH, "master-retired-tasks"), json.dumps(["t1"]))
    record = master_drill.run(drill(rig, doc={"tasks": [{"id": "t1"}]}))
    assert (record["retired"], record["seated_minute"], record["recovered"]) == ([OLD], None, False)
    assert rig.runtime.masters == []


@pytest.mark.feature("master-drill")
def test_a_task_added_since_the_master_retired_wakes_the_swarm_for_the_drill(rig: SimpleNamespace) -> None:
    rig.store.redis.set(rig.store.key(SCRATCH, "master-retired-tasks"), json.dumps(["t1"]))
    record = master_drill.run(drill(rig, doc={"tasks": [{"id": "t1"}, {"id": "t2"}]}))
    assert (record["seated_minute"], record["recovered"]) == (10, True)


@pytest.mark.feature("master-drill")
def test_a_starting_successor_that_wrote_the_ledger_seats_without_a_login_recovery(rig: SimpleNamespace) -> None:
    rig.runtime.reports = False
    record = master_drill.run(drill(rig, doc={"tasks": [], "_meta": {"events": rig.runtime.events}}))
    found = successor(rig)
    assert found.state == "starting"
    assert (record["seated_minute"], record["successor"], record["route"], record["recovered"]) == (
        10,
        found.name,
        "",
        False,
    )


@pytest.mark.feature("master-drill")
def test_a_front_door_master_its_hive_reports_gone_is_dropped_without_a_runtime_retire(rig: SimpleNamespace) -> None:
    hived = rig.store.next_name(SCRATCH, MASTER, NOW - 60 * MINUTE)
    placed = AgentRecord(
        hived, MASTER, MASTER, seat=front_door.seat(SCRATCH), started_at=NOW - 60 * MINUTE, hive="hive-a"
    )
    rig.store.start_execution(SCRATCH, placed)
    report = {"observed_at": NOW, "names": ["eng@abc123-0009"]}
    rig.store.redis.set(reports.key("hive-a"), json.dumps(report))
    record = master_drill.run(drill(rig))
    assert sorted(record["retired"]) == sorted([OLD, hived])
    assert rig.runtime.killed == [OLD] and hived not in {agent.name for agent in rig.store.agents(SCRATCH)}
    assert (record["seated_minute"], record["recovered"]) == (10, True)


@pytest.mark.feature("master-drill")
def test_a_front_door_master_the_runtime_cannot_retire_refuses_the_drill(rig: SimpleNamespace) -> None:
    rig.runtime.stuck.add(OLD)
    with pytest.raises(SwarmError) as refused:
        master_drill.run(drill(rig))
    assert str(refused.value) == (
        f"the runtime could not retire the front door master {OLD} of {SCRATCH}; the master drill needs an empty seat"
    )
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
    assert str(refused.value) == f"{slug} is not a scratch proof swarm; the master drill never runs on a live swarm"
    assert name in rig.runtime.live and name in {agent.name for agent in rig.store.agents(slug)}
    assert "nightwatch" not in rig.store.config(slug).gates and rig.clock == []
    assert rig.store.redis.get(rig.store.key(slug, master_drill.KEY)) is None


@pytest.mark.feature("master-drill")
@pytest.mark.parametrize("state", ["stopped", "stopping"])
def test_the_drill_refuses_a_scratch_swarm_that_is_not_running_or_paused(rig: SimpleNamespace, state: str) -> None:
    rig.store.update(SCRATCH, state=state)
    with pytest.raises(SwarmError) as refused:
        master_drill.run(drill(rig))
    assert str(refused.value) == (
        f"scratch swarm {SCRATCH} is {state}; the master drill runs on a running or paused scratch swarm"
    )
    assert OLD in rig.runtime.live and "nightwatch" not in rig.store.config(SCRATCH).gates and rig.clock == []


@pytest.mark.feature("master-drill")
def test_the_master_drill_command_prints_the_record_of_a_drill_through_the_routed_runtime(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    routes: list[dict[str, Any]] = []
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient", client_class(rig.ledger))
    monkeypatch.setattr("scripts.swarm_v2.runtime.routed.routed", lambda **kwargs: routes.append(kwargs) or rig.runtime)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    args = cli.build_parser().parse_args([SCRATCH, "master-drill"])
    args.handler(rig.store, args)
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(rig.store.redis.get(rig.store.key(SCRATCH, master_drill.KEY)))
    assert (printed["at"], printed["seated_minute"], printed["recovered"]) == (NOW, 10, True)
    assert rig.ledger.read == [SCRATCH]
    assert [list(kwargs) for kwargs in routes] == [["herdr"]] and isinstance(routes[0]["herdr"], HerdrRuntime)
